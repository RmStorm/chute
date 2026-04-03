from enum import StrEnum


class AdmissionMode(StrEnum):
    NONE = "none"
    AUTOMERGE = "automerge"
    QUEUE_NOW = "queue_now"


class PullRequestState(StrEnum):
    UNTRACKED = "untracked"
    ARMED = "armed"
    QUEUED = "queued"
    HEAD = "head"
    WAITING_CHECKS = "waiting_checks"
    READY = "ready"
    MERGED = "merged"
    FAILED = "failed"
    REMOVED = "removed"


class ChecksState(StrEnum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    PASSING = "passing"
    FAILING = "failing"


class ReviewState(StrEnum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class MergeableState(StrEnum):
    UNKNOWN = "unknown"
    MERGEABLE = "mergeable"
    CONFLICTING = "conflicting"
    BLOCKED = "blocked"


class BlockedReason(StrEnum):
    NONE = "none"
    CHECKS_PENDING = "checks_pending"
    CHECKS_FAILED = "checks_failed"
    REVIEW_MISSING = "review_missing"
    CONFLICT = "conflict"
    RETARGET_NEEDED = "retarget_needed"
    RESTACK_NEEDED = "restack_needed"
    LABEL_REMOVED = "label_removed"
    CLOSED = "closed"
    GITHUB_ERROR = "github_error"


class ActionType(StrEnum):
    STARTUP_SCRAPE = "startup_scrape"
    WEBHOOK_RECEIVED = "webhook_received"
    REFRESH_PR = "refresh_pr"
    RECONCILE_PR = "reconcile_pr"
    ENQUEUE_PR = "enqueue_pr"
    DEQUEUE_PR = "dequeue_pr"
    RETARGET_PR = "retarget_pr"
    MERGE_HEAD = "merge_head"
    SEND_NOTIFICATION = "send_notification"


class ActionStatus(StrEnum):
    PLANNED = "planned"
    SUCCESS = "success"
    ERROR = "error"
    SKIPPED = "skipped"

