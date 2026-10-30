# Phase A — Screening & Thesis Only (Automated Daily Task)

_Part of [FriesTrader](https://github.com/YizhiSong/FriesTrader), Copyright (c) 2026 Yizhi Song, MIT License. moomoo adaptation._

Automated subset of this pipeline (see `README.md`), run every weekday
4:30pm Central as a cloud routine, against moomoo's hosted MCP server.

Performs **ONLY Steps 1–3**. **NEVER** Step 4 (re-verify), 5 (risk
enforcement), 6 (order execution), or 7 (`trade_log.jsonl`) — those
belong to Phase B. Order tools (`trading_order_place`,
`trading_order_replace`, `trading_order_cancel`, `trading_order_confirm`,
`sim_trade_input_order`, `sim_trade_modify_order`,
`sim_trade_cancel_order`, every `crypto_account_*` write) are to be
hard-blocked at the connector level; do not attempt them anyway.

**Symbol format.** moomoo tools take symbols as `US.AAPL`. Every log
line in this pipeline records the bare ticker (`"symbol": "AAPL"`);
prepend `US.` when calling a tool, strip it when logging.

**Which account to read.** Read `execution.mode` from `risk_rules.json`
for exactly one purpose here — picking the account whose positions
count as "held": `"live"` → `accounts.live_acc_id` via
`account_positions`; `"paper"` or `"dry_run"` → `accounts.paper_acc_id`
via `sim_trade_position_list` (`market` = `accounts.paper_market_id`).
Nothing else in Phase A depends on the mode.

## Step 1 — Build the watchlist

Pull symbols from the moomoo watchlist group named
`universe.watchlist_name` in `risk_rules.json` (read fresh each run —
don't assume prior values or hardcode the name). Call
`quote_user_security` with `group_name` = that name — ignore all other
groups. If the group isn't found, call `quote_user_security_group` once
to list the available names, log the mismatch in the end-of-run summary,
and continue with the scan + held positions only.

**Supplementary market screen** (additive, not a replacement): call
`quote_stock_screen` with `screen_queries` = `universe.
supplementary_screen_queries` **verbatim** (see
`universe.supplementary_screen_note`) — a whole-market screen that
surfaces genuinely notable movers from outside your watchlist, so
candidate selection isn't limited to names you've personally added. Drop
any screen result that's already on the watchlist (it's already a
watchlist candidate, not a second one) or already a held position
(always included regardless, per below). From what's left, take up to
`universe.supplementary_screen_max_candidates` — the screen's own
default ordering, no re-ranking needed — and mark each
`"source": "market_scan"` on its `screened` line (watchlist-sourced and
held candidates get `"source": "watchlist"`). This cap is **separate
from and additive to** `watchlist_max_candidates` below — screen results
never compete with watchlist candidates for the same slots. If the
screen call fails, log it and continue without it.

Dedupe the combined (watchlist + capped screen) list.

**Pull reference data for the deduped list, fresh every run:**
- `quote_market_snapshot` with the whole list as `code_list` (one
  batched call, ≤400 symbols) — take `last_price` as `current_price`
  for Steps 2–3, plus `high_price_52wk`/`low_price_52wk` (or however
  the response names the 52-week high/low), `market_cap`, `volume`, and
  the security `name`.
- `quote_history_kline` per candidate — `symbol` = `US.<ticker>`,
  `ktype` = 2 (day), `autype` = 1 (forward-adjusted), `end` = today,
  `start` = today minus ~300 calendar days, `num` = 370 — enough to
  cover a `trend_filter_lookback_trading_days`-bar moving average plus
  buffer for weekends/holidays. This same series is reused in Step 2
  for the 60-day price-move and volume-spike signals and in the trend
  filter below — no second K-line call needed for either.

**Then let the script decide every mechanical filter and signal — do
not hand-compute a moving average, volume average, ratio, or
percentage in prose.** For each candidate, pipe its K-line bars (a JSON
array of `{"date", "close", "volume"}`, oldest first) into:

`python3 scripts/screen_metrics.py --today <date> --current-price <snapshot last_price> --high-52wk <snapshot 52wk high> --low-52wk <snapshot 52wk low> --market-cap <snapshot market_cap> --min-avg-volume <universe.min_avg_daily_volume> --min-market-cap <universe.min_market_cap_usd> --max-market-cap <universe.max_market_cap_usd> [--penny-threshold <universe.penny_stock_price_threshold_usd>, only if penny_stock_filter_enabled] [--trend-lookback <universe.trend_filter_lookback_trading_days>, only if trend_filter_enabled] --price-move-60d-pct <signal_thresholds.price_move_60d_pct> --volume-spike-multiple <signal_thresholds.volume_spike_multiple> --pct-from-52wk-extreme <signal_thresholds.pct_from_52wk_extreme> [--is-held]`

and use its JSON output directly: `passed_filters`, `filter_reason`
(already in the exact log wording), `avg_volume`, `market_cap`,
`trend_filter`, `signal_check`, `signal_inputs`, `qualifies`,
`magnitude_score`, `pct_below_52wk_high`. The script already applies
the volume, market-cap band (ceiling as well as floor), penny-stock,
and trend-filter rules in `risk_rules.json`'s `universe` block, and
skips (rather than guesses) the trend filter when fewer than
`trend_filter_lookback_trading_days` bars exist (e.g. a recent IPO).
**If the script fails for a candidate**, log it `"passed_filters":
false, "reason": "screen_metrics.py failed to run — excluded this
cycle, verify manually"` rather than computing by hand.

**Leveraged/inverse ETF filter** (the one filter the script can't do,
since it's a text match): `universe.leveraged_etf_filter_enabled` /
`universe.inverse_etf_filter_enabled` are mechanical, each independently
toggleable. When true, exclude if the snapshot's security `name` — or,
if the name is ambiguous, the `quote_company_profile` /
`quote_stock_basicinfo` description — contains "leveraged" / "inverse"
respectively, or a leverage multiple like "2x"/"3x"/"-1x"/"Ultra"/
"Bull"/"Bear" (case-insensitive) — fund providers state this directly
(e.g. "ProShares UltraPro QQQ", "Direxion Daily ... Bull 3X Shares"),
no judgment about current risk needed. Log the reason as
`"leveraged/inverse ETF (name/description: \"<matched phrase>\") — excluded per universe.<leveraged_etf_filter_enabled|inverse_etf_filter_enabled>"`.

Cap the **watchlist-sourced, non-held** portion of what passed at
`universe.watchlist_max_candidates` — the screen's own separate cap
above already bounds its own contribution, so this cap only ever
applies to watchlist candidates.

**Always ensure every held position is in the final list** (from the
positions call named at the top of this file) — if one already made it
through on its own (e.g. it's also on the watchlist), leave it as-is,
don't add a duplicate. Run `screen_metrics.py` for it with
`--is-held`: it then always reports `passed_filters: true` with
`"reason": "currently held — always included"` (what the filters would
have said is kept in `filters_would_have_said` for the record), and
`qualifies: true` regardless of signal. `watchlist_max_candidates` is a
cap on **non-held** candidates only: exclude held positions from that
count entirely before checking whether the cap was exceeded, so a held
position can never occupy a slot or cause a non-held candidate to be
dropped. A held position must stay eligible for a fresh thesis
(including `exit_existing`) and never get silently dropped for being
illiquid, small-cap, below its moving average, or off the list.

(This just builds the candidate list — not a risk/stop-loss check; that's
Phase B's job. See Hard stop below.)

## Step 2 — Gather signals

`screen_metrics.py`'s `signal_check` (from Step 1's call — no second
pull needed) already decided, mechanically and against `risk_rules.json`'s
`signal_thresholds`, whether a candidate qualifies for a news search by
meeting **any one** of:

1. **60-day price move**: `abs(latest_close - close_60d_ago) / close_60d_ago >= signal_thresholds.price_move_60d_pct`.
   **"60 days" = 60 *calendar* days, not trading bars** — the script
   takes `close_60d_ago` from the first bar dated on/after today minus
   60 calendar days (not 60 bars back, which drifts to ~85–90 calendar
   days and overstates the move). If less than 60 days of history
   exists (e.g. recent IPO), it computes over the available window and
   flags `window_shorter_than_60d`.
2. **Volume spike**: `latest_volume / avg_volume_30d >= signal_thresholds.volume_spike_multiple`
   (the 30 bars before the latest one).
3. **Near a 52-week extreme**: `(high_52wk - current_price) / high_52wk <= signal_thresholds.pct_from_52wk_extreme`
   **or** `(current_price - low_52wk) / low_52wk <= signal_thresholds.pct_from_52wk_extreme`.

Never reuse `close_60d_ago`, `latest_close`, or any other K-line-derived
value from a prior run's `pending_proposals.jsonl` or `trade_log.jsonl`,
even if today's figure looks unchanged from yesterday's — every number
in `signal_check` must come from this run's own tool call and script
output. **Log the raw inputs behind every ratio, not just the ratio**
(the script's `signal_check` strings already carry them — copy them
verbatim) — otherwise it can't be sanity-checked without re-pulling
data.

If `signal_check` is `"none"`, no search/thesis this run — log as
`screened`-only. Qualifying candidates' searches stay within
`cadence.news_search_budget_per_cycle` (per run, not per stock; held
positions draw from their own separate budget above, not this one).

**If more candidates qualify than the budget allows**, prioritize by the
script's `magnitude_score` — how far each one exceeded the specific
threshold it tripped (its highest score if it qualified under more than
one criterion) — not conviction or `risk_flags` (those don't exist yet;
they're outputs of the search this budget gates, not inputs to it).
Process qualifying candidates in descending score order, spending the
budget as you go. Any candidate that would push spend past
`cadence.news_search_budget_per_cycle` is skipped this cycle — log
`"stage": "screened", "passed_filters": true, "reason": "news search budget exhausted this cycle (<N> of <cadence.news_search_budget_per_cycle> already spent on higher-magnitude signals) — no thesis this run"`.
It remains a normal candidate next cycle, re-screened fresh (no
carryover priority).

**News sources.** Use web search where the session has it, and
moomoo's `news_search` (`keyword` = ticker or company name,
`news_type` = `ALL`, `sort_type` = `TIME`) — the latter is especially
useful for company announcements (`news_type` = `NOTICE`). Either
counts as one search against the budget.

**Every qualifying candidate's search — new entry or held position —**
must explicitly check, in addition to whatever catalyst-specific query
satisfied Step 2:
1. Whether any active lawsuit/regulatory investigation naming the
   company has a scheduled ruling, hearing, trial date, or compliance
   deadline in the next ~90 days (the `active_litigation` risk_flags
   criterion below).
2. Whether the company has a confirmed accounting restatement,
   for-cause auditor dismissal/resignation, or an indictment/plea
   involving a current or former executive or employee tied to company
   operations, disclosed within the last 3 years (the
   `governance_history` risk_flags criterion below).
Don't rely on either surfacing incidentally from a catalyst-only search
— a stock can carry an open investigation or a past scandal
indefinitely without any day's catalyst search happening to mention it.

**Exception — held positions always get a fresh thesis**, signal or not.
Run one targeted news search per held position (separate budget from
`cadence.news_search_budget_per_cycle`, bounded by
`max_concurrent_positions`, same pattern as Phase B's Monday weekend-gap
searches) and produce a thesis every run — this is what makes
`exit_existing` reachable, since a slow deterioration with no sharp
signal would otherwise go unnoticed.

**Before researching a held position, pull its immediately prior
thesis** — the last `"stage": "thesis"` line logged for this symbol,
from the most recent prior commit to `pending_proposals.jsonl` that has
one (walk back through `pending_proposals.jsonl`'s commit history; don't
look back further than this holding period's start, i.e. stop at a
commit before the position was last closed to zero, same scoping used
elsewhere in this pipeline). Note any counter-evidence or `risk_flags`
it named — this feeds the `high`-conviction check in Step 3. This is
**not** carrying forward the conviction rating itself (Step 3 still
rates conviction fresh, from only this run's own research) — it's
making sure a real, still-open risk factor doesn't quietly disappear
from the analysis just because today's search didn't happen to
resurface it on its own.

## Step 3 — Synthesize thesis

For each flagged candidate, produce the thesis record from `README.md`
(symbol, date, thesis, conviction, invalidation, direction).
- **No price targets.**
- **No forecasting as fact** — "this suggests..." not "this will...".

**`conviction` follows a fixed rubric, not open judgment** — the same
underlying facts must produce the same rating regardless of which day
this runs. Evaluate fresh each run using only what this run's own
research found; never carry forward or average against a prior day's
conviction for the same symbol.

- **`high`** requires **all** of:
  - The catalyst is a specific, already-confirmed, company-disclosed
    event (an earnings result, a signed deal/contract, a completed
    regulatory approval, a disclosed structural risk) — not a rumor,
    analyst opinion, technical pattern, or sector/macro-wide move, and
    not still pending/anticipated (e.g. "ahead of earnings" caps at
    `medium` no matter how bullish/bearish the setup sounds).
  - **For a non-held candidate**, that event is recent — its confirming
    date (earnings date, signing, approval, disclosure) is within the
    last 15 trading days. Older facts can still support a `medium` (they
    haven't expired), but a weeks-old catalyst the market has already
    absorbed can't carry a fresh-money `high`. **Held positions are
    exempt** — a still-valid catalyst can sustain a `high` on an existing
    holding regardless of age.
  - The thesis explicitly names the strongest available counter-evidence
    (a plausible positive if bearish, a plausible negative if bullish)
    and gives a concrete reason it doesn't change the read — silence on
    the counter-case, or listing it without resolving it, doesn't
    qualify. ("...regardless of X" / "even though X" / "even with X" —
    not just piling on more confirming evidence.)
    - The counter-case must be **fundamental or structural** — a
      competitive threat, a demand/margin risk, an execution risk, a
      balance-sheet or liquidity concern, a regulatory/legal exposure.
      **Valuation and price action alone don't qualify** as the sole
      counter-case for `high`: "already up X%," "priced for perfection,"
      "rich multiple," "near its 52-week high," or a stale/split analyst
      rating are not the strongest available counter-evidence. If that is
      genuinely all that can be raised against the thesis, the real bear
      case hasn't been engaged — cap at `medium`.
    - "Analysts raised targets after the print" does not resolve a
      counter-case — sell-side targets following a stock upward aren't
      independent confirmation. Resolve it on the company's own disclosed
      fundamentals.
    - **For a held position, a counter-case already named in the
      immediately prior thesis (see Step 2's prior-thesis pull) that
      hasn't been explicitly resolved stays open.** Reaching `high`
      again requires a specific, dated new fact that resolves or
      materially changes it — the flagged sale program disclosed as
      complete, a suit dismissed or settled, guidance reaffirmed after
      the concern was raised — not a different day's more favorable
      re-framing of the same unchanged fact, and not simply omitting it
      from today's write-up. Cap at `medium` until it's genuinely
      addressed.
  - At least one cited source is **primary or wire** — a company filing
    or press release, a regulatory/court document, or a wire service
    (Reuters, AP, Bloomberg, Dow Jones). Aggregator or content-farm
    write-ups (StockStory, StockTitan, Webull, stockanalysis.com, Simply
    Wall St, law-firm blogs, and similar) can support the narrative but
    can't be the sole basis for a `high`. If no primary/wire source for
    the catalyst can be found, cap at `medium`.
  - `risk_flags` is empty (see below).
  - No unresolved binary catalyst (earnings date, court ruling,
    regulatory deadline) falls before this position's next likely
    review that could reverse the read.
- **`low`** applies if **any** of:
  - The thesis itself frames the evidence as mixed, offsetting, or
    unresolved (e.g. "mixed," "offset by," "still isn't fully
    confident," "unpredictable") rather than reaching a clear net read.
  - The move is explained as technical, mechanical, or sentiment-driven
    in a way that discounts its fundamental significance (e.g. "largely
    mechanical," "sentiment-driven rather than a disclosed fundamental
    deterioration").
  - Any `risk_flags` entry is present.
  - The catalyst is macro/sector-wide rather than company-specific
    (e.g. "broad rotation," "sector sentiment").
- **`medium`** is everything else: a real, credible, company-specific
  catalyst exists and doesn't hit a `low` disqualifier, but the catalyst
  is still pending, or multiple contributing factors are listed without
  one clearly resolved as dominant, or no counter-case is explicitly
  engaged and dismissed (a valuation- or price-action-only counter-case
  counts as none here).

**For a held position**, `direction` is `"long"` (still supports holding)
or `"exit_existing"` (no longer does) — never `"avoid"` (that's only for
not-yet-held candidates).

**Early-exit guard** (only if `early_exit_guard.enabled`): if this
holding period's opening buy (the buy that took it from zero) was within
the last `early_exit_guard.window_trading_days` trading days,
`exit_existing` additionally requires a `new_fact_date` field — the
disclosure date of a specific fact that drives the exit and was **not
available** to the thesis that opened the position (i.e. dated after
that opening buy's `proposal_date`). A re-weighting of facts the entry
thesis already had — the same earnings print or guidance, an analyst
rating change, price action, commentary — doesn't qualify. If no such
fact exists, keep `direction: "long"` and lower `conviction` as the
rubric dictates; the stop-loss and conviction-trim still protect the
position.

**Include `risk_flags`** for every `direction: "long"` candidate — an
array of zero or more tags from this fixed set, based only on what this
run's sourced research already found (no extra searches):
- `"active_litigation"` — an active lawsuit or regulatory investigation
  naming the company or an executive, with a specific scheduled ruling,
  hearing, trial date, or compliance deadline within the next ~90 days.
  An open-ended investigation or long-running dispute with no scheduled
  next step doesn't qualify on its own (most large companies have one
  of these at any given time) — mention it in the thesis narrative if
  relevant, but don't flag it.
- `"governance_history"` — a confirmed accounting restatement, for-cause
  auditor dismissal/resignation, or an indictment/plea involving a
  current or former executive or employee tied to company operations,
  disclosed within the last 3 years. Must be a completed, sourced event
  (company filing, regulatory action, or named-source reporting) — a
  short-seller report alone, a rumor, or an investigation with no
  confirmed finding yet doesn't qualify on its own (that's
  `active_litigation`'s territory if it has a scheduled next step, or
  just thesis-narrative color otherwise). The point is a track record of
  already-happened failures, not a prediction about an unresolved one.
- `"dilution_risk"` — a completed or pending equity/convertible raise,
  share offering, or ATM program disclosed in the last ~90 days.
- `"insolvency_or_liquidity_concern"` — bankruptcy rumor, going-concern
  language, or reliance on an external backer to remain solvent.
- `"leadership_turnover"` — a C-suite departure/replacement in the last
  ~90 days tied to operational or execution problems (not routine
  succession).
Empty array (`[]`) if none apply. Used by Phase B (Step 7) as the
primary within-tier tie-break, ahead of `pct_below_52wk_high`.

**Include `pct_below_52wk_high`** for every `direction: "long"` candidate:
the script's `pct_below_52wk_high` from Step 1 (i.e.
`(high_52wk - current_price) / high_52wk`, e.g. `0.15`). Used by
Phase B (Step 7) as the secondary within-tier tie-break, after
`risk_flags` — a disclosed "room in the setup" proxy, not a fair-value
calc. Omit for `avoid`/`exit_existing`.

**Include `current_price`** (Step 1's snapshot `last_price`) on every
`thesis` line — Phase B's buy gate compares this morning's ask against
it.

**Include a `sources` field** listing outlet name + URL for every search
result that informed this thesis (e.g.
`["Reuters: https://...", "Company Q2 press release: https://..."]`) —
this is what makes the reasoning step auditable later instead of just
trusted. Prefer primary sources (company filings/press releases, wire
services like Reuters/AP) and major outlets (Bloomberg, WSJ, CNBC, etc.)
over aggregator/content-farm sites when both turn up in the same search;
if only a lower-tier source is available, use it and cite it rather than
omitting the field. A `high`-conviction thesis additionally requires at
least one primary or wire source (see the `high` rubric): for a confirmed
earnings, deal, or approval catalyst the company's own release or an
8-K/press wire is almost always available — cite it directly rather than a
secondary write-up of it. A moomoo `news_search` item cites as
`"<source name> (via moomoo news): <url if present, else the news id>"`.

## Output

**Overwrite `pending_proposals.jsonl` at the start of this run** — it
should hold only today's candidates; history remains auditable via
`trade_log.jsonl`, which Phase B writes to when acting on a proposal.

Every line needs a real `"timestamp"` (`HH:mm:ss`, e.g. via
`TZ='America/Chicago' date +'%H:%M:%S'` — never guessed) alongside
`"date"`. Time-of-day only, no date prefix. For human readability only —
never used for idempotency or other logic.

Write:
- One `"stage": "screened"` line per candidate (`passed_filters`,
  `source` (`"watchlist"` or `"market_scan"`), `avg_volume`,
  `market_cap`, `reason` if rejected — shape matches
  `trade_log_template.jsonl`), plus `"signal_check"` copied from the
  script: either its array of strings, **each ratio paired with its raw
  inputs** so the arithmetic is checkable — raw numbers are this run's
  actual pulled values, never back-computed to fit a percentage:
  - `"price_move_60d: 0.2925 (close_60d_ago: 424.10 -> latest_close: 548.13)"`
  - `"volume_spike: 2.3x (latest_volume: 68000000 / avg_volume_30d: 29421634)"`
  - `"near_52wk_high: 0.0200 (current_price: 314.86 / high_52_weeks: 321.00)"`
  - or `"none"` if it didn't qualify for a thesis this run.
- One `"stage": "thesis"` line per flagged candidate (shape matches
  `trade_log_template.jsonl`), plus `current_price`, and
  `pct_below_52wk_high` for `long` candidates (Step 3).

Do not touch `trade_log.jsonl` — reserved for Steps 4–9 (Phase B), which
reads `pending_proposals.jsonl` separately.

**After all `screened`/`thesis` lines, append one `"stage": "summary"`
line per decision bucket** (to `pending_proposals.jsonl`) — a plain
symbol list per bucket for at-a-glance readability. Phase B only reads
`"stage": "thesis"` entries, so these are inert to it. Always all five
buckets, in order, even if empty:

```json
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "summary", "decision": "rejected", "symbols": ["AMC", "ADDYY"]}
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "summary", "decision": "no_signal", "symbols": ["TSLA", "NVDA", "..."]}
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "summary", "decision": "avoid", "symbols": ["SPCX", "LCID", "..."]}
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "summary", "decision": "long", "symbols": ["AAPL (medium)", "AMD (high)", "..."]}
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "summary", "decision": "exit_existing", "symbols": []}
```

`rejected` = failed universe filter. `no_signal` = passed filters, no
Step 2 signal/thesis. `avoid`/`long`/`exit_existing` = matches the
thesis's `direction`. Plain symbol lists throughout, except `long`
appends each symbol's own thesis `conviction` as `"<symbol>
(<conviction>)"` — the one bucket where it drives sizing; the others
carry a conviction too, it's just not decision-relevant there. No other
reason/detail fields — a quick-glance list, not a substitute for the
thesis lines.

## Hard stop

Do not call `trading_order_place`, `trading_order_replace`,
`trading_order_cancel`, `trading_order_confirm`, `sim_trade_input_order`,
`sim_trade_modify_order`, `sim_trade_cancel_order`, or any
`crypto_account_*` write tool, and do not use `execution.mode` for
anything other than choosing which account's positions to read.
`account_positions`/`sim_trade_position_list` are for Step 1's candidate
list only — no stop-loss/drawdown computation here; all risk enforcement
is Phase B's job.
