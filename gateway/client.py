"""LLM 统一网关客户端。

统一接口,支持 DeepSeek / Kimi / OpenAI(兼容端点,base_url + api_key 可配)。
能力:多 key 轮询、指数退避重试、token/成本统计、超时。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

import httpx

from .config import GatewayConfig, ProviderConfig
from .otel import get_tracer
from .pricing import compute_cost, lookup_pricing


class LLMError(Exception):
    """LLM 调用失败(所有可重试错误耗尽后抛出)。"""


@dataclass
class ChatResponse:
    content: str
    model: str
    provider: str
    usage: dict = field(default_factory=dict)  # prompt_tokens / completion_tokens ...
    cost: float = 0.0  # 元(未配置单价时为 0)
    tool_calls: list | None = None  # [{id, name, arguments}] — 模型请求调用工具时非空

    @property
    def prompt_tokens(self) -> int:
        return self.usage.get("prompt_tokens", 0)

    @property
    def completion_tokens(self) -> int:
        return self.usage.get("completion_tokens", 0)


class LLMClient:
    """统一 LLM 客户端。统计字段可跨调用累计,便于做成本看板。"""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self._client = httpx.Client()
        # 跨调用累计统计
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_cost = 0.0
        self.call_count = 0

    # ------------------------------------------------------------------ 统一入口
    def chat(
        self,
        messages: list[dict],
        *,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict | None = None,
    ) -> ChatResponse:
        pc = self.config.get(provider)
        payload = {
            "model": model or pc.model,
            "messages": messages,
            "max_tokens": max_tokens or pc.max_tokens,
            "temperature": temperature if temperature is not None else pc.temperature,
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format is not None:
            payload["response_format"] = response_format
        url = pc.base_url.rstrip("/") + "/chat/completions"

        # OTel 追踪: 每次 LLM 往返一个 span(OTEL_ENABLED=1 时生效, 默认 no-op 零开销)
        tracer = get_tracer()
        with tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute("llm.provider", pc.name)
            span.set_attribute("llm.model", payload["model"])
            span.set_attribute("llm.tools", bool(tools))
            span.set_attribute("llm.response_format", bool(response_format))

            data, used_key = self._call_with_retry(pc, url, payload, timeout or pc.timeout)

            msg = data["choices"][0]["message"]
            content = msg.get("content", "") or ""
            tool_calls = None
            if msg.get("tool_calls"):
                tool_calls = [
                    {
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments", "{}"),
                    }
                    for tc in msg["tool_calls"]
                ]
            usage = data.get("usage", {})
            cost = self._compute_cost(pc, usage, payload["model"])
            span.set_attribute("llm.prompt_tokens", usage.get("prompt_tokens", 0))
            span.set_attribute("llm.completion_tokens", usage.get("completion_tokens", 0))
            span.set_attribute("llm.cost", cost)
            span.set_attribute("llm.tool_calls", len(tool_calls) if tool_calls else 0)
            span.set_attribute("llm.content_len", len(content))
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        self.total_cost += cost
        self.call_count += 1
        return ChatResponse(
            content=content,
            model=data.get("model", payload["model"]),
            provider=pc.name,
            usage=usage,
            cost=cost,
            tool_calls=tool_calls,
        )

    # ------------------------------------------------------------ 重试 + 多 key 轮询
    def _call_with_retry(
        self, pc: ProviderConfig, url: str, payload: dict, timeout: float
    ) -> tuple[dict, str]:
        keys = pc.api_keys or [""]
        last_err: Exception | None = None
        auth_errors = 0
        for attempt in range(pc.max_retries):
            key = keys[attempt % len(keys)]
            try:
                return self._post_once(url, payload, key, timeout), key
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    # key 无效:换下一个 key,不占退避等待
                    auth_errors += 1
                    if auth_errors >= len(keys):
                        raise LLMError(f"{pc.name} 所有 key 均无效(401/403)") from e
                    continue
                if status == 429 or status >= 500:
                    last_err = e  # 限流/服务端错误:退避后重试
                else:
                    # 其余 4xx 是业务错误,重试无意义
                    raise LLMError(f"请求失败 HTTP {status}: {e.response.text[:200]}") from e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e  # 超时/连接问题:退避后重试
            self._backoff(attempt)
        raise LLMError(f"{pc.name} 重试 {pc.max_retries} 次后仍失败: {last_err}") from last_err

    def _post_once(self, url: str, payload: dict, key: str, timeout: float) -> dict:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        resp = self._client.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _backoff(self, attempt: int) -> None:
        # 指数退避 + 随机抖动,避免多实例同时重试打爆服务
        delay = min(0.5 * (2**attempt), 8.0) + random.uniform(0, 0.3)
        time.sleep(delay)

    # ------------------------------------------------------------------ 成本统计
    @staticmethod
    def _compute_cost(pc: ProviderConfig, usage: dict, model: str = "") -> float:
        """成本核算: 显式单价(元/千)优先, 其次内置峰谷价表(按调用时刻北京时段 + 缓存命中/未命中)。"""
        if pc.cost_per_1k_input is not None:
            pin = usage.get("prompt_tokens", 0) / 1000 * pc.cost_per_1k_input
            pout = usage.get("completion_tokens", 0) / 1000 * (pc.cost_per_1k_output or 0.0)
            return round(pin + pout, 6)
        pricing = lookup_pricing(model or pc.model)
        if pricing is not None:
            cost = compute_cost(usage, pricing)
            if cost is not None:
                return cost
        return 0.0
