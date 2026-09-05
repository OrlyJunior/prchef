from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.models import (
    AgentRequest,
    AgentResult,
    CodeReview,
    DevelopmentTask,
    PullRequest,
    RepositorySpec,
    ReviewDecision,
    ValidationResult,
)
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.review_service import ReviewService, format_pull_request_review
from orchestrator.services.spec_service import SpecService


class RecordingProvider:
    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.requests: list[AgentRequest] = []

    def execute(self, request: AgentRequest, *, on_started: Any = None) -> AgentResult:
        del on_started
        self.requests.append(request)
        return self.result


class ChangedFilesGit:
    def changed_files(self) -> list[str]:
        return ["src/example.py"]


class RecordingGit:
    def __init__(self, changed_files: list[str]) -> None:
        self.changed_files_result = changed_files
        self.changed_files_calls = 0

    def changed_files(self) -> list[str]:
        self.changed_files_calls += 1
        return self.changed_files_result


def _task() -> DevelopmentTask:
    return DevelopmentTask(
        id="5",
        source="github",
        title="Provider contracts",
        description="Decouple roles from providers",
        url="https://github.example/issues/5",
        repository="owner/repo",
    )


def _spec() -> RepositorySpec:
    return RepositorySpec(
        technical_context="services",
        current_behavior="concrete clients",
        implementation_proposal="provider contract",
    )


def _write_project_context(path: Path) -> None:
    (path / "PROJECT.md").write_text(
        "# Durable context\nStable architecture rule.", encoding="utf-8"
    )


def test_planner_depends_only_on_common_agent_provider(tmp_path: Path) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(
        AgentResult(
            output="provider-native structured output",
            structured_output={
                "technical_context": "services",
                "current_behavior": "concrete clients",
                "involved_artifacts": [],
                "implementation_proposal": "provider contract",
                "acceptance_criteria": [],
                "expected_tests": [],
                "risks": [],
                "ambiguities": [],
                "safe_to_implement": True,
            },
        )
    )

    spec = SpecService(provider).generate(
        _task(), tmp_path, "owner/repo", "develop", lambda reference: None
    )

    assert spec.safe_to_implement
    assert provider.requests[0].structured_output is not None
    assert provider.requests[0].structured_output.name == "repository-spec"
    assert "Stable architecture rule." in provider.requests[0].instructions
    assert "keep\nPROJECT.md accurate" in provider.requests[0].instructions


def test_implementer_depends_only_on_common_agent_provider(tmp_path: Path) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(AgentResult(output="implemented"))

    result = ImplementationService(
        provider, ChangedFilesGit()  # type: ignore[arg-type]
    ).implement(_task(), _spec(), tmp_path)

    assert result.success
    assert result.changed_files == ["src/example.py"]
    assert provider.requests[0].workspace_path == tmp_path
    assert "Stable architecture rule." in provider.requests[0].instructions
    assert "Keep PROJECT.md accurate" in provider.requests[0].instructions


def test_implementation_inspects_changes_after_a_structured_provider_failure(
    tmp_path: Path,
) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(
        AgentResult(
            output="The patch was applied before a structured policy denial.",
            success=False,
            error_message="Codex reported a structured sandbox denial",
            workspace_changes_may_exist=True,
        )
    )
    git = RecordingGit(["src/example.py"])

    result = ImplementationService(provider, git).implement(_task(), _spec(), tmp_path)  # type: ignore[arg-type]

    assert not result.success
    assert result.changed_files == ["src/example.py"]
    assert result.error_message == "Codex reported a structured sandbox denial"
    assert result.workspace_changes_may_exist
    assert git.changed_files_calls == 1


def test_implementation_does_not_inspect_changes_for_regular_provider_failure(
    tmp_path: Path,
) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(
        AgentResult(output="failed", success=False, error_message="provider unavailable")
    )
    git = RecordingGit(["src/example.py"])

    result = ImplementationService(provider, git).implement(_task(), _spec(), tmp_path)  # type: ignore[arg-type]

    assert not result.success
    assert result.changed_files == []
    assert result.error_message == "provider unavailable"
    assert git.changed_files_calls == 0


def test_review_correction_prompt_includes_structured_review_feedback(tmp_path: Path) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(AgentResult(output="corrected"))

    result = ImplementationService(
        provider, ChangedFilesGit()  # type: ignore[arg-type]
    ).correct_review_findings(
        _task(),
        _spec(),
        CodeReview(
            result=ReviewDecision.CHANGES_REQUESTED,
            summary="Handle the empty input case.",
            findings=["Missing empty input behavior"],
            blocking_findings=["Return a useful error for empty input"],
            suggestions=["Add a focused unit test"],
        ),
        tmp_path,
    )

    assert result.success
    prompt = provider.requests[0].instructions
    assert "REVIEWER CHANGE REQUESTS FOR THE CURRENT IMPLEMENTATION" in prompt
    assert "Handle the empty input case." in prompt
    assert "Return a useful error for empty input" in prompt
    assert "Add a focused unit test" in prompt


def test_reviewer_depends_only_on_common_agent_provider(tmp_path: Path) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(
        AgentResult(
            output="provider-native structured output",
            structured_output={
                "decision": "APPROVED",
                "summary": "Looks good",
                "findings": [],
                "blocking_findings": [],
                "suggestions": [],
            },
        )
    )

    review = ReviewService(provider).review(
        task=_task(),
        spec=_spec(),
        pull_request=PullRequest(
            number=5,
            url="https://github.example/pull/5",
            title="Provider contracts",
            base_branch="develop",
            head_branch="feature/provider-contracts",
        ),
        diff="diff --git a/a b/a",
        validation=ValidationResult(success=True),
        repository_path=tmp_path,
        repository_slug="owner/repo",
        base_branch="develop",
        feature_branch="feature/provider-contracts",
        on_execution_started=lambda reference: None,
    )

    assert review.result == "APPROVED"
    assert review.findings == []
    assert review.blocking_findings == []
    assert review.suggestions == []
    assert provider.requests[0].structured_output is not None
    assert provider.requests[0].structured_output.name == "code-review"
    review_schema = provider.requests[0].structured_output.schema
    assert "blocking" in review_schema["properties"]["blocking_findings"]["description"]
    assert "Non-blocking" in review_schema["properties"]["suggestions"]["description"]
    assert "Stable architecture rule." in provider.requests[0].instructions
    assert "adversarial, evidence-based risk analysis" in provider.requests[0].instructions
    assert "concurrency and races" in provider.requests[0].instructions
    assert (
        "idempotency, duplicate execution, and partial-failure recovery"
        in provider.requests[0].instructions
    )
    assert "token and prompt efficiency" in provider.requests[0].instructions
    assert (
        "provider coupling, quota/rate-limit handling" in provider.requests[0].instructions
    )
    assert (
        "only blocking issues in `blocking_findings`"
        in provider.requests[0].instructions
    )
    assert "may accompany APPROVED" in provider.requests[0].instructions


def _review_with_output(tmp_path: Path, output: str) -> CodeReview:
    provider = RecordingProvider(AgentResult(output=output))

    return ReviewService(provider).review(
        task=_task(),
        spec=_spec(),
        pull_request=PullRequest(
            number=5,
            url="https://github.example/pull/5",
            title="Provider contracts",
            base_branch="develop",
            head_branch="feature/provider-contracts",
        ),
        diff="diff --git a/a b/a",
        validation=ValidationResult(success=True),
        repository_path=tmp_path,
        repository_slug="owner/repo",
        base_branch="develop",
        feature_branch="feature/provider-contracts",
        on_execution_started=lambda reference: None,
    )


def test_reviewer_approves_suggestions_without_blocking_findings(
    tmp_path: Path,
) -> None:
    _write_project_context(tmp_path)

    review = _review_with_output(
        tmp_path,
        """{
            "decision": "APPROVED",
            "summary": "Correct and safe.",
            "findings": [],
            "blocking_findings": [],
            "suggestions": ["Avoid rebuilding unchanged prompt context."]
        }""",
    )

    assert review.result is ReviewDecision.APPROVED
    assert review.blocking_findings == []
    assert review.suggestions == ["Avoid rebuilding unchanged prompt context."]


def test_pull_request_review_formatter_keeps_suggestions_non_blocking() -> None:
    body = format_pull_request_review(
        CodeReview(
            result=ReviewDecision.APPROVED,
            summary="Correct and safe.",
            suggestions=["Avoid rebuilding unchanged prompt context."],
        )
    )

    assert body.startswith("## Automated review: APPROVED")
    assert "### Blocking findings" not in body
    assert "### Non-blocking suggestions" in body
    assert "- Avoid rebuilding unchanged prompt context." in body


def test_pull_request_review_formatter_includes_blocking_findings() -> None:
    body = format_pull_request_review(
        CodeReview(
            result=ReviewDecision.CHANGES_REQUESTED,
            summary="The retry can duplicate writes after a timeout.",
            blocking_findings=["Retry is not idempotent."],
            suggestions=["Add a regression test."],
        )
    )

    assert body.startswith("## Automated review: CHANGES_REQUESTED")
    assert "### Blocking findings" in body
    assert "- Retry is not idempotent." in body
    assert "### Non-blocking suggestions" in body


def test_reviewer_requests_changes_for_concrete_blocking_findings(
    tmp_path: Path,
) -> None:
    _write_project_context(tmp_path)

    review = _review_with_output(
        tmp_path,
        """{
            "decision": "CHANGES_REQUESTED",
            "summary": "The retry can duplicate writes after a timeout.",
            "findings": ["Retry is not idempotent."],
            "blocking_findings": ["Retry is not idempotent."],
            "suggestions": []
        }""",
    )

    assert review.result is ReviewDecision.CHANGES_REQUESTED
    assert review.blocking_findings == ["Retry is not idempotent."]


@pytest.mark.parametrize(
    "decision, blocking_findings",
    [
        ("APPROVED", ["Concurrent writes can corrupt the persisted state."]),
        ("CHANGES_REQUESTED", []),
    ],
)
def test_reviewer_maps_contradictory_decisions_to_needs_human(
    tmp_path: Path, decision: str, blocking_findings: list[str]
) -> None:
    _write_project_context(tmp_path)
    output = json.dumps(
        {
            "decision": decision,
            "summary": "Review result.",
            "findings": blocking_findings,
            "blocking_findings": blocking_findings,
            "suggestions": [],
        }
    )

    review = _review_with_output(tmp_path, output)

    assert review.result is ReviewDecision.NEEDS_HUMAN
    assert "could not be interpreted safely" in review.summary
