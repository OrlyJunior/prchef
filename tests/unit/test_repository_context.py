from pathlib import Path

from pytest import MonkeyPatch

from orchestrator.models.development_task import DevelopmentTask
from orchestrator.services.repository_context import (
    IGNORED_DIRECTORY_NAMES,
    MAX_CONTEXT_CHARS,
    RepositoryContextCollector,
    collect_repository_context,
)


def _task() -> DevelopmentTask:
    return DevelopmentTask(
        id="context-1",
        source="test",
        title="Ground planner repository context",
        description="Include likely planner implementation symbols.",
        url="https://example.test/tasks/context-1",
    )


def test_collects_structure_and_task_matched_code_without_ignored_directories(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "planner_context.py"
    source.parent.mkdir()
    source.write_text(
        "class PlannerContext:\n    def collect_repository_context(self):\n        return 'ok'\n",
        encoding="utf-8",
    )
    ignored = tmp_path / "node_modules" / "package.js"
    ignored.parent.mkdir()
    ignored.write_text("planner context", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    context = collect_repository_context(tmp_path, _task())

    assert "PROJECT STRUCTURE:" in context
    assert "`src/`" in context
    assert "src/planner_context.py" in context
    assert "LIKELY RELEVANT FILES:" in context
    assert "PlannerContext" in context
    assert "node_modules" not in context
    assert ".git" in IGNORED_DIRECTORY_NAMES


def test_context_is_strictly_bounded(tmp_path: Path) -> None:
    for index in range(12):
        (tmp_path / f"planner_{index}.py").write_text(
            "def planner_context():\n" + "planner context " * 2_000,
            encoding="utf-8",
        )

    context = collect_repository_context(tmp_path, _task())

    assert len(context) <= MAX_CONTEXT_CHARS
    assert "planner context " * 2_000 not in context


def test_missing_or_unreadable_repository_context_degrades_to_empty(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    assert collect_repository_context(tmp_path / "missing", _task()) == ""

    def inaccessible(_: RepositoryContextCollector) -> list[Path]:
        raise PermissionError("denied")

    monkeypatch.setattr(RepositoryContextCollector, "_discover_files", inaccessible)

    assert collect_repository_context(tmp_path, _task()) == ""
