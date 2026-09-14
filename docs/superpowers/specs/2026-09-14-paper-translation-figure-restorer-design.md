# Paper Translation Figure Restorer — Design

## Purpose

Create a new Codex skill, `paper-translation-figure-restorer`, for the case where a user already has a satisfactory translated academic-paper Markdown file and the original PDF, but the Markdown is missing figures or contains caption-only placeholders.

The skill lives in its own `paper-translation-figure-restorer` Git repository. It must not be nested in, coupled to, or committed through the existing `paper-translator-skill` repository.

The skill restores visual assets without retranslating or broadly rewriting the document. The PDF remains authoritative for figure identity, boundaries, numbering, and visual content.

## Trigger and non-goals

Trigger when the user supplies both an academic PDF and an existing translated Markdown document and asks to add, restore, recover, complete, or repair its figures/images.

Do not trigger for:

- translating a PDF from scratch;
- general PDF conversion or OCR;
- extracting every decorative image from a PDF;
- replacing good Markdown tables with screenshots;
- redesigning, redrawing, or translating text inside figures; or
- editing the prose merely because the model prefers different wording.

The skill must not call MinerU or another external service by default. Its task is local visual reconciliation, not a second text-extraction pipeline.

## Inputs and outputs

Required inputs:

1. the authoritative source PDF; and
2. the existing translated Markdown file.

Default outputs:

- `<markdown-stem>_with_figures.md`, leaving the input Markdown untouched;
- `<output-markdown-stem>_assets/`, containing PNG figure crops; and
- `<output-markdown-stem>_figure_report.md`, recording coverage, skipped items, ambiguities, and non-visual anomalies.

An in-place update is permitted only when the user explicitly requests it. Even then, create a recoverable backup or use transactional replacement.

Image links must be relative POSIX paths and remain valid when the output Markdown and its asset directory move together.

## Core workflow

### 1. Inventory both documents

Read the Markdown without changing it. Build a Markdown inventory of figure identifiers, translated captions, existing image links, tables, and likely insertion anchors.

Render and inspect the PDF. Build a PDF inventory of figure identifiers, captions, page numbers, and likely visual bounds. Count figures independently from the Markdown; do not assume the Markdown inventory is complete.

Reconcile both inventories into one manifest. Every PDF figure must end in one of these states:

- `restore`: crop and insert;
- `already-present`: verify the existing linked asset;
- `skip`: non-substantive decoration, with a recorded reason; or
- `blocked`: uncertain mapping or boundary requiring user review.

### 2. Let model vision determine boundaries

The active model must visually inspect every page containing a figure and determine the complete figure boundary. Figure captions, nearby body text, page furniture, and adjacent figures must not leak into the crop unless they are integral to the figure.

Use page-coordinate scripts only as measurement and rendering helpers. Embedded-image enumeration is insufficient because academic figures may be vector graphics, composite panels, code blocks, plots, or page-native text. A helper exiting successfully is not evidence that a crop is correct.

For multi-panel figures, preserve the complete composite as one asset unless the PDF or Markdown treats panels as independently numbered figures. For figures spanning pages, emit ordered assets and one insertion block with a clear shared identifier.

### 3. Render deterministically

Represent approved crops in a manifest containing at least figure ID, one-based PDF page, PDF-point bounding box, insertion anchor, insertion position, output filename, and alt text. Default to inserting the image immediately before its matched translated caption; allow `after` only when the document's established structure requires it.

A bundled helper validates the manifest, renders crops at a readable resolution, writes assets through a staging directory, inserts marked image blocks into a copy of the Markdown, and writes a coverage report. It must reject path traversal, invalid pages, non-finite/out-of-page rectangles, duplicate figure IDs, ambiguous or missing anchors, output/input path collisions, and asset names outside the sibling asset directory.

Repeated execution against its own output must be idempotent: update or preserve the marked block instead of inserting a duplicate.

### 4. Preserve the translation

Outside generated image blocks, preserve the Markdown byte-for-byte. Do not rewrite headings, paragraphs, formulas, tables, references, captions, or whitespace. A detected text/formula/table discrepancy belongs in the report, not in an unsolicited edit.

Do not replace a structured Markdown table with a screenshot. A figure that contains a table-like visual remains a figure when the PDF labels it as one.

### 5. Visual and structural audit

After rendering, inspect each generated crop and its Markdown placement. Verify:

- every substantive PDF figure is restored, already present, skipped with reason, or blocked;
- numbering and captions agree between PDF, manifest, Markdown, and filenames;
- crops contain the entire figure and exclude unrelated page content;
- small labels, legends, axes, code, prompts, and multi-panel details remain readable;
- each relative link resolves within the portable bundle; and
- no existing prose or structured table changed outside generated blocks.

The workflow is incomplete while any substantive figure is silently absent. `blocked` items may be delivered only with a prominent report and must never be described as complete.

## Bundled resources

The new skill should contain:

```text
paper-translation-figure-restorer/
├── SKILL.md
├── references/
│   └── manifest-format.md
└── scripts/
    └── restore_figures.py
```

`SKILL.md` owns decisions and model behavior. `manifest-format.md` documents the task-specific manifest. `restore_figures.py` is a deterministic rendering/insertion example and must explicitly state that crop selection is model-owned and paper-specific.

## Testing strategy

### Deterministic tests

Use a generated synthetic PDF and Markdown fixture to verify:

- correct crop rendering and relative link insertion;
- source Markdown preservation;
- default non-destructive output naming;
- idempotent reruns;
- multi-figure and repeated-caption handling;
- transactional rollback;
- manifest, page, rectangle, anchor, and path validation; and
- a complete coverage report with blocked/skipped states.

Add static Skill-contract tests for trigger boundaries, no-retranslation rules, model-owned visual inspection, no MinerU dependency, table preservation, and the rule that helper success is not completeness proof.

### Skill evaluations

Evaluate at least three realistic cases:

1. a translated paper with caption-only placeholders and mixed vector/raster figures;
2. a translated paper that already contains some figures and good Markdown tables; and
3. an ambiguous layout with multiple figures on one page or mismatched numbering.

Compare with-skill behavior against a no-skill baseline. Grade non-destructive editing, figure coverage, crop correctness, link portability, table preservation, ambiguity reporting, and absence of unnecessary retranslating.

Use the supplied `paper.pdf` and `SAGE_论文完整中文翻译.md` as a local acceptance example, but do not commit either user document or generated acceptance output unless the user explicitly asks.

## Security and operational boundaries

- Process files locally; no uploads or external API calls.
- Never execute code copied from the paper or Markdown.
- Treat PDF paths, manifest strings, captions, and Markdown anchors as untrusted input.
- Do not write outside the requested output Markdown directory and its sibling asset/report paths.
- Do not modify dependency files while running the skill; report a missing dependency with installation guidance rather than changing the user's project.
- Scripts are examples for adaptation, not universal layout detectors.

## Acceptance criteria

The skill is ready when:

- its structure passes the Codex Skill validator;
- deterministic and contract tests pass;
- the SAGE acceptance run can account for figures 1–15 without altering the translated prose or Markdown tables;
- all generated links resolve and all crops pass visual review;
- review finds no Critical or Important issues; and
- repository scans show no user PDF, translated Markdown, generated acceptance assets, secrets, or machine-specific paths were committed.
