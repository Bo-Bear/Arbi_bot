"""N-leg parallel order executor for multi-outcome arbitrage.

Handles:
- Parallel FOK order placement across all outcome legs
- Fill monitoring and status tracking
- Partial fill detection and unwind logic
- Paper trading simulation
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Optional

from config import (
    EXEC_MODE, LIVE_PRICE_BUFFER, ORDER_TIMEOUT_S,
)
from models import (
    ArbitrageOpportunity, LegFill, ExecutionResult, OutcomeQuote,
)
from polymarket.clob import place_order, poll_order_status, sell_position

logger = logging.getLogger(__name__)


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _execute_single_leg_paper(
    quote: OutcomeQuote, size: float
) -> LegFill:
    """Simulate a fill in paper mode."""
    return LegFill(
        token_id=quote.token_id,
        outcome_name=quote.outcome_name,
        planned_price=quote.best_ask_price,
        actual_price=quote.best_ask_price,
        planned_size=size,
        filled_size=size,
        order_id=f"paper-{int(time.time() * 1000)}",
        fill_ts=_utc_ts(),
        latency_ms=0.0,
        status="filled",
    )


def _execute_single_leg_live(
    quote: OutcomeQuote, size: float
) -> LegFill:
    """Execute a single leg in live mode using FOK orders.

    Places a FOK (Fill or Kill) order at the best ask + buffer.
    If FOK is not supported, falls back to GTC with tight timeout.
    """
    t0 = time.monotonic()

    # Price with buffer for better fill probability
    limit_price = min(round(quote.best_ask_price + LIVE_PRICE_BUFFER, 2), 0.99)

    # Try FOK first (atomic: fills completely or cancels)
    result = place_order(
        token_id=quote.token_id,
        price=limit_price,
        size=size,
        side="BUY",
        order_type="FOK",
    )

    if not result["success"]:
        # FOK might not be supported — fall back to GTC
        error_msg = result.get("error", "")
        if "FOK" in str(error_msg).upper() or "unsupported" in str(error_msg).lower():
            logger.info(
                "FOK not supported for %s, falling back to GTC",
                quote.outcome_name,
            )
            result = place_order(
                token_id=quote.token_id,
                price=limit_price,
                size=size,
                side="BUY",
                order_type="GTC",
            )

    latency = (time.monotonic() - t0) * 1000

    if not result["success"]:
        return LegFill(
            token_id=quote.token_id,
            outcome_name=quote.outcome_name,
            planned_price=quote.best_ask_price,
            actual_price=None,
            planned_size=size,
            filled_size=0.0,
            order_id=result.get("order_id"),
            fill_ts=None,
            latency_ms=latency,
            status="rejected",
            error=result.get("error"),
        )

    order_id = result["order_id"]

    # For FOK, the order is either filled immediately or canceled
    # For GTC, we need to poll
    status, filled_size, avg_price = poll_order_status(
        order_id, timeout=ORDER_TIMEOUT_S
    )

    latency = (time.monotonic() - t0) * 1000

    return LegFill(
        token_id=quote.token_id,
        outcome_name=quote.outcome_name,
        planned_price=quote.best_ask_price,
        actual_price=avg_price if filled_size > 0 else None,
        planned_size=size,
        filled_size=filled_size,
        order_id=order_id,
        fill_ts=_utc_ts() if filled_size > 0 else None,
        latency_ms=latency,
        status=status,
        error=None if status == "filled" else f"status: {status}",
    )


def execute_opportunity(
    opportunity: ArbitrageOpportunity,
    size: Optional[float] = None,
) -> ExecutionResult:
    """Execute all legs of a multi-outcome arbitrage opportunity.

    Places orders on all N outcomes in parallel. For paper mode,
    simulates instant fills. For live mode, uses FOK orders.

    Args:
        opportunity: The detected arbitrage opportunity
        size: Override the executable size (default: use opportunity.executable_size)

    Returns:
        ExecutionResult with fill details for each leg
    """
    exec_size = size or opportunity.executable_size
    # Floor to integer contracts
    exec_size = float(int(exec_size))
    if exec_size < 1:
        exec_size = 1.0

    t0 = time.monotonic()
    is_paper = (EXEC_MODE == "paper")

    logger.info(
        "Executing %d-leg arb on '%s' | size=%.0f | cost=$%.4f | profit=%.2f%%",
        len(opportunity.quotes), opportunity.event_title[:40],
        exec_size, opportunity.total_cost, opportunity.profit_pct,
    )

    if is_paper:
        # Paper mode: instant simulated fills
        fills = [
            _execute_single_leg_paper(quote, exec_size)
            for quote in opportunity.quotes
        ]
    else:
        # Live mode: parallel FOK orders across all legs
        fills: List[LegFill] = []
        max_workers = min(len(opportunity.quotes), 10)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _execute_single_leg_live, quote, exec_size
                ): quote
                for quote in opportunity.quotes
            }
            for future in as_completed(futures):
                try:
                    fill = future.result(timeout=ORDER_TIMEOUT_S + 5)
                except Exception as e:
                    quote = futures[future]
                    fill = LegFill(
                        token_id=quote.token_id,
                        outcome_name=quote.outcome_name,
                        planned_price=quote.best_ask_price,
                        actual_price=None,
                        planned_size=exec_size,
                        filled_size=0.0,
                        order_id=None,
                        fill_ts=None,
                        latency_ms=(time.monotonic() - t0) * 1000,
                        status="error",
                        error=str(e),
                    )
                fills.append(fill)

    total_latency = (time.monotonic() - t0) * 1000

    # Compute aggregate results
    filled_legs = [f for f in fills if f.status == "filled"]
    failed_legs = [f for f in fills if f.status != "filled"]
    all_filled = len(failed_legs) == 0

    total_cost_actual = sum(
        (f.actual_price or f.planned_price) for f in fills
    )
    min_filled_size = (
        min(f.filled_size for f in filled_legs) if filled_legs else 0.0
    )

    result = ExecutionResult(
        opportunity=opportunity,
        leg_fills=fills,
        total_latency_ms=total_latency,
        all_filled=all_filled,
        total_cost_actual=total_cost_actual,
        total_filled_size=min_filled_size,
        num_legs_filled=len(filled_legs),
        num_legs_failed=len(failed_legs),
    )

    # Handle partial execution (some legs filled, some failed)
    if not all_filled and filled_legs and not is_paper:
        _handle_partial_execution(result)

    return result


def _handle_partial_execution(result: ExecutionResult):
    """Unwind filled legs when not all legs filled.

    This is the critical risk management function. If we bought
    some outcomes but not all, we have directional exposure instead
    of a riskless arb. We sell back the filled legs to close out.
    """
    filled = [f for f in result.leg_fills if f.status == "filled" and f.filled_size > 0]
    failed = [f for f in result.leg_fills if f.status != "filled"]

    logger.warning(
        "PARTIAL EXECUTION: %d/%d legs filled, %d failed — unwinding",
        len(filled), len(result.leg_fills), len(failed),
    )

    for leg in filled:
        try:
            logger.info(
                "Unwinding %s: selling %.0f @ ~$%.2f",
                leg.outcome_name, leg.filled_size,
                (leg.actual_price or leg.planned_price),
            )
            unwind_result = sell_position(
                token_id=leg.token_id,
                size=leg.filled_size,
                price=leg.actual_price or leg.planned_price,
            )
            if unwind_result["success"]:
                logger.info(
                    "Unwind submitted for %s (order %s)",
                    leg.outcome_name, unwind_result.get("order_id"),
                )
            else:
                logger.error(
                    "Unwind FAILED for %s: %s",
                    leg.outcome_name, unwind_result.get("error"),
                )
        except Exception as e:
            logger.error(
                "Unwind ERROR for %s: %s — MANUAL INTERVENTION MAY BE NEEDED",
                leg.outcome_name, e,
            )

    # Log failed legs for debugging
    for leg in failed:
        logger.error(
            "Failed leg: %s | status=%s | error=%s",
            leg.outcome_name, leg.status, leg.error,
        )
