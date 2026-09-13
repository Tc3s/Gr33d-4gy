#!/usr/bin/env python3
"""
Unit and Integration Test Suite for agy-supervisor
Tests quota detection, dynamic N slot discovery, circular rotation logic, and keyring helpers.
"""

import sys
import unittest
import tempfile
import json
from pathlib import Path

# Add bin directory to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "bin"))

# Import components from agy-supervisor
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("supervisor", str(repo_root / "bin" / "agy-supervisor"))
mod = loader.load_module()

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
        self.assertEqual(mod.parse_reset_seconds("No reset string"), 0)

class TestAccountManagerLogic(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.accounts_dir = Path(self.temp_dir.name)
        
        # Override ACCOUNTS_DIR in mod
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        self.orig_sleep = mod.time.sleep
        mod.ACCOUNTS_DIR = self.accounts_dir
        mod.STATE_FILE = self.accounts_dir / "supervisor_state.json"
        # Mock time.sleep to run tests instantly
        mod.time.sleep = lambda s: None
        
        self.mgr = mod.AccountManager()

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.STATE_FILE = self.orig_state
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
        self.orig_popen = mod.subprocess.Popen
        self.orig_sleep = mod.time.sleep

        mod.ACCOUNTS_DIR = self.temp_path / "accounts"
        mod.STATE_FILE = self.temp_path / "accounts" / "supervisor_state.json"
        mod.CLI_DIR = self.temp_path / "cli"
        mod.PRESENCE_DIR = self.temp_path / "cli" / "presence"
        mod.CLI_DIR.mkdir(parents=True)
        mod.PRESENCE_DIR.mkdir(parents=True)

        for s in [1, 2]:
            (mod.ACCOUNTS_DIR / str(s)).mkdir(parents=True)
            (mod.ACCOUNTS_DIR / str(s) / "oauth_creds.json").write_text("{\"email\": \"acc" + str(s) + "@gmail.com\"}")

        self.mgr = mod.AccountManager()
        self.mgr.apply_slot("1")

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_accounts
        mod.STATE_FILE = self.orig_state
        mod.CLI_DIR = self.orig_cli
        mod.PRESENCE_DIR = self.orig_presence
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
        quota_hit, reset_secs = sup.run_session([], is_continue=False)
        self.assertTrue(quota_hit)
        self.assertEqual(reset_secs, 17183)

        # Rotate to Slot 2
        mod.time.sleep = lambda s: None
        self.mgr.mark_exhausted("1", reset_secs=reset_secs)
        next_slot = self.mgr.rotate_to_next()
        self.assertEqual(next_slot, "2")
        self.assertEqual(self.mgr.get_current_slot(), "2")

        # Turn 2: Resumes session
        current_turn[0] = 2
        quota_hit2, reset_secs2 = sup.run_session([], is_continue=True)
        self.assertFalse(quota_hit2)

        # Assertions on commands
        self.assertEqual(history[0][1], ["agy", "--dangerously-skip-permissions"])
        self.assertEqual(history[1][1], ["agy", "-c", "--dangerously-skip-permissions"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
