import asyncio
import logging
from contextlib import suppress

from chute.core.enums import ActionStatus, ActionType
from chute.db.repo import ActionRepository, PullRequestRepository, QueueRepository
from chute.github.client import GitHubClient
from chute.reconcile.planner import plan_pull_requests

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(
        self,
        pr_repo: PullRequestRepository,
        queue_repo: QueueRepository,
        action_repo: ActionRepository,
        github_client: GitHubClient,
        interval_seconds: float,
    ) -> None:
        self.pr_repo = pr_repo
        self.queue_repo = queue_repo
        self.action_repo = action_repo
        self.github_client = github_client
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="chute-reconciler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("reconcile_loop_failed")
            await asyncio.sleep(self.interval_seconds)

    async def run_once(self) -> None:
        dirty_rows = self.pr_repo.list_dirty()
        for row in dirty_rows:
            refreshed = await self.github_client.get_pull_request(row.number)
            if refreshed is None:
                continue
            self.pr_repo.upsert(refreshed)
            self.action_repo.log(
                repo=refreshed.repo,
                pr_number=refreshed.number,
                action_type=ActionType.REFRESH_PR,
                status=ActionStatus.SUCCESS,
                message="Refreshed pull request from GitHub",
            )

        pull_requests = self.pr_repo.list_all()
        if not pull_requests:
            return

        repo = pull_requests[0].repo
        existing_queue = [row.pr_number for row in self.queue_repo.list_active()]
        planned_pull_requests, planned_queue = plan_pull_requests(pull_requests, existing_queue)

        self.pr_repo.save_all(planned_pull_requests)
        enqueue_sources = {
            pr.number: pr.admission_mode for pr in planned_pull_requests if pr.number in planned_queue
        }
        self.queue_repo.replace_active(repo, planned_queue, enqueue_sources)

        for pr in planned_pull_requests:
            self.action_repo.log(
                repo=pr.repo,
                pr_number=pr.number,
                action_type=ActionType.RECONCILE_PR,
                status=ActionStatus.SUCCESS,
                message="Reconciled pull request state",
                metadata={
                    "desired_state": pr.state.value,
                    "blocked_reason": pr.blocked_reason.value,
                    "is_queued": pr.is_queued,
                    "is_head": pr.is_head,
                },
            )
