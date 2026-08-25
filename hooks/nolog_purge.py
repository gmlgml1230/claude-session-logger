#!/usr/bin/env python3
"""
nolog_purge.py — Claude Code SessionEnd hook.

`#nolog` / `#기록제외` / `#skiplog` 를 친 세션의 **CLI transcript 를 디스크에서 지운다.**

session_log.py 는 Obsidian 기록만 막는다(`_is_excluded`). CLI 쪽 transcript
(`~/.claude/projects/<slug>/<sid>.jsonl`)는 그대로 남아 `/resume` 목록에 계속 뜨고,
`cleanupPeriodDays` 만료(기본 30일)까지 디스크에 있다. `/clear` 도 이 파일을 지우지
않는다 — 컨텍스트만 비우고 새 대화를 시작할 뿐이다. 이 훅이 그 구멍을 막는다.

지우는 것:
  <sid>.jsonl   대화 원문
  <sid>/        사이드카 — tool-results 등 도구 출력 원문이 여기 남는다

사용:
  실제 hook :  cat <stdin-json> | nolog_purge.py
  dry-run   :  nolog_purge.py --dry-run <transcript.jsonl>
"""

import os
import sys
import json
import shutil
from datetime import datetime

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

PROJECTS_ROOT = os.path.realpath(os.path.expanduser("~/.claude/projects"))
DEBUG_LOG_DIR = os.path.expanduser("~/.claude/hooks")


def _debug(msg):
    """session_log.py 와 **같은 파일**에 쓴다. 두 훅은 같은 SessionEnd 에서 경합하므로
    순서를 시간순으로 읽으려면 로그가 한 곳이어야 한다. `[nolog]` 로 구분한다."""
    try:
        now = datetime.now()
        p = os.path.join(DEBUG_LOG_DIR, f"sessionlog_debug_{now:%Y-%m-%d}.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{now.isoformat(timespec='seconds')} [nolog] {msg}\n")
    except Exception:
        pass


def _is_nolog(transcript):
    """판정은 session_log.py 의 규칙을 **그대로 재사용**한다.

    마커 인식이 두 곳에서 갈리면 'Obsidian 엔 안 남았는데 transcript 는 남는'
    (또는 그 반대의) 상태가 조용히 생긴다. `_has_marker` 는 마커가 **단독 줄**일 때만
    인정하는데, 그 규칙을 여기서 재구현하면 언젠가 어긋난다.

    import 부작용은 없다 — session_log.py 의 최상위 실행 코드는
    `if __name__ == "__main__"` 하나뿐이다.
    """
    import session_log as sl
    return sl._is_excluded(sl.parse_transcript(transcript))


def _safe_target(transcript):
    """`~/.claude/projects` 아래의 `.jsonl` 만 대상으로 삼는다.

    삭제는 되돌릴 수 없다. 훅 입력은 우리가 만든 값이 아니므로 경로를 신뢰하지 않는다.
    """
    if not transcript:
        return None
    rp = os.path.realpath(transcript)
    if not rp.startswith(PROJECTS_ROOT + os.sep) or not rp.endswith(".jsonl"):
        return None
    return rp


def purge(transcript, dry=False):
    target = _safe_target(transcript)
    if not target:
        _debug(f"SKIP: 삭제 대상 경로가 아님 — {transcript}")
        return False
    if not os.path.exists(target):
        _debug(f"SKIP: transcript 없음 — {target}")
        return False

    try:
        hit = _is_nolog(target)
    except Exception as e:
        # 판정에 실패하면 **보존한다.** 잘못 지우는 쪽의 대가가 훨씬 크다.
        _debug(f"ABORT: 마커 판정 실패 — 보존한다: {e!r}")
        return False

    if not hit:
        _debug(f"KEEP: 제외 마커 없음 — {os.path.basename(target)}")
        return False

    sidecar = target[: -len(".jsonl")]
    removed = []
    for path, kind in ((target, "transcript"), (sidecar, "사이드카")):
        if not os.path.exists(path):
            continue
        if dry:
            removed.append(f"(dry) {kind}: {path}")
            continue
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            removed.append(f"{kind}: {os.path.basename(path)}")
        except Exception as e:
            _debug(f"삭제 실패 {kind}={path}: {e!r}")

    _debug(("DRY-RUN " if dry else "PURGED ") + " | ".join(removed) if removed
           else "PURGE 대상이 이미 없음")
    return True


def main():
    if "--dry-run" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--dry-run"]
        if not rest:
            print("usage: nolog_purge.py --dry-run <transcript.jsonl>", file=sys.stderr)
            return 2
        hit = purge(rest[0], dry=True)
        print("제외 마커 O — 삭제 대상" if hit else "제외 마커 X — 보존")
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        _debug("ABORT: stdin JSON 파싱 실패")
        return 0
    transcript = payload.get("transcript_path")
    _debug(f"SessionEnd reason={payload.get('reason')} transcript={transcript}")
    purge(transcript)
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
