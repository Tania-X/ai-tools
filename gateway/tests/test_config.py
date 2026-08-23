"""gateway 配置加载单测(重点:空环境变量不得覆盖默认值)。"""

import os

from gateway.config import DEFAULT_BASE_URLS, load_config


def _clear_env(monkeypatch):
    """清掉所有 AI_GATEWAY_* 环境变量,保证测试隔离。"""
    for key in list(os.environ):
        if key.startswith("AI_GATEWAY_"):
            monkeypatch.delenv(key, raising=False)


def test_empty_env_keeps_defaults(monkeypatch):
    """GitHub Action 注入空串 env 时,必须保留默认 base_url/model(回归测试)。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_PROVIDER", "deepseek")
    monkeypatch.setenv("AI_GATEWAY_API_KEYS", "sk-test")
    # 模拟 action inputs 的空 default
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "")
    monkeypatch.setenv("AI_GATEWAY_MODEL", "")
    monkeypatch.setenv("AI_GATEWAY_MAX_TOKENS", "")
    monkeypatch.setenv("AI_GATEWAY_TEMPERATURE", "")

    cfg = load_config()
    pc = cfg.get("deepseek")
    assert pc.base_url == DEFAULT_BASE_URLS["deepseek"], "空串 base_url 覆盖了默认值!"
    assert pc.model == ""  # deepseek 无内置默认 model,空串不覆盖(保持空,由调用方兜底)
    assert pc.max_tokens == 1024
    assert pc.temperature == 0.7


def test_nonempty_env_overrides(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_PROVIDER", "deepseek")
    monkeypatch.setenv("AI_GATEWAY_API_KEYS", "sk-a,sk-b")
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://my-proxy.example.com")
    monkeypatch.setenv("AI_GATEWAY_MODEL", "deepseek-chat")
    monkeypatch.setenv("AI_GATEWAY_MAX_TOKENS", "2048")

    cfg = load_config()
    pc = cfg.get("deepseek")
    assert pc.base_url == "https://my-proxy.example.com"
    assert pc.model == "deepseek-chat"
    assert pc.api_keys == ["sk-a", "sk-b"]
    assert pc.max_tokens == 2048


def test_empty_env_does_not_crash_int_parse(monkeypatch):
    """修复前 int(os.environ.get(...)) 对空串会 ValueError,此处验证不再崩溃。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_PROVIDER", "deepseek")
    monkeypatch.setenv("AI_GATEWAY_API_KEYS", "sk-test")
    monkeypatch.setenv("AI_GATEWAY_MAX_TOKENS", "")
    monkeypatch.setenv("AI_GATEWAY_TEMPERATURE", "")
    cfg = load_config()  # 不抛异常即通过
    assert cfg.get("deepseek").max_tokens == 1024


def test_dynamic_pricing_env_parsed(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_PROVIDER", "deepseek")
    monkeypatch.setenv("AI_GATEWAY_API_KEYS", "sk-test")
    monkeypatch.setenv("AI_GATEWAY_DYNAMIC_PRICING", "1")
    monkeypatch.setenv("AI_GATEWAY_PRICING_TTL_SECONDS", "3600")

    cfg = load_config()
    pc = cfg.get("deepseek")
    assert pc.dynamic_pricing is True
    assert pc.pricing_cache_ttl_seconds == 3600.0


def test_dynamic_pricing_toml_parsed(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    toml = tmp_path / "gateway.toml"
    toml.write_text(
        """
default_provider = "deepseek"
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_keys = ["sk-test"]
dynamic_pricing = true
pricing_cache_ttl_seconds = 60
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(toml)
    pc = cfg.get("deepseek")
    assert pc.dynamic_pricing is True
    assert pc.pricing_cache_ttl_seconds == 60.0


def test_extra_body_env_parsed(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_PROVIDER", "deepseek")
    monkeypatch.setenv("AI_GATEWAY_API_KEYS", "sk-test")
    monkeypatch.setenv(
        "AI_GATEWAY_EXTRA_BODY", '{"thinking": {"type": "disabled"}}'
    )

    cfg = load_config()
    pc = cfg.get("deepseek")
    assert pc.extra_body == {"thinking": {"type": "disabled"}}
