"""Bump the app version and stamp today's date (IST) in fno/version.py:

    python tools/bump_version.py patch      # 0.3.1 -> 0.3.2   (a fix or a small change)
    python tools/bump_version.py minor      # 0.3.1 -> 0.4.0   (a new feature)
    python tools/bump_version.py major      # 0.3.1 -> 1.0.0
    python tools/bump_version.py date       # keep the number, restamp the date
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "fno" / "version.py"


def today_ist() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date().isoformat()


def bump(path: Path, part: str, today: str | None = None) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
    if not m:
        raise SystemExit("APP_VERSION = \"x.y.z\" not found in " + str(path))
    major, minor, patch = (int(x) for x in m.groups())
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    elif part != "date":
        raise SystemExit("part must be major, minor, patch or date")
    version, day = f"{major}.{minor}.{patch}", today or today_ist()
    text = re.sub(r'APP_VERSION\s*=\s*"[^"]*"', f'APP_VERSION = "{version}"', text)
    if re.search(r'APP_VERSION_DATE\s*=', text):
        text = re.sub(r'APP_VERSION_DATE\s*=\s*"[^"]*"', f'APP_VERSION_DATE = "{day}"', text)
    else:
        text = text.rstrip("\n") + f'\nAPP_VERSION_DATE = "{day}"\n'
    path.write_text(text, encoding="utf-8")
    return version, day


if __name__ == "__main__":
    v, d = bump(DEFAULT, sys.argv[1] if len(sys.argv) > 1 else "patch")
    print(f"version {v} dated {d}")
