#!/usr/bin/env python3
"""agent.py 전체 루프를 mock LLM + 정적 테스트 사이트로 실제 실행해 검증하는 e2e 스크립트.

- tests/test_site/ 를 http.server로 서빙
- tests/mock_llm.py 를 OpenAI 호환 서버로 띄움 (LLM_BASE_URL로 지정)
- HEADLESS=1 로 agent.py를 서브프로세스 실행, "done"까지 도는지 확인
- mock 서버가 기록한 포맷 오류(mock_llm_errors.log)가 없는지, 시나리오를
  끝까지 소비했는지(모든 tool_call 응답) 확인

성공 시 종료코드 0, 실패 시 비0 + 실패 사유 출력.
"""
import http.server
import os
import subprocess
import sys
import threading
import time
import urllib.request

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(TESTS_DIR)
SITE_DIR = os.path.join(TESTS_DIR, "test_site")
ERROR_LOG = os.path.join(TESTS_DIR, "mock_llm_errors.log")

SITE_PORT = 8099
LLM_PORT = 8098


def start_site_server():
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=SITE_DIR, **kw)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", SITE_PORT), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def start_mock_llm():
    sys.path.insert(0, TESTS_DIR)
    import mock_llm
    if os.path.exists(ERROR_LOG):
        os.remove(ERROR_LOG)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", LLM_PORT), mock_llm.Handler)
    server.lock = threading.Lock()
    server.call_count = 0
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, mock_llm


def wait_http(url, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def main():
    print("== 테스트 사이트 서버 시작 ==")
    site = start_site_server()
    print("== mock LLM 서버 시작 ==")
    mock_server, mock_llm = start_mock_llm()

    ok_site = wait_http(f"http://127.0.0.1:{SITE_PORT}/home.html")
    ok_llm = wait_http(f"http://127.0.0.1:{LLM_PORT}/v1/chat/completions") or True  # GET은 404지만 서버는 응답함
    if not ok_site:
        print("실패: 테스트 사이트 서버가 뜨지 않음")
        return 1

    env = dict(os.environ)
    env["LLM_BASE_URL"] = f"http://127.0.0.1:{LLM_PORT}/v1"
    env["LLM_MODEL"] = "mock-model"
    env["HEADLESS"] = "1"

    task = "http://127.0.0.1:8099/home.html 에서 검색 페이지로 이동해 'playwright'를 검색하고 결과를 요약해줘"
    print(f"== agent.py 실행 (task={task!r}) ==")
    proc = subprocess.run(
        [sys.executable, os.path.join(AGENT_DIR, "agent.py"), task],
        cwd=AGENT_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    print("--- agent.py stdout ---")
    print(proc.stdout)
    if proc.stderr.strip():
        print("--- agent.py stderr ---")
        print(proc.stderr)

    failures = []
    if proc.returncode != 0:
        failures.append(f"agent.py 종료코드 비정상: {proc.returncode}")
    if "[결과]" not in proc.stdout:
        failures.append("stdout에 '[결과]' 마커 없음 (done 도달 실패)")
    if "[중단]" in proc.stdout:
        failures.append("stdout에 '[중단]' (최대 스텝 도달) 발생")

    if os.path.exists(ERROR_LOG):
        with open(ERROR_LOG, encoding="utf-8") as f:
            log = f.read().strip()
        if log:
            failures.append(f"mock_llm 포맷 검증 실패:\n{log}")

    expected_calls = len(mock_llm.SCENARIO)
    actual_calls = mock_server.call_count
    if actual_calls != expected_calls:
        failures.append(f"mock LLM 호출 횟수 불일치: 기대 {expected_calls}, 실제 {actual_calls} "
                         "(agent.py가 시나리오를 끝까지 소비하지 못했거나 초과 호출함)")

    print(f"\nmock LLM 호출 횟수: {actual_calls}/{expected_calls}")

    if failures:
        print("\n=== E2E 실패 ===")
        for f_ in failures:
            print(f"- {f_}")
        return 1

    print("\n=== E2E 통과 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
