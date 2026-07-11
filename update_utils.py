"""Pure-Python helpers for the Learn Node GitHub ZIP updater."""

from __future__ import annotations

import json
import keyword
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote


Version = tuple[int, int, int]
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_BL_INFO_VERSION_PATTERN = re.compile(
    r'"version"\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)'
)

REQUIRED_RUNTIME_FILES = (
    "__init__.py",
    "update_utils.py",
)


class VersionError(ValueError):
    """Raised when a value is not a supported major.minor.patch version."""


class ArchiveValidationError(ValueError):
    """Raised when an update archive is not a valid Learn Node package."""


class InstallTransactionError(RuntimeError):
    """Raised when update installation or rollback fails."""

    def __init__(self, message: str, backup_path: str | None = None):
        super().__init__(message)
        self.backup_path = backup_path


class MetadataError(ValueError):
    """Raised when GitHub metadata has no supported update version."""


@dataclass(frozen=True)
class UpdateMetadata:
    version: Version
    ref: str
    archive_url: str
    release_url: str
    release_notes: str = ""


def format_version(version: Version) -> str:
    return ".".join(str(value) for value in version)


def parse_version(value: object) -> Version:
    """Return a normalized three-part version from a tag or tuple."""
    if isinstance(value, (tuple, list)):
        if len(value) != 3 or any(isinstance(part, bool) or not isinstance(part, int) for part in value):
            raise VersionError(f"Unsupported version: {value!r}")
        if any(part < 0 for part in value):
            raise VersionError(f"Unsupported version: {value!r}")
        return tuple(value)  # type: ignore[return-value]

    if isinstance(value, str):
        match = _VERSION_PATTERN.fullmatch(value.strip())
        if match:
            return tuple(int(group) for group in match.groups())  # type: ignore[return-value]

    raise VersionError(f"Unsupported version: {value!r}")


def metadata_from_release(
    payload: dict,
    *,
    archive_base_url: str,
    fallback_release_url: str,
) -> UpdateMetadata:
    """Convert a GitHub Release response into an installable update candidate."""
    ref = str(payload.get("tag_name") or payload.get("name") or "").strip()
    try:
        version = parse_version(ref)
    except VersionError as error:
        raise MetadataError(f"Release has no supported version tag: {ref!r}") from error

    release_url = str(payload.get("html_url") or fallback_release_url)
    release_notes = str(payload.get("body") or "").strip()
    expected_asset_name = f"LearnNodeBlender-v{format_version(version)}.zip".casefold()
    archive_url = archive_base_url + quote(ref, safe="")
    for asset in payload.get("assets") or ():
        asset_name = str(asset.get("name") or "").casefold()
        asset_url = str(asset.get("browser_download_url") or "").strip()
        if asset_name == expected_asset_name and asset_url:
            archive_url = asset_url
            break

    return UpdateMetadata(
        version=version,
        ref=ref,
        archive_url=archive_url,
        release_url=release_url,
        release_notes=release_notes,
    )


def metadata_from_tags(
    payload: list[dict],
    *,
    archive_base_url: str,
    fallback_release_url: str,
) -> UpdateMetadata:
    """Select the highest supported numeric version from GitHub tag responses."""
    candidates = []
    for tag in payload:
        try:
            candidates.append(
                metadata_from_release(
                    tag,
                    archive_base_url=archive_base_url,
                    fallback_release_url=fallback_release_url,
                )
            )
        except (MetadataError, AttributeError):
            continue
    if not candidates:
        raise MetadataError("No supported version tags were found")
    return max(candidates, key=lambda candidate: candidate.version)


def fetch_update_metadata(
    fetch_json,
    *,
    release_api_url: str,
    tags_api_url: str,
    archive_base_url: str,
    fallback_release_url: str,
) -> UpdateMetadata:
    """Fetch Release metadata, falling back to supported numeric Tags."""
    try:
        release_payload = fetch_json(release_api_url)
        return metadata_from_release(
            release_payload,
            archive_base_url=archive_base_url,
            fallback_release_url=fallback_release_url,
        )
    except Exception:
        tags_payload = fetch_json(tags_api_url)
        return metadata_from_tags(
            tags_payload,
            archive_base_url=archive_base_url,
            fallback_release_url=fallback_release_url,
        )


def read_version_from_init_text(content: str) -> Version:
    """Read the static three-part version declaration from add-on source."""
    match = _BL_INFO_VERSION_PATTERN.search(content)
    if not match:
        raise ArchiveValidationError("The package has no readable three-part bl_info version")
    return parse_version(tuple(int(group) for group in match.groups()))


def _normalize_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    windows_path = PureWindowsPath(normalized)
    if not normalized or normalized.startswith("/") or windows_path.drive or windows_path.root:
        raise ArchiveValidationError(f"Unsafe archive member path: {name!r}")

    parts = PurePosixPath(normalized).parts
    if any(part in ("..", "") for part in parts):
        raise ArchiveValidationError(f"Unsafe archive member path: {name!r}")
    return "/".join(part for part in parts if part != ".")


def _validated_members(archive: zipfile.ZipFile) -> list[tuple[str, bool]]:
    members = []
    seen = set()
    for info in archive.infolist():
        normalized = _normalize_member_name(info.filename.rstrip("/"))
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            raise ArchiveValidationError(f"Duplicate archive member path: {info.filename!r}")
        seen.add(key)
        members.append((normalized, info.is_dir() or info.filename.endswith("/")))
    return members


def _find_package_prefix(file_names: set[str], required_runtime_files: tuple[str, ...]) -> str:
    prefixes = []
    if "__init__.py" in file_names:
        prefixes.append("")

    for name in file_names:
        if not name.endswith("/__init__.py"):
            continue
        prefix = name[: -len("__init__.py")]
        if prefix.rstrip("/").count("/") != 0:
            continue
        prefixes.append(prefix)

    valid_prefixes = []
    for prefix in prefixes:
        if all(prefix + relative in file_names for relative in required_runtime_files):
            valid_prefixes.append(prefix)

    if len(valid_prefixes) != 1:
        raise ArchiveValidationError(
            "Archive must contain exactly one valid package root"
        )
    return valid_prefixes[0]


def extract_and_validate_archive(
    archive_path: str | Path,
    extraction_dir: str | Path,
    expected_version: Version,
    required_runtime_files: tuple[str, ...] = REQUIRED_RUNTIME_FILES,
) -> Path:
    """Extract and validate a complete Learn Node package into a temp directory."""
    archive_path = Path(archive_path)
    extraction_dir = Path(extraction_dir)
    extraction_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validated_members(archive)
            corrupt_member = archive.testzip()
            if corrupt_member:
                raise ArchiveValidationError(f"Corrupt archive member: {corrupt_member}")
            file_names = {name for name, is_dir in members if not is_dir}
            prefix = _find_package_prefix(file_names, required_runtime_files)
            archive.extractall(extraction_dir)
    except ArchiveValidationError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ArchiveValidationError(f"Could not read update archive: {error}") from error

    package_root = extraction_dir
    if prefix:
        package_root = extraction_dir.joinpath(*prefix.rstrip("/").split("/"))

    try:
        package_version = read_version_from_init_text(
            (package_root / "__init__.py").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ArchiveValidationError) as error:
        raise ArchiveValidationError(f"Could not validate package metadata: {error}") from error

    if package_version != parse_version(expected_version):
        raise ArchiveValidationError(
            f"Package version {package_version} does not match expected {expected_version}"
        )

    for data_name in required_runtime_files:
        data_path = package_root / data_name
        if not data_path.is_file():
            raise ArchiveValidationError(f"Missing required file: {data_name}")

    return package_root


def build_release_archive(
    source_dir: str | Path,
    destination: str | Path,
    package_name: str,
) -> Path:
    """Build a Blender-installable ZIP with a valid Python package root."""
    source_dir = Path(source_dir)
    destination = Path(destination)
    if not package_name.isidentifier() or keyword.iskeyword(package_name):
        raise ValueError(f"Invalid Python package name: {package_name!r}")
    if not source_dir.is_dir() or not (source_dir / "__init__.py").is_file():
        raise ValueError("Release source must contain a root __init__.py")

    runtime_files = sorted(
        path for path in source_dir.glob("*.py") if path.name != "build_release_zip.py"
    )
    data_dir = source_dir / "data"
    if data_dir.is_dir():
        runtime_files.extend(sorted(path for path in data_dir.rglob("*") if path.is_file()))
    if not runtime_files:
        raise ValueError("Release source contains no runtime files")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in runtime_files:
            relative_path = path.relative_to(source_dir).as_posix()
            archive.write(path, f"{package_name}/{relative_path}")
    return destination


def _clear_directory(directory: Path) -> None:
    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, dirs_exist_ok=True)


def install_package(
    source_dir: str | Path,
    target_dir: str | Path,
    work_dir: str | Path,
    *,
    copy_tree=_copy_tree,
) -> None:
    """Replace an installed package and restore it if replacement fails."""
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    work_dir = Path(work_dir)
    if not source_dir.is_dir():
        raise InstallTransactionError(f"Update package is not a directory: {source_dir}")
    if not target_dir.is_dir():
        raise InstallTransactionError(f"Installed add-on directory is unavailable: {target_dir}")

    work_dir.mkdir(parents=True, exist_ok=True)
    backup_path = work_dir / "backup"
    if backup_path.exists():
        shutil.rmtree(backup_path)
    copy_tree(target_dir, backup_path)

    try:
        _clear_directory(target_dir)
        copy_tree(source_dir, target_dir)
    except Exception as install_error:
        try:
            _clear_directory(target_dir)
            copy_tree(backup_path, target_dir)
        except Exception as rollback_error:
            raise InstallTransactionError(
                f"Update failed and rollback failed: {rollback_error}",
                backup_path=str(backup_path),
            ) from rollback_error
        raise InstallTransactionError(
            f"Update installation failed: {install_error}"
        ) from install_error
