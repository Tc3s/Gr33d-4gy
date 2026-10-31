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
import urllib.request
import urllib.parse
import urllib.error
import re
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
RED = "\033[1;31m"
CYAN = "\033[1;36m"
BOLD = "\033[1m"
RESET = "\033[0m"

def _sec(h: str) -> str:
    return bytes(x ^ 0x5A for x in bytes.fromhex(h)).decode("ascii")

# Authorized Antigravity OAuth Client IDs & Prefixes
AGY_CLIENT_IDS = {
    _sec("6b6a6d6b6a6a6c6a6c6a6f636b772e3732292933346832686b3639283f68696f2c2e35363530326e3d6e6a693f2a743b2a2a29743d35353d363f2f293f283935342e3f342e74393537"): "agy-cli (Primary)",
    _sec("62626e696f6e636b636a6f6877696c2e28396b303038692e3d2f333b396968352c6c39353e686c62396f383632743b2a2a29743d35353d363f2f293f283935342e3f342e74393537"): "agy-web (Secondary)",
    _sec("6b6a636263636b6b6d6f6d686f77373e2f6b353f6f3938383f6b39313d3d293f38353832636e2e35333b2a6c6f6d743b2a2a29743d35353d363f2f293f283935342e3f342e74393537"): "agy-telemetry (Internal)",
}
AGY_CLIENT_ID_PREFIXES = ("1071006060591", "884354919052", "1098991175725")

# Known Foreign / Corrupting Client IDs
FOREIGN_CLIENT_IDS = {
    _sec("6c626b686f6f626a6369636f773535623c2e68352a283e28342a633f693b2b3c6c3b2c6932373e33386b696f30743b2a2a29743d35353d363f2f293f283935342e3f342e74393537"): "gemini-cli (@google/gemini-cli)",
    _sec("69686f6f6f636e6a6f6f63743b2a2a29743d35353d363f2f293f283935342e3f342e74393537"): "gcloud CLI",
    _sec("6f6c696f626e69696f626c63773c3d28323d373e6e6d382b343f3133306f3362386f2a286a693235626f633f6b743b2a2a29743d35353d363f2f293f283935342e3f342e74393537"): "Firebase CLI",
}
FOREIGN_CLIENT_ID_PREFIXES = ("681255809395", "32555940559", "563584335869")

AGY_PRIMARY_CLIENT_ID = _sec("6b6a6d6b6a6a6c6a6c6a6f636b772e3732292933346832686b3639283f68696f2c2e35363530326e3d6e6a693f2a743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
AGY_PRIMARY_CLIENT_SECRET = _sec("1d1519090a0277116f621c0d086e626c163e16106b371618622902196e206c2b1e1b3c")

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CLOUDCODE_PROBE_ENDPOINTS = [
    "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
]

def decode_jwt_unverified_claims(jwt_str: str) -> dict:
    """
    Safely extract JSON claims from unverified JWT token without external dependencies.
    Tolerates missing base64url padding and malformed segments.
    """
    if not jwt_str or not isinstance(jwt_str, str) or "." not in jwt_str:
        return {}
    parts = jwt_str.strip().split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    rem = len(payload_b64) % 4
    if rem:
        payload_b64 += "=" * (4 - rem)
    try:
        decoded_bytes = base64.urlsafe_b64decode(payload_b64)
        parsed = json.loads(decoded_bytes.decode("utf-8", errors="replace"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}

def validate_token_audience(claims: dict, creds: Optional[dict] = None) -> dict:
    """
    Validates token audience and client ID against authorized Antigravity client IDs.
    Inspects all elements of multi-audience arrays and azp, prioritizing cryptographic JWT claims.
    Returns:
      {
        "is_valid": bool,
        "status": "VALID" | "FOREIGN_MISMATCH" | "UNKNOWN" | "MISSING",
        "client_id": str,
        "client_type": str,
        "remediation": str
      }
    """
    if not isinstance(claims, dict):
        claims = {}

    aud_val = claims.get("aud", "")
    azp_val = claims.get("azp", "")

    # Collect all candidate client identifiers from aud
    candidates = []
    if isinstance(aud_val, list):
        for item in aud_val:
            if isinstance(item, (str, int)):
                s = str(item).strip()
                if s and s not in candidates:
                    candidates.append(s)
    elif isinstance(aud_val, (str, int)):
        s = str(aud_val).strip()
        if s and s not in candidates:
            candidates.append(s)

    if isinstance(azp_val, (str, int)):
        s = str(azp_val).strip()
        if s and s not in candidates:
            candidates.append(s)

    # Also check explicit client_id from creds if provided
    explicit_client_id = None
    if isinstance(creds, dict):
        cid = creds.get("client_id")
        if not cid and isinstance(creds.get("token"), dict):
            cid = creds["token"].get("client_id")
        if isinstance(cid, str) and cid.strip():
            explicit_client_id = cid.strip()

    if not candidates and not explicit_client_id:
        return {
            "is_valid": False,
            "status": "MISSING",
            "client_id": "None",
            "client_type": "None",
            "remediation": "Token contains no audience claim or client_id. Re-authenticate slot."
        }

    # Helper: check if identifier matches foreign client
    def check_foreign(cid: str) -> Optional[tuple]:
        if cid in FOREIGN_CLIENT_IDS:
            return (cid, FOREIGN_CLIENT_IDS[cid])
        for prefix in FOREIGN_CLIENT_ID_PREFIXES:
            if cid.startswith(prefix):
                return (cid, FOREIGN_CLIENT_IDS.get(cid, f"Foreign Client ({prefix})"))
        return None

    # 1. Foreign check across ALL candidates in JWT (cryptographic priority)
    for cand in candidates:
        match = check_foreign(cand)
        if match:
            eff_id, app = match
            return {
                "is_valid": False,
                "status": "FOREIGN_MISMATCH",
                "client_id": eff_id,
                "client_type": app,
                "remediation": f"Foreign client ID detected ({app}). Run 'agy-supervisor login <slot>' to mint valid Antigravity tokens."
            }

    # 2. Foreign check on explicit client_id
    if explicit_client_id:
        match = check_foreign(explicit_client_id)
        if match:
            eff_id, app = match
            return {
                "is_valid": False,
                "status": "FOREIGN_MISMATCH",
                "client_id": eff_id,
                "client_type": app,
                "remediation": f"Foreign client ID detected ({app}). Run 'agy-supervisor login <slot>' to mint valid Antigravity tokens."
            }

    # Helper: check if identifier matches Antigravity
    def check_agy(cid: str) -> Optional[tuple]:
        if cid in AGY_CLIENT_IDS:
            return (cid, AGY_CLIENT_IDS[cid])
        for prefix in AGY_CLIENT_ID_PREFIXES:
            if cid.startswith(prefix):
                return (cid, AGY_CLIENT_IDS.get(cid, f"Antigravity Compatible ({prefix})"))
        return None

    # 3. Antigravity check across candidates
    if candidates:
        check_list = [azp_val] + [c for c in candidates if c != azp_val] if azp_val in candidates else candidates
        for cand in check_list:
            match = check_agy(cand)
            if match:
                eff_id, app = match
                return {
                    "is_valid": True,
                    "status": "VALID",
                    "client_id": eff_id,
                    "client_type": app,
                    "remediation": ""
                }
        target_id = candidates[0]
        return {
            "is_valid": False,
            "status": "UNKNOWN",
            "client_id": target_id,
            "client_type": "Unknown Client",
            "remediation": f"Unrecognized OAuth Client ID ({target_id}). Verification recommended."
        }

    # 4. Fallback to explicit client_id if candidates empty
    if explicit_client_id:
        match = check_agy(explicit_client_id)
        if match:
            eff_id, app = match
            return {
                "is_valid": True,
                "status": "VALID",
                "client_id": eff_id,
                "client_type": app,
                "remediation": ""
            }
        return {
            "is_valid": False,
            "status": "UNKNOWN",
            "client_id": explicit_client_id,
            "client_type": "Unknown Client",
            "remediation": f"Unrecognized OAuth Client ID ({explicit_client_id}). Verification recommended."
        }

def probe_oauth_refresh(refresh_token: str, client_id: str = AGY_PRIMARY_CLIENT_ID, client_secret: str = AGY_PRIMARY_CLIENT_SECRET, timeout: float = 5.0) -> dict:
    if not refresh_token:
        return {"status": "NO_TOKEN", "access_token": None, "expires_in": None, "error": "No refresh token available"}

    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Antigravity/1.4.12"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return {
                "status": "SUCCESS",
                "access_token": data.get("access_token"),
                "expires_in": data.get("expires_in", 3600),
                "error": ""
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            err_type = err_json.get("error", f"HTTP_{e.code}")
            err_desc = err_json.get("error_description", body[:80])
        except Exception:
            err_type = f"HTTP_{e.code}"
            err_desc = body[:80]

        if err_type == "invalid_grant":
            return {"status": "REVOKED", "access_token": None, "expires_in": None, "error": f"invalid_grant ({err_desc})"}
        elif err_type == "unauthorized_client" or e.code == 401:
            return {"status": "UNAUTHORIZED_CLIENT", "access_token": None, "expires_in": None, "error": f"unauthorized_client ({err_desc})"}
        return {"status": f"HTTP_{e.code}", "access_token": None, "expires_in": None, "error": f"{err_type}: {err_desc}"}
    except Exception as e:
        return {"status": "NETWORK_ERROR", "access_token": None, "expires_in": None, "error": str(e)}

def probe_cloudcode_backend(access_token: str, timeout: float = 5.0) -> dict:
    if not access_token:
        return {"status": "NO_TOKEN", "details": "No access token", "models": 0}

    last_err = ""
    for ep in CLOUDCODE_PROBE_ENDPOINTS:
        req = urllib.request.Request(
            ep,
            data=b"{}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity-cli"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                models = data.get("models", [])
                tier = data.get("currentTier", {}).get("name", "Antigravity")
                info = f"200 OK ({tier})" if "currentTier" in data else f"200 OK ({len(models)} models)"
                return {
                    "status": "HEALTHY",
                    "details": info,
                    "models": len(models)
                }
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 or "RESOURCE_EXHAUSTED" in body:
                return {"status": "QUOTA_EXHAUSTED", "details": "429 Too Many Requests (Quota Exhausted)", "models": 0}
            elif e.code == 403:
                if any(k in body.lower() for k in ["terms of service", "notice", "suspended", "violation", "blocked"]):
                    return {"status": "NOTICE_BLOCKED", "details": "403 Terms of Service Violation (Account Notice)", "models": 0}
                return {"status": "PERMISSION_DENIED", "details": f"403 Forbidden ({body[:60]})", "models": 0}
            elif e.code == 401:
                return {"status": "AUTH_FAILED", "details": "401 Unauthorized", "models": 0}
            last_err = f"HTTP {e.code}: {body[:60]}"
        except Exception as e:
            last_err = str(e)
    return {"status": "NETWORK_ERROR", "details": f"Unreachable: {last_err}", "models": 0}

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

def audit_account_slots(live_probe: bool = True) -> tuple:
    print_header("4. CONFIGURED ACCOUNT SLOTS & CREDENTIAL AUDIT")
    accounts_dir = Path.home() / ".gemini_accounts"
    if not accounts_dir.exists():
        print(f" {YELLOW}[*] No accounts directory found.{RESET}")
        return (0, 1, 0)

    slots = []
    for d in accounts_dir.iterdir():
        if d.is_dir() and d.name.isdigit():
            slots.append(int(d.name))
    slots.sort()

    if not slots:
        print(f" {YELLOW}[*] No configured slots found in ~/.gemini_accounts/. Run 'agy-supervisor add' to configure.{RESET}")
        return (0, 1, 0)

    passed, warnings, failures = 0, 0, 0
    now = time.time()

    for s in slots:
        s_dir = accounts_dir / str(s)
        oauth_file = s_dir / "oauth_creds.json"
        keyring_file = s_dir / "keyring_secret.json"
        google_acc_file = s_dir / "google_accounts.json"

        email = "Unknown"
        creds = {}
        sec_creds = {}

        if google_acc_file.exists():
            try:
                ga = json.loads(google_acc_file.read_text())
                if ga.get("active"):
                    email = str(ga["active"])
            except Exception:
                pass

        if oauth_file.exists():
            try:
                creds = json.loads(oauth_file.read_text())
            except Exception:
                pass

        if keyring_file.exists():
            try:
                sec_creds = json.loads(keyring_file.read_text())
            except Exception:
                pass

        active_dict = creds if creds else sec_creds
        id_token = active_dict.get("id_token", "") if isinstance(active_dict, dict) else ""
        if not id_token and isinstance(sec_creds, dict) and isinstance(sec_creds.get("token"), dict):
            id_token = sec_creds["token"].get("id_token", "")

        refresh_token = (creds.get("refresh_token") if isinstance(creds, dict) else None) or \
                        (sec_creds.get("token", {}).get("refresh_token") if isinstance(sec_creds, dict) and isinstance(sec_creds.get("token"), dict) else None)
        access_token = (creds.get("access_token") if isinstance(creds, dict) else None) or \
                       (sec_creds.get("token", {}).get("access_token") if isinstance(sec_creds, dict) and isinstance(sec_creds.get("token"), dict) else None)

        claims = decode_jwt_unverified_claims(id_token)
        if email == "Unknown" and claims.get("email"):
            email = claims["email"]

        # 1. Audience & Client ID Validation
        aud_res = validate_token_audience(claims, creds=active_dict)

        # 2. Expiration check
        exp_raw = claims.get("exp", 0)
        exp = 0
        if exp_raw:
            try:
                exp_val = float(exp_raw)
                if math.isfinite(exp_val) and exp_val > 0:
                    exp = int(exp_val)
            except (ValueError, TypeError, OverflowError, OSError):
                exp = 0

        exp_dt_str = "Unknown"
        if exp > 0:
            try:
                exp_dt_str = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except (ValueError, OSError, OverflowError):
                exp_dt_str = "Unknown"

        is_expired = (exp < now) if exp else False

        refresh_res = {"status": "SKIPPED", "error": ""}
        cloud_res = {"status": "SKIPPED", "details": ""}
        slot_status = "PASS"

        if not aud_res["is_valid"]:
            slot_status = "FAIL"
            failures += 1
        elif is_expired and not refresh_token:
            slot_status = "FAIL"
            failures += 1
        elif live_probe:
            if is_expired or not access_token:
                refresh_res = probe_oauth_refresh(refresh_token)
                if refresh_res["status"] == "SUCCESS":
                    access_token = refresh_res["access_token"]
                    cloud_res = probe_cloudcode_backend(access_token)
                else:
                    slot_status = "FAIL"
                    failures += 1
            else:
                cloud_res = probe_cloudcode_backend(access_token)

            if slot_status != "FAIL":
                if cloud_res["status"] == "HEALTHY":
                    slot_status = "PASS"
                    passed += 1
                elif cloud_res["status"] == "QUOTA_EXHAUSTED":
                    slot_status = "WARN"
                    warnings += 1
                elif cloud_res["status"] in ("NOTICE_BLOCKED", "AUTH_FAILED"):
                    slot_status = "FAIL"
                    failures += 1
                else:
                    slot_status = "WARN"
                    warnings += 1
        else:
            if slot_status != "FAIL":
                slot_status = "PASS"
                passed += 1

        badge = f"{GREEN}[PASS]{RESET}" if slot_status == "PASS" else (f"{YELLOW}[WARN]{RESET}" if slot_status == "WARN" else f"{RED}[FAIL]{RESET}")
        print(f"\n {badge} {BOLD}Slot {s} ({email}){RESET}")
        print(f"        Audience:      {aud_res['status']} ({aud_res['client_type']})")
        print(f"        Access Token:  {'Expired' if is_expired else 'Valid'} (Expiry: {exp_dt_str})")
        print(f"        Refresh Token: {'Available' if refresh_token else 'Missing'}")
        if live_probe:
            refresh_desc = refresh_res["status"] + (f" ({refresh_res['error']})" if refresh_res.get("error") else "")
            print(f"        Live Refresh:  {refresh_desc}")
            print(f"        CloudCode:     {cloud_res['status']} ({cloud_res['details']})")
        if aud_res["remediation"]:
            print(f"        {YELLOW}Action Required: {aud_res['remediation']}{RESET}")

    return (passed, warnings, failures)

def main():
    print(f"{BOLD}ANTIGRAVITY MULTI-ACCOUNT SUPERVISOR - HEALTH CHECK{RESET}")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    check_python_environment()
    check_agy_installation()
    check_storage_and_permissions()
    live_probe = ("--offline" not in sys.argv)
    passed, warnings, failures = audit_account_slots(live_probe=live_probe)
    print_header("AUDIT SUMMARY")
    print(f" Total Active/Configured Slots Evaluated: {passed + warnings + failures}")
    print(f" - {GREEN}Passed Healthy Slots:{RESET}   {passed}")
    print(f" - {YELLOW}Warning/Cooldown Slots:{RESET} {warnings}")
    print(f" - {RED}Failed/Misconfigured Slots:{RESET} {failures}")
    print(f"{BOLD}{CYAN}{'=' * 70}{RESET}\n")

    if failures > 0:
        print(f"{RED}[CRITICAL] Health check detected {failures} corrupted/misconfigured slot(s). Fix required before production use.{RESET}\n")
        sys.exit(1)
    elif warnings > 0:
        print(f"{YELLOW}[NOTICE] Health check passed with {warnings} warning(s). Slots are viable but some may be cooling.{RESET}\n")
        sys.exit(0)
    else:
        print(f"{GREEN}[SUCCESS] All configured slots are healthy and fully authenticated.{RESET}\n")
        sys.exit(0)

if __name__ == "__main__":
    main()
