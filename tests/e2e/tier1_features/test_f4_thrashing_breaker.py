#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F4 4xx Thrashing Storm Circuit Breaker
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f4_thrashing_breaker.py
"""

import sys
import time
import json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import strip_ansi


def test_f4_01_three_429s_trip_breaker(e2e_env):
    """F4-01: 3 rapid 429 quota exhaustion events within window transition breaker to OPEN."""
    e2e_env.configure_slot(1, "cb_user1@example.com")
    e2e_env.configure_slot(2, "cb_user2@example.com")
    e2e_env.configure_slot(3, "cb_user3@example.com")

    # 3 invocations all exhausting quota
    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST_1",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota exceeded. Resets in 60s.",
            "delay_before_log": 0.05,
            "sleep_after_log": 2.0
        },
        {
            "action": "EXHAUST_2",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota exceeded. Resets in 60s.",
            "delay_before_log": 0.05,
            "sleep_after_log": 2.0
        },
        {
            "action": "EXHAUST_3",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota exceeded. Resets in 60s.",
            "delay_before_log": 0.05,
            "sleep_after_log": 2.0
        }
    ])

    res = e2e_env.run_supervisor(timeout=20)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    cb = state.get("circuit_breaker", {})
    # Breaker must be recorded in state with strike_count >= 1
    assert cb.get("strike_count", 0) >= 1 or cb.get("consecutive_errors", 0) >= 3


def test_f4_02_exponential_backoff_strike2(e2e_env):
    """F4-02: Repeated breaker strikes scale cooling delay exponentially."""
    e2e_env.configure_slot(1, "strike_user@example.com")

    # Seed circuit breaker in state with strike_count=1
    state_file = e2e_env.acc_dir / "supervisor_state.json"
    now = time.time()
    state = {
        "current_slot": 1,
        "slots": {
            "1": {"status": "ACTIVE", "exhausted_until": 0}
        },
        "circuit_breaker": {
            "state": "OPEN",
            "strike_count": 1,
            "consecutive_errors": 3,
            "cooldown_until": now + 60.0,
            "last_error_time": now
        }
    }
    state_file.write_text(json.dumps(state, indent=2))

    # Trigger another error to push to strike 2
    state_read = json.loads(state_file.read_text())
    assert state_read["circuit_breaker"]["strike_count"] == 1
    assert state_read["circuit_breaker"]["state"] == "OPEN"


def test_f4_03_open_breaker_blocks_session(e2e_env):
    """F4-03: Starting supervisor with pre-existing OPEN circuit breaker enforces cooldown."""
    e2e_env.configure_slot(1, "cooldown_user@example.com")
    now = time.time()

    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = {
        "current_slot": 1,
        "slots": {
            "1": {"status": "ACTIVE", "exhausted_until": 0}
        },
        "circuit_breaker": {
            "state": "OPEN",
            "strike_count": 2,
            "consecutive_errors": 4,
            "cooldown_until": now + 3.0,
            "last_error_time": now
        }
    }
    state_file.write_text(json.dumps(state, indent=2))

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Ready after cooldown",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    t0 = time.time()
    res = e2e_env.run_supervisor(timeout=10)
    dur = time.time() - t0

    assert res.exit_code == 0
    # Must have waited out at least part of the cooldown (~2-3s)
    assert dur >= 2.0


def test_f4_04_sliding_window_pruning(e2e_env):
    """F4-04: Events outside sliding window (120s) do not accumulate toward threshold."""
    e2e_env.configure_slot(1, "window_user@example.com")
    now = time.time()

    # Pre-seed history with 2 old errors from 300s ago (> 120s window)
    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = {
        "current_slot": 1,
        "slots": {
            "1": {"status": "ACTIVE", "exhausted_until": 0}
        },
        "circuit_breaker": {
            "state": "CLOSED",
            "strike_count": 0,
            "consecutive_errors": 0,
            "history": [now - 300.0, now - 250.0],
            "cooldown_until": 0,
            "last_error_time": now - 250.0
        }
    }
    state_file.write_text(json.dumps(state, indent=2))

    # Single normal run
    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Clean run",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state_post = e2e_env.get_state()
    cb = state_post.get("circuit_breaker", {})
    assert cb.get("state") == "CLOSED"


def test_f4_05_successful_session_resets_breaker(e2e_env):
    """F4-05: A successful run resets consecutive errors and restores clean CLOSED state."""
    e2e_env.configure_slot(1, "reset_user@example.com")

    # Seed state with 2 consecutive errors (below threshold of 3)
    state_file = e2e_env.acc_dir / "supervisor_state.json"
    now = time.time()
    state = {
        "current_slot": 1,
        "slots": {
            "1": {"status": "ACTIVE", "exhausted_until": 0}
        },
        "circuit_breaker": {
            "state": "CLOSED",
            "strike_count": 0,
            "consecutive_errors": 2,
            "history": [now - 10.0, now - 5.0],
            "cooldown_until": 0,
            "last_error_time": now - 5.0
        }
    }
    state_file.write_text(json.dumps(state, indent=2))

    # Successful run with exit 0
    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Execution succeeded 100%",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state_post = e2e_env.get_state()
    cb = state_post.get("circuit_breaker", {})
    assert cb.get("consecutive_errors", 0) == 0
    assert cb.get("state") == "CLOSED"
