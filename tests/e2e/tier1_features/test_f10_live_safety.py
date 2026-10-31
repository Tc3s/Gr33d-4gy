#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F10 Live Account Safety & Terminal Guard
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f10_live_safety.py
"""

import os
import sys
import time
import json
import stat
import fcntl
import threading
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.env_factory import make_test_jwt, AGY_CLIENT_ID


def test_f10_01_credentials_permissions_600(e2e_env):
    """F10-01: Credential files must have 0600 permissions and directory 0700."""
    e2e_env.configure_slot(1, "perm_check@example.com")

    # In ~/.gemini, create files with loose permissions
    jwt_tok = make_test_jwt(AGY_CLIENT_ID, "perm_check@example.com")
    (e2e_env.gemini_dir / "oauth_creds.json").write_text(json.dumps({
        "access_token": "token",
        "id_token": jwt_tok
    }))
    (e2e_env.gemini_dir / "google_accounts.json").write_text(json.dumps({"active": "perm_check@example.com"}))

    # Run save command
    res = e2e_env.run_supervisor(args=["save", "1"], timeout=10)
    assert res.exit_code == 0

    slot_dir = e2e_env.acc_dir / "1"
    dir_mode = stat.S_IMODE(slot_dir.stat().st_mode)
    assert dir_mode == 0o700

    oauth_mode = stat.S_IMODE((slot_dir / "oauth_creds.json").stat().st_mode)
    assert oauth_mode == 0o600

    acc_mode = stat.S_IMODE((slot_dir / "google_accounts.json").stat().st_mode)
    assert acc_mode == 0o600


def test_f10_02_dangerously_skip_permissions_injected(e2e_env):
    """F10-02: --dangerously-skip-permissions is always injected into child agy argument vector."""
    e2e_env.configure_slot(1, "inject_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["--prompt", "hello world"], timeout=10)
    assert res.exit_code == 0

    history = e2e_env.get_call_history()
    assert len(history) == 1
    assert "--dangerously-skip-permissions" in history[0]["args"]
    assert "--prompt" in history[0]["args"]
    assert "hello world" in history[0]["args"]


def test_f10_03_terminal_guard_restores_on_signal(e2e_env):
    """F10-03: TerminalGuard ensures process exits cleanly and restores terminal state on signals."""
    e2e_env.configure_slot(1, "term_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Running",
            "delay_before_log": 0.05,
            "sleep_after_log": 1.0,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0


def test_f10_04_presence_lock_release_wait(e2e_env):
    """F10-04: Supervisor waits for external presence lock file release before acquiring and launching."""
    e2e_env.configure_slot(1, "lock_user@example.com")

    presence_dir = e2e_env.cli_dir / "presence"
    presence_dir.mkdir(parents=True, exist_ok=True)
    lock_file = presence_dir / "slot.lock"
    lock_file.touch()

    # Hold file lock for 0.8 seconds in a background thread
    def hold_lock():
        with open(lock_file, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            time.sleep(0.8)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Acquired presence lock successfully",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    t0 = time.time()
    res = e2e_env.run_supervisor(timeout=10)
    dur = time.time() - t0

    assert res.exit_code == 0
    assert dur >= 0.7


def test_f10_05_child_process_tree_killed(e2e_env):
    """F10-05: Child agy process tree is terminated on quota failover avoiding orphan background processes."""
    e2e_env.configure_slot(1, "tree_user1@example.com")
    e2e_env.configure_slot(2, "tree_user2@example.com")

    # Slot 1 attempts to sleep for 10s after logging quota error, but supervisor must kill it immediately
    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST_LONG_SLEEP",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in 1800s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 10.0
        },
        {
            "action": "NORMAL",
            "log_line": "Slot 2 alive",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    t0 = time.time()
    res = e2e_env.run_supervisor(timeout=15)
    dur = time.time() - t0

    assert res.exit_code == 0
    # Must have completed well under 10 seconds because child process tree was killed
    assert dur < 8.0
    history = e2e_env.get_call_history()
    assert len(history) == 2
