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
MAX_POSITION_COST: float = float(os.getenv("MAX_POSITION_COST", "200.0"))

# --- Cooldown ---
# After trading an event, skip it for this many scans so the bot finds
# new opportunities instead of re-trading the same stale orderbook.
EVENT_COOLDOWN_SCANS: int = int(os.getenv("EVENT_COOLDOWN_SCANS", "10"))

# --- Execution ---
ORDER_TIMEOUT_S: float = float(os.getenv("ORDER_TIMEOUT_S", "10"))
# Price buffer added to limit orders to improve fill rate
LIVE_PRICE_BUFFER: float = float(os.getenv("LIVE_PRICE_BUFFER", "0.02"))

# --- Risk management ---
MAX_SESSION_DRAWDOWN: float = float(os.getenv("MAX_SESSION_DRAWDOWN", "100.0"))
MAX_TRADES_PER_SESSION: int = int(os.getenv("MAX_TRADES_PER_SESSION", "50"))
MAX_CONSECUTIVE_FAILURES: int = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "100"))

# --- Logging ---
LOG_DIR: str = os.getenv("LOG_DIR", "logs")

# --- API endpoints ---
POLY_GAMMA_BASE: str = os.getenv("POLY_GAMMA_BASE", "https://gamma-api.polymarket.com")
POLY_CLOB_BASE: str = os.getenv("POLY_CLOB_BASE", "https://clob.polymarket.com")
