"""HTTP 服务端：协议 §5「LLM 网关（OpenAI 兼容）」的可运行实现。

两个端点，和协议表格一字对应：
    GET  <GATEWAY>/healthz              服务可用性检查
    POST <GATEWAY>/v1/chat/completions  OpenAI 兼容对话接口

网关只做转发与限额：提示词、模型输出解析、动作合法性、失败回退、何时调用，全部由参赛程序负责。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, errors as E
from .config import Config
from .limits import RateLimiter, TokenState
from .upstream import HttpUpstream, MockUpstream, UpstreamTimeout, UpstreamUnavailable


class GatewayState:
    """进程内共享状态：令牌账本、频率限制器、上游、审计日志。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started_at = time.time()
        self.tokens = {token: TokenState(spec.seat, spec.quota)
                       for token, spec in cfg.tokens.items()}
        self.limiter = RateLimiter(cfg.ip_limit, cfg.ip_window_s)
        if cfg.upstream_url:
            self.upstream = HttpUpstream(cfg.upstream_url, cfg.upstream_key,
                                         cfg.upstream_timeout_s)
        else:
            self.upstream = MockUpstream()
        self._audit_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = {"requests": 0, "ok": 0, "rejected": 0, "upstream_errors": 0}

    def bump(self, key: str) -> None:
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + 1

    def stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)

    def audit(self, record: dict) -> None:
        """每请求一行 JSON。绝不写令牌原文（协议：令牌不得写入公开日志）。"""
        if not self.cfg.audit_path:
            return
        line = json.dumps(record, ensure_ascii=False)
        with self._audit_lock:
            try:
                with open(self.cfg.audit_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                sys.stderr.write(f"[gw] 审计日志写入失败：{exc}\n")

    def health(self) -> dict:
        """可用性信息。有意不暴露任何令牌的剩余额度（协议：网关不提供配额查询）。"""
        cfg = self.cfg
        return {
            "status": "ok",
            "gateway": "rmtc-llm-gateway",
            "version": __version__,
            "upstream": self.upstream.name,
            "model": cfg.default_model,
            "tokens": len(self.tokens),
            "limits": {
                "ip_window_s": cfg.ip_window_s,
                "ip_limit": cfg.ip_limit,
                "max_inflight_per_token": cfg.max_inflight_per_token,
                "max_body_bytes": cfg.max_body_bytes,
                "upstream_timeout_s": cfg.upstream_timeout_s,
            },
            "stats": self.stats(),
            "uptime_s": round(time.time() - self.started_at, 1),
        }


def make_handler(state: GatewayState):
    cfg = state.cfg

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "rmtc-gateway/" + __version__
        sys_version = ""

        # ---------- 基础 ----------
        def log_message(self, fmt, *args):
            if cfg.verbose:
                sys.stderr.write("[gw] %s %s\n" % (self.client_address[0], fmt % args))

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _send_error(self, exc: E.GatewayError) -> None:
            self._send_bytes(exc.status, exc.to_bytes(), "application/json; charset=utf-8")

        # ---------- GET /healthz ----------
        def do_GET(self):
            if self.path.split("?")[0] == "/healthz":
                self._send_json(200, state.health())
            else:
                self._send_error(E.not_found())

        # ---------- POST /v1/chat/completions ----------
        def do_POST(self):
            started = time.perf_counter()
            record = {"ts": round(time.time(), 3), "ip": self.client_address[0],
                      "path": self.path, "status": None, "code": None, "model": None,
                      "bytes": 0, "seat": None, "latency_ms": None}
            state.bump("requests")
            token: TokenState | None = None
            reserved = False
            slot = False
            try:
                if self.path.split("?")[0] != "/v1/chat/completions":
                    raise E.not_found()

                raw = self._read_body()
                record["bytes"] = len(raw)

                seat, token = self._authenticate()
                record["seat"] = seat

                if not state.limiter.allow(record["ip"]):
                    raise E.rate_limit()

                payload, model = self._parse(raw)
                record["model"] = model

                if not token.reserve_quota():
                    raise E.quota_exceeded()
                reserved = True

                if not token.acquire_slot(cfg.max_inflight_per_token):
                    raise E.concurrency_limit()
                slot = True

                try:
                    result = state.upstream.complete(payload, model)
                except UpstreamTimeout:
                    state.bump("upstream_errors")
                    raise E.upstream_timeout()
                except UpstreamUnavailable:
                    state.bump("upstream_errors")
                    raise E.upstream_unavailable()

                token.commit_quota()
                reserved = False
                state.bump("ok")
                record["status"] = 200
                self._send_bytes(200, result.body, result.content_type)

            except E.GatewayError as exc:
                state.bump("rejected")
                record["status"] = exc.status
                record["code"] = exc.code
                self._send_error(exc)
            except (BrokenPipeError, ConnectionResetError):
                record["status"] = -1
                record["code"] = "client_gone"
            finally:
                if slot and token is not None:
                    token.release_slot()
                if reserved and token is not None:
                    token.release_quota()
                record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
                state.audit(record)

        # ---------- 请求解析 ----------
        def _read_body(self) -> bytes:
            """按协议顺序检查：Transfer-Encoding(501) -> Content-Length(400) -> 大小(413)。"""
            if self.headers.get("Transfer-Encoding"):
                raise E.unsupported_transfer_encoding()
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1:
                raise E.invalid_content_length()
            raw_length = lengths[0].strip()
            if not raw_length.isdigit():
                raise E.invalid_content_length()
            length = int(raw_length)
            if length > cfg.max_body_bytes:
                raise E.payload_too_large()
            self.close_connection = True
            try:
                self.connection.settimeout(cfg.read_timeout_s)
                data = self.rfile.read(length)
            except (socket.timeout, TimeoutError):
                raise E.incomplete_body()
            if len(data) != length:
                raise E.incomplete_body()
            return data

        def _authenticate(self):
            header = self.headers.get("Authorization") or ""
            parts = header.split()
            if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
                raise E.missing_token()
            token = state.tokens.get(parts[1])
            if token is None:
                raise E.invalid_token()
            return token.seat, token

        def _parse(self, raw: bytes):
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise E.invalid_json()
            if not isinstance(payload, dict):
                raise E.invalid_json()
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                raise E.invalid_messages()
            for message in messages:
                if not isinstance(message, dict) or not isinstance(message.get("role"), str):
                    raise E.invalid_messages()
            if payload.get("stream") not in (None, False):
                raise E.stream_unsupported()
            model = payload.get("model")
            if not isinstance(model, str) or not model.strip():
                model = cfg.default_model
            return payload, model.strip()

    return Handler


def create_server(cfg: Config | None = None):
    """建一个可测试的服务器对象，返回 (httpd, state)。测试里传 port=0 拿随机端口。"""
    cfg = cfg or Config.from_env()
    state = GatewayState(cfg)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(state))
    httpd.daemon_threads = True
    return httpd, state


def _print_banner(cfg: Config, httpd) -> None:
    host, port = httpd.server_address[:2]
    display = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    seats = sorted(spec.seat for spec in cfg.tokens.values())
    print(f"网关已就绪：http://{display}:{port}", flush=True)
    print(f"席位：{', '.join(seats)}　令牌数：{len(cfg.tokens)}　默认模型：{cfg.default_model}",
          flush=True)
    if all(token.startswith("practice-") for token in cfg.tokens):
        # 练习令牌本来就是公开的，方便现场对表；正式令牌一律不打印。
        print("练习令牌：" + "  ".join(sorted(cfg.tokens)), flush=True)
    else:
        print("正式令牌已加载（为安全起见不打印原文）。", flush=True)
    if cfg.upstream_mode == "mock":
        print("上游：本地模拟（不做真实语义理解，只能用来验证链路与错误处理）。", flush=True)
    else:
        print(f"上游：{cfg.upstream_url}（超时 {cfg.upstream_timeout_s:.0f}s）", flush=True)
    if cfg.audit_path:
        print(f"审计日志：{cfg.audit_path}", flush=True)


def run(cfg: Config | None = None) -> None:
    cfg = cfg or Config.from_env()
    httpd, _state = create_server(cfg)
    _print_banner(cfg, httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("网关已停止", flush=True)
    finally:
        httpd.server_close()
