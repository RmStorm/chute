from datetime import UTC, datetime

from sqlmodel import col, desc, select

from chute.core.enums import ActionStatus, ActionType, AdmissionMode
from chute.core.models import ActionRecord, EventRecord, PullRequestRecord, QueueEntryRecord
from chute.db.sqlite import get_session


class PullRequestRepository:
    def __init__(self, engine) -> None:
        self.engine = engine

    def upsert(self, pr: PullRequestRecord) -> PullRequestRecord:
        with get_session(self.engine) as session:
            merged = session.merge(pr)
            session.commit()
            session.refresh(merged)
            return merged

    def list_all(self) -> list[PullRequestRecord]:
        with get_session(self.engine) as session:
            statement = select(PullRequestRecord).order_by(
                desc(col(PullRequestRecord.is_head)),
                desc(col(PullRequestRecord.is_queued)),
                col(PullRequestRecord.number),
            )
            return list(session.exec(statement))

    def get(self, repo: str, number: int) -> PullRequestRecord | None:
        with get_session(self.engine) as session:
            statement = select(PullRequestRecord).where(
                PullRequestRecord.repo == repo,
                PullRequestRecord.number == number,
            )
            return session.exec(statement).first()

    def mark_dirty(self, repo: str, number: int, last_event_at: datetime | None = None) -> None:
        with get_session(self.engine) as session:
            pr = session.exec(
                select(PullRequestRecord).where(
                    PullRequestRecord.repo == repo,
                    PullRequestRecord.number == number,
                )
            ).first()
            if pr is None:
                return
            pr.dirty = True
            if last_event_at is not None:
                pr.last_event_at = last_event_at
            session.add(pr)
            session.commit()

    def upsert_webhook_pull_request(
        self,
        *,
        repo: str,
        payload: dict,
        last_event_at: datetime | None = None,
    ) -> None:
        number = payload["number"]
        record = PullRequestRecord.minimal_from_webhook(
            repo=repo,
            number=number,
            title=payload.get("title", f"PR #{number}"),
            author=payload.get("user", {}).get("login", "unknown"),
            base_ref=payload.get("base", {}).get("ref", "main"),
            head_ref=payload.get("head", {}).get("ref", f"pr-{number}"),
            head_sha=payload.get("head", {}).get("sha", "unknown"),
            labels=[label["name"] for label in payload.get("labels", [])],
            last_event_at=last_event_at,
        )
        existing = self.get(repo, number)
        if existing is not None:
            existing.title = record.title
            existing.author = record.author
            existing.base_ref = record.base_ref
            existing.head_ref = record.head_ref
            existing.head_sha = record.head_sha
            existing.labels = record.labels
            existing.admission_mode = record.admission_mode
            existing.dirty = True
            existing.last_event_at = record.last_event_at
            self.upsert(existing)
            return

        self.upsert(record)

    def list_dirty(self) -> list[PullRequestRecord]:
        with get_session(self.engine) as session:
            statement = select(PullRequestRecord).where(col(PullRequestRecord.dirty)).order_by(
                desc(col(PullRequestRecord.last_event_at)),
                col(PullRequestRecord.number),
            )
            return list(session.exec(statement))

    def save_all(self, pull_requests: list[PullRequestRecord]) -> None:
        with get_session(self.engine) as session:
            for pr in pull_requests:
                session.merge(pr)
            session.commit()


class QueueRepository:
    def __init__(self, engine) -> None:
        self.engine = engine

    def list_active(self) -> list[QueueEntryRecord]:
        with get_session(self.engine) as session:
            statement = select(QueueEntryRecord).where(col(QueueEntryRecord.active)).order_by(
                col(QueueEntryRecord.position),
                col(QueueEntryRecord.queued_at),
            )
            return list(session.exec(statement))

    def replace_active(
        self,
        repo: str,
        desired_pr_numbers: list[int],
        enqueue_sources: dict[int, AdmissionMode],
    ) -> None:
        now = datetime.now(UTC)
        with get_session(self.engine) as session:
            existing_rows = list(
                session.exec(
                    select(QueueEntryRecord).where(
                        QueueEntryRecord.repo == repo,
                        col(QueueEntryRecord.active),
                    )
                )
            )
            existing_times = {row.pr_number: row.queued_at for row in existing_rows}

            for row in existing_rows:
                row.active = False
                row.dequeued_at = now
                row.dequeue_reason = "replaced_by_reconciler"
                session.add(row)

            for position, pr_number in enumerate(desired_pr_numbers, start=1):
                session.add(
                    QueueEntryRecord(
                        repo=repo,
                        pr_number=pr_number,
                        position=position,
                        active=True,
                        enqueue_source=enqueue_sources[pr_number],
                        queued_at=existing_times.get(pr_number, now),
                    )
                )
            session.commit()


class EventRepository:
    def __init__(self, engine) -> None:
        self.engine = engine

    def insert(self, event: EventRecord) -> None:
        with get_session(self.engine) as session:
            session.merge(event)
            session.commit()

    def list_recent(self, limit: int = 50) -> list[EventRecord]:
        with get_session(self.engine) as session:
            statement = select(EventRecord).order_by(desc(EventRecord.received_at)).limit(limit)
            return list(session.exec(statement))


class ActionRepository:
    def __init__(self, engine) -> None:
        self.engine = engine

    def insert(self, action: ActionRecord) -> None:
        with get_session(self.engine) as session:
            session.add(action)
            session.commit()

    def log(
        self,
        repo: str,
        pr_number: int | None,
        action_type: ActionType,
        status: ActionStatus,
        message: str,
        metadata: dict | None = None,
    ) -> None:
        self.insert(
            ActionRecord(
                repo=repo,
                pr_number=pr_number,
                action_type=action_type,
                status=status,
                message=message,
                details=metadata or {},
            )
        )

    def list_recent(self, limit: int = 50) -> list[ActionRecord]:
        with get_session(self.engine) as session:
            statement = select(ActionRecord).order_by(desc(ActionRecord.created_at)).limit(limit)
            return list(session.exec(statement))
