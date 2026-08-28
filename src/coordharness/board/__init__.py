"""Dependency-free read-only board and native snapshot contract."""

from .snapshot import NATIVE_SNAPSHOT_SCHEMA, build_snapshot, validate_snapshot

__all__ = ["NATIVE_SNAPSHOT_SCHEMA", "build_snapshot", "validate_snapshot"]
