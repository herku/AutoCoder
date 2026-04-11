from __future__ import annotations

from autocoder.types import AgentResult

# Approximate pricing per million tokens (USD)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "sonnet": (3.0, 15.0),     # input, output
    "opus": (15.0, 75.0),
    "haiku": (0.25, 1.25),
}


def model_family_name(model: str) -> str:
    """Extract model family name for pricing lookup."""
    lower = model.lower()
    for family in MODEL_PRICING:
        if family in lower:
            return family
    return model


class BudgetTracker:
    def __init__(self, per_issue_token_budget: int, daily_cap_tokens: int):
        self.per_issue_token_budget = per_issue_token_budget
        self.daily_cap_tokens = daily_cap_tokens
        self._daily_total_tokens: int = 0
        self._daily_total_cost: float = 0.0
        self._issue_tokens: int = 0

    def record(self, result: AgentResult) -> None:
        tokens = result.tokens_in + result.tokens_out
        self._daily_total_tokens += tokens
        self._daily_total_cost += result.cost_usd
        self._issue_tokens += tokens

    def daily_exhausted(self) -> bool:
        return self._daily_total_tokens >= self.daily_cap_tokens

    def reset_issue(self) -> None:
        self._issue_tokens = 0

    def remaining_for_issue_usd(self, model: str = "sonnet") -> float:
        remaining_tokens = self.per_issue_token_budget - self._issue_tokens
        if remaining_tokens <= 0:
            return 0.01  # minimum to let claude report budget exceeded
        family = model_family_name(model)
        input_price, output_price = MODEL_PRICING.get(family, (3.0, 15.0))
        # Assume 60% input, 40% output ratio
        estimated_cost = (remaining_tokens * 0.6 * input_price + remaining_tokens * 0.4 * output_price) / 1_000_000
        return round(max(estimated_cost, 0.01), 2)

    @property
    def daily_tokens_used(self) -> int:
        return self._daily_total_tokens

    def summary(self) -> dict:
        return {
            "daily_total_tokens": self._daily_total_tokens,
            "daily_total_cost_usd": round(self._daily_total_cost, 4),
        }
