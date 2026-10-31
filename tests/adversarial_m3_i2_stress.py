#!/usr/bin/env python3
"""
INDEPENDENT ADVERSARIAL STRESS HARNESS: MILESTONE 3 ITERATION 2
Agent: m3_i2_chal_2 (Milestone 3 Iteration 2 Challenger 2)

Empirically challenges:
1. CloudCode 500, 502, 503, 504 and socket timeouts on retrieveUserQuota (Never swallowed as healthy)
2. OAuth 500, 502, 503, 504 and 429 on token refresh (Mapped to NETWORK_ERROR, never 10-year lockout)
3. Corrupt credential files (0-byte, truncated, binary nulls, non-dict JSON, {"token": null}, int id_token, dir-as-file, unreadable)
4. Past / invalid / non-ISO resetTime (Clamped to positive cooldown >= now + 3600)
5. Multi-slot isolation and state persistence under concurrent stress
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
import importlib.util
from importlib.machinery import SourceFileLoader

bin_path = str(REPO_ROOT / "bin" / "agy-supervisor")
loader = SourceFileLoader("supervisor", bin_path)
spec = importlib.util.spec_from_file_location("supervisor", bin_path, loader=loader)
mod = importlib.util.module_from_spec(spec)
sys.modules["supervisor"] = mod
spec.loader.exec_module(mod)

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

class TestHarness:
    def __init__(self):
        self.passes = 0
        self.failures = 0
        self.findings = []

    def check(self, condition: bool, test_id: str, desc: str, detail: str = ""):
        if condition:
            self.passes += 1
            print(f"  [PASS] {test_id}: {desc}")
            if detail:
                print(f"         Detail: {detail}")
        else:
            self.failures += 1
            print(f"  [FAIL] {test_id}: {desc}")
            if detail:
                print(f"         Detail: {detail}")
            self.findings.append({"id": test_id, "desc": desc, "detail": detail})

harness = TestHarness()

def run_stress_suite():
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

        # Setup standard healthy slot 1
        s1 = acc_dir / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.valid_s1_tok",
            "refresh_token": "1//refresh_s1",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="s1@gmail.com")
        }))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "s1@gmail.com"}))

        prober = SlotHealthProber(acc_dir)

        def mock_resp(body_dict, code=200):
            m = MagicMock()
            m.read.return_value = json.dumps(body_dict).encode("utf-8")
            m.code = code
            m.__enter__.return_value = m
            return m

        lca_200 = mock_resp({"currentTier": {"name": "Antigravity"}})

        # ======================================================================
        # SECTION 1: CloudCode Server Errors on retrieveUserQuota
        # ======================================================================
        print("\n--- SECTION 1: CloudCode Error Matrix on retrieveUserQuota ---")
        for err_code in [500, 502, 503, 504]:
            http_err = urllib.error.HTTPError(
                "https://daily-cloudcode-pa.googleapis.com", err_code, f"Server Error {err_code}", {},
                MagicMock(read=lambda c=err_code: f'{{"error": {{"code": {c}, "message": "Server failure"}}}}'.encode("utf-8"))
            )
            with patch("urllib.request.urlopen", side_effect=[lca_200, http_err]):
                res = prober.probe_slot(1, live_probe=True)
                harness.check(
                    res.status == SlotStatusType.NETWORK_ERROR and res.status != SlotStatusType.HEALTHY,
                    f"STRESS-CC-{err_code}",
                    f"retrieveUserQuota HTTP {err_code} mapped to NETWORK_ERROR (never swallowed as HEALTHY)",
                    f"status={res.status.value}, details={res.details}"
                )

        # What if retrieveUserQuota returns 200 with invalid JSON / HTML error page?
        mock_html_resp = MagicMock()
        mock_html_resp.read.return_value = b"<html><head><title>502 Bad Gateway</title></head><body>502</body></html>"
        mock_html_resp.__enter__.return_value = mock_html_resp
        with patch("urllib.request.urlopen", side_effect=[lca_200, mock_html_resp]):
            res = prober.probe_slot(1, live_probe=True)
            harness.check(
                res.status == SlotStatusType.NETWORK_ERROR,
                "STRESS-CC-HTML-200",
                "retrieveUserQuota HTML 200 error page handled as NETWORK_ERROR (no JSON crash)",
                f"status={res.status.value}, details={res.details}"
            )

        # ConnectionResetError and BrokenPipeError
        for net_exc in [ConnectionResetError("Connection reset by peer"), BrokenPipeError("Broken pipe")]:
            with patch("urllib.request.urlopen", side_effect=[lca_200, net_exc]):
                res = prober.probe_slot(1, live_probe=True)
                harness.check(
                    res.status == SlotStatusType.NETWORK_ERROR,
                    f"STRESS-NET-{type(net_exc).__name__}",
                    f"retrieveUserQuota {type(net_exc).__name__} correctly returns NETWORK_ERROR",
                    f"status={res.status.value}, details={res.details}"
                )

        # ======================================================================
        # SECTION 2: OAuth Refresh Error Matrix
        # ======================================================================
        print("\n--- SECTION 2: OAuth Refresh Error Matrix ---")
        # Expire slot token to force refresh
        s1_kd = {"token": {"refresh_token": "1//refresh_s1", "access_token": "ya29.old", "expiry": "1970-01-01T00:00:00Z"}, "id_token_email": "s1@gmail.com"}
        (s1 / "keyring_secret.json").write_text(json.dumps(s1_kd))
        (s1 / "oauth_creds.json").write_text(json.dumps({"refresh_token": "1//refresh_s1", "access_token": "ya29.old", "expiry_date": 1000}))

        for oauth_code in [500, 502, 503, 504, 429]:
            http_oauth_err = urllib.error.HTTPError(
                "https://oauth2.googleapis.com/token", oauth_code, f"OAuth {oauth_code}", {},
                MagicMock(read=lambda c=oauth_code: f'{{"error": "server_error_{c}"}}'.encode("utf-8"))
            )
            with patch("urllib.request.urlopen", side_effect=http_oauth_err):
                res = prober.probe_slot(1, live_probe=True)
                harness.check(
                    res.status == SlotStatusType.NETWORK_ERROR,
                    f"STRESS-OAUTH-{oauth_code}",
                    f"OAuth HTTP {oauth_code} returns NETWORK_ERROR (NOT TOKEN_REVOKED)",
                    f"status={res.status.value}, details={res.details}"
                )

        # Restore valid token
        (s1 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.valid_s1_tok",
            "refresh_token": "1//refresh_s1",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="s1@gmail.com")
        }))
        (s1 / "keyring_secret.json").write_text(json.dumps({
            "token": {"access_token": "ya29.valid_s1_tok", "refresh_token": "1//refresh_s1", "expiry": "2030-01-01T00:00:00Z"},
            "id_token_email": "s1@gmail.com"
        }))

        # ======================================================================
        # SECTION 3: Corrupt & Pathological Credential Files
        # ======================================================================
        print("\n--- SECTION 3: Pathological Credential Files Matrix ---")
        s2 = acc_dir / "2"
        s2.mkdir(parents=True, exist_ok=True)
        (s2 / "google_accounts.json").write_text(json.dumps({"active": "s2@gmail.com"}))

        pathological_cases = [
            ("EMPTY_FILE", b""),
            ("NULL_BYTES", b"\x00\x00\x00\x00\x00"),
            ("TRUNCATED_JSON", b'{"access_token": "ya29.'),
            ("JSON_ARRAY", b'["token1", "token2"]'),
            ("JSON_STRING", b'"just_a_string"'),
            ("JSON_INT", b'123456789'),
            ("JSON_NULL", b'null'),
            ("JSON_BOOL", b'true'),
            ("TOKEN_NULL_DICT", json.dumps({"token": None, "id_token_email": "s2@gmail.com"}).encode("utf-8")),
            ("TOKEN_STR_DICT", json.dumps({"token": "not_a_dict", "id_token_email": "s2@gmail.com"}).encode("utf-8")),
            ("ID_TOKEN_INT", json.dumps({"access_token": "ya29.tok", "id_token": 12345}).encode("utf-8")),
            ("ID_TOKEN_TWO_PARTS", json.dumps({"access_token": "ya29.tok", "id_token": "part1.part2"}).encode("utf-8")),
        ]

        for case_name, raw_content in pathological_cases:
            (s2 / "oauth_creds.json").write_bytes(raw_content)
            (s2 / "keyring_secret.json").write_bytes(raw_content)
            try:
                res = prober.probe_slot(2, live_probe=True)
                harness.check(
                    True,
                    f"STRESS-CORRUPT-{case_name}",
                    f"Pathological input {case_name} handled safely without unhandled exception",
                    f"status={res.status.value}"
                )
            except Exception as e:
                harness.check(
                    False,
                    f"STRESS-CORRUPT-{case_name}",
                    f"Pathological input {case_name} CRASHED with {type(e).__name__}: {e}"
                )

        # What if oauth_creds.json is a DIRECTORY?
        (s2 / "oauth_creds.json").unlink(missing_ok=True)
        (s2 / "keyring_secret.json").unlink(missing_ok=True)
        (s2 / "oauth_creds.json").mkdir(exist_ok=True)
        try:
            res = prober.probe_slot(2, live_probe=True)
            harness.check(
                True,
                "STRESS-CORRUPT-DIR_AS_FILE",
                "Directory named oauth_creds.json handled safely without crash",
                f"status={res.status.value}"
            )
        except Exception as e:
            harness.check(
                False,
                "STRESS-CORRUPT-DIR_AS_FILE",
                f"Directory named oauth_creds.json CRASHED with {type(e).__name__}: {e}"
            )
        (s2 / "oauth_creds.json").rmdir()

        # What if oauth_creds.json has 000 permissions (unreadable)?
        (s2 / "oauth_creds.json").write_text('{"access_token": "test"}')
        os.chmod(s2 / "oauth_creds.json", 0o000)
        try:
            res = prober.probe_slot(2, live_probe=True)
            harness.check(
                True,
                "STRESS-CORRUPT-UNREADABLE_FILE",
                "Unreadable oauth_creds.json handled safely without PermissionError crash",
                f"status={res.status.value}"
            )
        except Exception as e:
            harness.check(
                False,
                "STRESS-CORRUPT-UNREADABLE_FILE",
                f"Unreadable oauth_creds.json CRASHED with {type(e).__name__}: {e}"
            )
        os.chmod(s2 / "oauth_creds.json", 0o644)

        # ======================================================================
        # SECTION 4: Past, Stale, and Pathological resetTime
        # ======================================================================
        print("\n--- SECTION 4: Pathological resetTime Matrix ---")
        now_ts = int(time.time())
        reset_time_matrix = [
            ("PAST_1970", "1970-01-01T00:00:00Z"),
            ("PAST_2020", "2020-01-01T00:00:00Z"),
            ("PAST_1SEC_AGO", datetime.datetime.fromtimestamp(now_ts - 1, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")),
            ("NOW_EXACT", datetime.datetime.fromtimestamp(now_ts, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")),
            ("NON_STRING_INT", 123456789),
            ("EMPTY_STRING", ""),
            ("INVALID_FORMAT", "9999-99-99T99:99:99Z"),
            ("NONE_VALUE", None),
        ]

        mgr = AccountManager()

        for case_name, rst_val in reset_time_matrix:
            q_resp = mock_resp({
                "buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.0, "resetTime": rst_val}]
            })
            with patch("urllib.request.urlopen", side_effect=[lca_200, q_resp]):
                res = prober.probe_slot(1, live_probe=True)
                harness.check(
                    res.status == SlotStatusType.QUOTA_EXHAUSTED and res.reset_seconds > 0 and res.reset_timestamp > now_ts,
                    f"STRESS-RESET-{case_name}-PROBER",
                    f"prober clamps {case_name} to positive cooldown",
                    f"reset_seconds={res.reset_seconds}, reset_ts={res.reset_timestamp}"
                )
                mgr.sync_probe_result(res)
                exhausted_until = mgr.state["slots"]["1"].get("exhausted_until", 0)
                harness.check(
                    exhausted_until > now_ts,
                    f"STRESS-RESET-{case_name}-MGR",
                    f"sync_probe_result clamps {case_name} to future exhausted_until",
                    f"exhausted_until={exhausted_until} (now={now_ts})"
                )

        # ======================================================================
        # SECTION 5: Multi-Slot Concurrency & State Persistence Stress
        # ======================================================================
        print("\n--- SECTION 5: Multi-Slot Concurrency & State Persistence Stress ---")
        # Initialize 5 slots
        for s_idx in range(1, 6):
            s_p = acc_dir / str(s_idx)
            s_p.mkdir(parents=True, exist_ok=True)
            (s_p / "oauth_creds.json").write_text(json.dumps({
                "access_token": f"ya29.tok_{s_idx}",
                "refresh_token": f"1//refresh_{s_idx}",
                "expiry_date": (int(time.time()) + 3600) * 1000,
                "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email=f"user{s_idx}@gmail.com")
            }))
            (s_p / "google_accounts.json").write_text(json.dumps({"active": f"user{s_idx}@gmail.com"}))

        # Launch 20 concurrent threads running sync_probe_result and probe_slot
        sync_errors = []
        def stress_worker(t_id):
            try:
                for it in range(25):
                    target_slot = (t_id + it) % 5 + 1
                    status = SlotStatusType.HEALTHY if it % 2 == 0 else SlotStatusType.QUOTA_EXHAUSTED
                    r = SlotProbeResult(
                        slot=target_slot,
                        email=f"user{target_slot}@gmail.com",
                        status=status,
                        reset_seconds=3600,
                        reset_timestamp=int(time.time()) + 3600
                    )
                    mgr.sync_probe_result(r)
            except Exception as e:
                sync_errors.append(f"Thread {t_id}: {e}")

        threads = [threading.Thread(target=stress_worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        harness.check(
            len(sync_errors) == 0,
            "STRESS-CONCURRENCY-OPS",
            "500 concurrent sync operations across 20 threads completed without error",
            f"error_count={len(sync_errors)}"
        )

        try:
            state_raw = mod.STATE_FILE.read_text()
            loaded_state = json.loads(state_raw)
            harness.check(
                "slots" in loaded_state and len(loaded_state["slots"]) == 5,
                "STRESS-CONCURRENCY-JSON",
                "supervisor_state.json remains completely valid and uncorrupted after 500 concurrent writes",
                f"slots_found={list(loaded_state.get('slots', {}).keys())}"
            )
        except Exception as e:
            harness.check(
                False,
                "STRESS-CONCURRENCY-JSON",
                f"State file was corrupted: {e}"
            )

    finally:
        mod.ACCOUNTS_DIR = orig_acc
        mod.GEMINI_DIR = orig_gem
        mod.CLI_DIR = orig_cli
        mod.STATE_FILE = orig_state
        temp_dir.cleanup()

    print("\n" + "=" * 70)
    print(f"STRESS HARNESS COMPLETE: {harness.passes} PASSED, {harness.failures} FAILED")
    print("=" * 70)
    return harness.failures == 0

if __name__ == "__main__":
    success = run_stress_suite()
    sys.exit(0 if success else 1)
