"""Display helpers for terminal output with box-drawing characters."""

from typing import List, Optional

from models import ArbitrageOpportunity, ExecutionResult, LegFill

BOX_W = 68


def _box_top(label: str = "", w: int = BOX_W) -> str:
    if label:
        pad = w - len(label) - 2
        return "+" + "- " + label + " " + "-" * max(pad, 0) + "+"
    return "+" + "-" * (w + 2) + "+"


def _box_mid(w: int = BOX_W) -> str:
    return "|" + "-" * (w + 2) + "|"


def _box_bot(w: int = BOX_W) -> str:
    return "+" + "-" * (w + 2) + "+"


def _box_line(text: str, w: int = BOX_W) -> str:
    return "| " + text.ljust(w) + " |"


def print_scan_header(scan_num: int) -> None:
    print(f"\n--- Scan #{scan_num} {'---' * 10}")


def print_opportunity(opp: ArbitrageOpportunity) -> None:
    """Print a compact summary of an arbitrage opportunity."""
    print(f"\n{_box_top(opp.event_title[:60])}")
    print(_box_line(f"Event: {opp.event_id}"))
    print(_box_line(f"Outcomes: {len(opp.quotes)}"))
    print(_box_mid())

    # Show each outcome's price
    for q in opp.quotes:
        name = q.outcome_name[:30].ljust(30)
        price = f"${q.best_ask_price:.3f}"
        size = f"{q.available_size:.0f} avail"
        print(_box_line(f"  {name}  {price}  ({size})"))

    print(_box_mid())
    print(_box_line(f"Total Cost:  ${opp.total_cost:.4f}"))
    print(_box_line(f"Profit:      ${opp.profit_per_share:.4f}/share  ({opp.profit_pct:.2f}%)"))
    print(_box_line(f"Exec Size:   {opp.executable_size:.0f} shares"))
    print(_box_line(f"Est Return:  ${opp.profit_per_share * opp.executable_size:.2f}"))
    print(_box_bot())


def print_no_opportunities(num_events: int) -> None:
    print(f"  No arbitrage found across {num_events} multi-outcome events.")


def print_execution_result(result: ExecutionResult) -> None:
    """Print the result of executing an arbitrage opportunity."""
    opp = result.opportunity
    tag = "FILLED" if result.all_filled else "PARTIAL"

    print(f"\n{_box_top(f'{tag} - {opp.event_title[:45]}')}")
    print(_box_line(f"Legs: {result.num_legs_filled}/{len(result.leg_fills)} filled"))
    print(_box_line(f"Latency: {result.total_latency_ms:.0f}ms"))
    print(_box_mid())

    # Show each leg's fill status
    for fill in result.leg_fills:
        status_icon = "[OK]" if fill.status == "filled" else "[!!]"
        name = fill.outcome_name[:25].ljust(25)
        if fill.actual_price is not None:
            price_str = f"${fill.actual_price:.3f}"
        else:
            price_str = f"${fill.planned_price:.3f} (planned)"
        size_str = f"{fill.filled_size:.0f}/{fill.planned_size:.0f}"
        print(_box_line(f"  {status_icon} {name} {price_str}  {size_str}"))

    print(_box_mid())

    if result.all_filled:
        profit = 1.0 - result.total_cost_actual
        profit_total = profit * result.total_filled_size
        print(_box_line(f"Actual Cost: ${result.total_cost_actual:.4f}/share"))
        print(_box_line(f"Profit:      ${profit:.4f}/share"))
        print(_box_line(f"Total P&L:   ${profit_total:.2f}"))
    else:
        print(_box_line(f"INCOMPLETE - {result.num_legs_failed} legs failed"))
        print(_box_line(f"Unwinding {result.num_legs_filled} filled legs..."))

    print(_box_bot())


def print_session_summary(
    trades: List[ExecutionResult],
    scan_count: int,
    events_scanned: int,
) -> None:
    """Print end-of-session summary."""
    if not trades:
        print("\n  No trades executed this session.")
        return

    filled = [t for t in trades if t.all_filled]
    partial = [t for t in trades if not t.all_filled]

    total_profit = 0.0
    total_cost = 0.0
    for t in filled:
        profit = (1.0 - t.total_cost_actual) * t.total_filled_size
        total_profit += profit
        total_cost += t.total_cost_actual * t.total_filled_size

    print(f"\n{'=' * 72}")
    print(f"  SESSION SUMMARY")
    print(f"{'=' * 72}")
    print(f"  Scans:            {scan_count}")
    print(f"  Events scanned:   {events_scanned}")
    print(f"  Trades executed:  {len(trades)}")
    print(f"  Fully filled:     {len(filled)}")
    print(f"  Partial/failed:   {len(partial)}")
    print(f"  Total cost:       ${total_cost:.2f}")
    print(f"  Total profit:     ${total_profit:.2f}")
    if total_cost > 0:
        roi = (total_profit / total_cost) * 100
        print(f"  ROI:              {roi:.2f}%")
    print(f"{'=' * 72}")
