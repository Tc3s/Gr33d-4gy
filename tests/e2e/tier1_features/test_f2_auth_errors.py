#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F2 HTTP 401/403 Auth Error Detection
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f2_auth_errors.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import strip_ansi


def test_f2_01_unauthorized_client(e2e_env):
    """F2-01: unauthorized_client error isolates slot as AUTH_FAILED and switches to Slot 2."""
    e2e_env.configure_slot(1, "unauth@example.com")
    e2e_env.configure_slot(2, "healthy@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "401_ERR",
            "log_line": "OAuth error response: unauthorized_client: The client is not authorized.",
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
    assert slot1.get("status") == "AUTH_FAILED"
    assert state.get("current_slot") == 2


def test_f2_02_grpc_permission_denied(e2e_env):
    """F2-02: gRPC code = PermissionDenied triggers AUTH_FAILED classification and failover."""
    e2e_env.configure_slot(1, "perm_denied@example.com")
    e2e_env.configure_slot(2, "healthy_perm@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "PERM_DENIED",
            "log_line": "rpc error: code = PermissionDenied desc = The caller does not have permission",
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
    assert state["slots"]["1"]["status"] == "AUTH_FAILED"
    assert state.get("current_slot") == 2


def test_f2_03_invalid_grant_revocation(e2e_env):
    """F2-03: invalid_grant token revocation sets indefinite cooldown and does not retry slot."""
    e2e_env.configure_slot(1, "revoked_grant@example.com")
    e2e_env.configure_slot(2, "active_peer@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "INVALID_GRANT",
            "log_line": "Failed to refresh token: oauth2: cannot fetch token: 400 Bad Request Response: {\"error\": \"invalid_grant\", \"error_description\": \"Token has been expired or revoked.\"}",
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
    assert slot1.get("status") == "AUTH_FAILED"
    assert slot1.get("exhausted_until", 0) > time.time() + (5 * 365 * 86400)


def test_f2_04_http_401_unauthorized(e2e_env):
    """F2-04: HTTP 401 Unauthorized status code triggers failover."""
    e2e_env.configure_slot(1, "http401@example.com")
    e2e_env.configure_slot(2, "healthy_http@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "HTTP_401",
            "log_line": "Backend returned status code: 401 Unauthorized for endpoint streamGenerateContent",
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


def test_f2_05_subscription_required(e2e_env):
    """F2-05: subscription_required 403 entitlement error triggers failover to healthy account."""
    e2e_env.configure_slot(1, "unsubscribed@example.com")
    e2e_env.configure_slot(2, "pro_tier@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "SUB_REQ",
            "log_line": "Error from server: subscription_required. Account does not possess Gemini Advanced entitlement.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Connected",
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
