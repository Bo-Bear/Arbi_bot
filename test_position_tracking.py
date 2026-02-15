"""Tests for position tracking and per-event budget enforcement."""

import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from models import MultiOutcomeEvent, OutcomeToken, OutcomeQuote
from scanner import detect_arbitrage, scan_for_opportunities


def _make_event(event_id="evt_1", title="Test Event", n_outcomes=3):
    outcomes = [
        OutcomeToken(
            token_id=f"tok_{i}",
            outcome_name=f"Outcome {i}",
            market_id=f"mkt_{i}",
            question=f"Will outcome {i} win?",
        )
        for i in range(n_outcomes)
    ]
    return MultiOutcomeEvent(
        event_id=event_id,
        title=title,
        slug="test-event",
        outcomes=outcomes,
    )


def _make_quotes(n=3, price=0.30, size=100.0):
    """Create N quotes that sum to < $1 (an arb opportunity)."""
    return [
        OutcomeQuote(
            token_id=f"tok_{i}",
            outcome_name=f"Outcome {i}",
            market_id=f"mkt_{i}",
            best_ask_price=price,
            available_size=size,
        )
        for i in range(n)
    ]


# ---------- detect_arbitrage with remaining_budget ----------


def test_detect_arb_no_budget_param_uses_default():
    """Without remaining_budget, uses MAX_POSITION_COST (default $200)."""
    event = _make_event()
    # 3 outcomes at $0.30 each = $0.90 cost, 10% profit
    quotes = _make_quotes(3, price=0.30, size=1000.0)
    opp = detect_arbitrage(event, quotes)
    assert opp is not None
    # Default MAX_POSITION_COST=200, max_shares = 200/0.90 = 222.2
    assert opp.executable_size <= 222.3


def test_detect_arb_with_full_budget():
    """With remaining_budget=200, same as default."""
    event = _make_event()
    quotes = _make_quotes(3, price=0.30, size=1000.0)
    opp = detect_arbitrage(event, quotes, remaining_budget=200.0)
    assert opp is not None
    assert opp.executable_size <= 222.3


def test_detect_arb_with_reduced_budget():
    """With remaining_budget=50, executable_size is capped lower."""
    event = _make_event()
    # total_cost = 0.90, max_shares = 50/0.90 = 55.5
    quotes = _make_quotes(3, price=0.30, size=1000.0)
    opp = detect_arbitrage(event, quotes, remaining_budget=50.0)
    assert opp is not None
    assert opp.executable_size <= 55.6
    assert opp.executable_size >= 55.0


def test_detect_arb_budget_exhausted():
    """With remaining_budget=0, returns None (budget exhausted)."""
    event = _make_event()
    quotes = _make_quotes(3, price=0.30, size=1000.0)
    opp = detect_arbitrage(event, quotes, remaining_budget=0.0)
    assert opp is None


def test_detect_arb_budget_negative():
    """With remaining_budget<0, returns None."""
    event = _make_event()
    quotes = _make_quotes(3, price=0.30, size=1000.0)
    opp = detect_arbitrage(event, quotes, remaining_budget=-10.0)
    assert opp is None


def test_detect_arb_budget_too_small_for_min_size():
    """Budget too small to meet MIN_EXECUTABLE_SIZE returns None."""
    event = _make_event()
    # total_cost = 0.90, budget=4 -> max_shares = 4.4 < MIN_EXECUTABLE_SIZE(5)
    quotes = _make_quotes(3, price=0.30, size=1000.0)
    opp = detect_arbitrage(event, quotes, remaining_budget=4.0)
    assert opp is None


# ---------- scan_for_opportunities with position_costs ----------


def test_scan_skips_exhausted_events(monkeypatch):
    """Events with costs >= MAX_POSITION_COST are skipped entirely."""
    event = _make_event(event_id="evt_1")
    quotes = _make_quotes(3, price=0.30, size=100.0)

    # Mock quote_event to return our test quotes
    call_count = 0

    def mock_quote_event(e, ws_feed=None):
        nonlocal call_count
        call_count += 1
        return quotes

    monkeypatch.setattr("scanner.quote_event", mock_quote_event)

    # Event already at max budget -> should be skipped (no quote_event call)
    position_costs = {"evt_1": 200.0}
    opps = scan_for_opportunities([event], ws_feed=None, position_costs=position_costs)
    assert len(opps) == 0
    assert call_count == 0  # Should not have fetched quotes


def test_scan_passes_remaining_budget(monkeypatch):
    """Partial budget is passed through to detect_arbitrage."""
    event = _make_event(event_id="evt_1")
    quotes = _make_quotes(3, price=0.30, size=1000.0)

    def mock_quote_event(e, ws_feed=None):
        return quotes

    monkeypatch.setattr("scanner.quote_event", mock_quote_event)

    # $150 already spent, $50 remaining
    position_costs = {"evt_1": 150.0}
    opps = scan_for_opportunities([event], ws_feed=None, position_costs=position_costs)
    assert len(opps) == 1
    # max_shares = 50/0.90 = 55.5
    assert opps[0].executable_size <= 55.6


def test_scan_no_position_costs_uses_full_budget(monkeypatch):
    """Without position_costs, full budget is available."""
    event = _make_event(event_id="evt_1")
    quotes = _make_quotes(3, price=0.30, size=1000.0)

    def mock_quote_event(e, ws_feed=None):
        return quotes

    monkeypatch.setattr("scanner.quote_event", mock_quote_event)

    opps = scan_for_opportunities([event], ws_feed=None)
    assert len(opps) == 1
    # max_shares = 200/0.90 = 222.2
    assert opps[0].executable_size <= 222.3


def test_scan_mixed_events(monkeypatch):
    """Mix of exhausted and fresh events — only fresh ones are scanned."""
    evt_exhausted = _make_event(event_id="evt_1", title="Exhausted")
    evt_fresh = _make_event(event_id="evt_2", title="Fresh")

    scanned_ids = []

    def mock_quote_event(e, ws_feed=None):
        scanned_ids.append(e.event_id)
        return _make_quotes(3, price=0.30, size=100.0)

    monkeypatch.setattr("scanner.quote_event", mock_quote_event)

    position_costs = {"evt_1": 200.0}  # evt_1 exhausted
    opps = scan_for_opportunities(
        [evt_exhausted, evt_fresh], ws_feed=None, position_costs=position_costs,
    )
    assert len(opps) == 1
    assert opps[0].event_id == "evt_2"
    assert "evt_1" not in scanned_ids  # Should not have fetched quotes for exhausted event
    assert "evt_2" in scanned_ids


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
