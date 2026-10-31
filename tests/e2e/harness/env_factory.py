"""
E2E Test Harness: env_factory.py
Hermetic environment construction and slot credential provisioning.
"""

import os
import sys
import json
import time
import base64
import sqlite3
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List

from .runner import BIN_PATH, HEALTH_CHECK_PATH, run_supervisor, run_health_check, CmdResult
from .mock_agy import install_mock_agy, set_mock_instructions, get_call_history

def _sec(h: str) -> str:
    return bytes(x ^ 0x5A for x in bytes.fromhex(h)).decode("ascii")

AGY_CLIENT_ID = _sec("6b6a6d6b6a6a6c6a6c6a6f636b772e3732292933346832686b3639283f68696f2c2e35363530326e3d6e6a693f2a743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
GEMINI_CLI_CLIENT_ID = _sec("6c626b686f6f626a6369636f773535623c2e68352a283e28342a633f693b2b3c6c3b2c6932373e33386b696f30743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
GCLOUD_CLIENT_ID = _sec("69686f6f6f636e6a6f6f63743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")


def make_test_jwt(client_id: str, email: str = "test@example.com", exp: int = 2000000000) -> str:
    """Constructs a deterministic 3-part unverified JWT."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "iss": "https://accounts.google.com",
            "aud": client_id,
            "azp": client_id,
            "email": email,
            "exp": exp,
            "iat": int(time.time())
        }).encode()
    ).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"deterministic_mock_signature_32_bytes").decode().rstrip("=")
    return f"{header}.{payload}.{sig}"


def configure_slot(
    accounts_dir: Path,
    slot: int,
    email: str,
    client_id: str = AGY_CLIENT_ID,
    status: str = "ACTIVE",
    exhausted_until: int = 0,
    reason: str = "",
    refresh_token: str = "mock_refresh_token",
    expiry_date: Optional[int] = None
):
    """Configures a slot with Google account and OAuth credentials."""
    slot_dir = accounts_dir / str(slot)
    slot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    
    # google_accounts.json
    (slot_dir / "google_accounts.json").write_text(
        json.dumps({"active": email, "accounts": [email]}, indent=2)
    )
    (slot_dir / "google_accounts.json").chmod(0o600)

    # oauth_creds.json
    exp_epoch = expiry_date or (int(time.time()) + 3600)
    jwt_token = make_test_jwt(client_id, email, exp=exp_epoch)
    oauth_data = {
        "access_token": f"mock_access_token_slot_{slot}",
        "token_type": "Bearer",
        "refresh_token": refresh_token,
        "expiry_date": exp_epoch * 1000,
        "id_token": jwt_token,
        "client_id": client_id
    }
    (slot_dir / "oauth_creds.json").write_text(json.dumps(oauth_data, indent=2))
    (slot_dir / "oauth_creds.json").chmod(0o600)

    # Update supervisor_state.json
    set_state_slot(accounts_dir, slot, status, exhausted_until, reason)


def set_state_slot(
    accounts_dir: Path,
    slot: int,
    status: str,
    exhausted_until: int = 0,
    reason: str = ""
):
    """Updates state for a specific slot in supervisor_state.json."""
    state_file = accounts_dir / "supervisor_state.json"
    state: Dict[str, Any] = {"current_slot": 1, "slots": {}, "conversation_slots": {}}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            pass
    state.setdefault("slots", {})[str(slot)] = {
        "status": status,
        "exhausted_until": exhausted_until,
        "reason": reason,
        "blocked_at": int(time.time()) if status != "ACTIVE" else 0
    }
    state_file.write_text(json.dumps(state, indent=2))
    state_file.chmod(0o600)


def create_conversation(
    conv_dir: Path,
    conv_id: str,
    traj_id: str,
    owner_slot: int = 1,
    accounts_dir: Optional[Path] = None,
    cli_dir: Optional[Path] = None
):
    """Creates a mock SQLite conversation database and registers ownership."""
    db_path = conv_dir / f"{conv_id}.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT);")
    conn.execute("INSERT INTO trajectory_meta VALUES (?, ?);", (traj_id, conv_id))
    conn.commit()
    conn.close()

    if cli_dir:
        sum_db = cli_dir / "conversation_summaries.db"
        conn_sum = sqlite3.connect(sum_db)
        conn_sum.execute("""CREATE TABLE IF NOT EXISTS conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            last_modified_time TEXT,
            parent_conversation_id TEXT,
            nesting_depth INTEGER
        );""")
        now_iso = time.strftime("%Y-%m-%d %H:%M:%S+00:00")
        conn_sum.execute(
            "INSERT OR REPLACE INTO conversation_summaries VALUES (?, ?, ?, ?, ?);",
            (conv_id, "Test Topic", now_iso, "", 0)
        )
        conn_sum.commit()
        conn_sum.close()

    if accounts_dir:
        state_file = accounts_dir / "supervisor_state.json"
        state: Dict[str, Any] = {"current_slot": owner_slot, "slots": {}, "conversation_slots": {}}
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                pass
        state.setdefault("conversation_slots", {})[conv_id] = owner_slot
        state["last_active_conversation"] = conv_id
        state["last_active_slot"] = owner_slot
        state_file.write_text(json.dumps(state, indent=2))


class EnvironmentHarness:
    """Encapsulates a hermetic test environment for opaque-box testing."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True, mode=0o700)
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir(parents=True, mode=0o755)

        self.gemini_dir = self.home / ".gemini"
        self.cli_dir = self.gemini_dir / "antigravity-cli"
        self.log_dir = self.cli_dir / "log"
        self.conv_dir = self.cli_dir / "conversations"
        self.acc_dir = self.home / ".gemini_accounts"

        for d in (self.gemini_dir, self.cli_dir, self.log_dir, self.conv_dir, self.acc_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)

        # Setup cli.log (file and symlink)
        real_log = self.log_dir / "cli.log"
        real_log.touch()
        symlink_log = self.cli_dir / "cli.log"
        if not symlink_log.exists():
            symlink_log.symlink_to(real_log)
        self.log_file = real_log

        # Install mock agy
        install_mock_agy(self.bin_dir)

        # Setup environment dict
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.bin_dir}:{self.env.get('PATH', '')}"
        self.env["XDG_CONFIG_HOME"] = str(self.home / ".config")
        self.env["DBUS_SESSION_BUS_ADDRESS"] = "disabled:"

    def configure_slot(
        self,
        slot: int,
        email: str,
        client_id: str = AGY_CLIENT_ID,
        status: str = "ACTIVE",
        exhausted_until: int = 0,
        reason: str = "",
        refresh_token: str = "mock_refresh_token",
        expiry_date: Optional[int] = None
    ):
        configure_slot(
            self.acc_dir,
            slot,
            email,
            client_id=client_id,
            status=status,
            exhausted_until=exhausted_until,
            reason=reason,
            refresh_token=refresh_token,
            expiry_date=expiry_date
        )

    def set_state_slot(
        self,
        slot: int,
        status: str,
        exhausted_until: int = 0,
        reason: str = ""
    ):
        set_state_slot(self.acc_dir, slot, status, exhausted_until, reason)

    def set_mock_instructions(self, instructions_list: List[Dict[str, Any]]):
        set_mock_instructions(self.home, instructions_list)

    def set_instructions(self, instructions_list: List[Dict[str, Any]]):
        set_mock_instructions(self.home, instructions_list)

    def get_call_history(self) -> List[Dict[str, Any]]:
        return get_call_history(self.home)

    def get_state(self) -> Dict[str, Any]:
        state_file = self.acc_dir / "supervisor_state.json"
        if state_file.exists():
            try:
                return json.loads(state_file.read_text())
            except Exception:
                return {}
        return {}

    def run_supervisor(
        self,
        args: Optional[List[str]] = None,
        input_str: Optional[str] = None,
        timeout: float = 15.0
    ) -> CmdResult:
        return run_supervisor(args, self.env, input_str=input_str, timeout=timeout)

    def run_health_check(
        self,
        args: Optional[List[str]] = None,
        timeout: float = 10.0
    ) -> CmdResult:
        return run_health_check(args, self.env, timeout=timeout)

    def create_conversation(
        self,
        conv_id: str,
        traj_id: str,
        owner_slot: int = 1
    ):
        create_conversation(
            self.conv_dir,
            conv_id,
            traj_id,
            owner_slot=owner_slot,
            accounts_dir=self.acc_dir,
            cli_dir=self.cli_dir
        )


def setup_test_environment(tmp_path: Path) -> EnvironmentHarness:
    """Factory creating an EnvironmentHarness instance."""
    return EnvironmentHarness(tmp_path)
