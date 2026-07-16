"""OpenAI 호환 /v1/chat/completions 를 흉내내는 초소형 stdlib 목(mock) 서버.

agent.py를 실제 로컬 LLM 없이 e2e로 검증하기 위해, 미리 정해둔 시나리오대로
tool_calls 응답을 순서대로 반환한다. 매 요청마다 agent.py가 보낸 메시지 히스토리가
OpenAI Chat Completions 스키마에 맞는 "정상 포맷"인지도 검증(assert)한다.

정상 포맷 체크리스트:
  - 각 메시지는 role 키를 가진다 (system/user/assistant/tool 중 하나)
  - role=tool 메시지는 tool_call_id(str)와 content(str)를 가진다
  - role=assistant 메시지가 tool_calls를 가지면, 그 tool_calls의 각 id는
    바로 뒤에 동일한 tool_call_id를 가진 tool 메시지(들)로 응답되어 있어야
    한다 (assistant/tool 쌍이 끊어지지 않았는지 = trim_history 정합성 검증)
  - 헤더 role=system, 그 다음 role=user 가 항상 맨 앞에 유지되어야 한다
    (agent.py의 trim_history가 head를 보존하는지 검증)

위반 시 AssertionError를 그대로 올려 500 응답과 함께 errors.log 파일에 기록한다.
run_e2e.py는 이 로그 파일 존재 여부로 최종 성패를 판정한다.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ERROR_LOG = os.path.join(os.path.dirname(__file__), "mock_llm_errors.log")

# ---------------------------------------------------------------------------
# 시나리오: home.html -> (링크 클릭) search.html -> (검색어 입력+제출) results.html
# -> (한 응답에 tool_call 2개: scroll + read_page, 짝맞춤 검증용) -> scroll -> read_page -> done
# ---------------------------------------------------------------------------
SCENARIO = [
    [{"name": "navigate", "arguments": {"url": "http://127.0.0.1:8099/home.html"}}],
    [{"name": "read_page", "arguments": {}}],
    [{"name": "click", "arguments": {"element_id": 0}}],
    [{"name": "read_page", "arguments": {}}],
    [{"name": "type_text", "arguments": {"element_id": 0, "text": "playwright", "submit": True}}],
    [{"name": "read_page", "arguments": {}}],
    # 한 응답에 tool_call을 2개 담아 trim_history의 assistant/tool 짝맞춤을 검증
    [{"name": "scroll", "arguments": {"direction": "down"}},
     {"name": "read_page", "arguments": {}}],
    [{"name": "scroll", "arguments": {"direction": "up"}}],
    [{"name": "read_page", "arguments": {}}],
    [{"name": "done", "arguments": {"summary": "검색 결과 페이지에서 playwright 검색 결과를 확인했다."}}],
]


def _fail(msg):
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    raise AssertionError(msg)


def _validate_messages(messages, call_index):
    if not messages:
        _fail(f"[call {call_index}] messages가 비어있음")
    if messages[0].get("role") != "system":
        _fail(f"[call {call_index}] 첫 메시지가 system이 아님: {messages[0].get('role')}")
    if len(messages) > 1 and messages[1].get("role") != "user":
        _fail(f"[call {call_index}] 두 번째 메시지가 user가 아님: {messages[1].get('role')}")

    pending_tool_ids = set()
    for i, m in enumerate(messages):
        role = m.get("role")
        if role is None:
            _fail(f"[call {call_index}] 메시지[{i}]에 role 키가 없음: {m}")
        if role == "tool":
            if "tool_call_id" not in m or not isinstance(m["tool_call_id"], str):
                _fail(f"[call {call_index}] tool 메시지[{i}]에 tool_call_id(str) 없음: {m}")
            if "content" not in m or not isinstance(m["content"], str):
                _fail(f"[call {call_index}] tool 메시지[{i}]에 content(str) 없음: {m}")
            if m["tool_call_id"] not in pending_tool_ids:
                _fail(f"[call {call_index}] 메시지[{i}]의 tool_call_id={m['tool_call_id']!r}가 "
                      f"직전 assistant tool_calls와 짝이 맞지 않음 (고아 tool 메시지)")
            pending_tool_ids.discard(m["tool_call_id"])
        elif role == "assistant":
            # 새 assistant 턴이 시작되면 이전 턴의 tool 응답은 모두 채워져 있어야 함
            if pending_tool_ids:
                _fail(f"[call {call_index}] 메시지[{i}] assistant 턴 시작 전 "
                      f"응답 안 된 tool_call_id 남아있음: {pending_tool_ids}")
            for tc in (m.get("tool_calls") or []):
                if "id" not in tc:
                    _fail(f"[call {call_index}] assistant tool_calls에 id 없음: {tc}")
                pending_tool_ids.add(tc["id"])
        elif role in ("system", "user"):
            pass
        else:
            _fail(f"[call {call_index}] 알 수 없는 role: {role}")
    if pending_tool_ids:
        _fail(f"[call {call_index}] 마지막까지 응답 안 된 tool_call_id 남음: {pending_tool_ids}")


class Handler(BaseHTTPRequestHandler):
    server_version = "MockLLM/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock_llm] " + (fmt % args) + "\n")

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception as e:
            self._reply_500(f"잘못된 JSON 요청: {e}")
            return

        with self.server.lock:
            call_index = self.server.call_count
            self.server.call_count += 1

        try:
            _validate_messages(payload.get("messages", []), call_index)
        except AssertionError as e:
            self._reply_500(str(e))
            return

        if call_index >= len(SCENARIO):
            self._reply_500(f"시나리오 범위 초과 (call {call_index}, 시나리오 길이 {len(SCENARIO)})")
            return

        calls = SCENARIO[call_index]
        tool_calls = []
        for j, c in enumerate(calls):
            tool_calls.append({
                "id": f"call_{call_index}_{j}",
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                },
            })

        resp = {
            "id": f"chatcmpl-mock-{call_index}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "mock-model"),
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        body_out = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _reply_500(self, message):
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(message + "\n")
        body_out = json.dumps({"error": message}).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)


def serve(port=8098):
    if os.path.exists(ERROR_LOG):
        os.remove(ERROR_LOG)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.lock = threading.Lock()
    server.call_count = 0
    server.serve_forever()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8098
    print(f"[mock_llm] listening on http://127.0.0.1:{port}/v1")
    serve(port)
