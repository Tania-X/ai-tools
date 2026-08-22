"""pricing 模块测试: 官网 HTML 解析、TTL/磁盘缓存、动态定价回退。"""

from __future__ import annotations

import json

import pytest

from gateway.client import LLMClient
from gateway.config import ProviderConfig
from gateway.pricing import (
    PRICING_URL,
    fetch_deepseek_pricing,
    get_deepseek_pricing,
    parse_pricing_html,
)

SAMPLE_HTML = """<html><body><table>
<tr><td>模型</td><td>deepseek-v4-flash</td><td>deepseek-v4-pro</td></tr>
<tr><td rowspan="6">价格(1)(2)</td><td rowspan="2">百万tokens输入<br>（缓存命中）</td><td>空闲时段</td><td>0.05元</td><td>0.15元</td></tr>
<tr><td>高峰时段</td><td>0.10元</td><td>0.30元</td></tr>
<tr><td rowspan="2">百万tokens输入<br>（缓存未命中）</td><td>空闲时段</td><td>1.5元</td><td>4.5元</td></tr>
<tr><td>高峰时段</td><td>3.0元</td><td>9.0元</td></tr>
<tr><td rowspan="2">百万tokens输出</td><td>空闲时段</td><td>4.5元</td><td>13.5元</td></tr>
<tr><td>高峰时段</td><td>9.0元</td><td>27.0元</td></tr>
</table></body></html>"""

EXPECTED = {
    "deepseek-v4-flash": {
        "input_hit_peak": 0.10,
        "input_hit_offpeak": 0.05,
        "input_miss_peak": 3.0,
        "input_miss_offpeak": 1.5,
        "output_peak": 9.0,
        "output_offpeak": 4.5,
    },
    "deepseek-v4-pro": {
        "input_hit_peak": 0.30,
        "input_hit_offpeak": 0.15,
        "input_miss_peak": 9.0,
        "input_miss_offpeak": 4.5,
        "output_peak": 27.0,
        "output_offpeak": 13.5,
    },
}


@pytest.fixture
def isolated_pricing_cache(tmp_path, monkeypatch):
    """隔离磁盘缓存, 避免测试写入用户真实 ~/.cache。"""
    cache_file = tmp_path / "deepseek-pricing.json"
    monkeypatch.setenv("AI_GATEWAY_PRICING_CACHE_FILE", str(cache_file))
    monkeypatch.setattr("gateway.pricing._pricing_cache", {})
    return cache_file


def test_parse_pricing_html():
    assert parse_pricing_html(SAMPLE_HTML) == EXPECTED


def test_fetch_deepseek_pricing_parses_live(monkeypatch):
    """验证 fetch 流程确实把 HTML 交给解析器(避免只测了解析器)。"""
    monkeypatch.setattr("gateway.pricing.parse_pricing_html", lambda html: EXPECTED)
    monkeypatch.setattr(
        "gateway.pricing.urllib.request.urlopen",
        lambda req, timeout: _FakeResp(html=SAMPLE_HTML),
    )
    assert fetch_deepseek_pricing() == EXPECTED


def test_get_deepseek_pricing_cache(isolated_pricing_cache, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr("gateway.pricing._pricing_cache", {})

    def fake_fetch(url=PRICING_URL, timeout=5.0):
        calls["n"] += 1
        return EXPECTED

    monkeypatch.setattr("gateway.pricing.fetch_deepseek_pricing", fake_fetch)
    assert get_deepseek_pricing() == EXPECTED
    assert get_deepseek_pricing() == EXPECTED
    assert calls["n"] == 1


def test_get_deepseek_pricing_uses_disk_cache_on_fetch_failure(
    isolated_pricing_cache, monkeypatch
):
    isolated_pricing_cache.write_text(
        json.dumps({"url": PRICING_URL, "fetched_at": 0, "pricing": EXPECTED}),
        encoding="utf-8",
    )

    def boom(url=PRICING_URL, timeout=5.0):
        raise RuntimeError("network down")

    monkeypatch.setattr("gateway.pricing.fetch_deepseek_pricing", boom)
    assert get_deepseek_pricing() == EXPECTED


def test_ttl_from_env_controls_refresh(isolated_pricing_cache, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_PRICING_TTL_SECONDS", "0")
    calls = {"n": 0}

    def fake_fetch(url=PRICING_URL, timeout=5.0):
        calls["n"] += 1
        return EXPECTED

    monkeypatch.setattr("gateway.pricing.fetch_deepseek_pricing", fake_fetch)
    assert get_deepseek_pricing() == EXPECTED
    assert get_deepseek_pricing() == EXPECTED
    assert calls["n"] == 2


def test_compute_cost_dynamic_pricing_used_when_enabled(monkeypatch):
    pc = ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com",
        api_keys=["k"],
        model="deepseek-v4-flash",
        dynamic_pricing=True,
    )
    dynamic = {
        "input_hit_peak": 0.2,
        "input_hit_offpeak": 0.1,
        "input_miss_peak": 6.0,
        "input_miss_offpeak": 3.0,
        "output_peak": 18.0,
        "output_offpeak": 9.0,
    }
    monkeypatch.setattr(
        "gateway.client.lookup_dynamic_pricing", lambda model, ttl=None: dynamic
    )
    monkeypatch.setattr("gateway.pricing.is_peak_hour", lambda now=None: True)

    usage = {
        "prompt_cache_hit_tokens": 1_000_000,
        "prompt_cache_miss_tokens": 0,
        "completion_tokens": 1_000_000,
    }
    cost = LLMClient._compute_cost(pc, usage, model="deepseek-v4-flash")
    assert cost == pytest.approx(0.2 + 18.0)


def test_compute_cost_dynamic_fallback_to_builtin(monkeypatch):
    pc = ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com",
        api_keys=["k"],
        model="deepseek-v4-flash",
        dynamic_pricing=True,
    )

    def boom(model, ttl=None):
        raise RuntimeError("network down")

    monkeypatch.setattr("gateway.client.lookup_dynamic_pricing", boom)
    monkeypatch.setattr("gateway.pricing.is_peak_hour", lambda now=None: True)

    usage = {
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 1_000_000,
        "completion_tokens": 1_000_000,
    }
    cost = LLMClient._compute_cost(pc, usage, model="deepseek-v4-flash")
    # 内置 deepseek-v4-flash 高峰价: 输入未命中 3 元/百万 + 输出 9 元/百万
    assert cost == pytest.approx(3.0 + 9.0)


class _FakeResp:
    def __init__(self, html: str):
        self._html = html

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._html.encode("utf-8")
