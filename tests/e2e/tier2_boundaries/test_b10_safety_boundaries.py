#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B10 Safety & Process Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b10_safety_boundaries.py
"""

import os
import sys
import time
import json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_b10_01_stale_presence_lock_recovery(e2e_env):
    """B10-01: Stale unlocked slot.lock file from previously crashed process is acquired without hanging."""
    e2e_env.configure_slot(1, "stale_lock@example.com")

    presence_dir = e2e_env.cli_dir / "presence"
    presence_dir.mkdir(parents=True, exist_ok=True)
    # Stale lock file without active flock
    (presence_dir / "slot.lock").write_text("STALE_PID_999999\n")

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0


def test_b10_02_child_ignores_sigterm_escalation(e2e_env):
    """B10-02: Child process that traps/ignores SIGTERM is escalated to SIGKILL within timeout."""
    e2e_env.configure_slot(1, "sigterm_ignorer@example.com")
    e2e_env.configure_slot(2, "healthy_b10@example.com")

    # Instruction: child ignores SIGTERM, but supervisor escalates to SIGKILL on quota event
    e2e_env.set_mock_instructions([
        {
            "action": "IGNORE_SIGTERM",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in 1800s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 10.0
        },
        {
            "action": "NORMAL",
            "log_line": "Ready on Slot 2",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    t0 = time.time()
    res = e2e_env.run_supervisor(timeout=15)
    dur = time.time() - t0

    assert res.exit_code == 0
    # Must have completed well under 10 seconds via SIGKILL escalation
    assert dur < 8.0
    assert e2e_env.get_state().get("current_slot") == 2


def test_b10_03_corrupted_state_json_recovery(e2e_env):
    """B10-03: Truncated or corrupted supervisor_state.json is repaired and initialized safely."""
    e2e_env.configure_slot(1, "corrupt_state@example.com")

    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state_file.write_text("{\"current_slot\": 1, \"slots\": [TRUNCATED_INVALID_JSON...")

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert isinstance(state, dict)
    assert "current_slot" in state


def test_b10_04_atomic_state_save_tmp_replace(e2e_env):
    """B10-04: Atomic state saving via .tmp and replace guarantees valid JSON on disk at all times."""
    e2e_env.configure_slot(1, "atomic1@example.com")
    e2e_env.configure_slot(2, "atomic2@example.com")

    res = e2e_env.run_supervisor(args=["switch", "2"], timeout=10)
    assert res.exit_code == 0

    state_file = e2e_env.acc_dir / "supervisor_state.json"
    assert state_file.exists()
    assert not (e2e_env.acc_dir / "supervisor_state.json.tmp").exists()

    state = json.loads(state_file.read_text())
    assert state["current_slot"] == 2


def test_b10_05_non_standard_child_exit_codes(e2e_env):
    """B10-05: Non-standard child exit code without quota event resets failovers and exits cleanly."""
    e2e_env.configure_slot(1, "exit_code_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "NON_ZERO_EXIT",
            "log_line": "User invoked /exit",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.1,
            "exit_code": 2
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0
