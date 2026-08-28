from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import validate_documentation as validator  # noqa: E402


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _png(width: int = 2, height: int = 3) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    rows = b"".join(b"\x00" + (b"\x00\x00\x00\xff" * width) for _ in range(height))
    return b"".join(
        (
            validator.PNG_SIGNATURE,
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(rows)),
            chunk(b"IEND", b""),
        )
    )


def _svg(*, role: str = "img", title: str = "Title", desc: str = "Description") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'role="{role}" aria-labelledby="title desc">'
        f'<title id="title">{title}</title><desc id="desc">{desc}</desc>'
        "</svg>\n"
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _fixture(tmp_path: Path) -> tuple[Path, bytes, str]:
    root = tmp_path / "repo"
    root.mkdir()
    image = _png()
    image_rel = "docs/assets/screens/pixel.png"
    svg_rel = "docs/assets/nested/diagram.svg"
    _write(
        root / "README.md",
        "\n".join(
            (
                "[guide](docs/guide%20one.md?mode=read#start)",
                '<a href="docs/data.json?raw=1#top">data</a>',
                f"![pixel]({image_rel})",
                "[external](https://example.com/missing)",
                "[mail](mailto:docs@example.com)",
                "[same](#top)",
                "`[code](docs/not-a-link.md)`",
                "```md",
                "[fenced](docs/not-a-link-either.md)",
                "```",
                "",
            )
        ),
    )
    _write(root / "docs/guide one.md", "# Start\n\n[reference][data]\n[data]: data.json\n")
    _write(root / "docs/data.json", '{"ok": true}\n')
    _write(root / svg_rel, _svg())
    _write(root / image_rel, image)
    _write(
        root / "docs/assets/provenance.json",
        json.dumps(
            {
                "assets": [
                    {
                        "path": image_rel,
                        "purpose": "deterministic test image",
                        "source_truth": ["README.md"],
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "width": 2,
                        "height": 3,
                        "provenance_class": "synthetic-web-capture",
                        "synthetic": True,
                        "capture_method": "deterministic fixture renderer",
                        "viewport_or_device": "2x3 fixture",
                        "deterministic_fixture": "README.md",
                    },
                    {
                        "path": svg_rel,
                        "purpose": "deterministic test diagram",
                        "source_truth": ["README.md"],
                    },
                ]
            }
        )
        + "\n",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root, image, image_rel


def _messages(root: Path) -> str:
    return "\n".join(validator.validate(root).messages)


def _provenance(root: Path) -> dict:
    return json.loads((root / "docs/assets/provenance.json").read_text(encoding="utf-8"))


def _write_provenance(root: Path, value: dict) -> None:
    _write(root / "docs/assets/provenance.json", json.dumps(value) + "\n")


def test_clean_minimal_fixture_passes_with_exact_success_line(tmp_path: Path, capsys) -> None:
    root, _, _ = _fixture(tmp_path)
    assert validator.main([str(root)]) == 0
    captured = capsys.readouterr()
    assert captured.out == validator.SUCCESS + "\n"
    assert captured.err == ""


def test_missing_markdown_link_is_actionable_after_url_normalization(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "README.md", "[missing](docs/missing%20file.md?raw=1#part)\n")
    messages = _messages(root)
    assert "README.md:1: local link target does not exist" in messages
    assert "missing%20file.md?raw=1#part" in messages


def test_missing_html_link_is_actionable(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "docs/guide one.md", '<img src="missing-image.png#preview" alt="missing">\n')
    assert "docs/guide one.md:1: local link target does not exist" in _messages(root)


def test_invalid_url_encoded_utf8_is_reported_instead_of_crashing(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "README.md", "[bad](docs/%FF.md)\n")
    assert "invalid UTF-8 URL encoding" in _messages(root)


def test_invalid_nested_json_fails(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "docs/nested/bad.json", '{"broken": ]\n')
    _git(root, "add", "docs/nested/bad.json")
    assert "docs/nested/bad.json:1:12: invalid JSON" in _messages(root)


def test_malformed_svg_xml_fails(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "docs/assets/nested/diagram.svg", "<svg><title></svg>\n")
    assert "invalid SVG XML" in _messages(root)


def test_svg_requires_img_role(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "docs/assets/nested/diagram.svg", _svg(role="presentation"))
    assert 'root <svg> must have role="img"' in _messages(root)


def test_svg_requires_nonempty_referenced_title(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "docs/assets/nested/diagram.svg", _svg(title="   "))
    assert "aria-labelledby must reference a nonempty <title> by id" in _messages(root)


def test_svg_requires_nonempty_referenced_description(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "docs/assets/nested/diagram.svg", _svg(desc=""))
    assert "aria-labelledby must reference a nonempty <desc> by id" in _messages(root)


def test_svg_rejects_external_http_and_file_hrefs(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    svg = _svg().replace(
        "</svg>",
        '<image href="HTTPS://example.com/image.png"/><use href="file:///tmp/local.svg"/></svg>',
    )
    _write(root / "docs/assets/nested/diagram.svg", svg)
    report = validator.validate(root)
    assert sum("external http/file href is forbidden" in item for item in report.messages) == 2


def test_every_listed_png_must_exist(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    data = _provenance(root)
    data["assets"].append(
        {"path": "other/missing.png", "sha256": "0" * 64, "width": 1, "height": 1}
    )
    _write_provenance(root, data)
    assert "listed PNG does not exist" in _messages(root)


def test_png_sha_must_be_exact_lowercase(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    data = _provenance(root)
    data["assets"][0]["sha256"] = data["assets"][0]["sha256"].upper()
    _write_provenance(root, data)
    assert "sha256 must be exactly 64 lowercase hexadecimal characters" in _messages(root)


def test_png_sha_must_match_exact_bytes(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    data = _provenance(root)
    data["assets"][0]["sha256"] = "0" * 64
    _write_provenance(root, data)
    assert "sha256 does not match file bytes" in _messages(root)


def test_png_dimensions_must_match_ihdr(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    data = _provenance(root)
    data["assets"][0]["width"] = 99
    _write_provenance(root, data)
    assert "do not match IHDR 2x3" in _messages(root)


def test_fake_png_cannot_satisfy_provenance(tmp_path: Path) -> None:
    root, _, image_rel = _fixture(tmp_path)
    fake = b"not really a png"
    _write(root / image_rel, fake)
    data = _provenance(root)
    data["assets"][0]["sha256"] = hashlib.sha256(fake).hexdigest()
    _write_provenance(root, data)
    assert "file has no valid PNG IHDR" in _messages(root)


def test_tracked_docs_png_requires_one_provenance_entry(tmp_path: Path) -> None:
    root, _, image_rel = _fixture(tmp_path)
    _write_provenance(root, {"assets": []})
    assert f"{image_rel}: tracked visual asset has no provenance entry" in _messages(root)


def test_tracked_docs_png_rejects_duplicate_provenance_entries(tmp_path: Path) -> None:
    root, _, image_rel = _fixture(tmp_path)
    data = _provenance(root)
    data["assets"].append(dict(data["assets"][0]))
    _write_provenance(root, data)
    messages = _messages(root)
    assert image_rel in messages
    assert "duplicate provenance path" in messages


def test_untracked_docs_png_does_not_require_provenance(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(root / "docs/assets/screens/untracked.png", _png(1, 1))
    assert validator.validate(root).total == 0


def test_png_requires_complete_capture_metadata(tmp_path: Path) -> None:
    root, _, image_rel = _fixture(tmp_path)
    data = _provenance(root)
    entry = next(row for row in data["assets"] if row["path"] == image_rel)
    del entry["capture_method"]
    _write_provenance(root, data)
    assert "capture_method must be nonempty" in _messages(root)


def test_tracked_svg_requires_provenance(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    data = _provenance(root)
    data["assets"] = [row for row in data["assets"] if not row["path"].endswith(".svg")]
    _write_provenance(root, data)
    assert "docs/assets/nested/diagram.svg: tracked visual asset has no provenance entry" in _messages(root)


def test_failure_output_is_deterministic_and_bounded(tmp_path: Path, capsys) -> None:
    root, _, _ = _fixture(tmp_path)
    _write(
        root / "README.md",
        "".join(f"[missing {index}](docs/missing-{index:02d}.md)\n" for index in range(40)),
    )
    assert validator.main([str(root)]) == 1
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert lines[0] == "documentation validation failed: 40 issue(s); showing 25"
    assert len([line for line in lines if line.startswith("ERROR ")]) == validator.MAX_MESSAGES
    assert lines[-1] == "... 15 additional issue(s) suppressed"
