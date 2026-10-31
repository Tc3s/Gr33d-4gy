"""
E2E Test Harness: mock_agy.py
Controllable child process simulator for Antigravity CLI ('agy').
Injectable into PATH without altering production code.
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

MOCK_AGY_SCRIPT = """#!/usr/bin/env python3
import os
import sys
import time
import json
import signal
from pathlib import Path

home = Path(os.environ["HOME"])
log_file = home / ".gemini" / "antigravity-cli" / "cli.log"
inst_file = home / "mock_agy_instructions.json"
hist_file = home / "mock_agy_history.json"

# Record invocation history atomically
history = []
if hist_file.exists():
    try:
        history = json.loads(hist_file.read_text())
    except Exception:
        pass
call_index = len(history)
history.append({"args": sys.argv[1:], "time": time.time(), "pid": os.getpid()})
try:
    hist_file.write_text(json.dumps(history))
except Exception:
    pass

# Load instructions
instructions = []
if inst_file.exists():
    try:
        instructions = json.loads(inst_file.read_text())
    except Exception:
        pass

inst = instructions[call_index] if call_index < len(instructions) else (instructions[-1] if instructions else {})
action = inst.get("action", "NORMAL")

if action == "IGNORE_SIGTERM":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

events = inst.get("events", [])
exit_code = inst.get("exit_code", 0)

if events:
    for ev in events:
        d = ev.get("delay", 0.05)
        line = ev.get("log", "")
        time.sleep(d)
        if line and log_file.exists():
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\\n")
                f.flush()
    rem = inst.get("sleep_after", 1.0)
    if rem > 0:
        for _ in range(int(rem * 20)):
            time.sleep(0.05)
else:
    log_line = inst.get("log_line", "")
    log_lines = inst.get("log_lines", [])
    if log_line and not log_lines:
        log_lines = [log_line]

    delay_before_log = inst.get("delay_before_log", 0.05)
    sleep_after_log = inst.get("sleep_after_log", 1.0)

    if log_lines:
        time.sleep(delay_before_log)
        if log_file.exists():
            with open(log_file, "a", encoding="utf-8") as f:
                for line in log_lines:
                    f.write(line + "\\n")
                    f.flush()

    if sleep_after_log > 0:
        for _ in range(int(sleep_after_log * 20)):
            time.sleep(0.05)

sys.exit(exit_code)
"""


def install_mock_agy(bin_dir: Path):
    """Installs the mock agy binary into the provided directory."""
    agy_path = bin_dir / "agy"
    agy_path.write_text(MOCK_AGY_SCRIPT)
    agy_path.chmod(0o755)


def set_mock_instructions(home: Path, instructions_list: List[Dict[str, Any]]):
    """Sets the instruction list for upcoming mock agy invocations."""
    inst_file = home / "mock_agy_instructions.json"
    inst_file.write_text(json.dumps(instructions_list, indent=2))


def get_call_history(home: Path) -> List[Dict[str, Any]]:
    """Retrieves the history of all mock agy invocations."""
    hist_file = home / "mock_agy_history.json"
    if hist_file.exists():
        try:
            return json.loads(hist_file.read_text())
        except Exception:
            return []
    return []
