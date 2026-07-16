"""Agent browser v0 — 로컬 LLM + Playwright 최소 프로토타입.

사용법:
    python agent.py "hacker news에서 오늘 1위 글 제목 알려줘"

환경변수:
    LLM_BASE_URL (기본 http://localhost:11434/v1)
    LLM_MODEL    (기본 qwen2.5:7b)
    HEADLESS     (기본 1 = 헤드리스 실행. "0"이면 창을 띄워서 실행)

백엔드는 OpenAI 호환 /v1/chat/completions 엔드포인트면 무엇이든 LLM_BASE_URL /
LLM_MODEL 두 환경변수만 바꿔서 붙일 수 있다. 코드 변경 불필요.

  # Ollama
  LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:7b \
      python agent.py "..."

  # Apple MLX (mlx-lm 서버, Apple Silicon)
  #   pip install mlx-lm
  #   mlx_lm.server --model mlx-community/Qwen2.5-7B-Instruct-4bit
  LLM_BASE_URL=http://localhost:8080/v1 LLM_MODEL=mlx-community/Qwen2.5-7B-Instruct-4bit \
      python agent.py "..."

  # MLX Omni Server (mlx-omni-server, 기본 포트 10240)
  LLM_BASE_URL=http://localhost:10240/v1 LLM_MODEL=mlx-community/Qwen2.5-7B-Instruct-4bit \
      python agent.py "..."
"""
import json
import os
import sys

from openai import OpenAI
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b")
MAX_STEPS = 15
DEFAULT_HEADLESS = True  # 안전한 기본값: 명시적으로 HEADLESS=0을 줘야 창을 띄운다
MAX_TEXT = 3000       # read_page 텍스트 상한(문자)
MAX_ELEMENTS = 60     # 인터랙티브 요소 상한
HISTORY_TURNS = 6     # 최근 유지할 assistant/tool 턴 수 (로컬 모델 컨텍스트 절약)

TOOLS = [
    {"type": "function", "function": {
        "name": "navigate", "description": "URL로 이동한다",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "read_page", "description": "현재 페이지의 텍스트와 클릭 가능한 요소 목록을 읽는다",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "click", "description": "read_page가 보여준 [번호] 요소를 클릭한다",
        "parameters": {"type": "object", "properties": {
            "element_id": {"type": "integer"}}, "required": ["element_id"]}}},
    {"type": "function", "function": {
        "name": "type_text", "description": "[번호] 입력란에 텍스트를 입력한다",
        "parameters": {"type": "object", "properties": {
            "element_id": {"type": "integer"},
            "text": {"type": "string"},
            "submit": {"type": "boolean", "description": "입력 후 Enter"}},
            "required": ["element_id", "text"]}}},
    {"type": "function", "function": {
        "name": "scroll", "description": "페이지를 스크롤한다",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["down", "up"]}},
            "required": ["direction"]}}},
    {"type": "function", "function": {
        "name": "done", "description": "작업 완료. 결과를 요약해 보고한다",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]

SYSTEM = """너는 웹 브라우저를 조작하는 에이전트다.
규칙:
- 반드시 도구를 하나씩 호출해서 작업한다. 페이지 이동/클릭 후에는 read_page로 상태를 확인한다.
- 클릭/입력은 read_page가 부여한 [번호]로만 지정한다.
- 목표를 달성하면 done(summary=한국어 결과 요약)을 호출한다.
- 로그인, 결제, 구매, 삭제는 절대 수행하지 말고 done으로 상황을 보고한다."""

INTERACTIVE_SEL = "a, button, input, select, textarea, [role=button], [onclick]"

# done은 run()에서 별도로 처리하므로 Browser에 실제로 위임 가능한 도구만 화이트리스트로 둔다.
# (모델이 존재하지 않는/내부 메서드 이름을 tool_call로 잘못 생성해도 getattr로 임의
#  Browser 메서드(e.g. `_el`)가 호출되는 일을 막는다)
BROWSER_TOOLS = {t["function"]["name"] for t in TOOLS if t["function"]["name"] != "done"}


class Browser:
    def __init__(self, page):
        self.page = page
        self.elements = []  # element_id -> locator

    def navigate(self, url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return f"이동 완료: {self.page.url}"

    def read_page(self):
        self.page.wait_for_timeout(500)
        text = self.page.evaluate("() => document.body.innerText")
        text = " ".join(text.split())[:MAX_TEXT]
        self.elements = []
        lines = []
        for loc in self.page.locator(INTERACTIVE_SEL).all():
            try:
                if not loc.is_visible():
                    continue
                tag = loc.evaluate("el => el.tagName.toLowerCase()")
                label = (loc.inner_text(timeout=200) or
                         loc.get_attribute("placeholder") or
                         loc.get_attribute("aria-label") or
                         loc.get_attribute("value") or "")
                label = " ".join(label.split())[:80]
                if not label and tag not in ("input", "textarea", "select"):
                    continue
                idx = len(self.elements)
                self.elements.append(loc)
                lines.append(f'[{idx}] <{tag}> "{label}"')
                if idx + 1 >= MAX_ELEMENTS:
                    break
            except Exception:
                continue
        return (f"URL: {self.page.url}\n제목: {self.page.title()}\n"
                f"--- 본문 ---\n{text}\n--- 요소 ---\n" + "\n".join(lines))

    def _el(self, i):
        if not (0 <= i < len(self.elements)):
            raise ValueError(f"요소 번호 {i} 없음. read_page를 다시 호출하라.")
        return self.elements[i]

    def click(self, element_id):
        self._el(element_id).click(timeout=5000)
        return "클릭 완료. read_page로 결과를 확인하라."

    def type_text(self, element_id, text, submit=False):
        el = self._el(element_id)
        el.fill(text)
        if submit:
            el.press("Enter")
        return "입력 완료."

    def scroll(self, direction):
        dy = 600 if direction == "down" else -600
        self.page.mouse.wheel(0, dy)
        return "스크롤 완료. read_page로 새 내용을 확인하라."


def headless_from_env():
    """HEADLESS 환경변수를 해석한다.

    - 미설정: DEFAULT_HEADLESS(True) — CI/서버 환경에서 항상 안전하게 동작해야 한다.
    - "0"/"false"/"no": 명시적으로 headed 모드 (디버깅용, 디스플레이 필요)
    - 그 외 값(포함 "1"): headless 모드

    이전 구현(`os.environ.get("HEADLESS") == "1" or None`)은 HEADLESS가 무엇이든
    "1"이 아니면 항상 `None`을 넘겼는데, Playwright는 `headless=None`을 내부적으로
    기본값(headless=True)으로 취급한다. 즉 의도치 않게 "항상 headless"로 동작하면서도
    HEADLESS=0으로 headed를 켤 방법이 없었다(디버깅 불가) — 그리고 그 동작이 Playwright의
    문서화되지 않은 내부 기본값에 암묵적으로 의존하고 있었다. 아래는 그 의도를 명시적으로
    구현한다.
    """
    val = os.environ.get("HEADLESS")
    if val is None:
        return DEFAULT_HEADLESS
    return val.strip().lower() not in ("0", "false", "no")


def trim_history(messages):
    """system+user(목표)는 고정 유지, 그 뒤는 최근 턴만 유지해 컨텍스트 절약."""
    head, tail = messages[:2], messages[2:]
    if len(tail) > HISTORY_TURNS * 2:
        tail = tail[-HISTORY_TURNS * 2:]
        # tool 메시지가 잘려서 선행 assistant tool_call과 짝이 안 맞으면 앞쪽 tool 메시지 제거
        while tail and tail[0]["role"] == "tool":
            tail = tail[1:]
    return head + tail


def run(task, headless=None, on_step=None):
    """에이전트 루프를 실행한다.

    Args:
        task: 자연어 작업 지시.
        headless: True/False로 명시 override. None이면 headless_from_env() 사용.
        on_step: 로그 한 줄(str)을 받는 콜백. None이면 print()로 출력한다(CLI 기본
            동작과 동일). panel.py 등 다른 소비자가 로그를 스트리밍하고 싶을 때
            여기에 자체 콜백(예: 큐에 append)을 넘기면 된다.

    Returns:
        완료 시 결과 요약(str). 최대 스텝 도달이나 에러로 중단되면 None.
    """
    log = on_step if on_step else print
    use_headless = headless_from_env() if headless is None else headless

    client = OpenAI(base_url=BASE_URL, api_key="local")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=use_headless)
            try:
                b = Browser(browser.new_page())
                messages = [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": f"목표: {task}"}]
                for step in range(MAX_STEPS):
                    messages = trim_history(messages)
                    resp = client.chat.completions.create(
                        model=MODEL, messages=messages, tools=TOOLS, temperature=0.2)
                    msg = resp.choices[0].message
                    messages.append(msg.model_dump(exclude_none=True))
                    if not msg.tool_calls:
                        log(f"\n[결과] {msg.content}")
                        return msg.content
                    for tc in msg.tool_calls:
                        name = tc.function.name
                        args = json.loads(tc.function.arguments or "{}")
                        log(f"[step {step + 1}] {name}({args})")
                        if name == "done":
                            summary = args.get("summary")
                            log(f"\n[결과] {summary}")
                            return summary
                        if name not in BROWSER_TOOLS:
                            result = f"오류: 알 수 없는 도구 '{name}'"
                        else:
                            try:
                                result = getattr(b, name)(**args)
                            except Exception as e:
                                result = f"오류: {e}"
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": result})
                log("\n[중단] 최대 스텝 도달")
                return None
            finally:
                browser.close()
    except Exception as e:
        log(f"\n[오류] 에이전트 실행 실패: {e}")
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('사용법: python agent.py "작업 내용"')
    run(sys.argv[1])
