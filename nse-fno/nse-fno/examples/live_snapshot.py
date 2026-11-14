"""
Run the selector on a LIVE-style snapshot: today's real option chain (as an MCP get_option_chain tool or any API
returns it), the stock's daily bars, and (ideally) its ATM IV history.

This example fabricates the snapshot so it runs anywhere. In real use replace `records` with the tool's output and
`bars` with your adjusted daily bars (the dashboard's _fetch_history_adjusted gives them).

    python examples/live_snapshot.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fno import MarketConfig, StaticProvider, StrategyConfig, SyntheticProvider, chain_from_records, suggest
from fno.cli import print_result
from fno.demo_data import make_synthetic_bars

mkt = MarketConfig(lot_sizes={"DEMO": 500})
bars = make_synthetic_bars(400, seed=8)
asof = bars[-1]["date"]
fake_source = SyntheticProvider("DEMO", bars, mkt)
chains = {}
for expiry in fake_source.expiries("DEMO", asof, 3):                                  # a real snapshot has several expiries
    fake = fake_source.get_chain("DEMO", asof, expiry)
    # ---- stand-in for the MCP response: one record per strike with CE and PE side by side (NSE's style) ----
    records = [{"strikePrice": k, "CE": {"lastPrice": fake.get("CE", k).ltp, "bidprice": fake.get("CE", k).bid, "askPrice": fake.get("CE", k).ask,
                                        "impliedVolatility": round(fake.get("CE", k).iv * 100, 2), "openInterest": 5000},
                           "PE": {"lastPrice": fake.get("PE", k).ltp, "bidprice": fake.get("PE", k).bid, "askPrice": fake.get("PE", k).ask,
                                  "impliedVolatility": round(fake.get("PE", k).iv * 100, 2), "openInterest": 5000}}
               for k in fake.strikes() if fake.get("CE", k) and fake.get("PE", k)]
    chains[expiry] = chain_from_records("DEMO", asof, fake.spot, expiry, records, lot_size=500, mkt=mkt, futures_price=fake.futures_price)

iv_history = fake_source.atm_iv_history("DEMO", asof)                                 # replace with real ATM IV history
provider = StaticProvider("DEMO", bars, chains, iv_history=iv_history)
cfg = StrategyConfig(min_oi=1000, risk_budget=75_000)                                 # real OI is known here, so filter on it
print_result(suggest("DEMO", bars, provider, asof, cfg, mkt), why=True)
