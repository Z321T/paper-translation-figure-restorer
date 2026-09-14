"""Behavioral tests for safe manifest validation and deterministic rendering."""

from __future__ import annotations

import json
import math
from pathlib import Path

import fitz
import pytest

from restore_figures import (
    FigureSpec,
    RestorationError,
    load_manifest,
    render_figure_crops,
    validate_manifest,
)


def _manifest_file(tmp_path: Path, figures: list[dict[str, object]], **extra: object) -> Path:
    payload: dict[str, object] = {"version": 1, "figures": figures}
    payload.update(extra)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _restore(
    *,
    figure_id: str = "figure-1",
    page: int = 1,
    bbox: list[float] | list[object] | None = None,
    anchor: str = "**图 1：系统概览。**",
    occurrence: object = None,
    include_occurrence: bool = False,
    position: str | None = None,
    filename: str = "figure-001.png",
    alt: str = "图 1：系统概览",
) -> dict[str, object]:
    figure: dict[str, object] = {
        "id": figure_id,
        "status": "restore",
        "page": page,
        "bbox": bbox if bbox is not None else [40.0, 50.0, 240.0, 150.0],
        "anchor": anchor,
        "filename": filename,
        "alt": alt,
    }
    if include_occurrence:
        figure["occurrence"] = occurrence
    if position is not None:
        figure["position"] = position
    return figure


def _valid_specs(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> list[FigureSpec]:
    manifest_path = _manifest_file(tmp_path, [_restore()])
    manifest = load_manifest(manifest_path)
    return validate_manifest(
        manifest,
        pdf_document,
        translated_markdown,
        assets_dir,
    )


def _assert_category(
    fn,
    category: str,
    *,
    secret: str | None = None,
) -> None:
    with pytest.raises(RestorationError) as caught:
        fn()
    assert caught.value.category == category
    if secret is not None:
        assert secret not in str(caught.value)


def test_valid_crop_has_hand_derived_dimensions_and_page_native_pixels(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches a renderer that ignores the PDF clip or requested DPI."""

    specs = _valid_specs(tmp_path, pdf_document, translated_markdown, assets_dir)
    rendered = render_figure_crops(pdf_document, specs, assets_dir, dpi=144)

    assert len(rendered) == 1
    output = assets_dir / "figure-001.png"
    assert rendered[0].path == output
    assert output.is_file() and output.stat().st_size > 0

    pixmap = fitz.Pixmap(output)
    assert (pixmap.width, pixmap.height) == (400, 200)
    # The hand-selected center of the 200x100 point crop is inside the
    # red vector fill, proving page-native content was rendered.
    red, green, blue = pixmap.pixel(200, 100)
    assert red > 180 and green < 100 and blue < 100


@pytest.mark.parametrize(
    ("figures", "message"),
    [
        (
            [_restore(), _restore(figure_id="figure-1", filename="figure-002.png")],
            "duplicate figure IDs",
        ),
        (
            [_restore(), _restore(figure_id="figure-2")],
            "duplicate generated filenames",
        ),
    ],
)
def test_duplicate_ids_and_filenames_are_rejected(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    figures: list[dict[str, object]],
    message: str,
) -> None:
    """Catches mutations that overwrite one figure with another silently."""

    manifest = load_manifest(_manifest_file(tmp_path, figures))
    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "manifest",
    )
    assert message


def test_unknown_status_is_rejected_without_echoing_manifest_content(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches a parser that defaults an unrecognized state to restore."""

    secret = "UNSAFE_MANIFEST_PAYLOAD_9384"
    unknown = {"id": secret, "status": "invented", "reason": "no"}
    _assert_category(
        lambda: load_manifest(_manifest_file(tmp_path, [unknown])),
        "manifest",
        secret=secret,
    )


@pytest.mark.parametrize(
    ("status", "figure"),
    [
        ("restore", {"id": "figure-1", "status": "restore"}),
        (
            "already-present",
            {"id": "figure-1", "status": "already-present"},
        ),
        ("skip", {"id": "figure-1", "status": "skip"}),
        ("blocked", {"id": "figure-1", "status": "blocked"}),
    ],
)
def test_each_state_requires_its_contract_fields(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    status: str,
    figure: dict[str, object],
) -> None:
    """Catches accepting incomplete state records and later crashing in render."""

    assert status == figure["status"]
    _assert_category(
        lambda: load_manifest(_manifest_file(tmp_path, [figure])),
        "manifest",
    )


@pytest.mark.parametrize(
    "filename",
    ["../escape.png", "/tmp/absolute.png", "nested/figure.png", "nested\\figure.png"],
)
def test_generated_asset_filename_must_be_a_portable_basename(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    filename: str,
) -> None:
    """Catches writes outside staging_assets through unchecked filenames."""

    manifest = load_manifest(
        _manifest_file(tmp_path, [_restore(filename=filename)])
    )
    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "manifest",
    )


@pytest.mark.parametrize(
    "existing_asset",
    [
        "../outside.png",
        "/tmp/existing.png",
        "C:/outside.png",
        "nested/../outside.png",
        "nested\\existing.png",
    ],
)
def test_existing_asset_must_stay_inside_the_sibling_asset_directory(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    existing_asset: str,
) -> None:
    """Catches path normalization that accepts an existing asset escape."""

    figure = {
        "id": "figure-1",
        "status": "already-present",
        "existing_asset": existing_asset,
    }
    manifest = load_manifest(_manifest_file(tmp_path, [figure]))
    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "manifest",
    )


def test_already_present_requires_an_existing_relative_asset(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches treating a link string as proof that an asset exists."""

    absent = {
        "id": "figure-1",
        "status": "already-present",
        "existing_asset": "existing.png",
    }
    manifest = load_manifest(_manifest_file(tmp_path, [absent]))
    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "local_io",
    )

    (assets_dir / "existing.png").write_bytes(b"real image placeholder")
    manifest = load_manifest(_manifest_file(tmp_path, [absent]))
    specs = validate_manifest(manifest, pdf_document, translated_markdown, assets_dir)
    assert len(specs) == 1 and specs[0].existing_asset == "existing.png"


@pytest.mark.parametrize("status", ["skip", "blocked"])
@pytest.mark.parametrize("reason", ["", "   "])
def test_skip_and_blocked_require_non_empty_reasons(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    status: str,
    reason: str,
) -> None:
    """Catches silently dropping non-restored figure coverage decisions."""

    figure = {"id": "figure-1", "status": status, "reason": reason}
    _assert_category(
        lambda: load_manifest(_manifest_file(tmp_path, [figure])),
        "manifest",
    )


def test_skip_and_blocked_with_reasons_are_typed_and_validated(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches validation that accidentally accepts only restore records."""

    figures = [
        {"id": "skip-1", "status": "skip", "reason": "decorative rule"},
        {"id": "blocked-1", "status": "blocked", "reason": "ambiguous panel"},
    ]
    manifest = load_manifest(_manifest_file(tmp_path, figures))
    specs = validate_manifest(manifest, pdf_document, translated_markdown, assets_dir)
    assert [(spec.id, spec.status, spec.reason) for spec in specs] == [
        ("skip-1", "skip", "decorative rule"),
        ("blocked-1", "blocked", "ambiguous panel"),
    ]


@pytest.mark.parametrize("dpi", [0, -1, math.inf, math.nan])
def test_render_rejects_zero_negative_and_non_finite_dpi(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    dpi: float,
) -> None:
    """Catches passing unsafe resolution values into PyMuPDF."""

    specs = _valid_specs(tmp_path, pdf_document, translated_markdown, assets_dir)
    _assert_category(
        lambda: render_figure_crops(pdf_document, specs, assets_dir, dpi=dpi),
        "render",
    )


@pytest.mark.parametrize("page", [0, 3])
def test_page_numbers_are_one_based_and_in_range(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    page: int,
) -> None:
    """Catches off-by-one page access and silently clamped out-of-range pages."""

    manifest = load_manifest(_manifest_file(tmp_path, [_restore(page=page)]))
    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "manifest",
    )


@pytest.mark.parametrize(
    "bbox",
    [
        [40.0, 50.0, 40.0, 150.0],
        [40.0, 150.0, 240.0, 50.0],
        [float("nan"), 50.0, 240.0, 150.0],
        [40.0, 50.0, float("inf"), 150.0],
        [40.0, 50.0, 361.0, 150.0],
        [-1.0, 50.0, 240.0, 150.0],
    ],
)
def test_bbox_must_be_finite_positive_and_inside_page(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    bbox: list[float],
) -> None:
    """Catches invalid clips reaching get_pixmap and producing wrong crops."""

    _assert_category(
        lambda: validate_manifest(
            load_manifest(_manifest_file(tmp_path, [_restore(bbox=bbox)])),
            pdf_document,
            translated_markdown,
            assets_dir,
        ),
        "manifest",
    )


def test_missing_anchor_is_rejected_without_echoing_markdown(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches insertion preparation that silently falls back to file end."""

    secret = "PRIVATE_MARKDOWN_SENTINEL_4721"
    markdown = translated_markdown + secret
    manifest = load_manifest(
        _manifest_file(tmp_path, [_restore(anchor="**图 99：不存在。**")])
    )
    _assert_category(
        lambda: validate_manifest(manifest, pdf_document, markdown, assets_dir),
        "anchor",
        secret=secret,
    )


def test_zero_occurrence_is_rejected_even_when_anchor_is_present(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches treating zero as Python's last occurrence instead of one-based."""

    manifest = load_manifest(
        _manifest_file(
            tmp_path,
            [_restore(occurrence=0, include_occurrence=True)],
        )
    )
    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "anchor",
    )


def test_ambiguous_anchor_requires_a_valid_one_based_occurrence(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches selecting the first duplicate caption nondeterministically."""

    anchor = "**重复图：相同标题。**"
    markdown = f"{anchor}\n\n中间内容。\n\n{anchor}\n"
    ambiguous = _restore(anchor=anchor)
    manifest = load_manifest(_manifest_file(tmp_path, [ambiguous]))
    _assert_category(
        lambda: validate_manifest(manifest, pdf_document, markdown, assets_dir),
        "anchor",
    )

    resolved = _restore(anchor=anchor, occurrence=2, include_occurrence=True)
    manifest = load_manifest(_manifest_file(tmp_path, [resolved]))
    specs = validate_manifest(manifest, pdf_document, markdown, assets_dir)
    assert specs[0].occurrence == 2


@pytest.mark.parametrize(
    ("occurrence", "category"),
    [(-1, "anchor"), (0, "anchor"), (3, "anchor"), (1.5, "manifest"), ("1", "manifest")],
)
def test_occurrence_must_be_an_existing_one_based_integer(
    tmp_path: Path,
    pdf_document: fitz.Document,
    assets_dir: Path,
    occurrence: object,
    category: str,
) -> None:
    """Catches coercing invalid occurrence values into a valid list index."""

    anchor = "**重复图：相同标题。**"
    markdown = f"{anchor}\n\n中间内容。\n\n{anchor}\n"
    figure = _restore(
        anchor=anchor,
        occurrence=occurrence,
        include_occurrence=True,
    )
    _assert_category(
        lambda: validate_manifest(
            load_manifest(_manifest_file(tmp_path, [figure])),
            pdf_document,
            markdown,
            assets_dir,
        ),
        category,
    )


def test_unique_anchor_rejects_an_out_of_range_explicit_occurrence(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches accepting occurrence values without checking actual matches."""

    figure = _restore(occurrence=2, include_occurrence=True)
    manifest = load_manifest(_manifest_file(tmp_path, [figure]))
    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "anchor",
    )


def test_loader_rejects_wrong_version_extra_keys_and_malformed_json(
    tmp_path: Path,
) -> None:
    """Catches permissive parsing that discards unknown manifest structure."""

    wrong_version = _manifest_file(tmp_path, [], version=2)
    _assert_category(lambda: load_manifest(wrong_version), "manifest")

    non_integer_version = _manifest_file(tmp_path, [], version=1.0)
    _assert_category(lambda: load_manifest(non_integer_version), "manifest")

    extra_key = _manifest_file(tmp_path, [], unexpected="do not accept")
    _assert_category(lambda: load_manifest(extra_key), "manifest")

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"version": 1,', encoding="utf-8")
    _assert_category(lambda: load_manifest(malformed), "input")
