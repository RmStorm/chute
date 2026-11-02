from copy import deepcopy
from datetime import UTC, datetime

from sqlmodel import col, select

from chute.core.models import EventRecord, TrainJournal
from chute.db.sqlite import get_session


class TrainStore:
    def __init__(self, engine, repo: str):
        self.engine = engine
        self.repo = repo

    def load(self) -> dict:
        with get_session(self.engine) as session:
            row = session.get(TrainJournal, self.repo)
            return deepcopy(row.data) if row else {"entries": {}, "order": [], "error": None}

    def save(self, data: dict, event: str | None = None):
        with get_session(self.engine) as session:
            session.merge(TrainJournal(repo=self.repo, data=deepcopy(data)))
            if event:
                row = session.get(EventRecord, event)
                if row:
                    row.processed_at = datetime.now(UTC)
                    session.add(row)
            session.commit()

    def events(self) -> list[EventRecord]:
        with get_session(self.engine) as session:
            return list(
                session.exec(
                    select(EventRecord)
                    .where(
                        EventRecord.repo == self.repo,
                        col(EventRecord.processed_at).is_(None),
                    )
                    .order_by(col(EventRecord.received_at))
                )
            )
