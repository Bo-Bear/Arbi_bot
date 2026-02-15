"""Polymarket CLOB API client for orderbooks and order placement.

The CLOB (Central Limit Order Book) is the trading API. It requires
authentication for order placement but orderbook reads are public.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

from config import (
    POLY_CLOB_BASE, POLY_PRIVATE_KEY, POLY_SIGNATURE_TYPE,
    POLY_FUNDER_ADDRESS, LIVE_PRICE_BUFFER, ORDER_TIMEOUT_S,
)

logger = logging.getLogger(__name__)

# Optional imports for live trading
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False

_http_session: Optional[requests.Session] = None
_clob_client = None
_working_route_idx: Optional[int] = None


def _get_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
    return _http_session


def get_clob_client():
    """Initialize and return the Polymarket CLOB client for order placement."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    if not HAS_CLOB_CLIENT:
        raise RuntimeError(
            "'py-clob-client' package required for live trading: "
            "pip install py-clob-client"
        )
    if not POLY_PRIVATE_KEY:
        raise RuntimeError("POLY_PRIVATE_KEY not set")

    kwargs = {
        "host": POLY_CLOB_BASE,
        "key": POLY_PRIVATE_KEY,
        "chain_id": 137,
        "signature_type": POLY_SIGNATURE_TYPE,
    }
    if POLY_SIGNATURE_TYPE in (1, 2) and POLY_FUNDER_ADDRESS:
        kwargs["funder"] = POLY_FUNDER_ADDRESS

    client = ClobClient(**kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    _clob_client = client
    return _clob_client


def get_orderbook(token_id: str) -> List[Tuple[float, float]]:
    """Fetch ask-side orderbook for a token from the CLOB API.

    Returns list of (price, size) tuples sorted by price ascending.
    Falls back through multiple endpoint formats.
    """
    global _working_route_idx

    candidates = [
        (f"{POLY_CLOB_BASE}/book", {"token_id": str(token_id)}),
        (f"{POLY_CLOB_BASE}/book/{token_id}", None),
        (f"{POLY_CLOB_BASE}/orderbook", {"token_id": str(token_id)}),
        (f"{POLY_CLOB_BASE}/orderbook/{token_id}", None),
    ]

    # Try cached working route first
    if _working_route_idx is not None:
        order = [_working_route_idx] + [
            i for i in range(len(candidates)) if i != _working_route_idx
        ]
    else:
        order = list(range(len(candidates)))

    for idx in order:
        url, params = candidates[idx]
        try:
            r = _get_session().get(url, params=params, timeout=15)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()

            book = data.get("data", data)
            asks_raw = book.get("asks") or []
            asks: List[Tuple[float, float]] = []

            for lvl in asks_raw:
                try:
                    p = float(lvl.get("price"))
                    s = float(
                        lvl.get("size")
                        or lvl.get("quantity")
                        or lvl.get("amount")
                    )
                except (TypeError, ValueError):
                    continue
                if p > 0 and s > 0:
                    asks.append((p, s))

            asks.sort(key=lambda x: x[0])
            _working_route_idx = idx
            return asks
        except Exception as e:
            logger.debug("CLOB route %d failed: %s", idx, e)
            continue

    return []


def get_best_ask(token_id: str) -> Optional[Tuple[float, float]]:
    """Get the best ask price and size for a token.

    Returns (price, size) or None if no asks available.
    """
    asks = get_orderbook(token_id)
    if not asks:
        return None
    return asks[0]


def get_orderbooks_batch(
    token_ids: List[str],
) -> Dict[str, List[Tuple[float, float]]]:
    """Fetch orderbooks for multiple tokens.

    Uses sequential requests (Polymarket has rate limits).
    Returns {token_id: [(price, size), ...]}.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: Dict[str, List[Tuple[float, float]]] = {}

    # Limit concurrency to avoid rate limits
    max_workers = min(len(token_ids), 5)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(get_orderbook, tid): tid
            for tid in token_ids
        }
        for future in as_completed(futures):
            tid = futures[future]
            try:
                results[tid] = future.result(timeout=20)
            except Exception as e:
                logger.warning("Orderbook fetch failed for %s: %s", tid[:20], e)
                results[tid] = []

    return results


def place_order(
    token_id: str,
    price: float,
    size: float,
    side: str = "BUY",
    order_type: str = "FOK",
) -> dict:
    """Place an order on the CLOB.

    Args:
        token_id: The token to trade
        price: Limit price
        size: Number of contracts
        side: "BUY" or "SELL"
        order_type: "FOK" (Fill or Kill), "GTC" (Good Till Cancelled)

    Returns dict with keys: success, order_id, status, error
    """
    client = get_clob_client()

    order_side = BUY if side == "BUY" else SELL
    otype = OrderType.FOK if order_type == "FOK" else OrderType.GTC

    # Round price to valid tick (0.01)
    rounded_price = round(price, 2)

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=rounded_price,
            size=size,
            side=order_side,
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, otype)

        if not resp.get("success", False):
            error = resp.get("errorMsg") or resp.get("error") or str(resp)
            return {
                "success": False,
                "order_id": None,
                "status": "rejected",
                "error": error,
            }

        order_id = resp.get("orderID") or resp.get("id")
        return {
            "success": True,
            "order_id": order_id,
            "status": "submitted",
            "error": None,
        }
    except Exception as e:
        return {
            "success": False,
            "order_id": None,
            "status": "error",
            "error": str(e),
        }


def poll_order_status(
    order_id: str, timeout: float = None
) -> Tuple[str, float, float]:
    """Poll an order until it reaches a terminal state.

    Returns (status, filled_size, avg_price).
    Status is one of: "filled", "partial", "canceled", "timeout", "error".
    """
    client = get_clob_client()
    deadline = time.monotonic() + (timeout or ORDER_TIMEOUT_S)

    while time.monotonic() < deadline:
        try:
            o = client.get_order(order_id)
            o_status = (o.get("status") or "").lower()

            if o_status in ("matched", "filled"):
                filled = float(
                    o.get("size_matched", o.get("original_size", 0))
                )
                avg_price = float(o.get("price", 0))
                return "filled", filled, avg_price

            if o_status in ("canceled", "cancelled"):
                filled = float(o.get("size_matched", 0))
                avg_price = float(o.get("price", 0)) if filled > 0 else 0
                status = "partial" if filled > 0 else "canceled"
                return status, filled, avg_price

        except Exception as e:
            logger.debug("Poll error for %s: %s", order_id, e)

        time.sleep(0.5)

    # Timeout — try to cancel
    try:
        client.cancel(order_id)
        o = client.get_order(order_id)
        filled = float(o.get("size_matched", 0))
        if filled > 0:
            avg_price = float(o.get("price", 0))
            return "partial", filled, avg_price
    except Exception:
        pass

    return "timeout", 0.0, 0.0


def cancel_order(order_id: str) -> bool:
    """Cancel an order. Returns True if successfully canceled."""
    try:
        client = get_clob_client()
        client.cancel(order_id)
        return True
    except Exception as e:
        logger.warning("Cancel failed for %s: %s", order_id, e)
        return False


def sell_position(token_id: str, size: float, price: float) -> dict:
    """Sell a position to unwind a filled leg.

    Uses a discounted price to ensure fill.
    """
    sell_price = max(0.01, round(price - 0.02, 2))
    return place_order(
        token_id=token_id,
        price=sell_price,
        size=size,
        side="SELL",
        order_type="GTC",
    )
