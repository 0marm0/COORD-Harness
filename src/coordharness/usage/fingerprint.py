
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Literal


SCHEMA_VERSION = "usage-source-fingerprint-v2"
SourceType = Literal["cache", "ledger", "session", "source"]
_SOURCE_TYPES = frozenset({"auto", "cache", "ledger", "session", "source"})


@dataclass(frozen=True, slots=True)
class SourceRoot:

    path: str | os.PathLike[str]
    provider: str
    account_key: str
    timezone: str
    source_type: str = "auto"
    include_sha256: bool = False
    allow_symlink_escape: bool = False

    def __post_init__(self) -> None:
        for field_name in ("provider", "account_key", "timezone"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.account_key == "unknown":
            raise ValueError(
                "account_key 'unknown' is unsafe; use unknown:<profile-digest>"
            )
        if self.source_type not in _SOURCE_TYPES:
            choices = ", ".join(sorted(_SOURCE_TYPES))
            raise ValueError(f"source_type must be one of: {choices}")

    @property
    def normalized_path(self) -> Path:
        return Path(self.path).expanduser().absolute()


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    relative_path: str
    entry_type: Literal["file"]
    source_type: SourceType
    size: int
    mtime_ns: int
    inode: int | None
    sha256: str | None
    is_symlink: bool
    symlink_target: str | None
    read_consistent: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "entry_type": self.entry_type,
            "source_type": self.source_type,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "inode": self.inode,
            "sha256": self.sha256,
            "is_symlink": self.is_symlink,
            "symlink_target": self.symlink_target,
            "read_consistent": self.read_consistent,
        }


@dataclass(frozen=True, slots=True)
class ManifestIssue:
    relative_path: str
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "relative_path": self.relative_path,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    root_path: str
    provider: str
    account_key: str
    timezone: str
    declared_source_type: str
    include_sha256: bool
    fingerprint_strength: Literal["strong_sha256", "metadata_only"]
    allow_symlink_escape: bool
    status: Literal["available", "partial", "missing", "unavailable"]
    entries: tuple[ManifestEntry, ...]
    issues: tuple[ManifestIssue, ...]
    root_digest: str

    def to_dict(self) -> dict[str, object]:

        return {
            "schema_version": SCHEMA_VERSION,
            "root_path": self.root_path,
            "provider": self.provider,
            "account_key": self.account_key,
            "timezone": self.timezone,
            "declared_source_type": self.declared_source_type,
            "include_sha256": self.include_sha256,
            "fingerprint_strength": self.fingerprint_strength,
            "allow_symlink_escape": self.allow_symlink_escape,
            "status": self.status,
            "entries": [entry.to_dict() for entry in self.entries],
            "issues": [issue.to_dict() for issue in self.issues],
            "root_digest": self.root_digest,
        }


def classify_source_type(relative_path: str, declared: str = "auto") -> SourceType:

    if declared != "auto":
        if declared not in _SOURCE_TYPES:
            raise ValueError(f"unsupported declared source type: {declared}")
        return declared

    path = Path(relative_path)
    parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    suffix = path.suffix.casefold()

    if "ledger" in name or suffix in {".db", ".sqlite", ".sqlite3"}:
        return "ledger"
    if any("cache" in part for part in parts) or name.startswith(("claude-v", "codex-v")):
        return "cache"
    if (
        any(part in {"session", "sessions"} or "session" in part for part in parts)
        or suffix == ".jsonl"
    ):
        return "session"
    return "source"


def fingerprint_source(config: SourceRoot) -> SourceFingerprint:

    root = config.normalized_path
    root_path = os.fspath(root)
    base_payload = _base_digest_payload(config)

    try:
        root_exists = root.exists()
    except OSError as exc:
        return _empty_result(config, "unavailable", "root_unavailable", exc)
    if not root_exists:
        return _empty_result(config, "missing", "root_missing", None)

    try:
        resolved_root = root.resolve(strict=True) if root.is_dir() else root.parent.resolve(strict=True)
    except OSError as exc:
        return _empty_result(config, "unavailable", "root_unavailable", exc)

    entries: list[ManifestEntry] = []
    issues: list[ManifestIssue] = []

    try:
        if root.is_file() or root.is_symlink():
            relative_path = root.name
            entry = _inspect_candidate(
                root,
                relative_path,
                resolved_root,
                config,
                issues,
            )
            if entry is not None:
                entries.append(entry)
        elif root.is_dir():
            _walk_directory(root, resolved_root, config, entries, issues)
        else:
            issues.append(
                ManifestIssue(
                    relative_path=".",
                    code="unsupported_root_type",
                    detail="configured root is not a regular file or directory",
                )
            )
    except (OSError, PermissionError) as exc:
        issues.append(
            ManifestIssue(
                relative_path=".",
                code="root_unavailable",
                detail=_safe_error_detail(exc),
            )
        )

    entries.sort(key=lambda item: item.relative_path)
    issues.sort(key=lambda item: (item.relative_path, item.code, item.detail))
    root_unavailable = not entries and any(
        issue.relative_path == "."
        and issue.code in {"directory_unavailable", "root_unavailable"}
        for issue in issues
    )
    status: Literal["available", "partial", "missing", "unavailable"]
    if root_unavailable:
        status = "unavailable"
    else:
        status = "partial" if issues else "available"
    digest_payload = {
        **base_payload,
        "status": status,
        "entries": [_digest_entry(entry) for entry in entries],
        "issues": [issue.to_dict() for issue in issues],
    }
    return SourceFingerprint(
        root_path=root_path,
        provider=config.provider,
        account_key=config.account_key,
        timezone=config.timezone,
        declared_source_type=config.source_type,
        include_sha256=config.include_sha256,
        fingerprint_strength="strong_sha256" if config.include_sha256 else "metadata_only",
        allow_symlink_escape=config.allow_symlink_escape,
        status=status,
        entries=tuple(entries),
        issues=tuple(issues),
        root_digest=_canonical_digest(digest_payload),
    )


def fingerprint_sources(configs: Iterable[SourceRoot]) -> dict[str, object]:

    ordered = sorted(
        configs,
        key=lambda item: (
            item.provider,
            item.account_key,
            item.timezone,
            os.fspath(item.normalized_path),
            item.source_type,
            item.include_sha256,
            item.allow_symlink_escape,
        ),
    )
    roots = [fingerprint_source(config) for config in ordered]
    logical_roots = [
        {
            "provider": root.provider,
            "account_key": root.account_key,
            "timezone": root.timezone,
            "declared_source_type": root.declared_source_type,
            "fingerprint_strength": root.fingerprint_strength,
            "root_digest": root.root_digest,
        }
        for root in roots
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "roots": [root.to_dict() for root in roots],
        "aggregate_digest": _canonical_digest(
            {"schema_version": SCHEMA_VERSION, "roots": logical_roots}
        ),
    }


def _walk_directory(
    root: Path,
    resolved_root: Path,
    config: SourceRoot,
    entries: list[ManifestEntry],
    issues: list[ManifestIssue],
) -> None:
    pending: list[Path] = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda item: item.name)
        except (OSError, PermissionError) as exc:
            rel = _relative_display(directory, root)
            issues.append(ManifestIssue(rel, "directory_unavailable", _safe_error_detail(exc)))
            continue

        child_directories: list[Path] = []
        for child in children:
            path = Path(child.path)
            relative_path = path.relative_to(root).as_posix()
            try:
                if child.is_symlink():
                    entry = _inspect_candidate(
                        path,
                        relative_path,
                        resolved_root,
                        config,
                        issues,
                    )
                    if entry is not None:
                        entries.append(entry)
                    continue
                if child.is_dir(follow_symlinks=False):
                    child_directories.append(path)
                    continue
                if child.is_file(follow_symlinks=False):
                    entry = _fingerprint_file(path, relative_path, config, False, None, issues)
                    if entry is not None:
                        entries.append(entry)
                    continue
                issues.append(
                    ManifestIssue(
                        relative_path,
                        "unsupported_file_type",
                        "entry is not a regular file, directory, or symlink",
                    )
                )
            except (OSError, PermissionError) as exc:
                issues.append(
                    ManifestIssue(relative_path, "entry_unavailable", _safe_error_detail(exc))
                )

        pending.extend(reversed(child_directories))


def _inspect_candidate(
    path: Path,
    relative_path: str,
    resolved_root: Path,
    config: SourceRoot,
    issues: list[ManifestIssue],
) -> ManifestEntry | None:
    is_symlink = path.is_symlink()
    if not is_symlink:
        if path.is_file():
            return _fingerprint_file(path, relative_path, config, False, None, issues)
        issues.append(
            ManifestIssue(relative_path, "unsupported_file_type", "entry is not a regular file")
        )
        return None

    try:
        target = path.resolve(strict=True)
    except OSError as exc:
        issues.append(ManifestIssue(relative_path, "broken_symlink", _safe_error_detail(exc)))
        return None

    inside_root = _is_within(target, resolved_root)
    if not inside_root and not config.allow_symlink_escape:
        issues.append(
            ManifestIssue(
                relative_path,
                "rejected_symlink_escape",
                "symlink target resolves outside the configured source root",
            )
        )
        return None
    if target.is_dir():
        issues.append(
            ManifestIssue(
                relative_path,
                "symlink_directory_not_followed",
                "directory symlinks are not traversed to avoid cycles and duplicate custody",
            )
        )
        return None
    if not target.is_file():
        issues.append(
            ManifestIssue(relative_path, "unsupported_symlink_target", "target is not a regular file")
        )
        return None

    if inside_root:
        target_display = target.relative_to(resolved_root).as_posix()
    else:
        target_display = os.fspath(target)
    return _fingerprint_file(
        target,
        relative_path,
        config,
        True,
        target_display,
        issues,
    )


def _fingerprint_file(
    path: Path,
    relative_path: str,
    config: SourceRoot,
    is_symlink: bool,
    symlink_target: str | None,
    issues: list[ManifestIssue],
) -> ManifestEntry | None:
    try:
        before = path.stat(follow_symlinks=True)
        file_hash = _stream_sha256(path) if config.include_sha256 else None
        after = path.stat(follow_symlinks=True)
    except (OSError, PermissionError) as exc:
        issues.append(ManifestIssue(relative_path, "file_unavailable", _safe_error_detail(exc)))
        return None

    identity_before = (before.st_size, before.st_mtime_ns, getattr(before, "st_ino", None))
    identity_after = (after.st_size, after.st_mtime_ns, getattr(after, "st_ino", None))
    read_consistent = identity_before == identity_after
    if not read_consistent:
        issues.append(
            ManifestIssue(
                relative_path,
                "changed_during_scan",
                "size, mtime, or inode changed while the file was observed",
            )
        )

    return ManifestEntry(
        relative_path=relative_path,
        entry_type="file",
        source_type=classify_source_type(relative_path, config.source_type),
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        inode=getattr(after, "st_ino", None),
        sha256=file_hash,
        is_symlink=is_symlink,
        symlink_target=symlink_target,
        read_consistent=read_consistent,
    )


def _stream_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _empty_result(
    config: SourceRoot,
    status: Literal["missing", "unavailable"],
    code: str,
    exc: BaseException | None,
) -> SourceFingerprint:
    issue = ManifestIssue(
        relative_path=".",
        code=code,
        detail="configured root does not exist" if exc is None else _safe_error_detail(exc),
    )
    payload = {
        **_base_digest_payload(config),
        "status": status,
        "entries": [],
        "issues": [issue.to_dict()],
    }
    return SourceFingerprint(
        root_path=os.fspath(config.normalized_path),
        provider=config.provider,
        account_key=config.account_key,
        timezone=config.timezone,
        declared_source_type=config.source_type,
        include_sha256=config.include_sha256,
        fingerprint_strength="strong_sha256" if config.include_sha256 else "metadata_only",
        allow_symlink_escape=config.allow_symlink_escape,
        status=status,
        entries=(),
        issues=(issue,),
        root_digest=_canonical_digest(payload),
    )


def _base_digest_payload(config: SourceRoot) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": config.provider,
        "account_key": config.account_key,
        "timezone": config.timezone,
        "declared_source_type": config.source_type,
        "include_sha256": config.include_sha256,
        "allow_symlink_escape": config.allow_symlink_escape,
    }


def _digest_entry(entry: ManifestEntry) -> dict[str, object]:
    return {
        "relative_path": entry.relative_path,
        "entry_type": entry.entry_type,
        "source_type": entry.source_type,
        "size": entry.size,
        "mtime_ns": entry.mtime_ns,
        "sha256": entry.sha256,
        "is_symlink": entry.is_symlink,
        "symlink_target": entry.symlink_target,
        "read_consistent": entry.read_consistent,
    }


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _relative_display(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
        return relative or "."
    except ValueError:
        return "."


def _safe_error_detail(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
