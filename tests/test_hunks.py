from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agentflow.hunks import compute_hunks, hunk_identity
from agentflow.execution import init_execution_artifacts
from agentflow.hunks import effective_hunk_policy, unmapped_hunks
from agentflow.artifacts import append_jsonl


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Tests", "-c", "user.email=t@example.com", *args],
        cwd=str(root),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _seed(tmp: str, name: str, content: str) -> Path:
    root = Path(tmp)
    _git(root, "init")
    (root / name).write_text(content, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-m", "seed")
    return root


class HunkIdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_span_independent(self) -> None:
        a = hunk_identity("a.py", "modified", ["+new line"])
        b = hunk_identity("a.py", "modified", ["+new line"])
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_identity_changes_with_content(self) -> None:
        self.assertNotEqual(
            hunk_identity("a.py", "modified", ["+x"]),
            hunk_identity("a.py", "modified", ["+y"]),
        )


class ComputeHunksModifiedTests(unittest.TestCase):
    def test_modified_file_yields_one_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "f.py", "line1\nline2\nline3\n")
            (root / "f.py").write_text("line1\nCHANGED\nline3\n", encoding="utf-8")
            attribution, hunks = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            self.assertEqual(attribution, "hunked")
            self.assertEqual(len(hunks), 1)
            self.assertEqual(len(hunks[0]["hash"]), 64)
            self.assertEqual(hunks[0]["new_start"], 2)

    def test_two_separate_edits_yield_two_hunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
            root = _seed(tmp, "f.py", seed)
            lines = seed.splitlines()
            lines[1] = "EDIT_TOP"
            lines[18] = "EDIT_BOTTOM"
            (root / "f.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
            _, hunks = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            self.assertEqual(len(hunks), 2)

    def test_span_drift_does_not_change_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
            root = _seed(tmp, "f.py", seed)
            lines = seed.splitlines()
            lines[18] = "BOTTOM_EDIT"
            (root / "f.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
            _, before = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            # Insert an unrelated line ABOVE the edit, shifting its line numbers.
            lines2 = ["INSERTED_AT_TOP", *lines]
            (root / "f.py").write_text("\n".join(lines2) + "\n", encoding="utf-8")
            _, after = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            after_hashes = {h["hash"] for h in after}
            self.assertIn(before[0]["hash"], after_hashes)

    def test_content_lines_that_look_like_diff_headers_affect_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "f.py", "old\n")
            (root / "f.py").write_text("++A\n", encoding="utf-8")
            _, first = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            (root / "f.py").write_text("++B\n", encoding="utf-8")
            _, second = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            self.assertNotEqual(first[0]["hash"], second[0]["hash"])

            (root / "f.py").write_text("--A\n", encoding="utf-8")
            _, third = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            (root / "f.py").write_text("--B\n", encoding="utf-8")
            _, fourth = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            self.assertNotEqual(third[0]["hash"], fourth[0]["hash"])


class ComputeHunksChangeKindTests(unittest.TestCase):
    def test_untracked_added_file_yields_one_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "seed.txt", "seed\n")
            (root / "new.py").write_text("alpha\nbeta\n", encoding="utf-8")  # untracked ??
            attribution, hunks = compute_hunks(
                root, {"path": "new.py", "previous_path": "", "change_kind": "added"}
            )
            self.assertEqual(attribution, "hunked")
            self.assertEqual(len(hunks), 1)
            self.assertEqual(hunks[0]["new_count"], 2)

    def test_deleted_file_yields_removal_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "gone.py", "one\ntwo\n")
            (root / "gone.py").unlink()
            attribution, hunks = compute_hunks(
                root, {"path": "gone.py", "previous_path": "", "change_kind": "deleted"}
            )
            self.assertEqual(attribution, "hunked")
            self.assertEqual(len(hunks), 1)

    def test_renamed_file_diffs_against_previous_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "old name.py", "a\nb\nc\n")  # space in name
            (root / "old name.py").unlink()
            (root / "new.py").write_text("a\nB\nc\n", encoding="utf-8")
            attribution, hunks = compute_hunks(
                root, {"path": "new.py", "previous_path": "old name.py", "change_kind": "renamed"}
            )
            self.assertEqual(attribution, "hunked")
            self.assertEqual(len(hunks), 1)

    def test_rename_with_missing_previous_path_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "seed.txt", "seed\n")
            (root / "new.py").write_text("x\n", encoding="utf-8")
            attribution, hunks = compute_hunks(
                root, {"path": "new.py", "previous_path": "", "change_kind": "renamed"}
            )
            self.assertEqual(attribution, "whole_file_fallback")
            self.assertEqual(hunks, [])

    def test_binary_file_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "img.bin", "placeholder\n")
            (root / "img.bin").write_bytes(b"\x00\x01\x02\x03\xff\xfe")
            attribution, hunks = compute_hunks(
                root, {"path": "img.bin", "previous_path": "", "change_kind": "modified"}
            )
            self.assertEqual(attribution, "whole_file_fallback")
            self.assertEqual(hunks, [])

    def test_no_raw_text_in_hunk_dicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "f.py", "secret_seed\n")
            (root / "f.py").write_text("SUPER_SECRET_TOKEN\n", encoding="utf-8")
            _, hunks = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            blob = repr(hunks)
            self.assertNotIn("SUPER_SECRET_TOKEN", blob)
            self.assertNotIn("secret_seed", blob)


class PolicyAndUnmappedTests(unittest.TestCase):
    def test_policy_off_without_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "f.py", "x\n")
            self.assertEqual(effective_hunk_policy(root), "off")

    def test_policy_defaults_enforce_with_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "f.py", "x\n")
            init_execution_artifacts(root)
            self.assertEqual(effective_hunk_policy(root), "enforce")

    def test_policy_enforce_on_malformed_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "f.py", "x\n")
            init_execution_artifacts(root)
            (root / ".agent/execution.contract.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(effective_hunk_policy(root), "enforce")

    def test_unmapped_flags_stray_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
            root = _seed(tmp, "f.py", seed)
            init_execution_artifacts(root)
            # Record only the TOP edit as a hunked receipt.
            lines = seed.splitlines()
            lines[1] = "RECORDED_EDIT"
            (root / "f.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
            _, recorded = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            append_jsonl(
                root / ".agent/file-receipts.jsonl",
                {"id": "FR1", "path": "f.py", "change_kind": "modified",
                 "hunk_attribution": "hunked", "hunks": recorded},
            )
            # Now ALSO make a stray edit at the bottom, never recorded.
            lines[18] = "STRAY_EDIT"
            (root / "f.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = unmapped_hunks(
                root, [{"path": "f.py", "previous_path": "", "change_kind": "modified"}]
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["path"], "f.py")
            self.assertEqual(result[0]["reason"], "no_matching_hunk")

    def test_legacy_receipt_without_hunks_is_whole_file_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed(tmp, "f.py", "a\nb\nc\n")
            init_execution_artifacts(root)
            (root / "f.py").write_text("a\nZ\nc\n", encoding="utf-8")
            append_jsonl(  # legacy 0.3.0 shape: no "hunks" key
                root / ".agent/file-receipts.jsonl",
                {"id": "FR1", "path": "f.py", "change_kind": "modified"},
            )
            self.assertEqual(
                unmapped_hunks(root, [{"path": "f.py", "previous_path": "", "change_kind": "modified"}]),
                [],
            )

    def test_hunked_receipt_after_legacy_reenables_hunk_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
            root = _seed(tmp, "f.py", seed)
            init_execution_artifacts(root)
            lines = seed.splitlines()
            lines[1] = "LEGACY_EDIT"
            (root / "f.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
            _, recorded = compute_hunks(
                root, {"path": "f.py", "previous_path": "", "change_kind": "modified"}
            )
            append_jsonl(
                root / ".agent/file-receipts.jsonl",
                {"id": "FR1", "path": "f.py", "change_kind": "modified"},
            )
            append_jsonl(
                root / ".agent/file-receipts.jsonl",
                {
                    "id": "FR2",
                    "path": "f.py",
                    "change_kind": "modified",
                    "hunk_attribution": "hunked",
                    "hunks": [recorded[0]],
                },
            )
            lines[18] = "CURRENT_STRAY"
            (root / "f.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = unmapped_hunks(
                root, [{"path": "f.py", "previous_path": "", "change_kind": "modified"}]
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["reason"], "no_matching_hunk")
