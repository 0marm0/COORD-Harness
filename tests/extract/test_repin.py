from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "extract"
sys.path.insert(0, str(TOOLS))
import gate  # noqa: E402
import repin  # noqa: E402

DERIVED_BODY = "def adapted() -> int:\n    return 1\n"
AUTHORED_DESCRIPTION = "public helper written in this repository"
DERIVED_REASON = "adapted from a pinned source and reviewed once in this repository"
REPIN_REASON = "reviewed the new bytes; the change is a comment and a rename only"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def authored_document(files: dict[str, str], derived: dict[str, dict]) -> str:
    return json.dumps({"files": files, "derived": derived}, indent=2) + "\n"


def build_repo(
    root: Path,
    *,
    files: dict[str, str] | None = None,
    declared: dict[str, str] | None = None,
    derived_pin: str | None = None,
) -> Path:
    """A synthetic repository shaped like the public one: index, manifest, pins."""
    root.mkdir(parents=True, exist_ok=True)
    payload = {"src/authored.py": "VALUE = 1\n", "src/adapted.py": DERIVED_BODY}
    payload.update(files or {})
    for rel, text in payload.items():
        write(root / rel, text)
    declarations = declared if declared is not None else {
        rel: AUTHORED_DESCRIPTION for rel in payload if rel != "src/adapted.py"
    }
    pin = derived_pin or hashlib.sha256(payload["src/adapted.py"].encode()).hexdigest()
    write(root / "tools/extract/manifest.json", '{"files": []}\n')
    write(
        root / "tools/extract/authored.json",
        authored_document(
            declarations,
            {
                "src/adapted.py": {
                    "origin": "ported from a pinned source, then adapted here",
                    "reason": DERIVED_REASON,
                    "reviewed_sha256": pin,
                }
            },
        ),
    )
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "noreply" + "@" + "example.invalid")
    git(root, "add", "-A")
    return root


def run(root: Path, *args: str) -> tuple[int, str]:
    """Invoke the tool the way CI does, capturing exit code and output."""
    result = subprocess.run(
        [sys.executable, str(TOOLS / "repin.py"), "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def staged_authored(root: Path) -> dict:
    return json.loads(git(root, "show", ":tools/extract/authored.json"))


def test_clean_tree_exits_zero(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "clean")
    code, output = run(root, "--check")
    assert code == 0, output
    assert "MANIFEST CURRENT" in output


def test_undeclared_file_is_caught(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "undeclared")
    write(root / "src/new_surface.py", "VALUE = 2\n")
    git(root, "add", "-A")
    code, output = run(root, "--check")
    assert code == 1
    assert "UNDECLARED  src/new_surface.py" in output
    assert "--declare src/new_surface.py" in output


def test_stale_pin_is_caught_and_matches_the_gate(tmp_path: Path) -> None:
    """The planted defect must fail the check, and fail the gate the same way."""
    root = build_repo(tmp_path / "stale")
    before, _ = run(root, "--check")
    assert before == 0
    write(root / "src/adapted.py", DERIVED_BODY + "# edited upstream\n")
    git(root, "add", "-A")
    code, output = run(root, "--check")
    assert code == 1
    assert "STALE  src/adapted.py" in output
    assert "--repin src/adapted.py" in output
    report = gate.run(root)
    assert any("src/adapted.py: content differs from reviewed_sha256" in item
               for item in report.coverage), report.coverage


def test_apply_fixes_an_undeclared_file_and_a_stale_pin(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "apply")
    write(root / "src/new_surface.py", "VALUE = 2\n")
    write(root / "src/adapted.py", DERIVED_BODY + "# edited upstream\n")
    git(root, "add", "-A")
    assert run(root, "--check")[0] == 1
    code, output = run(
        root,
        "--apply",
        "--declare",
        "src/new_surface.py",
        "--description",
        "public surface written in this repository for the new endpoint",
        "--repin",
        "src/adapted.py",
        "--reason",
        REPIN_REASON,
    )
    assert code == 0, output
    assert "declared src/new_surface.py" in output and "re-pinned src/adapted.py" in output
    git(root, "add", "-A")
    code, output = run(root, "--check")
    assert code == 0, output
    document = staged_authored(root)
    entry = document["derived"]["src/adapted.py"]
    expected = hashlib.sha256((root / "src/adapted.py").read_bytes()).hexdigest()
    assert entry["reviewed_sha256"] == expected
    assert entry["repin"]["reason"] == REPIN_REASON
    assert entry["repin"]["method"] == repin.REPIN_METHOD
    assert "not an independent re-review" in entry["repin"]["method"]
    assert gate.run(root).coverage == []


def test_apply_refuses_to_invent_a_description(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "no-description")
    write(root / "src/new_surface.py", "VALUE = 2\n")
    git(root, "add", "-A")
    original = (root / "tools/extract/authored.json").read_text(encoding="utf-8")
    code, output = run(root, "--apply", "--declare", "src/new_surface.py")
    assert code == 1
    assert "refused" in output
    assert (root / "tools/extract/authored.json").read_text(encoding="utf-8") == original
    assert run(root, "--check")[0] == 1


def test_apply_refuses_a_placeholder_description(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "placeholder-description")
    write(root / "src/new_surface.py", "VALUE = 2\n")
    git(root, "add", "-A")
    code, output = run(
        root, "--apply", "--declare", "src/new_surface.py", "--description", "TODO"
    )
    assert code == 1
    assert "placeholder token" in output
    assert "src/new_surface.py" not in staged_authored(root)["files"]


def test_apply_refuses_an_unreasoned_repin(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "no-reason")
    write(root / "src/adapted.py", DERIVED_BODY + "# edited upstream\n")
    git(root, "add", "-A")
    stale_pin = staged_authored(root)["derived"]["src/adapted.py"]["reviewed_sha256"]
    code, output = run(root, "--apply", "--repin", "src/adapted.py")
    assert code == 1
    assert "--reason" in output
    for weak in ("tbd", "fine"):
        assert run(root, "--apply", "--repin", "src/adapted.py", "--reason", weak)[0] == 1
    document = json.loads((root / "tools/extract/authored.json").read_text(encoding="utf-8"))
    assert document["derived"]["src/adapted.py"]["reviewed_sha256"] == stale_pin
    assert "repin" not in document["derived"]["src/adapted.py"]


def test_check_rejects_a_planted_placeholder_and_an_unreasoned_stamp(tmp_path: Path) -> None:
    """A vacant description cannot merge even if a hand, not this tool, wrote it."""
    root = build_repo(
        tmp_path / "vacant",
        declared={"src/authored.py": "placeholder - " + repin.VACANT_MARKER},
    )
    code, output = run(root, "--check")
    assert code == 1
    assert "VACANT  src/authored.py" in output

    root = build_repo(tmp_path / "vacant-stamp")
    document = json.loads((root / "tools/extract/authored.json").read_text(encoding="utf-8"))
    document["derived"]["src/adapted.py"]["repin"] = {"reason": "", "method": repin.REPIN_METHOD}
    write(root / "tools/extract/authored.json", json.dumps(document, indent=2) + "\n")
    git(root, "add", "-A")
    code, output = run(root, "--check")
    assert code == 1
    assert "recorded re-pin reason is empty" in output


def test_an_honest_description_mentioning_placeholders_is_accepted(tmp_path: Path) -> None:
    """The vacancy rule must not fire on prose that describes real file content."""
    root = build_repo(
        tmp_path / "honest",
        declared={"src/authored.py": "generated client configuration template with placeholders only"},
    )
    code, output = run(root, "--check")
    assert code == 0, output


def test_repin_refuses_when_the_worktree_differs_from_the_index(tmp_path: Path) -> None:
    """Pinning staged bytes while the worktree has moved on would bless the wrong content."""
    root = build_repo(tmp_path / "divergent")
    write(root / "src/adapted.py", DERIVED_BODY + "# staged edit\n")
    git(root, "add", "-A")
    write(root / "src/adapted.py", DERIVED_BODY + "# later unstaged edit\n")
    code, output = run(root, "--apply", "--repin", "src/adapted.py", "--reason", REPIN_REASON)
    assert code == 1
    assert "worktree content differs from staged content" in output
    assert "repin" not in json.loads(
        (root / "tools/extract/authored.json").read_text(encoding="utf-8")
    )["derived"]["src/adapted.py"]


def test_check_reads_the_index_not_the_worktree(tmp_path: Path) -> None:
    """An unstaged edit is invisible to CI, so it is advice here, never a verdict."""
    root = build_repo(tmp_path / "unstaged")
    write(root / "src/adapted.py", DERIVED_BODY + "# unstaged\n")
    code, output = run(root, "--check")
    assert code == 0, output
    assert "its pin will go stale when you stage the file" in output


def test_declaring_an_unstaged_file_works_but_says_it_does_not_count_yet(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "unstaged-declare")
    write(root / "src/new_surface.py", "VALUE = 2\n")
    code, output = run(
        root,
        "--apply",
        "--declare",
        "src/new_surface.py",
        "--description",
        "public surface written in this repository for the new endpoint",
    )
    assert code == 0, output
    assert "not yet staged" in output
    # Declared but unstaged: the gate cannot see either the file or the entry.
    assert run(root, "--check")[0] == 0
    git(root, "add", "-A")
    assert run(root, "--check")[0] == 0


def test_declaring_a_nonexistent_path_is_refused(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "absent-declare")
    code, output = run(
        root, "--apply", "--declare", "src/absent.py", "--description", "a file that is not there"
    )
    assert code == 1
    assert "no such file" in output


def test_repin_all_needs_one_reason_and_fixes_every_stale_pin(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "repin-all")
    write(root / "src/adapted.py", DERIVED_BODY + "# edited upstream\n")
    git(root, "add", "-A")
    assert run(root, "--apply", "--repin-all")[0] == 1
    code, output = run(root, "--apply", "--repin-all", "--reason", REPIN_REASON)
    assert code == 0, output
    git(root, "add", "-A")
    assert run(root, "--check")[0] == 0


def test_a_four_character_description_is_not_a_description(tmp_path: Path) -> None:
    """One short word declared a file and turned the accounting into a length check."""
    root = build_repo(tmp_path / "thin-description")
    write(root / "src/new_surface.py", "VALUE = 2\n")
    git(root, "add", "-A")
    code, output = run(
        root, "--apply", "--declare", "src/new_surface.py", "--description", "asdf"
    )
    assert code == 1, output
    assert "too short" in output
    assert "src/new_surface.py" not in staged_authored(root)["files"]
    # A real but terse sentence still passes: the floor is a statement, not prose.
    assert run(
        root,
        "--apply",
        "--declare",
        "src/new_surface.py",
        "--description",
        "unit tests for the new endpoint",
    )[0] == 0


def test_check_rejects_a_thin_description_whatever_wrote_it(tmp_path: Path) -> None:
    root = build_repo(tmp_path / "thin-check", declared={"src/authored.py": "file"})
    code, output = run(root, "--check")
    assert code == 1, output
    assert "VACANT  src/authored.py" in output


def test_repin_all_refuses_to_stamp_several_files_with_one_reason(tmp_path: Path) -> None:
    """One sentence cannot be the judgement on two different files' new bytes."""
    root = build_repo(
        tmp_path / "repin-all-many",
        files={"src/adapted_two.py": DERIVED_BODY},
    )
    document = json.loads((root / "tools/extract/authored.json").read_text(encoding="utf-8"))
    document["derived"]["src/adapted_two.py"] = {
        "origin": "ported from a pinned source, then adapted here",
        "reason": DERIVED_REASON,
        "reviewed_sha256": hashlib.sha256(DERIVED_BODY.encode()).hexdigest(),
    }
    document["files"].pop("src/adapted_two.py", None)
    write(root / "tools/extract/authored.json", json.dumps(document, indent=2) + "\n")
    write(root / "src/adapted.py", DERIVED_BODY + "# edited upstream\n")
    write(root / "src/adapted_two.py", DERIVED_BODY + "# also edited upstream\n")
    git(root, "add", "-A")
    assert run(root, "--check")[0] == 1

    code, output = run(root, "--apply", "--repin-all", "--reason", "rubber stamp ok")
    assert code == 1, output
    assert "would re-pin them all under one reason" in output
    after = json.loads((root / "tools/extract/authored.json").read_text(encoding="utf-8"))
    assert all("repin" not in entry for entry in after["derived"].values())
    assert "--repin src/adapted.py" in output and "--repin src/adapted_two.py" in output

    # Two explicit paths need two reasons, in order.
    assert run(
        root, "--apply", "--repin", "src/adapted.py", "--repin", "src/adapted_two.py",
        "--reason", REPIN_REASON,
    )[0] == 1
    code, output = run(
        root,
        "--apply",
        "--repin",
        "src/adapted.py",
        "--repin",
        "src/adapted_two.py",
        "--reason",
        REPIN_REASON,
        "--reason",
        "read the second file's diff: a docstring and one renamed local",
    )
    assert code == 0, output
    git(root, "add", "-A")
    assert run(root, "--check")[0] == 0
    document = staged_authored(root)
    assert (
        document["derived"]["src/adapted_two.py"]["repin"]["reason"]
        == "read the second file's diff: a docstring and one renamed local"
    )


def test_a_repin_qualifies_the_reason_a_reader_sees(tmp_path: Path) -> None:
    """The old reason described dead content; the human-read field must say so."""
    root = build_repo(tmp_path / "qualified")
    write(root / "src/adapted.py", DERIVED_BODY + "# edited upstream\n")
    git(root, "add", "-A")
    assert run(root, "--apply", "--repin", "src/adapted.py", "--reason", REPIN_REASON)[0] == 0
    git(root, "add", "-A")
    entry = staged_authored(root)["derived"]["src/adapted.py"]
    assert entry["reason"].startswith(DERIVED_REASON)
    assert REPIN_REASON in entry["reason"]
    assert repin.REPIN_METHOD in entry["reason"]
    assert run(root, "--check")[0] == 0
