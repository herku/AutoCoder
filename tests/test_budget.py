from autocoder.budget import BudgetTracker
from autocoder.types import AgentResult


def _make_result(tokens_in=1000, tokens_out=500, cost=0.01):
    return AgentResult(
        session_id="test",
        result_text="ok",
        is_error=False,
        duration_ms=100,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_cached=0,
        cost_usd=cost,
        num_turns=1,
        model="sonnet",
    )


def test_record_and_summary():
    bt = BudgetTracker(per_issue_token_budget=10_000, daily_cap_tokens=100_000)
    bt.record(_make_result(tokens_in=5000, tokens_out=2000, cost=0.05))
    s = bt.summary()
    assert s["daily_total_tokens"] == 7000
    assert s["daily_total_cost_usd"] == 0.05


def test_daily_exhausted():
    bt = BudgetTracker(per_issue_token_budget=10_000, daily_cap_tokens=5000)
    assert not bt.daily_exhausted()
    bt.record(_make_result(tokens_in=3000, tokens_out=3000))
    assert bt.daily_exhausted()


def test_remaining_for_issue_usd():
    bt = BudgetTracker(per_issue_token_budget=500_000, daily_cap_tokens=5_000_000)
    usd = bt.remaining_for_issue_usd("sonnet")
    assert usd > 0
    # After recording tokens, remaining should decrease
    bt.record(_make_result(tokens_in=400_000, tokens_out=50_000))
    usd2 = bt.remaining_for_issue_usd("sonnet")
    assert usd2 < usd


def test_reset_issue():
    bt = BudgetTracker(per_issue_token_budget=10_000, daily_cap_tokens=100_000)
    bt.record(_make_result(tokens_in=5000, tokens_out=5000))
    bt.reset_issue()
    # Issue tokens reset, but daily total should remain
    usd = bt.remaining_for_issue_usd("sonnet")
    assert usd > 0
    assert bt.summary()["daily_total_tokens"] == 10_000


def test_issue_exhausted_transitions():
    bt = BudgetTracker(per_issue_token_budget=10_000, daily_cap_tokens=100_000)
    assert not bt.issue_exhausted()
    bt.record(_make_result(tokens_in=6000, tokens_out=4000))
    assert bt.issue_exhausted()
    bt.reset_issue()
    assert not bt.issue_exhausted()


def test_issue_tokens_used():
    bt = BudgetTracker(per_issue_token_budget=10_000, daily_cap_tokens=100_000)
    bt.record(_make_result(tokens_in=1000, tokens_out=500))
    assert bt.issue_tokens_used == 1500
