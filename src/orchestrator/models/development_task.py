from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DevelopmentTask:
    card_id: str
    title: str
    description: str
    url: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_original_prompt(self) -> str:
        """Render raw Trello fields without summarizing or rewriting them."""
        return f"TITLE:\n{self.title}\n\nDESCRIPTION:\n{self.description}"

