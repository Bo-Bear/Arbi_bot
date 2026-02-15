"""JSONL file logger for trade and scan data."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import LOG_DIR

_logger = logging.getLogger(__name__)


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def get_logfile_path() -> str:
    """Generate a logfile path for the current session."""
    ensure_log_dir()
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(LOG_DIR, f"multi_arb_{ts}.jsonl")


def append_log(path: str, row: dict) -> None:
    """Append a JSON line to the logfile."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        _logger.error("Failed to write log: %s", e)


def log_opportunity(
    logfile: str,
    scan_num: int,
    event_title: str,
    event_id: str,
    num_outcomes: int,
    total_cost: float,
    profit_pct: float,
    executable_size: float,
    quotes: List[dict],
) -> None:
    """Log a detected arbitrage opportunity."""
    append_log(logfile, {
        "log_type": "opportunity",
        "ts": _utc_ts(),
        "scan_num": scan_num,
        "event_title": event_title,
        "event_id": event_id,
        "num_outcomes": num_outcomes,
        "total_cost": total_cost,
        "profit_pct": profit_pct,
        "executable_size": executable_size,
        "quotes": quotes,
    })


def log_execution(
    logfile: str,
    scan_num: int,
    event_title: str,
    event_id: str,
    exec_mode: str,
    all_filled: bool,
    num_legs: int,
    num_filled: int,
    total_latency_ms: float,
    total_cost_planned: float,
    total_cost_actual: float,
    filled_size: float,
    leg_details: List[dict],
) -> None:
    """Log execution results."""
    append_log(logfile, {
        "log_type": "execution",
        "ts": _utc_ts(),
        "scan_num": scan_num,
        "event_title": event_title,
        "event_id": event_id,
        "exec_mode": exec_mode,
        "all_filled": all_filled,
        "num_legs": num_legs,
        "num_filled": num_filled,
        "total_latency_ms": round(total_latency_ms, 1),
        "total_cost_planned": total_cost_planned,
        "total_cost_actual": total_cost_actual,
        "filled_size": filled_size,
        "profit_per_share": round(1.0 - total_cost_actual, 6) if all_filled else None,
        "total_profit": round((1.0 - total_cost_actual) * filled_size, 4) if all_filled else None,
        "legs": leg_details,
    })


def log_skip(
    logfile: str,
    scan_num: int,
    event_title: str,
    reason: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Log why an event was skipped."""
    row: dict = {
        "log_type": "skip",
        "ts": _utc_ts(),
        "scan_num": scan_num,
        "event_title": event_title,
        "reason": reason,
    }
    if details:
        row.update(details)
    append_log(logfile, row)


def log_scan_summary(
    logfile: str,
    scan_num: int,
    num_events: int,
    num_opportunities: int,
    scan_ms: float,
) -> None:
    """Log per-scan summary."""
    append_log(logfile, {
        "log_type": "scan_summary",
        "ts": _utc_ts(),
        "scan_num": scan_num,
        "num_events": num_events,
        "num_opportunities": num_opportunities,
        "scan_ms": round(scan_ms, 1),
    })


def log_session_start(logfile: str, config: dict) -> None:
    """Log session startup with configuration snapshot."""
    append_log(logfile, {
        "log_type": "session_start",
        "ts": _utc_ts(),
        **config,
    })


def log_session_end(logfile: str, reason: str, stats: dict) -> None:
    """Log session shutdown."""
    append_log(logfile, {
        "log_type": "session_end",
        "ts": _utc_ts(),
        "reason": reason,
        **stats,
    })


def setup_logging(level: int = logging.INFO) -> None:
    """Configure Python logging for the bot."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
