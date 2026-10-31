#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B9 CLI Arguments Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b9_cli_boundaries.py
"""

import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_b9_01_switch_negative_or_zero_slot(e2e_env):
    """B9-01: 'switch 0' or 'switch -1' fails cleanly with exit code 1."""
    res_zero = e2e_env.run_supervisor(args=["switch", "0"], timeout=10)
    assert res_zero.exit_code == 1

    res_neg = e2e_env.run_supervisor(args=["switch", "-1"], timeout=10)
    assert res_neg.exit_code == 1


def test_b9_02_switch_unconfigured_slot(e2e_env):
    """B9-02: 'switch 999' on unconfigured non-existent slot exits with code 1."""
    e2e_env.configure_slot(1, "valid_slot@example.com")

    res = e2e_env.run_supervisor(args=["switch", "999"], timeout=10)
    assert res.exit_code == 1
    assert "chưa có dữ liệu" in res.stdout or "Lỗi" in res.stdout or "Error" in res.stdout


def test_b9_03_save_invalid_slot_argument(e2e_env):
    """B9-03: 'save abc' with non-numeric slot name fails with exit code 1."""
    res = e2e_env.run_supervisor(args=["save", "abc"], timeout=10)
    assert res.exit_code == 1


def test_b9_04_reset_unknown_slot(e2e_env):
    """B9-04: 'reset 999' on non-existent slot reports message without crashing valid slots."""
    e2e_env.configure_slot(1, "keep_valid@example.com")

    res = e2e_env.run_supervisor(args=["reset", "999"], timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "ACTIVE"


def test_b9_05_health_check_chmod_000(e2e_env):
    """B9-05: Unreadable accounts directory causes health_check.py to exit with code 1."""
    e2e_env.configure_slot(1, "perm_error@example.com")
    e2e_env.acc_dir.chmod(0o000)

    try:
        res = e2e_env.run_health_check(args=["--offline"], timeout=10)
        assert res.exit_code == 1
    finally:
        # Restore permissions for cleanup
        e2e_env.acc_dir.chmod(0o700)
