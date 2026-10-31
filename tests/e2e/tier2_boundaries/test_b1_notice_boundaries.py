#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B1 Notice Detection Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b1_notice_boundaries.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import strip_ansi


def test_b1_01_empty_log_line_handling(e2e_env):
    """B1-01: Chunks with empty lines, pure newlines, or whitespace are handled safely."""
    e2e_env.configure_slot(1, "empty_log@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "EMPTY_LINES",
            "log_lines": ["", "   ", "\t\t", "\n\n", "Session active"],
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0
    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"


def test_b1_02_case_insensitive_notice(e2e_env):
    """B1-02: Notice emitted in full UPPERCASE is caught by case-insensitive lower() matching."""
    e2e_env.configure_slot(1, "upper_notice@example.com")
    e2e_env.configure_slot(2, "healthy_b1@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "UPPER_NOTICE",
            "log_line": "FATAL: TERMS OF SERVICE VIOLATION - ACCOUNT SUSPENDED",
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
    assert state["slots"]["1"]["status"] == "NOTICE_BLOCKED"
    assert state.get("current_slot") == 2


def test_b1_03_huge_buffer_notice_embedding(e2e_env):
    """B1-03: Single notice line buried inside a 100KB buffer of benign log data is detected."""
    e2e_env.configure_slot(1, "huge_buffer@example.com")
    e2e_env.configure_slot(2, "healthy_peer@example.com")

    prefix_lines = ["I0915 normal info log event " * 10 for _ in range(300)]
    notice_line = "rpc error: code = PermissionDenied desc = user account is disabled"
    suffix_lines = ["I0915 trailing info log event " * 10 for _ in range(300)]
    all_lines = prefix_lines + [notice_line] + suffix_lines

    e2e_env.set_mock_instructions([
        {
            "action": "HUGE_STREAM",
            "log_lines": all_lines,
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Slot 2 ok",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "NOTICE_BLOCKED"
    assert state.get("current_slot") == 2


def test_b1_04_corrupted_utf8_in_notice(e2e_env):
    """B1-04: Notice line containing non-ASCII / latin-1 characters is handled safely without decode crash."""
    e2e_env.configure_slot(1, "corrupt_utf8@example.com")
    e2e_env.configure_slot(2, "healthy_utf8@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "CORRUPT_BYTES",
            "log_line": "Warning: \xc3\x28 account suspended by admin \xe2\x28 policy violation",
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
    assert state["slots"]["1"]["status"] == "NOTICE_BLOCKED"


def test_b1_05_benign_word_collision(e2e_env):
    """B1-05: Benign words like 'updating terms agreement' or 'privacy note' do not trigger false positive."""
    e2e_env.configure_slot(1, "benign_words@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "BENIGN_COLLISION",
            "log_lines": [
                "Notice: Updating terms agreement cache in local profile",
                "Privacy note: telemetric evaluation enabled",
                "Completed initialization"
            ],
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"
    assert state["slots"]["1"]["exhausted_until"] == 0
