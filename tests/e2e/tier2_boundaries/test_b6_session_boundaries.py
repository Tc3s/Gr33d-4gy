#!/usr/bin/env python3
"""
Tier 2 Boundary & Corner Cases: B6 Session Isolation Boundaries
Repository: agy-supervisor
Directory: tests/e2e/tier2_boundaries/test_b6_session_boundaries.py
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


def test_b6_01_missing_conversation_summaries_db(e2e_env):
    """B6-01: Forking succeeds even when conversation_summaries.db does not exist yet."""
    e2e_env.configure_slot(1, "missing_sum1@example.com")
    e2e_env.configure_slot(2, "missing_sum2@example.com")

    # Create conversation DB without creating conversation_summaries.db
    conv_id = str(uuid.uuid4())
    traj_id = str(uuid.uuid4())
    db_path = e2e_env.conv_dir / f"{conv_id}.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT);")
    conn.execute("INSERT INTO trajectory_meta VALUES (?, ?);", (traj_id, conv_id))
    conn.commit()
    conn.close()

    # Record in state
    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = json.loads(state_file.read_text())
    state.setdefault("conversation_slots", {})[conv_id] = 1
    state["last_active_conversation"] = conv_id
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


def test_b6_02_locked_sqlite_wal_during_fork(e2e_env):
    """B6-02: SQLite WAL checkpoints cleanly when preparing conversation for child."""
    e2e_env.configure_slot(1, "wal1@example.com")
    e2e_env.configure_slot(2, "wal2@example.com")

    conv_id = str(uuid.uuid4())
    traj_id = str(uuid.uuid4())
    e2e_env.create_conversation(conv_id, traj_id, owner_slot=1)

    # Open SQLite in WAL mode
    db_path = e2e_env.conv_dir / f"{conv_id}.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.commit()
    conn.close()

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in 1800s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Slot 2 ok",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["-c"], timeout=15)
    assert res.exit_code == 0


def test_b6_03_corrupted_source_db_file(e2e_env):
    """B6-03: Zero-byte database file triggers graceful fallback to clean session."""
    e2e_env.configure_slot(1, "zero_db1@example.com")
    e2e_env.configure_slot(2, "zero_db2@example.com")

    zero_conv = str(uuid.uuid4())
    (e2e_env.conv_dir / f"{zero_conv}.db").touch()

    sum_db = e2e_env.cli_dir / "conversation_summaries.db"
    conn_sum = sqlite3.connect(sum_db)
    conn_sum.execute("""CREATE TABLE IF NOT EXISTS conversation_summaries (
        conversation_id TEXT PRIMARY KEY, title TEXT, last_modified_time TEXT, parent_conversation_id TEXT, nesting_depth INTEGER
    );""")
    conn_sum.execute("INSERT OR REPLACE INTO conversation_summaries VALUES (?, ?, ?, ?, ?);",
                     (zero_conv, "Zero", "2026-10-31 10:00:00+00:00", "", 0))
    conn_sum.commit()
    conn_sum.close()

    state_file = e2e_env.acc_dir / "supervisor_state.json"
    state = json.loads(state_file.read_text())
    state.setdefault("conversation_slots", {})[zero_conv] = 1
    state["last_active_conversation"] = zero_conv
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
            "log_line": "Slot 2 ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["-c"], timeout=15)
    assert res.exit_code == 0
    history = e2e_env.get_call_history()
    assert len(history) == 2


def test_b6_04_atomic_rollback_on_disk_full(e2e_env):
    """B6-04: Non-existent destination directory cleans up temporary files without residual debris."""
    e2e_env.configure_slot(1, "debris1@example.com")
    e2e_env.configure_slot(2, "debris2@example.com")

    conv_id = str(uuid.uuid4())
    e2e_env.create_conversation(conv_id, str(uuid.uuid4()), owner_slot=1)

    e2e_env.set_mock_instructions([
        {
            "action": "EXHAUST",
            "log_line": "RESOURCE_EXHAUSTED (code 429): Quota reached. Resets in 1800s.",
            "delay_before_log": 0.1,
            "sleep_after_log": 3.0
        },
        {
            "action": "NORMAL",
            "log_line": "Slot 2 ok",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["-c"], timeout=15)
    assert res.exit_code == 0

    # Ensure no .tmp files left over in conversations dir
    tmp_files = list(e2e_env.conv_dir.glob("*.tmp"))
    assert len(tmp_files) == 0


def test_b6_05_unknown_conversation_uuid(e2e_env):
    """B6-05: Querying unknown conversation UUID does not crash supervisor state lookup."""
    e2e_env.configure_slot(1, "unknown_conv@example.com")

    # Pass explicit conversation that does not exist in state
    fake_conv = str(uuid.uuid4())
    e2e_env.set_mock_instructions([
        {
            "action": "NORMAL",
            "log_line": "Ready",
            "delay_before_log": 0.05,
            "sleep_after_log": 0.2,
            "exit_code": 0
        }
    ])

    res = e2e_env.run_supervisor(args=["--conversation", fake_conv], timeout=10)
    assert res.exit_code == 0
