"""网关配置。

所有配置都能用环境变量给，方便比赛现场临时换地址/令牌（协议要求：不能把令牌写进公开仓库）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class TokenSpec:
    """一个席位令牌。quota=0 表示不限额（协议：0 表示不限制）。"""

    seat: str
    quota: int = 100


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 8000
    # token -> TokenSpec
    tokens: dict = field(default_factory=dict)
    default_quota: int = 100
    # 协议 §5「请求频率与配额」：每来源 IP 每 10 秒最多 100 次 POST
    ip_window_s: float = 10.0
    ip_limit: int = 100
    # 协议 §5：每个席位令牌最多同时存在 2 个在途调用
    max_inflight_per_token: int = 2
    # 协议 §5：请求体上限 2 MiB，超过返回 413
    max_body_bytes: int = 2_097_152
    # 协议 §5：网关请求读取超时 5 秒
    read_timeout_s: float = 5.0
    # 上游模型服务；留空表示用本地模拟上游（只验证链路，不做真实理解）
    upstream_url: str = ""
    upstream_key: str = ""
    upstream_timeout_s: float = 30.0
    default_model: str = "mock-llm"
    audit_path: str = ""
    verbose: bool = False

    def with_port(self, port: int) -> "Config":
        return replace(self, port=port)

    def with_tokens(self, tokens: dict) -> "Config":
        return replace(self, tokens=dict(tokens))

    @property
    def upstream_mode(self) -> str:
        return "http" if self.upstream_url else "mock"

    @classmethod
    def from_env(cls) -> "Config":
        """RMTC_GW_TOKENS 形如 "A=practice-A,B=practice-B"。

        不设时退回练习包默认的 practice-A..practice-D（正式比赛必须换成主办方令牌）。
        """
        raw_tokens = os.environ.get("RMTC_GW_TOKENS", "").strip()
        default_quota = _env_int("RMTC_GW_QUOTA", 100)
        if raw_tokens:
            tokens = _parse_tokens(raw_tokens, default_quota)
        else:
            tokens = {
                f"practice-{seat}": TokenSpec(seat=seat, quota=default_quota)
                for seat in "ABCD"
            }
        return cls(
            host=os.environ.get("RMTC_GW_HOST", "0.0.0.0"),
            port=_env_int("RMTC_GW_PORT", 8000),
            tokens=tokens,
            default_quota=default_quota,
            ip_window_s=_env_float("RMTC_GW_IP_WINDOW_S", 10.0),
            ip_limit=_env_int("RMTC_GW_IP_LIMIT", 100),
            max_inflight_per_token=_env_int("RMTC_GW_INFLIGHT", 2),
            max_body_bytes=_env_int("RMTC_GW_MAX_BODY", 2_097_152),
            read_timeout_s=_env_float("RMTC_GW_READ_TIMEOUT_S", 5.0),
            upstream_url=os.environ.get("RMTC_GW_UPSTREAM_URL", "").strip(),
            upstream_key=os.environ.get("RMTC_GW_UPSTREAM_KEY", "").strip(),
            upstream_timeout_s=_env_float("RMTC_GW_UPSTREAM_TIMEOUT_S", 30.0),
            default_model=os.environ.get("RMTC_GW_MODEL", "mock-llm").strip() or "mock-llm",
            audit_path=os.environ.get("RMTC_GW_AUDIT", "").strip(),
            verbose=os.environ.get("RMTC_GW_VERBOSE", "") not in ("", "0", "false", "False"),
        )


def _parse_tokens(raw: str, quota: int) -> dict:
    tokens = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            seat, token = chunk.split("=", 1)
        elif ":" in chunk:
            seat, token = chunk.split(":", 1)
        else:
            raise ValueError(f"RMTC_GW_TOKENS 片段无法解析（要 seat=token）：{chunk!r}")
        seat = seat.strip().upper()[:1]
        token = token.strip()
        if not seat or not token:
            raise ValueError(f"RMTC_GW_TOKENS 片段为空：{chunk!r}")
        tokens[token] = TokenSpec(seat=seat, quota=quota)
    if not tokens:
        raise ValueError("RMTC_GW_TOKENS 解析后没有任何令牌")
    return tokens


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数，收到 {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字，收到 {raw!r}")
