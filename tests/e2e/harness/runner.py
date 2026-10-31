"""
E2E Test Harness: runner.py
Provides opaque-box subprocess execution of bin/agy-supervisor and scripts/health_check.py.
"""

import os
import sys
import subprocess
import time
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any


def strip_ansi(text: str) -> str:
    """Strips terminal ANSI color escape sequences for robust assertion matching."""
    return re.sub(r'\x1b\[[0-9;]*[mGKH]', '', text)


def find_repo_root() -> Path:
    """Discovers repository root directory containing bin/agy-supervisor."""
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "bin" / "agy-supervisor").exists():
            return cur
        cur = cur.parent
    return Path(__file__).resolve().parents[3]


REPO_ROOT = find_repo_root()
BIN_PATH = REPO_ROOT / "bin" / "agy-supervisor"
HEALTH_CHECK_PATH = REPO_ROOT / "scripts" / "health_check.py"


@dataclass
class CmdResult:
    exit_code: int
    stdout: str
    stderr: str
    duration: float


def run_supervisor(
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    input_str: Optional[str] = None,
    timeout: float = 15.0
) -> CmdResult:
    """
    Executes bin/agy-supervisor via subprocess.run with isolated environment.
    Opaque-box: only relies on CLI parameters, environment, and standard streams.
    """
    cmd = [sys.executable, str(BIN_PATH)] + (args or [])
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            env=env or os.environ.copy(),
            input=input_str.encode("utf-8") if input_str else None,
            capture_output=True,
            timeout=timeout
        )
        return CmdResult(
            exit_code=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
            duration=time.time() - start
        )
    except subprocess.TimeoutExpired as te:
        return CmdResult(
            exit_code=-999,
            stdout=(te.stdout or b"").decode("utf-8", errors="replace"),
            stderr=(te.stderr or b"").decode("utf-8", errors="replace"),
            duration=time.time() - start
        )


def run_health_check(
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 10.0
) -> CmdResult:
    """
    Executes scripts/health_check.py via subprocess.run with isolated environment.
    """
    cmd = [sys.executable, str(HEALTH_CHECK_PATH)] + (args or [])
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            env=env or os.environ.copy(),
            capture_output=True,
            timeout=timeout
        )
        return CmdResult(
            exit_code=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
            duration=time.time() - start
        )
    except subprocess.TimeoutExpired as te:
        return CmdResult(
            exit_code=-999,
            stdout=(te.stdout or b"").decode("utf-8", errors="replace"),
            stderr=(te.stderr or b"").decode("utf-8", errors="replace"),
            duration=time.time() - start
        )
