"""Multi-outcome market scanner and arbitrage detector.

Scans Polymarket events with N mutually exclusive outcomes,
fetches orderbooks, and identifies arbitrage opportunities
where the sum of best asks is less than $1.00.
"""

import logging
from typing import List, Optional

from config import (
    MIN_PROFIT_PCT, MAX_PROFIT_PCT, MIN_EXECUTABLE_SIZE, MAX_POSITION_COST,
)
from models import (
    MultiOutcomeEvent, OutcomeQuote, ArbitrageOpportunity,
)
from polymarket.clob import get_orderbook
from polymarket.websocket_feed import OrderbookFeed

logger = logging.getLogger(__name__)


def _fetch_asks_for_token(
    token_id: str, ws_feed: Optional[OrderbookFeed]
) -> List[tuple]:
    """Get ask-side book for a token, trying WS cache first."""
    if ws_feed is not None:
        cached = ws_feed.get_asks(token_id)
        if cached is not None:
            return cached
    return get_orderbook(token_id)


def quote_event(
    event: MultiOutcomeEvent,
    ws_feed: Optional[OrderbookFeed] = None,
) -> List[OutcomeQuote]:
    """Fetch current prices for all outcomes in a multi-outcome event.

    Returns a list of OutcomeQuote objects, one per outcome.
    Outcomes with no available asks are included with price=0 and size=0.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    token_ids = [o.token_id for o in event.outcomes]
    quotes: List[OutcomeQuote] = []

    # Subscribe all tokens to WS feed for future cache hits
    if ws_feed is not None:
        ws_feed.subscribe(token_ids)

    # Fetch orderbooks in parallel (limited concurrency)
    max_workers = min(len(token_ids), 5)
    asks_map: dict = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_asks_for_token, tid, ws_feed): tid
            for tid in token_ids
        }
        for future in as_completed(futures):
            tid = futures[future]
            try:
                asks_map[tid] = future.result(timeout=20)
            except Exception as e:
                logger.debug("Failed to fetch asks for %s: %s", tid[:20], e)
                asks_map[tid] = []

    # Build quotes
    for outcome in event.outcomes:
        asks = asks_map.get(outcome.token_id, [])
        if asks:
            best_price, best_size = asks[0]
        else:
            best_price, best_size = 0.0, 0.0

        quotes.append(OutcomeQuote(
            token_id=outcome.token_id,
            outcome_name=outcome.outcome_name,
            market_id=outcome.market_id,
            best_ask_price=best_price,
            available_size=best_size,
            ask_levels=asks[:10],  # keep top 10 levels
        ))

    return quotes


def detect_arbitrage(
    event: MultiOutcomeEvent,
    quotes: List[OutcomeQuote],
) -> Optional[ArbitrageOpportunity]:
    """Check if a multi-outcome event has an arbitrage opportunity.

    An opportunity exists when:
    1. All outcomes have valid ask prices
    2. Sum of best asks < 1.00 (room for profit)
    3. Profit % exceeds MIN_PROFIT_PCT
    4. Profit % is below MAX_PROFIT_PCT (filter stale data)
    5. Executable size meets MIN_EXECUTABLE_SIZE
    6. Total cost within MAX_POSITION_COST limits

    Returns an ArbitrageOpportunity if found, None otherwise.
    """
    # All outcomes must have valid asks
    valid_quotes = [q for q in quotes if q.best_ask_price > 0 and q.available_size > 0]
    if len(valid_quotes) != len(quotes):
        missing = len(quotes) - len(valid_quotes)
        logger.debug(
            "Event %s: %d/%d outcomes missing asks",
            event.title[:30], missing, len(quotes),
        )
        return None

    total_cost = sum(q.best_ask_price for q in quotes)
    profit_per_share = 1.0 - total_cost

    # No arb if total cost >= $1.00
    if profit_per_share <= 0:
        return None

    profit_pct = (profit_per_share / total_cost) * 100.0

    # Check profit thresholds
    if profit_pct < MIN_PROFIT_PCT:
        return None
    if profit_pct > MAX_PROFIT_PCT:
        logger.info(
            "Event %s: profit %.1f%% exceeds max %.1f%% — likely stale data",
            event.title[:30], profit_pct, MAX_PROFIT_PCT,
        )
        return None

    # Executable size is the minimum available across all legs
    executable_size = min(q.available_size for q in quotes)
    if executable_size < MIN_EXECUTABLE_SIZE:
        return None

    # Cap executable size by max position cost
    if total_cost > 0:
        max_shares = MAX_POSITION_COST / total_cost
        executable_size = min(executable_size, max_shares)

    return ArbitrageOpportunity(
        event_id=event.event_id,
        event_title=event.title,
        event_slug=event.slug,
        quotes=quotes,
        total_cost=total_cost,
        profit_per_share=profit_per_share,
        profit_pct=profit_pct,
        executable_size=executable_size,
        neg_risk=event.neg_risk,
    )


def scan_for_opportunities(
    events: List[MultiOutcomeEvent],
    ws_feed: Optional[OrderbookFeed] = None,
) -> List[ArbitrageOpportunity]:
    """Scan all multi-outcome events and return arbitrage opportunities.

    Returns opportunities sorted by profit_pct descending.
    """
    opportunities: List[ArbitrageOpportunity] = []

    for event in events:
        try:
            quotes = quote_event(event, ws_feed)
            opp = detect_arbitrage(event, quotes)
            if opp is not None:
                opportunities.append(opp)
        except Exception as e:
            logger.warning(
                "Error scanning event %s: %s", event.title[:30], e
            )
            continue

    # Sort by profit_pct descending (best opportunities first)
    opportunities.sort(key=lambda o: o.profit_pct, reverse=True)

    logger.info(
        "Found %d opportunities from %d events",
        len(opportunities), len(events),
    )
    return opportunities
