# Paper Translation Figure Restorer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate a standalone Codex skill that restores figures from an authoritative academic PDF into an already translated Markdown document without retranslating or rewriting its prose.

**Architecture:** The model owns visual inventory, PDF/Markdown reconciliation, crop selection, and final visual audit. A local deterministic Python helper consumes a task-specific JSON manifest, validates all paths/pages/crops/anchors, renders approved crops with PyMuPDF, inserts marker-delimited image blocks transactionally, and emits a coverage report. The source Markdown is unchanged by default; MinerU and all external services are outside this skill.

**Tech Stack:** Python 3.12+, PyMuPDF, standard-library `argparse`/`json`/`pathlib`/`tempfile`/`shutil`, pytest, Codex Skill Markdown.

**Spec:** `docs/superpowers/specs/2026-09-14-paper-translation-figure-restorer-design.md`

## Global Constraints

- This is an independent `paper-translation-figure-restorer` repository; do not modify or commit through `paper-translator-skill`.
- Required inputs are one authoritative PDF and one existing translated Markdown document.
- Process locally; do not call MinerU, upload files, browse the web, or use any external API.
- The model owns visual figure boundaries. Scripts are deterministic rendering/insertion examples, never universal layout detectors or completeness proofs.
- Outside generated marker blocks, preserve the Markdown byte-for-byte. Report text/formula/table anomalies instead of editing them.
- Do not replace good Markdown tables with screenshots.
- Default output is `<markdown-stem>_with_figures.md`; never overwrite the source unless the user explicitly requests in-place mode.
- Output assets live at `<output-markdown-stem>_assets/`; report lives at `<output-markdown-stem>_figure_report.md`.
- Image links are relative POSIX paths contained within the portable Markdown bundle.
- Treat PDFs, JSON fields, anchors, captions, and paths as untrusted. Reject traversal, duplicate IDs, invalid pages/crops, ambiguous anchors, and unsafe output paths.
- Do not commit the supplied `paper.pdf`, `SAGE_论文完整中文翻译.md`, generated SAGE assets, secrets, or machine-specific paths.
- Every implementation task uses TDD, records RED and GREEN evidence, commits only its scoped files, and may not dispatch subagents.

---

### Task 1: Repository scaffold and behavioral Skill contract

**Files:**
- Create: `.gitignore`
- Create: `README.md`
- Create: `pyproject.toml`
- Create: `paper-translation-figure-restorer/SKILL.md`
- Create: `paper-translation-figure-restorer/references/manifest-format.md`
- Create: `tests/test_skill_contract.py`

**Interfaces:**
- Consumes: the approved design specification and Global Constraints above.
- Produces: skill name `paper-translation-figure-restorer`; CLI contract `restore_figures.py PDF MARKDOWN MANIFEST [--output PATH] [--dpi N] [--in-place]`; manifest version `1`; documented figure states `restore`, `already-present`, `skip`, `blocked`.

- [ ] **Step 1: Write failing contract tests**

Create tests that load the Skill and reference text and assert behaviorally meaningful requirements:

```python
def test_skill_is_post_translation_and_non_destructive():
    text = SKILL.read_text(encoding="utf-8")
    assert "already translated" in text
    assert "Do not retranslate" in text
    assert "_with_figures.md" in text

def test_skill_makes_visual_judgment_model_owned():
    text = SKILL.read_text(encoding="utf-8")
    assert "visually inspect every PDF page containing a figure" in text
    assert "successful helper run is not evidence" in text

def test_skill_forbids_external_processing_and_table_regression():
    combined = SKILL.read_text(encoding="utf-8") + REFERENCE.read_text(encoding="utf-8")
    assert "Do not call MinerU" in combined
    assert "Do not replace a structured Markdown table" in combined
```

Also test that the YAML frontmatter has the exact name and a trigger description covering an original PDF plus an existing translated Markdown with missing figures, while excluding translation-from-scratch language from the workflow.

- [ ] **Step 2: Run the tests and confirm RED**

Run: `uv run pytest tests/test_skill_contract.py -v`

Expected: FAIL because the new Skill files do not exist.

- [ ] **Step 3: Create the minimal repository and Skill documentation**

Use this repository shape:

```text
paper-translation-figure-restorer/
├── SKILL.md
├── references/
│   └── manifest-format.md
└── scripts/
    └── restore_figures.py  # added by later tasks
```

`SKILL.md` must define this ordered workflow:

1. resolve PDF, translated Markdown, scope, and non-destructive output;
2. inventory Markdown figures and existing links;
3. render and visually inspect all PDF figure pages;
4. reconcile each PDF figure to one explicit state;
5. write a paper-specific manifest after visual crop selection;
6. run the helper;
7. inspect every crop and insertion;
8. audit figure coverage and prose preservation.

`manifest-format.md` must define the exact version-1 JSON object:

```json
{
  "version": 1,
  "figures": [
    {
      "id": "figure-1",
      "status": "restore",
      "page": 2,
      "bbox": [72.0, 120.0, 540.0, 420.0],
      "anchor": "**图 1：系统概览。**",
      "occurrence": 1,
      "position": "before",
      "filename": "figure-001.png",
      "alt": "图 1：系统概览"
    }
  ]
}
```

Document state-specific required fields: `restore` uses all shown placement/crop fields; `already-present` uses `existing_asset`; `skip` and `blocked` require `reason`. `occurrence` is one-based and optional only when the anchor occurs exactly once. `position` defaults to `before` and accepts only `before` or `after`.

Create `pyproject.toml` with project name `paper-translation-figure-restorer`, version `0.1.0`, Python `>=3.12`, runtime dependency `pymupdf>=1.28.2`, and dev dependency `pytest>=9.1.1`. Ignore `.venv/`, caches, generated acceptance output, and common `.env` files, but do not ignore committed test fixtures.

- [ ] **Step 4: Run contract tests and Skill validator**

Run:

```text
uv run pytest tests/test_skill_contract.py -v
python /home/lucky/.codex/skills/.system/skill-creator/scripts/quick_validate.py paper-translation-figure-restorer
```

Expected: all tests PASS and validator prints `Skill is valid!`.

- [ ] **Step 5: Commit Task 1**

```text
git add .gitignore README.md pyproject.toml uv.lock paper-translation-figure-restorer tests/test_skill_contract.py
git commit -m "feat: define figure restoration skill contract"
```

### Task 2: Safe manifest validation and deterministic crop rendering

**Files:**
- Create: `paper-translation-figure-restorer/scripts/restore_figures.py`
- Create: `tests/test_restore_figures.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Consumes: manifest version `1` and state field contracts from Task 1.
- Produces:
  - `RestorationError(category: str, message: str)` with safe categories `input`, `manifest`, `anchor`, `render`, `local_io`;
  - `load_manifest(path: Path) -> Manifest`;
  - `validate_manifest(manifest: Manifest, document: pymupdf.Document, markdown: str, assets_dir: Path) -> list[FigureSpec]`;
  - `render_figure_crops(document: pymupdf.Document, specs: Sequence[FigureSpec], staging_assets: Path, dpi: int) -> list[RenderedFigure]`.

- [ ] **Step 1: Generate controlled PDF/Markdown fixtures**

In `tests/conftest.py`, create a two-page PDF at test time with PyMuPDF: page 1 contains a colored vector rectangle labeled `FIGURE ONE`; page 2 contains two separated labeled rectangles. Create a translated Markdown fixture containing unique captions for figure 1 and figure 2 plus a real Markdown table. Do not commit binary fixtures.

- [ ] **Step 2: Write failing manifest and rendering tests**

Test real observable behavior:

- a valid crop produces a non-empty PNG whose pixel dimensions correspond to the requested PDF-point rectangle and DPI;
- vector/page-native content is present in the rendered crop;
- duplicate IDs and duplicate output filenames fail;
- unknown states and state-specific missing fields fail;
- path traversal, absolute filenames, separators, and assets escaping the sibling directory fail;
- zero/negative/non-finite DPI fails;
- page zero/out-of-range and non-finite/reversed/out-of-page rectangles fail;
- missing, zero-occurrence, and ambiguous anchors fail unless a valid one-based occurrence resolves them;
- `already-present` requires a relative existing asset inside the bundle;
- `skip`/`blocked` require a non-empty reason.

Each test names the production mutation it catches and uses hand-derived expected values.

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `uv run pytest tests/test_restore_figures.py -v`

Expected: FAIL because `restore_figures.py` is absent.

- [ ] **Step 4: Implement minimal validation and rendering**

Use frozen dataclasses with secret-free reprs where useful. Parse JSON into typed state objects; never carry unchecked dictionaries into rendering. Normalize paths with `Path.resolve()`/`is_relative_to()` and require basenames for generated asset filenames. Validate rectangles against `document[page - 1].rect` before calling `get_pixmap(dpi=dpi, clip=rect, alpha=False)`.

Catch PyMuPDF decode/render errors and local filesystem failures at their boundary and raise a sanitized `RestorationError`; do not print source content or a full manifest in errors.

- [ ] **Step 5: Run focused and full tests**

Run:

```text
uv run pytest tests/test_restore_figures.py -v
uv run pytest -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit Task 2**

```text
git add paper-translation-figure-restorer/scripts/restore_figures.py tests/conftest.py tests/test_restore_figures.py
git commit -m "feat: validate manifests and render figure crops"
```

### Task 3: Transactional Markdown insertion, reporting, and CLI

**Files:**
- Modify: `paper-translation-figure-restorer/scripts/restore_figures.py`
- Modify: `tests/test_restore_figures.py`
- Modify: `paper-translation-figure-restorer/references/manifest-format.md`

**Interfaces:**
- Consumes: validated `FigureSpec` and `RenderedFigure` objects from Task 2.
- Produces:
  - `apply_generated_blocks(markdown: str, figures: Sequence[RenderedFigure]) -> str`;
  - `restore_figures(pdf_path, markdown_path, manifest_path, *, output_path=None, dpi=220, in_place=False) -> RestorationResult`;
  - CLI exit `0` on complete success, `2` for invalid input/manifest/anchor, `3` for render/local-I/O failure, `4` when output is produced but one or more figures remain `blocked`;
  - markers `<!-- figure-restorer:start <id> -->` and `<!-- figure-restorer:end <id> -->`.

- [ ] **Step 1: Write failing output-behavior tests**

Cover:

- default output naming and sibling assets/report paths;
- exact preservation of all source Markdown bytes outside generated blocks;
- insertion immediately before the selected caption by default and after it when requested;
- percent-encoded relative POSIX links for filenames requiring encoding;
- repeated execution updates the same figure marker block rather than duplicating it;
- an existing marker with mismatched or nested IDs is rejected;
- `already-present` is verified and reported without copying or replacing it;
- `skip` and `blocked` appear in the report with reasons;
- any `blocked` item returns CLI exit `4` and forbids a completeness claim;
- good Markdown tables remain byte-identical;
- source/output collision fails unless `--in-place` is explicit;
- explicit in-place mode creates `<markdown-name>.bak` before transactional replacement;
- a render, report, rename, or write failure leaves the original Markdown and previous published bundle unchanged;
- absolute/symlinked output escapes are rejected.

- [ ] **Step 2: Run new tests and confirm RED**

Run the new test subset by node ID and confirm failures are caused by missing insertion/transaction behavior, not fixture errors.

- [ ] **Step 3: Implement insertion and transaction pipeline**

Generate this exact block shape:

```markdown
<!-- figure-restorer:start figure-1 -->
![图 1：系统概览](SAGE_论文完整中文翻译_with_figures_assets/figure-001.png)
<!-- figure-restorer:end figure-1 -->
```

Locate anchors in the unmodified source, resolve all placements before changing any text, and apply insertions from highest byte offset to lowest. For reruns, replace the matching existing marker range. Stage Markdown, assets, and report as siblings; publish only after every render, link, and report validates. If multi-artifact publication fails, restore prior paths from unique backups.

The report must include source basenames, output basenames, total manifest count, counts by state, and a table with ID/status/page/asset/reason. It must avoid absolute paths and must say `INCOMPLETE` whenever a `blocked` state exists.

- [ ] **Step 4: Implement and test CLI**

Expose only operational arguments:

```text
restore_figures.py PDF MARKDOWN MANIFEST --output PATH --dpi 220 --in-place
```

Do not expose network, token, arbitrary command, or dependency-install options. Sanitize errors and never emit Markdown/PDF content in a traceback.

- [ ] **Step 5: Run focused tests, full suite, compile, and CLI help**

Run:

```text
uv run pytest tests/test_restore_figures.py -v
uv run pytest -v
uv run python -m compileall -q paper-translation-figure-restorer/scripts
uv run python paper-translation-figure-restorer/scripts/restore_figures.py --help
```

Expected: all commands exit `0`.

- [ ] **Step 6: Commit Task 3**

```text
git add paper-translation-figure-restorer/scripts/restore_figures.py paper-translation-figure-restorer/references/manifest-format.md tests/test_restore_figures.py
git commit -m "feat: publish portable restored-figure bundles"
```

### Task 4: Real-paper acceptance and skill evaluations

**Files:**
- Create: `evals/evals.json`
- Create: `tests/test_acceptance_contract.py`
- Modify when evidence requires: `paper-translation-figure-restorer/SKILL.md`
- Modify when evidence requires: `paper-translation-figure-restorer/references/manifest-format.md`
- Modify when evidence requires: `paper-translation-figure-restorer/scripts/restore_figures.py`

**Interfaces:**
- Consumes: the completed Skill/helper, plus external local inputs `/home/lucky/devdir/paper-translator-skill/paper.pdf` and `/home/lucky/devdir/paper-translator-skill/SAGE_论文完整中文翻译.md`.
- Produces: three committed realistic eval prompts; credential-free local acceptance evidence; ignored output under `.acceptance/` or `/tmp`; any generalized fixes revealed by evaluation.

- [ ] **Step 1: Define three eval prompts and objective assertions**

Create `evals/evals.json` for:

1. caption-only translated Markdown with mixed vector/raster figures;
2. partial existing figures plus a good Markdown table; and
3. two figures on one page with mismatched or ambiguous numbering.

Assertions cover: no prose rewriting, all PDF figures accounted for, no duplicate assets/blocks, crops visually complete, links resolve, tables unchanged, ambiguity becomes `blocked`, no external call, and output/report naming is portable.

- [ ] **Step 2: Create a SAGE paper-specific manifest through visual inspection**

Render the 22-page source locally. Identify figures 1–15 independently from both the PDF captions and Markdown headings/captions. Visually determine page-point crop boxes for each complete figure, including code/prompt/trajectory composites. Store the task-specific manifest only under ignored `.acceptance/` or `/tmp`; never commit absolute source paths or the manifest.

- [ ] **Step 3: Run the Skill on the SAGE acceptance pair**

Produce ignored acceptance output and verify:

- all figures 1–15 have distinct readable PNG assets;
- each appears once at its corresponding Markdown caption;
- output image links resolve;
- the original 15 figure captions and all eight Markdown tables remain intact;
- removing generated marker blocks from output yields the exact original Markdown bytes; and
- the report declares complete only when no item is blocked.

- [ ] **Step 4: Visually inspect every SAGE crop**

Open all 15 generated PNGs. Reject crops with clipped legends/axes/code, included body text/captions/page numbers, wrong panels, insufficient resolution, or wrong figure mapping. Adjust only the acceptance manifest crop boxes, rerun, and reinspect until all 15 pass.

- [ ] **Step 5: Run with-skill and no-skill evals**

Use fresh Luna-Max subagents for paired runs where capacity permits. Neither evaluator may mutate the repository or dispatch subagents. Save outputs and timing in an ignored sibling evaluation workspace. Grade the objective assertions and generate the Skill Creator static review HTML. Do not tune the Skill to SAGE-specific labels or coordinates; only generalize recurring failures.

- [ ] **Step 6: Add acceptance-contract tests and any minimal generalized fixes**

Tests must check the committed eval schema and the invariant that acceptance/user files and machine-specific paths are absent from tracked files. If evaluation exposes a generalized defect, write a failing regression test before changing the Skill/helper.

- [ ] **Step 7: Run all validation and commit**

Run:

```text
uv run pytest -v
python /home/lucky/.codex/skills/.system/skill-creator/scripts/quick_validate.py paper-translation-figure-restorer
git diff --check
git grep -n '/home/lucky\|paper.pdf\|SAGE_论文完整中文翻译' -- ':!docs/superpowers/*'
```

The final grep must return no matches in runtime Skill files, tests, README, or project metadata. Commit only generalized Skill/eval/test changes:

```text
git add evals/evals.json tests/test_acceptance_contract.py paper-translation-figure-restorer README.md pyproject.toml uv.lock
git commit -m "test: validate figure restoration workflow"
```

### Task 5: Release documentation and final repository audit

**Files:**
- Modify: `README.md`
- Modify if needed: `paper-translation-figure-restorer/SKILL.md`
- Modify if needed: `pyproject.toml`
- Modify if needed: `uv.lock`
- Test: `tests/test_skill_contract.py`
- Test: `tests/test_restore_figures.py`
- Test: `tests/test_acceptance_contract.py`

**Interfaces:**
- Consumes: reviewed implementation and evaluation evidence from Tasks 1–4.
- Produces: a release-ready standalone repository at version `0.1.0` with no unreviewed Critical/Important findings.

- [ ] **Step 1: Write failing release/documentation checks where gaps exist**

Assert README documents the exact two-input contract, non-destructive default outputs, local-only operation, visual-first manifest workflow, CLI example, and distinction from translation-from-scratch. Assert version consistency between README, project metadata, and any Skill release statement.

- [ ] **Step 2: Update only evidence-backed documentation gaps**

Do not expand scope. README should explain installation prerequisites, portable output layout, concise usage, safety behavior, and why crop coordinates remain paper-specific.

- [ ] **Step 3: Run final verification**

Run fresh:

```text
uv sync --frozen
uv run pytest -v
python /home/lucky/.codex/skills/.system/skill-creator/scripts/quick_validate.py paper-translation-figure-restorer
uv run python -m compileall -q paper-translation-figure-restorer/scripts
uv run python paper-translation-figure-restorer/scripts/restore_figures.py --help
git diff --check
git status --short
```

Also scan tracked files for secrets, absolute machine paths, MinerU/API calls, committed user inputs, and generated acceptance assets. Inspect the Git diff and confirm every planned requirement has a matching test or review record.

- [ ] **Step 4: Commit the release audit**

```text
git add README.md paper-translation-figure-restorer pyproject.toml uv.lock tests
git commit -m "release: prepare figure restoration skill 0.1.0"
```

- [ ] **Step 5: Request final whole-branch review**

Provide the reviewer the complete merge-base-to-HEAD diff package, approved spec, implementation plan, deferred-minor ledger, and test reports. Fix all Critical and Important findings within the bounded review process before offering integration options.
