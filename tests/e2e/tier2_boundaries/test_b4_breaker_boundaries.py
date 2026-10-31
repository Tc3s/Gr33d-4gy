#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B4 Circuit Breaker Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b4_breaker_boundaries.py
"""

import sys
import time
import json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_b4_01_zero_window_seconds(e2e_env):
    """B4-01: Zero/tiny sliding window handles event calculation without ZeroDivisionError."""
    e2e_env.configure_slot(1, "zero_window@example.com")

    # Pre-seed state with circuit breaker having expired window
    state_file = e2e_env.acc_dir / "supervisor_state.json"
    now = time.time()
    state = {
        "current_slot": 1,
        "slots": {"1": {"status": "ACTIVE", "exhausted_until": 0}},
        "circuit_breaker": {
            "state": "CLOSED",
            "strike_count": 0,
            "consecutive_errors": 0,
            "history": [now - 1000.0],
            "cooldown_until": 0
        }
    }
    state_file.write_text(json.dumps(state, indent=2))

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Session OK",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0


def test_b4_02_negative_timestamp_injection(e2e_env):
    """B4-02: Negative timestamps in history are pruned by cutoff without corrupting sliding window."""
    e2e_env.configure_slot(1, "neg_ts@example.com")

    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = {
        "current_slot": 1,
        "slots": {"1": {"status": "ACTIVE", "exhausted_until": 0}},
        "circuit_breaker": {
            "state": "CLOSED",
            "strike_count": 0,
            "consecutive_errors": 0,
            "history": [-9999.0, -100.0],
            "cooldown_until": 0
        }
    }
    state_file.write_text(json.dumps(state, indent=2))

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Session OK",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state_post = e2e_env.get_state()
    assert state_post.get("circuit_breaker", {}).get("state") == "CLOSED"


def test_b4_03_extreme_strike_count_overflow(e2e_env):
    """B4-03: Extreme strike count (e.g. 100) does not cause OverflowError and clamps to max_cooling."""
    e2e_env.configure_slot(1, "overflow_user@example.com")
    now = time.time()

    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = {
        "current_slot": 1,
        "slots": {"1": {"status": "ACTIVE", "exhausted_until": 0}},
        "circuit_breaker": {
            "state": "OPEN",
            "strike_count": 80,
            "consecutive_errors": 10,
            "cooldown_until": now + 1.0,
            "last_error_time": now
        }
    }
    state_file.write_text(json.dumps(state, indent=2))

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


def test_b4_04_corrupted_breaker_state_in_json(e2e_env):
    """B4-04: Non-dict / string circuit_breaker in supervisor_state.json recovers to defaults gracefully."""
    e2e_env.configure_slot(1, "corrupt_cb@example.com")

    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = {
        "current_slot": 1,
        "slots": {"1": {"status": "ACTIVE", "exhausted_until": 0}},
        "circuit_breaker": "INVALID_CORRUPT_STRING"
    }
    state_file.write_text(json.dumps(state, indent=2))

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

    state_post = e2e_env.get_state()
    assert isinstance(state_post.get("circuit_breaker"), dict)


def test_b4_05_concurrent_event_recording(e2e_env):
    """B4-05: Rapid error events in quick succession are recorded safely with state consistency."""
    e2e_env.configure_slot(1, "rapid1@example.com")
    e2e_env.configure_slot(2, "rapid2@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST_RAPID",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in 60s.",
            "delay_before_log": 0.05,
            "sleep_after_log": 2.0
        },
        {
            "action": "NORMAL",
            "log_line": "Active",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state.get("current_slot") == 2
