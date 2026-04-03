from chute.core.enums import (
    AdmissionMode,
    ChecksState,
    MergeableState,
    PullRequestState,
    ReviewState,
)
from chute.core.models import PullRequestRecord
from chute.reconcile.planner import plan_pull_requests


def make_pr(
    number: int,
    labels: list[str],
    *,
    checks_state: ChecksState = ChecksState.PASSING,
    review_state: ReviewState = ReviewState.APPROVED,
    mergeable_state: MergeableState = MergeableState.MERGEABLE,
) -> PullRequestRecord:
    return PullRequestRecord(
        repo="acme/monorepo",
        number=number,
        title=f"PR {number}",
        author="alice",
        state=PullRequestState.UNTRACKED,
        admission_mode=AdmissionMode.NONE,
        base_ref="main",
        head_ref=f"branch-{number}",
        head_sha=f"sha-{number}",
        labels=labels,
        checks_state=checks_state,
        review_state=review_state,
        mergeable_state=mergeable_state,
    )


def test_queue_now_wins_over_automerge() -> None:
    planned, queue = plan_pull_requests(
        [make_pr(10, ["Automerge", "Automerge-queue-now"], checks_state=ChecksState.PENDING)],
        existing_queue=[],
    )

    assert queue == [10]
    assert planned[0].admission_mode is AdmissionMode.QUEUE_NOW
    assert planned[0].state is PullRequestState.WAITING_CHECKS


def test_automerge_stays_armed_until_eligible() -> None:
    planned, queue = plan_pull_requests(
        [make_pr(11, ["Automerge"], checks_state=ChecksState.PENDING)],
        existing_queue=[],
    )

    assert queue == []
    assert planned[0].state is PullRequestState.ARMED


def test_eligible_automerge_enters_queue_and_becomes_ready_head() -> None:
    planned, queue = plan_pull_requests(
        [make_pr(12, ["Automerge"])],
        existing_queue=[],
    )

    assert queue == [12]
    assert planned[0].state is PullRequestState.READY
    assert planned[0].is_head is True


def test_existing_queue_order_is_preserved_and_new_entries_append() -> None:
    planned, queue = plan_pull_requests(
        [
            make_pr(20, ["Automerge-queue-now"], checks_state=ChecksState.PENDING),
            make_pr(21, ["Automerge"]),
            make_pr(22, ["Automerge-queue-now"], checks_state=ChecksState.PENDING),
        ],
        existing_queue=[22],
    )

    assert queue == [22, 20, 21]
    planned_by_number = {pr.number: pr for pr in planned}
    assert planned_by_number[22].is_head is True
    assert planned_by_number[20].state is PullRequestState.QUEUED
    assert planned_by_number[21].state is PullRequestState.QUEUED


def test_failed_head_is_ejected_and_next_pr_advances() -> None:
    planned, queue = plan_pull_requests(
        [
            make_pr(30, ["Automerge-queue-now"], checks_state=ChecksState.FAILING),
            make_pr(31, ["Automerge-queue-now"], checks_state=ChecksState.PENDING),
        ],
        existing_queue=[30, 31],
    )

    planned_by_number = {pr.number: pr for pr in planned}
    assert queue == [31]
    assert planned_by_number[30].state is PullRequestState.FAILED
    assert planned_by_number[31].is_head is True
    assert planned_by_number[31].state is PullRequestState.WAITING_CHECKS
