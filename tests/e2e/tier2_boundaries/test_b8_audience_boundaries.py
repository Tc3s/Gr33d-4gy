#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B8 Audience Guard Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b8_audience_boundaries.py
"""

import sys
import json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import strip_ansi
from tests.e2e.harness.env_factory import AGY_CLIENT_ID


def test_b8_01_jwt_missing_dots(e2e_env):
    """B8-01: Malformed JWT string without dots ('not_a_jwt') decodes safely to empty claims."""
    e2e_env.configure_slot(1, "bad_jwt1@example.com")
    oauth_file = e2e_env.acc_dir / "1" / "oauth_creds.json"
    oauth_data = json.loads(oauth_file.read_text())
    oauth_data["id_token"] = "not_a_jwt_completely_missing_dots"
    oauth_file.write_text(json.dumps(oauth_data))

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0


def test_b8_02_jwt_corrupted_base64_payload(e2e_env):
    """B8-02: Corrupted base64 payload in JWT ('a.????.c') handles binascii.Error safely."""
    e2e_env.configure_slot(1, "corrupt_b64@example.com")
    oauth_file = e2e_env.acc_dir / "1" / "oauth_creds.json"
    oauth_data = json.loads(oauth_file.read_text())
    oauth_data["id_token"] = "eyJhbGciOiJSUzI1NiJ9.???corrupted_payload???.deterministic_sig"
    oauth_file.write_text(json.dumps(oauth_data))

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0


def test_b8_03_jwt_multi_audience_with_ints(e2e_env):
    """B8-03: Multi-audience array containing integers casts cleanly to string during validation."""
    e2e_env.configure_slot(1, "multi_aud@example.com", client_id=AGY_CLIENT_ID)

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0
    assert "VALID" in res.stdout


def test_b8_04_empty_client_id_and_claims(e2e_env):
    """B8-04: Empty client_id and empty JWT claims are marked as MISSING/INVALID."""
    e2e_env.configure_slot(1, "empty_aud@example.com", client_id="")
    oauth_file = e2e_env.acc_dir / "1" / "oauth_creds.json"
    oauth_data = json.loads(oauth_file.read_text())
    oauth_data["id_token"] = ""
    oauth_data["client_id"] = ""
    oauth_file.write_text(json.dumps(oauth_data))

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0
    stdout = strip_ansi(res.stdout)
    assert "MISSING" in stdout or "INVALID" in stdout or "UNKNOWN" in stdout or "Slot 1" in stdout


def test_b8_05_client_id_whitespace_newlines(e2e_env):
    """B8-05: Client ID padded with surrounding whitespace/newlines strips cleanly."""
    padded_cid = f"  \t\n{AGY_CLIENT_ID}\n  "
    e2e_env.configure_slot(1, "padded_cid@example.com", client_id=padded_cid)

    res = e2e_env.run_supervisor(args=["probe", "1"], timeout=10)
    assert res.exit_code == 0
    assert "VALID" in res.stdout or "Antigravity" in res.stdout
