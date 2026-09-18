#!/usr/bin/env python3
"""Claude Code 세션 대화를 마크다운으로 남긴다.

하는 일은 하나다 — **루트(기본 `~/day1`) 아래에서 돈 세션의 대화를 파일로 적는다.**
원본 트랜스크립트(`~/.claude/projects/*/*.jsonl`)는 만료되지만 이 기록은 남는다.

기록에서 빼는 것:
- 도구 실행 라인 (`TOOL_LINE_PREFIXES`)
- `#로그` 블록 — 붙여넣은 실행 로그
- `#nolog` 가 있는 세션 전체
- 루트 밖에서 돈 세션 (scratchpad·다른 프로젝트)
"""
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

# ── 설정 ────────────────────────────────────────────────────────────
# 루트 밖 세션은 기록하지 않는다 — scratchpad·일회성 디렉터리까지 쌓이면
# 정작 찾을 때 걸러내야 할 것이 더 많아진다.
ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("SESSIONLOG_ROOT") or "~/day1"))
LOG_DIRNAME = os.environ.get("SESSIONLOG_DIR") or "claude_log"
LOG_DIR = os.path.join(ROOT, LOG_DIRNAME)

# 상태는 레포 밖에 둔다 — clone 위치를 옮겨도 기록이 이어진다.
STATE_DIR = os.environ.get("SESSIONLOG_STATE_DIR") or os.path.expanduser("~/.claude/hooks")
DEBUG_LOG_DIR = STATE_DIR
DB_FILE = os.path.join(STATE_DIR, "sessionlog.db")
CATCHUP_DAYS = int(os.environ.get("SESSIONLOG_CATCHUP_DAYS", "3"))

# 기록할 가치 판정 — 인사 한 줄짜리 세션까지 파일로 만들지 않는다.
MIN_USER_CHARS = 12
MIN_TOTAL_CHARS = 60

PASTE_CAP = 20000
TOOL_LINE_PREFIXES = ("⌘ ", "→ 도구", "← 도구")
LOG_MARKER_RE = re.compile(r"^[ \t]*#로그[ \t]*$", re.M)
EXCLUDE_MARKERS = ("#nolog", "#기록제외", "#skiplog")   # 세션 전체 제외
MAX_TOOL_RESULT_CHARS = 280
LOG_FENCED_RE = re.compile(
    r"^[ \t]*#로그[ \t]*\n(?:[ \t]*\n)*[ \t]*`{3,}[^\n]*\n.*?\n[ \t]*`{3,}[ \t]*$",
    re.M | re.S)
COMPACT_PREAMBLE = "This session is being continued from a previous conversation"
TASK_NOTE_RE = re.compile(r"<task-notification>.*?</task-notification>", re.S)
def _debug(msg):
    try:
        now = datetime.now()
        path = os.path.join(DEBUG_LOG_DIR, f"sessionlog_debug_{now:%Y-%m-%d}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{now.isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


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


def _yaml_val(v):
    return '"' + str(v).replace("\n", " ").replace('"', "'") + '"'


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


def _flush_marker(sid8, date, rng):
    """이 파일에 이미 실린 **처리 구간**. 본문 부분문자열로 판정하면
    같은 질문을 다시 한 정상 대화가 '이미 있음' 으로 버려진다(실측)."""
    return f"<!-- flush {sid8} {date} {rng} -->"
# ── 루트 판정 ───────────────────────────────────────────────────────
def _in_root(cwd):
    """이 세션이 루트 아래에서 돌았나. 루트 밖이면 기록하지 않는다.

    realpath 로 비교한다 — symlink 를 지나는 경로를 문자열로만 보면 같은 위치가
    다르게 읽힌다. 경계에서 자르지 않으면 `~/day1-old` 같은 형제 디렉터리가 걸린다.
    """
    if not cwd:
        return False
    try:
        r = os.path.realpath(cwd)
    except OSError:
        return False
    root = os.path.realpath(ROOT)
    return r == root or r.startswith(root + os.sep)


# ── 기록 ────────────────────────────────────────────────────────────
def _write_conversation_page(base, sid8, date, turns, title=None, rng=None):
    """그날 대화 원문. 같은 날 두 번째 flush 는 이어 붙인다."""
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{sid8}_{date}.md")
    body = _render_turns(turns)
    mark = _flush_marker(sid8, date, rng) + "\n" if rng else ""
    if os.path.exists(path):
        cur = open(path, encoding="utf-8").read()
        if mark and mark.strip() in cur:
            # 마커 저장이 실패해 **같은 구간을 다시 처리**하는 경우다. 두 번 싣지 않는다.
            _debug(f"[log] {sid8}_{date}: 이미 실린 구간({rng}) — 건너뜀")
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + mark + body)
        return
    disp = f"{title or '대화'} · {date}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\ntitle: {_yaml_val(disp)}\n---\n\n")
        f.write(mark)
        f.write(f"# 💬 {date} 대화\n\n" + body)


def _process(transcript, base=None, db_path=DB_FILE):
    """트랜스크립트 하나를 기록한다. 증분 마커 이후 turn 만 본다."""
    base = base or LOG_DIR
    try:
        meta = parse_transcript(transcript)
    except Exception as e:
        _debug(f"[log] 파싱 실패: {transcript} — {e!r}")
        return
    if not meta or not meta.get("turns"):
        return
    if not _in_root(meta.get("cwd")):
        _debug(f"[log] SKIP: 루트 밖 — {meta.get('cwd')}")
        return
    if _is_excluded(meta):
        # 마커는 **세션 전체**를 본다. 증분만 보면 마커를 친 구간만 빠지고
        # 다음 flush 부터 아무 일 없었다는 듯 다시 기록된다.
        db_set_processed(meta["session_id"], len(meta["turns"]), db_path)
        _debug("[log] SKIP: 제외 마커")
        return

    sid = meta["session_id"]
    processed = db_get_processed(sid, db_path)
    new_turns = meta["turns"][processed:]
    if not new_turns:
        return
    if not is_significant({**meta, "turns": new_turns}):
        db_set_processed(sid, len(meta["turns"]), db_path)
        _debug("[log] SKIP: 새 turn 사소 (마커만 전진)")
        return

    start = _fmt_ts(meta["first_ts"])
    started = start.strftime("%Y-%m-%d") if start else datetime.now().strftime("%Y-%m-%d")
    sid8 = sid[:8]
    upto = processed
    for date, dturns in _group_by_date(new_turns, started):
        _write_conversation_page(base, sid8, date, dturns, meta.get("title"),
                                 rng=f"{upto}-{upto + len(dturns)}")
        upto += len(dturns)
        _debug(f"[log] {sid8}_{date} 기록")
    if not db_set_processed(sid, len(meta["turns"]), db_path):
        # 마커를 못 올리면 다음 실행이 같은 구간을 다시 본다. flush 마커가
        # 중복 기록은 막지만, 실패 사실은 남겨 둔다.
        _debug(f"[log] 마커 저장 실패: {sid}")


def _catchup():
    """SessionEnd 훅이 돌지 않은 세션을 뒤늦게 건진다(수동 실행 전용).

    자동 실행은 없다 — 자정 launchd 잡은 `~/Documents` 가 macOS 보호 폴더라
    TCC 로 조용히 거부당해(실측 3회 전량 실패) 제거했다.
    증분 마커가 있어 이미 처리된 세션은 즉시 넘어간다.
    """
    cutoff = datetime.now().timestamp() - CATCHUP_DAYS * 86400
    root = os.path.expanduser("~/.claude/projects")
    files = [f for f in glob.glob(os.path.join(root, "*", "*.jsonl"))
             if os.path.getmtime(f) >= cutoff]
    _debug(f"[catchup] 대상 {len(files)}개 (최근 {CATCHUP_DAYS}일)")
    for f in sorted(files, key=os.path.getmtime):
        _process(f)
    _debug("[catchup] 완료")


def main():
    if "--catchup" in sys.argv:
        _catchup()
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    tr = payload.get("transcript_path")
    if not tr or not os.path.exists(tr):
        _debug("[log] ABORT: transcript_path 없음")
        return 0
    _process(tr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
