# Agent Browser (로컬 LLM 구동) — 설계 & 사용법

Aside류 에이전트 브라우저를 클라우드 API 없이 **로컬 LLM만으로** 구동하는 프로젝트.
`agent.py`(CLI 에이전트 루프)와 `panel.py`(로컬 웹 패널)로 구성되며,
`tests/`의 mock LLM + 정적 테스트 사이트로 동작을 실제 검증한 상태다.

## 1. 목표
- 자연어 지시("이 사이트에서 가격 비교해줘")를 받아 브라우저를 자율 조작
- LLM 추론은 전부 로컬(Ollama / llama.cpp / LM Studio / Apple MLX) — 비용 0, 프라이버시 보장
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
- **LLM 연동**: OpenAI 호환 `/v1/chat/completions` endpoint를 쓰는 백엔드라면 무엇이든 붙는다.
  `LLM_BASE_URL` / `LLM_MODEL` 두 환경변수만 바꾸면 되고 `agent.py`/`panel.py` 코드 변경은 불필요하다.
  Ollama(`http://localhost:11434/v1`), llama.cpp server, LM Studio, Apple MLX 계열
  (`mlx-lm`, `mlx-omni-server`)을 모두 이 방식으로 지원한다 — 자세한 접속 방법은 3절 참고.

## 3. 로컬 LLM 백엔드 (tool calling 지원 필수)

`agent.py`/`panel.py`는 OpenAI 파이썬 SDK로 `LLM_BASE_URL`에 요청을 보낼 뿐이므로,
아래 어떤 백엔드를 쓰든 **환경변수만 바꾸면** 그대로 동작한다.

### Ollama
```bash
ollama pull qwen2.5:7b          # 또는 qwen2.5:14b
LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:7b python agent.py "..."
```

### Apple MLX (Apple Silicon, `mlx-lm` 서버) — 1급 지원
```bash
pip install mlx-lm
mlx_lm.server --model mlx-community/Qwen2.5-7B-Instruct-4bit
# 기본적으로 http://localhost:8080 에서 OpenAI 호환 /v1 을 제공한다
LLM_BASE_URL=http://localhost:8080/v1 \
LLM_MODEL=mlx-community/Qwen2.5-7B-Instruct-4bit \
  python agent.py "hacker news에서 오늘 1위 글 제목 알려줘"
```
- Apple Silicon(M-시리즈)에서는 MLX 4bit 양자화 모델이 같은 모델의 Ollama(GGUF) 실행보다
  더 빠른 경우가 많다(MLX가 Apple GPU/Neural Engine 메모리 대역폭에 최적화됨). Mac에서
  로컬로 돌린다면 MLX를 우선 검토할 가치가 있다.
- 대안: [`mlx-omni-server`](https://github.com/madroidmaq/mlx-omni-server)도 OpenAI 호환
  서버를 제공한다(`pip install mlx-omni-server`, 기본 포트 `10240`, 경로는 동일하게 `/v1`).
  `LLM_BASE_URL=http://localhost:10240/v1`로 지정하면 된다.

### 기타 (llama.cpp server, LM Studio 등)
OpenAI 호환 `/v1` 엔드포인트를 노출하는 서버라면 동일하게 `LLM_BASE_URL`만 맞추면 된다.

### 모델 선택 가이드 (tool calling 안정성 기준)

| 모델 | 크기 | 비고 |
|---|---|---|
| Qwen2.5-Coder / Qwen2.5 7B~14B | 8~16GB RAM | tool calling 안정적, 1순위 추천 (Ollama/MLX 둘 다 4bit 빌드 존재) |
| Llama 3.1 8B | ~8GB | 무난, 한국어 다소 약함 |
| Mistral-Nemo 12B | ~12GB | function calling 양호 |

- 24GB+ (V)RAM이면 Qwen2.5 32B 급으로 성공률 크게 상승
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
- **v0**: CLI + Playwright headless(기본) + 로컬 LLM(Ollama/MLX 등), 단일 태스크 실행 ← `agent.py`
- **v1-lite (완료)**: 로컬 웹 패널(`panel.py`, stdlib만 사용) — 태스크 입력 + 진행 로그 폴링 스트리밍
- **v1**: 사용자 실제 Chrome에 CDP attach, 세션/쿠키 재사용
- **v2**: 멀티탭, 작업 메모리(파일 기반), 스크린샷 기반 비전 모델 병행(Qwen2.5-VL), 매크로 저장/재생
- **v3**: 브라우저 확장 프로그램으로 패키징, 사용자 개입(human-in-the-loop) UI

## 8. 실행 방법

### 8.1 설치
```bash
pip install -r requirements.txt
playwright install chromium   # 브라우저가 이미 캐시돼 있는 환경(CI 등)이면 생략 가능
```

### 8.2 CLI로 실행
```bash
# 예: Ollama
ollama pull qwen2.5:7b
LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:7b \
  python agent.py "hacker news에서 오늘 1위 글 제목 알려줘"
```
- `HEADLESS` 환경변수: 기본값 `1`(헤드리스). 브라우저 창을 눈으로 보며 디버깅하려면
  `HEADLESS=0`으로 명시적으로 켜야 한다(디스플레이가 있는 환경에서만).
- 백엔드를 Ollama 대신 MLX 등으로 바꾸려면 3절의 `LLM_BASE_URL`/`LLM_MODEL` 값만 바꾸면 된다.

### 8.3 웹 패널로 실행 (v1-lite)
```bash
LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:7b python panel.py
# http://localhost:8765 접속 → 태스크 입력 → 진행 로그가 실시간(폴링)으로 표시됨
```
- `PANEL_PORT` 환경변수로 포트 변경 가능(기본 8765).
- `agent.py`의 `run()`을 백그라운드 스레드에서 그대로 호출한다. 외부 웹 프레임워크
  없이 `http.server` + `threading`만 사용.
- 동시에는 한 번에 한 태스크만 실행 가능(실행 중 다른 태스크 제출 시 409 오류).

### 8.4 테스트 (mock LLM 기반 e2e)
로컬 LLM 없이도 에이전트 루프 전체(도구 호출 → 브라우저 조작 → 관찰 → 재호출 → 완료)를
검증할 수 있는 e2e 테스트가 `tests/`에 있다.
```bash
pip install openai playwright   # requirements.txt와 동일
python tests/run_e2e.py
```
구성:
- `tests/test_site/`: 정적 테스트 페이지 3장(홈 → 검색 폼 → 결과). `run_e2e.py`가 자동으로
  `http.server`로 띄운다.
- `tests/mock_llm.py`: `/v1/chat/completions`를 흉내내는 stdlib HTTP 서버. 미리 정한
  시나리오대로 `navigate → read_page → click → read_page → type_text → read_page →
  (scroll+read_page 동시 tool_call) → scroll → read_page → done` tool_calls를 순서대로
  반환하고, agent.py가 되돌려 보내는 메시지 히스토리가 OpenAI 스키마(특히 tool/assistant
  짝맞춤)에 맞는지 매 호출마다 assert한다.
- `tests/run_e2e.py`: 위 둘을 띄우고 `HEADLESS=1`로 `agent.py`를 서브프로세스 실행해
  `done`까지 도달하는지, mock 서버 쪽 포맷 검증이 통과했는지, 시나리오를 끝까지
  소비했는지를 확인한다. 통과 시 `=== E2E 통과 ===`를 출력하고 종료코드 0.
