"""Value-redacted MCP configuration inventory and security checks."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable


_SECRET_KEY = re.compile(
    r"(?:^|[_-])(auth|bearer|cookie|credential|key|pass(?:word|wd)?|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_KNOWN_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----)"
)
_ENV_REFERENCE = re.compile(
    r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|"
    r"(?:env|environment):[A-Za-z_][A-Za-z0-9_]*)$",
    re.IGNORECASE,
)
_SECRET_FLAG = re.compile(
    r"^--?(?:api[-_]?key|auth|bearer|cookie|credential|password|secret|token)(?:=|$)",
    re.IGNORECASE,
)
_URI_CREDENTIAL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+:[^/@\s]+@")


@dataclass(frozen=True)
class McpServer:
    source: str
    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]

    def redacted(self) -> dict[str, Any]:
        command = Path(self.command).name if self.command else ""
        return {
            "source": self.source,
            "name": self.name,
            "command": command if not _looks_secret_literal(command) else "<redacted>",
            "args": ["<redacted>" for _ in self.args],
            "env": {key: "<redacted>" for key in sorted(self.env)},
        }


@dataclass(frozen=True)
class McpIssue:
    code: str
    source: str
    server: str
    field: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "source": self.source,
            "server": self.server,
            "field": self.field,
        }


class McpConfigError(ValueError):
    pass


def _looks_secret_literal(value: str) -> bool:
    text = str(value).strip()
    if not text or _ENV_REFERENCE.fullmatch(text):
        return False
    if _KNOWN_SECRET.search(text) or _URI_CREDENTIAL.search(text):
        return True
    if len(text) >= 24 and re.fullmatch(r"[A-Za-z0-9_./+=:-]+", text):
        classes = sum(
            bool(pattern.search(text))
            for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"))
        )
        try:
            is_path = Path(text).exists()
        except OSError:
            is_path = False
        return classes >= 2 and not is_path
    return False


def _server_mapping(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("mcpServers", "mcp_servers"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    mcp = payload.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
        return mcp["servers"]
    return {}


def read_config(path: Path, *, source: str, max_bytes: int = 1_000_000) -> list[McpServer]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise McpConfigError("MCP configuration is not readable") from exc
    if size > max_bytes:
        raise McpConfigError("MCP configuration exceeds the size limit")
    try:
        text = path.read_text(encoding="utf-8")
        payload = tomllib.loads(text) if path.suffix.lower() == ".toml" else json.loads(text)
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise McpConfigError("MCP configuration is invalid") from exc

    records: list[McpServer] = []
    for name, raw in sorted(_server_mapping(payload).items()):
        if not isinstance(raw, dict):
            continue
        args_raw = raw.get("args") or []
        env_raw = raw.get("env") or {}
        args = tuple(str(item) for item in args_raw) if isinstance(args_raw, list) else ()
        env = (
            {str(key): str(value) for key, value in env_raw.items()}
            if isinstance(env_raw, dict)
            else {}
        )
        records.append(
            McpServer(
                source=source,
                name=str(name),
                command=str(raw.get("command") or ""),
                args=args,
                env=env,
            )
        )
    return records


def security_issues(records: Iterable[McpServer]) -> list[McpIssue]:
    issues: list[McpIssue] = []
    for record in records:
        command_name = Path(record.command).name
        if command_name in {"bash", "sh", "zsh"} and "-c" in record.args:
            issues.append(McpIssue("mcp.shell_command", record.source, record.name, "command"))
        if command_name == "npx" and any("@latest" in arg for arg in record.args):
            issues.append(McpIssue("mcp.unpinned_package", record.source, record.name, "args"))

        prior_secret_flag = False
        for argument in record.args:
            secret_flag = bool(_SECRET_FLAG.match(argument))
            literal = argument.split("=", 1)[1] if secret_flag and "=" in argument else argument
            if (prior_secret_flag or secret_flag) and literal and not _ENV_REFERENCE.fullmatch(literal):
                issues.append(McpIssue("mcp.literal_secret", record.source, record.name, "args"))
            elif _looks_secret_literal(argument):
                issues.append(McpIssue("mcp.literal_secret", record.source, record.name, "args"))
            prior_secret_flag = secret_flag and "=" not in argument

        for key, value in record.env.items():
            if (_SECRET_KEY.search(key) and not _ENV_REFERENCE.fullmatch(value.strip())) or (
                _looks_secret_literal(value)
            ):
                issues.append(McpIssue("mcp.literal_secret", record.source, record.name, f"env:{key}"))
    unique = {(item.code, item.source, item.server, item.field): item for item in issues}
    return [unique[key] for key in sorted(unique)]


def redacted_inventory(records: Iterable[McpServer]) -> list[dict[str, Any]]:
    return [record.redacted() for record in records]
