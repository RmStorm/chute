from chute.core.enums import ChecksState, MergeableState, ReviewState
from chute.core.models import PullRequestRecord


def test_review_state_prefers_changes_requested() -> None:
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED"},
        {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
    ]
    assert PullRequestRecord.map_review_state(reviews) is ReviewState.CHANGES_REQUESTED


def test_review_state_uses_latest_review_per_user() -> None:
    reviews = [
        {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "alice"}, "state": "APPROVED"},
    ]
    assert PullRequestRecord.map_review_state(reviews) is ReviewState.APPROVED


def test_check_state_is_failing_when_completed_check_fails() -> None:
    check_runs = [{"status": "completed", "conclusion": "failure"}]
    assert PullRequestRecord.map_checks_state(check_runs, {"state": "success"}) is ChecksState.FAILING


def test_check_state_is_pending_when_checks_are_running() -> None:
    check_runs = [{"status": "in_progress", "conclusion": None}]
    assert PullRequestRecord.map_checks_state(check_runs, {"state": "pending"}) is ChecksState.PENDING


def test_mergeable_state_maps_conflicts_and_blocked() -> None:
    assert (
        PullRequestRecord.map_mergeable_state({"mergeable": False, "mergeable_state": "dirty"})
        is MergeableState.CONFLICTING
    )
    assert (
        PullRequestRecord.map_mergeable_state({"mergeable": None, "mergeable_state": "blocked"})
        is MergeableState.BLOCKED
    )
