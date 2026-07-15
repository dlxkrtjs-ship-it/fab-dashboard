"""Agent Browser 로컬 웹 패널 (v1-lite).

stdlib(http.server + threading)만 사용하는 단일 파일 웹 UI.
태스크를 입력해 제출하면 agent.py의 run()을 백그라운드 스레드로 돌리고,
브라우저는 짧은 주기로 폴링해서 진행 로그를 화면에 스트리밍한다.

사용법:
    python panel.py
    # http://localhost:8765 접속

환경변수:
    PANEL_PORT   (기본 8765)
    LLM_BASE_URL / LLM_MODEL / HEADLESS — agent.py와 동일하게 적용됨
    (Ollama든 MLX든, agent.py가 지원하는 OpenAI 호환 백엔드는 그대로 사용 가능)

외부 프레임워크 의존 없음(FastAPI/Flask 등 사용 안 함).
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent

PORT = int(os.environ.get("PANEL_PORT", "8765"))

STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "task": "",
    "logs": [],       # list[str] — 완료된 로그 줄들
    "result": None,   # 완료 시 요약 문자열
    "error": None,    # 패널 레벨에서 잡힌 예외 메시지(있다면)
}


def _append_log(line):
    with STATE_LOCK:
        STATE["logs"].append(line)


def _run_task_in_background(task):
    with STATE_LOCK:
        STATE["running"] = True
        STATE["task"] = task
        STATE["logs"] = [f"[시작] {task}"]
        STATE["result"] = None
        STATE["error"] = None
    try:
        # headless=None -> agent.headless_from_env() (HEADLESS 환경변수, 기본 True)를 그대로 따른다.
        result = agent.run(task, headless=None, on_step=_append_log)
        with STATE_LOCK:
            STATE["result"] = result
    except Exception as e:  # agent.run은 내부에서 대부분의 예외를 잡지만, 방어적으로 한 번 더 감싼다
        _append_log(f"[panel 오류] {e}")
        with STATE_LOCK:
            STATE["error"] = str(e)
    finally:
        with STATE_LOCK:
            STATE["running"] = False


INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Agent Browser Panel</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 780px; margin: 2rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.3rem; }
  form { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
  input[type=text] { flex: 1; padding: 0.5rem; font-size: 1rem; }
  button { padding: 0.5rem 1rem; font-size: 1rem; cursor: pointer; }
  button:disabled { cursor: not-allowed; opacity: 0.5; }
  #status { margin-bottom: 0.5rem; font-size: 0.9rem; color: #555; }
  #status.running { color: #b58900; }
  #status.done { color: #2a9d3f; }
  #status.error { color: #d33; }
  #log { background: #111; color: #d7d7d7; padding: 1rem; border-radius: 6px;
         height: 420px; overflow-y: auto; white-space: pre-wrap; font-family: monospace; font-size: 0.85rem; }
  #result { margin-top: 0.8rem; padding: 0.8rem; background: #eef7ee; border-radius: 6px; display: none; }
</style>
</head>
<body>
  <h1>Agent Browser Panel</h1>
  <form id="taskForm">
    <input type="text" id="taskInput" placeholder="예: hacker news에서 오늘 1위 글 제목 알려줘" autofocus>
    <button type="submit" id="runBtn">실행</button>
  </form>
  <div id="status">대기 중</div>
  <pre id="log"></pre>
  <div id="result"></div>

<script>
let since = 0;
let polling = false;

function setStatus(text, cls) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = cls || '';
}

async function poll() {
  try {
    const res = await fetch('/api/logs?since=' + since);
    const data = await res.json();
    const logEl = document.getElementById('log');
    if (data.logs && data.logs.length) {
      for (const line of data.logs) {
        logEl.textContent += line + "\\n";
      }
      logEl.scrollTop = logEl.scrollHeight;
      since = data.total;
    }
    document.getElementById('runBtn').disabled = data.running;
    if (data.running) {
      setStatus('실행 중...', 'running');
    } else if (data.error) {
      setStatus('오류: ' + data.error, 'error');
    } else if (data.result) {
      setStatus('완료', 'done');
    } else {
      setStatus('대기 중', '');
    }
    const resultEl = document.getElementById('result');
    if (data.result) {
      resultEl.style.display = 'block';
      resultEl.textContent = '결과: ' + data.result;
    } else {
      resultEl.style.display = 'none';
    }
  } catch (e) {
    setStatus('패널 서버와 통신 실패: ' + e, 'error');
  }
  setTimeout(poll, 1000);
}

document.getElementById('taskForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const task = document.getElementById('taskInput').value.trim();
  if (!task) return;
  document.getElementById('log').textContent = '';
  since = 0;
  const res = await fetch('/api/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({task})
  });
  const data = await res.json();
  if (!data.ok) {
    setStatus('실행 실패: ' + data.error, 'error');
  }
});

poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBrowserPanel/0.1"

    def log_message(self, fmt, *args):
        pass  # 콘솔 로그 조용히 (agent 로그와 섞이지 않게)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_html(INDEX_HTML)
            return
        if self.path.startswith("/api/logs"):
            since = 0
            if "?" in self.path:
                qs = self.path.split("?", 1)[1]
                for part in qs.split("&"):
                    if part.startswith("since="):
                        try:
                            since = int(part.split("=", 1)[1])
                        except ValueError:
                            since = 0
            with STATE_LOCK:
                logs = STATE["logs"][since:]
                total = len(STATE["logs"])
                running = STATE["running"]
                result = STATE["result"]
                error = STATE["error"]
            self._send_json({
                "logs": logs, "total": total, "running": running,
                "result": result, "error": error,
            })
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        if self.path == "/api/run":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send_json({"ok": False, "error": "잘못된 요청 본문"}, status=400)
                return
            task = (payload.get("task") or "").strip()
            if not task:
                self._send_json({"ok": False, "error": "task가 비어있음"}, status=400)
                return
            with STATE_LOCK:
                already_running = STATE["running"]
            if already_running:
                self._send_json({"ok": False, "error": "이미 실행 중인 작업이 있음"}, status=409)
                return
            t = threading.Thread(target=_run_task_in_background, args=(task,), daemon=True)
            t.start()
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, status=404)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Agent Browser Panel: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
