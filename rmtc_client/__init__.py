"""参赛程序侧的网关客户端：异步调用、超时、降级、事件理解。

原则（协议 §5 + 备赛指南第 4 件）：绝不能让"等模型"堵住动作窗。
"""

from .event_understanding import EventUnderstanding
from .json_extract import extract_first_json_object
from .llm_client import ClientConfig, LlmGatewayClient, LlmResult

__all__ = ["ClientConfig", "LlmGatewayClient", "LlmResult", "EventUnderstanding",
           "extract_first_json_object"]
