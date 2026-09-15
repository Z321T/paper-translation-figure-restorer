"""Behavioral tests for safe manifest validation and deterministic rendering."""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import fitz
import pytest

import restore_figures as restorer

from restore_figures import (
    FigureSpec,
    Manifest,
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
    ("figures", "expected_message"),
    [
        (
            [_restore(), _restore(figure_id="figure-1", filename="figure-002.png")],
            "figure IDs must be unique",
        ),
        (
            [_restore(), _restore(figure_id="figure-2")],
            "generated asset filenames must be unique",
        ),
    ],
)
def test_duplicate_ids_and_filenames_are_rejected(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
    figures: list[dict[str, object]],
    expected_message: str,
) -> None:
    """Catches mutations that overwrite one figure with another silently."""

    manifest = load_manifest(_manifest_file(tmp_path, figures))
    with pytest.raises(RestorationError) as caught:
        validate_manifest(manifest, pdf_document, translated_markdown, assets_dir)
    assert caught.value.category == "manifest"
    assert caught.value.message == expected_message


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


def test_manifest_bbox_overflow_is_sanitized_at_json_boundary(tmp_path: Path) -> None:
    """Catches a float conversion that leaks OverflowError from manifest loading."""

    path = tmp_path / "overflow-manifest.json"
    # Keep under Python's JSON integer digit guard while still overflowing float().
    huge_integer = "1" + "0" * 4000
    path.write_text(
        '{"version":1,"figures":[{"id":"figure-1","status":"restore",'
        '"page":1,"bbox":['
        + huge_integer
        + ',50,240,150],"anchor":"anchor","filename":"figure.png",'
        '"alt":"figure"}]}',
        encoding="utf-8",
    )
    _assert_category(
        lambda: load_manifest(path),
        "manifest",
    )


def test_direct_manifest_bbox_overflow_is_sanitized_at_validation_boundary(
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches a direct FigureSpec float conversion that leaks OverflowError."""

    spec = FigureSpec(
        id="figure-1",
        status="restore",
        page=1,
        bbox=(10**10000, 50.0, 240.0, 150.0),
        anchor="**图 1：系统概览。**",
        filename="figure-001.png",
        alt="图 1：系统概览",
    )
    _assert_category(
        lambda: validate_manifest(
            Manifest(version=1, figures=(spec,)),
            pdf_document,
            translated_markdown,
            assets_dir,
        ),
        "manifest",
    )


def test_dpi_overflow_is_sanitized_before_rendering(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches a DPI conversion that leaks OverflowError to callers."""

    specs = _valid_specs(tmp_path, pdf_document, translated_markdown, assets_dir)
    _assert_category(
        lambda: render_figure_crops(pdf_document, specs, assets_dir, dpi=10**10000),
        "render",
    )


def test_assets_dir_root_symlink_is_rejected_without_external_write(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
) -> None:
    """Catches resolving an asset-root symlink and accepting its outside target."""

    outside = tmp_path / "outside-assets"
    outside.mkdir()
    linked_root = tmp_path / "assets-link"
    linked_root.symlink_to(outside, target_is_directory=True)
    manifest = load_manifest(_manifest_file(tmp_path, [_restore()]))

    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, linked_root
        ),
        "local_io",
    )
    assert not (outside / "figure-001.png").exists()


def test_staging_assets_root_symlink_is_rejected_without_external_write(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches rendering through a staging-root symlink into a sibling directory."""

    specs = _valid_specs(tmp_path, pdf_document, translated_markdown, assets_dir)
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    linked_root = tmp_path / "staging-link"
    linked_root.symlink_to(outside, target_is_directory=True)

    _assert_category(
        lambda: render_figure_crops(pdf_document, specs, linked_root, dpi=144),
        "local_io",
    )
    assert not (outside / "figure-001.png").exists()


def test_escaping_symlink_component_is_rejected_without_external_write(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches accepting an existing asset path whose component resolves outside."""

    outside = tmp_path / "outside-component"
    outside.mkdir()
    (outside / "existing.png").write_bytes(b"outside")
    (assets_dir / "nested").symlink_to(outside, target_is_directory=True)
    figure = {
        "id": "figure-1",
        "status": "already-present",
        "existing_asset": "nested/existing.png",
    }
    manifest = load_manifest(_manifest_file(tmp_path, [figure]))

    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "local_io",
    )


def test_symlink_loop_resolution_is_sanitized_as_local_io(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches leaking RuntimeError when resolving a cyclic asset symlink."""

    first = assets_dir / "loop-a"
    second = assets_dir / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)
    figure = {
        "id": "figure-1",
        "status": "already-present",
        "existing_asset": "loop-a",
    }
    manifest = load_manifest(_manifest_file(tmp_path, [figure]))

    _assert_category(
        lambda: validate_manifest(
            manifest, pdf_document, translated_markdown, assets_dir
        ),
        "local_io",
    )


def test_validate_manifest_rejects_direct_manifest_version_bypass(
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches validation that trusts a manually constructed non-v1 Manifest."""

    _assert_category(
        lambda: validate_manifest(
            Manifest(version=2, figures=()),
            pdf_document,
            translated_markdown,
            assets_dir,
        ),
        "manifest",
    )


def test_explicit_null_occurrence_is_not_treated_as_omitted(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches permissive schema parsing that turns occurrence:null into default 1."""

    figure = _restore(include_occurrence=True, occurrence=None)
    _assert_category(
        lambda: validate_manifest(
            load_manifest(_manifest_file(tmp_path, [figure])),
            pdf_document,
            translated_markdown,
            assets_dir,
        ),
        "manifest",
    )


def test_generated_marker_is_inserted_and_replaced_on_rerun(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches missing marker insertion and duplicate blocks on rerun."""

    specs = _valid_specs(tmp_path, pdf_document, translated_markdown, assets_dir)
    rendered = render_figure_crops(pdf_document, specs, assets_dir, dpi=144)

    first = restorer.apply_generated_blocks(translated_markdown, rendered)
    assert first.count("<!-- figure-restorer:start figure-1 -->") == 1
    assert first.index("<!-- figure-restorer:start figure-1 -->") < first.index(
        "**图 1：系统概览。**"
    )

    changed = restorer.RenderedFigure(
        spec=replace(rendered[0].spec, filename="new-image.png"),
        path=assets_dir / "new-image.png",
        width=rendered[0].width,
        height=rendered[0].height,
    )
    second = restorer.apply_generated_blocks(first, [changed])
    assert second.count("<!-- figure-restorer:start figure-1 -->") == 1
    assert second.count("<!-- figure-restorer:end figure-1 -->") == 1
    assert "paper_assets/new-image.png" in second
    assert "paper_assets/figure-001.png" not in second


def test_blocked_cli_publishes_incomplete_report_and_exit_four(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches a CLI that claims complete coverage for blocked figures."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    manifest_path = _manifest_file(
        tmp_path,
        [{"id": "figure-1", "status": "blocked", "reason": "manual review"}],
    )
    output_path = tmp_path / "translated_with_figures.md"
    script = Path(__file__).resolve().parents[1] / "paper-translation-figure-restorer" / "scripts" / "restore_figures.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(controlled_pdf),
            str(markdown_path),
            str(manifest_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 4
    report = output_path.with_name("translated_with_figures_figure_report.md")
    assert output_path.is_file()
    assert report.is_file()
    assert "INCOMPLETE" in report.read_text(encoding="utf-8")
    assert "Traceback" not in result.stderr


def test_publish_failure_leaves_previous_bundle_unchanged(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches partial publication that destroys the previous output bundle."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    output_path = tmp_path / "translated_with_figures.md"
    output_path.write_text("OLD OUTPUT", encoding="utf-8")
    assets_path = tmp_path / "translated_with_figures_assets"
    assets_path.mkdir()
    (assets_path / "old.png").write_bytes(b"OLD ASSET")
    report_path = tmp_path / "translated_with_figures_figure_report.md"
    report_path.write_text("OLD REPORT", encoding="utf-8")
    manifest_path = _manifest_file(
        tmp_path,
        [{
            "id": "figure-1",
            "status": "blocked",
            "reason": "manual review",
        }],
    )

    def fail_render(*args: object, **kwargs: object) -> list[restorer.RenderedFigure]:
        raise restorer.RestorationError("render", "render failed")

    monkeypatch.setattr(restorer, "render_figure_crops", fail_render)
    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            output_path=output_path,
        )
    assert caught.value.category == "render"
    assert output_path.read_text(encoding="utf-8") == "OLD OUTPUT"
    assert (assets_path / "old.png").read_bytes() == b"OLD ASSET"
    assert report_path.read_text(encoding="utf-8") == "OLD REPORT"
    assert markdown_path.read_text(encoding="utf-8") == translated_markdown


def test_restore_derives_sibling_bundle_paths_and_preserves_source_bytes(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches default-path drift and prose/table rewrites outside markers."""

    markdown_path = tmp_path / "translated.md"
    source_bytes = translated_markdown.encode("utf-8")
    markdown_path.write_bytes(source_bytes)
    manifest_path = _manifest_file(tmp_path, [_restore()])

    result = restorer.restore_figures(controlled_pdf, markdown_path, manifest_path, dpi=144)

    assert result.output_path == tmp_path / "translated_with_figures.md"
    assert result.assets_dir == tmp_path / "translated_with_figures_assets"
    assert result.report_path == tmp_path / "translated_with_figures_figure_report.md"
    assert markdown_path.read_bytes() == source_bytes
    generated = result.output_path.read_text(encoding="utf-8")
    without_block = re.sub(
        r"<!-- figure-restorer:start figure-1 -->\n.*?<!-- figure-restorer:end figure-1 -->\n",
        "",
        generated,
        flags=re.DOTALL,
    )
    assert without_block == translated_markdown
    assert "| 输入 | 输出 |\n| --- | --- |\n| A | B |" in generated


def test_after_position_and_percent_encoded_links_are_portable(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches URI-unsafe filenames and placement before the wrong caption."""

    markdown_path = tmp_path / "translated source.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    output_path = tmp_path / "published copy.md"
    manifest_path = _manifest_file(
        tmp_path,
        [_restore(position="after", filename="figure one+?.png")],
    )

    result = restorer.restore_figures(
        controlled_pdf,
        markdown_path,
        manifest_path,
        output_path=output_path,
        dpi=144,
    )
    generated = output_path.read_text(encoding="utf-8")
    caption_end = generated.index("**图 1：系统概览。**") + len("**图 1：系统概览。**")
    marker_start = generated.index("<!-- figure-restorer:start figure-1 -->")
    assert caption_end < marker_start
    assert "published%20copy_assets/figure%20one%2B%3F.png" in generated
    assert (result.assets_dir / "figure one+?.png").is_file()


def test_restore_rerun_replaces_existing_marker_without_duplication(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches a pipeline that inserts a second block when rerun on its output."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    manifest_path = _manifest_file(tmp_path, [_restore()])
    first = restorer.restore_figures(
        controlled_pdf, markdown_path, manifest_path, dpi=144
    )
    second = restorer.restore_figures(
        controlled_pdf,
        first.output_path,
        manifest_path,
        output_path=first.output_path,
        in_place=True,
        dpi=220,
    )
    text = second.output_path.read_text(encoding="utf-8")
    assert text.count("<!-- figure-restorer:start figure-1 -->") == 1
    assert text.count("<!-- figure-restorer:end figure-1 -->") == 1


def test_multiple_insertions_resolve_offsets_from_original_markdown(
    tmp_path: Path,
    controlled_pdf: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches forward edits that shift a later caption's insertion offset."""

    figures = [
        _restore(),
        _restore(
            figure_id="figure-2",
            page=2,
            bbox=[35.0, 55.0, 325.0, 145.0],
            anchor="**图 2：双面板结果。**",
            filename="figure-002.png",
            alt="图 2：双面板结果",
        ),
    ]
    manifest_path = _manifest_file(tmp_path, figures)
    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    output_path = tmp_path / "translated_with_figures.md"
    result = restorer.restore_figures(
        controlled_pdf,
        markdown_path,
        manifest_path,
        output_path=output_path,
        dpi=144,
    )
    text = result.output_path.read_text(encoding="utf-8")
    assert text.count("figure-restorer:start") == 2
    assert text.index("figure-restorer:start figure-1") < text.index("**图 1：系统概览。**")
    assert text.index("figure-restorer:start figure-2") < text.index("**图 2：双面板结果。**")


def test_mismatched_and_nested_existing_markers_are_rejected(
    tmp_path: Path,
    pdf_document: fitz.Document,
    translated_markdown: str,
    assets_dir: Path,
) -> None:
    """Catches marker parsing that silently consumes another figure's block."""

    specs = _valid_specs(tmp_path, pdf_document, translated_markdown, assets_dir)
    rendered = render_figure_crops(pdf_document, specs, assets_dir, dpi=144)
    malformed = (
        "<!-- figure-restorer:start figure-1 -->\n"
        "<!-- figure-restorer:end figure-2 -->\n"
    )
    nested = (
        "<!-- figure-restorer:start figure-1 -->\n"
        "<!-- figure-restorer:start figure-1 -->\n"
        "<!-- figure-restorer:end figure-1 -->\n"
        "<!-- figure-restorer:end figure-1 -->\n"
    )
    for prefix in (malformed, nested):
        with pytest.raises(restorer.RestorationError) as caught:
            restorer.apply_generated_blocks(prefix + translated_markdown, rendered)
        assert caught.value.category == "anchor"


def test_already_present_skip_and_blocked_are_reported_without_asset_replacement(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches copying an existing asset and dropping non-restored states."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    output_path = tmp_path / "translated_with_figures.md"
    assets_path = tmp_path / "translated_with_figures_assets"
    assets_path.mkdir()
    existing = assets_path / "existing.png"
    existing.write_bytes(b"verified asset")
    original_stat = existing.stat()
    manifest_path = _manifest_file(
        tmp_path,
        [
            {
                "id": "figure-1",
                "status": "already-present",
                "existing_asset": "existing.png",
            },
            {"id": "figure-2", "status": "skip", "reason": "decorative"},
            {"id": "figure-3", "status": "blocked", "reason": "ambiguous"},
        ],
    )

    result = restorer.restore_figures(
        controlled_pdf,
        markdown_path,
        manifest_path,
        output_path=output_path,
    )

    assert result.exit_code == 4 and result.blocked
    assert existing.read_bytes() == b"verified asset"
    assert existing.stat().st_ino == original_stat.st_ino
    report = result.report_path.read_text(encoding="utf-8")
    assert "INCOMPLETE" in report
    assert "already-present" in report and "decorative" in report and "ambiguous" in report
    assert "COMPLETE" not in report.replace("INCOMPLETE", "")


def test_source_collision_requires_explicit_in_place_and_creates_backup(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches accidental source overwrite and missing recoverable backups."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    manifest_path = _manifest_file(
        tmp_path,
        [{"id": "figure-1", "status": "skip", "reason": "decorative"}],
    )

    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            output_path=markdown_path,
        )
    assert caught.value.category == "input"
    original = markdown_path.read_bytes()

    result = restorer.restore_figures(
        controlled_pdf,
        markdown_path,
        manifest_path,
        in_place=True,
    )
    assert result.output_path == markdown_path
    assert markdown_path.with_name("translated.md.bak").read_bytes() == original


def test_symlinked_output_is_rejected_before_publication(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches resolving a symlinked output into an external destination."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    output_path = tmp_path / "published.md"
    output_path.symlink_to(outside)
    manifest_path = _manifest_file(
        tmp_path,
        [{"id": "figure-1", "status": "skip", "reason": "decorative"}],
    )

    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            output_path=output_path,
        )
    assert caught.value.category == "local_io"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_report_write_failure_leaves_existing_bundle_unchanged(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches report writes that publish a new Markdown before failing."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    output_path = tmp_path / "published.md"
    output_path.write_text("OLD OUTPUT", encoding="utf-8")
    report_path = tmp_path / "published_figure_report.md"
    report_path.write_text("OLD REPORT", encoding="utf-8")
    assets_path = tmp_path / "published_assets"
    assets_path.mkdir()
    (assets_path / "old.png").write_bytes(b"OLD ASSET")
    manifest_path = _manifest_file(tmp_path, [_restore()])
    original_write_text = Path.write_text

    def fail_report_write(
        path: Path, data: str, *args: object, **kwargs: object
    ) -> int:
        if path.name == report_path.name:
            raise OSError("simulated report failure")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_report_write)
    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            output_path=output_path,
            dpi=144,
        )
    assert caught.value.category == "local_io"
    assert output_path.read_text(encoding="utf-8") == "OLD OUTPUT"
    assert report_path.read_text(encoding="utf-8") == "OLD REPORT"
    assert (assets_path / "old.png").read_bytes() == b"OLD ASSET"


def test_rename_failure_rolls_back_all_published_targets(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a failed second rename that leaves a partially published bundle."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    output_path = tmp_path / "published.md"
    output_path.write_text("OLD OUTPUT", encoding="utf-8")
    report_path = tmp_path / "published_figure_report.md"
    report_path.write_text("OLD REPORT", encoding="utf-8")
    assets_path = tmp_path / "published_assets"
    assets_path.mkdir()
    (assets_path / "old.png").write_bytes(b"OLD ASSET")
    manifest_path = _manifest_file(tmp_path, [_restore()])
    original_replace = restorer.os.replace
    calls = 0

    def fail_report_rename(source: str | Path, target: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated rename failure")
        original_replace(source, target)

    monkeypatch.setattr(restorer.os, "replace", fail_report_rename)
    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            output_path=output_path,
            dpi=144,
        )
    assert caught.value.category == "local_io"
    assert output_path.read_text(encoding="utf-8") == "OLD OUTPUT"
    assert report_path.read_text(encoding="utf-8") == "OLD REPORT"
    assert (assets_path / "old.png").read_bytes() == b"OLD ASSET"


@pytest.mark.parametrize("collision", ["pdf", "manifest", "assets", "report"])
def test_bundle_targets_reject_any_input_collision_or_containment(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
    collision: str,
) -> None:
    """Catches publication targets that overwrite or contain an input file."""

    markdown_path = tmp_path / "paper.md"
    source_bytes = translated_markdown.encode("utf-8")
    markdown_path.write_bytes(source_bytes)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"version": 1, "figures": [{"id": "figure-1", "status": "skip", "reason": "decorative"}]}
        ),
        encoding="utf-8",
    )
    if collision == "pdf":
        output_path = controlled_pdf
    elif collision == "manifest":
        output_path = manifest_path
    elif collision == "assets":
        output_path = tmp_path / "published.md"
        # The derived `published_assets` directory is also an input container.
        manifest_path = tmp_path / "published_assets"
        manifest_path.write_text(
            json.dumps(
                {"version": 1, "figures": [{"id": "figure-1", "status": "skip", "reason": "decorative"}]}
            ),
            encoding="utf-8",
        )
    else:
        output_path = tmp_path / "published.md"
        manifest_path = tmp_path / "published_figure_report.md"
        manifest_path.write_text(
            json.dumps(
                {"version": 1, "figures": [{"id": "figure-1", "status": "skip", "reason": "decorative"}]}
            ),
            encoding="utf-8",
        )
    input_snapshots = {
        controlled_pdf: controlled_pdf.read_bytes(),
        markdown_path: source_bytes,
        manifest_path: manifest_path.read_bytes(),
    }

    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            output_path=output_path,
        )
    assert caught.value.category == "input"
    for path, contents in input_snapshots.items():
        assert path.read_bytes() == contents


def test_anchor_text_inside_alt_does_not_make_a_valid_rerun_ambiguous(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches anchor validation that counts text inside its own generated block."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    anchor = "**图 1：系统概览。**"
    manifest_path = _manifest_file(
        tmp_path,
        [_restore(alt=anchor)],
    )
    first = restorer.restore_figures(
        controlled_pdf,
        markdown_path,
        manifest_path,
        dpi=144,
    )
    second = restorer.restore_figures(
        controlled_pdf,
        first.output_path,
        manifest_path,
        output_path=first.output_path,
        in_place=True,
        dpi=144,
    )
    text = second.output_path.read_text(encoding="utf-8")
    assert text.count("<!-- figure-restorer:start figure-1 -->") == 1
    assert text.count(anchor) == 2


def test_first_publication_rename_failure_leaves_no_partial_bundle(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a first-run failure that leaves newly created siblings behind."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    output_path = tmp_path / "published.md"
    manifest_path = _manifest_file(tmp_path, [_restore()])
    original_replace = restorer.os.replace

    def fail_first_rename(source: str | Path, target: str | Path) -> None:
        raise OSError("simulated first rename failure")

    monkeypatch.setattr(restorer.os, "replace", fail_first_rename)
    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            output_path=output_path,
            dpi=144,
        )
    assert caught.value.category == "local_io"
    assert not output_path.exists()
    assert not (tmp_path / "published_assets").exists()
    assert not (tmp_path / "published_figure_report.md").exists()
    # Keep the monkeypatch's referenced function live for implementations that
    # choose to make a preflight rename.
    assert original_replace is not None


def test_partial_asset_rename_failure_restores_previous_assets(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches asset-level publication failure after Markdown was replaced."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    output_path = tmp_path / "published.md"
    output_path.write_text("OLD OUTPUT", encoding="utf-8")
    assets_path = tmp_path / "published_assets"
    assets_path.mkdir()
    (assets_path / "figure-001.png").write_bytes(b"OLD FIGURE")
    (assets_path / "old.png").write_bytes(b"OLD ASSET")
    report_path = tmp_path / "published_figure_report.md"
    report_path.write_text("OLD REPORT", encoding="utf-8")
    figures = [
        _restore(filename="figure-001.png"),
        _restore(
            figure_id="figure-2",
            page=2,
            bbox=[35.0, 55.0, 325.0, 145.0],
            anchor="**图 2：双面板结果。**",
            filename="figure-002.png",
            alt="图 2：双面板结果",
        ),
    ]
    manifest_path = _manifest_file(tmp_path, figures)
    original_replace = restorer.os.replace
    calls = 0

    def fail_second_asset_move(source: str | Path, target: str | Path) -> None:
        nonlocal calls
        calls += 1
        # output backup/install, report backup/install, first asset backup;
        # fail before the second asset can be installed.
        if calls == 7:
            raise OSError("simulated asset rename failure")
        original_replace(source, target)

    monkeypatch.setattr(restorer.os, "replace", fail_second_asset_move)
    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            output_path=output_path,
            dpi=144,
        )
    assert caught.value.category == "local_io"
    assert output_path.read_text(encoding="utf-8") == "OLD OUTPUT"
    assert report_path.read_text(encoding="utf-8") == "OLD REPORT"
    assert (assets_path / "figure-001.png").read_bytes() == b"OLD FIGURE"
    assert (assets_path / "old.png").read_bytes() == b"OLD ASSET"
    assert not (assets_path / "figure-002.png").exists()


def test_cli_maps_validation_and_local_io_errors_without_tracebacks(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches CLI tracebacks/content leaks and incorrect error exit classes."""

    markdown_path = tmp_path / "translated.md"
    markdown_path.write_text(translated_markdown + "SECRET_MARKDOWN", encoding="utf-8")
    bad_manifest = _manifest_file(
        tmp_path,
        [_restore(anchor="**missing SECRET_MANIFEST**")],
    )
    script = Path(__file__).resolve().parents[1] / "paper-translation-figure-restorer" / "scripts" / "restore_figures.py"
    validation = subprocess.run(
        [sys.executable, str(script), str(controlled_pdf), str(markdown_path), str(bad_manifest)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert validation.returncode == 2
    assert "Traceback" not in validation.stderr
    assert "SECRET_MARKDOWN" not in validation.stderr
    output_path = tmp_path / "published.md"
    output_path.symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    valid_manifest = _manifest_file(
        tmp_path,
        [{"id": "figure-1", "status": "skip", "reason": "decorative"}],
    )
    local_io = subprocess.run(
        [
            sys.executable,
            str(script),
            str(controlled_pdf),
            str(markdown_path),
            str(valid_manifest),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert local_io.returncode == 3
    assert "Traceback" not in local_io.stderr
    assert "SECRET_MARKDOWN" not in local_io.stderr


@pytest.mark.parametrize("backup_input", ["manifest", "pdf"])
def test_in_place_backup_rejects_input_collision_before_any_write(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
    backup_input: str,
) -> None:
    """Catches `.bak` creation overwriting a PDF or manifest input."""

    markdown_path = tmp_path / "paper.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    backup_path = markdown_path.with_name("paper.md.bak")
    manifest_path = tmp_path / "manifest.json"
    manifest_bytes = json.dumps(
        {"version": 1, "figures": [{"id": "figure-1", "status": "skip", "reason": "decorative"}]}
    ).encode("utf-8")
    if backup_input == "manifest":
        backup_path.write_bytes(manifest_bytes)
        manifest_path = backup_path
        pdf_path = controlled_pdf
    else:
        backup_path.write_bytes(controlled_pdf.read_bytes())
        manifest_path.write_bytes(manifest_bytes)
        pdf_path = backup_path
    snapshots = {
        markdown_path: markdown_path.read_bytes(),
        backup_path: backup_path.read_bytes(),
        manifest_path: manifest_path.read_bytes(),
        pdf_path: pdf_path.read_bytes(),
    }

    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            pdf_path,
            markdown_path,
            manifest_path,
            in_place=True,
        )
    assert caught.value.category == "input"
    for path, contents in snapshots.items():
        assert path.read_bytes() == contents


def test_existing_in_place_backup_symlink_is_rejected_safely(
    tmp_path: Path,
    controlled_pdf: Path,
    translated_markdown: str,
) -> None:
    """Catches copying an in-place backup through a symlink to an escape path."""

    markdown_path = tmp_path / "paper.md"
    markdown_path.write_text(translated_markdown, encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    backup_path = markdown_path.with_name("paper.md.bak")
    backup_path.symlink_to(outside)
    manifest_path = _manifest_file(
        tmp_path,
        [{"id": "figure-1", "status": "skip", "reason": "decorative"}],
    )
    source_bytes = markdown_path.read_bytes()

    with pytest.raises(restorer.RestorationError) as caught:
        restorer.restore_figures(
            controlled_pdf,
            markdown_path,
            manifest_path,
            in_place=True,
        )
    assert caught.value.category == "local_io"
    assert markdown_path.read_bytes() == source_bytes
    assert outside.read_text(encoding="utf-8") == "outside"
