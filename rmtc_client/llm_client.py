"""OpenAI 兼容网关客户端。

只做四件事，全都围绕"不阻塞动作窗"：
 1. 请求（带 Content-Type / Authorization，永不设 stream）；
 2. 超时（默认 1.5s）与异常兜底 —— 任何失败都返回 LlmResult(ok=False)，不抛异常；
 3. 按错误码降级：quota_exceeded / invalid_token 直接停用；rate_limit、503、5xx 退避重试；
 4. 预算与冷却：本地先挡一遍，省额度也省动作窗。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# 协议里需要特殊处理的错误码
_FATAL_CODES = {"quota_exceeded", "invalid_token", "missing_token"}
_BACKOFF_CODES = {"rate_limit", "concurrency_limit", "audit_unavailable",
                  "upstream_unavailable", "upstream_timeout"}


@dataclass
class ClientConfig:
    url: str = "http://127.0.0.1:8080/v1/chat/completions"
    token: str = "practice-A"
    # 真实模型（DeepSeek）实测：中位 0.9s、p90 1.1s、最大 1.4s。
    # 500 ms/Tick 的节奏下 2.5 s 只占 5 个 Tick，且调用是异步的，不会堵动作窗。
    timeout_s: float = 2.5
    min_interval_s: float = 2.0   # 两次调用之间的最小间隔
    # 本地预算：规则 §11.3「暂定每席位令牌成功调用上限 100」。
    # 留出余量（心跳+事件都在里面），默认 60，别把额度一次性烧光。
    max_calls: int = 60
    backoff_s: float = 1.0
    model: str = ""
    max_tokens: int = 200


@dataclass
class LlmResult:
    ok: bool
    text: str = ""
    code: str = ""
    status: int = 0
    latency_ms: float = 0.0
    message: str = ""

    @property
    def is_fatal(self) -> bool:
        return self.code in _FATAL_CODES

    def __str__(self) -> str:
        if self.ok:
            return f"ok({self.latency_ms:.0f}ms) {self.text[:80]!r}"
        return f"fail[{self.code or self.status}] {self.message[:80]}"


@dataclass
class PendingCall:
    """一次后台调用。用 poll() 取结果，拿不到就当没有。"""

    prompt: str
    session_id: str = ""
    tick: int = -1
    started_at: float = field(default_factory=time.monotonic)
    result: LlmResult | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _thread: threading.Thread | None = None

    def done(self) -> bool:
        with self._lock:
            return self.result is not None

    def poll(self) -> LlmResult | None:
        with self._lock:
            return self.result


class LlmGatewayClient:
    def __init__(self, cfg: ClientConfig | None = None, logger=None):
        self.cfg = cfg or ClientConfig()
        self._logger = logger
        self._lock = threading.Lock()
        self._degraded: str = ""          # 非空表示已停用
        self._next_allowed = 0.0
        self._attempts = 0
        self._ok_calls = 0
        self._last_code = ""

    # ---------- 对外状态 ----------
    @property
    def healthy(self) -> bool:
        with self._lock:
            return not self._degraded

    @property
    def degraded_reason(self) -> str:
        with self._lock:
            return self._degraded

    @property
    def ok_calls(self) -> int:
        with self._lock:
            return self._ok_calls

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    def stats(self) -> dict:
        with self._lock:
            return {"attempts": self._attempts, "ok_calls": self._ok_calls,
                    "degraded": self._degraded, "last_code": self._last_code}

    def _log(self, level: str, text: str) -> None:
        if self._logger is not None:
            try:
                self._logger(level, text)
            except Exception:
                pass

    # ---------- 何时可以调用 ----------
    def _allow(self) -> tuple[bool, str]:
        with self._lock:
            if self._degraded:
                return False, self._degraded
            if self.cfg.max_calls and self._ok_calls >= self.cfg.max_calls:
                return False, "budget"
            if time.monotonic() < self._next_allowed:
                return False, "cooldown"
            return True, ""

    # ---------- 主调用 ----------
    def ask(self, prompt: str, messages: list | None = None,
            timeout_s: float | None = None, max_tokens: int | None = None) -> LlmResult:
        allowed, reason = self._allow()
        if not allowed:
            return LlmResult(ok=False, code="local_skipped", message=f"skip: {reason}")

        with self._lock:
            # 立刻占住冷却位，避免同 Tick 里多个线程一起打网关
            self._next_allowed = time.monotonic() + self.cfg.min_interval_s
            self._attempts += 1

        payload = {
            "messages": messages or [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens if max_tokens is not None else self.cfg.max_tokens,
        }
        if self.cfg.model:
            payload["model"] = self.cfg.model

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.cfg.url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.cfg.token})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout_s or self.cfg.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            return self._on_http_error(exc, started)
        except (TimeoutError, OSError) as exc:
            return self._fail("local_timeout", 0, f"请求失败/超时：{exc}", started)

        result = self._parse_success(raw, started)
        if result.ok:
            with self._lock:
                self._ok_calls += 1
                self._last_code = ""
        return result

    def ask_async(self, prompt: str, session_id: str = "", tick: int = -1,
                  messages: list | None = None) -> PendingCall:
        handle = PendingCall(prompt=prompt, session_id=session_id, tick=tick)

        def worker():
            outcome = self.ask(prompt, messages=messages)
            with handle._lock:
                handle.result = outcome

        thread = threading.Thread(target=worker, name="rmtc-llm", daemon=True)
        handle._thread = thread
        thread.start()
        return handle

    # ---------- 结果处理 ----------
    def _parse_success(self, raw: bytes, started: float) -> LlmResult:
        latency = (time.perf_counter() - started) * 1000.0
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("content 不是字符串")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return self._fail("bad_response", 200, f"响应无法解析：{exc}", started)
        return LlmResult(ok=True, text=content, status=200, latency_ms=latency)

    def _on_http_error(self, exc: urllib.error.HTTPError, started: float) -> LlmResult:
        code = ""
        message = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code") or "")
                message = str(error.get("message") or "")
        except Exception:
            pass
        code = code or f"http_{exc.code}"
        if code in _FATAL_CODES:
            with self._lock:
                self._degraded = code
                self._last_code = code
            self._log("error", f"网关返回 {code}，本地 AI 通道已停用（继续用本地策略）")
        elif code in _BACKOFF_CODES:
            with self._lock:
                self._next_allowed = time.monotonic() + self.cfg.backoff_s
                self._last_code = code
        else:
            with self._lock:
                self._last_code = code
        return self._fail(code, exc.code, message, started)

    def _fail(self, code: str, status: int, message: str, started: float) -> LlmResult:
        latency = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self._last_code = code or self._last_code
        return LlmResult(ok=False, code=code, status=status, message=message,
                         latency_ms=latency)
