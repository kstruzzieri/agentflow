import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentflow.git import changed_file_records, changed_files


class GitStatusTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def test_special_filenames_survive_status_parsing(self):
        paths = ["two words.py", "café.py", "literal -> name.py", " trailing.py ",
                 'quote".py', "line\r\nbreak.py"]
        for path in paths:
            (self.root / path).write_text("before\n")
        for kind, status in (("added", "??"), ("modified", " M"), ("deleted", " D")):
            with self.subTest(kind=kind):
                self.assertEqual(changed_file_records(self.root), [
                    {"path": path, "previous_path": "", "status": status, "change_kind": kind}
                    for path in sorted(paths)
                ])
                self.assertEqual(changed_files(self.root), sorted(paths))
            if kind == "added":
                self.git("add", ".")
                self.git("commit", "-qm", "baseline")
                self.assertEqual(changed_file_records(self.root), [])
                for path in paths:
                    (self.root / path).write_text("after\n")
            elif kind == "modified":
                for path in paths:
                    (self.root / path).unlink()

    def test_rename_consumes_old_path_without_parsing_it_as_a_record(self):
        before, after = "old café -> name.py", "new café name.py"
        (self.root / before).write_text("content\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.git("mv", before, after)
        (self.root / "untracked.py").write_text("new\n")

        self.assertEqual(changed_file_records(self.root), [
            {"path": after, "previous_path": before, "status": "R ", "change_kind": "renamed"},
            {"path": "untracked.py", "previous_path": "", "status": "??", "change_kind": "added"},
        ])
        self.assertEqual(changed_files(self.root), [after, "untracked.py"])

    def test_git_status_errors_are_not_an_empty_change_list(self):
        (self.root / ".git/index").write_bytes(b"broken index")
        for inspect in (changed_file_records, changed_files):
            with self.subTest(inspect=inspect.__name__), self.assertRaises(RuntimeError):
                inspect(self.root)

    def test_copy_consumes_source_path_and_preserves_following_record(self):
        source, copied = "source café.py", "copy name.py"
        (self.root / source).write_text("original\n" * 20)
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        (self.root / copied).write_text((self.root / source).read_text())
        (self.root / source).write_text("changed\n" * 20)
        self.git("add", ".")
        self.git("config", "status.renames", "copies")

        self.assertEqual(changed_file_records(self.root), [
            {"path": copied, "previous_path": source, "status": "C ", "change_kind": "renamed"},
            {"path": source, "previous_path": "", "status": "M ", "change_kind": "modified"},
        ])
        self.assertEqual(changed_files(self.root), [copied, source])
