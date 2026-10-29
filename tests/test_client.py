"""客户端测试：任何失败都不能抛异常、不能卡住调用方；错误码要正确降级。"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rmtc_client.event_understanding import EventUnderstanding
from rmtc_client.json_extract import extract_first_json_object
from rmtc_client.llm_client import ClientConfig, LlmGatewayClient


def completion(text: str) -> dict:
    return {"id": "chatcmpl-test", "object": "chat.completion", "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}}


def error_payload(code: str, status: int) -> dict:
    types = {401: "authentication_error", 429: "rate_limit_error",
             400: "invalid_request_error", 413: "invalid_request_error"}
    return {"error": {"message": code, "type": types.get(status, "server_error"),
                      "param": None, "code": code}}


class FakeGateway:
    def __init__(self, script=None):
        self.script = list(script or [])
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
                body = json.loads(raw or b"{}")
                outer.requests.append({"headers": dict(self.headers), "body": body})
                item = outer.script.pop(0) if outer.script else {
                    "status": 200, "payload": completion("默认回答")}
                if item.get("delay"):
                    time.sleep(item["delay"])
                if "raw" in item:
                    data = item["raw"]
                else:
                    data = json.dumps(item["payload"], ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(item["status"])
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


def make_client(url: str, **overrides) -> LlmGatewayClient:
    base = dict(url=url, token="practice-A", timeout_s=1.0,
                min_interval_s=0.0, backoff_s=0.05, max_calls=0, max_tokens=128)
    base.update(overrides)
    return LlmGatewayClient(ClientConfig(**base))


class TestTokenHygiene(unittest.TestCase):
    """令牌卫生：默认值必须为空、缺失必须立刻报错。

    历史事故：客户端默认 token='practice-A'、bot_node 又用 f"practice-{seat}" 兜底，
    现场忘设令牌时程序照跑，全程 401 → 本场无真实 AI 调用 → 任务分 0（规则 §10.2）。
    """

    def test_default_config_has_no_token(self):
        self.assertEqual(ClientConfig().token, "")

    def test_missing_token_raises_with_actionable_message(self):
        with self.assertRaises(ValueError) as ctx:
            LlmGatewayClient(ClientConfig(url="http://127.0.0.1:9/v1/chat/completions"))
        self.assertIn("RMTC_TEAM_TOKEN", str(ctx.exception))

    def test_missing_url_raises(self):
        with self.assertRaises(ValueError) as ctx:
            LlmGatewayClient(ClientConfig(token="t"))
        self.assertIn("RMTC_AI_URL", str(ctx.exception))

    def test_bot_node_has_no_default_token_or_url(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "contestant", "bot_node.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("RMTC_TEAM_TOKEN", source)
        self.assertIn("RMTC_AI_URL", source)
        self.assertNotIn('"practice-', source)      # 不许任何默认令牌
        self.assertNotIn('practice-' + '{seat}', source)
        self.assertNotIn("127.0.0.1:8080", source)  # 不许把练习网关当默认值


class TestGatewayPayload(unittest.TestCase):
    """请求体必须与官方口径一致（2026-10-28 答疑）。

    官方三条硬约束：
      1. 必须用统一默认模型 → 选手**不要**填 model（严禁私自调用别的模型）；
      2. 思考模式默认开启 → 要关必须在顶层发 thinking={"type":"disabled"}（关闭后实测 0.78–0.99 s）；
      3. max_tokens 建议 512–1024（我们原来 200，会把 avoid_cells 列表截断）。
    """

    def test_payload_shape(self):
        with FakeGateway() as gateway:
            # 用**默认配置**（不覆盖 max_tokens），验证出厂口径就是 512
            client = LlmGatewayClient(ClientConfig(url=gateway.url, token="practice-A",
                                                   timeout_s=2.0, min_interval_s=0.0))
            result = client.ask("请只输出 JSON：{\"ok\":true}")
            self.assertTrue(result.ok, result)
            body = gateway.requests[0]["body"]
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertEqual(body["max_tokens"], 512)
            self.assertNotIn("model", body)          # 不填 model：用官方默认
            self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_thinking_rejected_falls_back_once(self):
        with FakeGateway(script=[{"status": 400, "payload": error_payload(
                "unknown field: thinking", 400)}]) as gateway:
            client = make_client(gateway.url)
            result = client.ask("请只输出 JSON：{\"ok\":true}")
            self.assertTrue(result.ok, result)
            self.assertEqual(len(gateway.requests), 2)
            self.assertIn("thinking", gateway.requests[0]["body"])
            self.assertNotIn("thinking", gateway.requests[1]["body"])   # 回退后不再发
            self.assertEqual(client.thinking_mode, "default")

    def test_url_is_normalized(self):
        """现场可能给基址 / 基址+v1 / 完整地址三种写法，都要能用。"""
        from rmtc_client.llm_client import _normalize_chat_url
        self.assertEqual(_normalize_chat_url("http://h:8080"),
                         "http://h:8080/v1/chat/completions")
        self.assertEqual(_normalize_chat_url("http://h:8080/v1"),
                         "http://h:8080/v1/chat/completions")
        self.assertEqual(_normalize_chat_url("http://h:8080/v1/chat/completions/"),
                         "http://h:8080/v1/chat/completions")
        self.assertEqual(_normalize_chat_url("https://api.x.com/v1/chat/completions"),
                         "https://api.x.com/v1/chat/completions")

    def test_unknown_400_degrades_fields_then_succeeds(self):
        """万全：上游 400 但没说哪个字段 → 先丢 thinking，再丢 json_object，最后成功。"""
        with FakeGateway(script=[
                {"status": 400, "payload": error_payload("bad request", 400)},
                {"status": 200, "payload": completion("{\"ok\":true}")}]) as gateway:
            client = make_client(gateway.url)
            result = client.ask("请只输出 JSON：{\"ok\":true}")
            self.assertTrue(result.ok, result)
            self.assertEqual(len(gateway.requests), 2)
            self.assertIn("thinking", gateway.requests[0]["body"])
            self.assertNotIn("thinking", gateway.requests[1]["body"])

    def test_env_can_keep_thinking_on(self):
        with FakeGateway() as gateway:
            client = make_client(gateway.url, disable_thinking=False,
                                 model="deepseek-flash")
            self.assertTrue(client.ask("json").ok)
            body = gateway.requests[0]["body"]
            self.assertNotIn("thinking", body)
            self.assertEqual(body["model"], "deepseek-flash")           # 只在显式配置时才发


class TestClientCalls(unittest.TestCase):
    def test_success(self):
        with FakeGateway() as gateway:
            client = make_client(gateway.url)
            result = client.ask("你好")
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "默认回答")
            self.assertEqual(client.ok_calls, 1)
            request = gateway.requests[0]
            self.assertEqual(request["headers"].get("Authorization"), "Bearer practice-A")
            self.assertNotIn("stream", request["body"])
            self.assertEqual(request["body"]["max_tokens"], 128)
            self.assertEqual(request["body"]["messages"][0]["role"], "user")
            self.assertEqual(request["body"]["response_format"], {"type": "json_object"})

    def test_timeout_is_a_value_not_an_exception(self):
        with FakeGateway([{"status": 200, "payload": completion("慢"), "delay": 0.5}]) as gateway:
            client = make_client(gateway.url, timeout_s=0.15)
            started = time.perf_counter()
            result = client.ask("你好")
            elapsed = time.perf_counter() - started
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "local_timeout")
            self.assertLess(elapsed, 0.8)   # 只验证"没有等满 0.5s"，留出机器负载的余量
            self.assertTrue(client.healthy)

    def test_quota_exceeded_degrades_permanently(self):
        with FakeGateway([{"status": 429, "payload": error_payload("quota_exceeded", 429)}]) as gateway:
            client = make_client(gateway.url)
            first = client.ask("你好")
            self.assertFalse(first.ok)
            self.assertEqual(first.code, "quota_exceeded")
            self.assertFalse(client.healthy)
            self.assertEqual(client.degraded_reason, "quota_exceeded")
            second = client.ask("再来一次")
            self.assertEqual(second.code, "local_skipped")
            self.assertEqual(len(gateway.requests), 1)

    def test_invalid_token_degrades(self):
        with FakeGateway([{"status": 401, "payload": error_payload("invalid_token", 401)}]) as gateway:
            client = make_client(gateway.url)
            result = client.ask("你好")
            self.assertEqual(result.code, "invalid_token")
            self.assertFalse(client.healthy)

    def test_rate_limit_backs_off_then_recovers(self):
        with FakeGateway([
            {"status": 429, "payload": error_payload("rate_limit", 429)},
            {"status": 200, "payload": completion("恢复了")},
        ]) as gateway:
            client = make_client(gateway.url, backoff_s=0.05)
            first = client.ask("你好")
            self.assertEqual(first.code, "rate_limit")
            self.assertTrue(client.healthy)
            self.assertEqual(client.ask("马上再来").code, "local_skipped")
            time.sleep(0.15)
            second = client.ask("再来")
            self.assertTrue(second.ok)
            self.assertEqual(second.text, "恢复了")

    def test_local_budget(self):
        with FakeGateway() as gateway:
            client = make_client(gateway.url, max_calls=1)
            self.assertTrue(client.ask("第一次").ok)
            result = client.ask("第二次")
            self.assertEqual(result.code, "local_skipped")
            self.assertEqual(len(gateway.requests), 1)

    def test_broken_response_is_handled(self):
        with FakeGateway([{"status": 200, "raw": b"<html>oops</html>"}]) as gateway:
            client = make_client(gateway.url)
            result = client.ask("你好")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "bad_response")

    def test_budget_counts_in_flight_requests(self):
        """真机踩到的坑：预算 60 却打了 63 次 —— 多线程同时通过检查。现在在途也占预算。"""
        import threading
        with FakeGateway([{"status": 200, "payload": completion("慢"), "delay": 0.2} for _ in range(6)]) as gateway:
            client = make_client(gateway.url, max_calls=2, timeout_s=2.0)
            results = []

            def worker():
                results.append(client.ask("并发"))

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            self.assertLessEqual(len(gateway.requests), 2, "并发下请求数不能超过预算")
            self.assertLessEqual(client.ok_calls, 2)

    def test_unreachable_gateway_is_handled(self):
        client = make_client("http://127.0.0.1:1/v1/chat/completions", timeout_s=0.5)
        result = client.ask("你好")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "local_timeout")


class TestEventUnderstanding(unittest.TestCase):
    def test_hint_from_fenced_json(self):
        content = ('好的，这是我的判断：\n```json\n'
                   '{"threat_zone": [[4,4],[9,9]], "affects_me": true, '
                   '"advice": "向右上撤离", "urgency_ticks": 25}\n```')
        with FakeGateway([{"status": 200, "payload": completion(content)}]) as gateway:
            client = make_client(gateway.url)
            understanding = EventUnderstanding(client, max_age_ticks=20, min_repeat_s=0.0)
            self.assertTrue(understanding.offer("match-1", 50, "25 Tick 后 [4,4]-[9,9] 爆炸",
                                                mission_text="捡两个普通资源",
                                                position=(8, 8), map_size=(24, 24), seat="A"))
            hint = self._wait(understanding, "match-1", 50)
            self.assertIsNotNone(hint)
            self.assertEqual(hint["advice"], "向右上撤离")
            self.assertEqual(hint["parsed"]["urgency_ticks"], 25)

    def test_stale_hint_is_dropped(self):
        with FakeGateway([{"status": 200, "payload": completion('{"advice": "快跑"}')}]) as gateway:
            client = make_client(gateway.url)
            understanding = EventUnderstanding(client, max_age_ticks=5, min_repeat_s=0.0)
            understanding.offer("match-1", 10, "爆炸预告")
            self.assertIsNotNone(self._wait(understanding, "match-1", 10))
            self.assertIsNone(understanding.hint("match-1", 10 + 6))

    def test_new_session_clears_old_hint(self):
        with FakeGateway([{"status": 200, "payload": completion('{"advice": "旧场次建议"}')}]) as gateway:
            client = make_client(gateway.url)
            understanding = EventUnderstanding(client, max_age_ticks=100, min_repeat_s=0.0)
            understanding.offer("old-match", 10, "旧公告")
            self.assertIsNotNone(self._wait(understanding, "old-match", 10))
            understanding.offer("new-match", 1, "新公告")
            self.assertIsNone(understanding.hint("old-match", 10))

    def test_same_event_not_asked_twice(self):
        with FakeGateway([{"status": 200, "payload": completion('{"advice": "一次就够了"}')}]) as gateway:
            client = make_client(gateway.url)
            understanding = EventUnderstanding(client, max_age_ticks=100, min_repeat_s=30.0)
            self.assertTrue(understanding.offer("m", 10, "同一条公告"))
            self.assertFalse(understanding.offer("m", 12, "同一条公告"))
            deadline = time.time() + 2.0
            while not gateway.requests and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)   # 再等一会儿，确认第二次 offer 真的没有发请求
            self.assertEqual(len(gateway.requests), 1)

    def test_failed_call_keeps_local_strategy(self):
        with FakeGateway([{"status": 429, "payload": error_payload("quota_exceeded", 429)}]) as gateway:
            client = make_client(gateway.url)
            understanding = EventUnderstanding(client, min_repeat_s=0.0)
            understanding.offer("m", 10, "公告")
            time.sleep(0.2)
            self.assertIsNone(understanding.hint("m", 10))
            self.assertEqual(understanding.finished, 1)

    @staticmethod
    def _wait(understanding, session_id, tick, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            hint = understanding.hint(session_id, tick)
            if hint is not None:
                return hint
            time.sleep(0.02)
        return None


class TestJsonRepair(unittest.TestCase):
    """借 outputguard 的修复策略：模型吐坏 JSON 时还能救回来。"""

    def test_single_quotes(self):
        self.assertEqual(extract_first_json_object("{'a': 1}"), {"a": 1})

    def test_trailing_comma(self):
        self.assertEqual(extract_first_json_object('{"a": 1, "b": [1, 2,],}'), {"a": 1, "b": [1, 2]})

    def test_comments(self):
        text = '{"a": 1, // 注释\n "b": 2 /* 块注释 */}'
        self.assertEqual(extract_first_json_object(text), {"a": 1, "b": 2})

    def test_python_literals(self):
        self.assertEqual(extract_first_json_object('{"a": True, "b": None}'), {"a": True, "b": None})

    def test_truncated_is_repaired(self):
        # 模型被 max_tokens 截断
        self.assertEqual(extract_first_json_object('{"threat_zone": [[8,3],[12,7]], "direction_reversed": true'),
                         {"threat_zone": [[8, 3], [12, 7]], "direction_reversed": True})

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_first_json_object("我无法判断"))
        self.assertIsNone(extract_first_json_object("{a: b: c}"))


class TestJsonExtract(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_first_json_object('{"a": 1}'), {"a": 1})

    def test_with_prefix_and_suffix(self):
        text = '好的：{"a": 1} 以上是我的判断。'
        self.assertEqual(extract_first_json_object(text), {"a": 1})

    def test_braces_inside_string(self):
        text = '{"msg": "区域 [4,4] 有 {危险}", "n": 2}'
        self.assertEqual(extract_first_json_object(text), {"msg": "区域 [4,4] 有 {危险}", "n": 2})

    def test_code_fence(self):
        text = '```json\n{"ok": true}\n```'
        self.assertEqual(extract_first_json_object(text), {"ok": True})

    def test_no_json(self):
        self.assertIsNone(extract_first_json_object("我无法判断"))
        self.assertIsNone(extract_first_json_object(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
