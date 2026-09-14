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

The required and optional fields are:

| Status | Required fields | Optional fields |
| --- | --- | --- |
| `restore` | `id`, `status`, `page`, `bbox`, `anchor`, `filename`, `alt` | `occurrence`, `position` |
| `already-present` | `id`, `status`, `existing_asset` | — |
| `skip` | `id`, `status`, `reason` | — |
| `blocked` | `id`, `status`, `reason` | — |

`existing_asset` is the relative link to a verified existing image. Use
`already-present` only after inspecting that asset and confirming its identity
and coverage. A `skip` reason must identify a non-substantive decorative
element; do not use it to hide an uncertain mapping. A `blocked` reason must
describe the ambiguity or boundary requiring user review. Blocked work is not
complete.

`position` is optional; when omitted it defaults to `before`, and its only
accepted values are `before` and `after`. `before` inserts immediately before
the matched translated caption unless the document's established structure
requires `after`.

`occurrence` is optional only when the anchor occurs exactly once. When an
anchor occurs more than once, `occurrence` is required, one-based, and selects
the intended occurrence.

Filenames and existing assets are relative POSIX paths within the output's
sibling asset directory. Reject absolute paths, `..` traversal, separators
that escape that directory, and names that collide with the Markdown input.
The source Markdown is preserved outside helper-generated marked image blocks;
never replace a structured Markdown table with a screenshot. Crop decisions
are model-owned and paper-specific, not inferred solely from embedded-image
enumeration or a successful helper run.
