"""LLMClient / load_config 单元测试。

所有 HTTP 均通过 monkeypatch 打桩,mock 掉 httpx,不发起真实网络请求。
"""

from __future__ import annotations

import httpx
import pytest

from gateway.client import LLMClient, LLMError
from gateway.config import GatewayConfig, ProviderConfig, load_config


def make_client(keys: tuple[str, ...] = ("key-a",), retries: int = 3) -> LLMClient:
    cfg = GatewayConfig(
        default_provider="deepseek",
        providers={
            "deepseek": ProviderConfig(
                name="deepseek",
                base_url="https://api.deepseek.com",
                api_keys=list(keys),
                model="deepseek-chat",
                max_retries=retries,
                cost_per_1k_input=2.0,   # 元/百万 tokens
                cost_per_1k_output=8.0,
            )
        },
    )
    return LLMClient(cfg)


def fake_response(status: int = 200, body: dict | None = None) -> httpx.Response:
    body = body or {
        "id": "chatcmpl-test",
        "model": "deepseek-chat",
        "choices": [{"message": {"role": "assistant", "content": "你好"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return httpx.Response(status, json=body, request=httpx.Request("POST", "http://t"))


def stub_post(monkeypatch, handler) -> None:
    monkeypatch.setattr(httpx.Client, "post", handler)


def test_chat_success(monkeypatch):
    client = make_client()
    stub_post(monkeypatch, lambda self, url, json, headers, timeout: fake_response())

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.content == "你好"
    assert resp.provider == "deepseek"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert client.call_count == 1


def test_retry_on_429_then_success(monkeypatch):
    calls = {"n": 0}
    client = make_client()

    def handler(self, url, json, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, request=httpx.Request("POST", "http://t"))
        return fake_response()

    stub_post(monkeypatch, handler)
    monkeypatch.setattr(client, "_backoff", lambda attempt: None)  # 测试中跳过 sleep

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.content == "你好"
    assert calls["n"] == 2


def test_key_rotation_on_401(monkeypatch):
    used_auth = []
    client = make_client(keys=("bad-key", "good-key"))

    def handler(self, url, json, headers, timeout):
        used_auth.append(headers["Authorization"])
        if headers["Authorization"].endswith("bad-key"):
            return httpx.Response(401, request=httpx.Request("POST", "http://t"))
        return fake_response()

    stub_post(monkeypatch, handler)
    monkeypatch.setattr(client, "_backoff", lambda attempt: None)

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.content == "你好"
    assert used_auth == ["Bearer bad-key", "Bearer good-key"]


def test_all_keys_invalid_raises(monkeypatch):
    client = make_client(keys=("bad1", "bad2"))

    def handler(self, url, json, headers, timeout):
        return httpx.Response(401, request=httpx.Request("POST", "http://t"))

    stub_post(monkeypatch, handler)
    with pytest.raises(LLMError, match="所有 key 均无效"):
        client.chat([{"role": "user", "content": "hi"}])


def test_business_4xx_raises_immediately(monkeypatch):
    """400 是业务错误,不应触发重试。"""
    calls = {"n": 0}
    client = make_client()

    def handler(self, url, json, headers, timeout):
        calls["n"] += 1
        return httpx.Response(
            400, json={"error": {"message": "bad request"}},
            request=httpx.Request("POST", "http://t"),
        )

    stub_post(monkeypatch, handler)
    with pytest.raises(LLMError, match="HTTP 400"):
        client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1


def test_cost_accounting(monkeypatch):
    client = make_client()
    body = {
        "id": "chatcmpl-test",
        "model": "deepseek-chat",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000},
    }

    stub_post(monkeypatch, lambda self, url, json, headers, timeout: fake_response(body=body))

    client.chat([{"role": "user", "content": "hi"}])
    client.chat([{"role": "user", "content": "hi"}])

    # 每轮:输入 1000 tok × 2 元/百万 = 2 元,输出 1000 tok × 8 元/百万 = 8 元,共 10 元
    assert client.total_cost == pytest.approx(20.0)
    assert client.prompt_tokens == 2000
    assert client.completion_tokens == 2000
    assert client.call_count == 2


def test_load_config_from_toml(tmp_path):
    toml = tmp_path / "gateway.toml"
    toml.write_text(
        """
default_provider = "kimi"
[providers.kimi]
base_url = "https://api.moonshot.cn"
api_keys = ["sk-kimi-1"]
model = "moonshot-v1-8k"
max_tokens = 512
cost_per_1k_input = 12.0
cost_per_1k_output = 12.0
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(toml)

    assert cfg.default_provider == "kimi"
    kimi = cfg.get("kimi")
    assert kimi.base_url == "https://api.moonshot.cn"
    assert kimi.model == "moonshot-v1-8k"
    assert kimi.max_tokens == 512
    assert kimi.cost_per_1k_input == 12.0
