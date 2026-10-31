#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B2 Auth Error Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b2_auth_boundaries.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import strip_ansi


def test_b2_01_malformed_json_in_401(e2e_env):
    """B2-01: Truncated or malformed JSON in HTTP 401 response is handled cleanly without parser crash."""
    e2e_env.configure_slot(1, "malformed_json@example.com")
    e2e_env.configure_slot(2, "healthy_b2@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "CORRUPT_401",
            "log_line": "401 Unauthorized\r\n{\"error\": \"unauthorized_client\", \"details\": [broken_json_without_closing_bracket",
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
    assert state["slots"]["1"]["status"] == "AUTH_FAILED"
    assert state.get("current_slot") == 2


def test_b2_02_empty_or_null_reason_string(e2e_env):
    """B2-02: Auth error with minimal error details sets safe default fallback message."""
    e2e_env.configure_slot(1, "minimal_auth@example.com")
    e2e_env.configure_slot(2, "healthy_peer@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "BARE_401",
            "log_line": "unauthorized_client",
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
    assert state["slots"]["1"]["status"] == "AUTH_FAILED"
    assert state["slots"]["1"].get("reason") is not None


def test_b2_03_http_400_syntax_vs_401(e2e_env):
    """B2-03: Generic client HTTP 400 Bad Request error does not trigger false auth failover."""
    e2e_env.configure_slot(1, "bad_syntax@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "HTTP_400",
            "log_line": "HTTP 400 Bad Request: Syntax error in model parameter 'temperature'",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"


def test_b2_04_multiple_auth_errors_single_chunk(e2e_env):
    """B2-04: Single chunk containing multiple auth error patterns resolves cleanly without state corruption."""
    e2e_env.configure_slot(1, "multi_auth@example.com")
    e2e_env.configure_slot(2, "healthy_peer@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "MULTI_AUTH",
            "log_line": "status code: 401 unauthorized\n403 Forbidden: credential_revoked",
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
    assert state["slots"]["1"]["status"] == "AUTH_FAILED"
    assert state.get("current_slot") == 2


def test_b2_05_single_slot_auth_failure(e2e_env):
    """B2-05: When only a single slot is configured and it fails auth, supervisor exits cleanly with guidance."""
    e2e_env.configure_slot(1, "sole_slot@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "401_ERR",
            "log_line": "Backend returned: 401 unauthorized",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "CIRCUIT BREAKER" in stdout or "AUTH_FAILED" in stdout
    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "AUTH_FAILED"
