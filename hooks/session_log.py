#!/usr/bin/env python3
"""
session_log.py — Claude Code SessionEnd hook (증분 진행 로그 재설계).

세션(=프로젝트)을 유지하며 긴 작업을 이어가도, '활동한 날짜'에 정확히 기록한다.
매 SessionEnd에서 '지난 처리 이후 새 turn만' 요약해 append(덮어쓰기 X).

산출물:
  - topics/<slug>.md              : 주제의 정본 (결론 · 접은 안 · 진행 로그 · 다음 + 재개 좌표)
  - conversations/<sid8>_<날짜>.md : 그날 대화 원문 (주제가 없으면 결론도 여기 머리말로)
  - daily/<활동일>.md              : 그날 진행 한 줄
  - weekly/<ISO주차>.md            : 주간 다이제스트 (daily 기반 집계)
  - INDEX.md / 완료 아카이브.md     : 목차이자 할 일의 정본 (체크박스 완료, 2주 뒤 아카이브)

증분 상태(세션별 처리한 turn 수)는 SQLite(sessionlog.db)에 저장.
특정 세션 제외: user가 #nolog / #기록제외 / #skiplog 를 치면 기록 안 함.

사용:
  실제 hook :  cat <stdin-json> | session_log.py
  워커      :  session_log.py --worker <transcript.jsonl>
  dry-run   :  session_log.py --dry-run[-llm] <transcript.jsonl> [--out <dir>]
"""

import os
import sys
import json
import re
import glob
import fcntl
import difflib
import sqlite3
import contextlib
import shlex
import subprocess
from datetime import datetime, timedelta

# ── 설정값 ──────────────────────────────────────────────────────────
VAULT = os.environ.get("OBSIDIAN_VAULT") or os.path.expanduser("~/Documents/Obsidian")
SUMMARY_MODEL = os.environ.get("SESSIONLOG_MODEL", "claude-sonnet-4-6")
INDEX_FILENAME = os.environ.get("SESSIONLOG_INDEX_FILE", "INDEX.md")
DONE_RETAIN_DAYS = 14
ARCHIVE_FILENAME = "완료 아카이브.md"
TASKS_DONE_HEADER = f"## ✅ 완료 ({DONE_RETAIN_DAYS // 7}주 보관)"
GUARD_ENV = "CLAUDE_SESSIONLOG_RUNNING"

# 요약 프롬프트의 첫 문장. 요약기 자신의 `claude -p` 세션을 판별하는 데도 쓴다(_is_summarizer_session).
SUMMARY_SIGNATURE = "너는 내 작업 기록 비서다"

MAX_TOOL_RESULT_CHARS = 280
CLAUDE_TIMEOUT_SEC = 300

MIN_USER_CHARS = 12
MIN_TOTAL_CHARS = 60
EXCLUDE_MARKERS = ("#nolog", "#기록제외", "#skiplog")   # **세션 전체** 제외 (증분이 아니다)
# 태스크만 만들지 않는다. 기록(진행 로그·대화·주제 매칭)은 그대로 간다.
# EXCLUDE_MARKERS 와 역할이 다르다 — 이쪽은 '한 일은 남기되 할 일은 안 만든다'.
TASK_SKIP_MARKERS = ("#완료", "#done")
SUMMARY_CHAR_BUDGET = 15000
SUMMARY_TURN_MAX = 2000
SUMMARY_MAX_TRIES = 2

# ── 붙여넣기 대응 ───────────────────────────────────────────────────
# '#로그' 단독 줄 → 그 줄부터 메시지 끝까지 제외(줄 수·크기 무관).
# EXCLUDE_MARKERS(세션 전체 제외)와 역할이 다르다: 이쪽은 메시지 단위.
LOG_MARKER_RE = re.compile(r"^[ \t]*#로그[ \t]*$", re.M)
# 마커와 펜스 사이의 **빈 줄을 허용한다.** 붙여넣다 보면 한 줄이 끼는데, 그러면 펜스 형태로
# 인식되지 않고 '그 뒤 전부 삭제' 로 넘어가 **뒤에 쓴 질문까지 사라진다**(실측).
# 펜스 자체의 들여쓰기와 3개 이상의 백틱도 받는다.
LOG_FENCED_RE = re.compile(
    r"^[ \t]*#로그[ \t]*\n(?:[ \t]*\n)*[ \t]*`{3,}[^\n]*\n.*?\n[ \t]*`{3,}[ \t]*$",
    re.M | re.S)
# turn당 상한. 실측 User 발화 99백분위 = 19,392자 → 정상 발화 무영향, 전체의 0.20%만 대상
PASTE_CAP = 20000
TOOL_LINE_PREFIXES = ("⌘ ", "→ 도구", "← 도구")

# ── 상태 파일 위치 ──────────────────────────────────────────────────
# 증분 마커 DB·락·디버그 로그가 있는 곳. **코드 위치와 분리한다.**
# 스크립트를 어디에 두든(레포 clone 위치가 바뀌어도) 같은 sessionlog.db 를 계속 쓰기 위함.
# ⚠️ 이 경로가 바뀌면 증분 마커가 초기화되어 전 세션이 재요약되고 진행 로그가 중복된다.
STATE_DIR = os.environ.get("SESSIONLOG_STATE_DIR") or os.path.expanduser("~/.claude/hooks")
os.makedirs(STATE_DIR, exist_ok=True)

LOCK_FILE = os.path.join(STATE_DIR, ".sessionlog.lock")
TASK_BACKUP_KEEP = 10
DEBUG_LOG_DIR = STATE_DIR
DEBUG_LOG_KEEP_DAYS = 7

DB_FILE = os.path.join(STATE_DIR, "sessionlog.db")
CONV_DIRNAME = "conversations"
PROGRESS_HEADER = "## 📈 진행 로그"
NEXT_HEADER = "## 🔜 다음"

# ── 주제축 (topics/) ────────────────────────────────────────────────
TOPICS_DIRNAME = "topics"
TASK_TITLE_MAX = 80
CONCLUSION_HEADER = "## 📌 결론"
DROPPED_HEADER = "## ❌ 접은 안"


# ── 디버그 로그 ─────────────────────────────────────────────────────
def _atomic_write(path, text):
    """tmp 에 쓰고 rename. 도중에 죽어도 원본이 잘린 채 남지 않는다.
    topics/ 는 자체 백업이 없어(태스크 백업은 INDEX 전용) 손상되면 git 뿐이다."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _debug(msg):
    try:
        now = datetime.now()
        path = os.path.join(DEBUG_LOG_DIR, f"sessionlog_debug_{now:%Y-%m-%d}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{now.isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _rotate_debug_logs():
    try:
        cutoff = datetime.now().timestamp() - DEBUG_LOG_KEEP_DAYS * 86400
        for p in glob.glob(os.path.join(DEBUG_LOG_DIR, "sessionlog_debug_*.log")):
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except Exception:
        pass


# ── SQLite 증분 상태 ────────────────────────────────────────────────
def _db(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS session_state "
                 "(session_id TEXT PRIMARY KEY, processed_turns INTEGER)")
    return conn


def db_get_processed(sid, db_path=DB_FILE):
    """실패하면 **None**. 0 을 주면 '처음부터'가 되어 전 구간을 다시 요약하고
    대화·daily 에 중복 append 한다 — DB 장애 한 번이 데이터 오염으로 증폭된다."""
    try:
        with _db(db_path) as c:
            r = c.execute("SELECT processed_turns FROM session_state WHERE session_id=?",
                          (sid,)).fetchone()
            return r[0] if r else 0
    except Exception as e:
        _debug("db_get ERROR: " + repr(e))
        return None


def db_set_processed(sid, n, db_path=DB_FILE):
    """성공 여부를 돌려준다. 실패한 채 'DONE' 을 남기면 다음 실행이 같은 구간을 다시 처리한다."""
    try:
        with _db(db_path) as c:
            c.execute("INSERT INTO session_state(session_id, processed_turns) VALUES(?,?) "
                      "ON CONFLICT(session_id) DO UPDATE SET processed_turns=?", (sid, n, n))
        return True
    except Exception as e:
        _debug("db_set ERROR: " + repr(e))
        return False


def _alert_path(db_path=DB_FILE):
    """**그 DB 옆에** 둔다. 전역 파일로 두면 별도 DB 를 쓰는 dry-run 이
    운영 경고를 지운다. 뜻은 '이 저장소에 쓰기가 안 된다' 하나다."""
    return f"{db_path}.alert"


def _alert_set(msg, db_path=DB_FILE):
    """**DB 밖에** 남기는 경고. DB 가 죽으면 pending 도 못 쓰는데, 렌더가 DB 만 보면
    그 실패는 다음 렌더 한 번으로 사라진다(체크박스 한 번이면 지워진다)."""
    try:
        with open(_alert_path(db_path), "w", encoding="utf-8") as f:
            f.write(msg.strip() + "\n")
    except OSError:
        pass


def _alert_get(db_path=DB_FILE):
    try:
        with open(_alert_path(db_path), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _alert_clear(db_path=DB_FILE):
    try:
        os.remove(_alert_path(db_path))
    except OSError:
        pass


# ── 실패한 세션 재시도 큐 ───────────────────────────────────────────────────
def _db_pending(db_path):
    c = _db(db_path)
    c.execute("CREATE TABLE IF NOT EXISTS pending "
              "(transcript TEXT PRIMARY KEY, ts TEXT, tries INTEGER)")
    return c


def _pending_add(path, db_path=DB_FILE):
    """요약이 실패한 트랜스크립트를 적어 둔다.

    자동 catchup 이 없어서(TCC 로 launchd 가 거부됨) 실패한 세션은 **다시 실행될 계기가 없다.**
    수동 `--catchup` 전까지 INDEX 에서 영영 빠지는데 사용자에게 보이지도 않는다."""
    try:
        with _db_pending(db_path) as c:
            c.execute("INSERT INTO pending(transcript, ts, tries) VALUES(?,?,1) "
                      "ON CONFLICT(transcript) DO UPDATE SET ts=?, tries=tries+1",
                      (path, datetime.now().isoformat(timespec="seconds"),
                       datetime.now().isoformat(timespec="seconds")))
    except Exception as e:
        _debug("pending_add ERROR: " + repr(e))


def _pending_clear(path, db_path=DB_FILE):
    try:
        with _db_pending(db_path) as c:
            c.execute("DELETE FROM pending WHERE transcript=?", (path,))
    except Exception as e:
        _debug("pending_clear ERROR: " + repr(e))


PENDING_MAX_TRIES = 5
PENDING_DRAIN_PER_RUN = 2      # 백로그가 현재 세션 기록을 굶기지 않게


def _pending_rows(db_path=DB_FILE):
    """[(transcript, tries)] — 원본이 남아 있는 것만. **원본이 사라진 항목은 지운다**
    (트랜스크립트는 30일 뒤 삭제된다). 남겨 두면 처리할 수 없는 건수가 경고에 붙박이가 된다.

    **조회 실패는 None** 이다. 빈 리스트로 뭉개면 DB 가 죽었는데 '남은 일 없음' 이 되어
    복구 자동화가 거짓 성공한다."""
    try:
        with _db_pending(db_path) as c:
            rows = c.execute("SELECT transcript, tries FROM pending ORDER BY ts").fetchall()
            gone = [t for t, _ in rows if not os.path.exists(t)]
            for g in gone:
                c.execute("DELETE FROM pending WHERE transcript=?", (g,))
            if gone:
                _debug(f"[worker] pending {len(gone)}건 원본 만료 — 정리")
            return [(t, n) for t, n in rows if t not in gone]
    except Exception as e:
        _debug("pending_rows ERROR: " + repr(e))
        return None


def _pending_list(db_path=DB_FILE):
    """**자동 재시도 대상**. 상한을 넘긴 것은 여기서 빠지지만 경고에서는 빠지지 않는다 —
    자동 재시도를 멈추는 것과 없던 일로 만드는 것은 다르다."""
    return [t for t, n in (_pending_rows(db_path) or []) if n < PENDING_MAX_TRIES]


def _db_usage(db_path):
    c = _db(db_path)
    c.execute("CREATE TABLE IF NOT EXISTS llm_usage ("
              "ts TEXT, session_id TEXT, date TEXT, topic TEXT, model TEXT,"
              "prompt_chars INT, conv_chars INT, tasks_chars INT, topics_chars INT, instr_chars INT,"
              "input_tokens INT, output_tokens INT, cache_read INT, cache_write INT,"
              "cost_usd REAL, elapsed_ms INT)")
    return c


def _log_usage(db_path, sid, date, topic, parts, usage):
    """콜 1회의 비용과 프롬프트 구성을 남긴다. 실패해도 본 작업을 막지 않는다."""
    if not usage:
        return
    try:
        with _db_usage(db_path) as c:
            c.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                datetime.now().isoformat(timespec="seconds"), sid, date, topic or "none", SUMMARY_MODEL,
                parts.get("prompt_chars"), parts.get("conv_chars"), parts.get("tasks_chars"),
                parts.get("topics_chars"), parts.get("instr_chars"),
                usage.get("input_tokens"), usage.get("output_tokens"),
                usage.get("cache_read"), usage.get("cache_write"),
                usage.get("cost_usd"), usage.get("elapsed_ms")))
    except Exception as e:
        _debug("usage 기록 실패: " + repr(e))


def _db_tasks(db_path):
    c = _db(db_path)
    c.execute("CREATE TABLE IF NOT EXISTS task_snapshot (task_key TEXT PRIMARY KEY, status TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS task_events (task_key TEXT, status TEXT, ts TEXT)")
    return c


def _task_states(md):
    """작업현황 마크다운 → {task_key: 'open'|'done'}."""
    states = {}
    for ln in (md or "").splitlines():
        low = ln.lstrip().lower()
        if low.startswith("- [ ]"):
            states[_task_key(ln)] = "open"
        elif low.startswith("- [x]"):
            states[_task_key(ln)] = "done"
    return states


TASK_KEY_VERSION = 2      # 2: 키에 주제 슬러그 포함


def _task_key_migrate(base, db_path):
    """키 규칙이 바뀌면 **이벤트를 만들지 않고** snapshot 만 다시 세운다.

    안 하면 이미 완료된 항목 전부가 '오늘 새로 완료' 로 기록되고, 과거 완료 이벤트는
    새 키로 조회되지 않아 완료일이 오늘로 바뀐다."""
    try:
        with _db_tasks(db_path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
            r = c.execute("SELECT v FROM meta WHERE k='task_key_version'").fetchone()
            if r and int(r[0]) >= TASK_KEY_VERSION:
                return
            tf = os.path.join(base, INDEX_FILENAME)
            cur = _task_states(open(tf, encoding="utf-8").read()) if os.path.exists(tf) else {}
            c.execute("DELETE FROM task_snapshot")
            c.executemany("INSERT INTO task_snapshot(task_key, status) VALUES(?,?)",
                          list(cur.items()))
            c.execute("INSERT INTO meta(k, v) VALUES('task_key_version', ?) "
                      "ON CONFLICT(k) DO UPDATE SET v=?",
                      (str(TASK_KEY_VERSION), str(TASK_KEY_VERSION)))
            _debug(f"[worker] task_key v{TASK_KEY_VERSION} 로 snapshot 재구축 ({len(cur)}건, 이벤트 없음)")
    except Exception as e:
        _debug("task_key_migrate ERROR: " + repr(e))


def _sync_task_states(base, db_path=DB_FILE):
    """작업현황 현재 상태 vs snapshot diff → open→done 전이를 그 시각으로 task_events에 기록.
    FileChanged hook(외부 편집 즉시) + SessionEnd(폴백) 양쪽에서 호출."""
    _task_key_migrate(base, db_path)
    tf = os.path.join(base, INDEX_FILENAME)
    if not os.path.exists(tf):
        return
    cur = _task_states(open(tf, encoding="utf-8").read())
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with _db_tasks(db_path) as c:
            prev = dict(c.execute("SELECT task_key, status FROM task_snapshot").fetchall())
            for k, st in cur.items():
                if st == "done" and prev.get(k) != "done":
                    c.execute("INSERT INTO task_events(task_key, status, ts) VALUES(?,?,?)",
                              (k, "done", now))
            c.execute("DELETE FROM task_snapshot")
            c.executemany("INSERT INTO task_snapshot(task_key, status) VALUES(?,?)",
                          list(cur.items()))
    except Exception as e:
        _debug("sync_task ERROR: " + repr(e))


def _completion_date(task_key, fallback, db_path=DB_FILE):
    """task_events에서 실제 완료 시각(날짜) 조회. 없으면 fallback."""
    try:
        with _db_tasks(db_path) as c:
            # 최신 완료를 쓴다. 체크를 풀었다 다시 체크하면 **이번** 완료일이 맞다.
            r = c.execute("SELECT ts FROM task_events WHERE task_key=? AND status='done' "
                          "ORDER BY ts DESC LIMIT 1", (task_key,)).fetchone()
            if r and r[0]:
                return r[0][:10]
    except Exception:
        pass
    return fallback


# ── 트랜스크립트 파싱 ───────────────────────────────────────────────
def _strip_command_noise(s):
    s = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "", s, flags=re.S)
    s = re.sub(r"<command-message>.*?</command-message>", "", s, flags=re.S)
    s = re.sub(r"<command-args>.*?</command-args>", "", s, flags=re.S)
    s = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", s, flags=re.S)
    s = re.sub(r"<system-reminder>.*?</system-reminder>", "", s, flags=re.S)
    s = re.sub(r"<command-name>(.*?)</command-name>", r"⌘ \1", s, flags=re.S)
    return s.strip()


def _text_from_content(content):
    if isinstance(content, str):
        cleaned = _strip_command_noise(content)
        return [cleaned] if cleaned.strip() else []
    lines = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text", "").strip()
                if t:
                    lines.append(t)
            elif btype == "thinking":
                continue
            elif btype == "tool_use":
                lines.append(f"→ 도구 호출: {block.get('name', '?')}")
            elif btype == "tool_result":
                raw = block.get("content", "")
                if isinstance(raw, list):
                    raw = " ".join(b.get("text", "") for b in raw if isinstance(b, dict))
                raw = str(raw).replace("\n", " ").strip()
                if len(raw) > MAX_TOOL_RESULT_CHARS:
                    raw = raw[:MAX_TOOL_RESULT_CHARS] + f"… (총 {len(raw)}자)"
                lines.append(f"← 도구 결과: {raw}" if raw else "← 도구 결과")
    return lines


# ── 렌더 공용 필터 ──────────────────────────────────────────────────
# 주의: 여기서 거르고 _text_from_content 는 건드리지 않는다.
# _text_from_content 를 고치면 도구만 있던 turn 이 `if not lines: continue`(아래)로
# 사라져 turn 수가 8,810→1,853 수준으로 줄고, 증분 마커(processed_turns)가 리셋되어
# 세션 전량 재요약 + 진행 로그 중복 append 가 발생한다.
def _strip_log_blocks(text):
    """'#로그' 마커 적용.

    - 펜스 형태(```#로그 … ```)는 그 블록만 제외하고 뒤 내용을 보존
    - 단독 마커 줄은 그 줄부터 메시지 끝까지 제외 (줄 수·크기 무관)
    """
    if "#로그" not in text:
        return text
    text = LOG_FENCED_RE.sub("…(#로그 블록 생략)", text)
    m = LOG_MARKER_RE.search(text)
    if m:
        dropped = len(text) - m.start()
        text = text[:m.start()] + f"…(#로그 이후 {dropped:,}자 생략)"
    return text.strip()


def _clean_lines(lines):
    """도구 라인 제거 + '#로그' 블록 제거. 저장(_render_turns)·요약·게이트 공용."""
    out = []
    for ln in lines:
        if ln.startswith(TOOL_LINE_PREFIXES):
            continue
        s = _strip_log_blocks(ln)
        if s.strip():
            out.append(s)
    return out


def _capped(body, cap=PASTE_CAP):
    """turn 하나가 상한을 넘으면 잘라내고 생략 표시를 남긴다."""
    if cap and len(body) > cap:
        return body[:cap] + f"\n\n…({len(body) - cap:,}자 생략 — 붙여넣기 {cap:,}자 상한)"
    return body


def parse_transcript(path):
    title = None
    turns = []  # (role, [lines], ts)
    first_ts = last_ts = None
    cwd = git_branch = None
    in_tok = out_tok = 0

    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t == "ai-title":
                title = o.get("aiTitle") or title
                continue
            if t not in ("user", "assistant"):
                continue
            ts = o.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            cwd = o.get("cwd") or cwd
            git_branch = o.get("gitBranch") or git_branch
            msg = o.get("message") or {}
            role = msg.get("role", t)
            lines = _text_from_content(msg.get("content"))
            if role == "assistant":
                usage = msg.get("usage") or {}
                in_tok = max(in_tok, usage.get("input_tokens", 0) or 0)
                out_tok += usage.get("output_tokens", 0) or 0
            if not lines:
                continue
            turns.append((role, lines, ts))

    return {
        "title": title, "turns": turns,
        "first_ts": first_ts, "last_ts": last_ts,
        "cwd": cwd, "git_branch": git_branch,
        "in_tok": in_tok, "out_tok": out_tok,
        "session_id": os.path.splitext(os.path.basename(path))[0],
    }


# ── 게이트 ──────────────────────────────────────────────────────────
def _significance(meta):
    """도구 라인과 '#로그' 블록을 제외한 실질 분량으로 판정.
    로그만 붙여넣은 세션이 게이트를 통과하는 것을 막는다."""
    total = real_user = 0
    for role, lines, _ in meta["turns"]:
        for ln in _clean_lines(lines):
            t = ln.strip()
            total += len(t)
            if role == "user":
                real_user += len(t)
    return total, real_user


def is_significant(meta):
    total, real_user = _significance(meta)
    return real_user >= MIN_USER_CHARS and total >= MIN_TOTAL_CHARS


# compact 요약과 백그라운드 작업 알림은 **시스템이 주입한 텍스트**다. 마커를 인용하고 있어도
# 사용자가 친 것이 아니다 — 실측: 마커 오탐 4건이 전부 이 둘이었다.
COMPACT_PREAMBLE = "This session is being continued from a previous conversation"
TASK_NOTE_RE = re.compile(r"<task-notification>.*?</task-notification>", re.S)


def _marker_text(ln):
    """마커를 찾을 때만 쓰는 정제본.

    `_text_from_content` 는 **건드리지 않는다** — 거기서 줄을 지우면 도구만 있던 turn 이
    사라져 turn 수가 줄고, 증분 마커가 리셋되어 전 세션이 재요약된다(진행 로그 중복).
    """
    if ln.lstrip().startswith(COMPACT_PREAMBLE):
        return ""
    return TASK_NOTE_RE.sub(" ", ln)


def _has_marker(meta, markers):
    """user 발화에서 마커를 찾는다. **마커만 있는 줄**일 때만 인정한다.

    부분일치는 마커를 *이야기하는* 문장까지 잡는다 — `'#nolog 는 세션 로깅용 마커로 보이는데'`
    한 줄이 그 세션의 기록을 통째로 날린다. 실측: 실제 사용 47건이 전부 단독 줄이었고,
    문장 속 12건은 대부분 마커를 설명하는 대화였다.

    놓치는 쪽(`작업 #완료 했어` 처럼 문장에 섞어 친 경우)의 대가는 **눈에 보이는** 태스크
    몇 줄이고, 잡는 쪽의 대가는 **소리 없는** 기록 유실이다. 비용이 대칭이 아니다.
    도구 라인은 사용자가 친 것이 아니므로 제외한다.
    """
    pats = [re.compile(rf"^\s*{re.escape(m)}\s*$", re.I | re.M) for m in markers]
    for role, lines, _ in meta["turns"]:
        if role != "user":
            continue
        for ln in lines:
            if ln.startswith(("← 도구", "→ 도구", "⌘ ")):
                continue
            t = _marker_text(ln)
            if t and any(p.search(t) for p in pats):
                return True
    return False


def _is_excluded(meta):
    return _has_marker(meta, EXCLUDE_MARKERS)


def _is_task_skipped(meta):
    """'#완료' — 태스크만 만들지 않는다.

    판정을 LLM 에게 묻지 않는 이유는 아래 주석과 같다(비결정적).
    #nolog 와 달리 기록은 그대로 남는다 — 한 일은 남기되 할 일만 안 만든다.
    """
    return _has_marker(meta, TASK_SKIP_MARKERS)


# 기록 여부는 기계 게이트(is_significant)만으로 결정한다.
# LLM 판정(worth_logging)은 비결정적이라 제거 — 같은 대화가 날마다 다르게 처리되는 것을 막음.


# ── 시간·문자열 유틸 ────────────────────────────────────────────────
def _fmt_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def _turn_date(ts, fallback):
    d = _fmt_ts(ts)
    return d.strftime("%Y-%m-%d") if d else fallback


def _yaml_val(v):
    return '"' + str(v).replace("\n", " ").replace('"', "'") + '"'


def _git_state(cwd):
    """그 세션이 돌던 시점의 branch·HEAD. 재개할 때 **코드 드리프트**를 재는 기준점이다.
    12일 뒤에 돌아오면 세션 맥락보다 '그 사이 코드가 얼마나 변했나'가 더 중요하다."""
    if not cwd or not os.path.isdir(cwd):
        return None, None
    def run(*a):
        try:
            r = subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None
    br = run("rev-parse", "--abbrev-ref", "HEAD")
    head = run("rev-parse", "--short", "HEAD")
    return (br if br and br != "HEAD" else None), head


def _project_of(meta):
    return (meta.get("cwd") or "?").rstrip("/").split("/")[-1] or "?"


# ── 요약 (claude -p) ────────────────────────────────────────────────
def _first_para(text, limit=400):
    """'[Assistant]' 헤더 + 첫 문단만 남긴다. 결과 요지는 담되 분량을 억제."""
    head, _, body = text.partition("\n")
    para = body.strip().split("\n\n")[0].strip()
    if not para:
        return ""
    if len(para) > limit:
        para = para[:limit] + " …"
    return f"{head}\n{para}"


def render_conversation_for_summary(meta):
    texts = []
    for role, lines, _ in meta["turns"]:
        ls = _clean_lines(lines)
        if not ls:
            continue  # 도구/로그만 있던 turn 은 요약 대상에서 제외
        who = "User" if role == "user" else "Assistant"
        t = f"[{who}]\n" + "\n".join(ls)
        if len(t) > SUMMARY_TURN_MAX:
            t = t[:SUMMARY_TURN_MAX] + " …(생략)"
        texts.append((role, t))
    n = len(texts)
    keep = [False] * n
    trimmed = {}  # index → 축약본(Assistant 첫 문단)
    used = 0
    tail_cap = SUMMARY_CHAR_BUDGET * 2 // 3

    # 1단계: 뒤에서부터(최신 우선) tail_cap 까지 — User·Assistant 모두
    last_kept = n
    for i in range(n - 1, -1, -1):
        t = texts[i][1]
        if used and used + len(t) > tail_cap:
            break
        keep[i] = True
        used += len(t)
        last_kept = i

    # 2단계: 그 앞쪽에서 User 발화 + 직후 Assistant 첫 문단까지 (결정 V)
    #   User 만 담으면 "무엇을 시켰나"는 남고 "결과가 뭐였나"가 사라진다.
    for i in range(last_kept - 1, -1, -1):
        if texts[i][0] != "user":
            continue
        a_idx, a_txt = None, ""
        if i + 1 < n and not keep[i + 1] and texts[i + 1][0] == "assistant":
            a_txt = _first_para(texts[i + 1][1])
            if a_txt:
                a_idx = i + 1
        add = len(texts[i][1]) + len(a_txt)
        if used and used + add > SUMMARY_CHAR_BUDGET:
            break
        keep[i] = True
        used += len(texts[i][1])
        if a_idx is not None:
            keep[a_idx] = True
            trimmed[a_idx] = a_txt
            used += len(a_txt)

    parts = []
    prev = -1
    for i in range(n):
        if not keep[i]:
            continue
        if prev != -1 and i > prev + 1:
            parts.append("[…중략…]")
        parts.append(trimmed.get(i, texts[i][1]))
        prev = i
    return "\n\n".join(parts)


def build_summary_prompt(meta, current_tasks, choices=()):
    conversation = render_conversation_for_summary(meta)
    title = meta.get("title") or "(제목 없음)"
    slug_list = "\n".join(f"- {sl} — {ti}" for sl, ti in choices) or "- (등록된 주제 없음)"
    # 시그니처를 상수로 이어 붙인다 — f-string 을 쓰면 본문의 {"slug": …} 중괄호가 깨진다.
    instructions = SUMMARY_SIGNATURE + """. 아래는 한 프로젝트 세션에서 '이번에 새로 진행한 대화'다. 읽고 뽑아라.

- topic: 이 대화가 속한 주제 슬러그를 '# 주제 목록'에서 **정확히 하나** 골라라. 해당 없으면 "none".
  · 목록에 없는 값을 topic 에 넣지 마라. 반드시 목록의 값 또는 "none" 이어야 한다.
- topic_new: topic 이 "none" 일 때만 채운다. 이 대화를 한 덩어리로 부를 이름을 지어라.
  · {"slug": "kebab-case-영문", "title": "한글 제목"} 형태. slug 는 소문자 영문·숫자·하이픈만,
    title 은 40자 안쪽.
  · 주제 파일을 만들지는 않는다 — 이 대화에서 나온 할 일들을 목차에서 **한 묶음으로 묶는 이름표**다.
  · 이번 대화에 남은 할 일도 결론도 없으면 null 로 두어라.
- progress: 이번에 '실제로 한 일'을 1~3개의 짧은 불릿(- ...)으로. 계획이 아니라 한 일.
- resume: '지금 앉으면 무엇부터' **한 줄**. 남은 단계를 전부 늘어놓지 마라 — 그건 태스크가 담는다.
- tasks_add: **이번 대화에서 새로 생긴 할 일만** 배열로. 기존 목록은 절대 반환하지 마라(건드릴 수 없다).
  · 원소는 {"text": "할 일", "after": "이 항목 바로 뒤에 넣어라"} 형태. 순서가 상관없으면 after 를 빼라.
  · after 에는 '# 현재 미완료 작업' 목록에 있는 문구를 그대로 적어라. 못 찾으면 맨 뒤에 붙는다.
  · **한 줄은 80자 안쪽.** 명령·경로·근거 같은 상세는 쓰지 마라 — 그건 주제의 진행 로그와 설계 문서가
    담는다. 태스크는 '무엇을 해야 하는가' 한 줄이다.
  · 주제의 다음 단계도 포함한다 — 순서대로 체크해 나가면 되고, 체크박스여야 완료 이력이 남는다.
    순서가 있으면 제목 앞에 ① ② ③ 를 붙여라.
  · 이미 목록에 있는 것과 같은 일이면 넣지 마라. 새로 생긴 게 없으면 [] 로 두어라.
- blocker: **막혀서 진행이 안 되는 것**이 있으면 한 줄. 없으면 "".
  · 남의 승인·VPN·권한·다른 작업 완료처럼 **내가 지금 당장 풀 수 없는 것**만. 할 일은 여기 쓰지 마라.
- verified: 이번에 **실제로 확인·검증된 것** 한 줄. 테스트를 돌렸는지, 배포했는지, 코드만 짜고 안 돌렸는지.
  · "dry-run 통과, 실제 적용 안 함" 처럼 **어디까지 확실한지**를 적어라. 재개할 때 이것부터 본다.
  · 확인한 게 없으면 "" 로 두어라. 지어내지 마라.
- conclusions: **다음에 같은 작업을 할 때 몰랐으면 헤맬 사실**만 배열로. 수치·조건·이유를 담아라.
  · 이번에 무엇을 했는지는 progress 의 몫이다. 여기 쓰지 마라.
  · 해당 없으면 [] 로 두어라. 억지로 만들지 마라.
- dropped: 이번 대화에서 **검토했다가 기각한 대안**을 "안 — 기각 이유" 형태 배열로.
  · 기각 이유가 없으면 넣지 마라. 없으면 [].

모든 답변 한국어. 확실치 않으면 짧게."""
    prompt = (
        f"{instructions}\n\n"
        "반드시 아래 JSON 하나로만 답하라. 코드펜스/설명 금지:\n"
        '{"topic": "slug 또는 none", "topic_new": null, "progress": "- ...", "resume": "...",\n'
        '  "verified": "...", "blocker": "",\n'
        '  "conclusions": [], "dropped": [], "tasks_add": []}\n\n'
        f"# 주제 목록\n{slug_list}\n\n"
        f"# 프로젝트\n{title}\n\n"
        f"# 현재 미완료 작업 (참고용 — 중복 방지·after 지정에만 쓴다)\n{current_tasks or '(없음)'}\n\n"
        f"# 이번에 새로 진행한 대화\n{conversation}\n"
    )
    # 구성요소별 크기 — "무관한 태스크 목록이 비용을 얼마나 밀어올리는가" 를 나중에 따지기 위함
    parts = {"prompt_chars": len(prompt), "conv_chars": len(conversation),
             "tasks_chars": len(current_tasks or ""), "topics_chars": len(slug_list),
             "instr_chars": len(instructions)}
    return prompt, parts


def _extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _valid_summary(parsed):
    if not isinstance(parsed, dict):
        return None
    for k in ("topic", "progress", "resume", "verified", "blocker"):
        v = parsed.get(k)
        if v is not None and not isinstance(v, str):
            return None
    for k in ("conclusions", "dropped", "tasks_add"):
        v = parsed.get(k)
        if v is not None and not isinstance(v, list):
            return None
    return {
        "topic": parsed.get("topic"),
        "topic_new": _norm_topic_new(parsed.get("topic_new")),
        # 결론·접은 안도 문서에 그대로 들어간다 — 개행이 남으면 `## ` 가 새 섹션 경계를
        # 만들고 `- [x] …` 가 가짜 완료 태스크로 렌더된다.
        "conclusions": [y for y in (_one_line(x) for x in (parsed.get("conclusions") or [])) if y],
        "dropped": [y for y in (_one_line(x) for x in (parsed.get("dropped") or [])) if y],
        "progress": parsed.get("progress"),
        "resume": parsed.get("resume"),
        "verified": parsed.get("verified"),
        "blocker": parsed.get("blocker"),
        "tasks_add": _norm_adds(parsed.get("tasks_add")),
    }


def _norm_topic_new(raw):
    """{'slug','title'} 로 정규화. 슬러그는 파일명이 될 수 있어야 하므로 [a-z0-9-] 만 남긴다."""
    if not isinstance(raw, dict):
        return None
    slug = re.sub(r"[^a-z0-9-]+", "-", str(raw.get("slug") or "").strip().lower()).strip("-")
    if not slug:
        return None
    return {"slug": slug, "title": _safe_alias(raw.get("title")) or slug}


def _safe_alias(s):
    """위키링크 alias 로 넣어도 안전한 문자열. `[`·`]`·`|` 가 들어가면 링크가 그 자리에서 끊겨
    **제목이 통째로 소실된다**(`_task_topic_alias` 파싱 실패). LLM 이 만드는 제목이라
    `[긴급]` 같은 대괄호 태그가 실제로 들어온다."""
    s = re.sub(r"[\[\]|]+", " ", str(s or "")).replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()[:TASK_TITLE_MAX]


def _one_line(s, cap=300):
    """LLM 문자열을 **한 줄**로 강제한다.

    타입 검사만으로는 구조 파괴를 못 막는다 — 태스크 문구에 개행이 남으면 그 뒷줄이
    `- [x] 가짜 완료` 로 렌더되고, resume 에 `## ` 가 남으면 주제 파일에 없던 섹션 경계가 생긴다.
    """
    s = " ".join(str(s or "").split())
    # **공백을 동반한** 마크다운 토큰만 뗀다. `[#>\-*]+` 로 훑으면
    # `--force 없이 재실행` 이 `force 없이…` 가 되고 `#123 이슈` 가 `123 이슈` 가 된다.
    s = re.sub(r"^(?:[#>*]+\s+|-\s+)+", "", s).strip()
    return s if len(s) <= cap else s[:cap].rstrip() + "…"


def _clean_progress(s, cap=300):
    """진행 로그는 여러 줄이지만 **불릿만** 허용한다. 헤더 줄이 섞이면 섹션이 갈라진다."""
    out = []
    for ln in str(s or "").splitlines():
        t = ln.strip()
        if not t.startswith("-"):
            continue
        t = " ".join(t.lstrip("-").split())
        if t:
            out.append(f"- {t[:cap]}")
    return "\n".join(out)


def _norm_adds(raw):
    """tasks_add 원소를 {'text','after'} 로 정규화. 문자열만 온 경우도 받아준다."""
    out = []
    for it in (raw or []):
        if isinstance(it, str):
            t, af = it, None
        elif isinstance(it, dict):
            t, af = it.get("text") or it.get("task") or "", it.get("after")
        else:
            continue
        t = _one_line(re.sub(r"^\s*-\s*\[[ xX]\]\s*", "", str(t)), cap=200)
        if t:
            out.append({"text": t, "after": (_one_line(af, cap=200) if af else None)})
    return out


def call_claude(prompt):
    """(응답 텍스트, usage) 를 준다. 실패하면 (None, None).

    `--output-format json` 을 쓰면 total_cost_usd 와 토큰 내역이 함께 온다 —
    프롬프트의 어느 부분이 비용을 만드는지 나중에 따지려면 이 값이 있어야 한다."""
    env = dict(os.environ)
    env[GUARD_ENV] = "1"
    t0 = datetime.now()
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", SUMMARY_MODEL, "--output-format", "json"],
            input=prompt, text=True, capture_output=True, env=env, timeout=CLAUDE_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    if proc.returncode != 0:
        return None, None
    elapsed = int((datetime.now() - t0).total_seconds() * 1000)
    try:
        o = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout, None          # 구버전 CLI 호환: 평문 응답
    u = o.get("usage") or {}
    return o.get("result") or "", {
        "cost_usd": o.get("total_cost_usd"),
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "cache_read": u.get("cache_read_input_tokens"),
        "cache_write": u.get("cache_creation_input_tokens"),
        "elapsed_ms": elapsed,
    }


def _match_known(tn, known, threshold=0.75):
    """신규 슬러그 제안을 **기존 전체 목록(완료 주제·파일 없는 슬러그 포함)** 과 대조한다.

    선택지(프롬프트)에는 active/paused 만 준다 — 완료 주제를 선택지에 넣으면 요약기가 그것을 골라
    되살리게 되고, 그것은 '완료 체크는 사람만 한다'를 훅이 깨는 것이다. 대조는 파이썬이 뒤에서만
    한다(0토큰). 맞으면 기존 슬러그를 돌려주고, 아니면 None.
    """
    for slug, _ in known:
        if slug == tn["slug"]:
            return slug
    best, bs = 0.0, None
    for slug, title in known:
        r = max(difflib.SequenceMatcher(None, _norm_line(tn["slug"]), _norm_line(slug)).ratio(),
                difflib.SequenceMatcher(None, _norm_line(tn["title"]), _norm_line(title)).ratio())
        if r > best:
            best, bs = r, slug
    return bs if best >= threshold else None


def summarize(meta, current_tasks, use_llm=True, choices=(), known=()):
    """요약 결과. **LLM 호출 자체가 실패하면 None 을 준다** — 호출자가 마커를 전진시키지
    않고 다음 실행에 재시도하게 하기 위함이다. 오프라인에서 돌면 '(요약 실패)' 를 기록하고
    마커까지 전진해 그 구간이 영영 요약되지 않는 사고가 난다."""
    title = meta.get("title") or "세션"
    if not use_llm:
        return {"topic": None, "topic_title": "", "conclusions": [], "dropped": [],
                "progress": f"- [{title}] (dry-run)", "resume": "(dry-run)", "verified": "", "blocker": "",
                "tasks_add": [], "_usage": None, "_parts": {}}
    base_prompt, parts = build_summary_prompt(meta, current_tasks, choices)
    nudge = "\n\n[중요] 직전 응답이 형식에 안 맞았다. JSON 객체 하나만 출력하라."
    valid, usage = None, None
    for attempt in range(SUMMARY_MAX_TRIES):
        out, usage = call_claude(base_prompt if attempt == 0 else base_prompt + nudge)
        valid = _valid_summary(_extract_json(out)) if out else None
        if valid is not None:
            break
        _debug(f"[worker] 요약 무효 — 재시도({attempt + 1})")
    if valid is None:
        _debug("[worker] 요약 실패(LLM 응답 없음/형식 불일치) — 기록하지 않는다")
        return None

    # 닫힌 선택지 강제: 목록에 없는 슬러그는 none 으로 (결정 E — 훅은 새 주제를 만들지 않는다)
    topic = (valid["topic"] or "").strip()
    if topic.lower() in ("", "none", "null", "n/a"):
        topic = None
    elif choices and topic not in {sl for sl, _ in choices}:
        # 선택지 밖이어도 **이미 쓰이는 이름**이면 받는다. 새 주제를 만드는 것이 아니라
        # 있는 묶음에 붙이는 것이다. 여기서 떨어뜨리면 그 작업만 무소속이 된다.
        if topic in {sl for sl, _ in (known or ())}:
            _debug(f"[worker] 선택지 밖이지만 이미 쓰는 묶음 '{topic}' — 그대로 사용")
        else:
            _debug(f"[worker] 미등록 슬러그 '{topic}' → none 처리")
            topic = None

    # 매칭 실패 시: 제안된 이름표를 기존 목록과 대조해 구제하고, 아니면 그 이름표를 그대로 쓴다.
    # 여기서 정해지는 슬러그에 **파일이 없을 수 있다** — 그건 주제가 아니라 목차의 묶음 키다.
    topic_title = ""
    if topic is None and valid.get("topic_new"):
        tn = valid["topic_new"]
        hit = _match_known(tn, known)
        if hit:
            topic, topic_title = hit, ""
            _debug(f"[worker] 신규 제안 '{tn['slug']}' → 기존 '{hit}' 에 합침")
        else:
            topic, topic_title = tn["slug"], tn["title"]
            _debug(f"[worker] 신규 묶음 키 '{topic}' ({topic_title}) — 주제 파일은 만들지 않는다")
    return {
        "topic": topic,
        "topic_title": topic_title,
        "conclusions": valid["conclusions"],
        "dropped": valid["dropped"],
        "progress": _clean_progress(valid["progress"]) or "- (내용 없음)",
        "resume": _one_line(valid["resume"]) or "(다음 미기재)",
        "verified": _one_line(valid.get("verified")),
        "blocker": _one_line(valid.get("blocker")),
        "tasks_add": valid["tasks_add"],
        "_usage": usage,
        "_parts": parts,
    }


# ── 태스크 (작업현황) ───────────────────────────────────────────────
def _split_tasks(md):
    """미완료/완료 태스크. 주제 줄(**3.**)은 체크박스를 갖지만 태스크가 아니므로 뺀다."""
    open_t, done_t = [], []
    for ln in (md or "").splitlines():
        s = ln.rstrip()
        # `**3.**` 은 머리줄(주제·묶음)이고 `**3-1**` 이 태스크다. 링크 유무와 무관하게 뺀다 —
        # 묶음 줄에는 링크가 없어 TOPIC_LINE_RE 로는 안 걸리는데, 누가 raw 편집으로
        # 체크박스를 붙이면 가짜 완료 태스크가 되어 완료 섹션·아카이브까지 흘러간다.
        if HEADER_LINE_RE.match(s):
            continue
        low = s.lstrip().lower()
        if low.startswith("- [ ]"):
            open_t.append(s)
        elif low.startswith("- [x]"):
            done_t.append(s)
    return open_t, done_t


def _stamp_done(line, date_str):
    return line if "✅" in line else f"{line} ✅ {date_str}"


def _unstamp(line):
    """열린 줄의 `✅ 날짜` 제거. 체크를 풀었는데 스탬프가 남으면 weekly 가 그 항목을
    계속 완료로 세고, 사람 눈에도 '완료인데 열려 있는' 모순으로 보인다."""
    # **줄 끝**만 본다. 줄 중간까지 지우면 `- [ ] 인증서 ✅ 2026-09-01까지 갱신` 처럼
    # 사람이 본문에 쓴 날짜가 통째로 사라진다(실측).
    return re.sub(r"\s*✅\s*\d{4}-\d{2}-\d{2}\s*$", "", line).rstrip()


# 앞의 들여쓰기까지 흡수한다 — 렌더가 매번 탭을 새로 붙이므로 여기서 정규화하지 않으면
# 재렌더마다 탭이 누적되고 번호 패턴이 매칭되지 않는다.
# 주제 줄: - [ ] **3.** [[topics/slug|제목]] …   (태스크는 **3-1** 이라 겹치지 않는다)
TOPIC_LINE_RE = re.compile(
    r"^\s*-\s*\[([ xX])\]\s*\*\*(\d+)\.\*\*\s*\[\[" + re.escape(TOPICS_DIRNAME) + r"/([^\]|]+)")

# 머리줄 전반(주제 줄 + 묶음 줄). 태스크는 `**3-1**` 이라 겹치지 않는다.
HEADER_LINE_RE = re.compile(r"^\s*-\s*(?:\[[ xX]\]\s*)?\*\*\d+\.\*\*")

NUM_RE = re.compile(r"^\s*(-\s*\[[ xX]\]\s*)(?:\*\*[0-9]+-[0-9]+\*\*\s*)?(.*)$")


def _numbered(line, num):
    """줄에 **n-j** 번호를 붙인다(이미 있으면 갈아끼운다). 번호는 렌더 순번이라 매번 다시 매긴다.
    num 이 None 이면 번호를 떼기만 한다(주제 없는 태스크)."""
    m = NUM_RE.match(line)
    if not m:
        return line
    return f"{m.group(1)}**{num}** {m.group(2)}" if num else f"{m.group(1)}{m.group(2)}"


def _task_key(line):
    """항목의 동일성 판정 키. **렌더 순번(3-2)과 링크는 반드시 빼야 한다** —
    주제가 하나 추가돼 번호가 밀리면 같은 태스크가 새 항목으로 인식되어
    완료 시각·주제 태그·아카이브 중복 판정이 전부 끊긴다."""
    s = re.sub(r"-\s*\[[ xX]\]", "", line)
    s = re.sub(r"\*\*\d+-\d+\*\*", "", s)
    s = re.sub(r"\[\[.*?\]\]", "", s)
    s = re.sub(r"✅\s*\d{4}-\d{2}-\d{2}", "", s)
    # **주제 슬러그는 남긴다.** 문구만 쓰면 주제 A 의 `- [ ] VM 재배포` 와
    # 주제 B 의 `- [x] VM 재배포` 가 같은 항목이 되어, B 의 완료가 A 의 미완료를
    # 렌더 직전에 지워 버린다. 링크 전체를 남기지 않는 이유는 대화 링크가 매번 바뀌기 때문.
    slug = _task_topic(line)
    return (f"{slug}|{s.strip().lower()}" if slug else s.strip().lower())


def _done_date(line):
    m = re.search(r"✅\s*(\d{4}-\d{2}-\d{2})", line)
    return m.group(1) if m else None


def _archive_done(base, lines):
    path = os.path.join(base, ARCHIVE_FILENAME)
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    seen = {_task_key(l) for l in existing.splitlines() if l.lstrip().lower().startswith("- [x]")}
    fresh = [l for l in lines if _task_key(l) not in seen]
    if not fresh:
        return
    with open(path, "a", encoding="utf-8") as f:
        if not existing:
            f.write("# ✅ 완료 아카이브\n\n")
        elif not existing.endswith("\n"):
            f.write("\n")
        for l in fresh:
            f.write(l.rstrip() + "\n")


def _find_task(lines, needle):
    """`after` 로 지정된 문구와 같은 줄의 위치. 정규화 일치 → 부분 포함 → 유사도 순으로 찾는다."""
    if not needle:
        return None
    n = _norm_line(needle)
    keys = [_norm_line(re.sub(r"\[\[.*?\]\]", "", l)) for l in lines]
    for i, k in enumerate(keys):
        if k == n:
            return i
    for i, k in enumerate(keys):
        if n and (n in k or k in n):
            return i
    best, bi = 0.0, None
    for i, k in enumerate(keys):
        r = difflib.SequenceMatcher(None, n, k).ratio()
        if r > best:
            best, bi = r, i
    return bi if best >= 0.6 else None


def _apply_task_adds(open_cur, adds, topic, conv_link, topic_title=""):
    """새 태스크를 목록에 넣는다. after 가 가리키는 줄 **뒤에**, 못 찾으면 맨 뒤에.

    기존 줄은 손대지 않는다 — LLM 은 추가분만 주므로 순서·태그·링크가 훼손될 수 없다.

    `topic_title` 은 **주제 파일이 없는 묶음**일 때만 채워진다. 그 경우 링크 alias 가
    묶음 제목의 유일한 저장소다 — 목차는 매 렌더 다시 쓰이므로 제목을 둘 데가 여기뿐이다.
    파일이 있는 주제는 렌더 시 파일에서 title 을 읽으므로 alias 는 🔧 로 둔다."""
    out = list(open_cur)
    for a in adds or []:
        text = a["text"]
        # **같은 주제 안에서만** 중복을 본다. 링크를 지우고 전체와 비교하면
        # 주제 A 의 'VM 재배포' 때문에 주제 B 의 'VM 재배포' 가 생기지 못한다.
        peers = [l for l in out if _task_topic(l) == topic]
        if _dedup_against([text], [re.sub(r"\[\[.*?\]\]", "", l) for l in peers],
                          threshold=TASK_DUP_RATIO, substr=False) == []:
            _debug(f"[worker] 태스크 중복({topic or '무소속'}) — 건너뜀: {text[:40]}")
            continue
        line = f"- [ ] {text}"
        if topic:
            # 제목이 안 넘어왔으면 **같은 슬러그의 기존 줄에서 물려받는다.**
            # 요약기가 기존 묶음에 합칠 때는 제목을 다시 주지 않는데(`_match_known` 적중),
            # 제목을 들고 있던 줄 하나가 완료되면 남은 줄이 전부 🔧 라서 묶음 제목이
            # 슬러그로 퇴화한다. 모든 줄이 제목을 갖고 있으면 그 일이 생기지 않는다.
            alias = _safe_alias(topic_title) or next(
                (a for a in (_task_topic_alias(l) for l in out if _task_topic(l) == topic) if a), "")
            line += f"  [[{TOPICS_DIRNAME}/{topic}|{alias or '🔧'}]]"
        if conv_link:
            line += f"  [[{conv_link}|↗ 대화]]"
        pos = _find_task(out, a.get("after"))
        if pos is None:
            out.append(line)
        else:
            out.insert(pos + 1, line)
    return out


def _task_topic(line):
    """태스크 줄에 심긴 주제 슬러그."""
    m = re.search(r"\[\[" + re.escape(TOPICS_DIRNAME) + r"/([^|\]]+)", line)
    return m.group(1).strip() if m else None


def _task_topic_alias(line):
    """태스크 줄 링크의 alias. 파일 없는 묶음은 여기에 제목이 들어 있다(🔧 는 제목이 아니다)."""
    m = re.search(r"\[\[" + re.escape(TOPICS_DIRNAME) + r"/[^|\]]+\|([^\]]*)\]\]", line)
    a = (m.group(1).strip() if m else "")
    return "" if a == "🔧" else a


# 링크 **대상**(`|` 앞)의 날짜만 잡는다. `conversations/<sid8>_2026-08-10` 과
# 구설계 `2026-06-05_1334_..._sid8` 을 모두 잡되, `[^\]|]` 라서 `|` 를 넘지 못하므로
# **alias(제목) 안의 날짜는 걸리지 않는다** — 제목에 "정산 마감 2099-12-31" 같은 말이 들어가면
# 그것이 마지막 활동일로 둔갑한다. `✅ 2026-08-10` 완료 스탬프는 대괄호 밖이라 애초에 무관하다.
LINK_DATE_RE = re.compile(r"\[\[[^\]|]*?(\d{4}-\d{2}-\d{2})")

# 방치 경고 임계값. 짧게 잡으면 대부분의 묶음이 상시 경고가 되어 신호가 죽고,
# 90일(원안)은 두 달 넘게 조용하다. 한 달이면 되짚을 때가 됐다는 뜻이고
# 완료 보관(14일)·아카이브 주기와도 겹치지 않는다.
STALE_DAYS = 30


def _last_activity(lines):
    ds = [m.group(1) for l in lines for m in LINK_DATE_RE.finditer(l)]
    return max(ds) if ds else None


def _activity_mark(lines, today=None, fallback=None):
    """묶음 줄 꼬리표: 마지막 활동일 + 방치 경과. 저장소가 필요 없다 —
    파일 없는 묶음은 status 를 둘 곳이 없으므로 상태를 저장하는 대신 **계산해서 보여준다**.

    날짜를 못 구하면 **침묵하지 않는다.** 손으로 적어 넣은 태스크에는 대화 링크가 없어
    날짜가 안 나오는데, 꼬리표가 통째로 빠지면 '⚠ N일째' 가 없는 것이 '괜찮다'로 읽힌다.
    fallback 은 같은 슬러그의 완료 태스크 `✅ 날짜` 다(실제 활동 시점).
    """
    d = _last_activity(lines) or fallback
    if not d:
        return " · 활동일 미상"
    out = f" · 마지막 활동 {d[5:]}"
    try:
        ref = datetime.strptime(today or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")
        days = (ref - datetime.strptime(d, "%Y-%m-%d")).days
    except ValueError:
        return out
    return out + (f" · ⚠ {days}일째" if days >= STALE_DAYS else "")


def _session_refs(lines):
    """태스크 줄에 심긴 세션 참조 → [(날짜, sid8, 대화문서 이름)] 최근 순.

    링크 형태가 둘이다 — 현행 `conversations/<sid8>_<날짜>` 와
    구설계 `<날짜>_<시각>_<제목>_<sid8>`. **구설계 파일도 `conversations/` 안에 있다** —
    링크에 경로가 안 붙어 있을 뿐이다. 한 곳에서 뽑아 재개 경로와 승격이 같은 것을 본다.
    """
    out = {}
    for l in lines:
        for tgt in re.findall(r"\[\[([^\]|]+)", l):
            name = tgt.strip().rpartition("/")[2]
            if tgt.strip().startswith(CONV_DIRNAME + "/"):
                sid8, date = name.partition("_")[0], name.partition("_")[2]
            else:
                sid8 = name.rsplit("_", 1)[-1]
                if not re.fullmatch(r"[0-9a-f]{8}", sid8):
                    continue
                m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
                date = m.group(1) if m else ""
            out[(date, sid8)] = name
    return [(d, sid, out[(d, sid)]) for d, sid in sorted(out, reverse=True)]


def _group_resume(lines):
    """파일 없는 묶음의 재개 한 줄. 슬러그로 묶여 있는데 '어디서 이어서 하나' 가 없으면
    그 묶음만 목차에서 막다른 길이 된다(실측: DataHub 묶음에 cd·resume 이 없었다).
    대화 링크의 sid8 → history.jsonl 로 세션 id 와 작업 경로를 복구한다."""
    for _date, c, _name in _session_refs(lines):   # 최근 대화부터
        sess, cwd = _history_lookup(c)
        if not (sess and cwd and os.path.isdir(cwd)):
            continue
        if _resumable(sess):
            return f"\n\t  ↳ `cd {shlex.quote(cwd)} && claude -r {shlex.quote(sess)}`"
        return f"\n\t  ↳ `cd {shlex.quote(cwd)}`  (원본 만료)"
    return ""


def _known_topics(base, open_lines=()):
    """유사도 대조용 (slug, title) **전체** 목록.

    `_topic_choices` 와 다르다 — 저쪽은 요약기에게 주는 선택지라 완료 주제를 뺀다.
    이쪽은 파이썬이 뒤에서 대조만 하므로 완료 주제와 **파일 없이 태스크에만 심긴 슬러그**까지
    넣는다. 그래야 ① 완료 주제와 같은 일이 새 이름으로 갈라지지 않고
    ② 같은 묶음이 세션마다 다른 슬러그를 얻지 않는다."""
    d = os.path.join(base, TOPICS_DIRNAME)
    out, seen = [], set()
    for slug in _topic_slugs(base):
        m = _topic_meta(os.path.join(d, f"{slug}.md"))
        out.append((slug, m.get("title") or slug))
        seen.add(slug)
    for l in open_lines or ():
        s = _task_topic(l)
        if s and s not in seen:
            out.append((s, _task_topic_alias(l) or s))
            seen.add(s)
    return out


def _count_tasks(md):
    """와이프 방지용 개수. **주제 줄은 세지 않는다** — 주제가 남아 있으면
    태스크가 전부 사라져도 0 이 아니게 되어 방지가 무력해진다."""
    o, d = _split_tasks(md)
    return len(o) + len(d)


def _backup_tasks(path, content):
    d = os.path.join(os.path.dirname(path), ".task-backups")
    os.makedirs(d, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    with open(os.path.join(d, f"tasks_{ts}.md"), "w", encoding="utf-8") as f:
        f.write(content)
    for old in sorted(glob.glob(os.path.join(d, "tasks_*.md")))[:-TASK_BACKUP_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass


RENDER_STAMP_PREFIX = "> 이 목차는 "


def _strip_stamp(md):
    """비교용 정규화. **시각 값만** 지운다 — 줄 전체를 지우면 문구를 고쳐도
    다른 변경이 없는 한 반영되지 않는다(업그레이드 직후가 바로 그 상황이다)."""
    return "\n".join(
        re.sub(r"\*\*\d{4}-\d{2}-\d{2} \d{2}:\d{2}\*\*", "**?**", l)
        if l.startswith(RENDER_STAMP_PREFIX) else l
        for l in (md or "").splitlines())


def _safe_write_index(path, new_md):
    cur = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cur = f.read()
    # 시각 한 줄만 달라진 렌더는 쓰지 않는다 — 안 그러면 아무것도 안 바뀐 FileChanged 마다
    # 파일이 변경되고 git 스냅샷에 의미 없는 커밋이 쌓인다. 그래서 이 시각은
    # '마지막 렌더' 가 아니라 **'마지막으로 내용이 바뀐 때'** 다(문구도 그렇게 적었다).
    if cur and _strip_stamp(cur) == _strip_stamp(new_md):
        return True
    cur_n, new_n = _count_tasks(cur), _count_tasks(new_md)
    if cur_n >= 1 and new_n == 0:
        _debug(f"[worker] INDEX 덮어쓰기 거부(와이프 방지): {cur_n}→0")
        return False
    if new_n < cur_n:
        # 정당한 감소(아카이브 이동·항목 병합)도 있으므로 막지는 않는다.
        # 다만 LLM 이 조용히 항목을 떨어뜨리는 경우를 사후에 알 수 있어야 한다.
        _debug(f"[worker] 태스크 항목 감소: {cur_n}→{new_n} (직전 사본 .task-backups/)")
    if cur.strip():
        _backup_tasks(path, cur)
    _atomic_write(path, new_md.rstrip() + "\n")
    return True


def _update_tasks(base, open_new, done_cur, db_path=DB_FILE, base_keys=None):
    """미완료/완료를 정리해 INDEX 를 다시 쓴다.

    태스크의 정본은 INDEX.md 하나다 — 별도 목록 파일을 두면 번호가 어긋나고
    '체크했는데 목차엔 남아있다' 가 생긴다."""
    _sync_task_states(base, db_path)  # 폴백: 아직 미기록된 완료 전이 포착
    # done 은 호출자가 넘긴 스냅샷이 아니라 **지금 파일**에서 다시 읽는다.
    # 락을 쥔 채 LLM 을 기다리는 37~91초 사이에 사람이 체크한 항목이 있으면,
    # 옛 스냅샷으로 덮어쓸 때 그 체크가 조용히 되돌려진다.
    tf_now = os.path.join(base, INDEX_FILENAME)
    prev_open = []
    if os.path.exists(tf_now):
        with open(tf_now, encoding="utf-8") as f:
            prev_open, done_cur = _split_tasks(f.read())
    # open 쪽은 3-way 병합한다. base_keys = 이번 flush 시작 시점의 미완료 키.
    #  · base 에 있었는데 지금 파일에 없다 → 사람이 **지우거나 체크했다**. 되살리지 않는다.
    #  · 지금 파일에 있는데 open_new 에 없다 → 사람이 **체크를 해제했거나 손으로 추가했다**.
    #    done_cur 에도 없고 open_new 에도 없어 그대로 두면 **소멸한다.**
    # 순서는 open_new 기준 — after 위치 계산을 그쪽이 갖고 있다.
    prev_keys = {_task_key(p) for p in prev_open}
    if base_keys is not None:
        dropped = [o for o in (open_new or [])
                   if _task_key(o) in base_keys and _task_key(o) not in prev_keys]
        if dropped:
            _debug(f"[worker] 사람이 지운 태스크 {len(dropped)}건 — 되살리지 않음")
        open_new = [o for o in (open_new or []) if o not in dropped]
    have = {_task_key(o) for o in (open_new or [])}
    revived = [p for p in prev_open if _task_key(p) not in have]
    if revived:
        _debug(f"[worker] 락 대기 중 사람이 편집한 태스크 {len(revived)}건 복원")
        open_new = list(open_new or []) + revived
    open_new = [_unstamp(o) for o in open_new]
    today = datetime.now().strftime("%Y-%m-%d")
    done_keys = {_task_key(d) for d in done_cur}
    open_new = [o for o in (open_new or []) if _task_key(o) not in done_keys]
    # 번호는 렌더 순번이라 완료 시점에 뗀다 (아카이브까지 그대로 간다)
    done_stamped = [_stamp_done(_numbered(d, None), _completion_date(_task_key(d), today, db_path))
                    for d in done_cur]
    cutoff = (datetime.now() - timedelta(days=DONE_RETAIN_DAYS)).strftime("%Y-%m-%d")
    recent, old = [], []
    for d in done_stamped:
        dd = _done_date(d)
        (old if (dd and dd < cutoff) else recent).append(d)
    if old:
        _archive_done(base, old)
        _debug(f"[worker] 완료 아카이브 이동: {len(old)}건")
    recent.sort(key=lambda x: _done_date(x) or "", reverse=True)   # 최신 완료가 위
    _write_index(base, open_new, recent, db_path)
    _sync_task_states(base, db_path)  # 쓰기 후 snapshot 갱신


def _git_snapshot(base):
    """vault 가 git 저장소면 이번 flush 결과를 커밋한다.

    topics/ 는 자체 백업이 없어(.task-backups 는 INDEX 전용) git 이 유일한 복구 수단인데
    수동 커밋에만 의존하면 되돌릴 지점이 드문드문해진다. 실패는 무시한다 — 기록이 우선이다."""
    if not os.path.isdir(os.path.join(base, ".git")):
        return
    # **훅이 쓰는 경로만** 담는다. `add -A` 는 사용자가 편집 중이던 노트까지 끌어와
    # 'auto: SessionEnd flush' 라는 이름으로 남의 작업을 커밋한다.
    names = (INDEX_FILENAME, ARCHIVE_FILENAME, TOPICS_DIRNAME, CONV_DIRNAME, "daily", "weekly")
    tracked = set()
    try:
        r = subprocess.run(["git", "-C", base, "ls-files", "--", *names],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            tracked = {l.split("/", 1)[0] for l in r.stdout.splitlines() if l}
    except Exception:
        pass
    def has_content(path):
        if os.path.isfile(path):
            return True
        return any(fs for _r, _d, fs in os.walk(path)) if os.path.isdir(path) else False

    # 존재 여부만 보면 ① 삭제된 최상위 파일이 pathspec 에서 빠져 삭제가 커밋되지 않고,
    # ② 빈 디렉터리가 pathspec 에 들어가 `git commit` 전체가
    # "did not match any file(s) known to git" 로 실패한다 — 스테이징만 되고 커밋은 안 된다(실측).
    owned = [p for p in names
             if p in tracked or has_content(os.path.join(base, p))]
    if not owned:
        return
    try:
        st = subprocess.run(["git", "-C", base, "status", "--porcelain", "--", *owned],
                            capture_output=True, text=True, timeout=30)
        if st.returncode != 0 or not st.stdout.strip():
            return
        subprocess.run(["git", "-C", base, "add", "--", *owned], capture_output=True, timeout=60)
        msg = f"auto: SessionEnd flush {datetime.now():%Y-%m-%d %H:%M}"
        r = subprocess.run(["git", "-C", base, "commit", "-m", msg, "--", *owned],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            _debug(f"[worker] git 스냅샷 완료: {msg}")
        else:
            # 사유 없이 '실패' 만 남기면 왜 커밋이 안 됐는지 알 수 없다.
            _debug(f"[worker] git 스냅샷 실패({r.returncode}): "
                   f"{(r.stderr or r.stdout).strip()[:200]} · paths={owned}")
    except Exception as e:
        _debug("git 스냅샷 예외: " + repr(e))


# ── 동시성 락 ───────────────────────────────────────────────────────
@contextlib.contextmanager
def _vault_lock(blocking=True):
    """blocking=False 면 잡히지 않을 때 곧바로 False 를 준다.
    FileChanged 는 사람이 체크한 직후 도는 훅이라 워커의 LLM 대기(최대 90초)를
    기다리면 안 된다 — 못 잡으면 건너뛰고, 어차피 그 워커가 끝나며 다시 렌더한다."""
    f = open(LOCK_FILE, "w")
    got = False
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            got = False
        yield got
    finally:
        if got:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


# ── 증분 렌더링 (허브 / 대화 페이지 / daily / weekly) ────────────────
def _group_by_date(turns, fallback_date):
    groups, order, last = {}, [], fallback_date
    for t in turns:
        ds = _turn_date(t[2], last)
        last = ds
        if ds not in groups:
            groups[ds] = []
            order.append(ds)
        groups[ds].append(t)
    return [(d, groups[d]) for d in order]  # 시간순


def _render_turns(turns):
    """대화 페이지 본문. 도구 라인·'#로그' 제외 + turn당 PASTE_CAP 상한."""
    out = []
    for role, lines, _ in turns:
        ls = _clean_lines(lines)
        if not ls:
            continue  # 도구/로그만 있던 turn 은 저장하지 않음
        # 이모지만으로 역할이 구분되므로 'User'/'Assistant' 표기는 생략한다.
        # (요약 프롬프트 쪽은 [User]/[Assistant] 를 유지 — 요약기에는 역할 라벨이 필요하다)
        out.append("### 👤" if role == "user" else "### 🤖")
        out.append(_capped("\n".join(ls)))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _conv_head(progress, conclusions=(), dropped=()):
    """대화 페이지 머리말. 주제 파일이 없는 세션에서는 **여기가 결론의 유일한 착지점**이다 —
    `_append_topic` 이 호출되지 않으면 요약기가 뽑아둔 결론·접은 안이 그대로 버려진다."""
    out = []
    if progress:
        out.append(f"## 📈 이날 진행\n\n{progress.rstrip()}\n")
    for header, items in ((CONCLUSION_HEADER, conclusions), (DROPPED_HEADER, dropped)):
        if items:
            out.append(header + "\n\n" + "\n".join(f"- {x}" for x in items) + "\n")
    return "\n".join(out) + "\n" if out else ""


def _flush_marker(sid8, date, rng):
    """이 파일에 이미 실린 **처리 구간**. 본문 부분문자열로 판정하면
    같은 질문을 다시 한 정상 대화가 '이미 있음' 으로 버려진다(실측)."""
    return f"<!-- flush {sid8} {date} {rng} -->"


def _write_conversation_page(base, sid8, date, turns, title=None, progress=None, topic=None,
                             conclusions=(), dropped=(), rng=None):
    """그날 대화 원문. 세션 허브를 두지 않으므로 진행 요약도 여기 얹는다
    (주제가 잡힌 세션은 topics/ 가 정본이고, 여기 요약은 대화를 여는 사람용 머리말)."""
    d = os.path.join(base, CONV_DIRNAME)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sid8}_{date}.md")
    body = _render_turns(turns)
    head_extra = _conv_head(progress, conclusions, dropped)
    mark = _flush_marker(sid8, date, rng) + "\n" if rng else ""
    if os.path.exists(path):
        # 같은 날 두 번째 flush. 머리말을 다시 실어 중간에 헤더가 반복되지만,
        # 빼면 두 번째 증분에서 나온 결론이 유실된다 — 중복이 유실보다 낫다.
        cur = open(path, encoding="utf-8").read()
        if mark and mark.strip() in cur:
            # 마커 저장이 실패해 **같은 구간을 다시 처리**하는 경우다. 두 번 싣지 않는다.
            _debug(f"[worker] {sid8}_{date}: 이미 실린 구간({rng}) — 재기록 건너뜀")
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + mark + head_extra + body)
        return
    # title frontmatter: Front Matter Title 플러그인이 파일명 대신 표시
    disp = f"{title or topic or '대화'} · {date}"
    head = f"---\ntitle: {_yaml_val(disp)}\n---\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        if topic:
            f.write(f"> 주제: [[{TOPICS_DIRNAME}/{topic}]]\n\n")
        f.write(mark)
        f.write(head_extra)
        f.write(f"# 💬 {date} 대화\n\n" + body)


def _append_daily(base, date, label, progress, link_target):
    """label 은 topic 슬러그 우선. 매칭 실패 시에만 cwd 기반 project 로 폴백.
    한 주제가 여러 repo 에 걸치면 cwd 기준 그룹핑은 실제와 어긋난다."""
    d = os.path.join(base, "daily")
    os.makedirs(d, exist_ok=True)
    first = progress.strip().splitlines()[0] if progress.strip() else "진행"
    first = re.sub(r"^-\s*", "", first).strip()
    line = f"- [{label}] {first}  [[{link_target}|↗]]"
    fp = os.path.join(d, f"{date}.md")
    if os.path.exists(fp) and line in open(fp, encoding="utf-8").read():
        return          # 같은 구간 재처리 — daily 한 줄이 두 번 쌓이지 않게
    with open(fp, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── 주제축 (topics/ · INDEX.md) ─────────────────────────────────────
def _topic_slugs(base):
    d = os.path.join(base, TOPICS_DIRNAME)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(d, "*.md")))


# 들여쓰기는 **공백 3칸까지**만 펜스다. 줄머리 탭은 4칸으로 펼쳐지므로 펜스가 아니다.
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\n]*)$", re.M)


def _fence_spans(txt):
    """코드펜스 구간들. 이 안의 `## …` 는 문서 예시이지 섹션이 아니다.

    여는 펜스의 **문자와 길이**를 기억한다. ```` 로 연 블록은 그 안의 ``` 로 닫히지 않는다 —
    개수만 세면 4-backtick 예시 블록에서 경계가 어긋나 진짜 헤더를 지운다(실측).
    `~~~` 도 펜스이고, 줄 안에서 닫히는 인라인 코드(```x```)는 펜스가 아니다.
    """
    spans, open_at, marker = [], None, None
    for m in FENCE_RE.finditer(txt):
        tok, rest = m.group(1), m.group(2)
        if tok[0] == "`" and "`" in rest:
            continue                      # ```x``` 처럼 한 줄에서 닫히는 인라인 코드
        if open_at is None:
            # 물결표 펜스의 info string 에는 `~` 도 백틱도 들어갈 수 있다(`~~~python~3`).
            open_at, marker = m.start(), tok
        elif tok[0] == marker[0] and len(tok) >= len(marker) and not rest.strip():
            spans.append((open_at, m.end()))
            open_at = None
    # **닫히지 않은 펜스는 버린다.** EOF 까지 마스킹하면 그 뒤의 진짜 헤더가 전부 숨는데,
    # 대화 문서는 붙여넣기 때문에 펜스가 실제로 자주 어긋난다(실측: vault 133건 중 3건).
    # 코드블록 안의 헤더를 한 번 잘못 읽는 쪽이, 뒤쪽 섹션을 통째로 잃는 쪽보다 낫다.
    return spans


def _body_start(txt):
    m = re.match(r"^---\n.*?\n---\n", txt, re.S)
    return m.end() if m else 0


def _find_header(txt, header, start=0):
    """본문에서 **줄 전체가** 그 헤더인 위치. frontmatter 의 `title: "## 📌 결론"` 도,
    코드펜스 안의 예시도 섹션으로 세지 않는다 — 둘 다 실제 파일에서 오독을 만들었다."""
    off = max(start, _body_start(txt))
    fences = _fence_spans(txt)
    for mm in re.compile(rf"^{re.escape(header)}[ \t]*$", re.M).finditer(txt, off):
        if not any(a <= mm.start() < b for a, b in fences):
            return mm.start()
    return -1


def _next_section(txt, pos, pattern=r"^#{2,3} "):
    """pos 이후 다음 섹션 머리의 **앞 개행 위치**(없으면 -1). `txt.find("\n## ")` 대체다 —
    코드펜스 안의 헤더에서 끊기지 않게 한다."""
    fences = _fence_spans(txt)
    for mm in re.compile(pattern, re.M).finditer(txt, pos):
        i = mm.start()
        if any(a <= i < b for a, b in fences):
            continue
        return i - 1 if i and txt[i - 1] == "\n" else i
    return -1


def _section_items(txt, header):
    """'## 헤더' 아래의 '- ' 항목들. 다음 제목(`# ` 또는 `## `)에서 멈춘다.

    `## ` 만 보면 대화 문서의 `# 💬 … 대화` 를 넘어가 대화 본문의 '- ' 줄까지 끌어온다
    (마지막 `##` 섹션에서 실측). `### ` 는 진행 로그 블록 머리라 경계로 쓰지 않는다.
    """
    out, i = [], _find_header(txt, header)
    # 헤더가 여러 번 나오면 **전부** 모은다. 같은 날 두 번째 flush 는 대화 문서에
    # 머리말을 append 하므로(중복이 유실보다 낫다), 첫 블록만 읽으면 뒤 결론이 사라진다.
    while i != -1:
        for ln in txt[i + len(header):].splitlines():
            s = ln.strip()
            if re.match(r"^#{1,2}\s", s):
                break
            if s.startswith("- ") and s != "- _(없음)_":
                out.append(s[2:].strip())
        i = _find_header(txt, header, i + len(header))
    return out


SUBSTR_MIN = 6      # 부분 포함으로 중복 판정하려면 이 길이 이상이어야 한다

# 태스크 전용 임계값. 결론·접은 안(긴 문장)의 0.75 를 태스크에 쓰면 **형제 작업이 삼켜진다** —
# 'skillflo sink PII 컬럼 제거 적용' vs 'skillmatch …' 가 0.818, cutover 쌍이 0.857.
# 실측 ①: 실제 태스크 94개 전수 쌍 비교에서 0.70 이상인 쌍은 둘뿐이었고 **둘 다 서로 다른 작업**이었다.
# 실측 ②: 접두어가 긴 형제 쌍은 더 올라간다 — 'dim_voucher …' vs 'dim_voucher_use …' 가 0.923.
# 그래서 0.93 도 여유가 0.007 밖에 안 된다.
# **오탐과 미탐의 비용이 대칭이 아니다.** 미탐은 눈에 보이는 중복 한 줄이라 지우면 끝이고,
# 오탐은 태스크가 소리 없이 사라지는 것이다. 그래서 사실상 완전일치에 가깝게 잡는다.
TASK_DUP_RATIO = 0.97


def _norm_line(s):
    return re.sub(r"[\s`*_·,.\-—()\[\]]+", "", s).lower()


def _dedup_against(items, existing, threshold=0.75, substr=True):
    """기존 항목과 유사한 것을 걸러낸다. 요약기는 기존 목록을 못 보므로
    (topic 이 같은 콜에서 정해져 프롬프트에 미리 넣을 수 없다) 파이썬에서 막는다.

    `substr=False` 는 **태스크용**이다. 순차 로드맵은 앞 단계가 뒤 단계의 부분 문자열인 것이
    정상이라('운영 데이터 백필' → '운영 데이터 백필 결과 검증') 포함 판정이 곧바로 유실이 된다.
    """
    norm = _norm_line
    out, seen = [], [norm(e) for e in existing]
    for it in items:
        n = norm(it)
        if not n:
            continue
        # 부분 포함 검사의 최소 길이는 **짧은 쪽**에 건다. 새 항목 길이만 보면
        # 기존의 "배포" 가 새 "VM 재배포 후 검증" 안에 있다는 이유로 새 항목이 사라진다.
        if any((substr and min(len(n), len(e)) >= SUBSTR_MIN and (n in e or e in n))
               or difflib.SequenceMatcher(None, n, e).ratio() >= threshold
               for e in seen):
            continue
        out.append(it.strip())
        seen.append(n)   # 같은 응답 안에서의 중복도 막는다
    return out


def _append_section(txt, header, items):
    """섹션 끝에 항목을 append. 섹션이 없으면 만들지 않는다(스키마 보존).
    기존 줄은 절대 고치지 않는다 — 사람이 다듬은 문장을 훅이 덮지 않기 위함."""
    if not items:
        return txt
    i = _find_header(txt, header)
    if i == -1:
        return txt
    j = _next_section(txt, i + 1, r"^## ")
    body = (txt[i:j] if j != -1 else txt[i:]).replace("- _(없음)_", "").rstrip()
    body += "\n" + "\n".join(f"- {x}" for x in items) + "\n"
    return txt[:i] + body + (f"\n{txt[j + 1:]}" if j != -1 else "")


def _topic_meta(path):
    """frontmatter(status/title/plan) + '🔜 다음' 첫 줄."""
    meta = {"status": "active", "title": "", "next": "", "plan": "", "updated": "", "created": "",
            "cwd": "", "branch": "", "head": "", "session": "", "verified": "", "verified_head": "", "workspaces": "", "repos": "", "blocker": ""}
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return meta
    m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
    if m:
        for ln in m.group(1).splitlines():
            k, _, v = ln.partition(":")
            k, v = k.strip(), v.strip().strip('"')
            if k in ("status", "title", "plan", "updated", "created",
                     "cwd", "branch", "head", "session", "verified", "verified_head", "workspaces", "repos", "blocker"):
                meta[k] = v
    i = _find_header(txt, NEXT_HEADER)
    if i != -1:
        for ln in txt[i + len(NEXT_HEADER):].splitlines():
            s = ln.strip().lstrip("-").strip()
            if s.startswith("#"):
                break
            if s:
                meta["next"] = s
                break
    return meta


def _topic_choices(base, open_lines=()):
    """프롬프트에 줄 (slug, title) 목록. 완료(status: done) 주제는 제외.

    슬러그만 주면 매칭이 안 된다 — 'session-memory-architecture' 라는 문자열만으로
    그게 무슨 작업인지 요약기가 알 수 없기 때문. title 을 함께 준다.

    **파일 없는 묶음도 넣는다.** 미완료 태스크가 달려 있는 슬러그는 이미 쓰이는 이름이고,
    이어지는 작업이 그리로 붙어야 한다. 빼 두면 요약기가 그 이름을 정확히 골라도
    '미등록 슬러그' 로 버려져 태스크가 무소속으로 흩어진다(실측: watcher-local-test).
    완료 주제를 빼는 이유와 다르다 — 그쪽은 훅이 되살리면 안 되지만, 이쪽은 아직 열려 있다.
    """
    d = os.path.join(base, TOPICS_DIRNAME)
    out, seen = [], set()
    for slug in _topic_slugs(base):
        m = _topic_meta(os.path.join(d, f"{slug}.md"))
        if (m.get("status") or "active") == "done":
            continue
        out.append((slug, m.get("title") or slug))
        seen.add(slug)
    for l in open_lines or ():
        slug = _task_topic(l)
        if slug and slug not in seen:
            out.append((slug, _task_topic_alias(l) or slug))
            seen.add(slug)
    return out


def _fm_list(txt, key):
    """frontmatter 의 `key: [a, b]` 를 리스트로. 없으면 [].

    스칼라(`key: a`)도 받는다 — 못 읽으면 []를 주고 호출부가 그 위에 덮어써서
    **기존 값이 조용히 사라진다.** 사람이 손으로 적으면 스칼라가 되기 쉽다."""
    m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
    if not m:
        return []
    mm = re.search(rf"^{re.escape(key)}:\s*(.*?)\s*$", m.group(1), re.M)
    if not mm:
        return []
    v = mm.group(1).strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [x.strip().strip("\"'") for x in v.split(",") if x.strip()]


def _fm_set(txt, key, value):
    m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
    if not m:
        # frontmatter 가 없으면 **만들어서** 쓴다. 조용히 건너뛰면 INDEX 에서 체크한
        # status: done 이 저장되지 않아 다음 렌더에서 체크가 저절로 풀린다
        # (사람이 Obsidian 에서 만든 빈 주제 파일에서 실측).
        if txt.lstrip().startswith("---"):
            return txt      # 닫히지 않은 frontmatter — 망가진 파일은 건드리지 않는다
        return f"---\n{key}: {value}\n---\n\n" + txt.lstrip("\n")
    fm, line = m.group(1), f"{key}: {value}"
    if re.search(rf"^{re.escape(key)}:.*$", fm, re.M):
        fm = re.sub(rf"^{re.escape(key)}:.*$", line, fm, count=1, flags=re.M)
    else:
        fm = fm.rstrip() + "\n" + line
    return f"---\n{fm}\n---\n" + txt[m.end():]


TOPIC_SECTIONS = (CONCLUSION_HEADER, DROPPED_HEADER, PROGRESS_HEADER, NEXT_HEADER)


def _ensure_sections(txt):
    """필수 4섹션이 없으면 만들어 둔다.

    `_append_section` 은 헤더가 없으면 무동작이고 `_append_topic` 은 성공을 돌려주므로,
    섹션이 빠진 주제 파일에서는 결론·접은 안이 **성공 로그를 남기고 사라진다.**
    사람이 frontmatter 만 적어 만든 주제가 정확히 그 상태다."""
    missing = [h for h in TOPIC_SECTIONS if _find_header(txt, h) == -1]
    if not missing:
        return txt
    return txt.rstrip() + "".join(f"\n\n{h}\n" for h in missing) + "\n"


def _fm_del(txt, key):
    """frontmatter **안에서만** 키를 지운다.

    문서 전체에 `^key:.*` 를 걸면 본문이나 코드블록의 `blocker: 외부 승인` 같은 줄까지
    지운다 — 사람이 적은 문장이 조용히 사라지는 부류다."""
    m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
    if not m:
        return txt
    fm = re.sub(rf"^{re.escape(key)}:.*\n?", "", m.group(1) + "\n",
                count=1, flags=re.M).rstrip()
    return f"---\n{fm}\n---\n" + txt[m.end():]


def _append_topic(base, slug, date, sid8, progress, next_step,
                  conclusions=(), dropped=(), cwd=None, session_id=None, verified=None,
                  blocker=None):
    """주제 파일의 📈 진행 로그에 prepend + frontmatter 갱신.
    파일이 없으면 만들지 않는다 — 신규 주제 생성은 사용자 지시로만(결정 E)."""
    path = os.path.join(base, TOPICS_DIRNAME, f"{slug}.md")
    if not os.path.exists(path):
        _debug(f"[worker] topics/{slug}.md 없음 — daily 에만 기록")
        return False
    txt = _ensure_sections(open(path, encoding="utf-8").read())

    # ① 결론·접은 안 — append 만 한다. 기존 줄은 고치지 않는다.
    #    요약기는 기존 목록을 볼 수 없으므로(topic 이 같은 콜에서 정해진다) 여기서 중복을 막는다.
    #    진행 로그 중복 여부와 **무관하게** 먼저 처리한다 — 같은 날 두 번째 flush 에서
    #    새로 나온 결론이 유실되면 안 되기 때문이다.
    for header, items in ((CONCLUSION_HEADER, conclusions), (DROPPED_HEADER, dropped)):
        fresh = _dedup_against(list(items or ()), _section_items(txt, header))
        if fresh:
            txt = _append_section(txt, header, fresh)
            _debug(f"[worker] topics/{slug}: {header} +{len(fresh)}건")

    # ② 진행 로그 — 같은 (날짜, 세션) 블록이 이미 있으면 이 단계만 건너뛴다.
    # 같은 (날짜, 세션) 블록이 이미 있으면 **그 블록 끝에 이어붙인다.**
    # 건너뛰면 하루에 두 번 flush 될 때(자정 자동 flush → 그날 세션 종료) 뒤쪽 진행이 유실된다.
    marker = f"### {date}  [[{CONV_DIRNAME}/{sid8}_{date}"
    i0 = txt.find(marker)
    if i0 != -1:
        # 블록의 끝은 **다음 ### 이거나 다음 ## 이거나 문서 끝** 중 가장 앞이다.
        # ### 만 보면 그 뒤에 오는 '## 🔜 다음' 을 넘어 문서 끝에 붙고,
        # 아래 '다음' 교체가 그 구간을 통째로 지운다 — 로그는 '이어붙임' 성공으로 남는다(실측).
        ends = [x for x in (_next_section(txt, i0 + 1, r"^### "),
                            _next_section(txt, i0 + 1, r"^## ")) if x != -1]
        blk_end = min(ends) if ends else len(txt.rstrip())
        # 진행 로그는 **완전 일치**만 중복으로 본다. 유사도(0.75)를 쓰면
        # "오전 작업"/"오후 작업" 처럼 짧고 다른 줄이 오탐으로 사라진다 — 유실보다 중복이 낫다.
        have = {_norm_line(l.strip().lstrip("-").strip())
                for l in txt[i0:blk_end].splitlines() if l.strip().startswith("-")}
        fresh = [x for x in (l.strip().lstrip("-").strip()
                             for l in progress.splitlines() if l.strip().startswith("-"))
                 if x and _norm_line(x) not in have]
        if fresh:
            add = "\n" + "\n".join(f"- {x}" for x in fresh)
            txt = txt[:blk_end] + add + txt[blk_end:]
            _debug(f"[worker] topics/{slug}: {date}/{sid8} 블록에 +{len(fresh)}줄 이어붙임")
        else:
            _debug(f"[worker] topics/{slug}: {date}/{sid8} 새 진행 없음")
    else:
        block = f"{marker}|💬 대화]]\n{progress.rstrip()}"
        i = _find_header(txt, PROGRESS_HEADER)
        if i == -1:
            txt = txt.rstrip() + f"\n\n{PROGRESS_HEADER}\n\n{block}\n"
        else:
            head = txt[:i + len(PROGRESS_HEADER)]
            tail = txt[i + len(PROGRESS_HEADER):].lstrip("\n")
            txt = f"{head}\n\n{block}\n\n{tail}"

    txt = _fm_set(txt, "updated", date)
    # ── 인계 패킷 ────────────────────────────────────────────────────
    # 재개에 필요한 것은 "어느 세션이었나"보다 "어디서·어떤 코드 상태에서 하던 일인가"다.
    if cwd:
        txt = _fm_set(txt, "cwd", cwd)
        br, head = _git_state(cwd)
        if br:
            txt = _fm_set(txt, "branch", br)
        if head:
            txt = _fm_set(txt, "head", head)
    if session_id:
        txt = _fm_set(txt, "session", session_id)      # 마지막 세션 full id — resume 대상
    # blocker 는 풀리면 사라져야 하므로 매번 덮어쓴다(없으면 지운다)
    if blocker:
        txt = _fm_set(txt, "blocker", _yaml_val(blocker))
    else:
        txt = _fm_del(txt, "blocker")
    if verified:
        txt = _fm_set(txt, "verified", _yaml_val(verified))
        # 검증은 특정 코드 상태에 대한 것이다. dirty 면 그 HEAD 가 검증 대상을 대표하지 못한다.
        _, vh = _git_state(cwd) if cwd else (None, None)
        dirty = any(W_DIRTY in w for w in _repo_warnings(cwd)) if cwd else True
        if vh and not dirty:
            txt = _fm_set(txt, "verified_head", vh)
        else:
            txt = _fm_del(txt, "verified_head")
    # 작업 경로 — 이어서 하려면 어디로 cd 할지가 필요한데 지금은 `plan:` 이 있는 주제만 알 수 있다.
    # cwd 는 파이썬이 이미 아는 값이라 0토큰이다. 한 주제가 여러 repo 에 걸치므로 누적한다.
    if cwd:
        repo = cwd.rstrip("/").split("/")[-1]
        cur = _fm_list(txt, "repos")
        if repo and repo != "?" and repo not in cur:
            txt = _fm_set(txt, "repos", "[" + ", ".join(cur + [repo]) + "]")
    if next_step:
        # '다음'의 정본은 '## 🔜 다음' 섹션 하나다 (_topic_meta 가 여기서 읽는다).
        # frontmatter 에 next 를 중복 기록하지 않는다.
        j = _find_header(txt, NEXT_HEADER)
        if j != -1:
            # '🔜 다음' 이 마지막 섹션이면 -1 이 온다.
            # txt[-1:] 로 흘러가면 마지막 문자가 뒤에 붙으므로 명시적으로 분기한다.
            k = _next_section(txt, j + 1, r"^#{2,}")
            tail = txt[k:].lstrip("\n") if k != -1 else ""
            body = f"{NEXT_HEADER}\n\n- {next_step.strip()}\n"
            txt = txt[:j] + body + (f"\n{tail}" if tail else "")
    _atomic_write(path, txt.rstrip() + "\n")
    return True


CONV_PROGRESS_HEADER = "## 📈 이날 진행"


_HISTORY_MAP = None


def _history_map():
    """sid8 → (ts, full session id, 작업 경로). 파일이 2MB·6천 줄이라 **프로세스당 한 번만** 읽는다."""
    global _HISTORY_MAP
    if _HISTORY_MAP is not None:
        return _HISTORY_MAP
    m = {}
    try:
        with open(os.path.expanduser("~/.claude/history.jsonl"), encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                sid, proj = r.get("sessionId") or "", r.get("project")
                if not (sid and proj):
                    continue
                ts, prev = r.get("timestamp") or 0, m.get(sid[:8])
                if not prev or ts > prev[0]:
                    m[sid[:8]] = (ts, sid, proj)
    except OSError:
        pass
    _HISTORY_MAP = m
    return m


def _history_lookup(sid8):
    """sid8 → (full session id, 작업 경로).

    대화 문서에는 sid8 밖에 없는데 `claude -r` 은 full id 를 받는다. history.jsonl 은
    트랜스크립트(30일 보관)보다 오래 남아, 만료된 세션의 작업 경로도 여기서만 알 수 있다.
    """
    r = _history_map().get(sid8)
    return (r[1], r[2]) if r else (None, None)


def _seed_topic(base, slug, title, lines):
    """사람이 만든 **빈 주제 파일**에 뼈대·제목·기존 대화 기록을 채운다.

    '묶음을 주제로 올리려면 topics/<slug>.md 를 만들기만 하면 된다'가 문서화된 경로인데
    빈 파일은 세 곳에서 조용히 깨진다 — ① 제목이 없어 목차에 슬러그가 그대로 뜨고,
    ② frontmatter 가 없어 INDEX 주제 체크가 저장되지 않으며,
    ③ 섹션이 없어 이후 세션의 결론·접은 안이 버려진다(_append_section 은 헤더가 없으면 무동작).
    **파일을 만들지는 않는다** — 신규 주제 생성은 여전히 사람 몫이다(결정 E).
    """
    path = os.path.join(base, TOPICS_DIRNAME, f"{slug}.md")
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return False
    if txt.lstrip().startswith("---"):
        return False                       # 이미 제 모양을 갖춘 파일
    # 그 주제를 가리키던 대화에서 결론·진행을 끌어온다. 승격 시점에 정본이 비어 있으면
    # 사람이 손으로 옮겨야 하고, 안 옮기면 대화가 만료될 때 같이 사라진다.
    refs = _session_refs(lines)[:5]        # 최근 대화가 진행 로그 맨 위
    concl, dropped, blocks = [], [], []
    for date, _sid8, name in refs:
        try:
            ct = open(os.path.join(base, CONV_DIRNAME, f"{name}.md"), encoding="utf-8").read()
        except OSError:
            continue
        concl += _dedup_against(_section_items(ct, CONCLUSION_HEADER), concl)
        dropped += _dedup_against(_section_items(ct, DROPPED_HEADER), dropped)
        prog = _section_items(ct, CONV_PROGRESS_HEADER)
        if prog:
            blocks.append(f"### {date}  [[{CONV_DIRNAME}/{name}|💬 대화]]\n"
                          + "\n".join(f"- {x}" for x in prog))
    date = _last_activity(lines) or datetime.now().strftime("%Y-%m-%d")
    # 재개 좌표. 없으면 목차에 제목만 뜨고 '어디서 이어서 하나'가 다시 사라진다.
    # branch·head 는 넣지 않는다 — 그 세션 당시 값을 모르므로, 지금 값을 적으면
    # '변화 없음'이라는 없는 확신을 만든다(INDEX 는 '기준 HEAD 미기록'으로 표시된다).
    sess = cwd = None
    for _d, sid8, _n in refs:             # 최근 대화부터 — 재개 대상은 마지막 세션이다
        sess, cwd = _history_lookup(sid8)
        if sess:
            break
    fm = [f"title: {_yaml_val(title)}", "status: active",
          f"created: {date}", f"updated: {date}"]
    if sess:
        fm.append(f"session: {sess}")
    if cwd:
        fm.append(f"cwd: {cwd}")
        repo = cwd.rstrip("/").split("/")[-1]
        if repo:
            fm.append(f"repos: [{repo}]")

    def sec(header, items):
        body = "\n".join(f"- {x}" for x in items) if items else "- _(없음)_"
        return f"{header}\n\n{body}\n\n"

    out = ("---\n" + "\n".join(fm) + "\n---\n\n"
           + sec(CONCLUSION_HEADER, concl)
           + sec(DROPPED_HEADER, dropped)
           + f"{PROGRESS_HEADER}\n\n" + ("\n\n".join(blocks) + "\n\n" if blocks else "")
           + f"{NEXT_HEADER}\n")
    body = txt.strip()
    if body:                               # 사람이 적어 둔 메모는 지우지 않는다
        out += f"\n{body}\n"
    _atomic_write(path, out)
    _debug(f"[worker] topics/{slug}: 빈 주제 파일 채움 (대화 {len(refs)}건 반영)")
    return True


def _sync_topic_status(base):
    """INDEX 에서 체크된 주제를 `topics/<slug>.md` 의 status: done 으로 반영한다.
    주제를 닫는 것도 목차에서 할 수 있어야 한다 — 안 그러면 태스크가 0이 된 주제가
    '끝난 것'인지 '아직 안 쪼갠 것'인지 목차만 봐서는 알 수 없다."""
    p = os.path.join(base, INDEX_FILENAME)
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for ln in lines:
        m = TOPIC_LINE_RE.match(ln)
        if not m or m.group(1).lower() != "x":
            continue
        tp = os.path.join(base, TOPICS_DIRNAME, f"{m.group(3)}.md")
        if not os.path.exists(tp):
            continue
        with open(tp, encoding="utf-8") as f:
            txt = f.read()
        if re.search(r"^status:\s*done\s*$", txt, re.M):
            continue
        txt = _fm_set(txt, "status", "done")
        txt = _fm_set(txt, "updated", datetime.now().strftime("%Y-%m-%d"))
        if not re.search(r"^status:\s*done\s*$", txt, re.M):
            # 성공 로그만 남고 실제로는 안 써지면 '체크가 저절로 풀리는' 현상이 되는데,
            # 로그가 거짓이라 원인을 찾을 수 없다. 실패는 실패로 남긴다.
            _debug(f"[worker] topics/{m.group(3)}: status 기록 실패 — frontmatter 확인 필요")
            continue
        _atomic_write(tp, txt)
        _debug(f"[worker] topics/{m.group(3)}: status → done (INDEX 체크)")


def _resumable(session_id):
    """원본 트랜스크립트가 아직 있는가. 30일이 지나면 지워져 --resume 이 불가능하다."""
    if not session_id:
        return False
    pat = os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl")
    return bool(glob.glob(pat))


def _ago(datestr):
    """'오늘 · 어제 · 5일 전 · 3주 전'. 절대 날짜보다 판단이 빠르다."""
    try:
        d = datetime.strptime(datestr, "%Y-%m-%d").date()
    except Exception:
        return ""
    n = (datetime.now().date() - d).days
    if n <= 0: return "오늘"
    if n == 1: return "어제"
    if n < 14: return f"{n}일 전"
    if n < 60: return f"{n // 7}주 전"
    return f"{n // 30}개월 전"


# 경고 문자열을 부분일치로 판정하면 **`미커밋` 안에 `커밋` 이 들어 있어** 오판한다(실측:
# 미커밋 변경만 있는데 '검증 이후 코드 변경됨' 이 떴다). 판정에 쓰는 조각은 여기 모아 둔다.
W_AHEAD, W_DIVERGED, W_DIRTY = "그 뒤 ", "분기됨", "미커밋"
W_UNKNOWN = ("판정 불가", "경로 없음")


def _drifted(warns):
    """기록 시점 이후 **커밋 수준의 변경**이 있었나. 미커밋 변경은 별도로 본다."""
    return any(W_AHEAD in w or W_DIVERGED in w for w in warns)


def _repo_warnings(path, branch=None, head=None, label=None, cache=None):
    """재개 전에 알아야 할 저장소 상태를 경고 목록으로. **'변화 없음'과 '측정 불가'를 구분한다.**

    기준 HEAD 가 없으면 0커밋이 아니라 '판정 불가'다 — 없는 확신을 만들지 않기 위함이다.
    """
    # cache 는 **호출자가 만들어 넘기는 렌더 스코프 딕트**다. 전역으로 두면 커밋을 한 뒤에도
    # 옛 판정이 남는다(실측: 테스트의 드리프트 감지가 깨졌다). 같은 저장소를 경고·dirty·verified
    # 세 군데서 다시 묻고 한 번이 git 명령 3~4회라, 렌더 안에서만 재사용한다
    # (실측: 주제 20개 렌더에 git 20회·393ms — FileChanged 는 사람이 체크할 때마다 돈다).
    ck = (path, branch, head, label)
    if cache is not None and ck in cache:
        return cache[ck]
    out = []
    tag = f"{label}: " if label else ""
    if not (path and os.path.isdir(path)):
        out = [f"{tag}경로 없음"]
        if cache is not None:
            cache[ck] = out
        return out
    def git(*a):
        try:
            return subprocess.run(["git", "-C", path, *a], capture_output=True, text=True, timeout=10)
        except Exception:
            return None
    cur_br = git("rev-parse", "--abbrev-ref", "HEAD")
    cur_br = cur_br.stdout.strip() if cur_br and cur_br.returncode == 0 else None
    if branch and cur_br and branch != cur_br:
        out.append(f"{tag}브랜치 {branch}→{cur_br}")
    if not head:
        out.append(f"{tag}기록 HEAD 없음 — 변경 판정 불가")
    else:
        ok = git("cat-file", "-e", f"{head}^{{commit}}")
        if not ok or ok.returncode != 0:
            out.append(f"{tag}기록 커밋 없음 — 판정 불가")
        else:
            anc = git("merge-base", "--is-ancestor", head, "HEAD")
            if anc is None:
                out.append(f"{tag}판정 불가")
            elif anc.returncode != 0:
                out.append(f"{tag}브랜치 분기됨")
            else:
                r = git("rev-list", "--count", f"{head}..HEAD")
                if r and r.returncode == 0 and r.stdout.strip().isdigit():
                    n = int(r.stdout.strip())
                    if n:
                        out.append(f"{tag}그 뒤 {n}커밋")
    st = git("status", "--porcelain")
    if st and st.returncode == 0 and st.stdout.strip():
        # 파일 수까지 준다. '있음' 만으로는 손댈 만한 일인지 판단이 안 돼서
        # 결국 저장소에 가서 다시 봐야 한다 — 목차가 답을 못 주는 셈이다.
        out.append(f"{tag}미커밋 변경 {len(st.stdout.strip().splitlines())}개 파일")
    if cache is not None:
        cache[ck] = out
    return out


_REPO_ROOT_CACHE = {}


def _repo_root(path):
    """`.git` 이 나올 때까지 올라간다.

    하위 디렉터리를 cwd 로 기록한 세션은 저장소로 인식되지 않아 **드리프트가 통째로 숨는다**
    (실측: `repo/pkg` 에서 작업하면 INDEX 에 경고가 하나도 안 뜬다).
    worktree 는 `.git` 이 파일이므로 isdir 가 아니라 exists 로 본다."""
    # **실제로 존재하는 디렉터리만** 출발점으로 받는다. 없는 경로나 상대 경로를 넣으면
    # `abspath` 가 프로세스 cwd 기준으로 풀려 **전혀 상관없는 상위 저장소**를 잡는다
    # (실측: vault 안에서 렌더하면 vault 자신이 작업 저장소로 들어왔다).
    if not path or not os.path.isdir(path):
        return None
    if path in _REPO_ROOT_CACHE:
        return _REPO_ROOT_CACHE[path]
    def walk(start):
        q = start
        while q and q != "/":
            if os.path.exists(os.path.join(q, ".git")):
                return q
            nxt = os.path.dirname(q)
            if nxt == q:
                return None
            q = nxt
        return None

    ap = os.path.abspath(path)
    root = None
    # symlink 이 없는 흔한 경우는 파일 시스템만 봐도 답이 같다 — git 을 부르면
    # 렌더가 주제 수만큼 느려진다(실측: 268ms → 407ms).
    if os.path.realpath(ap) == ap:
        root = walk(ap)
    if root is None:
        try:
            # `.git` 을 찾아 올라가는 방식은 **symlink 에서 틀린다** — 저장소 밖을 가리키는
            # 링크는 못 찾고, 상위 저장소 안에 있는 링크는 엉뚱하게 상위를 집는다(실측).
            r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                root = r.stdout.strip()
        except Exception:
            root = None
    if root is None:
        root = walk(os.path.realpath(ap))
    # 음성 결과도 담는다 — 비저장소 cwd 는 렌더 한 번에 여러 번 조회된다(주제당 2회).
    # 대신 캐시 수명을 **렌더 1회**로 묶어(_write_index 에서 비운다) 오래 도는 --catchup
    # 도중에 저장소가 생기거나 symlink 가 바뀌어도 다음 렌더에서 다시 본다.
    _REPO_ROOT_CACHE[path] = root
    return root


def _workspaces(m):
    """주제가 걸친 저장소들 → [(path, head)].

    한 주제가 여러 repo 를 오가면(nl2sql-slack = data-nl2sql + data-dbt) cwd 하나만
    검사해서는 다른 repo 의 변화를 놓친다. cwd 자체가 git 저장소가 아닐 수도 있다
    (day1 처럼 여러 repo 를 담은 상위 폴더) — 그때는 repos 에 적힌 하위 저장소를 본다.
    """
    cwd = (m.get("cwd") or "").rstrip("/")
    out, seen = [], set()

    def add(path, sha=None):
        root = _repo_root((path or "").rstrip("/"))
        if root and root not in seen:
            seen.add(root)
            out.append((root, sha or None))

    add(cwd, m.get("head"))                      # cwd 가 저장소면 그것이 기준
    for src in (m.get("workspaces"), m.get("repos")):
        for item in re.findall(r"[^\s,\[\]]+", src or ""):
            name, _, sha = item.partition("@")
            if name.startswith("/"):
                add(name, sha)
            else:
                add(os.path.join(cwd, name), sha)                     # cwd 하위
                add(os.path.join(os.path.dirname(cwd), name), sha)    # cwd 형제
    return out


def _topic_order(base):
    """진행 중 주제를 표시 순서대로. INDEX 와 작업현황이 **같은 번호**를 쓰게 하려면
    순서를 한 곳에서 정해야 한다. 두 파일이 다른 순서를 내면 5-2 를 보고 찾아갈 수 없다."""
    d = os.path.join(base, TOPICS_DIRNAME)
    entries = []
    for slug in _topic_slugs(base):
        m = _topic_meta(os.path.join(d, f"{slug}.md"))
        st = m.get("status") or "active"
        if st != "done":
            entries.append((slug, m, st))
    # 최근 활동 역순. 이어서 할 것은 대개 최근 것이므로 알파벳 순은 찾는 비용만 만든다.
    entries.sort(key=lambda e: (e[1].get("updated") or e[1].get("created") or ""), reverse=True)
    entries.sort(key=lambda e: e[2] == "paused")   # 보류는 뒤로 (stable)
    return entries


def _open_tasks_by_topic(base, open_lines=None):
    """미완료 태스크를 주제별로 나눈다 → ({slug: [원문 줄]}, [무소속 줄]).
    open_lines 가 없으면 INDEX 자신에서 읽는다(사람이 체크만 한 경우)."""
    if open_lines is None:
        p = os.path.join(base, INDEX_FILENAME)
        open_lines = _split_tasks(open(p, encoding="utf-8").read())[0] if os.path.exists(p) else []
    by_topic, orphan = {}, []
    for l in open_lines:
        t = _task_topic(l)
        (by_topic.setdefault(t, []) if t else orphan).append(l)
    return by_topic, orphan


def _shorten_done(line, base):
    """완료 줄에서 **매 줄 반복되는 것**을 줄인다 — 주제 제목과 대화 링크.

    완료 25건 기준 실측: 한 줄 중앙값 153자·최대 218자였고, 그 대부분이 주제 제목(40자)과
    대화 링크(45자)의 반복이었다.

    **주제 파일이 있을 때만 줄인다.** 파일 없는 묶음은 링크 alias 가 제목의 유일한 저장소이고
    대화 링크가 그 작업의 유일한 흔적이라, 줄이면 되짚을 길이 사라진다.
    `✅ 날짜`·`[[topics/…]]` 는 남긴다 — 아카이브 이동과 주제 그룹핑이 그것으로 돈다.
    """
    slug = _task_topic(line)
    if not slug or not os.path.exists(os.path.join(base, TOPICS_DIRNAME, f"{slug}.md")):
        return line
    line = re.sub(r"(\[\[" + re.escape(TOPICS_DIRNAME) + r"/[^|\]]+\|)[^\]]*\]\]",
                  r"\g<1>🔧]]", line)
    # 주제 링크 외의 링크(대화·구설계 세션)를 뗀다. 그 내용은 주제 파일의 진행 로그에 있다.
    line = re.sub(r"\s*\[\[(?!" + re.escape(TOPICS_DIRNAME) + r"/)[^\]]*\]\]", "", line)
    return line


def _write_index(base, open_lines=None, done_lines=None, db_path=DB_FILE, alerts=()):
    """INDEX.md 재생성. **태스크의 정본이자 목차**다.

    사람은 체크박스만 건드리고 나머지는 자동 생성이다. open/done 을 주지 않으면
    현재 파일에서 읽어 그대로 다시 렌더한다(FileChanged 로 체크만 바뀐 경우)."""
    d = os.path.join(base, TOPICS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(base, INDEX_FILENAME)
    wcache = {}                # 이 렌더 동안만 git 판정을 재사용한다
    _REPO_ROOT_CACHE.clear()   # 저장소 루트 판정도 렌더 단위로만 재사용한다
    if open_lines is None or done_lines is None:
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        o, dn = _split_tasks(cur)
        open_lines = o if open_lines is None else open_lines
        done_lines = dn if done_lines is None else done_lines

    # 빈 주제 파일 채우기. **status 반영보다 먼저**다 — 체크를 저장할 frontmatter 가
    # 없으면 status: done 이 갈 곳이 없고, 닫히는 주제도 제목 없는 껍데기로 남는다.
    all_lines = list(open_lines or []) + list(done_lines or [])
    for slug in _topic_slugs(base):
        rel = [l for l in all_lines if _task_topic(l) == slug]
        title = next((a for a in (_task_topic_alias(l) for l in rel) if a), "") or slug
        _seed_topic(base, slug, title, rel)
    _sync_topic_status(base)   # 체크된 주제는 목록에서 빠지도록 반영

    by_topic, orphan = _open_tasks_by_topic(base, open_lines)
    # 🔜 다음 은 그 주제로 세션이 돌 때만 갱신된다. 태스크만 체크하고 작업을 안 하면
    # 이미 끝낸 일을 계속 '다음'으로 가리킨다 — 미완료 0 + 완료 있음이 그 신호다.
    done_by_topic, done_date_by_topic = {}, {}
    for l in (done_lines or []):
        t = _task_topic(l)
        if t:
            done_by_topic[t] = done_by_topic.get(t, 0) + 1
            dd = _done_date(l)
            if dd and dd > done_date_by_topic.get(t, ""):
                done_date_by_topic[t] = dd
    summary_bits, dirty_repos, newest = [], [], None
    active, paused, n = [], [], 0
    for n, (slug, m, st) in enumerate(_topic_order(base), 1):
        mine = by_topic.pop(slug, [])
        # 제목의 ] · | 는 위키링크 문법을 깨므로 치환한다
        disp = re.sub(r"[\[\]|]", "·", m.get("title") or slug)
        line = f"- [ ] **{n}.** [[{TOPICS_DIRNAME}/{slug}|{disp}]]"
        # 언제·어디서 하던 일인가 — 재개 판단에 제목보다 먼저 필요한 정보다.
        bits = [x for x in (_ago(m.get("updated") or m.get("created")),
                            os.path.basename(m.get("cwd") or "") or None) if x]
        if st == "paused":
            bits.append("보류")
        if bits:
            line += " · " + " · ".join(bits)
        line += f" · 남은 일 {len(mine)}" if mine else ""
        stale_next = not mine and done_by_topic.get(slug) and m.get("next")
        if not mine and m.get("next"):
            line += f" — {m['next']}"
        # 재개 한 줄. 원본이 30일 지나 지워졌으면 문서 재개로 안내한다.
        sess, cwd = m.get("session"), m.get("cwd")
        ws = _workspaces(m)          # 재개 명령과 검증 무효화가 같은 목록을 본다
        if not cwd and m.get("plan"):
            # cwd 가 없으면 plan 이 있는 저장소를 시작 위치로 삼는다 — 명령이 아예 없는 것보다 낫다
            # 변수명이 d 면 바깥의 topics 디렉터리를 덮어써 아래 묶음 제목 조회가 틀어진다
            pd = os.path.dirname(m["plan"])
            while pd and pd != "/" and not os.path.isdir(os.path.join(pd, ".git")):
                pd = os.path.dirname(pd)
            cwd = pd if pd and pd != "/" else None
        if cwd:
            # 만료됐을 때도 **그대로 실행되는** 명령을 준다. "파일을 읽혀서 재개하라"는
            # 안내문은 사람이 다시 조립해야 하므로 재개 경로가 아니다.
            # cd 한 뒤 실행되므로 vault 는 절대경로 + --add-dir 로 접근 권한을 미리 준다.
            # 경로·문구는 전부 shlex 로 인용한다. 제목이나 plan 경로에 따옴표·백틱·공백·$ 가
            # 들어가면 그대로 복사한 명령이 깨진다(실측: bash -n 구문 오류).
            docs = os.path.join(base, TOPICS_DIRNAME, f"{slug}.md")
            if m.get("plan"):
                docs += f" 와 {m['plan']}"
            boot = (f"{docs} 를 읽고, 기록 시점과 지금 git 상태의 차이를 먼저 보고한 뒤 "
                    f"남은 일부터 이어서 하자")
            # git 은 realpath 를 돌려주고 cwd 는 기록된 그대로다. 문자열로 비교하면
            # symlink 를 지나는 경로에서 같은 저장소가 --add-dir 로 한 번 더 붙는다.
            rc = os.path.realpath(cwd)
            extra = " ".join(f"--add-dir {shlex.quote(w)}"
                             for w, _ in ws if os.path.realpath(w) != rc)
            if _resumable(sess):
                # 여러 저장소에 걸친 주제는 재개해도 나머지 저장소에 접근 권한이 없다.
                # `--add-dir` 는 `--resume` 과 함께 쓸 수 있다(claude --help 확인).
                line += (f"\n  ↳ `cd {shlex.quote(cwd)} && claude"
                         f"{' ' + extra if extra else ''} -r {shlex.quote(sess)}`")
            else:
                line += (f"\n  ↳ `cd {shlex.quote(cwd)} && claude --add-dir {shlex.quote(base)}"
                         f"{' ' + extra if extra else ''} {shlex.quote(boot)}`  (원본 만료)")
            # 저장소 상태 경고 — workspace 마다 따로 잰다
            warns, nobase = [], ""
            # cwd 가 저장소 하위 디렉터리면 ws 에는 **루트**가 담긴다. 문자열로 비교하면
            # 기록 브랜치가 영영 안 넘어가 브랜치 변경만 조용히 빠진다(실측).
            cwd_root = os.path.realpath(_repo_root(cwd) or cwd)
            for wpath, whead in ws:
                for w in _repo_warnings(wpath,
                                        m.get("branch") if os.path.realpath(wpath) == cwd_root else None,
                                        whead, os.path.basename(wpath) if len(ws) > 1 else None,
                                        cache=wcache):
                    if "기록 HEAD 없음" in w:
                        nobase = nobase or "기준 HEAD 미기록"
                    elif "기록 커밋 없음" in w:
                        nobase = nobase or "기준 커밋 확인 불가"
                    elif "판정 불가" in w:
                        nobase = nobase or "Git 비교 불가"
                    else:
                        warns.append(w)
            if nobase:
                line += f"  · {nobase}"
            if warns:
                line += "\n  ⚠ " + " · ".join(warns)
        if stale_next:
            line += ("\n  ⚠ 태스크를 모두 끝냈는데 '다음'이 갱신되지 않았습니다 — "
                     "주제를 닫거나(주제 줄 체크) 다음 할 일을 정하세요")
        if m.get("blocker"):
            line += f"\n  🚧 막힘: {m['blocker']}"
        if m.get("verified"):
            vh, note = m.get("verified_head"), ""
            # 검증 sha 가 어느 저장소 것인지는 적혀 있지 않다. **그 sha 를 아는 저장소**에서
            # 재야 한다 — cwd 만 보면, cwd 가 저장소가 아닐 때 "기록 커밋 없음 — 판정 불가"가
            # 나오고 그 안의 '커밋' 이라는 글자에 걸려 **판정 불가가 '변경됨' 으로 둔갑한다**(실측).
            drift = None
            for wp, _wh in (ws or []) + [(cwd, None)]:
                ws_warn = _repo_warnings(wp, None, vh, cache=wcache) if vh else []
                if any(u in w for w in ws_warn for u in W_UNKNOWN):
                    continue                      # 이 저장소는 그 sha 를 모른다
                drift = _drifted(ws_warn)
                break
            if not vh:
                note = " (검증 시점 미기록)"
            elif drift is None:
                note = " (검증 시점 대조 불가)"
            elif drift:
                note = " ⚠ 검증 이후 코드 변경됨"
            elif any(W_DIRTY in w for wp, _wh in (ws or [(cwd, None)])
                     for w in _repo_warnings(wp, cache=wcache)):
                # 커밋만 보면 같은 HEAD 에서 작업트리만 고친 경우를 놓치고,
                # cwd 만 보면 한 주제가 걸친 **보조 저장소**의 변경을 놓친다.
                note = " ⚠ 검증 이후 미커밋 변경 있음"
            line += f"\n  ✅ 확인: {m['verified']}{note}"
        for j, l in enumerate(mine, 1):
            line += "\n\t" + _numbered(l, f"{n}-{j}")
        if st != "paused":
            if newest is None:
                newest = (m.get("title") or slug, _ago(m.get("updated") or m.get("created")))
            for wpath, _wh in ws:
                nm = os.path.basename(wpath)
                if nm not in dirty_repos and any(W_DIRTY in w
                                                 for w in _repo_warnings(wpath, cache=wcache)):
                    dirty_repos.append(nm)
        (paused if st == "paused" else active).append(line)
    # 남은 슬러그 = 주제 파일이 없거나 완료된 것. 낱개로 흘려보내지 않고 **묶음으로** 렌더한다.
    # 묶음 줄에는 체크박스를 두지 않는다 — 파일이 없어 '묶음 완료' 상태를 저장할 곳이 없고,
    # 체크를 두면 다음 렌더에서 조용히 사라진다. 하위 태스크 체크는 주제와 동일하게 동작한다.
    # 하위가 전부 완료되면 그 슬러그에 미완료가 0개가 되어 묶음 줄 자체가 사라진다.
    groups = []
    for slug in sorted(by_topic, key=lambda s: _last_activity(by_topic[s]) or "", reverse=True):
        mine = by_topic[slug]
        n += 1
        tp = os.path.join(d, f"{slug}.md")
        title = (_topic_meta(tp).get("title") if os.path.exists(tp) else "") \
            or next((a for a in (_task_topic_alias(l) for l in mine) if a), "") or slug
        g = (f"- **{n}.** {title} · 남은 일 {len(mine)}"
             f"{_activity_mark(mine, fallback=done_date_by_topic.get(slug))}"
             f"{_group_resume(mine + [l for l in (done_lines or []) if _task_topic(l) == slug])}")
        for j, l in enumerate(mine, 1):
            g += "\n\t" + _numbered(l, f"{n}-{j}")
        groups.append(g)

    # DB 가 통째로 죽으면 pending 도 못 쓴다 — 그때는 이번 실행이 **메모리로** 들고 온
    # 사실만이 유일한 근거다. 목차에 못 실으면 그 실패는 어디에도 안 남는다.
    saved = _alert_get(db_path)
    for a in list(alerts or ()) + ([saved] if saved else []):
        if a and f"⚠ {a}" not in summary_bits:
            summary_bits.append(f"⚠ {a}")
    rows = _pending_rows(db_path)
    if rows is None:
        summary_bits.append("⚠ pending 조회 실패 — 기록 상태를 확인할 수 없습니다")
        rows = []
    if rows:
        # 조용히 빠지면 '없었던 일' 이 된다. 세는 것은 0토큰이므로 목차에 드러낸다.
        stuck = sum(1 for _, n in rows if n >= PENDING_MAX_TRIES)
        # 명령은 **그대로 복사해 실행되는 형태**로 준다. `session_log.py` 는 PATH 에 없다.
        cmd = f"python3 {shlex.quote(os.path.abspath(__file__))} --retry-pending"
        bit = f"⚠ 기록 실패 {len(rows)}세션"
        bit += (f" — {stuck}건은 자동 재시도 중단" if stuck else " — 다음 세션에서 재시도")
        bit += f" · 지금 회수: `{cmd}`"
        summary_bits.append(bit)
    summary_bits.append(f"진행 중 {len(active)}개")
    if newest:
        summary_bits.append(f"가장 최근 **{newest[0]}**({newest[1]})")
    if dirty_repos:
        summary_bits.append("하다 만 흔적: " + "·".join(dirty_repos[:4]))

    out = ["# 🧭 INDEX", "",
           "> 주제·할 일 목차. **체크박스만 직접 건드리세요** — 나머지는 세션 종료 시 다시 씁니다.",
           "> 주제를 체크하면 그 주제가 닫히고(`status: done`) 목록에서 빠집니다.",
           "> 번호(`5-2`)는 그 시점 렌더의 순번이라 **체크할 때마다 밀립니다** — 남는 문서에 쓰지 마세요.",
           # '3일 전' 도 git 경고도 **렌더 시점의 스냅샷**이다. 파일을 여는 것만으로는
           # 갱신되지 않으므로, 무엇을 기준으로 한 말인지 밝혀야 오독이 안 생긴다.
           f"{RENDER_STAMP_PREFIX}**{datetime.now():%Y-%m-%d %H:%M}** 에 갱신됐습니다 — "
           "경과·git 상태는 그때의 값입니다.", "",
           # 고를 재료는 주되 기계가 고르지는 않는다. 우선순위를 잘못 정하면 정보가 없느니만 못하다.
           "> " + " · ".join(summary_bits), "",
           "## 🔧 진행 중인 주제", ""]
    out += active or ["_(없음)_"]
    if groups or orphan:
        out += ["", "## ☑️ 기타 태스크", ""] + groups + [_numbered(l, None) for l in orphan]
    if paused:  # 지금 손대지 않는 것이므로 아래로
        out += ["", "## ⏸ 보류", ""] + paused
    # 완료 섹션. **원시 HTML(`<details>`)을 쓰지 않는다** — Obsidian 이 그 안의 마크다운을
    # 렌더하지 않아 체크박스도 링크도 없는 텍스트 덩어리로 보인다(실측).
    # 접는 것은 제목 왼쪽 화살표로 하면 된다 — 마크다운 제목은 원래 접힌다.
    done_head = TASKS_DONE_HEADER + (f" · {len(done_lines)}건" if done_lines else "")
    out += ["", done_head, "",
            f"> 제목 왼쪽 화살표로 접을 수 있습니다. "
            f"{DONE_RETAIN_DAYS}일이 지나면 [[{os.path.splitext(ARCHIVE_FILENAME)[0]}]] 로 옮겨집니다.", ""]
    if done_lines:
        # 날짜로 묶는다. '어제 뭘 끝냈나' 가 완료 목록을 여는 이유이고,
        # 묶으면 날짜가 소제목 하나로 접혀 줄마다 반복되지 않는다.
        groups, order = {}, []
        for l in done_lines:                      # 이미 최신 완료가 위로 정렬돼 있다
            d = _done_date(l) or "날짜 미상"
            if d not in groups:
                groups[d] = []
                order.append(d)
            groups[d].append(l)
        for i, d in enumerate(order):
            if i:
                out.append("")
            out.append(f"**{d[5:] if d != '날짜 미상' else d}** ({len(groups[d])}건)")
            out += [_numbered(_shorten_done(l, base), None) for l in groups[d]]
    else:
        out += ["_(완료 항목 없음)_"]
    _safe_write_index(path, "\n".join(out).rstrip() + "\n")


def _write_weekly_digest(base, ref=None):
    """daily 노트 기반 집계 → weekly/<ISO주차>.md (활동일 기준, 0토큰).
    ref가 속한 주를 재생성 (기본: 이번 주)."""
    now = ref or datetime.now()
    iso = now.isocalendar()
    monday = now - timedelta(days=now.weekday())
    days = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    wk_start, wk_end = days[0], days[6]
    weekly_dir = os.path.join(base, "weekly")
    os.makedirs(weekly_dir, exist_ok=True)

    by_proj = {}
    n_entries = 0
    for ds in days:
        fp = os.path.join(base, "daily", f"{ds}.md")
        if not os.path.exists(fp):
            continue
        for ln in open(fp, encoding="utf-8").read().splitlines():
            s = ln.strip()
            if not s.startswith("-"):
                continue
            n_entries += 1
            m = re.match(r"-\s*\[(.*?)\]\s*(.*)", s)
            proj = m.group(1) if m else "기타"
            text = (m.group(2) if m else s).strip()
            by_proj.setdefault(proj, []).append((ds[5:], text))

    done = []
    for fn in (INDEX_FILENAME, ARCHIVE_FILENAME):
        fp = os.path.join(base, fn)
        if os.path.exists(fp):
            for ln in open(fp, encoding="utf-8").read().splitlines():
                if not ln.lstrip().lower().startswith("- [x]"):
                    continue          # 스탬프만 보면 체크를 푼 항목도 완료로 센다
                dd = _done_date(ln)
                if dd and wk_start <= dd <= wk_end:
                    done.append(ln.strip())

    out = [f"# 📅 주간 다이제스트 {iso[0]}-W{iso[1]:02d}", f"> {wk_start} ~ {wk_end}", "",
           f"**활동 {n_entries}건 · 프로젝트 {len(by_proj)}개 · 완료 {len(done)}건**", ""]
    for proj, items in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        out.append(f"## {proj} ({len(items)})")
        out += [f"- {d} {t}" for d, t in items]
        out.append("")
    out.append("## ✅ 이번 주 완료")
    out += done if done else ["_(없음)_"]
    with open(os.path.join(weekly_dir, f"{iso[0]}-W{iso[1]:02d}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")


# ── 증분 처리 (핵심) ────────────────────────────────────────────────
def _process(transcript, base=None, db_path=DB_FILE, use_llm=True):
    base = base or VAULT
    try:
        _rotate_debug_logs()
        if not transcript or not os.path.exists(transcript):
            _debug("[worker] ABORT: transcript 없음")
            return
        meta = parse_transcript(transcript)
        # 요약기 자신의 `claude -p` 세션은 기록하지 않는다. **여기가 유일한 길목이다** —
        # `GUARD_ENV` 는 SessionEnd 경로만 막고 `_is_summarizer_session` 은 catchup 안에만
        # 있어서, `--worker` 나 pending 재시도로 들어오면 그대로 기록된다(실측).
        # 그 대화의 본문은 요약 프롬프트이고 거기에 **현재 미완료 태스크 목록이 통째로** 들어 있어,
        # 다음 요약이 그것을 새 태스크로 되돌려 놓는다.
        if _is_summarizer_session(meta):
            _pending_clear(transcript, db_path)
            _debug("[worker] SKIP: 요약기 자신의 세션")
            return
        sid = meta["session_id"]
        sid8 = sid[:8]
        all_turns = meta["turns"]

        with _vault_lock():
            processed = db_get_processed(sid, db_path)
            if processed is None:
                # 여기서 조용히 나가면 **이번 세션이 통째로 빠진 사실이 어디에도 안 남는다.**
                _alert_set("증분 마커를 읽지 못해 이번 세션을 기록하지 못했습니다", db_path)
                try:
                    _write_index(base, db_path=db_path)
                except Exception as e2:
                    _debug("[worker] 실패 표시 렌더 실패: " + repr(e2))
                _debug("[worker] ABORT: 증분 마커를 읽지 못했다 — 중복 append 를 막기 위해 중단")
                return
            if len(all_turns) < processed:  # 트랜스크립트 축소 → 리셋
                processed = 0
            new_turns = all_turns[processed:]
            if not new_turns:
                _pending_clear(transcript, db_path)
                _debug(f"[worker] SKIP: 새 turn 없음 (processed={processed})")
                return
            new_meta = {**meta, "turns": new_turns}
            # 제외·사소 경로도 마커가 전진해야 한다. 실패하면 다음 flush 의 증분이
            # 이 구간을 다시 포함하는데, `#nolog` 는 그 증분 **전체**를 제외하므로
            # 그 사이에 한 정상 작업까지 함께 버려진다.
            # 제외 마커는 **세션 전체**를 본다(new_meta 가 아니라 meta).
            # 증분만 보면 마커를 친 그 구간만 빠지고 다음 flush 부터 아무 일 없었다는 듯
            # 다시 기록한다. 사용자는 "이 세션은 기록하지 마"로 이해하고 Claude 도 그렇게
            # 답하는데, 실측 결과 그렇게 답해 놓고 이어지는 날짜가 전부 기록돼 있었다.
            # 마커는 계속 전진시킨다 — 재시도 큐가 같은 세션을 붙들고 있을 이유가 없다.
            if _is_excluded(meta):
                if not db_set_processed(sid, len(all_turns), db_path):
                    _alert_set("제외 구간의 마커 저장 실패 — 다음 기록이 함께 제외될 수 있습니다",
                               db_path)
                _debug("[worker] SKIP: 제외 마커(이번 증분만 제외)")
                return
            if not is_significant(new_meta):
                if not db_set_processed(sid, len(all_turns), db_path):
                    _alert_set("사소 구간의 마커 저장 실패 — 다음 실행이 다시 판정합니다", db_path)
                _debug("[worker] SKIP: 새 turn 사소 (마커만 전진)")
                return
            task_skip = _is_task_skipped(new_meta)
            if task_skip:
                _debug("[worker] '#완료' 마커 — 기록은 남기고 태스크만 만들지 않음")

            start = _fmt_ts(meta["first_ts"])
            started = start.strftime("%Y-%m-%d") if start else datetime.now().strftime("%Y-%m-%d")
            project = _project_of(meta)

            current_tasks = ""
            tf = os.path.join(base, INDEX_FILENAME)
            if os.path.exists(tf):
                current_tasks = open(tf, encoding="utf-8").read()
            open_cur, done_cur = _split_tasks(current_tasks)
            base_keys = {_task_key(o) for o in open_cur}   # 3-way 병합의 기준점

            # 선택지는 루프 안에서 다시 만든다(known 과 같은 이유) — 앞 날짜가 만든
            # 묶음 키를 뒤 날짜가 그대로 고를 수 있어야 한 세션이 여러 날에 걸쳐도 안 흩어진다.
            groups = _group_by_date(new_turns, started)
            last_summary, last_conv = None, None
            processed_upto, done_groups = processed, 0
            for date, dturns in groups:
                dmeta = {**meta, "turns": dturns}
                # known 은 루프 안에서 다시 만든다 — 앞 날짜가 만든 묶음 키를 뒤 날짜가 재사용해야
                # 한 세션이 며칠에 걸쳐도 같은 묶음으로 모인다.
                summary = summarize(dmeta, "\n".join(open_cur), use_llm=use_llm,
                                    choices=_topic_choices(base, open_cur),
                                    known=_known_topics(base, open_cur))
                if summary is None:
                    # LLM 호출 실패(오프라인·타임아웃 등). 여기서 멈추고 **마커를 전진시키지 않는다** —
                    # 그래야 다음 실행(자정 flush 등)이 이 구간을 다시 요약한다.
                    _pending_add(transcript, db_path)
                    _debug(f"[worker] {date}: 요약 실패 — pending 등록, 다음 SessionEnd 가 재시도")
                    break
                last_summary = summary
                prog = summary["progress"].rstrip()
                topic = summary.get("topic")
                topic_title = summary.get("topic_title") or ""
                # topic 에 대응하는 파일이 없으면 on_topic 은 False 다 — 그건 주제가 아니라
                # 목차의 묶음 키이고, 진행 로그·결론은 대화 페이지가 받는다.
                on_topic = bool(topic) and _append_topic(
                    base, topic, date, sid8, prog, summary["resume"],
                    summary.get("conclusions"), summary.get("dropped"), cwd=meta.get("cwd"),

                    session_id=meta.get("session_id"), verified=summary.get("verified"),
                      blocker=summary.get("blocker"))
                # 주제에 붙었으면 진행 로그 정본은 topics/ 다. 대화 페이지에는 머리말로만 얹는다.
                _write_conversation_page(
                    base, sid8, date, dturns, meta.get("title"),
                    None if on_topic else prog, topic if on_topic else None,
                    conclusions=() if on_topic else summary.get("conclusions"),
                    dropped=() if on_topic else summary.get("dropped"),
                    rng=f"{processed_upto}-{processed_upto + len(dturns)}")
                last_conv = f"{CONV_DIRNAME}/{sid8}_{date}"
                # daily 라벨은 묶음 키까지 쓴다 — cwd 폴백은 한 주제가 여러 repo 에 걸치면 어긋난다.
                _append_daily(base, date, topic or project,
                              summary["progress"],
                              f"{TOPICS_DIRNAME}/{topic}" if on_topic else last_conv)
                _log_usage(db_path, sid, date, topic, summary.get("_parts") or {}, summary.get("_usage"))
                # 날짜별 결과를 이어받는다. 마지막 요약만 쓰면 중간 날짜에서 나온 태스크가
                # 통째로 사라진다 — 다중 활동일 flush 는 실측 42%(19건 중 8건)다.
                # '#완료' 면 open_cur 를 그대로 둔다 — 기존 목록이 다시 쓰여 신규가 안 생긴다.
                if not task_skip:
                    open_cur = _apply_task_adds(open_cur, summary.get("tasks_add"),
                                                topic, last_conv,
                                                "" if on_topic else topic_title)
                processed_upto += len(dturns)
                done_groups += 1
                _debug(f"[worker] {date}: topic={topic or 'none'} 진행 로그·대화·데일리 기록")

            if done_groups == 0:
                # 여기서 그냥 나가면 pending 에 넣어 놓고도 **목차에는 아무 표시가 없다.**
                # 다음에 Claude 를 쓸 때까지 누락 사실을 알 방법이 없어진다.
                try:
                    _write_index(base, db_path=db_path)
                except Exception as e:
                    _debug("[worker] 실패 표시 렌더 실패: " + repr(e))
                _debug("[worker] ABORT: 한 그룹도 처리 못 함 — 마커 유지")
                return
            if done_groups == len(groups):
                _pending_clear(transcript, db_path)

            _update_tasks(base, open_cur, done_cur, db_path, base_keys)
            # 이번 flush가 건드린 모든 주차 + 이번 주를 재생성 (주 경계 넘김 대응)
            weeks = {}
            for date, _ in groups[:done_groups]:
                try:
                    dd = datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    continue
                weeks[(dd.isocalendar()[0], dd.isocalendar()[1])] = dd
            now = datetime.now()
            weeks[(now.isocalendar()[0], now.isocalendar()[1])] = now
            for dd in weeks.values():
                _write_weekly_digest(base, dd)
            marked = db_set_processed(sid, processed_upto, db_path)
            if marked and _alert_get(db_path):
                # 쓰기가 되살아났다. **다시 렌더해야** 목차에서 경고가 걷힌다 —
                # 파일만 지우면 INDEX 는 다음 렌더까지 낡은 경고를 이고 있다.
                _alert_clear(db_path)
                try:
                    _write_index(base, db_path=db_path)
                except Exception as e2:
                    _debug("[worker] 경고 해제 렌더 실패: " + repr(e2))
            if not marked:
                # 마커를 못 남겼으면 다음 실행이 같은 구간을 다시 처리한다.
                _pending_add(transcript, db_path)
                # pending 도 같은 DB 라 함께 죽었을 수 있다. **DB 밖에** 적어 두어야
                # 그 뒤의 평범한 렌더(체크박스 한 번)에도 경고가 지워지지 않는다.
                _alert_set("증분 마커 저장 실패 — 다음 실행이 같은 구간을 다시 기록합니다",
                           db_path)
                try:
                    _write_index(base, db_path=db_path)
                except Exception as e2:
                    _debug("[worker] 실패 표시 렌더 실패: " + repr(e2))
            _git_snapshot(base)
            _debug(f"[worker] {'DONE' if marked else 'DONE(마커 미저장)'}: "
                   f"+{processed_upto - processed}turn, {done_groups}/{len(groups)}일")
    except Exception as e:
        # 요약은 됐는데 파일 쓰기에서 터진 경우가 여기로 온다. pending 에 넣지 않으면
        # 마커는 그대로여도 **다시 실행될 계기가 없다.**
        try:
            _pending_add(transcript, db_path)
            # 락은 이미 풀린 뒤다(with 블록을 빠져나오며 해제). 잡히면 경고를 실어 준다 —
            # 쓰기 도중 터진 실패가 목차에 안 뜨면 요약 실패와 똑같이 조용한 누락이 된다.
            with _vault_lock(blocking=False) as got:
                if got:
                    _write_index(base, db_path=db_path)
        except Exception:
            pass
        import traceback
        _debug("[worker] ERROR: " + repr(e) + "\n" + traceback.format_exc())


# ── 엔트리포인트 ────────────────────────────────────────────────────
def _run_dry(use_llm):
    flag = "--dry-run-llm" if use_llm else "--dry-run"
    transcript = sys.argv[sys.argv.index(flag) + 1]
    out = "/tmp/sessionlog-dryrun"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    os.makedirs(out, exist_ok=True)
    _process(transcript, base=out, db_path=os.path.join(out, "test.db"), use_llm=use_llm)
    print(f"[{flag}] 처리 완료 → {out}")
    for p in sorted(glob.glob(os.path.join(out, "**", "*.md"), recursive=True)):
        print("  ", os.path.relpath(p, out))


CATCHUP_DAYS = int(os.environ.get("SESSIONLOG_CATCHUP_DAYS", "3"))


LAUNCHD_LOG_MAX = 1_000_000     # launchd StandardOut/ErrorPath 는 로테이션이 없다


def _trim_launchd_logs():
    """launchd 가 append 하는 로그에 상한을 둔다. 정상 동작 시에는 비어 있지만
    (진행 내역은 _debug 로 별도 파일에 가고 그쪽은 7일 로테이션이 있다),
    예외가 반복되면 무한히 자랄 수 있어 실행 끝에 잘라 둔다."""
    for f in glob.glob("/tmp/claude-obsidian-logger.catchup.*"):
        try:
            if os.path.getsize(f) > LAUNCHD_LOG_MAX:
                with open(f, "w"):
                    pass
                _debug(f"[catchup] {os.path.basename(f)} 이 상한 초과 — 비움")
        except OSError:
            pass


def _is_summarizer_session(meta):
    """요약기 자신의 `claude -p` 세션인가.

    SessionEnd 경로는 `GUARD_ENV` 로 걸러지지만 `--catchup` 은 트랜스크립트를 직접 훑으므로
    그 방어가 닿지 않는다. 요약 프롬프트는 12,000자짜리 user 발화로 파싱되어 `is_significant`
    를 통과하고, 그대로 두면 프롬프트에 담긴 미완료 태스크 목록이 다시 태스크로 돌아온다.
    `type == "queue-operation"` 은 판별에 쓸 수 없다 — 실제 대화 세션에도 섞여 있다."""
    for role, lines, _ in meta.get("turns") or ():
        if role != "user":
            continue
        return "\n".join(lines).lstrip().startswith(SUMMARY_SIGNATURE)
    return False


def _catchup(done=None):
    """열려 있는(=최근 수정된) 세션들을 SessionEnd 와 똑같이 기록한다. 세션 자체는 건드리지 않는다.

    **자동 실행은 없다.** 자정 launchd 잡은 제거했다 — `~/Documents` 가 macOS 보호 폴더라
    백그라운드 launchd 프로세스가 TCC 로 조용히 거부당했고(실측 3회 전량 실패, 기록 0건),
    SessionEnd 만으로 빠짐이 없음을 확인했기 때문이다. 이 함수는 수동 실행용으로만 남는다.

    증분 마커가 있어 이미 처리된 세션은 즉시 스킵되므로 '열림' 여부를 정확히 가릴 필요가 없다.
    LLM 호출이 실패하면 _process 가 마커를 전진시키지 않으므로 다음 실행이 그대로 재시도한다."""
    cutoff = datetime.now().timestamp() - CATCHUP_DAYS * 86400
    root = os.path.expanduser("~/.claude/projects")
    files = [f for f in glob.glob(os.path.join(root, "*", "*.jsonl"))
             if os.path.getmtime(f) >= cutoff]
    # _process 가 같은 판별을 다시 하지만 여기서 먼저 걸러 트랜스크립트를 두 번 읽지 않는다.
    todo, skipped = [], 0
    for f in files:
        try:
            if _is_summarizer_session(parse_transcript(f)):
                skipped += 1
                continue
        except Exception:
            pass
        todo.append(f)
    _debug(f"[catchup] 대상 {len(todo)}개 (최근 {CATCHUP_DAYS}일, 요약기 세션 {skipped}개 제외)")
    for f in sorted(todo, key=os.path.getmtime):
        _process(f)
        if done is not None:
            done.add(f)
    _trim_launchd_logs()
    _debug("[catchup] 완료")


def _dispatch_worker(transcript):
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker", transcript],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=os.environ.copy())
        _debug(f"dispatched worker → {transcript}")
    except Exception as e:
        _debug("dispatch ERROR: " + repr(e))


def main():
    if "--dry-run-llm" in sys.argv:
        return _run_dry(use_llm=True)
    if "--dry-run" in sys.argv:
        return _run_dry(use_llm=False)
    if "--retry-pending" in sys.argv:
        # 상한(PENDING_MAX_TRIES)을 넘겨 자동 재시도가 멈춘 것까지 **전부** 다시 돌린다.
        # 경고에 이 명령을 적어 두었으므로 여기가 사용자의 복구 경로다.
        rows = _pending_rows()
        if rows is None:
            print("pending 조회 실패 — DB 를 열 수 없습니다", file=sys.stderr)
            return 1
        _debug(f"[retry-pending] 대상 {len(rows)}건")
        for path, _tries in rows:
            print(f"재시도: {path}")
            _process(path)
        left = _pending_rows()
        if left is None:
            print("재시도 후 pending 조회 실패 — DB 를 열 수 없습니다", file=sys.stderr)
            return 1
        print(f"완료 — 남은 pending {len(left)}건")
        # 남은 게 있으면 실패다. 종료 코드 0 이면 자동화에서 성공과 구분할 수 없다.
        return 1 if left else 0
    if "--catchup" in sys.argv:   # 수동 회수: 열린 세션 + 밀린 pending
        seen = set()
        _catchup(seen)
        rows = _pending_rows()
        if rows is None:
            print("pending 조회 실패 — DB 를 열 수 없습니다", file=sys.stderr)
            return 1
        for path, _t in rows:
            if path in seen:
                # 방금 catchup 에서 실패해 pending 에 들어온 것이다. 같은 실행에서 또 돌리면
                # LLM 비용을 두 번 쓰고 tries 만 두 번 오른다.
                continue
            _debug(f"[catchup] pending 재시도: {path}")
            _process(path)
        return
    if "--worker" in sys.argv:
        tr = sys.argv[sys.argv.index("--worker") + 1]
        # **현재 세션이 먼저다.** 재시도를 앞에 두면 백로그가 쌓였을 때
        # 방금 끝난 세션의 기록이 몇 시간씩 밀린다(실측: 21건이면 22번째로 처리된다).
        _process(tr)
        if _pending_rows() is None:
            # 훅은 종료 코드를 보지만 사람은 안 본다. 조용히 0 으로 끝내지 않는다.
            _debug("[worker] pending 조회 실패 — 재시도 큐를 확인할 수 없다")
            return 1
        for old in _pending_list()[:PENDING_DRAIN_PER_RUN]:
            if old != tr and os.path.exists(old):
                _debug(f"[worker] pending 재시도: {old}")
                _process(old)
        return
    if "--filechanged" in sys.argv:  # INDEX 외부 편집 감지 → 완료 전이 기록 + 즉시 재렌더
        try:
            sys.stdin.read()
        except Exception:
            pass
        _sync_task_states(VAULT)
        with _vault_lock(blocking=False) as got:
            if got:
                # 렌더만 하면 `✅ 날짜` 가 안 붙고 weekly 에도 안 잡힌다 — 체크한 뒤
                # 12일 동안 Claude 를 안 쓰면 그동안 날짜 없는 완료로 남는다.
                p = os.path.join(VAULT, INDEX_FILENAME)
                cur = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
                o, dn = _split_tasks(cur)
                _update_tasks(VAULT, o, dn)
                _write_weekly_digest(VAULT)
            else:
                _debug("[filechanged] 워커가 락 보유 — 재렌더는 그쪽에 맡김")
        return

    if os.environ.get(GUARD_ENV):
        return
    _debug("=== SessionEnd hook fired ===")
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        _debug("ABORT: stdin JSON 파싱 실패")
        return
    transcript = payload.get("transcript_path")
    _debug(f"transcript={transcript} exists={bool(transcript) and os.path.exists(transcript)}")
    if not transcript or not os.path.exists(transcript):
        _debug("ABORT: transcript_path 없음")
        return
    _dispatch_worker(transcript)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
