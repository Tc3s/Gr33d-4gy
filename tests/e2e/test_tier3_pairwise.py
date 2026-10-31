#!/usr/bin/env python3
"""
E2E Tier 3: Cross-Feature Pairwise Combinations Test Suite
Repository: agy-supervisor
Directory: tests/e2e/test_tier3_pairwise.py

Methodology:
- Opaque-box subprocess invocation of bin/agy-supervisor and scripts/health_check.py.
- Hermetic filesystem sandboxing via isolated temporary HOME directory.
- Dynamic PATH injection of mock agy executable to simulate child process runtime behaviors.
- Pairwise combinatorial testing verifying interactions between features F1 through F10.
"""

import os
import sys
import time
import json
import uuid
import sqlite3
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.env_factory import (
    AGY_CLIENT_ID,
    GEMINI_CLI_CLIENT_ID,
)


# ==============================================================================
# TIER 3 PAIRWISE TEST CASES (COVERING FEATURES F1 - F10)
# ==============================================================================

def test_pairwise_f1_notice_and_f4_thrashing_circuit_breaker(e2e_env):
    """
    TC-T3-01: F1 (Notice Detection) x F4 (4xx Thrashing Storm Breaker)
    Verifies that a fatal Google Terms of Service / Account Notice isolates the slot
    with a 10-year lock (NOTICE_BLOCKED) WITHOUT thrashing the 4xx circuit breaker
    into false multi-strike backoff for innocent accounts.
    """
    e2e_env.configure_slot(1, "notice_victim@example.com")
    e2e_env.configure_slot(2, "healthy_account@example.com")

    # Instruction: Slot 1 emits Google ToS suspension notice, Slot 2 runs normally
    e2e_env.set_mock_instructions([
        {
            "action": "EMIT_NOTICE",
            "log_line": "I0913 14:02:11.123456 99999 run.go:387] Run: attempt 1 failed (PERMISSION_DENIED (code 403): Google Terms of Service violation - Account suspended)",
            "delay_before_log": 0.15,
            "sleep_after_log": 4.0
        },
        {
            "action": "NORMAL",
            "log_line": "Session started successfully on model gemini-2.5-pro",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    slot1_info = state["slots"].get("1", {})
    assert slot1_info.get("status") == "NOTICE_BLOCKED"
    # Notice must impose indefinite (>= 10 year) cooling
    assert slot1_info.get("exhausted_until", 0) > time.time() + (5 * 365 * 86400)
    assert "Terms of Service" in (slot1_info.get("reason") or slot1_info.get("suspension_reason"))

    # Active slot transitioned to Slot 2
    assert state.get("current_slot") == 2

    # Assert circuit breaker remains CLOSED and did not trip into multi-strike backoff
    cb_state = state.get("circuit_breaker", {})
    assert cb_state.get("state") == "CLOSED"
    assert cb_state.get("strike_count", 0) <= 1


def test_pairwise_f3_503_transient_flapping_and_f5_adaptive_cooling(e2e_env):
    """
    TC-T3-02: F3 (503 Transient Flapping) x F5 (Adaptive Cooling & Quota Exhaustion)
    Verifies that upstream 503 transient overload errors are completely bypassed
    (allowing agy child to self-retry), and subsequent genuine 429 quota exhaustion
    correctly triggers adaptive cooling with calibrated handover jitter [2.5, 5.0]s.
    """
    e2e_env.configure_slot(1, "worker1@example.com")
    e2e_env.configure_slot(2, "worker2@example.com")

    # Instruction 1: Slot 1 emits 2x 503s, then a genuine 429 quota exhaustion
    # Instruction 2: Slot 2 starts and completes normally
    e2e_env.set_mock_instructions([
        {
            "action": "FLAP_503_THEN_429",
            "log_line": "I0915 11:06:14.038746 581 run.go:389] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server), retrying in 4s\nRun: attempt 2 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1800s.)",
            "delay_before_log": 0.15,
            "sleep_after_log": 4.0
        },
        {
            "action": "NORMAL",
            "log_line": "Code assistance ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    t_start = time.time()
    res = e2e_env.run_supervisor(timeout=15)
    t_elapsed = time.time() - t_start

    assert res.exit_code == 0
    # Must observe handover jitter pause (>= 2.5s)
    assert t_elapsed >= 2.5
    assert "Handover Jitter: Tạm dừng" in res.stdout

    state = e2e_env.get_state()
    # Slot 1 must be marked EXHAUSTED_DAILY with calculated window
    slot1 = state["slots"].get("1", {})
    assert slot1.get("status") == "EXHAUSTED_DAILY"
    assert slot1.get("exhausted_until", 0) >= int(time.time()) + 1750
    assert state.get("current_slot") == 2


def test_pairwise_f6_session_trajectory_isolation_and_f5_rapid_slot_rotation(e2e_env):
    """
    TC-T3-03: F6 (Session Trajectory Isolation) x F5 (Adaptive Cooling & Rapid Slot Rotation)
    Verifies that when continuing a chat session (-c) across successive quota rotations
    (Slot 1 -> Slot 2 -> Slot 3), each rotation safely forks the SQLite conversation,
    allocates fresh trajectory UUIDs to prevent Sybil clustering, and passes --conversation <id>.
    """
    e2e_env.configure_slot(1, "traj_user1@example.com")
    e2e_env.configure_slot(2, "traj_user2@example.com")
    e2e_env.configure_slot(3, "traj_user3@example.com")

    # Seed initial conversation owned by Slot 1
    init_conv_id = str(uuid.uuid4())
    init_traj_id = str(uuid.uuid4())
    e2e_env.create_conversation(init_conv_id, init_traj_id, owner_slot=1)

    # Instruction 1: Slot 1 exhausts quota
    # Instruction 2: Slot 2 exhausts quota
    # Instruction 3: Slot 3 runs normally
    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST_1",
            "log_line": "Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 3600s.)",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "EXHAUST_2",
            "log_line": "Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 3600s.)",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Session stable on Slot 3",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["-c"], timeout=20)
    assert res.exit_code == 0

    history = e2e_env.get_call_history()
    assert len(history) == 3

    # Invocation 1 on Slot 1 launched with -c
    assert "-c" in history[0]["args"]

    # Invocation 2 on Slot 2 must have intercepted account boundary and passed --conversation <id>
    assert "--conversation" in history[1]["args"]
    conv_id_2 = history[1]["args"][history[1]["args"].index("--conversation") + 1]
    assert conv_id_2 != init_conv_id

    # Invocation 3 on Slot 3 must have forked conv_id_2 into conv_id_3
    assert "--conversation" in history[2]["args"]
    conv_id_3 = history[2]["args"][history[2]["args"].index("--conversation") + 1]
    assert conv_id_3 != conv_id_2
    assert conv_id_3 != init_conv_id

    # Verify SQLite database files exist and have distinct trajectory_ids
    db2 = e2e_env.conv_dir / f"{conv_id_2}.db"
    db3 = e2e_env.conv_dir / f"{conv_id_3}.db"
    assert db2.exists()
    assert db3.exists()

    conn2 = sqlite3.connect(db2)
    traj2 = conn2.execute("SELECT trajectory_id FROM trajectory_meta;").fetchone()[0]
    conn2.close()

    conn3 = sqlite3.connect(db3)
    traj3 = conn3.execute("SELECT trajectory_id FROM trajectory_meta;").fetchone()[0]
    conn3.close()

    assert traj2 != init_traj_id
    assert traj3 != traj2


def test_pairwise_f8_client_id_guard_and_f7_all_slot_cloud_probing(e2e_env):
    """
    TC-T3-04: F8 (Client ID Guard) x F7 (All-Slot Cloud Probing)
    Verifies that running 'agy-supervisor probe all' against a mixed pool containing
    a corrupted gemini-cli token in Slot 1 and a valid Antigravity token in Slot 2
    accurately isolates Slot 1 as CLIENT_ID_MISMATCH with remediation instructions,
    and reports Slot 2 cleanly without cross-slot credential pollution.
    """
    # Slot 1 infected with foreign gemini-cli client ID
    e2e_env.configure_slot(1, "infected_slot1@example.com", client_id=GEMINI_CLI_CLIENT_ID)
    # Slot 2 configured with authentic Antigravity client ID
    e2e_env.configure_slot(2, "clean_slot2@example.com", client_id=AGY_CLIENT_ID)

    res = e2e_env.run_supervisor(args=["probe", "all"], timeout=10)
    assert res.exit_code == 0

    stdout = res.stdout
    assert "Slot 1 [infected_slot1@example.com]" in stdout
    assert "CLIENT_ID_MISMATCH" in stdout
    assert "gemini-cli" in stdout
    assert "Action Required: Chạy 'agy-supervisor login 1'" in stdout

    assert "Slot 2 [clean_slot2@example.com]" in stdout
    assert "Audience / Client ID: VALID" in stdout or "HEALTHY" in stdout or "Antigravity" in stdout


def test_pairwise_f1_notice_suspension_during_continuous_chat_session_f6(e2e_env):
    """
    TC-T3-05: F1 (Notice Suspension) x F6 (Session Isolation during agy -c)
    Verifies that when an account notice occurs during an active continuous session (-c),
    the supervisor marks the slot NOTICE_BLOCKED and ensures the next account does NOT
    reuse the tainted trajectory ID directly without forking.
    """
    e2e_env.configure_slot(1, "banned_turn@example.com")
    e2e_env.configure_slot(2, "fresh_turn@example.com")

    init_conv = str(uuid.uuid4())
    e2e_env.create_conversation(init_conv, str(uuid.uuid4()), owner_slot=1)

    e2e_env.set_mock_instructions([
        {
            "action": "NOTICE_EMIT",
            "log_line": "Run: attempt 1 failed (PERMISSION_DENIED (code 403): Google Terms of Service violation - Account suspended)",
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

    res = e2e_env.run_supervisor(args=["-c"], timeout=15)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "NOTICE_BLOCKED"
    assert state["current_slot"] == 2

    # Verify session arguments passed to Slot 2
    history = e2e_env.get_call_history()
    assert len(history) == 2
    slot2_args = history[1]["args"]
    assert "--conversation" in slot2_args
    new_conv = slot2_args[slot2_args.index("--conversation") + 1]
    assert new_conv != init_conv


def test_pairwise_f2_401_auth_failure_storm_and_f4_rotational_circuit_breaker(e2e_env):
    """
    TC-T3-06: F2 (401 Auth Failure Storm) x F4 (Rotational / Master Circuit Breaker)
    Verifies that when ALL configured slots suffer from revoked credentials (401 unauthorized_client),
    the supervisor marks each slot as AUTH_FAILED, halts at the Master Circuit Breaker,
    prints actionable guidance, and exits cleanly without entering an infinite thrashing loop.
    """
    e2e_env.configure_slot(1, "revoked1@example.com")
    e2e_env.configure_slot(2, "revoked2@example.com")

    e2e_env.set_mock_instructions([
        {
            "action": "401_FAIL",
            "log_line": "Run: attempt 1 failed (UNAUTHENTICATED (code 401): unauthorized_client: Token has been revoked.)",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "401_FAIL",
            "log_line": "Run: attempt 1 failed (UNAUTHENTICATED (code 401): unauthorized_client: Token has been revoked.)",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    # Circuit breaker triggers graceful termination
    assert res.exit_code == 0

    stdout = res.stdout
    assert "CIRCUIT BREAKER: Toàn bộ 2 tài khoản khả dụng đều không thể sử dụng!" in stdout
    assert "AUTH_FAILED" in stdout
    assert "agy-supervisor login <slot>" in stdout

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "AUTH_FAILED"
    assert state["slots"]["2"]["status"] == "AUTH_FAILED"


def test_pairwise_f8_client_id_mismatch_on_startup_and_f5_auto_failover_jitter(e2e_env):
    """
    TC-T3-07: F8 (Client ID Mismatch on Startup) x F5 (Handover Jitter & Auto-Failover)
    Verifies that when Slot 1 has an invalid client ID at supervisor startup,
    apply_slot rejects Slot 1 with a diagnostic banner, auto-rotates to Slot 2,
    applies handover jitter, and boots successfully.
    """
    # Slot 1 has gemini-cli token, Slot 2 has authentic token
    e2e_env.configure_slot(1, "foreign_start@example.com", client_id=GEMINI_CLI_CLIENT_ID)
    e2e_env.configure_slot(2, "clean_start@example.com", client_id=AGY_CLIENT_ID)

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Model initialized successfully on Slot 2",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=15)
    assert res.exit_code == 0

    stdout = res.stdout
    assert "CREDENTIAL GUARD ERROR: SLOT 1 TOKEN AUDIENCE INVALID" in stdout
    assert "Đang tự động tìm kiếm và kích hoạt tài khoản an toàn tiếp theo" in stdout
    assert "kích hoạt Slot 2" in stdout

    state = e2e_env.get_state()
    assert state["slots"]["1"]["status"] == "CLIENT_ID_MISMATCH"
    assert state["current_slot"] == 2


def test_pairwise_f3_high_density_503_burst_immunity_vs_f4_storm_breaker(e2e_env):
    """
    TC-T3-08: F3 (High-Density 503 Burst) x F4 (4xx Thrashing Storm Breaker Immunity)
    Verifies that high error velocity of HTTP 503 transient overload logs does NOT
    increment consecutive errors or trip the sliding-window circuit breaker.
    """
    e2e_env.configure_slot(1, "burst_user@example.com")

    # Generate 20 consecutive 503 log lines
    lines_503 = "\n".join([
        f"I0915 11:06:{i:02d}.000000 581 run.go:389] Run: attempt {i} failed (UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server), retrying in 1s"
        for i in range(1, 21)
    ])

    e2e_env.set_mock_instructions([
        {
            "action": "503_BURST_THEN_EXIT",
            "log_line": lines_503,
            "delay_before_log": 0.1,
            "sleep_after_log": 0.3,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(timeout=10)
    assert res.exit_code == 0

    state = e2e_env.get_state()
    cb_state = state.get("circuit_breaker", {})
    # Circuit breaker must remain CLOSED with 0 consecutive errors
    assert cb_state.get("state") == "CLOSED"
    assert cb_state.get("consecutive_errors", 0) == 0
    assert cb_state.get("strike_count", 0) == 0


def test_pairwise_f6_session_forking_resilience_on_corrupt_conversation_db(e2e_env):
    """
    TC-T3-09: F6 (Session Forking Fallback) x F8 (Contaminated / Corrupt DB)
    Verifies that if the conversation SQLite DB is corrupted during an account
    boundary crossing, prepare_session handles the failure gracefully and falls
    back to a clean session with --dangerously-skip-permissions without crashing.
    """
    e2e_env.configure_slot(1, "corrupt_user1@example.com")
    e2e_env.configure_slot(2, "corrupt_user2@example.com")

    # Create a corrupt (zero-byte or non-sqlite) DB
    corrupt_conv_id = str(uuid.uuid4())
    corrupt_file = e2e_env.conv_dir / f"{corrupt_conv_id}.db"
    corrupt_file.write_text("CORRUPTED_NON_SQLITE_HEADER")

    sum_db = e2e_env.cli_dir / "conversation_summaries.db"
    conn_sum = sqlite3.connect(sum_db)
    conn_sum.execute("""CREATE TABLE IF NOT EXISTS conversation_summaries (
        conversation_id TEXT PRIMARY KEY, title TEXT, last_modified_time TEXT, parent_conversation_id TEXT, nesting_depth INTEGER
    );""")
    conn_sum.execute("INSERT OR REPLACE INTO conversation_summaries VALUES (?, ?, ?, ?, ?);",
                     (corrupt_conv_id, "Corrupt", "2026-10-31 10:00:00+00:00", "", 0))
    conn_sum.commit()
    conn_sum.close()

    # Ownership assigned to Slot 1
    e2e_env.set_state_slot(1, "ACTIVE")
    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = json.loads(state_file.read_text())
    state.setdefault("conversation_slots", {})[corrupt_conv_id] = 1
    state["last_active_conversation"] = corrupt_conv_id
    state["last_active_slot"] = 1
    state_file.write_text(json.dumps(state))

    # Instruction 1: Slot 1 exhausts quota
    # Instruction 2: Slot 2 starts cleanly
    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST",
            "log_line": "Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1800s.)",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Fresh session on Slot 2",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["-c"], timeout=15)
    assert res.exit_code == 0

    stdout = res.stdout
    # Fallback message must be reported
    assert "Không thể fork phiên cũ an toàn; khởi chạy phiên mới độc lập" in stdout

    # Child agy invocation on Slot 2 must run cleanly with --dangerously-skip-permissions
    history = e2e_env.get_call_history()
    assert len(history) == 2
    assert "--dangerously-skip-permissions" in history[1]["args"]


def test_pairwise_f7_all_slot_probe_with_mixed_token_expirations_and_revocations_f2(e2e_env):
    """
    TC-T3-10: F7 (All-Slot Probing) x F2 (401 Revoked Token & Auto-Refresh Isolation)
    Verifies that running 'agy-supervisor probe all' properly differentiates between
    valid slots, expired auto-renewable slots, and revoked slots without state cross-contamination.
    """
    # Slot 1: Active with valid unexpired token
    e2e_env.configure_slot(1, "valid_probe@example.com")

    # Slot 2: Expired token with revoked status in supervisor state
    e2e_env.configure_slot(2, "revoked_probe@example.com")
    e2e_env.set_state_slot(2, "AUTH_FAILED", exhausted_until=int(time.time()) + 1000000, reason="Revoked token")

    # Slot 3: Notice blocked slot
    e2e_env.configure_slot(3, "notice_probe@example.com")
    e2e_env.set_state_slot(3, "NOTICE_BLOCKED", exhausted_until=int(time.time()) + 100000000, reason="ToS Notice")

    res = e2e_env.run_supervisor(args=["probe", "all"], timeout=10)
    assert res.exit_code == 0

    stdout = res.stdout
    assert "Slot 1 [valid_probe@example.com]" in stdout
    assert "Slot 2 [revoked_probe@example.com]" in stdout
    assert "Slot 3 [notice_probe@example.com]" in stdout

    # State file must remain intact without corrupting slot statuses
    state = e2e_env.get_state()
    assert state["slots"]["2"]["status"] == "AUTH_FAILED"
    assert state["slots"]["3"]["status"] in ("NOTICE_BLOCKED", "AUTH_FAILED")
