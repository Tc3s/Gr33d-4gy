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

class TestAccountManagerLogic(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.accounts_dir = Path(self.temp_dir.name)
        
        # Override ACCOUNTS_DIR in mod
        self.orig_dir = mod.ACCOUNTS_DIR
        self.orig_state = mod.STATE_FILE
        mod.ACCOUNTS_DIR = self.accounts_dir
        mod.STATE_FILE = self.accounts_dir / "supervisor_state.json"
        
        self.mgr = mod.AccountManager()

    def tearDown(self):
        mod.ACCOUNTS_DIR = self.orig_dir
        mod.STATE_FILE = self.orig_state
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

if __name__ == "__main__":
    unittest.main(verbosity=2)
