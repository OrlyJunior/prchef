from dataclasses import dataclass, field


@dataclass(slots=True)
class RepositorySpec:
    technical_context: str
    current_behavior: str
    involved_artifacts: list[str] = field(default_factory=list)
    implementation_proposal: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    expected_tests: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    safe_to_implement: bool = True
    raw_text: str = ""

    @property
    def has_blocking_ambiguity(self) -> bool:
        return not self.safe_to_implement

    def as_prompt_context(self) -> str:
        def bullets(values: list[str]) -> str:
            return "\n".join(f"- {value}" for value in values) or "- None"

        return (
            f"TECHNICAL CONTEXT:\n{self.technical_context}\n\n"
            f"CURRENT BEHAVIOR:\n{self.current_behavior}\n\n"
            f"INVOLVED ARTIFACTS:\n{bullets(self.involved_artifacts)}\n\n"
            f"IMPLEMENTATION PROPOSAL:\n{self.implementation_proposal}\n\n"
            f"ACCEPTANCE CRITERIA:\n{bullets(self.acceptance_criteria)}\n\n"
            f"EXPECTED TESTS:\n{bullets(self.expected_tests)}\n\n"
            f"RISKS:\n{bullets(self.risks)}\n\n"
            f"AMBIGUITIES:\n{bullets(self.ambiguities)}"
        )

