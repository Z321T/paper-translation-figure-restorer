"""Contract tests for committed skill evaluations and repository hygiene."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals" / "evals.json"


def _load_evals() -> dict[str, object]:
    payload = json.loads(EVALS.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [path for path in result.stdout.decode().split("\x00") if path]


def test_evals_json_has_three_realistic_cases_with_objective_assertions() -> None:
    """Catches dropping a realistic scenario or replacing assertions with prose."""

    payload = _load_evals()
    assert set(payload) == {"skill_name", "evals"}
    assert payload["skill_name"] == "paper-translation-figure-restorer"

    evals = payload["evals"]
    assert isinstance(evals, list)
    assert len(evals) == 3

    required_fields = {
        "id",
        "prompt",
        "expected_output",
        "files",
        "expectations",
    }
    ids: list[int] = []
    prompts: list[str] = []
    for case in evals:
        assert isinstance(case, dict)
        assert set(case) == required_fields
        assert isinstance(case["id"], int)
        ids.append(case["id"])
        assert isinstance(case["prompt"], str) and case["prompt"].strip()
        prompts.append(case["prompt"].lower())
        assert (
            isinstance(case["expected_output"], str)
            and case["expected_output"].strip()
        )
        assert isinstance(case["files"], list)
        assert all(isinstance(path, str) and path for path in case["files"])
        expectations = case["expectations"]
        assert isinstance(expectations, list) and expectations
        assert all(isinstance(item, str) and item.strip() for item in expectations)

    assert ids == [1, 2, 3]
    assert any("caption-only" in prompt and "vector" in prompt and "raster" in prompt for prompt in prompts)
    assert any("table" in prompt and ("already" in prompt or "partial" in prompt) for prompt in prompts)
    assert any(
        ("ambiguous" in prompt or "mismatched" in prompt)
        and ("one page" in prompt or "same page" in prompt)
        for prompt in prompts
    )


def test_eval_assertions_cover_safety_and_audit_outcomes() -> None:
    """Catches evals that only check whether an output file was produced."""

    payload = _load_evals()
    evals = payload["evals"]
    assert isinstance(evals, list)
    assertions = " ".join(
        assertion.lower()
        for case in evals
        for assertion in case["expectations"]
    )
    for required in (
        "prose",
        "accounted",
        "duplicate",
        "visually",
        "link",
        "table",
        "blocked",
        "external",
        "portable",
    ):
        assert required in assertions


def test_tracked_files_exclude_acceptance_inputs_outputs_and_machine_paths() -> None:
    """Catches committing user documents, generated crops, or host-specific paths."""

    tracked = _tracked_paths()
    # Construct sensitive names without embedding the complete user filenames in
    # this tracked test, so the hygiene check itself does not trip repository scans.
    user_pdf = "paper" + ".pdf"
    user_translation = "SAGE_" + "论文完整中文翻译" + ".md"
    forbidden_path_fragments = (
        user_pdf,
        user_translation,
        ".acceptance/",
        "acceptance/",
        "acceptance-output/",
        "generated-acceptance/",
    )
    machine_root = "/" + "home/lucky"

    for path in tracked:
        if path.startswith("docs/superpowers/"):
            continue
        assert not any(fragment in path for fragment in forbidden_path_fragments)
        assert machine_root not in path
