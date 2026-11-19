#!/usr/bin/env python3
"""Inspect the Agentflow wheel, sdist and zipapps before release handoff.

Checks names and metadata, then compares every packaged Python file with
the checkout byte for byte. Run it from a fresh checkout of the tagged
commit to catch a file that changed during the build.
"""

from __future__ import annotations

import argparse
import configparser
import gzip
import importlib.util
import io
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
SDIST_EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "top_level.txt",
}
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_TAR_STREAM_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1_024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_INSPECTED_MEMBER_BYTES = 1024 * 1024
SUPPORTED_WHEEL_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
ZIPAPP_INTERPRETER_LINE = b"#!/usr/bin/env python3\n"


class DistributionCheckError(ValueError):
    """A built wheel or sdist violates the packaging contract."""


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _project_contract(
    root: Path,
) -> tuple[str, str, str, set[str], dict[str, object]]:
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
    requires_python = project.get("requires-python")
    if not isinstance(name, str) or not name:
        raise DistributionCheckError("project.name must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise DistributionCheckError("project.version must be a non-empty string")
    if not isinstance(requires_python, str) or not requires_python:
        raise DistributionCheckError(
            "project.requires-python must be a non-empty string"
        )
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
    return name, version, requires_python, sources, packaging_config


def _sole_artifact(dist_dir: Path, pattern: str, label: str) -> Path:
    paths = sorted(dist_dir.glob(pattern))
    if len(paths) != 1:
        raise DistributionCheckError(
            f"expected exactly one {label} in {dist_dir}; found {len(paths)}"
        )
    return paths[0]


def _metadata(
    text: str,
    label: str,
    name: str,
    version: str,
    requires_python: str,
) -> None:
    message = Parser().parsestr(text)
    actual = (message.get("Name"), message.get("Version"))
    expected = (name, version)
    if actual != expected:
        raise DistributionCheckError(
            f"{label} metadata expected Name/Version {expected!r}; found {actual!r}"
        )
    actual_requires_python = message.get("Requires-Python")
    if actual_requires_python != requires_python:
        raise DistributionCheckError(
            f"{label} metadata Requires-Python expected {requires_python!r}; "
            f"found {actual_requires_python!r}"
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
    portable_names: set[str] = set()
    for name in names:
        if "\\" in name:
            raise DistributionCheckError(f"unsafe {label} path: {name}")
        trailing_slash = name.endswith("/")
        raw_path = name[:-1] if trailing_slash else name
        path = PurePosixPath(raw_path)
        if name.startswith("/") or path.is_absolute() or ".." in path.parts:
            raise DistributionCheckError(f"unsafe {label} path: {name}")
        canonical = path.as_posix()
        if raw_path != canonical:
            raise DistributionCheckError(
                f"non-canonical {label} path: {name}"
            )
        portable_name = canonical.casefold()
        if portable_name in portable_names:
            raise DistributionCheckError(
                f"{label} contains duplicate or aliased paths: {name}"
            )
        portable_names.add(portable_name)


def _check_archive_size(path: Path, label: str) -> None:
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise DistributionCheckError(
            f"{label} archive is too large: {size} bytes"
        )


def _check_member_limits(
    sizes: Sequence[int], label: str
) -> None:
    if len(sizes) > MAX_ARCHIVE_MEMBERS:
        raise DistributionCheckError(f"too many {label} members")
    if any(size < 0 for size in sizes):
        raise DistributionCheckError(f"invalid {label} member size")
    total = sum(sizes)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise DistributionCheckError(
            f"{label} members are too large when uncompressed: {total} bytes"
        )


def _open_bounded_tar(path: Path) -> tarfile.TarFile:
    with gzip.open(path, "rb") as compressed:
        body = compressed.read(MAX_TAR_STREAM_BYTES + 1)
    if len(body) > MAX_TAR_STREAM_BYTES:
        raise DistributionCheckError(
            f"sdist archive expands beyond {MAX_TAR_STREAM_BYTES} bytes"
        )
    return tarfile.open(fileobj=io.BytesIO(body), mode="r:")


def _read_zip_bytes(
    archive: zipfile.ZipFile, name: str, label: str
) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_INSPECTED_MEMBER_BYTES:
        raise DistributionCheckError(
            f"{label} member is too large to inspect: {name}"
        )
    with archive.open(info) as member:
        body = member.read(MAX_INSPECTED_MEMBER_BYTES + 1)
    if len(body) > MAX_INSPECTED_MEMBER_BYTES:
        raise DistributionCheckError(
            f"{label} member is too large to inspect: {name}"
        )
    return body


def _read_zip_text(
    archive: zipfile.ZipFile, name: str, label: str
) -> str:
    return _read_zip_bytes(archive, name, label).decode("utf-8")


def _read_tar_bytes(
    archive: tarfile.TarFile, member: tarfile.TarInfo, label: str
) -> bytes:
    if member.size > MAX_INSPECTED_MEMBER_BYTES:
        raise DistributionCheckError(
            f"{label} member is too large to inspect: {member.name}"
        )
    extracted = archive.extractfile(member)
    if extracted is None:
        raise DistributionCheckError(f"cannot read {member.name}")
    body = extracted.read(MAX_INSPECTED_MEMBER_BYTES + 1)
    if len(body) > MAX_INSPECTED_MEMBER_BYTES:
        raise DistributionCheckError(
            f"{label} member is too large to inspect: {member.name}"
        )
    return body


def _read_tar_text(
    archive: tarfile.TarFile, member: tarfile.TarInfo, label: str
) -> str:
    return _read_tar_bytes(archive, member, label).decode("utf-8")


def _require_checkout_bytes(
    packaged: bytes, checkout_file: Path, label: str, member: str
) -> None:
    """Fail unless a packaged file is byte-identical to the checkout's copy."""
    if packaged != checkout_file.read_bytes():
        raise DistributionCheckError(
            f"{label} file differs from the checkout: {member}"
        )


def _allowed_sdist_members(
    root: Path,
    prefix: str,
    normalized: str,
    sources: set[str],
) -> tuple[set[str], set[str]]:
    relative_files = set(SDIST_DOCUMENTS) | {"PKG-INFO", "setup.cfg"}
    relative_files.update(f"src/{source}" for source in sources)
    if (root / "MANIFEST.in").is_file():
        relative_files.add("MANIFEST.in")
    tests_root = root / "tests"
    if tests_root.is_dir():
        relative_files.update(
            PurePosixPath("tests", *path.relative_to(tests_root).parts).as_posix()
            for path in tests_root.rglob("*.py")
        )
    egg_info = f"src/{normalized}.egg-info"
    relative_files.update(
        f"{egg_info}/{filename}" for filename in SDIST_EGG_INFO_FILES
    )
    files = {f"{prefix}/{relative}" for relative in relative_files}
    directories = {prefix}
    for filename in files:
        parent = PurePosixPath(filename).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return files, directories


def _validate_generated_setup_cfg(text: str) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise DistributionCheckError(f"invalid sdist setup.cfg: {exc}") from exc
    expected = {"tag_build": "", "tag_date": "0"}
    if (
        parser.defaults()
        or parser.sections() != ["egg_info"]
        or dict(parser.items("egg_info", raw=True)) != expected
    ):
        raise DistributionCheckError(
            "sdist setup.cfg contains unexpected build configuration"
        )


def _inspect_wheel(
    path: Path,
    root: Path,
    name: str,
    version: str,
    requires_python: str,
    sources: set[str],
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
    record_path = f"{dist_info}/RECORD"
    _check_archive_size(path, "wheel")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        unsupported = next(
            (
                info
                for info in infos
                if info.compress_type not in SUPPORTED_WHEEL_COMPRESSION
            ),
            None,
        )
        if unsupported is not None:
            raise DistributionCheckError(
                "unsupported wheel compression for "
                f"{unsupported.filename}: {unsupported.compress_type}"
            )
        _check_member_limits([info.file_size for info in infos], "wheel")
        names = [info.filename for info in infos]
        _safe_archive_names(names, "wheel")
        for required in sources | {
            metadata_path,
            entries_path,
            f"{dist_info}/WHEEL",
            license_path,
            record_path,
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
            _read_zip_text(archive, metadata_path, "wheel"),
            "wheel",
            name,
            version,
            requires_python,
        )
        scripts = _entry_points(_read_zip_text(archive, entries_path, "wheel"))
        if scripts != EXPECTED_SCRIPTS:
            raise DistributionCheckError(
                f"wheel console entry points expected {EXPECTED_SCRIPTS!r}; "
                f"found {scripts!r}"
            )
        for source in sorted(sources):
            _require_checkout_bytes(
                _read_zip_bytes(archive, source, "wheel"),
                root / "src" / source,
                "wheel",
                source,
            )


def _inspect_sdist(
    path: Path,
    root: Path,
    name: str,
    version: str,
    requires_python: str,
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
    _check_archive_size(path, "sdist")
    with _open_bounded_tar(path) as archive:
        members: list[tarfile.TarInfo] = []
        total_size = 0
        for member in archive:
            members.append(member)
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise DistributionCheckError("too many sdist members")
            if member.size < 0:
                raise DistributionCheckError("invalid sdist member size")
            total_size += member.size
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise DistributionCheckError(
                    "sdist members are too large when uncompressed: "
                    f"{total_size} bytes"
                )
        names = [member.name for member in members]
        _safe_archive_names(names, "sdist")
        allowed_files, allowed_directories = _allowed_sdist_members(
            root, prefix, normalized, sources
        )
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise DistributionCheckError(
                    f"unsupported sdist member: {member.name}"
                )
            allowed = allowed_files if member.isfile() else allowed_directories
            if member.name not in allowed:
                raise DistributionCheckError(
                    f"unexpected sdist path: {member.name}"
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
        _metadata(
            _read_tar_text(archive, files[metadata_path], "sdist"),
            "sdist",
            name,
            version,
            requires_python,
        )
        pyproject_path = f"{prefix}/pyproject.toml"
        try:
            archived_config = tomllib.loads(
                _read_tar_text(archive, files[pyproject_path], "sdist")
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
        setup_cfg_path = f"{prefix}/setup.cfg"
        if setup_cfg_path in files:
            _validate_generated_setup_cfg(
                _read_tar_text(archive, files[setup_cfg_path], "sdist")
            )
        for member_name in sorted(files):
            if member_name.endswith(".py"):
                _require_checkout_bytes(
                    _read_tar_bytes(archive, files[member_name], "sdist"),
                    root / member_name[len(prefix) + 1 :],
                    "sdist",
                    member_name,
                )


def _zipapp_builder():
    """Load the sibling build script: it owns the targets and ``__main__``."""
    spec = importlib.util.spec_from_file_location(
        "agentflow_build_zipapp", Path(__file__).with_name("build_zipapp.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inspect_zipapp(
    path: Path, module: str, root: Path, sources: set[str]
) -> None:
    label = path.name
    if not path.is_file():
        raise DistributionCheckError(f"missing zipapp: {label}")
    _check_archive_size(path, label)
    with path.open("rb") as handle:
        if handle.read(len(ZIPAPP_INTERPRETER_LINE)) != ZIPAPP_INTERPRETER_LINE:
            raise DistributionCheckError(
                f"{label} does not start with the expected interpreter line"
            )
    main = _zipapp_builder().MAIN_TEMPLATE.format(module=module).encode("utf-8")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        _check_member_limits([info.file_size for info in infos], label)
        names = [info.filename for info in infos]
        _safe_archive_names(names, label)
        for member in names:
            if member.endswith("/"):
                if not any(source.startswith(member) for source in sources):
                    raise DistributionCheckError(
                        f"unexpected {label} path: {member}"
                    )
            elif member != "__main__.py" and member not in sources:
                raise DistributionCheckError(f"unexpected {label} path: {member}")
        for required in sorted(sources | {"__main__.py"}):
            if required not in names:
                raise DistributionCheckError(f"{label} is missing {required}")
        if _read_zip_bytes(archive, "__main__.py", label) != main:
            raise DistributionCheckError(
                f"{label} file differs from the build script: __main__.py"
            )
        for source in sorted(sources):
            _require_checkout_bytes(
                _read_zip_bytes(archive, source, label),
                root / "src" / source,
                label,
                source,
            )


def inspect_distribution(root: Path, dist_dir: Path) -> list[Path]:
    """Validate and return the wheel, the sdist and both zipapps in ``dist_dir``."""
    name, version, requires_python, sources, packaging_config = _project_contract(
        root
    )
    wheel = _sole_artifact(dist_dir, "*.whl", "wheel")
    sdist = _sole_artifact(dist_dir, "*.tar.gz", "sdist")
    _inspect_wheel(wheel, root, name, version, requires_python, sources)
    _inspect_sdist(
        sdist,
        root,
        name,
        version,
        requires_python,
        sources,
        packaging_config,
    )
    zipapps = []
    for target, module in sorted(_zipapp_builder().TARGETS.items()):
        zipapp_path = dist_dir / f"{target}.pyz"
        _inspect_zipapp(zipapp_path, module, root, sources)
        zipapps.append(zipapp_path)
    return [wheel, sdist, *zipapps]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--dist-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    dist_dir = args.dist_dir or args.root / "dist"
    try:
        artifacts = inspect_distribution(args.root, dist_dir)
    except (
        DistributionCheckError,
        EOFError,
        OSError,
        UnicodeDecodeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        sys.stderr.write(f"distribution check failed: {exc}\n")
        return 1
    print(
        "distribution artifacts passed: "
        + ", ".join(artifact.name for artifact in artifacts)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
