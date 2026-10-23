"""错误响应。

协议 §5「响应与错误」给定了 HTTP 状态码 + type + code 三元组，这里逐条对应，
保证参赛程序能只按 code 做判断（协议明说：错误提示文字不作为程序判断依据）。
"""

from __future__ import annotations

import json

# HTTP 状态码 -> 默认 error.type（与协议表格一致）
_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    404: "invalid_request_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    501: "invalid_request_error",
    502: "upstream_error",
    503: "server_error",
    504: "upstream_error",
}


class GatewayError(Exception):
    """网关自身拒绝的请求（不会打到上游）。"""

    def __init__(self, status: int, code: str, message: str, type_: str | None = None,
                 param: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.type = type_ or _TYPE_BY_STATUS.get(status, "invalid_request_error")
        self.param = param

    def to_dict(self) -> dict:
        return {"error": {"message": self.message, "type": self.type,
                          "param": self.param, "code": self.code}}

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False).encode("utf-8")


# ---- 401 ----
def missing_token() -> GatewayError:
    return GatewayError(401, "missing_token", "Missing bearer token")


def invalid_token() -> GatewayError:
    return GatewayError(401, "invalid_token", "Unknown or revoked bearer token")


# ---- 404 ----
def not_found() -> GatewayError:
    return GatewayError(404, "not_found", "Request path not recognised by this gateway")


# ---- 400 ----
def invalid_content_length() -> GatewayError:
    return GatewayError(400, "invalid_content_length",
                        "Content-Length missing, duplicated or not a number")


def incomplete_body() -> GatewayError:
    return GatewayError(400, "incomplete_body", "Request body shorter than Content-Length")


def invalid_json() -> GatewayError:
    return GatewayError(400, "invalid_json", "Request body is not valid JSON")


def invalid_messages() -> GatewayError:
    return GatewayError(400, "invalid_messages", "messages must be a non-empty array")


def stream_unsupported() -> GatewayError:
    return GatewayError(400, "stream_unsupported",
                        "stream must be omitted or false; this gateway buffers the response")


# ---- 413 / 501 ----
def payload_too_large() -> GatewayError:
    return GatewayError(413, "payload_too_large", "Request body exceeds 2097152 bytes")


def unsupported_transfer_encoding() -> GatewayError:
    return GatewayError(501, "unsupported_transfer_encoding",
                        "Transfer-Encoding is not supported; send Content-Length")


# ---- 429 ----
def quota_exceeded() -> GatewayError:
    return GatewayError(429, "quota_exceeded", "Seat token quota exhausted",
                        type_="insufficient_quota")


def rate_limit() -> GatewayError:
    return GatewayError(429, "rate_limit", "Too many POST requests from this IP",
                        type_="rate_limit_error")


def concurrency_limit() -> GatewayError:
    return GatewayError(429, "concurrency_limit",
                        "Too many in-flight calls for this seat token",
                        type_="rate_limit_error")


# ---- 5xx ----
def audit_unavailable() -> GatewayError:
    return GatewayError(503, "audit_unavailable",
                        "Gateway cannot confirm this call was recorded")


def upstream_unavailable() -> GatewayError:
    return GatewayError(502, "upstream_unavailable", "Cannot reach upstream model service")


def upstream_timeout() -> GatewayError:
    return GatewayError(504, "upstream_timeout", "Upstream model service timed out")
