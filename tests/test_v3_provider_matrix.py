"""Wave3: control-plane + adapter-matrix regressions (no paid Provider calls)."""

from __future__ import annotations

import unittest

from plowwhip.decision_policy import build_decision_options
from plowwhip.provider import PROVIDER_CAPABILITIES, PROVIDERS
from plowwhip.provider_policy import MODEL_PROVIDER_KEYS
from plowwhip.recovery_policy import failure_signature
from plowwhip.store import DEFAULT_SETTINGS
from plowwhip.timeout_policy import (
    soft_extension_requires_increment,
)
from plowwhip.verification import _merge_contract_and_semantic_verdict


class ControlPlaneSharedContractTest(unittest.TestCase):
    def test_a16a_semantic_reject_not_overridden(self):
        semantic = {
            "valid": True,
            "passed": False,
            "verdict": "CHANGES_REQUIRED",
            "decision_reason": "scope defect",
            "acceptances": [],
            "repair_package": [],
        }
        contract = {
            "valid": True,
            "passed": True,
            "verdict": "PASS",
            "decision_reason": "chapters present",
            "acceptances": [],
            "repair_package": [],
        }
        self.assertIs(
            _merge_contract_and_semantic_verdict(semantic, contract),
            semantic,
        )

    def test_nd_options_never_include_isomorphic_continue(self):
        for scenario in ("plan", "recovery_cap", "checker", "empty_delivery"):
            option_ids = {
                item["option_id"]
                for item in build_decision_options(
                    scenario=scenario, reason="blocked", basis="matrix"
                )
            }
            self.assertNotIn("continue", option_ids)
            self.assertNotIn("isomorphic_continue", option_ids)

    def test_failure_signature_changes_with_revision(self):
        a = failure_signature(1, "execute", "provider", None)
        b = failure_signature(2, "execute", "provider", None)
        self.assertNotEqual(a, b)

    def test_t14_second_soft_requires_increment(self):
        self.assertFalse(soft_extension_requires_increment(0))
        self.assertTrue(soft_extension_requires_increment(1))


class ProviderAdapterMatrixTest(unittest.TestCase):
    def test_default_order_is_cursor_then_deepseek(self):
        order = DEFAULT_SETTINGS["provider_order"]
        for role in (
            "planner",
            "fullstack",
            "independent_checker",
            "simple",
            "provider_probe",
        ):
            self.assertEqual(order[role][:2], ["cursor_cli", "deepseek"])
            self.assertNotIn("codex_cli", order[role][:2])

    def test_registered_model_providers_have_capabilities(self):
        for key in MODEL_PROVIDER_KEYS:
            self.assertIn(key, PROVIDERS)
            self.assertIn(key, PROVIDER_CAPABILITIES)

    def test_json_worker_pair_shares_adapter_not_model_transport(self):
        self.assertEqual(PROVIDERS["deepseek"]["adapter"], "json-worker")
        self.assertEqual(PROVIDERS["kimi"]["adapter"], "json-worker")
        self.assertEqual(
            PROVIDER_CAPABILITIES["deepseek"]["model_transport"], "env"
        )
        self.assertEqual(
            PROVIDER_CAPABILITIES["kimi"]["model_transport"], "env"
        )
        self.assertFalse(
            PROVIDER_CAPABILITIES["deepseek"]["accepts_model_flag"]
        )
        self.assertFalse(PROVIDER_CAPABILITIES["kimi"]["accepts_model_flag"])

    def test_cursor_and_codex_use_flag_model_transport(self):
        self.assertEqual(
            PROVIDER_CAPABILITIES["cursor_cli"]["model_transport"], "flag"
        )
        self.assertEqual(
            PROVIDER_CAPABILITIES["codex_cli"]["model_transport"], "flag"
        )

    def test_local_script_is_deterministic_runner(self):
        self.assertEqual(PROVIDERS["local_script"]["adapter"], "local-script")
        self.assertEqual(
            DEFAULT_SETTINGS["provider_order"]["local_script_runner"],
            ["local_script"],
        )


if __name__ == "__main__":
    unittest.main()
