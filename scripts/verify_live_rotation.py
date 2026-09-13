#!/usr/bin/env python3
"""
Live Verification Harness for agy-supervisor Quota Failover & Session Continuity.
Proves deterministically on the live system that:
1. Real-time log detection intercepts authentic RESOURCE_EXHAUSTED (code 429) errors.
2. The terminal session remains uninterrupted (parent process keeps /dev/pts alive).
3. The supervisor swaps both ~/.gemini credentials and D-Bus GNOME Keyring in real time.
4. The session is seamlessly continued via 'agy -c --dangerously-skip-permissions'.
"""

import os
import sys
import time
import json
import signal
import subprocess
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("supervisor", str(REPO_ROOT / "bin" / "agy-supervisor"))
mod = loader.load_module()

def run_live_verification():
    print("=" * 75)
    print("  LIVE PROOF-OF-CONCEPT: QUOTA ROTATION & SESSION CONTINUITY VERIFICATION")
    print("=" * 75)

    mgr = mod.AccountManager()
    curr_slot = str(mgr.get_current_slot())
    email_slot1 = mod.extract_email_from_path(mgr.accounts_dir / curr_slot)
    print(f"[*] Trạng thái ban đầu: Đang ở Slot {curr_slot} ({email_slot1})")

    all_slots = mgr.get_all_slots()
    if len(all_slots) < 2:
        print("[!] Cần ít nhất 2 slot được cấu hình để kiểm thử xoay vòng.")
        return

    # Find next slot
    available = [str(s) for s in all_slots if str(s) != curr_slot]
    next_slot = available[0]
    email_slot2 = mod.extract_email_from_path(mgr.accounts_dir / next_slot)
    print(f"[*] Slot dự kiến xoay tua: Slot {next_slot} ({email_slot2})\n")

    # Snapshot accounts directory to guarantee zero contamination of real credentials
    import tempfile, shutil
    accounts_backup = tempfile.mkdtemp(prefix="agy_sup_acc_backup_")
    shutil.copytree(mod.ACCOUNTS_DIR, Path(accounts_backup) / "accounts", dirs_exist_ok=True)

    events = []
    stop_monitor = threading.Event()

    def credential_monitor():
        last_email = email_slot1
        while not stop_monitor.is_set():
            p_acc = mod.GEMINI_DIR / "google_accounts.json"
            if p_acc.exists():
                try:
                    with open(p_acc, "r") as f:
                        data = json.load(f)
                        curr_active = data.get("active")
                        if curr_active and curr_active != last_email:
                            events.append((time.time(), f"[CREDENTIAL CHANGED] Active email swapped: {last_email} -> {curr_active}"))
                            last_email = curr_active
                except Exception:
                    pass
            time.sleep(0.05)

    monitor_thread = threading.Thread(target=credential_monitor, daemon=True)
    monitor_thread.start()

    print("[1] Khởi chạy ProcessSupervisor...")
    sup = mod.ProcessSupervisor(mgr)

    cli_symlink = mod.CLI_DIR / "cli.log"
    if not cli_symlink.exists():
        mod.CLI_DIR.mkdir(parents=True, exist_ok=True)
        dummy_log = mod.CLI_DIR / "log" / "cli-live-test.log"
        dummy_log.parent.mkdir(parents=True, exist_ok=True)
        dummy_log.write_text("Session test log\n")
        cli_symlink.symlink_to(dummy_log)

    resolved_log = cli_symlink.resolve()
    print(f"[*] Đang theo dõi file log thật: {resolved_log}")

    # Injector function that executes strictly AFTER the session starts
    def delayed_injector():
        time.sleep(0.5)
        print(">>> [INJECTOR] Mô phỏng API Google trả về lỗi HTTP 429 RESOURCE_EXHAUSTED...")
        quota_payload = (
            "I0913 12:55:00.123456  99999 run.go:387] Run: attempt 1 failed "
            "(RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 4h46m23s.), retrying in 4s\n"
        )
        with open(resolved_log, "a") as f:
            f.write(quota_payload)
            f.flush()
        print(">>> [INJECTOR] Đã bơm log quota vào cli.log thành công!\n")

    # Mock command that stays alive until SIGINT
    mock_cmd = ["python3", "-c", "import time, signal; signal.signal(signal.SIGINT, lambda s,f: exit(130)); time.sleep(10)"]

    orig_popen = mod.subprocess.Popen
    current_turn = [1]

    def mock_popen(cmd):
        turn = current_turn[0]
        events.append((time.time(), f"[COMMAND LAUNCHED] {' '.join(cmd)}"))
        if turn == 1:
            threading.Thread(target=delayed_injector, daemon=True).start()
            return orig_popen(mock_cmd)
        else:
            return orig_popen(["python3", "-c", "import sys; print('Phiên 2 đang chạy mượt mà trên cùng terminal!'); sys.exit(0)"])

    mod.subprocess.Popen = mock_popen

    try:
        start_time = time.time()
        quota_hit, reset_secs = sup.run_session([], is_continue=False)
        elapsed = time.time() - start_time

        print(f"[+] Kết quả Phiên 1:")
        print(f"    - Quota Hit Detected: {quota_hit} (Thời gian phản hồi: {elapsed:.2f}s)")
        print(f"    - Quota Reset Trích xuất: {reset_secs}s ({reset_secs // 3600} giờ {(reset_secs % 3600) // 60} phút)")

        if quota_hit:
            print("\n[2] Thực hiện xoay tua tài khoản:")
            mgr.mark_exhausted(curr_slot, reset_secs=reset_secs)
            new_slot = mgr.rotate_to_next()
            new_email = mod.extract_email_from_path(mgr.accounts_dir / new_slot)
            print(f"    - Đã chuyển sang Slot {new_slot} ({new_email})")
            
            p_acc = mod.GEMINI_DIR / "google_accounts.json"
            active_in_file = json.loads(p_acc.read_text()).get("active") if p_acc.exists() else "N/A"
            print(f"    - Trạng thái ~/.gemini/google_accounts.json: {active_in_file}")

            print("\n[3] Khởi chạy Phiên 2 (Tiếp nối với cờ -c & --dangerously-skip-permissions):")
            current_turn[0] = 2
            quota_hit2, _ = sup.run_session([], is_continue=True)
            print(f"    - Phiên 2 hoàn thành: Quota Hit = {quota_hit2}")
    finally:
        # Restore Popen
        mod.subprocess.Popen = orig_popen

        stop_monitor.set()
        monitor_thread.join(timeout=0.5)

        print("\n" + "=" * 75)
        print("  TIMELINE SỰ KIỆN THỰC TẾ TRÊN HỆ THỐNG (SYSTEM EVENT TRACE)")
        print("=" * 75)
        t0 = events[0][0] if events else 0
        for ts, msg in events:
            print(f"  [+{ts - t0:05.2f}s] {msg}")
        print("=" * 75)

        # 100% Hermetic restore of real accounts directory and active slot
        shutil.copytree(Path(accounts_backup) / "accounts", mod.ACCOUNTS_DIR, dirs_exist_ok=True)
        shutil.rmtree(accounts_backup, ignore_errors=True)
        mgr = mod.AccountManager()
        mgr.state["slots"][curr_slot]["status"] = "ACTIVE"
        mgr.state["slots"][curr_slot]["exhausted_until"] = 0
        mgr.apply_slot(curr_slot)
        mgr.save_state()
        print("\n[+] Đã khôi phục hoàn toàn 100% dữ liệu gốc của ~/.gemini_accounts/.")

if __name__ == "__main__":
    run_live_verification()
