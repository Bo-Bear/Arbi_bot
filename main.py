"""Multi-outcome arbitrage bot for Polymarket.

Scans multi-outcome events on Polymarket where the sum of YES token
prices across all mutually exclusive outcomes is less than $1.00.
Buys all outcomes to lock in a guaranteed profit regardless of which
outcome wins.

Usage:
    python main.py

Configure via .env file (see .env.example).
"""

import os
import time
from typing import Dict, List, Optional

import config
from display import (
    print_execution_result,
    print_no_opportunities,
    print_opportunity,
    print_scan_header,
    print_session_summary,
)
from executor import execute_opportunity
import logging
from logger import (
    append_log,
    get_logfile_path,
    log_execution,
    log_opportunity,
    log_scan_summary,
    log_session_end,
    log_session_start,
    setup_logging,
)

main_logger = logging.getLogger(__name__)
from models import ArbitrageOpportunity, ExecutionResult
from polymarket.gamma import discover_multi_outcome_events, fetch_all_active_events
from polymarket.websocket_feed import OrderbookFeed
from position_store import load_positions, save_positions
from scanner import scan_for_opportunities


def _confirm_settings() -> bool:
    """Display settings and get user confirmation to proceed."""
    mode = "LIVE TRADING" if config.EXEC_MODE == "live" else "Paper Testing"
    print(f"\n{'=' * 60}")
    print(f"  MULTI-OUTCOME ARBITRAGE BOT")
    print(f"{'=' * 60}")
    print(f"  Mode:              {mode}")
    print(f"  Min outcomes:      {config.MIN_OUTCOMES}")
    print(f"  Max outcomes:      {config.MAX_OUTCOMES}")
    print(f"  Min profit:        {config.MIN_PROFIT_PCT}% + {config.FEE_BUFFER_PCT}% fee buffer = {config.MIN_PROFIT_PCT + config.FEE_BUFFER_PCT}% effective")
    print(f"  Max profit:        {config.MAX_PROFIT_PCT}% (stale data filter)")
    print(f"  Min exec size:     {config.MIN_EXECUTABLE_SIZE} shares")
    print(f"  Max position cost: ${config.MAX_POSITION_COST:.2f}")
    print(f"  Scan interval:     {config.SCAN_INTERVAL_SECONDS}s")
    print(f"  Max trades:        {config.MAX_TRADES_PER_SESSION}")
    print(f"  Max drawdown:      ${config.MAX_SESSION_DRAWDOWN:.2f}")
    print(f"  Circuit breaker:   {config.MAX_CONSECUTIVE_FAILURES} empty scans")
    print(f"{'=' * 60}")

    if config.EXEC_MODE == "live":
        print("\n  *** WARNING: LIVE TRADING MODE ***")
        print("  Real orders will be placed on Polymarket.")
        ans = input("  Continue? (y/N): ").strip().lower()
        return ans == "y"

    print("\n  Running in paper mode (no real orders).")
    return True


def _validate_live_credentials() -> bool:
    """Check that all required credentials are available for live trading."""
    from polymarket.clob import HAS_CLOB_CLIENT

    missing = []
    if not config.POLY_PRIVATE_KEY:
        missing.append("POLY_PRIVATE_KEY")
    if not HAS_CLOB_CLIENT:
        missing.append("py-clob-client package (pip install py-clob-client)")

    if missing:
        print("\n  LIVE MODE BLOCKED — missing:")
        for m in missing:
            print(f"    - {m}")
        return False

    # Test CLOB client initialization
    try:
        from polymarket.clob import get_clob_client
        client = get_clob_client()
        print("  CLOB client initialized OK")
    except Exception as e:
        print(f"\n  CLOB client error: {e}")
        return False

    return True


def main() -> None:
    setup_logging()
    logfile = get_logfile_path()

    if not _confirm_settings():
        print("Aborted.")
        return

    if config.EXEC_MODE == "live" and not _validate_live_credentials():
        return

    # Log session start
    log_session_start(logfile, {
        "exec_mode": config.EXEC_MODE,
        "min_outcomes": config.MIN_OUTCOMES,
        "max_outcomes": config.MAX_OUTCOMES,
        "min_profit_pct": config.MIN_PROFIT_PCT,
        "fee_buffer_pct": config.FEE_BUFFER_PCT,
        "effective_min_profit_pct": config.MIN_PROFIT_PCT + config.FEE_BUFFER_PCT,
        "max_profit_pct": config.MAX_PROFIT_PCT,
        "min_executable_size": config.MIN_EXECUTABLE_SIZE,
        "max_position_cost": config.MAX_POSITION_COST,
        "scan_interval": config.SCAN_INTERVAL_SECONDS,
        "max_quote_staleness_s": config.MAX_QUOTE_STALENESS_S,
        "allow_gtc_fallback": config.ALLOW_GTC_FALLBACK,
        "unwind_timeout_s": config.UNWIND_TIMEOUT_S,
        "order_timeout_s": config.ORDER_TIMEOUT_S,
    })

    # Start WebSocket feed for real-time orderbook data
    ws_feed = OrderbookFeed()
    if ws_feed.available:
        ws_feed.start()
        print("  WebSocket: Polymarket CLOB connected")
    else:
        print("  WebSocket: not available (pip install websocket-client)")

    print(f"\n  Logging to: {logfile}")
    print(f"  Press Ctrl+C to stop.\n")

    trades: List[ExecutionResult] = []
    scan_count = 0
    total_events_scanned = 0
    consecutive_failures = 0
    session_cost = 0.0
    session_profit = 0.0
    # Load persisted position state (survives restarts).
    position_costs, event_last_traded = load_positions()
    if position_costs:
        total_deployed = sum(position_costs.values())
        main_logger.info(
            "Restored position state: %d events, $%.2f deployed",
            len(position_costs), total_deployed,
        )
        append_log(logfile, {
            "log_type": "position_restore",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "num_events": len(position_costs),
            "total_deployed": round(total_deployed, 4),
            "events": {k: round(v, 4) for k, v in position_costs.items()},
        })
        print(f"  Restored positions: {len(position_costs)} events, ${total_deployed:.2f} deployed")

    exit_reason = "max_trades_reached"

    try:
        while len(trades) < config.MAX_TRADES_PER_SESSION:
            scan_count += 1
            scan_t0 = time.monotonic()
            print_scan_header(scan_count)

            # Step 1: Fetch all active events from Gamma API
            try:
                print("  Fetching events...", end=" ", flush=True)
                raw_events = fetch_all_active_events()
                print(f"{len(raw_events)} raw events")
            except Exception as e:
                print(f"\n  ERROR fetching events: {e}")
                raw_events = []

            if not raw_events:
                print("  WARNING: Gamma API returned 0 events (API may be rate-limited)")
                _sleep_with_summary(scan_count, scan_t0, logfile, 0, 0, raw_count=0)
                continue

            # Step 2: Filter to multi-outcome events
            multi_events = discover_multi_outcome_events(raw_events)
            total_events_scanned += len(multi_events)
            print(f"  Multi-outcome events: {len(multi_events)} (of {len(raw_events)} total)")

            if not multi_events:
                print("  No qualifying multi-outcome neg_risk events found.")
                _sleep_with_summary(scan_count, scan_t0, logfile, 0, 0, raw_count=len(raw_events))
                continue

            # Show how many events are budget-capped
            if position_costs:
                capped = sum(
                    1 for e in multi_events
                    if position_costs.get(e.event_id, 0.0) >= config.MAX_POSITION_COST
                )
                if capped:
                    print(f"  Budget-capped events: {capped} (already at max position)")

            # Filter out events still on cooldown from a recent trade
            if event_last_traded:
                before = len(multi_events)
                multi_events = [
                    e for e in multi_events
                    if e.event_id not in event_last_traded
                    or (scan_count - event_last_traded[e.event_id])
                    > config.EVENT_COOLDOWN_SCANS
                ]
                cooled = before - len(multi_events)
                if cooled:
                    print(f"  Cooldown-skipped events: {cooled} (traded recently)")

            # Step 3: Scan for arbitrage opportunities
            print("  Scanning orderbooks...", end=" ", flush=True)
            opportunities = scan_for_opportunities(
                multi_events, ws_feed, position_costs=position_costs,
            )
            print(f"{len(opportunities)} opportunities found")

            if not opportunities:
                print_no_opportunities(len(multi_events))
                _sleep_with_summary(
                    scan_count, scan_t0, logfile,
                    len(multi_events), 0,
                )
                consecutive_failures += 1
                if consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"\n  Circuit breaker: {consecutive_failures} "
                        f"scans with no opportunities. Stopping."
                    )
                    exit_reason = "circuit_breaker"
                    break
                continue

            consecutive_failures = 0

            # Step 4: Display and execute the best opportunity
            for opp in opportunities:
                print_opportunity(opp)

            # Execute the top opportunity
            best = opportunities[0]

            # Log the opportunity
            log_opportunity(
                logfile, scan_count,
                event_title=best.event_title,
                event_id=best.event_id,
                num_outcomes=len(best.quotes),
                total_cost=best.total_cost,
                profit_pct=best.profit_pct,
                executable_size=best.executable_size,
                quotes=[
                    {
                        "token_id": q.token_id,
                        "outcome": q.outcome_name,
                        "price": q.best_ask_price,
                        "size": q.available_size,
                    }
                    for q in best.quotes
                ],
            )

            # Pre-trade drawdown guard: estimate worst-case loss if all
            # legs fill but the arb somehow loses (e.g. slippage).  The
            # maximum loss is the total cost of the position.
            estimated_cost = best.total_cost * float(int(best.executable_size))
            if session_profit - estimated_cost < -config.MAX_SESSION_DRAWDOWN:
                main_logger.warning(
                    "DRAWDOWN GUARD: est_cost=$%.2f, session_pnl=$%.2f, limit=$%.2f — skipping %s",
                    estimated_cost, session_profit,
                    config.MAX_SESSION_DRAWDOWN, best.event_title[:40],
                )
                append_log(logfile, {
                    "log_type": "drawdown_guard",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "scan_num": scan_count,
                    "event_title": best.event_title,
                    "event_id": best.event_id,
                    "estimated_cost": round(estimated_cost, 4),
                    "session_profit": round(session_profit, 4),
                    "drawdown_limit": config.MAX_SESSION_DRAWDOWN,
                })
                print(
                    f"\n  DRAWDOWN GUARD: next trade could cost ${estimated_cost:.2f}, "
                    f"session P&L=${session_profit:.2f}, limit=${config.MAX_SESSION_DRAWDOWN:.2f}. "
                    f"Skipping."
                )
                _sleep_with_summary(
                    scan_count, scan_t0, logfile,
                    len(multi_events), len(opportunities),
                    raw_count=len(raw_events),
                )
                continue

            print(f"\n  Executing best opportunity: {best.event_title[:50]}")
            result = execute_opportunity(best, ws_feed=ws_feed)
            trades.append(result)

            # Display result
            print_execution_result(result)

            # Log execution
            log_execution(
                logfile, scan_count,
                event_title=best.event_title,
                event_id=best.event_id,
                exec_mode=config.EXEC_MODE,
                all_filled=result.all_filled,
                num_legs=len(result.leg_fills),
                num_filled=result.num_legs_filled,
                total_latency_ms=result.total_latency_ms,
                total_cost_planned=best.total_cost,
                total_cost_actual=result.total_cost_actual,
                filled_size=result.total_filled_size,
                leg_details=[
                    {
                        "token_id": f.token_id,
                        "outcome": f.outcome_name,
                        "planned_price": f.planned_price,
                        "actual_price": f.actual_price,
                        "planned_size": f.planned_size,
                        "filled_size": f.filled_size,
                        "order_id": f.order_id,
                        "status": f.status,
                        "latency_ms": round(f.latency_ms, 1),
                        "error": f.error,
                    }
                    for f in result.leg_fills
                ],
            )

            # Track session P&L and per-event position costs
            if result.all_filled:
                cost = result.total_cost_actual * result.total_filled_size
                profit = (1.0 - result.total_cost_actual) * result.total_filled_size
                session_cost += cost
                session_profit += profit

                # Accumulate cost for this event to prevent re-trading
                eid = best.event_id
                position_costs[eid] = position_costs.get(eid, 0.0) + cost
                # Start cooldown so we don't re-trade this event next scan
                event_last_traded[eid] = scan_count
                remaining = config.MAX_POSITION_COST - position_costs[eid]
                # Persist position state immediately after every trade
                save_positions(position_costs, event_last_traded)
                print(
                    f"\n  Position [{eid}]: "
                    f"${position_costs[eid]:.2f} deployed, "
                    f"${max(remaining, 0):.2f} remaining"
                )
                print(
                    f"  Session P&L: ${session_profit:.2f} profit "
                    f"on ${session_cost:.2f} deployed "
                    f"({len(trades)} trades)"
                )

                # Drawdown check
                if session_profit < -config.MAX_SESSION_DRAWDOWN:
                    print(
                        f"\n  DRAWDOWN LIMIT: ${-session_profit:.2f} "
                        f">= ${config.MAX_SESSION_DRAWDOWN:.2f}. Stopping."
                    )
                    exit_reason = "drawdown_limit"
                    break

            # Log scan summary
            scan_ms = (time.monotonic() - scan_t0) * 1000
            log_scan_summary(
                logfile, scan_count,
                len(multi_events), len(opportunities), scan_ms,
                raw_count=len(raw_events),
            )

            # Also display other opportunities found
            if len(opportunities) > 1:
                print(f"\n  ({len(opportunities) - 1} other opportunities this scan)")

            if len(trades) < config.MAX_TRADES_PER_SESSION:
                print(f"\n  Sleeping {config.SCAN_INTERVAL_SECONDS}s...")
                time.sleep(config.SCAN_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        exit_reason = "ctrl_c"
        print("\n\n  Shutdown: Ctrl+C received.")
    except Exception as e:
        exit_reason = f"error: {e}"
        print(f"\n\n  Shutdown: unexpected error: {e}")
    finally:
        ws_feed.stop()
        print(f"\n  Logs saved to: {logfile}")

    # Session summary
    print(f"  Exit reason: {exit_reason}")
    print_session_summary(trades, scan_count, total_events_scanned)
    log_session_end(logfile, exit_reason, {
        "total_scans": scan_count,
        "total_trades": len(trades),
        "session_cost": round(session_cost, 4),
        "session_profit": round(session_profit, 4),
        "unique_events_traded": len(position_costs),
        "position_costs": {k: round(v, 4) for k, v in position_costs.items()},
    })


def _sleep_with_summary(
    scan_num: int,
    scan_t0: float,
    logfile: str,
    num_events: int,
    num_opps: int,
    raw_count: int = -1,
) -> None:
    """Log scan summary and sleep between scans."""
    scan_ms = (time.monotonic() - scan_t0) * 1000
    log_scan_summary(
        logfile, scan_num, num_events, num_opps, scan_ms,
        raw_count=raw_count,
    )
    print(f"  Sleeping {config.SCAN_INTERVAL_SECONDS}s...")
    time.sleep(config.SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
