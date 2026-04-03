from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from chute.core.enums import (
    ActionStatus,
    ActionType,
    AdmissionMode,
    BlockedReason,
    ChecksState,
    MergeableState,
    PullRequestState,
    ReviewState,
)


class PullRequestRecord(SQLModel, table=True):
    __tablename__ = "pull_requests"

    repo: str = Field(primary_key=True)
    number: int = Field(primary_key=True)
    title: str
    author: str
    state: PullRequestState = Field(default=PullRequestState.UNTRACKED)
    admission_mode: AdmissionMode = Field(default=AdmissionMode.NONE)
    base_ref: str
    head_ref: str
    head_sha: str
    labels: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    checks_state: ChecksState = Field(default=ChecksState.UNKNOWN)
    review_state: ReviewState = Field(default=ReviewState.UNKNOWN)
    mergeable_state: MergeableState = Field(default=MergeableState.UNKNOWN)
    blocked_reason: BlockedReason = Field(default=BlockedReason.NONE)
    dirty: bool = Field(default=True)
    is_tracked: bool = Field(default=True)
    is_queued: bool = Field(default=False)
    is_head: bool = Field(default=False)
    last_github_sync_at: datetime | None = Field(default=None)
    last_decided_at: datetime | None = Field(default=None)
    last_event_at: datetime | None = Field(default=None)

    @classmethod
    def from_github(
        cls,
        repo_full_name: str,
        payload: dict[str, Any],
        *,
        reviews: list[dict[str, Any]],
        check_runs: list[dict[str, Any]],
        combined_status: dict[str, Any],
    ) -> "PullRequestRecord":
        from chute.reconcile.planner import determine_admission_mode

        labels = [label["name"] for label in payload.get("labels", [])]
        return cls(
            repo=repo_full_name,
            number=payload["number"],
            title=payload["title"],
            author=payload["user"]["login"],
            admission_mode=determine_admission_mode(labels),
            base_ref=payload["base"]["ref"],
            head_ref=payload["head"]["ref"],
            head_sha=payload["head"]["sha"],
            labels=labels,
            checks_state=cls.map_checks_state(check_runs, combined_status),
            review_state=cls.map_review_state(reviews),
            mergeable_state=cls.map_mergeable_state(payload),
            last_github_sync_at=datetime.now(UTC),
        )

    @classmethod
    def minimal_from_webhook(
        cls,
        *,
        repo: str,
        number: int,
        title: str,
        author: str,
        base_ref: str,
        head_ref: str,
        head_sha: str,
        labels: list[str],
        last_event_at: datetime | None = None,
    ) -> "PullRequestRecord":
        from chute.reconcile.planner import determine_admission_mode

        return cls(
            repo=repo,
            number=number,
            title=title,
            author=author,
            base_ref=base_ref,
            head_ref=head_ref,
            head_sha=head_sha,
            admission_mode=determine_admission_mode(labels),
            labels=labels,
            last_event_at=last_event_at,
        )

    @staticmethod
    def map_review_state(reviews: list[dict[str, Any]]) -> ReviewState:
        latest_by_user: dict[str, str] = {}
        for review in reviews:
            user = review.get("user", {}).get("login")
            state = review.get("state")
            if user and state:
                latest_by_user[user] = state

        states = set(latest_by_user.values())
        if "CHANGES_REQUESTED" in states:
            return ReviewState.CHANGES_REQUESTED
        if "APPROVED" in states:
            return ReviewState.APPROVED
        if latest_by_user:
            return ReviewState.PENDING
        return ReviewState.UNKNOWN

    @staticmethod
    def map_checks_state(
        check_runs: list[dict[str, Any]],
        combined_status: dict[str, Any],
    ) -> ChecksState:
        conclusions = [run.get("conclusion") for run in check_runs if run.get("status") == "completed"]
        statuses = [run.get("status") for run in check_runs]

        if any(conclusion not in {"success", "neutral", "skipped"} for conclusion in conclusions):
            return ChecksState.FAILING
        if check_runs and any(status != "completed" for status in statuses):
            return ChecksState.PENDING

        overall_state = combined_status.get("state")
        if overall_state == "failure":
            return ChecksState.FAILING
        if overall_state == "pending":
            return ChecksState.PENDING
        if overall_state == "success":
            return ChecksState.PASSING

        if check_runs:
            return ChecksState.PASSING
        return ChecksState.UNKNOWN

    @staticmethod
    def map_mergeable_state(payload: dict[str, Any]) -> MergeableState:
        mergeable = payload.get("mergeable")
        mergeable_state = payload.get("mergeable_state")
        if mergeable is False or mergeable_state == "dirty":
            return MergeableState.CONFLICTING
        if mergeable is True and mergeable_state in {"clean", "has_hooks", "unstable"}:
            return MergeableState.MERGEABLE
        if mergeable_state in {"blocked", "behind", "draft"}:
            return MergeableState.BLOCKED
        return MergeableState.UNKNOWN


class QueueEntryRecord(SQLModel, table=True):
    __tablename__ = "queue_entries"

    id: int | None = Field(default=None, primary_key=True)
    repo: str
    pr_number: int
    position: int
    active: bool
    enqueue_source: AdmissionMode
    queued_at: datetime
    dequeued_at: datetime | None = None
    dequeue_reason: str | None = None


class EventRecord(SQLModel, table=True):
    __tablename__ = "events"

    delivery_id: str = Field(primary_key=True)
    event_type: str
    action: str | None = None
    repo: str | None = None
    pr_number: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    received_at: datetime
    processed_at: datetime | None = None


class ActionRecord(SQLModel, table=True):
    __tablename__ = "actions"

    id: int | None = Field(default=None, primary_key=True)
    repo: str
    pr_number: int | None = None
    action_type: ActionType
    status: ActionStatus
    message: str
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NotificationRecord(SQLModel, table=True):
    __tablename__ = "notifications"

    id: int | None = Field(default=None, primary_key=True)
    repo: str
    pr_number: int
    kind: str
    status: str
    message: str
    sent_at: datetime | None = None
