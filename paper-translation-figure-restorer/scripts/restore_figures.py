"""Validate figure manifests and render deterministic PDF crops.

This module deliberately stops at typed validation and crop rendering.  The
calling workflow owns Markdown insertion, publication, and reporting.
"""

from __future__ import annotations

import json
import math
import numbers
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Sequence

import pymupdf


_CATEGORIES: Final = frozenset(
    {"input", "manifest", "anchor", "render", "local_io"}
)
_STATUSES: Final = frozenset(
    {"restore", "already-present", "skip", "blocked"}
)
_RESTORE_FIELDS: Final = frozenset(
    {
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
)
_ALREADY_PRESENT_FIELDS: Final = frozenset({"id", "status", "existing_asset"})
_REASON_FIELDS: Final = frozenset({"id", "status", "reason"})


class RestorationError(Exception):
    """A safe, categorized failure from manifest validation or rendering."""

    def __init__(self, category: str, message: str) -> None:
        if category not in _CATEGORIES:
            raise ValueError("unknown restoration error category")
        self.category = category
        self.message = message
        super().__init__(message)

    def __repr__(self) -> str:
        return f"RestorationError(category={self.category!r}, message={self.message!r})"


@dataclass(frozen=True, slots=True, repr=False)
class FigureSpec:
    """A parsed figure state with no unchecked mapping retained."""

    id: str
    status: str
    page: int | None = field(default=None, repr=False)
    bbox: tuple[float, float, float, float] | None = field(default=None, repr=False)
    anchor: str | None = field(default=None, repr=False)
    occurrence: int | None = field(default=None, repr=False)
    position: str = field(default="before", repr=False)
    filename: str | None = field(default=None, repr=False)
    alt: str | None = field(default=None, repr=False)
    existing_asset: str | None = field(default=None, repr=False)
    reason: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"FigureSpec(status={self.status!r})"


@dataclass(frozen=True, slots=True, repr=False)
class Manifest:
    """Versioned parsed manifest containing only typed figure records."""

    version: int
    figures: tuple[FigureSpec, ...]

    def __repr__(self) -> str:
        return f"Manifest(version={self.version}, figure_count={len(self.figures)})"


@dataclass(frozen=True, slots=True, repr=False)
class RenderedFigure:
    """A rendered crop written to the caller-provided staging directory."""

    spec: FigureSpec
    path: Path
    width: int
    height: int

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def filename(self) -> str:
        # A rendered figure always originates from a validated restore spec.
        return self.spec.filename or ""

    def __repr__(self) -> str:
        return f"RenderedFigure(id={self.spec.id!r}, path={self.path.name!r})"


def _error(category: str, message: str) -> RestorationError:
    """Construct an error without interpolating untrusted source content."""

    return RestorationError(category, message)


def _require_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _error("manifest", "manifest figure entries must be JSON objects")
    if not all(isinstance(key, str) for key in value):
        raise _error("manifest", "manifest object keys must be strings")
    return value


def _require_string(value: object, field_name: str, *, non_empty: bool = True) -> str:
    if not isinstance(value, str) or (non_empty and not value.strip()):
        raise _error("manifest", f"manifest field {field_name} must be a non-empty string")
    return value


def _require_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise _error("manifest", f"manifest field {field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise _error("manifest", f"manifest field {field_name} must be finite")
    return number


def _parse_figure(raw: object) -> FigureSpec:
    item = _require_mapping(raw)
    status = item.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        raise _error("manifest", "manifest figure status is not supported")

    expected_fields = {
        "restore": _RESTORE_FIELDS,
        "already-present": _ALREADY_PRESENT_FIELDS,
        "skip": _REASON_FIELDS,
        "blocked": _REASON_FIELDS,
    }[status]
    if set(item) - expected_fields:
        raise _error("manifest", "manifest figure contains unsupported fields")

    required = {
        "restore": {"id", "status", "page", "bbox", "anchor", "filename", "alt"},
        "already-present": {"id", "status", "existing_asset"},
        "skip": {"id", "status", "reason"},
        "blocked": {"id", "status", "reason"},
    }[status]
    if not required.issubset(item):
        raise _error("manifest", "manifest figure is missing required fields")

    figure_id = _require_string(item.get("id"), "id")

    if status == "restore":
        page = item.get("page")
        if isinstance(page, bool) or not isinstance(page, int):
            raise _error("manifest", "manifest field page must be an integer")

        raw_bbox = item.get("bbox")
        if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
            raise _error("manifest", "manifest field bbox must contain four numbers")
        bbox = tuple(_require_number(value, "bbox") for value in raw_bbox)

        anchor = _require_string(item.get("anchor"), "anchor")
        filename = _require_string(item.get("filename"), "filename")
        alt = _require_string(item.get("alt"), "alt")

        raw_occurrence = item.get("occurrence")
        if raw_occurrence is not None:
            if isinstance(raw_occurrence, bool) or not isinstance(raw_occurrence, int):
                raise _error("manifest", "manifest field occurrence must be an integer")
            occurrence: int | None = raw_occurrence
        else:
            occurrence = None

        raw_position = item.get("position", "before")
        if not isinstance(raw_position, str):
            raise _error("manifest", "manifest field position must be a string")

        return FigureSpec(
            id=figure_id,
            status=status,
            page=page,
            bbox=bbox,
            anchor=anchor,
            occurrence=occurrence,
            position=raw_position,
            filename=filename,
            alt=alt,
        )

    if status == "already-present":
        existing_asset = _require_string(item.get("existing_asset"), "existing_asset")
        return FigureSpec(
            id=figure_id,
            status=status,
            existing_asset=existing_asset,
        )

    reason = _require_string(item.get("reason"), "reason")
    return FigureSpec(id=figure_id, status=status, reason=reason)


def load_manifest(path: Path) -> Manifest:
    """Read and structurally parse a version-1 JSON manifest."""

    if not isinstance(path, Path):
        raise _error("input", "manifest path must be a Path")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _error("input", "manifest could not be read") from exc

    try:
        raw = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _error("input", "manifest is not valid JSON") from exc

    if not isinstance(raw, dict) or set(raw) != {"version", "figures"}:
        raise _error("manifest", "manifest must contain only version and figures")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise _error("manifest", "unsupported manifest version")
    figures = raw.get("figures")
    if not isinstance(figures, list):
        raise _error("manifest", "manifest figures must be an array")

    parsed: list[FigureSpec] = []
    for figure in figures:
        parsed.append(_parse_figure(figure))
    return Manifest(version=version, figures=tuple(parsed))


def _safe_relative_path(
    value: str,
    root: Path,
    *,
    require_basename: bool,
) -> Path:
    """Resolve a portable POSIX path and prove it stays under ``root``."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise _error("manifest", "asset paths must be relative POSIX paths")
    pure = PurePosixPath(value)
    drive_prefix = (
        pure.parts
        and len(pure.parts[0]) == 2
        and pure.parts[0][0].isalpha()
        and pure.parts[0][1] == ":"
    )
    if (
        pure.is_absolute()
        or drive_prefix
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise _error("manifest", "asset path is outside the asset directory")
    if require_basename and len(pure.parts) != 1:
        raise _error("manifest", "generated asset filename must be a basename")

    try:
        root_resolved = root.resolve()
        candidate = (root_resolved.joinpath(*pure.parts)).resolve(strict=False)
    except OSError as exc:
        raise _error("local_io", "asset path could not be resolved") from exc
    except ValueError as exc:
        raise _error("manifest", "asset path could not be resolved") from exc
    if not candidate.is_relative_to(root_resolved):
        raise _error("manifest", "asset path is outside the asset directory")
    return candidate


def _validate_anchor(spec: FigureSpec, markdown: str) -> FigureSpec:
    if not isinstance(spec.anchor, str) or not spec.anchor.strip():
        raise _error("manifest", "figure anchor must be a non-empty string")
    occurrences = markdown.count(spec.anchor)
    if occurrences == 0:
        raise _error("anchor", "figure anchor was not found")

    occurrence = spec.occurrence
    if occurrence is None:
        if occurrences != 1:
            raise _error("anchor", "figure anchor is ambiguous")
        occurrence = 1
    elif (
        isinstance(occurrence, bool)
        or not isinstance(occurrence, int)
        or occurrence < 1
        or occurrence > occurrences
    ):
        raise _error("anchor", "figure anchor occurrence is out of range")
    return FigureSpec(
        id=spec.id,
        status=spec.status,
        page=spec.page,
        bbox=spec.bbox,
        anchor=spec.anchor,
        occurrence=occurrence,
        position=spec.position,
        filename=spec.filename,
        alt=spec.alt,
        existing_asset=spec.existing_asset,
        reason=spec.reason,
    )


def _validate_restore_geometry(spec: FigureSpec, document: pymupdf.Document) -> None:
    if not isinstance(document, pymupdf.Document):
        raise _error("input", "PDF document could not be accessed")
    try:
        page_count = len(document)
    except Exception as exc:
        raise _error("input", "PDF document could not be accessed") from exc
    if (
        isinstance(spec.page, bool)
        or not isinstance(spec.page, int)
        or spec.page < 1
        or spec.page > page_count
    ):
        raise _error("manifest", "figure page is outside the document")
    if (
        not isinstance(spec.bbox, (tuple, list))
        or len(spec.bbox) != 4
        or any(
            isinstance(coordinate, bool)
            or not isinstance(coordinate, numbers.Real)
            or not math.isfinite(float(coordinate))
            for coordinate in spec.bbox
        )
    ):
        raise _error("manifest", "figure bbox must contain four finite numbers")
    x0, y0, x1, y1 = (float(coordinate) for coordinate in spec.bbox)
    if not (x0 < x1 and y0 < y1):
        raise _error("manifest", "figure bbox must have positive width and height")
    try:
        page_rect = document[spec.page - 1].rect
    except Exception as exc:
        raise _error("input", "PDF page could not be accessed") from exc
    if not (
        x0 >= page_rect.x0
        and y0 >= page_rect.y0
        and x1 <= page_rect.x1
        and y1 <= page_rect.y1
    ):
        raise _error("manifest", "figure bbox is outside the PDF page")


def validate_manifest(
    manifest: Manifest,
    document: pymupdf.Document,
    markdown: str,
    assets_dir: Path,
) -> list[FigureSpec]:
    """Validate all state records and return typed specs for every figure."""

    if not isinstance(manifest, Manifest):
        raise _error("manifest", "manifest must be a parsed Manifest")
    if not isinstance(document, pymupdf.Document):
        raise _error("input", "PDF document could not be accessed")
    if not isinstance(markdown, str):
        raise _error("input", "translated Markdown must be text")
    if not isinstance(assets_dir, Path):
        raise _error("input", "asset directory must be a Path")
    try:
        root = assets_dir.resolve()
        if root.exists() and not root.is_dir():
            raise _error("local_io", "asset path is not a directory")
    except OSError as exc:
        raise _error("local_io", "asset directory could not be resolved") from exc

    seen_ids: set[str] = set()
    seen_filenames: set[str] = set()
    validated: list[FigureSpec] = []
    for spec in manifest.figures:
        if not isinstance(spec, FigureSpec):
            raise _error("manifest", "manifest figures must be typed FigureSpec objects")
        if not isinstance(spec.id, str) or not spec.id.strip():
            raise _error("manifest", "figure ID must be a non-empty string")
        if spec.id in seen_ids:
            raise _error("manifest", "figure IDs must be unique")
        seen_ids.add(spec.id)

        if spec.status not in _STATUSES:
            raise _error("manifest", "manifest figure status is not supported")

        if spec.status == "restore":
            if (
                not isinstance(spec.page, int)
                or isinstance(spec.page, bool)
                or spec.bbox is None
                or not isinstance(spec.anchor, str)
            ):
                raise _error("manifest", "restore figure is missing required fields")
            if spec.position not in {"before", "after"}:
                raise _error("manifest", "figure position is not supported")
            if (
                not isinstance(spec.filename, str)
                or not spec.filename
                or not isinstance(spec.alt, str)
                or not spec.alt.strip()
            ):
                raise _error("manifest", "restore figure has invalid asset fields")
            if spec.filename in seen_filenames:
                raise _error("manifest", "generated asset filenames must be unique")
            seen_filenames.add(spec.filename)
            _safe_relative_path(spec.filename, root, require_basename=True)
            _validate_restore_geometry(spec, document)
            spec = _validate_anchor(spec, markdown)
        elif spec.status == "already-present":
            if not isinstance(spec.existing_asset, str) or not spec.existing_asset.strip():
                raise _error("manifest", "existing asset is required")
            existing_path = _safe_relative_path(
                spec.existing_asset,
                root,
                require_basename=False,
            )
            try:
                if not existing_path.is_file():
                    raise _error("local_io", "existing asset was not found")
            except OSError as exc:
                raise _error("local_io", "existing asset could not be checked") from exc
        elif spec.status in {"skip", "blocked"}:
            if not isinstance(spec.reason, str) or not spec.reason.strip():
                raise _error("manifest", "non-restored figure requires a reason")

        validated.append(spec)
    return validated


def _validate_render_spec(spec: FigureSpec, document: pymupdf.Document) -> None:
    if spec.status != "restore":
        return
    if (
        not isinstance(spec.page, int)
        or isinstance(spec.page, bool)
        or spec.bbox is None
        or not isinstance(spec.filename, str)
    ):
        raise _error("manifest", "render figure is missing required fields")
    _validate_restore_geometry(spec, document)


def render_figure_crops(
    document: pymupdf.Document,
    specs: Sequence[FigureSpec],
    staging_assets: Path,
    dpi: int,
) -> list[RenderedFigure]:
    """Render restore specs to PNGs under a caller-provided staging directory."""

    if not isinstance(document, pymupdf.Document):
        raise _error("input", "PDF document could not be accessed")
    if isinstance(dpi, bool) or not isinstance(dpi, numbers.Real):
        raise _error("render", "DPI must be a positive finite number")
    dpi_number = float(dpi)
    if (
        not math.isfinite(dpi_number)
        or dpi_number <= 0
        or not dpi_number.is_integer()
    ):
        raise _error("render", "DPI must be a positive finite number")
    dpi = int(dpi_number)
    if not isinstance(staging_assets, Path):
        raise _error("input", "staging asset directory must be a Path")

    try:
        staging_assets.mkdir(parents=True, exist_ok=True)
        root = staging_assets.resolve()
        if not root.is_dir():
            raise _error("local_io", "staging asset path is not a directory")
    except RestorationError:
        raise
    except OSError as exc:
        raise _error("local_io", "staging asset directory could not be prepared") from exc

    rendered: list[RenderedFigure] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for spec in specs:
        if not isinstance(spec, FigureSpec):
            raise _error("manifest", "render specs must be typed FigureSpec objects")
        if spec.status != "restore":
            continue
        if spec.id in seen_ids:
            raise _error("manifest", "figure IDs must be unique")
        seen_ids.add(spec.id)
        _validate_render_spec(spec, document)
        output = _safe_relative_path(spec.filename, root, require_basename=True)
        if output in seen_paths:
            raise _error("manifest", "generated asset filenames must be unique")
        seen_paths.add(output)
        if spec.page is None or spec.bbox is None:
            raise _error("manifest", "render figure is missing required fields")
        try:
            page = document[spec.page - 1]
            clip = pymupdf.Rect(*spec.bbox)
            pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
        except Exception as exc:  # PyMuPDF exposes several version-specific errors.
            raise _error("render", "PDF crop could not be rendered") from exc
        try:
            pixmap.save(output, output="png")
        except Exception as exc:
            raise _error("local_io", "rendered crop could not be written") from exc
        rendered.append(
            RenderedFigure(
                spec=spec,
                path=output,
                width=pixmap.width,
                height=pixmap.height,
            )
        )
    return rendered


__all__ = [
    "FigureSpec",
    "Manifest",
    "RenderedFigure",
    "RestorationError",
    "load_manifest",
    "render_figure_crops",
    "validate_manifest",
]
