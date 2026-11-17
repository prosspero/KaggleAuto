#!/usr/bin/env python3
"""
KaggleAuto — Terminal Monitoring Dashboard

Reads state.json and log files to show a live overview:
  • Active account and time remaining
  • Quota usage per account
  • Last health check result
  • Recent log entries

Auto-refreshes every 30 seconds. Press Ctrl+C to exit.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
LOGS_DIR = ROOT / "logs"

REFRESH_INTERVAL = 30  # seconds


# ── Helpers ──────────────────────────────────────────────────────────────
def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def load_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def human_duration(seconds: float) -> str:
    if seconds < 0:
        return "overdue"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def get_latest_log(max_lines: int = 15) -> list[str]:
    """Return the tail of the most recently modified log file."""
    if not LOGS_DIR.is_dir():
        return ["  (no logs/ directory found)"]
    log_files = sorted(LOGS_DIR.glob("*.log*"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    if not log_files:
        return ["  (no log files found)"]
    try:
        with open(log_files[0], "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-max_lines:]
        return [line.rstrip() for line in tail]
    except Exception as exc:
        return [f"  (error reading log: {exc})"]


# ── Dashboard sections ───────────────────────────────────────────────────
def render_header() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("╔══════════════════════════════════════════════════════════╗")
    print("║           KaggleAuto  —  Monitoring Dashboard           ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Refreshed: {now}    (every {REFRESH_INTERVAL}s, Ctrl+C to quit)")


def render_active_session(state: dict | None, config: dict | None = None) -> None:
    print("\n── Active Session ─────────────────────────────────────────")
    if not state:
        print("  state.json not found — orchestrator may not be running.")
        return

    accounts = config.get("accounts", []) if config else []
    idx = state.get("current_account_index", 0)
    active = accounts[idx]["username"] if idx < len(accounts) else f"index {idx}"
    start_iso = state.get("current_session_start")
    duration_h = 11.5
    if config:
        duration_h = config.get("timing", {}).get("session_duration_hours", 11.5)

    # Determine status from health check
    last_ok = state.get("last_health_ok", None)
    if last_ok is True:
        status = "healthy"
    elif last_ok is False:
        status = f"unhealthy (failures: {state.get('consecutive_failures', 0)})"
    else:
        status = "unknown"

    print(f"  Account : {active} (#{idx + 1} of {len(accounts)})")
    print(f"  Health  : {status}")

    if start_iso:
        try:
            start_dt = datetime.fromisoformat(start_iso)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
            remaining = duration_h * 3600 - elapsed
            print(f"  Started : {start_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print(f"  Elapsed : {human_duration(elapsed)}")
            print(f"  Remaining: {human_duration(remaining)}")
        except (ValueError, TypeError):
            print(f"  Started : {start_iso} (could not parse)")
    else:
        print("  Started : —")


def render_quota(state: dict | None, config: dict | None) -> None:
    print("\n── Quota Usage (this week) ─────────────────────────────────")
    weekly_limit = 30.0
    if config:
        weekly_limit = config.get("quota", {}).get("weekly_limit_hours", 30.0)

    quota_raw = state.get("quota", {}) if state else {}
    # The orchestrator stores quota as {"username": {"used_hours": X, "week_start": ...}}
    quota_data = {k: v.get("used_hours", 0.0) if isinstance(v, dict) else v
                  for k, v in quota_raw.items()}
    accounts = config.get("accounts", []) if config else []
    usernames = [a.get("username", f"account_{i}") for i, a in enumerate(accounts)]

    if not usernames and not quota_data:
        print("  (no account data available)")
        return

    # Merge: show all known usernames
    all_users = list(dict.fromkeys(usernames + list(quota_data.keys())))
    if not all_users:
        print("  (no account data available)")
        return

    # Header
    print(f"  {'Account':<24} {'Used':>8}  {'Limit':>8}  {'Bar'}")
    print(f"  {'─' * 24} {'─' * 8}  {'─' * 8}  {'─' * 20}")
    for user in all_users:
        used = quota_data.get(user, 0.0)
        pct = min(used / weekly_limit, 1.0) if weekly_limit > 0 else 0
        bar_len = int(pct * 16)
        bar = "█" * bar_len + "░" * (16 - bar_len)
        flag = " ⚠" if pct >= 0.9 else ""
        print(f"  {user:<24} {used:>7.1f}h  {weekly_limit:>7.1f}h  {bar}{flag}")


def render_health(state: dict | None) -> None:
    print("\n── Health Check ────────────────────────────────────────────")
    if not state:
        print("  (no state data)")
        return

    last_check = state.get("last_health_check")
    if not last_check:
        print("  No health check recorded yet.")
        return

    last_ok = state.get("last_health_ok", False)
    failures = state.get("consecutive_failures", 0)

    icon = "✅" if last_ok else "❌"
    print(f"  Status  : {icon} {'ok' if last_ok else 'failing'}")
    print(f"  Time    : {last_check}")
    print(f"  Consecutive failures: {failures}")


def render_logs() -> None:
    print("\n── Recent Logs ─────────────────────────────────────────────")
    lines = get_latest_log(max_lines=12)
    for line in lines:
        print(f"  {line}")


# ── Main loop ────────────────────────────────────────────────────────────
def main() -> None:
    print("Starting KaggleAuto monitor… (Ctrl+C to exit)\n")
    try:
        while True:
            state = load_json(STATE_PATH)
            config = load_json(CONFIG_PATH)

            clear_screen()
            render_header()
            render_active_session(state, config)
            render_quota(state, config)
            render_health(state)
            render_logs()
            print(f"\n  Next refresh in {REFRESH_INTERVAL}s…")

            time.sleep(REFRESH_INTERVAL)
    except KeyboardInterrupt:
        print("\n\nMonitor stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
