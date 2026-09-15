# Paper Translation Figure Restorer

Release: 0.1.0

`paper-translation-figure-restorer` is a local-only Codex skill for restoring
substantive figures in a paper that already has a translated Markdown
document. It preserves that translation and adds the missing visual assets;
it is a post-translation repair workflow, not for translating a paper from
scratch.

## Prerequisites

- Python >=3.12
- [`uv`](https://docs.astral.sh/uv/)
- A locally readable authoritative academic PDF and an existing translated
  Markdown document

Install the pinned environment with:

```bash
uv sync --frozen
```

This installs PyMuPDF for local PDF rendering; pytest is included as the
development dependency for repository checks.

The skill is local-only: it reads the supplied files, does not upload paper
content, and makes no external API, service, or network calls. In particular,
do not call MinerU as part of this workflow.

## Inputs and scope

There are exactly two document inputs:

1. the original academic PDF, which is authoritative for figure identity and
   visual boundaries; and
2. the existing translated Markdown document, whose prose is preserved.

The required version-1 manifest is a local control file derived after visual
inspection. It records the model's paper-specific crop and anchor decisions;
it is not a third source document or a translation input. Use this skill to
add, restore, recover, or repair missing substantive figures and caption-only
placeholders. It does not translate the paper, perform general PDF
conversion/OCR, redraw figures, or replace structured Markdown tables with
screenshots.

## Visual-first manifest workflow

Before running the helper, render and visually inspect every PDF page
containing a figure. The model decides each complete figure boundary and then
writes the paper-specific version-1 manifest. PDF-point bbox coordinates remain
paper-specific because page sizes, layouts, panels, and surrounding page
furniture differ from paper to paper.

`restore_figures.py` is a deterministic executor and rendering/insertion
helper. It is not a universal layout detector: a successful helper run is not
evidence that every crop is correct or that coverage is complete. Inspect every
crop and insertion, then reconcile every substantive PDF figure as
`restore`, `already-present`, `skip`, or `blocked`.

## Usage

The operational CLI is:

```text
restore_figures.py PDF MARKDOWN MANIFEST [--output PATH] [--dpi N] [--in-place]
```

For example:

```bash
uv run python paper-translation-figure-restorer/scripts/restore_figures.py \
  source.pdf translated.md manifest.json \
  --output translated_with_figures.md --dpi 220
```

The manifest format and state-specific fields are documented in
[`paper-translation-figure-restorer/references/manifest-format.md`](paper-translation-figure-restorer/references/manifest-format.md).

## Portable output bundle

For a source named `paper.md`, the default output is:

```text
paper_with_figures.md
paper_with_figures_assets/
  figure-001.png
paper_with_figures_figure_report.md
```

Custom `--output` names derive the sibling `_assets/` directory and
`_figure_report.md` report from that output stem. Image links are relative
POSIX paths so the Markdown and its assets can be moved together.

## Safety and completion

By default, leave the input Markdown unchanged; the helper publishes a new
`<markdown-stem>_with_figures.md` bundle. It rejects path traversal,
unsafe or absolute asset paths, invalid crops, duplicate IDs, and missing or
ambiguous anchors. Generated blocks are marker-delimited and reruns replace
the matching block instead of duplicating it. Use `--in-place` only after an
explicit request; it creates a recoverable `.bak` backup before replacement.

The helper preserves Markdown bytes outside generated blocks, including
headings, prose, formulas, references, captions, whitespace, and structured
Markdown tables. A `blocked` item is reported as `INCOMPLETE`; neither that
report nor a successful helper run proves complete visual coverage.
