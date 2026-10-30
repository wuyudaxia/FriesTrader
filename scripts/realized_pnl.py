#!/usr/bin/env python3
# Part of FriesTrader (https://github.com/YizhiSong/FriesTrader), moomoo adaptation
# MIT License -- see LICENSE
"""Sum this pipeline's own realized P&L for today and for this week from
trade_log.jsonl, per PHASE_B_TASK.md Step 7's loss-limit check.

moomoo's MCP has no "realized P&L by span" call like Robinhood's
get_realized_pnl, but the account is dedicated to this pipeline, so its
own confirmed sell fills are the complete record. A sell `order` entry
counts when it carries a confirmed fill (`fill_price` and
`fill_quantity` non-null) in the mode being scored; its realized P&L is
(fill_price - entry_price) * fill_quantity, where entry_price is the
average cost logged on that same cycle's stop_loss/take_profit/
conviction_trim/risk_check line for the symbol (the order entry may
also carry `entry_price` directly, which takes precedence).

Prints daily_realized_usd and weekly_realized_usd (Monday through
today) for pnl_pct.py, plus the fills they came from.
"""
import argparse
import datetime
import json
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trade-log", default="trade_log.jsonl")
    p.add_argument("--today", required=True, help="ISO date of this run")
    p.add_argument("--mode", required=True, choices=["dry_run", "paper", "live"],
                   help="only order entries logged in this execution mode count")
    args = p.parse_args()

    today = datetime.date.fromisoformat(args.today)
    week_start = today - datetime.timedelta(days=today.weekday())

    entries = []
    try:
        with open(args.trade_log) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except FileNotFoundError:
        entries = []

    # entry_price per (date, symbol) from the sell-side stage lines
    entry_prices = {}
    for e in entries:
        if e.get("stage") in ("stop_loss", "take_profit", "conviction_trim") and e.get("entry_price") is not None:
            entry_prices[(e.get("date"), e.get("symbol"))] = float(e["entry_price"])

    fills = []
    daily = 0.0
    weekly = 0.0
    for e in entries:
        if e.get("stage") != "order" or e.get("action") != "sell" or e.get("mode") != args.mode:
            continue
        if e.get("fill_price") is None or e.get("fill_quantity") is None:
            continue
        d = datetime.date.fromisoformat(e["date"])
        if d < week_start or d > today:
            continue
        entry_price = e.get("entry_price")
        if entry_price is None:
            entry_price = entry_prices.get((e["date"], e["symbol"]))
        if entry_price is None:
            fills.append({"date": e["date"], "symbol": e["symbol"], "skipped": "no entry_price found"})
            continue
        pnl = (float(e["fill_price"]) - float(entry_price)) * float(e["fill_quantity"])
        fills.append({"date": e["date"], "symbol": e["symbol"], "fill_price": e["fill_price"],
                      "fill_quantity": e["fill_quantity"], "entry_price": entry_price,
                      "realized_usd": round(pnl, 2)})
        weekly += pnl
        if d == today:
            daily += pnl

    print(json.dumps({
        "mode": args.mode,
        "week_start": week_start.isoformat(),
        "daily_realized_usd": round(daily, 2),
        "weekly_realized_usd": round(weekly, 2),
        "fills": fills,
    }))


if __name__ == "__main__":
    main()
