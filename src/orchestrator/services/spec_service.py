from collections.abc import Callable
from pathlib import Path

from orchestrator.models.agent import (
    AgentExecutionReference,
    AgentRequest,
    StructuredOutputExpectation,
)
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.services.exceptions import StructuredOutputError
from orchestrator.services.project_context import project_context_prompt
from orchestrator.services.repository_context import repository_context_prompt
from orchestrator.services.structured_output import parse_json_object, string_list

_SPEC_OUTPUT = StructuredOutputExpectation(
    name="repository-spec",
    schema={
        "type": "object",
        "properties": {
            "technical_context": {"type": "string"},
            "current_behavior": {"type": "string"},
            "involved_artifacts": {"type": "array", "items": {"type": "string"}},
            "implementation_proposal": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "expected_tests": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "ambiguities": {"type": "array", "items": {"type": "string"}},
            "safe_to_implement": {"type": "boolean"},
        },
        "required": [
            "technical_context",
            "current_behavior",
            "involved_artifacts",
            "implementation_proposal",
            "acceptance_criteria",
            "expected_tests",
            "risks",
            "ambiguities",
            "safe_to_implement",
        ],
        "additionalProperties": False,
    },
)


class SpecService:
    def __init__(self, provider: AgentProvider) -> None:
        self._provider = provider

    def generate(
        self,
        task: DevelopmentTask,
        repository_path: Path,
        repository_slug: str,
        base_branch: str,
        on_execution_started: Callable[[AgentExecutionReference], None],
    ) -> RepositorySpec:
        prompt = self._build_prompt(task, repository_path)
        result = self._provider.execute(
            AgentRequest(
                instructions=prompt,
                title="Orchestrator technical specification",
                workspace_path=repository_path,
                repository=repository_slug,
                revision=base_branch,
                structured_output=_SPEC_OUTPUT,
            ),
            on_started=on_execution_started,
        )
        if not result.success:
            raise RuntimeError(result.error_message or "Planner provider failed")
        raw = result.output
        data = (
            dict(result.structured_output)
            if result.structured_output is not None
            else parse_json_object(raw, "Planner output")
        )
        required_text = [
            "technical_context",
            "current_behavior",
            "implementation_proposal",
        ]
        for field in required_text:
            if not isinstance(data.get(field), str):
                raise StructuredOutputError(f"Planner output.{field} must be a string")
        safe = data.get("safe_to_implement")
        if not isinstance(safe, bool):
            raise StructuredOutputError("Planner output.safe_to_implement must be a boolean")
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
    def _build_prompt(task: DevelopmentTask, repository_path: Path) -> str:
        return f"""You are the read-only technical specification role.
Analyze the repository and the original task below.

STRICT SAFETY RULES:
- Do not edit, create, delete, or rename files.
- Do not create branches, commits, tags, pull requests, or remote changes.
- Do not invent requirements. Mark blocking ambiguity explicitly.

ORIGINAL TASK (preserve as authoritative):
{task.as_original_prompt()}

{project_context_prompt(repository_path)}When writing implementation_proposal, explicitly direct
{repository_context_prompt(repository_path, task)}When writing implementation_proposal, explicitly
direct
the implementation agent to keep
PROJECT.md accurate when the change affects durable project knowledge (architecture, workflows,
domain behavior, invariants, configuration, persistence, integrations, or design decisions).

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
OUTPUT CONTRACT:
- Your final agent message itself must be that JSON object.
- Return plain valid JSON only: no Markdown fence, heading, explanation, status, or
  prose before or after it.
- Do not claim that JSON was returned in a tool result, plan, artifact, or completion
  note; include it directly in the final agent message.
Set safe_to_implement to false if an ambiguity prevents safe implementation.
"""
