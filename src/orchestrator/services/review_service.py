import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from orchestrator.models.agent import (
    AgentExecutionReference,
    AgentRequest,
    StructuredOutputExpectation,
)
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationResult
from orchestrator.models.workflow_status import ReviewDecision
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.services.exceptions import StructuredOutputError
from orchestrator.services.structured_output import parse_json_object, string_list

_REVIEW_OUTPUT = StructuredOutputExpectation(
    name="code-review",
    schema={
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["APPROVED", "CHANGES_REQUESTED", "NEEDS_HUMAN"],
            },
            "summary": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "blocking_findings": {"type": "array", "items": {"type": "string"}},
            "suggestions": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "decision",
            "summary",
            "findings",
            "blocking_findings",
            "suggestions",
        ],
        "additionalProperties": False,
    },
)


class ReviewService:
    def __init__(self, provider: AgentProvider) -> None:
        self._provider = provider

    def review(
        self,
        *,
        task: DevelopmentTask,
        spec: RepositorySpec,
        pull_request: PullRequest,
        diff: str,
        validation: ValidationResult,
        repository_path: Path,
        repository_slug: str,
        base_branch: str,
        feature_branch: str,
        on_execution_started: Callable[[AgentExecutionReference], None],
    ) -> CodeReview:
        prompt = f"""Review this implementation against the original task and technical spec.
Do not edit files, create commits, push, merge, or change the pull request.
Do not invent requirements.

ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

PULL REQUEST:
{json.dumps(asdict(pull_request), ensure_ascii=False, indent=2)}

REPOSITORY CONTEXT:
{json.dumps(
    {
        "repository": repository_slug,
        "base_branch": base_branch,
        "feature_branch": feature_branch,
    },
    ensure_ascii=False,
    indent=2,
)}

VALIDATION RESULT:
{json.dumps(asdict(validation), ensure_ascii=False, indent=2)}

IMPLEMENTATION DIFF:
{diff}

Return exactly one JSON object:
{{
  "decision": "APPROVED" or "CHANGES_REQUESTED" or "NEEDS_HUMAN",
  "summary": "string",
  "findings": ["string"],
  "blocking_findings": ["string"],
  "suggestions": ["string"]
}}
Use NEEDS_HUMAN when a safe decision cannot be made from the available context.
"""
        result = self._provider.execute(
            AgentRequest(
                instructions=prompt,
                title="Orchestrator code review",
                workspace_path=repository_path,
                repository=repository_slug,
                revision=feature_branch,
                structured_output=_REVIEW_OUTPUT,
            ),
            on_started=on_execution_started,
        )
        if not result.success:
            raise RuntimeError(result.error_message or "Reviewer provider failed")
        raw = result.output
        try:
            data = (
                dict(result.structured_output)
                if result.structured_output is not None
                else parse_json_object(raw, "Reviewer output")
            )
            decision = ReviewDecision(str(data.get("decision")))
            summary = data.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise StructuredOutputError(
                    "Reviewer output.summary must be a non-empty string"
                )
            findings = string_list(data.get("findings"), "findings", "review")
            blocking_findings = string_list(
                data.get("blocking_findings"), "blocking_findings", "review"
            )
            suggestions = string_list(data.get("suggestions", []), "suggestions", "review")
            if decision is ReviewDecision.APPROVED and blocking_findings:
                raise StructuredOutputError(
                    "Reviewer output is contradictory: APPROVED has blocking findings"
                )
            if decision is ReviewDecision.CHANGES_REQUESTED and not blocking_findings:
                raise StructuredOutputError(
                    "Reviewer output is contradictory: CHANGES_REQUESTED has no blocking findings"
                )
        except (StructuredOutputError, ValueError) as exc:
            return CodeReview(
                result=ReviewDecision.NEEDS_HUMAN,
                summary=f"Reviewer output could not be interpreted safely: {exc}",
                raw_text=raw,
            )
        return CodeReview(
            result=decision,
            summary=summary,
            findings=findings,
            blocking_findings=blocking_findings,
            suggestions=suggestions,
            raw_text=raw,
        )
