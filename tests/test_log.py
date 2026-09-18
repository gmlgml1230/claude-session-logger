#!/usr/bin/env python3
"""대화 기록 회귀 테스트. 각 항목은 실제로 재현된 결함이나 지켜야 할 계약에 대응한다."""
import atexit
import importlib.util, json, os, shutil, tempfile

_STATE = tempfile.mkdtemp(prefix="sessionlog-test-")
os.environ["SESSIONLOG_STATE_DIR"] = _STATE
atexit.register(shutil.rmtree, _STATE, True)

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "session_log.py")

FAIL = []
def chk(name, got, want=True):
    if got != want:
        FAIL.append(name); print(f"✗ {name}\n   got : {got!r}\n   want: {want!r}")
    else:
        print(f"✓ {name}")


def transcript(path, sid, cwd, turns):
    with open(path, "w", encoding="utf-8") as f:
        for role, text in turns:
            f.write(json.dumps({
                "type": role, "timestamp": "2026-09-18T10:00:00Z", "cwd": cwd,
                "message": {"role": role, "content": text},
            }, ensure_ascii=False) + "\n")


def main():
    tmp = tempfile.mkdtemp()
    try:
        root = os.path.join(tmp, "day1"); os.makedirs(root)
        os.environ["SESSIONLOG_ROOT"] = root
        spec = importlib.util.spec_from_file_location("sl", HOOK)
        sl = importlib.util.module_from_spec(spec); spec.loader.exec_module(sl)
        sl.DEBUG_LOG_DIR = tmp
        base = os.path.join(root, "claude_log")
        db = os.path.join(tmp, "t.db")

        # ① 루트 아래 세션은 기록된다
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        tr = os.path.join(tmp, f"{sid}.jsonl")
        transcript(tr, sid, os.path.join(root, "data-dbt"), [
            ("user", "dim_voucher 모델의 증분 조건을 점검하고 누락분을 찾아줘."),
            ("assistant", "lookback 을 30분에서 6시간으로 늘려 누락 12건을 회수했습니다."),
        ])
        sl._process(tr, base=base, db_path=db)
        files = os.listdir(base) if os.path.isdir(base) else []
        chk("루트 아래 세션이 기록된다", len(files), 1)
        txt = open(os.path.join(base, files[0]), encoding="utf-8").read()
        chk("사용자 발화가 실린다", "dim_voucher" in txt)
        chk("어시스턴트 응답이 실린다", "lookback" in txt)

        # ② 루트 밖 세션은 기록하지 않는다 — scratchpad·다른 프로젝트
        sid2 = "bbbbbbbb-1111-2222-3333-444444444444"
        tr2 = os.path.join(tmp, f"{sid2}.jsonl")
        outside = os.path.join(tmp, "elsewhere"); os.makedirs(outside)
        transcript(tr2, sid2, outside, [
            ("user", "이 스크래치패드에서 임시 파일을 정리하고 결과를 알려줘."),
            ("assistant", "임시 파일 8개를 지웠습니다."),
        ])
        sl._process(tr2, base=base, db_path=db)
        chk("루트 밖 세션은 기록하지 않는다", len(os.listdir(base)), 1)
        # 형제 디렉터리가 접두어로 걸리면 안 된다 (day1 vs day1-old)
        sib = root + "-old"; os.makedirs(sib, exist_ok=True)
        chk("형제 디렉터리는 루트가 아니다", sl._in_root(sib), False)
        chk("루트 자신은 루트다", sl._in_root(root))
        chk("루트 하위는 루트다", sl._in_root(os.path.join(root, "a", "b")))

        # ③ 도구 실행 라인은 남기지 않는다
        sid3 = "cccccccc-1111-2222-3333-444444444444"
        tr3 = os.path.join(tmp, f"{sid3}.jsonl")
        transcript(tr3, sid3, root, [
            ("user", "day1_mart 데이터셋의 테이블 목록을 조회해서 개수와 이름을 정리해줘."),
            ("assistant", [
                {"type": "tool_use", "name": "Bash"},
                {"type": "tool_result", "content": "42 rows: dim_voucher, fact_order, ..."},
                {"type": "text",
                 "text": "조회 결과 42개입니다. dim_ 접두어 18개와 fact_ 접두어 24개로 나뉩니다."},
            ]),
        ])
        sl._process(tr3, base=base, db_path=db)
        t3 = open(os.path.join(base, f"{sid3[:8]}_2026-09-18.md"), encoding="utf-8").read()
        chk("도구 라인 제거", "⌘ bq ls" not in t3 and "→ 도구" not in t3 and "← 도구" not in t3)
        chk("도구 라인 외 본문은 남는다", "조회 결과 42개" in t3)

        # ④ '#로그' 블록은 남기지 않는다
        sid4 = "dddddddd-1111-2222-3333-444444444444"
        tr4 = os.path.join(tmp, f"{sid4}.jsonl")
        transcript(tr4, sid4, root, [
            ("user", "아래 붙여넣은 로그를 보고 원인을 찾아줘. 어느 단계에서 실패한 건지 알려줘.\n"
                     "#로그\n```\nERROR 2026-09-18 스택트레이스 아주 긴 내용\n```"),
            ("assistant", "커넥터 재시작 한도 초과가 원인입니다. restart.attempts 를 올려야 합니다."),
        ])
        sl._process(tr4, base=base, db_path=db)
        t4 = open(os.path.join(base, f"{sid4[:8]}_2026-09-18.md"), encoding="utf-8").read()
        chk("#로그 블록 제거", "스택트레이스" not in t4)
        chk("#로그 앞 발화는 남는다", "원인을 찾아줘" in t4)

        # ⑤ '#nolog' 는 세션 전체를 뺀다
        sid5 = "eeeeeeee-1111-2222-3333-444444444444"
        tr5 = os.path.join(tmp, f"{sid5}.jsonl")
        transcript(tr5, sid5, root, [
            ("user", "#nolog"),
            ("user", "이건 기록하지 말고 그냥 계산만 해줘. 합계가 얼마인지 알려줘."),
            ("assistant", "합계는 128 입니다."),
        ])
        sl._process(tr5, base=base, db_path=db)
        chk("#nolog 세션은 파일이 안 생긴다",
            os.path.exists(os.path.join(base, f"{sid5[:8]}_2026-09-18.md")), False)
        chk("#nolog 도 마커는 전진한다 — 다음 flush 가 되살리지 않게",
            sl.db_get_processed(sid5, db) > 0)

        # ⑥ 같은 구간을 다시 처리해도 두 번 실리지 않는다
        before = open(os.path.join(base, files[0]), encoding="utf-8").read()
        sl.db_set_processed(sid, 0, db)      # 마커 저장 실패 상황 재현
        sl._process(tr, base=base, db_path=db)
        after = open(os.path.join(base, files[0]), encoding="utf-8").read()
        chk("같은 구간 재처리 시 중복 기록 없음", after, before)

        # ⑦ 사소한 세션은 파일을 만들지 않는다
        sid7 = "77777777-1111-2222-3333-444444444444"
        tr7 = os.path.join(tmp, f"{sid7}.jsonl")
        transcript(tr7, sid7, root, [("user", "고마워"), ("assistant", "네")])
        sl._process(tr7, base=base, db_path=db)
        chk("사소한 세션은 기록하지 않는다",
            os.path.exists(os.path.join(base, f"{sid7[:8]}_2026-09-18.md")), False)

        # ⑧ 이어지는 대화는 append 되고 구간이 겹치지 않는다
        transcript(tr, sid, os.path.join(root, "data-dbt"), [
            ("user", "dim_voucher 모델의 증분 조건을 점검하고 누락분을 찾아줘."),
            ("assistant", "lookback 을 30분에서 6시간으로 늘려 누락 12건을 회수했습니다."),
            ("user", "그럼 같은 문제가 다른 모델에도 있는지 전수로 확인해줘."),
            ("assistant", "dim_order 등 3개 모델에서 같은 패턴을 찾았습니다."),
        ])
        sl._process(tr, base=base, db_path=db)
        t8 = open(os.path.join(base, files[0]), encoding="utf-8").read()
        chk("이어지는 대화가 append 된다", "dim_order" in t8)
        chk("앞 구간이 중복되지 않는다", t8.count("누락 12건을 회수"), 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("=== 전부 통과 ===" if not FAIL else f"=== 실패 {len(FAIL)}건: {FAIL} ==="))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
