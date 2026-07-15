# Agent Browser (로컬 LLM 구동) — 설계 초안

Aside류 에이전트 브라우저를 클라우드 API 없이 **로컬 LLM만으로** 구동하는 프로젝트 초안.

## 1. 목표
- 자연어 지시("이 사이트에서 가격 비교해줘")를 받아 브라우저를 자율 조작
- LLM 추론은 전부 로컬(Ollama / llama.cpp / LM Studio) — 비용 0, 프라이버시 보장
- 데스크톱 앱이 아닌 **기존 브라우저 + 사이드카 에이전트** 구조로 시작 (개발 비용 최소화)

## 2. 아키텍처

```
┌─────────────┐   자연어 지시    ┌──────────────────┐
│  사용자 UI   │ ───────────────▶│  Agent Loop      │
│ (CLI→웹패널) │ ◀─────────────── │  (Python)        │
└─────────────┘   진행상황/결과   └───┬──────────┬───┘
                                     │ tool call │ OpenAI 호환 API
                              ┌──────▼─────┐ ┌───▼─────────┐
                              │ Playwright │ │ Ollama      │
                              │ (Chromium) │ │ (로컬 LLM)   │
                              └────────────┘ └─────────────┘
```

- **Agent Loop**: 관찰(페이지 상태) → 추론(LLM tool call) → 행동(브라우저 조작) 반복
- **브라우저 제어**: Playwright CDP. 나중에 사용자의 실제 Chrome에 `--remote-debugging-port`로 attach 가능
- **LLM 연동**: Ollama의 OpenAI 호환 endpoint(`http://localhost:11434/v1`) 사용 → 나중에 llama.cpp server, LM Studio로 교체해도 코드 변경 불필요

## 3. 로컬 LLM 선택 (tool calling 지원 필수)

| 모델 | 크기 | 비고 |
|---|---|---|
| Qwen2.5-Coder / Qwen2.5 7B~14B | 8~16GB RAM | tool calling 안정적, 1순위 추천 |
| Llama 3.1 8B | ~8GB | 무난, 한국어 다소 약함 |
| Mistral-Nemo 12B | ~12GB | function calling 양호 |

- 24GB+ VRAM이면 Qwen2.5 32B 급으로 성공률 크게 상승
- 작은 모델은 긴 DOM을 못 버티므로 **관찰 압축**(아래 5절)이 핵심

## 4. 도구(Tool) 스키마 — v0 최소셋

| tool | 인자 | 설명 |
|---|---|---|
| `navigate` | url | 페이지 이동 |
| `read_page` | – | 페이지를 텍스트+인터랙티브 요소 목록으로 요약 반환 |
| `click` | element_id | read_page가 부여한 번호로 클릭 |
| `type_text` | element_id, text, submit | 입력 후 Enter 옵션 |
| `scroll` | direction | 스크롤 후 자동 re-read |
| `done` | summary | 작업 완료 보고 |

element_id 방식(스냅샷마다 인터랙티브 요소에 번호 부여)을 쓰는 이유:
로컬 소형 모델은 CSS 셀렉터 생성을 자주 틀리지만 "3번 클릭"은 잘함.

## 5. 관찰 압축 (로컬 LLM의 핵심 제약 대응)
- 컨텍스트 8k~32k 가정. 원본 DOM은 수십만 토큰 → 그대로 못 넣음
- `read_page`는 다음만 추출:
  1. 보이는 텍스트(중복/공백 정리, 상한 ~3천 자)
  2. 인터랙티브 요소(a, button, input, select 등)의 `[번호] 태그 "라벨"` 목록 (상한 ~60개)
- 대화 히스토리는 최근 N턴만 유지, 오래된 관찰은 버림 (행동 로그만 남김)

## 6. 안전장치
- 최대 스텝 수 제한 (기본 15)
- 구매/결제/로그인/삭제 등 위험 행동은 사용자 확인 후 진행 (v0: 도메인 allowlist + 확인 프롬프트)
- 새 도메인 이동 시 로그 표시

## 7. 로드맵
- **v0 (이 초안)**: CLI + Playwright headed + Ollama, 단일 태스크 실행 ← `agent.py`
- **v1**: 사용자 실제 Chrome에 CDP attach, 사이드패널 UI(로컬 웹서버), 세션/쿠키 재사용
- **v2**: 멀티탭, 작업 메모리(파일 기반), 스크린샷 기반 비전 모델 병행(Qwen2.5-VL), 매크로 저장/재생
- **v3**: 브라우저 확장 프로그램으로 패키징, 사용자 개입(human-in-the-loop) UI

## 8. 실행 방법 (v0)
```bash
ollama pull qwen2.5:7b          # 또는 qwen2.5:14b
pip install -r requirements.txt
playwright install chromium
python agent.py "hacker news에서 오늘 1위 글 제목 알려줘"
```
