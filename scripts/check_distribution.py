#!/usr/bin/env python3
"""Inspect Agentflow wheel and sdist contents before release handoff."""

from __future__ import annotations

import argparse
import configparser
import re
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_SCRIPTS = {
    "agentflow": "agentflow.cli:main",
    "agentflow-mcp": "agentflow.mcp_server:main",
}
SDIST_DOCUMENTS = {"CHANGELOG.md", "LICENSE", "README.md", "pyproject.toml"}


class DistributionCheckError(ValueError):
    """A built wheel or sdist violates the packaging contract."""


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _project_contract(
    root: Path,
) -> tuple[str, str, set[str], dict[str, object]]:
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DistributionCheckError(f"cannot read {path}: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise DistributionCheckError("pyproject.toml has no [project] table")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not name:
        raise DistributionCheckError("project.name must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise DistributionCheckError("project.version must be a non-empty string")
    if project.get("dependencies") != []:
        raise DistributionCheckError("runtime dependencies must remain empty")
    if project.get("scripts") != EXPECTED_SCRIPTS:
        raise DistributionCheckError(
            f"project.scripts must equal {EXPECTED_SCRIPTS!r}"
        )
    packaging_config = {
        key: data.get(key) for key in ("build-system", "project", "tool")
    }
    package_root = root / "src" / "agentflow"
    sources = {
        PurePosixPath("agentflow", *path.relative_to(package_root).parts).as_posix()
        for path in package_root.rglob("*.py")
    }
    if not sources:
        raise DistributionCheckError(f"no Python sources found under {package_root}")
    return name, version, sources, packaging_config


def _sole_artifact(dist_dir: Path, pattern: str, label: str) -> Path:
    paths = sorted(dist_dir.glob(pattern))
    if len(paths) != 1:
        raise DistributionCheckError(
            f"expected exactly one {label} in {dist_dir}; found {len(paths)}"
        )
    return paths[0]


def _metadata(text: str, label: str, name: str, version: str) -> None:
    message = Parser().parsestr(text)
    actual = (message.get("Name"), message.get("Version"))
    expected = (name, version)
    if actual != expected:
        raise DistributionCheckError(
            f"{label} metadata expected Name/Version {expected!r}; found {actual!r}"
        )
    if message.get_all("Requires-Dist"):
        raise DistributionCheckError(f"{label} metadata declares runtime dependencies")


def _entry_points(text: str) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise DistributionCheckError(f"invalid wheel entry_points.txt: {exc}") from exc
    if not parser.has_section("console_scripts"):
        return {}
    return dict(parser.items("console_scripts"))


def _safe_archive_names(names: Sequence[str], label: str) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise DistributionCheckError(f"unsafe {label} path: {name}")


def _inspect_wheel(
    path: Path, name: str, version: str, sources: set[str]
) -> None:
    normalized = _normalize(name)
    expected_filename = f"{normalized}-{version}-py3-none-any.whl"
    if path.name != expected_filename:
        raise DistributionCheckError(
            f"wheel filename must be {expected_filename}; found {path.name}"
        )
    dist_info = f"{normalized}-{version}.dist-info"
    metadata_path = f"{dist_info}/METADATA"
    entries_path = f"{dist_info}/entry_points.txt"
    license_path = f"{dist_info}/licenses/LICENSE"
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _safe_archive_names(names, "wheel")
        if len(names) != len(set(names)):
            raise DistributionCheckError("wheel contains duplicate paths")
        for required in sources | {
            metadata_path,
            entries_path,
            f"{dist_info}/WHEEL",
            license_path,
        }:
            if required not in names:
                raise DistributionCheckError(f"wheel is missing {required}")
        for member in names:
            parts = PurePosixPath(member).parts
            if "__pycache__" in parts or member.endswith((".pyc", ".pyo")):
                raise DistributionCheckError(f"unexpected wheel path: {member}")
            if member.startswith("agentflow/"):
                if member.endswith("/"):
                    if not any(source.startswith(member) for source in sources):
                        raise DistributionCheckError(
                            f"unexpected wheel path: {member}"
                        )
                elif member not in sources:
                    raise DistributionCheckError(f"unexpected wheel path: {member}")
            elif not member.startswith(f"{dist_info}/"):
                raise DistributionCheckError(f"unexpected wheel path: {member}")
        _metadata(
            archive.read(metadata_path).decode("utf-8"),
            "wheel",
            name,
            version,
        )
        scripts = _entry_points(archive.read(entries_path).decode("utf-8"))
        if scripts != EXPECTED_SCRIPTS:
            raise DistributionCheckError(
                f"wheel console entry points expected {EXPECTED_SCRIPTS!r}; "
                f"found {scripts!r}"
            )


def _inspect_sdist(
    path: Path,
    name: str,
    version: str,
    sources: set[str],
    packaging_config: dict[str, object],
) -> None:
    normalized = _normalize(name)
    expected_filename = f"{normalized}-{version}.tar.gz"
    if path.name != expected_filename:
        raise DistributionCheckError(
            f"sdist filename must be {expected_filename}; found {path.name}"
        )
    prefix = f"{normalized}-{version}"
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        _safe_archive_names(names, "sdist")
        if len(names) != len(set(names)):
            raise DistributionCheckError("sdist contains duplicate paths")
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise DistributionCheckError(
                    f"unsupported sdist member: {member.name}"
                )
        if any(
            member != prefix and not member.startswith(f"{prefix}/")
            for member in names
        ):
            raise DistributionCheckError(f"sdist paths must be rooted at {prefix}/")
        files = {member.name: member for member in members if member.isfile()}
        required = {f"{prefix}/{document}" for document in SDIST_DOCUMENTS}
        required.update(
            f"{prefix}/src/{source}" for source in sources
        )
        metadata_path = f"{prefix}/PKG-INFO"
        required.add(metadata_path)
        missing = sorted(required - files.keys())
        if missing:
            raise DistributionCheckError(f"sdist is missing {missing[0]}")
        metadata_member = archive.extractfile(files[metadata_path])
        if metadata_member is None:
            raise DistributionCheckError(f"cannot read {metadata_path}")
        _metadata(
            metadata_member.read().decode("utf-8"),
            "sdist",
            name,
            version,
        )
        pyproject_path = f"{prefix}/pyproject.toml"
        pyproject_member = archive.extractfile(files[pyproject_path])
        if pyproject_member is None:
            raise DistributionCheckError(f"cannot read {pyproject_path}")
        try:
            archived_config = tomllib.loads(
                pyproject_member.read().decode("utf-8")
            )
        except tomllib.TOMLDecodeError as exc:
            raise DistributionCheckError(
                f"invalid sdist pyproject.toml: {exc}"
            ) from exc
        archived_packaging_config = {
            key: archived_config.get(key)
            for key in ("build-system", "project", "tool")
        }
        if archived_packaging_config != packaging_config:
            raise DistributionCheckError(
                "sdist pyproject.toml does not match the source packaging contract"
            )


def inspect_distribution(root: Path, dist_dir: Path) -> tuple[Path, Path]:
    """Validate and return the sole wheel and sdist in ``dist_dir``."""
    name, version, sources, packaging_config = _project_contract(root)
    wheel = _sole_artifact(dist_dir, "*.whl", "wheel")
    sdist = _sole_artifact(dist_dir, "*.tar.gz", "sdist")
    _inspect_wheel(wheel, name, version, sources)
    _inspect_sdist(sdist, name, version, sources, packaging_config)
    return wheel, sdist


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--dist-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    dist_dir = args.dist_dir or args.root / "dist"
    try:
        wheel, sdist = inspect_distribution(args.root, dist_dir)
    except (
        DistributionCheckError,
        OSError,
        UnicodeDecodeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        sys.stderr.write(f"distribution check failed: {exc}\n")
        return 1
    print(f"distribution artifacts passed: {wheel.name}, {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
