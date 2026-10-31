#!/usr/bin/env python3
"""
ADVERSARIAL FUZZER & EMPIRICAL VERIFICATION HARNESS (Milestone 2 Iteration 3)
Agent: m2_i3_challenger_1

Empirical Tests:
1. Handover Jitter Statistics & Distribution (5,000 samples, bounds [2.5, 5.0]s, variance, Chi2, backoff)
2. 503 Transient Overload & Benign Log Immunity (Fuzz 10,000 events, 0 strikes)
3. Circuit Breaker Persistence Across Multiple run_session Invocations and AccountManager Reloads
4. Rotational Storm Breaker Window & Slot Scaling Stress Test
5. ThrashingCircuitBreaker.load_dict Mutated Input Fuzzing (1,000 mutated inputs, deadlock & exception safety)
"""

import sys
import os
import time
import math
import random
import statistics
import threading
import tempfile
import json
import shutil
import subprocess
from pathlib import Path
from importlib.machinery import SourceFileLoader

REPO_ROOT = Path(__file__).resolve().parent.parent
LOADER = SourceFileLoader("supervisor", str(REPO_ROOT / "bin" / "agy-supervisor"))
mod = LOADER.load_module()

LogEventType = mod.LogEventType
LogEventClassification = mod.LogEventClassification
ThrashingCircuitBreaker = mod.ThrashingCircuitBreaker
CircuitBreakerConfig = mod.CircuitBreakerConfig
CircuitState = mod.CircuitState
calculate_handover_jitter = mod.calculate_handover_jitter
AccountManager = mod.AccountManager
ProcessSupervisor = mod.ProcessSupervisor
classify_log_event = mod.classify_log_event
resolve_chunk_event = mod.resolve_chunk_event
kill_process_tree = mod.kill_process_tree


class TestResultLedger:
    def __init__(self):
        self.passes = 0
        self.failures = 0
        self.records = []

    def record_pass(self, name: str, evidence: str):
        self.passes += 1
        self.records.append({"status": "PASS", "test": name, "evidence": evidence})
        print(f"  [PASS] {name} | Evidence: {evidence}")

    def record_fail(self, name: str, evidence: str):
        self.failures += 1
        self.records.append({"status": "FAIL", "test": name, "evidence": evidence})
        print(f"  [FAIL] {name} | Evidence: {evidence}")


ledger = TestResultLedger()


def test_suite_jitter_bounds_and_statistics():
    print("\n" + "=" * 80)
    print("SUITE 1: JITTER DISTRIBUTION & BOUNDS (5,000 SAMPLES & STATISTICAL FIT)")
    print("=" * 80)

    N_5K = 5000
    samples_5k = [calculate_handover_jitter(0) for _ in range(N_5K)]
    min_5k = min(samples_5k)
    max_5k = max(samples_5k)
    mean_5k = statistics.mean(samples_5k)
    var_5k = statistics.variance(samples_5k)

    # 1. Bounds check [2.5, 5.0]
    if min_5k >= 2.5000 and max_5k <= 5.0000:
        ledger.record_pass(
            "jitter_5k_strict_bounds",
            f"N=5000, min={min_5k:.6f}s (>=2.5s), max={max_5k:.6f}s (<=5.0s)",
        )
    else:
        ledger.record_fail(
            "jitter_5k_strict_bounds",
            f"Bounds violated! min={min_5k}, max={max_5k}",
        )

    # 2. Variance check (> 0.40, theoretical ~0.5208)
    if var_5k > 0.40:
        ledger.record_pass(
            "jitter_5k_variance_check",
            f"N=5000, variance={var_5k:.6f} (> 0.40, theoretical ~0.5208)",
        )
    else:
        ledger.record_fail(
            "jitter_5k_variance_check",
            f"Variance too low! var={var_5k:.6f} <= 0.40",
        )

    # 3. Mean check (theoretical 3.75)
    if abs(mean_5k - 3.75) < 0.05:
        ledger.record_pass(
            "jitter_5k_mean_uniformity",
            f"N=5000, mean={mean_5k:.6f} (within 0.05 of 3.75)",
        )
    else:
        ledger.record_fail(
            "jitter_5k_mean_uniformity",
            f"Mean skewed! mean={mean_5k:.6f}",
        )

    # 4. Legacy 2.0s eradication check
    legacy_count = sum(1 for s in samples_5k if abs(s - 2.0) < 0.45)
    if legacy_count == 0:
        ledger.record_pass(
            "jitter_5k_legacy_eradication",
            f"0 occurrences of static 2.0s or [1.55, 2.45]s in {N_5K} samples",
        )
    else:
        ledger.record_fail(
            "jitter_5k_legacy_eradication",
            f"Found {legacy_count} legacy samples near 2.0s!",
        )

    # 5. Chi-Square Uniformity Test across 10 equal bins
    bins = [0] * 10
    for s in samples_5k:
        idx = min(9, int((s - 2.5) / 0.25))
        bins[idx] += 1
    expected_bin_count = N_5K / 10.0  # 500
    chi2 = sum(((b - expected_bin_count) ** 2) / expected_bin_count for b in bins)
    # Degrees of freedom = 9. Critical value at alpha=0.01 is 21.67
    if chi2 < 25.0:
        ledger.record_pass(
            "jitter_5k_chi2_uniformity",
            f"Chi2={chi2:.2f} (bins: {bins}), passes uniform distribution goodness-of-fit",
        )
    else:
        ledger.record_fail(
            "jitter_5k_chi2_uniformity",
            f"Chi2={chi2:.2f} too high, bins: {bins}",
        )

    # 6. Progressive backoff verification
    j_0 = [calculate_handover_jitter(0) for _ in range(500)]
    j_1 = [calculate_handover_jitter(1) for _ in range(500)]
    j_2 = [calculate_handover_jitter(2) for _ in range(500)]
    j_10 = [calculate_handover_jitter(10) for _ in range(500)]

    if min(j_1) >= 3.75 and max(j_1) <= 7.5:
        ledger.record_pass("progressive_backoff_k1", f"k=1 range: [{min(j_1):.3f}, {max(j_1):.3f}] within [3.75, 7.5]")
    else:
        ledger.record_fail("progressive_backoff_k1", f"k=1 violated: [{min(j_1)}, {max(j_1)}]")

    if min(j_2) >= 5.625 and max(j_2) <= 11.25:
        ledger.record_pass("progressive_backoff_k2", f"k=2 range: [{min(j_2):.3f}, {max(j_2):.3f}] within [5.625, 11.25]")
    else:
        ledger.record_fail("progressive_backoff_k2", f"k=2 violated: [{min(j_2)}, {max(j_2)}]")

    if all(s == 30.0 for s in j_10):
        ledger.record_pass("progressive_backoff_cap", "k=10 strictly capped at 30.0s max jitter")
    else:
        ledger.record_fail("progressive_backoff_cap", f"k=10 uncapped values found: {set(j_10)}")


def test_suite_503_and_benign_logs_immunity():
    print("\n" + "=" * 80)
    print("SUITE 2: TRANSIENT 503 & BENIGN LOG IMMUNITY FUZZING (10,000 EVENTS)")
    print("=" * 80)

    cb = ThrashingCircuitBreaker(CircuitBreakerConfig(error_threshold=3, window_seconds=60.0))

    benign_templates = [
        b"2026-10-31T12:00:00Z [INFO] Loading tokenizer weights from cache",
        b"[chat] User message: How do I configure Nginx for HTTP/2?",
        b"{\"level\": \"info\", \"msg\": \"Tokens streamed: 450\", \"latency_ms\": 120}",
        b"data: {\"response\": \"Sure, here is the configuration file...\"}",
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"ready\"}",
        b"DEBUG: stream chunk parsed successfully: 1024 bytes",
        b"2026-10-31T12:00:01Z [Jetski] Sending heartbeat to daily-cloudcode-pa.googleapis.com",
        b"{\"candidates\": [{\"content\": {\"parts\": [{\"text\": \"Done!\"}]}}]}",
    ]

    transient_503_templates = [
        b"No capacity available for model",
        b"503 Service Unavailable: The model is currently overloaded. Please try again shortly.",
        b"HTTP/2 503\r\ncontent-type: application/grpc\r\ngrpc-status: 14\r\ngrpc-message: No capacity available for model gemini-pro",
        b"{\"error\": {\"code\": 503, \"message\": \"Resource temporarily exhausted. No capacity available for model\"}}",
        b"2026-10-31T12:00:02Z [ERROR] RPC failed: 14 UNAVAILABLE: No capacity available for model",
        b"Retrying request after 14 UNAVAILABLE backoff...",
        b"status: 503, reason: No capacity available for model, will retry in 1.5s",
    ]

    total_fuzzed = 10000
    tripped_count = 0
    now = 1000.0

    for i in range(total_fuzzed):
        if random.random() < 0.5:
            raw = random.choice(benign_templates)
        else:
            raw = random.choice(transient_503_templates)

        ev = classify_log_event(raw)
        tripped = cb.record_event(ev, timestamp=now + (i * 0.001))
        if tripped:
            tripped_count += 1

    if tripped_count == 0 and cb.strike_count == 0 and cb.state == CircuitState.CLOSED and not cb.is_open(now + 20):
        ledger.record_pass(
            "503_and_benign_immunity_fuzzing",
            f"Fuzzed {total_fuzzed:,} events. Tripped: {tripped_count}, Strikes: {cb.strike_count}, State: {cb.state.value}",
        )
    else:
        ledger.record_fail(
            "503_and_benign_immunity_fuzzing",
            f"Fuzz failure! Tripped={tripped_count}, Strikes={cb.strike_count}, State={cb.state.value}",
        )


def test_suite_circuit_breaker_persistence_and_multi_session():
    print("\n" + "=" * 80)
    print("SUITE 3: MULTI-SESSION STRIKE ACCUMULATION & PERSISTENCE ACROSS RELOADS")
    print("=" * 80)

    temp_dir = tempfile.TemporaryDirectory()
    try:
        tpath = Path(temp_dir.name)
        state_file = tpath / "supervisor_state.json"
        accounts_dir = tpath / "accounts"
        accounts_dir.mkdir(parents=True, exist_ok=True)
        slot1_dir = accounts_dir / "1"
        slot1_dir.mkdir(parents=True, exist_ok=True)
        (slot1_dir / "oauth_creds.json").write_text(json.dumps({"token": "fake_token_1", "email": "slot1@test.com"}))

        slot2_dir = accounts_dir / "2"
        slot2_dir.mkdir(parents=True, exist_ok=True)
        (slot2_dir / "oauth_creds.json").write_text(json.dumps({"token": "fake_token_2", "email": "slot2@test.com"}))

        orig_state_file = mod.STATE_FILE
        orig_accounts_dir = mod.ACCOUNTS_DIR
        mod.STATE_FILE = state_file
        mod.ACCOUNTS_DIR = accounts_dir

        # Initialize AccountManager 1
        mgr1 = AccountManager()
        if mgr1.circuit_breaker.strike_count == 0 and mgr1.circuit_breaker.state == CircuitState.CLOSED:
            ledger.record_pass("cb_initial_state", "mgr1 initialized with strike_count=0, state=CLOSED")
        else:
            ledger.record_fail("cb_initial_state", f"Invalid init state: {mgr1.circuit_breaker.to_dict()}")

        # Session 1: Simulate 3 429 errors within sliding window triggering Trip 1 (Strike 1)
        now_s1 = 1000.0
        ev_429 = LogEventClassification(
            event_type=LogEventType.QUOTA_EXHAUSTED,
            status_code=429,
            reason="Rate limit exceeded",
        )
        for t in [now_s1, now_s1 + 5.0, now_s1 + 10.0]:
            mgr1.circuit_breaker.record_event(ev_429, timestamp=t)

        if mgr1.circuit_breaker.is_open(now=now_s1 + 10.0) and mgr1.circuit_breaker.strike_count == 1:
            ledger.record_pass("session_1_trip", "Session 1 triggered Strike 1 (state=OPEN, strike_count=1)")
        else:
            ledger.record_fail("session_1_trip", f"Trip 1 failed: {mgr1.circuit_breaker.to_dict()}")

        mgr1.save_state()

        with open(state_file, "r") as f:
            disk_data = json.load(f)
        if "circuit_breaker" in disk_data and disk_data["circuit_breaker"]["strike_count"] == 1:
            ledger.record_pass(
                "persistence_disk_serialization",
                f"Disk state verified: strike_count={disk_data['circuit_breaker']['strike_count']}, state={disk_data['circuit_breaker']['state']}",
            )
        else:
            ledger.record_fail("persistence_disk_serialization", f"Missing or corrupted disk data: {disk_data}")

        # Simulate process reload: instantiate fresh AccountManager 2
        mgr2 = AccountManager()
        if mgr2.circuit_breaker.strike_count == 1 and mgr2.circuit_breaker.state == CircuitState.OPEN:
            ledger.record_pass(
                "reload_survival_mgr2",
                "mgr2 successfully restored strike_count=1 and state=OPEN from disk",
            )
        else:
            ledger.record_fail("reload_survival_mgr2", f"mgr2 failed to restore: {mgr2.circuit_breaker.to_dict()}")

        raw_state = mgr2.load_state()
        if raw_state.get("circuit_breaker", {}).get("strike_count") == 1:
            ledger.record_pass("direct_load_state_verification", "mgr2.load_state() preserves circuit_breaker dictionary")
        else:
            ledger.record_fail("direct_load_state_verification", f"load_state() lost circuit_breaker: {raw_state}")

        # Advance time past cooldown of Strike 1
        now_s2 = mgr2.circuit_breaker.cooldown_until + 1.0
        if not mgr2.circuit_breaker.is_open(now=now_s2):
            ledger.record_pass("cooling_expiration_half_open", f"At t={now_s2:.1f}, circuit breaker transitioned from OPEN")
        else:
            ledger.record_fail("cooling_expiration_half_open", f"Still OPEN at t={now_s2:.1f}")

        # Session 2: Trigger another 3 errors -> Strike 2 and double base cooling
        for t in [now_s2, now_s2 + 2.0, now_s2 + 4.0]:
            mgr2.circuit_breaker.record_event(ev_429, timestamp=t)

        if mgr2.circuit_breaker.strike_count == 2 and mgr2.circuit_breaker.is_open(now=now_s2 + 4.0):
            expected_min_delay = 120.0 + 2.5
            actual_delay = mgr2.circuit_breaker.cooldown_until - (now_s2 + 4.0)
            if actual_delay >= expected_min_delay:
                ledger.record_pass(
                    "session_2_exponential_backoff",
                    f"Session 2 reached Strike 2! Cooldown delay={actual_delay:.1f}s (>= expected 122.5s)",
                )
            else:
                ledger.record_fail(
                    "session_2_exponential_backoff",
                    f"Delay too short for Strike 2: {actual_delay:.1f}s",
                )
        else:
            ledger.record_fail("session_2_exponential_backoff", f"Strike 2 failed: {mgr2.circuit_breaker.to_dict()}")

        mgr2.save_state()

        # Simulate second reload: instantiate fresh AccountManager 3
        mgr3 = AccountManager()
        if mgr3.circuit_breaker.strike_count == 2 and mgr3.circuit_breaker.state == CircuitState.OPEN:
            ledger.record_pass(
                "reload_survival_mgr3",
                "mgr3 successfully restored strike_count=2 and state=OPEN from disk",
            )
        else:
            ledger.record_fail("reload_survival_mgr3", f"mgr3 failed to restore: {mgr3.circuit_breaker.to_dict()}")

        # Clean Session: main() reset contract
        mgr3.circuit_breaker.reset()
        mgr3.save_state()

        if mgr3.circuit_breaker.strike_count == 0 and mgr3.circuit_breaker.state == CircuitState.CLOSED:
            ledger.record_pass("clean_session_reset_in_memory", "mgr3.circuit_breaker.reset() restored CLOSED with 0 strikes")
        else:
            ledger.record_fail("clean_session_reset_in_memory", f"Reset failed: {mgr3.circuit_breaker.to_dict()}")

        mgr4 = AccountManager()
        if mgr4.circuit_breaker.strike_count == 0 and mgr4.circuit_breaker.state == CircuitState.CLOSED:
            ledger.record_pass("clean_session_reset_persisted", "mgr4 restored clean 0 strikes after disk reload")
        else:
            ledger.record_fail("clean_session_reset_persisted", f"mgr4 failed reset check: {mgr4.circuit_breaker.to_dict()}")

    finally:
        mod.STATE_FILE = orig_state_file
        mod.ACCOUNTS_DIR = orig_accounts_dir
        temp_dir.cleanup()


def test_suite_rotational_storm_breaker():
    print("\n" + "=" * 80)
    print("SUITE 4: ROTATIONAL STORM BREAKER ADVERSARIAL STRESS TEST")
    print("=" * 80)

    temp_dir = tempfile.TemporaryDirectory()
    try:
        tpath = Path(temp_dir.name)
        state_file = tpath / "supervisor_state.json"
        accounts_dir = tpath / "accounts"
        accounts_dir.mkdir(parents=True, exist_ok=True)

        orig_state_file = mod.STATE_FILE
        orig_accounts_dir = mod.ACCOUNTS_DIR
        mod.STATE_FILE = state_file
        mod.ACCOUNTS_DIR = accounts_dir

        # Case A: 3 slots configured -> storm_limit = max(4, 3) = 4
        for s in [1, 2, 3]:
            s_dir = accounts_dir / str(s)
            s_dir.mkdir(parents=True, exist_ok=True)
            (s_dir / "oauth_creds.json").write_text(json.dumps({"token": f"tok_{s}", "email": f"s{s}@test.com"}))

        mgr = AccountManager()
        rotations = []
        for i in range(3):
            next_s = mgr.rotate_to_next()
            rotations.append(next_s)

        if all(r != "" for r in rotations) and len(mgr.rotation_timestamps) == 3:
            ledger.record_pass("storm_breaker_under_threshold", f"3 rotations allowed (slots: {rotations})")
        else:
            ledger.record_fail("storm_breaker_under_threshold", f"Unexpected block at < 4 rotations: {rotations}")

        storm_slot = mgr.rotate_to_next()
        if storm_slot == "":
            ledger.record_pass(
                "storm_breaker_trip_at_threshold_3slots",
                f"4th rapid rotation halted with empty string '' (rotations in window: {len(mgr.rotation_timestamps)})",
            )
        else:
            ledger.record_fail(
                "storm_breaker_trip_at_threshold_3slots",
                f"Expected empty string '', got '{storm_slot}'!",
            )

        now = time.time()
        mgr.rotation_timestamps.clear()
        mgr.rotation_timestamps.extend([now - 75.0, now - 70.0, now - 65.0])
        resumed_slot = mgr.rotate_to_next()
        if resumed_slot != "":
            ledger.record_pass(
                "storm_breaker_recovery_after_60s_window",
                f"Aged timestamps pruned, rotation resumed to slot {resumed_slot} (timestamps len: {len(mgr.rotation_timestamps)})",
            )
        else:
            ledger.record_fail("storm_breaker_recovery_after_60s_window", "Rotation failed to resume after 60s window!")

        # Case B: 6 slots configured -> storm_limit = max(4, 6 + 1) = 7
        for s in [4, 5, 6]:
            s_dir = accounts_dir / str(s)
            s_dir.mkdir(parents=True, exist_ok=True)
            (s_dir / "oauth_creds.json").write_text(json.dumps({"token": f"tok_{s}", "email": f"s{s}@test.com"}))

        import unittest.mock as mock
        mgr_6slots = AccountManager()
        mgr_6slots.rotation_timestamps.clear()

        with mock.patch("time.sleep", return_value=None):
            six_rotations = []
            for i in range(6):
                nxt = mgr_6slots.rotate_to_next()
                six_rotations.append(nxt)

            if all(r != "" for r in six_rotations) and len(mgr_6slots.rotation_timestamps) == 6:
                ledger.record_pass(
                    "storm_breaker_6slots_scaling_allowed",
                    f"6 slots allowed 6 rapid rotations: {six_rotations} (timestamps len: {len(mgr_6slots.rotation_timestamps)})",
                )
            else:
                ledger.record_fail("storm_breaker_6slots_scaling_allowed", f"Premature block on 6 slots: {six_rotations}")

            seventh = mgr_6slots.rotate_to_next()
            if seventh == "" and len(mgr_6slots.rotation_timestamps) == 7:
                ledger.record_pass(
                    "storm_breaker_6slots_trip_at_7",
                    f"7th rotation halted with empty string '' on 6-slot system (timestamps len: {len(mgr_6slots.rotation_timestamps)})",
                )
            else:
                ledger.record_fail("storm_breaker_6slots_trip_at_7", f"Expected '', got '{seventh}'")

    finally:
        mod.STATE_FILE = orig_state_file
        mod.ACCOUNTS_DIR = orig_accounts_dir
        temp_dir.cleanup()


def test_suite_fuzz_load_dict_mutated_inputs():
    print("\n" + "=" * 80)
    print("SUITE 5: FUZZ ThrashingCircuitBreaker.load_dict (1,000 MUTATED INPUTS & CONCURRENCY)")
    print("=" * 80)

    cb = ThrashingCircuitBreaker()
    uncaught_exceptions = []
    deadlock_detected = False

    # Generator for adversarial and mutated dictionary payloads
    extreme_values = [
        None,
        True,
        False,
        0,
        -1,
        -999999,
        1,
        2,
        100,
        10**9,
        10**30,
        10**100,
        -10**50,
        float("nan"),
        float("inf"),
        float("-inf"),
        1e-300,
        1e300,
        3.1415926535,
        "",
        "0",
        "-1",
        "100",
        "nan",
        "inf",
        "-inf",
        "corrupt_string",
        "CLOSED",
        "OPEN",
        "HALF_OPEN",
        "closed",
        "open",
        "half_open",
        "  OPEN  ",
        "UNKNOWN_STATE",
        [],
        [1, 2, 3],
        {},
        {"nested": "dict"},
        object(),
        b"\x00\xff\xfe",
    ]

    keys = ["strike_count", "consecutive_errors", "state", "cooldown_until", "last_tripped_at", "trip_reason"]

    # Test non-dict inputs first
    non_dict_inputs = [
        None,
        "string_input",
        12345,
        3.14,
        True,
        False,
        [1, 2, 3],
        ("tuple", 1),
        set([1, 2]),
        object(),
        b"raw_bytes",
    ]

    for item in non_dict_inputs:
        try:
            cb.load_dict(item)
            assert cb.strike_count == 0
            assert cb.state == CircuitState.CLOSED
        except Exception as e:
            uncaught_exceptions.append((f"non_dict_{type(item).__name__}", e))

    # Fuzz 1,000 mutated inputs
    N_FUZZ = 1000
    for idx in range(N_FUZZ):
        # Generate varied mutated inputs
        fuzz_mode = idx % 5
        if fuzz_mode == 0:
            # Completely random keys and random extreme values
            payload = {
                random.choice(keys): random.choice(extreme_values)
                for _ in range(random.randint(1, len(keys)))
            }
        elif fuzz_mode == 1:
            # Full key set with randomized extreme values
            payload = {k: random.choice(extreme_values) for k in keys}
        elif fuzz_mode == 2:
            # All None values
            payload = {k: None for k in keys}
        elif fuzz_mode == 3:
            # Type mismatches and arbitrary injected keys
            payload = {
                k: random.choice(extreme_values) for k in keys
            }
            payload["__injected_evil_key__"] = "malicious_payload"
            payload["sub_obj"] = {"deep": None}
        else:
            # Boundary values
            payload = {
                "strike_count": random.choice([-100, 0, 1, 999999999999999999999, "not_an_int"]),
                "consecutive_errors": random.choice([-50, 0, 5, "NaN", float("inf")]),
                "state": random.choice(["OPEN", "CLOSED", "HALF_OPEN", "BOGUS", None, 999]),
                "cooldown_until": random.choice([-10.0, 0.0, 1000000.0, float("nan"), float("inf"), "-inf"]),
                "last_tripped_at": random.choice([None, float("nan"), 123456.78]),
                "trip_reason": random.choice([None, "", "a" * 5000, 12345, ["list"]]),
            }

        try:
            cb.load_dict(payload)
            # Verify invariants: strike_count >= 0, consecutive_errors >= 0, state is CircuitState enum, cooldown_until >= 0
            assert isinstance(cb.strike_count, int) and cb.strike_count >= 0
            assert isinstance(cb.consecutive_errors, int) and cb.consecutive_errors >= 0
            assert isinstance(cb.state, CircuitState)
            assert isinstance(cb.cooldown_until, (int, float)) and math.isfinite(cb.cooldown_until) and cb.cooldown_until >= 0.0
            assert isinstance(cb.last_tripped_at, (int, float)) and math.isfinite(cb.last_tripped_at) and cb.last_tripped_at >= 0.0
            assert isinstance(cb.trip_reason, str)

            # Also verify to_dict serialization always succeeds after load_dict
            serialized = cb.to_dict()
            assert isinstance(serialized, dict)
            json.dumps(serialized)  # Must be strictly JSON-serializable
        except Exception as e:
            uncaught_exceptions.append((f"fuzz_round_{idx}", e))

    if len(uncaught_exceptions) == 0:
        ledger.record_pass(
            "load_dict_1000_mutated_fuzzing",
            f"Fuzzed {N_FUZZ} mutated/adversarial dictionaries + non-dict inputs: 0 uncaught exceptions, state invariants preserved",
        )
    else:
        ledger.record_fail(
            "load_dict_1000_mutated_fuzzing",
            f"{len(uncaught_exceptions)} exceptions caught! Sample: {uncaught_exceptions[:3]}",
        )

    # Concurrency and Deadlock Stress Test
    # Run 4 threads concurrently calling load_dict, to_dict, record_event, reset, and is_open
    thread_exceptions = []
    stop_event = threading.Event()

    def worker_load_dict(tid):
        try:
            for _ in range(500):
                if stop_event.is_set():
                    break
                p = {k: random.choice(extreme_values) for k in keys}
                cb.load_dict(p)
                _ = cb.to_dict()
        except Exception as ex:
            thread_exceptions.append((f"worker_{tid}", ex))

    def worker_record_event(tid):
        try:
            ev = LogEventClassification(event_type=LogEventType.QUOTA_EXHAUSTED, status_code=429)
            for i in range(500):
                if stop_event.is_set():
                    break
                cb.record_event(ev, timestamp=1000.0 + i)
                _ = cb.is_open(1000.0 + i)
        except Exception as ex:
            thread_exceptions.append((f"worker_{tid}", ex))

    def worker_reset(tid):
        try:
            for _ in range(250):
                if stop_event.is_set():
                    break
                cb.reset()
                time.sleep(0.0001)
        except Exception as ex:
            thread_exceptions.append((f"worker_{tid}", ex))

    threads = [
        threading.Thread(target=worker_load_dict, args=(1,)),
        threading.Thread(target=worker_load_dict, args=(2,)),
        threading.Thread(target=worker_record_event, args=(3,)),
        threading.Thread(target=worker_reset, args=(4,)),
    ]

    for th in threads:
        th.start()

    # Join with strict timeout to detect deadlocks
    start_join = time.time()
    for th in threads:
        th.join(timeout=5.0)
        if th.is_alive():
            deadlock_detected = True
            stop_event.set()

    join_elapsed = time.time() - start_join

    if not deadlock_detected and len(thread_exceptions) == 0:
        ledger.record_pass(
            "load_dict_concurrency_deadlock_free",
            f"4 concurrent threads executed 1,750 operations in {join_elapsed:.3f}s: 0 deadlocks, 0 race exceptions",
        )
    else:
        ledger.record_fail(
            "load_dict_concurrency_deadlock_free",
            f"Deadlock={deadlock_detected}, Exceptions={thread_exceptions}",
        )


def run_all_suites():
    print("\n" + "#" * 80)
    print("STARTING EMPIRICAL ADVERSARIAL FUZZER HARNESS (M2 I3 CHALLENGER 1)")
    print("#" * 80)

    test_suite_jitter_bounds_and_statistics()
    test_suite_503_and_benign_logs_immunity()
    test_suite_circuit_breaker_persistence_and_multi_session()
    test_suite_rotational_storm_breaker()
    test_suite_fuzz_load_dict_mutated_inputs()

    print("\n" + "=" * 80)
    print("FUZZER SUMMARY:")
    print(f"  TOTAL PASSED: {ledger.passes}")
    print(f"  TOTAL FAILED: {ledger.failures}")
    print("=" * 80)

    if ledger.failures > 0:
        print("\nFAILED TESTS DETAILS:")
        for r in ledger.records:
            if r["status"] == "FAIL":
                print(f"  - {r['test']}: {r['evidence']}")
        sys.exit(1)
    else:
        print("\n>>> ALL ADVERSARIAL FUZZING TESTS PASSED EMPIRICALLY! <<<")
        sys.exit(0)


if __name__ == "__main__":
    run_all_suites()
