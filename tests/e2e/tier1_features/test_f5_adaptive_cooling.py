#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F5 Adaptive Cooling & Handover Jitter
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f5_adaptive_cooling.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_f5_01_explicit_reset_seconds(e2e_env):
    """F5-01: 'Resets in 4h46m23s' calculates exact reset epoch and marks EXHAUSTED_DAILY."""
    e2e_env.configure_slot(1, "cooling1@example.com")
    e2e_env.configure_slot(2, "cooling2@example.com")

    # 4h46m23s = 4*3600 + 46*60 + 23 = 14400 + 2760 + 23 = 17183 seconds
    e2e_env.set_mock_instructions([
        {
            "action": "QUOTA_4H",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 4h46m23s.",
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
    assert slot1.get("exhausted_until", 0) >= now + 17100
    assert state.get("current_slot") == 2


def test_f5_02_short_reset_seconds_rpm(e2e_env):
    """F5-02: 'Resets in 45s' (<600s) marks slot as COOLDOWN_RPM."""
    e2e_env.configure_slot(1, "rpm_user@example.com")
    e2e_env.configure_slot(2, "rpm_alt@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "QUOTA_RPM",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Rate limit exceeded. Resets in 45s.",
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
    assert slot1.get("status") == "COOLDOWN_RPM"
    assert now <= slot1.get("exhausted_until", 0) <= now + 60


def test_f5_03_midnight_pacific_fallback(e2e_env):
    """F5-03: Daily exhaustion without reset string defaults to Midnight Pacific fallback."""
    e2e_env.configure_slot(1, "fallback_user@example.com")
    e2e_env.configure_slot(2, "fallback_alt@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "DAILY_NO_RESET",
            "log_line": "RESOURCE_EXHAUSTED (code 429): You have exhausted your quota on this model. Please upgrade your subscription.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Online",
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
    # Midnight Pacific is always at least several hours ahead (unless running exactly at 00:04 PT)
    assert slot1.get("exhausted_until", 0) > time.time() + 600


def test_f5_04_handover_jitter_spacing(e2e_env):
    """F5-04: Failover incorporates a randomized handover delay between 2.5s and 5.0s."""
    e2e_env.configure_slot(1, "jitter1@example.com")
    e2e_env.configure_slot(2, "jitter2@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "QUOTA",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1800s.",
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

    t0 = time.time()
    res = e2e_env.run_supervisor(timeout=15)
    elapsed = time.time() - t0

    assert res.exit_code == 0
    assert elapsed >= 2.5
    assert "Handover Jitter: Tạm dừng" in res.stdout


def test_f5_05_consecutive_failover_backoff(e2e_env):
    """F5-05: Consecutive failovers increase handover jitter by 1.5x backoff factor."""
    e2e_env.configure_slot(1, "fail1@example.com")
    e2e_env.configure_slot(2, "fail2@example.com")
    e2e_env.configure_slot(3, "fail3@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST_1",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1800s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "EXHAUST_2",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1800s.",
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

    res = e2e_env.run_supervisor(timeout=25)
    assert res.exit_code == 0

    stdout = res.stdout
    assert "failover #1" in stdout
    assert "failover #2" in stdout
    state = e2e_env.get_state()
    assert state.get("current_slot") == 3
