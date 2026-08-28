from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_machine_private_provider_state_is_ignored_and_untracked():
    names = [
        "provider-catalog.json", "provider-profiles.json", "provider-routing.json",
        "usage-ledger-private.json", "usage-ledger-private.sqlite", ".env",
    ]
    check = subprocess.run(
        ["git", "check-ignore", "--stdin"], cwd=ROOT,
        input="\n".join(names) + "\n", text=True, capture_output=True, check=False,
    )
    assert check.returncode == 0
    ignored = set(check.stdout.splitlines())
    assert set(names) <= ignored
    tracked = subprocess.run(
        ["git", "ls-files", "--", *names], cwd=ROOT,
        text=True, capture_output=True, check=True,
    )
    assert tracked.stdout == ""


def test_coord_provider_projection_strips_private_connection_fields():
    source = (ROOT / "src/coordharness/usage/provider_management.py").read_text(encoding="utf-8")
    # These may be accepted by a private source registry, but COORD's
    # sanitizer must not copy them into its public response dictionary.
    response_builder = source[source.index("def _safe_response"):source.index("class ProviderManagementForwarder")]
    for private_field in ("credential_env", "endpoint_env", "dashboard_url", '"endpoint"'):
        assert private_field not in response_builder
