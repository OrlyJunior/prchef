from dataclasses import dataclass, field


@dataclass(slots=True)
class ValidationCheck:
    name: str
    command: list[str]
    success: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


@dataclass(slots=True)
class ValidationResult:
    success: bool
    checks: list[ValidationCheck] = field(default_factory=list)
    error_message: str | None = None

