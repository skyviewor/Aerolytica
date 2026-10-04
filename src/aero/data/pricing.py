"""Model pricing and TokenTracker for Aero.

All prices in CNY per 1K tokens.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass
class ModelPrice:
    input_price: float
    cached_input_price: float
    output_price: float
    context_window: int


PRICING: dict[str, ModelPrice] = {
    # --- LLM ---
    # DeepSeek official off-peak list price (CNY per 1K tokens).
    # Provider-aware peak pricing is applied when usage is recorded.
    "deepseek-v4-flash": ModelPrice(
        input_price=0.001,
        cached_input_price=0.000020,
        output_price=0.002,
        context_window=1_000_000,
    ),
    "deepseek-v4-pro": ModelPrice(
        input_price=0.003,
        cached_input_price=0.000025,
        output_price=0.006,
        context_window=1_000_000,
    ),
    "deepseek-chat": ModelPrice(
        input_price=0.001,
        cached_input_price=0.000020,
        output_price=0.002,
        context_window=1_000_000,
    ),
    "deepseek-reasoner": ModelPrice(
        input_price=0.001,
        cached_input_price=0.000020,
        output_price=0.002,
        context_window=1_000_000,
    ),
    # Kimi (CNY per 1K tokens)
    "kimi-k2.6": ModelPrice(
        input_price=0.0065,
        cached_input_price=0.0011,
        output_price=0.027,
        context_window=262_144,
    ),
    "kimi-k2.5": ModelPrice(
        input_price=0.004,
        cached_input_price=0.0007,
        output_price=0.021,
        context_window=262_144,
    ),
    "kimi-k2-thinking": ModelPrice(
        input_price=0.004,
        cached_input_price=0.0007,
        output_price=0.021,
        context_window=262_144,
    ),
    "kimi-k2-0905-preview": ModelPrice(
        input_price=0.004,
        cached_input_price=0.0007,
        output_price=0.021,
        context_window=262_144,
    ),
    "moonshot-v1-128k": ModelPrice(
        input_price=0.01,
        cached_input_price=0.01,
        output_price=0.03,
        context_window=131_072,
    ),
    "moonshot-v1-32k": ModelPrice(
        input_price=0.005,
        cached_input_price=0.005,
        output_price=0.02,
        context_window=32_768,
    ),
    # Qwen / Bailian (CNY per 1K tokens)
    # Prices are the standard China-mainland prices published by Model Studio;
    # implicit cache hits for Bailian-hosted models cost 20% of input tokens.
    "qwen3.7": ModelPrice(
        input_price=0.002,
        cached_input_price=0.0004,
        output_price=0.008,
        context_window=1_000_000,
    ),
    "qwen3.7-max": ModelPrice(
        input_price=0.012,
        cached_input_price=0.0024,
        output_price=0.036,
        context_window=1_000_000,
    ),
    "qwen-plus": ModelPrice(
        input_price=0.002,
        cached_input_price=0.002,
        output_price=0.006,
        context_window=131_072,
    ),
    "qwen-max": ModelPrice(
        input_price=0.02,
        cached_input_price=0.02,
        output_price=0.06,
        context_window=32_768,
    ),
    "qwen-turbo": ModelPrice(
        input_price=0.0005,
        cached_input_price=0.0005,
        output_price=0.0015,
        context_window=131_072,
    ),
    "qwen-long": ModelPrice(
        input_price=0.0005,
        cached_input_price=0.0005,
        output_price=0.0015,
        context_window=1_000_000,
    ),
    "qwen3-max": ModelPrice(
        input_price=0.008,
        cached_input_price=0.008,
        output_price=0.024,
        context_window=131_072,
    ),
    "qwen3-plus": ModelPrice(
        input_price=0.0035,
        cached_input_price=0.0035,
        output_price=0.0105,
        context_window=131_072,
    ),
    # OpenAI (USD → CNY, per 1K tokens; cache pricing from prompt caching docs)
    "gpt-4o": ModelPrice(
        input_price=0.018,
        cached_input_price=0.009,
        output_price=0.072,
        context_window=128_000,
    ),
    "gpt-4o-mini": ModelPrice(
        input_price=0.00108,
        cached_input_price=0.00054,
        output_price=0.00432,
        context_window=128_000,
    ),
    # --- Vision ---
    "qwen3-vl-plus": ModelPrice(
        input_price=0.003,
        cached_input_price=0.003,
        output_price=0.012,
        context_window=32_768,
    ),
    "qwen3-vl-flash": ModelPrice(
        input_price=0.0015,
        cached_input_price=0.0015,
        output_price=0.006,
        context_window=32_768,
    ),
    "qwen-vl-max": ModelPrice(
        input_price=0.003,
        cached_input_price=0.003,
        output_price=0.012,
        context_window=32_768,
    ),
    "qwen-vl-plus": ModelPrice(
        input_price=0.0015,
        cached_input_price=0.0015,
        output_price=0.006,
        context_window=32_768,
    ),
    "qwen3.5-flash": ModelPrice(
        input_price=0.0015,
        cached_input_price=0.0015,
        output_price=0.006,
        context_window=1_000_000,
    ),
    "qwen3.5-plus": ModelPrice(
        input_price=0.003,
        cached_input_price=0.003,
        output_price=0.012,
        context_window=1_000_000,
    ),
    "qwen3.6-flash": ModelPrice(
        input_price=0.0015,
        cached_input_price=0.0015,
        output_price=0.006,
        context_window=1_000_000,
    ),
    "qwen3.6-plus": ModelPrice(
        input_price=0.003,
        cached_input_price=0.003,
        output_price=0.012,
        context_window=1_000_000,
    ),
    "qwen3.7-plus": ModelPrice(
        input_price=0.002,
        cached_input_price=0.0004,
        output_price=0.008,
        context_window=1_000_000,
    ),
}

DEEPSEEK_OFFICIAL_PRICING: dict[str, ModelPrice] = {
    "deepseek-v4-flash": ModelPrice(
        input_price=0.001,
        cached_input_price=0.000020,
        output_price=0.002,
        context_window=1_000_000,
    ),
    "deepseek-v4-pro": ModelPrice(
        input_price=0.003,
        cached_input_price=0.000025,
        output_price=0.006,
        context_window=1_000_000,
    ),
    # Compatibility aliases use V4 Flash pricing on the official endpoint.
    "deepseek-chat": ModelPrice(
        input_price=0.001,
        cached_input_price=0.000020,
        output_price=0.002,
        context_window=1_000_000,
    ),
    "deepseek-reasoner": ModelPrice(
        input_price=0.001,
        cached_input_price=0.000020,
        output_price=0.002,
        context_window=1_000_000,
    ),
}

BAILIAN_DEEPSEEK_PRICING: dict[str, ModelPrice] = {
    "deepseek-v4-flash": ModelPrice(
        input_price=0.001,
        cached_input_price=0.0002,
        output_price=0.002,
        context_window=1_000_000,
    ),
    "deepseek-v4-pro": ModelPrice(
        input_price=0.012,
        cached_input_price=0.0024,
        output_price=0.024,
        context_window=1_000_000,
    ),
}


# Context limits are independent from pricing so an unknown custom model never
# inherits a plausible-looking percentage from the fallback price.
CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "qwen3.7-max": 1_000_000,
    "qwen3.7-plus": 1_000_000,
    "qwen3.7-flash": 1_000_000,
    "glm-5.2": 1_000_000,
    "qwen3.6-plus": 1_000_000,
    "qwen3.6-flash": 1_000_000,
    "qwen3.5-plus": 1_000_000,
    "qwen3.5-flash": 1_000_000,
    "kimi-k3": 1_000_000,
    "kimi-k2.7-code": 262_144,
    "kimi-k2.6": 262_144,
    "kimi-k2.5": 262_144,
}


_DEFAULT_PRICE = ModelPrice(
    input_price=0.002,
    cached_input_price=0.002,
    output_price=0.008,
    context_window=128_000,
)


def get_price(model: str, provider: str = "") -> ModelPrice:
    provider = provider.strip().lower()
    model = model.strip().lower()
    if provider == "deepseek":
        return DEEPSEEK_OFFICIAL_PRICING.get(model, PRICING.get(model, _DEFAULT_PRICE))
    if provider == "bailian":
        return BAILIAN_DEEPSEEK_PRICING.get(model, PRICING.get(model, _DEFAULT_PRICE))
    return PRICING.get(model, _DEFAULT_PRICE)


def context_window_for(model: str) -> int | None:
    return CONTEXT_WINDOWS.get(model.strip().lower())


@dataclass
class ModelUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    provider: str = ""
    model: str = ""
    cost: float | None = None


@dataclass
class ServiceUsage:
    calls: int = 0
    unit_price: float = 0.0
    cost: float = 0.0


@dataclass(frozen=True)
class CostBreakdownItem:
    kind: str
    name: str
    cost: float
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0


def _sum_usage_attr(usages: dict[str, ModelUsage], attr: str) -> int:
    return sum(getattr(u, attr) for u in usages.values())


@dataclass
class TokenTracker:
    _llm_usage: dict[str, ModelUsage] = field(default_factory=dict)
    _vision_usage: dict[str, ModelUsage] = field(default_factory=dict)
    _service_usage: dict[str, ServiceUsage] = field(default_factory=dict)
    current_prompt_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return _sum_usage_attr(self._llm_usage, "prompt_tokens")

    @property
    def completion_tokens(self) -> int:
        return _sum_usage_attr(self._llm_usage, "completion_tokens")

    @property
    def cached_tokens(self) -> int:
        return _sum_usage_attr(self._llm_usage, "cached_tokens")

    @property
    def vision_prompt_tokens(self) -> int:
        return _sum_usage_attr(self._vision_usage, "prompt_tokens")

    @property
    def vision_completion_tokens(self) -> int:
        return _sum_usage_attr(self._vision_usage, "completion_tokens")

    @property
    def vision_cached_tokens(self) -> int:
        return _sum_usage_attr(self._vision_usage, "cached_tokens")

    @property
    def total_tokens(self) -> int:
        return (
            self.prompt_tokens
            + self.completion_tokens
            + self.vision_prompt_tokens
            + self.vision_completion_tokens
        )

    @property
    def llm_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def vision_tokens(self) -> int:
        return self.vision_prompt_tokens + self.vision_completion_tokens

    def add_llm(
        self,
        usage: dict | None,
        model: str,
        provider: str = "",
        occurred_at: datetime | None = None,
    ) -> None:
        if not usage:
            return
        current_prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        details = usage.get("prompt_tokens_details")
        cached = (
            details.get("cached_tokens", usage.get("prompt_cache_hit_tokens", 0))
            if isinstance(details, dict)
            else usage.get("prompt_cache_hit_tokens", 0)
        )

        key = _model_usage_key(provider, model)
        entry = self._llm_usage.setdefault(
            key,
            ModelUsage(provider=provider, model=model, cost=0.0),
        )
        entry.prompt_tokens += current_prompt
        entry.completion_tokens += completion
        entry.cached_tokens += cached
        entry.cost = (entry.cost or 0.0) + _usage_increment_cost(
            model,
            provider,
            current_prompt,
            completion,
            cached,
            occurred_at=occurred_at,
        )
        self.current_prompt_tokens = current_prompt

    def add_vision(self, usage: dict | None, model: str, provider: str = "") -> None:
        if not usage:
            return
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0

        key = _model_usage_key(provider, model)
        entry = self._vision_usage.setdefault(
            key,
            ModelUsage(provider=provider, model=model, cost=0.0),
        )
        entry.prompt_tokens += prompt
        entry.completion_tokens += completion
        entry.cached_tokens += cached
        entry.cost = (entry.cost or 0.0) + _usage_increment_cost(
            model,
            provider,
            prompt,
            completion,
            cached,
        )

    def add_service(self, service: str, *, calls: int = 1, unit_price: float) -> None:
        """Record a per-call billable service in CNY."""
        if calls <= 0:
            return
        entry = self._service_usage.setdefault(
            service,
            ServiceUsage(unit_price=unit_price),
        )
        entry.calls += calls
        entry.unit_price = unit_price
        entry.cost += calls * unit_price

    @property
    def service_cost(self) -> float:
        return sum(u.cost for u in self._service_usage.values())

    def cost_breakdown(self) -> list[CostBreakdownItem]:
        """Return all billable session items, sorted by descending spend."""
        items: list[CostBreakdownItem] = []
        for kind, usages in (
            ("llm", self._llm_usage),
            ("vision", self._vision_usage),
        ):
            for key, usage in usages.items():
                model = usage.model or key
                items.append(
                    CostBreakdownItem(
                        kind=kind,
                        name=model,
                        provider=usage.provider,
                        cost=(
                            usage.cost
                            if usage.cost is not None
                            else _model_usage_cost(model, usage, usage.provider)
                        ),
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        cached_tokens=usage.cached_tokens,
                    )
                )
        for service, usage in self._service_usage.items():
            items.append(
                CostBreakdownItem(
                    kind="service",
                    name=service,
                    cost=usage.cost,
                    calls=usage.calls,
                )
            )
        return sorted(items, key=lambda item: (-item.cost, item.kind, item.name))

    def total_cost(self) -> float:
        return sum(item.cost for item in self.cost_breakdown())

    def cache_ratio(self) -> float:
        if self.prompt_tokens == 0:
            return 0
        return self.cached_tokens / self.prompt_tokens

    def to_dict(self) -> dict:
        return {
            "llm_usage": {
                model: {
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "cached_tokens": u.cached_tokens,
                    "provider": u.provider,
                    "model": u.model,
                    "cost": u.cost,
                }
                for model, u in self._llm_usage.items()
            },
            "vision_usage": {
                model: {
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "cached_tokens": u.cached_tokens,
                    "provider": u.provider,
                    "model": u.model,
                    "cost": u.cost,
                }
                for model, u in self._vision_usage.items()
            },
            "service_usage": {
                service: {
                    "calls": u.calls,
                    "unit_price": u.unit_price,
                    "cost": u.cost,
                }
                for service, u in self._service_usage.items()
            },
            "current_prompt_tokens": self.current_prompt_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TokenTracker":
        llm_usage = {}
        for key, u in d.get("llm_usage", {}).items():
            llm_usage[key] = ModelUsage(
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                cached_tokens=u.get("cached_tokens", 0),
                provider=u.get("provider", ""),
                model=u.get("model", key),
                cost=u.get("cost"),
            )
        vision_usage = {}
        for key, u in d.get("vision_usage", {}).items():
            vision_usage[key] = ModelUsage(
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                cached_tokens=u.get("cached_tokens", 0),
                provider=u.get("provider", ""),
                model=u.get("model", key),
                cost=u.get("cost"),
            )
        service_usage = {}
        for service, u in d.get("service_usage", {}).items():
            service_usage[service] = ServiceUsage(
                calls=u.get("calls", 0),
                unit_price=u.get("unit_price", 0.0),
                cost=u.get(
                    "cost",
                    u.get("calls", 0) * u.get("unit_price", 0.0),
                ),
            )
        return cls(
            _llm_usage=llm_usage,
            _vision_usage=vision_usage,
            _service_usage=service_usage,
            current_prompt_tokens=d.get("current_prompt_tokens", 0),
        )

    def copy(self) -> "TokenTracker":
        return TokenTracker(
            _llm_usage={
                model: ModelUsage(
                    prompt_tokens=u.prompt_tokens,
                    completion_tokens=u.completion_tokens,
                    cached_tokens=u.cached_tokens,
                    provider=u.provider,
                    model=u.model,
                    cost=u.cost,
                )
                for model, u in self._llm_usage.items()
            },
            _vision_usage={
                model: ModelUsage(
                    prompt_tokens=u.prompt_tokens,
                    completion_tokens=u.completion_tokens,
                    cached_tokens=u.cached_tokens,
                    provider=u.provider,
                    model=u.model,
                    cost=u.cost,
                )
                for model, u in self._vision_usage.items()
            },
            _service_usage={
                service: ServiceUsage(
                    calls=u.calls,
                    unit_price=u.unit_price,
                    cost=u.cost,
                )
                for service, u in self._service_usage.items()
            },
            current_prompt_tokens=self.current_prompt_tokens,
        )


_CURRENT_TRACKER: contextvars.ContextVar[TokenTracker | None] = contextvars.ContextVar(
    "aero_cost_tracker",
    default=None,
)


@contextmanager
def use_token_tracker(tracker: TokenTracker):
    token = _CURRENT_TRACKER.set(tracker)
    try:
        yield
    finally:
        _CURRENT_TRACKER.reset(token)


def record_service_cost(service: str, *, calls: int = 1, unit_price: float) -> None:
    """Charge the tracker associated with the currently executing agent tool."""
    tracker = _CURRENT_TRACKER.get()
    if tracker is not None:
        tracker.add_service(service, calls=calls, unit_price=unit_price)


def _model_usage_cost(model: str, usage: ModelUsage, provider: str = "") -> float:
    price = get_price(model, provider)
    non_cached = usage.prompt_tokens - usage.cached_tokens
    return (
        non_cached * price.input_price
        + usage.cached_tokens * price.cached_input_price
        + usage.completion_tokens * price.output_price
    ) / 1000


def _model_usage_key(provider: str, model: str) -> str:
    return f"{provider.strip().lower()}::{model.strip().lower()}" if provider else model


def _usage_increment_cost(
    model: str,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    *,
    occurred_at: datetime | None = None,
) -> float:
    price = get_price(model, provider)
    multiplier = deepseek_official_price_multiplier(provider, occurred_at)
    non_cached = prompt_tokens - cached_tokens
    return (
        multiplier
        * (
            non_cached * price.input_price
            + cached_tokens * price.cached_input_price
            + completion_tokens * price.output_price
        )
        / 1000
    )


def deepseek_official_price_multiplier(
    provider: str,
    occurred_at: datetime | None = None,
) -> float:
    """Return the official DeepSeek V4 peak multiplier in Beijing time."""
    if provider.strip().lower() != "deepseek":
        return 1.0
    moment = occurred_at or datetime.now(tz=ZoneInfo("Asia/Shanghai"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    local = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    minute = local.hour * 60 + local.minute
    is_peak = 9 * 60 <= minute < 12 * 60 or 14 * 60 <= minute < 18 * 60
    return 2.0 if is_peak else 1.0


def format_token_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_cost(n: float) -> str:
    return f"¥{n:.2f}"
