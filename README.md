# Paper Translation Figure Restorer

`paper-translation-figure-restorer` is a local Codex skill for a paper that
already has a translated Markdown document but is missing figures or contains
caption-only placeholders. The source PDF is authoritative for figure identity
and visual boundaries. The workflow restores visual assets without
retranslating or broadly rewriting the existing translation.

## Scope

The skill consumes an original academic PDF, an existing translated Markdown
file, and a paper-specific version-1 manifest. It writes a new Markdown file
named `<markdown-stem>_with_figures.md` by default, a sibling asset directory,
and a coverage report. The input Markdown remains unchanged unless the user
explicitly requests `--in-place`; in-place operation must use a recoverable
backup or transactional replacement.

The model selects figure boundaries after visually inspecting the relevant PDF
pages. Rendering and insertion are deterministic helper operations; a
successful helper run is not proof that a crop is correct. Tables remain
structured Markdown tables, and no external service or second translation
pipeline is used.

## Command contract

After the bundled helper is available, invoke it as:

```text
restore_figures.py PDF MARKDOWN MANIFEST [--output PATH] [--dpi N] [--in-place]
```

The manifest has version `1`. Every PDF figure is accounted for as `restore`,
`already-present`, `skip` (with a reason), or `blocked` (with a reason). See
[`manifest-format.md`](paper-translation-figure-restorer/references/manifest-format.md)
for the exact object and required fields.

## Development

This repository targets Python 3.12 or newer and uses `uv` for the environment:

```bash
uv sync
uv run pytest
```

The project depends on PyMuPDF for local PDF rendering and pytest for
development checks. It does not upload paper content or call MinerU/API
services.
