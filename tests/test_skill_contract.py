"""Behavioral contract tests for the figure-restoration skill documentation."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "paper-translation-figure-restorer" / "SKILL.md"
REFERENCE = (
    ROOT / "paper-translation-figure-restorer" / "references" / "manifest-format.md"
)


def _frontmatter(text: str) -> tuple[str, str]:
    """Return the frontmatter and body without depending on a YAML package."""

    parts = text.split("---", 2)
    assert len(parts) == 3, "SKILL.md must have YAML frontmatter"
    return parts[1], parts[2]


def test_skill_is_post_translation_and_non_destructive() -> None:
    text = SKILL.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "already translated" in lowered
    assert "do not retranslate" in lowered
    assert "_with_figures.md" in text
    assert "leave the input markdown untouched" in lowered


def test_skill_makes_visual_judgment_model_owned() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()

    assert "visually inspect every pdf page containing a figure" in text
    assert "successful helper run is not evidence" in text
    assert "model-owned" in text


def test_skill_forbids_external_processing_and_table_regression() -> None:
    combined = (
        SKILL.read_text(encoding="utf-8")
        + REFERENCE.read_text(encoding="utf-8")
    )
    lowered = combined.lower()

    assert "do not call mineru" in lowered
    assert "do not replace a structured markdown table" in lowered
    assert "process files locally" in lowered


def test_skill_frontmatter_has_the_figure_restoration_trigger() -> None:
    frontmatter, body = _frontmatter(SKILL.read_text(encoding="utf-8"))
    name = re.search(r"(?m)^name:\s*(\S+)\s*$", frontmatter)
    description = re.search(r"(?m)^description:\s*(.+?)\s*$", frontmatter)
    assert name and name.group(1) == "paper-translation-figure-restorer"
    assert description, "frontmatter must include a one-line trigger description"

    trigger = description.group(1).lower()
    assert "pdf" in trigger
    assert "translated markdown" in trigger
    assert any(term in trigger for term in ("missing figure", "restore", "recover"))
    assert "from scratch" not in trigger

    workflow = body.lower()
    assert "do not trigger" in workflow
    assert "from scratch" in workflow


def test_skill_documents_the_ordered_workflow() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()
    markers = (
        "resolve the pdf",
        "inventory markdown figures",
        "render and visually inspect",
        "reconcile each pdf figure",
        "write a paper-specific manifest",
        "run the helper",
        "inspect every crop and insertion",
        "audit figure coverage",
    )
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_manifest_reference_defines_version_one_state_fields() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    lowered = text.lower()

    assert '"version": 1' in text
    assert '"figures"' in text
    for state in ("restore", "already-present", "skip", "blocked"):
        assert f"`{state}`" in text
    for field in ("page", "bbox", "anchor", "filename", "alt", "existing_asset", "reason"):
        assert f'`{field}`' in text or f'"{field}"' in text
    assert "one-based" in lowered
    assert "before" in lowered and "after" in lowered
