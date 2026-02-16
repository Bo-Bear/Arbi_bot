"""Configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Polymarket credentials ---
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_SIGNATURE_TYPE: int = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
POLY_FUNDER_ADDRESS: str = os.getenv("POLY_FUNDER_ADDRESS", "")

# --- Execution mode ---
EXEC_MODE: str = os.getenv("EXEC_MODE", "paper").lower()

# --- Scanning ---
SCAN_INTERVAL_SECONDS: float = float(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
MIN_OUTCOMES: int = int(os.getenv("MIN_OUTCOMES", "3"))
MAX_OUTCOMES: int = int(os.getenv("MAX_OUTCOMES", "20"))

# --- Arbitrage thresholds ---
# Minimum profit % to consider an opportunity (e.g., 2.0 = 2%)
MIN_PROFIT_PCT: float = float(os.getenv("MIN_PROFIT_PCT", "2.0"))
# Maximum profit % — skip outliers that are likely stale/bad data
MAX_PROFIT_PCT: float = float(os.getenv("MAX_PROFIT_PCT", "15.0"))
# Minimum number of shares executable across all legs
MIN_EXECUTABLE_SIZE: float = float(os.getenv("MIN_EXECUTABLE_SIZE", "5"))
# Maximum total dollar cost per arb (risk cap)
MAX_POSITION_COST: float = float(os.getenv("MAX_POSITION_COST", "20.0"))

# --- Cooldown ---
# After trading an event, skip it for this many scans so the bot finds
# new opportunities instead of re-trading the same stale orderbook.
EVENT_COOLDOWN_SCANS: int = int(os.getenv("EVENT_COOLDOWN_SCANS", "10"))
# Minimum decrease in total_cost (per share) needed to re-trade an event
# after cooldown expires.  Prevents re-trading at identical prices.
MIN_REPRICE_IMPROVEMENT: float = float(os.getenv("MIN_REPRICE_IMPROVEMENT", "0.005"))

# --- Execution ---
ORDER_TIMEOUT_S: float = float(os.getenv("ORDER_TIMEOUT_S", "20"))
# Price buffer added to limit orders to improve fill rate
LIVE_PRICE_BUFFER: float = float(os.getenv("LIVE_PRICE_BUFFER", "0.02"))
# Maximum quote staleness (seconds) allowed before execution.
# If any leg's WS data is older than this, quotes are refreshed via HTTP.
MAX_QUOTE_STALENESS_S: float = float(os.getenv("MAX_QUOTE_STALENESS_S", "5.0"))
# Estimated fee + slippage overhead (percentage points) subtracted from
# raw profit before the MIN_PROFIT_PCT check. Ensures opportunities remain
# profitable after real-world execution costs.
FEE_BUFFER_PCT: float = float(os.getenv("FEE_BUFFER_PCT", "1.0"))
# Maximum seconds to wait for unwind sell orders to fill before alerting.
UNWIND_TIMEOUT_S: float = float(os.getenv("UNWIND_TIMEOUT_S", "30"))
# Allow FOK → GTC fallback when the exchange doesn't support FOK.
# Disable for stricter atomicity (leg will fail instead of falling back).
ALLOW_GTC_FALLBACK: bool = os.getenv("ALLOW_GTC_FALLBACK", "false").lower() == "true"

# --- Risk management ---
MAX_SESSION_DRAWDOWN: float = float(os.getenv("MAX_SESSION_DRAWDOWN", "20.0"))
MAX_TRADES_PER_SESSION: int = int(os.getenv("MAX_TRADES_PER_SESSION", "50"))
# Maximum total dollar cost of trades per session.  Once session_cost
# reaches this limit the bot stops placing new trades.
MAX_SESSION_COST: float = float(os.getenv("MAX_SESSION_COST", "20.0"))
MAX_CONSECUTIVE_FAILURES: int = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "100"))
# File path for persisting position state across restarts.
POSITION_STATE_FILE: str = os.getenv("POSITION_STATE_FILE", "position_state.json")

# --- Logging ---
LOG_DIR: str = os.getenv("LOG_DIR", "logs")

# --- API endpoints ---
POLY_GAMMA_BASE: str = os.getenv("POLY_GAMMA_BASE", "https://gamma-api.polymarket.com")
POLY_CLOB_BASE: str = os.getenv("POLY_CLOB_BASE", "https://clob.polymarket.com")
