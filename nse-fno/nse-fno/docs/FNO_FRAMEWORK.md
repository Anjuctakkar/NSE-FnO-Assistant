# NSE F&O framework

Technical + Greeks-based trade suggestions for NSE stock futures and options, with a backtester and a forward
evaluator that grade the framework's own suggestions. Pure Python with no dependencies (the five indicator
functions it uses are bundled; beside the dashboard it uses the dashboard's `technical.py`).

```
daily bars ──┐
             ├─► signals.py ─► Regime (trend -4..+4, IV rank, realised vol, ATR, swing levels)
ATM IV hist ─┘                      │
                                    ▼
option chain ─► selector.py: rule matrix ─► strategies.py (strikes by delta) ─► filters ─► score ─► Suggestion
(any provider)                                                                          (legs, Greeks, POP, exit plan, reasons)
                                    │
              ┌─────────────────────┴──────────────────────┐
              ▼                                            ▼
      backtest.py (walk-forward, daily MTM,        evaluate.py (log suggestions, replay them later
      costs, Greek attribution, random baseline)   on new data, same simulator, taken vs not)
```

## Quick start

```bash
# unzip anywhere: no installation, and no other file needed
python -m fno bhav sync --data-dir data          # download NSE F&O bhavcopy (or: bhav import FILE ...)
python -m fno serve --data-dir data              # the web UI at http://127.0.0.1:8100: press "Find trades", no stock needed
python -m fno suggest --source bhavcopy --symbol TCS --data-dir data --risk-budget 50000
python -m fno backtest --source bhavcopy --symbol TCS --data-dir data      # add --random for the baseline
python -m fno demo                               # everything on synthetic data, no files or network needed
python tests/run_tests.py                        # 124 tests, offline, no pytest needed (pytest also works)
python tests/ui_smoke.py                         # drives the real UI in Chromium (needs: pip install playwright)
```

Without bhavcopy yet, choose **Synthetic demo** in the UI's Prices menu to see every screen on generated data.

## The UI

One page, built to answer a trader's questions in order. It opens on **Whole market**: press **Find trades** and no stock
is needed (choose **One stock** to look at a single name in detail).

**Whole market** shows, top to bottom:

1. **The market today**: stocks scanned, trend mix (bullish / range-bound / bearish), median IV rank, median put/call OI
   ratio, and how many stocks show long or short futures build-up.
2. **Ranked ideas**, one per stock, best first, with filter chips (Bullish / Bearish / Range-bound; Selling premium / Buying
   premium / Futures). Each idea leads with "Risk ₹X to make up to ₹Y · N% model chance of profit" and its open-interest
   evidence (build-up, put/call ratio, put and call walls, points added to the score), then folds the trade away.
3. **Open an idea** for the legs, a payoff chart (at expiry and today, with the OI walls marked), a plain-English reading of
   the Greeks, and when to get out. **Why this trade** holds the Greeks, the open-interest table, the score breakdown
   and the reasoning. From an idea you can add it to your log, open the stock on its own, or backtest it.
4. **What was skipped** and why (no traded options, probable split, too little history): skipped stocks are never scored.

The **Backtest** tab (one stock) compares the rules against a random-trend baseline and shows where the P&L came from
(delta, gamma, theta, vega, slippage) and whether the model's win chances were right. **My log** keeps the suggestions you
save (in this browser, not on a server), replays them on newer data and shows how they played out. Rarely changed
settings (minimum score, volume, open interest, futures) sit under **Advanced**. The "Synthetic" source carries a red
banner on every screen.

## How the market scan works (`fno/scanner.py`, `fno/summary.py`)

Evaluating ~200 stocks in full per click is too slow, so it is two passes:

- **A one-time index** of every stock's day (price, ATM IV, call and put OI, near-month futures price and OI, liquidity)
  is built from the stored bhavcopy. It takes seconds per hundred days, is built once, and new days are added as they
  are synced. A test proves its IV history matches the full-chain IV history to 1e-9.
- **Pass 1 (every stock, cheap):** trend score from the stock's own closes, IV rank, futures build-up, put/call tilt and
  liquidity combine into a shortlist score (40 trend, 25 open-interest backing, 20 how extreme IV rank is, 15 liquidity).
  Guards: a one-day move of 25% or more is treated as a probable split or bonus (bhavcopy prices are not adjusted) and
  only later days are used; stocks with no traded options or no ATM IV are skipped and counted.
- **Pass 2 (the shortlist, in full):** the same selector used for one stock, with open interest feeding the score. The
  best idea per stock is kept and stocks are ranked by score.

**Open interest in the score** (`fno/oi.py`), up to ±20 points, every one explained in the reasoning:
futures build-up (price against OI change: long build-up, short build-up, short covering, long unwinding), put/call OI
ratio, OI walls (the strike with most call OI above and put OI below the stock, within 15%, holding at least a tenth of
that side's OI) against the trade's strikes or breakeven, and max pain for range trades. These are conventions traders
use, not laws: OI shows positions, not who is right. **Nothing here proves OI adds edge**; the weights are judgement.
With no OI data (synthetic prices) the adjustment is zero and scores are unchanged.

**Limits.** The scan's technicals use the closes in the bhavcopy (no intraday high/low), so ATR and swing levels are
close-to-close. The scan as a whole is **not backtested** (a market-wide historical scan every day is a bigger job): a
single stock can be backtested from its idea, and the log replays real suggestions forward, which is the honest test.
The shortlist can miss a stock whose best trade only shows up in the full evaluation.

## What the selector does

**Regime.** Trend score from -4 to +4 (SMA20 vs SMA50, close vs SMA20, MACD vs signal, RSI zone), plus IV rank, IV
percentile, 20-day realised volatility, ATR and 10-day swing levels.

| | IV rank low (≤30) | IV middle | IV high (≥50) |
|---|---|---|---|
| bullish | bull call spread, long call* | bull call or bull put spread | bull put spread |
| bearish | bear put spread, long put* | bear put or bear call spread | bear call spread |
| neutral | no trade | no trade | iron condor |

\* long single options only on strong trends (|score| ≥ 3). Futures are added on strong trends at any IV and need
their own, lower score bar because their risk is undefined and the Greeks have nothing to confirm. Only
**defined-risk** option structures are built: no naked short options.

**How the Greeks are used** (they filter and score, they are not decoration):

- **Theta**: a long-premium trade is rejected if it burns more than 3% of the debit per day.
- **Vega**: credit structures are short vega and want a high IV rank; debit structures are long vega and want a low one.
- **Gamma against theta**: the position's break-even daily move is `sqrt(2·|theta|/|gamma|)`. A short-gamma trade is
  paid theta to absorb moves smaller than that; a long-gamma trade needs bigger ones. Greek fit compares it with the
  stock's recent daily move.
- **Delta**: strikes are chosen by delta (0.60 long leg, 0.25-0.30 short strikes, 0.08-0.10 wings), so the rule works
  for any price level and tracks current IV.

**Score (0-100):** signal fit 25, IV fit 25, variance premium 15, Greek fit 20, trade quality 15, minus a cost-drag
penalty (up to 15) and 5 for chasing a stretched move (RSI above 75 or below 25). Default minimum 55 (futures 50). Every suggestion carries its
`score_parts`, so a number is never unexplained.

**Hard filters** (a rejected candidate is listed with its reason; use `--why`): bid-ask spread, open interest, days to
expiry (short-gamma trades are not opened inside 10 days), credit as a share of width, probability of profit,
reward:risk, theta burn, and a rupee risk budget (`risk_budget` sets the number of lots from the worst-case loss, and
rejects the trade if one lot is too big).

**Exit plan** on every suggestion: profit target, stop, time exit (5 days before expiry, which also avoids physical
settlement of stock options), and a technical invalidation level. Credit structures close at 50% of max profit or when
the loss reaches 1.5x the credit; debit spreads at 60% of max profit or -50% of the debit; single long options at +80%
or -50% of the premium.

**Probability of profit** is model-implied (risk-neutral, from ATM IV), not a forecast. A fairly priced structure has
about zero expected value before costs, so any edge has to come from the signal or a volatility view, and costs mean
the default is a small loss. Calibration against outcomes is reported by the backtest.

## Backtesting

Walk-forward, one position per symbol. On each flat day the selector sees only data up to that close (a test rewrites
all later prices and checks that earlier trades do not change). Positions are marked daily; exits are checked on daily
closes in the order invalidation, stop, target, time, expiry. Costs include slippage (a fraction of the half spread),
STT, exchange, SEBI, stamp, GST and brokerage on every order.

Each trade carries a **Greek P&L attribution** (delta, gamma, theta, vega, entry and exit slippage, unexplained). A
test confirms the Greek terms predict a one-day repricing, so the split is meaningful and not just a reconciliation.

**Random baseline** (`--random`): same structures, strike rules, exits and costs, but the trend is drawn at random.
If the signal does not beat it on the same data, the technical view adds nothing.

**Forward evaluation** is the genuine out-of-sample test: `log_suggestions()` records each suggestion with its fills
and exit plan; later `evaluate_log()` replays them on newer data with the same simulator, separately for all
suggestions and those you marked taken (`set_taken()`).

## Read this before trusting any number

1. **Synthetic mode prices options from realised volatility.** Its "IV rank" is a realised-vol rank, and real IV has a
   variance premium, event premia and skew that it lacks. `iv_scale` (default 1.0) decides how much edge option
   sellers are handed. Measured on the bundled generator, 5 seeds, credit structures only:

   | iv_scale | trades | win rate | expectancy | net |
   |---|---|---|---|---|
   | 0.9 | 14 | 57% | -0.31 R | -Rs 88k |
   | 1.0 | 35 | 66% | -0.14 R | -Rs 107k |
   | 1.1 | 50 | 74% | -0.02 R | -Rs 28k |
   | 1.25 | 50 | 80% | +0.09 R | +Rs 129k |

   The sign of the result flips with an assumption the data does not support. Always run the sweep.
2. **The demo's "signal beats random" is built in.** The generator has persistent trend regimes, so a trend signal is
   expected to look good. It proves the machinery works, not that there is an edge.
3. **Few trades prove little.** Under 30 closed trades is flagged. Picking stocks that look good today flatters any backtest.
4. **Daily closes only.** Intraday stop touches, gaps through a stop, and earnings or other events are not modelled
   (a stop can lose more than 1 R). No event calendar is built in.
5. **Cash settlement in the simulator.** Stock options settle physically in reality; the plan exits early to avoid it.
6. **Not verified against real NSE data.** All 124 tests run on synthetic data, hand calculations and a local server that
   imitates NSE. See "Real data" above for exactly what that does and does not cover.

## Real data: the NSE F&O bhavcopy connector (`fno/bhavcopy.py`)

```bash
python -m fno bhav sync   [--from 2026-09-01] [--to 2026-10-23] --data-dir data   # missing days only
python -m fno bhav import fo_bhavcopy.zip other.csv --data-dir data                # files saved from a browser
python -m fno bhav status --data-dir data
python -m fno bhav schema fo_bhavcopy.zip           # how a real file maps onto the expected columns: run this on a new download
python -m fno probe https://host/path/mcp           # which tools does an MCP server offer?
```

Each day is converted on ingest to one compact canonical schema (date, symbol, inst, expiry, strike, kind, price, oi,
volume, under, lot), gzipped, one file per day (about 10 KB for a test day, against tens of MB raw). Everything else
reads only that schema, so if NSE changes a layout again only the ingest adapter changes, and a file that cannot be
read fails loudly with its header instead of producing silent garbage. The download sends browser-style headers after a
cookie warm-up, skips weekends, reports holidays apart from blocks, stops after the first refusal instead of hammering
NSE, and `import` is the supported fallback when NSE blocks scripts.

**What is and is not verified.** Tested against a local server that behaves like NSE (cookie and User-Agent required,
404 on holidays, refusal mode), on both the UDiFF layout and the older layout. **Not verified:** this sandbox has no
internet, so nothing has touched nseindia.com. The F&O download URL follows the pattern NSE uses for the cash market
(which I confirmed) and is widely used, but I did not see it work. The UDiFF column names (`TckrSymb`, `FinInstrmTp`,
`StrkPric`, `OptnTp`, `SttlmPric`, `OpnIntrst`, `TtlTradgVol`, `NewBrdLotQty`, ...) were written from memory. NSE is also
changing how bhavcopy is distributed this year (the members' Extranet files are being discontinued in December 2026;
the public website reports are not described as changing, but check). Run `bhav schema` on a real file first, and
adjust `FIELD_ALIASES` or the URL templates (`BhavcopyStore(url_templates=...)`) if they differ.

**What bhavcopy cannot give you:** no bid/ask (slippage uses an assumed spread), no implied volatility (solved from the
settlement price), and untraded contracts carry a theoretical settlement price. Use the minimum-volume and
open-interest filters. IV rank needs about a year of stored days to mean anything: the UI warns when there are fewer
than 100. Cash-market bars for the technicals come from the dashboard (when hosted there), a `SYMBOL.csv` in
`data/bars/`, or, failing both, the underlying close in the bhavcopy (no intraday range, so ATR and swing levels are
close-to-close, and the response says so).

**Live suggestions** use `StaticProvider` with today's chains from any option-chain source converted by
`chain_from_records` (`examples/live_snapshot.py`). I could not confirm that NSE's MCP service has an F&O endpoint;
`fno probe` is how you check a candidate URL.

## Troubleshooting

| What you see | Why, and what to do |
|---|---|
| "One-time index needed" | Press the button. It summarises each stored day once so the scan can read the whole market quickly; later syncs add days automatically. |
| "Not enough price history" (or the CLI's DATA PROBLEM) | Fewer than 60 trading days of prices. The trend rules need 60 (the 50-day average alone needs 50) and IV rank wants about a year. Press **Sync from NSE** with *1 year* selected, or run `python -m fno bhav sync`. If you did sync, check the sidebar for how many days are actually stored. |
| Sync says "NSE had no file for any of the N weekdays" | Holidays explain a few days a year, not a whole window. The download address may be wrong or changed (it is unverified), or NSE does not publish files that far back. The sync stops instead of repeating the failure. Save a file from nseindia.com and use **Import file**; run `python -m fno bhav schema FILE` to check its columns. |
| Sync stops with "NSE refused the download" | NSE blocks scripts at times. Import files saved from a browser instead. |
| "No trade" with a reason | A genuine answer: the trend, IV level, Greeks and costs did not line up. Read the reason and the rejected-candidates list. |
| IV rank looks odd or is missing | Needs about a year of stored days. Under 60 days the rules cannot run; 60 to 200 is usable but thin. |

## Settings to verify (`config.py`)

STT is from the Finance Act 2026 (effective 1 April 2026): 0.05% on futures sells, 0.15% on option premium sells,
0.15% on intrinsic value when a long ITM option is exercised. Everything else is a default to check against NSE
circulars and your broker's contract note: lot sizes (the fallback sizing is not NSE's), monthly expiry weekday
(Tuesday), exchange charges, stamp duty, risk-free rate, holidays.

## Fitting it into the dashboard project

Hosting the UI inside the dashboard gives it your Google sign-in, per-user limits and HTTPS. The standalone server has
none of those, so it refuses to listen beyond `127.0.0.1`. The wiring is documented at the top of `fno/api.py`:

1. Copy `fno/static/fno.js` to the dashboard's `static/` folder and add `<script src="/fno.js" defer></script>` to
   `index.html`. It adds an "F&O" tab by itself and works under the dashboard's content-security policy (no external
   scripts or styles).
2. In `server.py`, create `fno.api.Backend(fno.service.Context(DATA_DIR / "fno"), fetch_bars=..., heavy=heavy_slot)` and
   `app.include_router(fno.api.build_router(backend, auth.current_user, auth.require_admin))`. `fetch_bars` is the
   dashboard's own split/bonus-adjusted history, so the technicals use real OHLC.
3. Sync and import change shared data, so they are admin-only. Everything else needs a signed-in user. Each user's
   suggestion log stays in their own browser, like the dashboard's Evaluation list.

The logic is tested without FastAPI (`Backend` is plain asyncio). **The router itself, 30 lines of glue, is not tested
here because FastAPI is not installed in this sandbox**: run one request through it before relying on it.

To expose the engine to the chat assistant as well, add `fno_suggest` and `fno_backtest` handlers to `agent_tools.py`
returning `service.run_suggest(...)` / `run_backtest_req(...)`, and extend `SYSTEM_PROMPT`: it currently says "long
setups only" and "stay on topic: NSE stocks", so it would otherwise refuse or mis-describe derivatives.

**Compliance:** `DEPLOY.md` already flags that showing entry, stop and target levels may need SEBI research-analyst or
adviser registration. Specific F&O trades are a stronger case of the same issue, and SEBI's own studies of individual
F&O traders found most lose money. Get qualified advice before showing these to anyone outside a small trusted group,
and keep every output labelled as the output of a rule set, not a recommendation. Margin (SPAN plus exposure) is not
calculated: the UI says so and points to the broker's calculator.

## Layout

```
fno/config.py      MarketConfig, CostModel, StrategyConfig       fno/signals.py     regime and IV rank
fno/greeks.py      Black-Scholes, Greeks, implied vol            fno/strategies.py  structure builders
fno/models.py      Quote, Chain, Leg, Structure, Suggestion      fno/selector.py    rules, filters, score, exit plan
fno/costs.py       charges and fills                             fno/backtest.py    simulator, metrics, baseline
fno/expiry.py      expiry calendar                               fno/evaluate.py    log and forward evaluation
fno/data.py        Synthetic / Bhavcopy / Static providers       fno/bhavcopy.py    NSE connector, canonical store
fno/oi.py          open-interest analysis and score adjustment   fno/summary.py     per-stock daily index for the scan
fno/scanner.py     the two-pass market scan
fno/service.py     JSON-in/JSON-out functions for any front end  fno/mcp_probe.py   list an MCP server's tools
fno/server.py      standalone local web server                   fno/api.py         dashboard backend + router
fno/static/        index.html, fno.js (the UI)                   fno/cli.py         command line
tests/             124 tests + run_tests.py + ui_smoke.py         examples/          live_snapshot.py
```
