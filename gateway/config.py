"""gateway 配置加载。

优先级:环境变量 > TOML 文件(AI_GATEWAY_CONFIG) > 内置默认值。
支持多 provider(deepseek / kimi / openai 兼容端点),每个 provider 可配多个 api_key。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 各 provider 默认端点(可在配置中覆盖)
DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "kimi": "https://api.moonshot.cn",
    "openai": "https://api.openai.com",
}


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_keys: list[str] = field(default_factory=list)  # 多 key 轮询
    model: str = ""
    max_tokens: int = 1024
    temperature: float = 0.7
    timeout: float = 60.0
    max_retries: int = 3
    # 单位:元 / 百万 tokens。留空则不核算成本,只统计 token 数。
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None


@dataclass
class GatewayConfig:
    default_provider: str = "deepseek"
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    def get(self, name: str | None = None) -> ProviderConfig:
        name = name or self.default_provider
        try:
            return self.providers[name]
        except KeyError:
            raise ValueError(f"未知 provider: {name!r},可用: {list(self.providers)}") from None


def _env_list(value: str | None) -> list[str]:
    return [k.strip() for k in value.split(",") if k.strip()] if value else []


def load_config(path: str | Path | None = None) -> GatewayConfig:
    """加载配置:先读 TOML 文件(如有),再让环境变量覆盖。

    TOML 示例:
        default_provider = "deepseek"
        [providers.deepseek]
        base_url = "https://api.deepseek.com"
        api_keys = ["sk-xxx"]
        model = "deepseek-chat"
        max_tokens = 1024
        temperature = 0.7
        cost_per_1k_input = 2.0
        cost_per_1k_output = 8.0
    """
    cfg = GatewayConfig()

    path = path or os.environ.get("AI_GATEWAY_CONFIG")
    data: dict = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)

    cfg.default_provider = data.get("default_provider", cfg.default_provider)
    for name, pc in data.get("providers", {}).items():
        cfg.providers[name] = ProviderConfig(
            name=name,
            base_url=pc.get("base_url", DEFAULT_BASE_URLS.get(name, "")),
            api_keys=list(pc.get("api_keys", [])),
            model=pc.get("model", ""),
            max_tokens=int(pc.get("max_tokens", 1024)),
            temperature=float(pc.get("temperature", 0.7)),
            timeout=float(pc.get("timeout", 60.0)),
            max_retries=int(pc.get("max_retries", 3)),
            cost_per_1k_input=pc.get("cost_per_1k_input"),
            cost_per_1k_output=pc.get("cost_per_1k_output"),
        )

    # 环境变量:单 provider 快捷配置(适合 GitHub Action 场景)
    # 注意:环境变量存在但为空串时不得覆盖默认值(GitHub Action 会把空 input 注入为空 env)
    env_name = os.environ.get("AI_GATEWAY_PROVIDER")
    if env_name:
        cfg.default_provider = env_name
        cfg.providers.setdefault(
            env_name, ProviderConfig(name=env_name, base_url=DEFAULT_BASE_URLS.get(env_name, ""))
        )
    provider = cfg.providers.get(cfg.default_provider)
    if provider:
        base_url = os.environ.get("AI_GATEWAY_BASE_URL")
        if base_url:
            provider.base_url = base_url
        keys = _env_list(os.environ.get("AI_GATEWAY_API_KEYS"))
        if keys:
            provider.api_keys = keys
        model = os.environ.get("AI_GATEWAY_MODEL")
        if model:
            provider.model = model
        max_tokens = os.environ.get("AI_GATEWAY_MAX_TOKENS")
        if max_tokens:
            provider.max_tokens = int(max_tokens)
        temperature = os.environ.get("AI_GATEWAY_TEMPERATURE")
        if temperature:
            provider.temperature = float(temperature)

    if not cfg.providers:
        raise ValueError(
            "未配置任何 provider:请设置 AI_GATEWAY_CONFIG 指向 TOML 文件,"
            "或至少设置环境变量 AI_GATEWAY_API_KEYS"
        )
    return cfg
