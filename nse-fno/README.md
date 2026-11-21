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

The app version is shown beside the navigation tabs and is maintained in `fno/version.py`.

Use the floating chat button to ask about the Breeze connection, the currently displayed quote, market values, or the latest strategy ideas. **App data only** mode answers deterministically from values already loaded in the page and makes no model request. **AI model** mode is optional and supports OpenAI, Anthropic, and Google Gemini. It sends the recent conversation and a summary of displayed F&O data through this server to the selected provider. The API key is sent transiently to this server and forwarded to the provider, but is not logged or stored; its field clears when you switch mode or provider. Use model mode only if you are comfortable sharing the prompt and displayed context with that provider.

In **One stock**, choose **Refresh live quote** for an on-demand cash quote. For strategies based on current data, choose **ICICI Breeze live (5-minute)**: the app fetches the current cash quote and option chain, recent five-minute cash candles, and historical five-minute ATM option candles to derive IV history. Technical periods are candle counts, not days; IV rank is limited to the option history Breeze returns and is not a one-year daily rank. If Breeze does not return enough required data, the app reports the problem rather than falling back to bhavcopy. Live mode is single-stock only; whole-market scans, backtests, and futures are disabled. The lot size is marked as estimated unless Breeze provides it or it is configured in `MarketConfig`. **Sync latest** checks for new bhavcopy files; **Sync history** backfills the selected range for bhavcopy mode.

Read `docs/FNO_FRAMEWORK.md` first, especially "Read this before trusting any number": results on synthetic prices
show how the rules behave, not what the market would have paid, and the sign of credit-spread results flips with one
volatility assumption. Information only; not investment advice.
