from dataclasses import dataclass, field


@dataclass(slots=True)
class ValidationCheck:
    name: str
    command: list[str]
    success: bool
    # None only when the command was terminated for exceeding its timeout.
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    timeout_seconds: float | None = None


@dataclass(slots=True)
class ValidationResult:
    success: bool
    checks: list[ValidationCheck] = field(default_factory=list)
    error_message: str | None = None

