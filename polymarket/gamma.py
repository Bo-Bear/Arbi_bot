"""Polymarket Gamma API client for event and market discovery.

The Gamma API is the public read-only API that provides market metadata,
event groupings, and current prices. No authentication required.
"""

import json
import logging
from typing import List, Optional

import requests

from config import POLY_GAMMA_BASE, MIN_OUTCOMES, MAX_OUTCOMES
from models import MultiOutcomeEvent, OutcomeToken

logger = logging.getLogger(__name__)

_http_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
    return _http_session


def fetch_active_events(limit: int = 200, offset: int = 0) -> List[dict]:
    """Fetch active, open events from the Gamma API.

    Returns raw event dicts with nested market data.
    """
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
        "order": "liquidity",
        "ascending": "false",
    }
    try:
        r = _get_session().get(
            f"{POLY_GAMMA_BASE}/events", params=params, timeout=20
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("Gamma /events fetch failed: %s", e)
        return []


def fetch_all_active_events(max_pages: int = 5, page_size: int = 200) -> List[dict]:
    """Paginate through all active events."""
    all_events: List[dict] = []
    for page in range(max_pages):
        batch = fetch_active_events(limit=page_size, offset=page * page_size)
        if not batch:
            break
        all_events.extend(batch)
        if len(batch) < page_size:
            break
    return all_events


def _parse_outcomes(market: dict) -> Optional[OutcomeToken]:
    """Parse a single Gamma market into an OutcomeToken for its YES side.

    Each market in a multi-outcome event represents one outcome.
    We extract the YES token ID and the outcome name.
    """
    condition_id = market.get("conditionId") or market.get("condition_id") or ""
    question = market.get("question") or ""

    # Parse clobTokenIds — can be a JSON string or a list
    clob_token_ids = market.get("clobTokenIds") or market.get("clob_token_ids")
    if not clob_token_ids:
        return None
    if isinstance(clob_token_ids, str):
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except (json.JSONDecodeError, TypeError):
            # Try comma-separated
            clob_token_ids = [t.strip() for t in clob_token_ids.split(",") if t.strip()]

    if not isinstance(clob_token_ids, list) or len(clob_token_ids) < 1:
        return None

    # Parse outcomes list
    outcomes_raw = market.get("outcomes")
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except (json.JSONDecodeError, TypeError):
            outcomes = []
    elif isinstance(outcomes_raw, list):
        outcomes = outcomes_raw
    else:
        outcomes = []

    # Map outcomes to token IDs. First token is typically YES.
    # For multi-outcome events, each market represents one outcome
    # and the YES token is "this outcome wins".
    yes_token_id = str(clob_token_ids[0]) if clob_token_ids else ""
    if not yes_token_id:
        return None

    # Derive outcome name from the market question or outcomes list
    # For multi-outcome events, the market question is typically like
    # "Will Sinner win?" — the outcome name is the subject.
    outcome_name = _extract_outcome_name(question, outcomes, market)

    return OutcomeToken(
        token_id=yes_token_id,
        outcome_name=outcome_name,
        market_id=condition_id,
        question=question,
    )


def _extract_outcome_name(question: str, outcomes: list, market: dict) -> str:
    """Best-effort extraction of the outcome name from market data."""
    # Try groupItemTitle first (Polymarket often sets this for grouped markets)
    group_title = market.get("groupItemTitle") or ""
    if group_title:
        return group_title

    # Try the first outcome label
    if outcomes and len(outcomes) >= 1:
        first = outcomes[0]
        if isinstance(first, str) and first.lower() != "yes":
            return first

    # Fall back to parsing the question
    # "Will Sinner win?" -> "Sinner"
    q = question.strip().rstrip("?")
    if q.lower().startswith("will ") and q.lower().endswith(" win"):
        return q[5:-4].strip()

    # Last resort: use the full question
    return question[:50] if question else "Unknown"


def discover_multi_outcome_events(events: Optional[List[dict]] = None) -> List[MultiOutcomeEvent]:
    """Scan Gamma events and return multi-outcome events suitable for arb.

    Filters for:
    - Events with MIN_OUTCOMES to MAX_OUTCOMES markets
    - Markets that have valid YES token IDs
    - Active, non-closed markets
    """
    if events is None:
        events = fetch_all_active_events()

    multi_events: List[MultiOutcomeEvent] = []

    for event_data in events:
        markets = event_data.get("markets") or []

        # Skip events with too few or too many markets
        if len(markets) < MIN_OUTCOMES or len(markets) > MAX_OUTCOMES:
            continue

        event_id = str(event_data.get("id") or "")
        title = event_data.get("title") or ""
        slug = event_data.get("slug") or ""

        # Check if this is a neg_risk event (mutually exclusive outcomes)
        # All markets in the event should agree on neg_risk status
        neg_risk = any(m.get("negRisk", False) for m in markets)

        # Parse each market into an outcome token
        outcome_tokens: List[OutcomeToken] = []
        for m in markets:
            # Skip closed/inactive markets
            if m.get("closed", False):
                continue
            if m.get("active") is False:
                continue

            token = _parse_outcomes(m)
            if token:
                outcome_tokens.append(token)

        # Need at least MIN_OUTCOMES valid tokens
        if len(outcome_tokens) < MIN_OUTCOMES:
            continue

        multi_events.append(MultiOutcomeEvent(
            event_id=event_id,
            title=title,
            slug=slug,
            outcomes=outcome_tokens,
            neg_risk=neg_risk,
        ))

    logger.info(
        "Discovered %d multi-outcome events (from %d total events)",
        len(multi_events), len(events),
    )
    return multi_events
