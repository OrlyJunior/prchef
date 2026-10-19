from types import SimpleNamespace
from unittest.mock import Mock

from prchef.models import CodeReview, ReviewDecision
from prchef.services.workflow_stage_actions import StageActions


def test_identical_review_from_previous_attempt_is_not_reused() -> None:
    execution = SimpleNamespace(review_step_id=20)
    repository = Mock()
    repository.get_execution.return_value = execution
    published: list[str] = []
    github = Mock()
    github.review_already_published.side_effect = (
        lambda repository_slug, number, event, body: body in published
    )
    github.submit_pull_request_review.side_effect = lambda **kwargs: published.append(
        kwargs["body"]
    )
    actions = StageActions(repository, Mock())
    clients = SimpleNamespace(github=github)
    review = CodeReview(result=ReviewDecision.APPROVED)

    actions._publish_review(
        execution_id="execution", execution_clients=clients,
        repository_slug="owner/repo", pull_request_number=1, review=review,
    )
    execution.review_step_id = 21
    actions._publish_review(
        execution_id="execution", execution_clients=clients,
        repository_slug="owner/repo", pull_request_number=1, review=review,
        reconcile=True,
    )
    actions._publish_review(
        execution_id="execution", execution_clients=clients,
        repository_slug="owner/repo", pull_request_number=1, review=review,
        reconcile=True,
    )

    assert len(published) == 2
    assert "<!-- orchestrator-review:execution:20 -->" in published[0]
    assert "<!-- orchestrator-review:execution:21 -->" in published[1]
