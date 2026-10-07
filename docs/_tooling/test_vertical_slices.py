import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path


sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("vertical_slices.py")
SPEC = importlib.util.spec_from_file_location("vertical_slices", SCRIPT)
VS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VS)


class VerticalSlicesTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.tooling = self.repo / "docs" / "_tooling"
        self.tooling.mkdir(parents=True)
        for name in ("document.schema.json", "slice.schema.json", "delivery-docs.schema.json"):
            source = VS.distribution_asset(name)
            (self.tooling / name).write_bytes(source.read_bytes())
        self.config = {
            "schema": "delivery.documentation/v1",
            "tooling": {"install_dir": "docs/_tooling"},
            "documents": {
                "include": ["*.md", "docs/**/*.md"],
                "exclude": ["docs/_tooling/node_modules/**"],
                "schema": "docs/_tooling/document.schema.json",
            },
            "slices": {
                "root": "docs/slices",
                "manifest": "manifest.yaml",
                "schema": "docs/_tooling/slice.schema.json",
            },
            "projections": [],
            "diagrams": {"mermaid": False},
        }
        self.config_path = self.repo / "docs" / "delivery-docs.yaml"
        self.config_path.write_text(VS.yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()
        self.redirect.__exit__(None, None, None)

    def args(self, **values):
        defaults = {"repo": self.repo, "config": "docs/delivery-docs.yaml", "plan": False}
        defaults.update(values)
        return Namespace(**defaults)

    def write_document(self, path: Path, **updates):
        metadata = {
            "title": "Test",
            "kind": "guide",
            "lifecycle": "stable",
            "authority": "Test authority",
            "summary": "Test document.",
            "maintenance": ["The tested invariant changes."],
        }
        metadata.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VS.frontmatter_document(metadata, "# Test\n"), encoding="utf-8")

    def git_init(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True)

    def test_missing_frontmatter_is_rejected(self):
        path = self.repo / "docs" / "bad.md"
        path.write_text("# Missing\n", encoding="utf-8")
        _, config = VS.load_config(self.repo, "docs/delivery-docs.yaml")
        with self.assertRaisesRegex(VS.DeliveryError, "missing YAML frontmatter"):
            VS.load_documents(self.repo, config)

    def test_malformed_configuration_is_rejected(self):
        self.config_path.write_text("schema: wrong\n", encoding="utf-8")
        with self.assertRaisesRegex(VS.DeliveryError, "delivery.documentation/v1"):
            VS.load_config(self.repo, "docs/delivery-docs.yaml")

    def test_init_project_plan_and_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            args = Namespace(
                repo=repo, config="docs/delivery-docs.yaml", plan=True,
                install_dir="docs/_tooling", vendor=True,
            )
            VS.command_init_project(args)
            self.assertFalse((repo / "docs").exists())
            args.plan = False
            VS.command_init_project(args)
            self.assertTrue((repo / "docs" / "delivery-docs.yaml").exists())
            self.assertTrue((repo / "docs" / "_tooling" / "document.schema.json").exists())
            self.assertTrue((repo / "docs" / "_tooling" / "vertical_slices.py").exists())
            self.assertEqual("3.12\n", (repo / "docs" / "_tooling" / ".python-version").read_text())
            self.assertTrue((repo / "docs" / "_tooling" / "pyproject.toml").exists())
            self.assertTrue((repo / "docs" / "_tooling" / "uv.lock").exists())
            self.assertFalse((repo / "docs" / "_tooling" / "package.json").exists())
            with self.assertRaisesRegex(VS.DeliveryError, "refusing to overwrite"):
                VS.command_init_project(args)

    def test_missing_dependencies_have_one_remediation_command(self):
        result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("uv run --project", result.stderr + result.stdout)
        self.assertIn("--frozen", result.stderr + result.stdout)

    def test_duplicate_document_ids_are_rejected(self):
        self.write_document(self.repo / "docs" / "one.md", id="DOC-1")
        self.write_document(self.repo / "docs" / "two.md", id="DOC-1")
        _, config = VS.load_config(self.repo, "docs/delivery-docs.yaml")
        with self.assertRaisesRegex(VS.DeliveryError, "duplicate document id"):
            VS.load_documents(self.repo, config)

    def test_custom_document_schema_is_honored(self):
        schema_path = self.tooling / "custom.schema.json"
        schema_path.write_text(
            json.dumps({"type": "object", "required": ["custom"], "properties": {"custom": {"const": True}}}),
            encoding="utf-8",
        )
        self.config["documents"]["schema"] = "docs/_tooling/custom.schema.json"
        self.config_path.write_text(VS.yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")
        self.write_document(self.repo / "README.md")
        _, config = VS.load_config(self.repo, "docs/delivery-docs.yaml")
        with self.assertRaisesRegex(VS.DeliveryError, "custom"):
            VS.load_documents(self.repo, config)

    def test_new_slice_plan_is_non_mutating(self):
        VS.command_new_slice(
            self.args(
                id="TEST-001", title="Test outcome", summary="A bounded outcome.", outcome="The owner sees success.",
                gate=["TEST-001-END=Exercise the real path."], contract_version="v1", runbook=True,
                plan=True,
            )
        )
        self.assertFalse((self.repo / "docs" / "slices" / "TEST-001").exists())

    def test_new_slice_creates_bundle_and_refuses_overwrite(self):
        args = self.args(
            id="TEST-001", title="Test outcome", summary="A bounded outcome.", outcome="The owner sees success.",
            gate=["TEST-001-END=Exercise the real path."], contract_version="v1", runbook=True,
        )
        VS.command_new_slice(args)
        bundle = self.repo / "docs" / "slices" / "TEST-001"
        self.assertEqual(
            {"README.md", "contract-v1.md", "manifest.yaml", "runbook.md"},
            {path.name for path in bundle.iterdir()},
        )
        with self.assertRaisesRegex(VS.DeliveryError, "refusing to overwrite"):
            VS.command_new_slice(args)

    def test_invalid_state_verdict_and_missing_evidence_are_rejected(self):
        args = self.args(
            id="TEST-001", title="Test outcome", summary="A bounded outcome.", outcome="The owner sees success.",
            gate=["TEST-001-END=Exercise the real path."], contract_version="v1", runbook=False,
        )
        VS.command_new_slice(args)
        manifest_path = self.repo / "docs" / "slices" / "TEST-001" / "manifest.yaml"
        manifest = VS.read_yaml(manifest_path)
        manifest["state"] = "active"
        manifest["verdict"] = "accepted"
        manifest_path.write_text(VS.yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        _, config = VS.load_config(self.repo, "docs/delivery-docs.yaml")
        documents = VS.load_documents(self.repo, config)
        with self.assertRaisesRegex(VS.DeliveryError, "invalid state/verdict"):
            VS.load_manifests(self.repo, config, documents)
        manifest["verdict"] = "pending"
        manifest["gates"]["TEST-001-END"]["state"] = "pass"
        manifest["next_gate"] = None
        manifest_path.write_text(VS.yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        with self.assertRaisesRegex(VS.DeliveryError, "needs evidence"):
            VS.load_manifests(self.repo, config, documents)

    def test_evidence_captures_stable_dirty_digest_without_contents(self):
        args = self.args(
            id="TEST-001", title="Test outcome", summary="A bounded outcome.", outcome="The owner sees success.",
            gate=["TEST-001-END=Exercise the real path."], contract_version="v1", runbook=False,
        )
        VS.command_new_slice(args)
        self.git_init()
        secret = self.repo / "untracked-secret.txt"
        secret.write_text("never-copy-this-value", encoding="utf-8")
        result_file = self.repo / "result.json"
        result_file.write_text('{"passed": true, "detail": "do-not-copy"}\n', encoding="utf-8")
        evidence_args = self.args(
            slice="TEST-001", name="attempt-1", summary="Local result.",
            gate=["TEST-001-END=pass"], result_file=[str(result_file)],
        )
        first = VS.repository_identity(self.repo)
        second = VS.repository_identity(self.repo)
        self.assertEqual(first["working_tree_digest"], second["working_tree_digest"])
        VS.command_new_evidence(evidence_args)
        evidence_path = self.repo / "docs" / "slices" / "TEST-001" / "evidence" / "attempt-1.md"
        text = evidence_path.read_text(encoding="utf-8")
        self.assertNotIn("never-copy-this-value", text)
        self.assertNotIn("untracked-secret.txt", text)
        self.assertNotIn("do-not-copy", text)
        self.assertIn("working_tree_digest", text)
        self.assertIn("result.json", text)
        with self.assertRaisesRegex(VS.DeliveryError, "refusing to overwrite"):
            VS.command_new_evidence(evidence_args)

    def test_reconcile_requires_matching_evidence_and_updates_manifest(self):
        args = self.args(
            id="TEST-001", title="Test outcome", summary="A bounded outcome.", outcome="The owner sees success.",
            gate=["TEST-001-END=Exercise the real path."], contract_version="v1", runbook=False,
        )
        VS.command_new_slice(args)
        self.git_init()
        VS.command_new_evidence(
            self.args(slice="TEST-001", name="attempt-1", summary="Local result.", gate=["TEST-001-END=pass"], result_file=[])
        )
        evidence = "docs/slices/TEST-001/evidence/attempt-1.md"
        VS.command_reconcile(
            self.args(
                slice="TEST-001", evidence=evidence, gate=["TEST-001-END=pass"], state="closed",
                owner_verdict="pending", blocker="-", next_gate="-",
            )
        )
        manifest = VS.read_yaml(self.repo / "docs" / "slices" / "TEST-001" / "manifest.yaml")
        self.assertEqual("pass", manifest["gates"]["TEST-001-END"]["state"])
        self.assertIsNone(manifest["next_gate"])

    def test_evidence_and_reconcile_plan_are_non_mutating(self):
        VS.command_new_slice(
            self.args(
                id="TEST-001", title="Test outcome", summary="A bounded outcome.", outcome="The owner sees success.",
                gate=["TEST-001-END=Exercise the real path."], contract_version="v1", runbook=False,
            )
        )
        self.git_init()
        evidence_args = self.args(
            slice="TEST-001", name="attempt-1", summary="Local result.",
            gate=["TEST-001-END=pass"], result_file=[], plan=True,
        )
        VS.command_new_evidence(evidence_args)
        evidence_path = self.repo / "docs" / "slices" / "TEST-001" / "evidence" / "attempt-1.md"
        self.assertFalse(evidence_path.exists())
        evidence_args.plan = False
        VS.command_new_evidence(evidence_args)
        manifest_path = self.repo / "docs" / "slices" / "TEST-001" / "manifest.yaml"
        before = manifest_path.read_bytes()
        VS.command_reconcile(
            self.args(
                slice="TEST-001", evidence=str(evidence_path.relative_to(self.repo)), gate=["TEST-001-END=pass"],
                state="closed", owner_verdict="pending", blocker="-", next_gate="-", plan=True,
            )
        )
        self.assertEqual(before, manifest_path.read_bytes())

    def test_generated_output_is_deterministic_and_check_detects_stale(self):
        self.write_document(
            self.repo / "docs" / "README.md", kind="landing", generated_by="vertical_slices.py",
            generated_sections=["catalog"],
        )
        path = self.repo / "docs" / "README.md"
        metadata, _ = VS.split_frontmatter(self.repo, path)
        path.write_text(
            VS.frontmatter_document(metadata, "# Docs\n\n<!-- BEGIN GENERATED:catalog -->\nold\n<!-- END GENERATED:catalog -->\n"),
            encoding="utf-8",
        )
        self.config["projections"] = [{"type": "catalog", "target": "docs/README.md", "marker": "catalog"}]
        self.config_path.write_text(VS.yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")
        with self.assertRaisesRegex(VS.DeliveryError, "stale"):
            VS.command_check(self.args())
        VS.command_write(self.args())
        first = path.read_bytes()
        VS.command_write(self.args())
        self.assertEqual(first, path.read_bytes())
        VS.command_check(self.args())

    def test_broken_link_and_prose_guard_are_rejected(self):
        path = self.repo / "docs" / "guide.md"
        self.write_document(path)
        metadata, _ = VS.split_frontmatter(self.repo, path)
        path.write_text(VS.frontmatter_document(metadata, "# Test\n\n[Missing](missing.md)\n"), encoding="utf-8")
        _, config = VS.load_config(self.repo, "docs/delivery-docs.yaml")
        documents = VS.load_documents(self.repo, config)
        with self.assertRaisesRegex(VS.DeliveryError, "broken Markdown links"):
            VS.validate_links(self.repo, documents)
        path.write_text(VS.frontmatter_document(metadata, "# Test\n\n0123456789abcdef0123456789abcdef01234567\n"), encoding="utf-8")
        self.config["prose_guards"] = [{"paths": ["docs/guide.md"], "deny": ["git-sha"]}]
        self.config_path.write_text(VS.yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")
        _, config = VS.load_config(self.repo, "docs/delivery-docs.yaml")
        with self.assertRaisesRegex(VS.DeliveryError, "structured metadata"):
            VS.validate_prose(self.repo, config, VS.load_documents(self.repo, config))

    def test_navigation_depth_is_enforced(self):
        for name in ("README.md", "middle.md", "required.md"):
            self.write_document(self.repo / "docs" / name)
        start = self.repo / "docs" / "README.md"
        metadata, _ = VS.split_frontmatter(self.repo, start)
        start.write_text(VS.frontmatter_document(metadata, "# Start\n\n[Middle](middle.md)\n"), encoding="utf-8")
        self.config["navigation"] = {
            "start": "docs/README.md", "max_depth": 1, "required": ["docs/required.md"], "active_evidence": False,
        }
        self.config_path.write_text(VS.yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")
        _, config = VS.load_config(self.repo, "docs/delivery-docs.yaml")
        documents = VS.load_documents(self.repo, config)
        with self.assertRaisesRegex(VS.DeliveryError, "within 1 links"):
            VS.validate_navigation(self.repo, config, documents, {})

    def test_inspect_is_read_only(self):
        self.write_document(self.repo / "README.md")
        before = {path: path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        VS.command_inspect(self.args(format="json"))
        after = {path: path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_vendor_round_trip_and_local_edit_detection(self):
        schema_before = (self.tooling / "document.schema.json").read_bytes()
        config_before = self.config_path.read_bytes()
        VS.install_vendor(self.repo, "docs/vendor", False)
        installed = self.repo / "docs" / "vendor" / "vertical_slices.py"
        VS.install_vendor(self.repo, "docs/vendor", False)
        self.assertEqual(schema_before, (self.tooling / "document.schema.json").read_bytes())
        self.assertEqual(config_before, self.config_path.read_bytes())
        installed.write_text(installed.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
        with self.assertRaisesRegex(VS.DeliveryError, "locally edited"):
            VS.install_vendor(self.repo, "docs/vendor", False)

    def test_vendor_plan_is_non_mutating(self):
        VS.install_vendor(self.repo, "docs/vendor", True)
        self.assertFalse((self.repo / "docs" / "vendor").exists())


if __name__ == "__main__":
    unittest.main()
