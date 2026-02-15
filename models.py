"""Data models for the multi-outcome arbitrage bot."""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class OutcomeToken:
    """A single outcome token within a multi-outcome event."""
    token_id: str
    outcome_name: str       # e.g., "Sinner", "Alcaraz"
    market_id: str           # conditionId of the parent binary market
    question: str            # e.g., "Will Sinner win?"


@dataclass
class MultiOutcomeEvent:
    """A Polymarket event with multiple mutually exclusive outcomes."""
    event_id: str
    title: str
    slug: str
    outcomes: List[OutcomeToken]
    neg_risk: bool = False

    @property
    def num_outcomes(self) -> int:
        return len(self.outcomes)


@dataclass
class OutcomeQuote:
    """A priced outcome ready for potential trading."""
    token_id: str
    outcome_name: str
    market_id: str
    best_ask_price: float
    available_size: float     # contracts available at best ask
    ask_levels: List[tuple] = field(default_factory=list)  # [(price, size), ...]


@dataclass
class ArbitrageOpportunity:
    """A detected multi-outcome arbitrage opportunity."""
    event_id: str
    event_title: str
    event_slug: str
    quotes: List[OutcomeQuote]
    total_cost: float          # sum of best asks
    profit_per_share: float    # 1.0 - total_cost
    profit_pct: float          # profit_per_share / total_cost * 100
    executable_size: float     # min available size across all legs
    neg_risk: bool = False

    def __str__(self) -> str:
        return (
            f"Arb: {self.event_title} | "
            f"Outcomes: {len(self.quotes)} | "
            f"Cost: ${self.total_cost:.4f} | "
            f"Profit: {self.profit_pct:.2f}% | "
            f"Size: {self.executable_size:.1f}"
        )


@dataclass
class LegFill:
    """Result of executing one leg of a multi-outcome arb."""
    token_id: str
    outcome_name: str
    planned_price: float
    actual_price: Optional[float]
    planned_size: float
    filled_size: float
    order_id: Optional[str]
    fill_ts: Optional[str]      # ISO UTC timestamp
    latency_ms: float
    status: str                  # "filled", "partial", "rejected", "error", "skipped"
    error: Optional[str] = None


@dataclass
class ExecutionResult:
    """Combined result of executing all legs of a multi-outcome arb."""
    opportunity: ArbitrageOpportunity
    leg_fills: List[LegFill]
    total_latency_ms: float
    all_filled: bool
    total_cost_actual: float     # sum of actual fill prices
    total_filled_size: float     # min filled size across legs
    num_legs_filled: int
    num_legs_failed: int
