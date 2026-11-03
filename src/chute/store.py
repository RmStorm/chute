from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import JSON, Column, inspect, text
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Field, Session, SQLModel, col, create_engine, select


class Event(SQLModel, table=True):
    __tablename__ = "events"
    delivery_id: str = Field(primary_key=True)
    event_type: str
    action: str | None = None
    repo: str
    pr_number: int | None = None
    payload: dict = Field(sa_column=Column(JSON, nullable=False))
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    processed_at: datetime | None = None


class Action(SQLModel, table=True):
    __tablename__ = "actions"
    id: int | None = Field(default=None, primary_key=True)
    repo: str
    pr_number: int | None = None
    action_type: str
    status: str
    message: str
    details: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Journal(SQLModel, table=True):
    __tablename__ = "train_journal"
    repo: str = Field(primary_key=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Store:
    def __init__(self, path, repo):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.repo = repo
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

    def load(self):
        with Session(self.engine) as session:
            row = session.get(Journal, self.repo)
            return deepcopy(row.data) if row else {"entries": {}, "order": [], "error": None}

    def migrate(self):
        # Run under the controller lock. Retain retired tables as historical data.
        state = self.load()
        if state.get("schema_version", 1) >= 2:
            return state
        with Session(self.engine) as session:
            if inspect(self.engine).has_table("pull_requests"):
                rows = session.connection().execute(
                    text("SELECT number, title, author FROM pull_requests WHERE repo=:repo"),
                    {"repo": self.repo},
                )
                for number, title, author in rows:
                    if entry := state["entries"].get(str(number)):
                        entry.update(title=title, author=author)
            session.connection().execute(
                text(
                    "UPDATE actions SET status=lower(status), action_type=lower(action_type) "
                    "WHERE repo=:repo"
                ),
                {"repo": self.repo},
            )
            state["schema_version"] = 2
            session.merge(Journal(repo=self.repo, data=state))
            session.commit()
        return state

    def save(self, data, event=None):
        with Session(self.engine) as session:
            session.merge(Journal(repo=self.repo, data=deepcopy(data)))
            if event and (row := session.get(Event, event)):
                row.processed_at = datetime.now(UTC)
                session.add(row)
            session.commit()

    def accept(self, delivery, kind, payload):
        event = Event(
            delivery_id=delivery,
            event_type=kind,
            repo=self.repo,
            action=payload.get("action"),
            payload=payload,
            pr_number=payload.get("pull_request", {}).get("number"),
        )
        with Session(self.engine) as session:
            result = session.connection().execute(
                insert(Event)
                .values(**event.model_dump())
                .on_conflict_do_nothing(index_elements=["delivery_id"])
            )
            session.commit()
            return result.rowcount == 1

    def events(self):
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(Event)
                    .where(Event.repo == self.repo, col(Event.processed_at).is_(None))
                    .order_by(col(Event.received_at), col(Event.delivery_id))
                )
            )

    def log(self, number, message, error=False, action="reconcile_pr"):
        with Session(self.engine) as session:
            session.add(
                Action(
                    repo=self.repo,
                    pr_number=number,
                    message=message,
                    status="error" if error else "success",
                    action_type=action,
                )
            )
            session.commit()

    def recent(self, model, limit=50):
        timestamp = model.received_at if model is Event else model.created_at
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(model)
                    .where(model.repo == self.repo)
                    .order_by(col(timestamp).desc())
                    .limit(limit)
                )
            )
