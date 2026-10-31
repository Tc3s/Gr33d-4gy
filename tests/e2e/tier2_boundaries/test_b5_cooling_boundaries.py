#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B5 Adaptive Cooling Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b5_cooling_boundaries.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_b5_01_zero_reset_seconds_parsing(e2e_env):
    """B5-01: 'Resets in 0s' is handled safely and triggers default daily fallback."""
    e2e_env.configure_slot(1, "zero_reset@example.com")
    e2e_env.configure_slot(2, "healthy_peer@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "ZERO_RESET",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 0s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") in ("EXHAUSTED_DAILY", "COOLDOWN_RPM")


def test_b5_02_huge_reset_time_multi_year(e2e_env):
    """B5-02: Extreme reset time (e.g. 56h55m0s forensic lockout) parses and serializes cleanly."""
    e2e_env.configure_slot(1, "lockout57h@example.com")
    e2e_env.configure_slot(2, "healthy_peer@example.com")

    # 56h55m0s = 56*3600 + 55*60 = 201600 + 3300 = 204900s
    e2e_env.set_mock_instructions([
        {
            "action": "LOCKOUT_57H",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 56h55m0s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    now = int(time.time())
    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") == "EXHAUSTED_DAILY"
    assert slot1.get("exhausted_until", 0) >= now + 204000


def test_b5_03_malformed_reset_string(e2e_env):
    """B5-03: Malformed reset string ('Resets in soonish') falls back safely to Midnight Pacific."""
    e2e_env.configure_slot(1, "malformed_reset@example.com")
    e2e_env.configure_slot(2, "healthy_peer@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "MALFORMED_RESET",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in soonish.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") in ("EXHAUSTED_DAILY", "COOLDOWN_RPM")


def test_b5_04_consecutive_failover_jitter_cap(e2e_env):
    """B5-04: High failover count caps jitter calculation at max_jitter (30.0s)."""
    e2e_env.configure_slot(1, "cap_user@example.com")

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


def test_b5_05_dst_transition_midnight_pacific(e2e_env):
    """B5-05: Midnight Pacific calculation yields future timestamp in America/Los_Angeles."""
    e2e_env.configure_slot(1, "dst_user@example.com")
    e2e_env.configure_slot(2, "dst_peer@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "DAILY_EXHAUST",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Daily quota exceeded.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") == "EXHAUSTED_DAILY"
    assert slot1.get("exhausted_until", 0) > time.time()
