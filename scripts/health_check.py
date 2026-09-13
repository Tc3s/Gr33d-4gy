#!/usr/bin/env python3
"""
agy-supervisor System Diagnostic & Health Check Tool
Performs comprehensive auditing of environment, D-Bus session, binary availability,
slot configurations, OAuth token expirations, and secret file permissions.
"""

import sys
import os
import json
import base64
import time
import shutil
from pathlib import Path
from datetime import datetime, timezone

GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
RED = "\033[1;31m"
CYAN = "\033[1;36m"
BOLD = "\033[1m"
RESET = "\033[0m"

def print_header(title: str):
    print(f"\n{BOLD}{CYAN}{'=' * 70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 70}{RESET}")

def report_item(category: str, item: str, status: str, details: str = ""):
    badge = f"{GREEN}[PASS]{RESET}" if status == "PASS" else (f"{YELLOW}[WARN]{RESET}" if status == "WARN" else f"{RED}[FAIL]{RESET}")
    detail_str = f" - {details}" if details else ""
    print(f" {badge} {BOLD}{item:<26}{RESET}{detail_str}")

def check_python_environment():
    print_header("1. PYTHON & RUNTIME ENVIRONMENT")
    ver = sys.version_info
    ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
    if ver.major >= 3 and ver.minor >= 8:
        report_item("Python", "Python Version", "PASS", f"v{ver_str} (>= 3.8 required)")
    else:
        report_item("Python", "Python Version", "FAIL", f"v{ver_str} (Requires Python >= 3.8)")

    # D-Bus module check
    try:
        import dbus
        report_item("D-Bus", "dbus-python module", "PASS", "Installed and loadable")
        # Probe session bus
        try:
            bus = dbus.SessionBus()
            report_item("D-Bus", "SessionBus Connection", "PASS", f"Connected via {os.environ.get('DBUS_SESSION_BUS_ADDRESS', 'default')}")
        except Exception as e:
            report_item("D-Bus", "SessionBus Connection", "WARN", f"Unable to connect: {e} (Headless/SSH mode)")
    except ImportError:
        report_item("D-Bus", "dbus-python module", "WARN", "Not installed (sudo apt install python3-dbus)")

def check_agy_installation():
    print_header("2. ANTIGRAVITY CLI (AGY) BINARY")
    agy_path = shutil.which("agy")
    if agy_path:
        report_item("CLI", "Binary Existence", "PASS", agy_path)
        # Check permissions
        if os.access(agy_path, os.X_OK):
            report_item("CLI", "Execution Permission", "PASS", "Executable")
        else:
            report_item("CLI", "Execution Permission", "FAIL", "Not executable")
    else:
        report_item("CLI", "Binary Existence", "FAIL", "agy not found in PATH")

    # Supervisor binary
    sup_path = shutil.which("agy-supervisor")
    if sup_path:
        report_item("Supervisor", "System Installation", "PASS", sup_path)
    else:
        local_bin = Path.home() / ".local/bin/agy-supervisor"
        if local_bin.exists():
            report_item("Supervisor", "System Installation", "WARN", f"Found at {local_bin} but ~/.local/bin not in PATH")
        else:
            report_item("Supervisor", "System Installation", "FAIL", "agy-supervisor not installed")

def check_storage_and_permissions():
    print_header("3. STORAGE & SECURITY PERMISSIONS")
    accounts_dir = Path.home() / ".gemini_accounts"
    if not accounts_dir.exists():
        report_item("Storage", "Accounts Directory", "WARN", "~/.gemini_accounts does not exist yet")
        return

    st = accounts_dir.stat()
    mode = oct(st.st_mode & 0o777)
    if (st.st_mode & 0o077) == 0:
        report_item("Storage", "Directory Permissions", "PASS", f"{accounts_dir} ({mode})")
    else:
        report_item("Storage", "Directory Permissions", "WARN", f"{accounts_dir} ({mode} - should be 0700)")

    state_file = accounts_dir / "supervisor_state.json"
    if state_file.exists():
        try:
            with open(state_file, "r") as f:
                state_data = json.load(f)
            cur = state_data.get("current_slot", 1)
            slots_count = len(state_data.get("slots", {}))
            report_item("State", "supervisor_state.json", "PASS", f"Active Slot: {cur}, Tracked Slots: {slots_count}")
        except Exception as e:
            report_item("State", "supervisor_state.json", "FAIL", f"Corrupt JSON: {e}")
    else:
        report_item("State", "supervisor_state.json", "PASS", "Not yet initialized (will be created on first run)")

def audit_account_slots():
    print_header("4. CONFIGURED ACCOUNT SLOTS & CREDENTIAL AUDIT")
    accounts_dir = Path.home() / ".gemini_accounts"
    if not accounts_dir.exists():
        print(f" {YELLOW}[*] No accounts directory found.{RESET}")
        return

    slots = []
    for d in accounts_dir.iterdir():
        if d.is_dir() and d.name.isdigit():
            slots.append(int(d.name))
    slots.sort()

    if not slots:
        print(f" {YELLOW}[*] No configured slots found in ~/.gemini_accounts/. Run 'agy-supervisor add' to configure.{RESET}")
        return

    now = time.time()
    for s in slots:
        s_dir = accounts_dir / str(s)
        oauth_file = s_dir / "oauth_creds.json"
        keyring_file = s_dir / "keyring_secret.json"

        email = "Unknown"
        token_status = "NOT_FOUND"
        exp_info = "-"

        if oauth_file.exists():
            # Check permission
            st = oauth_file.stat()
            mode = oct(st.st_mode & 0o777)
            perm_ok = (st.st_mode & 0o077) == 0

            try:
                with open(oauth_file, "r") as f:
                    creds = json.load(f)
                id_token = creds.get("id_token", "")
                if "." in id_token:
                    parts = id_token.split(".")
                    payload = parts[1]
                    rem = len(payload) % 4
                    if rem:
                        payload += "=" * (4 - rem)
                    claims = json.loads(base64.urlsafe_b64decode(payload))
                    email = claims.get("email", "Unknown")
                    exp = claims.get("exp", 0)
                    if exp:
                        exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        if exp < now:
                            exp_info = f"EXPIRED ({exp_dt})"
                            token_status = "EXPIRED"
                        else:
                            rem_mins = int((exp - now) // 60)
                            exp_info = f"Valid for {rem_mins}m ({exp_dt})"
                            token_status = "ACTIVE"
                elif creds.get("refresh_token"):
                    token_status = "ACTIVE (Refresh token present)"
            except Exception as e:
                token_status = f"ERROR ({e})"
        elif keyring_file.exists():
            token_status = "KEYRING_ONLY"

        has_keyring = "Yes" if keyring_file.exists() else "No"
        status_flag = "PASS" if token_status in ["ACTIVE", "KEYRING_ONLY"] or "Refresh token" in token_status else "WARN"
        report_item(f"Slot {s}", f"Slot {s} ({email})", status_flag, f"Status: {token_status} | Keyring: {has_keyring} | {exp_info}")

def main():
    print(f"{BOLD}ANTIGRAVITY MULTI-ACCOUNT SUPERVISOR - HEALTH CHECK{RESET}")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    check_python_environment()
    check_agy_installation()
    check_storage_and_permissions()
    audit_account_slots()
    print_header("AUDIT SUMMARY")
    print(" Run 'python3 tests/test_supervisor.py' to execute the regression test suite.")
    print(" Run 'agy-supervisor status' to inspect live cooldown and quota status.")
    print(f"{BOLD}{CYAN}{'=' * 70}{RESET}\n")

if __name__ == "__main__":
    main()
