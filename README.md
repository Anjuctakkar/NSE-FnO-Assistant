# nse-fno: F&O framework for NSE stocks

Suggests futures and options trades from technicals and option Greeks, backtests the rules, and grades its own
suggestions. Pure Python, no dependencies (the five indicator functions it needs are bundled).

**Install:** unzip anywhere and run it. Nothing to install. Placed next to the dashboard's `technical.py` it uses that
file's indicators (so both always agree); on its own it uses a bundled copy that a test proves is identical.

```bash
python -m fno serve                          # the web UI: press "Find trades" (no stock needed). "Synthetic demo" works without data
python -m fno bhav sync --data-dir data      # real NSE F&O bhavcopy, then: python -m fno serve --data-dir data
python -m fno demo                           # end to end in the terminal on synthetic data
python tests/run_tests.py                    # offline test suite, no pytest needed
```

The NSE bhavcopy sync downloads published F&O settlement files (end-of-day, not live). Open **Settings** to enter your ICICI Breeze API key, API secret, and session token, then choose **Test connection** to verify authentication. Install the optional SDK with `python -m pip install -r requirements-live.txt`. Credentials entered in Settings are sent only to this F&O server for Breeze requests; this UI does not write them to disk or browser storage. Alternatively, configure `BREEZE_API_KEY`, `BREEZE_API_SECRET`, and `BREEZE_SESSION_TOKEN` in the server environment and leave the fields blank. Keep all credentials private.

The app version and its release date are shown beside the navigation tabs (for example "App version 0.3.1 · 26 Nov 2026") and are kept in `fno/version.py`. Change both on every change: `python tools/bump_version.py patch` (or `minor` / `major`) raises the number and stamps today's date in IST.

## Deploy to Render

This repository includes a Render Blueprint at [`render.yaml`](../render.yaml). In Render, choose **New > Blueprint** and
select this repository. Render installs `nse-fno/requirements-live.txt`, starts `nse-fno/render_server.py`, and uses
`/health` for health checks.

The deployed service runs synthetic/demo mode out of the box. For live bhavcopy or Breeze features, add the optional
environment variables in the Render service settings: `BREEZE_API_KEY`, `BREEZE_API_SECRET`, and
`BREEZE_SESSION_TOKEN`. Render provides `PORT` automatically. Data in `nse-fno/data` is ephemeral unless you attach a
Render persistent disk and set `FNO_DATA_DIR` to its mount path.

Use the floating chat button to ask about the Breeze connection, the currently displayed quote, market values, or the latest strategy ideas. **App data only** mode answers deterministically from values already loaded in the page and makes no model request. **AI model** mode is optional and supports OpenAI, Anthropic, and Google Gemini. It sends the recent conversation and a summary of displayed F&O data through this server to the selected provider. The API key is sent transiently to this server and forwarded to the provider, but is not logged or stored; its field clears when you switch mode or provider. Use model mode only if you are comfortable sharing the prompt and displayed context with that provider.

In **One stock**, choose **Refresh live quote** for an on-demand cash quote. For strategies based on current data, choose **ICICI Breeze live (5-minute)**: the app fetches the current cash quote and option chain, recent five-minute cash candles, and historical five-minute ATM option candles to derive IV history. Technical periods are candle counts, not days; IV rank is limited to the option history Breeze returns and is not a one-year daily rank. If Breeze does not return enough required data, the app reports the problem rather than falling back to bhavcopy. Live mode is single-stock only; whole-market scans, backtests, and futures are disabled. The lot size is marked as estimated unless Breeze provides it or it is configured in `MarketConfig`. **Sync latest** checks for new bhavcopy files; **Sync history** backfills the selected range for bhavcopy mode.

## Win probability, calibration and the whole-market live scan

**Win probability.** Each idea now carries a `win` block. The old number (`pop`) is the risk-neutral chance of profit if held to
expiry; it is still shown, relabelled. The win probability answers a different question, whether *this trade as managed* ends
with a net profit, and is built in layers (`fno/probability.py`): skew-aware probability from the IV smile, the same after costs,
a real-world version (own drift, volatility blended from IV and realised), then a Monte-Carlo of the trade with fat-tailed
shocks, mean-reverting stochastic IV, the real exit plan, slippage and charges. The result is shown with a 90% range and a
confidence grade (low / medium / high) that depends on how many past trades the model has been checked against.

```bash
python -m fno calibrate --source bhavcopy --data-dir data --symbols TCS,INFY,RELIANCE,HDFCBANK   # backtest, then fit the record
```

`calibrate` backtests the raw model on your stored history and keeps (predicted probability, outcome) pairs in
`data/calibration-<source>.json`. Later suggestions shrink their probability toward what those past predictions actually
realised and widen or narrow the range accordingly. It also prints an out-of-sample check (calibrated vs raw Brier score), so you
can see whether the calibration helps on your data. Synthetic outcomes are kept in a separate file and never tune real-data
probabilities. On a Render free instance `data/` is ephemeral: attach a disk and set `FNO_DATA_DIR`.

**Hybrid data: bhavcopy EOD base, Breeze live overlay.** Choose *Hybrid: live Breeze + bhavcopy EOD* (source `breeze-live`). The
stored bhavcopy supplies what only history can: the universe, daily price history, a **one-year IV history** (so IV rank is a real
one-year rank) and last session's futures/open-interest build-up. Breeze supplies what only the market can: the current price (today's
candle, with its high and low), daily candles, and the live option chain with bid/ask; the ATM IV that gets ranked is the live one. The
single-stock view and the whole-market scan use the same pipeline (`live_scan.hybrid_inputs`), so both see the same current picture.
With no stored bhavcopy for a stock the single-stock view falls back to live-only 5-minute data and says so.

**Whole-market scan, live in two stages.** *Stage A*: every eligible stock is priced live (1 Breeze call each, about 2.5 minutes) and
re-scored on today's move, so a stock that gapped or broke out is ranked on today's price rather than last night's close. *Stage B*: the
best N of that live ranking (default 30, up to 120) are evaluated in full on live option chains (about 3 more calls each) by the same
selector as everywhere else. Each idea shows today's move and how its rank changed. It runs as a background job
(`/api/fno/live-scan/start`, `/status`, `/cancel`), one at a time.

**Refresh.** *Refresh live data* re-pulls the best 8 ideas on screen (about 4 calls each) and merges them into the list. *Auto-refresh*
does this every 5 minutes for a single stock and every 10 for the market ideas, only while the NSE session is open (weekdays 09:15 to
15:30 IST; exchange holidays are not known), only while the tab is visible, and it stops when the day's Breeze allowance runs low.

**Breeze limits.** Every Breeze call passes a rate limiter (default 75 a minute and 5,000 a day; override with `BREEZE_CALLS_PER_MIN` /
`BREEZE_CALLS_PER_DAY`). The day's count survives restarts and a reserve is kept for single-stock use. Stage A is skipped, with the reason
shown, when the budget cannot cover it. A scan that runs out of budget stops and lists what it did not reach. Needs stored bhavcopy for
the universe and IV history (or name the stocks with `symbols`); no futures; open-interest build-up is last session's.

**Strategy changes.** The trend score now has company: ADX (trend strength), a momentum z-score and range position. A condor is
refused in a strong trend, selling premium is refused when IV is below realised volatility, buying premium is refused when IV is rich
and IV rank is not low, and a trade is refused when its modelled expected value after costs is clearly negative. The invalidation
level is never closer than 2 ATR to the entry price. Option fills now snap to the 0.05 tick against you and assume at least a
two-tick spread when no bid/ask is known. Backtests report a 90% interval on win rate and expectancy and compare against several
random-trend seeds instead of one.

Read `docs/FNO_FRAMEWORK.md` first, especially "Read this before trusting any number": results on synthetic prices
show how the rules behave, not what the market would have paid, and the sign of credit-spread results flips with one
volatility assumption. Information only; not investment advice.
