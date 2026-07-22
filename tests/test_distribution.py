"""Tests for Python distribution metadata and built artifact inspection."""

from __future__ import annotations

import base64
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import tracemalloc
import unittest
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path

from scripts import check_distribution as distribution_check


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_distribution.py"
VERSION = "0.4.0"
DIST_NAME = "agentflow-proof"
NORMALIZED_NAME = "agentflow_proof"
PACKAGE_FILES = ("__init__.py", "cli.py", "mcp_server.py")
REQUIRES_PYTHON = ">=3.11"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


class DistributionInspectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        package = self.root / "src" / "agentflow"
        package.mkdir(parents=True)
        for name in PACKAGE_FILES:
            (package / name).write_text(f"# {name}\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[build-system]",
                    'requires = ["setuptools==83.0.0"]',
                    'build-backend = "setuptools.build_meta"',
                    "",
                    "[project]",
                    f'name = "{DIST_NAME}"',
                    f'version = "{VERSION}"',
                    f'requires-python = "{REQUIRES_PYTHON}"',
                    'dependencies = []',
                    "",
                    "[project.scripts]",
                    'agentflow = "agentflow.cli:main"',
                    'agentflow-mcp = "agentflow.mcp_server:main"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.write_wheel()
        self.write_sdist()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @property
    def wheel_path(self) -> Path:
        return self.dist / f"{NORMALIZED_NAME}-{VERSION}-py3-none-any.whl"

    @property
    def sdist_path(self) -> Path:
        return self.dist / f"{NORMALIZED_NAME}-{VERSION}.tar.gz"

    def run_inspector(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(self.root),
                "--dist-dir",
                str(self.dist),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def write_wheel(
        self,
        filename: str | None = None,
        *,
        metadata_name: str = DIST_NAME,
        metadata_version: str = VERSION,
        metadata_requires_python: str | None = REQUIRES_PYTHON,
        entry_points: dict[str, str] | None = None,
        omit: str | None = None,
        extra: dict[str, bytes] | None = None,
        compression: int = zipfile.ZIP_STORED,
    ) -> Path:
        path = self.dist / (
            filename or f"{NORMALIZED_NAME}-{VERSION}-py3-none-any.whl"
        )
        dist_info = f"{NORMALIZED_NAME}-{VERSION}.dist-info"
        entries = {
            f"agentflow/{name}": f"# {name}\n".encode()
            for name in PACKAGE_FILES
        }
        entries.update(
            {
                f"{dist_info}/METADATA": (
                    "Metadata-Version: 2.4\n"
                    f"Name: {metadata_name}\n"
                    f"Version: {metadata_version}\n"
                    + (
                        f"Requires-Python: {metadata_requires_python}\n"
                        if metadata_requires_python is not None
                        else ""
                    )
                    + "\n"
                ).encode(),
                f"{dist_info}/WHEEL": (
                    "Wheel-Version: 1.0\n"
                    "Generator: test\n"
                    "Root-Is-Purelib: true\n"
                    "Tag: py3-none-any\n"
                ).encode(),
                f"{dist_info}/entry_points.txt": self._entry_points(
                    entry_points
                ).encode(),
                f"{dist_info}/licenses/LICENSE": b"MIT\n",
            }
        )
        if extra:
            entries.update(extra)
        record_path = f"{dist_info}/RECORD"
        record_lines = []
        for name, body in sorted(entries.items()):
            digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest())
            digest = digest.rstrip(b"=").decode("ascii")
            record_lines.append(f"{name},sha256={digest},{len(body)}")
        record_lines.append(f"{record_path},,")
        entries[record_path] = ("\n".join(record_lines) + "\n").encode()
        if omit is not None:
            entries.pop(omit)
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, body in entries.items():
                archive.writestr(name, body)
        return path

    def write_sdist(
        self,
        *,
        omit: str | None = None,
        metadata_requires_python: str | None = REQUIRES_PYTHON,
        pyproject: bytes | None = None,
        setup_cfg: bytes | None = None,
        extra_member: tarfile.TarInfo | None = None,
        extra_members: Sequence[tarfile.TarInfo] = (),
    ) -> Path:
        prefix = f"{NORMALIZED_NAME}-{VERSION}"
        entries = {
            "PKG-INFO": (
                "Metadata-Version: 2.4\n"
                f"Name: {DIST_NAME}\n"
                f"Version: {VERSION}\n"
                + (
                    f"Requires-Python: {metadata_requires_python}\n"
                    if metadata_requires_python is not None
                    else ""
                )
                + "\n"
            ).encode(),
            "pyproject.toml": (
                pyproject
                if pyproject is not None
                else (self.root / "pyproject.toml").read_bytes()
            ),
            "README.md": b"# Agentflow\n",
            "LICENSE": b"MIT\n",
            "CHANGELOG.md": b"# Changelog\n",
        }
        entries.update(
            {
                f"src/agentflow/{name}": f"# {name}\n".encode()
                for name in PACKAGE_FILES
            }
        )
        if setup_cfg is not None:
            entries["setup.cfg"] = setup_cfg
        if omit is not None:
            entries.pop(omit)
        with tarfile.open(self.sdist_path, "w:gz") as archive:
            for name, body in entries.items():
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
            if extra_member is not None:
                archive.addfile(extra_member)
            for member in extra_members:
                archive.addfile(member)
        return self.sdist_path

    @staticmethod
    def _entry_points(values: dict[str, str] | None) -> str:
        scripts = values or {
            "agentflow": "agentflow.cli:main",
            "agentflow-mcp": "agentflow.mcp_server:main",
        }
        lines = ["[console_scripts]"]
        lines.extend(f"{name} = {target}" for name, target in scripts.items())
        return "\n".join(lines) + "\n"

    def test_valid_wheel_and_sdist_pass(self) -> None:
        result = self.run_inspector()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("distribution artifacts passed", result.stdout)

    def test_requires_exactly_one_wheel_and_sdist(self) -> None:
        self.write_wheel("extra-0.4.0-py3-none-any.whl")

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("expected exactly one wheel", result.stderr)

    def test_rejects_wrong_metadata_name_or_version(self) -> None:
        for field, value in (("name", "agentflow"), ("version", "9.9.9")):
            with self.subTest(field=field):
                self.write_wheel(
                    metadata_name=value if field == "name" else DIST_NAME,
                    metadata_version=value if field == "version" else VERSION,
                )

                result = self.run_inspector()

                self.assertEqual(result.returncode, 1)
                self.assertIn("wheel metadata", result.stderr)

    def test_rejects_changed_console_entry_points(self) -> None:
        self.write_wheel(entry_points={"agentflow": "other:main"})

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("console entry points", result.stderr)

    def test_rejects_missing_package_source(self) -> None:
        self.write_wheel(omit="agentflow/cli.py")

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("agentflow/cli.py", result.stderr)

    def test_rejects_missing_wheel_record(self) -> None:
        dist_info = f"{NORMALIZED_NAME}-{VERSION}.dist-info"
        self.write_wheel(omit=f"{dist_info}/RECORD")

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("RECORD", result.stderr)

    def test_rejects_wrong_or_missing_requires_python_metadata(self) -> None:
        for artifact in ("wheel", "sdist"):
            for value in (">=3.12", None):
                with self.subTest(artifact=artifact, value=value):
                    self.write_wheel()
                    self.write_sdist()
                    if artifact == "wheel":
                        self.write_wheel(metadata_requires_python=value)
                    else:
                        self.write_sdist(metadata_requires_python=value)

                    result = self.run_inspector()

                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        f"{artifact} metadata Requires-Python", result.stderr
                    )

    def test_rejects_wheel_repository_leakage(self) -> None:
        self.write_wheel(extra={"tests/test_cli.py": b"pass\n"})

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected wheel path", result.stderr)

    def test_rejects_nested_wheel_tests_and_directory_entries(self) -> None:
        for member in ("agentflow/tests/test_cli.py", "agentflow/tests/"):
            with self.subTest(member=member):
                self.write_wheel(extra={member: b"pass\n"})

                result = self.run_inspector()

                self.assertEqual(result.returncode, 1)
                self.assertIn("unexpected wheel path", result.stderr)

    def test_rejects_missing_sdist_document_or_source(self) -> None:
        for missing in ("CHANGELOG.md", "src/agentflow/cli.py"):
            with self.subTest(missing=missing):
                self.write_sdist(omit=missing)

                result = self.run_inspector()

                self.assertEqual(result.returncode, 1)
                self.assertIn(missing, result.stderr)

    def test_rejects_changed_sdist_pyproject(self) -> None:
        pyproject = (self.root / "pyproject.toml").read_bytes().replace(
            b"agentflow.cli:main", b"other:main"
        )
        self.write_sdist(pyproject=pyproject)

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("sdist pyproject.toml", result.stderr)

    def test_rejects_sdist_links_and_unsafe_non_files(self) -> None:
        prefix = f"{NORMALIZED_NAME}-{VERSION}"
        link = tarfile.TarInfo(f"{prefix}/linked")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        unsafe_directory = tarfile.TarInfo(f"{prefix}/../escape")
        unsafe_directory.type = tarfile.DIRTYPE
        for member, message in (
            (link, "unsupported sdist member"),
            (unsafe_directory, "unsafe sdist path"),
        ):
            with self.subTest(member=member.name):
                self.write_sdist(extra_member=member)

                result = self.run_inspector()

                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_rejects_noncanonical_sdist_path_alias(self) -> None:
        prefix = f"{NORMALIZED_NAME}-{VERSION}"
        alias = tarfile.TarInfo(f"{prefix}/./pyproject.toml")

        self.write_sdist(extra_member=alias)
        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("non-canonical sdist path", result.stderr)

    def test_rejects_case_colliding_sdist_paths(self) -> None:
        prefix = f"{NORMALIZED_NAME}-{VERSION}"
        collision = tarfile.TarInfo(f"{prefix}/readme.md")

        self.write_sdist(extra_member=collision)
        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate or aliased paths", result.stderr)

    def test_rejects_unexpected_sdist_build_or_private_files(self) -> None:
        prefix = f"{NORMALIZED_NAME}-{VERSION}"
        for relative in ("setup.py", ".agent/private.txt"):
            with self.subTest(relative=relative):
                member = tarfile.TarInfo(f"{prefix}/{relative}")
                self.write_sdist(extra_member=member)

                result = self.run_inspector()

                self.assertEqual(result.returncode, 1)
                self.assertIn("unexpected sdist path", result.stderr)

    def test_rejects_excessive_sdist_member_count(self) -> None:
        prefix = f"{NORMALIZED_NAME}-{VERSION}"
        members = [
            tarfile.TarInfo(f"{prefix}/extra-{index}.txt")
            for index in range(1_025)
        ]

        self.write_sdist(extra_members=members)
        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("too many sdist members", result.stderr)

    def test_rejects_expansive_pax_header_before_member_parsing(self) -> None:
        prefix = f"{NORMALIZED_NAME}-{VERSION}"
        member = tarfile.TarInfo(f"{prefix}/pax-data.txt")
        member.pax_headers = {
            "comment": "x" * (MAX_ARCHIVE_BYTES + 1024)
        }

        self.write_sdist(extra_member=member)
        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("sdist archive expands beyond", result.stderr)

    def test_rejects_unsafe_generated_setup_cfg(self) -> None:
        self.write_sdist(setup_cfg=b"[options]\npy_modules = payload\n")

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected build configuration", result.stderr)

    def test_rejects_archive_file_over_size_limit(self) -> None:
        self.wheel_path.write_bytes(b"x" * (MAX_ARCHIVE_BYTES + 1))

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("wheel archive is too large", result.stderr)

    def test_rejects_excessive_wheel_uncompressed_size(self) -> None:
        dist_info = f"{NORMALIZED_NAME}-{VERSION}.dist-info"
        self.write_wheel(
            extra={
                f"{dist_info}/padding.bin": (
                    b"x" * (MAX_UNCOMPRESSED_BYTES + 1)
                )
            },
            compression=zipfile.ZIP_DEFLATED,
        )

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("wheel members are too large when uncompressed", result.stderr)

    def test_rejects_oversized_inspected_metadata(self) -> None:
        dist_info = f"{NORMALIZED_NAME}-{VERSION}.dist-info"
        metadata = (
            "Metadata-Version: 2.4\n"
            f"Name: {DIST_NAME}\n"
            f"Version: {VERSION}\n"
            f"Requires-Python: {REQUIRES_PYTHON}\n\n"
        ).encode() + (b"x" * (1024 * 1024))
        self.write_wheel(extra={f"{dist_info}/METADATA": metadata})

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("wheel member is too large to inspect", result.stderr)

    def test_bounded_zip_read_does_not_trust_declared_size(self) -> None:
        dist_info = f"{NORMALIZED_NAME}-{VERSION}.dist-info"
        metadata_path = f"{dist_info}/METADATA"
        self.write_wheel(
            extra={metadata_path: b"M" + (b"x" * (8 * 1024 * 1024))},
            compression=zipfile.ZIP_DEFLATED,
        )

        with zipfile.ZipFile(self.wheel_path) as archive:
            info = archive.getinfo(metadata_path)
            info.file_size = 1
            info.CRC = zlib.crc32(b"M")
            tracemalloc.start()
            try:
                text = distribution_check._read_zip_text(
                    archive, metadata_path, "wheel"
                )
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertEqual(text, "M")
        self.assertLess(
            peak, 4 * distribution_check.MAX_INSPECTED_MEMBER_BYTES
        )

    def test_rejects_unsupported_wheel_compression(self) -> None:
        self.write_wheel(compression=zipfile.ZIP_BZIP2)

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported wheel compression", result.stderr)

    def test_truncated_gzip_fails_without_traceback(self) -> None:
        self.sdist_path.write_bytes(b"\x1f\x8b\x08\x00")

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("distribution check failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rejects_non_normalized_artifact_filename(self) -> None:
        self.wheel_path.unlink()
        self.write_wheel("agentflow-proof-0.4.0-py3-none-any.whl")

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("wheel filename", result.stderr)

    def test_corrupt_archive_fails_without_traceback(self) -> None:
        self.wheel_path.write_bytes(b"not a zip")

        result = self.run_inspector()

        self.assertEqual(result.returncode, 1)
        self.assertIn("distribution check failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class RepositoryDistributionMetadataTests(unittest.TestCase):
    def test_repository_declares_fallback_name_and_stable_commands(self) -> None:
        data = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(data["build-system"]["requires"], ["setuptools==83.0.0"])
        self.assertEqual(data["project"]["name"], DIST_NAME)
        self.assertEqual(data["project"]["dependencies"], [])
        self.assertEqual(
            data["project"]["scripts"],
            {
                "agentflow": "agentflow.cli:main",
                "agentflow-mcp": "agentflow.mcp_server:main",
            },
        )


if __name__ == "__main__":
    unittest.main()
