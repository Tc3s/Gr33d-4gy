#!/usr/bin/env python3
"""
Adversarial Verification & Stress Testing Harness for Milestone 3 Iteration 2
Challenger: m3_i2_chal_1 (Critic / Specialist)
Target: bin/agy-supervisor, scripts/health_check.py

Empirical verification of 4 fixed vulnerabilities:
1. Spoofed client_id with foreign id_token strictly rejected as FOREIGN_MISMATCH.
2. Multi-audience arrays with foreign tokens at any index rejected.
3. Architectural consistency between supervisor and health_check.py.
4. Type safety on bizarre types and sync_back_current quarantined slot protection.
"""

import os
import sys
import json
import time
import base64
import tempfile
import unittest
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_BIN = REPO_ROOT / "bin" / "agy-supervisor"
HEALTH_CHECK_SCRIPT = REPO_ROOT / "scripts" / "health_check.py"

import importlib.util
from importlib.machinery import SourceFileLoader

def _load_module_from_file(module_name: str, file_path: Path):
    if not file_path.suffix:
        loader = SourceFileLoader(module_name, str(file_path))
        spec = importlib.util.spec_from_file_location(module_name, str(file_path), loader=loader)
    else:
        spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

sup = _load_module_from_file("agy_supervisor", SUPERVISOR_BIN)
hc = _load_module_from_file("health_check", HEALTH_CHECK_SCRIPT)

def _sec(h: str) -> str:
    return bytes(x ^ 0x5A for x in bytes.fromhex(h)).decode("ascii")

# Known Client IDs
PRIMARY_AGY_CLIENT_ID = _sec("6b6a6d6b6a6a6c6a6c6a6f636b772e3732292933346832686b3639283f68696f2c2e35363530326e3d6e6a693f2a743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
SECONDARY_AGY_CLIENT_ID = _sec("62626e696f6e636b636a6f6877696c2e28396b303038692e3d2f333b396968352c6c39353e686c62396f383632743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
TELEMETRY_AGY_CLIENT_ID = _sec("6b6a636263636b6b6d6f6d686f77373e2f6b353f6f3938383f6b39313d3d293f38353832636e2e35333b2a6c6f6d743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")

GEMINI_CLI_CLIENT_ID = _sec("6c626b686f6f626a6369636f773535623c2e68352a283e28342a633f693b2b3c6c3b2c6932373e33386b696f30743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
GCLOUD_CLI_CLIENT_ID = _sec("69686f6f6f636e6a6f6f63743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
FIREBASE_CLI_CLIENT_ID = _sec("6f6c696f626e69696f626c63773c3d28323d373e6e6d382b343f3133306f3362386f2a286a693235626f633f6b743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")
UNKNOWN_CLIENT_ID = f"999999999999-unknown.{_sec('3b2a2a29743d35353d363f2f293f283935342e3f342e74393537')}"


def make_jwt(claims: dict, header: dict = None) -> str:
    """Helper to generate an unverified JWT with arbitrary claims."""
    if header is None:
        header = {"alg": "RS256", "typ": "JWT"}
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(b"fakesig").decode().rstrip("=")
    return f"{h_b64}.{p_b64}.{sig_b64}"


class TestSpoofedClientIdPrecedenceAttacks(unittest.TestCase):
    """
    Empirical tests verifying that an unauthenticated plaintext client_id
    NEVER overrides or bypasses foreign JWT id_token claims.
    """

    def test_spoof_primary_agy_with_gcloud_jwt(self):
        jwt_str = make_jwt({"aud": GCLOUD_CLI_CLIENT_ID, "azp": GCLOUD_CLI_CLIENT_ID})
        creds = {
            "client_id": PRIMARY_AGY_CLIENT_ID,
            "id_token": jwt_str,
            "refresh_token": "1//dummy_gcloud"
        }
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)
        self.assertIn("Google Cloud SDK (gcloud CLI)", rep.app_name)

    def test_spoof_secondary_agy_with_gemini_cli_jwt(self):
        jwt_str = make_jwt({"aud": GEMINI_CLI_CLIENT_ID, "azp": GEMINI_CLI_CLIENT_ID})
        creds = {
            "client_id": SECONDARY_AGY_CLIENT_ID,
            "id_token": jwt_str,
            "refresh_token": "1//dummy_gemini"
        }
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)
        self.assertIn("gemini-cli", rep.app_name)

    def test_spoof_telemetry_agy_with_firebase_jwt(self):
        jwt_str = make_jwt({"aud": FIREBASE_CLI_CLIENT_ID, "azp": FIREBASE_CLI_CLIENT_ID})
        creds = {
            "client_id": TELEMETRY_AGY_CLIENT_ID,
            "id_token": jwt_str,
            "refresh_token": "1//dummy_firebase"
        }
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)
        self.assertIn("Firebase CLI", rep.app_name)

    def test_nested_token_spoofed_client_id(self):
        jwt_str = make_jwt({"aud": GEMINI_CLI_CLIENT_ID})
        creds = {
            "token": {
                "client_id": PRIMARY_AGY_CLIENT_ID,
                "id_token": jwt_str
            }
        }
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_spoofed_client_id_foreign_azp_no_aud(self):
        jwt_str = make_jwt({"azp": GCLOUD_CLI_CLIENT_ID})
        creds = {
            "client_id": PRIMARY_AGY_CLIENT_ID,
            "id_token": jwt_str
        }
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_spoofed_client_id_valid_aud_foreign_azp(self):
        # aud is AGY, but azp is foreign
        jwt_str = make_jwt({"aud": PRIMARY_AGY_CLIENT_ID, "azp": GEMINI_CLI_CLIENT_ID})
        creds = {
            "client_id": PRIMARY_AGY_CLIENT_ID,
            "id_token": jwt_str
        }
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_spoofed_client_id_foreign_aud_valid_azp(self):
        # aud is foreign, azp is AGY
        jwt_str = make_jwt({"aud": GEMINI_CLI_CLIENT_ID, "azp": PRIMARY_AGY_CLIENT_ID})
        creds = {
            "client_id": PRIMARY_AGY_CLIENT_ID,
            "id_token": jwt_str
        }
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_foreign_explicit_client_id_no_token(self):
        creds = {"client_id": GCLOUD_CLI_CLIENT_ID}
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_conflicting_explicit_client_id_untrusted_project(self):
        jwt_str = make_jwt({"aud": PRIMARY_AGY_CLIENT_ID})
        creds = {
            "client_id": UNKNOWN_CLIENT_ID,
            "id_token": jwt_str
        }
        rep = sup.AudienceValidator.validate_credentials_dict(creds)
        self.assertEqual(rep.status, sup.ClientIDStatus.UNKNOWN_CLIENT)
        self.assertFalse(rep.is_safe_to_activate)
        self.assertIn("CONFLICTING CLIENT ID", rep.diagnostic_message)


class TestMultiAudienceArrayAttacks(unittest.TestCase):
    """
    Empirical tests verifying multi-audience arrays with foreign tokens
    at ANY index are strictly detected and rejected.
    """

    def test_multi_audience_foreign_at_index_1(self):
        jwt_str = make_jwt({"aud": [PRIMARY_AGY_CLIENT_ID, GEMINI_CLI_CLIENT_ID]})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_multi_audience_foreign_at_index_0(self):
        jwt_str = make_jwt({"aud": [GEMINI_CLI_CLIENT_ID, PRIMARY_AGY_CLIENT_ID]})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_multi_audience_foreign_at_index_2(self):
        jwt_str = make_jwt({"aud": [PRIMARY_AGY_CLIENT_ID, SECONDARY_AGY_CLIENT_ID, GCLOUD_CLI_CLIENT_ID]})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_multi_audience_foreign_at_index_50(self):
        arr = [PRIMARY_AGY_CLIENT_ID] * 50 + [FIREBASE_CLI_CLIENT_ID]
        jwt_str = make_jwt({"aud": arr})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_multi_audience_all_valid(self):
        jwt_str = make_jwt({"aud": [PRIMARY_AGY_CLIENT_ID, SECONDARY_AGY_CLIENT_ID]})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.VALID)
        self.assertTrue(rep.is_safe_to_activate)

    def test_multi_audience_bare_project_numbers(self):
        jwt_str = make_jwt({"aud": ["32555940559", "1071006060591"]})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_multi_audience_integer_project_numbers(self):
        jwt_str = make_jwt({"aud": [1071006060591, 681255809395]})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)

    def test_multi_audience_empty_list(self):
        jwt_str = make_jwt({"aud": []})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.UNKNOWN_CLIENT)
        self.assertFalse(rep.is_safe_to_activate)

    def test_multi_audience_mixed_non_string_types(self):
        jwt_str = make_jwt({"aud": [None, {}, [], 12345, GEMINI_CLI_CLIENT_ID]})
        rep = sup.AudienceValidator.validate_credentials_dict({"id_token": jwt_str})
        self.assertEqual(rep.status, sup.ClientIDStatus.FOREIGN_MISMATCH)
        self.assertFalse(rep.is_safe_to_activate)


class TestSupervisorVsHealthCheckConsistency(unittest.TestCase):
    """
    Matrix testing: verify bin/agy-supervisor and scripts/health_check.py
    agree 100% on safety determination across diverse claim structures.
    """

    def _assert_aligned(self, claims: dict, creds: dict = None):
        jwt_str = make_jwt(claims)
        c_dict = creds.copy() if creds else {}
        c_dict["id_token"] = jwt_str

        sup_rep = sup.AudienceValidator.validate_credentials_dict(c_dict)
        hc_rep = hc.validate_token_audience(claims, creds)

        # 1. Primary safety verdict must match
        self.assertEqual(
            sup_rep.is_safe_to_activate,
            hc_rep["is_valid"],
            f"Safety divergence for claims={claims}, creds={creds}: sup.safe={sup_rep.is_safe_to_activate} vs hc.valid={hc_rep['is_valid']}"
        )

        # 2. Status categorization must align
        status_map = {
            sup.ClientIDStatus.VALID: ["VALID"],
            sup.ClientIDStatus.FOREIGN_MISMATCH: ["FOREIGN_MISMATCH"],
            sup.ClientIDStatus.UNKNOWN_CLIENT: ["UNKNOWN", "MISSING"],
            sup.ClientIDStatus.MISSING_TOKEN: ["MISSING"],
        }
        allowed_hc_statuses = status_map.get(sup_rep.status, [])
        self.assertIn(
            hc_rep["status"],
            allowed_hc_statuses,
            f"Status mismatch for claims={claims}: sup={sup_rep.status.value} vs hc={hc_rep['status']}"
        )

    def test_matrix_all_cases(self):
        cases = [
            # 1. Primary valid
            ({"aud": PRIMARY_AGY_CLIENT_ID, "azp": PRIMARY_AGY_CLIENT_ID}, None),
            # 2. Secondary valid
            ({"aud": SECONDARY_AGY_CLIENT_ID, "azp": SECONDARY_AGY_CLIENT_ID}, None),
            # 3. Telemetry valid
            ({"aud": TELEMETRY_AGY_CLIENT_ID}, None),
            # 4. Gemini-cli foreign
            ({"aud": GEMINI_CLI_CLIENT_ID, "azp": GEMINI_CLI_CLIENT_ID}, None),
            # 5. Gcloud foreign
            ({"aud": GCLOUD_CLI_CLIENT_ID, "azp": GCLOUD_CLI_CLIENT_ID}, None),
            # 6. Firebase foreign
            ({"aud": FIREBASE_CLI_CLIENT_ID, "azp": FIREBASE_CLI_CLIENT_ID}, None),
            # 7. Conflicting aud=gemini-cli, azp=primary_agy
            ({"aud": GEMINI_CLI_CLIENT_ID, "azp": PRIMARY_AGY_CLIENT_ID}, None),
            # 8. Conflicting aud=primary_agy, azp=gemini-cli
            ({"aud": PRIMARY_AGY_CLIENT_ID, "azp": GEMINI_CLI_CLIENT_ID}, None),
            # 9. Array [valid, gemini-cli]
            ({"aud": [PRIMARY_AGY_CLIENT_ID, GEMINI_CLI_CLIENT_ID]}, None),
            # 10. Array [gemini-cli, valid]
            ({"aud": [GEMINI_CLI_CLIENT_ID, PRIMARY_AGY_CLIENT_ID]}, None),
            # 11. Array [gcloud, firebase]
            ({"aud": [GCLOUD_CLI_CLIENT_ID, FIREBASE_CLI_CLIENT_ID]}, None),
            # 12. Array [primary_agy, secondary_agy]
            ({"aud": [PRIMARY_AGY_CLIENT_ID, SECONDARY_AGY_CLIENT_ID]}, None),
            # 13. Unknown client ID
            ({"aud": UNKNOWN_CLIENT_ID, "azp": UNKNOWN_CLIENT_ID}, None),
            # 14. Bare foreign project number
            ({"aud": "32555940559"}, None),
            # 15. Bare valid project number
            ({"aud": "1071006060591"}, None),
            # 16. Empty array
            ({"aud": []}, None),
        ]
        for claims, creds in cases:
            with self.subTest(claims=claims, creds=creds):
                self._assert_aligned(claims, creds)


class TestTypeSafetyAndWeirdInputs(unittest.TestCase):
    """
    Verify immunity to unhashable types, type juggling, and malformed structures.
    """

    def test_client_id_as_list_does_not_crash(self):
        rep = sup.AudienceValidator.validate_credentials_dict({"client_id": ["list", "of", "items"]})
        self.assertEqual(rep.status, sup.ClientIDStatus.MALFORMED_JWT)
        self.assertFalse(rep.is_safe_to_activate)
        self.assertIn("expected str, got list", rep.diagnostic_message)

    def test_client_id_as_dict_does_not_crash(self):
        rep = sup.AudienceValidator.validate_credentials_dict({"client_id": {"nested": "dict"}})
        self.assertEqual(rep.status, sup.ClientIDStatus.MALFORMED_JWT)
        self.assertFalse(rep.is_safe_to_activate)
        self.assertIn("expected str, got dict", rep.diagnostic_message)

    def test_client_id_as_int_does_not_crash(self):
        rep = sup.AudienceValidator.validate_credentials_dict({"client_id": 1071006060591})
        self.assertEqual(rep.status, sup.ClientIDStatus.MALFORMED_JWT)
        self.assertFalse(rep.is_safe_to_activate)

    def test_client_id_as_bool_does_not_crash(self):
        rep = sup.AudienceValidator.validate_credentials_dict({"client_id": True})
        self.assertEqual(rep.status, sup.ClientIDStatus.MALFORMED_JWT)
        self.assertFalse(rep.is_safe_to_activate)

    def test_id_token_non_string_handled_safely(self):
        for val in [123, True, ["list"], {"dict": 1}]:
            # In strict mode (production/health_check), missing/non-string token must not be safe
            rep_strict = sup.AudienceValidator.validate_credentials_dict({"id_token": val}, strict=True)
            self.assertFalse(rep_strict.is_safe_to_activate)
            self.assertEqual(rep_strict.status, sup.ClientIDStatus.MISSING_TOKEN)

            # In lenient mode, returns MISSING_TOKEN without unhandled crash
            rep_lenient = sup.AudienceValidator.validate_credentials_dict({"id_token": val}, strict=False)
            self.assertEqual(rep_lenient.status, sup.ClientIDStatus.MISSING_TOKEN)


class TestSyncBackQuarantinedSlotProtection(unittest.TestCase):
    """
    Empirical tests verifying AccountManager.sync_back_current NEVER
    overwrites or contaminates a quarantined or misconfigured slot.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.td.name)
        self.accounts_dir = self.base_dir / "accounts"
        self.gemini_dir = self.base_dir / "gemini"
        self.state_file = self.base_dir / "supervisor_state.json"
        self.cli_dir = self.base_dir / "cli"

        self.accounts_dir.mkdir(parents=True)
        self.gemini_dir.mkdir(parents=True)
        self.cli_dir.mkdir(parents=True)

        self.orig_accounts = sup.ACCOUNTS_DIR
        self.orig_gemini = sup.GEMINI_DIR
        self.orig_state = sup.STATE_FILE
        self.orig_cli = sup.CLI_DIR

        sup.ACCOUNTS_DIR = self.accounts_dir
        sup.GEMINI_DIR = self.gemini_dir
        sup.STATE_FILE = self.state_file
        sup.CLI_DIR = self.cli_dir

        self.mgr = sup.AccountManager()

    def tearDown(self):
        sup.ACCOUNTS_DIR = self.orig_accounts
        sup.GEMINI_DIR = self.orig_gemini
        sup.STATE_FILE = self.orig_state
        sup.CLI_DIR = self.orig_cli
        self.td.cleanup()

    def _setup_slot(self, slot_id: int, client_id: str, email: str, token_tag: str):
        s_dir = self.accounts_dir / str(slot_id)
        s_dir.mkdir(parents=True, exist_ok=True)
        jwt_str = make_jwt({"aud": client_id, "azp": client_id, "email": email})
        oauth_data = {
            "id_token": jwt_str,
            "access_token": f"ya29.{token_tag}",
            "refresh_token": f"1//refresh_{token_tag}"
        }
        (s_dir / "oauth_creds.json").write_text(json.dumps(oauth_data, indent=2))
        (s_dir / "google_accounts.json").write_text(json.dumps({"active": email}, indent=2))
        return s_dir

    def _setup_gemini_active(self, email: str, token_tag: str):
        jwt_str = make_jwt({"aud": PRIMARY_AGY_CLIENT_ID, "azp": PRIMARY_AGY_CLIENT_ID, "email": email})
        oauth_data = {
            "id_token": jwt_str,
            "access_token": f"ya29.{token_tag}",
            "refresh_token": f"1//refresh_{token_tag}"
        }
        (self.gemini_dir / "oauth_creds.json").write_text(json.dumps(oauth_data, indent=2))
        (self.gemini_dir / "google_accounts.json").write_text(json.dumps({"active": email}, indent=2))

    def test_sync_back_aborts_on_client_id_mismatch_status(self):
        s1 = self._setup_slot(1, GEMINI_CLI_CLIENT_ID, "s1@example.com", "original_s1_token")
        self._setup_gemini_active("s1@example.com", "gemini_new_token")

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "CLIENT_ID_MISMATCH", "exhausted_until": int(time.time()) + 99999}
        self.mgr.active_slot_applied = "1"

        self.mgr.sync_back_current()

        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds["access_token"], "ya29.original_s1_token", "Quarantined slot must NOT be overwritten!")

    def test_sync_back_aborts_on_notice_blocked_status(self):
        s1 = self._setup_slot(1, PRIMARY_AGY_CLIENT_ID, "s1@example.com", "original_s1_token")
        self._setup_gemini_active("s1@example.com", "gemini_new_token")

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "NOTICE_BLOCKED", "exhausted_until": int(time.time()) + 99999}
        self.mgr.active_slot_applied = "1"

        self.mgr.sync_back_current()

        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds["access_token"], "ya29.original_s1_token", "Notice-blocked slot must NOT be overwritten!")

    def test_sync_back_aborts_on_auth_failed_status(self):
        s1 = self._setup_slot(1, PRIMARY_AGY_CLIENT_ID, "s1@example.com", "original_s1_token")
        self._setup_gemini_active("s1@example.com", "gemini_new_token")

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "AUTH_FAILED", "exhausted_until": int(time.time()) + 99999}
        self.mgr.active_slot_applied = "1"

        self.mgr.sync_back_current()

        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds["access_token"], "ya29.original_s1_token", "Auth-failed slot must NOT be overwritten!")

    def test_sync_back_aborts_on_slot_dir_containing_foreign_token_even_if_state_active(self):
        # Slot state claims ACTIVE, but files on disk are foreign gemini-cli
        s1 = self._setup_slot(1, GEMINI_CLI_CLIENT_ID, "s1@example.com", "original_s1_token")
        self._setup_gemini_active("s1@example.com", "gemini_new_token")

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "ACTIVE", "exhausted_until": 0}
        self.mgr.active_slot_applied = "1"

        self.mgr.sync_back_current()

        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds["access_token"], "ya29.original_s1_token", "Slot with foreign token must NOT be overwritten!")

    def test_sync_back_aborts_on_cross_account_email_mismatch(self):
        s1 = self._setup_slot(1, PRIMARY_AGY_CLIENT_ID, "alice@example.com", "alice_token")
        self._setup_gemini_active("bob@example.com", "bob_token")

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "ACTIVE", "exhausted_until": 0}
        self.mgr.active_slot_applied = "1"

        self.mgr.sync_back_current()

        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds["access_token"], "ya29.alice_token", "Email mismatch must abort sync_back!")

    def test_sync_back_aborts_on_unapplied_slot(self):
        s1 = self._setup_slot(1, PRIMARY_AGY_CLIENT_ID, "alice@example.com", "alice_token")
        self._setup_gemini_active("alice@example.com", "new_token")

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "ACTIVE", "exhausted_until": 0}
        self.mgr.active_slot_applied = "2"  # Mismatch: slot 2 was applied, not slot 1

        self.mgr.sync_back_current()

        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds["access_token"], "ya29.alice_token", "Unapplied slot must NOT be overwritten!")

    def test_sync_back_aborts_when_gemini_creds_empty_or_corrupt(self):
        s1 = self._setup_slot(1, PRIMARY_AGY_CLIENT_ID, "alice@example.com", "alice_token")

        # Corrupt JSON in gemini dir
        (self.gemini_dir / "oauth_creds.json").write_text("{broken json...")
        (self.gemini_dir / "google_accounts.json").write_text(json.dumps({"active": "alice@example.com"}))

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "ACTIVE", "exhausted_until": 0}
        self.mgr.active_slot_applied = "1"

        self.mgr.sync_back_current()

        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds["access_token"], "ya29.alice_token", "Corrupt gemini creds must NOT overwrite slot!")

    def test_sync_back_succeeds_on_healthy_slot_matching_email(self):
        s1 = self._setup_slot(1, PRIMARY_AGY_CLIENT_ID, "alice@example.com", "alice_old_token")
        self._setup_gemini_active("alice@example.com", "alice_refreshed_token")

        self.mgr.state["current_slot"] = 1
        self.mgr.state["slots"]["1"] = {"status": "ACTIVE", "exhausted_until": 0}
        self.mgr.active_slot_applied = "1"

        self.mgr.sync_back_current()

        s1_creds = json.loads((s1 / "oauth_creds.json").read_text())
        self.assertEqual(s1_creds["access_token"], "ya29.alice_refreshed_token", "Legitimate refreshed token must be saved!")


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestSpoofedClientIdPrecedenceAttacks))
    suite.addTests(loader.loadTestsFromTestCase(TestMultiAudienceArrayAttacks))
    suite.addTests(loader.loadTestsFromTestCase(TestSupervisorVsHealthCheckConsistency))
    suite.addTests(loader.loadTestsFromTestCase(TestTypeSafetyAndWeirdInputs))
    suite.addTests(loader.loadTestsFromTestCase(TestSyncBackQuarantinedSlotProtection))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
