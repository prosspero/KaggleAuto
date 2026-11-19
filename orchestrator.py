#!/usr/bin/env python3
"""
KaggleAuto Orchestrator
=======================
Rotates 6 Kaggle accounts to serve Qwen3-32B 24/7 via Cloudflare Tunnel.
Runs on the user's LOCAL Windows machine and manages remote Kaggle kernels
through the ``kaggle`` CLI.

Usage:
    python orchestrator.py              # normal start / resume
    python orchestrator.py --reset      # clear state and restart from scratch
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import logging
import logging.handlers
import os
import pathlib
import signal
import subprocess
import shutil
import sys
import threading
import time
from typing import Any, Dict, List, Optional

# ──────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────
BASE_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
NOTEBOOK_TEMPLATE_DIR = BASE_DIR / "notebook"

# ──────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("kaggle_orchestrator")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(_fmt)
logger.addHandler(_console)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "orchestrator.log",
    maxBytes=10 * 1024 * 1024,   # overridden after config load
    backupCount=5,
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)


# ──────────────────────────────────────────────────────────────────────
# Shutdown machinery
# ──────────────────────────────────────────────────────────────────────
_shutdown_event = threading.Event()


def _signal_handler(signum: int, _frame: Any) -> None:
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.info("Received %s — initiating graceful shutdown …", sig_name)
    _shutdown_event.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)
if sys.platform == "win32":
    signal.signal(signal.SIGBREAK, _signal_handler)  # Ctrl+Break on Windows


# ──────────────────────────────────────────────────────────────────────
# Config & state helpers
# ──────────────────────────────────────────────────────────────────────
_config: Dict[str, Any] = {}


def load_config() -> Dict[str, Any]:
    """Read *config.json* and return it; also refresh the module-level cache."""
    global _config
    if not CONFIG_PATH.exists():
        logger.critical("Config file not found at %s — copy config.example.json and fill it in.", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        _config = json.load(fh)

    # Apply log tuning from config
    _file_handler.maxBytes = _config.get("log_max_bytes", 10 * 1024 * 1024)
    _file_handler.backupCount = _config.get("log_backup_count", 5)

    n = len(_config.get("accounts", []))
    if n == 0:
        logger.critical("No accounts defined in config.json")
        sys.exit(1)
    logger.info("Loaded config with %d accounts.", n)
    return _config


def _default_state() -> Dict[str, Any]:
    return {
        "current_account_index": 0,
        "current_session_start": None,       # ISO-8601 or null
        "quota": {},                         # {"username": {"used_hours": 0.0, "week_start": "ISO"}}
        "last_health_check": None,
        "last_health_ok": False,
        "consecutive_failures": 0,
        "history": [],                       # last 50 events
    }


def load_state() -> Dict[str, Any]:
    """Load persisted state or create a fresh one."""
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            logger.info("Resumed state from %s (account index %s).", STATE_PATH, state.get("current_account_index"))
            return state
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Corrupt state file (%s) — starting fresh.", exc)
    return _default_state()


def save_state(state: Dict[str, Any]) -> None:
    """Atomically persist state to disk."""
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    shutil.move(str(tmp), str(STATE_PATH))


def _record_event(state: Dict[str, Any], event: str, **extra: Any) -> None:
    entry = {"time": _now_iso(), "event": event, **extra}
    state.setdefault("history", []).append(entry)
    state["history"] = state["history"][-50:]   # keep bounded


# ──────────────────────────────────────────────────────────────────────
# Time helpers
# ──────────────────────────────────────────────────────────────────────
def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(s: Optional[str]) -> Optional[_dt.datetime]:
    if not s:
        return None
    return _dt.datetime.fromisoformat(s)


def _hours_since(iso_str: Optional[str]) -> float:
    t = _parse_iso(iso_str)
    if t is None:
        return 0.0
    delta = _now() - t
    return delta.total_seconds() / 3600.0


# ──────────────────────────────────────────────────────────────────────
# Quota tracking
# ──────────────────────────────────────────────────────────────────────
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _current_week_start(reset_day: str) -> _dt.datetime:
    """Return the most recent occurrence of *reset_day* at 00:00 UTC."""
    target_wd = _WEEKDAYS.get(reset_day.lower(), 6)
    now = _now()
    days_since = (now.weekday() - target_wd) % 7
    start = (now - _dt.timedelta(days=days_since)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start


def track_quota(state: Dict[str, Any], username: str, hours_used: float) -> None:
    """Add *hours_used* to the account's weekly tally, resetting if a new week."""
    reset_day = _config.get("quota", {}).get("quota_reset_day", "sunday")
    week_start = _current_week_start(reset_day).isoformat()

    quota = state.setdefault("quota", {})
    entry = quota.setdefault(username, {"used_hours": 0.0, "week_start": week_start})

    # Reset if new week
    if entry.get("week_start") != week_start:
        logger.info("Quota reset for %s (new week starting %s).", username, week_start)
        entry["used_hours"] = 0.0
        entry["week_start"] = week_start

    entry["used_hours"] = round(entry["used_hours"] + hours_used, 4)
    logger.debug("Quota for %s: %.2f h used this week.", username, entry["used_hours"])


def _quota_remaining(state: Dict[str, Any], username: str) -> float:
    limit = _config.get("quota", {}).get("weekly_limit_hours", 30)
    reset_day = _config.get("quota", {}).get("quota_reset_day", "sunday")
    week_start = _current_week_start(reset_day).isoformat()

    entry = state.get("quota", {}).get(username, {})
    if entry.get("week_start") != week_start:
        return float(limit)
    return max(0.0, limit - entry.get("used_hours", 0.0))


# ──────────────────────────────────────────────────────────────────────
# Kaggle CLI wrappers
# ──────────────────────────────────────────────────────────────────────
def _kaggle_env(account: Dict[str, str]) -> Dict[str, str]:
    """Return an env dict with KAGGLE credentials injected."""
    env = os.environ.copy()
    key = account["api_key"]
    env["KAGGLE_USERNAME"] = account["username"]
    env["KAGGLE_KEY"] = key
    env["KAGGLE_API_TOKEN"] = key
    return env


def _prepare_kernel_metadata(account: Dict[str, str]) -> pathlib.Path:
    """Copy notebook template dir into a temp push directory, replacing placeholders."""
    push_dir = BASE_DIR / ".push_staging" / account["username"]
    push_dir.mkdir(parents=True, exist_ok=True)

    # Copy and patch notebook script
    src_script = NOTEBOOK_TEMPLATE_DIR / "kaggle_notebook.py"
    if not src_script.exists():
        logger.error("Notebook template not found at %s", src_script)
        raise FileNotFoundError(src_script)
    with open(src_script, "r", encoding="utf-8") as fh:
        script_text = fh.read()
    token = _config.get("cloudflare_tunnel_token", "")
    script_text = script_text.replace("__CLOUDFLARE_TUNNEL_TOKEN__", token)
    with open(push_dir / "kaggle_notebook.py", "w", encoding="utf-8") as fh:
        fh.write(script_text)

    # Copy and patch kernel-metadata.json
    meta_src = NOTEBOOK_TEMPLATE_DIR / "kernel-metadata.json"
    if not meta_src.exists():
        logger.error("kernel-metadata.json template not found at %s", meta_src)
        raise FileNotFoundError(meta_src)

    with open(meta_src, "r", encoding="utf-8") as fh:
        meta_text = fh.read()

    # Replace placeholders
    slug = account.get("kernel_slug", f"{account['username']}/qwen3-32b-serve")
    kernel_title = slug.split("/")[-1]
    meta_text = meta_text.replace("__USERNAME__", account["username"])
    meta_text = meta_text.replace("__KERNEL_SLUG__", slug)
    meta_text = meta_text.replace("__KERNEL_TITLE__", kernel_title)

    with open(push_dir / "kernel-metadata.json", "w", encoding="utf-8") as fh:
        fh.write(meta_text)

    return push_dir


def push_notebook(account: Dict[str, str]) -> bool:
    """Push the kernel for *account*. Returns True on success."""
    username = account["username"]
    logger.info("Pushing notebook for account %s …", username)

    try:
        push_dir = _prepare_kernel_metadata(account)
    except FileNotFoundError:
        return False

    try:
        result = subprocess.run(
            ["kaggle", "kernels", "push", "-p", str(push_dir)],
            capture_output=True, text=True, timeout=120,
            env=_kaggle_env(account),
        )
        if result.returncode == 0 and "error" not in result.stdout.lower():
            logger.info("Push succeeded for %s: %s", username, result.stdout.strip())
            return True
        else:
            err = result.stderr.strip() or result.stdout.strip()
            logger.error("Push failed for %s: %s", username, err)
            return False
    except subprocess.TimeoutExpired:
        logger.error("Push timed out for %s.", username)
        return False
    except FileNotFoundError:
        logger.critical("'kaggle' CLI not found — install it with: pip install kaggle")
        return False
    except Exception as exc:
        logger.exception("Unexpected error pushing notebook for %s: %s", username, exc)
        return False


def check_notebook_status(account: Dict[str, str]) -> str:
    """Return the kernel status string (e.g. 'running', 'complete', 'error', 'unknown')."""
    username = account["username"]
    slug = account.get("kernel_slug", f"{username}/qwen3-32b-serve")

    try:
        result = subprocess.run(
            ["kaggle", "kernels", "status", slug],
            capture_output=True, text=True, timeout=60,
            env=_kaggle_env(account),
        )
        output = result.stdout.strip().lower()
        logger.debug("Status for %s: %s", slug, output)

        # Kaggle CLI outputs lines like: "status 'running'"  or just "running"
        for keyword in ("cancel_requested", "running", "queued", "complete", "error", "cancel_acknowledged", "cancelacknowledged", "failed"):
            if keyword in output:
                return keyword
        return output or "unknown"
    except subprocess.TimeoutExpired:
        logger.warning("Status check timed out for %s.", slug)
        return "timeout"
    except Exception as exc:
        logger.exception("Status check error for %s: %s", slug, exc)
        return "unknown"


def stop_notebook(account: Dict[str, str], wait_for_stop: bool = True, max_wait_sec: int = 60) -> bool:
    """Stop a running kernel by pushing a self-terminating script.

    Kaggle CLI has no 'cancel' command, so we overwrite the kernel with a
    tiny script that exits immediately.  The old GPU session is replaced and
    freed within seconds.
    """
    username = account["username"]
    slug = account.get("kernel_slug", f"{username}/qwen3-32b-serve")

    # Only stop if it's actually running, queued, or in cancel requested
    status = check_notebook_status(account)
    if status not in ("running", "queued", "cancel_requested"):
        logger.info("Kernel %s is already '%s' — no stop needed.", slug, status)
        return True

    logger.info("Stopping kernel %s (current status: %s) …", slug, status)

    stop_dir = BASE_DIR / ".stop_staging" / username
    stop_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Write a self-terminating script
        with open(stop_dir / "kaggle_notebook.py", "w", encoding="utf-8") as fh:
            fh.write('import sys; print("Session stopped by KaggleAuto orchestrator."); sys.exit(0)\n')

        # Write kernel metadata (must match the slug)
        kernel_title = slug.split("/")[-1]
        meta = {
            "id": slug,
            "title": kernel_title,
            "code_file": "kaggle_notebook.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False,          # no GPU needed for exit script
            "enable_tpu": False,
            "enable_internet": False,
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
            "keywords": [],
            "docker_image_pinning_type": "latest",
        }
        with open(stop_dir / "kernel-metadata.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)

        result = subprocess.run(
            ["kaggle", "kernels", "push", "-p", str(stop_dir)],
            capture_output=True, text=True, timeout=120,
            env=_kaggle_env(account),
        )
        if result.returncode == 0:
            logger.info("Stop-push succeeded for %s: %s", username, result.stdout.strip())
            if wait_for_stop:
                logger.info("Waiting for %s to confirm stopped …", slug)
                waited = 0
                while waited < max_wait_sec and not _shutdown_event.is_set():
                    time.sleep(5)
                    waited += 5
                    st = check_notebook_status(account)
                    if st not in ("running", "queued", "cancel_requested"):
                        logger.info("Kernel %s confirmed stopped (%s) after %ds.", slug, st, waited)
                        break
            return True
        else:
            logger.error("Stop-push failed for %s (rc=%d): %s", username, result.returncode, result.stderr.strip())
            return False
    except Exception as exc:
        logger.exception("Error stopping notebook for %s: %s", username, exc)
        return False
    finally:
        shutil.rmtree(stop_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────
# Health checks
# ──────────────────────────────────────────────────────────────────────
def health_check(url: Optional[str] = None) -> bool:
    """HTTP GET the Cloudflare tunnel endpoint; return True if model is up."""
    import urllib.request
    import urllib.error

    url = url or _config.get("health_check_url", "")
    if not url:
        logger.warning("No health_check_url configured — skipping.")
        return True  # optimistic if not configured

    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "KaggleAutoOrchestrator/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                logger.debug("Health check OK (%s).", url)
                return True
            else:
                logger.warning("Health check returned HTTP %d.", resp.status)
                return False
    except urllib.error.URLError as exc:
        logger.warning("Health check failed: %s", exc.reason)
        return False
    except Exception as exc:
        logger.warning("Health check error: %s", exc)
        return False


class HealthCheckThread(threading.Thread):
    """Background thread that periodically pings the model endpoint."""

    def __init__(self, state: Dict[str, Any], interval: float = 60.0):
        super().__init__(daemon=True, name="health-checker")
        self.state = state
        self.interval = interval
        self._consecutive_failures = 0
        self.max_consecutive_failures = 5
        self.startup_grace_minutes = 15.0

    @property
    def is_unhealthy(self) -> bool:
        return self._consecutive_failures >= self.max_consecutive_failures

    def reset(self) -> None:
        """Reset failure counter when rotating or starting a new session."""
        self._consecutive_failures = 0
        self.state["consecutive_failures"] = 0
        self.state["last_health_ok"] = False

    def run(self) -> None:
        logger.info("Health-check thread started (interval=%ds).", self.interval)
        while not _shutdown_event.is_set():
            # Check how long the current session has been running
            session_start = self.state.get("current_session_start")
            elapsed_m = (_hours_since(session_start) * 60.0) if session_start else 0.0

            ok = health_check()
            self.state["last_health_check"] = _now_iso()
            self.state["last_health_ok"] = ok

            if ok:
                if self._consecutive_failures > 0:
                    logger.info("✔ Model endpoint is now healthy and responding!")
                self._consecutive_failures = 0
            else:
                if elapsed_m < self.startup_grace_minutes:
                    # Still in boot/download grace period — expected behavior!
                    logger.info(
                        "Model booting / loading on Kaggle (%.1f min elapsed, grace period %.0f min).",
                        elapsed_m, self.startup_grace_minutes,
                    )
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                    logger.warning(
                        "Health check failure #%d/%d (session elapsed: %.1f min).",
                        self._consecutive_failures,
                        self.max_consecutive_failures,
                        elapsed_m,
                    )

            self.state["consecutive_failures"] = self._consecutive_failures
            save_state(self.state)
            _shutdown_event.wait(self.interval)


# ──────────────────────────────────────────────────────────────────────
# Account selection
# ──────────────────────────────────────────────────────────────────────
def _next_account_index(state: Dict[str, Any], accounts: List[Dict]) -> int:
    """Pick the next account that has quota remaining."""
    n = len(accounts)
    start = (state["current_account_index"] + 1) % n
    for offset in range(n):
        idx = (start + offset) % n
        remaining = _quota_remaining(state, accounts[idx]["username"])
        if remaining >= 1.0:  # need at least 1 hour
            return idx
    # All exhausted — warn and use the one with the most headroom
    logger.warning("All accounts have depleted quota! Picking least-used.")
    best_idx, best_rem = 0, -1.0
    for idx, acct in enumerate(accounts):
        rem = _quota_remaining(state, acct["username"])
        if rem > best_rem:
            best_idx, best_rem = idx, rem
    return best_idx


def rotate_account(state: Dict[str, Any], accounts: List[Dict], *, force: bool = False) -> int:
    """
    Execute the account switchover.

    1. Pick the next account.
    2. Push its notebook (start NEXT before stopping CURRENT for overlap).
    3. Wait for the new notebook to be running.
    4. Return the new account index.

    If *force* is True the overlap wait is skipped (used for crash failover).
    """
    old_idx = state["current_account_index"]
    new_idx = _next_account_index(state, accounts)
    old_acct = accounts[old_idx]
    new_acct = accounts[new_idx]

    logger.info(
        "Rotating: %s (idx %d) → %s (idx %d) %s",
        old_acct["username"], old_idx,
        new_acct["username"], new_idx,
        "[FORCED]" if force else "",
    )
    _record_event(state, "rotate_start", old=old_acct["username"], new=new_acct["username"], forced=force)

    # 1. Stop the OLD notebook and WAIT until it's confirmed stopped (zero overlap)
    logger.info("Stopping old notebook on %s before starting next account …", old_acct["username"])
    stop_notebook(old_acct, wait_for_stop=True)

    # 2. Record session accounting for the old account
    if state.get("current_session_start"):
        hours = _hours_since(state["current_session_start"])
        track_quota(state, old_acct["username"], hours)
        logger.info("Account %s ran for %.2f h this session.", old_acct["username"], hours)

    # 3. Push the new notebook
    push_ok = push_notebook(new_acct)
    if not push_ok:
        logger.error("Failed to push notebook for %s — trying next account.", new_acct["username"])
        # Try the one after that
        state["current_account_index"] = new_idx
        alt_idx = _next_account_index(state, accounts)
        if alt_idx == new_idx:
            logger.critical("No alternative account available. Staying on current.")
            state["current_account_index"] = old_idx
            return old_idx
        return rotate_account(state, accounts, force=True)

    # 4. Wait for new notebook to reach "running" (or "queued" → "running")
    max_wait = _config.get("timing", {}).get("max_start_wait_minutes", 15) * 60
    poll = _config.get("timing", {}).get("status_poll_interval_seconds", 30)
    waited = 0.0
    running = False

    while waited < max_wait and not _shutdown_event.is_set():
        status = check_notebook_status(new_acct)
        if status == "running":
            running = True
            break
        if status in ("error", "cancelacknowledged", "cancel_acknowledged", "failed", "complete"):
            logger.error("New notebook %s entered '%s' — retrying push.", new_acct["username"], status)
            push_notebook(new_acct)  # retry once
        _shutdown_event.wait(poll)
        waited += poll

    if not running:
        logger.error("New notebook %s did not start within %d min.", new_acct["username"], max_wait // 60)
        _record_event(state, "rotate_fail", account=new_acct["username"])
        # Fall through — update state anyway; the main loop will notice via health check

    # 5. Update state
    state["current_account_index"] = new_idx
    state["current_session_start"] = _now_iso()
    state["consecutive_failures"] = 0
    state["last_health_ok"] = False
    _record_event(state, "rotate_complete", account=new_acct["username"])
    save_state(state)

    logger.info("Rotation complete. Active account: %s", new_acct["username"])
    return new_idx


# ──────────────────────────────────────────────────────────────────────
# Initial start (cold boot)
# ──────────────────────────────────────────────────────────────────────
def cold_start(state: Dict[str, Any], accounts: List[Dict]) -> None:
    """Push the first notebook if no session is currently active."""
    idx = state["current_account_index"]
    acct = accounts[idx]

    # Stop any other accounts that might still have active sessions
    for i, other in enumerate(accounts):
        if i != idx and other.get("api_key") and not other.get("api_key", "").startswith("hex_"):
            stop_notebook(other)

    logger.info("Cold start: pushing notebook for %s (idx %d).", acct["username"], idx)

    ok = push_notebook(acct)
    if not ok:
        # Try the next one
        logger.warning("Cold start push failed — rotating to next account.")
        rotate_account(state, accounts, force=True)
        return

    state["current_session_start"] = _now_iso()
    _record_event(state, "cold_start", account=acct["username"])
    save_state(state)

    # Wait until it's running
    max_wait = _config.get("timing", {}).get("max_start_wait_minutes", 15) * 60
    poll = _config.get("timing", {}).get("status_poll_interval_seconds", 30)
    waited = 0.0
    while waited < max_wait and not _shutdown_event.is_set():
        status = check_notebook_status(acct)
        if status == "running":
            logger.info("Cold start: notebook is running.")
            return
        if status in ("error", "cancelacknowledged", "cancel_acknowledged", "failed", "complete"):
            logger.error("Cold start notebook entered '%s' — rotating.", status)
            rotate_account(state, accounts, force=True)
            return
        _shutdown_event.wait(poll)
        waited += poll

    logger.warning("Cold start: notebook did not reach 'running' within timeout.")


# ──────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────
def main_loop() -> None:
    """Core scheduling loop — runs until SIGINT / SIGTERM."""
    config = load_config()
    state = load_state()
    accounts: List[Dict] = config["accounts"]
    timing = config.get("timing", {})

    session_dur_h: float = timing.get("session_duration_hours", 11.5)
    overlap_min: float = timing.get("overlap_minutes", 5)
    hc_interval: float = timing.get("health_check_interval_seconds", 60)
    poll_interval: float = timing.get("status_poll_interval_seconds", 30)

    # Decide whether we need a cold start
    if state.get("current_session_start") is None:
        cold_start(state, accounts)
    else:
        elapsed = _hours_since(state["current_session_start"])
        logger.info(
            "Resuming. Active account: %s, elapsed %.2f h.",
            accounts[state["current_account_index"]]["username"],
            elapsed,
        )
        # If the previous session is way past its limit, rotate immediately
        if elapsed > session_dur_h + 1.0:
            logger.warning("Previous session exceeded duration — rotating now.")
            rotate_account(state, accounts, force=True)

    # Start health-check thread
    hc_thread = HealthCheckThread(state, interval=hc_interval)
    hc_thread.start()

    logger.info(
        "Orchestrator running. Session=%.1fh, Overlap=%dmin, %d accounts.",
        session_dur_h, overlap_min, len(accounts),
    )

    loop_sleep = min(poll_interval, 30.0)

    while not _shutdown_event.is_set():
        try:
            _loop_tick(state, accounts, session_dur_h, overlap_min, hc_thread)
        except Exception:
            logger.exception("Unhandled error in main loop tick:")

        save_state(state)
        _shutdown_event.wait(loop_sleep)

    # ── Graceful shutdown ──
    logger.info("Shutting down orchestrator …")
    # Save final quota for current session
    if state.get("current_session_start"):
        hours = _hours_since(state["current_session_start"])
        acct = accounts[state["current_account_index"]]
        track_quota(state, acct["username"], hours)
    save_state(state)
    logger.info("State saved. Goodbye.")


def _loop_tick(
    state: Dict[str, Any],
    accounts: List[Dict],
    session_dur_h: float,
    overlap_min: float,
    hc_thread: HealthCheckThread,
) -> None:
    """One iteration of the main scheduling loop."""
    idx = state["current_account_index"]
    acct = accounts[idx]
    elapsed = _hours_since(state.get("current_session_start"))

    # ── 1. Check if current notebook is still alive ──
    status = check_notebook_status(acct)
    if status in ("error", "complete", "cancelacknowledged", "cancel_acknowledged", "failed"):
        logger.error("Notebook %s is '%s' — immediate failover!", acct["username"], status)
        track_quota(state, acct["username"], elapsed)
        _record_event(state, "crash_failover", account=acct["username"], status=status)
        state["current_session_start"] = None
        rotate_account(state, accounts, force=True)
        return

    # ── 2. Proactive health-check failover (give 30 min grace period for model download & warmup) ──
    if hc_thread.is_unhealthy and elapsed >= 0.5:
        logger.error(
            "Model endpoint unresponsive after grace period (%d consecutive failures) — failover.",
            state.get("consecutive_failures", 0),
        )
        track_quota(state, acct["username"], elapsed)
        _record_event(state, "health_failover", account=acct["username"])
        state["current_session_start"] = None
        rotate_account(state, accounts, force=True)
        return

    # ── 3. Scheduled rotation (with overlap) ──
    rotation_threshold_h = session_dur_h - (overlap_min / 60.0)
    if elapsed >= rotation_threshold_h:
        logger.info(
            "Session for %s at %.2f h (threshold %.2f h) — starting rotation with overlap.",
            acct["username"], elapsed, rotation_threshold_h,
        )
        rotate_account(state, accounts, force=False)
        return


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="KaggleAuto GPU Rotation Orchestrator")
    parser.add_argument("--reset", action="store_true", help="Clear state and start fresh.")
    args = parser.parse_args()

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        logger.info("State reset.")

    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — exiting.")
    except SystemExit:
        raise
    except Exception:
        logger.critical("Fatal error:", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
