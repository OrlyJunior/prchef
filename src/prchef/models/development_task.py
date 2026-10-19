from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DevelopmentTask:
    id: str
    source: str
    title: str
    description: str
    url: str
    repository: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_original_prompt(self) -> str:
        """Render original task fields without summarizing or rewriting them."""
        return f"TITLE:\n{self.title}\n\nDESCRIPTION:\n{self.description}"

    @property
    def card_id(self) -> str:
        """Compatibility alias for code that still identifies Trello tasks as cards."""
        return self.id
