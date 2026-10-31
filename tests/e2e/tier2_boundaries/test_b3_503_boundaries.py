#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B3 503 Transient Overload Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b3_503_boundaries.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_b3_01_503_with_go_log_prefix(e2e_env):
    """B3-01: Realistic Go klog/glog timestamp prefixes do not inhibit 503 pattern matching."""
    e2e_env.configure_slot(1, "klog_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "GO_KLOG_503",
            "log_line": "I1031 09:15:45.123456 12345 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available on the server), retrying in 4s",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"


def test_b3_02_503_and_429_text_collision(e2e_env):
    """B3-02: Chunk containing both 503 transient log and 429 quota log resolves to quota failover."""
    e2e_env.configure_slot(1, "collision_slot1@example.com")
    e2e_env.configure_slot(2, "collision_slot2@example.com")

    collision_chunk = (
        "I0915 11:06:01.000000 100 run.go:389] UNAVAILABLE (code 503): No capacity available for model\n"
        "I0915 11:06:05.000000 100 run.go:389] RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1800s."
    )

    e2e_env.set_mock_instructions([
        {
            "action": "COLLISION_BURST",
            "log_line": collision_chunk,
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Ready on Slot 2",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "EXHAUSTED_DAILY"
    assert state.get("current_slot") == 2


def test_b3_03_high_frequency_503_storm(e2e_env):
    """B3-03: 100 rapid 503 log lines appended in tight loop do not trip circuit breaker."""
    e2e_env.configure_slot(1, "storm_user@example.com")

    lines_503 = [f"Attempt {i}: HTTP/2.0 503 Service Unavailable: server overloaded" for i in range(1, 101)]

    e2e_env.set_mock_instructions([
        {
            "action": "STORM_503",
            "log_lines": lines_503,
            "delay_before_log": 0.05,
            "sleep_after_log": 0.3,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    cb = state.get("circuit_breaker", {})
    assert cb.get("consecutive_errors", 0) == 0
    assert cb.get("strike_count", 0) == 0


def test_b3_04_zero_byte_read_polling(e2e_env):
    """B3-04: Log watcher thread polls quietly on stagnant log file without CPU spin or hang."""
    e2e_env.configure_slot(1, "quiet_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "QUIET_RUN",
            "log_line": "Started without emitting any error lines",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.4,
            "exit_code": 0
        }
    ])

    t0 = time.time()
    res = e2e_env.run_supervisor(timeout=10)
    dur = time.time() - t0

    assert res.exit_code == 0
    assert dur < 5.0


def test_b3_05_multiline_trace_ending_in_503(e2e_env):
    """B3-05: 50-line gRPC stack trace ending with 503 is recognized as transient."""
    e2e_env.configure_slot(1, "trace_user@example.com")

    stack_trace = "\n".join([f"    at google.golang.org/grpc/internal/transport/http2_client.go:{i}" for i in range(1, 40)])
    full_log = f"ERROR: RPC stream failed with transport error:\n{stack_trace}\nUNAVAILABLE (code 503): No capacity available for model"

    e2e_env.set_mock_instructions([
        {
            "action": "STACKTRACE_503",
            "log_line": full_log,
            "delay_before_log": 0.05,
            "sleep_after_log": 0.3,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"
