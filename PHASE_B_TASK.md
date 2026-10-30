# Phase B — Re-verify, Risk Enforcement, Order Execution, and Logging (Automated Daily Task)

_Part of [FriesTrader](https://github.com/YizhiSong/FriesTrader), Copyright (c) 2026 Yizhi Song, MIT License. moomoo adaptation._

Automated second half of this pipeline (see `README.md`), run every
weekday 8:35am Central (5 min after 9:30am ET open) as a cloud routine,
against moomoo's hosted MCP server. Performs **Steps 4–9**, consuming
candidates from Phase A's `pending_proposals.jsonl`.

Authorized to place real live orders under a narrow condition (see Step
6's "Live-order gate" — Step 8 reuses the identical gate for buys).
**That authorization must be given explicitly, in advance, by whoever
operates this pipeline — after being warned that an unattended
scheduled task has no human confirmation at the moment of execution.**
Do not add, remove, or loosen any gate condition on your own judgment.

**Symbol format.** moomoo tools take `US.AAPL` (live tools: `code`;
simulated-trading tools: bare `AAPL` as `symbol` plus `market`). Every
log line records the bare ticker (`"symbol": "AAPL"`).

## Step 0 — Load state (do this first, every run)

1. Read `risk_rules.json` **fresh** — never cache across runs.
2. **Pick the account from `execution.mode`** — exactly one per run:
   - `"live"` → the real account `accounts.live_acc_id`. State comes
     from `account_positions` / `account_funds` (`currency` = `USD`);
     orders go through `trading_order_place`.
   - `"paper"` → the simulated account `accounts.paper_acc_id`
     (`market` = `accounts.paper_market_id`). State comes from
     `sim_trade_position_list` / `sim_trade_cash_info`; orders go
     through `sim_trade_input_order`.
   - `"dry_run"` → same state source as `"paper"`, but **no order tool
     is called at all** — every order is logged `would_execute: true`.
   Never read the other account's state in the same run, and never mix
   the two: a `paper` run that touches `trading_order_place`, or a
   `live` run that reads `sim_trade_position_list`, is a bug — stop and
   report it.
   Optionally confirm the ids once with `account_authorized_trd_accs`
   (live) / `sim_trade_account_list` (paper); if the configured id
   isn't in the list, log a `cycle_summary` noting it and stop — never
   substitute another account.
3. Determine today's day of week mechanically (e.g.
   `TZ='America/Chicago' date +'%A'`) — don't infer it from the date
   string. Needed for Step 7's weekend-gap check.
4. **Market open check**: call `quote_market_state` for the U.S.
   market. If it reports the market is closed today (holiday), log a
   `cycle_summary` with `"note": "US market closed"` and stop. Pull the
   trading calendar once with `quote_trading_days` (`market` = `US`,
   `start` = today minus 45 calendar days, `end` = today plus 45) —
   every "trading days since/until" count in Steps 4 and 7 is counted
   on this list, never guessed.
5. Read `pending_proposals.jsonl` (overwritten each Phase A run, holds only
   the latest run — use its `"stage": "thesis"` entries directly as
   today's candidates). If missing or empty, still run Step 5's
   sell-side checks on held positions, then log a `cycle_summary`
   noting nothing new to process — don't error.
6. Read `trade_log.jsonl` (if present):
   - **Idempotency — key off the proposal's own `date`, not today's.**
     Skip a candidate if `trade_log.jsonl` already has a `risk_check`/
     `order` entry for that symbol with a matching `proposal_date` (not
     the entry's top-level `date`, which reflects when the decision was
     made and changes daily even for a stale proposal). This matters
     because if Phase A ever fails to run, an un-refreshed proposal would
     otherwise look "new" every day and could be re-bought repeatedly;
     keying off `proposal_date` means it's decided once. `stop_loss`/
     `take_profit` are exempt — always run fresh.
   - **Paper cycle count**: number of **distinct dates** with a
     `cycle_summary` entry where `mode` is `dry_run` or `paper` — not
     raw entry count (same-day reruns count once). This represents
     validated days, not executions, and must be
     `>= execution.paper_min_cycles_before_live` before the
     live-order gate (Step 6 for sells, Step 8 for buys) can open.

## Step 4 — Classify candidates

Pull the positions list for this run's account (Step 0) — this
snapshot, taken before any of this cycle's sells execute, is also what
Step 5's stop-loss/take-profit checks use. For each position take its
quantity, average cost (`cost_price` / however the response names it)
and market value.

`direction: "avoid"` candidates aren't processed further (already
logged in Phase A). `exit_existing` candidates (Phase A's
recommendation to sell a currently-held position) go straight into
Step 6's sell-execution pass — selling is never gated on price or tax,
with one exception for thesis-driven exits only:

**Early-exit guard** (only if `early_exit_guard.enabled`): find this
holding period's opening buy in `trade_log.jsonl` (the executed `order`
in this mode that took the position from zero). If it was within the
last `early_exit_guard.window_trading_days` trading days (count on
Step 0's calendar), the `exit_existing` candidate must carry a
`new_fact_date` strictly later than that opening buy's `proposal_date`.
If it's missing or not later, don't sell — log `"stage": "risk_check",
"passed": false, "proposal_date": "<candidate's date>", "reason":
"early exit guard -- exit_existing within <N> trading days of the
<opening-buy date> entry without a fact newer than the entry thesis
(<new_fact_date or 'none'>) -- sell skipped"` and treat it as held for
the rest of the cycle (its stop-loss/take-profit/conviction-trim checks
still run). This never applies to stop_loss, take_profit, or
conviction_trim sells.

Split the remaining `direction: "long"` candidates, using this
snapshot, into:
- **new**: not a live open position — a genuine new entry, the only
  kind that consumes a slot.
- **held**: already a live open position — a potential top-up
  (Step 7). Top-ups never consume a slot and are always considered
  regardless of account fullness.

**This classification stays fixed for the rest of the cycle**, even
if a same-cycle sell later empties the position — otherwise a
symbol whose stop-loss fires this cycle would silently shift from
**held** to **new** by the time Step 7 runs, and Step 7's
same-cycle sell-then-buy guard (which operates on the **held**
group) would no longer find it there.

## Step 5 — Sell-side risk enforcement

### Stop-loss check (always runs, independent of new candidates)

Gather inputs, then let the script decide — do not hand-compute the
reference price, drawdown, stdev, or clamp. For each open position
(the snapshot pulled in Step 4):
- Pull a fresh quote via `quote_market_snapshot` (batched for all held
  symbols in one call) — `last_price` is the current price; keep the
  best `bid` for Step 6's sell estimate.
- Check `trade_log.jsonl` for whether any `take_profit` tier has fired
  for this position's current holding period (same "since quantity
  last reached zero" scope as the take-profit check below).
- If a tier has fired, pull daily `high` bars via `quote_history_kline`
  (`ktype` = 2, `autype` = 1) from the holding period's entry date (the
  buy that started it from zero) through yesterday, for `--daily-highs`.
- If `risk_rules.json`'s `stop_loss.mode` is `"volatility_scaled"` and
  the current price is below the stop reference price (below average
  cost, or below the trailing high once a take-profit tier has fired --
  even if still at a gain on average cost), pull the last
  `stop_loss.volatility_lookback_trading_days` trading days of daily
  closes via `quote_history_kline` (`ktype` = 2, `autype` = 1; request
  ~30 calendar days back to cover weekends/holidays), oldest first
  through yesterday, for `--daily-closes`. Skip this pull only when the
  current price is at or above the stop reference price -- a
  non-positive drawdown can never meet a positive `stop_pct`.

Run:
`python3 scripts/stop_loss.py --average-cost <avg cost> --current-price <fresh last_price> --mode <stop_loss.mode> --hard-stop-pct <stop_loss.hard_stop_pct> --volatility-multiplier <stop_loss.volatility_stdev_multiplier> --min-stop-pct <stop_loss.min_stop_pct> --max-stop-pct <stop_loss.max_stop_pct> --fallback-stop-pct <stop_loss.fallback_stop_pct> --min-bars 10 [--daily-closes <comma-separated closes>] [--take-profit-tier-fired --daily-highs <comma-separated highs> --trailing-high-since <entry date>]`
and use its JSON output directly (`stop_reference_basis`,
`stop_reference_price`, `drawdown_pct`, `stop_pct_used`, `stdev_20d`
when computed, `fallback_reason` when the fallback applied,
`triggered`, `action`) rather than recomputing any of it. This only
changes what the stop protects — it does not affect the take-profit
gain calculation below, which always measures gain from average cost
regardless of `stop_reference_basis`.

**If the script fails to run**, do not guess a result: treat this
position as if a loss-limit breach applied this cycle (`entries_halted
= true` for new entries/top-ups, this position itself excluded from
any sell decision) and log `"stage": "stop_loss"` with
`"stop_pct_used": null, "triggered": false, "action":
"halt_entries_check_manually", "notes": "stop_loss.py failed to run —
verify this position's stop manually before next cycle"`. Do not fall
back to manual computation.

If `triggered` is true — immediate full-position sell (the whole
quantity), no thesis review, never blocked by a loss-limit halt,
executed in Step 6's sell-execution pass. A good thesis never cancels
a stop-loss — see `risk_rules.json`'s note. Log `"stage": "stop_loss"`
with `entry_price` (the average cost) and `current_price`, plus:
- the script's `stop_pct_used`, `stop_reference_basis`,
  `stop_reference_price`
- `trailing_high_since`, when trailing
- the script's own `"notes": "gain, stop not computed"` as-is, when
  `stop_pct_used` is null
- `stdev_20d`/`fallback_reason`, when present

### Take-profit check (always runs, independent of new candidates, tiered partial sells)

Using the same pull as the stop-loss check (no need to call again —
average cost, quantity, and fresh price as they stood at the start of
this step, before any of this cycle's sells execute in Step 6), check
`trade_log.jsonl` for `"stage": "take_profit"` entries for this symbol
at each tier's exact `gain_pct`, logged since the position's quantity
last reached zero (a full exit) — collect the `gain_pct` values
already fired this holding period.

Run:
`python3 scripts/take_profit.py --average-cost <avg cost> --current-price <fresh last_price> --quantity <quantity> --tiers <risk_rules.json take_profit.tiers as "gain_pct:sell_fraction" pairs, e.g. "0.15:0.25,0.30:0.25,0.50:0.25"> [--already-fired <comma-separated gain_pct values already fired this holding period>] --whole-shares`
(`--whole-shares` whenever `order_execution.whole_shares_only` is
true) and use its JSON output directly (`gain_pct`, `tiers_status`,
`fired_this_cycle`, `triggered`, `action`) rather than recomputing any
of it — the script already handles the ascending-order,
cascading-quantity logic for **when a single cycle's gain has jumped
past more than one not-yet-fired tier at once**, and floors each
tier's `quantity_sold` to whole shares. A tier whose floored quantity
is 0 (`sell_skipped_zero_shares: true`) is still marked fired — log it
as such so it is never re-attempted — but produces no order.

**If the script fails to run**, treat it like a stop-loss script
failure: `entries_halted = true` for new entries/top-ups this cycle,
exclude this position from any sell decision, and log `"stage":
"take_profit"` with `"triggered": false, "action":
"halt_entries_check_manually", "notes": "take_profit.py failed to run
— verify this position's tiers manually before next cycle"`. Do not
fall back to manual computation.

**If `fired_this_cycle` is non-empty** — each fired tier with a
non-zero `quantity_sold` is executed in Step 6's sell-execution pass,
no thesis review, never blocked by a loss-limit halt (it's an exit,
not a new entry). Log one line per entry in it (already in ascending
order), `"stage": "take_profit", "triggered": true, "action":
"sell_partial_position"` (or `"triggered": false,
"sell_skipped_zero_shares": true` for a zero-share tier), with:
- `"entry_price"` (average cost), `"current_price"`
- `"tier_gain_pct"`, `"sell_fraction"`, `"quantity_before"`,
  `"quantity_sold"` straight from that entry
- the script's top-level `"gain_pct"` and `"tiers_status"`

**If `fired_this_cycle` is empty**, log one line with the script's
`gain_pct` and `tiers_status`, `"triggered": false, "action":
"hold_monitor"`.

Once all three tiers have fired, the remaining quantity is held long
indefinitely — only the stop-loss check above still applies to it.
Tiers become eligible again only after the position is fully closed to
zero shares and a new entry is later opened (a genuinely new holding
period, not a top-up).

### Conviction-trim check (held positions only, mechanical rebalance-down)

Skip entirely if `risk_rules.json`'s `conviction_trim.enabled` is `false`.
Otherwise, for every **held** position, using this cycle's fresh
`conviction` and `target_size` (`position_sizing.conviction_pct[<conviction>]
× total_value`, with `total_value` from this run's funds call — pull it
now if not yet available), look back through this symbol's `risk_check`
entries in `trade_log.jsonl`, most recent first, and count consecutive
entries (not including this cycle, and not crossing back over a prior
full exit to zero) where `conviction != "high"` (i.e. `"low"` or
`"medium"`) and `current_position_value` exceeded `target_size` by more
than `conviction_trim.overweight_trigger_pct`.

Run:
`python3 scripts/conviction_trim.py --conviction <this cycle's conviction> --current-position-value <current_position_value> --target-size <target_size> --overweight-trigger-pct <conviction_trim.overweight_trigger_pct> --prior-consecutive-overweight-cycles <count from the lookback above> --min-overweight-conviction-cycles <conviction_trim.min_overweight_conviction_cycles> --bid-price <fresh bid>`
and use its JSON output directly (`overweight_pct`, `qualifies_this_cycle`,
`consecutive_cycles`, `triggered`, `trim_dollar_amount`, `trim_quantity`,
`action`) rather than recomputing any of it — with `--bid-price` the
script floors the trim to whole shares and reports `triggered: false`
when that comes to 0 shares.

**If the script fails to run**, treat it like a stop-loss/take-profit
script failure: `entries_halted = true` for new entries/top-ups this
cycle, exclude this position from any sell decision, and log `"stage":
"conviction_trim"` with `"triggered": false, "action":
"halt_entries_check_manually", "notes": "conviction_trim.py failed to
run — verify this position manually before next cycle"`.

**If `triggered` is true** — sell `trim_quantity` shares, executed in
Step 6's sell-execution pass, never blocked by a loss-limit halt, same
as stop-loss/take-profit. Log `"stage": "conviction_trim"` with the
script's `overweight_pct`, `consecutive_cycles`, `trim_dollar_amount`,
`trim_quantity`, `"triggered": true, "action": "sell_partial_position"`,
plus `entry_price` (average cost) and `current_price`.

**If `triggered` is false**, log one line with the script's
`overweight_pct`, `qualifies_this_cycle`, `consecutive_cycles`,
`"action": "hold_monitor"`.

Applies only to **held** positions — never a **new**-group candidate,
which has no existing position to be overweight in.

## Step 6 — Sell-side execution

**Execute sells now — every stop-loss trigger, fired take-profit tier,
conviction-trim trigger, and Step 4's `exit_existing` candidates:** run
this procedure for each, before touching anything buy-side below — a
sell must actually clear before its freed cash/slot can be counted
toward a same-cycle buy, and before today's realized P&L (loss-limit
check, further down) can see it.

moomoo has no "preview" order tool, so the pre-trade check is the
broker's own capacity query:

1. **Always** run the pre-trade check first — never places anything:
   - `live`: `account_trading_info` (`acc_id` = live id, `code` =
     `US.<ticker>`, `order_type` = `order_execution.order_type`).
   - `paper`/`dry_run`: `sim_trade_max_buy_sell` (paper `acc_id`,
     `market`, bare `symbol`).
   The sell quantity must be `<=` the reported maximum sellable
   quantity. Log the comparison as `"pre_trade_check": "<which tool>
   max_sell_qty <N> >= <qty>"`.
2. If the check fails (quantity exceeds the maximum, the symbol is
   reported as not tradable, or the call errors), do not proceed to
   placement regardless of mode; log the response verbatim and treat as
   rejected.
3. Otherwise, branch on `execution.mode` (fresh from Step 0):

   - `execution.mode == "dry_run"`: log
     `"stage": "order", "mode": "dry_run", "would_execute": true` and
     stop. **No order tool is called.**

   - `execution.mode == "paper"`: call `sim_trade_input_order` with
     `acc_id` = `accounts.paper_acc_id`, `market` =
     `accounts.paper_market_id`, `symbol` = bare ticker, `order_type` =
     3 (market), `order_side` = the Side enum's SELL value, `qty` =
     the share quantity, `text` = `"FriesTrader <reason> <date>"`. Then
     confirm the fill: call `sim_trade_history_order_list` for the
     paper account (`time_begin` = start of today) and find this
     `order_id`; if its state isn't terminal, wait
     `order_execution.fill_check_wait_seconds` and check once more —
     never poll more than twice. Log `"stage": "order", "mode":
     "paper", "placed": true, "account": "paper", "order_id": "<id>",
     "order_state": "<state>", "fill_price": <average fill price if
     filled, else null>, "fill_quantity": <filled qty, else null>`.

   - `execution.mode == "live"` — **Live-order gate, ALL must be true:**
     - `execution.mode == "live"`
     - paper cycle count `>= execution.paper_min_cycles_before_live`
     - the pre-trade check for this order passed

     - **Gate open**: call `trading_order_place` with `acc_id` =
       `accounts.live_acc_id`, `code` = `US.<ticker>`, `side` = `SELL`,
       `order_type` = `order_execution.order_type`, `qty` = the share
       quantity, `time_in_force` = `order_execution.time_in_force`,
       `session` = `order_execution.session`, `remark` =
       `"FriesTrader <reason> <date>"` (no `price` for a MARKET order).
       **If the response has `need_order_confirm: true`, do NOT call
       `trading_order_confirm`** (`order_execution.on_need_order_confirm`
       is `reject`): log `"placed": false, "pre_trade_check":
       "need_order_confirm true — rejected per
       order_execution.on_need_order_confirm", "confirm_prompt":
       "<verbatim>"` and treat as rejected — a broker-side risk warning
       is always surfaced to the human, never auto-acknowledged by an
       unattended run. Otherwise confirm the real fill before logging —
       the placement response alone is not enough:
       1. Call `account_orders_detail` with this `order_id` (and the
          `exchange` the placement response returned).
       2. If the state is terminal (filled, partially filled, cancelled,
          rejected, failed), use it.
       3. Otherwise wait `order_execution.fill_check_wait_seconds` and
          check once more; use whatever state comes back, terminal or
          not — never poll more than twice or block the cycle waiting
          for a fill.
       Log `"stage": "order", "mode": "live", "placed": true, "account":
       "live", "order_id": "<id>", "order_state": "<confirmed state>",
       "fill_price": <average fill price if filled/partially filled,
       else null>, "fill_quantity": <filled quantity, else null>` in
       addition to the pre-trade `quote_bid`/`quantity` estimate already
       logged (not in place of it) — the log should show both the
       estimate and the confirmed real outcome.
     - `execution.mode == "live"` but cycle count still under threshold:
       do **not** place. Log
       `"stage": "order", "mode": "live_blocked_insufficient_cycles", "would_execute": true, "placed": false`
       with current vs. required count.

Every sell `order` entry additionally carries `"action": "sell"`,
`"reason": "<stop_loss|take_profit|conviction_trim|exit_existing>"`,
`"quote_bid"`, and `"entry_price"` (the average cost at the time of the
sale — `scripts/realized_pnl.py` reads it for the loss-limit check).
Never invent/guess a field value — if a tool call fails, log the
failure and skip that candidate. Every `order` entry must carry
`proposal_date` (Step 0's idempotency key).

**Wash-sale flag on sells (informational only, never blocks a sell):**
whenever the stop-loss check triggers, a take-profit tier fires, a
conviction-trim fires, or an `exit_existing` sell is processed, and
that specific sale realizes a loss (a stop-loss sell is always a loss
by definition; check take-profit/`exit_existing` case by case against
the fill), check the same `wash_sale_avoidance.linked_acc_ids` for a
purchase of that symbol within `lookback_window_days` days before today
(`account_fills_history` per linked account, `trd_market` = `US`,
`code` = `US.<ticker>`, `start` = today minus the window).

A qualifying purchase alone is not enough to flag — the wash-sale rule
disallows the loss by rolling it into the cost basis of stock you still
hold, so if nothing of that symbol remains held anywhere after this
sale, there is no replacement position for a disallowed loss to attach
to and it is not a wash sale, whatever the calendar gap. Before adding
the flag, call `account_positions` for every account in
`wash_sale_avoidance.linked_acc_ids` and confirm at least one of them
still holds a nonzero quantity of the symbol after this sale, sourced
from a purchase inside the lookback window. Only then add
`"wash_sale_flag": true, "wash_sale_note": "possible wash sale -- <symbol> was bought in account <acc_id> on <date>, within <lookback_window_days> days of this sale, and a replacement position remains held in account <holding_acc_id> -- this loss may be disallowed for tax purposes"`
to that sell's `order` log entry. Purely informational for the human's
own tax reconciliation — it never blocks, delays, or resizes the sell
itself, and it does not require `wash_sale_avoidance.enabled` to be
`true`. Skip the whole flag check in `paper`/`dry_run` (a simulated
sale has no tax consequence).

**Re-pull fresh account state (now reflects this cycle's executed
sells, not an estimate):** call the funds tool for this run's account
(`account_funds` with `currency` = `USD`, or `sim_trade_cash_info`) for
`total_value` (total assets / net asset value) and `cash`, and the
positions tool again (for the live open position count) — the earlier
pull is now stale for any sell that actually executed above. Use these
fresh values for Step 7's capacity computation, loss-limit check, and
candidate sizing below. In `dry_run` mode these won't have changed
(nothing executed), which is expected.

## Step 7 — Buy-side risk enforcement and sizing

**Compute capacity (using Step 6's fresh re-pulled account state, not
an estimate):** `open_slots = max_concurrent_positions - (live
positions per the re-pulled positions call above)`. Only
**new**-group candidates (Step 4's classification) consume a slot;
fixed for the rest of this cycle unless one gets approved below.

**If `open_slots <= 0`**: no **new** candidate can be approved this
cycle. Skip the weekend-gap search and buy gate below for every
**new**-group candidate — log instead:
`"stage": "risk_check", "passed": false, "proposal_date": "<candidate's date from pending_proposals.jsonl>", "reason": "no open slots this cycle (X of Y max already held/approved) — skipped without staleness re-check"`.
**held** group is unaffected.

**If `open_slots > 0`**: **new** group continues normally (slots may
still run out mid-ranking via ordinary per-candidate concurrency check).

**No same-cycle sell-then-buy**: if a symbol's stop-loss fired, any
take-profit tier fired, or a conviction-trim fired earlier this cycle
(Step 5), it is not eligible for a top-up this same cycle, regardless
of thesis or conviction — drop it from the **held** group before
continuing, logging
`"stage": "risk_check", "passed": false, "position_action": "top_up", "reason": "stop-loss/take-profit fired this cycle — not eligible for a same-cycle top-up"`.
This applies unconditionally (any mode) since it's about not producing
a self-contradictory sell-and-buy decision within one cycle, not about
whether the sell actually executed. It's a normal top-up candidate
again starting next cycle (subject to the buy gate below, including
the sell re-entry lock). Checking this before the weekend-gap search
and buy gate below avoids spending news-search budget or API calls on
a candidate that's getting dropped regardless.

For every **new** candidate not short-circuited above, and every
**held** candidate not dropped above:

### Weekend gap (Monday runs only)

A Friday proposal is staler than an overnight one — 2.5 days vs ~16
hours. **If today is Monday**:

1. Before the price-staleness check, run **one additional targeted search
   per pending proposal** covering Saturday/Sunday (earnings, M&A,
   guidance, macro) — web search and/or moomoo `news_search` — separate
   from and not counted against `cadence.news_search_budget_per_cycle`.
   Same sourcing rules as Phase A's thesis `sources` field (prefer
   primary/major-outlet sources; cite whatever you used).
2. If anything materially contradicts the thesis/invalidation criteria,
   drop it — log
   `"stage": "risk_check", "passed": false, "proposal_date": "<candidate's date from pending_proposals.jsonl>", "reason": "weekend news invalidated thesis: <what you found>", "sources": ["Outlet Name: https://..."]`
   — don't process further.
3. If nothing turns up, proceed to the price-based check.

Other weekdays: skip straight to the price-based check.

### Buy gate (every day)

**Buys only — new entries and top-ups; never applies to
stop_loss/take_profit/conviction_trim/exit_existing sells, which are
never gated on price or tax considerations.** One script call answers
every independent, per-symbol condition that can block this candidate;
`position_sizing.py`'s slot/cash allocation across the whole candidate
list is a separate, later check further down in this same step, since
it depends on other candidates, not just this one.

Pull a fresh quote via `quote_market_snapshot` (batched for all
candidates in one call) — re-verify against this morning's open, not
Phase A's prior-close price. Use the best `ask` as `fresh_ask`.

**Gather inputs, then let the script decide — do not hand-compute any
gap, average, or lock condition:**
- `--fresh-ask`: this morning's fresh ask.
- `--thesis-price`: Phase A's thesis-time `current_price` (on the
  `thesis` line).
- `--daily-closes`: the last `entry_extension.lookback_trading_days`
  trading days of daily closes via `quote_history_kline` (`ktype` = 2,
  `autype` = 1; request ~30 calendar days back to cover
  weekends/holidays).
- Wash-sale inputs, only if `wash_sale_avoidance.enabled` is `true`
  **and** `execution.mode` is `live` (pass `--wash-sale-enabled` and
  `--wash-sale-lookback-days <wash_sale_avoidance.lookback_window_days>`;
  omit both otherwise — a simulated account has no tax consequence):
  loss-sale dates come from two places, merged into
  `--loss-sale-dates <comma-separated ISO dates>` (omit if none):
  - this pipeline's own account: every `live` sell `order` of this
    symbol in `trade_log.jsonl` inside the window whose `fill_price` is
    below its `entry_price`;
  - every other acc_id in `wash_sale_avoidance.linked_acc_ids`:
    `account_fills_history` (`trd_market` = `US`, `code` =
    `US.<ticker>`, `start` = today minus the window) — fill history
    carries no cost basis, so **any** SELL fill there counts as a
    possible loss sale (conservative: it can only block a buy, never a
    sell).
  Also pass `--today <today's date, ISO>`.
- Sell re-entry lock inputs, whenever `trade_log.jsonl` has this
  symbol's most recent `order` entry (in this mode) as a sell with a
  confirmed fill (`fill_quantity` non-null) — a `dry_run` sell never
  actually reduces the position, so omit these inputs entirely in that
  case (evaluate the symbol normally). If it did execute, pass
  `--last-sell-reason <reason>`, `--last-sell-price <that entry's
  fill_price>`, `--last-sell-date <that entry's date>`, and
  `--last-sell-was-gain <true|false>` — that entry's `fill_price`
  compared to its logged `entry_price`. If the sale closed at a gain,
  also pass `--reentry-lock-max-trading-days
  <sell_reentry_lock.gain_close_max_trading_days>` and
  `--trading-days-since-sell <trading days elapsed since that sell
  date, counted on Step 0's calendar>`.
- Earnings blackout inputs, only if `earnings_blackout.enabled` is
  `true`. moomoo's MCP has no next-earnings-date tool, so source the
  date in this order and log which one you used as
  `"earnings_date_source"`: (1) a company-confirmed date from
  `news_search` (`keyword` = `<ticker> earnings`, `news_type` =
  `NOTICE`, then `ALL`) or web search — the company's own IR
  announcement of its next report date; (2) a major-outlet or
  data-provider listing (Nasdaq.com, Zacks, Yahoo Finance) marked
  confirmed. Pass `--earnings-blackout-trading-days
  <earnings_blackout.blackout_trading_days>` and
  `--trading-days-until-earnings <trading days from today to that
  date on Step 0's calendar, 0 if today>`. If no date can be found
  (e.g. an ETF) or only an unconfirmed estimate exists, omit
  `--trading-days-until-earnings` and log
  `"earnings_date_source": "unknown"` on the candidate's `risk_check`
  line — the buy is **not** blocked on an unknown date (see README
  "Known gaps"), but the gap is recorded.

Run:
`python3 scripts/entry_gate.py --fresh-ask <ask> --thesis-price
<thesis current_price> --entry-price-gap-max-pct
<entry_price_gap.max_pct> --daily-closes <comma-separated closes>
--max-extension-pct <entry_extension.max_extension_pct>
[--wash-sale-enabled --wash-sale-lookback-days <N> --loss-sale-dates
<dates>] --today <date> [--last-sell-reason <reason> --last-sell-price
<price> --last-sell-date <date> --last-sell-was-gain <true|false>
[--reentry-lock-max-trading-days <N> --trading-days-since-sell <N>]]
[--earnings-blackout-trading-days <N> [--trading-days-until-earnings
<N>]]`
and use its JSON output (`entry_price_gap`, `entry_extension`,
`wash_sale_avoidance`, `sell_reentry_lock`, `earnings_blackout`, `passed`,
`blocking_conditions`, `action`) directly rather than recomputing any
of it.

**If the script fails to run**, do not guess a result — skip the buy
this cycle and log `"stage": "risk_check", "passed": false,
"proposal_date": "<candidate's date>", "reason": "entry_gate.py failed
to run — buy skipped this cycle, verify manually"`.

**If `passed` is false**, skip the buy this cycle regardless of how
strong the thesis still reads — mechanical, not a judgment call, same
as a stop-loss. The candidate isn't blacklisted, just re-evaluated
fresh on the next Phase A run (or, for the re-entry lock, once its own
clearing condition is met). Log one `"stage": "risk_check", "passed":
false, "proposal_date": "<candidate's date>"` line per entry in
`blocking_conditions`, using the matching reason text:
- `entry_price_gap`: `"reason": "price gapped <gap_pct>% above
  thesis-time price ($<thesis-price> -> $<fresh-ask>), exceeds
  entry_price_gap.max_pct (<threshold>) -- buy skipped this cycle"`
- `entry_extension`: `"reason": "price <extension_pct>% above its
  <N>-day average ($<moving_avg> -> $<fresh-ask>), exceeds
  entry_extension.max_extension_pct (<threshold>) -- buy skipped this
  cycle"`
- `wash_sale_avoidance`: `"reason": "wash sale guard -- <symbol> was
  sold at a loss within the <lookback_window_days>-day wash-sale
  window (<matching_loss_sale_date>)"` (add `"position_action":
  "top_up"` if it's a top-up candidate)
- `sell_reentry_lock`: `"reason": "sell re-entry lock — current price
  <fresh-ask> is above the <last-sell-price> it was sold at on
  <last-sell-date> (reason: <last-sell-reason>)"` (for a gain-closed
  sell still locked only on the time condition, append `", N of <max>
  trading days elapsed"`)
- `earnings_blackout`: `"reason": "earnings blackout -- next earnings
  <date> is <trading_days_until_earnings> trading days away, within
  earnings_blackout.blackout_trading_days (<threshold>) -- buy skipped
  this cycle"` (add `"position_action": "top_up"` if it's a top-up
  candidate)

**If `passed` is true but `entry_price_gap.gap_pct` is still
non-trivial**, re-check against the thesis's `invalidation` criteria —
same sourcing rules as Phase A's thesis `sources` field (prefer
primary/major-outlet sources; cite whatever you used), citing whatever
explains the gap in a `"sources"` field on the resulting log line; if
it plausibly invalidates the thesis even under the hard ceiling, drop
it — log `"stage": "risk_check", "passed": false, "proposal_date":
"<candidate's date>", "reason": "overnight news invalidated thesis:
<what you found>", "sources": ["Outlet Name: https://..."]`, same as
the weekend-gap check above.

**Loss-limit halt check (always runs, gates all new entries and top-ups):**
Compute the loss-limit base fresh this cycle:
- `total_value` = total assets / net asset value from Step 6's fresh
  funds call.
- `net_deposits_usd`:
  - `live`: `account_cash_logs` for the live account — sum of all
    deposits minus all withdrawals, all time. Log the components.
  - `paper`/`dry_run`: from `sim_trade_cash_info` — net asset value
    minus (realized P&L + unrealized P&L) as that call reports them;
    if it doesn't report P&L, use the paper account's initial capital
    as shown in `sim_trade_account_list`. Log which one you used as
    `"net_deposits_source"`.
- Realized P&L today and this week: run
  `python3 scripts/realized_pnl.py --trade-log trade_log.jsonl --today <today> --mode <execution.mode>`
  — it sums this pipeline's own confirmed sell fills (the account is
  dedicated to this pipeline, so its log is the complete record) and
  prints `daily_realized_usd` / `weekly_realized_usd`. Include the
  cycle's own sells executed in Step 6 above (they're already logged
  by now).

Then run
`python3 scripts/pnl_pct.py --daily-realized-usd <daily_realized_usd> --weekly-realized-usd <weekly_realized_usd> --net-deposits-usd <net_deposits_usd> --daily-limit-pct <loss_limits.daily_loss_limit_pct_of_account> --weekly-limit-pct <loss_limits.weekly_loss_limit_pct_of_account>`
and use its JSON output (`daily_pnl_pct`, `weekly_pnl_pct`,
`entries_halted`, `halt_reason`) directly. If either script fails to
run or any of the calls above can't be determined cleanly, fail safe:
treat as breached (`entries_halted = true`). Halts both new entries and
top-ups (a top-up still spends cash/exposure, even though it skips the
concurrency check).
Log as `"stage": "loss_limit_check"`, including `mode`,
`daily_realized_usd`, `weekly_realized_usd`, `net_deposits_usd`,
`net_deposits_source`, and `total_value`.

**Candidate priority order — new entries and top-ups compete equally
(decide before any per-candidate check):**
Merge **new** and **held** groups from Step 4's classification
(excluding new-group candidates already rejected by this step's
capacity short-circuit, same-cycle guard, weekend-gap check, or buy
gate above) into one list. **Do not hand-sort or hand-compute any of
this — gather the inputs below and let the scripts decide.**

**Gather, for every candidate in the merged list:** `symbol`,
`conviction`, `risk_flags` (omit the key entirely if the thesis
disclosed none — an omitted key and an empty array mean different
things to the ranking script below), `pct_below_52wk_high` (omit if
not available), `group` (`"new"` or `"held"`), `ask_price` (this
morning's fresh ask from the snapshot above), and — for **held**
candidates only — `current_position_value` (quantity from the fresh
re-pulled positions × fresh last price). Also use the re-pulled
`total_value` and `cash` from the funds call above (`cash` is the
starting `cash_remaining`), and `concurrent_positions_start` (the
`open_slots` computation above).

Rank the candidates, then size them — pipe the candidate list (a JSON
array) through both scripts in sequence (directly chainable):
`python3 scripts/rank_candidates.py | python3 scripts/position_sizing.py --total-value <total_value> --cash-start <cash> --concurrent-positions-start <concurrent_positions_start> [--entries-halted] --max-position-pct <position_sizing.max_position_pct_of_account> --max-concurrent-positions <position_sizing.max_concurrent_positions> --min-cash-buffer-pct <position_sizing.min_cash_buffer_pct> --min-top-up-usd <position_sizing.min_top_up_usd> --min-top-up-pct-of-target <position_sizing.min_top_up_pct_of_target> --conviction-pct "<position_sizing.conviction_pct as high:0.20,medium:0.12,low:0.06>" --whole-shares`
(`--whole-shares` whenever `order_execution.whole_shares_only` is
true). `rank_candidates.py` sorts by conviction tier first (`high`
before `medium` before `low`), then `risk_flags` count ascending
within a tier (a missing `risk_flags` field sorts last, treated as
worst case), then `pct_below_52wk_high` descending (a missing field
sorts last in its tier) — a high-conviction top-up can end up ranked
ahead of a lower-conviction new entry and vice versa.
`position_sizing.py` then processes strictly in that ranked order,
floors each dollar amount to whole shares at `ask_price` (rejecting a
candidate whose share price exceeds its tier's dollar size, rather
than rounding up), and compounds the running cash/concurrency totals
as each candidate is approved. Pass `--entries-halted` whenever the
loss-limit check above (or a stop-loss/take-profit script failure)
halted entries this cycle — the sizing script then rejects every
candidate uniformly with the standard halt reason and leaves the
totals unchanged.

**Log `risk_flags` and `pct_below_52wk_high` as structured fields on
every risk_check entry from this sort — winners and rejections alike**
(the only place this survives, since `pending_proposals.jsonl` is
overwritten daily).

Use the final script's JSON output directly — rather than recomputing
any of it:
- its `results` array: one entry per candidate, in ranked order, each
  carrying `passed`, and depending on outcome: `reason`,
  `position_action: "top_up"`, `dollar_amount`, `quantity`,
  `ask_price`, `current_position_value`, `target_size`, `headroom`,
  `concurrent_positions_after`, `cash_remaining_after`,
  `cash_buffer_after_pct`
- top-level `cash_remaining_final` and
  `concurrent_positions_after_final`

A **new**-group candidate rejected purely for lack of slots carries
the script's own `"concurrent_positions_after (N) exceeds
max_concurrent_positions (M) — cap filled by higher-priority
candidates this cycle"` wording, to show it's scarcity, not quality.

**If either script fails to run**, do not guess a result: reject every
still-pending candidate this cycle — log `"stage": "risk_check",
"passed": false, "reason": "rank_candidates.py/position_sizing.py
failed to run — no orders attempted this cycle, verify manually"` for
each — rather than falling back to manual computation.

For each candidate, log `"stage": "risk_check"` with that candidate's
`results` entry fields verbatim.

Every `risk_check` entry must include `proposal_date` (copied from the
candidate's `"date"` in `pending_proposals.jsonl` — Step 0's idempotency
key) and, for `direction: "long"`, `risk_flags` and `pct_below_52wk_high`
(for auditing the priority sort). Top-up entries must also include
`"position_action": "top_up"`.

## Step 8 — Buy-side execution

**Execute approved buys:** for every candidate Step 7's sizing
approved (new entries and top-ups), in ranked order, run the exact
same pre-trade check → mode branch → live-order gate → place → confirm
procedure as Step 6's "Execute sells now" — with these differences:
- the pre-trade check compares the buy `quantity` against the maximum
  **purchasable** quantity (`account_trading_info` /
  `sim_trade_max_buy_sell`);
- `side` = `BUY` (live) / the Side enum's BUY value (paper);
- the pre-trade estimate logged alongside each order is
  `quote_ask`/`quantity`/`dollar_amount` here (a buy fills near the
  ask), not the `quote_bid` used for sells, and `"action": "buy"`.

Never change `execution.mode` yourself. Every `order` entry must carry
`proposal_date` (same as Step 7's `risk_check` entries) — Step 0's
idempotency check matches against either a `risk_check` or `order`
entry.

## Step 9 — Logging

Append every decision to `trade_log.jsonl` — one JSON line each:
`stop_loss`, `take_profit`, `conviction_trim`, `loss_limit_check`,
`risk_check` (pass/fail, including Step 7's weekend-gap rejections,
buy-gate rejections (price-gap, extension, wash-sale, sell re-entry
lock, earnings blackout), and top-up evaluations), and `order` stages,
matching the shape already in `trade_log.jsonl`/
`trade_log_template.jsonl`. Top-up entries must include
`"position_action": "top_up"`.

**Every line — including the final `cycle_summary` — needs a real
`"timestamp"`** (`HH:mm:ss`, e.g. via `TZ='America/Chicago' date +'%H:%M:%S'`
— never guessed), no date prefix. Separate from `"date"`/`"proposal_date"`
— for readability only, never used for idempotency, paper-cycle count,
or other logic; only `date` and `proposal_date` are mechanical.

**Always append exactly one final line per run**, even if nothing else
happened:
```json
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "cycle_summary", "mode": "dry_run|paper|live", "candidates_considered": N, "orders_reviewed": N, "orders_placed": N}
```
Load-bearing — Step 0's paper cycle count depends on this line existing
every run, keyed off `"date"` (distinct dates), not `"timestamp"`.

**After appending, regenerate `trade_log_recent.md`** (full overwrite,
not append) — a short, plain-English recap of today's cycle for a quick
mobile/GitHub read, not another machine format. A `# YYYY-MM-DD`
heading with the mode in parentheses, then prose/bullet sections
covering only what actually happened this cycle (skip anything empty):
the loss-limit check result, including this cycle's computed
`net_deposits_usd`; each held position's stop-loss/take-profit status
(symbol, `stop_pct` used, gain/drawdown, and whether it triggered,
fired a tier, sold, or is just holding); each new-entry/top-up
candidate considered and its outcome in one line (approved and sized
— shares and dollars — or rejected and why); and any orders actually
placed (symbol, buy/sell, shares, dollar amount, fill state). This is a
readable render of what this cycle already decided — no new research,
no re-deciding anything. Convenience view only, not a second audit
trail: `trade_log.jsonl` is still the source of truth, and if the two
ever disagree, trust `trade_log.jsonl`.

### Saving to the repo

Once everything above is written, commit and push. This works whether
the checkout is on `main` or a detached HEAD — don't check which, and
don't try to fix it:
```
git add trade_log.jsonl trade_log_recent.md
git commit -m "Phase B run <date> <timestamp>"
git push origin HEAD:main
```
If the push is rejected, run `git pull --rebase origin main` once, then
`git push origin HEAD:main` once more. If it still fails, stop retrying
and report the exact error in the final summary. The commit stays
local, so it can still be recovered.
**Never** run `git checkout`, `git switch`, `git reset`, `git stash`,
`git merge`, `git branch -f`, or any force-push to "fix" branch state.
In an unattended run these can be blocked by the permission system,
stalling the run with real fills never pushed.

## Hard rules

- Never change `execution.mode`, `accounts.*`, or any `risk_rules.json`
  value.
- Never call `trading_order_place` unless the live-order gate (Step 6
  for sells, Step 8 for buys — same conditions) is open at that moment.
  Never call `trading_order_confirm` at all. Never call
  `sim_trade_input_order` unless `execution.mode` is `"paper"`. Never
  call `trading_order_replace`/`trading_order_cancel`/
  `sim_trade_modify_order`/`sim_trade_cancel_order` — this pipeline
  only ever places DAY market orders and leaves them alone.
- A "high conviction" thesis never overrides a failed mechanical check.
- If required data can't be retrieved (funds, positions, calendar),
  fail safe — treat the check as failed/halt new entries — and log
  exactly what failed.
- The wash-sale guard (Step 7's buy gate) only ever blocks a buy. It
  must never block, delay, or resize a
  stop_loss/take_profit/conviction_trim/exit_existing sell — a tax
  outcome never overrides risk management.
