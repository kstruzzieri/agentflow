from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agentflow.contracts import WORKFLOW_PACK_SCHEMA_VERSION
from agentflow.packs import (
    Pack,
    PackError,
    _safe_relative_path,
    find_profile,
    inspect_summary,
    load_pack,
    profile_to_contract,
    resolve_manifest_path,
    template_to_plan,
    validate_pack_manifest,
)
from agentflow.validation import validate_plan
from agentflow.workflow_contract import validate_workflow_contract


class PackSchemaVersionTests(unittest.TestCase):
    def test_pack_schema_version_is_v0_1_0(self) -> None:
        self.assertEqual(WORKFLOW_PACK_SCHEMA_VERSION, "0.1.0")


class SafeRelativePathTests(unittest.TestCase):
    def test_accepts_simple_relative_paths(self) -> None:
        self.assertTrue(_safe_relative_path("README.md"))
        self.assertTrue(_safe_relative_path("hooks/pre-commit.sh"))

    def test_rejects_unsafe_paths(self) -> None:
        for bad in [
            "",
            "/etc/passwd",
            "C:/Windows/system32",
            "hooks\\pre-commit.sh",
            "../escape",
            "a/../b",
            "./a",
            "a/",
            "with\x00nul",
            123,
            None,
        ]:
            self.assertFalse(_safe_relative_path(bad), bad)


class ResolveManifestPathTests(unittest.TestCase):
    def _write_pack(self, root: Path) -> Path:
        pack_dir = root / ".agentflow-pack"
        pack_dir.mkdir(parents=True)
        manifest = pack_dir / "pack.json"
        manifest.write_text("{}", encoding="utf-8")
        return manifest

    def test_resolves_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_pack(root)
            self.assertEqual(resolve_manifest_path(root), manifest)

    def test_resolves_pack_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_pack(root)
            self.assertEqual(resolve_manifest_path(root / ".agentflow-pack"), manifest)

    def test_resolves_manifest_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_pack(root)
            self.assertEqual(resolve_manifest_path(manifest), manifest)

    def test_raises_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PackError):
                resolve_manifest_path(Path(tmp))


def valid_template() -> dict:
    return {
        "schema_version": "0.3.0",
        "objective": "TODO: describe the objective",
        "scope": ["src/"],
        "non_goals": [],
        "invariants": ["stdlib only"],
        "allowed_files": ["src/", ".agent/"],
        "blocked_files": [],
        "validation_gates": ["unit-tests"],
        "rollback_plan": "git restore .",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
        },
        "steps": [
            {
                "id": "P1",
                "action": "do the thing",
                "files": ["src/"],
                "preconditions": [],
                "expected_diff": [],
                "validation": ["unit-tests"],
                "evidence_ids": [],
            }
        ],
        "evidence_ids": [],
        "locked": False,
        "locked_at": None,
    }


def valid_pack() -> dict:
    return {
        "schema_version": "0.1.0",
        "id": "python-library-proof-gate",
        "name": "Python Library Proof Gate",
        "description": "Stdlib-only Python library workflow.",
        "plan_templates": {"python-library": valid_template()},
        "profiles": [
            {
                "id": "default",
                "review_depth": "standard",
                "required_capabilities": [{"id": "python", "required": True}],
                "validation_policy": {"required_gates": ["unit-tests"]},
                "proof_policy": {"hunk_attribution": "enforce", "require_review_run": False},
                "plan_template": "python-library",
            }
        ],
        "hook_templates": [{"id": "pre-commit", "path": "hooks/pre-commit.sh", "describe": "gate"}],
        "readme": "README.md",
    }


class ValidatePackManifestTests(unittest.TestCase):
    def test_valid_pack_has_no_errors(self) -> None:
        self.assertEqual(validate_pack_manifest(valid_pack()), [])

    def test_rejects_non_object(self) -> None:
        self.assertEqual(validate_pack_manifest([]), ["pack manifest must be a JSON object"])

    def test_rejects_unknown_pack_field(self) -> None:
        pack = valid_pack()
        pack["registry"] = "https://example.com"
        self.assertIn("unknown pack field: registry", validate_pack_manifest(pack))

    def test_rejects_missing_required_field(self) -> None:
        pack = valid_pack()
        del pack["profiles"]
        self.assertIn("missing required pack field: profiles", validate_pack_manifest(pack))

    def test_rejects_bad_schema_version(self) -> None:
        pack = valid_pack()
        pack["schema_version"] = "9.9.9"
        self.assertTrue(any("schema_version" in e for e in validate_pack_manifest(pack)))

    def test_rejects_empty_profiles(self) -> None:
        pack = valid_pack()
        pack["profiles"] = []
        self.assertIn("profiles must be a non-empty array", validate_pack_manifest(pack))

    def test_rejects_unknown_profile_field(self) -> None:
        pack = valid_pack()
        pack["profiles"][0]["weight"] = 1
        self.assertIn("profiles[1] unknown field: weight", validate_pack_manifest(pack))

    def test_rejects_bad_review_depth(self) -> None:
        pack = valid_pack()
        pack["profiles"][0]["review_depth"] = "exhaustive"
        self.assertTrue(any("review_depth" in e for e in validate_pack_manifest(pack)))

    def test_rejects_bad_hunk_attribution(self) -> None:
        pack = valid_pack()
        pack["profiles"][0]["proof_policy"]["hunk_attribution"] = "maybe"
        self.assertTrue(any("hunk_attribution" in e for e in validate_pack_manifest(pack)))

    def test_rejects_bad_capability_shape(self) -> None:
        pack = valid_pack()
        pack["profiles"][0]["required_capabilities"] = [{"id": "python", "required": "yes"}]
        self.assertTrue(
            any("required must be boolean" in e for e in validate_pack_manifest(pack))
        )
        pack = valid_pack()
        pack["profiles"][0]["required_capabilities"] = [{"required": True}]
        self.assertTrue(
            any("id must be a non-empty string" in e for e in validate_pack_manifest(pack))
        )

    def test_rejects_unknown_template_reference(self) -> None:
        pack = valid_pack()
        pack["profiles"][0]["plan_template"] = "missing"
        self.assertIn(
            "profiles[1] plan_template references unknown template: missing",
            validate_pack_manifest(pack),
        )

    def test_rejects_non_string_template_reference(self) -> None:
        pack = valid_pack()
        pack["profiles"][0]["plan_template"] = ["python-library"]
        self.assertIn(
            "profiles[1].plan_template must be a non-empty string",
            validate_pack_manifest(pack),
        )

    def test_rejects_gates_not_subset_of_template(self) -> None:
        pack = valid_pack()
        pack["profiles"][0]["validation_policy"]["required_gates"] = ["typo-gate"]
        self.assertIn(
            "profiles[1] validation_policy.required_gates not a subset of template "
            "python-library validation_gates: typo-gate",
            validate_pack_manifest(pack),
        )

    def test_malformed_template_gates_no_spurious_subset_error(self) -> None:
        # A non-list validation_gates is a template error; it must NOT also
        # surface a confusing "not a subset" error against the broken template.
        pack = valid_pack()
        pack["plan_templates"]["python-library"]["validation_gates"] = "unit-tests"
        errors = validate_pack_manifest(pack)
        self.assertTrue(any("plan_templates[python-library]:" in e for e in errors))
        self.assertFalse(any("not a subset of template" in e for e in errors))

    def test_rejects_locked_template(self) -> None:
        pack = valid_pack()
        pack["plan_templates"]["python-library"]["locked"] = True
        self.assertIn(
            "plan_templates[python-library] must be unlocked (locked must not be true)",
            validate_pack_manifest(pack),
        )

    def test_rejects_non_null_locked_at(self) -> None:
        pack = valid_pack()
        pack["plan_templates"]["python-library"]["locked_at"] = "2026-06-27T00:00:00+00:00"
        self.assertIn(
            "plan_templates[python-library] must be unlocked (locked_at must be null)",
            validate_pack_manifest(pack),
        )

    def test_rejects_invalid_template_plan(self) -> None:
        pack = valid_pack()
        pack["plan_templates"]["python-library"]["objective"] = ""
        errors = validate_pack_manifest(pack)
        self.assertTrue(any(e.startswith("plan_templates[python-library]:") for e in errors))

    def test_rejects_unsafe_hook_path(self) -> None:
        pack = valid_pack()
        pack["hook_templates"][0]["path"] = "../escape.sh"
        self.assertIn(
            "hook_templates[1].path must be a safe relative path",
            validate_pack_manifest(pack),
        )

    def test_rejects_unsafe_readme(self) -> None:
        pack = valid_pack()
        pack["readme"] = "/etc/passwd"
        self.assertIn("readme must be a safe relative path", validate_pack_manifest(pack))


class LoadPackTests(unittest.TestCase):
    def _write(self, root: Path, text: str) -> Path:
        pack_dir = root / ".agentflow-pack"
        pack_dir.mkdir(parents=True)
        manifest = pack_dir / "pack.json"
        manifest.write_text(text, encoding="utf-8")
        return manifest

    def test_loads_valid_pack_with_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = json.dumps(valid_pack())
            manifest = self._write(root, text)
            pack = load_pack(root)
            self.assertIsInstance(pack, Pack)
            self.assertEqual(pack.manifest["id"], "python-library-proof-gate")
            self.assertEqual(pack.manifest_path, manifest)
            self.assertEqual(
                pack.manifest_sha256,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )

    def test_raises_on_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "{not json")
            with self.assertRaises(PackError):
                load_pack(root)

    def test_raises_on_invalid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, json.dumps({"id": "x"}))
            with self.assertRaises(PackError):
                load_pack(root)

    def test_load_then_template_to_plan_is_valid_and_unlocked(self) -> None:
        # End-to-end: a pack read from disk projects into a lockable plan.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, json.dumps(valid_pack()))
            pack = load_pack(root)
            template_id = find_profile(pack.manifest, "default")["plan_template"]
            plan = template_to_plan(pack.manifest, template_id)
            self.assertEqual(validate_plan(plan), [])
            self.assertIs(plan["locked"], False)
            self.assertIsNone(plan["locked_at"])


class ProfileToContractTests(unittest.TestCase):
    def test_contract_round_trips_through_validator(self) -> None:
        contract = profile_to_contract(
            valid_pack(), "default", "init --pack", "because reasons"
        )
        self.assertEqual(validate_workflow_contract(contract), [])
        self.assertEqual(contract["workflow_pack"], "python-library-proof-gate")
        self.assertEqual(contract["workflow_profile"], "default")
        self.assertEqual(contract["selected_by"], "init --pack")
        self.assertEqual(contract["selection_reason"], "because reasons")
        self.assertEqual(contract["review_depth"], "standard")

    def test_unknown_profile_raises(self) -> None:
        with self.assertRaises(PackError):
            profile_to_contract(valid_pack(), "missing", "init --pack", "r")

    def test_find_profile_returns_match(self) -> None:
        self.assertEqual(find_profile(valid_pack(), "default")["id"], "default")


class TemplateToPlanTests(unittest.TestCase):
    def test_plan_is_valid_and_unlocked(self) -> None:
        plan = template_to_plan(valid_pack(), "python-library")
        self.assertEqual(validate_plan(plan), [])
        self.assertIs(plan["locked"], False)
        self.assertIsNone(plan["locked_at"])

    def test_forces_unlocked_even_if_template_mutated(self) -> None:
        pack = valid_pack()
        # A template that slips through with locked metadata is still forced open.
        pack["plan_templates"]["python-library"]["locked"] = True
        pack["plan_templates"]["python-library"]["locked_at"] = "2026-06-27T00:00:00+00:00"
        plan = template_to_plan(pack, "python-library")
        self.assertIs(plan["locked"], False)
        self.assertIsNone(plan["locked_at"])

    def test_does_not_mutate_source_template(self) -> None:
        pack = valid_pack()
        template_to_plan(pack, "python-library")
        self.assertIs(pack["plan_templates"]["python-library"]["locked"], False)

    def test_unknown_template_raises(self) -> None:
        with self.assertRaises(PackError):
            template_to_plan(valid_pack(), "missing")


class ExamplePackTests(unittest.TestCase):
    EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "packs"

    def test_python_library_pack_loads(self) -> None:
        pack = load_pack(self.EXAMPLES / "python-library-proof-gate")
        self.assertEqual(pack.manifest["id"], "python-library-proof-gate")
        contract = profile_to_contract(pack.manifest, "default", "test", "r")
        self.assertEqual(validate_workflow_contract(contract), [])
        plan = template_to_plan(
            pack.manifest, find_profile(pack.manifest, "default")["plan_template"]
        )
        self.assertEqual(validate_plan(plan), [])

    def test_openhands_pack_loads_and_declares_hook(self) -> None:
        pack = load_pack(self.EXAMPLES / "openhands-hook-integration")
        self.assertEqual(pack.manifest["id"], "openhands-hook-integration")
        summary = inspect_summary(pack)
        hook_paths = [hook["path"] for hook in summary["hook_templates"]]
        self.assertIn("hooks/pre-commit.sh", hook_paths)
        # The pack's profile and template must also materialize validly.
        contract = profile_to_contract(pack.manifest, "default", "test", "r")
        self.assertEqual(validate_workflow_contract(contract), [])
        plan = template_to_plan(
            pack.manifest, find_profile(pack.manifest, "default")["plan_template"]
        )
        self.assertEqual(validate_plan(plan), [])


if __name__ == "__main__":
    unittest.main()
