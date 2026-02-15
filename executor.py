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
    MAX_QUOTE_STALENESS_S, ALLOW_GTC_FALLBACK, UNWIND_TIMEOUT_S,
)
from models import (
    ArbitrageOpportunity, LegFill, ExecutionResult, OutcomeQuote,
)
from polymarket.clob import place_order, poll_order_status, sell_position
from polymarket.websocket_feed import OrderbookFeed

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
    GTC fallback is only used when ALLOW_GTC_FALLBACK is True.
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
        error_msg = result.get("error", "")
        is_fok_unsupported = (
            "FOK" in str(error_msg).upper()
            or "unsupported" in str(error_msg).lower()
        )
        if is_fok_unsupported and ALLOW_GTC_FALLBACK:
            logger.warning(
                "FOK not supported for %s, falling back to GTC (ALLOW_GTC_FALLBACK=true)",
                quote.outcome_name,
            )
            result = place_order(
                token_id=quote.token_id,
                price=limit_price,
                size=size,
                side="BUY",
                order_type="GTC",
            )
        elif is_fok_unsupported:
            logger.error(
                "FOK not supported for %s and GTC fallback disabled — leg failed",
                quote.outcome_name,
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


def _refresh_quotes(
    opportunity: ArbitrageOpportunity,
    ws_feed: Optional[OrderbookFeed] = None,
) -> Optional[List[OutcomeQuote]]:
    """Re-fetch quotes for all legs immediately before execution.

    Checks WS staleness first. If any quote is too stale (or WS is
    unavailable), falls back to HTTP orderbook fetch.

    Returns updated quotes if the arb still looks valid, None if the
    opportunity has disappeared (total cost >= $1.00).
    """
    from scanner import quote_event
    from models import MultiOutcomeEvent, OutcomeToken

    # Check staleness via WS
    needs_http_refresh = False
    if ws_feed is not None:
        for q in opportunity.quotes:
            staleness = ws_feed.get_staleness(q.token_id)
            if staleness is None or staleness > MAX_QUOTE_STALENESS_S:
                needs_http_refresh = True
                logger.info(
                    "Quote stale for %s (%.1fs) — refreshing via HTTP",
                    q.outcome_name,
                    staleness if staleness is not None else -1,
                )
                break
    else:
        needs_http_refresh = True

    if not needs_http_refresh:
        # WS data is fresh — re-read from WS cache
        refreshed = []
        for q in opportunity.quotes:
            asks = ws_feed.get_asks(q.token_id)
            if asks and len(asks) > 0:
                refreshed.append(OutcomeQuote(
                    token_id=q.token_id,
                    outcome_name=q.outcome_name,
                    market_id=q.market_id,
                    best_ask_price=asks[0][0],
                    available_size=asks[0][1],
                    ask_levels=asks[:10],
                ))
            else:
                needs_http_refresh = True
                break

        if not needs_http_refresh:
            total_cost = sum(q.best_ask_price for q in refreshed)
            if total_cost >= 1.0:
                logger.warning(
                    "Arb vanished after WS refresh: cost=$%.4f >= $1.00",
                    total_cost,
                )
                return None
            return refreshed

    # Fall back to HTTP refresh
    event = MultiOutcomeEvent(
        event_id=opportunity.event_id,
        title=opportunity.event_title,
        slug=opportunity.event_slug,
        outcomes=[
            OutcomeToken(
                token_id=q.token_id,
                outcome_name=q.outcome_name,
                market_id=q.market_id,
                question=q.outcome_name,
            )
            for q in opportunity.quotes
        ],
        neg_risk=opportunity.neg_risk,
    )
    refreshed = quote_event(event, ws_feed=None)  # force HTTP

    total_cost = sum(q.best_ask_price for q in refreshed)
    if total_cost >= 1.0:
        logger.warning(
            "Arb vanished after HTTP refresh: cost=$%.4f >= $1.00",
            total_cost,
        )
        return None

    # Check all legs still have valid asks
    for q in refreshed:
        if q.best_ask_price <= 0 or q.available_size <= 0:
            logger.warning(
                "Leg %s has no asks after refresh — aborting",
                q.outcome_name,
            )
            return None

    return refreshed


def execute_opportunity(
    opportunity: ArbitrageOpportunity,
    size: Optional[float] = None,
    ws_feed: Optional[OrderbookFeed] = None,
) -> ExecutionResult:
    """Execute all legs of a multi-outcome arbitrage opportunity.

    Places orders on all N outcomes in parallel. For paper mode,
    simulates instant fills. For live mode, uses FOK orders.

    Args:
        opportunity: The detected arbitrage opportunity
        size: Override the executable size (default: use opportunity.executable_size)
        ws_feed: WebSocket feed for staleness checks and quote refresh

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

    # Live mode: refresh quotes right before execution to avoid stale prices
    if not is_paper:
        old_cost = opportunity.total_cost
        refresh_t0 = time.monotonic()
        refreshed = _refresh_quotes(opportunity, ws_feed)
        refresh_ms = (time.monotonic() - refresh_t0) * 1000

        if refreshed is None:
            logger.warning(
                "Opportunity vanished on pre-execution refresh (%.0fms) — skipping",
                refresh_ms,
            )
            return ExecutionResult(
                opportunity=opportunity,
                leg_fills=[],
                total_latency_ms=(time.monotonic() - t0) * 1000,
                all_filled=False,
                total_cost_actual=0.0,
                total_filled_size=0.0,
                num_legs_filled=0,
                num_legs_failed=len(opportunity.quotes),
            )

        new_cost = sum(q.best_ask_price for q in refreshed)
        cost_drift = new_cost - old_cost
        logger.info(
            "Quote refresh OK (%.0fms): cost $%.4f -> $%.4f (drift %+.4f)",
            refresh_ms, old_cost, new_cost, cost_drift,
        )
        if abs(cost_drift) > 0.01:
            logger.warning(
                "Significant price drift detected: %+.4f ($%.4f -> $%.4f)",
                cost_drift, old_cost, new_cost,
            )

        # Update the quotes used for execution
        opportunity = ArbitrageOpportunity(
            event_id=opportunity.event_id,
            event_title=opportunity.event_title,
            event_slug=opportunity.event_slug,
            quotes=refreshed,
            total_cost=new_cost,
            profit_per_share=1.0 - new_cost,
            profit_pct=((1.0 - new_cost) / new_cost * 100.0),
            executable_size=min(q.available_size for q in refreshed),
            neg_risk=opportunity.neg_risk,
        )
        # Recompute exec_size with refreshed availability
        exec_size = min(exec_size, float(int(opportunity.executable_size)))
        if exec_size < 1:
            exec_size = 1.0

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

    Sells via GTC orders and polls each for fill confirmation up to
    UNWIND_TIMEOUT_S.  Any legs that remain open after the timeout
    are flagged with a CRITICAL log for manual intervention.
    """
    filled = [f for f in result.leg_fills if f.status == "filled" and f.filled_size > 0]
    failed = [f for f in result.leg_fills if f.status != "filled"]

    logger.warning(
        "PARTIAL EXECUTION: %d/%d legs filled, %d failed — unwinding",
        len(filled), len(result.leg_fills), len(failed),
    )

    # Submit all unwind orders and collect order IDs for polling
    unwind_orders = []  # list of (leg, order_id)
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
                oid = unwind_result.get("order_id")
                logger.info(
                    "Unwind submitted for %s (order %s)",
                    leg.outcome_name, oid,
                )
                unwind_orders.append((leg, oid))
            else:
                logger.error(
                    "Unwind SUBMIT FAILED for %s: %s — MANUAL INTERVENTION NEEDED",
                    leg.outcome_name, unwind_result.get("error"),
                )
        except Exception as e:
            logger.error(
                "Unwind ERROR for %s: %s — MANUAL INTERVENTION NEEDED",
                leg.outcome_name, e,
            )

    # Poll all submitted unwind orders for fill confirmation
    if unwind_orders:
        logger.info(
            "Polling %d unwind orders (timeout=%.0fs)...",
            len(unwind_orders), UNWIND_TIMEOUT_S,
        )
        remaining = list(unwind_orders)
        deadline = time.monotonic() + UNWIND_TIMEOUT_S
        while remaining and time.monotonic() < deadline:
            still_pending = []
            for leg, oid in remaining:
                try:
                    status, filled_size, _ = poll_order_status(oid, timeout=2)
                    if status == "filled":
                        logger.info(
                            "Unwind CONFIRMED for %s (%.0f filled)",
                            leg.outcome_name, filled_size,
                        )
                    elif status in ("canceled", "partial"):
                        logger.error(
                            "Unwind %s for %s (filled=%.0f/%.0f) — MANUAL INTERVENTION NEEDED",
                            status.upper(), leg.outcome_name, filled_size, leg.filled_size,
                        )
                    else:
                        # Still open — check again next loop
                        still_pending.append((leg, oid))
                except Exception as e:
                    logger.debug("Unwind poll error for %s: %s", leg.outcome_name, e)
                    still_pending.append((leg, oid))
            remaining = still_pending
            if remaining:
                time.sleep(1.0)

        # Any orders that never resolved
        for leg, oid in remaining:
            logger.critical(
                "UNWIND TIMEOUT for %s (order %s) — "
                "POSITION STILL OPEN, MANUAL INTERVENTION REQUIRED",
                leg.outcome_name, oid,
            )

    # Log failed legs for debugging
    for leg in failed:
        logger.error(
            "Failed leg: %s | status=%s | error=%s",
            leg.outcome_name, leg.status, leg.error,
        )
