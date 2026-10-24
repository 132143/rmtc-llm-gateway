"""网关测试：把协议 §5 每一条要求都拿真实 HTTP 请求验一遍。

跑法：python3 run_tests.py -v
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rmtc_gateway.config import Config, TokenSpec
from rmtc_gateway.server import create_server

OK_PAYLOAD = {
    "id": "chatcmpl-upstream", "object": "chat.completion", "model": "upstream-model",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "上游的回答"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


class FakeUpstream:
    """可控制延迟 / 状态码的假上游。"""

    def __init__(self, delay: float = 0.0, status: int = 200, payload=None):
        self.delay = delay
        self.status = status
        self.payload = payload if payload is not None else OK_PAYLOAD
        self.requests: list = []
        self.httpd = None
        self.url = ""

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    outer.requests.append(json.loads(raw or b"{}"))
                except json.JSONDecodeError:
                    outer.requests.append({})
                if outer.delay:
                    time.sleep(outer.delay)
                data = json.dumps(outer.payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/chat/completions"
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class GatewayCase(unittest.TestCase):
    def start(self, tokens=None, **overrides):
        cfg = Config(host="127.0.0.1", port=0,
                     tokens=tokens or {"practice-A": TokenSpec("A", 100)}, **overrides)
        httpd, state = create_server(cfg)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.state = state
        self.cfg = cfg
        return self.base

    # ---------- 请求工具 ----------
    def post(self, body, token="practice-A", path="/v1/chat/completions", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        merged = {"Content-Type": "application/json"}
        if token is not None:
            merged["Authorization"] = "Bearer " + token
        merged.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data,
                                        headers=merged, method="POST")
        return self._open(request)

    def get(self, path="/healthz"):
        return self._open(urllib.request.Request(self.base + path, method="GET"))

    @staticmethod
    def _open(request):
        # 上一个用例的服务器线程收尾时偶尔会把新连接掐掉（Windows 上遇到过），重试一次即可
        last_error = None
        for _ in range(2):
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    raw = response.read()
                    return response.status, json.loads(raw or b"{}")
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    return exc.code, json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return exc.code, {}
            except (urllib.error.URLError, ConnectionError) as exc:
                last_error = exc
                time.sleep(0.1)
        raise last_error

    def raw(self, head: str, body: bytes = b"") -> tuple:
        """直接开 socket，用来构造 urllib 不肯发的畸形请求。"""
        host, port = self.base.replace("http://", "").split(":")
        sock = socket.create_connection((host, int(port)), timeout=10)
        try:
            sock.sendall(head.encode("ascii") + body)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        data = b"".join(chunks)
        headers_blob, _, payload = data.partition(b"\r\n\r\n")
        status = int(headers_blob.split(b" ")[1])
        try:
            return status, json.loads(payload or b"{}")
        except json.JSONDecodeError:
            return status, {}

    @staticmethod
    def chat(text="你好"):
        return {"messages": [{"role": "user", "content": text}]}


class TestHealthAndAuth(GatewayCase):
    def test_healthz_ok_without_token(self):
        self.start()
        status, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["upstream"], "mock")

    def test_healthz_does_not_expose_quota_usage(self):
        self.start()
        _, body = self.get()
        blob = json.dumps(body)
        self.assertNotIn("remaining", blob)
        self.assertNotIn("used", blob)

    def test_missing_token(self):
        self.start()
        status, body = self.post(self.chat(), token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "missing_token")
        self.assertEqual(body["error"]["type"], "authentication_error")

    def test_malformed_authorization_is_missing_token(self):
        self.start()
        status, body = self.post(self.chat(), token=None,
                                 headers={"Authorization": "Basic abc"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "missing_token")

    def test_invalid_token(self):
        self.start()
        status, body = self.post(self.chat(), token="not-issued")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "invalid_token")

    def test_unknown_path(self):
        self.start()
        status, body = self.get("/v1/models")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        status, body = self.post(self.chat(), path="/v1/completions")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


class TestRequestShape(GatewayCase):
    def setUp(self):
        self.start()

    def test_invalid_json(self):
        status, body = self.post(b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_empty_messages(self):
        status, body = self.post({"messages": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_messages")

    def test_missing_messages(self):
        status, body = self.post({"model": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_messages")

    def test_stream_true_rejected(self):
        status, body = self.post({"messages": [{"role": "user", "content": "hi"}],
                                  "stream": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "stream_unsupported")

    def test_transfer_encoding_rejected(self):
        status, body = self.raw(
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: t\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"0\r\n\r\n")
        self.assertEqual(status, 501)
        self.assertEqual(body["error"]["code"], "unsupported_transfer_encoding")

    def test_missing_content_length(self):
        status, body = self.raw(
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: t\r\nAuthorization: Bearer practice-A\r\n"
            "Content-Type: application/json\r\nConnection: close\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_content_length")

    def test_duplicate_content_length(self):
        status, body = self.raw(
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: t\r\nAuthorization: Bearer practice-A\r\n"
            "Content-Length: 2\r\nContent-Length: 2\r\n\r\n",
            b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_content_length")

    def test_payload_too_large(self):
        status, body = self.raw(
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: t\r\nAuthorization: Bearer practice-A\r\n"
            "Content-Length: 2097153\r\n\r\n")
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "payload_too_large")

    def test_incomplete_body(self):
        status, body = self.raw(
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: t\r\nAuthorization: Bearer practice-A\r\n"
            "Content-Length: 100\r\n\r\n",
            b"{\"a\":")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "incomplete_body")


class TestSuccessAndQuota(GatewayCase):
    def test_mock_success_shape(self):
        self.start()
        status, body = self.post(self.chat())
        self.assertEqual(status, 200)
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "mock-llm")
        content = body["choices"][0]["message"]["content"]
        self.assertIn("mock", content.lower())
        self.assertEqual(self.state.tokens["practice-A"].used, 1)

    def test_quota_exhaustion(self):
        self.start(tokens={"practice-A": TokenSpec("A", 2)})
        self.assertEqual(self.post(self.chat())[0], 200)
        self.assertEqual(self.post(self.chat())[0], 200)
        status, body = self.post(self.chat())
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "quota_exceeded")
        self.assertEqual(body["error"]["type"], "insufficient_quota")

    def test_rejected_request_does_not_consume_quota(self):
        self.start(tokens={"practice-A": TokenSpec("A", 1)})
        self.assertEqual(self.post(b"nope")[0], 400)
        self.assertEqual(self.state.tokens["practice-A"].used, 0)
        self.assertEqual(self.post(self.chat())[0], 200)
        self.assertEqual(self.post(self.chat())[1]["error"]["code"], "quota_exceeded")

    def test_quota_zero_means_unlimited(self):
        self.start(tokens={"practice-A": TokenSpec("A", 0)})
        for _ in range(4):
            self.assertEqual(self.post(self.chat())[0], 200)

    def test_rate_limit_per_ip(self):
        self.start(ip_limit=2, ip_window_s=10.0)
        self.assertEqual(self.post(self.chat())[0], 200)
        self.assertEqual(self.post(self.chat())[0], 200)
        status, body = self.post(self.chat())
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "rate_limit")

    def test_concurrency_limit(self):
        with FakeUpstream(delay=0.5) as upstream:
            self.start(upstream_url=upstream.url, max_inflight_per_token=1)
            results = {}

            def first():
                results["first"] = self.post(self.chat())

            thread = threading.Thread(target=first)
            thread.start()
            time.sleep(0.3)   # 等第一个请求真的进入在途状态
            results["second"] = self.post(self.chat())
            thread.join(5)
            self.assertEqual(results["second"][0], 429)
            self.assertEqual(results["second"][1]["error"]["code"], "concurrency_limit")
            self.assertEqual(results["first"][0], 200)


class TestUpstream(GatewayCase):
    def test_http_upstream_forwards_and_defaults_model(self):
        with FakeUpstream() as upstream:
            self.start(upstream_url=upstream.url, default_model="demo-model")
            status, body = self.post(self.chat())
            self.assertEqual(status, 200)
            self.assertEqual(body["choices"][0]["message"]["content"], "上游的回答")
            self.assertEqual(len(upstream.requests), 1)
            forwarded = upstream.requests[0]
            self.assertEqual(forwarded["model"], "demo-model")
            self.assertNotIn("stream", forwarded)

    def test_http_upstream_keeps_explicit_model(self):
        with FakeUpstream() as upstream:
            self.start(upstream_url=upstream.url)
            self.post({"model": "my-model", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(upstream.requests[0]["model"], "my-model")

    def test_upstream_unreachable_is_502(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.start(upstream_url=f"http://127.0.0.1:{port}/v1/chat/completions")
        status, body = self.post(self.chat())
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "upstream_unavailable")

    def test_upstream_timeout_is_504_and_free(self):
        with FakeUpstream(delay=1.0) as upstream:
            self.start(upstream_url=upstream.url, upstream_timeout_s=0.2)
            status, body = self.post(self.chat())
            self.assertEqual(status, 504)
            self.assertEqual(body["error"]["code"], "upstream_timeout")
            # 响应是先写回给客户端的，记账清理在 finally 里——给服务端线程一点时间收尾
            for _ in range(50):
                if (self.state.tokens["practice-A"].pending == 0
                        and self.state.tokens["practice-A"].inflight == 0):
                    break
                time.sleep(0.02)
            self.assertEqual(self.state.tokens["practice-A"].used, 0)
            self.assertEqual(self.state.tokens["practice-A"].pending, 0)
            self.assertEqual(self.state.tokens["practice-A"].inflight, 0)

    def test_upstream_error_status_is_502(self):
        with FakeUpstream(status=400) as upstream:
            self.start(upstream_url=upstream.url)
            status, body = self.post(self.chat())
            self.assertEqual(status, 502)
            self.assertEqual(body["error"]["code"], "upstream_unavailable")


class TestAudit(GatewayCase):
    def test_audit_records_upstream_usage(self):
        with FakeUpstream() as upstream:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                f"audit-usage-{os.getpid()}-{time.time_ns()}.jsonl")
            self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
            self.start(upstream_url=upstream.url, audit_path=path)
            self.post(self.chat())
            for _ in range(100):
                if os.path.exists(path) and "completion_tokens" in open(path, encoding="utf-8").read():
                    break
                time.sleep(0.02)
            records = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
            self.assertEqual(records[0]["prompt_tokens"], 3)
            self.assertEqual(records[0]["completion_tokens"], 2)
            self.assertEqual(records[0]["model"], "upstream-model")
            self.assertEqual(self.state.stats()["tokens"]["completion"], 2)

    def test_audit_records_seat_but_never_token(self):
        # 用带时间戳的唯一文件名：曾经因为在全量跑时复用了同一个 audit-test.jsonl 而偶发失败
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"audit-test-{os.getpid()}-{time.time_ns()}.jsonl")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.start(audit_path=path)
        self.post(self.chat())
        self.post(self.chat(), token="bogus")
        # 审计记录在响应之后才落盘（服务端在 finally 里写），所以要等它写完再断言
        for _ in range(100):
            if os.path.exists(path) and len([l for l in open(path, encoding="utf-8") if l.strip()]) >= 2:
                break
            time.sleep(0.02)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("practice-A", text)
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["status"], 200)
        self.assertEqual(lines[0]["seat"], "A")
        self.assertEqual(lines[1]["status"], 401)
        self.assertEqual(lines[1]["code"], "invalid_token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
