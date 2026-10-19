from dataclasses import dataclass


@dataclass(slots=True)
class PullRequest:
    number: int
    url: str
    title: str
    base_branch: str
    head_branch: str

