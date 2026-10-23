"""上游模型服务。

两种模式：
- mock：本地模拟，只证明"链路 + 错误处理"是通的，不做任何语义理解（不是真模型）；
- http：转发到真实 OpenAI 兼容服务（比赛时由主办方提供）。

协议要求：网关不重试上游请求；响应整体缓冲后返回；读超时默认 30 秒。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request


class UpstreamTimeout(Exception):
    """上游超时 -> 504 upstream_timeout。"""


class UpstreamUnavailable(Exception):
    """上游连不上或返回非 2xx -> 502 upstream_unavailable。"""


class UpstreamResult:
    __slots__ = ("body", "content_type", "model")

    def __init__(self, body: bytes, content_type: str, model: str):
        self.body = body
        self.content_type = content_type
        self.model = model


def _completion_payload(model: str, content: str, prompt_chars: int) -> dict:
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, len(content) // 4)
    return {
        "id": "chatcmpl-rmtc-mock",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


class MockUpstream:
    """本地模拟上游。

    刻意只回一句固定说明，不去猜公告内容：协议要求随机事件必须由真实大模型理解，
    用本地模板/关键词"假解析"是违规的，模拟上游不提供那种能力，也不该被拿来用。
    """

    name = "mock"

    def complete(self, payload: dict, model: str) -> UpstreamResult:
        prompt_chars = sum(len(str(m.get("content", "")))
                           for m in payload.get("messages", [])
                           if isinstance(m, dict))
        content = json.dumps(
            {"mock": True,
             "note": "本地模拟上游：只验证网关链路与错误处理，未调用任何真实模型；"
                     "正式比赛必须使用主办方统一网关。"},
            ensure_ascii=False)
        body = json.dumps(_completion_payload(model, content, prompt_chars),
                          ensure_ascii=False).encode("utf-8")
        return UpstreamResult(body, "application/json", model)


class HttpUpstream:
    """转发到真实 OpenAI 兼容服务（比赛时由主办方提供）。"""

    name = "http"

    def __init__(self, url: str, key: str = "", timeout_s: float = 30.0):
        self.url = url
        self.key = key
        self.timeout_s = timeout_s

    def complete(self, payload: dict, model: str) -> UpstreamResult:
        body = dict(payload)
        body["model"] = model
        # 协议：测试代理只支持非流式；网关自己也是整体缓冲后返回。
        body.pop("stream", None)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "application/json")
                return UpstreamResult(raw, content_type, model)
        except urllib.error.HTTPError as exc:
            raise UpstreamUnavailable(f"upstream http {exc.code}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise UpstreamTimeout("upstream read timeout") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise UpstreamTimeout("upstream read timeout") from exc
            raise UpstreamUnavailable(f"upstream unreachable: {reason}") from exc
        except OSError as exc:  # 连接被重置等
            raise UpstreamUnavailable(f"upstream unreachable: {exc}") from exc
