#!/usr/bin/env python3
"""
ADVERSARIAL CHALLENGER HARNESS: MILESTONE 3 (Multi-Slot Isolation & CloudCode Probing)
Agent: m3_challenger_2 (Milestone 3 Multi-Slot Probe Challenger 2)

Adversarial Objectives:
1. Multi-Slot State Pollution Probe:
   - Run SlotHealthProber.probe_slot() on simulated passive slot while an active session is running.
   - Assert that active ~/.gemini/ files (oauth_creds.json, google_accounts.json, antigravity-cli/) are untouched.
   - Assert that GNOME Keyring credentials for the active slot remain unmodified.
2. Live Probe Edge Case Harness:
   - Mock backend returns: HTTP 429 with malformed resetTime, HTTP 403 with Terms of Service notice text,
     HTTP 500, dropped TCP connection, slow response (socket timeout).
   - Verify how SlotHealthProber maps each case to SlotStatusType.
3. Verify AccountManager.sync_probe_result() accurately updates supervisor_state.json without corruption.
"""

import sys
import os
import time
import socket
import json
import base64
import tempfile
import shutil
import datetime
import http.client
import urllib.error
import urllib.request
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock
from importlib.machinery import SourceFileLoader

REPO_ROOT = Path(__file__).resolve().parent.parent
LOADER = SourceFileLoader("supervisor", str(REPO_ROOT / "bin" / "agy-supervisor"))
mod = LOADER.load_module()

SlotHealthProber = mod.SlotHealthProber
SlotStatusType = mod.SlotStatusType
SlotProbeResult = mod.SlotProbeResult
AudienceValidator = mod.AudienceValidator
ClientIDStatus = mod.ClientIDStatus
AccountManager = mod.AccountManager

def make_mock_jwt(aud: str, email: str = "test@example.com", exp: int = 1900000000, azp: str = None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
    claims = {"iss": "https://accounts.google.com", "aud": aud, "email": email, "exp": exp}
    if azp:
        claims["azp"] = azp
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"sig").decode().rstrip("=")
    return f"{header}.{payload}.{sig}"

class AdversarialLedger:
    def __init__(self):
        self.passes = 0
        self.failures = 0
        self.findings = []
        self.records = []

    def record_pass(self, test_id: str, description: str, evidence: str):
        self.passes += 1
        item = {"status": "PASS", "id": test_id, "desc": description, "evidence": evidence}
        self.records.append(item)
        print(f"  [PASS] {test_id}: {description}")
        print(f"         Evidence: {evidence}")

    def record_fail(self, test_id: str, description: str, evidence: str, severity: str = "HIGH"):
        self.failures += 1
        item = {"status": "FAIL", "id": test_id, "desc": description, "evidence": evidence, "severity": severity}
        self.records.append(item)
        self.findings.append(item)
        print(f"  [FAIL] ({severity}) {test_id}: {description}")
        print(f"         Evidence: {evidence}")

ledger = AdversarialLedger()

# ==============================================================================
# SUITE 1: MULTI-SLOT PROBE ISOLATION & STATE POLLUTION HARNESS
# ==============================================================================
print("\n" + "=" * 75)
print(" SUITE 1: MULTI-SLOT PROBE ISOLATION & CREDENTIAL POLLUTION AUDIT")
print("=" * 75)

def suite1_test_passive_probe_isolation():
    temp_dir = tempfile.TemporaryDirectory()
    try:
        acc_dir = Path(temp_dir.name) / "accounts"
        acc_dir.mkdir(parents=True, exist_ok=True)
        gem_dir = Path(temp_dir.name) / "gemini"
        gem_dir.mkdir(parents=True, exist_ok=True)
        cli_dir = gem_dir / "antigravity-cli"
        cli_dir.mkdir(parents=True, exist_ok=True)
        conv_dir = cli_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)

        orig_acc = mod.ACCOUNTS_DIR
        orig_gem = mod.GEMINI_DIR
        orig_cli = mod.CLI_DIR
        orig_state = mod.STATE_FILE

        mod.ACCOUNTS_DIR = acc_dir
        mod.GEMINI_DIR = gem_dir
        mod.CLI_DIR = cli_dir
        mod.STATE_FILE = acc_dir / "supervisor_state.json"

        # Active Slot 1 Setup
        s1 = acc_dir / "1"
        s1.mkdir(parents=True, exist_ok=True)
        s1_oauth_content = {
            "access_token": "ya29.active_slot_1_original_token",
            "refresh_token": "1//refresh_slot_1",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="active1@gmail.com")
        }
        (s1 / "oauth_creds.json").write_text(json.dumps(s1_oauth_content, indent=2))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "active1@gmail.com"}))
        (s1 / "keyring_secret.json").write_text(json.dumps({
            "token": {"access_token": "ya29.active_slot_1_original_token", "refresh_token": "1//refresh_slot_1"},
            "auth_method": "consumer",
            "id_token_email": "active1@gmail.com"
        }))

        # Active ~/.gemini mirror
        active_oauth_file = gem_dir / "oauth_creds.json"
        active_oauth_file.write_text(json.dumps(s1_oauth_content, indent=2))
        active_ga_file = gem_dir / "google_accounts.json"
        active_ga_file.write_text(json.dumps({"active": "active1@gmail.com"}))

        # Dummy active conversation
        dummy_conv = conv_dir / "active_conv.json"
        dummy_conv.write_text(json.dumps({"conversation_id": "active-uuid-1111", "slot": 1}))
        conv_stat_before = dummy_conv.stat().st_mtime_ns

        # Passive Slot 2 Setup (Expired Token -> Forces OAuth refresh)
        s2 = acc_dir / "2"
        s2.mkdir(parents=True, exist_ok=True)
        s2_oauth_content = {
            "access_token": "ya29.passive_slot_2_stale_token",
            "refresh_token": "1//refresh_slot_2",
            "expiry_date": 1000, # expired in 1970
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="passive2@gmail.com")
        }
        (s2 / "oauth_creds.json").write_text(json.dumps(s2_oauth_content, indent=2))
        (s2 / "google_accounts.json").write_text(json.dumps({"active": "passive2@gmail.com"}))
        (s2 / "keyring_secret.json").write_text(json.dumps({
            "token": {"access_token": "ya29.passive_slot_2_stale_token", "refresh_token": "1//refresh_slot_2"},
            "auth_method": "consumer",
            "id_token_email": "passive2@gmail.com"
        }))

        # Supervisor State: current_slot = 1
        mgr = AccountManager()
        mgr.state["current_slot"] = 1
        mgr.save_state()

        prober = SlotHealthProber(acc_dir, is_active_slot_fn=lambda s: str(s) == "1")

        # Mock Network: OAuth refresh succeeds for Slot 2, CloudCode succeeds
        new_slot2_token = "ya29.refreshed_passive_token_2222"
        mock_oauth_resp = MagicMock()
        mock_oauth_resp.read.return_value = json.dumps({
            "access_token": new_slot2_token,
            "expires_in": 3600,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="passive2@gmail.com")
        }).encode("utf-8")
        mock_oauth_resp.__enter__.return_value = mock_oauth_resp

        mock_lca_resp = MagicMock()
        mock_lca_resp.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode("utf-8")
        mock_lca_resp.__enter__.return_value = mock_lca_resp

        mock_quota_resp = MagicMock()
        mock_quota_resp.read.return_value = json.dumps({
            "buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 1.0}]
        }).encode("utf-8")
        mock_quota_resp.__enter__.return_value = mock_quota_resp

        with patch("urllib.request.urlopen", side_effect=[mock_oauth_resp, mock_lca_resp, mock_quota_resp]), \
             patch.object(mod, "set_keyring_secret") as mock_set_keyring:

            res = prober.probe_slot(2, live_probe=True)

            # Verification 1: Passive Slot 2 credentials updated on disk
            s2_after = json.loads((s2 / "oauth_creds.json").read_text())
            s2_kd_after = json.loads((s2 / "keyring_secret.json").read_text())
            slot2_refreshed = (s2_after.get("access_token") == new_slot2_token and 
                               s2_kd_after.get("token", {}).get("access_token") == new_slot2_token)

            if slot2_refreshed:
                ledger.record_pass("ISO-01", "Passive slot 2 credentials updated in isolated slot directory",
                                   f"s2 access_token={s2_after.get('access_token')[:15]}...")
            else:
                ledger.record_fail("ISO-01", "Passive slot 2 credentials were NOT updated in slot directory",
                                   str(s2_after))

            # Verification 2: Active ~/.gemini/oauth_creds.json UNTOUCHED
            gem_oauth_after = json.loads(active_oauth_file.read_text())
            active_untouched = (gem_oauth_after.get("access_token") == "ya29.active_slot_1_original_token")
            if active_untouched:
                ledger.record_pass("ISO-02", "Active ~/.gemini/oauth_creds.json strictly unmodified by passive probe",
                                   f"active token={gem_oauth_after.get('access_token')[:20]}...")
            else:
                ledger.record_fail("ISO-02", "Active ~/.gemini/oauth_creds.json POLLUTED by passive probe",
                                   f"active token mutated to: {gem_oauth_after.get('access_token')}", severity="CRITICAL")

            # Verification 3: GNOME Keyring set_keyring_secret was NEVER called
            if mock_set_keyring.call_count == 0:
                ledger.record_pass("ISO-03", "GNOME Keyring set_keyring_secret was NOT invoked for passive slot",
                                   f"call_count={mock_set_keyring.call_count}")
            else:
                ledger.record_fail("ISO-03", "GNOME Keyring was MUTATED during passive slot probe",
                                   f"call_count={mock_set_keyring.call_count}", severity="CRITICAL")

            # Verification 4: Active conversation files untouched
            conv_stat_after = dummy_conv.stat().st_mtime_ns
            if conv_stat_before == conv_stat_after:
                ledger.record_pass("ISO-04", "Active conversations and antigravity-cli directory untouched",
                                   f"mtime_before={conv_stat_before}, mtime_after={conv_stat_after}")
            else:
                ledger.record_fail("ISO-04", "Active conversation directory mutated during passive probe",
                                   f"mtime changed from {conv_stat_before} to {conv_stat_after}")

            # Verification 5: supervisor_state current_slot preserved
            st_after = json.loads(mod.STATE_FILE.read_text())
            if str(st_after.get("current_slot")) == "1":
                ledger.record_pass("ISO-05", "supervisor_state.json current_slot preserved as 1",
                                   f"current_slot={st_after.get('current_slot')}")
            else:
                ledger.record_fail("ISO-05", "supervisor_state.json current_slot altered",
                                   f"current_slot={st_after.get('current_slot')}", severity="CRITICAL")

        # Now test Active Slot Probing: Probing Slot 1 SHOULD update ~/.gemini and Keyring
        s1_refresh_token = "ya29.refreshed_active_token_1111"
        mock_oauth_resp_active = MagicMock()
        mock_oauth_resp_active.read.return_value = json.dumps({
            "access_token": s1_refresh_token,
            "expires_in": 3600,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="active1@gmail.com")
        }).encode("utf-8")
        mock_oauth_resp_active.__enter__.return_value = mock_oauth_resp_active

        # Expire slot 1 token
        s1_kd = json.loads((s1 / "keyring_secret.json").read_text())
        s1_kd["token"]["expiry"] = "2000-01-01T00:00:00Z"
        (s1 / "keyring_secret.json").write_text(json.dumps(s1_kd))

        with patch("urllib.request.urlopen", side_effect=[mock_oauth_resp_active, mock_lca_resp, mock_quota_resp]), \
             patch.object(mod, "set_keyring_secret") as mock_set_keyring_active:

            res_active = prober.probe_slot(1, live_probe=True)
            active_gem_after = json.loads(active_oauth_file.read_text())
            if active_gem_after.get("access_token") == s1_refresh_token and mock_set_keyring_active.call_count == 1:
                ledger.record_pass("ISO-06", "Active slot probe correctly synchronizes refreshed token to ~/.gemini and Keyring",
                                   f"active_token={active_gem_after.get('access_token')[:20]}, keyring_calls={mock_set_keyring_active.call_count}")
            else:
                ledger.record_fail("ISO-06", "Active slot probe failed to synchronize active ~/.gemini or Keyring",
                                   f"token={active_gem_after.get('access_token')}, keyring_calls={mock_set_keyring_active.call_count}")

    finally:
        mod.ACCOUNTS_DIR = orig_acc
        mod.GEMINI_DIR = orig_gem
        mod.CLI_DIR = orig_cli
        mod.STATE_FILE = orig_state
        temp_dir.cleanup()

suite1_test_passive_probe_isolation()

# ==============================================================================
# SUITE 2: MOCK CLOUDCODE BACKEND EDGE CASES HARNESS
# ==============================================================================
print("\n" + "=" * 75)
print(" SUITE 2: MOCK CLOUDCODE BACKEND EDGE CASES & EXCEPTION SAFETY AUDIT")
print("=" * 75)

def suite2_test_cloudcode_edge_cases():
    temp_dir = tempfile.TemporaryDirectory()
    try:
        acc_dir = Path(temp_dir.name) / "accounts"
        acc_dir.mkdir(parents=True, exist_ok=True)
        orig_acc = mod.ACCOUNTS_DIR
        mod.ACCOUNTS_DIR = acc_dir

        # Setup healthy slot with non-expired token
        s1 = acc_dir / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.valid_token_test",
            "refresh_token": "1//refresh_test",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="slot1@gmail.com")
        }))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "slot1@gmail.com"}))

        prober = SlotHealthProber(acc_dir)

        # Helper mock response
        def make_mock_resp(body_dict):
            m = MagicMock()
            m.read.return_value = json.dumps(body_dict).encode("utf-8")
            m.__enter__.return_value = m
            return m

        lca_200 = make_mock_resp({"currentTier": {"name": "Antigravity"}})

        # ----------------------------------------------------------------------
        # Test 2.1: Quota 429 with Malformed resetTime string (Non-ISO string)
        # ----------------------------------------------------------------------
        quota_malformed_reset = make_mock_resp({
            "buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.0, "resetTime": "MALFORMED_NON_ISO_STRING"}]
        })
        with patch("urllib.request.urlopen", side_effect=[lca_200, quota_malformed_reset]):
            res = prober.probe_slot(1, live_probe=True)
            if res.status == SlotStatusType.QUOTA_EXHAUSTED and res.reset_seconds == 3600:
                ledger.record_pass("CC-01", "Malformed resetTime string gracefully falls back to default 3600s cooldown",
                                   f"status={res.status.value}, reset_seconds={res.reset_seconds}")
            else:
                ledger.record_fail("CC-01", "Malformed resetTime string handled unexpectedly",
                                   f"status={res.status}, reset_seconds={res.reset_seconds}")

        # ----------------------------------------------------------------------
        # Test 2.2: Quota 429 with resetTime as Non-String (e.g. integer or None)
        # ----------------------------------------------------------------------
        quota_int_reset = make_mock_resp({
            "buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.0, "resetTime": 12345678}]
        })
        with patch("urllib.request.urlopen", side_effect=[lca_200, quota_int_reset]):
            try:
                res = prober.probe_slot(1, live_probe=True)
                if res.status == SlotStatusType.QUOTA_EXHAUSTED and res.reset_seconds == 3600:
                    ledger.record_pass("CC-02", "Integer resetTime safely handled without crash, falls back to 3600s",
                                       f"status={res.status.value}, reset_seconds={res.reset_seconds}")
                else:
                    ledger.record_fail("CC-02", "Integer resetTime produced unexpected result",
                                       f"status={res.status}, reset_seconds={res.reset_seconds}")
            except Exception as e:
                ledger.record_fail("CC-02", "Integer resetTime crashed SlotHealthProber", str(e), severity="HIGH")

        # ----------------------------------------------------------------------
        # Test 2.3: Quota 429 with resetTime IN THE PAST (e.g. 2020-01-01)
        # ----------------------------------------------------------------------
        quota_past_reset = make_mock_resp({
            "buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.0, "resetTime": "2020-01-01T00:00:00Z"}]
        })
        with patch("urllib.request.urlopen", side_effect=[lca_200, quota_past_reset]):
            res = prober.probe_slot(1, live_probe=True)
            # Notice: epoch is 1577836800. max_reset_ts is 1577836800.
            # rem_sec = max(0, 1577836800 - now) -> 0.
            # reset_timestamp = 1577836800.
            if res.reset_seconds == 0 and res.reset_timestamp == 1577836800:
                ledger.record_fail("CC-03", "Quota resetTime in past leaves reset_timestamp in past and reset_seconds=0",
                                   f"reset_timestamp={res.reset_timestamp} (2020-01-01), reset_seconds={res.reset_seconds}. Causes immediate un-exhaustion in supervisor!",
                                   severity="HIGH")
            else:
                ledger.record_pass("CC-03", "Quota resetTime in past properly guarded with positive cooldown",
                                   f"reset_seconds={res.reset_seconds}, reset_timestamp={res.reset_timestamp}")

        # ----------------------------------------------------------------------
        # Test 2.4: Terms of Service Suspensions & Account Notices on HTTP 403 / 400
        # ----------------------------------------------------------------------
        tos_cases = [
            (403, '{"error": {"code": 403, "message": "Terms of Service violation"}}', "Google ToS Violation"),
            (403, '{"error": {"code": 403, "message": "Account suspended by administrator"}}', "Account Suspended"),
            (400, '{"error": {"code": 400, "message": "Account blocked: user terms update required"}}', "Terms Update Required"),
            (403, '{"error": {"code": 403, "message": "PERMISSION_DENIED: Access forbidden"}}', "Permission Denied")
        ]
        all_tos_passed = True
        for code, body_str, label in tos_cases:
            http_err = urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", code, "Err", {},
                                              MagicMock(read=lambda b=body_str: b.encode("utf-8")))
            with patch("urllib.request.urlopen", side_effect=http_err):
                res = prober.probe_slot(1, live_probe=True)
                if res.status != SlotStatusType.NOTICE_SUSPENDED:
                    all_tos_passed = False
                    ledger.record_fail(f"CC-04-{label}", f"Expected NOTICE_SUSPENDED for {label}, got {res.status}",
                                       f"code={code}, body={body_str}")
        if all_tos_passed:
            ledger.record_pass("CC-04", "All 4 Terms of Service and Account Notice 403/400 patterns mapped to NOTICE_SUSPENDED",
                               "4/4 cases successfully classified as NOTICE_SUSPENDED")

        # ----------------------------------------------------------------------
        # Test 2.5: Backend Server Errors (HTTP 500 / 502 / 503 / 504)
        # ----------------------------------------------------------------------
        # Case A: loadCodeAssist returns HTTP 500 AND retrieveUserQuota returns HTTP 500
        http_500_err = urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", 500, "Internal Server Error", {},
                                              MagicMock(read=lambda: b'{"error": "Internal Server Error"}'))
        with patch("urllib.request.urlopen", side_effect=http_500_err):
            res = prober.probe_slot(1, live_probe=True)
            if res.status in (SlotStatusType.NETWORK_ERROR, "ERROR"):
                ledger.record_pass("CC-05A", "Backend HTTP 500 on both endpoints mapped to NETWORK_ERROR",
                                   f"status={res.status.value}")
            else:
                ledger.record_fail("CC-05A", "Backend HTTP 500 silently ignored and falsely reported as HEALTHY!",
                                   f"Actual returned status: {res.status.value}, details={res.details}", severity="CRITICAL")

        # Case B: loadCodeAssist returns 200 OK, but retrieveUserQuota returns HTTP 500
        with patch("urllib.request.urlopen", side_effect=[lca_200, http_500_err]):
            res = prober.probe_slot(1, live_probe=True)
            if res.status in (SlotStatusType.NETWORK_ERROR, "ERROR"):
                ledger.record_pass("CC-05B", "retrieveUserQuota HTTP 500 mapped to error status",
                                   f"status={res.status.value}")
            else:
                ledger.record_fail("CC-05B", "retrieveUserQuota HTTP 500 silently ignored and slot marked HEALTHY 100%!",
                                   f"Actual returned status: {res.status.value}, details={res.details}", severity="CRITICAL")

        # Case C: OAuth Token Refresh returns HTTP 500
        http_500_oauth = urllib.error.HTTPError("https://oauth2.googleapis.com/token", 500, "Internal Server Error", {},
                                                MagicMock(read=lambda: b'{"error": "backend_failure"}'))
        # Expire slot token to force refresh
        s1_kd = {"token": {"refresh_token": "1//refresh_test", "access_token": "ya29.old", "expiry": "1970-01-01T00:00:00Z"}, "id_token_email": "slot1@gmail.com"}
        (s1 / "keyring_secret.json").write_text(json.dumps(s1_kd))
        (s1 / "oauth_creds.json").write_text(json.dumps({"refresh_token": "1//refresh_test", "access_token": "ya29.old", "expiry_date": 1000}))

        with patch("urllib.request.urlopen", side_effect=http_500_oauth):
            res = prober.probe_slot(1, live_probe=True)
            if res.status == SlotStatusType.NETWORK_ERROR:
                ledger.record_pass("CC-05C", "OAuth HTTP 500 mapped to temporary NETWORK_ERROR",
                                   f"status={res.status.value}")
            else:
                ledger.record_fail("CC-05C", "OAuth HTTP 500 falsely classified as permanent TOKEN_REVOKED (10-yr lockout)!",
                                   f"Actual status: {res.status.value}, details={res.details}", severity="HIGH")

        # Restore non-expired token for next tests
        (s1 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.valid_token_test",
            "refresh_token": "1//refresh_test",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="slot1@gmail.com")
        }))
        (s1 / "keyring_secret.json").write_text(json.dumps({
            "token": {"access_token": "ya29.valid_token_test", "refresh_token": "1//refresh_test", "expiry": "2030-01-01T00:00:00Z"},
            "id_token_email": "slot1@gmail.com"
        }))

        # ----------------------------------------------------------------------
        # Test 2.6: Network Timeouts & Dropped TCP Connections
        # ----------------------------------------------------------------------
        # Case A: loadCodeAssist socket timeout
        timeout_err = urllib.error.URLError(socket.timeout("The read operation timed out"))
        with patch("urllib.request.urlopen", side_effect=timeout_err):
            res = prober.probe_slot(1, live_probe=True)
            if res.status == SlotStatusType.NETWORK_ERROR:
                ledger.record_pass("CC-06A", "loadCodeAssist socket timeout correctly returns NETWORK_ERROR",
                                   f"status={res.status.value}, details={res.details}")
            else:
                ledger.record_fail("CC-06A", "loadCodeAssist socket timeout not mapped to NETWORK_ERROR",
                                   f"status={res.status.value}")

        # Case B: retrieveUserQuota socket timeout
        with patch("urllib.request.urlopen", side_effect=[lca_200, timeout_err]):
            res = prober.probe_slot(1, live_probe=True)
            if res.status == SlotStatusType.NETWORK_ERROR:
                ledger.record_pass("CC-06B", "retrieveUserQuota socket timeout correctly returns NETWORK_ERROR",
                                   f"status={res.status.value}")
            else:
                ledger.record_fail("CC-06B", "retrieveUserQuota socket timeout swallowed and reported as HEALTHY!",
                                   f"Actual returned status: {res.status.value}, details={res.details}", severity="CRITICAL")

        # Case C: retrieveUserQuota dropped TCP connection (http.client.RemoteDisconnected)
        remote_disconnect_err = http.client.RemoteDisconnected("Remote end closed connection without response")
        with patch("urllib.request.urlopen", side_effect=[lca_200, remote_disconnect_err]):
            res = prober.probe_slot(1, live_probe=True)
            if res.status == SlotStatusType.NETWORK_ERROR:
                ledger.record_pass("CC-06C", "retrieveUserQuota TCP drop correctly returns NETWORK_ERROR",
                                   f"status={res.status.value}")
            else:
                ledger.record_fail("CC-06C", "retrieveUserQuota dropped TCP swallowed and reported as HEALTHY!",
                                   f"Actual returned status: {res.status.value}, details={res.details}", severity="CRITICAL")

        # ----------------------------------------------------------------------
        # Test 2.7: Local Corrupted Credential Files (Invalid JSON)
        # ----------------------------------------------------------------------
        # Case A: Corrupted oauth_creds.json ({bad_json)
        (s1 / "oauth_creds.json").write_text("{bad json: true, corrupted")
        try:
            res = prober.probe_slot(1, live_probe=True)
            if res.status in (SlotStatusType.CLIENT_ID_MISMATCH, SlotStatusType.TOKEN_REVOKED):
                ledger.record_pass("CC-07A", "Corrupt oauth_creds.json handled without crash",
                                   f"status={res.status.value}")
            else:
                ledger.record_fail("CC-07A", "Corrupt oauth_creds.json returned unexpected status",
                                   f"status={res.status.value}")
        except Exception as e:
            ledger.record_fail("CC-07A", "Corrupt oauth_creds.json CRASHED SlotHealthProber with unhandled exception!",
                               f"{type(e).__name__}: {e}", severity="CRITICAL")

        # Case B: Corrupt keyring_secret.json
        (s1 / "oauth_creds.json").unlink(missing_ok=True)
        (s1 / "keyring_secret.json").write_text("{invalid keyring json")
        try:
            res = prober.probe_slot(1, live_probe=True)
            ledger.record_pass("CC-07B", "Corrupt keyring_secret.json handled without crash",
                               f"status={res.status.value}")
        except Exception as e:
            ledger.record_fail("CC-07B", "Corrupt keyring_secret.json CRASHED SlotHealthProber!",
                               f"{type(e).__name__}: {e}", severity="CRITICAL")

    finally:
        mod.ACCOUNTS_DIR = orig_acc
        temp_dir.cleanup()

suite2_test_cloudcode_edge_cases()

# ==============================================================================
# SUITE 3: ACCOUNTMANAGER.SYNC_PROBE_RESULT() STATE INTEGRITY HARNESS
# ==============================================================================
print("\n" + "=" * 75)
print(" SUITE 3: ACCOUNTMANAGER.SYNC_PROBE_RESULT() STATE INTEGRITY AUDIT")
print("=" * 75)

def suite3_test_sync_probe_result_integrity():
    temp_dir = tempfile.TemporaryDirectory()
    try:
        acc_dir = Path(temp_dir.name) / "accounts"
        acc_dir.mkdir(parents=True, exist_ok=True)
        orig_acc = mod.ACCOUNTS_DIR
        orig_state = mod.STATE_FILE
        mod.ACCOUNTS_DIR = acc_dir
        mod.STATE_FILE = acc_dir / "supervisor_state.json"

        mgr = AccountManager()
        now = int(time.time())

        # Test 3.1: All SlotStatusType transitions
        # HEALTHY
        res_healthy = SlotProbeResult(slot=1, email="slot1@gmail.com", status=SlotStatusType.HEALTHY)
        mgr.sync_probe_result(res_healthy)
        s1_st = mgr.state["slots"]["1"]
        if s1_st.get("status") == "ACTIVE" and s1_st.get("exhausted_until") == 0:
            ledger.record_pass("SYNC-01", "HEALTHY result sets status=ACTIVE and exhausted_until=0",
                               f"s1_state={s1_st}")
        else:
            ledger.record_fail("SYNC-01", "HEALTHY transition failed", str(s1_st))

        # QUOTA_EXHAUSTED
        target_reset = now + 4500
        res_quota = SlotProbeResult(slot=1, email="slot1@gmail.com", status=SlotStatusType.QUOTA_EXHAUSTED,
                                    reset_seconds=4500, reset_timestamp=target_reset, details="Quota limit hit")
        mgr.sync_probe_result(res_quota)
        s1_st = mgr.state["slots"]["1"]
        if s1_st.get("status") == "EXHAUSTED_DAILY" and s1_st.get("exhausted_until") == target_reset:
            ledger.record_pass("SYNC-02", "QUOTA_EXHAUSTED sets status=EXHAUSTED_DAILY and exact reset_timestamp",
                               f"status={s1_st.get('status')}, exhausted_until={s1_st.get('exhausted_until')}")
        else:
            ledger.record_fail("SYNC-02", "QUOTA_EXHAUSTED transition failed", str(s1_st))

        # NOTICE_SUSPENDED
        res_notice = SlotProbeResult(slot=2, email="slot2@gmail.com", status=SlotStatusType.NOTICE_SUSPENDED,
                                     notice_reason="ToS violation detected")
        mgr.sync_probe_result(res_notice)
        s2_st = mgr.state["slots"]["2"]
        if s2_st.get("status") == "NOTICE_BLOCKED" and s2_st.get("exhausted_until") > now + 365 * 86400:
            ledger.record_pass("SYNC-03", "NOTICE_SUSPENDED sets status=NOTICE_BLOCKED with indefinite lockout",
                               f"status={s2_st.get('status')}, exhausted_until={s2_st.get('exhausted_until')}")
        else:
            ledger.record_fail("SYNC-03", "NOTICE_SUSPENDED transition failed", str(s2_st))

        # CLIENT_ID_MISMATCH
        res_mismatch = SlotProbeResult(slot=3, email="slot3@gmail.com", status=SlotStatusType.CLIENT_ID_MISMATCH,
                                       details="Token minted for gemini-cli")
        mgr.sync_probe_result(res_mismatch)
        s3_st = mgr.state["slots"]["3"]
        if s3_st.get("status") == "CLIENT_ID_MISMATCH" and s3_st.get("exhausted_until") > now + 365 * 86400:
            ledger.record_pass("SYNC-04", "CLIENT_ID_MISMATCH sets status=CLIENT_ID_MISMATCH with indefinite lockout",
                               f"status={s3_st.get('status')}, exhausted_until={s3_st.get('exhausted_until')}")
        else:
            ledger.record_fail("SYNC-04", "CLIENT_ID_MISMATCH transition failed", str(s3_st))

        # TOKEN_REVOKED
        res_revoked = SlotProbeResult(slot=4, email="slot4@gmail.com", status=SlotStatusType.TOKEN_REVOKED,
                                      details="Token invalid_grant")
        mgr.sync_probe_result(res_revoked)
        s4_st = mgr.state["slots"]["4"]
        if s4_st.get("status") == "AUTH_FAILED" and s4_st.get("exhausted_until") > now + 365 * 86400:
            ledger.record_pass("SYNC-05", "TOKEN_REVOKED sets status=AUTH_FAILED with indefinite lockout",
                               f"status={s4_st.get('status')}, exhausted_until={s4_st.get('exhausted_until')}")
        else:
            ledger.record_fail("SYNC-05", "TOKEN_REVOKED transition failed", str(s4_st))

        # NETWORK_ERROR: Does NOT mutate slot status
        res_net = SlotProbeResult(slot=1, email="slot1@gmail.com", status=SlotStatusType.NETWORK_ERROR,
                                  details="Connection timeout")
        # Save previous state
        prev_s1_status = mgr.state["slots"]["1"]["status"]
        mgr.sync_probe_result(res_net)
        curr_s1_status = mgr.state["slots"]["1"]["status"]
        if curr_s1_status == prev_s1_status:
            ledger.record_pass("SYNC-06", "NETWORK_ERROR safely preserves previous slot status without false lockouts",
                               f"status preserved as: {curr_s1_status}")
        else:
            ledger.record_fail("SYNC-06", "NETWORK_ERROR unexpectedly mutated slot status",
                               f"status changed from {prev_s1_status} to {curr_s1_status}")

        # Test 3.2: Atomic file write persistence (200 rapid sequential syncs)
        file_valid = True
        for i in range(200):
            r = SlotProbeResult(slot=(i % 5) + 1, email=f"user{(i % 5) + 1}@gmail.com", status=SlotStatusType.HEALTHY)
            mgr.sync_probe_result(r)
            try:
                content = json.loads(mod.STATE_FILE.read_text())
                if "slots" not in content:
                    file_valid = False
                    break
            except Exception:
                file_valid = False
                break
        if file_valid:
            ledger.record_pass("SYNC-07", "200 rapid sequential state syncs maintained 100% valid JSON and file integrity",
                               "File readable and valid after 200 atomic updates")
        else:
            ledger.record_fail("SYNC-07", "State file corrupted during rapid sequential syncs",
                               "JSONDecodeError or missing keys detected", severity="CRITICAL")

        # Test 3.3: Concurrent Multi-Threaded Syncing (10 threads)
        errors = []
        def worker(thread_id):
            try:
                for j in range(20):
                    slot_id = (thread_id % 4) + 1
                    status = SlotStatusType.HEALTHY if j % 2 == 0 else SlotStatusType.QUOTA_EXHAUSTED
                    r = SlotProbeResult(slot=slot_id, email=f"user{slot_id}@gmail.com", status=status,
                                        reset_seconds=3600, reset_timestamp=now + 3600)
                    mgr.sync_probe_result(r)
            except Exception as ex:
                errors.append(str(ex))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            final_content = json.loads(mod.STATE_FILE.read_text())
            has_slots = "slots" in final_content and len(final_content["slots"]) >= 4
            if not errors and has_slots:
                ledger.record_pass("SYNC-08", "10 concurrent threads (200 ops) executed without crash or JSON corruption",
                                   f"Slots present: {list(final_content['slots'].keys())}")
            else:
                ledger.record_fail("SYNC-08", "Concurrent state sync produced errors or data loss",
                                   f"Errors: {errors}, Slots: {final_content.get('slots')}")
        except Exception as e:
            ledger.record_fail("SYNC-08", "Concurrent state sync resulted in unreadable corrupted JSON!",
                               str(e), severity="CRITICAL")

        # Test 3.4: Quota Reset in the Past Edge Case in sync_probe_result()
        # If res.reset_timestamp is in the past (e.g. 1577836800), does sync_probe_result set exhausted_until in the past?
        res_stale = SlotProbeResult(slot=5, email="slot5@gmail.com", status=SlotStatusType.QUOTA_EXHAUSTED,
                                    reset_seconds=0, reset_timestamp=1577836800, details="Quota limit hit")
        mgr.sync_probe_result(res_stale)
        s5_st = mgr.state["slots"]["5"]
        if s5_st.get("exhausted_until") == 1577836800:
            ledger.record_fail("SYNC-09", "sync_probe_result persists past reset_timestamp directly into state",
                               f"exhausted_until={s5_st.get('exhausted_until')} (< now {now}). Slot immediately un-cooldowns!",
                               severity="HIGH")
        else:
            ledger.record_pass("SYNC-09", "sync_probe_result clamps past reset_timestamp to future cooldown",
                               f"exhausted_until={s5_st.get('exhausted_until')}")

    finally:
        mod.ACCOUNTS_DIR = orig_acc
        mod.STATE_FILE = orig_state
        temp_dir.cleanup()

suite3_test_sync_probe_result_integrity()

# ==============================================================================
# SUMMARY & VERDICT GENERATION
# ==============================================================================
print("\n" + "=" * 75)
print(f" ADVERSARIAL CHALLENGE EXECUTION SUMMARY")
print(f" Total Tests Executed : {ledger.passes + ledger.failures}")
print(f" Passed Tests         : {ledger.passes}")
print(f" Failed Tests         : {ledger.failures}")
print("=" * 75)

if ledger.findings:
    print("\n[!] CONFIRMED VULNERABILITIES & REGRESSIONS:")
    for idx, f in enumerate(ledger.findings, 1):
        print(f"  {idx}. [{f['severity']}] {f['id']}: {f['desc']}")
        print(f"     Evidence: {f['evidence']}")
else:
    print("\n[+] All adversarial stress tests passed cleanly!")

sys.exit(0 if ledger.failures == 0 else 1)
