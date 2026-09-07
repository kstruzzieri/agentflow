from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agentflow import aggregate
from agentflow.aggregate import Source
from agentflow.artifacts import read_json, read_jsonl
from agentflow.cli_contract import JSON_OUTPUTS, build_cli_contract
from agentflow.events import project_events
from agentflow.execution import doctor
from agentflow.porcelain import Action, next_action
from agentflow.versioning import parse_schema_version


ROOT = Path(__file__).resolve().parents[1]
COMPATIBILITY = ROOT / "tests/fixtures/compatibility"

# Working state that these tests read back through the live readers. Auxiliary
# ledgers are excluded: they keep independent versions and never gate a major.
_WORKING_STATE = (
    ".agent/execution.contract.json",
    ".agent/plan.lock.json",
    ".agent/step-runs.jsonl",
    ".agent/command-receipts.jsonl",
    ".agent/file-receipts.jsonl",
    ".agent/verification-runs.jsonl",
)
# The payload samples below need one row from each of these, plus the auxiliary
# runtime ledger, so a thin root cannot silently qualify as "representative".
_REQUIRED_ROWS = (
    ".agent/step-runs.jsonl",
    ".agent/command-receipts.jsonl",
    ".agent/file-receipts.jsonl",
    ".agent/verification-runs.jsonl",
    ".agent/runtime-snapshots.jsonl",
)


def _perturb_rows(root: Path) -> None:
    """Make a copied root collide with its original instead of deduplicating.

    Aggregation drops byte-identical rows on purpose, so a plain copy produces
    no overlap at all. Changing a non-identifying field on each row keeps the
    same step id and file path -- which is what step_overlap and file_overlap
    key on -- while leaving the rows schema-valid.
    """
    for rel, field, value in (
        (".agent/step-runs.jsonl", "agent_id", "twin-agent"),
        (".agent/file-receipts.jsonl", "after_sha256", "f" * 64),
    ):
        path = root / rel
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            row[field] = value
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )


def current_state_fixture() -> Path:
    """The compatibility fixture whose working state the live readers accept.

    The matrix deliberately keeps historical roots (legacy-0.3,
    released-v0.4.0, and every superseded current-* snapshot) that a newer
    major must refuse: docs/compatibility.md gives working state no
    cross-major promise, so those are covered through verify-proof's
    historical guarantee instead, not here.

    Selection is by recorded version under the live constants, not by name,
    because the frozen fixtures cannot be edited in place -- a major transition
    adds a new root beside them. The newest readable root wins, and it must win
    outright: picking the first readable directory would let these tests keep
    passing against a staler sample after a transition, which is precisely the
    silent coverage loss the freeze exists to prevent.
    """
    ranked: dict[Path, tuple] = {}
    for root in sorted(p for p in COMPATIBILITY.iterdir() if p.is_dir()):
        if not all((root / rel).exists() for rel in _REQUIRED_ROWS):
            continue
        try:
            versions = []
            for rel in _WORKING_STATE:
                path = root / rel
                rows = read_jsonl(path) if path.suffix == ".jsonl" else [read_json(path)]
                if not rows:
                    raise ValueError(f"{rel} is empty")
                versions.append(parse_schema_version(rows[0]["schema_version"]))
        except (ValueError, OSError, KeyError, TypeError):
            # Unreadable here means "written by another major", which is the
            # documented behaviour, not a fixture defect.
            continue
        ranked[root] = tuple((v.major, v.minor, v.patch) for v in versions)
    if not ranked:
        raise AssertionError(
            "no compatibility fixture is readable by the live schema constants. "
            "A major transition must add a current-major fixture beside the "
            "historical ones (docs/compatibility.md)."
        )
    best = max(ranked.values())
    winners = [root for root, key in ranked.items() if key == best]
    if len(winners) != 1:
        raise AssertionError(
            "compatibility fixtures tie for newest working state: "
            + ", ".join(sorted(p.name for p in winners))
        )
    return winners[0]


class StabilityPolicyTests(unittest.TestCase):
    def assertJsonContract(self, command: str, payload: object) -> None:
        def is_type(value: object, declared: str) -> bool:
            names = declared.split("|")
            checks = {
                "array": lambda item: isinstance(item, list),
                "boolean": lambda item: isinstance(item, bool),
                "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
                "null": lambda item: item is None,
                "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
                "object": lambda item: isinstance(item, dict),
                "string": lambda item: isinstance(item, str),
            }
            return any(checks[name](value) for name in names)

        def declared_type(declared: object) -> str:
            return declared if isinstance(declared, str) else declared["type"]

        def matches(value: object, declared: object) -> bool:
            kind = declared_type(declared)
            if not is_type(value, kind):
                return False
            if isinstance(declared, str) or value is None:
                return True
            if "object" in kind.split("|"):
                keys = declared["keys"]
                required = {
                    key
                    for key, child in keys.items()
                    if "null" not in declared_type(child).split("|")
                }
                if not required.issubset(value) or not set(value).issubset(keys):
                    return False
                return all(matches(item, keys[key]) for key, item in value.items())
            if "array" in kind.split("|") and "items" in declared:
                return all(matches(item, declared["items"]) for item in value)
            return True

        self.assertTrue(
            any(matches(payload, variant) for variant in JSON_OUTPUTS[command]),
            f"{command} payload does not match its public JSON contract: {payload!r}",
        )

    def test_json_contract_matches_representative_runtime_payloads(self) -> None:
        root = current_state_fixture()

        def rows(name: str) -> list[dict[str, object]]:
            return [
                json.loads(line)
                for line in (root / ".agent" / name).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        next_action_payload = next_action(root).to_dict()
        self.assertIsInstance(next_action_payload["resumability"], dict)

        samples = [
            ("doctor", doctor(root)),
            ("events", project_events(root)),
            ("next-action", next_action_payload),
            ("next-action", Action(
                "validation_missing", "validation gate is unmet", gate="test gate"
            ).to_dict()),
            ("claim-step", rows("step-runs.jsonl")[0]),
            ("run", rows("command-receipts.jsonl")[0]),
            ("record-file-change", [rows("file-receipts.jsonl")[0]]),
            ("verify-run", rows("verification-runs.jsonl")[0]),
            ("runtime-status", rows("runtime-snapshots.jsonl")[0]),
            (
                "record-review",
                {
                    "schema_version": "0.6.0",
                    "review_run_id": "RR-20260713T000000Z-1234abcd",
                    "recorded_at": "2026-07-13T00:00:00+00:00",
                    "state_dir": "docs/ai/state/main",
                    "manifest_path": "docs/ai/state/main/review-manifest.json",
                    "manifest_sha256": "0" * 64,
                    "plan_sha256": "1" * 64,
                    "policy": "main",
                    "gate_status": "pass",
                    "active_blocking": [],
                    "depth_profile": "deep",
                    "amendment_ready": True,
                    "findings": {"index": []},
                    "artifacts": [],
                },
            ),
            (
                "review-manifest",
                {
                    "schema_version": "1.0.0",
                    "review_run_id": "RR-20260713T000000Z-1234abcd",
                    "state_dir": "docs/ai/state/main",
                    "policy": "main",
                    "gate_status": "pass",
                    "active_blocking": [],
                    "depth_profile": "deep",
                    "amendment_ready": True,
                    "findings": {"index": []},
                    "artifacts": [{"path": "findings-final.json"}],
                },
            ),
        ]
        for command, payload in samples:
            with self.subTest(command=command):
                self.assertJsonContract(command, payload)

    def test_aggregate_json_contract_matches_runtime_payloads(self) -> None:
        current = current_state_fixture()
        sources = [Source(current, "current", "current")]
        dry_run = aggregate.analyze(sources, current, base_ref="HEAD")
        self.assertEqual(dry_run["status"], "ok")

        # The scratch output must live inside the repo so base_ref="HEAD"
        # resolves, and must carry fixture.txt so the file-receipt
        # precondition hash check sees the same target as the fixture root.
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            output = Path(tmp)
            shutil.copyfile(current / "fixture.txt", output / "fixture.txt")

            # Collide against a twin of the *current* root rather than a
            # historical one. Pairing with an older-major fixture also reports
            # "collision", but as malformed_ledger -- the assertion would keep
            # passing while the step/file overlap it exists to prove went
            # unexercised. The rows must differ: identical rows deduplicate.
            twin = output / "twin"
            shutil.copytree(current, twin)
            _perturb_rows(twin)
            collision_sources = [*sources, Source(twin, "twin", "twin")]
            dry_collision = aggregate.analyze(collision_sources, current, base_ref="HEAD")
            self.assertEqual(dry_collision["status"], "collision")
            kinds = {c["kind"] for c in dry_collision["collisions"]}
            self.assertIn("step_overlap", kinds)
            self.assertIn("file_overlap", kinds)
            self.assertNotIn("malformed_ledger", kinds)

            written = aggregate.write_canonical(sources, output, base_ref="HEAD")
            # Aim the collision write at the scratch dir, not the committed
            # fixture tree: if collision detection ever regressed to "ok",
            # this call would replace its target's .agent/.
            write_collision = aggregate.write_canonical(
                collision_sources, output, base_ref="HEAD"
            )
        self.assertEqual(written["status"], "ok")
        self.assertEqual(write_collision, dry_collision)

        for payload in (dry_run, dry_collision, write_collision, written):
            with self.subTest(status=payload["status"], keys=sorted(payload)):
                self.assertJsonContract("aggregate-ledgers", payload)

    def test_aggregate_refuses_a_root_written_by_another_major(self) -> None:
        # docs/compatibility.md gives aggregation no cross-major promise. This
        # only has a subject once a historical fixture is genuinely a major
        # behind; while every fixture shares the current major there is nothing
        # to reject, so it activates on its own at the transition.
        current = current_state_fixture()
        foreign = [
            root
            for root in sorted(p for p in COMPATIBILITY.iterdir() if p.is_dir())
            if root != current and (root / ".agent/step-runs.jsonl").exists()
        ]
        older_major = []
        for root in foreign:
            try:
                read_jsonl(root / ".agent/step-runs.jsonl")
            except (ValueError, OSError):
                older_major.append(root)
        if not older_major:
            self.skipTest("no compatibility fixture is a major behind the live schemas")
        report = aggregate.analyze(
            [Source(current, "current", "current"), Source(older_major[0], "old", "old")],
            current,
            base_ref="HEAD",
        )
        self.assertEqual(report["status"], "collision")
        self.assertIn("malformed_ledger", {c["kind"] for c in report["collisions"]})

    def test_next_action_contract_documents_resumability_shape(self) -> None:
        resumability = JSON_OUTPUTS["next-action"][0]["keys"]["resumability"]
        self.assertEqual(resumability["type"], "object|null")
        self.assertEqual(
            set(resumability["keys"]),
            {
                "contract",
                "agent_id",
                "step",
                "attempt",
                "lease",
                "receipts",
                "gates",
                "recovery_actions",
                "diagnostics",
            },
        )
        recovery = resumability["keys"]["recovery_actions"]["items"]
        self.assertEqual(
            set(recovery["keys"]),
            {"action", "allowed", "automatic", "break_glass", "reason"},
        )

    def test_next_step_contract_documents_optional_design_decision_ids(self) -> None:
        step = JSON_OUTPUTS["next-step"][0]

        self.assertEqual(
            step["keys"]["design_decision_ids"],
            "array|null",
        )
        self.assertJsonContract(
            "next-step",
            {
                "id": "P1",
                "action": "Implement.",
                "files": ["src/x.py"],
                "preconditions": [],
                "validation": ["python3 -m unittest"],
                "expected_diff": ["Feature exists."],
                "evidence_ids": [],
            },
        )

    def test_cli_contract_manifest_matches_parser_and_covers_json_modes(self) -> None:
        manifest = json.loads((ROOT / "docs/cli-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest,
            build_cli_contract(),
            "docs/cli-contract.json is stale; regenerate with: "
            "python3 scripts/gen_cli_contract.py",
        )

        pack_path = next(
            argument
            for argument in manifest["commands"]["pack inspect"]
            if argument["names"] == ["path"]
        )
        optional_plan = next(
            argument
            for argument in manifest["commands"]["lock-plan"]
            if argument["names"] == ["plan"]
        )
        remainder = next(
            argument
            for argument in manifest["commands"]["run"]
            if argument["names"] == ["command"]
        )
        self.assertTrue(pack_path["required"])
        self.assertFalse(optional_plan["required"])
        self.assertFalse(remainder["required"])

        json_commands = {
            command
            for command, args in manifest["commands"].items()
            if any("--json" in arg["names"] or "--jsonl" in arg["names"] for arg in args)
        }
        self.assertEqual(set(manifest["json_outputs"]), json_commands)
        for command, variants in manifest["json_outputs"].items():
            with self.subTest(command=command):
                self.assertTrue(variants)
                for variant in variants:
                    self.assertIn(variant["type"], {"array", "object", "null"})
                    if variant["type"] == "object":
                        self.assertTrue(variant["keys"])

    def test_public_surface_and_deprecation_contract_is_explicit(self) -> None:
        policy = (ROOT / "docs/stability.md").read_text(encoding="utf-8")

        for surface in (
            "CLI commands and flags",
            "exit codes",
            "JSON output",
            "MCP tools",
            "AGENTFLOW_",
            "Python internals",
            "agentflow-proof",
            "agentflow-mcp",
        ):
            self.assertIn(surface, policy)
        self.assertIn("at least one minor release and 90 days", policy)
        self.assertIn("`deprecations` array", policy)

    def test_historical_promise_is_verify_proof_only_and_bounded(self) -> None:
        policy = (ROOT / "docs/compatibility.md").read_text(encoding="utf-8")

        self.assertIn("`verify-proof`", policy)
        self.assertIn("proofs built by Agentflow 0.4.0 or later", policy)
        self.assertIn("Agentflow 2.0 may drop pre-1.0 proofs", policy)
        self.assertIn("newer schema", policy)
        self.assertIn("tell the user to **upgrade**", policy)
        self.assertIn("proof-pack **schema** version", policy)
        self.assertIn("build-proof before a major upgrade", policy)
        for excluded in (
            "`verify-run`",
            "plan loading",
            "receipt loading",
            "aggregation",
            "mutation commands",
        ):
            self.assertIn(excluded, policy)

    def test_audit_records_inventory_defect_and_mechanical_soak_reset(self) -> None:
        audit = (ROOT / "docs/schema-freeze-audit.md").read_text(encoding="utf-8")

        for schema in (
            "PLAN_SCHEMA_VERSION",
            "EXECUTION_CONTRACT_SCHEMA_VERSION",
            "PROOF_PACK_SCHEMA_VERSION",
            "STEP_RUNS_SCHEMA_VERSION",
            "COMMAND_RECEIPTS_SCHEMA_VERSION",
            "FILE_RECEIPTS_SCHEMA_VERSION",
            "VERIFICATION_RUNS_SCHEMA_VERSION",
            "DRIFT_REPORT_SCHEMA_VERSION",
        ):
            self.assertIn(schema, audit)
        self.assertIn("_AGGREGATION_SCHEMA_VERSION_RE", audit)
        self.assertIn("candidate commit", audit)
        self.assertIn("freeze set", audit)
        # Pin the reset rule's substance, not its phrasing: the clock is derived
        # from Git rather than a declared start date, and an earlier verbatim
        # assertion here forced the audit to quote wording it had outgrown.
        for phrase in ("reset the candidate commit", "soak clock"):
            self.assertIn(phrase, audit)

    def test_audit_documents_the_post_soak_version_only_carve_out(self) -> None:
        audit = (ROOT / "docs/schema-freeze-audit.md").read_text(encoding="utf-8")
        # Collapse wrapping so the assertions pin content, not line breaks.
        flowed = " ".join(audit.split())

        self.assertIn("version-only change", flowed)
        self.assertIn("Once the soak window has elapsed", flowed)
        self.assertIn("strict increase", flowed)


if __name__ == "__main__":
    unittest.main()
