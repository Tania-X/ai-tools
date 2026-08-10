"""LLM 统一网关(gateway)。

所有 ai-tools 模块只依赖本包,换模型/换 provider 只改配置一处。
"""

from .client import ChatResponse, LLMClient, LLMError
from .config import GatewayConfig, ProviderConfig, load_config

__all__ = [
    "ChatResponse",
    "LLMClient",
    "LLMError",
    "GatewayConfig",
    "ProviderConfig",
    "load_config",
]
