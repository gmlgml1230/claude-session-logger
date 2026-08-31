#!/usr/bin/env python3
"""SessionEnd 입력 → topic frontmatter → INDEX 전체 경로 통합 테스트.

단위 테스트만으로는 '호출부에서 인자가 안 넘어가는' 결함을 못 잡는다(실제로 놓쳤다).
여기서는 _process 를 LLM 없이 끝까지 돌려 인계 패킷이 실제로 기록되는지 본다.
"""
import atexit
import importlib.util, json, os, re, subprocess, sys, tempfile, shutil

# 실제 ~/.claude/hooks 를 건드리지 않는다 — 그러면 진짜 워커 락과 경합하고
# 실제 pending DB 를 읽어 테스트가 비결정적이 된다. import 시점에 읽히므로 그 전에 건다.
_STATE = tempfile.mkdtemp(prefix="sessionlog-test-")
os.environ["SESSIONLOG_STATE_DIR"] = _STATE
atexit.register(shutil.rmtree, _STATE, True)   # 실행마다 남지 않게

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "session_log.py")
spec = importlib.util.spec_from_file_location("sl", HOOK)
sl = importlib.util.module_from_spec(spec); spec.loader.exec_module(sl)

FAIL = []
def chk(name, got, want=True):
    if got != want:
        FAIL.append(name); print(f"✗ {name}\n   got : {got!r}\n   want: {want!r}")
    else:
        print(f"✓ {name}")


def make_repo(d):
    subprocess.run(["git", "-C", d, "init", "-q"], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", d, "config", k, v], check=True)
    open(os.path.join(d, "a.txt"), "w").write("1")
    subprocess.run(["git", "-C", d, "add", "-A"], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    return subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def make_transcript(path, sid, cwd, turns):
    with open(path, "w", encoding="utf-8") as f:
        for role, text in turns:
            f.write(json.dumps({
                "type": role, "timestamp": "2026-08-19T10:00:00Z", "cwd": cwd,
                "message": {"role": role, "content": text},
            }, ensure_ascii=False) + "\n")


def main():
    tmp = tempfile.mkdtemp()
    try:
        vault, repo = os.path.join(tmp, "vault"), os.path.join(tmp, "repo")
        sl.DEBUG_LOG_DIR = tmp      # 테스트 흔적이 실제 디버그 로그를 오염시키지 않게
        os.makedirs(os.path.join(vault, "topics")); os.makedirs(repo)
        head = make_repo(repo)
        open(os.path.join(vault, "topics", "demo.md"), "w", encoding="utf-8").write(
            "---\ntitle: 데모 주제\nstatus: active\n---\n\n"
            "## 🔜 다음\n\n- 처음\n\n## 📌 결론\n\n- _(없음)_\n\n"
            "## ❌ 접은 안\n\n- _(없음)_\n\n## 📈 진행 로그\n\n")
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        tr = os.path.join(tmp, f"{sid}.jsonl")
        make_transcript(tr, sid, repo, [
            ("user", "데모 주제 작업을 진행하자. 스키마를 정리하고 테스트를 돌려줘."),
            ("assistant", "정리하고 pytest 를 돌렸습니다. 18개 통과."),
        ])
        # 요약만 가짜로 두고 **실제 호출 경로**를 그대로 탄다.
        # use_llm=False 는 topic 을 None 으로 만들어 _append_topic 을 건너뛰므로,
        # "호출부에서 인자가 안 넘어가는" 종류의 결함을 잡지 못한다.
        sl.summarize = lambda *a, **k: {
            "topic": "demo", "topic_title": "", "progress": "- 스키마 정리",
            "resume": "다음은 배포", "verified": "pytest 18 통과",
            "conclusions": ["결론 A"], "dropped": [], "tasks_add": [],
            "_usage": None, "_parts": {},
        }
        sl._process(tr, base=vault, db_path=os.path.join(tmp, "t.db"))

        fm = open(os.path.join(vault, "topics", "demo.md"), encoding="utf-8").read()
        g = lambda k: (re.search(rf"^{k}:\s*(.+)$", fm, re.M) or [None, ""])[1].strip()
        chk("cwd 기록", g("cwd") == repo)
        chk("session(full id) 기록", g("session") == sid)
        chk("branch 기록", g("branch") in ("main", "master"))
        chk("head 기록", g("head") == head)
        chk("verified 기록", "pytest" in g("verified"))
        # 어느 저장소의 sha 인지 함께 적는다 — 읽는 쪽이 그 sha 를 아는 저장소를
        # 찾아 헤매지 않아도 되고, cwd 가 저장소가 아닌 세션도 기준을 남길 수 있다.
        chk("verified_head 기록(clean, name@sha)",
            g("verified_head") == f"{os.path.basename(repo)}@{head}")
        chk("진행 로그 append", "스키마 정리" in fm)
        chk("결론 append", "결론 A" in fm)

        sl._write_index(vault)
        idx = open(os.path.join(vault, "INDEX.md"), encoding="utf-8").read()
        chk("INDEX 에 재개 명령", f"claude -r {sid}" in idx or f"cd {repo}" in idx)
        chk("INDEX 에 경과 표기", "오늘" in idx or "일 전" in idx)

        # 커밋을 하나 더 쌓으면 드리프트가 잡혀야 한다
        open(os.path.join(repo, "b.txt"), "w").write("2")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "second"], check=True)
        chk("드리프트 감지", any("그 뒤 1커밋" in w for w in sl._repo_warnings(repo, None, head)))
        chk("기준 없음 구분", any("기록 HEAD 없음" in w for w in sl._repo_warnings(repo, None, None)))
        sl._write_index(vault)
        chk("INDEX 에 드리프트 반영", "그 뒤 1커밋" in open(os.path.join(vault, "INDEX.md"), encoding="utf-8").read())

        before = open(os.path.join(vault, "INDEX.md"), encoding="utf-8").read()
        sl._write_index(vault)
        chk("멱등", before == open(os.path.join(vault, "INDEX.md"), encoding="utf-8").read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("=== 전부 통과 ===" if not FAIL else f"=== 실패 {len(FAIL)}건: {FAIL} ==="))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
