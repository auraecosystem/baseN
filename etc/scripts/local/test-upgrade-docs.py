#!/usr/bin/env python3
"""Exercise the upgrade documentation CLI on disposable repositories."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("upgrade-docs.py")


class UpgradeDocsTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("etc/scripts/local/upgrade-docs.py", SCRIPT.read_text())
        self.write("crates/common/genesis/src/feature.rs", """
features! {
    /// Include the deposit nonce in receipts.
    DepositNonce => Regolith,
}
""")
        self.write("crates/common/genesis/src/chain/upgrade.rs", """
    BaseUpgrade {
        Bedrock,
        Regolith,
    }
""")
        for adapter in (
            "genesis/src/rollup.rs", "chains/src/upgrades.rs", "evm/src/spec.rs", "evm2/src/spec.rs",
            "chains/src/upgrade.rs", "genesis/src/schedule.rs", "evm2/src/transition.rs",
        ):
            self.write("crates/common/" + adapter, "// schedule adapter\n")
        self.write("crates/execution/receipt/src/lib.rs", """
fn receipt() { if enabled(BaseFeature::DepositNonce) { include_nonce(); } }
#[cfg(test)]
mod tests { fn fixture() {} }
""")
        self.write("docs/guides/UPGRADES.md", """# Upgrades
Baseline protocol.
<!-- REVIEW:Bedrock:pending -->
<!-- FEATURES:Bedrock:START -->
<!-- FEATURES:Bedrock:END -->
Deposit nonce behavior.
<!-- REVIEW:Regolith:pending -->
<!-- FEATURES:Regolith:START -->
<!-- FEATURES:Regolith:END -->
""")

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def run_cli(self, *args, success=True):
        result = subprocess.run(
            ["python3", "etc/scripts/local/upgrade-docs.py", *args],
            cwd=self.root, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def stamp(self):
        self.run_cli("stamp", "Bedrock", "Regolith")
        self.run_cli("check")

    def test_sync_does_not_attest_to_review_and_code_changes_expire_review(self):
        self.run_cli("sync")
        self.assertIn("review prose", self.run_cli("check", success=False))
        self.stamp()
        consumer = self.root / "crates/execution/receipt/src/lib.rs"
        consumer.write_text(consumer.read_text().replace("include_nonce()", "omit_nonce()"))
        self.run_cli("sync")
        self.assertIn("Regolith: production code changed", self.run_cli("check", success=False))
        self.run_cli("stamp", "Regolith")
        self.run_cli("check")
        # Test-only edits must not force prose review.
        consumer.write_text(consumer.read_text().replace("fixture()", "different_fixture()"))
        self.run_cli("check")
        # An early test helper declaration must not hide subsequent production code.
        consumer.write_text("#[cfg(test)]\nmod mocks;\n" + consumer.read_text())
        self.run_cli("stamp", "Regolith")
        consumer.write_text(consumer.read_text().replace("omit_nonce()", "include_nonce()"))
        self.assertIn("Regolith: production code changed", self.run_cli("check", success=False))

    def test_schedule_changes_without_feature_references_expire_review(self):
        for path in (
            "genesis/src/chain/upgrade.rs",
            "chains/src/upgrade.rs",
            "genesis/src/schedule.rs",
            "evm2/src/transition.rs",
        ):
            with self.subTest(path=path):
                schedule = self.root / "crates/common" / path
                schedule.write_text(schedule.read_text() + "\nfn activation() { configured_time() }\n")
                self.stamp()
                schedule.write_text(schedule.read_text().replace("configured_time()", "other_time()"))
                self.run_cli("sync")
                errors = self.run_cli("check", success=False)
                for upgrade in ("Bedrock", "Regolith"):
                    self.assertIn(f"{upgrade}: production code changed", errors)
                self.stamp()

    def test_attribute_like_source_does_not_stall_inventory(self):
        consumer = self.root / "crates/execution/receipt/src/lib.rs"
        repeated_attributes = "#[cfg(test)]#[" + "]#[" * 32 + "!"
        consumer.write_text(
            f'const ATTRIBUTES: &str = "{repeated_attributes}";\n' + consumer.read_text()
        )
        self.stamp()

    def test_inventory_tracks_new_and_deleted_consumers(self):
        self.stamp()
        self.write("crates/proof/receipt/src/lib.rs", """
fn proof() { BaseFeature::activation_states([BaseFeature::DepositNonce], enabled); }
""")
        self.assertIn("inventory is stale", self.run_cli("check", success=False))
        self.run_cli("stamp", "Regolith")
        guide = (self.root / "docs/guides/UPGRADES.md").read_text()
        self.assertIn("../../crates/proof/receipt/src/lib.rs", guide)
        shutil.rmtree(self.root / "crates/proof")
        self.assertIn("broken code/documentation link", self.run_cli("check", success=False))

    def test_unknown_feature_missing_upgrade_and_dead_feature_fail(self):
        self.stamp()
        self.write("crates/execution/receipt/src/lib.rs", "fn receipt() { enabled(BaseFeature::Unknown); }")
        self.assertIn("unregistered feature", self.run_cli("check", success=False))
        self.write("crates/execution/receipt/src/lib.rs", "fn receipt() {}")
        self.assertIn("no production consumers", self.run_cli("check", success=False))
        self.write("crates/execution/receipt/src/lib.rs", "fn receipt() { enabled(BaseFeature::DepositNonce); }")
        upgrade = self.root / "crates/common/genesis/src/chain/upgrade.rs"
        upgrade.write_text(upgrade.read_text().replace("        Regolith,", "        Regolith,\n        Canyon,"))
        self.assertIn("exactly one FEATURES block", self.run_cli("check", success=False))

    def test_named_behavior_gates_are_rejected_but_ethereum_and_tests_are_allowed(self):
        self.stamp()
        consumer = self.root / "crates/execution/receipt/src/lib.rs"
        original = consumer.read_text()
        for gate in (
            "cfg.is_regolith_active(10)",
            "spec.is_enabled_in(BaseUpgrade::Regolith)",
            "upgrade >= BaseUpgrade::Regolith",
        ):
            consumer.write_text(f"fn bypass() {{ {gate}; }}\n" + original)
            self.run_cli("stamp", "Regolith")
            self.assertIn("must use a feature gate", self.run_cli("check", success=False))
        consumer.write_text(original.replace(
            "fn fixture() {}", "fn fixture() { cfg.is_regolith_active(0); }"
        ).replace("#[cfg(test)]", '#[cfg(all(test, feature = "std"))]\n#[rustfmt::skip]')
            .replace("mod tests", "pub(super) mod tests"))
        consumer.write_text("fn ethereum() { spec.is_enabled_in(SpecId::OSAKA); }\n" + consumer.read_text())
        self.run_cli("stamp", "Regolith")
        self.run_cli("check")


if __name__ == "__main__":
    unittest.main()
