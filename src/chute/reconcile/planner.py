from datetime import UTC, datetime

from chute.core.enums import AdmissionMode, BlockedReason, PullRequestState
from chute.core.models import PullRequestRecord

QUEUE_NOW_LABEL = "Automerge-queue-now"
AUTOMERGE_LABEL = "Automerge"


def determine_admission_mode(labels: list[str]) -> AdmissionMode:
    label_set = set(labels)
    if QUEUE_NOW_LABEL in label_set:
        return AdmissionMode.QUEUE_NOW
    if AUTOMERGE_LABEL in label_set:
        return AdmissionMode.AUTOMERGE
    return AdmissionMode.NONE


def compute_blocked_reason(pr: PullRequestRecord) -> BlockedReason:
    if pr.mergeable_state.name == "CONFLICTING":
        return BlockedReason.CONFLICT
    if pr.review_state.name == "CHANGES_REQUESTED":
        return BlockedReason.REVIEW_MISSING
    if pr.review_state.name != "APPROVED":
        return BlockedReason.REVIEW_MISSING
    if pr.checks_state.name == "FAILING":
        return BlockedReason.CHECKS_FAILED
    if pr.checks_state.name != "PASSING":
        return BlockedReason.CHECKS_PENDING
    return BlockedReason.NONE


def is_eligible(pr: PullRequestRecord) -> bool:
    return compute_blocked_reason(pr) is BlockedReason.NONE


def plan_pull_requests(
    pull_requests: list[PullRequestRecord],
    existing_queue: list[int],
) -> tuple[list[PullRequestRecord], list[int]]:
    now = datetime.now(UTC)
    by_number = {pr.number: pr for pr in pull_requests}

    normalized: list[PullRequestRecord] = []
    for pr in pull_requests:
        admission_mode = determine_admission_mode(pr.labels)
        normalized.append(
            pr.model_copy(
                update={
                    "admission_mode": admission_mode,
                    "blocked_reason": compute_blocked_reason(pr)
                    if admission_mode is not AdmissionMode.NONE
                    else BlockedReason.LABEL_REMOVED,
                    "last_decided_at": now,
                }
            )
        )

    by_number = {pr.number: pr for pr in normalized}
    queued_numbers: list[int] = []

    for pr_number in existing_queue:
        pr = by_number.get(pr_number)
        if pr is None:
            continue
        if pr.admission_mode is AdmissionMode.NONE:
            continue
        if pr_number not in queued_numbers:
            queued_numbers.append(pr_number)

    newcomers = [
        pr.number
        for pr in sorted(normalized, key=lambda item: item.number)
        if should_be_queued(pr) and pr.number not in queued_numbers
    ]
    queued_numbers.extend(newcomers)

    planned: list[PullRequestRecord] = []
    failed_head: int | None = None
    if queued_numbers:
        head_number = queued_numbers[0]
        head_pr = by_number[head_number]
        if compute_blocked_reason(head_pr) is BlockedReason.CHECKS_FAILED:
            failed_head = head_number
            queued_numbers = queued_numbers[1:]

    for pr in normalized:
        planned.append(apply_state(pr, queued_numbers, failed_head))

    return planned, queued_numbers


def should_be_queued(pr: PullRequestRecord) -> bool:
    if pr.admission_mode is AdmissionMode.QUEUE_NOW:
        return True
    if pr.admission_mode is AdmissionMode.AUTOMERGE:
        return is_eligible(pr)
    return False


def apply_state(
    pr: PullRequestRecord,
    queued_numbers: list[int],
    failed_head: int | None,
) -> PullRequestRecord:
    if pr.number == failed_head:
        return pr.model_copy(
            update={
                "state": PullRequestState.FAILED,
                "blocked_reason": BlockedReason.CHECKS_FAILED,
                "is_queued": False,
                "is_head": False,
                "dirty": False,
            }
        )

    if pr.admission_mode is AdmissionMode.NONE:
        return pr.model_copy(
            update={
                "state": PullRequestState.REMOVED,
                "is_queued": False,
                "is_head": False,
                "dirty": False,
            }
        )

    if pr.number in queued_numbers:
        is_head = queued_numbers and pr.number == queued_numbers[0]
        if is_head and is_eligible(pr):
            state = PullRequestState.READY
        elif is_head:
            state = PullRequestState.WAITING_CHECKS
        else:
            state = PullRequestState.QUEUED
        return pr.model_copy(
            update={
                "state": state,
                "is_queued": True,
                "is_head": is_head,
                "dirty": False,
            }
        )

    return pr.model_copy(
        update={
            "state": PullRequestState.ARMED,
            "is_queued": False,
            "is_head": False,
            "dirty": False,
        }
    )
