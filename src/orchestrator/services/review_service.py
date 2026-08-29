import json
from collections.abc import Callable
from dataclasses import asdict

from orchestrator.clients.jules_client import JulesClient
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.jules_session import JulesSession
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationResult
from orchestrator.models.workflow_status import ReviewDecision
from orchestrator.services.exceptions import StructuredOutputError
from orchestrator.services.structured_output import parse_json_object, string_list


class ReviewService:
    def __init__(self, jules_client: JulesClient) -> None:
        self._jules = jules_client

    def review(
        self,
        *,
        task: DevelopmentTask,
        spec: RepositorySpec,
        pull_request: PullRequest,
        diff: str,
        validation: ValidationResult,
        repository_slug: str,
        base_branch: str,
        feature_branch: str,
        on_session_created: Callable[[JulesSession], None],
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
        operation = self._jules.review(
            prompt, repository_slug, feature_branch, on_session_created
        )
        raw = operation.result_text
        try:
            data = parse_json_object(raw, "Jules review")
            decision = ReviewDecision(str(data.get("decision")))
            summary = data.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise StructuredOutputError("Jules review.summary must be a non-empty string")
            findings = string_list(data.get("findings"), "findings", "review")
            blocking_findings = string_list(
                data.get("blocking_findings"), "blocking_findings", "review"
            )
            suggestions = string_list(data.get("suggestions", []), "suggestions", "review")
            if decision is ReviewDecision.APPROVED and blocking_findings:
                raise StructuredOutputError(
                    "Jules review is contradictory: APPROVED has blocking findings"
                )
            if decision is ReviewDecision.CHANGES_REQUESTED and not blocking_findings:
                raise StructuredOutputError(
                    "Jules review is contradictory: CHANGES_REQUESTED has no blocking findings"
                )
        except (StructuredOutputError, ValueError) as exc:
            return CodeReview(
                result=ReviewDecision.NEEDS_HUMAN,
                summary=f"Jules review could not be interpreted safely: {exc}",
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
