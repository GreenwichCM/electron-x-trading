"""Is production running and recording? On the always-on machine: `uv run python deploy/status.py`.

Reads only: systemd, production's Redis database, the api and the service logs.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

from redis import Redis
from trading_core.hours import ExchangeHours

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("marketdata", "dropcopy", "riskmonitor", "recorder", "api")


def env_value(path: str, key: str, default: str) -> str:
    try:
        for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("'\"") or default
    except OSError:
        pass
    return default


def run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
    except OSError, subprocess.TimeoutExpired:
        return ""


def main() -> None:
    print("== services")
    for s in SERVICES:
        state = run("systemctl", "is-active", f"trading-{s}") or "not installed"
        since = run("systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", f"trading-{s}")
        print(f"  {s:<12} {state:<10} {('since ' + since) if since and state == 'active' else ''}")

    url = env_value("services/prod.env", "REDIS_URL", "redis://127.0.0.1:6379/2")
    print(f"== production data ({re.sub(r'//[^@]*@', '//', url)})")
    try:
        r = Redis.from_url(url, socket_timeout=3, decode_responses=True)
        dc = json.loads(r.get("dc:positions") or "{}")
        books = sum(1 for _ in r.scan_iter("md:last:*", count=1000))
        last = r.xrevrange("md:trades", "+", "-", count=1)
        last_time = json.loads(last[0][1]["data"]).get("time") if last else "none yet"
        print(f"  marked as:   {r.get('trading:environment') or 'nobody'}'s")
        print(f"  drop copy:   {'connected' if dc.get('connected') else 'NOT CONNECTED'}, as of {dc.get('time', '?')}")
        print(f"  order books: {books}")
        print(f"  trades kept: {r.xlen('md:trades')}, the last at {last_time}")
    except Exception as exc:  # noqa: BLE001 - a status page reports, it does not fail
        print(f"  Redis is not answering ({exc}): docker compose -f deploy/compose.prod.yaml up -d")

    port = env_value("services/api/.env", "API_PORT", "8000")
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/environment", timeout=3) as res:
            print(f"== api: {res.read().decode()} on http://localhost:{port}")
    except OSError as exc:
        print(f"== api: NOT ANSWERING on port {port} ({exc})")

    status = ExchangeHours().status(datetime.now(UTC))
    change = status.next_change.astimezone(UTC).strftime("%a %d %b %H:%M UTC")
    print(
        f"== ElectronX: {'open' if status.open else 'closed (' + status.reason + ')'}, "
        f"{'closes' if status.open else 'opens'} {change}"
    )

    recorded = [
        line
        for line in run("journalctl", "-u", "trading-recorder", "-n", "300", "--no-pager", "-q").splitlines()
        if "recorded" in line
    ]
    print(f"== recorder: {recorded[-1].split(': ', 1)[-1] if recorded else 'no progress line in its log yet'}")
    print("== warnings and errors in the last hour")
    print(
        run("journalctl", "-u", "trading-*", "--since", "1 hour ago", "-p", "warning", "--no-pager", "-q") or "  none"
    )


if __name__ == "__main__":
    main()
