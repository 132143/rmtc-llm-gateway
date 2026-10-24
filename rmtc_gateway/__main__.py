"""命令行入口：python3 -m rmtc_gateway --port 8000"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from .config import Config, TokenSpec
from .server import run


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="python3 -m rmtc_gateway",
        description="RMTC 统一 LLM 网关（OpenAI 兼容），协议 §5 实现。")
    parser.add_argument("--host", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, help="监听端口，默认 8000")
    parser.add_argument("--tokens", help="席位令牌，形如 A=practice-A,B=practice-B")
    parser.add_argument("--quota", type=int, help="每令牌额度，0 表示不限，默认 100")
    parser.add_argument("--ip-limit", type=int, help="每 IP 每窗口最多 POST 次数，默认 100")
    parser.add_argument("--inflight", type=int, help="每令牌最大在途调用，默认 2")
    parser.add_argument("--upstream", help="真实上游完整地址；留空则用本地模拟上游")
    parser.add_argument("--upstream-key", help="上游密钥（若有）")
    parser.add_argument("--upstream-timeout", type=float, help="上游超时秒数，默认 30")
    parser.add_argument("--model", help="默认模型名")
    parser.add_argument("--audit", help="审计日志 jsonl 路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条请求日志")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    changes = {}
    for field, value in (("host", args.host), ("port", args.port),
                         ("ip_limit", args.ip_limit),
                         ("upstream_url", args.upstream), ("upstream_key", args.upstream_key),
                         ("upstream_timeout_s", args.upstream_timeout),
                         ("audit_path", args.audit), ("verbose", args.verbose or None)):
        if value is not None:
            changes[field] = value
    if args.inflight is not None:
        changes["max_inflight_per_token"] = args.inflight
    if args.model:
        changes["default_model"] = args.model
    cfg = dataclasses.replace(cfg, **changes)

    if args.tokens:
        quota = args.quota if args.quota is not None else cfg.default_quota
        tokens = {}
        for chunk in args.tokens.replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            seat, _, token = chunk.partition("=")
            seat = seat.strip().upper()[:1]
            token = token.strip()
            if not seat or not token:
                parser.error(f"--tokens 片段无法解析：{chunk!r}")
            tokens[token] = TokenSpec(seat=seat, quota=quota)
        cfg = dataclasses.replace(cfg, tokens=tokens, default_quota=quota)
    elif args.quota is not None:
        cfg = dataclasses.replace(
            cfg, default_quota=args.quota,
            tokens={tok: TokenSpec(spec.seat, args.quota) for tok, spec in cfg.tokens.items()})

    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
