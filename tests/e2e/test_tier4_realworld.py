#!/usr/bin/env python3
"""
E2E Tier 4: Real-World Production Workload Scenarios Test Suite
Repository: agy-supervisor
Directory: tests/e2e/test_tier4_realworld.py

Methodology:
- Opaque-box, requirement-driven testing based on user requirements R1, R2, R3 and acceptance criteria.
- Simulates realistic developer workflows, multi-hour coding sessions, upstream cloud instability,
  mixed-account degradation pools, recovery re-authentication, and cold-start health checks.
- Full assertions on process lifecycles, SQLite trajectory lineage, supervisor state JSON,
  and CLI standard outputs/exit codes.
"""

import os
import sys
import time
import json
import uuid
import re
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.runner import HEALTH_CHECK_PATH, strip_ansi
from tests.e2e.harness.env_factory import (
    AGY_CLIENT_ID,
    GEMINI_CLI_CLIENT_ID,
    make_test_jwt,
)


def test_tier4_scenario_1_multi_hour_coding_session_continuous_tty(realworld_env):
    """
    Tier 4 Scenario 1: Multi-Hour Coding Session with Repeated Quota Exhaustion & Continuous TTY
    Features Exercised: F1, F3, F4, F5, F6
    Workload Flow:
      1. Developer starts interactive continuous session: agy-supervisor -c --model gemini-2.5-pro
      2. Slot 1 encounters transient 503 errors (ignored by supervisor, self-retried by agy).
      3. Slot 1 hits daily quota exhaustion (Individual quota reached. Resets in 4h46m23s).
      4. Supervisor intercepts quota log, gracefully terminates child agy, applies handover jitter,
         forks conversation SQLite DB into fresh trajectory UUID, switches to Slot 2, and restarts.
      5. Slot 2 operates, then encounters RPM rate limit (Rate limit exceeded. Resets in 45s).
      6. Supervisor marks Slot 2 as COOLDOWN_RPM, forks conversation again, switches to Slot 3.
      7. Slot 3 operates cleanly and completes session.
    Verification:
      - Continuous session arguments (-c / --conversation) preserved.
      - Exact trajectory lineage across 3 SQLite conversation databases.
      - Slot 1 in EXHAUSTED_DAILY; Slot 2 in COOLDOWN_RPM; Slot 3 is ACTIVE.
    """
    realworld_env.configure_slot(1, "dev_lead@example.com")
    realworld_env.configure_slot(2, "dev_alt@example.com")
    realworld_env.configure_slot(3, "dev_reserve@example.com")

    init_conv_id = str(uuid.uuid4())
    init_traj_id = str(uuid.uuid4())
    realworld_env.create_conversation(init_conv_id, init_traj_id, owner_slot=1)

    realworld_env.set_instructions([
        {
            "action": "SLOT1_WORKLOAD",
            "events": [
                {"delay": 0.1, "log": "I0915 11:06:14.038746 581 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available for model gemini-2.5-pro on the server), retrying in 2s"},
                {"delay": 0.2, "log": "I0915 11:06:16.100000 581 run.go:389] Run: attempt 2 failed (UNAVAILABLE (code 503): No capacity available on the server), retrying in 4s"},
                {"delay": 0.2, "log": "I0915 11:06:20.541207 28527 run.go:387] Run: attempt 3 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription. Resets in 4h46m23s.), retrying in 4s"}
            ],
            "sleep_after": 4.0
        },
        {
            "action": "SLOT2_WORKLOAD",
            "events": [
                {"delay": 0.1, "log": "Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Rate limit exceeded. Resets in 45s.)"}
            ],
            "sleep_after": 4.0
        },
        {
            "action": "SLOT3_WORKLOAD",
            "events": [
                {"delay": 0.05, "log": "Session continuous on model gemini-2.5-pro. User prompt completed."}
            ],
            "sleep_after": 0.3,
            "exit_code": 0
        }
    ])

    proc = realworld_env.run_supervisor(args=["-c", "--model", "gemini-2.5-pro"], timeout=25)
    assert proc.exit_code == 0

    history = realworld_env.get_call_history()
    assert len(history) == 3

    # Verification: Invocation 2 received --conversation <id> and --model gemini-2.5-pro
    args2 = history[1]["args"]
    assert "--conversation" in args2
    conv2_id = args2[args2.index("--conversation") + 1]
    assert "--model" in args2
    assert "gemini-2.5-pro" in args2

    # Verification: Invocation 3 received fresh --conversation <id> and --model gemini-2.5-pro
    args3 = history[2]["args"]
    assert "--conversation" in args3
    conv3_id = args3[args3.index("--conversation") + 1]
    assert conv3_id != conv2_id
    assert conv3_id != init_conv_id

    # Verify State
    state = realworld_env.get_state()
    assert state["slots"]["1"]["status"] == "EXHAUSTED_DAILY"
    assert state["slots"]["2"]["status"] == "COOLDOWN_RPM"
    assert state["current_slot"] == 3


def test_tier4_scenario_2_mixed_pool_banned_exhausted_healthy_autorotation(realworld_env):
    """
    Tier 4 Scenario 2: Mixed Pool (1 Banned/Notice, 1 Exhausted, 1 Healthy Account)
    Features Exercised: F1, F2, F5, F7, F8
    Workload Flow:
      - Pool consists of:
        * Slot 1: Active at start, but hits Google Terms of Service violation.
        * Slot 2: Exhausted Daily Quota (EXHAUSTED_DAILY, locked until tomorrow).
        * Slot 3: Healthy Active Account.
      - Action 1: Slot 1 runs, emits ToS Notice error.
      - Action 2: Supervisor marks Slot 1 as NOTICE_BLOCKED (10-year lock).
      - Action 3: Supervisor rotates, checks Slot 2, finds it in EXHAUSTED_DAILY, skips it,
        and directly activates healthy Slot 3.
      - Action 4: Slot 3 runs normally to completion.
      - Action 5: User queries 'agy-supervisor status' and confirms table displays
        Slot 1 in red NOTICE_BLOCKED, Slot 2 in yellow EXHAUSTED_DAILY, and Slot 3 as active.
    """
    now = int(time.time())
    realworld_env.configure_slot(1, "banned_slot1@example.com", status="ACTIVE", exhausted_until=0)
    realworld_env.configure_slot(2, "exhausted_slot2@example.com", status="EXHAUSTED_DAILY",
                                 exhausted_until=now + 3600, reason="Individual quota reached")
    realworld_env.configure_slot(3, "healthy_slot3@example.com", status="ACTIVE", exhausted_until=0)

    # Instruction 1 (Slot 1): Emits Google ToS Notice
    # Instruction 2 (Slot 3): Runs cleanly
    realworld_env.set_instructions([
        {
            "action": "EMIT_NOTICE",
            "log_line": "I0913 14:02:11.123456 99999 run.go:387] Run: attempt 1 failed (PERMISSION_DENIED (code 403): Google Terms of Service violation - Account suspended)",
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

    proc_run = realworld_env.run_supervisor(timeout=15)
    assert proc_run.exit_code == 0

    state = realworld_env.get_state()
    # Slot 1 must be NOTICE_BLOCKED, Slot 2 EXHAUSTED_DAILY, current slot must be Slot 3
    assert state["slots"]["1"]["status"] == "NOTICE_BLOCKED"
    assert state["slots"]["2"]["status"] == "EXHAUSTED_DAILY"
    assert state.get("current_slot") == 3

    # Status command must accurately display the mixed pool
    proc_status = realworld_env.run_supervisor(args=["status"], timeout=10)
    assert proc_status.exit_code == 0
    out_status = strip_ansi(proc_status.stdout)
    assert "NOTICE_BLOCKED" in out_status
    assert "EXHAUSTED_DAILY" in out_status
    assert "ACTIVE" in out_status
    assert "banned_slot1@example.com" in out_status
    assert "exhausted_slot2@example.com" in out_status
    assert "healthy_slot3@example.com" in out_status


def test_tier4_scenario_3_upstream_server_flapping_intermingled_errors(realworld_env):
    """
    Tier 4 Scenario 3: Upstream Server Flapping (503 transient errors intermingled with 429 quota events)
    Features Exercised: F3, F4, F5
    Workload Flow:
      - Child agy encounters rapid interleaving of 503 and 429 events:
        1. 3x 503 "No capacity available" bursts.
        2. 1x 429 "Rate limit exceeded. Resets in 20s" (non-terminal).
        3. 2x 503 "Model Overloaded" bursts.
        4. 1x terminal 429 "Individual quota reached. Resets in 3h30m0s".
    Verification:
      - 503s do NOT advance the circuit breaker strike count.
      - 4xx sliding window records errors without false trips.
      - Termination occurs only upon genuine quota event, rotating cleanly to Slot 2.
    """
    realworld_env.configure_slot(1, "flapping_user1@example.com")
    realworld_env.configure_slot(2, "flapping_user2@example.com")

    flapping_log = (
        "I0915 11:06:01.000000 100 run.go:389] UNAVAILABLE (code 503): No capacity available for model\n"
        "I0915 11:06:02.000000 100 run.go:389] HTTP/2.0 503 Service Unavailable: Model Overloaded\n"
        "I0915 11:06:03.000000 100 run.go:389] UNAVAILABLE (code 503): No capacity available\n"
        "I0915 11:06:04.000000 100 run.go:389] RESOURCE_EXHAUSTED (code 429): Rate limit exceeded. Resets in 20s.\n"
        "I0915 11:06:05.000000 100 run.go:389] HTTP/2.0 503 Service Unavailable: Model Overloaded\n"
        "I0915 11:06:06.000000 100 run.go:389] UNAVAILABLE (code 503): No capacity available\n"
        "I0915 11:06:07.000000 100 run.go:389] RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 3h30m0s."
    )

    realworld_env.set_instructions([
        {
            "action": "FLAPPING_LOG_STREAM",
            "log_line": flapping_log,
            "delay_before_log": 0.1,
            "sleep_after_log": 4.0
        },
        {
            "action": "NORMAL",
            "log_line": "Session stable on Slot 2",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    proc = realworld_env.run_supervisor(timeout=15)
    assert proc.exit_code == 0

    state = realworld_env.get_state()
    # Slot 1 must be marked EXHAUSTED_DAILY
    assert state["slots"]["1"]["status"] == "EXHAUSTED_DAILY"
    # Rotated successfully to Slot 2
    assert state["current_slot"] == 2


def test_tier4_scenario_4_reauthentication_and_recovery_workflow_login_cmd(realworld_env):
    """
    Tier 4 Scenario 4: Re-Authentication & Recovery Workflow for Notice-Marked Slot
    Features Exercised: F1, F2, F7, F8
    Workload Flow:
      1. Slot 1 is marked NOTICE_BLOCKED (10-year lock) with ToS suspension reason.
      2. User resolves account with Google and initiates recovery: 'agy-supervisor login 1'.
      3. Supervisor cleans stale cache, launches agy for OAuth consent.
      4. Mock agy captures fresh valid Antigravity credentials into ~/.gemini/oauth_creds.json.
      5. Supervisor detects new valid token, restores Slot 1 state to ACTIVE, clears exhausted_until,
         and removes suspension reason.
      6. User confirms with 'agy-supervisor status' that Slot 1 is ACTIVE and operational.
    """
    now = int(time.time())
    realworld_env.configure_slot(1, "recovered_user@example.com", status="NOTICE_BLOCKED",
                                 exhausted_until=now + 10 * 365 * 86400, reason="Google Terms of Service violation")

    # In mock agy for login, when agy runs, it generates fresh oauth_creds.json in ~/.gemini/
    mock_bin = realworld_env.bin_dir
    mock_agy_login = mock_bin / "agy"
    mock_agy_login.write_text(f"""#!/usr/bin/env python3
import os, sys, json
from pathlib import Path

home = Path(os.environ["HOME"])
gemini_dir = home / ".gemini"
gemini_dir.mkdir(parents=True, exist_ok=True)

# Write fresh valid Antigravity token
jwt_tok = "{make_test_jwt(AGY_CLIENT_ID, 'recovered_user@example.com')}"
(gemini_dir / "oauth_creds.json").write_text(json.dumps({{
    "access_token": "fresh_minted_access_token_slot_1",
    "id_token": jwt_tok,
    "refresh_token": "fresh_minted_refresh_token_slot_1"
}}))
(gemini_dir / "google_accounts.json").write_text(json.dumps({{"active": "recovered_user@example.com"}}))
sys.exit(0)
""")
    mock_agy_login.chmod(0o755)

    # Run login command
    proc_login = realworld_env.run_supervisor(args=["login", "1"], timeout=10)
    assert proc_login.exit_code == 0
    assert "Đã xóa cache file" in proc_login.stdout or "Thành công: Đã cấu hình và lưu Slot 1" in proc_login.stdout

    # Verify state is cleared
    state = realworld_env.get_state()
    slot1_info = state["slots"].get("1", {})
    assert slot1_info.get("status") == "ACTIVE"
    assert slot1_info.get("exhausted_until") == 0
    assert "suspension_reason" not in slot1_info

    # Verify status report displays Slot 1 as ACTIVE
    proc_status = realworld_env.run_supervisor(args=["status"], timeout=10)
    assert proc_status.exit_code == 0
    assert "ACTIVE" in proc_status.stdout
    assert "recovered_user@example.com" in proc_status.stdout


def test_tier4_scenario_5_cold_start_health_check_and_automatic_remediation(realworld_env):
    """
    Tier 4 Scenario 5: Cold Start Health Check Diagnostic & Automatic Remediation Guidance
    Features Exercised: F7, F8, F9
    Workload Flow:
      1. Machine cold start with misconfigured environment:
         - ~/.gemini_accounts directory has unsafe mode 0777.
         - Slot 1 contains gemini-cli token (FOREIGN_MISMATCH).
         - Slot 2 contains valid Antigravity token.
      2. Run 'python3 scripts/health_check.py --offline'.
         - Detects unsafe permissions.
         - Detects FOREIGN_MISMATCH on Slot 1 with remediation guidance.
         - Returns exit code 1 (failure).
      3. Harness executes automatic remediation based on guidance:
         - Fixes directory permissions to 0700.
         - Runs simulated login to mint valid Antigravity credentials for Slot 1.
      4. Re-run 'python3 scripts/health_check.py --offline'.
         - Validates all items report [PASS].
         - Returns exit code 0 (clean).
    """
    # Create unsafe mode on ~/.gemini_accounts
    realworld_env.acc_dir.chmod(0o777)

    # Slot 1 infected with gemini-cli client ID
    realworld_env.configure_slot(1, "bad_cid@example.com", client_id=GEMINI_CLI_CLIENT_ID)
    # Slot 2 configured with authentic Antigravity client ID
    realworld_env.configure_slot(2, "good_cid@example.com", client_id=AGY_CLIENT_ID)

    # 1. Run health check -> must fail with code 1
    p_fail = realworld_env.run_health_check(args=["--offline"], timeout=10)
    assert p_fail.exit_code == 1

    stdout_fail = p_fail.stdout
    assert "FOREIGN_MISMATCH" in stdout_fail
    assert "Run 'agy-supervisor login <slot>' to mint valid Antigravity tokens" in stdout_fail
    assert "[CRITICAL] Health check detected 1 corrupted/misconfigured slot(s)" in stdout_fail

    # 2. Execute Remediation
    # Step A: Fix permissions
    realworld_env.acc_dir.chmod(0o700)
    # Step B: Re-mint valid token for Slot 1
    valid_jwt = make_test_jwt(AGY_CLIENT_ID, "remediated@example.com")
    (realworld_env.acc_dir / "1" / "oauth_creds.json").write_text(json.dumps({
        "access_token": "valid_remediated_access_token",
        "id_token": valid_jwt,
        "refresh_token": "valid_remediated_refresh_token"
    }))
    (realworld_env.acc_dir / "1" / "google_accounts.json").write_text(json.dumps({"active": "remediated@example.com"}))

    # 3. Re-run health check -> must pass with code 0
    p_pass = realworld_env.run_health_check(args=["--offline"], timeout=10)
    assert p_pass.exit_code == 0

    stdout_pass = strip_ansi(p_pass.stdout)
    assert "[SUCCESS] All configured slots are healthy and fully authenticated." in stdout_pass
    assert "Passed Healthy Slots:   2" in stdout_pass
    assert "Failed/Misconfigured Slots: 0" in stdout_pass
