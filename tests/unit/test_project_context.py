from pathlib import Path

from orchestrator.services.project_context import (
    MAX_PROJECT_CONTEXT_CHARS,
    load_project_context,
    project_context_prompt,
)


def test_load_project_context_reads_root_project_file(tmp_path: Path) -> None:
    (tmp_path / "PROJECT.md").write_text("# Durable context\nA stable rule.", encoding="utf-8")

    assert load_project_context(tmp_path) == "# Durable context\nA stable rule."
    assert "A stable rule." in project_context_prompt(tmp_path)


def test_missing_project_context_is_omitted_without_error(tmp_path: Path) -> None:
    assert load_project_context(tmp_path) is None
    assert project_context_prompt(tmp_path) == ""


def test_project_context_prompt_caps_oversized_content(tmp_path: Path) -> None:
    (tmp_path / "PROJECT.md").write_text(
        "x" * (MAX_PROJECT_CONTEXT_CHARS + 1), encoding="utf-8"
    )

    prompt = project_context_prompt(tmp_path)

    assert "[PROJECT.md truncated; inspect the repository file for the full context.]" in prompt
    assert "x" * (MAX_PROJECT_CONTEXT_CHARS + 1) not in prompt
