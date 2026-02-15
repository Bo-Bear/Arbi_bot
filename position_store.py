"""Persistent position state across bot restarts.

Saves position_costs and event_last_traded to a JSON file so the bot
doesn't re-deploy capital on events it already holds positions in.
"""

import json
import logging
import os
from typing import Dict, Optional, Tuple

from config import POSITION_STATE_FILE

logger = logging.getLogger(__name__)


def save_positions(
    position_costs: Dict[str, float],
    event_last_traded: Dict[str, int],
    event_traded_costs: Optional[Dict[str, float]] = None,
) -> None:
    """Persist current position state to disk."""
    state = {
        "position_costs": {k: round(v, 6) for k, v in position_costs.items()},
        "event_last_traded": event_last_traded,
        "event_traded_costs": {k: round(v, 6) for k, v in (event_traded_costs or {}).items()},
    }
    tmp = POSITION_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, POSITION_STATE_FILE)
        total = sum(position_costs.values())
        logger.info(
            "Position state saved: %d events, $%.2f total deployed",
            len(position_costs), total,
        )
    except Exception as e:
        logger.error("Failed to save position state: %s", e)


def load_positions() -> Tuple[Dict[str, float], Dict[str, int], Dict[str, float]]:
    """Load position state from disk. Returns empty dicts if no file."""
    if not os.path.exists(POSITION_STATE_FILE):
        return {}, {}, {}
    try:
        with open(POSITION_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        position_costs = {k: float(v) for k, v in state.get("position_costs", {}).items()}
        event_last_traded = {k: int(v) for k, v in state.get("event_last_traded", {}).items()}
        event_traded_costs = {k: float(v) for k, v in state.get("event_traded_costs", {}).items()}
        logger.info(
            "Loaded position state: %d events, $%.2f deployed",
            len(position_costs), sum(position_costs.values()),
        )
        return position_costs, event_last_traded, event_traded_costs
    except Exception as e:
        logger.error("Failed to load position state: %s", e)
        return {}, {}, {}
