from collections.abc import Callable

from orchestrator.clients.jules_client import JulesClient
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.jules_session import JulesSession
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.services.exceptions import StructuredOutputError
from orchestrator.services.structured_output import parse_json_object, string_list


class SpecService:
    def __init__(self, jules_client: JulesClient) -> None:
        self._jules = jules_client

    def generate(
        self,
        task: DevelopmentTask,
        repository_slug: str,
        base_branch: str,
        on_session_created: Callable[[JulesSession], None],
    ) -> RepositorySpec:
        prompt = self._build_prompt(task)
        operation = self._jules.generate_spec(
            prompt, repository_slug, base_branch, on_session_created
        )
        raw = operation.result_text
        data = parse_json_object(raw, "Jules spec")
        required_text = [
            "technical_context",
            "current_behavior",
            "implementation_proposal",
        ]
        for field in required_text:
            if not isinstance(data.get(field), str):
                raise StructuredOutputError(f"Jules spec.{field} must be a string")
        safe = data.get("safe_to_implement")
        if not isinstance(safe, bool):
            raise StructuredOutputError("Jules spec.safe_to_implement must be a boolean")
        return RepositorySpec(
            technical_context=data["technical_context"],
            current_behavior=data["current_behavior"],
            involved_artifacts=string_list(
                data.get("involved_artifacts"), "involved_artifacts", "spec"
            ),
            implementation_proposal=data["implementation_proposal"],
            acceptance_criteria=string_list(
                data.get("acceptance_criteria"), "acceptance_criteria", "spec"
            ),
            expected_tests=string_list(data.get("expected_tests"), "expected_tests", "spec"),
            risks=string_list(data.get("risks"), "risks", "spec"),
            ambiguities=string_list(data.get("ambiguities"), "ambiguities", "spec"),
            safe_to_implement=safe,
            raw_text=raw,
        )

    @staticmethod
    def _build_prompt(task: DevelopmentTask) -> str:
        return f"""You are the read-only technical specification role.
Analyze the repository and the original task below.

STRICT SAFETY RULES:
- Do not edit, create, delete, or rename files.
- Do not create branches, commits, tags, pull requests, or remote changes.
- Do not invent requirements. Mark blocking ambiguity explicitly.

ORIGINAL TASK (preserve as authoritative):
{task.as_original_prompt()}

Return exactly one JSON object with this shape:
{{
  "technical_context": "string",
  "current_behavior": "string",
  "involved_artifacts": ["file, class, function, or module"],
  "implementation_proposal": "string",
  "acceptance_criteria": ["string"],
  "expected_tests": ["string"],
  "risks": ["string"],
  "ambiguities": ["string"],
  "safe_to_implement": true
}}
Set safe_to_implement to false if an ambiguity prevents safe implementation.
"""
