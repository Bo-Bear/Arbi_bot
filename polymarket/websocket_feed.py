"""Real-time Polymarket CLOB orderbook via WebSocket.

Maintains an in-memory cache of ask-side books for subscribed tokens.
Falls back gracefully if websocket-client is not installed.
"""

import json
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from websocket import WebSocketApp
    HAS_WS = True
except ImportError:
    HAS_WS = False

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class OrderbookFeed:
    """Background WebSocket that caches ask-side orderbook data."""

    def __init__(self):
        self._asks: Dict[str, Dict[float, float]] = {}  # token -> {price: size}
        self._lock = threading.Lock()
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._subscribed: set = set()
        self._ready: set = set()  # tokens that received at least one snapshot
        self._running = False
        self._connected = threading.Event()
        self._last_update: Dict[str, float] = {}
        self._hits = 0
        self._misses = 0

    @property
    def available(self) -> bool:
        return HAS_WS

    def start(self):
        if not HAS_WS:
            logger.warning(
                "websocket-client not installed; using HTTP-only mode"
            )
            return
        self._running = True
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._thread.start()

    def _connect_loop(self):
        while self._running:
            try:
                ws = WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                pass
            self._connected.clear()
            if self._running:
                time.sleep(0.5)

    def _on_open(self, ws):
        self._connected.set()
        if self._subscribed:
            self._send_subscribe(list(self._subscribed), initial=True)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        if isinstance(data, list):
            for item in data:
                self._handle_event(item)
        else:
            self._handle_event(data)

    def _handle_event(self, data: dict):
        if not isinstance(data, dict):
            return
        event_type = data.get("event_type")

        if event_type == "book":
            asset_id = data.get("asset_id")
            if not asset_id:
                return
            asks_dict: Dict[float, float] = {}
            for lvl in data.get("asks", []):
                try:
                    p = float(lvl["price"])
                    s = float(lvl["size"])
                    if p > 0 and s > 0:
                        asks_dict[p] = s
                except (KeyError, ValueError, TypeError):
                    continue
            with self._lock:
                self._asks[asset_id] = asks_dict
                self._ready.add(asset_id)
                self._last_update[asset_id] = time.monotonic()

        elif event_type == "price_change":
            with self._lock:
                for pc in data.get("price_changes", []):
                    asset_id = pc.get("asset_id")
                    if not asset_id or asset_id not in self._ready:
                        continue
                    self._last_update[asset_id] = time.monotonic()
                    # Update ask level from SELL trades
                    side = str(pc.get("side", "")).upper()
                    if side == "SELL" and asset_id in self._asks:
                        try:
                            p = float(pc["price"])
                            s = float(pc["size"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        if s > 0:
                            self._asks[asset_id][p] = s
                        else:
                            self._asks[asset_id].pop(p, None)

    def _on_error(self, ws, error):
        logger.debug("WS error: %s", error)

    def _on_close(self, ws, close_status, close_msg):
        self._connected.clear()

    def subscribe(self, token_ids: List[str]):
        """Subscribe to orderbook updates for given token IDs."""
        new_ids = [tid for tid in token_ids if tid not in self._subscribed]
        if not new_ids:
            return
        self._subscribed.update(new_ids)
        if self._connected.is_set() and self._ws:
            self._send_subscribe(new_ids, initial=False)

    def _send_subscribe(self, token_ids: List[str], initial: bool = False):
        if not self._ws:
            return
        try:
            if initial:
                self._ws.send(json.dumps({
                    "assets_ids": token_ids,
                    "type": "market",
                    "custom_feature_enabled": True,
                }))
            else:
                self._ws.send(json.dumps({
                    "assets_ids": token_ids,
                    "operation": "subscribe",
                }))
        except Exception:
            pass

    def get_asks(self, token_id: str) -> Optional[List[Tuple[float, float]]]:
        """Get cached asks for a token. Returns None if not cached."""
        with self._lock:
            if token_id not in self._ready:
                self._misses += 1
                return None
            self._hits += 1
            asks = self._asks.get(token_id, {})
            return sorted(
                [(p, s) for p, s in asks.items() if s > 0],
                key=lambda x: x[0],
            )

    def get_staleness(self, token_id: str) -> Optional[float]:
        """Seconds since last update for this token. None if never updated."""
        with self._lock:
            ts = self._last_update.get(token_id)
            if ts is None:
                return None
            return time.monotonic() - ts

    def get_stats(self) -> Tuple[int, int]:
        """Return (cache_hits, cache_misses) and reset counters."""
        with self._lock:
            hits, misses = self._hits, self._misses
            self._hits = self._misses = 0
            return hits, misses

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
