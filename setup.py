#!/usr/bin/env python3
"""
KaggleAuto — Setup & Validation Script

Checks that the local environment is ready to run the orchestrator:
  1. Python version >= 3.8
  2. kaggle CLI installed
  3. config.json exists and has the expected structure
  4. Each Kaggle account's API credentials work
  5. Cloudflare tunnel health-check URL is reachable
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.json"

# ── Formatting helpers ───────────────────────────────────────────────────
PASS = "\u2705"  # ✅
FAIL = "\u274C"  # ❌
WARN = "\u26A0\uFE0F"  # ⚠️


def banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def result(ok: bool, msg: str) -> bool:
    icon = PASS if ok else FAIL
    print(f"  {icon}  {msg}")
    return ok


# ── Individual checks ───────────────────────────────────────────────────
def check_python_version() -> bool:
    v = sys.version_info
    ok = v >= (3, 8)
    return result(ok, f"Python {v.major}.{v.minor}.{v.micro}" +
                  ("" if ok else " (need 3.8+)"))


def check_kaggle_cli() -> bool:
    path = shutil.which("kaggle")
    if path is None:
        return result(False, "kaggle CLI not found on PATH — run: pip install kaggle")
    try:
        out = subprocess.run(
            ["kaggle", "--version"],
            capture_output=True, text=True, timeout=15,
        )
        version_str = out.stdout.strip() or out.stderr.strip()
        return result(True, f"kaggle CLI found ({version_str})")
    except Exception as exc:
        return result(False, f"kaggle CLI found at {path} but errored: {exc}")


def check_config_exists() -> bool:
    if CONFIG_PATH.is_file():
        return result(True, f"config.json found at {CONFIG_PATH}")
    hint = " (copy config.example.json → config.json and fill it in)" \
        if EXAMPLE_CONFIG_PATH.is_file() else ""
    return result(False, f"config.json not found{hint}")


def load_and_validate_config():
    """Returns (config_dict | None, list_of_errors)."""
    if not CONFIG_PATH.is_file():
        return None, ["config.json missing"]
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as exc:
        return None, [f"config.json is not valid JSON: {exc}"]

    errors: list[str] = []

    # --- accounts ---
    accounts = cfg.get("accounts")
    if not isinstance(accounts, list) or len(accounts) == 0:
        errors.append("'accounts' must be a non-empty list")
    else:
        valid_count = 0
        for i, acct in enumerate(accounts):
            username = acct.get("username", "")
            key = acct.get("api_key", "")
            is_placeholder = (
                not username or username.startswith("kaggle_user_") or
                not key or key.startswith("hex_api_key_")
            )
            if is_placeholder:
                print(f"  {WARN}  Account #{i+1} is still placeholder (skipped)")
            else:
                valid_count += 1
        if valid_count == 0:
            errors.append("No valid Kaggle accounts configured in config.json")

    # --- cloudflare ---
    token = cfg.get("cloudflare_tunnel_token", "")
    if not token or token.startswith("eyJhIjoiYWJjZGVm"):
        errors.append("cloudflare_tunnel_token is missing or still a placeholder")

    url = cfg.get("health_check_url", "")
    if not url or "yourdomain" in url or "your-tunnel" in url:
        errors.append("health_check_url is missing or still a placeholder")

    # --- timing ---
    timing = cfg.get("timing", {})
    if not isinstance(timing, dict):
        errors.append("'timing' must be an object")
    else:
        for key in ("session_duration_hours", "overlap_minutes",
                     "health_check_interval_seconds", "status_poll_interval_seconds",
                     "max_start_wait_minutes"):
            if key not in timing:
                errors.append(f"timing.{key} is missing")

    # --- quota ---
    quota = cfg.get("quota", {})
    if not isinstance(quota, dict):
        errors.append("'quota' must be an object")
    else:
        if "weekly_limit_hours" not in quota:
            errors.append("quota.weekly_limit_hours is missing")

    return cfg, errors


def check_config_format() -> tuple[bool, dict | None]:
    cfg, errors = load_and_validate_config()
    if errors:
        for e in errors:
            result(False, e)
        return False, None
    result(True, f"config.json structure is valid ({len(cfg['accounts'])} accounts)")
    return True, cfg


def check_kaggle_credentials(cfg: dict) -> bool:
    """Test each account by calling `kaggle kernels list --mine`."""
    all_ok = True
    for acct in cfg.get("accounts", []):
        username = acct.get("username", "?")
        key = acct.get("api_key", "")
        if not key or username.startswith("kaggle_user_") or key.startswith("hex_api_key_"):
            continue
        env = os.environ.copy()
        env["KAGGLE_USERNAME"] = acct.get("username", "")
        env["KAGGLE_KEY"] = key
        env["KAGGLE_API_TOKEN"] = key
        try:
            proc = subprocess.run(
                ["kaggle", "kernels", "list", "--mine", "--page-size", "1"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if proc.returncode == 0:
                result(True, f"Account '{username}' — API credentials OK")
            else:
                stderr = proc.stderr.strip()[:120]
                result(False, f"Account '{username}' — API error: {stderr}")
                all_ok = False
        except FileNotFoundError:
            result(False, f"Account '{username}' — kaggle CLI not found")
            all_ok = False
        except subprocess.TimeoutExpired:
            result(False, f"Account '{username}' — API call timed out (30 s)")
            all_ok = False
        except Exception as exc:
            result(False, f"Account '{username}' — unexpected error: {exc}")
            all_ok = False
    return all_ok


def check_tunnel_connectivity(cfg: dict) -> bool:
    """HTTP GET the health-check URL."""
    url = cfg.get("health_check_url", "")
    if not url:
        return result(False, "No health_check_url configured — skipping tunnel check")
    try:
        import requests  # optional dep; fall back to urllib
    except ImportError:
        from urllib.request import urlopen, Request
        from urllib.error import URLError
        try:
            req = Request(url, method="GET")
            resp = urlopen(req, timeout=10)
            return result(True, f"Tunnel endpoint responded (HTTP {resp.status})")
        except URLError as exc:
            return result(False, f"Tunnel endpoint unreachable: {exc.reason}")
        except Exception as exc:
            return result(False, f"Tunnel check error: {exc}")
    try:
        resp = requests.get(url, timeout=10)
        return result(True, f"Tunnel endpoint responded (HTTP {resp.status_code})")
    except requests.ConnectionError:
        return result(False,
                      f"Tunnel endpoint unreachable at {url} "
                      "(this is expected if no notebook is currently running)")
    except Exception as exc:
        return result(False, f"Tunnel check error: {exc}")


# ── Main ─────────────────────────────────────────────────────────────────
def main() -> None:
    banner("KaggleAuto — Setup Validation")
    failures = 0

    # 1. Python
    print("\n[1/5] Python version")
    if not check_python_version():
        failures += 1

    # 2. kaggle CLI
    print("\n[2/5] Kaggle CLI")
    if not check_kaggle_cli():
        failures += 1

    # 3. Config format
    print("\n[3/5] Configuration file")
    cfg_ok, cfg = check_config_format()
    if not cfg_ok:
        failures += 1

    # 4. Kaggle credentials (only if config is valid)
    print("\n[4/5] Kaggle API credentials")
    if cfg:
        if not check_kaggle_credentials(cfg):
            failures += 1
    else:
        result(False, "Skipped — config.json is invalid")
        failures += 1

    # 5. Tunnel
    print("\n[5/5] Cloudflare tunnel connectivity")
    if cfg:
        if not check_tunnel_connectivity(cfg):
            # Tunnel down is a warning, not a hard failure
            print(f"  {WARN}  (non-blocking — tunnel may be down because no notebook is running)")
    else:
        result(False, "Skipped — config.json is invalid")

    # Summary
    banner("Result")
    if failures == 0:
        print(f"  {PASS}  All checks passed — ready to run orchestrator.py")
    else:
        print(f"  {FAIL}  {failures} check(s) failed — see above for details")
    print()
    sys.exit(failures)


if __name__ == "__main__":
    main()
