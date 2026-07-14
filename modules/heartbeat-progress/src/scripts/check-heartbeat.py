#!/usr/bin/env python3
"""Pipeline heartbeat checker.

Usage:
  check-heartbeat.py <heartbeat-json-path> <stale-after-seconds>

Exits:
- 0 if heartbeat exists and is fresh enough
- 1 if missing/invalid/stale

Heartbeat JSON recommended fields:
- timestamp (ISO8601)
- progress counters (monotonic)

This script only checks staleness by timestamp.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_ts(s: str) -> datetime:
    # Accept Z or offset.
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check-heartbeat.py <heartbeat-json-path> <stale-after-seconds>", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    stale_after = int(sys.argv[2])

    if not path.exists():
        print("missing heartbeat file")
        return 1

    try:
        obj = json.loads(path.read_text())
    except Exception:
        print("invalid json")
        return 1

    ts = obj.get("timestamp") or obj.get("time")
    if not isinstance(ts, str) or not ts:
        print("missing timestamp")
        return 1

    try:
        dt = parse_ts(ts)
    except Exception:
        print("bad timestamp")
        return 1

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - dt).total_seconds()
    if age > stale_after:
        print(f"stale age_seconds={age:.0f}")
        return 1

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
