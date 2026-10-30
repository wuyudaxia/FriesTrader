#!/usr/bin/env python3
# Part of FriesTrader (https://github.com/YizhiSong/FriesTrader), moomoo adaptation
# MIT License -- see LICENSE
"""Run Phase A's mechanical universe/signal arithmetic for ONE candidate
from its daily K-line series, per risk_rules.json/PHASE_A_TASK.md
Steps 1-2 -- so the model never hand-computes a moving average, a
60-day move, a volume ratio, or a 52-week proximity in prose.

Bars JSON is read from stdin: an array of daily bars, oldest first,
each at least {"date": "YYYY-MM-DD", "close": float, "volume": float}
(moomoo quote_history_kline, ktype=2 day, autype=1 forward-adjusted).
The series should span ~300 calendar days so the trend filter has
enough bars; the 60-calendar-day price move is taken from this same
series by date, not by counting bars back.

Prints one JSON object with every filter/signal verdict plus the raw
inputs behind each ratio, in the exact shapes PHASE_A_TASK.md logs.
"""
import argparse
import datetime
import json
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--today", required=True, help="ISO date of this run")
    p.add_argument("--current-price", type=float, required=True,
                   help="last price from quote_market_snapshot")
    p.add_argument("--high-52wk", type=float, required=True)
    p.add_argument("--low-52wk", type=float, required=True)
    p.add_argument("--market-cap", type=float, required=True)
    p.add_argument("--is-held", action="store_true",
                   help="currently held position -- always passes filters and the signal gate")
    # universe filters
    p.add_argument("--min-avg-volume", type=float, required=True)
    p.add_argument("--min-market-cap", type=float, required=True)
    p.add_argument("--max-market-cap", type=float, required=True)
    p.add_argument("--penny-threshold", type=float,
                   help="universe.penny_stock_price_threshold_usd; omit if the filter is disabled")
    p.add_argument("--trend-lookback", type=int,
                   help="universe.trend_filter_lookback_trading_days; omit if the filter is disabled")
    # signal thresholds
    p.add_argument("--price-move-60d-pct", type=float, required=True)
    p.add_argument("--volume-spike-multiple", type=float, required=True)
    p.add_argument("--pct-from-52wk-extreme", type=float, required=True)
    p.add_argument("--avg-volume-days", type=int, default=30)
    args = p.parse_args()

    try:
        bars = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid bars JSON on stdin: {e}"}), file=sys.stderr)
        sys.exit(1)
    bars = [b for b in bars if b.get("close") is not None]
    if not bars:
        print(json.dumps({"error": "no usable bars"}), file=sys.stderr)
        sys.exit(1)
    bars.sort(key=lambda b: b["date"])
    closes = [float(b["close"]) for b in bars]
    volumes = [float(b.get("volume") or 0) for b in bars]
    today = datetime.date.fromisoformat(args.today)

    result = {"bars_available": len(bars), "latest_bar_date": bars[-1]["date"]}

    # --- universe filters -------------------------------------------------
    avg_volume = sum(volumes[-args.avg_volume_days:]) / min(len(volumes), args.avg_volume_days)
    result["avg_volume"] = round(avg_volume)
    result["market_cap"] = args.market_cap
    reasons = []
    if avg_volume < args.min_avg_volume:
        reasons.append(f"avg daily volume {avg_volume:,.0f} below universe.min_avg_daily_volume "
                       f"({args.min_avg_volume:,.0f}) — excluded per universe filters")
    if args.market_cap < args.min_market_cap:
        reasons.append(f"market cap ${args.market_cap:,.0f} below universe.min_market_cap_usd "
                       f"(${args.min_market_cap:,.0f}) — excluded per universe filters")
    if args.market_cap > args.max_market_cap:
        reasons.append(f"market cap ${args.market_cap:,.0f} exceeds universe.max_market_cap_usd "
                       f"(${args.max_market_cap:,.0f}) — excluded per universe filters")
    if args.penny_threshold is not None and args.current_price < args.penny_threshold:
        reasons.append(f"penny stock (price ${args.current_price:.2f}, under ${args.penny_threshold:.2f}) "
                       f"— excluded per universe.penny_stock_filter_enabled")
    if args.trend_lookback is not None:
        if len(closes) < args.trend_lookback:
            result["trend_filter"] = {
                "skipped": True,
                "note": f"trend filter skipped — fewer than {args.trend_lookback} daily bars available",
            }
        else:
            ma = sum(closes[-args.trend_lookback:]) / args.trend_lookback
            below = (ma - args.current_price) / ma
            result["trend_filter"] = {"skipped": False, "moving_avg": round(ma, 4),
                                      "pct_below_trend": round(below, 6),
                                      "excluded": args.current_price < ma}
            if args.current_price < ma:
                reasons.append(f"{args.trend_lookback}-day MA ${ma:.2f}, current price "
                               f"${args.current_price:.2f} ({below:.1%} below trend) — excluded per "
                               f"universe.trend_filter_enabled")
    if args.is_held:
        result["passed_filters"] = True
        result["filter_reason"] = "currently held — always included"
        result["filters_would_have_said"] = reasons
    else:
        result["passed_filters"] = not reasons
        result["filter_reason"] = "; ".join(reasons) if reasons else None

    # --- signals ----------------------------------------------------------
    latest_close = closes[-1]
    cutoff = today - datetime.timedelta(days=60)
    window = [b for b in bars if datetime.date.fromisoformat(b["date"]) >= cutoff]
    if not window:
        window = bars
    close_60d_ago = float(window[0]["close"])
    move = abs(latest_close - close_60d_ago) / close_60d_ago
    signed_move = (latest_close - close_60d_ago) / close_60d_ago
    latest_volume = volumes[-1]
    prior = volumes[-(args.avg_volume_days + 1):-1] or volumes
    avg_volume_prior = sum(prior) / len(prior)
    spike = latest_volume / avg_volume_prior if avg_volume_prior else 0.0
    near_high = (args.high_52wk - args.current_price) / args.high_52wk
    near_low = (args.current_price - args.low_52wk) / args.low_52wk

    signals = []
    scores = []
    if move >= args.price_move_60d_pct:
        signals.append(f"price_move_60d: {signed_move:.4f} (close_60d_ago: {close_60d_ago} -> "
                       f"latest_close: {latest_close})")
        scores.append(move / args.price_move_60d_pct)
    if spike >= args.volume_spike_multiple:
        signals.append(f"volume_spike: {spike:.1f}x (latest_volume: {latest_volume:.0f} / "
                       f"avg_volume_{args.avg_volume_days}d: {avg_volume_prior:.0f})")
        scores.append(spike / args.volume_spike_multiple)
    if near_high <= args.pct_from_52wk_extreme:
        signals.append(f"near_52wk_high: {near_high:.4f} (current_price: {args.current_price} / "
                       f"high_52_weeks: {args.high_52wk})")
        scores.append(args.pct_from_52wk_extreme / near_high if near_high > 0 else float("inf"))
    elif near_low <= args.pct_from_52wk_extreme:
        signals.append(f"near_52wk_low: {near_low:.4f} (current_price: {args.current_price} / "
                       f"low_52_weeks: {args.low_52wk})")
        scores.append(args.pct_from_52wk_extreme / near_low if near_low > 0 else float("inf"))

    result["signal_inputs"] = {
        "window_start_date": window[0]["date"], "close_60d_ago": close_60d_ago,
        "latest_close": latest_close, "price_move_60d_pct": round(signed_move, 6),
        "latest_volume": latest_volume, "avg_volume_prior": round(avg_volume_prior),
        "volume_spike": round(spike, 4), "pct_from_52wk_high": round(near_high, 6),
        "pct_from_52wk_low": round(near_low, 6),
        "window_shorter_than_60d": window is bars and (today - datetime.date.fromisoformat(bars[0]["date"])).days < 60,
    }
    result["signal_check"] = signals if signals else "none"
    result["qualifies"] = bool(signals) or args.is_held
    result["magnitude_score"] = round(max(scores), 4) if scores else None
    result["pct_below_52wk_high"] = round(max(near_high, 0.0), 6)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
