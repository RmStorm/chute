from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine


def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )


def create_db_and_tables(engine) -> None:
    SQLModel.metadata.create_all(engine)


def get_session(engine) -> Session:
    return Session(engine)
