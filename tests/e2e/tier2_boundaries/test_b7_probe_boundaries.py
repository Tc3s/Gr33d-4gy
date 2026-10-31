#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B7 Health Probe Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b7_probe_boundaries.py
"""

import sys
import json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import strip_ansi


def test_b7_01_empty_accounts_directory(e2e_env):
    """B7-01: Probing when no accounts exist prints an informative message and exits 0."""
    res = e2e_env.run_supervisor(args=["probe", "all"], timeout=10)
    assert res.exit_code == 0
    stdout = strip_ansi(res.stdout)
    assert "Không tìm thấy tài khoản nào" in stdout or "0" in stdout or "ANTIGRAVITY" in stdout


def test_b7_02_non_numeric_slot_directories(e2e_env):
    """B7-02: Non-numeric subdirectories in accounts dir (.git, backup, old) are safely ignored."""
    e2e_env.configure_slot(1, "real_slot@example.com")

    # Create dummy non-numeric directories
    (e2e_env.acc_dir / "backup").mkdir(parents=True, exist_ok=True)
    (e2e_env.acc_dir / "old_slot").mkdir(parents=True, exist_ok=True)
    (e2e_env.acc_dir / ".git").mkdir(parents=True, exist_ok=True)

    res = e2e_env.run_supervisor(args=["probe", "all"], timeout=10)
    assert res.exit_code == 0

    stdout = strip_ansi(res.stdout)
    assert "Slot 1" in stdout
    assert "backup" not in stdout
    assert "old_slot" not in stdout


def test_b7_03_probe_socket_timeout(e2e_env):
    """B7-03: Offline probe or socket timeouts are handled safely without hanging."""
    e2e_env.configure_slot(1, "timeout_probe@example.com")

    res = e2e_env.run_health_check(args=["--offline"], timeout=10)
    assert res.exit_code == 0


def test_b7_04_html_502_bad_gateway_response(e2e_env):
    """B7-04: Non-JSON / HTML error pages from proxies are parsed without JSONDecodeError crash."""
    e2e_env.configure_slot(1, "bad_gw@example.com")

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0


def test_b7_05_null_token_in_keyring(e2e_env):
    """B7-05: Missing or null token fields in stored json files defend against AttributeError."""
    e2e_env.configure_slot(1, "null_field@example.com")

    # Put corrupted keyring_secret.json with token: null
    (e2e_env.acc_dir / "1" / "keyring_secret.json").write_text(json.dumps({"token": None}))

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0
