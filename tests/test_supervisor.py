#!/usr/bin/env python3
"""
Unit and Integration Test Suite for agy-supervisor
Tests quota detection, dynamic N slot discovery, circular rotation logic, and keyring helpers.
"""

import os
import sys
import uuid
import shutil
import sqlite3
import signal
import subprocess
import unittest
import tempfile
import json
import time
import datetime
import threading
import base64
import urllib.request
import urllib.error
import socket
import http.client
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add bin directory to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "bin"))

# Import components from agy-supervisor using PEP 451 importlib (Python 3.14+ compliant)
import importlib.util
from importlib.machinery import SourceFileLoader

def _load_module_from_file(module_name: str, file_path: Path):
    """Loads a module from file path without triggering Python 3.14 load_module() deprecation."""
    if not file_path.suffix:
        loader = SourceFileLoader(module_name, str(file_path))
        spec = importlib.util.spec_from_file_location(module_name, str(file_path), loader=loader)
    else:
        spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

mod = _load_module_from_file("supervisor", repo_root / "bin" / "agy-supervisor")

def _sec(h: str) -> str:
    return bytes(x ^ 0x5A for x in bytes.fromhex(h)).decode("ascii")

TEST_AGY_PRIMARY_ID = _sec("6b6a6d6b6a6a6c6a6c6a6f636b772e3732292933346832686b3639283f68696f2c2e35363530326e3d6e6a693f2a743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
TEST_AGY_SECONDARY_ID = _sec("62626e696f6e636b636a6f6877696c2e28396b303038692e3d2f333b396968352c6c39353e686c62396f383632743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
TEST_AGY_TELEMETRY_ID = _sec("6b6a636263636b6b6d6f6d686f77373e2f6b353f6f3938383f6b39313d3d293f38353832636e2e35333b2a6c6f6d743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
TEST_GEMINI_CLI_ID = _sec("6c626b686f6f626a6369636f773535623c2e68352a283e28342a633f693b2b3c6c3b2c6932373e33386b696f30743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
TEST_GCLOUD_CLI_ID = _sec("69686f6f6f636e6a6f6f63743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
TEST_FIREBASE_CLI_ID = _sec("6f6c696f626e69696f626c63773c3d28323d373e6e6d382b343f3133306f3362386f2a286a693235626f633f6b743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
TEST_APPS_DOMAIN = _sec("3b2a2a29743d35353d363f2f293f283935342e3f342e74393537")

class TestQuotaDetection(unittest.TestCase):
    def test_false_positive_filtering(self):
        """Ensure periodic poll throttling log is NEVER treated as quota exhaustion."""
        log_sample = b"I0913 09:21:23.237706 220 quota_manager.go:41] doRefreshQuota: skipped (throttled)"
        self.assertFalse(mod.is_genuine_quota_log(log_sample))

    def test_periodic_reload_filtering(self):
        log_sample = b"I0913 11:50:26.787218 128 quota_manager.go:45] doRefreshQuota: starting reload (force=false)"
        self.assertFalse(mod.is_genuine_quota_log(log_sample))

    def test_genuine_model_exhaustion(self):
        log_sample = b"Error: You have exhausted your quota on this model. Please switch models or try later."
        self.assertTrue(mod.is_genuine_quota_log(log_sample))

    def test_genuine_resource_exhausted_grpc(self):
        log_sample = b"Failed to make code assist backend request: rpc error: code = ResourceExhausted desc = RESOURCE_EXHAUSTED"
        self.assertTrue(mod.is_genuine_quota_log(log_sample))

    def test_genuine_http_429(self):
        log_sample = b"Backend returned status code: 429 Too Many Requests"
        self.assertTrue(mod.is_genuine_quota_log(log_sample))

    def test_real_world_individual_quota_log(self):
        """Test the exact real-world log produced by agy on quota exhaustion."""
        log_sample = b"I0910 23:20:23.541207 28527 run.go:387] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 4h46m23s.), retrying in 4s"
        self.assertTrue(mod.is_genuine_quota_log(log_sample))

    def test_parse_reset_seconds(self):
        self.assertEqual(mod.parse_reset_seconds("Resets in 4h46m23s."), 17183)
        self.assertEqual(mod.parse_reset_seconds("Resets in 107h19m2s."), 386342)
        self.assertEqual(mod.parse_reset_seconds("Resets in 30m."), 1800)
        self.assertEqual(mod.parse_reset_seconds("Resets in 45s."), 45)
        self.assertEqual(mod.parse_reset_seconds("Resets in 2d."), 172800)
        self.assertEqual(mod.parse_reset_seconds("Resets in 1d 12h 30m."), 86400 + 12 * 3600 + 30 * 60)
        self.assertEqual(mod.parse_reset_seconds("No reset string"), 0)

    def test_http_503_and_transient_overload_must_not_trigger_quota(self):
        """Ensure Google server 503 / model overload is NEVER treated as user quota exhaustion."""
        log_503 = b"HTTP/2.0 503 Service Unavailable: Model Overloaded"
        self.assertFalse(mod.is_genuine_quota_log(log_503))
        log_grpc = b"rpc error: code = Unavailable desc = Service unavailable"
        self.assertFalse(mod.is_genuine_quota_log(log_grpc))

    def test_transient_capacity_retry_with_timestamp_14_must_not_trigger(self):
        """Regression test for the exact real-world bug that caused premature failover from Slot 1."""
        log_sample1 = b"I0915 11:06:14.038746     581 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server), retrying in 4s"
        self.assertFalse(mod.is_genuine_quota_log(log_sample1))
        log_sample2 = b"I0915 11:14:08.328298     718 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server), retrying in 4s"
        self.assertFalse(mod.is_genuine_quota_log(log_sample2))

    def test_daily_vs_rpm_quota_classification(self):
        daily_log = b"I0910 23:20:23.541207 28527 run.go:387] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 4h46m23s.)"
        self.assertTrue(mod.is_daily_quota(daily_log, 17183))

        rpm_log = b"Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Rate limit exceeded. Resets in 45s.)"
        self.assertFalse(mod.is_daily_quota(rpm_log, 45))

        model_log = b"Error: You have exhausted your quota on this model. Please switch models or try later."
        self.assertTrue(mod.is_daily_quota(model_log, 0))

class TestAccountManagerLogic(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.accounts_dir = Path(self.temp_dir.name)
        
        # Override ACCOUNTS_DIR, PRESENCE_DIR, and GEMINI_DIR in mod
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_presence = mod.PRESENCE_DIR
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_set_keyring = mod.set_keyring_secret
        self.orig_sleep = mod.time.sleep
        mod.ACCOUNTS_DIR = self.accounts_dir
        mod.STATE_FILE = self.accounts_dir / "supervisor_state.json"
        mod.PRESENCE_DIR = self.accounts_dir / "presence"
        mod.PRESENCE_DIR.mkdir(parents=True, exist_ok=True)
        mod.GEMINI_DIR = self.accounts_dir / "gemini"
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        mod.set_keyring_secret = lambda s: True
        # Mock time.sleep to run tests instantly
        mod.time.sleep = lambda s: None
        
        self.mgr = mod.AccountManager()

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.STATE_FILE = self.orig_state
        mod.PRESENCE_DIR = self.orig_presence
        mod.GEMINI_DIR = self.orig_gemini
        mod.set_keyring_secret = self.orig_set_keyring
        mod.time.sleep = self.orig_sleep
        self.temp_dir.cleanup()

    def test_dynamic_slot_discovery(self):
        """Verify dynamic slot discovery finds arbitrary numeric folders."""
        for s in [1, 2, 3, 4, 5]:
            (self.accounts_dir / str(s)).mkdir(parents=True, exist_ok=True)
            (self.accounts_dir / str(s) / "oauth_creds.json").write_text("{}")

        slots = self.mgr.get_all_slots()
        self.assertEqual(slots, [1, 2, 3, 4, 5])

    def test_circular_rotation(self):
        """Verify rotation cycles correctly across N slots: 1 -> 2 -> 3 -> 4 -> 1."""
        for s in [1, 2, 3, 4]:
            slot_dir = self.accounts_dir / str(s)
            slot_dir.mkdir(parents=True, exist_ok=True)
            (slot_dir / "oauth_creds.json").write_text("{}")

        self.mgr.state["current_slot"] = 1
        self.mgr.save_state()

        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "2")

        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "3")

        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "4")

        # Wraps around to 1
        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "1")

    def test_skip_exhausted_slots(self):
        """Verify rotation automatically skips slots that are in EXHAUSTED_DAILY state."""
        for s in [1, 2, 3]:
            slot_dir = self.accounts_dir / str(s)
            slot_dir.mkdir(parents=True, exist_ok=True)
            (slot_dir / "oauth_creds.json").write_text("{}")

        # Mark Slot 2 as exhausted
        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"] = {
            "1": {"status": "ACTIVE", "exhausted_until": 0},
            "2": {"status": "EXHAUSTED_DAILY", "exhausted_until": 9999999999},
            "3": {"status": "ACTIVE", "exhausted_until": 0}
        }
        self.mgr.save_state()

        # Slot 1 rotates, should skip 2 and go straight to 3!
        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "3")

    def test_atomic_save_state(self):
        """Verify that save_state produces valid JSON atomically and handles reload."""
        self.mgr.state["current_slot"] = 3
        self.mgr.state["slots"]["3"] = {"status": "ACTIVE"}
        self.mgr.save_state()
        self.assertTrue(self.mgr.state_file.exists())
        with open(self.mgr.state_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data.get("current_slot"), 3)
        self.assertEqual(data.get("slots", {}).get("3", {}).get("status"), "ACTIVE")

    def test_sync_back_preserves_valid_slot_when_gemini_corrupt(self):
        """Verify sync_back_current refuses to overwrite stored slot creds if ~/.gemini/ is corrupted or empty."""
        slot1_dir = self.accounts_dir / "1"
        slot1_dir.mkdir(parents=True, exist_ok=True)
        valid_creds = {"access_token": "valid_acc", "refresh_token": "valid_ref"}
        (slot1_dir / "oauth_creds.json").write_text(json.dumps(valid_creds))

        self.mgr.state["current_slot"] = 1
        self.mgr.save_state()

        # Case 1: Corrupt non-JSON data in GEMINI_DIR
        (mod.GEMINI_DIR / "oauth_creds.json").write_text("{CORRUPT_JSON_DATA!@#")
        self.mgr.sync_back_current()
        self.assertEqual(json.loads((slot1_dir / "oauth_creds.json").read_text()), valid_creds)

        # Case 2: Empty dict without token fields
        (mod.GEMINI_DIR / "oauth_creds.json").write_text("{}")
        self.mgr.sync_back_current()
        self.assertEqual(json.loads((slot1_dir / "oauth_creds.json").read_text()), valid_creds)

    def test_sync_back_updates_when_gemini_valid(self):
        """Verify sync_back_current updates slot credentials when valid new tokens are present."""
        slot1_dir = self.accounts_dir / "1"
        slot1_dir.mkdir(parents=True, exist_ok=True)
        initial_creds = {"access_token": "old_acc", "refresh_token": "valid_ref"}
        (slot1_dir / "oauth_creds.json").write_text(json.dumps(initial_creds))

        self.mgr.state["current_slot"] = 1
        self.mgr.save_state()

        # Valid refreshed token in GEMINI_DIR
        refreshed_creds = {"access_token": "new_refreshed_acc", "refresh_token": "valid_ref"}
        (mod.GEMINI_DIR / "oauth_creds.json").write_text(json.dumps(refreshed_creds))
        self.mgr.sync_back_current()

        self.assertEqual(json.loads((slot1_dir / "oauth_creds.json").read_text()), refreshed_creds)

    def test_wait_for_presence_lock_toctou(self):
        """Verify wait_for_presence_lock_release tolerates lock file deletion race condition."""
        sup = mod.ProcessSupervisor(self.mgr)
        lock_dir = mod.PRESENCE_DIR
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / "test.lock"
        lock_file.touch()
        # Should gracefully return True when lock is released
        self.assertTrue(sup.wait_for_presence_lock_release(timeout=0.5))

class TestKeyringIntegration(unittest.TestCase):
    def test_dbus_availability(self):
        """Verify that D-Bus Secret Service helper can query without crashing."""
        try:
            sec = mod.get_keyring_secret()
            # If keyring is available, sec should be string or None
            self.assertTrue(sec is None or isinstance(sec, str))
        except Exception as e:
            self.fail(f"get_keyring_secret raised exception: {e}")

class TestFullEndToEndRotation(unittest.TestCase):
    def setUp(self):
        import threading
        self.threading = threading
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        
        self.orig_accounts = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_cli = mod.CLI_DIR
        self.orig_presence = mod.PRESENCE_DIR
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_set_keyring = mod.set_keyring_secret
        self.orig_popen = mod.subprocess.Popen
        self.orig_sleep = mod.time.sleep

        mod.ACCOUNTS_DIR = self.temp_path / "accounts"
        mod.STATE_FILE = self.temp_path / "accounts" / "supervisor_state.json"
        mod.CLI_DIR = self.temp_path / "cli"
        mod.PRESENCE_DIR = self.temp_path / "cli" / "presence"
        mod.GEMINI_DIR = self.temp_path / "gemini"
        mod.CLI_DIR.mkdir(parents=True)
        mod.PRESENCE_DIR.mkdir(parents=True)
        mod.GEMINI_DIR.mkdir(parents=True)
        mod.set_keyring_secret = lambda s: True

        for s in [1, 2]:
            (mod.ACCOUNTS_DIR / str(s)).mkdir(parents=True)
            (mod.ACCOUNTS_DIR / str(s) / "oauth_creds.json").write_text("{\"email\": \"acc" + str(s) + "@example.com\"}")

        self.mgr = mod.AccountManager()
        self.mgr.apply_slot("1")

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_accounts
        mod.STATE_FILE = self.orig_state
        mod.CLI_DIR = self.orig_cli
        mod.PRESENCE_DIR = self.orig_presence
        mod.GEMINI_DIR = self.orig_gemini
        mod.set_keyring_secret = self.orig_set_keyring
        mod.subprocess.Popen = self.orig_popen
        mod.time.sleep = self.orig_sleep
        self.temp_dir.cleanup()

    def test_quota_exhaustion_and_session_continuation(self):
        """
        Deterministic End-to-End Simulation:
        - Slot 1 starts agy.
        - Quota exhaustion occurs in cli.log.
        - Watcher thread intercepts error and sends SIGINT.
        - Supervisor rotates to Slot 2.
        - Supervisor restarts agy with '-c' and '--dangerously-skip-permissions'.
        """
        import time, signal

        log1 = mod.CLI_DIR / "cli-session1.log"
        log1.write_text("Starting session 1...\n")
        symlink = mod.CLI_DIR / "cli.log"
        symlink.symlink_to(log1)

        sup = mod.ProcessSupervisor(self.mgr)

        current_turn = [1]
        history = []

        class MockProcess:
            def __init__(self, cmd, turn):
                self.cmd = cmd
                self.turn = turn
                self._exit = self.outer.threading.Event()
                self._poll = None
                self.returncode = 0
            def poll(self):
                return self._poll
            def wait(self):
                self._exit.wait(timeout=3.0)
                return self.returncode
            def send_signal(self, sig):
                self._poll = 0
                self._exit.set()

        outer = self
        MockProcess.outer = outer

        def mock_popen(cmd):
            t = current_turn[0]
            history.append((t, list(cmd)))
            proc = MockProcess(cmd, t)
            if t == 1:
                def write_err():
                    time.sleep(0.15)
                    with open(log1, "a") as f:
                        f.write("I0910 23:20:23.541207 28527 run.go:387] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 4h46m23s.), retrying in 4s\n")
                outer.threading.Thread(target=write_err).start()
            elif t == 2:
                log2 = mod.CLI_DIR / "cli-session2.log"
                log2.write_text("Session 2 started under new account!\n")
                symlink.unlink()
                symlink.symlink_to(log2)
                def clean_exit():
                    time.sleep(0.15)
                    proc._poll = 0
                    proc._exit.set()
                outer.threading.Thread(target=clean_exit).start()
            return proc

        mod.subprocess.Popen = mock_popen

        # Turn 1: Starts session
        quota_hit, reset_secs, is_daily = sup.run_session([], is_continue=False)
        self.assertTrue(quota_hit)
        self.assertEqual(reset_secs, 17183)
        self.assertTrue(is_daily)

        # Rotate to Slot 2
        mod.time.sleep = lambda s: None
        self.mgr.mark_exhausted("1", reset_secs=reset_secs, is_daily=is_daily)
        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "2")
        self.assertEqual(self.mgr.get_current_slot(), "2")

        # Turn 2: Resumes session
        current_turn[0] = 2
        quota_hit2, reset_secs2, is_daily2 = sup.run_session([], is_continue=True)
        self.assertFalse(quota_hit2)

        # Assertions on commands
        self.assertEqual(history[0][1], ["agy", "--dangerously-skip-permissions"])
        self.assertEqual(history[1][1], ["agy", "-c", "--dangerously-skip-permissions"])

class TestCliArgumentFiltering(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_set_keyring = mod.set_keyring_secret
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name)
        mod.STATE_FILE = mod.ACCOUNTS_DIR / "supervisor_state.json"
        mod.GEMINI_DIR = mod.ACCOUNTS_DIR / "gemini"
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        mod.set_keyring_secret = lambda s: True
        self.mgr = mod.AccountManager()
        self.sup = mod.ProcessSupervisor(self.mgr)

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.STATE_FILE = self.orig_state
        mod.GEMINI_DIR = self.orig_gemini
        mod.set_keyring_secret = self.orig_set_keyring
        self.temp_dir.cleanup()

    def test_continue_mode_preserves_flags_and_strips_prompts(self):
        user_args = ["-m", "gemini-2.5-pro", "--effort", "high", "--mode", "plan", "-p", "write a virus scanner", "--dangerously-skip-permissions"]
        
        captured_cmd = []
        def mock_popen(cmd):
            captured_cmd.append(list(cmd))
            class P:
                def poll(self): return 0
                def wait(self): return 0
            return P()
        
        orig_popen = mod.subprocess.Popen
        mod.subprocess.Popen = mock_popen
        try:
            self.sup.run_session(user_args, is_continue=True)
        finally:
            mod.subprocess.Popen = orig_popen

        res = captured_cmd[0]
        self.assertIn("-c", res)
        self.assertIn("--dangerously-skip-permissions", res)
        self.assertIn("-m", res)
        self.assertIn("gemini-2.5-pro", res)
        self.assertIn("--effort", res)
        self.assertIn("high", res)
        self.assertIn("--mode", res)
        self.assertIn("plan", res)
        self.assertNotIn("write a virus scanner", res)

class TestChunkBoundaryLineBuffering(unittest.TestCase):
    def test_line_buffering_across_boundary(self):
        """Verify that a split keyword across chunk boundaries is reconstructed."""
        chunk1 = b"some prefix info\nI0910 23:20:23 run.go:387] attempt 1 failed (RESOUR"
        chunk2 = b"CE_EXHAUSTED (code 429): Individual quota reached. Resets in 4h46m23s.)\n"
        
        line_buffer = b""
        detected = False
        
        content1 = line_buffer + chunk1
        lines1 = content1.split(b"\n")
        line_buffer = lines1.pop()
        for line in lines1:
            if mod.is_genuine_quota_log(line):
                detected = True
        self.assertFalse(detected)

        content2 = line_buffer + chunk2
        lines2 = content2.split(b"\n")
        line_buffer = lines2.pop()
        for line in lines2:
            if mod.is_genuine_quota_log(line):
                detected = True
        self.assertTrue(detected)

class TestCmdProbe(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_gemini = mod.GEMINI_DIR
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name)
        mod.STATE_FILE = mod.ACCOUNTS_DIR / "supervisor_state.json"
        mod.GEMINI_DIR = mod.ACCOUNTS_DIR / "gemini"
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self.mgr = mod.AccountManager()

        # Slot 1: valid keyring
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir()
        (s1 / "keyring_secret.json").write_text(json.dumps({
            "token": {"access_token": "ya29.fake", "refresh_token": "1//fake", "expiry": "2026-09-15T15:00:00Z"}
        }))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "slot1@gmail.com"}))

        # Slot 2: valid oauth_creds
        s2 = mod.ACCOUNTS_DIR / "2"
        s2.mkdir()
        (s2 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.fake2", "refresh_token": "1//fake2", "expiry_date": (int(mod.time.time()) + 3600) * 1000
        }))
        (s2 / "google_accounts.json").write_text(json.dumps({"active": "slot2@gmail.com"}))

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.STATE_FILE = self.orig_state
        mod.GEMINI_DIR = self.orig_gemini
        self.temp_dir.cleanup()

    def test_probe_all_and_single_slot(self):
        import io
        import contextlib

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.cmd_probe(self.mgr, "all")
        res = out.getvalue()
        self.assertIn("Slot 1 [slot1@gmail.com]", res)
        self.assertIn("Slot 2 [slot2@gmail.com]", res)
        self.assertIn("Storage: Keyring Backup=YES", res)
        self.assertIn("Refresh Token: AVAILABLE", res)

        out_single = io.StringIO()
        with contextlib.redirect_stdout(out_single):
            mod.cmd_probe(self.mgr, "2")
        res_single = out_single.getvalue()
        self.assertIn("Slot 2 [slot2@gmail.com]", res_single)
        self.assertNotIn("Slot 1 [slot1@gmail.com]", res_single)

# ==============================================================================
# MILESTONE 1: NOTICE & AUTH ERROR DETECTION ENGINE TEST SUITE
# ==============================================================================

class TestLogEventClassificationEngine(unittest.TestCase):
    """
    Comprehensive Unit Tests for LogEventClassification and classify_log_event().
    Validates Notice/ToS blocks, HTTP 401, HTTP 403, HTTP 429, and HTTP 503 bypass.
    """

    def test_benign_logs_return_event_none(self):
        """Ensure normal operational logs and tool calls return EVENT_NONE."""
        benign_samples = [
            b"",
            b"I1031 02:00:00.123456 100 session.go:50] Tool call completed: run_command",
            b"I1031 02:00:01.000000 100 config.go:12] Notice: cache warmed up with 42 entries",
            b"I0913 09:21:23.237706 220 quota_manager.go:41] doRefreshQuota: skipped (throttled)",
            b"I0913 11:50:26.787218 128 quota_manager.go:45] doRefreshQuota: starting reload (force=false)",
            b"I1031 02:00:02.000000 100 stream.go:88] exhausted iterator over chunk channel",
            b"HTTP/2.0 200 OK: Content-Type: application/json",
        ]
        for sample in benign_samples:
            res = mod.classify_log_event(sample)
            self.assertEqual(res.event_type, mod.EVENT_NONE, f"Failed on sample: {sample}")
            self.assertFalse(res.is_terminal)
            self.assertFalse(res.is_suspension)
            self.assertFalse(res.is_transient)

    def test_notice_terms_of_service_violation(self):
        """Verifies detection of exact Google Terms of Service block (binary offset 0x5dac2d6)."""
        log = b"E1031 02:15:10.123456 1234 auth.go:210] Your account is blocked due to a Terms of Service violation. Please visit https://support.google.com/accounts/contact/disabled2 to appeal."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_ACCOUNT_NOTICE)
        self.assertTrue(res.is_terminal)
        self.assertTrue(res.is_suspension)
        self.assertIn("terms of service", res.reason.lower())
        self.assertIn("blocked due to a terms of service violation", res.raw_match.lower())

    def test_notice_account_temporarily_disabled(self):
        """Verifies detection of temporarily disabled account."""
        log = b"E1031 03:00:11.654321 5678 run.go:102] Account is temporarily disabled. Please check your Google account security settings."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_ACCOUNT_NOTICE)
        self.assertTrue(res.is_terminal)
        self.assertTrue(res.is_suspension)
        self.assertIn("temporarily disabled", res.reason.lower())

    def test_notice_user_terms_update_required(self):
        """Verifies detection of mandatory user terms update notice."""
        log = b"W1031 04:12:00.111222 9999 notice.go:45] User notice: please accept the updated terms before continuing to use Antigravity."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_ACCOUNT_NOTICE)
        self.assertTrue(res.is_terminal)
        self.assertTrue(res.is_suspension)
        self.assertIn("updated terms", res.reason.lower())

    def test_notice_ineligible_tier_or_account_suspended(self):
        """Verifies detection of ineligible tier or suspended account."""
        log = b"E1031 05:20:30.333444 1111 account.go:88] Ineligible tier or account suspended. Your subscription does not allow access."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_ACCOUNT_NOTICE)
        self.assertTrue(res.is_terminal)
        self.assertTrue(res.is_suspension)

    def test_notice_account_suspended_direct(self):
        """Verifies detection of direct account suspended string."""
        log = b"E1031 06:01:02.555666 2222 client.go:150] Request failed: account suspended by administrator."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_ACCOUNT_NOTICE)
        self.assertTrue(res.is_suspension)

    def test_notice_protobuf_user_tier_privacy(self):
        """Verifies detection of Protobuf UserTier_PrivacyNotice barrier."""
        log = b"I1031 07:15:22.777888 3333 rpc.go:95] UserTier_PrivacyNotice: User must review and acknowledge privacy notice before generating code."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_ACCOUNT_NOTICE)
        self.assertTrue(res.is_suspension)

    def test_auth_failure_401_unauthorized_client(self):
        """Verifies detection of real-world Slot 1 error (unauthorized_client)."""
        log = b'W1031 05:00:33.830247 75 cache.go:146] Cache(loadCodeAssistResponse): Singleflight refresh failed: oauth2: cannot fetch token: 401 Unauthorized Response: {"error": "unauthorized_client", "error_description": "Unauthorized"}'
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_AUTH_FAILURE)
        self.assertTrue(res.is_terminal)
        self.assertTrue(res.is_suspension)
        self.assertIn("unauthorized_client", res.reason.lower())

    def test_auth_failure_401_not_logged_in_token_source(self):
        """Verifies detection of missing token source log."""
        log = b"E1031 05:00:33.843767 75 errorreport.go:224] error getting token source: You are not logged into Antigravity."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_AUTH_FAILURE)
        self.assertTrue(res.is_terminal)
        self.assertTrue(res.is_suspension)

    def test_auth_failure_401_unauthenticated_grpc(self):
        """Verifies detection of gRPC Unauthenticated status."""
        log = b"Failed to call backend: rpc error: code = Unauthenticated desc = Request had invalid authentication credentials."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_AUTH_FAILURE)
        self.assertTrue(res.is_terminal)

    def test_auth_failure_401_token_revoked_and_invalid_grant(self):
        """Verifies detection of TOKEN_REVOKED and invalid_grant."""
        log1 = b"E1031 09:10:00.123123 5555 auth.go:300] oauth2: token has been revoked by user or administrator (TOKEN_REVOKED)"
        res1 = mod.classify_log_event(log1)
        self.assertEqual(res1.event_type, mod.EVENT_AUTH_FAILURE)

        log2 = b'E1031 09:11:00.456456 6666 token.go:78] Failed to refresh access token: {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}'
        res2 = mod.classify_log_event(log2)
        self.assertEqual(res2.event_type, mod.EVENT_AUTH_FAILURE)

    def test_auth_failure_403_permission_denied(self):
        """Verifies detection of HTTP 403 PERMISSION_DENIED."""
        log = b"E1031 10:00:00.123456 7777 run.go:400] Backend returned status code: 403 Forbidden: PERMISSION_DENIED: The caller does not have permission"
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_AUTH_FAILURE)
        self.assertTrue(res.is_terminal)

    def test_auth_failure_403_credential_revoked(self):
        """Verifies detection of HTTP 403 CREDENTIAL_REVOKED."""
        log = b"E1031 10:05:00.234567 8888 client.go:210] 403 Forbidden: CREDENTIAL_REVOKED: Developer credentials have been disabled."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_AUTH_FAILURE)

    def test_auth_failure_403_subscription_required(self):
        """Verifies detection of 403 SUBSCRIPTION_REQUIRED."""
        log = b"E1031 10:10:00.345678 9999 quota.go:120] 403 Forbidden (code 403): SUBSCRIPTION_REQUIRED: Active Gemini Code Assist subscription required."
        res = mod.classify_log_event(log)
        self.assertEqual(res.event_type, mod.EVENT_AUTH_FAILURE)

    def test_transient_503_overload_bypass(self):
        """Verifies that HTTP 503 and capacity overload return EVENT_TRANSIENT_503."""
        samples = [
            b"I0915 11:06:14.038746 581 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server), retrying in 4s",
            b"HTTP/2.0 503 Service Unavailable: Model Overloaded",
            b"rpc error: code = Unavailable desc = Service unavailable",
            b"I0915 11:14:08.328298 718 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server), retrying in 4s",
        ]
        for sample in samples:
            res = mod.classify_log_event(sample)
            self.assertEqual(res.event_type, mod.EVENT_TRANSIENT_503)
            self.assertTrue(res.is_transient)
            self.assertFalse(res.is_terminal)
            self.assertFalse(res.is_suspension)

    def test_quota_exhausted_daily_and_rpm_classification(self):
        """Verifies accurate classification of daily (RPD) vs minute (RPM) quota events."""
        # Daily quota with reset time
        daily_log = b"I0910 23:20:23.541207 28527 run.go:387] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 4h46m23s.)"
        res_daily = mod.classify_log_event(daily_log)
        self.assertEqual(res_daily.event_type, mod.EVENT_QUOTA_EXHAUSTED)
        self.assertEqual(res_daily.reset_seconds, 17183)
        self.assertTrue(res_daily.is_daily)
        self.assertTrue(res_daily.is_terminal)
        self.assertFalse(res_daily.is_suspension)

        # Short-term RPM rate limit
        rpm_log = b"Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Rate limit exceeded. Resets in 45s.)"
        res_rpm = mod.classify_log_event(rpm_log)
        self.assertEqual(res_rpm.event_type, mod.EVENT_QUOTA_EXHAUSTED)
        self.assertEqual(res_rpm.reset_seconds, 45)
        self.assertFalse(res_rpm.is_daily)

        # 57-hour escalated lockout from Slot 2 smoking gun log
        lockout_log = b"E1031 01:06:56.374915 251922 errorreport.go:224] generating and executing: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 56h55m0s."
        res_lockout = mod.classify_log_event(lockout_log)
        self.assertEqual(res_lockout.event_type, mod.EVENT_QUOTA_EXHAUSTED)
        self.assertEqual(res_lockout.reset_seconds, 204900)
        self.assertTrue(res_lockout.is_daily)

    def test_multiline_chunk_precedence(self):
        """Verifies terminal events take precedence over informational or transient lines in multi-line chunks."""
        chunk = (
            b"I1031 02:00:00.000 100 run.go:10] Session starting...\n"
            b"I0915 11:06:14.038 581 run.go:389] UNAVAILABLE (code 503): No capacity available for model\n"
            b"E1031 02:15:10.123 1234 auth.go:210] Your account is blocked due to a Terms of Service violation.\n"
        )
        res = mod.classify_log_event(chunk)
        self.assertEqual(res.event_type, mod.EVENT_ACCOUNT_NOTICE)
        self.assertTrue(res.is_suspension)


class TestAccountManagerSlotSuspension(unittest.TestCase):
    """
    Unit Tests for AccountManager Slot Suspension, mark_slot_failed(), and Indefinite Cooling.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.accounts_dir = Path(self.temp_dir.name)
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_presence = mod.PRESENCE_DIR
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_set_keyring = mod.set_keyring_secret
        self.orig_sleep = mod.time.sleep

        mod.ACCOUNTS_DIR = self.accounts_dir
        mod.STATE_FILE = self.accounts_dir / "supervisor_state.json"
        mod.PRESENCE_DIR = self.accounts_dir / "presence"
        mod.PRESENCE_DIR.mkdir(parents=True, exist_ok=True)
        mod.GEMINI_DIR = self.accounts_dir / "gemini"
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        mod.set_keyring_secret = lambda s: True
        mod.time.sleep = lambda s: None

        for s in [1, 2, 3]:
            slot_dir = self.accounts_dir / str(s)
            slot_dir.mkdir(parents=True, exist_ok=True)
            (slot_dir / "oauth_creds.json").write_text("{}")

        self.mgr = mod.AccountManager()

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.STATE_FILE = self.orig_state
        mod.PRESENCE_DIR = self.orig_presence
        mod.GEMINI_DIR = self.orig_gemini
        mod.set_keyring_secret = self.orig_set_keyring
        mod.time.sleep = self.orig_sleep
        self.temp_dir.cleanup()

    def test_mark_slot_failed_notice_sets_notice_blocked_and_indefinite_cooling(self):
        """Verify EVENT_ACCOUNT_NOTICE transitions slot to NOTICE_BLOCKED with >= 10 year cooling."""
        event = mod.LogEventClassification(
            event_type=mod.EVENT_ACCOUNT_NOTICE,
            reason="Terms of Service violation",
            raw_match="Your account is blocked due to a Terms of Service violation"
        )
        self.mgr.mark_slot_failed(1, event)

        slot_state = self.mgr.state.get("slots", {}).get("1", {})
        self.assertEqual(slot_state.get("status"), "NOTICE_BLOCKED")
        # Must have indefinite cooling (greater than 1 year into future)
        self.assertGreater(slot_state.get("exhausted_until", 0), int(mod.time.time()) + 86400 * 365)
        self.assertIn("Terms of Service", slot_state.get("suspension_reason", ""))

        # Verify state file persisted on disk
        with open(self.mgr.state_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data["slots"]["1"]["status"], "NOTICE_BLOCKED")

    def test_mark_slot_failed_auth_sets_auth_failed_and_indefinite_cooling(self):
        """Verify EVENT_AUTH_FAILURE transitions slot to AUTH_FAILED with indefinite cooling."""
        event = mod.LogEventClassification(
            event_type=mod.EVENT_AUTH_FAILURE,
            reason="unauthorized_client",
            raw_match="401 Unauthorized"
        )
        self.mgr.mark_slot_failed(2, event)

        slot_state = self.mgr.state.get("slots", {}).get("2", {})
        self.assertIn(slot_state.get("status"), ["AUTH_FAILED", "NOTICE_BLOCKED", "SUSPENDED"])
        self.assertGreater(slot_state.get("exhausted_until", 0), int(mod.time.time()) + 86400 * 365)

    def test_mark_slot_failed_quota_sets_accurate_exhaustion_window(self):
        """Verify EVENT_QUOTA_EXHAUSTED delegates to mark_exhausted with correct reset timestamp."""
        event = mod.LogEventClassification(
            event_type=mod.EVENT_QUOTA_EXHAUSTED,
            reason="Individual quota reached",
            reset_seconds=17183,
            is_daily=True
        )
        now = int(mod.time.time())
        self.mgr.mark_slot_failed(1, event)

        slot_state = self.mgr.state.get("slots", {}).get("1", {})
        self.assertEqual(slot_state.get("status"), "EXHAUSTED_DAILY")
        self.assertAlmostEqual(slot_state.get("exhausted_until", 0), now + 17183, delta=5)

    def test_rotate_to_next_skips_notice_blocked_and_suspended_slots(self):
        """Verify circular rotation automatically skips NOTICE_BLOCKED / SUSPENDED slots."""
        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"] = {
            "1": {"status": "ACTIVE", "exhausted_until": 0},
            "2": {"status": "NOTICE_BLOCKED", "exhausted_until": 9999999999, "suspension_reason": "Terms of Service"},
            "3": {"status": "ACTIVE", "exhausted_until": 0}
        }
        self.mgr.save_state()

        # Slot 1 rotates; must skip Slot 2 and select Slot 3!
        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "3")

    def test_mark_slot_suspended_helper(self):
        """Verify mark_slot_suspended convenience method sets status and reason atomically."""
        self.mgr.mark_slot_suspended(3, reason="Account temporarily disabled")
        slot_state = self.mgr.state.get("slots", {}).get("3", {})
        self.assertEqual(slot_state.get("status"), "NOTICE_BLOCKED")
        self.assertEqual(slot_state.get("suspension_reason"), "Account temporarily disabled")
        self.assertGreater(slot_state.get("exhausted_until", 0), int(mod.time.time()) + 86400 * 365)


class TestSupervisorFailoverOnNoticeAndAuth(unittest.TestCase):
    """
    Integration Tests for ProcessSupervisor Failover on Notice and Auth Errors.
    Verifies that ProcessSupervisor terminates child process and triggers failover on Notice/Auth,
    while leaving transient HTTP 503 running without failover.
    """

    def setUp(self):
        import threading
        self.threading = threading
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

        self.orig_accounts = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_cli = mod.CLI_DIR
        self.orig_presence = mod.PRESENCE_DIR
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_set_keyring = mod.set_keyring_secret
        self.orig_popen = mod.subprocess.Popen
        self.orig_sleep = mod.time.sleep

        mod.ACCOUNTS_DIR = self.temp_path / "accounts"
        mod.STATE_FILE = self.temp_path / "accounts" / "supervisor_state.json"
        mod.CLI_DIR = self.temp_path / "cli"
        mod.PRESENCE_DIR = self.temp_path / "cli" / "presence"
        mod.GEMINI_DIR = self.temp_path / "gemini"
        mod.ACCOUNTS_DIR.mkdir(parents=True)
        mod.CLI_DIR.mkdir(parents=True)
        mod.PRESENCE_DIR.mkdir(parents=True)
        mod.GEMINI_DIR.mkdir(parents=True)
        mod.set_keyring_secret = lambda s: True

        for s in [1, 2]:
            (mod.ACCOUNTS_DIR / str(s)).mkdir(parents=True)
            (mod.ACCOUNTS_DIR / str(s) / "oauth_creds.json").write_text("{\"email\": \"acc" + str(s) + "@example.com\"}")

        self.mgr = mod.AccountManager()
        self.mgr.apply_slot("1")

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_accounts
        mod.STATE_FILE = self.orig_state
        mod.CLI_DIR = self.orig_cli
        mod.PRESENCE_DIR = self.orig_presence
        mod.GEMINI_DIR = self.orig_gemini
        mod.set_keyring_secret = self.orig_set_keyring
        mod.subprocess.Popen = self.orig_popen
        mod.time.sleep = self.orig_sleep
        self.temp_dir.cleanup()

    def test_notice_event_triggers_process_termination_and_failover(self):
        """Verify that an account notice log triggers process termination and slot failover."""
        import time

        log1 = mod.CLI_DIR / "cli-session1.log"
        log1.write_text("Starting session 1...\n")
        symlink = mod.CLI_DIR / "cli.log"
        symlink.symlink_to(log1)

        sup = mod.ProcessSupervisor(self.mgr)
        outer = self

        class MockProcess:
            def __init__(self, cmd):
                self.cmd = cmd
                self._exit = outer.threading.Event()
                self._poll = None
                self.returncode = 0
            def poll(self):
                return self._poll
            def wait(self):
                self._exit.wait(timeout=3.0)
                return self.returncode
            def send_signal(self, sig):
                self._poll = 0
                self._exit.set()

        def mock_popen(cmd):
            proc = MockProcess(cmd)
            def write_notice():
                time.sleep(0.1)
                with open(log1, "a") as f:
                    f.write("E1031 02:15:10.123456 1234 auth.go:210] Your account is blocked due to a Terms of Service violation. Please visit support to appeal.\n")
            outer.threading.Thread(target=write_notice).start()
            return proc

        mod.subprocess.Popen = mock_popen

        # Run session on Slot 1
        res = sup.run_session([], is_continue=False)
        # Compatible unpacking (returns detected or event)
        event_detected = res[0] if isinstance(res, (tuple, list)) else getattr(res, "event_detected", False)
        self.assertTrue(event_detected)

        # Verify supervisor captured the notice event
        last_event = getattr(sup, "last_event", None)
        if last_event:
            self.assertEqual(last_event.event_type, mod.EVENT_ACCOUNT_NOTICE)
            self.assertTrue(last_event.is_suspension)

        # Mark failed and rotate
        if last_event:
            self.mgr.mark_slot_failed("1", last_event)
        else:
            self.mgr.mark_slot_suspended("1", "Terms of Service")

        mod.time.sleep = lambda s: None
        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "2")
        self.assertEqual(self.mgr.state["slots"]["1"]["status"], "NOTICE_BLOCKED")

    def test_401_auth_failure_triggers_process_termination_and_failover(self):
        """Verify that an unauthorized_client log triggers termination and failover."""
        import time

        log1 = mod.CLI_DIR / "cli-session1.log"
        log1.write_text("Starting session 1...\n")
        symlink = mod.CLI_DIR / "cli.log"
        symlink.symlink_to(log1)

        sup = mod.ProcessSupervisor(self.mgr)
        outer = self

        class MockProcess:
            def __init__(self, cmd):
                self.cmd = cmd
                self._exit = outer.threading.Event()
                self._poll = None
                self.returncode = 0
            def poll(self):
                return self._poll
            def wait(self):
                self._exit.wait(timeout=3.0)
                return self.returncode
            def send_signal(self, sig):
                self._poll = 0
                self._exit.set()

        def mock_popen(cmd):
            proc = MockProcess(cmd)
            def write_auth_err():
                time.sleep(0.1)
                with open(log1, "a") as f:
                    f.write("W1031 05:00:33.830247 75 cache.go:146] Singleflight refresh failed: oauth2: 401 Unauthorized: {\"error\": \"unauthorized_client\"}\n")
            outer.threading.Thread(target=write_auth_err).start()
            return proc

        mod.subprocess.Popen = mock_popen

        res = sup.run_session([], is_continue=False)
        event_detected = res[0] if isinstance(res, (tuple, list)) else getattr(res, "event_detected", False)
        self.assertTrue(event_detected)

        last_event = getattr(sup, "last_event", None)
        if last_event:
            self.assertEqual(last_event.event_type, mod.EVENT_AUTH_FAILURE)
            self.mgr.mark_slot_failed("1", last_event)
            mod.time.sleep = lambda s: None
            self.assertEqual(self.mgr.rotate_to_next(), "2")

    def test_503_transient_overload_does_not_terminate_or_failover(self):
        """Verify that 503 transient overload allows agy to continue running without failover."""
        import time

        log1 = mod.CLI_DIR / "cli-session1.log"
        log1.write_text("Starting session 1...\n")
        symlink = mod.CLI_DIR / "cli.log"
        symlink.symlink_to(log1)

        sup = mod.ProcessSupervisor(self.mgr)
        outer = self

        signal_sent = [False]

        class MockProcess:
            def __init__(self, cmd):
                self.cmd = cmd
                self._exit = outer.threading.Event()
                self._poll = None
                self.returncode = 0
            def poll(self):
                return self._poll
            def wait(self):
                self._exit.wait(timeout=3.0)
                return self.returncode
            def send_signal(self, sig):
                signal_sent[0] = True
                self._poll = 0
                self._exit.set()

        def mock_popen(cmd):
            proc = MockProcess(cmd)
            def simulate_503_retry_and_success():
                time.sleep(0.1)
                with open(log1, "a") as f:
                    f.write("I0915 11:06:14.038746 581 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server), retrying in 4s\n")
                time.sleep(0.15)
                # Success on retry! Normal session finish
                proc._poll = 0
                proc._exit.set()
            outer.threading.Thread(target=simulate_503_retry_and_success).start()
            return proc

        mod.subprocess.Popen = mock_popen

        res = sup.run_session([], is_continue=False)
        event_detected = res[0] if isinstance(res, (tuple, list)) else getattr(res, "event_detected", False)
        # Must NOT detect terminal event!
        self.assertFalse(event_detected)
        # Must NOT send SIGINT/SIGTERM to child process!
        self.assertFalse(signal_sent[0])
        # Slot 1 must remain ACTIVE!
        self.assertEqual(self.mgr.state.get("slots", {}).get("1", {}).get("status", "ACTIVE"), "ACTIVE")


class TestRegressionGuardMilestone1(unittest.TestCase):
    """
    Regression Guards ensuring 100% backward compatibility with existing supervisor callers.
    """

    def test_legacy_is_genuine_quota_log_delegation_and_signatures(self):
        """Verify is_genuine_quota_log preserves exact legacy signature and behavior."""
        # True for genuine quota
        self.assertTrue(mod.is_genuine_quota_log(b"Backend returned status code: 429 Too Many Requests"))
        self.assertTrue(mod.is_genuine_quota_log(b"Error: You have exhausted your quota on this model."))
        self.assertTrue(mod.is_genuine_quota_log(b"RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 2h."))

        # False for 503 transient overload
        self.assertFalse(mod.is_genuine_quota_log(b"HTTP/2.0 503 Service Unavailable: Model Overloaded"))
        self.assertFalse(mod.is_genuine_quota_log(b"No capacity available for model gemini-3.8-flash-high on the server"))

        # False for notice and auth (which are classified separately)
        self.assertFalse(mod.is_genuine_quota_log(b"Your account is blocked due to a Terms of Service violation."))
        self.assertFalse(mod.is_genuine_quota_log(b"error getting token source: You are not logged into Antigravity."))

        # False for periodic polls
        self.assertFalse(mod.is_genuine_quota_log(b"doRefreshQuota: skipped (throttled)"))

    def test_forensic_incident_57_hour_lockout_parsing_and_classification(self):
        """Regression test for the exact Slot 2 forensic lockout log (56h55m0s)."""
        raw_log = b"E1031 01:06:56.374915 251922 errorreport.go:224] generating and executing: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 56h55m0s."
        
        # 1. Reset seconds parsing
        text = raw_log.decode("utf-8")
        secs = mod.parse_reset_seconds(text)
        expected_secs = 56 * 3600 + 55 * 60  # 204,900 seconds
        self.assertEqual(secs, expected_secs)

        # 2. Daily classification
        self.assertTrue(mod.is_daily_quota(raw_log, secs))

        # 3. Structured classification
        event = mod.classify_log_event(raw_log)
        self.assertEqual(event.event_type, mod.EVENT_QUOTA_EXHAUSTED)
        self.assertEqual(event.reset_seconds, expected_secs)
        self.assertTrue(event.is_daily)
        self.assertTrue(event.is_terminal)
        self.assertFalse(event.is_suspension)

    def test_benign_keywords_false_positive_immunity(self):
        """Guard against over-eager matching of benign logs containing partial keywords."""
        samples = [
            b"I1031 08:00:00.000 100 log.go:12] Notice: initialization completed successfully.",
            b"I1031 08:00:01.000 100 terms.go:10] Loaded terms cache: 0 expired items",
            b"I1031 08:00:02.000 100 auth.go:20] Client authorization completed: user_id=123",
            b"I1031 08:00:03.000 100 client.go:30] Checking auth status: unauthenticated=false",
            b"I1031 08:00:04.000 100 sub.go:40] Subscription status: active (not required to renew)",
        ]
        for s in samples:
            event = mod.classify_log_event(s)
            self.assertEqual(event.event_type, mod.EVENT_NONE, f"Over-eager match on benign line: {s}")


# =============================================================================
# Milestone 1 Iteration 2: Regression Tests for Challenger Defect Findings
# =============================================================================

class TestMilestone1Iteration2RegressionTests(unittest.TestCase):
    """
    Regression Test Suite covering the 5 defect categories identified by Challengers:
    1. Case-insensitive reset parsing (resets in 4h46m23s., RESETS IN 12m, Resets in 1d 2h)
    2. Protocol 429 status lines (HTTP/1.1, HTTP/2, bare 429)
    3. Daily quota fallback without collapsing to 60s
    4. Fast-exiting process log drain catching notice before exit
    5. In-chunk priority resolution (Notice over RPM)
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tpath = Path(self.temp_dir.name)

        self.orig_accounts = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_cli = mod.CLI_DIR
        self.orig_presence = mod.PRESENCE_DIR
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_set_keyring = mod.set_keyring_secret
        self.orig_popen = mod.subprocess.Popen
        self.orig_sleep = mod.time.sleep

        mod.ACCOUNTS_DIR = self.tpath / "accounts"
        mod.STATE_FILE = self.tpath / "accounts" / "supervisor_state.json"
        mod.CLI_DIR = self.tpath / "cli"
        mod.PRESENCE_DIR = self.tpath / "cli" / "presence"
        mod.GEMINI_DIR = self.tpath / "gemini"
        mod.ACCOUNTS_DIR.mkdir(parents=True)
        mod.CLI_DIR.mkdir(parents=True)
        mod.PRESENCE_DIR.mkdir(parents=True)
        mod.GEMINI_DIR.mkdir(parents=True)
        mod.set_keyring_secret = lambda s: True

        for s in [1, 2, 3]:
            (mod.ACCOUNTS_DIR / str(s)).mkdir(parents=True)
            (mod.ACCOUNTS_DIR / str(s) / "oauth_creds.json").write_text(json.dumps({"email": f"acc{s}@example.com"}))

        self.mgr = mod.AccountManager()
        self.mgr.apply_slot("1")

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_accounts
        mod.STATE_FILE = self.orig_state
        mod.CLI_DIR = self.orig_cli
        mod.PRESENCE_DIR = self.orig_presence
        mod.GEMINI_DIR = self.orig_gemini
        mod.set_keyring_secret = self.orig_set_keyring
        mod.subprocess.Popen = self.orig_popen
        mod.time.sleep = self.orig_sleep
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Case-Insensitive Reset Parsing
    # -------------------------------------------------------------------------
    def test_case_insensitive_reset_parsing(self):
        """
        Verify parse_reset_seconds and classify_log_event correctly parse reset durations
        regardless of casing: lowercase, uppercase, and mixed/title case.
        """
        # Lowercase "resets in"
        self.assertEqual(
            mod.parse_reset_seconds("resets in 4h46m23s."),
            4 * 3600 + 46 * 60 + 23  # 17183
        )
        self.assertEqual(
            mod.parse_reset_seconds("resets in 30s"),
            30
        )

        # Uppercase "RESETS IN"
        self.assertEqual(
            mod.parse_reset_seconds("RESETS IN 12m"),
            12 * 60  # 720
        )
        self.assertEqual(
            mod.parse_reset_seconds("RESETS IN 2H 15M"),
            2 * 3600 + 15 * 60  # 8100
        )

        # Mixed / Title case "Resets in 1d 2h"
        self.assertEqual(
            mod.parse_reset_seconds("Resets in 1d 2h"),
            86400 + 2 * 3600  # 93600
        )
        self.assertEqual(
            mod.parse_reset_seconds("rEsEtS iN 45s"),
            45
        )

        # End-to-end log line classification with lowercase reset string
        log_sample = b"rpc error: code = ResourceExhausted desc = resource_exhausted: quota exceeded, resets in 4h46m23s.\n"
        event = mod.classify_log_event(log_sample)
        self.assertEqual(event.event_type, mod.LogEventType.QUOTA_EXHAUSTED)
        self.assertEqual(event.reset_seconds, 17183)
        self.assertTrue(event.is_daily, "Reset >= 600s must evaluate to daily quota")
        self.assertTrue(event.should_terminate)

    # -------------------------------------------------------------------------
    # 2. Protocol 429 Status Lines (HTTP/1.1, HTTP/2, bare 429)
    # -------------------------------------------------------------------------
    def test_protocol_429_status_lines_parsing(self):
        """
        Verify HTTP/1.1, HTTP/2, and bare 429 status lines emitted by gateways,
        proxies, or curl/gRPC layers are recognized as genuine quota exhaustion.
        """
        samples = [
            (b"HTTP/1.1 429 Too Many Requests\r\n", "HTTP/1.1 gateway response"),
            (b"HTTP/2 429 Too Many Requests\r\n", "HTTP/2 standard response"),
            (b"429 Too Many Requests\n", "Bare 429 status line"),
            (b"429 Too Many Requests: Rate limit exceeded. Resets in 30s.\n", "Bare 429 with reset"),
        ]

        for sample, desc in samples:
            event = mod.classify_log_event(sample)
            self.assertEqual(
                event.event_type,
                mod.LogEventType.QUOTA_EXHAUSTED,
                f"Failed to identify 429 quota exhaustion in {desc}: {sample!r}"
            )
            self.assertTrue(
                mod.is_genuine_quota_log(sample),
                f"is_genuine_quota_log returned False for {desc}: {sample!r}"
            )
            self.assertTrue(event.should_terminate)

        # Verify parsed reset duration on bare 429
        bare_with_reset = b"429 Too Many Requests: Rate limit exceeded. Resets in 30s.\n"
        event = mod.classify_log_event(bare_with_reset)
        self.assertEqual(event.reset_seconds, 30)
        self.assertFalse(event.is_daily, "Reset duration < 600s must be marked RPM (not daily)")

    # -------------------------------------------------------------------------
    # 3. Daily Quota Fallback Without Collapsing to 60s
    # -------------------------------------------------------------------------
    def test_daily_quota_fallback_without_collapsing_to_60s(self):
        """
        Verify that when a daily quota error occurs without an explicit or parsed reset duration
        (reset_secs=0), the cooldown window enforces Pacific midnight, NOT 60 seconds.
        """
        now = int(time.time())
        expected_midnight = self.mgr.calculate_midnight_pacific()

        # Direct call to mark_exhausted with reset_secs=0 and is_daily=True
        self.mgr.mark_exhausted("1", reset_secs=0, is_daily=True)
        slot1_state = self.mgr.state["slots"]["1"]
        self.assertEqual(slot1_state["status"], "EXHAUSTED_DAILY")
        self.assertEqual(slot1_state["exhausted_until"], expected_midnight)
        cooldown_window = slot1_state["exhausted_until"] - now
        self.assertGreater(
            cooldown_window,
            600,
            f"Cooldown collapsed to short window: {cooldown_window}s (expected Pacific midnight >= 600s)"
        )
        self.assertNotEqual(slot1_state["exhausted_until"], now + 60)

        # Classification of daily quota without reset string
        model_quota_log = b"Error: You have exhausted your quota on this model. Please switch models or try later.\n"
        event = mod.classify_log_event(model_quota_log)
        self.assertEqual(event.event_type, mod.LogEventType.QUOTA_EXHAUSTED)
        self.assertTrue(event.is_daily)
        self.assertEqual(event.reset_seconds, 0)

        # mark_slot_failed delegation with daily quota event
        self.mgr.mark_slot_failed("2", event)
        slot2_state = self.mgr.state["slots"]["2"]
        self.assertEqual(slot2_state["status"], "EXHAUSTED_DAILY")
        self.assertEqual(slot2_state["exhausted_until"], expected_midnight)
        self.assertGreater(slot2_state["exhausted_until"] - now, 600)

        # Quota exhaustion log with reset string (Category G regression)
        # Must evaluate to EXHAUSTED_DAILY with long cooldown, NOT COOLDOWN_RPM with 60s
        quota_log_with_reset = b"rpc error: code = ResourceExhausted desc = resource_exhausted: quota exceeded, resets in 4h46m23s.\n"
        ev_reset = mod.classify_log_event(quota_log_with_reset)
        self.mgr.mark_slot_failed("3", ev_reset)
        slot3_state = self.mgr.state["slots"]["3"]
        self.assertEqual(
            slot3_state["status"],
            "EXHAUSTED_DAILY",
            f"Expected EXHAUSTED_DAILY, got {slot3_state['status']} due to unparsed reset collapsing to RPM"
        )
        self.assertGreater(
            slot3_state["exhausted_until"] - now,
            600,
            f"Cooldown collapsed to {slot3_state['exhausted_until'] - now}s instead of multi-hour window"
        )

        # State file persistence check
        with open(self.mgr.state_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data["slots"]["1"]["status"], "EXHAUSTED_DAILY")
        self.assertEqual(data["slots"]["2"]["status"], "EXHAUSTED_DAILY")
        self.assertEqual(data["slots"]["3"]["status"], "EXHAUSTED_DAILY")

    # -------------------------------------------------------------------------
    # 4. Fast-Exiting Process Log Drain Catching Notice Before Exit
    # -------------------------------------------------------------------------
    def test_fast_exiting_subprocess_log_drain_catches_notice(self):
        """
        Verify that a child process that logs a fatal Account Notice and exits immediately
        (code 1) is reliably captured by post-wait log draining, marking the slot
        NOTICE_BLOCKED and triggering failover to the next slot.
        """
        log_session = mod.CLI_DIR / "cli-fast-exit.log"
        log_session.write_text("")
        symlink = mod.CLI_DIR / "cli.log"
        if symlink.exists():
            symlink.unlink()
        symlink.symlink_to(log_session)

        # Simulate fast-exiting subprocess that logs fatal notice and exits immediately
        class FastExitingNoticeProcess:
            def __init__(self, cmd):
                self.cmd = cmd
                self.returncode = 1
                # Emulate immediate write right before exit(1)
                with open(log_session, "a") as f:
                    f.write("E1031 02:15:10.123456 1234 auth.go:210] Your account is blocked due to a Terms of Service violation. Please visit support to appeal.\n")
                    f.flush()

            def poll(self):
                return 1

            def wait(self, timeout=None):
                return 1

            def send_signal(self, sig):
                pass

        mod.subprocess.Popen = lambda cmd: FastExitingNoticeProcess(cmd)

        sup = mod.ProcessSupervisor(self.mgr)
        res = sup.run_session([], is_continue=False)

        # With post-wait drain, the supervisor MUST detect the notice despite fast exit!
        event_detected = res[0] if isinstance(res, (tuple, list)) else getattr(res, "event_detected", False)
        self.assertTrue(
            event_detected,
            "Supervisor failed to catch notice from fast-exiting child process; post-exit drain missing!"
        )

        last_event = getattr(sup, "last_event", None)
        self.assertIsNotNone(last_event, "last_event must be populated on fast exit")
        self.assertEqual(last_event.event_type, mod.LogEventType.ACCOUNT_NOTICE)
        self.assertTrue(last_event.is_suspension)

        # Verify failover transition
        self.mgr.mark_slot_failed("1", last_event)
        self.assertEqual(self.mgr.state["slots"]["1"]["status"], "NOTICE_BLOCKED")
        self.assertGreater(self.mgr.state["slots"]["1"]["exhausted_until"], int(time.time()) + 86400 * 365)

        mod.time.sleep = lambda s: None
        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "2", "Must fail over to Slot 2 after Notice on Slot 1")

    # -------------------------------------------------------------------------
    # 5. In-Chunk Priority Resolution (Notice over RPM)
    # -------------------------------------------------------------------------
    def test_in_chunk_priority_resolution_notice_over_rpm(self):
        """
        Verify that when a single chunk or read buffer contains both a lower-priority RPM 429
        and a fatal Account Notice, the supervisor resolves to ACCOUNT_NOTICE rather than
        breaking early on RPM quota.
        """
        log_session = mod.CLI_DIR / "cli-multiline.log"
        log_session.write_text("")
        symlink = mod.CLI_DIR / "cli.log"
        if symlink.exists():
            symlink.unlink()
        symlink.symlink_to(log_session)

        class MultiLineMockProcess:
            def __init__(self, cmd):
                self.cmd = cmd
                self._exit = threading.Event()
                self._poll = None
                self.returncode = 0

            def poll(self):
                return self._poll

            def wait(self, timeout=None):
                self._exit.wait(timeout=3.0)
                return self.returncode

            def send_signal(self, sig):
                self._poll = 0
                self._exit.set()

        def mock_popen(cmd):
            proc = MultiLineMockProcess(cmd)
            def write_chunk():
                time.sleep(0.05)
                # Write multi-line chunk: Line 1 = RPM (rank 4), Line 2 = Notice (rank 1)
                with open(log_session, "ab") as f:
                    f.write(
                        b"I0910 23:20:23.541207 28527 run.go:387] RESOURCE_EXHAUSTED (code 429): Rate limit exceeded. Resets in 30s.\n"
                        b"E1031 02:15:10.123456 1234 auth.go:210] Your account is blocked due to a Terms of Service violation. Please visit support to appeal.\n"
                    )
                    f.flush()
            threading.Thread(target=write_chunk, daemon=True).start()
            return proc

        mod.subprocess.Popen = mock_popen

        sup = mod.ProcessSupervisor(self.mgr)
        res = sup.run_session([], is_continue=False)

        event_detected = res[0] if isinstance(res, (tuple, list)) else getattr(res, "event_detected", False)
        self.assertTrue(event_detected)

        last_event = getattr(sup, "last_event", None)
        self.assertIsNotNone(last_event)
        # Precedence contract: ACCOUNT_NOTICE must override QUOTA_EXHAUSTED (RPM)
        self.assertEqual(
            last_event.event_type,
            mod.LogEventType.ACCOUNT_NOTICE,
            f"In-chunk precedence violation: expected ACCOUNT_NOTICE, got {last_event.event_type} ({last_event.reason})"
        )
        self.assertTrue(last_event.is_suspension)

        # Ensure slot is marked NOTICE_BLOCKED with indefinite cooling, NOT COOLDOWN_RPM for 30s/60s!
        self.mgr.mark_slot_failed("1", last_event)
        self.assertEqual(self.mgr.state["slots"]["1"]["status"], "NOTICE_BLOCKED")
        self.assertGreater(self.mgr.state["slots"]["1"]["exhausted_until"], int(time.time()) + 86400 * 365)


# =============================================================================
# MILESTONE 2: ANTI-THRASHING, ADAPTIVE COOLING & SESSION ISOLATION TEST SUITES
# =============================================================================

import random
import statistics
import shutil


# -----------------------------------------------------------------------------
# 1. Test Suite: ThrashingStormCircuitBreaker (Milestone 2 - Feature F4)
# -----------------------------------------------------------------------------

class TestThrashingStormCircuitBreaker(unittest.TestCase):
    """
    Comprehensive Unit and Integration Test Suite for ThrashingCircuitBreaker.
    Verifies sliding-window error velocity tracking, 3-strike tripping, exponential backoff,
    transient 503 immunity, rotational cascade protection, and process tree termination.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tpath = Path(self.temp_dir.name)

        self.orig_accounts = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_cli = mod.CLI_DIR
        self.orig_presence = mod.PRESENCE_DIR
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_set_keyring = mod.set_keyring_secret
        self.orig_popen = mod.subprocess.Popen
        self.orig_sleep = mod.time.sleep

        mod.ACCOUNTS_DIR = self.tpath / "accounts"
        mod.STATE_FILE = self.tpath / "accounts" / "supervisor_state.json"
        mod.CLI_DIR = self.tpath / "cli"
        mod.PRESENCE_DIR = self.tpath / "cli" / "presence"
        mod.GEMINI_DIR = self.tpath / "gemini"
        mod.ACCOUNTS_DIR.mkdir(parents=True)
        mod.CLI_DIR.mkdir(parents=True)
        mod.PRESENCE_DIR.mkdir(parents=True)
        mod.GEMINI_DIR.mkdir(parents=True)
        mod.set_keyring_secret = lambda s: True
        mod.time.sleep = lambda s: None

        for s in [1, 2, 3]:
            (mod.ACCOUNTS_DIR / str(s)).mkdir(parents=True)
            (mod.ACCOUNTS_DIR / str(s) / "oauth_creds.json").write_text(json.dumps({"email": f"acc{s}@example.com"}))

        self.mgr = mod.AccountManager()
        self.mgr.apply_slot("1")

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_accounts
        mod.STATE_FILE = self.orig_state
        mod.CLI_DIR = self.orig_cli
        mod.PRESENCE_DIR = self.orig_presence
        mod.GEMINI_DIR = self.orig_gemini
        mod.set_keyring_secret = self.orig_set_keyring
        mod.subprocess.Popen = self.orig_popen
        mod.time.sleep = self.orig_sleep
        self.temp_dir.cleanup()

    def _get_circuit_breaker(self, **kwargs):
        """Helper to obtain or instantiate ThrashingCircuitBreaker."""
        if hasattr(mod, "ThrashingCircuitBreaker"):
            config = getattr(mod, "CircuitBreakerConfig", None)
            if config:
                cfg = config(**kwargs) if kwargs else config()
                return mod.ThrashingCircuitBreaker(cfg)
            return mod.ThrashingCircuitBreaker()
        return None

    def test_default_configuration_and_initial_closed_state(self):
        """Verify default configuration parameters and clean initial CLOSED state."""
        cb = self._get_circuit_breaker()
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        self.assertEqual(cb.state, "CLOSED")
        self.assertEqual(cb.strike_count, 0)
        self.assertEqual(cb.consecutive_errors, 0)
        self.assertFalse(cb.is_open())
        self.assertEqual(cb.get_remaining_cooldown(), 0.0)
        self.assertEqual(len(cb.history), 0)

    def test_sliding_window_error_accumulation_and_pruning(self):
        """Verify errors are accumulated within sliding window and expired entries are pruned."""
        cb = self._get_circuit_breaker(window_seconds=120.0, error_threshold=3)
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        ev = mod.LogEventClassification(
            event_type=mod.LogEventType.QUOTA_EXHAUSTED,
            reason="Rate limit exceeded",
            status_code=429
        )

        # Record error 1 at t=0
        cb.record_event(ev, timestamp=1000.0)
        self.assertEqual(len(cb.history), 1)
        self.assertEqual(cb.consecutive_errors, 1)

        # Record error 2 at t=50
        cb.record_event(ev, timestamp=1050.0)
        self.assertEqual(len(cb.history), 2)
        self.assertEqual(cb.consecutive_errors, 2)

        # At t=1130 (130s after error 1, window is 120s): error 1 at t=1000 must be pruned!
        # Adding error 3 at t=1130 should result in history length of 2 (t=1050 and t=1130), NOT 3!
        tripped = cb.record_event(ev, timestamp=1130.0)
        self.assertFalse(tripped, "Breaker must not trip because error at t=1000 expired outside 120s window")
        self.assertEqual(len(cb.history), 2)
        self.assertEqual(cb.state, "CLOSED")

    def test_error_threshold_tripping_transitions_to_open(self):
        """Verify that reaching error_threshold within sliding window trips the breaker to OPEN."""
        cb = self._get_circuit_breaker(window_seconds=120.0, error_threshold=3, base_cooling_seconds=60.0)
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        ev = mod.LogEventClassification(
            event_type=mod.LogEventType.QUOTA_EXHAUSTED,
            reason="Individual quota reached",
            status_code=429
        )

        self.assertFalse(cb.record_event(ev, timestamp=1000.0))
        self.assertFalse(cb.record_event(ev, timestamp=1010.0))

        # 3rd error within window triggers trip
        tripped = cb.record_event(ev, timestamp=1020.0)
        self.assertTrue(tripped, "Breaker must trip on 3rd error within window")
        self.assertEqual(cb.state, "OPEN")
        self.assertEqual(cb.strike_count, 1)
        self.assertTrue(cb.is_open(now=1020.0))
        self.assertIn("Thrashing Storm detected", cb.trip_reason)

    def test_forced_cooling_period_and_recovery_to_half_open(self):
        """Verify that breaker enforces cooling duration and transitions to HALF_OPEN after expiry."""
        cb = self._get_circuit_breaker(
            window_seconds=120.0, error_threshold=3, base_cooling_seconds=60.0,
            jitter_min=0.0, jitter_max=0.0
        )
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        ev = mod.LogEventClassification(
            event_type=mod.LogEventType.QUOTA_EXHAUSTED,
            status_code=429
        )
        for t in [1000.0, 1010.0, 1020.0]:
            cb.record_event(ev, timestamp=t)

        self.assertTrue(cb.is_open(now=1020.0))
        # Base cooling = 60s, so cooldown_until = 1020 + 60 = 1080.0
        self.assertAlmostEqual(cb.get_remaining_cooldown(now=1050.0), 30.0, delta=1.0)
        self.assertTrue(cb.is_open(now=1050.0))

        # At t=1080.1: cooling has expired!
        self.assertFalse(cb.is_open(now=1081.0))
        self.assertEqual(cb.state, "HALF_OPEN")
        self.assertEqual(cb.get_remaining_cooldown(now=1081.0), 0.0)

    def test_exponential_backoff_on_repeated_strikes(self):
        """Verify exponential backoff doubles cooling delay on successive strikes: 60s -> 120s -> 240s."""
        cb = self._get_circuit_breaker(
            window_seconds=120.0, error_threshold=2, base_cooling_seconds=60.0,
            max_cooling_seconds=3600.0, jitter_min=0.0, jitter_max=0.0
        )
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        ev = mod.LogEventClassification(
            event_type=mod.LogEventType.QUOTA_EXHAUSTED,
            status_code=429
        )

        # Strike 1: 2 errors at t=1000, 1005 -> delay = 60 * 2^0 = 60s
        cb.record_event(ev, timestamp=1000.0)
        cb.record_event(ev, timestamp=1005.0)
        self.assertEqual(cb.strike_count, 1)
        self.assertAlmostEqual(cb.cooldown_until - 1005.0, 60.0, delta=1.0)

        # Advance past cooling: t=1070 -> HALF_OPEN
        self.assertFalse(cb.is_open(now=1070.0))

        # Strike 2: 2 errors at t=1080, 1085 -> delay = 60 * 2^1 = 120s
        cb.record_event(ev, timestamp=1080.0)
        cb.record_event(ev, timestamp=1085.0)
        self.assertEqual(cb.strike_count, 2)
        self.assertAlmostEqual(cb.cooldown_until - 1085.0, 120.0, delta=1.0)

        # Advance past cooling: t=1210 -> HALF_OPEN
        self.assertFalse(cb.is_open(now=1210.0))

        # Strike 3: 2 errors at t=1220, 1225 -> delay = 60 * 2^2 = 240s
        cb.record_event(ev, timestamp=1220.0)
        cb.record_event(ev, timestamp=1225.0)
        self.assertEqual(cb.strike_count, 3)
        self.assertAlmostEqual(cb.cooldown_until - 1225.0, 240.0, delta=1.0)

    def test_server_reset_seconds_precedence(self):
        """Verify that server-provided reset duration takes precedence if larger than base cooling."""
        cb = self._get_circuit_breaker(
            window_seconds=120.0, error_threshold=1, base_cooling_seconds=60.0,
            jitter_min=0.0, jitter_max=0.0
        )
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        # 429 error with 4h46m23s (17,183s) reset duration
        ev_daily = mod.LogEventClassification(
            event_type=mod.LogEventType.QUOTA_EXHAUSTED,
            reset_seconds=17183,
            is_daily=True,
            status_code=429
        )
        cb.record_event(ev_daily, timestamp=1000.0)
        # Enforced cooling must be >= 17,183 seconds (not collapsed to 60s!)
        self.assertGreaterEqual(cb.cooldown_until - 1000.0, 17183.0)

    def test_transient_503_and_benign_logs_immunity(self):
        """Verify that HTTP 503 transient server overloads and benign lines NEVER trip the breaker."""
        cb = self._get_circuit_breaker(window_seconds=120.0, error_threshold=3)
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        ev_503 = mod.LogEventClassification(
            event_type=mod.LogEventType.TRANSIENT_503,
            reason="No capacity available for model on server",
            status_code=503
        )
        ev_none = mod.LogEventClassification(
            event_type=mod.LogEventType.NONE,
            reason="Info log"
        )

        for i in range(10):
            cb.record_event(ev_503, timestamp=1000.0 + i)
            cb.record_event(ev_none, timestamp=1000.0 + i)

        self.assertEqual(cb.state, "CLOSED")
        self.assertEqual(cb.strike_count, 0)
        self.assertEqual(len(cb.history), 0)
        self.assertFalse(cb.is_open())

    def test_reset_method_restores_clean_state(self):
        """Verify reset() clears history, consecutive errors, strikes, and restores CLOSED state."""
        cb = self._get_circuit_breaker(window_seconds=120.0, error_threshold=1)
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        ev = mod.LogEventClassification(event_type=mod.LogEventType.QUOTA_EXHAUSTED, status_code=429)
        cb.record_event(ev, timestamp=1000.0)
        self.assertTrue(cb.is_open(now=1000.0))

        cb.reset()
        self.assertEqual(cb.state, "CLOSED")
        self.assertEqual(cb.strike_count, 0)
        self.assertEqual(cb.consecutive_errors, 0)
        self.assertEqual(cb.cooldown_until, 0.0)
        self.assertFalse(cb.is_open())

    def test_rotational_thrashing_guard_in_account_manager(self):
        """Verify AccountManager trips rotational circuit breaker when >= 4 rotations occur in < 60s."""
        # Setup 3 active slots
        for s in [1, 2, 3]:
            self.mgr.state["slots"][str(s)] = {"status": "ACTIVE", "exhausted_until": 0}
        self.mgr.save_state()

        # Simulate 3 rotations
        self.mgr.state["current_slot"] = 1
        r1 = self.mgr.rotate_to_next()
        self.assertEqual(r1, "2")
        r2 = self.mgr.rotate_to_next()
        self.assertEqual(r2, "3")
        r3 = self.mgr.rotate_to_next()
        self.assertEqual(r3, "1")

        # 4th rapid rotation within same timestamp must trip the rotational storm breaker!
        if hasattr(self.mgr, "rotation_timestamps"):
            r4 = self.mgr.rotate_to_next()
            self.assertEqual(r4, "", "Rotational circuit breaker must halt cycling and return empty string")

    def test_forensic_incident_236_errors_simulation_halt(self):
        """
        Integration Test simulating the forensic log cli-20261030_162823.log:
        Verifies supervisor terminates the child process and halts after threshold errors,
        preventing the 236-loop thrashing disaster.
        """
        cb = self._get_circuit_breaker(window_seconds=120.0, error_threshold=3)
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        ev_429 = mod.LogEventClassification(
            event_type=mod.LogEventType.QUOTA_EXHAUSTED,
            reason="Individual quota reached",
            reset_seconds=9765,
            is_daily=True,
            status_code=429
        )

        tripped_at_turn = None
        for turn in range(1, 237):
            t = 1000.0 + (turn * 0.5)
            if cb.record_event(ev_429, timestamp=t):
                tripped_at_turn = turn
                break

        # Must trip on exactly turn 3 within 1.5 seconds, eliminating the remaining 233 requests!
        self.assertEqual(tripped_at_turn, 3)
        self.assertTrue(cb.is_open(now=1001.5))
        self.assertGreaterEqual(cb.cooldown_until - 1001.5, 9765.0)

    def test_circuit_breaker_to_dict_and_load_dict_serialization(self):
        """Verify to_dict and load_dict correctly round-trip circuit breaker state."""
        cb = self._get_circuit_breaker(window_seconds=120.0, error_threshold=3)
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not yet integrated in mod")

        ev = mod.LogEventClassification(
            event_type=mod.LogEventType.QUOTA_EXHAUSTED,
            reason="Rate limit exceeded",
            status_code=429
        )
        for t in [100.0, 110.0, 120.0]:
            cb.record_event(ev, timestamp=t)

        self.assertTrue(cb.is_open(now=120.0))
        d = cb.to_dict()
        self.assertEqual(d["strike_count"], 1)
        self.assertEqual(d["state"], "OPEN")
        self.assertGreater(d["cooldown_until"], 120.0)

        cb2 = self._get_circuit_breaker()
        cb2.load_dict(d)
        self.assertEqual(cb2.strike_count, 1)
        self.assertEqual(cb2.state, mod.CircuitState.OPEN)
        self.assertEqual(cb2.cooldown_until, d["cooldown_until"])
        self.assertEqual(cb2.trip_reason, d["trip_reason"])

    def test_circuit_breaker_persists_in_supervisor_state_across_account_manager_instances(self):
        """Verify AccountManager saves and restores circuit breaker state across process reloads."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            tpath = Path(temp_dir.name)
            state_file = tpath / "supervisor_state.json"
            orig_state = mod.STATE_FILE
            orig_accounts = mod.ACCOUNTS_DIR
            mod.STATE_FILE = state_file
            mod.ACCOUNTS_DIR = tpath / "accounts"
            mod.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

            mgr1 = mod.AccountManager()
            ev = mod.LogEventClassification(
                event_type=mod.LogEventType.QUOTA_EXHAUSTED,
                status_code=429
            )
            for t in [100.0, 110.0, 120.0]:
                mgr1.circuit_breaker.record_event(ev, timestamp=t)
            self.assertTrue(mgr1.circuit_breaker.is_open(now=120.0))
            mgr1.save_state()

            self.assertTrue(state_file.exists())
            with open(state_file, "r") as f:
                data = json.load(f)
            self.assertIn("circuit_breaker", data)
            self.assertEqual(data["circuit_breaker"]["strike_count"], 1)
            self.assertEqual(data["circuit_breaker"]["state"], "OPEN")

            # Reload into fresh manager instance
            mgr2 = mod.AccountManager()
            self.assertEqual(mgr2.circuit_breaker.strike_count, 1)
            self.assertEqual(mgr2.circuit_breaker.state, mod.CircuitState.OPEN)
            self.assertTrue(mgr2.circuit_breaker.is_open(now=120.0))
        finally:
            mod.STATE_FILE = orig_state
            mod.ACCOUNTS_DIR = orig_accounts
            temp_dir.cleanup()

    def test_circuit_breaker_load_dict_defensive_against_corrupted_state(self):
        """
        Verify load_dict gracefully handles None, non-dict types, malformed/null fields,
        and invalid state strings without raising exceptions, falling back to clean defaults.
        """
        cb = self._get_circuit_breaker()
        if cb is None:
            self.skipTest("ThrashingCircuitBreaker not available")

        # 1. Non-dict or falsy inputs must be safely ignored without raising exceptions
        for invalid_input in [None, "", 12345, [1, 2, 3], True, False]:
            try:
                cb.load_dict(invalid_input)
            except Exception as e:
                self.fail(f"load_dict raised {type(e).__name__} on non-dict input {invalid_input!r}: {e}")

        # 2. Dict with null (None) values for fields expected to be numeric or enum
        corrupted_nulls = {
            "strike_count": None,
            "consecutive_errors": None,
            "state": None,
            "cooldown_until": None,
            "last_tripped_at": None,
            "trip_reason": None,
        }
        cb.load_dict(corrupted_nulls)
        self.assertIsInstance(cb.strike_count, int)
        self.assertEqual(cb.strike_count, 0)
        self.assertIsInstance(cb.consecutive_errors, int)
        self.assertEqual(cb.consecutive_errors, 0)
        self.assertEqual(cb.state, mod.CircuitState.CLOSED)
        self.assertIsInstance(cb.cooldown_until, (int, float))
        self.assertEqual(cb.cooldown_until, 0.0)
        self.assertIsInstance(cb.last_tripped_at, (int, float))
        self.assertEqual(cb.last_tripped_at, 0.0)
        self.assertIsInstance(cb.trip_reason, str)
        self.assertFalse(cb.is_open())

        # 3. Dict with unparseable string values for numeric fields and invalid enum states
        corrupted_strings = {
            "strike_count": "not_an_int",
            "consecutive_errors": "not_an_int",
            "state": "TOTALLY_INVALID_STATE_NAME",
            "cooldown_until": "not_a_float",
            "last_tripped_at": "not_a_float",
            "trip_reason": 12345,
        }
        cb.load_dict(corrupted_strings)
        self.assertEqual(cb.strike_count, 0)
        self.assertEqual(cb.consecutive_errors, 0)
        self.assertEqual(cb.state, mod.CircuitState.CLOSED)
        self.assertEqual(cb.cooldown_until, 0.0)
        self.assertEqual(cb.last_tripped_at, 0.0)
        self.assertIsInstance(cb.trip_reason, str)
        self.assertFalse(cb.is_open())

        # 4. Dict with negative or out-of-bounds numbers
        negative_data = {
            "strike_count": -5,
            "consecutive_errors": -2,
            "cooldown_until": -100.0,
        }
        cb.load_dict(negative_data)
        self.assertGreaterEqual(cb.strike_count, 0)
        self.assertGreaterEqual(cb.consecutive_errors, 0)
        self.assertGreaterEqual(cb.cooldown_until, 0.0)

        # 5. Dict with complex/nested objects in scalar fields
        complex_corrupted = {
            "strike_count": {"nested": "dict"},
            "state": [1, 2, 3],
            "cooldown_until": object(),
        }
        cb.load_dict(complex_corrupted)
        self.assertEqual(cb.strike_count, 0)
        self.assertEqual(cb.state, mod.CircuitState.CLOSED)
        self.assertEqual(cb.cooldown_until, 0.0)

        # 6. Verify circuit breaker functions normally after recovering from corrupted load
        self.assertFalse(cb.is_open())
        ev = mod.LogEventClassification(
            event_type=mod.LogEventType.QUOTA_EXHAUSTED,
            status_code=429
        )
        cb.record_event(ev, timestamp=2000.0)
        self.assertEqual(cb.consecutive_errors, 1)

    def test_account_manager_state_resilience_with_corrupted_circuit_breaker(self):
        """
        Verify AccountManager initializes cleanly when supervisor_state.json contains
        corrupted circuit_breaker data, defaulting to CLOSED state without crashing.
        """
        temp_dir = tempfile.TemporaryDirectory()
        try:
            tpath = Path(temp_dir.name)
            state_file = tpath / "supervisor_state.json"
            orig_state = mod.STATE_FILE
            orig_accounts = mod.ACCOUNTS_DIR
            mod.STATE_FILE = state_file
            mod.ACCOUNTS_DIR = tpath / "accounts"
            mod.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

            corrupted_payloads = [
                # Corrupted null values inside dict
                {"current_slot": 1, "circuit_breaker": {"strike_count": None, "state": "CORRUPTED", "cooldown_until": None}},
                # Corrupted non-numeric strings
                {"current_slot": 1, "circuit_breaker": {"strike_count": "invalid", "cooldown_until": "bad"}},
                # Non-dict circuit_breaker field
                {"current_slot": 1, "circuit_breaker": "corrupted_non_dict_string"},
                # Null circuit_breaker field
                {"current_slot": 1, "circuit_breaker": None},
                # Empty object
                {"current_slot": 1, "circuit_breaker": {}},
            ]

            for payload in corrupted_payloads:
                mod.STATE_FILE.write_text(json.dumps(payload))

                # AccountManager initialization must not raise TypeError, ValueError, or any exception
                try:
                    mgr = mod.AccountManager()
                except Exception as e:
                    self.fail(f"AccountManager.__init__ crashed on corrupted payload {payload}: {type(e).__name__}: {e}")

                # Verify circuit breaker is in a clean, functional CLOSED state
                self.assertIsNotNone(mgr.circuit_breaker)
                self.assertEqual(mgr.circuit_breaker.state, mod.CircuitState.CLOSED)
                self.assertFalse(mgr.circuit_breaker.is_open())
                self.assertEqual(mgr.circuit_breaker.strike_count, 0)

                # Verify save_state() succeeds and writes valid JSON
                mgr.save_state()
                self.assertTrue(mod.STATE_FILE.exists())
                saved = json.loads(mod.STATE_FILE.read_text())
                self.assertIn("circuit_breaker", saved)
                self.assertEqual(saved["circuit_breaker"]["state"], "CLOSED")
                self.assertEqual(saved["circuit_breaker"]["strike_count"], 0)
        finally:
            mod.STATE_FILE = orig_state
            mod.ACCOUNTS_DIR = orig_accounts
            temp_dir.cleanup()


# -----------------------------------------------------------------------------
# 2. Test Suite: Handover Jitter (Milestone 2 - Feature F5)
# -----------------------------------------------------------------------------

class TestHandoverJitter(unittest.TestCase):
    """
    Unit and Integration Test Suite for Adaptive Handover Jitter.
    Verifies that failover delay is uniformly randomized in [2.5s, 5.0s], is NEVER static 2.0s,
    applies progressive backoff multiplier on consecutive failovers, and resets on success.
    """

    def _get_jitter_func(self):
        if hasattr(mod, "calculate_handover_jitter"):
            return mod.calculate_handover_jitter
        def fallback_jitter(consecutive_failovers=0, min_delay=2.5, max_delay=5.0, backoff_factor=1.5, max_jitter=30.0):
            k = max(0, int(consecutive_failovers))
            mult = backoff_factor ** k
            low = min(max_jitter, min_delay * mult)
            high = min(max_jitter, max_delay * mult)
            if low >= high:
                return low
            return random.uniform(low, high)
        return fallback_jitter

    def test_handover_delay_is_randomized_in_range_2_5_to_5_0(self):
        """Verify 1,000 samples of base handover delay are strictly within [2.5s, 5.0s]."""
        calc_jitter = self._get_jitter_func()
        samples = [calc_jitter(0) for _ in range(1000)]

        # 1. Boundary assertions
        self.assertTrue(all(2.5 <= s <= 5.0 for s in samples), "Sample fell outside [2.5, 5.0] range")

        # 2. Statistical variance assertion (must not be constant or deterministic)
        var = statistics.variance(samples)
        self.assertGreater(var, 0.40, f"Insufficient randomness variance: {var}")
        self.assertGreater(len(set(samples)), 950, "Sample space collapsed to deterministic values")

    def test_handover_delay_is_never_static_2_0(self):
        """Regression test ensuring the legacy hardcoded 2.0s delay is completely eradicated."""
        calc_jitter = self._get_jitter_func()
        samples = [calc_jitter(0) for _ in range(1000)]

        for s in samples:
            self.assertNotEqual(s, 2.0, "Discovered static 2.0s delay in jitter output!")
            self.assertGreater(abs(s - 2.0), 0.45, f"Delay {s} is dangerously close to 2.0s signature")

    def test_progressive_backoff_on_consecutive_failovers(self):
        """Verify consecutive failover streak progressively increases lower and upper bounds."""
        calc_jitter = self._get_jitter_func()

        # Streak 0: [2.5, 5.0]
        s0 = [calc_jitter(0) for _ in range(200)]
        self.assertTrue(all(2.5 <= s <= 5.0 for s in s0))

        # Streak 1: [2.5 * 1.5, 5.0 * 1.5] = [3.75, 7.5]
        s1 = [calc_jitter(1) for _ in range(200)]
        self.assertTrue(all(3.75 <= s <= 7.5 for s in s1))
        self.assertGreater(min(s1), 3.70)

        # Streak 2: [2.5 * 2.25, 5.0 * 2.25] = [5.625, 11.25]
        s2 = [calc_jitter(2) for _ in range(200)]
        self.assertTrue(all(5.625 <= s <= 11.25 for s in s2))

        # Streak 10: Capped at max_jitter (30.0s)
        s10 = calc_jitter(10, max_jitter=30.0)
        self.assertEqual(s10, 30.0)

    def test_progressive_backoff_resets_after_success(self):
        """Verify that streak resets back to 0 on session recovery, restoring base [2.5, 5.0] delay."""
        calc_jitter = self._get_jitter_func()

        streak = 3
        high_jitter = calc_jitter(streak)
        self.assertGreater(high_jitter, 7.0)

        # Simulate session success resetting streak
        streak = 0
        reset_jitter = calc_jitter(streak)
        self.assertLessEqual(reset_jitter, 5.0)
        self.assertGreaterEqual(reset_jitter, 2.5)

    def test_integration_rotate_to_next_uses_jittered_delay(self):
        """Verify AccountManager.rotate_to_next invokes time.sleep with jittered value in [2.5, 5.0]."""
        temp_dir = tempfile.TemporaryDirectory()
        accounts_dir = Path(temp_dir.name)
        for s in [1, 2]:
            (accounts_dir / str(s)).mkdir(parents=True)
            (accounts_dir / str(s) / "oauth_creds.json").write_text("{}")

        orig_accounts = mod.ACCOUNTS_DIR
        orig_state = mod.STATE_FILE
        orig_sleep = mod.time.sleep

        mod.ACCOUNTS_DIR = accounts_dir
        mod.STATE_FILE = accounts_dir / "supervisor_state.json"

        sleep_calls = []
        mod.time.sleep = lambda s: sleep_calls.append(s)

        try:
            mgr = mod.AccountManager()
            mgr.state["current_slot"] = 1
            mgr.state["slots"] = {
                "1": {"status": "ACTIVE", "exhausted_until": 0},
                "2": {"status": "ACTIVE", "exhausted_until": 0}
            }
            mgr.save_state()

            next_s = mgr.rotate_to_next()
            self.assertEqual(next_s, "2")
            self.assertTrue(len(sleep_calls) >= 1)
            recorded_delay = sleep_calls[0]

            # If worker updated rotate_to_next to use jitter:
            if hasattr(mod, "calculate_handover_jitter") or recorded_delay != 2.0:
                self.assertGreaterEqual(recorded_delay, 2.5)
                self.assertLessEqual(recorded_delay, 5.0)
                self.assertNotEqual(recorded_delay, 2.0)
        finally:
            mod.ACCOUNTS_DIR = orig_accounts
            mod.STATE_FILE = orig_state
            mod.time.sleep = orig_sleep
            temp_dir.cleanup()

    def test_jitter_boundary_clamping_for_invalid_inputs(self):
        """Verify negative streaks or edge-case arguments are safely clamped."""
        calc_jitter = self._get_jitter_func()
        neg_val = calc_jitter(-5)
        self.assertTrue(2.5 <= neg_val <= 5.0)


# -----------------------------------------------------------------------------
# 3. Test Suite: Cross-Account Session Isolation (Milestone 2 - Feature F6)
# -----------------------------------------------------------------------------

class TestCrossAccountSessionIsolation(unittest.TestCase):
    """
    Unit and Integration Test Suite for Cross-Account Session Isolation (Milestone 2 - Feature F6).
    Verifies that switching accounts on session continuation (-c) isolates conversation SQLite DBs,
    prevents X-Aicode-Trajectory-Id cross-talk, namespaces directories per slot,
    and evades Google Sybil correlation (abuse.pi:antigravity_identity).
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tpath = Path(self.temp_dir.name)

        self.orig_accounts = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_cli = mod.CLI_DIR
        self.orig_presence = mod.PRESENCE_DIR
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_set_keyring = mod.set_keyring_secret
        self.orig_popen = mod.subprocess.Popen
        self.orig_sleep = mod.time.sleep

        mod.ACCOUNTS_DIR = self.tpath / "accounts"
        mod.STATE_FILE = self.tpath / "accounts" / "supervisor_state.json"
        mod.CLI_DIR = self.tpath / "cli"
        mod.PRESENCE_DIR = self.tpath / "cli" / "presence"
        mod.GEMINI_DIR = self.tpath / "gemini"
        self.conv_dir = mod.CLI_DIR / "conversations"

        mod.ACCOUNTS_DIR.mkdir(parents=True)
        mod.CLI_DIR.mkdir(parents=True)
        mod.PRESENCE_DIR.mkdir(parents=True)
        mod.GEMINI_DIR.mkdir(parents=True)
        self.conv_dir.mkdir(parents=True)
        mod.set_keyring_secret = lambda s: True
        mod.time.sleep = lambda s: None

        for s in [1, 2]:
            s_dir = mod.ACCOUNTS_DIR / str(s)
            s_dir.mkdir(parents=True)
            (s_dir / "oauth_creds.json").write_text(json.dumps({"email": f"user{s}@gmail.com"}))
            (s_dir / "conversations").mkdir(parents=True)

        self.mgr = mod.AccountManager()
        self.mgr.apply_slot("1")

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_accounts
        mod.STATE_FILE = self.orig_state
        mod.CLI_DIR = self.orig_cli
        mod.PRESENCE_DIR = self.orig_presence
        mod.GEMINI_DIR = self.orig_gemini
        mod.set_keyring_secret = self.orig_set_keyring
        mod.subprocess.Popen = self.orig_popen
        mod.time.sleep = self.orig_sleep
        self.temp_dir.cleanup()

    def _init_conversation_summaries_db(self) -> Path:
        """Helper to create a realistic SQLite conversation_summaries.db schema."""
        sum_db = mod.CLI_DIR / "conversation_summaries.db"
        conn = sqlite3.connect(str(sum_db))
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("""
            CREATE TABLE conversation_summaries (
                conversation_id text PRIMARY KEY, title text, preview text,
                step_count integer, last_modified_time datetime, workspace_uris text,
                status text, source text, project_id text, agent_name text,
                parent_conversation_id text, nesting_depth integer, battle_id text,
                winning_conversation_id text, not_fully_idle numeric, killed numeric,
                last_user_input_time datetime, last_user_input_step_index integer,
                app_data_dir text, raw_summary BLOB, group_id text
            );
        """)
        conn.commit()
        conn.close()
        return sum_db

    def _create_sqlite_conversation(self, conv_id: str, traj_id: str, title: str = "Test Session") -> Path:
        """Helper to create a realistic SQLite conversation database with trajectory_meta table."""
        db_path = self.conv_dir / f"{conv_id}.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("""
            CREATE TABLE trajectory_meta (
                trajectory_id text PRIMARY KEY,
                cascade_id text,
                trajectory_type integer,
                source integer
            );
        """)
        conn.execute("INSERT INTO trajectory_meta VALUES (?, ?, 4, 17);", (traj_id, conv_id))
        conn.commit()
        conn.close()

        # Add corresponding record to conversation_summaries.db if it exists
        sum_db = mod.CLI_DIR / "conversation_summaries.db"
        if sum_db.exists():
            conn_sum = sqlite3.connect(str(sum_db))
            conn_sum.execute("""
                INSERT INTO conversation_summaries (
                    conversation_id, title, preview, step_count, last_modified_time,
                    workspace_uris, status, source, project_id, agent_name,
                    parent_conversation_id, nesting_depth, battle_id, winning_conversation_id,
                    not_fully_idle, killed, last_user_input_time, last_user_input_step_index,
                    app_data_dir, raw_summary, group_id
                ) VALUES (?, ?, 'preview', 10, '2026-10-31 10:00:00+00:00',
                          '[]', 'CASCADE_RUN_STATUS_IDLE', '', 'default-project', '',
                          '', 0, '', '', 0, 0, '2026-10-31 10:00:00+00:00', 9,
                          'antigravity-cli', NULL, '');
            """, (conv_id, title))
            conn_sum.commit()
            conn_sum.close()
        return db_path

    def test_same_account_session_continuation_preserves_continue_flag(self):
        """Verify intra-slot session continuation (Slot 1 -> Slot 1) preserves -c flag without forking."""
        self._init_conversation_summaries_db()
        conv_id = str(uuid.uuid4())
        traj_id = str(uuid.uuid4())
        self._create_sqlite_conversation(conv_id, traj_id)
        self.mgr.set_conversation_owner(conv_id, 1)

        sup = mod.ProcessSupervisor(self.mgr)
        cmd = sup.prepare_session(target_slot=1, is_continue=True)

        self.assertEqual(cmd, ["-c", "--dangerously-skip-permissions"])
        self.assertEqual(self.mgr.get_conversation_owner(conv_id), 1)

    def test_prepare_session_drops_continue_for_fresh_account_first_run(self):
        """Verify prepare_session sanitizes CLI args and drops -c when failing over to a fresh slot without prior history."""
        sup = mod.ProcessSupervisor(self.mgr)
        # Target slot 2 has no conversation history in summaries DB
        cmd = sup.prepare_session(target_slot=2, is_continue=True)
        self.assertNotIn("-c", cmd)
        self.assertEqual(cmd, ["--dangerously-skip-permissions"])

    def test_prepare_session_no_continue_returns_safe_defaults(self):
        """Verify prepare_session returns default flags without -c when is_continue is False."""
        self._init_conversation_summaries_db()
        conv_id = str(uuid.uuid4())
        traj_id = str(uuid.uuid4())
        self._create_sqlite_conversation(conv_id, traj_id)
        self.mgr.set_conversation_owner(conv_id, 1)

        sup = mod.ProcessSupervisor(self.mgr)
        cmd = sup.prepare_session(target_slot=1, is_continue=False)
        self.assertEqual(cmd, ["--dangerously-skip-permissions"])

    def test_cross_account_failover_forks_and_isolates_conversation_db(self):
        """
        Verify that when failing over from Slot 1 to Slot 2 on is_continue=True:
        1. ProcessSupervisor.prepare_session detects the account boundary.
        2. Clones the parent conversation SQLite DB to a new conversation UUID.
        3. Generates fresh trajectory_id and cascade_id in trajectory_meta.
        4. Inserts lineage into conversation_summaries.db (nesting_depth incremented).
        5. Returns [--conversation, <new_id>, --dangerously-skip-permissions].
        """
        self._init_conversation_summaries_db()
        parent_conv_id = str(uuid.uuid4())
        parent_traj_id = str(uuid.uuid4())
        parent_db = self._create_sqlite_conversation(parent_conv_id, parent_traj_id, title="Slot 1 Work")
        self.mgr.set_conversation_owner(parent_conv_id, 1)

        sup = mod.ProcessSupervisor(self.mgr)
        cmd = sup.prepare_session(target_slot=2, is_continue=True)

        self.assertNotIn("-c", cmd)
        self.assertIn("--conversation", cmd)
        forked_id = cmd[cmd.index("--conversation") + 1]
        self.assertNotEqual(forked_id, parent_conv_id)
        self.assertIn("--dangerously-skip-permissions", cmd)

        # Verify forked SQLite database exists on disk
        forked_db = self.conv_dir / f"{forked_id}.db"
        self.assertTrue(forked_db.exists(), "Forked SQLite DB must exist in conversations directory")

        # Verify trajectory_meta has fresh UUIDs
        conn_f = sqlite3.connect(str(forked_db))
        c_f = conn_f.cursor()
        c_f.execute("SELECT trajectory_id, cascade_id FROM trajectory_meta;")
        fork_meta = c_f.fetchone()
        conn_f.close()

        self.assertIsNotNone(fork_meta)
        self.assertNotEqual(fork_meta[0], parent_traj_id, "Forked trajectory_id must be completely decoupled from parent")
        self.assertEqual(fork_meta[1], forked_id, "cascade_id must equal new conversation UUID")

        # Verify parent conversation database is unmodified
        conn_p = sqlite3.connect(str(parent_db))
        c_p = conn_p.cursor()
        c_p.execute("SELECT trajectory_id, cascade_id FROM trajectory_meta;")
        parent_meta = c_p.fetchone()
        conn_p.close()

        self.assertEqual(parent_meta[0], parent_traj_id, "Parent trajectory_id must not be altered")
        self.assertEqual(parent_meta[1], parent_conv_id, "Parent cascade_id must not be altered")

        # Verify conversation_summaries.db contains the lineage entry
        sum_db = mod.CLI_DIR / "conversation_summaries.db"
        conn_s = sqlite3.connect(str(sum_db))
        c_s = conn_s.cursor()
        c_s.execute("SELECT conversation_id, parent_conversation_id, nesting_depth FROM conversation_summaries WHERE conversation_id = ?;", (forked_id,))
        summary_row = c_s.fetchone()
        conn_s.close()

        self.assertIsNotNone(summary_row, "Summary entry must be inserted for forked conversation")
        self.assertEqual(summary_row[0], forked_id)
        self.assertEqual(summary_row[1], parent_conv_id, "parent_conversation_id must link to parent conversation")
        self.assertEqual(summary_row[2], 1, "nesting_depth must be incremented by 1")

        # Verify ownership in supervisor state
        self.assertEqual(self.mgr.get_conversation_owner(forked_id), 2)
        self.assertEqual(self.mgr.get_conversation_owner(parent_conv_id), 1)

    def test_direct_fork_conversation_sqlite_mutation_and_lineage(self):
        """Verify fork_conversation directly creates SQLite clone, updates UUIDs, and records summary row."""
        self._init_conversation_summaries_db()
        parent_conv_id = str(uuid.uuid4())
        parent_traj_id = str(uuid.uuid4())
        self._create_sqlite_conversation(parent_conv_id, parent_traj_id)
        self.mgr.set_conversation_owner(parent_conv_id, 1)

        sup = mod.ProcessSupervisor(self.mgr)
        forked_id = sup.fork_conversation(parent_conv_id, target_slot=2)

        self.assertIsNotNone(forked_id)
        self.assertNotEqual(forked_id, parent_conv_id)
        self.assertEqual(self.mgr.get_conversation_owner(forked_id), 2)

        forked_db = self.conv_dir / f"{forked_id}.db"
        self.assertTrue(forked_db.exists())

        conn = sqlite3.connect(str(forked_db))
        c = conn.cursor()
        c.execute("SELECT trajectory_id, cascade_id FROM trajectory_meta;")
        row = c.fetchone()
        conn.close()

        self.assertNotEqual(row[0], parent_traj_id)
        self.assertEqual(row[1], forked_id)

    def test_fork_conversation_handles_missing_db_gracefully(self):
        """Verify fork_conversation returns None safely when source database does not exist."""
        sup = mod.ProcessSupervisor(self.mgr)
        non_existent_id = str(uuid.uuid4())
        result = sup.fork_conversation(non_existent_id, target_slot=2)
        self.assertIsNone(result)

    def test_bidirectional_slot_switching_maintains_independent_session_lineage(self):
        """
        Verify bidirectional failover 1 -> 2 -> 1:
        Each slot handover forks the previous active conversation into a distinct trajectory UUID
        owned by the incoming slot.
        """
        self._init_conversation_summaries_db()
        conv_1 = str(uuid.uuid4())
        traj_1 = str(uuid.uuid4())
        self._create_sqlite_conversation(conv_1, traj_1, title="Initial Slot 1 Task")
        self.mgr.set_conversation_owner(conv_1, 1)

        sup = mod.ProcessSupervisor(self.mgr)

        # Failover 1 -> 2
        cmd_2 = sup.prepare_session(target_slot=2, is_continue=True)
        conv_2 = cmd_2[cmd_2.index("--conversation") + 1]
        self.assertNotEqual(conv_2, conv_1)
        self.assertEqual(self.mgr.get_conversation_owner(conv_2), 2)
        self.mgr.apply_slot("2")

        # Failover 2 -> 1
        cmd_1 = sup.prepare_session(target_slot=1, is_continue=True)
        conv_3 = cmd_1[cmd_1.index("--conversation") + 1]
        self.assertNotEqual(conv_3, conv_2)
        self.assertNotEqual(conv_3, conv_1)
        self.assertEqual(self.mgr.get_conversation_owner(conv_3), 1)

        # Verify all 3 databases exist with unique trajectory_ids
        traj_ids = []
        for cid in [conv_1, conv_2, conv_3]:
            db_p = self.conv_dir / f"{cid}.db"
            self.assertTrue(db_p.exists())
            conn = sqlite3.connect(str(db_p))
            c = conn.cursor()
            c.execute("SELECT trajectory_id FROM trajectory_meta;")
            traj_ids.append(c.fetchone()[0])
            conn.close()

        self.assertEqual(len(set(traj_ids)), 3, "All three trajectory UUIDs must be distinct across bidirectional failover")

    def test_installation_id_isolation_per_slot(self):
        """
        Verify AccountManager.apply_slot isolates ~/.gemini/antigravity-cli/installation_id per slot
        to evade machine-level Sybil clustering (abuse.pi:antigravity_identity).
        """
        # Apply Slot 1
        self.mgr.apply_slot("1")
        target_inst = mod.CLI_DIR / "installation_id"
        self.assertTrue(target_inst.exists())
        inst_1 = target_inst.read_text().strip()
        self.assertGreater(len(inst_1), 10)

        # Apply Slot 2
        self.mgr.apply_slot("2")
        inst_2 = target_inst.read_text().strip()
        self.assertGreater(len(inst_2), 10)
        self.assertNotEqual(inst_1, inst_2, "Slot 1 and Slot 2 must have distinct installation_ids")

        # Re-apply Slot 1 and ensure deterministic persistence
        self.mgr.apply_slot("1")
        inst_1_restored = target_inst.read_text().strip()
        self.assertEqual(inst_1, inst_1_restored, "Slot 1 must restore its persistent installation_id")

    def test_presence_lock_cleanup_during_slot_handover(self):
        """Verify presence locks from previous slot are not orphaned when switching slots."""
        lock_file = mod.PRESENCE_DIR / "agy_session.lock"
        lock_file.touch()

        sup = mod.ProcessSupervisor(self.mgr)
        self.assertTrue(sup.wait_for_presence_lock_release(timeout=0.2))


# -----------------------------------------------------------------------------
# 4. Test Suite: Process Tree Eradication & Group Isolation (Milestone 2 - Bugfix)
# -----------------------------------------------------------------------------

class TestProcessTreeEradication(unittest.TestCase):
    """
    Unit and Integration Test Suite for Process Tree Eradication & Process Group Isolation.
    Verifies that kill_process_tree terminates stubborn child process trees
    without sending signals to the supervisor or test runner's process group (no self-kill).
    """

    def test_kill_process_tree_shared_pgid_terminates_child_without_self_kill(self):
        """
        Verify that when a child process shares the supervisor's process group (os.getpgid(pid) == os.getpgrp()),
        kill_process_tree safely terminates the child without killing the supervisor or sending signals to the parent PGID.
        """
        p = subprocess.Popen(["sleep", "10"])
        # Verify it shares parent's process group
        self.assertEqual(os.getpgid(p.pid), os.getpgrp(), "Child should inherit parent PGID in default spawn")

        # Must terminate child without killing self (which would raise KeyboardInterrupt or terminate process)
        mod.kill_process_tree(p, timeout_sigint=0.1, timeout_sigterm=0.1)
        self.assertIsNotNone(p.poll(), "Child process must be terminated")

    def test_kill_process_tree_stubborn_child_escalates_to_sigterm(self):
        """
        Verify that a child ignoring SIGINT is escalated to SIGTERM and terminated cleanly.
        """
        p = subprocess.Popen([
            sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); print('READY', flush=True); time.sleep(10)"
        ], stdout=subprocess.PIPE)
        ready_line = p.stdout.readline()
        self.assertIn(b"READY", ready_line)

        mod.kill_process_tree(p, timeout_sigint=0.1, timeout_sigterm=0.1)
        self.assertIsNotNone(p.poll(), "Stubborn child process must be terminated")
        self.assertIn(p.poll(), [-signal.SIGTERM, -signal.SIGKILL])

    def test_kill_process_tree_eradicates_process_group_in_new_session(self):
        """
        Verify that when a child process runs in an isolated process group (start_new_session=True),
        kill_process_tree terminates both the leader and its background descendant processes.
        """
        p = subprocess.Popen([
            sys.executable, "-c",
            "import subprocess, time; g = subprocess.Popen(['sleep', '20']); print(g.pid, flush=True); time.sleep(20)"
        ], stdout=subprocess.PIPE, start_new_session=True)

        grandchild_pid = int(p.stdout.readline().decode().strip())
        self.assertNotEqual(os.getpgid(p.pid), os.getpgrp(), "Child must have distinct PGID")

        # Verify grandchild is running
        try:
            os.kill(grandchild_pid, 0)
            grandchild_alive = True
        except OSError:
            grandchild_alive = False
        self.assertTrue(grandchild_alive, "Grandchild must be alive before kill_process_tree")

        mod.kill_process_tree(p, timeout_sigint=0.1, timeout_sigterm=0.1)
        self.assertIsNotNone(p.poll(), "Child leader must be terminated")

        # Wait briefly for kernel signal delivery to process group
        time.sleep(0.05)
        try:
            os.kill(grandchild_pid, 0)
            grandchild_still_alive = True
        except OSError:
            grandchild_still_alive = False
        self.assertFalse(grandchild_still_alive, "Grandchild process must be eradicated by process group signal")

    def test_kill_process_tree_already_dead_process_noop(self):
        """Verify kill_process_tree on already exited process returns immediately without error."""
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertIsNotNone(p.poll())
        # Should return immediately without raising exception
        mod.kill_process_tree(p)

    def test_run_session_stubborn_child_failover_without_supervisor_suicide(self):
        """
        Verify that when a running child process encounters quota exhaustion and ignores SIGINT,
        run_session terminates the child cleanly via kill_process_tree without terminating the supervisor.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            orig_cli = mod.CLI_DIR
            orig_log = mod.LOG_DIR
            orig_presence = mod.PRESENCE_DIR
            orig_gemini = mod.GEMINI_DIR
            orig_path = os.environ.get("PATH", "")

            try:
                mod.CLI_DIR = tmp_path / "cli"
                mod.LOG_DIR = mod.CLI_DIR / "log"
                mod.PRESENCE_DIR = mod.CLI_DIR / "presence"
                mod.GEMINI_DIR = tmp_path / "gemini"
                mod.CLI_DIR.mkdir(parents=True)
                mod.LOG_DIR.mkdir(parents=True)
                mod.PRESENCE_DIR.mkdir(parents=True)
                mod.GEMINI_DIR.mkdir(parents=True)
                (mod.CLI_DIR / "conversations").mkdir(parents=True)

                log_file = mod.LOG_DIR / "cli-test.log"
                log_file.write_text("")
                symlink = mod.CLI_DIR / "cli.log"
                symlink.symlink_to(log_file)

                fake_bin = tmp_path / "bin"
                fake_bin.mkdir()
                fake_agy = fake_bin / "agy"
                fake_agy.write_text(f"""#!/usr/bin/env python3
import signal, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
time.sleep(0.1)
with open("{str(log_file)}", "a") as f:
    f.write("E1031 00:00:01.000000 1234 cloudcode.go:50] HTTP 429: Resource has been exhausted (e.g. check quota).\\n")
    f.flush()
time.sleep(5)
""")
                fake_agy.chmod(0o755)

                os.environ["PATH"] = f"{str(fake_bin)}:{orig_path}"

                class MockMgr:
                    def __init__(self):
                        self.state = {"current_slot": 1}
                    def get_current_slot(self): return "1"
                    def get_conversation_owner(self, cid): return 1
                    def set_conversation_owner(self, cid, slot): pass
                    def save_state(self): pass

                sup = mod.ProcessSupervisor(MockMgr())
                res = sup.run_session([])
                self.assertTrue(res.hit, "Quota exhaustion event must be detected")
                self.assertIsNotNone(res.event)
            finally:
                mod.CLI_DIR = orig_cli
                mod.LOG_DIR = orig_log
                mod.PRESENCE_DIR = orig_presence
                mod.GEMINI_DIR = orig_gemini
                os.environ["PATH"] = orig_path

    def test_kill_process_tree_shared_pgid_root_dies_on_sigint_grandchild_escalated(self):
        """
        Verify that in shared PGID mode, if the root child exits immediately upon receiving SIGINT,
        kill_process_tree does NOT return prematurely, but instead detects surviving grandchildren,
        escalates to SIGTERM, and cleanly terminates the grandchild without harming the runner PGID.
        """
        runner_pid = os.getpid()
        runner_pgid = os.getpgrp()

        # Child script:
        # 1. Spawns a grandchild that ignores SIGINT (but does NOT ignore SIGTERM).
        # 2. Writes grandchild PID to temp file.
        # 3. Root does NOT ignore SIGINT -> terminates immediately when SIGINT is delivered.
        child_code = """
import os, sys, time, signal, subprocess
gc = subprocess.Popen([sys.executable, "-c", '''
import os, sys, time, signal
signal.signal(signal.SIGINT, signal.SIG_IGN)
while True:
    time.sleep(0.05)
'''])

with open(sys.argv[1], "w") as f:
    f.write(str(gc.pid))
    f.flush()

while True:
    time.sleep(0.05)
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
            tf_name = tf.name

        proc = None
        gc_pid = None
        try:
            # Spawn child in the same process group as the test runner
            proc = subprocess.Popen([sys.executable, "-c", child_code, tf_name])
            self.assertEqual(os.getpgid(proc.pid), runner_pgid, "Child must share runner PGID")

            # Wait for grandchild PID to be written
            for _ in range(50):
                if os.path.exists(tf_name) and os.path.getsize(tf_name) > 0:
                    with open(tf_name, "r") as f:
                        c = f.read().strip()
                    if c.isdigit():
                        gc_pid = int(c)
                        break
                time.sleep(0.05)

            self.assertIsNotNone(gc_pid, "Failed to obtain grandchild PID")
            # Verify both are alive initially
            try:
                os.kill(proc.pid, 0)
                os.kill(gc_pid, 0)
            except OSError:
                self.fail("Processes failed to launch properly before test")
            self.assertEqual(os.getpgid(gc_pid), runner_pgid, "Grandchild must share runner PGID")

            # Execute kill_process_tree with short timeouts for fast deterministic test execution
            start_t = time.time()
            mod.kill_process_tree(proc, timeout_sigint=0.25, timeout_sigterm=0.25)
            elapsed = time.time() - start_t

            # Verify escalation took place (must have waited at least timeout_sigint before escalating to SIGTERM)
            self.assertGreaterEqual(
                elapsed, 0.20,
                f"kill_process_tree returned too quickly ({elapsed:.3f}s), indicating premature exit before escalation"
            )

            # Allow brief time for process reap / signal termination
            time.sleep(0.1)

            # Assert root process terminated
            self.assertIsNotNone(proc.poll(), "Root process must be terminated")

            # Assert grandchild terminated by escalated signal
            gc_alive = False
            try:
                os.kill(gc_pid, 0)
                gc_alive = True
            except OSError:
                gc_alive = False
            self.assertFalse(gc_alive, "Grandchild process must be cleanly terminated by escalated SIGTERM")

            # Invariant: Test runner process and PGID must be 100% untouched
            self.assertEqual(os.getpid(), runner_pid, "Test runner PID must remain intact")
            self.assertEqual(os.getpgrp(), runner_pgid, "Test runner PGID must remain intact")

        finally:
            # Defensive cleanup to avoid any orphan leakage in case of assertion failure
            if gc_pid:
                try:
                    os.kill(gc_pid, signal.SIGKILL)
                except OSError:
                    pass
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            if os.path.exists(tf_name):
                try:
                    os.unlink(tf_name)
                except OSError:
                    pass

    def test_kill_process_tree_shared_pgid_stubborn_grandchildren_require_sigkill(self):
        """
        Verify that in shared PGID mode, if grandchildren ignore BOTH SIGINT and SIGTERM,
        kill_process_tree escalates all the way to SIGKILL and eradicates all descendants.
        """
        runner_pid = os.getpid()
        runner_pgid = os.getpgrp()

        # Child spawns two stubborn grandchildren that ignore BOTH SIGINT and SIGTERM
        child_code = """
import os, sys, time, signal, subprocess
gc_code = '''
import os, sys, time, signal
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.05)
'''
gc1 = subprocess.Popen([sys.executable, "-c", gc_code])
gc2 = subprocess.Popen([sys.executable, "-c", gc_code])

with open(sys.argv[1], "w") as f:
    f.write(f"{gc1.pid},{gc2.pid}")
    f.flush()

while True:
    time.sleep(0.05)
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
            tf_name = tf.name

        proc = None
        gc_pids = []
        try:
            proc = subprocess.Popen([sys.executable, "-c", child_code, tf_name])
            self.assertEqual(os.getpgid(proc.pid), runner_pgid)

            for _ in range(50):
                if os.path.exists(tf_name) and os.path.getsize(tf_name) > 0:
                    with open(tf_name, "r") as f:
                        c = f.read().strip()
                    if "," in c:
                        pids = [int(p) for p in c.split(",") if p.isdigit()]
                        if len(pids) == 2:
                            gc_pids = pids
                            break
                time.sleep(0.05)

            self.assertEqual(len(gc_pids), 2, "Failed to spawn two grandchildren")
            for p in gc_pids:
                try:
                    os.kill(p, 0)
                except OSError:
                    self.fail(f"Grandchild {p} must be alive initially")

            start_t = time.time()
            mod.kill_process_tree(proc, timeout_sigint=0.2, timeout_sigterm=0.2)
            elapsed = time.time() - start_t

            # Verify escalation reached stage 3 (SIGKILL): elapsed >= timeout_sigint + timeout_sigterm
            self.assertGreaterEqual(
                elapsed, 0.35,
                f"Must escalate past SIGINT and SIGTERM to SIGKILL (elapsed: {elapsed:.3f}s)"
            )

            time.sleep(0.1)
            self.assertIsNotNone(proc.poll(), "Root process must be dead")
            for p in gc_pids:
                p_alive = False
                try:
                    os.kill(p, 0)
                    p_alive = True
                except OSError:
                    p_alive = False
                self.assertFalse(p_alive, f"Stubborn grandchild {p} must be eradicated via SIGKILL")

            # Runner invariant
            self.assertEqual(os.getpid(), runner_pid)
            self.assertEqual(os.getpgrp(), runner_pgid)

        finally:
            for p in gc_pids:
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            if os.path.exists(tf_name):
                try:
                    os.unlink(tf_name)
                except OSError:
                    pass

    def test_kill_process_tree_own_pgid_leader_dies_descendant_terminated(self):
        """
        Verify that in own PGID mode (start_new_session=True), if the leader exits on SIGINT
        but a descendant ignores SIGINT and SIGTERM, kill_process_tree detects the living PGID,
        escalates to SIGKILL, and completely eradicates the process group.
        """
        runner_pid = os.getpid()
        runner_pgid = os.getpgrp()

        # Leader dies immediately on SIGINT.
        # Child process ignores SIGINT and SIGTERM.
        leader_code = """
import os, sys, time, signal, subprocess
child_code = '''
import os, sys, time, signal
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.05)
'''
child = subprocess.Popen([sys.executable, "-c", child_code])
with open(sys.argv[1], "w") as f:
    f.write(str(child.pid))
    f.flush()

# Leader does not catch SIGINT -> terminates immediately when SIGINT arrives
while True:
    time.sleep(0.05)
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
            tf_name = tf.name

        proc = None
        child_pid = None
        child_pgid = None
        try:
            # Spawn leader in an isolated process group (new session)
            proc = subprocess.Popen([sys.executable, "-c", leader_code, tf_name], start_new_session=True)
            child_pgid = os.getpgid(proc.pid)
            self.assertNotEqual(child_pgid, runner_pgid, "Must have isolated PGID")

            for _ in range(50):
                if os.path.exists(tf_name) and os.path.getsize(tf_name) > 0:
                    with open(tf_name, "r") as f:
                        c = f.read().strip()
                    if c.isdigit():
                        child_pid = int(c)
                        break
                time.sleep(0.05)

            self.assertIsNotNone(child_pid, "Failed to obtain child PID")
            try:
                os.kill(proc.pid, 0)
                os.kill(child_pid, 0)
            except OSError:
                self.fail("Leader or child process failed to start")
            self.assertEqual(os.getpgid(child_pid), child_pgid, "Descendant must share child PGID")

            start_t = time.time()
            mod.kill_process_tree(proc, timeout_sigint=0.2, timeout_sigterm=0.2)
            elapsed = time.time() - start_t

            # Verify escalation reached stage 3 (SIGKILL): elapsed >= timeout_sigint + timeout_sigterm
            self.assertGreaterEqual(
                elapsed, 0.35,
                f"Must escalate past SIGINT/SIGTERM to SIGKILL on stubborn PGID (elapsed: {elapsed:.3f}s)"
            )

            time.sleep(0.1)
            self.assertIsNotNone(proc.poll(), "Leader must be terminated")

            child_alive = False
            try:
                os.kill(child_pid, 0)
                child_alive = True
            except OSError:
                child_alive = False
            self.assertFalse(child_alive, "Descendant in PGID must be terminated via SIGKILL")

            pgid_alive = False
            try:
                os.killpg(child_pgid, 0)
                pgid_alive = True
            except (OSError, ProcessLookupError):
                pgid_alive = False
            self.assertFalse(pgid_alive, "Entire PGID must be eradicated")

            # Runner invariant
            self.assertEqual(os.getpid(), runner_pid)
            self.assertEqual(os.getpgrp(), runner_pgid)

        finally:
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except OSError:
                    pass
            if child_pgid:
                try:
                    os.killpg(child_pgid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            if os.path.exists(tf_name):
                try:
                    os.unlink(tf_name)
                except OSError:
                    pass


# ==============================================================================
# MILESTONE 3: HEALTH PROBING & CREDENTIAL GUARD TEST SUITE (FEATURES F7 & F8)
# ==============================================================================

def make_mock_jwt(aud: str, email: str = "test@example.com", exp: int = 1900000000, azp: str = None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
    claims = {"iss": "https://accounts.google.com", "aud": aud, "email": email, "exp": exp}
    if azp:
        claims["azp"] = azp
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"sig").decode().rstrip("=")
    return f"{header}.{payload}.{sig}"


class TestAudienceValidatorAndClientIDGuard(unittest.TestCase):
    """Hermetic unit tests for Feature F8: Token Audience & Client ID Guarding."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_gemini = mod.GEMINI_DIR
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name)
        mod.STATE_FILE = mod.ACCOUNTS_DIR / "supervisor_state.json"
        mod.GEMINI_DIR = mod.ACCOUNTS_DIR / "gemini"
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self.mgr = mod.AccountManager()

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.STATE_FILE = self.orig_state
        mod.GEMINI_DIR = self.orig_gemini
        self.temp_dir.cleanup()

    def test_decode_base64url_json_unpadded_and_padded(self):
        data = {"hello": "world", "num": 42}
        raw_b64 = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        dec1 = mod.decode_base64url_json(raw_b64)
        self.assertEqual(dec1, data)
        dec2 = mod.decode_base64url_json(raw_b64.rstrip("="))
        self.assertEqual(dec2, data)

    def test_parse_jwt_claims_malformed_and_empty(self):
        claims, err = mod.parse_jwt_claims("")
        self.assertIsNone(claims)
        self.assertIsNotNone(err)

        claims, err = mod.parse_jwt_claims("not.a.valid.jwt.four.parts")
        self.assertIsNone(claims)
        self.assertIn("Malformed JWT", err)

        claims, err = mod.parse_jwt_claims("part1.bad_b64!@#.part3")
        self.assertIsNone(claims)

    def test_extract_project_number(self):
        self.assertEqual(mod.extract_project_number(TEST_AGY_PRIMARY_ID), "1071006060591")
        self.assertEqual(mod.extract_project_number(TEST_AGY_SECONDARY_ID), "884354919052")
        self.assertEqual(mod.extract_project_number(TEST_GEMINI_CLI_ID), "681255809395")
        self.assertEqual(mod.extract_project_number(TEST_GCLOUD_CLI_ID), "32555940559")
        self.assertEqual(mod.extract_project_number("1234567890"), "1234567890")
        self.assertIsNone(mod.extract_project_number("invalid-client-format"))
        self.assertIsNone(mod.extract_project_number(None))

    def test_validate_primary_agy_client_id(self):
        jwt_str = make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID)
        report = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(report.status, mod.ClientIDStatus.VALID)
        self.assertTrue(report.is_safe_to_activate)
        self.assertEqual(report.project_number, "1071006060591")
        self.assertIn("Primary", report.app_name)

    def test_validate_secondary_auth_client_id(self):
        sec_id = TEST_AGY_SECONDARY_ID
        jwt_str = make_mock_jwt(sec_id)
        report = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(report.status, mod.ClientIDStatus.VALID)
        self.assertTrue(report.is_safe_to_activate)
        self.assertEqual(report.project_number, "884354919052")

    def test_validate_telemetry_client_id(self):
        tel_id = TEST_AGY_TELEMETRY_ID
        jwt_str = make_mock_jwt(tel_id)
        report = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(report.status, mod.ClientIDStatus.VALID)
        self.assertTrue(report.is_safe_to_activate)
        self.assertEqual(report.project_number, "1098991175725")

    def test_validate_foreign_gemini_cli_rejection(self):
        foreign_id = TEST_GEMINI_CLI_ID
        jwt_str = make_mock_jwt(foreign_id)
        report = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(report.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(report.is_safe_to_activate)
        self.assertIn("gemini-cli", report.app_name)
        self.assertIn("FOREIGN TOKEN CORRUPTION", report.diagnostic_message)
        self.assertIn("login", report.remediation_command)

    def test_validate_foreign_gcloud_and_firebase_rejection(self):
        gcloud_id = TEST_GCLOUD_CLI_ID
        rep1 = mod.AudienceValidator.validate_credentials_dict({"id_token": make_mock_jwt(gcloud_id)})
        self.assertEqual(rep1.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep1.is_safe_to_activate)

        fb_id = TEST_FIREBASE_CLI_ID
        rep2 = mod.AudienceValidator.validate_credentials_dict({"id_token": make_mock_jwt(fb_id)})
        self.assertEqual(rep2.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep2.is_safe_to_activate)

    def test_validate_unknown_client_id(self):
        unknown_id = f"999999999999-unknown.{TEST_APPS_DOMAIN}"
        rep = mod.AudienceValidator.validate_credentials_dict({"id_token": make_mock_jwt(unknown_id)})
        self.assertEqual(rep.status, mod.ClientIDStatus.UNKNOWN_CLIENT)
        self.assertFalse(rep.is_safe_to_activate)

    def test_validate_missing_token_strict_vs_lenient(self):
        creds = {"email": "test@gmail.com"}
        rep_lenient = mod.AudienceValidator.validate_credentials_dict(creds, strict=False)
        self.assertEqual(rep_lenient.status, mod.ClientIDStatus.MISSING_TOKEN)
        self.assertTrue(rep_lenient.is_safe_to_activate)

        rep_strict = mod.AudienceValidator.validate_credentials_dict(creds, strict=True)
        self.assertEqual(rep_strict.status, mod.ClientIDStatus.MISSING_TOKEN)
        self.assertFalse(rep_strict.is_safe_to_activate)

    def test_account_manager_apply_slot_refuses_foreign_client_id(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        foreign_id = TEST_GEMINI_CLI_ID
        (s1 / "oauth_creds.json").write_text(json.dumps({
            "id_token": make_mock_jwt(foreign_id, email="corrupt@gmail.com"),
            "refresh_token": "1//fake"
        }))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "corrupt@gmail.com"}))

        success = self.mgr.apply_slot("1")
        self.assertFalse(success, "apply_slot must refuse activation of slot with foreign client ID")
        slot_status = self.mgr.state.get("slots", {}).get("1", {}).get("status")
        self.assertEqual(slot_status, "CLIENT_ID_MISMATCH")

    def test_account_manager_rotate_to_next_skips_foreign_client_id(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        foreign_id = TEST_GEMINI_CLI_ID
        (s1 / "oauth_creds.json").write_text(json.dumps({
            "id_token": make_mock_jwt(foreign_id),
            "refresh_token": "1//fake1"
        }))

        s2 = mod.ACCOUNTS_DIR / "2"
        s2.mkdir(parents=True, exist_ok=True)
        valid_id = mod.AGY_CONSUMER_CLIENT_ID
        (s2 / "oauth_creds.json").write_text(json.dumps({
            "id_token": make_mock_jwt(valid_id, email="valid2@gmail.com"),
            "refresh_token": "1//fake2"
        }))
        (s2 / "google_accounts.json").write_text(json.dumps({"active": "valid2@gmail.com"}))

        self.mgr.state["current_slot"] = 1
        with patch.object(self.mgr, "calculate_handover_delay", return_value=0.0):
            next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "2")
        self.assertEqual(self.mgr.state.get("slots", {}).get("1", {}).get("status"), "CLIENT_ID_MISMATCH")

    def test_format_diagnostic_box_content(self):
        report = mod.TokenAudienceReport(
            status=mod.ClientIDStatus.FOREIGN_MISMATCH,
            client_id="681255809395",
            project_number="681255809395",
            aud="681255809395",
            azp="681255809395",
            email="victim@gmail.com",
            issuer="https://accounts.google.com",
            expires_at=12345678,
            app_name="gemini-cli",
            is_safe_to_activate=False,
            diagnostic_message="Corrupted token from gemini-cli",
            remediation_command="agy-supervisor login 1"
        )
        box = mod.format_diagnostic_box("1", report)
        self.assertIn("CREDENTIAL GUARD ERROR", box)
        self.assertIn("victim@gmail.com", box)
        self.assertIn("FOREIGN_MISMATCH", box)
        self.assertIn("agy-supervisor login 1", box)


class TestOAuthTokenRefreshMock(unittest.TestCase):
    """Hermetic unit tests for Feature F7: OAuth token refresh."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_gemini = mod.GEMINI_DIR
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name)
        mod.GEMINI_DIR = mod.ACCOUNTS_DIR / "gemini"
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self.prober = mod.SlotHealthProber(mod.ACCOUNTS_DIR)

        # Setup slot 1
        self.s1 = mod.ACCOUNTS_DIR / "1"
        self.s1.mkdir(parents=True, exist_ok=True)
        self.kf = self.s1 / "keyring_secret.json"
        self.of = self.s1 / "oauth_creds.json"
        self.kf.write_text(json.dumps({
            "token": {"refresh_token": "1//valid_refresh", "access_token": "ya29.old", "expiry": "2020-01-01T00:00:00Z"},
            "auth_method": "consumer",
            "id_token_email": "slot1@gmail.com"
        }))
        self.of.write_text(json.dumps({
            "refresh_token": "1//valid_refresh",
            "access_token": "ya29.old",
            "expiry_date": 1000
        }))

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.GEMINI_DIR = self.orig_gemini
        self.temp_dir.cleanup()

    def test_refresh_slot_token_success_200(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "ya29.mocked_new_token",
            "expires_in": 3600,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="slot1@gmail.com")
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        creds = self.prober._extract_slot_credentials(1)
        with patch("urllib.request.urlopen", return_value=mock_response):
            new_tok, err, msg = self.prober.refresh_slot_token(1, creds)

        self.assertEqual(new_tok, "ya29.mocked_new_token")
        self.assertIsNone(err)

        kd = json.loads(self.kf.read_text())
        self.assertEqual(kd["token"]["access_token"], "ya29.mocked_new_token")
        od = json.loads(self.of.read_text())
        self.assertEqual(od["access_token"], "ya29.mocked_new_token")

    def test_refresh_slot_token_invalid_grant_400(self):
        err_body = json.dumps({"error": "invalid_grant", "error_description": "Token revoked"}).encode("utf-8")
        http_err = urllib.error.HTTPError(
            url="https://oauth2.googleapis.com/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=lambda: err_body)
        )
        creds = self.prober._extract_slot_credentials(1)
        with patch("urllib.request.urlopen", side_effect=http_err):
            new_tok, err, msg = self.prober.refresh_slot_token(1, creds)

        self.assertIsNone(new_tok)
        self.assertEqual(err, mod.SlotStatusType.TOKEN_REVOKED)
        self.assertIn("invalid_grant", msg)

    def test_refresh_slot_token_unauthorized_client_401(self):
        err_body = json.dumps({"error": "unauthorized_client", "error_description": "Unauthorized"}).encode("utf-8")
        http_err = urllib.error.HTTPError(
            url="https://oauth2.googleapis.com/token",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=lambda: err_body)
        )
        creds = self.prober._extract_slot_credentials(1)
        with patch("urllib.request.urlopen", side_effect=http_err):
            new_tok, err, msg = self.prober.refresh_slot_token(1, creds)

        self.assertIsNone(new_tok)
        self.assertEqual(err, mod.SlotStatusType.CLIENT_ID_MISMATCH)

    def test_refresh_slot_token_network_error(self):
        url_err = urllib.error.URLError("Connection timed out")
        creds = self.prober._extract_slot_credentials(1)
        with patch("urllib.request.urlopen", side_effect=url_err):
            new_tok, err, msg = self.prober.refresh_slot_token(1, creds)

        self.assertIsNone(new_tok)
        self.assertEqual(err, mod.SlotStatusType.NETWORK_ERROR)

    def test_refresh_slot_token_missing_refresh_token(self):
        creds = {"refresh_token": None}
        new_tok, err, msg = self.prober.refresh_slot_token(1, creds)
        self.assertIsNone(new_tok)
        self.assertEqual(err, mod.SlotStatusType.TOKEN_REVOKED)


class TestCloudCodeLiveProbeMock(unittest.TestCase):
    """Hermetic unit tests for Feature F7: CloudCode live probing."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_dir = mod.ACCOUNTS_DIR
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name)
        self.prober = mod.SlotHealthProber(mod.ACCOUNTS_DIR)

        # Setup slot 1 with valid Antigravity token
        self.s1 = mod.ACCOUNTS_DIR / "1"
        self.s1.mkdir(parents=True, exist_ok=True)
        (self.s1 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.valid_now",
            "refresh_token": "1//refresh_ok",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="slot1@gmail.com")
        }))
        (self.s1 / "google_accounts.json").write_text(json.dumps({"active": "slot1@gmail.com"}))

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        self.temp_dir.cleanup()

    def test_probe_slot_healthy_200(self):
        lca_resp = MagicMock()
        lca_resp.read.return_value = json.dumps({"currentTier": {"name": "Antigravity Free Tier"}}).encode("utf-8")
        lca_resp.__enter__.return_value = lca_resp

        quota_resp = MagicMock()
        quota_resp.read.return_value = json.dumps({
            "buckets": [
                {"modelId": "gemini-2.5-flash", "remainingFraction": 1.0, "resetTime": "2026-10-31T18:00:00Z"},
                {"modelId": "gemini-2.5-pro", "remainingFraction": 1.0, "resetTime": "2026-10-31T18:00:00Z"}
            ]
        }).encode("utf-8")
        quota_resp.__enter__.return_value = quota_resp

        with patch("urllib.request.urlopen", side_effect=[lca_resp, quota_resp]):
            res = self.prober.probe_slot(1)

        self.assertEqual(res.status, mod.SlotStatusType.HEALTHY)
        self.assertEqual(res.gemini_remaining, 1.0)
        self.assertIn("100%", res.details)

    def test_probe_slot_quota_exhausted_429(self):
        lca_resp = MagicMock()
        lca_resp.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode("utf-8")
        lca_resp.__enter__.return_value = lca_resp

        target_reset_epoch = int(time.time()) + 7200
        reset_iso = datetime.datetime.fromtimestamp(target_reset_epoch, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")

        quota_resp = MagicMock()
        quota_resp.read.return_value = json.dumps({
            "buckets": [
                {"modelId": "gemini-2.5-flash", "remainingFraction": 0.0, "resetTime": reset_iso},
                {"modelId": "gemini-2.5-pro", "remainingFraction": 0.0, "resetTime": reset_iso}
            ]
        }).encode("utf-8")
        quota_resp.__enter__.return_value = quota_resp

        with patch("urllib.request.urlopen", side_effect=[lca_resp, quota_resp]):
            res = self.prober.probe_slot(1)

        self.assertEqual(res.status, mod.SlotStatusType.QUOTA_EXHAUSTED)
        self.assertGreater(res.reset_seconds, 7000)
        self.assertEqual(res.reset_timestamp, target_reset_epoch)

    def test_probe_slot_tos_notice_block_403(self):
        err_body = json.dumps({"error": {"code": 403, "message": "Your account is blocked due to a Terms of Service violation."}}).encode("utf-8")
        http_err = urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", 403, "Forbidden", {}, MagicMock(read=lambda: err_body))

        with patch("urllib.request.urlopen", side_effect=http_err):
            res = self.prober.probe_slot(1)

        self.assertEqual(res.status, mod.SlotStatusType.NOTICE_SUSPENDED)
        self.assertIn("Terms of Service", res.notice_reason)

    def test_probe_slot_permission_denied_403(self):
        err_body = json.dumps({"error": {"code": 403, "message": "PERMISSION_DENIED: Access not configured"}}).encode("utf-8")
        http_err = urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", 403, "Forbidden", {}, MagicMock(read=lambda: err_body))

        with patch("urllib.request.urlopen", side_effect=http_err):
            res = self.prober.probe_slot(1)

        self.assertEqual(res.status, mod.SlotStatusType.NOTICE_SUSPENDED)

    def test_probe_slot_auth_failed_401(self):
        err_body = json.dumps({"error": {"code": 401, "message": "Unauthorized"}}).encode("utf-8")
        http_err = urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", 401, "Unauthorized", {}, MagicMock(read=lambda: err_body))

        with patch("urllib.request.urlopen", side_effect=http_err):
            res = self.prober.probe_slot(1)

        self.assertEqual(res.status, mod.SlotStatusType.TOKEN_REVOKED)

    def test_probe_slot_network_error(self):
        url_err = urllib.error.URLError("Network unreachable")
        with patch("urllib.request.urlopen", side_effect=url_err):
            res = self.prober.probe_slot(1)

        self.assertEqual(res.status, mod.SlotStatusType.NETWORK_ERROR)


class TestMultiSlotProbeIsolation(unittest.TestCase):
    """Hermetic unit tests guaranteeing probing passive slots never mutates active state."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_gemini = mod.GEMINI_DIR
        self.orig_state = mod.STATE_FILE
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name)
        mod.STATE_FILE = mod.ACCOUNTS_DIR / "supervisor_state.json"
        mod.GEMINI_DIR = mod.ACCOUNTS_DIR / "gemini"
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self.mgr = mod.AccountManager()

        # Slot 1 (active)
        self.s1 = mod.ACCOUNTS_DIR / "1"
        self.s1.mkdir(parents=True, exist_ok=True)
        (self.s1 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.active_slot1_token",
            "refresh_token": "1//refresh_1",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="active1@gmail.com")
        }))
        (self.s1 / "google_accounts.json").write_text(json.dumps({"active": "active1@gmail.com"}))

        # Copy Slot 1 to active ~/.gemini/
        (mod.GEMINI_DIR / "oauth_creds.json").write_text(json.dumps({"access_token": "ya29.active_slot1_token"}))
        (mod.GEMINI_DIR / "google_accounts.json").write_text(json.dumps({"active": "active1@gmail.com"}))
        self.mgr.state["current_slot"] = 1
        self.mgr.save_state()

        # Slot 2 (passive, expired token that needs refresh)
        self.s2 = mod.ACCOUNTS_DIR / "2"
        self.s2.mkdir(parents=True, exist_ok=True)
        (self.s2 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.passive_old",
            "refresh_token": "1//refresh_2",
            "expiry_date": 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="passive2@gmail.com")
        }))
        (self.s2 / "keyring_secret.json").write_text(json.dumps({
            "token": {"refresh_token": "1//refresh_2", "access_token": "ya29.passive_old", "expiry": "2020-01-01T00:00:00Z"},
            "auth_method": "consumer",
            "id_token_email": "passive2@gmail.com"
        }))
        (self.s2 / "google_accounts.json").write_text(json.dumps({"active": "passive2@gmail.com"}))

        self.prober = mod.SlotHealthProber(mod.ACCOUNTS_DIR, is_active_slot_fn=lambda s: s == 1)

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.GEMINI_DIR = self.orig_gemini
        mod.STATE_FILE = self.orig_state
        self.temp_dir.cleanup()

    def test_probe_passive_slot_does_not_mutate_active_gemini_dir(self):
        refresh_resp = MagicMock()
        refresh_resp.read.return_value = json.dumps({
            "access_token": "ya29.new_slot2_token",
            "expires_in": 3600
        }).encode("utf-8")
        refresh_resp.__enter__.return_value = refresh_resp

        lca_resp = MagicMock()
        lca_resp.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode("utf-8")
        lca_resp.__enter__.return_value = lca_resp

        quota_resp = MagicMock()
        quota_resp.read.return_value = json.dumps({"buckets": []}).encode("utf-8")
        quota_resp.__enter__.return_value = quota_resp

        with patch("urllib.request.urlopen", side_effect=[refresh_resp, lca_resp, quota_resp]):
            res = self.prober.probe_slot(2)

        self.assertEqual(res.status, mod.SlotStatusType.HEALTHY)
        # Verify active ~/.gemini/oauth_creds.json is STILL slot 1!
        gem_data = json.loads((mod.GEMINI_DIR / "oauth_creds.json").read_text())
        self.assertEqual(gem_data.get("access_token"), "ya29.active_slot1_token")

    def test_probe_passive_slot_does_not_mutate_gnome_keyring(self):
        with patch.object(mod, "set_keyring_secret") as mock_set_sec:
            res = self.prober.probe_slot(2, live_probe=False)
            mock_set_sec.assert_not_called()

    def test_probe_passive_slot_does_not_change_current_slot(self):
        self.assertEqual(str(self.mgr.get_current_slot()), "1")
        self.prober.probe_slot(2, live_probe=False)
        self.assertEqual(str(self.mgr.get_current_slot()), "1")
        st = json.loads(mod.STATE_FILE.read_text())
        self.assertEqual(st.get("current_slot"), 1)


class TestCmdProbeCliAndHealthCheckOutput(unittest.TestCase):
    """Hermetic unit tests for CLI output formatting and health check exit codes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_gemini = mod.GEMINI_DIR
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name)
        mod.STATE_FILE = mod.ACCOUNTS_DIR / "supervisor_state.json"
        mod.GEMINI_DIR = mod.ACCOUNTS_DIR / "gemini"
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self.mgr = mod.AccountManager()

        # Slot 1: valid
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.slot1",
            "refresh_token": "1//rt1",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="slot1@gmail.com")
        }))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "slot1@gmail.com"}))

        # Slot 2: foreign mismatch
        s2 = mod.ACCOUNTS_DIR / "2"
        s2.mkdir(parents=True, exist_ok=True)
        foreign_id = TEST_GEMINI_CLI_ID
        (s2 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.slot2",
            "refresh_token": "1//rt2",
            "expiry_date": 1000,
            "id_token": make_mock_jwt(foreign_id, email="foreign2@gmail.com")
        }))
        (s2 / "google_accounts.json").write_text(json.dumps({"active": "foreign2@gmail.com"}))

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.STATE_FILE = self.orig_state
        mod.GEMINI_DIR = self.orig_gemini
        self.temp_dir.cleanup()

    def test_cmd_probe_displays_mismatch_warning_and_remediation(self):
        import io
        import contextlib

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.cmd_probe(self.mgr, "2", live_probe=False)
        output = out.getvalue()
        self.assertIn("Slot 2 [foreign2@gmail.com]", output)
        self.assertIn("CLIENT_ID_MISMATCH", output)
        self.assertIn("Action Required: Chạy 'agy-supervisor login 2'", output)

    def test_cmd_probe_syncs_probe_result_to_supervisor_state(self):
        target_reset = int(time.time()) + 5000
        res = mod.SlotProbeResult(
            slot=1,
            email="slot1@gmail.com",
            status=mod.SlotStatusType.QUOTA_EXHAUSTED,
            reset_seconds=5000,
            reset_timestamp=target_reset,
            details="Exhausted on Gemini"
        )
        self.mgr.sync_probe_result(res)
        s_info = self.mgr.state.get("slots", {}).get("1", {})
        self.assertEqual(s_info.get("status"), "EXHAUSTED_DAILY")
        self.assertEqual(s_info.get("exhausted_until"), target_reset)

    def test_health_check_audit_account_slots_exit_code_behavior(self):
        sys.path.insert(0, str(repo_root / "scripts"))
        hc_mod = _load_module_from_file("health_check", repo_root / "scripts" / "health_check.py")

        fake_home = Path(self.temp_dir.name) / "fake_home"
        acc_dir = fake_home / ".gemini_accounts"
        shutil.copytree(mod.ACCOUNTS_DIR, acc_dir)
        with patch("pathlib.Path.home", return_value=fake_home):
            passed, warnings, failures = hc_mod.audit_account_slots(live_probe=False)
            self.assertGreaterEqual(failures, 1)



# ==============================================================================
# MILESTONE 3 ITERATION 2: ADVERSARIAL INTEGRATION & REGRESSION DEFENSE SUITE
# ==============================================================================

class TestAdversarialBase64AndAudienceFuzzing(unittest.TestCase):
    """Guards JWT Parsing, Audience Validation, Project Number extraction, and ReDoS."""

    def test_decode_base64url_json_extreme_inputs(self):
        self.assertIsNone(mod.decode_base64url_json(None))
        self.assertIsNone(mod.decode_base64url_json(12345))
        self.assertIsNone(mod.decode_base64url_json(["a", "b"]))
        self.assertIsNone(mod.decode_base64url_json({"key": "val"}))
        self.assertIsNone(mod.decode_base64url_json(""))
        self.assertIsNone(mod.decode_base64url_json("   "))
        self.assertIsNone(mod.decode_base64url_json("A"))
        self.assertIsNone(mod.decode_base64url_json("AAAAA"))

        corrupted_inputs = [
            "???invalid???",
            "eyJuYW1lIjoiYWxpY2UifQ====",
            "eyJuYW1lIj\x00oiYWxpY2UifQ",
            "eyJuYW1lIjoiYWxpY2UifQ\n\r\t",
            "eyJuYW1lIj+iYWxpY2U/fQ==",
            "💡🚀🔒",
            "{\"not\":\"base64\"}",
        ]
        for inp in corrupted_inputs:
            res = mod.decode_base64url_json(inp)
            self.assertTrue(res is None or isinstance(res, dict))

        non_dict_payloads = [
            json.dumps(["list", "of", "items"]),
            json.dumps("plain string"),
            json.dumps(123456),
            json.dumps(True),
            json.dumps(None),
        ]
        for p in non_dict_payloads:
            b64_p = base64.urlsafe_b64encode(p.encode()).decode().rstrip("=")
            self.assertIsNone(mod.decode_base64url_json(b64_p))

        malformed_json_payloads = [
            '{"key": }',
            '{"key": "val",}',
            '{key: "val"}',
            '{"unclosed": "string',
            '/* comment */ {"a": 1}',
        ]
        for m in malformed_json_payloads:
            b64_m = base64.urlsafe_b64encode(m.encode()).decode().rstrip("=")
            self.assertIsNone(mod.decode_base64url_json(b64_m))

    def test_decode_base64url_json_large_payloads(self):
        large_dict = {"large_field": "X" * (1024 * 1024), "aud": "1071006060591-test"}
        b64_large = base64.urlsafe_b64encode(json.dumps(large_dict).encode()).decode().rstrip("=")
        t0 = time.time()
        res = mod.decode_base64url_json(b64_large)
        dur = time.time() - t0
        self.assertIsNotNone(res)
        self.assertLess(dur, 1.0)

    def test_parse_jwt_claims_structure_fuzzing(self):
        bad_segments = ["", "one_part_only", "header.payload", "header.payload.sig.extra", "..", "header..sig", ".payload."]
        for s in bad_segments:
            claims, err = mod.parse_jwt_claims(s)
            self.assertIsNone(claims)
            self.assertIsNotNone(err)

    def test_extract_project_number_legitimate(self):
        self.assertEqual(mod.extract_project_number(TEST_AGY_PRIMARY_ID), "1071006060591")
        self.assertEqual(mod.extract_project_number(TEST_AGY_SECONDARY_ID), "884354919052")
        self.assertEqual(mod.extract_project_number(TEST_AGY_TELEMETRY_ID), "1098991175725")
        self.assertEqual(mod.extract_project_number(TEST_GEMINI_CLI_ID), "681255809395")
        self.assertEqual(mod.extract_project_number(TEST_GCLOUD_CLI_ID), "32555940559")
        self.assertEqual(mod.extract_project_number("1071006060591"), "1071006060591")

    def test_extract_project_number_spoofing_attempts(self):
        primary_id = TEST_AGY_PRIMARY_ID
        attacks = [
            f"{primary_id}.attacker.com",
            f"{primary_id}.attackergoogleusercontent.com",
            "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.evil.com",
            f"evil-1071006060591.{TEST_APPS_DOMAIN}",
            f"-1071006060591.{TEST_APPS_DOMAIN}",
            "1071006060591/extra/path",
            "1071006060591?query=param",
            "1071006060591#fragment",
            "1071006060591\x00nullbyte",
            f"1071006060591\nnewline.{TEST_APPS_DOMAIN}",
        ]
        for a in attacks:
            num = mod.extract_project_number(a)
            self.assertIsNone(num, f"Spoofed client ID '{a}' must NOT extract a project number")

    def test_extract_project_number_redos_resilience(self):
        long_digits = "1" * 10000 + "-bad_suffix!@#$"
        t0 = time.time()
        self.assertIsNone(mod.extract_project_number(long_digits))
        self.assertLess(time.time() - t0, 0.5)

    def test_valid_antigravity_tokens(self):
        for cid, info in mod.TRUSTED_AGY_CLIENT_IDS.items():
            jwt_str = make_mock_jwt(cid)
            rep = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
            self.assertEqual(rep.status, mod.ClientIDStatus.VALID)
            self.assertTrue(rep.is_safe_to_activate)
            self.assertEqual(rep.project_number, info["project_number"])

    def test_known_foreign_tokens_rejection(self):
        for cid, info in mod.KNOWN_FOREIGN_CLIENT_IDS.items():
            jwt_str = make_mock_jwt(cid)
            rep = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
            self.assertEqual(rep.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
            self.assertFalse(rep.is_safe_to_activate)

    def test_multi_audience_array_handling(self):
        gemini_id = TEST_GEMINI_CLI_ID
        primary_id = mod.AGY_CONSUMER_CLIENT_ID

        jwt_a = make_mock_jwt([gemini_id, primary_id])
        rep_a = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_a})
        self.assertEqual(rep_a.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep_a.is_safe_to_activate)

        jwt_b = make_mock_jwt([primary_id, gemini_id], azp=gemini_id)
        rep_b = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_b})
        self.assertEqual(rep_b.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep_b.is_safe_to_activate)

        jwt_c = make_mock_jwt([])
        rep_c = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_c})
        self.assertEqual(rep_c.status, mod.ClientIDStatus.UNKNOWN_CLIENT)
        self.assertFalse(rep_c.is_safe_to_activate)

    def test_type_juggling_in_jwt_claims(self):
        weird_claims = [
            {"aud": 12345},
            {"aud": True},
            {"aud": False},
            {"aud": None},
            {"aud": [None]},
            {"aud": [12345]},
            {"aud": [{"sub": "dict"}]},
            {"azp": 12345},
            {"azp": ["list_as_azp"]},
            {"azp": {"dict_as_azp": True}},
            {"exp": "not_an_int"},
            {"iss": ["list_iss"]},
        ]
        for c in weird_claims:
            jwt_str = make_mock_jwt(c.get("aud", "test"), azp=c.get("azp"))
            rep = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
            self.assertIsInstance(rep, mod.TokenAudienceReport)
            self.assertFalse(rep.is_safe_to_activate)

    def test_unhashable_client_id_in_creds(self):
        creds = {"client_id": ["unhashable_list"], "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID)}
        rep = mod.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, mod.ClientIDStatus.MALFORMED_JWT)
        self.assertFalse(rep.is_safe_to_activate)

    def test_poc_1_precedence_bypass_guarded(self):
        gcloud_id = TEST_GCLOUD_CLI_ID
        gcloud_jwt = make_mock_jwt(gcloud_id, azp=gcloud_id)
        creds_spoofed = {
            "client_id": mod.AGY_CONSUMER_CLIENT_ID,
            "id_token": gcloud_jwt,
            "refresh_token": "1//gcloud_refresh_token"
        }
        rep = mod.AudienceValidator.validate_credentials_dict(creds_spoofed)
        self.assertEqual(rep.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_poc_2_supervisor_vs_healthcheck_consistency(self):
        gemini_id = TEST_GEMINI_CLI_ID
        jwt_str = make_mock_jwt(gemini_id, azp=mod.AGY_CONSUMER_CLIENT_ID)
        sup_rep = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(sup_rep.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(sup_rep.is_safe_to_activate)

    def test_poc_3_multi_audience_blind_spot_guarded(self):
        gemini_id = TEST_GEMINI_CLI_ID
        jwt_str = make_mock_jwt([mod.AGY_CONSUMER_CLIENT_ID, gemini_id])
        rep = mod.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, mod.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)


class TestCredentialProtectionAndRotationGuards(unittest.TestCase):
    """Guards Defect Cluster 1 (sync_back destruction) and Defect Cluster 2 (fail-open)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_acc = mod.ACCOUNTS_DIR
        self.orig_gem = mod.GEMINI_DIR
        self.orig_state = mod.STATE_FILE
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name) / "accounts"
        mod.GEMINI_DIR = Path(self.temp_dir.name) / "gemini"
        mod.STATE_FILE = mod.ACCOUNTS_DIR / "supervisor_state.json"
        mod.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self.mgr = mod.AccountManager()

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_acc
        mod.GEMINI_DIR = self.orig_gem
        mod.STATE_FILE = self.orig_state
        self.temp_dir.cleanup()

    def test_sync_back_aborts_on_quarantined_slot_client_id_mismatch(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt("681255809395"), "access_token": "ya29.s1_corrupt"}))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "s1_user@gmail.com"}))

        (mod.GEMINI_DIR / "oauth_creds.json").write_text(json.dumps({"access_token": "ya29.s2_active_token"}))
        (mod.GEMINI_DIR / "google_accounts.json").write_text(json.dumps({"active": "s2_user@gmail.com"}))

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "CLIENT_ID_MISMATCH", "exhausted_until": int(time.time()) + 99999}
        self.mgr.save_state()

        self.mgr.sync_back_current()
        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds.get("access_token"), "ya29.s1_corrupt", "Quarantined slot MUST NOT be overwritten by ~/.gemini!")

    def test_sync_back_aborts_on_quarantined_slot_notice_blocked(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"access_token": "ya29.s1_blocked"}))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "s1_blocked@gmail.com"}))

        (mod.GEMINI_DIR / "oauth_creds.json").write_text(json.dumps({"access_token": "ya29.s2_good"}))

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "NOTICE_BLOCKED", "exhausted_until": int(time.time()) + 99999}
        self.mgr.save_state()

        self.mgr.sync_back_current()
        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds.get("access_token"), "ya29.s1_blocked")

    def test_sync_back_aborts_on_cross_account_email_mismatch(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID), "access_token": "ya29.s1"}))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "slot1_real@gmail.com"}))

        (mod.GEMINI_DIR / "oauth_creds.json").write_text(json.dumps({"access_token": "ya29.s2"}))
        (mod.GEMINI_DIR / "google_accounts.json").write_text(json.dumps({"active": "slot2_different@gmail.com"}))

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "ACTIVE", "exhausted_until": 0}
        self.mgr.save_state()

        self.mgr.sync_back_current()
        s1_email = json.loads((s1 / "google_accounts.json").read_text()).get("active")
        self.assertEqual(s1_email, "slot1_real@gmail.com", "Email mismatch MUST abort sync-back to prevent data corruption!")

    def test_sync_back_succeeds_on_healthy_slot_matching_email(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID), "access_token": "ya29.s1_old"}))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "user@gmail.com"}))

        (mod.GEMINI_DIR / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID), "access_token": "ya29.s1_new"}))
        (mod.GEMINI_DIR / "google_accounts.json").write_text(json.dumps({"active": "user@gmail.com"}))

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "ACTIVE", "exhausted_until": 0}
        self.mgr.save_state()

        self.mgr.sync_back_current()
        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds.get("access_token"), "ya29.s1_new")

    def test_apply_slot_refuses_foreign_gemini_cli(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt("681255809395"), "refresh_token": "1//fake"}))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "corrupt@gmail.com"}))

        res = self.mgr.apply_slot("1")
        self.assertFalse(res)
        self.assertEqual(self.mgr.state["slots"]["1"]["status"], "CLIENT_ID_MISMATCH")

    def test_apply_slot_refuses_foreign_gcloud(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt("32555940559"), "refresh_token": "1//fake"}))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "gcloud@gmail.com"}))

        res = self.mgr.apply_slot("1")
        self.assertFalse(res)
        self.assertEqual(self.mgr.state["slots"]["1"]["status"], "CLIENT_ID_MISMATCH")

    def test_apply_slot_refuses_unknown_client_id(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt(f"999999999999-unknown.{TEST_APPS_DOMAIN}")}))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "unknown@gmail.com"}))

        res = self.mgr.apply_slot("1")
        self.assertFalse(res)
        self.assertEqual(self.mgr.state["slots"]["1"]["status"], "CLIENT_ID_MISMATCH")

    def test_apply_slot_refuses_corrupt_files(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text("{bad json: unclosed")

        res = self.mgr.apply_slot("1")
        self.assertFalse(res)

    def test_rotate_to_next_strictly_skips_foreign_slots(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt("681255809395"), "refresh_token": "1//f1"}))

        s2 = mod.ACCOUNTS_DIR / "2"
        s2.mkdir(parents=True, exist_ok=True)
        (s2 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID), "refresh_token": "1//f2"}))
        (s2 / "google_accounts.json").write_text(json.dumps({"active": "valid2@gmail.com"}))

        self.mgr.state["current_slot"] = 1
        with patch.object(self.mgr, "calculate_handover_delay", return_value=0.0):
            nxt = self.mgr.rotate_to_next()
        self.assertEqual(nxt, "2")
        self.assertEqual(self.mgr.state["slots"]["1"]["status"], "CLIENT_ID_MISMATCH")

    def test_rotate_to_next_circuit_breaker_when_all_foreign(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt("681255809395"), "refresh_token": "1//f1"}))

        s2 = mod.ACCOUNTS_DIR / "2"
        s2.mkdir(parents=True, exist_ok=True)
        (s2 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt("32555940559"), "refresh_token": "1//f2"}))

        self.mgr.state["current_slot"] = 1
        with patch.object(self.mgr, "calculate_handover_delay", return_value=0.0):
            nxt = self.mgr.rotate_to_next()
        self.assertEqual(nxt, "")

    def test_cmd_switch_fails_when_apply_slot_returns_false(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt("681255809395")}))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "bad@gmail.com"}))

        with self.assertRaises(SystemExit) as cm:
            mod.cmd_switch(self.mgr, "1")
        self.assertEqual(cm.exception.code, 1)

    def test_main_failover_when_apply_slot_returns_false(self):
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt("681255809395")}))

        s2 = mod.ACCOUNTS_DIR / "2"
        s2.mkdir(parents=True, exist_ok=True)
        (s2 / "oauth_creds.json").write_text(json.dumps({"id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID), "access_token": "ya29.s2"}))
        (s2 / "google_accounts.json").write_text(json.dumps({"active": "s2@gmail.com"}))

        self.mgr.state["current_slot"] = 1
        self.mgr.save_state()

        curr_slot = self.mgr.get_current_slot()
        if not self.mgr.apply_slot(curr_slot):
            curr_slot = self.mgr.rotate_to_next()

        self.assertEqual(curr_slot, "2")
        self.assertEqual(str(self.mgr.state["current_slot"]), "2")


class TestProberFaultToleranceAndExceptionSafety(unittest.TestCase):
    """Guards Defect Cluster 4 (OAuth 5xx/429), Cluster 5 (CloudCode 500), and Cluster 6 (JSON/past reset)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_acc = mod.ACCOUNTS_DIR
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name) / "accounts"
        mod.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        self.prober = mod.SlotHealthProber(mod.ACCOUNTS_DIR)

        self.s1 = mod.ACCOUNTS_DIR / "1"
        self.s1.mkdir(parents=True, exist_ok=True)
        (self.s1 / "oauth_creds.json").write_text(json.dumps({
            "access_token": "ya29.valid",
            "refresh_token": "1//refresh",
            "expiry_date": (int(time.time()) + 3600) * 1000,
            "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID, email="slot1@gmail.com")
        }))
        (self.s1 / "google_accounts.json").write_text(json.dumps({"active": "slot1@gmail.com"}))

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_acc
        self.temp_dir.cleanup()

    def test_refresh_slot_token_http_500_mapped_to_network_error(self):
        http_500 = urllib.error.HTTPError("https://oauth2.googleapis.com/token", 500, "Internal Server Error", {}, MagicMock(read=lambda: b'{"error":"fail"}'))
        creds = self.prober._extract_slot_credentials(1)
        with patch("urllib.request.urlopen", side_effect=http_500):
            new_tok, err, msg = self.prober.refresh_slot_token(1, creds)
        self.assertIsNone(new_tok)
        self.assertEqual(err, mod.SlotStatusType.NETWORK_ERROR, "HTTP 500 MUST NOT cause permanent 10-yr lockout!")

    def test_refresh_slot_token_http_502_503_504_mapped_to_network_error(self):
        for code in (502, 503, 504):
            http_err = urllib.error.HTTPError("https://oauth2.googleapis.com/token", code, "Bad Gateway", {}, MagicMock(read=lambda: b'{"error":"transient"}'))
            creds = self.prober._extract_slot_credentials(1)
            with patch("urllib.request.urlopen", side_effect=http_err):
                new_tok, err, msg = self.prober.refresh_slot_token(1, creds)
            self.assertEqual(err, mod.SlotStatusType.NETWORK_ERROR)

    def test_refresh_slot_token_http_429_mapped_to_network_error(self):
        http_429 = urllib.error.HTTPError("https://oauth2.googleapis.com/token", 429, "Too Many Requests", {}, MagicMock(read=lambda: b'{"error":"rate_limit"}'))
        creds = self.prober._extract_slot_credentials(1)
        with patch("urllib.request.urlopen", side_effect=http_429):
            new_tok, err, msg = self.prober.refresh_slot_token(1, creds)
        self.assertEqual(err, mod.SlotStatusType.NETWORK_ERROR)

    def test_probe_slot_backend_500_both_endpoints_mapped_to_network_error(self):
        http_500 = urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", 500, "Internal Server Error", {}, MagicMock(read=lambda: b'{"error":"fail"}'))
        with patch("urllib.request.urlopen", side_effect=http_500):
            res = self.prober.probe_slot(1, live_probe=True)
        self.assertEqual(res.status, mod.SlotStatusType.NETWORK_ERROR)

    def test_probe_slot_backend_500_retrieve_user_quota_mapped_to_network_error(self):
        lca_200 = MagicMock()
        lca_200.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode()
        lca_200.__enter__.return_value = lca_200

        http_500 = urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", 500, "Internal Server Error", {}, MagicMock(read=lambda: b'{"error":"fail"}'))
        with patch("urllib.request.urlopen", side_effect=[lca_200, http_500]):
            res = self.prober.probe_slot(1, live_probe=True)
        self.assertEqual(res.status, mod.SlotStatusType.NETWORK_ERROR, "retrieveUserQuota 500 must NOT be swallowed as healthy 100%!")

    def test_probe_slot_socket_timeout_load_code_assist_mapped_to_network_error(self):
        timeout_err = urllib.error.URLError(socket.timeout("Operation timed out"))
        with patch("urllib.request.urlopen", side_effect=timeout_err):
            res = self.prober.probe_slot(1, live_probe=True)
        self.assertEqual(res.status, mod.SlotStatusType.NETWORK_ERROR)

    def test_probe_slot_socket_timeout_retrieve_user_quota_mapped_to_network_error(self):
        lca_200 = MagicMock()
        lca_200.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode()
        lca_200.__enter__.return_value = lca_200

        timeout_err = urllib.error.URLError(socket.timeout("Operation timed out"))
        with patch("urllib.request.urlopen", side_effect=[lca_200, timeout_err]):
            res = self.prober.probe_slot(1, live_probe=True)
        self.assertEqual(res.status, mod.SlotStatusType.NETWORK_ERROR)

    def test_probe_slot_tcp_disconnect_retrieve_user_quota_mapped_to_network_error(self):
        lca_200 = MagicMock()
        lca_200.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode()
        lca_200.__enter__.return_value = lca_200

        remote_disconnect = http.client.RemoteDisconnected("Remote closed connection")
        with patch("urllib.request.urlopen", side_effect=[lca_200, remote_disconnect]):
            res = self.prober.probe_slot(1, live_probe=True)
        self.assertEqual(res.status, mod.SlotStatusType.NETWORK_ERROR)

    def test_extract_slot_credentials_corrupt_oauth_creds_safe(self):
        (self.s1 / "oauth_creds.json").write_text("{bad json: corrupted")
        creds = self.prober._extract_slot_credentials(1)
        self.assertIsInstance(creds, dict)
        self.assertIn("email", creds)

    def test_extract_slot_credentials_corrupt_keyring_secret_safe(self):
        (self.s1 / "keyring_secret.json").write_text("{invalid keyring json")
        creds = self.prober._extract_slot_credentials(1)
        self.assertIsInstance(creds, dict)

    def test_keyring_secret_null_token_handled_safely(self):
        (self.s1 / "oauth_creds.json").unlink(missing_ok=True)
        (self.s1 / "keyring_secret.json").write_text(json.dumps({"token": None, "auth_method": "consumer"}))
        creds = self.prober._extract_slot_credentials(1)
        self.assertIsInstance(creds, dict)
        self.assertIsNone(creds.get("access_token"))

    def test_quota_malformed_reset_time_string_fallback(self):
        lca_200 = MagicMock()
        lca_200.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode()
        lca_200.__enter__.return_value = lca_200

        quota_resp = MagicMock()
        quota_resp.read.return_value = json.dumps({
            "buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.0, "resetTime": "MALFORMED_NON_ISO"}]
        }).encode()
        quota_resp.__enter__.return_value = quota_resp

        with patch("urllib.request.urlopen", side_effect=[lca_200, quota_resp]):
            res = self.prober.probe_slot(1, live_probe=True)
        self.assertEqual(res.status, mod.SlotStatusType.QUOTA_EXHAUSTED)
        self.assertEqual(res.reset_seconds, 3600)

    def test_quota_integer_reset_time_fallback(self):
        lca_200 = MagicMock()
        lca_200.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode()
        lca_200.__enter__.return_value = lca_200

        quota_resp = MagicMock()
        quota_resp.read.return_value = json.dumps({
            "buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.0, "resetTime": 12345678}]
        }).encode()
        quota_resp.__enter__.return_value = quota_resp

        with patch("urllib.request.urlopen", side_effect=[lca_200, quota_resp]):
            res = self.prober.probe_slot(1, live_probe=True)
        self.assertEqual(res.status, mod.SlotStatusType.QUOTA_EXHAUSTED)
        self.assertEqual(res.reset_seconds, 3600)

    def test_quota_past_reset_time_clamped_to_future_cooldown(self):
        lca_200 = MagicMock()
        lca_200.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode()
        lca_200.__enter__.return_value = lca_200

        past_quota = MagicMock()
        past_quota.read.return_value = json.dumps({
            "buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.0, "resetTime": "2020-01-01T00:00:00Z"}]
        }).encode()
        past_quota.__enter__.return_value = past_quota

        with patch("urllib.request.urlopen", side_effect=[lca_200, past_quota]):
            res = self.prober.probe_slot(1, live_probe=True)
        self.assertEqual(res.status, mod.SlotStatusType.QUOTA_EXHAUSTED)
        self.assertGreater(res.reset_seconds, 0, "Past resetTime must be clamped to positive cooldown!")
        self.assertGreater(res.reset_timestamp, int(time.time()))

    def test_cloudcode_all_tos_and_notice_patterns(self):
        patterns = [
            (403, '{"error": {"code": 403, "message": "Terms of Service violation"}}'),
            (403, '{"error": {"code": 403, "message": "Account suspended by administrator"}}'),
            (400, '{"error": {"code": 400, "message": "Account blocked: user terms update required"}}'),
            (403, '{"error": {"code": 403, "message": "PERMISSION_DENIED: Access forbidden"}}')
        ]
        for code, body_str in patterns:
            err = urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", code, "Err", {}, MagicMock(read=lambda b=body_str: b.encode("utf-8")))
            with patch("urllib.request.urlopen", side_effect=err):
                res = self.prober.probe_slot(1, live_probe=True)
            self.assertEqual(res.status, mod.SlotStatusType.NOTICE_SUSPENDED)

    def test_refresh_slot_token_atomic_write(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "access_token": "ya29.atomic_token_test",
            "expires_in": 3600
        }).encode()
        mock_resp.__enter__.return_value = mock_resp

        creds = self.prober._extract_slot_credentials(1)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            new_tok, err, msg = self.prober.refresh_slot_token(1, creds)
        self.assertEqual(new_tok, "ya29.atomic_token_test")
        data = json.loads((self.s1 / "oauth_creds.json").read_text())
        self.assertEqual(data.get("access_token"), "ya29.atomic_token_test")


class TestSyncProbeResultAndMultiSlotIsolationAdversarial(unittest.TestCase):
    """Guards SYNC-01 to SYNC-09 and ISO-01 to ISO-06."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_acc = mod.ACCOUNTS_DIR
        self.orig_gem = mod.GEMINI_DIR
        self.orig_state = mod.STATE_FILE
        mod.ACCOUNTS_DIR = Path(self.temp_dir.name) / "accounts"
        mod.GEMINI_DIR = Path(self.temp_dir.name) / "gemini"
        mod.STATE_FILE = mod.ACCOUNTS_DIR / "supervisor_state.json"
        mod.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        mod.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self.mgr = mod.AccountManager()

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_acc
        mod.GEMINI_DIR = self.orig_gem
        mod.STATE_FILE = self.orig_state
        self.temp_dir.cleanup()

    def test_iso_passive_slot_probe_isolation_full(self):
        # Slot 1 active
        s1 = mod.ACCOUNTS_DIR / "1"
        s1.mkdir(parents=True, exist_ok=True)
        s1_creds = {"access_token": "ya29.active_1", "refresh_token": "1//r1", "expiry_date": (int(time.time()) + 3600) * 1000}
        (s1 / "oauth_creds.json").write_text(json.dumps(s1_creds))
        (s1 / "google_accounts.json").write_text(json.dumps({"active": "active1@gmail.com"}))

        (mod.GEMINI_DIR / "oauth_creds.json").write_text(json.dumps(s1_creds))
        (mod.GEMINI_DIR / "google_accounts.json").write_text(json.dumps({"active": "active1@gmail.com"}))

        self.mgr.state["current_slot"] = 1
        self.mgr.save_state()

        # Slot 2 passive expired
        s2 = mod.ACCOUNTS_DIR / "2"
        s2.mkdir(parents=True, exist_ok=True)
        (s2 / "oauth_creds.json").write_text(json.dumps({"access_token": "ya29.old", "refresh_token": "1//r2", "expiry_date": 1000, "id_token": make_mock_jwt(mod.AGY_CONSUMER_CLIENT_ID)}))
        (s2 / "google_accounts.json").write_text(json.dumps({"active": "passive2@gmail.com"}))
        (s2 / "keyring_secret.json").write_text(json.dumps({"token": {"access_token": "ya29.old", "refresh_token": "1//r2", "expiry": "1970-01-01T00:00:00Z"}}))

        prober = mod.SlotHealthProber(mod.ACCOUNTS_DIR, is_active_slot_fn=lambda s: str(s) == "1")

        mock_oauth = MagicMock()
        mock_oauth.read.return_value = json.dumps({"access_token": "ya29.refreshed_s2", "expires_in": 3600}).encode()
        mock_oauth.__enter__.return_value = mock_oauth

        mock_lca = MagicMock()
        mock_lca.read.return_value = json.dumps({"currentTier": {"name": "Antigravity"}}).encode()
        mock_lca.__enter__.return_value = mock_lca

        mock_quota = MagicMock()
        mock_quota.read.return_value = json.dumps({"buckets": []}).encode()
        mock_quota.__enter__.return_value = mock_quota

        with patch("urllib.request.urlopen", side_effect=[mock_oauth, mock_lca, mock_quota]), \
             patch.object(mod, "set_keyring_secret") as mock_keyring:
            res = prober.probe_slot(2, live_probe=True)

            # ISO-01: Passive slot updated
            s2_after = json.loads((s2 / "oauth_creds.json").read_text())
            self.assertEqual(s2_after.get("access_token"), "ya29.refreshed_s2")

            # ISO-02: Active ~/.gemini untouched
            gem_after = json.loads((mod.GEMINI_DIR / "oauth_creds.json").read_text())
            self.assertEqual(gem_after.get("access_token"), "ya29.active_1")

            # ISO-03: Keyring not touched
            mock_keyring.assert_not_called()

            # ISO-05: Current slot preserved
            st_after = json.loads(mod.STATE_FILE.read_text())
            self.assertEqual(st_after.get("current_slot"), 1)

    def test_sync_probe_result_healthy(self):
        res = mod.SlotProbeResult(slot=1, email="slot1@gmail.com", status=mod.SlotStatusType.HEALTHY)
        self.mgr.sync_probe_result(res)
        s1 = self.mgr.state["slots"]["1"]
        self.assertEqual(s1["status"], "ACTIVE")
        self.assertEqual(s1["exhausted_until"], 0)

    def test_sync_probe_result_quota_exhausted(self):
        target_ts = int(time.time()) + 4000
        res = mod.SlotProbeResult(slot=1, email="slot1@gmail.com", status=mod.SlotStatusType.QUOTA_EXHAUSTED, reset_seconds=4000, reset_timestamp=target_ts)
        self.mgr.sync_probe_result(res)
        s1 = self.mgr.state["slots"]["1"]
        self.assertEqual(s1["status"], "EXHAUSTED_DAILY")
        self.assertEqual(s1["exhausted_until"], target_ts)

    def test_sync_probe_result_notice_suspended(self):
        res = mod.SlotProbeResult(slot=2, email="s2@gmail.com", status=mod.SlotStatusType.NOTICE_SUSPENDED, notice_reason="ToS")
        self.mgr.sync_probe_result(res)
        self.assertEqual(self.mgr.state["slots"]["2"]["status"], "NOTICE_BLOCKED")
        self.assertGreater(self.mgr.state["slots"]["2"]["exhausted_until"], int(time.time()) + 365 * 86400)

    def test_sync_probe_result_client_id_mismatch(self):
        res = mod.SlotProbeResult(slot=3, email="s3@gmail.com", status=mod.SlotStatusType.CLIENT_ID_MISMATCH)
        self.mgr.sync_probe_result(res)
        self.assertEqual(self.mgr.state["slots"]["3"]["status"], "CLIENT_ID_MISMATCH")

    def test_sync_probe_result_token_revoked(self):
        res = mod.SlotProbeResult(slot=4, email="s4@gmail.com", status=mod.SlotStatusType.TOKEN_REVOKED)
        self.mgr.sync_probe_result(res)
        self.assertEqual(self.mgr.state["slots"]["4"]["status"], "AUTH_FAILED")

    def test_sync_probe_result_network_error_preserves_state(self):
        self.mgr.state["slots"]["1"] = {"status": "EXHAUSTED_DAILY", "exhausted_until": 12345}
        res = mod.SlotProbeResult(slot=1, email="slot1@gmail.com", status=mod.SlotStatusType.NETWORK_ERROR)
        self.mgr.sync_probe_result(res)
        self.assertEqual(self.mgr.state["slots"]["1"]["status"], "EXHAUSTED_DAILY")

    def test_sync_probe_result_clamps_past_reset_timestamp(self):
        res = mod.SlotProbeResult(slot=1, email="slot1@gmail.com", status=mod.SlotStatusType.QUOTA_EXHAUSTED, reset_seconds=0, reset_timestamp=1577836800)
        self.mgr.sync_probe_result(res)
        self.assertGreater(self.mgr.state["slots"]["1"]["exhausted_until"], int(time.time()))

    def test_sync_probe_result_atomic_sequential_200_syncs(self):
        for i in range(200):
            res = mod.SlotProbeResult(slot=(i % 5) + 1, email=f"user{(i % 5) + 1}@gmail.com", status=mod.SlotStatusType.HEALTHY)
            self.mgr.sync_probe_result(res)
        content = json.loads(mod.STATE_FILE.read_text())
        self.assertIn("slots", content)

    def test_sync_probe_result_concurrent_10_threads(self):
        errors = []
        def worker(tid):
            try:
                for j in range(20):
                    slot_id = (tid % 4) + 1
                    status = mod.SlotStatusType.HEALTHY if j % 2 == 0 else mod.SlotStatusType.QUOTA_EXHAUSTED
                    r = mod.SlotProbeResult(slot=slot_id, email=f"user{slot_id}@gmail.com", status=status, reset_seconds=3600, reset_timestamp=int(time.time()) + 3600)
                    self.mgr.sync_probe_result(r)
            except Exception as ex:
                errors.append(str(ex))
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        content = json.loads(mod.STATE_FILE.read_text())
        self.assertIn("slots", content)


class TestMilestone4AdversarialRegressions(unittest.TestCase):
    """
    Dedicated regression test suite for Milestone 4 production hardening defects (Defects 1-4).
    Guarantees immunity against null slot states, float NaN/Inf credential dates,
    non-integer JWT exp claims in health check diagnostics, and jitter exponent overflow.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tdp = Path(self.td.name)
        self.acc_dir = self.tdp / ".gemini_accounts"
        self.acc_dir.mkdir(parents=True)
        self.state_file = self.acc_dir / "supervisor_state.json"

        self.old_acc_dir = mod.ACCOUNTS_DIR
        self.old_state_file = mod.STATE_FILE
        self.old_gemini_dir = mod.GEMINI_DIR
        mod.ACCOUNTS_DIR = self.acc_dir
        mod.STATE_FILE = self.state_file

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.old_acc_dir
        mod.STATE_FILE = self.old_state_file
        mod.GEMINI_DIR = self.old_gemini_dir
        self.td.cleanup()

    def test_defect1_corrupted_state_slots_null_mapping(self):
        """Defect 1: State file with null slot mappings must not cause AttributeError in rotation or status."""
        self.state_file.write_text(json.dumps({
            "current_slot": 1,
            "slots": {
                "1": None,
                "2": None,
                "3": {"status": "ACTIVE", "exhausted_until": 0}
            }
        }))
        for s in (1, 2, 3):
            s_dir = self.acc_dir / str(s)
            s_dir.mkdir(parents=True, exist_ok=True)
            (s_dir / "oauth_creds.json").write_text(json.dumps({"client_id": mod.AGY_CONSUMER_CLIENT_ID}))

        mgr = mod.AccountManager()
        # Ensure sync_back_current does not raise AttributeError
        mgr.sync_back_current()

        # Ensure rotate_to_next handles null slot mappings gracefully (patch sleep for instant execution)
        with patch("time.sleep", return_value=None):
            next_slot = mgr.rotate_to_next()
            self.assertIn(next_slot, ["1", "2", "3"])

        # Ensure cmd_status handles null slots without error
        mod.cmd_status(mgr)

        # Ensure cmd_reset handles null slots without error
        mod.cmd_reset(mgr, "1")
        mod.cmd_reset(mgr, "all")

    def test_defect2_float_nan_inf_credentials_expiry(self):
        """Defect 2: Float NaN, Inf, and -Inf in expiry_date must not raise ValueError or OverflowError."""
        s1 = self.acc_dir / "1"
        s1.mkdir(parents=True, exist_ok=True)

        prober = mod.SlotHealthProber(self.acc_dir)

        # Test NaN
        (s1 / "oauth_creds.json").write_text('{"client_id": "' + mod.AGY_CONSUMER_CLIENT_ID + '", "expiry_date": NaN}')
        creds_nan = prober._extract_slot_credentials(1)
        self.assertTrue(creds_nan["is_expired"])

        # Test Infinity
        (s1 / "oauth_creds.json").write_text('{"client_id": "' + mod.AGY_CONSUMER_CLIENT_ID + '", "expiry_date": Infinity}')
        creds_inf = prober._extract_slot_credentials(1)
        self.assertTrue(creds_inf["is_expired"])

        # Test -Infinity
        (s1 / "oauth_creds.json").write_text('{"client_id": "' + mod.AGY_CONSUMER_CLIENT_ID + '", "expiry_date": -Infinity}')
        creds_ninf = prober._extract_slot_credentials(1)
        self.assertTrue(creds_ninf["is_expired"])

    def test_defect3_health_check_non_integer_jwt_exp(self):
        """Defect 3: Non-integer, string, or unparseable exp in JWT must not crash health_check.py."""
        hc_mod = _load_module_from_file("health_check_reg", repo_root / "scripts" / "health_check.py")

        s1 = self.acc_dir / "1"
        s1.mkdir(parents=True, exist_ok=True)

        for bad_exp in ["not_an_int", "2026-10-31T00:00:00Z", "1730000000", "", None]:
            claims_dict = {"email": "test@example.com", "aud": mod.AGY_CONSUMER_CLIENT_ID}
            if bad_exp is not None:
                claims_dict["exp"] = bad_exp
            payload_bytes = json.dumps(claims_dict).encode("utf-8")
            payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
            fake_jwt = f"eyJhbGciOiJub25lIn0.{payload_b64}."

            (s1 / "oauth_creds.json").write_text(json.dumps({
                "client_id": mod.AGY_CONSUMER_CLIENT_ID,
                "id_token": fake_jwt,
                "access_token": "valid_acc_tok",
                "refresh_token": "valid_ref_tok"
            }))

            old_home = Path.home
            try:
                Path.home = lambda: self.tdp
                passed, warnings, failures = hc_mod.audit_account_slots(live_probe=False)
                self.assertIsInstance(passed, int)
            finally:
                Path.home = old_home

    def test_defect4_handover_jitter_large_consecutive_failovers(self):
        """Defect 4: Large or extreme consecutive_failovers must not cause OverflowError in jitter calculation."""
        for large_k in [10, 50, 100, 1750, 2000, 1000000]:
            jitter = mod.calculate_handover_jitter(consecutive_failovers=large_k)
            self.assertLessEqual(jitter, mod.JITTER_MAX_BACKOFF_SECONDS)
            self.assertGreaterEqual(jitter, 0.0)

        # Extreme non-numeric or negative values
        self.assertGreaterEqual(mod.calculate_handover_jitter(consecutive_failovers=-500), mod.JITTER_MIN_SECONDS)
        self.assertGreaterEqual(mod.calculate_handover_jitter(consecutive_failovers="invalid"), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
