#!/usr/bin/env python3
"""
Tier 1 Feature Coverage: F6 Cross-Account Session Isolation
Repository: agy-supervisor
Directory: tests/e2e/tier1_features/test_f6_session_isolation.py
"""

import sys
import uuid
import sqlite3
import json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_f6_01_cross_account_forks_db(e2e_env):
    """F6-01: Switching accounts under -c forks conversation DB and assigns new trajectory_id."""
    e2e_env.configure_slot(1, "fork1@example.com")
    e2e_env.configure_slot(2, "fork2@example.com")

    init_conv = str(uuid.uuid4())
    init_traj = str(uuid.uuid4())
    e2e_env.create_conversation(init_conv, init_traj, owner_slot=1)

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota exceeded. Resets in 1800s.",
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

    res = e2e_env.run_supervisor(args=["-c"], timeout=15)
    assert res.exit_code == 0

    history = e2e_env.get_call_history()
    assert len(history) == 2
    args2 = history[1]["args"]
    assert "--conversation" in args2
    new_conv = args2[args2.index("--conversation") + 1]
    assert new_conv != init_conv

    # Check that new database file exists and has new trajectory_id
    new_db = e2e_env.conv_dir / f"{new_conv}.db"
    assert new_db.exists()
    conn = sqlite3.connect(new_db)
    new_traj = conn.execute("SELECT trajectory_id FROM trajectory_meta;").fetchone()[0]
    conn.close()
    assert new_traj != init_traj


def test_f6_02_conversation_summaries_updated(e2e_env):
    """F6-02: Forked conversation is registered in conversation_summaries.db with parent_conversation_id."""
    e2e_env.configure_slot(1, "summary1@example.com")
    e2e_env.configure_slot(2, "summary2@example.com")

    init_conv = str(uuid.uuid4())
    init_traj = str(uuid.uuid4())
    e2e_env.create_conversation(init_conv, init_traj, owner_slot=1)

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in 1800s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Active",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["-c"], timeout=15)
    assert res.exit_code == 0

    history = e2e_env.get_call_history()
    new_conv = history[1]["args"][history[1]["args"].index("--conversation") + 1]

    sum_db = e2e_env.cli_dir / "conversation_summaries.db"
    conn = sqlite3.connect(sum_db)
    row = conn.execute("SELECT parent_conversation_id FROM conversation_summaries WHERE conversation_id = ?;", (new_conv,)).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == init_conv


def test_f6_03_child_args_injected_with_uuid(e2e_env):
    """F6-03: Forked session omits -c and injects --conversation <uuid> --dangerously-skip-permissions."""
    e2e_env.configure_slot(1, "args1@example.com")
    e2e_env.configure_slot(2, "args2@example.com")

    init_conv = str(uuid.uuid4())
    e2e_env.create_conversation(init_conv, str(uuid.uuid4()), owner_slot=1)

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in 1800s.",
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

    res = e2e_env.run_supervisor(args=["-c", "--model", "gemini-2.5-pro"], timeout=15)
    assert res.exit_code == 0

    history = e2e_env.get_call_history()
    slot2_args = history[1]["args"]
    assert "-c" not in slot2_args
    assert "--conversation" in slot2_args
    assert "--dangerously-skip-permissions" in slot2_args
    assert "--model" in slot2_args
    assert "gemini-2.5-pro" in slot2_args


def test_f6_04_same_account_preserves_continue(e2e_env):
    """F6-04: Running on the same account preserves -c without executing database fork."""
    e2e_env.configure_slot(1, "same1@example.com")

    init_conv = str(uuid.uuid4())
    e2e_env.create_conversation(init_conv, str(uuid.uuid4()), owner_slot=1)

    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Session online",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["-c"], timeout=10)
    assert res.exit_code == 0

    history = e2e_env.get_call_history()
    assert len(history) == 1
    # Preserved -c
    assert "-c" in history[0]["args"]
    assert "--conversation" not in history[0]["args"]


def test_f6_05_fork_failure_fallback_clean(e2e_env):
    """F6-05: Corrupted source DB during boundary crossing falls back to clean session without crash."""
    e2e_env.configure_slot(1, "bad_db1@example.com")
    e2e_env.configure_slot(2, "bad_db2@example.com")

    # Create invalid non-sqlite file
    bad_conv = str(uuid.uuid4())
    (e2e_env.conv_dir / f"{bad_conv}.db").write_text("NOT_A_VALID_SQLITE_DATABASE")

    sum_db = e2e_env.cli_dir / "conversation_summaries.db"
    conn_sum = sqlite3.connect(sum_db)
    conn_sum.execute("""CREATE TABLE IF NOT EXISTS conversation_summaries (
        conversation_id TEXT PRIMARY KEY, title TEXT, last_modified_time TEXT, parent_conversation_id TEXT, nesting_depth INTEGER
    );""")
    conn_sum.execute("INSERT OR REPLACE INTO conversation_summaries VALUES (?, ?, ?, ?, ?);",
                     (bad_conv, "Corrupt", "2026-10-31 10:00:00+00:00", "", 0))
    conn_sum.commit()
    conn_sum.close()

    # Mark as owned by Slot 1
    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = json.loads(state_file.read_text())
    state.setdefault("conversation_slots", {})[bad_conv] = 1
    state["last_active_conversation"] = bad_conv
    state["last_active_slot"] = 1
    state_file.write_text(json.dumps(state))

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in 1800s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Clean fresh start",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["-c"], timeout=15)
    assert res.exit_code == 0
    assert "Không thể fork phiên cũ an toàn; khởi chạy phiên mới độc lập" in res.stdout
    history = e2e_env.get_call_history()
    assert len(history) == 2
    assert "--dangerously-skip-permissions" in history[1]["args"]
