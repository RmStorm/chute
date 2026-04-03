from chute.core.enums import ActionStatus, ActionType
from chute.db.repo import ActionRepository, PullRequestRepository
from chute.github.client import GitHubClient


async def bootstrap_pull_requests(
    pr_repo: PullRequestRepository,
    action_repo: ActionRepository,
    github_client: GitHubClient,
) -> int:
    prs = await github_client.list_relevant_open_pull_requests()
    for pr in prs:
        pr_repo.upsert(pr)

    action_repo.log(
        repo=github_client.repository.full_name,
        pr_number=None,
        action_type=ActionType.STARTUP_SCRAPE,
        status=ActionStatus.SUCCESS,
        message="Startup scrape completed",
        metadata={"count": len(prs)},
    )
    return len(prs)

