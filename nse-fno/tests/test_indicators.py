import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def series(seed, n=200, holes=True):
    rng = random.Random(seed)
    x, out = 100.0, []
    for i in range(n):
        x *= 1 + rng.gauss(0, 0.02)
        out.append(None if holes and i in (3, 77) else round(x, 2))
    return out


def test_bundled_indicators_match_the_dashboards_technical_py_exactly():
    """If technical.py is importable, the bundled copy must give identical numbers (otherwise standalone and hosted results differ)."""
    try:
        import technical as dash
    except ImportError:
        print("   (technical.py not on the path: equivalence check skipped)")
        return
    from fno import _technical as mine
    for seed in range(5):
        for holes in (False, True):
            v = series(seed, holes=holes)
            assert mine.sma(v, 20) == dash.sma(v, 20) and mine.sma(v, 50) == dash.sma(v, 50)
            assert mine.ema(v, 12) == dash.ema(v, 12)
            assert mine.rsi(v, 14) == dash.rsi(v, 14)
            assert mine.macd(v) == dash.macd(v)
            assert mine.latest_value(v) == dash.latest_value(v)


def run_clean(code, extra_path=None):
    """Run python with ONLY this package importable (no technical.py anywhere), as after unzipping on its own."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "FNO_TECHNICAL_DIR")}
    if extra_path:
        env["PYTHONPATH"] = extra_path
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)


STANDALONE = """
import sys
sys.modules['technical'] = None            # makes `import technical` raise ImportError, whatever is installed
import fno, fno.signals as sg
from fno import SyntheticProvider, StrategyConfig, suggest
from fno.demo_data import make_synthetic_bars
print('source:', sg.INDICATOR_SOURCE)
bars = make_synthetic_bars(300, seed=3); prov = SyntheticProvider('T', bars)
r = suggest('T', bars, prov, bars[-1]['date'], StrategyConfig(min_score=0))
print('regime:', r.regime.trend if r.regime else None)
"""


def test_runs_standalone_with_no_technical_py_anywhere():
    r = run_clean(STANDALONE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "source: bundled" in r.stdout and "regime: " in r.stdout and "regime: None" not in r.stdout


def test_an_unrelated_module_called_technical_does_not_break_it():
    d = tempfile.mkdtemp()
    (Path(d) / "technical.py").write_text("SOMETHING_ELSE = 1\n")           # e.g. an unrelated package of the same name
    r = run_clean("import fno.signals as sg; print('source:', sg.INDICATOR_SOURCE)", extra_path=d)
    assert r.returncode == 0 and "source: bundled" in r.stdout, r.stdout + r.stderr


def test_the_dashboards_technical_py_is_preferred_when_it_is_there():
    d = tempfile.mkdtemp()
    (Path(d) / "technical.py").write_text((ROOT / "fno" / "_technical.py").read_text())     # same five functions
    r = run_clean("import fno.signals as sg; print('source:', sg.INDICATOR_SOURCE)", extra_path=d)
    assert r.returncode == 0 and "source: dashboard technical.py" in r.stdout, r.stdout + r.stderr
