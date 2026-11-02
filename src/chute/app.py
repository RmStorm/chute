import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from chute.api.health import router as health_router
from chute.api.state import router as state_router
from chute.api.ui import router as ui_router
from chute.api.webhooks import router as webhook_router
from chute.config import Settings
from chute.db.repo import ActionRepository, EventRepository, PullRequestRepository, QueueRepository
from chute.db.sqlite import connect, create_db_and_tables
from chute.github.client import GitHubClient
from chute.reconcile.loop import Reconciler

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: object
    pr_repo: PullRequestRepository
    queue_repo: QueueRepository
    event_repo: EventRepository
    action_repo: ActionRepository
    github_client: GitHubClient
    reconciler: Reconciler


def build_container(settings: Settings) -> Container:
    engine = connect(settings.database_path)
    create_db_and_tables(engine)

    pr_repo = PullRequestRepository(engine)
    queue_repo = QueueRepository(engine)
    event_repo = EventRepository(engine)
    action_repo = ActionRepository(engine)
    github_client = GitHubClient(settings)
    reconciler = Reconciler(
        pr_repo=pr_repo,
        queue_repo=queue_repo,
        action_repo=action_repo,
        github_client=github_client,
        interval_seconds=settings.reconcile_interval_seconds,
    )
    return Container(
        settings=settings,
        engine=engine,
        pr_repo=pr_repo,
        queue_repo=queue_repo,
        event_repo=event_repo,
        action_repo=action_repo,
        github_client=github_client,
        reconciler=reconciler,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    container = app.state.container
    await container.reconciler.start()
    try:
        yield
    finally:
        await container.reconciler.stop()
        await container.github_client.close()


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title=settings.app_name, docs_url="/docs", redoc_url=None, lifespan=lifespan)
    container = build_container(settings)
    app.state.container = container

    app.include_router(health_router)
    app.include_router(state_router)
    app.include_router(webhook_router)
    app.include_router(ui_router)

    return app
