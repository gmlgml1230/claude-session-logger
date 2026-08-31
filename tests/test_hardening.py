#!/usr/bin/env python3
"""버그 하드닝 회귀 테스트. 각 항목은 실제로 재현된 결함에 대응한다."""
import atexit
import importlib.util, json, os, re, subprocess, tempfile, shutil

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

SKELETON = ('---\ntitle: "T"\nstatus: active\nupdated: 2026-08-20\n---\n\n'
            "## 📌 결론\n\n- _(없음)_\n\n## ❌ 접은 안\n\n- _(없음)_\n\n"
            "## 📈 진행 로그\n\n## 🔜 다음\n")


def main():
    tmp = tempfile.mkdtemp()
    sl.DEBUG_LOG_DIR = tmp
    try:
        base = os.path.join(tmp, "v"); os.makedirs(os.path.join(base, "topics"))
        db = os.path.join(tmp, "t.db")
        tp = os.path.join(base, "topics", "t.md")

        # ① 같은 날 두 번째 flush 가 첫 진행을 지우지 않고, 자기 자신도 살아남는다
        open(tp, "w", encoding="utf-8").write(SKELETON)
        sl._append_topic(base, "t", "2026-08-21", "abc12345", "- 첫 진행", "첫 다음")
        sl._append_topic(base, "t", "2026-08-21", "abc12345", "- 둘째 진행", "둘째 다음")
        txt = open(tp, encoding="utf-8").read()
        chk("같은 날 2차 flush: 첫 진행 보존", "첫 진행" in txt)
        chk("같은 날 2차 flush: 둘째 진행 보존", "둘째 진행" in txt)
        chk("같은 날 2차 flush: 다음 갱신", "둘째 다음" in txt and "첫 다음" not in txt)
        chk("진행이 '다음' 아래로 새지 않음",
            txt.index("둘째 진행") < txt.index(sl.NEXT_HEADER))

        # ② frontmatter 만 있고 섹션이 없는 주제에서도 결론이 남는다
        open(tp, "w", encoding="utf-8").write(
            '---\ntitle: "T"\nstatus: active\n---\n\n사람이 적어 둔 메모\n')
        ok = sl._append_topic(base, "t", "2026-08-21", "abc12345", "- 진행", "다음",
                              conclusions=["결론이 사라지면 안 된다"])
        txt = open(tp, encoding="utf-8").read()
        chk("섹션 없는 주제: append 성공", ok)
        chk("섹션 없는 주제: 결론 기록", "결론이 사라지면 안 된다" in txt)
        chk("섹션 없는 주제: 사람 메모 보존", "사람이 적어 둔 메모" in txt)

        # ③ frontmatter 삭제가 본문을 건드리지 않는다
        doc = '---\ntitle: "T"\nblocker: 승인 대기\n---\n\n본문\nblocker: 이건 사람이 쓴 줄\n'
        out = sl._fm_del(doc, "blocker")
        chk("_fm_del: frontmatter 키 제거", "blocker: 승인 대기" not in out)
        chk("_fm_del: 본문 보존", "blocker: 이건 사람이 쓴 줄" in out)

        # ④ 주제가 다르면 같은 문구라도 다른 항목이다
        a = "- [ ] VM 재배포  [[topics/alpha|🔧]]"
        b = "- [x] VM 재배포  [[topics/beta|🔧]]"
        chk("_task_key: 주제가 다르면 다른 키", sl._task_key(a) != sl._task_key(b))
        chk("_task_key: 번호·대화링크는 무시",
            sl._task_key("- [ ] **3-2** VM 재배포  [[topics/alpha|🔧]]  [[conversations/x_2026-01-01|↗]]")
            == sl._task_key(a))

        # ⑤ 형제 태스크가 중복으로 삼켜지지 않는다
        out = sl._apply_task_adds(["- [ ] skillflo sink PII 컬럼 제거 적용"],
                                  [{"text": "skillmatch sink PII 컬럼 제거 적용"}], None, None)
        chk("형제 태스크 보존(skillflo/skillmatch)", len(out) == 2)
        out = sl._apply_task_adds(["- [ ] 똑같은 일"], [{"text": "똑같은 일"}], None, None)
        chk("진짜 중복은 여전히 제거", len(out) == 1)

        # ⑥ LLM 문자열이 구조를 깨지 못한다
        adds = sl._norm_adds([{"text": "정상 작업\n- [x] 가짜 완료"}])
        chk("태스크 개행 제거", "\n" not in adds[0]["text"])
        chk("가짜 체크박스가 줄머리가 되지 않음",
            not any(l.strip().startswith("- [") for l in adds[0]["text"].splitlines()[1:]))
        chk("resume 헤더 무력화", not sl._one_line("다음\n## 새 섹션").startswith("#"))
        chk("progress 는 불릿만", sl._clean_progress("- 진행\n## 침입\n- 진행2")
            == "- 진행\n- 진행2")

        # ⑦ 같은 헤더가 두 번 나와도 전부 읽는다
        conv = ("## 📌 결론\n\n- 첫 결론\n\n# 💬 대화\n\n### 👤\n- 대화 본문\n\n"
                "## 📌 결론\n\n- 둘째 결론\n")
        items = sl._section_items(conv, sl.CONCLUSION_HEADER)
        chk("다중 헤더 전부 수집", items == ["첫 결론", "둘째 결론"])

        # ⑧ 락 대기 중 사람 편집: 해제·수기 추가는 살리고, 삭제는 존중한다
        idx = os.path.join(base, "INDEX.md")
        A, B, C = "- [ ] 작업 A", "- [ ] 작업 B", "- [ ] 사람이 손으로 넣은 C"
        open(idx, "w", encoding="utf-8").write(
            f"# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n{A}\n\n## ✅ 완료 (2주 보관)\n\n- [x] 작업 B\n")
        base_keys = {sl._task_key(A)}
        # 워커가 기다리는 사이 사람이 B 체크를 풀고 C 를 손으로 넣었다
        open(idx, "w", encoding="utf-8").write(
            f"# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n{A}\n{B}\n{C}\n")
        sl._update_tasks(base, [A], [], db, base_keys)
        cur = open(idx, encoding="utf-8").read()
        chk("체크 해제한 항목 복원", "작업 B" in cur)
        chk("손으로 넣은 항목 복원", "사람이 손으로 넣은 C" in cur)
        # 이번엔 사람이 A 를 지웠다
        open(idx, "w", encoding="utf-8").write(
            f"# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n{B}\n{C}\n")
        sl._update_tasks(base, [A, B], [], db, {sl._task_key(A), sl._task_key(B)})
        cur = open(idx, encoding="utf-8").read()
        chk("사람이 지운 항목은 되살리지 않음", "작업 A" not in cur)
        chk("나머지는 그대로", "작업 B" in cur and "사람이 손으로 넣은 C" in cur)
        # 아무도 안 건드린 정상 경로에서는 순서가 그대로여야 한다
        rows = [f"- [ ] 작업 {i}" for i in range(1, 6)]
        open(idx, "w", encoding="utf-8").write(
            "# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n" + "\n".join(rows) + "\n")
        sl._update_tasks(base, list(rows), [], db, {sl._task_key(r) for r in rows})
        got = [l.strip() for l in open(idx, encoding="utf-8")
               if l.lstrip().startswith("- [ ]") and not sl.HEADER_LINE_RE.match(l)]
        chk("정상 경로에서 순서 보존", got == rows)

        # ⑨ 실패한 세션이 pending 에 남고, 성공하면 빠진다
        tr = os.path.join(tmp, "aaaaaaaa-1111-2222-3333-444444444444.jsonl")
        with open(tr, "w", encoding="utf-8") as f:
            for r, t in (("user", "데모 주제 작업을 이어서 하자. 스키마를 정리하고 "
                                   "테스트를 전부 돌려서 결과를 알려줘."),
                         ("assistant", "스키마를 정리하고 테스트를 돌렸습니다. 18개 전부 통과했습니다.")):
                f.write(json.dumps({"type": r, "timestamp": "2026-08-21T10:00:00Z", "cwd": tmp,
                                    "message": {"role": r, "content": t}},
                                   ensure_ascii=False) + "\n")
        real = sl.summarize
        sl.summarize = lambda *a, **k: None                     # LLM 실패
        sl._process(tr, base=base, db_path=db)
        chk("요약 실패 → pending 등록", tr in sl._pending_list(db))
        chk("요약 실패 → 마커 전진 안 함",
            sl.db_get_processed("aaaaaaaa-1111-2222-3333-444444444444", db) == 0)
        sl.summarize = lambda *a, **k: {
            "topic": None, "topic_title": "", "progress": "- 했다", "resume": "다음",
            "verified": "", "blocker": "", "conclusions": [], "dropped": [],
            "tasks_add": [], "_usage": None, "_parts": {}}
        sl._process(tr, base=base, db_path=db)
        chk("성공하면 pending 해제", sl._pending_list(db) == [])
        # 상한: 파일이 실제로 있어야 '만료 정리'가 아니라 '상한'으로 빠지는지 검증된다
        stuck = os.path.join(tmp, "stuck.jsonl"); open(stuck, "w").write("{}\n")
        sl._pending_add(stuck, db)
        chk("상한 이내면 목록에 있음", stuck in sl._pending_list(db))
        for _ in range(sl.PENDING_MAX_TRIES):
            sl._pending_add(stuck, db)
        chk("재시도 상한 초과분은 목록에서 빠짐", stuck not in sl._pending_list(db))
        # 원본이 사라진 항목은 정리된다 (30일 뒤 트랜스크립트 삭제)
        sl._pending_add("/does/not/exist.jsonl", db)
        chk("만료된 pending 은 정리", "/does/not/exist.jsonl" not in sl._pending_list(db))
        sl.summarize = real

        # ⑩ git 스냅샷은 훅 경로만, 삭제도 반영
        g = os.path.join(tmp, "g"); os.makedirs(os.path.join(g, "topics"))
        subprocess.run(["git", "-C", g, "init", "-q"], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", g, "config", k, v], check=True)
        open(os.path.join(g, "topics", "a.md"), "w").write("a")
        open(os.path.join(g, "내노트.md"), "w").write("사람이 편집 중")
        subprocess.run(["git", "-C", g, "add", "topics"], capture_output=True)
        subprocess.run(["git", "-C", g, "commit", "-qm", "init"], capture_output=True)
        os.remove(os.path.join(g, "topics", "a.md"))
        open(os.path.join(g, "topics", "b.md"), "w").write("b")
        sl._git_snapshot(g)
        show = subprocess.run(["git", "-C", g, "show", "--name-status", "HEAD"],
                              capture_output=True, text=True).stdout
        chk("스냅샷: 삭제 반영", "topics/a.md" in show and "D\t" in show)
        chk("스냅샷: 신규 반영", "topics/b.md" in show)
        chk("스냅샷: 사람 노트 제외", "내노트" not in show)
        os.remove(os.path.join(g, "topics", "b.md"))
        open(os.path.join(g, "INDEX.md"), "w").write("i")
        sl._git_snapshot(g)
        os.remove(os.path.join(g, "INDEX.md"))
        sl._git_snapshot(g)
        show = subprocess.run(["git", "-C", g, "show", "--name-status", "HEAD"],
                              capture_output=True, text=True).stdout
        chk("스냅샷: 최상위 파일 삭제도 커밋", "D\tINDEX.md" in show)

        # ⑪ 중복 판정은 같은 주제 안에서만, 부분포함은 태스크에서 끈다
        chk("교차 주제 같은 문구는 각자 생긴다",
            len(sl._apply_task_adds(["- [ ] VM 재배포  [[topics/alpha|🔧]]"],
                                    [{"text": "VM 재배포"}], "beta", None)) == 2)
        chk("앞 단계가 뒤 단계의 부분문자열이어도 남는다",
            len(sl._apply_task_adds(["- [ ] 운영 데이터 백필"],
                                    [{"text": "운영 데이터 백필 결과 검증"}], None, None)) == 2)
        # 접두어가 긴 형제 쌍(0.923). 임계값이 여기까지 내려오면 태스크가 소리 없이 사라진다.
        chk("접두어가 긴 형제 태스크도 둘 다 남는다",
            len(sl._apply_task_adds(["- [ ] dim_voucher 증분 적재 누락 백필"],
                                    [{"text": "dim_voucher_use 증분 적재 누락 백필"}],
                                    None, None)) == 2)

        # ⑫ 결론·접은 안도 한 줄, _one_line 은 선두 기호를 보존
        c1 = sl._valid_summary({"topic": "x", "conclusions": ["정상\n## 침입\n- [x] 가짜"]})
        chk("결론 한 줄 강제", "\n" not in c1["conclusions"][0])
        chk("_one_line: CLI 옵션 보존", sl._one_line("--force 없이 재실행") == "--force 없이 재실행")
        chk("_one_line: 이슈 번호 보존", sl._one_line("#123 확인") == "#123 확인")
        chk("_one_line: 불릿은 제거", sl._one_line("- 불릿") == "불릿")
        chk("_one_line: 잘리면 표시", sl._one_line("가" * 400).endswith("…"))

        # ⑬ frontmatter 문자열을 섹션으로 오인하지 않는다
        chk("헤더는 줄 전체로만 인식",
            sl._ensure_sections('---\ntitle: "## 📌 결론"\n---\n\n본문\n')
            .count("\n## 📌 결론") == 1)

        # ⑭ 같은 구간 재처리가 대화·daily 를 두 번 쌓지 않는다
        turns = [("user", ["안녕하세요 반갑습니다"], None)]
        for _ in range(2):   # 같은 **구간**을 두 번 처리 = 마커 저장 실패 후 재시도
            sl._write_conversation_page(base, "ab12cd34", "2026-08-21", turns, "T", "- 진행",
                                        rng="0-1")
            sl._append_daily(base, "2026-08-21", "t", "- 진행", "conversations/ab12cd34_2026-08-21")
        cpath = os.path.join(base, "conversations", "ab12cd34_2026-08-21.md")
        cp = open(cpath, encoding="utf-8").read()
        chk("같은 구간 재처리: 본문 1회만", cp.count("안녕하세요 반갑습니다") == 1)
        # 다른 구간에서 같은 말을 또 했다면 그건 정상 대화다 — 부분문자열 판정이면 여기서 유실된다
        sl._write_conversation_page(base, "ab12cd34", "2026-08-21", turns, "T", "- 또 진행",
                                    rng="1-2")
        cp = open(cpath, encoding="utf-8").read()
        chk("다른 구간의 같은 발화는 보존", cp.count("안녕하세요 반갑습니다") == 2)
        chk("daily 1줄만",
            open(os.path.join(base, "daily", "2026-08-21.md"), encoding="utf-8").read().count("[t]") == 1)

        # ⑮ 완료일은 최신, 열린 줄에는 스탬프가 없다
        chk("열린 줄 스탬프 제거", "✅" not in sl._unstamp("- [ ] X ✅ 2026-01-01"))
        with sl._db_tasks(db) as c:
            for ts in ("2026-01-01T00:00:00", "2026-08-21T00:00:00"):
                c.execute("INSERT INTO task_events VALUES(?,?,?)", ("k", "done", ts))
        chk("재완료 시 최신 완료일", sl._completion_date("k", "fb", db) == "2026-08-21")

        # ⑯ 재시도 상한을 넘겨도 경고 집계에는 남는다
        stuck2 = os.path.join(tmp, "stuck2.jsonl"); open(stuck2, "w").write("{}")
        for _ in range(sl.PENDING_MAX_TRIES + 1):
            sl._pending_add(stuck2, db)
        chk("상한 초과: 자동 재시도 제외", stuck2 not in sl._pending_list(db))
        chk("상한 초과: 경고에는 남음",
            any(t == stuck2 and n >= sl.PENDING_MAX_TRIES for t, n in sl._pending_rows(db)))

        # ⑰ task_key 규칙 교체 시 가짜 완료 이벤트를 만들지 않는다
        vault2 = os.path.join(tmp, "v2"); os.makedirs(vault2)
        db2 = os.path.join(tmp, "m.db")
        open(os.path.join(vault2, "INDEX.md"), "w", encoding="utf-8").write(
            "# I\n\n## ☑️ 기타 태스크\n\n- [ ] 열린 일  [[topics/a|🔧]]\n\n"
            "## ✅ 완료 (2주 보관)\n\n- [x] 끝난 일  [[topics/a|🔧]] ✅ 2026-01-05\n")
        sl._sync_task_states(vault2, db2)
        with sl._db_tasks(db2) as c:
            n_ev = c.execute("SELECT count(*) FROM task_events").fetchone()[0]
        chk("키 교체 첫 실행: 이벤트 0건", n_ev == 0)

        # ⑱ 헤더 탐색은 frontmatter 값도 코드펜스도 섹션으로 세지 않는다
        tp2 = os.path.join(base, "topics", "hdr.md")
        open(tp2, "w", encoding="utf-8").write(
            '---\ntitle: "## 🔜 다음"\nstatus: active\n---\n\n'
            "## 📌 결론\n\n## ❌ 접은 안\n\n## 📈 진행 로그\n\n## 🔜 다음\n")
        sl._append_topic(base, "hdr", "2026-08-21", "abc12345", "- 진행", "실제 다음")
        t2 = open(tp2, encoding="utf-8").read()
        fm2 = re.match(r"^---\n(.*?)\n---\n", t2, re.S)
        chk("frontmatter 가 닫혀 있다", bool(fm2))
        chk("frontmatter 안에 제목이 그대로", bool(fm2) and 'title: "## 🔜 다음"' in fm2.group(1))
        chk("frontmatter 안에 본문이 섞이지 않았다", bool(fm2) and "- 실제 다음" not in fm2.group(1))
        chk("그래도 다음·진행은 기록됨", "- 실제 다음" in t2 and "- 진행" in t2)
        fenced = '---\ntitle: T\n---\n\n```md\n## 📌 결론\n- 예시\n```\n\n본문\n'
        chk("코드펜스 안의 헤더는 섹션이 아니다", sl._find_header(fenced, "## 📌 결론") == -1)
        # 닫히지 않은 펜스가 뒤쪽 섹션을 통째로 숨기면 안 된다 (실제 대화 문서에서 흔하다)
        unclosed = "---\ntitle: T\n---\n\n```\n예시\n\n## 📌 결론\n\n- 진짜 결론\n"
        chk("닫히지 않은 펜스 뒤의 헤더는 보인다",
            sl._find_header(unclosed, "## 📌 결론") != -1)
        chk("그 섹션의 항목도 읽힌다",
            sl._section_items(unclosed, "## 📌 결론") == ["진짜 결론"])
        chk("그래서 실제 섹션을 만든다",
            sl._ensure_sections(fenced).rstrip().endswith("## 🔜 다음"))
        # 펜스는 여는 문자·길이로 닫힌다. 개수만 세면 4-backtick 예시에서 경계가 어긋난다.
        H = "## 🔜 다음"
        for name, doc in (
            ("4-backtick", '---\nt: 1\n---\n\n````md\n```\n## 🔜 다음\n```\n````\n\n## 🔜 다음\n\n- 진짜\n'),
            ("~~~ 펜스", '---\nt: 1\n---\n\n~~~md\n## 🔜 다음\n~~~\n\n## 🔜 다음\n\n- 진짜\n'),
            ("줄머리 인라인 코드", '---\nt: 1\n---\n\n```x```\n## 🔜 다음\n\n- 진짜\n```y```\n'),
        ):
            want = doc.rindex(H) if doc.count(H) > 1 else doc.index(H)
            chk(f"펜스 판정: {name}",
                sl._find_header(doc, H) == want and sl._section_items(doc, H) == ["진짜"])
        # 물결표 펜스의 info string 에는 ~ 가 들어갈 수 있고, 줄머리 탭은 펜스가 아니다
        d1 = '---\nt: 1\n---\n\n~~~python~3\n## 🔜 다음\n~~~\n\n## 🔜 다음\n\n- 진짜\n'
        chk("펜스 판정: ~~~python~3",
            sl._find_header(d1, H) == d1.rindex(H) and sl._section_items(d1, H) == ["진짜"])
        d2 = '---\nt: 1\n---\n\n\t```\n## 🔜 다음\n\n- 진짜\n'
        chk("펜스 판정: 줄머리 탭은 펜스가 아니다",
            sl._find_header(d2, H) != -1 and sl._section_items(d2, H) == ["진짜"])

        # ㉒ 하위 디렉터리 cwd 에서도 브랜치 드리프트가 보인다
        rr = os.path.join(tmp, "brepo"); os.makedirs(os.path.join(rr, "pkg"))
        subprocess.run(["git", "-C", rr, "init", "-q", "-b", "oldbranch"], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", rr, "config", k, v], check=True)
        open(os.path.join(rr, "a"), "w").write("a")
        subprocess.run(["git", "-C", rr, "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", rr, "commit", "-qm", "i"], capture_output=True)
        subprocess.run(["git", "-C", rr, "checkout", "-q", "-b", "newbranch"], check=True)
        bv = os.path.join(tmp, "bv"); os.makedirs(os.path.join(bv, "topics"))
        open(os.path.join(bv, "topics", "t.md"), "w", encoding="utf-8").write(
            f'---\ntitle: T\nstatus: active\nupdated: 2026-08-10\n'
            f'cwd: {os.path.join(rr, "pkg")}\nbranch: oldbranch\nsession: x\n---\n\n## 🔜 다음\n')
        sl._write_index(bv)
        chk("하위 디렉터리에서도 브랜치 변경이 보인다",
            "oldbranch→newbranch" in open(os.path.join(bv, "INDEX.md"), encoding="utf-8").read())

        # ㉓ 없는 저장소는 캐시하지 않는다
        nd = os.path.join(tmp, "notrepo"); os.makedirs(nd)
        chk("비저장소는 None", sl._repo_root(nd) is None)
        chk("같은 렌더 안에서는 재조회하지 않는다", nd in sl._REPO_ROOT_CACHE)
        subprocess.run(["git", "-C", nd, "init", "-q"], check=True)
        sl._write_index(bv)      # 렌더 경계에서 캐시가 비워진다
        chk("렌더가 지나면 뒤늦게 만들어진 저장소도 인식", sl._repo_root(nd) is not None)

        # ㉔ 시각만 달라진 렌더는 파일을 건드리지 않는다
        p_idx = os.path.join(bv, "INDEX.md")
        before = open(p_idx, encoding="utf-8").read()
        mt = os.path.getmtime(p_idx)
        sl._write_index(bv)
        chk("시각만 다른 재렌더는 무변경", open(p_idx, encoding="utf-8").read() == before)
        chk("파일을 다시 쓰지도 않는다", os.path.getmtime(p_idx) == mt)
        # 시각 '값' 만 정규화한다 — 줄 전체를 지우면 문구를 고쳐도 반영되지 않는다
        open(p_idx, "w", encoding="utf-8").write(before.replace("에 갱신됐습니다", "기준입니다"))
        sl._write_index(bv)
        chk("머리말 문구 변경은 반영된다",
            "에 갱신됐습니다" in open(p_idx, encoding="utf-8").read())

        # ㉕ DB 조회 실패는 '빈 큐' 와 구분된다
        chk("조회 실패는 None", sl._pending_rows(os.path.join(tmp, "없는폴더", "x.db")) is None)
        chk("_pending_list 는 그래도 리스트",
            sl._pending_list(os.path.join(tmp, "없는폴더", "x.db")) == [])

        # ㉖ DB 밖 경고는 후속 렌더에서도 살아남는다
        sl._alert_set("증분 마커 저장 실패 — 테스트")
        sl._write_index(bv)
        first = "증분 마커 저장 실패" in open(p_idx, encoding="utf-8").read()
        sl._write_index(bv)
        again = "증분 마커 저장 실패" in open(p_idx, encoding="utf-8").read()
        sl._alert_clear(); sl._write_index(bv)
        gone = "증분 마커 저장 실패" not in open(p_idx, encoding="utf-8").read()
        chk("경고가 뜬다", first)
        chk("체크박스 한 번에 지워지지 않는다", again)
        chk("해제하면 사라진다", gone)
        # 경고 파일은 **그 DB 옆에** 있다 — 별도 DB 를 쓰는 dry-run 이 운영 경고를 지우면 안 된다
        prod, test = os.path.join(tmp, "prod.db"), os.path.join(tmp, "test.db")
        sl._alert_set("운영 경고", prod)
        sl._alert_clear(test)
        chk("다른 DB 의 해제가 운영 경고를 지우지 않는다", sl._alert_get(prod) == "운영 경고")

        # ㉖-2 verified 판정: '미커밋' 을 '커밋' 으로 읽지 않고, cwd 가 저장소가 아니어도
        #      그 sha 를 아는 저장소에서 잰다
        vr = os.path.join(tmp, "vrepo"); os.makedirs(vr)
        subprocess.run(["git", "-C", vr, "init", "-q"], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", vr, "config", k, v], check=True)
        open(os.path.join(vr, "a"), "w").write("a")
        subprocess.run(["git", "-C", vr, "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", vr, "commit", "-qm", "i"], capture_output=True)
        vhead = subprocess.run(["git", "-C", vr, "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        notrepo = os.path.join(tmp, "plain"); os.makedirs(notrepo)
        vv = os.path.join(tmp, "vv"); os.makedirs(os.path.join(vv, "topics"))
        open(os.path.join(vv, "topics", "t.md"), "w", encoding="utf-8").write(
            f'---\ntitle: T\nstatus: active\nupdated: 2026-08-21\ncwd: {notrepo}\n'
            f'repos: [{os.path.basename(vr)}]\nsession: x\n'
            f'verified: "테스트 통과"\nverified_head: {vhead}\n---\n\n## 🔜 다음\n')
        sl._write_index(vv)
        line = open(os.path.join(vv, "INDEX.md"), encoding="utf-8").read()
        chk("cwd 가 비저장소여도 '변경됨' 으로 단정하지 않는다", "검증 이후 코드 변경됨" not in line)
        chk("clean 이면 경고가 안 붙는다", "✅ 확인: 테스트 통과\n" in line + "\n")
        open(os.path.join(vr, "a"), "w").write("b")      # 미커밋 변경만 만든다
        sl._write_index(vv)
        line = open(os.path.join(vv, "INDEX.md"), encoding="utf-8").read()
        chk("미커밋은 미커밋으로 보고한다", "검증 이후 미커밋 변경 있음" in line)
        chk("미커밋을 '코드 변경됨' 으로 승격하지 않는다", "검증 이후 코드 변경됨" not in line)
        subprocess.run(["git", "-C", vr, "commit", "-aqm", "next"], capture_output=True)
        sl._write_index(vv)
        chk("실제 커밋이 쌓이면 그때 '코드 변경됨'",
            "검증 이후 코드 변경됨" in open(os.path.join(vv, "INDEX.md"), encoding="utf-8").read())

        # ㉖-3 요약기 자신의 세션은 어느 경로로 들어와도 기록하지 않는다
        sv = os.path.join(tmp, "sv"); os.makedirs(os.path.join(sv, "topics"))
        sum_tr = os.path.join(tmp, "eeeeeeee-1111-2222-3333-444444444444.jsonl")
        with open(sum_tr, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "timestamp": "2026-08-21T13:00:00Z", "cwd": tmp,
                "message": {"role": "user", "content": sl.SUMMARY_SIGNATURE +
                            ". 아래는 한 프로젝트 세션에서 이번에 새로 진행한 대화다. "
                            "읽고 뽑아라. # 현재 미완료 작업\n- [ ] 되돌아오면 안 되는 태스크"}},
                ensure_ascii=False) + "\n")
            f.write(json.dumps({
                "type": "assistant", "timestamp": "2026-08-21T13:00:01Z", "cwd": tmp,
                "message": {"role": "assistant", "content": '{"topic": "none"}'}},
                ensure_ascii=False) + "\n")
        sl._pending_add(sum_tr, db)
        sl._process(sum_tr, base=sv, db_path=db)
        chk("요약기 세션은 대화 문서를 만들지 않는다",
            not os.path.isdir(os.path.join(sv, "conversations")))
        chk("요약기 세션은 pending 에서도 빠진다", sum_tr not in sl._pending_list(db))

        # ㉖-4 마커는 단독 줄일 때만. 인용·시스템 주입 텍스트에 걸리면 세션이 통째로 사라진다
        def mk(txt):
            return {"turns": [("user", [txt], None)]}
        chk("단독 줄이면 인식", sl._is_excluded(mk("#nolog")))
        chk("앞뒤 공백도 인식", sl._is_excluded(mk("  #nolog  ")))
        chk("여러 줄 중 한 줄이어도 인식", sl._is_excluded(mk("정리해줘\n#nolog\n고마워")))
        chk("문장 속 인용은 무시",
            not sl._is_excluded(mk("#nolog 는 세션 로깅용 마커로 보이는데 처리를 못 찾았습니다")))
        chk("조사가 붙어도 무시", not sl._is_excluded(mk("앞서 보내신 #nolog는 무슨 뜻이야")))
        chk("compact 요약 안의 인용은 무시",
            not sl._is_excluded(mk(sl.COMPACT_PREAMBLE + " that ran out of context.\n"
                                   "user 가 #nolog 를 치면 기록하지 않는다\n")))
        chk("작업 알림 안의 인용은 무시",
            not sl._is_excluded(mk("<task-notification>\n#nolog\n</task-notification>")))
        chk("#완료 도 같은 규칙", sl._is_task_skipped(mk("#완료"))
            and not sl._is_task_skipped(mk("결론은 #완료 시점에 생성하지 않는다는 것")))

        # ㉖-5 제외 마커는 세션 전체에 걸린다 — 마커를 친 그 구간만 빠지면 안 된다
        nolog_tr = os.path.join(tmp, "ffffffff-1111-2222-3333-444444444444.jsonl")
        turns = [("user", "#nolog"), ("assistant", "기록 없이 진행할게요.")]
        turns += [("user", "이 작업을 이어서 하자. 설정을 정리하고 배포까지 확인한 뒤 알려줘."),
                  ("assistant", "설정을 정리하고 배포를 확인했습니다. 이상 없이 끝났습니다.")]
        with open(nolog_tr, "w", encoding="utf-8") as f:
            for i, (r, t) in enumerate(turns):
                f.write(json.dumps({"type": r, "timestamp": f"2026-08-2{i%9}T09:00:00Z",
                                    "cwd": tmp, "message": {"role": r, "content": t}},
                                   ensure_ascii=False) + "\n")
        nv = os.path.join(tmp, "nv"); os.makedirs(os.path.join(nv, "topics"))
        ndb = os.path.join(tmp, "n.db")
        sl._process(nolog_tr, base=nv, db_path=ndb)          # 1차: 마커가 증분에 있다
        first = os.path.isdir(os.path.join(nv, "conversations"))
        with open(nolog_tr, "a", encoding="utf-8") as f:      # 마커 뒤에 새 작업이 이어진다
            for r, t in (("user", "이번엔 스키마를 정리하고 테스트를 전부 돌려서 결과를 알려줘."),
                         ("assistant", "스키마를 정리하고 테스트 18개를 전부 돌렸습니다. 통과.")):
                f.write(json.dumps({"type": r, "timestamp": "2026-08-29T09:00:00Z", "cwd": tmp,
                                    "message": {"role": r, "content": t}},
                                   ensure_ascii=False) + "\n")
        sl._process(nolog_tr, base=nv, db_path=ndb)          # 2차: 마커는 증분 밖이다
        chk("#nolog 친 세션은 첫 flush 부터 기록 안 함", not first)
        chk("#nolog 는 다음 flush 에서도 유지된다",
            not os.path.isdir(os.path.join(nv, "conversations")))

        # ㉖-6 #로그: 펜스 형태는 블록만, 단독 형태는 그 뒤 전부
        L = sl._strip_log_blocks
        chk("#로그 단독 → 그 뒤 전부 삭제", "생략" in L("#로그\nERROR aaa\n뒤 문장"))
        chk("#로그 앞의 말은 남는다", L("이 로그 봐줘\n#로그\nERROR").startswith("이 로그 봐줘"))
        chk("펜스 형태 → 블록만", "뒷말" in L("앞말\n#로그\n```\nERROR\n```\n뒷말"))
        # 빈 줄 한 줄에 뒤 내용까지 날아가던 결함
        chk("마커와 펜스 사이 빈 줄 허용",
            "뒷말" in L("앞말\n#로그\n\n```\nERROR\n```\n뒷말"))
        chk("빈 줄 여러 개도 허용",
            "뒷말" in L("앞말\n#로그\n\n\n```\nERROR\n```\n뒷말"))
        chk("펜스 들여쓰기 허용", "뒷말" in L("앞말\n#로그\n  ```\n  ERROR\n  ```\n뒷말"))
        chk("백틱 4개도 펜스", "뒷말" in L("앞말\n#로그\n````\nERROR\n````\n뒷말"))
        chk("같은 줄에 이어 쓰면 무효", L("#로그  v2.1\n둘째") == "#로그  v2.1\n둘째")
        # 여러 번 쓸 수 있어야 한다 — 로그 덩어리마다 마커를 붙이는 것이 정상 사용이다
        two = L("에러 둘 봐줘\n#로그\n```\nA\n```\n그리고 이것도\n#로그\n```\nB\n```\n원인은?")
        chk("펜스형을 두 번 쓰면 둘 다 빠진다", two.count("블록 생략") == 2)
        chk("사이의 설명과 마지막 질문은 남는다",
            "그리고 이것도" in two and "원인은?" in two and "A" not in two and "B" not in two)
        chk("연속 세 번도 전부", L("#로그\n```\nA\n```\n#로그\n```\nB\n```\n"
                                "#로그\n```\nC\n```\n끝").count("블록 생략") == 3)
        mixed = L("앞말\n#로그\n```\nA\n```\n중간\n#로그\nB 원문\n이 뒤는?")
        chk("펜스형 뒤에 단독형을 섞으면 순서대로 처리",
            mixed.count("블록 생략") == 1 and "이후" in mixed and "중간" in mixed)

        # ㉖-7 완료 섹션: 날짜로 묶고, **주제 파일이 있을 때만** 줄인다
        dv = os.path.join(tmp, "dv"); os.makedirs(os.path.join(dv, "topics"))
        open(os.path.join(dv, "topics", "alpha.md"), "w", encoding="utf-8").write(
            '---\ntitle: "알파"\nstatus: active\n---\n\n## 🔜 다음\n')
        done = [
            "- [x] 첫째 일  [[topics/alpha|알파]]  [[conversations/aa11bb22_2026-08-21|↗ 대화]] ✅ 2026-08-21",
            "- [x] 둘째 일  [[topics/alpha|알파]] ✅ 2026-08-21",
            "- [x] 셋째 일  [[topics/beta|베타 묶음]]  [[conversations/cc33dd44_2026-08-19|↗ 대화]] ✅ 2026-08-19",
        ]
        open(os.path.join(dv, "INDEX.md"), "w", encoding="utf-8").write(
            "# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n- [ ] 열린 일\n")
        sl._write_index(dv, ["- [ ] 열린 일"], done)
        idx = open(os.path.join(dv, "INDEX.md"), encoding="utf-8").read()
        # 완료 섹션만 본다 — `[[topics/alpha|알파]]` 는 진행 중 주제 줄에도 정상적으로 있다
        sec = idx.split("## ✅ 완료", 1)[1]
        chk("완료를 날짜로 묶는다", "**08-21** (2건)" in sec and "**08-19** (1건)" in sec)
        chk("주제 파일이 있으면 alias 를 🔧 로", "[[topics/alpha|🔧]]" in sec
            and "[[topics/alpha|알파]]" not in sec)
        chk("주제 파일이 있으면 대화 링크를 뗀다", "aa11bb22" not in sec)
        chk("✅ 날짜는 남긴다(아카이브가 이걸로 돈다)", sec.count("✅ 2026-08-21") == 2)
        chk("주제 파일이 없으면 제목도 대화 링크도 보존",
            "[[topics/beta|베타 묶음]]" in sec and "cc33dd44" in sec)
        # Obsidian 은 원시 HTML 안의 마크다운을 렌더하지 않는다 — 체크박스도 링크도 죽는다
        chk("INDEX 에 원시 HTML 태그가 없다",
            not re.search(r"</?(details|summary|div|span|br)\b", idx))
        chk("완료 건수는 제목에", "## ✅ 완료 (2주 보관) · 3건" in idx)
        before = idx
        sl._write_index(dv, ["- [ ] 열린 일"], done)
        chk("완료 섹션 렌더 멱등",
            sl._strip_stamp(open(os.path.join(dv, "INDEX.md"), encoding="utf-8").read())
            == sl._strip_stamp(before))

        # ㉖-8 파일 없는 묶음도 선택지에 들어가고, 선택지 밖이어도 이미 쓰는 이름이면 받는다
        cv = os.path.join(tmp, "cv"); os.makedirs(os.path.join(cv, "topics"))
        open(os.path.join(cv, "topics", "have-file.md"), "w", encoding="utf-8").write(
            '---\ntitle: "파일 있는 주제"\nstatus: active\n---\n\n## 🔜 다음\n')
        open(os.path.join(cv, "topics", "closed.md"), "w", encoding="utf-8").write(
            '---\ntitle: "닫힌 주제"\nstatus: done\n---\n\n## 🔜 다음\n')
        opens = ["- [ ] 남은 일  [[topics/no-file-group|파일 없는 묶음]]"]
        ch = dict(sl._topic_choices(cv, opens))
        chk("선택지에 파일 있는 주제", ch.get("have-file") == "파일 있는 주제")
        chk("선택지에 파일 없는 묶음", ch.get("no-file-group") == "파일 없는 묶음")
        chk("완료 주제는 선택지에서 제외", "closed" not in ch)

        real4 = sl.summarize
        base_ret = {"topic": "no-file-group", "topic_new": None, "progress": "- 함",
                    "resume": "다음", "verified": "", "blocker": "",
                    "conclusions": [], "dropped": [], "tasks_add": []}
        sl.call_claude = lambda *a, **k: (json.dumps(base_ret, ensure_ascii=False), None)
        got = sl.summarize({"turns": [], "title": "t"}, "",
                           choices=[("have-file", "파일 있는 주제")],
                           known=[("no-file-group", "파일 없는 묶음")])
        chk("선택지 밖이어도 이미 쓰는 묶음이면 받는다", got["topic"] == "no-file-group")
        got = sl.summarize({"turns": [], "title": "t"}, "",
                           choices=[("have-file", "파일 있는 주제")], known=[])
        chk("정말 모르는 슬러그는 none 으로", got["topic"] is None)
        sl.summarize = real4

        # ㉗ DB 를 못 읽어 중단할 때도 목차에 남는다
        av = os.path.join(tmp, "av"); os.makedirs(os.path.join(av, "topics"))
        open(os.path.join(av, "INDEX.md"), "w", encoding="utf-8").write(
            "# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n- [ ] 뭔가\n")
        baddb = os.path.join(tmp, "bad.db"); os.makedirs(baddb)
        alert_tr = os.path.join(tmp, "dddddddd-1111-2222-3333-444444444444.jsonl")
        with open(alert_tr, "w", encoding="utf-8") as f:
            for r, t in (("user", "이 작업을 이어서 하자. 설정을 정리하고 배포까지 "
                                  "확인한 다음 결과를 알려줘."),
                         ("assistant", "설정을 정리하고 배포를 확인했습니다. 이상 없이 끝났습니다.")):
                f.write(json.dumps({"type": r, "timestamp": "2026-08-21T12:00:00Z", "cwd": tmp,
                                    "message": {"role": r, "content": t}},
                                   ensure_ascii=False) + "\n")
        sl._process(alert_tr, base=av, db_path=baddb)
        chk("읽기 실패가 목차에 남는다",
            "기록하지 못했습니다" in open(os.path.join(av, "INDEX.md"), encoding="utf-8").read())

        # ㉘ 쓰기가 되살아나면 **그 렌더에서** 경고가 걷힌다
        good = os.path.join(tmp, "good.db")
        sl._alert_set("증분 마커 저장 실패 — 테스트", good)
        sl._write_index(av, db_path=good)
        was = "증분 마커 저장 실패" in open(os.path.join(av, "INDEX.md"), encoding="utf-8").read()
        real3 = sl.summarize
        sl.summarize = lambda *a, **k: {
            "topic": None, "topic_title": "", "progress": "- 했다", "resume": "다음",
            "verified": "", "blocker": "", "conclusions": [], "dropped": [],
            "tasks_add": [], "_usage": None, "_parts": {}}
        sl._process(alert_tr, base=av, db_path=good)
        sl.summarize = real3
        chk("복구 전 경고가 있었다", was)
        chk("복구 직후 같은 렌더에서 걷힌다",
            "증분 마커 저장 실패" not in open(os.path.join(av, "INDEX.md"), encoding="utf-8").read())

        # ⑲ _unstamp 는 줄 끝만 — 사람이 본문에 쓴 날짜를 지우지 않는다
        chk("본문 중간의 ✅ 날짜 보존",
            sl._unstamp("- [ ] 인증서 ✅ 2026-09-01까지 갱신") == "- [ ] 인증서 ✅ 2026-09-01까지 갱신")

        # ⑳ 저장소 하위 디렉터리에서 작업해도 드리프트가 보인다
        rp = os.path.join(tmp, "repo"); os.makedirs(os.path.join(rp, "pkg"))
        subprocess.run(["git", "-C", rp, "init", "-q"], check=True)
        got_ws = sl._workspaces({"cwd": os.path.join(rp, "pkg")})
        chk("하위 디렉터리 → 저장소 루트로 인식",
            [(os.path.realpath(w), h) for w, h in got_ws] == [(os.path.realpath(rp), None)])
        # symlink 로 저장소 밖에서 가리켜도 실제 저장소를 찾아야 한다
        link = os.path.join(tmp, "link-to-pkg")
        os.symlink(os.path.join(rp, "pkg"), link)
        chk("symlink 경로도 실제 저장소로",
            os.path.realpath(sl._repo_root(link) or "") == os.path.realpath(rp))
        # 없는 경로/상대 경로가 프로세스 cwd 를 타고 엉뚱한 상위 저장소를 잡으면 안 된다
        chk("없는 경로는 저장소가 아니다", sl._repo_root(os.path.join(rp, "없는폴더")) is None)
        chk("상대 경로는 저장소가 아니다", sl._repo_root("data-airflow") is None)

        # ㉑ 요약 실패는 그 자리에서 목차에 뜬다
        fail_tr = os.path.join(tmp, "bbbbbbbb-1111-2222-3333-444444444444.jsonl")
        with open(fail_tr, "w", encoding="utf-8") as f:
            for r, t in (("user", "또 다른 주제 작업을 이어서 하자. 설정을 정리하고 "
                                   "배포까지 확인한 다음 결과를 알려줘."),
                         ("assistant", "설정을 정리하고 배포를 확인했습니다. 이상 없이 끝났습니다.")):
                f.write(json.dumps({"type": r, "timestamp": "2026-08-21T11:00:00Z", "cwd": tmp,
                                    "message": {"role": r, "content": t}},
                                   ensure_ascii=False) + "\n")
        real2 = sl.summarize
        sl.summarize = lambda *a, **k: None
        sl._process(fail_tr, base=base, db_path=db)
        sl.summarize = real2
        chk("실패가 INDEX 에 즉시 표시됨",
            "기록 실패" in open(os.path.join(base, "INDEX.md"), encoding="utf-8").read())

        # ㉙ 여러 저장소에 걸친 주제는 **저장소마다** 기준 HEAD 를 갖는다.
        #    cwd 것만 `head:` 에 담으면 나머지는 영영 '기록 HEAD 없음' 이고,
        #    그 하나 때문에 줄 전체가 '기준 HEAD 미기록' 이 된다(실측: watcher-local-test).
        mv = os.path.join(tmp, "mv"); os.makedirs(os.path.join(mv, "topics"))
        mrepo = {}
        for nm in ("ra", "rb"):
            d = os.path.join(tmp, nm); os.makedirs(d)
            subprocess.run(["git", "-C", d, "init", "-q", "-b", "main"], check=True)
            for k, v in (("user.email", "t@t"), ("user.name", "t")):
                subprocess.run(["git", "-C", d, "config", k, v], check=True)
            open(os.path.join(d, "a"), "w").write("1")
            subprocess.run(["git", "-C", d, "add", "-A"], capture_output=True)
            subprocess.run(["git", "-C", d, "commit", "-qm", "i"], capture_output=True)
            mrepo[nm] = d
        mtp = os.path.join(mv, "topics", "m.md")
        open(mtp, "w", encoding="utf-8").write(SKELETON)
        # 두 저장소를 각각 cwd 로 한 세션이 차례로 끝난다
        sl._append_topic(mv, "m", "2026-08-21", "aaaa1111", "- ra 작업", "다음",
                         cwd=mrepo["ra"], session_id="s" * 8)
        sl._append_topic(mv, "m", "2026-08-22", "bbbb2222", "- rb 작업", "다음",
                         cwd=mrepo["rb"], session_id="s" * 8)
        mtxt = open(mtp, encoding="utf-8").read()
        repos = sl._fm_list(mtxt, "repos")
        chk("다중 저장소: repos 에 둘 다", sorted(r.partition("@")[0] for r in repos),
            ["ra", "rb"])
        chk("다중 저장소: 저장소마다 기준 sha", all("@" in r for r in repos))
        # 두 저장소 모두 그 뒤로 한 커밋씩 진행된다
        for nm, d in mrepo.items():
            open(os.path.join(d, "b"), "w").write("2")
            subprocess.run(["git", "-C", d, "add", "-A"], capture_output=True)
            subprocess.run(["git", "-C", d, "commit", "-qm", "n"], capture_output=True)
        ws = dict(sl._workspaces(sl._topic_meta(mtp)))
        chk("다중 저장소: 두 저장소 다 기준을 안다",
            sorted(os.path.basename(k) for k, v in ws.items() if v), ["ra", "rb"])
        wr = []
        for wp, wh in ws.items():
            wr += sl._repo_warnings(wp, None, wh, os.path.basename(wp))
        chk("다중 저장소: 기록 HEAD 없음이 안 난다",
            [w for w in wr if "기록 HEAD 없음" in w], [])
        chk("다중 저장소: 양쪽 드리프트가 다 보인다",
            sorted(w for w in wr if "그 뒤" in w), ["ra: 그 뒤 1커밋", "rb: 그 뒤 1커밋"])
        # 기준을 **아는** 출처가 이긴다 — sha 없는 `workspaces:` 가 먼저 읽혀
        # sha 있는 `repos:` 를 선점하면 그 저장소는 영영 기준을 잃는다(실측).
        wtxt = open(mtp, encoding="utf-8").read().replace(
            "repos:", "workspaces: [ra, rb]\nrepos:", 1)
        open(mtp, "w", encoding="utf-8").write(wtxt)
        ws2 = dict(sl._workspaces(sl._topic_meta(mtp)))
        chk("sha 없는 출처가 기준을 선점하지 않는다",
            sorted(os.path.basename(k) for k, v in ws2.items() if v), ["ra", "rb"])

        # ㉚ head 를 기록하면 소급 복구가 남긴 head_source 잔재를 걷는다.
        #    안 걷으면 '미기록(소급분)' 과 실제 sha 가 한 파일에 공존해 서로를 부정한다.
        htp = os.path.join(mv, "topics", "h.md")
        open(htp, "w", encoding="utf-8").write(
            SKELETON.replace("updated: 2026-08-20\n",
                             "updated: 2026-08-20\nhead_source: unknown   # 세션 당시 HEAD 미기록(소급 복구분)\n"))
        sl._append_topic(mv, "h", "2026-08-21", "cccc3333", "- 작업", "다음",
                         cwd=mrepo["ra"], session_id="s" * 8)
        htxt = open(htp, encoding="utf-8").read()
        chk("head 기록 시 head_source 잔재 제거", "head_source" not in htxt)
        chk("head 기록 시 head 는 남는다", bool(re.search(r"^head: \w+", htxt, re.M)))

        # ㉛ cwd 가 저장소가 아닌 세션도 검증 sha 를 남긴다.
        #    여러 repo 를 담은 상위 폴더에서 일하면(day1 처럼) cwd 는 저장소가 아니다.
        #    cwd 만 보면 vh 가 None 이라 verified_head 가 **매번 지워진다**(실측:
        #    logger-hardening 이 검증 직후 '검증 시점 미기록' 이 됐다).
        parent = os.path.join(tmp, "parent"); os.makedirs(parent)
        only = os.path.join(parent, "only")
        shutil.copytree(mrepo["ra"], only)
        vtp = os.path.join(mv, "topics", "v.md")
        open(vtp, "w", encoding="utf-8").write(
            SKELETON.replace("updated: 2026-08-20\n", "updated: 2026-08-20\nrepos: [only]\n"))
        sl._append_topic(mv, "v", "2026-08-21", "dddd4444", "- 작업", "다음",
                         cwd=parent, session_id="s" * 8, verified="테스트 통과")
        vm = sl._topic_meta(vtp)
        chk("비저장소 cwd 도 검증 sha 를 남긴다", bool(vm.get("verified_head")))
        chk("검증 sha 는 어느 저장소 것인지 함께 적는다",
            (vm.get("verified_head") or "").startswith("only@"))
        # 읽는 쪽이 그 형식을 그대로 해석한다 — 드리프트 0 이면 경고가 붙지 않는다
        sl._write_index(mv, db_path=db)
        idx = open(os.path.join(mv, "INDEX.md"), encoding="utf-8").read()
        chk("name@sha 를 읽어 대조까지 간다",
            "검증 시점 미기록" not in idx and "검증 시점 대조 불가" not in idx)
        # 저장소가 여럿이면 어느 것에 대한 검증인지 알 수 없다 — 적지 않는다
        two = os.path.join(parent, "two"); shutil.copytree(mrepo["rb"], two)
        open(vtp, "w", encoding="utf-8").write(
            SKELETON.replace("updated: 2026-08-20\n",
                             "updated: 2026-08-20\nrepos: [only, two]\n"))
        sl._append_topic(mv, "v", "2026-08-22", "dddd4444", "- 작업", "다음",
                         cwd=parent, session_id="s" * 8, verified="테스트 통과")
        chk("어느 저장소인지 모호하면 적지 않는다",
            sl._topic_meta(vtp).get("verified_head"), "")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("=== 전부 통과 ===" if not FAIL else f"=== 실패 {len(FAIL)}건: {FAIL} ==="))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
