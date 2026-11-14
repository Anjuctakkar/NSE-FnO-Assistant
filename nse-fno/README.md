# nse-fno: F&O framework for NSE stocks

Suggests futures and options trades from technicals and option Greeks, backtests the rules, and grades its own
suggestions. Pure Python, no dependencies (the five indicator functions it needs are bundled).

**Install:** unzip anywhere and run it. Nothing to install. Placed next to the dashboard's `technical.py` it uses that
file's indicators (so both always agree); on its own it uses a bundled copy that a test proves is identical.

```bash
python -m fno serve                          # the web UI: press "Find trades" (no stock needed). "Synthetic demo" works without data
python -m fno bhav sync --data-dir data      # real NSE F&O bhavcopy, then: python -m fno serve --data-dir data
python -m fno demo                           # end to end in the terminal on synthetic data
python tests/run_tests.py                    # 124 tests, offline, no pytest needed
```

Read `docs/FNO_FRAMEWORK.md` first, especially "Read this before trusting any number": results on synthetic prices
show how the rules behave, not what the market would have paid, and the sign of credit-spread results flips with one
volatility assumption. Information only; not investment advice.
