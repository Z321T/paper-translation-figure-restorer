"""Behavioral contract tests for the figure-restoration skill documentation."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "paper-translation-figure-restorer" / "SKILL.md"
REFERENCE = (
    ROOT / "paper-translation-figure-restorer" / "references" / "manifest-format.md"
)
PYPROJECT = ROOT / "pyproject.toml"


def _frontmatter(text: str) -> tuple[str, str]:
    """Return frontmatter only when both delimiters are line-oriented and at start."""

    lines = text.splitlines(keepends=True)
    assert lines and lines[0].rstrip("\r\n") == "---", "frontmatter must start at byte zero"
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    assert closing is not None, "SKILL.md must have a closing frontmatter delimiter"
    return "".join(lines[1:closing]), "".join(lines[closing + 1 :])


def _frontmatter_field(frontmatter: str, field: str) -> str:
    match = re.search(rf"(?m)^{re.escape(field)}:\s*(.+?)\s*$", frontmatter)
    assert match, f"frontmatter must define {field}"
    return match.group(1)


def _manifest_example(text: str) -> dict[str, object]:
    fenced = re.findall(r"(?ms)^```json\s*\n(.*?)^```\s*$", text)
    assert fenced, "reference must contain a fenced JSON example"
    parsed = json.loads(fenced[0])
    assert isinstance(parsed, dict)
    return parsed


def _fields(cell: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z_]+)`", cell))


def _status_contracts(text: str) -> dict[str, tuple[set[str], set[str]]]:
    rows = re.findall(
        r"(?m)^\|\s*`(restore|already-present|skip|blocked)`\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$",
        text,
    )
    return {
        status: (_fields(required), _fields(optional))
        for status, required, optional in rows
    }


def test_skill_is_post_translation_and_non_destructive() -> None:
    text = SKILL.read_text(encoding="utf-8")
    lowered = text.lower()

    assert re.search(r"\balready translated\b", lowered)
    assert re.search(r"\bdo not retranslate\b", lowered)
    assert re.search(r"_with_figures\.md\b", text)
    assert re.search(r"leave the input markdown untouched", lowered)


def test_skill_makes_visual_judgment_model_owned() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()

    assert re.search(r"visually inspect every pdf page containing a figure", text)
    assert re.search(r"successful helper run is not evidence", text)
    assert re.search(r"model-owned", text)


def test_skill_forbids_external_processing_and_table_regression() -> None:
    combined = (
        SKILL.read_text(encoding="utf-8")
        + REFERENCE.read_text(encoding="utf-8")
    )
    lowered = combined.lower()

    assert re.search(r"do not call\s+mineru", lowered)
    assert re.search(r"do not replace a structured markdown table", lowered)
    assert re.search(r"process files locally", lowered)


def test_skill_frontmatter_has_the_figure_restoration_trigger() -> None:
    frontmatter, body = _frontmatter(SKILL.read_text(encoding="utf-8"))
    assert _frontmatter_field(frontmatter, "name") == "paper-translation-figure-restorer"

    trigger = _frontmatter_field(frontmatter, "description").lower()
    assert re.search(r"original\s+(?:academic\s+)?pdf", trigger)
    assert re.search(r"existing translated markdown", trigger)
    assert re.search(r"user asks", trigger)
    assert re.search(r"\b(?:add|restore|recover|repair)\b", trigger)
    assert re.search(r"substantive", trigger)
    assert re.search(r"caption-only", trigger)
    assert re.search(r"(?:not|exclude|excluding).*decorative", trigger)
    assert "from scratch" not in trigger

    workflow = body.lower()
    assert re.search(r"do not trigger", workflow)
    assert re.search(r"from scratch", workflow)


def test_frontmatter_parser_rejects_a_prefixed_delimiter() -> None:
    with pytest.raises(AssertionError):
        _frontmatter("preamble\n---\nname: wrong\n---\n")


def test_skill_documents_the_ordered_workflow() -> None:
    text = SKILL.read_text(encoding="utf-8")
    section = re.search(
        r"(?ms)^## Ordered workflow\s+(.*?)(?=^## |\Z)",
        text,
    )
    assert section, "Skill must define a distinct ordered workflow section"
    steps = re.findall(
        r"(?m)^(\d+)\.\s+\*\*(?P<title>[^*]+)\*\*", section.group(1)
    )
    assert [int(number) for number, _ in steps] == list(range(1, 9))
    titles = [title.lower() for _, title in steps]
    required_terms = (
        ("resolve", "output"),
        ("inventory", "figures", "links"),
        ("render", "inspect", "pdf"),
        ("reconcile", "figure", "state"),
        ("manifest", "crop"),
        ("run", "helper"),
        ("inspect", "crop", "insertion"),
        ("audit", "coverage", "preservation"),
    )
    for title, terms in zip(titles, required_terms, strict=True):
        assert all(term in title for term in terms)


def test_v1_marks_multi_page_figures_blocked() -> None:
    text = " ".join(SKILL.read_text(encoding="utf-8").lower().split())
    assert re.search(r"(?:multi-page|spanning pages).{0,180}blocked", text)
    assert not re.search(r"emit ordered assets and one insertion block", text)


def test_cli_contract_default_output_and_project_metadata() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    combined = skill + "\n" + readme
    command = re.compile(
        r"restore_figures\.py\s+PDF\s+MARKDOWN\s+MANIFEST\s+"
        r"\[--output PATH\]\s+\[--dpi N\]\s+\[--in-place\]"
    )
    assert command.search(combined)
    assert re.search(r"default output.{0,120}_with_figures\.md", combined, re.I | re.S)

    metadata = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = metadata["project"]
    assert project["name"] == "paper-translation-figure-restorer"
    assert project["version"] == "0.1.0"
    assert project["requires-python"] == ">=3.12"
    assert "pymupdf>=1.28.2" in project["dependencies"]
    dev_dependencies = metadata.get("dependency-groups", {}).get("dev", [])
    assert "pytest>=9.1.1" in dev_dependencies


def test_manifest_reference_defines_version_one_state_fields() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    lowered = text.lower()

    example = _manifest_example(text)
    assert set(example) == {"version", "figures"}
    assert example["version"] == 1
    assert isinstance(example["figures"], list) and len(example["figures"]) == 1
    restore_example = example["figures"][0]
    assert isinstance(restore_example, dict)
    assert set(restore_example) == {
        "id",
        "status",
        "page",
        "bbox",
        "anchor",
        "occurrence",
        "position",
        "filename",
        "alt",
    }
    assert restore_example["status"] == "restore"

    assert _status_contracts(text) == {
        "restore": (
            {"id", "status", "page", "bbox", "anchor", "filename", "alt"},
            {"occurrence", "position"},
        ),
        "already-present": ({"id", "status", "existing_asset"}, set()),
        "skip": ({"id", "status", "reason"}, set()),
        "blocked": ({"id", "status", "reason"}, set()),
    }
    compact = " ".join(lowered.split())
    assert re.search(
        r"`position` is optional.*defaults? to `before`.*only accepted values.*`before`.*`after`",
        compact,
    )
    assert re.search(
        r"`occurrence` is optional only when the anchor occurs exactly once",
        compact,
    )
    assert re.search(
        r"when an anchor occurs more than once.*`occurrence` is required",
        compact,
    )
    assert "one-based" in compact
