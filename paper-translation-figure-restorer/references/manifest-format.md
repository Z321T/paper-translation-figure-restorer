# Figure restoration manifest format

The helper consumes one paper-specific JSON object. Version 1 has exactly two
top-level members: integer `version` and array `figures`. The `figures` array
contains one object per PDF figure, with one explicit status: `restore`,
`already-present`, `skip`, or `blocked`.

In JSON form, the allowed status values are `"restore"`,
`"already-present"`, `"skip"`, and `"blocked"`.

## Version-1 example

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

The sample illustrates the complete placement and crop fields for a
`restore` item. IDs are unique within the manifest. PDF `page` numbers are
one-based. `bbox` is a finite PDF-point rectangle `[x0, y0, x1, y1]` inside the
page, with positive width and height, selected after visual inspection.

## Status-specific fields

Every item requires `id` and `status`.

- `restore` requires `page`, `bbox`, `anchor`, `position`, `filename`, and
  `alt`. It may include `occurrence` when the anchor is repeated.
- `already-present` requires `existing_asset`, the relative link to the
  verified existing image. Use this only after inspecting that asset and
  confirming its identity and coverage.
- `skip` requires a specific `reason`, such as a non-substantive decorative
  element. Do not use it to hide an uncertain mapping.
- `blocked` requires a specific `reason` describing the ambiguity or boundary
  requiring user review. A blocked item is prominently reported and is not
  complete.

`occurrence` is optional only when the anchor occurs exactly once. When an
anchor occurs more than once, `occurrence` is required, one-based, and selects
the intended occurrence. `position` defaults to `before` and accepts only
`before` or `after`; `before` inserts immediately before the matched translated
caption unless the document's established structure requires `after`.

Filenames and existing assets are relative POSIX paths within the output's
sibling asset directory. Reject absolute paths, `..` traversal, separators
that escape that directory, and names that collide with the Markdown input.
The source Markdown is preserved outside helper-generated marked image blocks;
never replace a structured Markdown table with a screenshot. Crop decisions
are model-owned and paper-specific, not inferred solely from embedded-image
enumeration or a successful helper run.
