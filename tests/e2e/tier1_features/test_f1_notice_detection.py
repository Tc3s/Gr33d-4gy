#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F1 Notice & ToS Block Detection
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f1_notice_detection.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import strip_ansi


def test_f1_01_tos_violation_block(e2e_env):
    """F1-01: Terms of service violation triggers immediate child termination and 10-year lock."""
    e2e_env.configure_slot(1, "tos_victim@example.com")
    e2e_env.configure_slot(2, "healthy_peer@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "TOS_NOTICE",
            "log_line": "Run: attempt 1 failed (PERMISSION_DENIED (code 403): Your account is blocked due to a terms of service violation)",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Model session online on Slot 2",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    stdout_clean = strip_ansi(res.stdout)
    assert "CẢNH BÁO NGUY HIỂM" in stdout_clean or "KHÓA TÀI KHOẢN" in stdout_clean or "NOTICE_BLOCKED" in stdout_clean

    state = e2e_env.get_state()
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") == "NOTICE_BLOCKED"
    assert slot1.get("exhausted_until", 0) > time.time() + (5 * 365 * 86400)
    assert state.get("current_slot") == 2


def test_f1_02_policy_violation_quarantine(e2e_env):
    """F1-02: Google Policy violation pattern leads to slot quarantine."""
    e2e_env.configure_slot(1, "policy_violator@example.com")
    e2e_env.configure_slot(2, "clean_alt@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "POLICY_NOTICE",
            "log_line": "I0913 14:02:11.123456 999 run.go:387] Google Policy violation: Account restricted from using model.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Model active",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") == "NOTICE_BLOCKED"
    assert "policy violation" in (slot1.get("reason") or "").lower()
    assert state.get("current_slot") == 2


def test_f1_03_account_disabled_interception(e2e_env):
    """F1-03: User account disabled message intercepted and quarantined."""
    e2e_env.configure_slot(1, "disabled_user@example.com")
    e2e_env.configure_slot(2, "active_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "DISABLED_NOTICE",
            "log_line": "rpc error: code = PermissionDenied desc = user account is disabled",
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
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") == "NOTICE_BLOCKED"
    assert "user account disabled" in (slot1.get("reason") or "").lower()


def test_f1_04_abuse_pi_identity_isolation(e2e_env):
    """F1-04: abuse.pi:antigravity_identity heuristic triggers quarantine."""
    e2e_env.configure_slot(1, "abuse_flagged@example.com")
    e2e_env.configure_slot(2, "backup_user@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "ABUSE_PI_NOTICE",
            "log_line": "Security violation flagged: abuse.pi:antigravity_identity check failed",
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
    assert slot1.get("status") == "NOTICE_BLOCKED"
    assert "anti-abuse" in (slot1.get("reason") or "").lower()


def test_f1_05_all_slots_notice_clean_exit(e2e_env):
    """F1-05: When all slots receive notice blocks, supervisor provides login guidance and exits 0."""
    e2e_env.configure_slot(1, "notice1@example.com")
    e2e_env.configure_slot(2, "notice2@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "NOTICE_1",
            "log_line": "Your account has been suspended due to policy violations.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NOTICE_2",
            "log_line": "Your account has been suspended due to policy violations.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    stdout_clean = strip_ansi(res.stdout)
    assert "agy-supervisor login <slot>" in stdout_clean or "login" in stdout_clean
    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "NOTICE_BLOCKED"
    assert state["slots"]["2"]["status"] == "NOTICE_BLOCKED"
