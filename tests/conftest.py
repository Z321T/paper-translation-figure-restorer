"""Runtime-generated inputs for figure restoration behavior tests."""

from __future__ import annotations

import sys
from pathlib import Path

import fitz
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "paper-translation-figure-restorer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def controlled_pdf(tmp_path: Path) -> Path:
    """Create a two-page PDF containing only page-native vector figures."""

    path = tmp_path / "controlled-document.pdf"
    document = fitz.open()
    try:
        page_one = document.new_page(width=360, height=260)
        figure_one = fitz.Rect(40, 50, 240, 150)
        page_one.draw_rect(
            figure_one,
            color=(0.1, 0.1, 0.1),
            fill=(0.85, 0.18, 0.08),
            width=2,
        )
        page_one.insert_text(
            (68, 108),
            "FIGURE ONE",
            fontsize=18,
            color=(1, 1, 1),
        )

        page_two = document.new_page(width=360, height=260)
        left = fitz.Rect(35, 55, 155, 145)
        right = fitz.Rect(205, 55, 325, 145)
        page_two.draw_rect(
            left,
            color=(0.05, 0.15, 0.35),
            fill=(0.15, 0.55, 0.85),
            width=2,
        )
        page_two.draw_rect(
            right,
            color=(0.25, 0.1, 0.25),
            fill=(0.75, 0.35, 0.75),
            width=2,
        )
        page_two.insert_text(
            (55, 107),
            "FIGURE TWO LEFT",
            fontsize=11,
            color=(1, 1, 1),
        )
        page_two.insert_text(
            (217, 107),
            "FIGURE TWO RIGHT",
            fontsize=11,
            color=(1, 1, 1),
        )

        document.save(path)
    finally:
        document.close()
    return path


@pytest.fixture
def pdf_document(controlled_pdf: Path):
    """Open the generated PDF as a real PyMuPDF document for each test."""

    document = fitz.open(controlled_pdf)
    try:
        yield document
    finally:
        document.close()


@pytest.fixture
def translated_markdown() -> str:
    """A translated document with unique captions, repeats, and a table."""

    return (
        "# 受控论文\n\n"
        "引言段落保持不变。\n\n"
        "**图 1：系统概览。**\n\n"
        "图 1 的说明文字。\n\n"
        "| 输入 | 输出 |\n"
        "| --- | --- |\n"
        "| A | B |\n\n"
        "**图 2：双面板结果。**\n\n"
        "图 2 的说明文字。\n"
    )


@pytest.fixture
def assets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "paper_assets"
    directory.mkdir()
    return directory
