"""Validate, render, insert, and publish portable restored-figure bundles.

Crop coordinates remain model-selected and paper-specific; this helper only
performs deterministic validation, rendering, marked insertion, reporting, and
transactional publication.
"""

from __future__ import annotations

import json
import math
import numbers
import argparse
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Sequence
from urllib.parse import quote

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


@dataclass(frozen=True, slots=True, repr=False)
class RestorationResult:
    """Published bundle paths and coverage outcome."""

    output_path: Path
    assets_dir: Path
    report_path: Path
    figures: tuple[FigureSpec, ...]
    blocked: bool
    exit_code: int

    @property
    def complete(self) -> bool:
        return not self.blocked

    def __repr__(self) -> str:
        return (
            f"RestorationResult(output={self.output_path.name!r}, "
            f"report={self.report_path.name!r}, blocked={self.blocked!r})"
        )


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
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _error("manifest", f"manifest field {field_name} must be finite") from exc
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

        if "occurrence" in item:
            raw_occurrence = item["occurrence"]
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
        candidate_path = root_resolved.joinpath(*pure.parts)
        _reject_symlink_components(candidate_path)
        candidate = candidate_path.resolve(strict=False)
    except OSError as exc:
        raise _error("local_io", "asset path could not be resolved") from exc
    except RuntimeError as exc:
        raise _error("local_io", "asset path could not be resolved") from exc
    except ValueError as exc:
        raise _error("manifest", "asset path could not be resolved") from exc
    if not candidate.is_relative_to(root_resolved):
        raise _error("manifest", "asset path is outside the asset directory")
    return candidate


def _reject_symlink_components(path: Path) -> None:
    """Reject symlink roots/components before canonicalizing untrusted paths."""

    try:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            if current.is_symlink():
                raise _error("local_io", "asset path contains a symlink")
    except RestorationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("local_io", "asset path could not be inspected") from exc


def _reject_tree_symlinks(root: Path) -> None:
    """Reject symlink entries anywhere in an existing published asset tree."""

    try:
        if not root.exists():
            return
        for current, directories, files in os.walk(root, followlinks=False):
            for name in (*directories, *files):
                if Path(current, name).is_symlink():
                    raise _error("local_io", "asset path contains a symlink")
    except RestorationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("local_io", "asset path could not be inspected") from exc


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
    if not isinstance(spec.bbox, (tuple, list)) or len(spec.bbox) != 4:
        raise _error("manifest", "figure bbox must contain four finite numbers")
    if any(isinstance(coordinate, bool) or not isinstance(coordinate, numbers.Real) for coordinate in spec.bbox):
        raise _error("manifest", "figure bbox must contain four finite numbers")
    try:
        coordinates = tuple(float(coordinate) for coordinate in spec.bbox)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _error("manifest", "figure bbox must contain four finite numbers") from exc
    if not all(math.isfinite(coordinate) for coordinate in coordinates):
        raise _error("manifest", "figure bbox must contain four finite numbers")
    x0, y0, x1, y1 = coordinates
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
    if (
        isinstance(manifest.version, bool)
        or not isinstance(manifest.version, int)
        or manifest.version != 1
    ):
        raise _error("manifest", "unsupported manifest version")
    try:
        _reject_symlink_components(assets_dir)
        root = assets_dir.resolve()
        if root.exists() and not root.is_dir():
            raise _error("local_io", "asset path is not a directory")
    except OSError as exc:
        raise _error("local_io", "asset directory could not be resolved") from exc

    seen_ids: set[str] = set()
    seen_filenames: set[str] = set()
    validated: list[FigureSpec] = []
    marker_ids = {
        spec.id for spec in manifest.figures if isinstance(spec, FigureSpec)
    }
    marker_ranges = _marker_ranges(markdown, marker_ids)
    anchor_markdown = _remove_marker_ranges(markdown, marker_ranges, marker_ids)
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
            spec = _validate_anchor(spec, anchor_markdown)
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
    try:
        dpi_number = float(dpi)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _error("render", "DPI must be a positive finite number") from exc
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
        _reject_symlink_components(staging_assets)
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


_MARKER_RE: Final = re.compile(
    r"<!--\s*figure-restorer:(start|end)\s+([^\s]+)\s*-->"
)


def _marker_ranges(markdown: str, allowed_ids: set[str]) -> dict[str, tuple[int, int]]:
    """Parse generated blocks and reject malformed, mismatched, or nested IDs."""

    stack: tuple[str, int] | None = None
    ranges: dict[str, tuple[int, int]] = {}
    for match in _MARKER_RE.finditer(markdown):
        kind, figure_id = match.groups()
        if kind == "start":
            if stack is not None:
                raise _error("anchor", "generated figure markers are nested")
            if figure_id not in allowed_ids:
                raise _error("anchor", "generated figure marker ID is not expected")
            if figure_id in ranges:
                raise _error("anchor", "generated figure marker ID is duplicated")
            stack = (figure_id, match.start())
            continue

        if stack is None or stack[0] != figure_id:
            raise _error("anchor", "generated figure markers have mismatched IDs")
        ranges[figure_id] = (stack[1], match.end())
        stack = None

    if stack is not None:
        raise _error("anchor", "generated figure marker is missing its end")
    return ranges


def _remove_marker_ranges(
    markdown: str,
    ranges: dict[str, tuple[int, int]],
    ids: set[str],
) -> str:
    """Remove selected generated blocks while preserving all other text."""

    result = markdown
    for _figure_id, (start, end) in sorted(
        ((figure_id, bounds) for figure_id, bounds in ranges.items() if figure_id in ids),
        key=lambda item: item[1][0],
        reverse=True,
    ):
        result = result[:start] + result[end:]
    return result


def _anchor_occurrences_outside_markers(
    markdown: str,
    anchor: str,
    ranges: dict[str, tuple[int, int]],
) -> list[int]:
    """Find anchor offsets in source text, excluding generated marker blocks."""

    if not ranges:
        return [
            position
            for position in (
                match.start() for match in re.finditer(re.escape(anchor), markdown)
            )
        ]
    positions: list[int] = []
    cursor = 0
    for start, end in sorted(ranges.values()):
        if cursor < start:
            positions.extend(
                cursor + match.start()
                for match in re.finditer(re.escape(anchor), markdown[cursor:start])
            )
        cursor = max(cursor, end)
    if cursor < len(markdown):
        positions.extend(
            cursor + match.start()
            for match in re.finditer(re.escape(anchor), markdown[cursor:])
        )
    return positions


def _encoded_asset_link(figure: RenderedFigure) -> str:
    """Return a relative POSIX link for a staged crop."""

    if not isinstance(figure.path, Path):
        raise _error("local_io", "rendered crop path is invalid")
    asset_directory = figure.path.parent.name
    filename = figure.filename
    if not asset_directory or asset_directory in {".", ".."} or not filename:
        raise _error("local_io", "rendered crop path is invalid")
    # quote each component separately so the slash remains a portable separator.
    return "/".join(
        quote(component, safe="-._~")
        for component in (asset_directory, filename)
    )


def _markdown_alt(value: str) -> str:
    """Keep a manifest alt label inside one safe Markdown image label."""

    return (
        value.replace("\\", "\\\\")
        .replace("]", "\\]")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _generated_block(figure: RenderedFigure) -> str:
    if not isinstance(figure, RenderedFigure):
        raise _error("manifest", "generated figures must be typed RenderedFigure objects")
    spec = figure.spec
    if (
        spec.status != "restore"
        or not isinstance(spec.id, str)
        or not spec.id
        or not isinstance(spec.alt, str)
    ):
        raise _error("manifest", "generated figure is not a restore item")
    if (
        any(character.isspace() for character in spec.id)
        or "<!--" in spec.id
        or "-->" in spec.id
    ):
        raise _error("manifest", "figure IDs must not contain whitespace")
    link = _encoded_asset_link(figure)
    return (
        f"<!-- figure-restorer:start {spec.id} -->\n"
        f"![{_markdown_alt(spec.alt)}]({link})\n"
        f"<!-- figure-restorer:end {spec.id} -->"
    )


def _nth_occurrence(text: str, needle: str, occurrence: int) -> int:
    position = -1
    start = 0
    for _ in range(occurrence):
        position = text.find(needle, start)
        if position < 0:
            raise _error("anchor", "figure anchor occurrence is out of range")
        start = position + len(needle)
    return position


def apply_generated_blocks(
    markdown: str,
    figures: Sequence[RenderedFigure],
) -> str:
    """Insert or replace marked image blocks without rewriting source prose."""

    if not isinstance(markdown, str):
        raise _error("input", "translated Markdown must be text")
    try:
        items = tuple(figures)
    except TypeError as exc:
        raise _error("manifest", "generated figures must be a sequence") from exc

    by_id: dict[str, RenderedFigure] = {}
    for figure in items:
        if not isinstance(figure, RenderedFigure):
            raise _error("manifest", "generated figures must be typed RenderedFigure objects")
        if figure.id in by_id:
            raise _error("manifest", "figure IDs must be unique")
        by_id[figure.id] = figure

    marker_ranges = _marker_ranges(markdown, set(by_id))
    operations: list[tuple[int, int, str]] = []
    for figure_id, figure in by_id.items():
        spec = figure.spec
        block = _generated_block(figure)
        if figure_id in marker_ranges:
            start, end = marker_ranges[figure_id]
            operations.append((start, end, block))
            continue

        if not isinstance(spec.anchor, str) or not spec.anchor:
            raise _error("anchor", "figure anchor was not found")
        occurrence = spec.occurrence
        anchor_positions = _anchor_occurrences_outside_markers(
            markdown, spec.anchor, marker_ranges
        )
        count = len(anchor_positions)
        if count == 0:
            raise _error("anchor", "figure anchor was not found")
        if occurrence is None:
            if count != 1:
                raise _error("anchor", "figure anchor is ambiguous")
            occurrence = 1
        if (
            isinstance(occurrence, bool)
            or not isinstance(occurrence, int)
            or occurrence < 1
            or occurrence > count
        ):
            raise _error("anchor", "figure anchor occurrence is out of range")
        anchor_start = anchor_positions[occurrence - 1]
        if spec.position == "before":
            operations.append((anchor_start, anchor_start, block + "\n"))
        elif spec.position == "after":
            anchor_end = anchor_start + len(spec.anchor)
            operations.append((anchor_end, anchor_end, "\n" + block))
        else:
            raise _error("manifest", "figure position is not supported")

    # All offsets were calculated against the same unmodified source. Applying
    # in descending order keeps every lower byte offset stable.
    operations.sort(key=lambda operation: (operation[0], operation[1]), reverse=True)
    previous_start = len(markdown) + 1
    result = markdown
    for start, end, replacement in operations:
        if end > previous_start:
            raise _error("anchor", "generated figure blocks overlap")
        result = result[:start] + replacement + result[end:]
        previous_start = start
    return result


def _coerce_path(value: object, field_name: str) -> Path:
    if not isinstance(value, (str, Path, os.PathLike)):
        raise _error("input", f"{field_name} path is invalid")
    try:
        path = Path(value)
        if not str(path):
            raise _error("input", f"{field_name} path is invalid")
        return path
    except (OSError, TypeError, ValueError) as exc:
        raise _error("input", f"{field_name} path is invalid") from exc


def _resolved_path(path: Path, field_name: str) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("local_io", f"{field_name} path could not be resolved") from exc


def _bundle_paths(
    markdown_path: Path,
    output_path: object,
    *,
    in_place: bool,
    input_paths: Sequence[Path] = (),
) -> tuple[Path, Path, Path]:
    """Resolve output, sibling assets, and sibling report safely."""

    source = _resolved_path(markdown_path, "Markdown")
    try:
        source_is_file = source.is_file()
    except OSError as exc:
        raise _error("input", "translated Markdown could not be read") from exc
    if not source_is_file:
        raise _error("input", "translated Markdown could not be read")
    if in_place and output_path is None:
        output = source
    elif output_path is None:
        output = source.with_name(f"{source.stem}_with_figures{source.suffix or '.md'}")
    else:
        raw_output = _coerce_path(output_path, "output")
        try:
            if ".." in raw_output.parts:
                raise _error("input", "output path traversal is not allowed")
            raw_output = raw_output.absolute()
            _reject_symlink_components(raw_output)
        except RestorationError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise _error("local_io", "output path could not be inspected") from exc
        output = _resolved_path(raw_output, "output")
    if output_path is None and not in_place:
        output = _resolved_path(output, "output")

    if in_place:
        if output != source:
            raise _error("input", "in-place mode requires the Markdown source as output")
    elif output == source:
        raise _error("input", "output would overwrite the Markdown source")

    try:
        _reject_symlink_components(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(output)
    except RestorationError:
        raise
    except OSError as exc:
        raise _error("local_io", "output directory could not be prepared") from exc

    assets = output.with_name(f"{output.stem}_assets")
    report = output.with_name(f"{output.stem}_figure_report.md")
    for candidate in (assets, report):
        try:
            _reject_symlink_components(candidate)
        except RestorationError:
            raise
    _reject_tree_symlinks(assets)
    if assets == source or report == source or assets == output or report == output:
        raise _error("input", "output bundle paths collide")
    if assets == report:
        raise _error("input", "output bundle paths collide")
    resolved_inputs = tuple(_resolved_path(path, "input") for path in input_paths)
    for target in (output, assets, report):
        for input_path in resolved_inputs:
            if in_place and target == output == source == input_path:
                continue
            if (
                target == input_path
                or target.is_relative_to(input_path)
                or input_path.is_relative_to(target)
            ):
                raise _error("input", "output bundle collides with an input")
    try:
        if output.exists() and not output.is_file():
            raise _error("local_io", "output path is not a file")
        if report.exists() and not report.is_file():
            raise _error("local_io", "report path is not a file")
        if assets.exists() and not assets.is_dir():
            raise _error("local_io", "asset path is not a directory")
    except OSError as exc:
        raise _error("local_io", "output bundle path could not be checked") from exc
    return output, assets, report


def _read_markdown(path: Path) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error("input", "translated Markdown is not UTF-8") from exc
    except OSError as exc:
        raise _error("input", "translated Markdown could not be read") from exc


def _report_safe_text(value: object) -> str:
    """Render untrusted report cells without absolute paths or line breaks."""

    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|/)[^\s|]+", "[path]", text)
    return text.replace("|", "\\|").strip()


def _coverage_report(
    source: Path,
    output: Path,
    specs: Sequence[FigureSpec],
) -> str:
    counts = {status: 0 for status in sorted(_STATUSES)}
    for spec in specs:
        counts[spec.status] = counts.get(spec.status, 0) + 1
    incomplete = counts.get("blocked", 0) > 0
    lines = [
        "# Figure restoration report",
        "",
        f"Status: {'INCOMPLETE' if incomplete else 'COMPLETE'}",
        f"Source basename: {_report_safe_text(source.name)}",
        f"Output basename: {_report_safe_text(output.name)}",
        f"Total manifest figures: {len(specs)}",
        "",
        "## Counts by state",
        "",
    ]
    for status in ("restore", "already-present", "skip", "blocked"):
        lines.append(f"- {status}: {counts.get(status, 0)}")
    lines.extend(
        [
            "",
            "## Figure coverage",
            "",
            "| ID | Status | Page | Asset | Reason |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for spec in specs:
        page = str(spec.page) if spec.page is not None else "—"
        if spec.status == "restore":
            asset = spec.filename or "—"
            reason = "—"
        elif spec.status == "already-present":
            asset = spec.existing_asset or "—"
            reason = "verified existing asset"
        else:
            asset = "—"
            reason = spec.reason or "—"
        lines.append(
            "| "
            + " | ".join(
                (
                    _report_safe_text(spec.id),
                    _report_safe_text(spec.status),
                    _report_safe_text(page),
                    _report_safe_text(asset),
                    _report_safe_text(reason),
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _remove_published_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        elif path.exists() or path.is_symlink():
            path.unlink()
    except OSError:
        # Rollback is best effort; the original exception remains the useful
        # categorized failure for the caller.
        pass


def _publish_bundle(
    *,
    stage_root: Path,
    staged_output: Path,
    staged_report: Path,
    staged_assets: Path,
    output: Path,
    report: Path,
    assets: Path,
) -> None:
    """Publish staged siblings with a journal that can restore old targets."""

    backup_root = stage_root / ".backups"
    try:
        backup_root.mkdir()
    except OSError as exc:
        raise _error("local_io", "publication staging could not be prepared") from exc
    journal: list[tuple[Path, Path | None, bool]] = []
    assets_created = False
    try:
        for staged, target in ((staged_output, output), (staged_report, report)):
            old_exists = target.exists() or target.is_symlink()
            backup: Path | None = None
            if old_exists:
                backup = backup_root / f"backup-{len(journal)}"
                os.replace(target, backup)
            journal.append((target, backup, old_exists))
            os.replace(staged, target)

        if not assets.exists():
            assets.mkdir()
            assets_created = True
        elif not assets.is_dir():
            raise _error("local_io", "asset path is not a directory")

        staged_files = sorted(
            (path for path in staged_assets.iterdir() if path.is_file()),
            key=lambda path: path.name,
        )
        for staged_file in staged_files:
            target = assets / staged_file.name
            _reject_symlink_components(target)
            old_exists = target.exists() or target.is_symlink()
            backup = None
            if old_exists:
                backup = backup_root / f"backup-{len(journal)}"
                os.replace(target, backup)
            journal.append((target, backup, old_exists))
            os.replace(staged_file, target)
    except RestorationError:
        _rollback_bundle(journal, assets, assets_created)
        raise
    except OSError as exc:
        _rollback_bundle(journal, assets, assets_created)
        raise _error("local_io", "published bundle could not be written") from exc


def _rollback_bundle(
    journal: Sequence[tuple[Path, Path | None, bool]],
    assets: Path,
    assets_created: bool,
) -> None:
    for target, backup, old_exists in reversed(journal):
        if target.exists() or target.is_symlink():
            _remove_published_path(target)
        if old_exists and backup is not None and backup.exists():
            try:
                os.replace(backup, target)
            except OSError:
                pass
    if assets_created:
        _remove_published_path(assets)


def _create_in_place_backup(source: Path) -> Path:
    backup = source.with_name(source.name + ".bak")
    try:
        _reject_symlink_components(backup)
        shutil.copy2(source, backup)
    except RestorationError:
        raise
    except OSError as exc:
        raise _error("local_io", "in-place Markdown backup could not be created") from exc
    return backup


def restore_figures(
    pdf_path: object,
    markdown_path: object,
    manifest_path: object,
    *,
    output_path: object = None,
    dpi: int = 220,
    in_place: bool = False,
) -> RestorationResult:
    """Validate, render, and publish a portable restored-figure bundle."""

    pdf = _resolved_path(_coerce_path(pdf_path, "PDF"), "PDF")
    markdown_file = _resolved_path(
        _coerce_path(markdown_path, "Markdown"), "Markdown"
    )
    manifest_file = _resolved_path(
        _coerce_path(manifest_path, "manifest"), "manifest"
    )
    if not pdf.is_file():
        raise _error("input", "PDF could not be read")
    output, assets, report = _bundle_paths(
        markdown_file,
        output_path,
        in_place=bool(in_place),
        input_paths=(pdf, markdown_file, manifest_file),
    )
    _, markdown = _read_markdown(markdown_file)
    manifest = load_manifest(manifest_file)

    try:
        document = pymupdf.open(pdf)
    except Exception as exc:
        raise _error("input", "PDF could not be opened") from exc

    stage_root: Path | None = None
    try:
        specs = validate_manifest(manifest, document, markdown, assets)
        if any(
            spec.status == "restore" and spec.filename == markdown_file.name
            for spec in specs
        ):
            raise _error("manifest", "generated asset filename collides with Markdown")
        try:
            stage_root = Path(
                tempfile.mkdtemp(prefix=".figure-restorer-", dir=str(output.parent))
            )
            staged_assets = stage_root / assets.name
            staged_assets.mkdir()
            rendered = render_figure_crops(document, specs, staged_assets, dpi)
            generated_markdown = apply_generated_blocks(markdown, rendered)
            staged_output = stage_root / output.name
            staged_report = stage_root / report.name
            staged_output.write_bytes(generated_markdown.encode("utf-8"))
            staged_report.write_text(
                _coverage_report(markdown_file, output, specs),
                encoding="utf-8",
            )
        except RestorationError:
            raise
        except (OSError, UnicodeError) as exc:
            raise _error("local_io", "staged bundle could not be written") from exc
        except Exception as exc:
            raise _error("local_io", "staged bundle could not be written") from exc

        if in_place:
            _create_in_place_backup(markdown_file)
        _publish_bundle(
            stage_root=stage_root,
            staged_output=staged_output,
            staged_report=staged_report,
            staged_assets=staged_assets,
            output=output,
            report=report,
            assets=assets,
        )
        blocked = any(spec.status == "blocked" for spec in specs)
        return RestorationResult(
            output_path=output,
            assets_dir=assets,
            report_path=report,
            figures=tuple(specs),
            blocked=blocked,
            exit_code=4 if blocked else 0,
        )
    finally:
        document.close()
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="restore_figures.py",
        description="Render approved PDF crops into a translated Markdown bundle.",
    )
    parser.add_argument("PDF", help="authoritative PDF")
    parser.add_argument("MARKDOWN", help="existing translated Markdown")
    parser.add_argument("MANIFEST", help="version-1 figure manifest JSON")
    parser.add_argument("--output", metavar="PATH", help="output Markdown path")
    parser.add_argument("--dpi", metavar="N", type=int, default=220)
    parser.add_argument("--in-place", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _cli_parser().parse_args(argv)
        result = restore_figures(
            args.PDF,
            args.MARKDOWN,
            args.MANIFEST,
            output_path=args.output,
            dpi=args.dpi,
            in_place=args.in_place,
        )
        return result.exit_code
    except RestorationError as exc:
        exit_code = 2 if exc.category in {"input", "manifest", "anchor"} else 3
        print(f"error: {exc.category}: {exc.message}", file=sys.stderr)
        return exit_code
    except Exception:
        print("error: local_io: operation failed", file=sys.stderr)
        return 3


__all__ = [
    "FigureSpec",
    "Manifest",
    "RenderedFigure",
    "RestorationResult",
    "RestorationError",
    "apply_generated_blocks",
    "load_manifest",
    "main",
    "render_figure_crops",
    "restore_figures",
    "validate_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
