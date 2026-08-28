from dataclasses import dataclass, field


@dataclass(slots=True)
class ImplementationResult:
    success: bool
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    session_id: str | None = None
    error_message: str | None = None

