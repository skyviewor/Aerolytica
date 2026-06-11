"""Model pricing and TokenTracker for Meteora.

All prices in CNY per 1K tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelPrice:
    input_price: float
    cached_input_price: float
    output_price: float
    context_window: int


PRICING: dict[str, ModelPrice] = {
    # --- LLM ---
    # DeepSeek (USD → CNY at ~7.2 rate, per 1K tokens)
    "deepseek-v4-flash": ModelPrice(
        input_price=0.001008, cached_input_price=0.000020, output_price=0.002016,
        context_window=1_000_000,
    ),
    "deepseek-v4-pro": ModelPrice(
        input_price=0.003132, cached_input_price=0.000026, output_price=0.006264,
        context_window=1_000_000,
    ),
    "deepseek-chat": ModelPrice(
        input_price=0.001008, cached_input_price=0.000020, output_price=0.002016,
        context_window=1_000_000,
    ),
    "deepseek-reasoner": ModelPrice(
        input_price=0.001008, cached_input_price=0.000020, output_price=0.002016,
        context_window=1_000_000,
    ),
    # Kimi (CNY per 1K tokens)
    "kimi-k2.6": ModelPrice(
        input_price=0.0065, cached_input_price=0.0011, output_price=0.027,
        context_window=262_144,
    ),
    "kimi-k2.5": ModelPrice(
        input_price=0.004, cached_input_price=0.0007, output_price=0.021,
        context_window=262_144,
    ),
    "kimi-k2-thinking": ModelPrice(
        input_price=0.004, cached_input_price=0.0007, output_price=0.021,
        context_window=262_144,
    ),
    "kimi-k2-0905-preview": ModelPrice(
        input_price=0.004, cached_input_price=0.0007, output_price=0.021,
        context_window=262_144,
    ),
    "moonshot-v1-128k": ModelPrice(
        input_price=0.01, cached_input_price=0.01, output_price=0.03,
        context_window=131_072,
    ),
    "moonshot-v1-32k": ModelPrice(
        input_price=0.005, cached_input_price=0.005, output_price=0.02,
        context_window=32_768,
    ),
    # Qwen / Bailian (CNY per 1K tokens)
    "qwen3.7": ModelPrice(
        input_price=0.02, cached_input_price=0.02, output_price=0.06,
        context_window=131_072,
    ),
    "qwen-plus": ModelPrice(
        input_price=0.002, cached_input_price=0.002, output_price=0.006,
        context_window=131_072,
    ),
    "qwen-max": ModelPrice(
        input_price=0.02, cached_input_price=0.02, output_price=0.06,
        context_window=32_768,
    ),
    "qwen-turbo": ModelPrice(
        input_price=0.0005, cached_input_price=0.0005, output_price=0.0015,
        context_window=131_072,
    ),
    "qwen-long": ModelPrice(
        input_price=0.0005, cached_input_price=0.0005, output_price=0.0015,
        context_window=1_000_000,
    ),
    "qwen3-max": ModelPrice(
        input_price=0.008, cached_input_price=0.008, output_price=0.024,
        context_window=131_072,
    ),
    "qwen3-plus": ModelPrice(
        input_price=0.0035, cached_input_price=0.0035, output_price=0.0105,
        context_window=131_072,
    ),
    # OpenAI (USD → CNY, per 1K tokens; cache pricing from prompt caching docs)
    "gpt-4o": ModelPrice(
        input_price=0.018, cached_input_price=0.009, output_price=0.072,
        context_window=128_000,
    ),
    "gpt-4o-mini": ModelPrice(
        input_price=0.00108, cached_input_price=0.00054, output_price=0.00432,
        context_window=128_000,
    ),
    # --- Vision ---
    "qwen3-vl-plus": ModelPrice(
        input_price=0.003, cached_input_price=0.003, output_price=0.012,
        context_window=32_768,
    ),
    "qwen3-vl-flash": ModelPrice(
        input_price=0.0015, cached_input_price=0.0015, output_price=0.006,
        context_window=32_768,
    ),
    "qwen-vl-max": ModelPrice(
        input_price=0.003, cached_input_price=0.003, output_price=0.012,
        context_window=32_768,
    ),
    "qwen-vl-plus": ModelPrice(
        input_price=0.0015, cached_input_price=0.0015, output_price=0.006,
        context_window=32_768,
    ),
    "qwen3.5-flash": ModelPrice(
        input_price=0.0015, cached_input_price=0.0015, output_price=0.006,
        context_window=32_768,
    ),
    "qwen3.5-plus": ModelPrice(
        input_price=0.003, cached_input_price=0.003, output_price=0.012,
        context_window=32_768,
    ),
    "qwen3.6-flash": ModelPrice(
        input_price=0.0015, cached_input_price=0.0015, output_price=0.006,
        context_window=32_768,
    ),
    "qwen3.6-plus": ModelPrice(
        input_price=0.003, cached_input_price=0.003, output_price=0.012,
        context_window=32_768,
    ),
    "qwen3.7-plus": ModelPrice(
        input_price=0.003, cached_input_price=0.003, output_price=0.012,
        context_window=32_768,
    ),
}


_DEFAULT_PRICE = ModelPrice(
    input_price=0.002, cached_input_price=0.002, output_price=0.008,
    context_window=128_000,
)


def get_price(model: str) -> ModelPrice:
    return PRICING.get(model, _DEFAULT_PRICE)


def context_window_for(model: str) -> int:
    return get_price(model).context_window


@dataclass
class TokenTracker:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    vision_prompt_tokens: int = 0
    vision_completion_tokens: int = 0
    vision_cached_tokens: int = 0
    current_prompt_tokens: int = 0

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

    def add_llm(self, usage: dict | None) -> None:
        if not usage:
            return
        current_prompt = usage.get("prompt_tokens", 0)
        self.prompt_tokens += current_prompt
        self.completion_tokens += usage.get("completion_tokens", 0)
        self.current_prompt_tokens = current_prompt
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            self.cached_tokens += details.get("cached_tokens", 0)

    def add_vision(self, usage: dict | None) -> None:
        if not usage:
            return
        self.vision_prompt_tokens += usage.get("prompt_tokens", 0)
        self.vision_completion_tokens += usage.get("completion_tokens", 0)
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            self.vision_cached_tokens += details.get("cached_tokens", 0)

    def llm_cost(self, model: str) -> float:
        price = get_price(model)
        non_cached = self.prompt_tokens - self.cached_tokens
        return (
            non_cached * price.input_price
            + self.cached_tokens * price.cached_input_price
            + self.completion_tokens * price.output_price
        ) / 1000

    def vision_cost(self, model: str) -> float:
        price = get_price(model)
        non_cached = self.vision_prompt_tokens - self.vision_cached_tokens
        return (
            non_cached * price.input_price
            + self.vision_cached_tokens * price.cached_input_price
            + self.vision_completion_tokens * price.output_price
        ) / 1000

    def total_cost(self, llm_model: str, vision_model: str) -> float:
        return self.llm_cost(llm_model) + self.vision_cost(vision_model)

    def cache_ratio(self) -> float:
        if self.prompt_tokens == 0:
            return 0
        return self.cached_tokens / self.prompt_tokens

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "vision_prompt_tokens": self.vision_prompt_tokens,
            "vision_completion_tokens": self.vision_completion_tokens,
            "vision_cached_tokens": self.vision_cached_tokens,
            "current_prompt_tokens": self.current_prompt_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TokenTracker":
        return cls(
            prompt_tokens=d.get("prompt_tokens", 0),
            completion_tokens=d.get("completion_tokens", 0),
            cached_tokens=d.get("cached_tokens", 0),
            vision_prompt_tokens=d.get("vision_prompt_tokens", 0),
            vision_completion_tokens=d.get("vision_completion_tokens", 0),
            vision_cached_tokens=d.get("vision_cached_tokens", 0),
            current_prompt_tokens=d.get("current_prompt_tokens", 0),
        )

    def copy(self) -> "TokenTracker":
        return TokenTracker(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cached_tokens=self.cached_tokens,
            vision_prompt_tokens=self.vision_prompt_tokens,
            vision_completion_tokens=self.vision_completion_tokens,
            vision_cached_tokens=self.vision_cached_tokens,
            current_prompt_tokens=self.current_prompt_tokens,
        )


def format_token_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_cost(n: float) -> str:
    return f"¥{n:.2f}"
