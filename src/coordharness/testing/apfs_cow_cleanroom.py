
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence
from urllib.parse import quote


SCHEMA = "coordharness.apfs-cow-cleanroom.v1"

CLONE_NOFOLLOW = 0x0001
CLONE_NOOWNERCOPY = 0x0002
CLONE_ACL = 0x0004
CLONE_NOFOLLOW_ANY = 0x0008
CLONE_RESOLVE_BENEATH = 0x0010
CLONE_FLAGS = CLONE_NOFOLLOW | CLONE_NOOWNERCOPY | CLONE_NOFOLLOW_ANY | CLONE_RESOLVE_BENEATH

TERMINAL_STATES = frozenset(
    {
        "TOPOLOGY_PASS",
        "FIXTURE_PASS",
        "NOT_READY_CANDIDATE_NOT_FROZEN",
        "REFUSED_PLATFORM",
        "REFUSED_CAPACITY_BEFORE",
        "REFUSED_CAPACITY_AFTER",
        "REFUSED_CANDIDATE_REF",
        "REFUSED_GIT_CHECKOUT",
        "REFUSED_SOURCE_TOPOLOGY",
        "REFUSED_SYMLINK_BROKEN",
        "REFUSED_SYMLINK_CYCLE",
        "REFUSED_EXTERNAL_LINK",
        "REFUSED_SPECIAL_FILE",
        "REFUSED_COW",
        "REFUSED_SOURCE_DRIFT",
        "REFUSED_SQLITE_BACKUP",
        "REFUSED_VENV_LEAK",
        "REFUSED_MANIFEST_NONDETERMINISM",
        "REFUSED_SANDBOX_RED_ARM",
        "REFUSED_ATTEMPT_REUSE",
        "INCOMPLETE_INFRA",
        "INCOMPLETE_CARDINALITY",
        "COMPLETE_RED",
        "COMPLETE_GREEN",
    }
)


@dataclass(frozen=True)
class StoreSpec:
    store_id: str
    relative_path: str


DEFAULT_STORE_SPECS: tuple[StoreSpec, ...] = (
    StoreSpec("project_data", ".coordharness/stores/data"),
    StoreSpec("models", ".coordharness/stores/models"),
    StoreSpec("repositories", ".coordharness/stores/repositories"),
)


@dataclass(frozen=True)
class TopologyEntry:
    store_id: str
    path: str
    kind: str
    mode: int
    size: int
    source_sha256: str | None = None
    link_target: str | None = None
    target_namespace: str | None = None
    rewritten_target: str | None = None
    hardlink_group: str | None = None


class CleanroomRefusal(RuntimeError):

    def __init__(
        self,
        state: str,
        detail: str,
        *,
        path: str | os.PathLike[str] | None = None,
        error_number: int | None = None,
    ) -> None:
        if state not in TERMINAL_STATES:
            raise ValueError(f"unknown terminal state: {state}")
        self.state = state
        self.detail = detail
        self.path = None if path is None else os.fspath(path)
        self.error_number = error_number
        super().__init__(f"{state}: {detail}" + (f" [{self.path}]" if self.path else ""))

    def receipt(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "path": self.path,
            "errno": self.error_number,
            "errno_name": errno.errorcode.get(self.error_number) if self.error_number else None,
        }


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_manifest(entries: Iterable[TopologyEntry]) -> dict[str, Any]:

    rows = [asdict(entry) for entry in entries]
    rows.sort(key=lambda row: (row["store_id"].encode("utf-8"), os.fsencode(row["path"])))
    body = {
        "schema": SCHEMA,
        "scope": "topology_and_explicit_content_hashes",
        "entries": rows,
    }
    return {**body, "sha256": hashlib.sha256(_canonical_json_bytes(body)).hexdigest()}


def _absolute_syntactic(path: Path) -> Path:

    raw = os.fspath(path)
    candidate = raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)
    components = [component for component in candidate.split(os.sep) if component]
    if any(component in {".", ".."} for component in components):
        raise CleanroomRefusal(
            "REFUSED_SOURCE_TOPOLOGY", "configured roots may not contain dot traversal", path=path
        )
    return Path(os.sep, *components)


def _relative_to_component_prefix(path: Path, prefix: Path) -> Path | None:
    try:
        return path.relative_to(prefix)
    except ValueError:
        return None


def _filesystem_collision_key(path: Path) -> str:
    return unicodedata.normalize("NFD", os.fspath(path)).casefold()


def _normalized_component_parts(path: Path) -> tuple[str, ...]:
    absolute = _absolute_syntactic(path)
    return tuple(
        unicodedata.normalize("NFD", component).casefold()
        for component in absolute.parts[1:]
    )


def _normalized_component_overlap(first: Path, second: Path) -> bool:
    first_parts = _normalized_component_parts(first)
    second_parts = _normalized_component_parts(second)
    shorter = min(len(first_parts), len(second_parts))
    return first_parts[:shorter] == second_parts[:shorter]


def _directory_identity_chain(path: Path) -> tuple[tuple[int, int], ...]:
    absolute = _absolute_syntactic(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(os.sep, flags)
    identities: list[tuple[int, int]] = []
    try:
        root_info = os.fstat(descriptor)
        identities.append((int(root_info.st_dev), int(root_info.st_ino)))
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            identities.append((int(info.st_dev), int(info.st_ino)))
        return tuple(identities)
    finally:
        os.close(descriptor)


def _existing_directory_identity_chain(path: Path) -> tuple[tuple[int, int], ...]:

    absolute = _absolute_syntactic(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(os.sep, flags)
    identities: list[tuple[int, int]] = []
    try:
        root_info = os.fstat(descriptor)
        identities.append((int(root_info.st_dev), int(root_info.st_ino)))
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                break
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            identities.append((int(info.st_dev), int(info.st_ino)))
        return tuple(identities)
    finally:
        os.close(descriptor)


def longest_prefix_mapping(
    path: Path | str,
    mappings: Mapping[Path | str, Path | str],
) -> tuple[Path, Path, Path] | None:

    candidate = _absolute_syntactic(Path(path))
    normalized = [
        (_absolute_syntactic(Path(source)), _absolute_syntactic(Path(destination)))
        for source, destination in mappings.items()
    ]
    normalized.sort(key=lambda row: (-len(row[0].parts), os.fsencode(str(row[0]))))
    for source, destination in normalized:
        relative = _relative_to_component_prefix(candidate, source)
        if relative is not None:
            return source, destination, relative
    return None


def rewritten_symlink_target(
    *,
    source_link: Path,
    destination_link: Path,
    raw_target: str,
    mappings: Mapping[Path | str, Path | str],
    external_allowlist: Sequence[Path | str] = (),
) -> tuple[Path, str, str]:

    if os.path.isabs(raw_target):
        target = _absolute_syntactic(Path(raw_target))
    else:
        if ".." in Path(raw_target).parts:
            raise CleanroomRefusal(
                "REFUSED_SOURCE_TOPOLOGY",
                "relative parent traversal requires componentwise symlink-graph resolution",
                path=source_link,
            )
        target = _absolute_syntactic(source_link.parent / raw_target)
    mapped = longest_prefix_mapping(target, mappings)
    if mapped is not None:
        source_prefix, destination_prefix, relative = mapped
        rewritten_absolute = destination_prefix / relative
        rewritten = os.path.relpath(rewritten_absolute, start=destination_link.parent)
        return target, "mapped_store", rewritten
    for allowed in sorted(
        (_absolute_syntactic(Path(item)) for item in external_allowlist),
        key=lambda item: (-len(item.parts), os.fsencode(str(item))),
    ):
        if _relative_to_component_prefix(target, allowed) is not None:
            return target, "external_readonly", raw_target
    raise CleanroomRefusal(
        "REFUSED_EXTERNAL_LINK",
        f"symlink target is outside every physical-store mapping and exact allowlist: {raw_target!r}",
        path=source_link,
    )


def _open_directory_nofollow(path: Path) -> int:

    absolute = _absolute_syntactic(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise CleanroomRefusal(
                "REFUSED_SOURCE_TOPOLOGY", "physical root must be a directory", path=absolute
            )
        return descriptor
    except CleanroomRefusal:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise CleanroomRefusal(
            "REFUSED_SOURCE_TOPOLOGY",
            "physical root component is absent, non-directory, or a symlink",
            path=absolute,
            error_number=exc.errno,
        ) from exc


def _mkdir_absolute_nofollow(path: Path, mode: int = 0o755) -> int:

    absolute = _absolute_syntactic(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise CleanroomRefusal(
            "REFUSED_COW",
            "destination parent is a symlink, non-directory, or could not be created",
            path=absolute,
            error_number=exc.errno,
        ) from exc


def _assert_path_absent_nofollow(path: Path) -> None:
    absolute = _absolute_syntactic(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(os.sep, flags)
    try:
        components = absolute.parts[1:]
        for index, component in enumerate(components):
            if index == len(components) - 1:
                try:
                    os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return
                raise CleanroomRefusal("REFUSED_COW", "destination already exists", path=absolute)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                return
            os.close(descriptor)
            descriptor = child
    except CleanroomRefusal:
        raise
    except OSError as exc:
        raise CleanroomRefusal(
            "REFUSED_COW",
            "destination path contains a symlink or non-directory parent",
            path=absolute,
            error_number=exc.errno,
        ) from exc
    finally:
        os.close(descriptor)


def _open_child_directory(parent_fd: int, name: str, display: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise CleanroomRefusal(
            "REFUSED_SOURCE_TOPOLOGY",
            "directory changed or resolved through a symlink during traversal",
            path=display,
            error_number=exc.errno,
        ) from exc


def _entry_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def _target_components(link_path: Path, raw_target: str) -> tuple[list[str], deque[str]]:
    if os.path.isabs(raw_target):
        base: list[str] = []
    else:
        base = list(link_path.parent.parts[1:])
    pending = deque(component for component in raw_target.split(os.sep) if component not in {"", "."})
    return base, pending


def _resolve_symlink_target_components(
    link_path: Path,
    *,
    links: Mapping[Path, str],
    known_kinds: Mapping[Path, str],
    physical_roots: Sequence[Path],
    external_allowlist: Sequence[Path],
) -> Path:

    resolved, pending = _target_components(link_path, links[link_path])
    visited: list[Path] = [link_path]
    while pending:
        component = pending.popleft()
        if component == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = Path(os.sep, *resolved, component)
        if candidate in links:
            if candidate in visited:
                chain = " -> ".join(os.fspath(item) for item in (*visited, candidate))
                raise CleanroomRefusal(
                    "REFUSED_SYMLINK_CYCLE",
                    f"componentwise symlink cycle: {chain}",
                    path=candidate,
                )
            visited.append(candidate)
            nested_base, nested_pending = _target_components(candidate, links[candidate])
            if os.path.isabs(links[candidate]):
                resolved = nested_base
            else:
                resolved = nested_base
            pending.extendleft(reversed(nested_pending))
            continue
        resolved.append(component)
        known_kind = known_kinds.get(candidate)
        if pending and known_kind not in {None, "directory"}:
            raise CleanroomRefusal(
                "REFUSED_SYMLINK_BROKEN",
                "non-directory component occurs before the end of a symlink target",
                path=candidate,
            )
    current = Path(os.sep, *resolved)
    inside = any(_relative_to_component_prefix(current, root) is not None for root in physical_roots)
    allowed = any(_relative_to_component_prefix(current, root) is not None for root in external_allowlist)
    if inside and current not in known_kinds:
        raise CleanroomRefusal(
            "REFUSED_SYMLINK_BROKEN", "internal symlink target is absent from the no-follow inventory", path=current
        )
    if not inside and not allowed:
        raise CleanroomRefusal(
            "REFUSED_EXTERNAL_LINK", "resolved symlink chain escapes every allowed namespace", path=current
        )
    if allowed and not inside:
        allowed_root = next(
            root for root in external_allowlist if _relative_to_component_prefix(current, root) is not None
        )
        relative = current.relative_to(allowed_root)
        root_fd = _open_directory_nofollow(allowed_root)
        try:
            if relative == Path("."):
                info = os.fstat(root_fd)
            else:
                parent_fd = _open_relative_directory(root_fd, relative.parent)
                try:
                    info = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
                finally:
                    os.close(parent_fd)
        except CleanroomRefusal as exc:
            raise CleanroomRefusal(
                "REFUSED_EXTERNAL_LINK",
                f"allowlisted external ancestry contains a symlink or non-directory: {exc.detail}",
                path=current,
            ) from exc
        except OSError as exc:
            state = (
                "REFUSED_EXTERNAL_LINK"
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}
                else "REFUSED_SYMLINK_BROKEN"
            )
            raise CleanroomRefusal(
                state,
                "allowlisted external target is missing or has a symlink/non-directory ancestor",
                path=current,
                error_number=exc.errno,
            ) from exc
        finally:
            os.close(root_fd)
        kind = _entry_kind(info.st_mode)
        if kind not in {"regular", "directory"}:
            raise CleanroomRefusal(
                "REFUSED_SPECIAL_FILE",
                "allowlisted external target must resolve to a regular file or directory",
                path=current,
            )
    return current


def inventory_topology(
    store_roots: Mapping[str, Path | str],
    *,
    destination_roots: Mapping[str, Path | str] | None = None,
    external_allowlist: Sequence[Path | str] = (),
    hash_regular_files: bool = False,
    require_disjoint_destinations: bool = False,
) -> dict[str, Any]:

    if not store_roots:
        raise CleanroomRefusal("REFUSED_SOURCE_TOPOLOGY", "at least one physical store is required")
    roots = {name: _absolute_syntactic(Path(path)) for name, path in store_roots.items()}
    if len(set(roots.values())) != len(roots):
        raise CleanroomRefusal("REFUSED_SOURCE_TOPOLOGY", "physical store roots must be unique")
    source_collision_keys = [_filesystem_collision_key(path) for path in roots.values()]
    if len(set(source_collision_keys)) != len(source_collision_keys):
        raise CleanroomRefusal(
            "REFUSED_SOURCE_TOPOLOGY", "physical roots collide under casefold/Unicode normalization"
        )
    root_values = tuple(roots.values())
    for index, root in enumerate(root_values):
        for other in root_values[index + 1 :]:
            if (
                _relative_to_component_prefix(root, other) is not None
                or _relative_to_component_prefix(other, root) is not None
            ):
                raise CleanroomRefusal(
                    "REFUSED_SOURCE_TOPOLOGY", "physical roots must be component-disjoint", path=root
                )
    destinations = {
        name: _absolute_syntactic(Path(path))
        for name, path in (destination_roots or store_roots).items()
    }
    if set(destinations) != set(roots):
        raise CleanroomRefusal("REFUSED_SOURCE_TOPOLOGY", "source/destination store IDs differ")
    if require_disjoint_destinations:
        destination_values = tuple(destinations.values())
        destination_collision_keys = [_filesystem_collision_key(path) for path in destination_values]
        if len(set(destination_collision_keys)) != len(destination_collision_keys):
            raise CleanroomRefusal(
                "REFUSED_SOURCE_TOPOLOGY", "destination roots collide under casefold/Unicode normalization"
            )
        if set(destination_collision_keys) & set(source_collision_keys):
            raise CleanroomRefusal(
                "REFUSED_SOURCE_TOPOLOGY", "source/destination roots collide under casefold/Unicode normalization"
            )
        for destination in destination_values:
            for source in root_values:
                if _normalized_component_overlap(destination, source):
                    raise CleanroomRefusal(
                        "REFUSED_SOURCE_TOPOLOGY",
                        "source/destination roots overlap by casefolded Unicode path components",
                        path=destination,
                    )
        for index, destination in enumerate(destination_values):
            for other in destination_values[index + 1 :]:
                if _normalized_component_overlap(destination, other):
                    raise CleanroomRefusal(
                        "REFUSED_SOURCE_TOPOLOGY",
                        "destination roots overlap by casefolded Unicode path components",
                        path=destination,
                    )
        for destination in destination_values:
            for source in root_values:
                if (
                    _relative_to_component_prefix(destination, source) is not None
                    or _relative_to_component_prefix(source, destination) is not None
                ):
                    raise CleanroomRefusal(
                        "REFUSED_SOURCE_TOPOLOGY",
                        "source and destination roots must be component-disjoint",
                        path=destination,
                    )
        for index, destination in enumerate(destination_values):
            for other in destination_values[index + 1 :]:
                if (
                    _relative_to_component_prefix(destination, other) is not None
                    or _relative_to_component_prefix(other, destination) is not None
                ):
                    raise CleanroomRefusal(
                        "REFUSED_SOURCE_TOPOLOGY", "destination roots must be component-disjoint", path=destination
                    )
    mapping = {roots[name]: destinations[name] for name in roots}
    allowed = tuple(_absolute_syntactic(Path(path)) for path in external_allowlist)
    for item in allowed:
        if any(
            _relative_to_component_prefix(item, root) is not None
            or _relative_to_component_prefix(root, item) is not None
            for root in roots.values()
        ):
            raise CleanroomRefusal(
                "REFUSED_SOURCE_TOPOLOGY", "external allowlist overlaps a protected store", path=item
            )

    root_fds: dict[str, int] = {}
    entries: list[TopologyEntry] = []
    known_kinds: dict[Path, str] = {path: "directory" for path in roots.values()}
    link_targets: dict[Path, str] = {}
    link_rows: dict[Path, int] = {}
    hardlink_first: dict[tuple[int, int], str] = {}
    hardlink_paths: dict[tuple[int, int], list[str]] = {}
    hardlink_expected: dict[tuple[int, int], int] = {}
    allowed_fds: list[int] = []
    try:
        for name, path in roots.items():
            root_fds[name] = _open_directory_nofollow(path)
        root_identities = [(int(os.fstat(fd).st_dev), int(os.fstat(fd).st_ino)) for fd in root_fds.values()]
        if len(set(root_identities)) != len(root_identities):
            raise CleanroomRefusal(
                "REFUSED_SOURCE_TOPOLOGY", "two configured roots alias the same physical directory"
            )
        root_chains = {name: _directory_identity_chain(roots[name]) for name in roots}
        for name, identity in zip(roots, root_identities, strict=True):
            for other_name, chain in root_chains.items():
                if name != other_name and identity in chain[:-1]:
                    raise CleanroomRefusal(
                        "REFUSED_SOURCE_TOPOLOGY",
                        f"physical root {name} is an inode ancestor of {other_name}",
                        path=roots[name],
                    )
        for item in allowed:
            allowed_fds.append(_open_directory_nofollow(item))
        for store_id in store_roots:
            root = roots[store_id]
            destination = destinations[store_id]
            stack: list[tuple[int, Path, Path]] = [(os.dup(root_fds[store_id]), Path("."), root)]
            while stack:
                directory_fd, relative_dir, display_dir = stack.pop()
                try:
                    names = sorted(os.listdir(directory_fd), key=os.fsencode, reverse=True)
                    pending_directories: list[tuple[int, Path, Path]] = []
                    for name in names:
                        relative = Path(name) if relative_dir == Path(".") else relative_dir / name
                        source_path = root / relative
                        try:
                            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        except OSError as exc:
                            raise CleanroomRefusal(
                                "REFUSED_SOURCE_DRIFT",
                                "entry disappeared during no-follow inventory",
                                path=source_path,
                                error_number=exc.errno,
                            ) from exc
                        kind = _entry_kind(info.st_mode)
                        known_kinds[source_path] = kind
                        if kind == "special":
                            raise CleanroomRefusal(
                                "REFUSED_SPECIAL_FILE", "socket/device/FIFO or unknown file kind", path=source_path
                            )
                        source_digest: str | None = None
                        raw_target: str | None = None
                        target_namespace: str | None = None
                        rewritten_target: str | None = None
                        hardlink_group: str | None = None
                        if kind == "directory":
                            child = _open_child_directory(directory_fd, name, source_path)
                            pending_directories.append((child, relative, source_path))
                        elif kind == "regular":
                            if info.st_nlink > 1:
                                key = (int(info.st_dev), int(info.st_ino))
                                logical_path = f"{store_id}:{relative.as_posix()}"
                                first = hardlink_first.setdefault(key, logical_path)
                                hardlink_group = first
                                hardlink_paths.setdefault(key, []).append(logical_path)
                                expected = hardlink_expected.setdefault(key, int(info.st_nlink))
                                if expected != int(info.st_nlink):
                                    raise CleanroomRefusal(
                                        "REFUSED_SOURCE_DRIFT",
                                        "hardlink nlink changed during inventory",
                                        path=source_path,
                                    )
                            if hash_regular_files:
                                flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                                try:
                                    file_fd = os.open(name, flags, dir_fd=directory_fd)
                                    with os.fdopen(file_fd, "rb", closefd=True) as stream:
                                        digest = hashlib.sha256()
                                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                            digest.update(chunk)
                                        source_digest = digest.hexdigest()
                                except OSError as exc:
                                    raise CleanroomRefusal(
                                        "REFUSED_SOURCE_DRIFT",
                                        "regular file could not be hashed without following",
                                        path=source_path,
                                        error_number=exc.errno,
                                    ) from exc
                        else:
                            raw_target = os.readlink(name, dir_fd=directory_fd)
                            raw_components = [
                                component
                                for component in raw_target.split(os.sep)
                                if component not in {"", "."}
                            ]
                            direct_first = (
                                Path(os.sep, *raw_components[:1])
                                if os.path.isabs(raw_target)
                                else source_path.parent / Path(*raw_components[:1])
                            )
                            if raw_components and (
                                direct_first == source_path or raw_target == str(source_path)
                            ):
                                raise CleanroomRefusal(
                                    "REFUSED_SYMLINK_CYCLE",
                                    f"direct componentwise self-cycle with target {raw_target!r}",
                                    path=source_path,
                                )
                            link_targets[source_path] = raw_target
                            link_rows[source_path] = len(entries)
                        entries.append(
                            TopologyEntry(
                                store_id=store_id,
                                path=relative.as_posix(),
                                kind=kind,
                                mode=stat.S_IMODE(info.st_mode),
                                size=int(info.st_size),
                                source_sha256=source_digest,
                                link_target=raw_target,
                                target_namespace=target_namespace,
                                rewritten_target=rewritten_target,
                                hardlink_group=hardlink_group,
                            )
                        )
                    stack.extend(pending_directories)
                finally:
                    os.close(directory_fd)

        for key, aliases in hardlink_paths.items():
            expected = hardlink_expected[key]
            if len(aliases) != expected:
                raise CleanroomRefusal(
                    "REFUSED_SOURCE_TOPOLOGY",
                    f"regular hardlink group is incomplete: inventoried={len(aliases)} nlink={expected} aliases={aliases}",
                    path=aliases[0],
                )
            stores = {alias.split(":", 1)[0] for alias in aliases}
            if len(stores) != 1:
                raise CleanroomRefusal(
                    "REFUSED_SOURCE_TOPOLOGY",
                    f"cross-store regular hardlink group is unsupported and cannot be silently split: {aliases}",
                    path=aliases[0],
                )

        for link_path in sorted(link_targets, key=lambda item: os.fsencode(str(item))):
            target = _resolve_symlink_target_components(
                link_path,
                links=link_targets,
                known_kinds=known_kinds,
                physical_roots=tuple(roots.values()),
                external_allowlist=allowed,
            )
            row_index = link_rows[link_path]
            row = entries[row_index]
            target_match = longest_prefix_mapping(target, mapping)
            if target_match is not None:
                _, destination_prefix, relative_target = target_match
                rewritten_absolute = destination_prefix / relative_target
                destination_link = destinations[row.store_id] / row.path
                rewritten_target = os.path.relpath(rewritten_absolute, start=destination_link.parent)
                namespace = "mapped_store"
                logical_target = f"@mapped/{relative_target.as_posix()}"
            else:
                namespace = "external_readonly"
                destination_link = destinations[row.store_id] / row.path
                rewritten_target = os.path.relpath(target, start=destination_link.parent)
                logical_target = "@external_readonly"
            entries[row_index] = replace(
                row,
                link_target=logical_target,
                target_namespace=namespace,
                rewritten_target=rewritten_target,
            )
    finally:
        for descriptor in allowed_fds:
            os.close(descriptor)
        for descriptor in root_fds.values():
            os.close(descriptor)

    manifest = stable_manifest(entries)
    return {
        "schema": SCHEMA,
        "state": "TOPOLOGY_PASS",
        "root_count": len(roots),
        "roots": [
            {"store_id": name, "source": str(roots[name]), "destination": str(destinations[name])}
            for name in store_roots
        ],
        "entry_count": len(entries),
        "symlink_count": sum(entry.kind == "symlink" for entry in entries),
        "regular_count": sum(entry.kind == "regular" for entry in entries),
        "directory_count": sum(entry.kind == "directory" for entry in entries),
        "manifest": manifest,
    }


CloneCall = Callable[[int, int, bytes, int], int | None]


def _darwin_fclonefileat(src_fd: int, dst_dir_fd: int, dst_name: bytes, flags: int) -> int:
    if sys.platform != "darwin":
        raise CleanroomRefusal("REFUSED_PLATFORM", "fclonefileat is Darwin-only")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        function = libc.fclonefileat
    except AttributeError as exc:
        raise CleanroomRefusal("REFUSED_PLATFORM", "libc has no fclonefileat") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    result = int(function(src_fd, dst_dir_fd, dst_name, flags))
    if result != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value), os.fsdecode(dst_name))
    return result


def clone_regular_file_cow(
    source_fd: int,
    destination_directory_fd: int,
    destination_name: str,
    *,
    clone_call: CloneCall | None = None,
) -> None:

    if destination_name in {"", ".", ".."} or "/" in destination_name or "\0" in destination_name:
        raise CleanroomRefusal("REFUSED_COW", "destination must be one safe path component", path=destination_name)
    try:
        source_info = os.fstat(source_fd)
    except OSError as exc:
        raise CleanroomRefusal("REFUSED_COW", "source fd is unreadable", error_number=exc.errno) from exc
    if not stat.S_ISREG(source_info.st_mode):
        raise CleanroomRefusal("REFUSED_COW", "source fd is not a regular file")
    operation = clone_call or _darwin_fclonefileat
    try:
        result = operation(source_fd, destination_directory_fd, os.fsencode(destination_name), CLONE_FLAGS)
        if result not in (0, None):
            raise OSError(errno.EIO, f"clone callback returned unexpected result {result}")
    except CleanroomRefusal:
        raise
    except OSError as exc:
        raise CleanroomRefusal(
            "REFUSED_COW",
            "fclonefileat failed; byte-copy fallback is forbidden",
            path=destination_name,
            error_number=exc.errno,
        ) from exc


def clone_or_link_regular_file(
    *,
    source_fd: int,
    source_stat: os.stat_result,
    destination_root_fd: int,
    destination_directory_fd: int,
    destination_relative: Path,
    hardlinks: MutableMapping[tuple[int, int], str],
    clone_call: CloneCall | None = None,
) -> str:

    key = (int(source_stat.st_dev), int(source_stat.st_ino))
    first = hardlinks.get(key) if source_stat.st_nlink > 1 else None
    if first is not None:
        try:
            os.link(
                first,
                destination_relative.name,
                src_dir_fd=destination_root_fd,
                dst_dir_fd=destination_directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise CleanroomRefusal(
                "REFUSED_COW", "clone-local hardlink creation failed", path=destination_relative, error_number=exc.errno
            ) from exc
        return "hardlink"
    clone_regular_file_cow(
        source_fd,
        destination_directory_fd,
        destination_relative.name,
        clone_call=clone_call,
    )
    if source_stat.st_nlink > 1:
        hardlinks[key] = destination_relative.as_posix()
    return "fclonefileat"


def _open_relative_directory(root_fd: int, relative: Path) -> int:
    descriptor = os.dup(root_fd)
    try:
        if relative == Path("."):
            return descriptor
        for component in relative.parts:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def materialize_stores_cow(
    store_roots: Mapping[str, Path | str],
    destination_roots: Mapping[str, Path | str],
    *,
    external_allowlist: Sequence[Path | str] = (),
    clone_call: CloneCall | None = None,
) -> dict[str, Any]:

    topology = inventory_topology(
        store_roots,
        destination_roots=destination_roots,
        external_allowlist=external_allowlist,
        hash_regular_files=True,
        require_disjoint_destinations=True,
    )
    sources = {name: _absolute_syntactic(Path(path)) for name, path in store_roots.items()}
    destinations = {name: _absolute_syntactic(Path(path)) for name, path in destination_roots.items()}
    source_identities: set[tuple[int, int]] = set()
    source_chains: list[tuple[tuple[int, int], ...]] = []
    for source in sources.values():
        descriptor = _open_directory_nofollow(source)
        try:
            info = os.fstat(descriptor)
            source_identities.add((int(info.st_dev), int(info.st_ino)))
        finally:
            os.close(descriptor)
        source_chains.append(_directory_identity_chain(source))
    for destination in destinations.values():
        _assert_path_absent_nofollow(destination)
        existing_chain = _existing_directory_identity_chain(destination)
        if any(source_identity in existing_chain for source_identity in source_identities):
            raise CleanroomRefusal(
                "REFUSED_COW",
                "an existing destination-parent inode is within a protected source root",
                path=destination,
            )

    destination_identities: set[tuple[int, int]] = set()
    destination_chains: list[tuple[tuple[int, int], ...]] = []
    for store_id, destination in destinations.items():
        descriptor = _mkdir_absolute_nofollow(destination)
        try:
            info = os.fstat(descriptor)
            identity = (int(info.st_dev), int(info.st_ino))
            if identity in source_identities or identity in destination_identities:
                raise CleanroomRefusal(
                    "REFUSED_COW", "destination root aliases a source or sibling destination", path=destination
                )
            destination_identities.add(identity)
        finally:
            os.close(descriptor)
        destination_chain = _directory_identity_chain(destination)
        if any(source_identity in destination_chain for source_identity in source_identities):
            raise CleanroomRefusal(
                "REFUSED_COW", "destination is physically nested beneath a source inode", path=destination
            )
        if any(identity in source_chain for source_chain in source_chains):
            raise CleanroomRefusal(
                "REFUSED_COW", "destination inode physically contains or aliases a source path", path=destination
            )
        if any(identity in chain or chain[-1] in destination_chain for chain in destination_chains):
            raise CleanroomRefusal(
                "REFUSED_COW", "destination roots are physically nested or aliased", path=destination
            )
        destination_chains.append(destination_chain)

    rows_by_store: dict[str, list[dict[str, Any]]] = {name: [] for name in sources}
    for row in topology["manifest"]["entries"]:
        rows_by_store[str(row["store_id"])].append(row)

    clone_calls = 0
    hardlink_calls = 0
    symlink_calls = 0
    for store_id in store_roots:
        source_root_fd = _open_directory_nofollow(sources[store_id])
        destination_root_fd = _open_directory_nofollow(destinations[store_id])
        hardlinks: dict[tuple[int, int], str] = {}
        try:
            directories = sorted(
                (row for row in rows_by_store[store_id] if row["kind"] == "directory"),
                key=lambda row: (len(Path(row["path"]).parts), os.fsencode(row["path"])),
            )
            for row in directories:
                relative = Path(row["path"])
                parent_fd = _open_relative_directory(destination_root_fd, relative.parent)
                try:
                    os.mkdir(relative.name, int(row["mode"]), dir_fd=parent_fd)
                except OSError as exc:
                    raise CleanroomRefusal(
                        "REFUSED_COW", "destination directory creation failed", path=relative, error_number=exc.errno
                    ) from exc
                finally:
                    os.close(parent_fd)

            non_directories = sorted(
                (row for row in rows_by_store[store_id] if row["kind"] != "directory"),
                key=lambda row: os.fsencode(row["path"]),
            )
            for row in non_directories:
                relative = Path(row["path"])
                source_parent_fd = _open_relative_directory(source_root_fd, relative.parent)
                destination_parent_fd = _open_relative_directory(destination_root_fd, relative.parent)
                try:
                    if row["kind"] == "symlink":
                        target = str(row["rewritten_target"] or row["link_target"])
                        os.symlink(target, relative.name, dir_fd=destination_parent_fd)
                        symlink_calls += 1
                        continue
                    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                    source_fd = os.open(relative.name, flags, dir_fd=source_parent_fd)
                    try:
                        source_info = os.fstat(source_fd)
                        method = clone_or_link_regular_file(
                            source_fd=source_fd,
                            source_stat=source_info,
                            destination_root_fd=destination_root_fd,
                            destination_directory_fd=destination_parent_fd,
                            destination_relative=relative,
                            hardlinks=hardlinks,
                            clone_call=clone_call,
                        )
                        destination_info = os.stat(
                            relative.name,
                            dir_fd=destination_parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            int(destination_info.st_dev),
                            int(destination_info.st_ino),
                        ) == (int(source_info.st_dev), int(source_info.st_ino)):
                            raise CleanroomRefusal(
                                "REFUSED_COW",
                                "destination inode aliases the canonical source instead of an independent COW inode",
                                path=relative,
                            )
                    finally:
                        os.close(source_fd)
                    if method == "hardlink":
                        hardlink_calls += 1
                    else:
                        clone_calls += 1
                except CleanroomRefusal:
                    raise
                except OSError as exc:
                    raise CleanroomRefusal(
                        "REFUSED_COW", "fixture materialization failed", path=relative, error_number=exc.errno
                    ) from exc
                finally:
                    os.close(destination_parent_fd)
                    os.close(source_parent_fd)
        finally:
            os.close(destination_root_fd)
            os.close(source_root_fd)

    source_topology_after = inventory_topology(
        store_roots,
        destination_roots=destination_roots,
        external_allowlist=external_allowlist,
        hash_regular_files=True,
        require_disjoint_destinations=True,
    )
    if source_topology_after["manifest"]["sha256"] != topology["manifest"]["sha256"]:
        raise CleanroomRefusal(
            "REFUSED_SOURCE_DRIFT",
            "second source census found additions, removals, topology changes, or content changes",
        )
    destination_topology = inventory_topology(
        destination_roots,
        destination_roots=destination_roots,
        external_allowlist=external_allowlist,
        hash_regular_files=True,
    )
    source_rows = topology["manifest"]["entries"]
    destination_rows = destination_topology["manifest"]["entries"]
    source_content = {
        (row["store_id"], row["path"]): row["source_sha256"]
        for row in source_rows
        if row["kind"] == "regular"
    }
    destination_content = {
        (row["store_id"], row["path"]): row["source_sha256"]
        for row in destination_rows
        if row["kind"] == "regular"
    }
    if source_content != destination_content:
        raise CleanroomRefusal("REFUSED_SOURCE_DRIFT", "source/destination content hashes differ")
    def semantic_projection(rows: Sequence[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
        projected = [
            (
                row["store_id"],
                row["path"],
                row["kind"],
                row["mode"],
                row["source_sha256"],
                row["target_namespace"],
                row["rewritten_target"],
                row["hardlink_group"],
            )
            for row in rows
        ]
        return sorted(projected, key=lambda row: (str(row[0]).encode(), os.fsencode(str(row[1]))))

    if semantic_projection(source_rows) != semantic_projection(destination_rows):
        raise CleanroomRefusal(
            "REFUSED_SOURCE_DRIFT",
            "destination topology or relocated symlink semantics differ from the stable source manifest",
        )
    return {
        "schema": SCHEMA,
        "state": "FIXTURE_PASS",
        "source_manifest_sha256": topology["manifest"]["sha256"],
        "source_manifest_after_sha256": source_topology_after["manifest"]["sha256"],
        "source_manifest_stable": True,
        "destination_manifest_sha256": destination_topology["manifest"]["sha256"],
        "content_hashes_equal": True,
        "clone_calls": clone_calls,
        "hardlink_calls": hardlink_calls,
        "symlink_calls": symlink_calls,
    }


def backup_sqlite_online(source: Path, destination: Path) -> dict[str, Any]:

    try:
        source_info = os.lstat(source)
    except OSError as exc:
        raise CleanroomRefusal(
            "REFUSED_SQLITE_BACKUP", "SQLite source is unreadable", path=source, error_number=exc.errno
        ) from exc
    if not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
        raise CleanroomRefusal("REFUSED_SQLITE_BACKUP", "SQLite source must be a physical regular file", path=source)
    if destination.exists() or destination.is_symlink():
        raise CleanroomRefusal("REFUSED_SQLITE_BACKUP", "SQLite destination must not exist", path=destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{quote(str(source))}?mode=ro"
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source_uri, uri=True, timeout=5.0)
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection, pages=1024, sleep=0.01)
        destination_connection.commit()
        quick_check = str(destination_connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise RuntimeError(f"quick_check={quick_check!r}")
        page_count = int(destination_connection.execute("PRAGMA page_count").fetchone()[0])
        schema_version = int(destination_connection.execute("PRAGMA schema_version").fetchone()[0])
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise CleanroomRefusal(
            "REFUSED_SQLITE_BACKUP", f"online SQLite backup failed: {type(exc).__name__}: {exc}", path=source
        ) from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    return {
        "method": "sqlite3.Connection.backup",
        "source": str(source),
        "destination": str(destination),
        "destination_sha256": sha256_file(destination),
        "destination_bytes": destination.stat().st_size,
        "quick_check": "ok",
        "page_count": page_count,
        "schema_version": schema_version,
    }


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ):
        environment.pop(key, None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def validate_physical_git_checkout(
    checkout: Path,
    *,
    candidate_ref: str,
    expected_oid: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:

    if not candidate_ref.startswith("refs/") or candidate_ref.endswith("/"):
        raise CleanroomRefusal("REFUSED_CANDIDATE_REF", "candidate ref must be fully qualified")
    if len(expected_oid) != 40 or any(character not in "0123456789abcdef" for character in expected_oid):
        raise CleanroomRefusal("REFUSED_CANDIDATE_REF", "expected OID must be lowercase 40-hex")
    git_dir = checkout / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise CleanroomRefusal("REFUSED_GIT_CHECKOUT", ".git must be a physical checkout-local directory", path=git_dir)
    alternates = git_dir / "objects/info/alternates"
    if alternates.exists() or alternates.is_symlink():
        raise CleanroomRefusal("REFUSED_GIT_CHECKOUT", "Git alternates are forbidden", path=alternates)

    def git(*arguments: str) -> str:
        completed = runner(
            ["git", "-C", str(checkout), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=_git_environment(),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
            raise CleanroomRefusal("REFUSED_GIT_CHECKOUT", f"git {' '.join(arguments)} failed: {detail}")
        return completed.stdout.strip()

    head = git("rev-parse", "HEAD")
    ref_oid = git("rev-parse", candidate_ref)
    if head != expected_oid or ref_oid != expected_oid:
        raise CleanroomRefusal(
            "REFUSED_CANDIDATE_REF", f"candidate mismatch: HEAD={head}, ref={ref_oid}, expected={expected_oid}"
        )
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise CleanroomRefusal("REFUSED_GIT_CHECKOUT", "candidate checkout is dirty")
    git("fsck", "--no-progress", "--connectivity-only")
    return {
        "candidate_ref": candidate_ref,
        "candidate_oid": expected_oid,
        "git_dir_physical": True,
        "alternates_absent": True,
        "worktree_clean": True,
    }


def render_venv_python_shim(
    *,
    shared_python: Path,
    clone_source: Path,
    shared_site_packages: Path,
) -> str:

    python = shlex.quote(str(shared_python))
    pythonpath = shlex.quote(f"{clone_source}{os.pathsep}{shared_site_packages}")
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "export PYTHONNOUSERSITE=1\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        "export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1\n"
        f"export PYTHONPATH={pythonpath}\n"
        f"exec {python} -S \"$@\"\n"
    )


def write_venv_python_shim(
    shim_path: Path,
    *,
    shared_python: Path,
    clone_source: Path,
    shared_site_packages: Path,
) -> str:
    if shim_path.exists() or shim_path.is_symlink():
        raise CleanroomRefusal("REFUSED_VENV_LEAK", "venv shim destination must not exist", path=shim_path)
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    payload = render_venv_python_shim(
        shared_python=shared_python,
        clone_source=clone_source,
        shared_site_packages=shared_site_packages,
    )
    shim_path.write_text(payload, encoding="utf-8")
    shim_path.chmod(0o755)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_venv_probe_payload(
    payload: Mapping[str, Any],
    *,
    clone_root: Path,
    canonical_root: Path,
    shared_venv_root: Path | None = None,
) -> dict[str, Any]:
    raw_module = payload.get("coord_file")
    if not isinstance(raw_module, str) or not raw_module:
        raise CleanroomRefusal("REFUSED_VENV_LEAK", "probe omitted an explicit coord_file")
    module_file = _absolute_syntactic(Path(raw_module))
    clone = _absolute_syntactic(clone_root)
    canonical = _absolute_syntactic(canonical_root)
    clone_fd: int | None = None
    canonical_fd: int | None = None
    try:
        clone_fd = _open_directory_nofollow(clone)
        canonical_fd = _open_directory_nofollow(canonical)
    except CleanroomRefusal as exc:
        if clone_fd is not None:
            os.close(clone_fd)
        raise CleanroomRefusal(
            "REFUSED_VENV_LEAK", f"clone/canonical root failed no-follow validation: {exc.detail}", path=exc.path
        ) from exc
    try:
        assert clone_fd is not None and canonical_fd is not None
        clone_identity = (int(os.fstat(clone_fd).st_dev), int(os.fstat(clone_fd).st_ino))
        canonical_identity = (int(os.fstat(canonical_fd).st_dev), int(os.fstat(canonical_fd).st_ino))
    finally:
        os.close(canonical_fd)
        os.close(clone_fd)
    clone_real = Path(os.path.realpath(clone))
    canonical_real = Path(os.path.realpath(canonical))
    if (
        clone_identity == canonical_identity
        or _relative_to_component_prefix(clone_real, canonical_real) is not None
        or _relative_to_component_prefix(canonical_real, clone_real) is not None
    ):
        raise CleanroomRefusal("REFUSED_VENV_LEAK", "clone and canonical roots are not physically disjoint")
    try:
        module_info = os.lstat(module_file)
    except OSError as exc:
        raise CleanroomRefusal(
            "REFUSED_VENV_LEAK", "reported coord_file does not exist", path=module_file, error_number=exc.errno
        ) from exc
    if stat.S_ISLNK(module_info.st_mode) or not stat.S_ISREG(module_info.st_mode):
        raise CleanroomRefusal("REFUSED_VENV_LEAK", "reported coord_file must be a physical regular file", path=module_file)
    module_real = Path(os.path.realpath(module_file))
    if _relative_to_component_prefix(module_real, clone_real) is None:
        raise CleanroomRefusal("REFUSED_VENV_LEAK", f"coordharness import is not clone-local: {module_file}")
    relative_module = module_real.relative_to(clone_real)
    canonical_counterpart = canonical_real / relative_module
    if canonical_counterpart.exists():
        counterpart_info = os.stat(canonical_counterpart, follow_symlinks=False)
        if (int(counterpart_info.st_dev), int(counterpart_info.st_ino)) == (
            int(module_info.st_dev),
            int(module_info.st_ino),
        ):
            raise CleanroomRefusal("REFUSED_VENV_LEAK", "clone module hardlinks canonical source", path=module_file)
    sys_path = payload.get("sys_path")
    if not isinstance(sys_path, list) or not all(isinstance(item, str) for item in sys_path):
        raise CleanroomRefusal("REFUSED_VENV_LEAK", "probe sys_path must be a list of strings")
    if payload.get("pytest_disable_plugin_autoload") != "1":
        raise CleanroomRefusal("REFUSED_VENV_LEAK", "PYTEST_DISABLE_PLUGIN_AUTOLOAD is not pinned to 1")
    loaded_modules = payload.get("loaded_modules")
    if not isinstance(loaded_modules, dict) or loaded_modules.get("coordharness") != raw_module:
        raise CleanroomRefusal("REFUSED_VENV_LEAK", "loaded_modules must bind coordharness to the explicit coord_file")
    shared_real = (
        Path(os.path.realpath(_absolute_syntactic(shared_venv_root)))
        if shared_venv_root is not None
        else None
    )
    leaked: list[str] = []
    for item in sys_path:
        text = item
        if not text:
            continue
        syntactic = _absolute_syntactic(Path(text))
        real = Path(os.path.realpath(syntactic)) if os.path.lexists(syntactic) else syntactic
        under_shared = shared_real is not None and _relative_to_component_prefix(real, shared_real) is not None
        if not under_shared and (
            _relative_to_component_prefix(syntactic, canonical) is not None
            or _relative_to_component_prefix(real, canonical_real) is not None
        ):
            leaked.append(text)
    for module_name, module_path in loaded_modules.items():
        if not isinstance(module_name, str) or not isinstance(module_path, str) or not module_path:
            raise CleanroomRefusal("REFUSED_VENV_LEAK", "loaded module paths must be nonempty strings")
        real = Path(os.path.realpath(_absolute_syntactic(Path(module_path))))
        in_clone = _relative_to_component_prefix(real, clone_real) is not None
        in_shared = shared_real is not None and _relative_to_component_prefix(real, shared_real) is not None
        if not in_clone and not in_shared:
            raise CleanroomRefusal(
                "REFUSED_VENV_LEAK", f"loaded module {module_name!r} is outside clone/shared roots: {real}"
            )
    if leaked:
        raise CleanroomRefusal("REFUSED_VENV_LEAK", f"canonical source leaked through sys.path: {leaked}")
    return {"clone_local_coordharness": str(module_real), "canonical_sys_path_entries": []}


def validate_sandbox_roots(*, canonical_root: Path, cleanroom_root: Path) -> dict[str, Any]:
    canonical = _absolute_syntactic(canonical_root)
    cleanroom = _absolute_syntactic(cleanroom_root)
    canonical_fd = _open_directory_nofollow(canonical)
    cleanroom_fd = _open_directory_nofollow(cleanroom)
    try:
        canonical_identity = (int(os.fstat(canonical_fd).st_dev), int(os.fstat(canonical_fd).st_ino))
        cleanroom_identity = (int(os.fstat(cleanroom_fd).st_dev), int(os.fstat(cleanroom_fd).st_ino))
    finally:
        os.close(cleanroom_fd)
        os.close(canonical_fd)
    canonical_real = Path(os.path.realpath(canonical))
    cleanroom_real = Path(os.path.realpath(cleanroom))
    if (
        canonical_identity == cleanroom_identity
        or _relative_to_component_prefix(canonical_real, cleanroom_real) is not None
        or _relative_to_component_prefix(cleanroom_real, canonical_real) is not None
    ):
        raise CleanroomRefusal(
            "REFUSED_SANDBOX_RED_ARM", "canonical and cleanroom roots are not physically disjoint"
        )
    return {
        "canonical_root": str(canonical_real),
        "cleanroom_root": str(cleanroom_real),
        "physically_disjoint": True,
    }


def sandbox_profile_text() -> str:
    return """(version 1)
(deny default)
(allow process*)
(allow mach-lookup)
(allow sysctl-read)
(allow file-read* (subpath (param \"CLEANROOM_ROOT\")))
(allow file-read* (subpath (param \"SHARED_VENV\")))
(allow file-read* (subpath \"/System\"))
(allow file-read* (subpath \"/usr\"))
(allow file-read* (subpath \"/Library\"))
(allow file-read* (subpath \"/private/etc\"))
(allow file-read* (subpath \"/dev\"))
(allow file-write* (literal \"/dev/null\"))
(allow file-write* (subpath (param \"CLEANROOM_ROOT\")))
(deny file-read*
  (require-all
    (subpath (param \"CANONICAL_ROOT\"))
    (require-not (subpath (param \"SHARED_VENV\")))))
(deny file-write* (subpath (param \"CANONICAL_ROOT\")))
(deny network*)
"""


def red_arm_probe(
    protected_files: Sequence[Path],
    writable_root: Path,
    *,
    canonical_root: Path,
) -> dict[str, Any]:

    disjoint = validate_sandbox_roots(canonical_root=canonical_root, cleanroom_root=writable_root)
    canonical_real = Path(disjoint["canonical_root"])
    rows: list[dict[str, Any]] = []
    for path in protected_files:
        protected_real = Path(os.path.realpath(path))
        if _relative_to_component_prefix(protected_real, canonical_real) is None:
            raise CleanroomRefusal(
                "REFUSED_SANDBOX_RED_ARM", "protected red-arm file is outside CANONICAL_ROOT", path=path
            )
        try:
            descriptor = os.open(path, os.O_WRONLY)
        except OSError as exc:
            rows.append({"path": str(path), "errno": exc.errno, "errno_name": errno.errorcode.get(exc.errno)})
            if exc.errno != errno.EPERM:
                raise CleanroomRefusal(
                    "REFUSED_SANDBOX_RED_ARM",
                    f"protected O_WRONLY returned {errno.errorcode.get(exc.errno, exc.errno)}, expected EPERM",
                    path=path,
                    error_number=exc.errno,
                ) from exc
        else:
            os.close(descriptor)
            raise CleanroomRefusal(
                "REFUSED_SANDBOX_RED_ARM", "protected O_WRONLY unexpectedly succeeded", path=path
            )
    probe = writable_root / ".sandbox-write-probe"
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, b"clone-local\n")
        os.close(descriptor)
    except OSError as exc:
        raise CleanroomRefusal(
            "REFUSED_SANDBOX_RED_ARM", "clone-local positive write arm failed", path=probe, error_number=exc.errno
        ) from exc
    return {"protected_writes": rows, "clone_local_write": "PASS", "root_disjointness": disjoint}


def zero_mutation_counters() -> dict[str, int]:
    return {
        "clone_calls": 0,
        "checkout_calls": 0,
        "sqlite_backup_calls": 0,
        "sandbox_launch_calls": 0,
        "pytest_launch_calls": 0,
    }


def create_one_level_physical_link(link: Path, physical_target: Path) -> dict[str, Any]:

    link = _absolute_syntactic(link)
    target = _absolute_syntactic(physical_target)
    if link.exists() or link.is_symlink():
        raise CleanroomRefusal("REFUSED_ATTEMPT_REUSE", "cleanroom link already exists", path=link)
    target_info = os.lstat(target)
    if not stat.S_ISDIR(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
        raise CleanroomRefusal(
            "REFUSED_SOURCE_TOPOLOGY",
            "one-level cleanroom target must be a physical directory",
            path=target,
        )
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.fspath(target), link, target_is_directory=True)
    return {
        "link": str(link),
        "raw_target": os.readlink(link),
        "physical_target": str(target),
        "one_hop": os.readlink(link) == str(target) and not target.is_symlink(),
        "target_device": int(target_info.st_dev),
        "target_inode": int(target_info.st_ino),
        "access_posture": "PRELAUNCH_MEASUREMENT_ONLY_NO_CHILD_EXECUTION",
    }


__all__ = [
    "CLONE_FLAGS",
    "CleanroomRefusal",
    "DEFAULT_STORE_SPECS",
    "SCHEMA",
    "StoreSpec",
    "TERMINAL_STATES",
    "TopologyEntry",
    "backup_sqlite_online",
    "clone_or_link_regular_file",
    "clone_regular_file_cow",
    "create_one_level_physical_link",
    "inventory_topology",
    "longest_prefix_mapping",
    "materialize_stores_cow",
    "red_arm_probe",
    "render_venv_python_shim",
    "rewritten_symlink_target",
    "sandbox_profile_text",
    "sha256_file",
    "stable_manifest",
    "validate_physical_git_checkout",
    "validate_sandbox_roots",
    "validate_venv_probe_payload",
    "write_venv_python_shim",
    "zero_mutation_counters",
]
