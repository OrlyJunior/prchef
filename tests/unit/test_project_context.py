from pathlib import Path

from prchef.services.project_context import (
    MAX_PROJECT_CONTEXT_CHARS,
    load_project_context,
    project_context_prompt,
)


def test_load_project_context_reads_docs_project_context_file(tmp_path: Path) -> None:
    context_file = tmp_path / "docs" / "PROJECT_CONTEXT.md"
    context_file.parent.mkdir()
    context_file.write_text("# Durable context\nA stable rule.", encoding="utf-8")

    assert load_project_context(tmp_path) == "# Durable context\nA stable rule."
    prompt = project_context_prompt(tmp_path)
    assert "DURABLE PROJECT CONTEXT (docs/PROJECT_CONTEXT.md):" in prompt
    assert "A stable rule." in prompt


def test_missing_project_context_is_omitted_without_error(tmp_path: Path) -> None:
    assert load_project_context(tmp_path) is None
    assert project_context_prompt(tmp_path) == ""


def test_load_project_context_ignores_root_project_md(tmp_path: Path) -> None:
    (tmp_path / "PROJECT.md").write_text("Legacy context", encoding="utf-8")

    assert load_project_context(tmp_path) is None
    assert project_context_prompt(tmp_path) == ""


def test_project_context_prompt_caps_oversized_content(tmp_path: Path) -> None:
    context_file = tmp_path / "docs" / "PROJECT_CONTEXT.md"
    context_file.parent.mkdir()
    context_file.write_text(
        "x" * (MAX_PROJECT_CONTEXT_CHARS + 1), encoding="utf-8"
    )

    prompt = project_context_prompt(tmp_path)

    assert (
        "[docs/PROJECT_CONTEXT.md truncated; inspect the repository file for the full context.]"
        in prompt
    )
    assert "x" * (MAX_PROJECT_CONTEXT_CHARS + 1) not in prompt


def test_project_context_warning_uses_stripped_character_count(tmp_path: Path) -> None:
    context_file = tmp_path / "docs" / "PROJECT_CONTEXT.md"
    context_file.parent.mkdir()
    reported: list[tuple[Path, int, int]] = []

    def callback(path: Path, actual: int, cap: int) -> None:
        reported.append((path, actual, cap))

    context_file.write_text("x" * MAX_PROJECT_CONTEXT_CHARS + "\n", encoding="utf-8")
    exact_prompt = project_context_prompt(tmp_path, callback)
    assert reported == []
    assert "truncated" not in exact_prompt
    assert exact_prompt == project_context_prompt(tmp_path)

    context_file.write_text("x" * (MAX_PROJECT_CONTEXT_CHARS + 1) + " \n", encoding="utf-8")
    oversized_prompt = project_context_prompt(tmp_path, callback)
    assert reported == [(context_file, MAX_PROJECT_CONTEXT_CHARS + 1, MAX_PROJECT_CONTEXT_CHARS)]
    assert "[docs/PROJECT_CONTEXT.md truncated;" in oversized_prompt
    assert oversized_prompt == project_context_prompt(tmp_path)


def test_missing_project_context_does_not_warn(tmp_path: Path) -> None:
    reported: list[tuple[Path, int, int]] = []
    assert project_context_prompt(tmp_path, lambda *values: reported.append(values)) == ""
    assert reported == []


def test_repository_durable_context_stays_within_prompt_cap() -> None:
    repository_path = Path(__file__).resolve().parents[2]
    content = load_project_context(repository_path)
    assert content is not None
    assert len(content) <= MAX_PROJECT_CONTEXT_CHARS, (
        f"docs/PROJECT_CONTEXT.md has {len(content)} stripped characters; "
        f"the prompt cap is {MAX_PROJECT_CONTEXT_CHARS}"
    )
