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
    # 官方口径（2026-10-28 主办方答疑）：建议 512–1024 tokens。原来 200 太紧——
    # 模型输出 JSON + avoid_cells 列表实测接近 300 tokens，200 会把列表截断成坏 JSON。
    max_tokens: int = 512
    # 官方网关除 model 外全部字段直接转发；DeepSeek 思考模式**默认开启**，
    # 关闭方式为请求 JSON 顶层 thinking={"type":"disabled"}。关思考后官方实测 0.78–0.99 s。
    # 不关的话思考 token 与回答共占 max_tokens，小预算很容易只返回空 content，且延迟顶到 4 s 超时。
    disable_thinking: bool = True
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
        self._thinking_off = False      # 上游拒绝 thinking 字段时置位（本场改发默认思考模式）
        self._logged_shape = False      # 只打印一次请求参数（检录时用来证明 thinking=disabled）

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

    @property
    def thinking_mode(self) -> str:
        """当前实际发送的思考模式：disabled（官方关闭写法）/ default（上游默认，开思考）。"""
        if self.cfg.disable_thinking and not self._thinking_off:
            return "disabled"
        return "default"

    def stats(self) -> dict:
        with self._lock:
            return {"attempts": self._attempts, "ok_calls": self._ok_calls,
                    "in_flight": self._in_flight, "spent": self._spent,
                    "degraded": self._degraded, "last_code": self._last_code,
                    "thinking": self.thinking_mode, "max_tokens": self.cfg.max_tokens}

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
            # 最多额外重试 2 次：response_format 与 thinking 两个"可选字段被上游拒绝"各允许回退一次。
            # 400 不产生 2xx、不消耗网关成功配额（规则 §11.3 只按成功调用记账），所以回退重试不白烧额度；
            # 反过来，如果因为一个可选字段就让整场放弃 AI，等于直接丢掉任务分 15 分（规则 §10.2）。
            result = None
            for _ in range(3):
                result = self._ask_once(prompt, messages, timeout_s, max_tokens)
                if result.code not in ("json_mode_rejected", "thinking_rejected"):
                    return result
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
        if self.cfg.disable_thinking and not self._thinking_off:
            # 官方口径：顶层 thinking={"type":"disabled"} 关闭思考模式（关闭后实测 0.78–0.99 s）。
            # 不关的话思考 token 与回答共占 max_tokens，小预算容易只返回空 content，延迟也会顶到超时。
            payload["thinking"] = {"type": "disabled"}
        if self.cfg.model:
            # 官方要求：必须使用统一默认模型，选手**不要**填写 model（严禁私自调用其他模型）。
            # 这里保留字段只为离线直连 api.deepseek.com 录制用；正式比赛 RMTC_MODEL 不设即可。
            payload["model"] = self.cfg.model
        if not self._logged_shape:
            # 只打一次、且不含令牌与提示词：检录时用这一行证明"思考已关、预算是多少"。
            self._logged_shape = True
            self._log("info", "AI 请求参数：thinking={0}，json_mode={1}，max_tokens={2}，timeout={3}s".format(
                self.thinking_mode,
                "on" if (self.cfg.json_mode and not self._json_mode_off) else "off",
                max_tokens if max_tokens is not None else self.cfg.max_tokens,
                timeout_s or self.cfg.timeout_s))

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
        if (not self._thinking_off and self.cfg.disable_thinking and exc.code == 400
                and "thinking" in lowered):
            # 上游不认识 thinking 字段（换了实现/旧网关）：本场改发默认思考模式，立刻重试一次。
            with self._lock:
                self._thinking_off = True
                self._last_code = "thinking_rejected"
            self._log("warn", "上游拒绝 thinking 字段（{0}），本场改为默认思考模式".format(message[:80]))
            return self._fail("thinking_rejected", exc.code, message, started)
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
