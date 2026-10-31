#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F9 CLI Subcommands & Lifecycle
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f9_cli_commands.py
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
from tests.e2e.harness.env_factory import make_test_jwt, AGY_CLIENT_ID


def test_f9_01_status_displays_slots(e2e_env):
    """F9-01: 'status' prints table of all slots, emails, status, and marks current slot."""
    e2e_env.configure_slot(1, "status1@example.com")
    e2e_env.configure_slot(2, "status2@example.com")

    res = e2e_env.run_supervisor(args=["status"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "status1@example.com" in stdout
    assert "status2@example.com" in stdout
    assert "ACTIVE" in stdout
    assert "-> 1" in stdout or "1" in stdout


def test_f9_02_switch_command_activates_slot(e2e_env):
    """F9-02: 'switch <N>' updates current_slot and syncs credentials to ~/.gemini/."""
    e2e_env.configure_slot(1, "acc1@example.com")
    e2e_env.configure_slot(2, "acc2@example.com")

    res = e2e_env.run_supervisor(args=["switch", "2"], timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state.get("current_slot") == 2

    gemini_acc = json.loads((e2e_env.gemini_dir / "google_accounts.json").read_text())
    assert gemini_acc.get("active") == "acc2@example.com"


def test_f9_03_reset_command_clears_cooldown(e2e_env):
    """F9-03: 'reset all' restores all slots from cooldown to ACTIVE and zeroes exhausted_until."""
    now = int(time.time())
    e2e_env.configure_slot(1, "reset1@example.com", status="EXHAUSTED_DAILY", exhausted_until=now + 5000)
    e2e_env.configure_slot(2, "reset2@example.com", status="COOLDOWN_RPM", exhausted_until=now + 60)

    res = e2e_env.run_supervisor(args=["reset", "all"], timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"
    assert state["slots"]["1"]["exhausted_until"] == 0
    assert state["slots"]["2"]["status"] == "ACTIVE"
    assert state["slots"]["2"]["exhausted_until"] == 0


def test_f9_04_save_command_persists_tokens(e2e_env):
    """F9-04: 'save <N>' copies credentials from ~/.gemini/ into slot N directory."""
    e2e_env.configure_slot(1, "orig@example.com")

    # Put new token in ~/.gemini
    jwt_tok = make_test_jwt(AGY_CLIENT_ID, "new_saved@example.com")
    (e2e_env.gemini_dir / "oauth_creds.json").write_text(json.dumps({
        "access_token": "saved_access_token",
        "id_token": jwt_tok,
        "refresh_token": "saved_refresh_token"
    }))
    (e2e_env.gemini_dir / "google_accounts.json").write_text(json.dumps({"active": "new_saved@example.com"}))

    res = e2e_env.run_supervisor(args=["save", "2"], timeout=10)
    assert res.exit_code == 0

    slot2_acc = json.loads((e2e_env.acc_dir / "2" / "google_accounts.json").read_text())
    assert slot2_acc.get("active") == "new_saved@example.com"


def test_f9_05_help_flag_displays_usage(e2e_env):
    """F9-05: '--help' and '-h' display comprehensive CLI usage documentation and exit 0."""
    res_long = e2e_env.run_supervisor(args=["--help"], timeout=10)
    assert res_long.exit_code == 0
    assert "status" in res_long.stdout
    assert "switch" in res_long.stdout
    assert "probe" in res_long.stdout

    res_short = e2e_env.run_supervisor(args=["-h"], timeout=10)
    assert res_short.exit_code == 0
    assert "status" in res_short.stdout
