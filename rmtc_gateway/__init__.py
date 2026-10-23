"""RMTC 统一 LLM 网关（OpenAI 兼容）的本地实现。

按《RMTC 机甲大师图灵对决赛通信协议 v1.1》§5 实现，只做转发与限额，不做任何决策。
"""

__version__ = "1.0.0"

# 版本号必须先定义：server.py 会 from . import __version__。
from .config import Config, TokenSpec  # noqa: E402
from .server import create_server, run  # noqa: E402

__all__ = ["Config", "TokenSpec", "create_server", "run", "__version__"]
