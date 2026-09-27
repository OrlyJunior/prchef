from pathlib import Path

import pytest
from pytest import MonkeyPatch

from orchestrator.config.project_config import RepositoryContextSettings
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.services.repository_context import (
    IGNORED_DIRECTORY_NAMES,
    MAX_CONTEXT_CHARS,
    HeuristicRepositoryContextStrategy,
    RepositoryContextCollector,
    RepositoryContextRequest,
    RepositoryContextStrategyError,
    collect_repository_context,
    repository_context_strategy_names,
    resolve_repository_context_strategy,
    semantic_fallback_diagnostic,
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


def test_heuristic_context_excludes_root_role_instructions(tmp_path: Path) -> None:
    (tmp_path / "PLANNER.md").write_text(
        "Unique planner context guidance.", encoding="utf-8"
    )
    (tmp_path / "IMPLEMENTER.md").write_text("Private implementer context", encoding="utf-8")
    (tmp_path / "REVIEWER.md").write_text("Private reviewer context", encoding="utf-8")
    (tmp_path / "planner.py").write_text("def planner_context(): pass", encoding="utf-8")

    context = collect_repository_context(tmp_path, _task())

    assert "planner.py" in context
    assert "PLANNER.md" not in context
    assert "Unique planner context guidance." not in context
    assert "IMPLEMENTER.md" not in context
    assert "REVIEWER.md" not in context


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


def test_resolver_returns_heuristic_strategy() -> None:
    assert repository_context_strategy_names() == ("heuristic", "qdrant")

    strategy = resolve_repository_context_strategy("heuristic")

    assert isinstance(strategy, HeuristicRepositoryContextStrategy)


def test_resolver_constructs_qdrant_strategy_with_repository_identity(tmp_path: Path) -> None:
    from orchestrator.services.qdrant_repository_context import (
        QdrantRepositoryContextStrategy,
    )

    settings = RepositoryContextSettings.model_validate(
        {
            "strategy": "qdrant",
            "qdrant": {"mode": "server", "url": "http://localhost:6333"},
        }
    )

    strategy = resolve_repository_context_strategy(
        "qdrant",
        settings=settings,
        repository_key="example",
        configured_root=tmp_path,
    )

    assert isinstance(strategy, QdrantRepositoryContextStrategy)


def test_heuristic_strategy_preserves_role_specific_default_context(tmp_path: Path) -> None:
    source = tmp_path / "planner.py"
    source.write_text("def planner_context():\n    pass\n", encoding="utf-8")
    strategy = HeuristicRepositoryContextStrategy()

    planner_context = strategy.render(
        RepositoryContextRequest(
            role="planner",
            repository_path=tmp_path,
            task=_task(),
            repository_slug="owner/repo",
            base_branch="develop",
        )
    )
    reviewer_context = strategy.render(
        RepositoryContextRequest(
            role="reviewer",
            repository_path=tmp_path,
            task=_task(),
            repository_slug="owner/repo",
            base_branch="develop",
            feature_branch="feature/context",
        )
    )

    assert "LOCAL REPOSITORY CONTEXT" in planner_context
    assert "planner.py" in planner_context
    assert reviewer_context == (
        'REPOSITORY CONTEXT:\n{\n  "repository": "owner/repo",\n'
        '  "base_branch": "develop",\n  "feature_branch": "feature/context"\n}\n\n'
    )
    assert "LOCAL REPOSITORY CONTEXT" not in reviewer_context


def test_resolver_rejects_unknown_strategy_clearly() -> None:
    with pytest.raises(RepositoryContextStrategyError) as exc_info:
        resolve_repository_context_strategy("semantic")

    message = str(exc_info.value)
    assert "semantic" in message
    assert "heuristic" in message


def test_path_only_qdrant_falls_back_with_actionable_diagnostic(tmp_path: Path) -> None:
    diagnostics: list[dict[str, str]] = []
    strategy = resolve_repository_context_strategy(
        "qdrant",
        settings=RepositoryContextSettings(),
        repository_key=None,
        configured_root=tmp_path,
        on_diagnostic=diagnostics.append,
    )

    assert isinstance(strategy, HeuristicRepositoryContextStrategy)
    assert diagnostics[0]["kind"] == "REPOSITORY_CONTEXT_FALLBACK"
    assert "registered key" in diagnostics[0]["reason"]
    assert "Register" in diagnostics[0]["remedy"]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (RuntimeError("offline download at /private/secret"), "download"),
        (RuntimeError("unsupported platform at /private/secret"), "platform"),
        (RuntimeError("index lock at /private/secret"), "locked"),
    ],
)
def test_semantic_fallback_diagnostic_is_actionable_and_redacted(
    failure: RuntimeError, reason: str
) -> None:
    diagnostic = semantic_fallback_diagnostic(failure)

    assert reason in diagnostic["reason"]
    assert diagnostic["remedy"]
    assert "/private/secret" not in str(diagnostic)
