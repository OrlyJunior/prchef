from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class AgentRole(StrEnum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"


@dataclass(frozen=True, slots=True)
class StructuredOutputExpectation:
    name: str
    schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AgentExecutionReference:
    provider: str
    external_id: str
    url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


AgentStartedCallback = Callable[[AgentExecutionReference], None]


@dataclass(frozen=True, slots=True)
class AgentRequest:
    instructions: str
    title: str
    workspace_path: Path | None = None
    repository: str | None = None
    revision: str | None = None
    structured_output: StructuredOutputExpectation | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentResult:
    output: str
    success: bool = True
    structured_output: Mapping[str, Any] | None = None
    error_message: str | None = None
    execution: AgentExecutionReference | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
