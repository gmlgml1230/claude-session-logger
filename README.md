# claude-session-logger

Claude Code 세션의 **대화를 마크다운으로 남긴다.** 하는 일은 그것 하나다.

원본 트랜스크립트(`~/.claude/projects/*/*.jsonl`)는 시간이 지나면 사라진다.
이 훅이 남긴 기록은 남는다 — 실측으로 4~6월 대화가 원본에서 만료된 뒤에도 여기에는 있었다.
도구 실행 라인을 걷어내므로 원본보다 **42배 작다**(761MB → 18MB).

## 설치

`~/.claude/settings.json` 의 `SessionEnd` 에 건다.

```json
{
  "hooks": {
    "SessionEnd": [
      { "hooks": [
        { "type": "command", "command": "python3 /path/to/claude-session-logger/hooks/session_log.py" },
        { "type": "command", "command": "python3 /path/to/claude-session-logger/hooks/nolog_purge.py" }
      ] }
    ]
  }
}
```

파이썬 3.9+ 외에 의존성이 없다.

## 무엇을 남기고 무엇을 빼는가

기록은 `$SESSIONLOG_ROOT/claude_log/<세션8자리>_<날짜>.md` 에 쌓인다.
세션이 여러 날에 걸치면 날짜별로 나뉜다.

**빼는 것**

- **루트 밖에서 돈 세션** — `SESSIONLOG_ROOT`(기본 `~/day1`) 아래가 아니면 기록하지 않는다.
  scratchpad·일회성 디렉터리까지 쌓이면 정작 찾을 때 걸러낼 것이 더 많아진다.
- **도구 실행 라인** — `→ 도구 호출` · `← 도구 결과` · `⌘ ` 로 시작하는 줄.
- **`#로그` 블록** — 붙여넣은 실행 로그. 마커 다음의 코드펜스까지 함께 지운다.
- **`#nolog` 가 있는 세션 전체** — 마커가 **단독 줄**일 때만 인정한다. 부분일치로 잡으면
  마커를 *이야기하는* 문장 하나가 그 세션 기록을 통째로 날린다.
- **사소한 세션** — 도구 라인을 걷어낸 실질 분량이 60자 미만이면 파일을 만들지 않는다.

## 이어서 기록하기

증분 마커(`~/.claude/hooks/sessionlog.db`)에 세션별 처리 지점을 둔다.
같은 세션이 여러 번 종료돼도 새 turn 만 이어 붙는다.

문서에도 `<!-- flush <sid> <날짜> <구간> -->` 를 남긴다 — 마커 저장이 실패해 같은 구간을
다시 처리해도 두 번 실리지 않는다.

## 빠진 세션 되살리기

```bash
python3 hooks/session_log.py --catchup
```

최근 `SESSIONLOG_CATCHUP_DAYS`(기본 3)일 안에 수정된 트랜스크립트를 훑어 SessionEnd 와
똑같이 기록한다. 훅이 돌지 않은 세션(비정상 종료·훅 실패)을 되살리는 수동 경로다.
이미 기록된 세션은 증분 마커를 보고 즉시 넘어간다.

자동 실행은 없다. 자정 launchd 잡이 있었지만 `~/Documents` 가 macOS 보호 폴더라
백그라운드 프로세스가 TCC 로 조용히 거부당했고(실측 3회 전량 실패, 기록 0건), 제거했다.

## 설정

| 환경변수 | 기본값 | 뜻 |
|---|---|---|
| `SESSIONLOG_ROOT` | `~/day1` | 이 아래에서 돈 세션만 기록한다 |
| `SESSIONLOG_DIR` | `claude_log` | 루트 아래 기록 디렉터리명 |
| `SESSIONLOG_STATE_DIR` | `~/.claude/hooks` | 증분 마커 DB·디버그 로그 위치 |
| `SESSIONLOG_CATCHUP_DAYS` | `3` | `--catchup` 이 훑는 기간(일) |

상태는 레포 밖에 둔다 — clone 위치를 옮겨도 기록이 이어지고, 레포에 로컬 상태가 섞이지 않는다.

## 테스트

pytest 가 아니라 그냥 스크립트다. 임시 루트와 임시 상태 디렉터리를 만들어 돌므로
실제 기록·DB 를 건드리지 않는다.

```bash
python3 tests/test_log.py
```
