---
name: repo-fit-check
description: 내 목적·단계·제약에 맞는 GitHub repo를 찾고(후보 탐색) 맞는 점/안 맞는 점/확인 못한 점을 증거와 함께 정리하는 스킬. "이 repo 써도 될까", "~에 쓸 만한 repo 찾아줘", "스타 목록에서 골라줘", "repo 적합성" 같은 요청에 사용.
---

# repo-fit-check

> 구현 상태: **a(입력) · b(후보 탐색) 구현됨**. c(검증) · d(판정 리포트)는 PRD 단계 — 아직 이 스킬이 하지 않는다.
> 따라서 지금 출력은 "후보 목록"이며, 어떤 repo도 "적합하다"고 말하지 않는다.

## 필요한 것
`gh` CLI 로그인(`gh auth status`)과 Python 3. GitHub MCP는 필요 없다.

## a. 입력 받기
다음 3가지를 받는다. 비어 있으면 묻지 말고 기본값을 쓰고 결과에 "가정"으로 적는다.

| 항목 | 예 | 기본값 |
|---|---|---|
| 목적 | 한국 노동법 준수 점검 도구 | (필수) |
| 단계 | 기획 / 설계 / 구현 / 검증 / 배포 | 구현 |
| 제약 | 라이선스 MIT·Apache-2.0, 피할 것 AGPL, 언어 TypeScript | 허용형 라이선스, AGPL 제외 |

## b. 후보 탐색
### 1) 영어 검색어 만들기 (가장 중요)
목적에서 **핵심 개념 3~4개**를 뽑아 영어 동의어 묶음으로 만든다. 형식: `"a|b, c|d|e, f"` (쉼표=필수 그룹, `|`=동의어).
- 한국어 목적은 직접 번역한다. 번역하기 어려우면 `--purpose "<한국어>"`로 넘기면 `references/ko_glossary.json`이 변환하되, 못 바꾸는 단어가 있으면 오류로 멈춘다(빈 결과로 넘어가지 않는다).
- 모호한 단어(`fit`, `tool`, `platform`)는 넣지 않는다. 구체적인 도메인 단어를 넣는다.
- 예: 노동법 점검 → `"korea|korean, labor|employment, law|statute|compliance"`

### 2) 실제 환경 스캔 (중복 추천 방지)
```bash
python "$HOME/.claude/skills/repo-fit-check/scripts/scan_env.py" --out "$TEMP/env.json" [--live live.txt]
```
디스크에서 읽는 것: `~/.claude/skills`, 플러그인 폴더의 스킬·에이전트·`.mcp.json`(서버 **이름만**), `~/.claude.json`, `~/.codex`.
디스크에 없는 것(claude.ai 커넥터의 현재 연결 상태, 이 세션의 deferred tool)은 지금 세션에서 보이는 이름을 `live.txt`에 한 줄씩 적어 `--live`로 넘긴다(스킬 폴더: `~/.claude/skills/repo-fit-check`)(`mcp:Supabase`, `skill:xxx`).
결과의 `[weak]` 표시는 이름 일치가 3개 미만이라 "있다"고 단정하기 약하다는 뜻이다.

### 3) 후보 찾기
```bash
python "$HOME/.claude/skills/repo-fit-check/scripts/discover.py" --terms "<검색어>" --stage <단계> \
    --license MIT,Apache-2.0,BSD-3-Clause --avoid AGPL [--language TypeScript] [--source stars,search] [--top 10]
python "$HOME/.claude/skills/repo-fit-check/scripts/discover.py" --gaps "$TEMP/env.json"        # 환경 공백 기반 탐색
```
- `stars`: 내 스타 목록(`gh api user/starred`, 24시간 캐시)을 **먼저** 같은 기준으로 채점한다.
- `search`: GitHub 검색. 결과가 부족하면 엄격 → 한 단어 빼기 → 토픽 → 단어 쌍 → 동의어 순으로 자동 완화하고, 시도한 쿼리(trace)를 출력한다.
- 정렬은 **관련도 70%** + 활동도 15% + 스타(로그, 상한) 10% + 라이선스 5%. 스타순이 아니다. 드문 용어일수록 가중치가 높다.
- 스타 500 미만이면서 관련도가 높은 repo는 "작은 repo" 표로 따로 보여 준다.

## 결과를 사용자에게 말하는 법
1. 탐색 출력(후보 표)을 그대로 요약하되 **"후보"** 라고 부른다. 적합 판정이 아니다.
2. 사용한 검색어, 가정한 기본값, 스타 목록에서 몇 개를 훑었는지 한 줄로 밝힌다.
3. 라이선스 `unknown`, 관련도가 낮은 행, 완화 단계(`pairs`/`singles`)에서만 잡힌 행은 그렇다고 표시한다.
4. 상위 후보에 대해서는 c·d가 구현되기 전까지 "README와 실제 코드가 일치하는지, 이슈·PR 상태는 확인하지 않았다"고 명시한다.
5. 설치·실행은 사용자 승인 뒤에만 한다.

## 테스트
```bash
python -I -m unittest discover -s "$HOME/.claude/skills/repo-fit-check/tests"
```
네트워크 없이 도는 회귀 테스트(환경 스캔 비밀값 미노출, 스타 쏠림, 완화 사다리, 한국어 입력 실패 처리 등).

## 출처
카테고리 목록과 환경 스캔의 발상은 lukeleekr/claude-github-scout(MIT)에서 가져와 확장했다. `NOTICE.md` 참고.
