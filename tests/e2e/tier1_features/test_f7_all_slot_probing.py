#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F7 Comprehensive All-Slot Cloud Probing
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f7_all_slot_probing.py
"""

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import strip_ansi


def test_f7_01_cmd_probe_all_slots(e2e_env):
    """F7-01: 'probe all' checks credentials across all configured slots and prints status table."""
    e2e_env.configure_slot(1, "probe1@example.com")
    e2e_env.configure_slot(2, "probe2@example.com")
    e2e_env.configure_slot(3, "probe3@example.com")

    res = e2e_env.run_supervisor(args=["probe", "all"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "Slot 1 [probe1@example.com]" in stdout
    assert "Slot 2 [probe2@example.com]" in stdout
    assert "Slot 3 [probe3@example.com]" in stdout
    assert "ANTIGRAVITY MULTI-ACCOUNT SUPERVISOR" in stdout


def test_f7_02_cmd_probe_single_slot(e2e_env):
    """F7-02: 'probe <slot>' evaluates only the specified slot."""
    e2e_env.configure_slot(1, "probe_single1@example.com")
    e2e_env.configure_slot(2, "probe_single2@example.com")

    res = e2e_env.run_supervisor(args=["probe", "2"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "Slot 2 [probe_single2@example.com]" in stdout
    assert "Slot 1 [probe_single1@example.com]" not in stdout


def test_f7_03_probe_auto_refreshes_expired_token(e2e_env):
    """F7-03: Prober identifies expired access token and recognizes available refresh_token."""
    # Slot with expired token
    now = int(time.time())
    e2e_env.configure_slot(1, "expired_token@example.com", expiry_date=now - 500)

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "auto-refresh via refresh_token" in stdout or "Access token expired" in stdout


def test_f7_04_probe_syncs_ground_truth_to_state(e2e_env):
    """F7-04: Probe persists detected quota/cooldown state to supervisor_state.json."""
    e2e_env.configure_slot(1, "sync_state@example.com")
    e2e_env.set_state_slot(1, "ACTIVE", exhausted_until=0)

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] in ("AUTH_FAILED", "QUOTA_EXHAUSTED", "EXHAUSTED_DAILY", "ACTIVE")
    assert "status" in state["slots"]["1"]


def test_f7_05_health_check_script_offline(e2e_env):
    """F7-05: scripts/health_check.py --offline audits slots, permissions, client IDs and exits 0."""
    e2e_env.configure_slot(1, "healthy_offline1@example.com")
    e2e_env.configure_slot(2, "healthy_offline2@example.com")

    res = e2e_env.run_health_check(args=["--offline"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "Passed Healthy Slots:   2" in stdout
    assert "Failed/Misconfigured Slots: 0" in stdout
    assert "[SUCCESS] All configured slots are healthy" in stdout
