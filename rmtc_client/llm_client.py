"""OpenAI 兼容网关客户端。

只做四件事，全都围绕"不阻塞动作窗"：
 1. 请求（带 Content-Type / Authorization，永不设 stream）；
 2. 超时（默认 1.5s）与异常兜底 —— 任何失败都返回 LlmResult(ok=False)，不抛异常；
 3. 按错误码降级：quota_exceeded / invalid_token 直接停用；rate_limit、503、5xx 退避重试；
 4. 预算与冷却：本地先挡一遍，省额度也省动作窗。
"""

from __future__ import annotations

import json
import random
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
    # 网关地址同样不设默认值：正式比赛必须用主办方地址，练习时显式给本地裁判地址。
    # 留一个"看起来能用"的默认值 = 现场会静默连到练习端口（然后 401/超时）。
    url: str = ""
    # 令牌必须来自现场环境变量（RMTC_TEAM_TOKEN）。默认留空并**快速失败**：
    # 之前默认 practice-A 会导致"忘了设令牌 → 全程 401 → 任务分 0 但程序照跑"，
    # 这是最危险的静默失败（规则 §10.2：本场没有真实 AI 成功调用则 M=0）。
    token: str = ""
    # 真实模型实测延迟：中位 1.0~1.3s，最大 2.7s。超时先设 4s：调用是异步的，
    # 不会堵动作窗；而超时会白吃额度（规则 §11.3），所以宁可多等。
    timeout_s: float = 4.0
    # 两次调用之间的最小间隔。事件窗口只有 10~16 Tick（第二阶段 5~8 秒），
    # 2 秒的间隔会整段错过公告；0.8 秒 + 预算 90 次能在 500 Tick 内保持"每 4~6 Tick 一次"。
    min_interval_s: float = 0.8
    # 本地预算：规则 §11.3「暂定每席位令牌成功调用上限 100」。
    # 默认 90：留 10 次余量（重试/心跳），同时让事件响应足够密（配合 0.8s 间隔）。
    max_calls: int = 90
    backoff_s: float = 1.0
    model: str = ""
    max_tokens: int = 200
    json_mode: bool = True        # 让模型只输出 JSON（减少解析失败）


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
        if not str(getattr(self.cfg, "token", "") or "").strip():
            raise ValueError("缺少队伍令牌：请设置环境变量 RMTC_TEAM_TOKEN 后再启动"
                             "（客户端不会用任何默认令牌，避免静默 401 导致 AI 资格丢失）")
        if not str(getattr(self.cfg, "url", "") or "").strip():
            raise ValueError("缺少网关地址：请设置环境变量 RMTC_AI_URL 后再启动")
        self._logger = logger
        self._lock = threading.Lock()
        self._degraded: str = ""          # 非空表示已停用
        self._next_allowed = 0.0
        self._attempts = 0
        self._ok_calls = 0
        self._in_flight = 0            # 已放行、还没回来的请求
        self._spent = 0                 # 已占用的额度（按发出的请求计，超时也算）
        self._last_code = ""
        self._json_mode_off = False     # 网关不接受 response_format 时置位（见 _on_http_error）

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
                    "in_flight": self._in_flight, "spent": self._spent,
                    "degraded": self._degraded, "last_code": self._last_code}

    def _log(self, level: str, text: str) -> None:
        if self._logger is not None:
            try:
                self._logger(level, text)
            except Exception:
                pass

    # ---------- 何时可以调用 ----------
    def _reserve(self) -> tuple[bool, str]:
        """准入 + 占位。

        规则 §11.3：额度按成功调用累计，超额没有补回。所以预算必须把**在途请求**也算进去，
        否则两个线程同时通过检查就会超额（真机实测过：预算 60 却打了 63 次）。
        """
        with self._lock:
            if self._degraded:
                return False, self._degraded
            # 保守口径：额度按"请求是否发出"占，而不是按"是否成功"。
            # 规则 §11.3 明确：成功响应即消耗额度，客户端超时也不退回——超时同样吃配额。
            if self.cfg.max_calls and self._spent >= self.cfg.max_calls:
                return False, "budget"
            if time.monotonic() < self._next_allowed:
                return False, "cooldown"
            self._in_flight += 1
            self._spent += 1
            self._next_allowed = time.monotonic() + self.cfg.min_interval_s
            self._attempts += 1
            return True, ""

    def _release(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    # ---------- 主调用 ----------
    def ask(self, prompt: str, messages: list | None = None,
            timeout_s: float | None = None, max_tokens: int | None = None) -> LlmResult:
        allowed, reason = self._reserve()
        if not allowed:
            return LlmResult(ok=False, code="local_skipped", message=f"skip: {reason}")

        try:
            result = self._ask_once(prompt, messages, timeout_s, max_tokens)
            if result.code == "json_mode_rejected":
                # 已经关掉 response_format，立刻重试一次（不再占预算名额之外的名额）
                result = self._ask_once(prompt, messages, timeout_s, max_tokens)
            return result
        finally:
            self._release()

    def _ask_once(self, prompt, messages, timeout_s, max_tokens) -> LlmResult:
        payload = {
            "messages": messages or [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens if max_tokens is not None else self.cfg.max_tokens,
        }
        if self.cfg.json_mode and not self._json_mode_off:
            # OpenAI 兼容的 JSON 模式：要求模型只输出 JSON 对象（DeepSeek 支持）。
            # 只是"约束输出格式"，不改变规则要求的事件理解仍由模型完成。
            # 真机踩坑：DeepSeek 对 json_object 要求 prompt 里出现 "json"，否则 400；
            # 网关/模型换代也可能直接不支持这个字段 —— 一旦被拒就本场关掉它（见 _on_http_error）。
            payload["response_format"] = {"type": "json_object"}
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
        # 只针对"这个网关不接受 response_format=json_object"的一种 400：关掉该字段后重试一次。
        # 为什么值得为此多花 1 次配额：AI 资格=任务分 15 分，一次格式被拒不能连带整场归零。
        lowered = message.lower()
        if (not self._json_mode_off and exc.code == 400
                and "json_object" in lowered + str(code) and "response_format" in lowered):
            with self._lock:
                self._json_mode_off = True
                self._last_code = "json_mode_rejected"
            self._log("warn", f"网关拒绝 json_object 输出模式（{message[:80]}），本场改用普通输出")
            return self._fail("json_mode_rejected", exc.code, message, started)
        if code in _FATAL_CODES:
            with self._lock:
                self._degraded = code
                self._last_code = code
            self._log("error", f"网关返回 {code}，本地 AI 通道已停用（继续用本地策略）")
        elif code in _BACKOFF_CODES:
            # 抖动退避（思路同 tenacity）：多队伍共享网关时，固定退避会撞车
            delay = self.cfg.backoff_s * (1 + random.random() * 0.5)
            with self._lock:
                self._next_allowed = time.monotonic() + delay
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
