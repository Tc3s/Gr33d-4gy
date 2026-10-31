#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F3 Transient 503 Overload Ignore
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f3_transient_503.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_f3_01_no_capacity_model_ignored(e2e_env):
    """F3-01: 'No capacity available for model' log is ignored, allowing child to continue."""
    e2e_env.configure_slot(1, "capacity_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "503_THEN_FINISH",
            "log_line": "I0915 11:06:14.038746 581 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server), retrying in 4s",
            "delay_before_log": 0.1,
            "sleep_after_log": 0.5,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") == "ACTIVE"
    assert slot1.get("exhausted_until", 0) == 0


def test_f3_02_http_503_service_unavailable(e2e_env):
    """F3-02: 'HTTP/2.0 503 Service Unavailable' does not cause premature failover."""
    e2e_env.configure_slot(1, "http503_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "503_LOG",
            "log_line": "Stream error: HTTP/2.0 503 Service Unavailable: upstream overloaded",
            "delay_before_log": 0.1,
            "sleep_after_log": 0.4,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"


def test_f3_03_grpc_unavailable_ignored(e2e_env):
    """F3-03: 'code = 503' transient server condition allows natural child retries."""
    e2e_env.configure_slot(1, "grpc503_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "GRPC_503",
            "log_line": "rpc error: (code 503) desc = server temporarily overloaded",
            "delay_before_log": 0.1,
            "sleep_after_log": 0.4,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"


def test_f3_04_repeated_503_bursts_allowed(e2e_env):
    """F3-04: Multiple rapid 503 bursts do not trip circuit breaker strikes."""
    e2e_env.configure_slot(1, "burst503_user@example.com")

    five_503_lines = "\n".join([
        f"Attempt {i}: HTTP/2.0 503 Service Unavailable: No capacity available on the server"
        for i in range(1, 6)
    ])

    e2e_env.set_mock_instructions([
        {
            "action": "BURST_503",
            "log_line": five_503_lines,
            "delay_before_log": 0.1,
            "sleep_after_log": 0.4,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    cb_state = state.get("circuit_breaker", {})
    assert cb_state.get("state") == "CLOSED"
    assert cb_state.get("strike_count", 0) == 0


def test_f3_05_503_followed_by_success(e2e_env):
    """F3-05: 503 followed by successful completion preserves active state with 0 exit code."""
    e2e_env.configure_slot(1, "success_after_503@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "503_THEN_SUCCESS",
            "log_line": "No capacity available for model\nRetrying request...\nGeneration completed 100 tokens.",
            "delay_before_log": 0.1,
            "sleep_after_log": 0.3,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"
    assert state["slots"]["1"]["exhausted_until"] == 0
