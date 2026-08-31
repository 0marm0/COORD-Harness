'''Portable agent configuration and clean-room onboarding verification.'''
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from coordharness.bootstrap import database_current
from coordharness.safety.mcp import read_config, security_issues

REPORT_SCHEMA = 'coordharness.onboarding.v1'
PASS = 'PASS'
BLOCKED = 'BLOCKED'
SKIPPED = 'SKIPPED'
INSTRUCTION_SENTINEL = 'COORDHARNESS_AGENT_INSTRUCTION_SENTINEL=v1'
_CORE_MCP_TOOLS = {'preflight', 'board', 'next_work', 'work_context', 'claim_work', 'complete'}


def codex_config_text() -> str:
    return '''# Portable project-scoped MCP configuration. The project-local virtual environment
# avoids reliance on an activated shell or a GUI application's PATH.
[mcp_servers.coordharness]
command = "./scripts/coord-mcp-launch.sh"
cwd = "."
startup_timeout_sec = 20
tool_timeout_sec = 60

[mcp_servers.coordharness.env]
COORD_PROJECT_ROOT = "."
COORD_DB = ".coordharness/coord.db"
COORD_KNOWLEDGE_DB = ".coordharness/knowledge.db"
COORD_DEPLOYMENT_PROFILE = "generic"
COORD_ACTOR = "codex"
'''


def claude_config_text() -> str:
    return json.dumps({'mcpServers': {'coordharness': {
        'type': 'stdio',
        'command': './scripts/coord-mcp-launch.sh',
        'env': {
            'COORD_PROJECT_ROOT': '.',
            'COORD_DB': '.coordharness/coord.db',
            'COORD_KNOWLEDGE_DB': '.coordharness/knowledge.db',
            'COORD_DEPLOYMENT_PROFILE': 'generic',
            'COORD_ACTOR': 'claude',
        },
    }}}, indent=2, sort_keys=True) + '\n'


def write_portable_configs(project_root: Path) -> dict[str, Any]:
    '''Create project configs without replacing user-owned configuration.'''
    root = project_root.expanduser().resolve(strict=True)
    values = {
        root / '.codex' / 'config.toml': codex_config_text(),
        root / '.mcp.json': claude_config_text(),
    }
    created: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for path, text in values.items():
        relative = path.relative_to(root).as_posix()
        if path.exists():
            if path.is_file() and path.read_text(encoding='utf-8') == text:
                unchanged.append(relative)
            else:
                conflicts.append(relative)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        created.append(relative)
    return {'created': created, 'unchanged': unchanged, 'conflicts': conflicts, 'ok': not conflicts}


def _finding(finding_id: str, status: str, summary: str, **details: Any) -> dict[str, Any]:
    return {'id': finding_id, 'status': status, 'summary': summary, 'details': details}


def _check_instructions_and_skills(root: Path) -> dict[str, Any]:
    required = [
        root / 'AGENTS.md', root / 'CLAUDE.md', root / 'docs' / 'agent-onboarding.md',
        root / 'docs' / 'agent-protocol.md', root / 'docs' / 'context-architecture.md',
        root / 'docs' / 'context-and-memory.md', root / 'docs' / 'jobs-and-runs.md',
        root / '.agents' / 'skills' / 'operating-coordharness' / 'SKILL.md',
        root / '.claude' / 'skills' / 'operating-coordharness' / 'SKILL.md',
    ]
    missing = [path.relative_to(root).as_posix() for path in required if not path.is_file()]
    oversized = [path.name for path in (root / 'AGENTS.md', root / 'CLAUDE.md')
                 if path.is_file() and path.stat().st_size > 12_000]
    sentinel_missing = [path.name for path in (root / 'AGENTS.md', root / 'CLAUDE.md')
                        if path.is_file() and INSTRUCTION_SENTINEL not in path.read_text(encoding='utf-8')]
    codex_skill = root / '.agents' / 'skills' / 'operating-coordharness'
    claude_skill = root / '.claude' / 'skills' / 'operating-coordharness'
    mirror_equal = False
    if codex_skill.is_dir() and claude_skill.is_dir():
        left = {p.relative_to(codex_skill): p.read_bytes() for p in codex_skill.rglob('*') if p.is_file()}
        right = {p.relative_to(claude_skill): p.read_bytes() for p in claude_skill.rglob('*') if p.is_file()}
        mirror_equal = left == right
    blocked = bool(missing or oversized or sentinel_missing or not mirror_equal)
    return _finding('onboarding.instructions_skills', BLOCKED if blocked else PASS,
        'root instructions and mirrored operating skills are discoverable' if not blocked
        else 'instruction or skill discovery wiring is incomplete',
        missing=missing, oversized=oversized, sentinel_missing=sentinel_missing,
        skill_mirrors_equal=mirror_equal)


def _resolve_server_command(root: Path, command: str) -> Path | None:
    raw = Path(command)
    candidate = raw if raw.is_absolute() else root / raw
    try:
        launch_path = Path(os.path.abspath(candidate))
        if not raw.is_absolute():
            launch_path.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    if not launch_path.is_file() or not os.access(launch_path, os.X_OK):
        # A shim that resolves and exists but lacks the executable bit fails every
        # client launch with a raw EACCES; treat it identically to "not found" so
        # the doctor blocks here with a clear problem code instead of reporting
        # PASS on a command that cannot actually run.
        return None
    # Preserve a virtual-environment launcher path rather than its interpreter
    # symlink target; Python derives sys.prefix from argv[0].
    return launch_path


def _check_configs(root: Path) -> tuple[dict[str, Any], Any | None]:
    paths = (root / '.codex' / 'config.toml', root / '.mcp.json')
    records = []
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            problems.append(f'missing:{path.relative_to(root).as_posix()}')
            continue
        try:
            records.extend(read_config(path, source=path.relative_to(root).as_posix()))
        except Exception:
            problems.append(f'invalid:{path.relative_to(root).as_posix()}')
    coord_records = [record for record in records if record.name == 'coordharness']
    if len(coord_records) != 2:
        problems.append('coordharness_server_count')
    expected = {'COORD_PROJECT_ROOT': '.', 'COORD_DB': '.coordharness/coord.db',
                'COORD_DEPLOYMENT_PROFILE': 'generic'}
    for record in coord_records:
        if _resolve_server_command(root, record.command) is None:
            problems.append(f'command_unavailable:{record.source}')
        if Path(record.command).is_absolute():
            problems.append(f'machine_specific_command:{record.source}')
        if any(record.env.get(key) != value for key, value in expected.items()):
            problems.append(f'nonportable_env:{record.source}')
        # The tracked launch command is a checked-in shim (scripts/coord-mcp-launch.sh)
        # that always exists post-clone; it fails closed with an instruction, not an
        # ENOENT, when the venv it execs into is missing. Check the venv directly so an
        # unset-up clone still reads BLOCKED here rather than a false PASS.
        if not (root / '.venv' / 'bin' / 'python').is_file():
            problems.append(f'runtime_missing:{record.source}')
    problems.extend(issue.code for issue in security_issues(records))
    unique = sorted(set(problems))
    codex_record = next((r for r in coord_records if r.source == '.codex/config.toml'), None)
    return _finding('onboarding.agent_configs', BLOCKED if unique else PASS,
        'Codex and Claude use portable project-local stdio configuration' if not unique
        else 'agent MCP configuration is incomplete or nonportable',
        config_count=len(paths), server_count=len(coord_records), problem_codes=unique), codex_record


async def _probe_mcp_async(root: Path, record: Any) -> tuple[list[str], bool]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    command = _resolve_server_command(root, record.command)
    if command is None:
        raise RuntimeError('configured MCP command is unavailable')
    env = dict(os.environ)
    env.update(record.env)
    params = StdioServerParameters(command=str(command), args=list(record.args), env=env, cwd=root)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            result = await session.call_tool('preflight',
                {'actor': 'codex', 'session_id': 'codex:onboarding-doctor'})
            return names, not bool(getattr(result, 'isError', False))


def _check_mcp_probe(root: Path, record: Any | None) -> dict[str, Any]:
    try:
        if record is None:
            raise RuntimeError('valid Codex project configuration is unavailable')
        names, preflight_ok = asyncio.run(asyncio.wait_for(_probe_mcp_async(root, record), timeout=20.0))
        missing = sorted(_CORE_MCP_TOOLS - set(names))
        blocked = bool(missing or not preflight_ok)
        return _finding('onboarding.mcp_stdio', BLOCKED if blocked else PASS,
            'configured MCP server listed core tools and answered preflight' if not blocked
            else 'configured MCP server failed list or preflight verification',
            tool_count=len(names), missing_tools=missing, preflight_ok=preflight_ok)
    except Exception as exc:
        return _finding('onboarding.mcp_stdio', BLOCKED,
            'configured MCP server could not complete a bounded stdio probe',
            error_type=type(exc).__name__, tool_count=0,
            missing_tools=sorted(_CORE_MCP_TOOLS), preflight_ok=False)


def client_registration_command(root: Path, client: str) -> list[str]:
    """Return the exact absolute-path registration command for an installed client."""
    binary = shutil.which(client)
    if binary is None:
        return []
    project = root.expanduser().resolve(strict=True)
    python = project / ".venv" / "bin" / "python"
    env = {
        "COORD_PROJECT_ROOT": str(project),
        "COORD_DB": str(project / ".coordharness" / "coord.db"),
        "COORD_KNOWLEDGE_DB": str(project / ".coordharness" / "knowledge.db"),
        "COORD_DEPLOYMENT_PROFILE": "generic",
        "COORD_ACTOR": client,
    }
    if client == "codex":
        command = [binary, "mcp", "add", "coordharness"]
        for key, value in env.items():
            command.extend(("--env", f"{key}={value}"))
    elif client == "claude":
        # Claude's variadic --env option consumes following positional text, so
        # the server name must precede the first environment option.
        command = [
            binary, "mcp", "add", "--scope", "project", "--transport", "stdio",
            "coordharness",
        ]
        for key, value in env.items():
            command.extend(("--env", f"{key}={value}"))
    else:
        raise ValueError(f"unsupported MCP client: {client}")
    return [*command, "--", str(python), "-m", "coordharness.coord.mcp_coord_server"]


def _client_get(root: Path, client: str) -> tuple[subprocess.CompletedProcess[str] | None, str]:
    binary = shutil.which(client)
    if binary is None:
        return None, ""
    command = (
        [binary, "-C", str(root), "mcp", "get", "coordharness"]
        if client == "codex"
        else [binary, "mcp", "get", "coordharness"]
    )
    completed = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=15, check=False
    )
    return completed, f"{completed.stdout}\n{completed.stderr}".lower()


def register_clients(root: Path) -> dict[str, Any]:
    """Idempotently register missing installed clients; never replace an existing entry."""
    project = root.expanduser().resolve(strict=True)
    results: list[dict[str, Any]] = []
    for client in ("codex", "claude"):
        command = client_registration_command(project, client)
        if not command:
            results.append({
                "client": client, "status": SKIPPED, "available": False,
                "changed": False, "summary": f"{client} is not installed",
            })
            continue
        try:
            before, _ = _client_get(project, client)
            if before is not None and before.returncode == 0:
                results.append({
                    "client": client, "status": PASS, "available": True,
                    "changed": False, "summary": "registration already present",
                    "registration_command": command,
                })
                continue
            completed = subprocess.run(
                command, cwd=project, capture_output=True, text=True, timeout=30, check=False
            )
            after, output = _client_get(project, client)
            present = after is not None and after.returncode == 0
            results.append({
                "client": client, "status": PASS if present else BLOCKED,
                "available": True, "changed": completed.returncode == 0,
                "summary": (
                    "registration created"
                    if present
                    else "registration command did not create a visible entry"
                ),
                "registration_command": command,
                "command_exit_code": completed.returncode,
                "approval_pending": "pending approval" in output,
            })
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({
                "client": client, "status": BLOCKED, "available": True,
                "changed": False, "summary": "registration command did not complete",
                "registration_command": command, "error_type": type(exc).__name__,
            })
    return {
        "ok": not any(item["status"] == BLOCKED for item in results),
        "changed": any(item["changed"] for item in results),
        "clients": results,
    }


def _check_client_list(root: Path, client: str) -> dict[str, Any]:
    if shutil.which(client) is None:
        return _finding(
            f"onboarding.{client}_client_registration", SKIPPED,
            f"{client} is not installed; client registration verification was skipped",
            available=False, registration_command=[],
        )
    try:
        completed, combined = _client_get(root, client)
        assert completed is not None
        pending = "pending approval" in combined
        present = completed.returncode == 0
        ok = present and not pending
        if ok:
            summary = f"{client} client registration is present and approved"
        elif pending and present:
            summary = (
                f"{client} registration is present but approval is pending; "
                "run `claude` in this project and approve coordharness"
            )
        else:
            summary = (
                f"{client} client registration is missing; "
                "run `coord onboard --register-clients`"
            )
        return _finding(
            f"onboarding.{client}_client_registration", PASS if ok else BLOCKED,
            summary, available=True, exit_code=completed.returncode,
            server_listed=present, approval_pending=pending,
            registration_command=client_registration_command(root, client),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _finding(
            f"onboarding.{client}_client_registration", BLOCKED,
            f"{client} MCP registration probe did not complete",
            available=True, error_type=type(exc).__name__,
        )


def run_onboarding_doctor(*, project_root: Path, db_path: Path,
                          probe_clients: bool = True, probe_mcp: bool = True) -> dict[str, Any]:
    '''Return a bounded report; never create, migrate, or repair state.'''
    try:
        root = project_root.expanduser().resolve(strict=True)
    except OSError:
        return {'schema': REPORT_SCHEMA, 'status': BLOCKED, 'read_only': True,
                'findings': [_finding('onboarding.project_root', BLOCKED,
                    'project root is not an existing directory', available=False)]}
    findings = [_check_instructions_and_skills(root)]
    config_finding, codex_record = _check_configs(root)
    findings.append(config_finding)
    current = database_current(db_path)
    findings.append(_finding('onboarding.coord_db', PASS if current else BLOCKED,
        'coord.db is initialized and current' if current
        else 'coord.db is missing or not current; run `coord board` once',
        current=current, reference='.coordharness/coord.db'))
    findings.append(_check_mcp_probe(root, codex_record) if probe_mcp else
        _finding('onboarding.mcp_stdio', SKIPPED, 'MCP stdio probe was explicitly skipped'))
    if probe_clients:
        findings.extend((_check_client_list(root, 'codex'), _check_client_list(root, 'claude')))
    else:
        findings.extend(_finding(f"onboarding.{client}_client_registration", SKIPPED,
            "client registration verification was explicitly skipped",
            available=shutil.which(client) is not None) for client in ('codex', 'claude'))
    findings.sort(key=lambda item: item['id'])
    return {'schema': REPORT_SCHEMA,
            'status': BLOCKED if any(item['status'] == BLOCKED for item in findings) else PASS,
            'read_only': True, 'instruction_sentinel': INSTRUCTION_SENTINEL,
            'findings': findings}
