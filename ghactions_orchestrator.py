#!/usr/bin/env python3
"""
KaggleAuto — GitHub Actions Orchestrator
=========================================
Stateless rotation script designed to run every 15 minutes via GitHub Actions.
Reads state.json from repo, decides if action is needed, and updates state.

No long-running process needed — GitHub Actions cron handles the scheduling.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
NOTEBOOK_DIR = ROOT / "notebook"

# ── Configuration from environment ───────────────────────────────────────
SESSION_DURATION_HOURS = 11.5  # auto-rotate after this
OVERLAP_MINUTES = 5
MAX_START_WAIT_SECONDS = 600   # 10 min max wait for notebook to start
WEEKLY_LIMIT_HOURS = 30.0
QUOTA_RESET_DAY = "sunday"     # 0=Monday, 6=Sunday


def get_accounts():
    """Build account list from environment variables or fallback to config.json."""
    accounts = []
    config_path = ROOT / "config.json"
    cfg_accounts = []
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_accounts = json.load(f).get("accounts", [])
        except Exception:
            pass

    for i in range(1, 7):
        user = os.environ.get(f"KAGGLE_USER_{i}", "")
        key = os.environ.get(f"KAGGLE_KEY_{i}", "")
        # Fallback to config.json if env vars not provided
        if (not user or not key) and i - 1 < len(cfg_accounts):
            ca = cfg_accounts[i - 1]
            if ca.get("username") and ca.get("api_key") and not ca["api_key"].startswith("hex_"):
                user = ca["username"]
                key = ca["api_key"]
                slug = ca.get("kernel_slug", f"{user}/qwen-35b-serve-{i}")
        else:
            slug = f"{user}/qwen-35b-serve-{i}"

        if user and key and not key.startswith("hex_"):
            accounts.append({
                "username": user,
                "api_key": key,
                "kernel_slug": slug,
            })

    if not accounts:
        print("ERROR: No valid Kaggle accounts found in environment or config.json.")
        sys.exit(1)
    print(f"Loaded {len(accounts)} accounts.")
    return accounts


# ── Time helpers ─────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def parse_iso(s):
    if not s:
        return None
    return datetime.fromisoformat(s)


def hours_since(iso_str):
    t = parse_iso(iso_str)
    if t is None:
        return 0.0
    delta = now_utc() - t
    return delta.total_seconds() / 3600.0


# ── State management ────────────────────────────────────────────────────
def default_state():
    return {
        "current_account_index": 0,
        "current_session_start": None,
        "quota": {},
        "last_rotation": None,
        "history": [],
    }


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r") as f:
                state = json.load(f)
            print(f"Loaded state: account index={state.get('current_account_index')}, "
                  f"session_start={state.get('current_session_start')}")
            return state
        except (json.JSONDecodeError, KeyError) as e:
            print(f"WARNING: Corrupt state.json ({e}), starting fresh.")
    return default_state()


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    shutil.move(str(tmp), str(STATE_PATH))
    print("State saved.")


def record_event(state, event, **extra):
    entry = {"time": now_iso(), "event": event, **extra}
    state.setdefault("history", []).append(entry)
    state["history"] = state["history"][-30:]


# ── Quota tracking ──────────────────────────────────────────────────────
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}


def current_week_start():
    target_wd = WEEKDAYS.get(QUOTA_RESET_DAY.lower(), 6)
    n = now_utc()
    days_since = (n.weekday() - target_wd) % 7
    start = (n - timedelta(days=days_since)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat()


def track_quota(state, username, hours_used):
    week_start = current_week_start()
    quota = state.setdefault("quota", {})
    entry = quota.setdefault(username, {"used_hours": 0.0, "week_start": week_start})
    if entry.get("week_start") != week_start:
        print(f"  Quota reset for {username} (new week).")
        entry["used_hours"] = 0.0
        entry["week_start"] = week_start
    entry["used_hours"] = round(entry["used_hours"] + hours_used, 4)
    print(f"  Quota for {username}: {entry['used_hours']:.2f}h used this week.")


def quota_remaining(state, username):
    week_start = current_week_start()
    entry = state.get("quota", {}).get(username, {})
    if entry.get("week_start") != week_start:
        return WEEKLY_LIMIT_HOURS
    return max(0.0, WEEKLY_LIMIT_HOURS - entry.get("used_hours", 0.0))


# ── Kaggle CLI wrappers ─────────────────────────────────────────────────
def kaggle_env(account):
    env = os.environ.copy()
    key = account["api_key"]
    env["KAGGLE_USERNAME"] = account["username"]
    env["KAGGLE_KEY"] = key
    env["KAGGLE_API_TOKEN"] = key
    return env


def prepare_push_dir(account):
    push_dir = ROOT / ".push_staging" / account["username"]
    push_dir.mkdir(parents=True, exist_ok=True)

    # Copy and patch notebook script
    src_script = NOTEBOOK_DIR / "kaggle_notebook.py"
    with open(src_script, "r", encoding="utf-8") as f:
        script_text = f.read()
    token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
    if not token and (ROOT / "config.json").exists():
        try:
            with open(ROOT / "config.json", "r", encoding="utf-8") as f:
                token = json.load(f).get("cloudflare_tunnel_token", "")
        except Exception:
            pass
    script_text = script_text.replace("__CLOUDFLARE_TUNNEL_TOKEN__", token)
    with open(push_dir / "kaggle_notebook.py", "w", encoding="utf-8") as f:
        f.write(script_text)

    # Copy and patch metadata
    with open(NOTEBOOK_DIR / "kernel-metadata.json", "r") as f:
        meta = f.read()

    slug = account.get("kernel_slug", f"{account['username']}/qwen-35b-serve")
    kernel_title = slug.split("/")[-1]
    meta = meta.replace("__USERNAME__", account["username"])
    meta = meta.replace("__KERNEL_SLUG__", slug)
    meta = meta.replace("__KERNEL_TITLE__", kernel_title)

    with open(push_dir / "kernel-metadata.json", "w") as f:
        f.write(meta)

    return push_dir


def push_notebook(account):
    username = account["username"]
    print(f"  Pushing notebook for {username}...")

    try:
        push_dir = prepare_push_dir(account)
    except FileNotFoundError as e:
        print(f"  ERROR: Template missing: {e}")
        return False

    try:
        result = subprocess.run(
            ["kaggle", "kernels", "push", "-p", str(push_dir)],
            capture_output=True, text=True, timeout=120,
            env=kaggle_env(account),
        )
        if result.returncode == 0:
            print(f"  Push OK: {result.stdout.strip()}")
            return True
        else:
            print(f"  Push FAILED (rc={result.returncode}): {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  Push TIMEOUT for {username}.")
        return False
    except Exception as e:
        print(f"  Push ERROR for {username}: {e}")
        return False


def check_status(account):
    username = account["username"]
    slug = account.get("kernel_slug", f"{username}/qwen3-32b-serve")

    try:
        result = subprocess.run(
            ["kaggle", "kernels", "status", slug],
            capture_output=True, text=True, timeout=60,
            env=kaggle_env(account),
        )
        output = result.stdout.strip().lower()
        for keyword in ("running", "complete", "error", "cancelacknowledged", "queued"):
            if keyword in output:
                return keyword
        return output or "unknown"
    except Exception as e:
        print(f"  Status check error for {slug}: {e}")
        return "unknown"


def stop_notebook(account):
    """Stop a running kernel by pushing a self-terminating script."""
    username = account["username"]
    slug = account.get("kernel_slug", f"{username}/qwen3-32b-serve")

    status = check_status(account)
    if status not in ("running", "queued"):
        print(f"  Kernel {slug} is already '{status}' — no stop needed.")
        return True

    print(f"  Stopping kernel {slug} (status: {status})...")
    stop_dir = ROOT / ".stop_staging" / username
    stop_dir.mkdir(parents=True, exist_ok=True)

    try:
        with open(stop_dir / "kaggle_notebook.py", "w", encoding="utf-8") as fh:
            fh.write('import sys; print("Session stopped by KaggleAuto orchestrator."); sys.exit(0)\n')

        kernel_title = slug.split("/")[-1]
        meta = {
            "id": slug,
            "title": kernel_title,
            "code_file": "kaggle_notebook.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False,
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
            env=kaggle_env(account),
        )
        if result.returncode == 0:
            print(f"  Stop-push OK for {username}: {result.stdout.strip()}")
            return True
        else:
            print(f"  Stop-push FAILED for {username}: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"  Error stopping notebook for {username}: {e}")
        return False
    finally:
        shutil.rmtree(stop_dir, ignore_errors=True)


# ── Health check ────────────────────────────────────────────────────────
def health_check():
    url = os.environ.get("HEALTH_CHECK_URL", "")
    if not url and (ROOT / "config.json").exists():
        try:
            with open(ROOT / "config.json", "r", encoding="utf-8") as f:
                url = json.load(f).get("health_check_url", "")
        except Exception:
            pass
    if not url:
        print("  No HEALTH_CHECK_URL set — skipping health check.")
        return None  # unknown

    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            method="GET"
        )
        req.add_header("User-Agent", "KaggleAutoGHA/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                print(f"  Health check OK ({url})")
                return True
    except urllib.error.URLError as e:
        print(f"  Health check FAILED: {e.reason}")
    except Exception as e:
        print(f"  Health check ERROR: {e}")
    return False


# ── Account selection ───────────────────────────────────────────────────
def pick_next_account(state, accounts):
    n = len(accounts)
    start = (state["current_account_index"] + 1) % n
    for offset in range(n):
        idx = (start + offset) % n
        remaining = quota_remaining(state, accounts[idx]["username"])
        if remaining >= 1.0:
            return idx
    # All exhausted — pick least used
    print("  WARNING: All accounts low on quota! Picking least-used.")
    best_idx = 0
    best_rem = -1.0
    for idx, acct in enumerate(accounts):
        rem = quota_remaining(state, acct["username"])
        if rem > best_rem:
            best_idx, best_rem = idx, rem
    return best_idx


# ── Core rotation logic ────────────────────────────────────────────────
def do_rotation(state, accounts, reason="scheduled"):
    old_idx = state["current_account_index"]
    new_idx = pick_next_account(state, accounts)
    old_acct = accounts[old_idx]
    new_acct = accounts[new_idx]

    print(f"\n>>> ROTATING: {old_acct['username']} → {new_acct['username']} (reason: {reason})")

    # 1. Stop old notebook first so only ONE session is ever active
    print(f"  Stopping old notebook on {old_acct['username']} before starting next account...")
    stop_notebook(old_acct)

    # 2. Track quota for old account
    if state.get("current_session_start"):
        hours = hours_since(state["current_session_start"])
        track_quota(state, old_acct["username"], hours)
        print(f"  Old account ran for {hours:.2f}h this session.")

    # 3. Push new notebook
    ok = push_notebook(new_acct)
    if not ok:
        print(f"  Push failed for {new_acct['username']}, trying next...")
        state["current_account_index"] = new_idx
        alt_idx = pick_next_account(state, accounts)
        if alt_idx != new_idx:
            new_acct = accounts[alt_idx]
            new_idx = alt_idx
            ok = push_notebook(new_acct)
        if not ok:
            print("  CRITICAL: Cannot push to any account!")
            state["current_account_index"] = old_idx
            record_event(state, "rotate_failed", reason=reason)
            return

    # 4. Wait briefly for it to start (GH Actions has limited time)
    print(f"  Waiting for {new_acct['username']} to start...")
    waited = 0
    while waited < MAX_START_WAIT_SECONDS:
        status = check_status(new_acct)
        print(f"    Status after {waited}s: {status}")
        if status == "running":
            break
        if status in ("error", "complete", "cancelacknowledged"):
            print(f"  Notebook entered '{status}' — pushing again...")
            push_notebook(new_acct)
        time.sleep(30)
        waited += 30

    # 5. Update state
    state["current_account_index"] = new_idx
    state["current_session_start"] = now_iso()
    state["last_rotation"] = now_iso()
    record_event(state, "rotated", old=old_acct["username"],
                 new=new_acct["username"], reason=reason)
    print(f">>> Rotation complete. Active: {new_acct['username']}")


# ── Main decision logic ────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"  KaggleAuto — GitHub Actions Check @ {now_iso()}")
    print("=" * 60)

    accounts = get_accounts()
    state = load_state()

    force = os.environ.get("FORCE_ROTATE", "false").lower() == "true"

    idx = state["current_account_index"]
    if idx >= len(accounts):
        print(f"Account index {idx} out of range, resetting to 0.")
        state["current_account_index"] = 0
        idx = 0

    acct = accounts[idx]
    session_start = state.get("current_session_start")

    # ── CASE 1: Force rotation ──
    if force:
        print("\n[FORCE] Manual rotation requested.")
        do_rotation(state, accounts, reason="manual_force")
        save_state(state)
        return

    # ── CASE 2: No session running (first ever run, or after crash) ──
    if session_start is None:
        print("\n[COLD START] No active session. Starting first notebook...")
        ok = push_notebook(acct)
        if ok:
            state["current_session_start"] = now_iso()
            record_event(state, "cold_start", account=acct["username"])
            # Wait for it to start
            waited = 0
            while waited < MAX_START_WAIT_SECONDS:
                status = check_status(acct)
                print(f"  Status after {waited}s: {status}")
                if status == "running":
                    break
                time.sleep(30)
                waited += 30
        else:
            print("  Cold start push failed — rotating to next account.")
            do_rotation(state, accounts, reason="cold_start_failed")
        save_state(state)
        return

    # ── Active session exists — check it ──
    elapsed_h = hours_since(session_start)
    print(f"\nActive: {acct['username']} | Elapsed: {elapsed_h:.2f}h / {SESSION_DURATION_HOURS}h")

    # ── CASE 3: Check notebook status ──
    status = check_status(acct)
    print(f"Notebook status: {status}")

    if status in ("error", "complete", "cancelacknowledged"):
        print(f"\n[CRASH] Notebook is '{status}' — immediate failover!")
        do_rotation(state, accounts, reason=f"crash_{status}")
        save_state(state)
        return

    # ── CASE 4: Health check ──
    # Grace period: 15 min for notebook to boot + 10 min for model pull = 25 min total
    # Extend grace to 30 min (0.5h) so a fresh account has time to load
    HEALTH_GRACE_HOURS = 0.5
    hc = health_check()
    if hc is False and elapsed_h > HEALTH_GRACE_HOURS:
        print(f"\n[HEALTH] Model endpoint is down after {elapsed_h:.2f}h — failover!")
        do_rotation(state, accounts, reason="health_failure")
        save_state(state)
        return

    # ── CASE 5: Time-based rotation ──
    rotation_threshold = SESSION_DURATION_HOURS - (OVERLAP_MINUTES / 60.0)
    if elapsed_h >= rotation_threshold:
        print(f"\n[TIMER] Session at {elapsed_h:.2f}h >= threshold {rotation_threshold:.2f}h — rotating!")
        do_rotation(state, accounts, reason="scheduled")
        save_state(state)
        return

    # ── CASE 6: Everything is fine ──
    state["last_health_check"] = now_iso()
    state["last_health_ok"] = (hc is not False)
    remaining = SESSION_DURATION_HOURS - elapsed_h
    print(f"\n[OK] All good. {remaining:.2f}h remaining on {acct['username']}. Nothing to do.")
    save_state(state)


if __name__ == "__main__":
    main()
