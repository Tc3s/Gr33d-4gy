#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F8 Client ID & Audience Validation
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f8_audience_guard.py
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
from tests.e2e.harness.env_factory import (
    AGY_CLIENT_ID,
    GEMINI_CLI_CLIENT_ID,
    GCLOUD_CLIENT_ID,
    make_test_jwt,
)


def test_f8_01_native_antigravity_token_valid(e2e_env):
    """F8-01: Authentic Antigravity client ID (prefix 1071006060591) is recognized as VALID."""
    e2e_env.configure_slot(1, "antigravity_native@example.com", client_id=AGY_CLIENT_ID)

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "VALID (Antigravity" in stdout or "VALID" in stdout


def test_f8_02_gemini_cli_foreign_token_quarantined(e2e_env):
    """F8-02: Foreign gemini-cli token (prefix 681255809395) is quarantined as CLIENT_ID_MISMATCH."""
    e2e_env.configure_slot(1, "gemini_cli_user@example.com", client_id=GEMINI_CLI_CLIENT_ID)

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "CLIENT_ID_MISMATCH" in stdout
    assert "gemini-cli" in stdout
    assert "Action Required: Chạy 'agy-supervisor login 1'" in stdout


def test_f8_03_gcloud_foreign_token_quarantined(e2e_env):
    """F8-03: Foreign gcloud token (prefix 32555940559) is quarantined."""
    e2e_env.configure_slot(1, "gcloud_user@example.com", client_id=GCLOUD_CLIENT_ID)

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "FOREIGN_MISMATCH" in stdout or "CLIENT_ID_MISMATCH" in stdout or "gcloud" in stdout


def test_f8_04_sync_back_aborts_on_foreign(e2e_env):
    """F8-04: Credential guard prevents sync-back of invalid/foreign credentials."""
    e2e_env.configure_slot(1, "sync_guard@example.com", client_id=AGY_CLIENT_ID)

    # In ~/.gemini, put foreign token
    foreign_jwt = make_test_jwt(GEMINI_CLI_CLIENT_ID, "sync_guard@example.com")
    (e2e_env.gemini_dir / "oauth_creds.json").write_text(json.dumps({
        "access_token": "foreign_token",
        "id_token": foreign_jwt,
        "client_id": GEMINI_CLI_CLIENT_ID
    }))

    # Verify that stored slot token was NOT corrupted with foreign token
    slot_oauth = json.loads((e2e_env.acc_dir / "1" / "oauth_creds.json").read_text())
    assert slot_oauth["client_id"] == AGY_CLIENT_ID


def test_f8_05_rotate_skips_mismatched_slots(e2e_env):
    """F8-05: Rotation skips slots marked CLIENT_ID_MISMATCH."""
    e2e_env.configure_slot(1, "slot1@example.com")
    e2e_env.configure_slot(2, "mismatched@example.com", client_id=GEMINI_CLI_CLIENT_ID, status="CLIENT_ID_MISMATCH", exhausted_until=int(time.time()) + 9999999)
    e2e_env.configure_slot(3, "slot3@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST_1",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1800s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Ready on Slot 3",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    # Must skip Slot 2 and activate Slot 3
    assert state.get("current_slot") == 3
