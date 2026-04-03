import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from chute.config import Settings
from chute.core.models import PullRequestRecord
from chute.github.auth import GitHubAppCredentials, build_app_jwt
from chute.reconcile.planner import AUTOMERGE_LABEL, QUEUE_NOW_LABEL

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GitHubRepositoryRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repository = GitHubRepositoryRef(owner=settings.github_owner, name=settings.github_repo)
        self.base_url = settings.github_api_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "chute",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        self._installation_token: str | None = None
        self._installation_token_expires_at: datetime | None = None

    @property
    def is_configured(self) -> bool:
        return all(
            [
                self.settings.github_app_id,
                self.settings.github_installation_id,
                self._private_key_pem,
            ]
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def list_relevant_open_pull_requests(self) -> list[PullRequestRecord]:
        if not self.is_configured:
            logger.info("github_client_not_configured skipping_startup_scrape")
            return []

        pulls = cast(
            list[dict[str, Any]],
            await self._get_json(
            f"/repos/{self.repository.owner}/{self.repository.name}/pulls",
            params={"state": "open", "per_page": 100},
            ),
        )
        relevant_numbers = [
            pr["number"]
            for pr in pulls
            if has_relevant_label([label["name"] for label in pr.get("labels", [])])
        ]
        result: list[PullRequestRecord] = []
        for number in relevant_numbers:
            pull_request = await self.get_pull_request(number)
            if pull_request is not None:
                result.append(pull_request)
        return result

    async def get_pull_request(self, number: int) -> PullRequestRecord | None:
        if not self.is_configured:
            logger.info("github_client_not_configured skipping_pull_request_fetch pr=%s", number)
            return None

        repo_path = f"/repos/{self.repository.owner}/{self.repository.name}"
        try:
            payload = cast(dict[str, Any], await self._get_json(f"{repo_path}/pulls/{number}"))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.info("github_pull_request_not_found pr=%s", number)
                return None
            raise
        reviews = cast(
            list[dict[str, Any]],
            await self._get_json(f"{repo_path}/pulls/{number}/reviews", params={"per_page": 100}),
        )
        check_runs_response = cast(
            dict[str, Any],
            await self._get_json(
                f"{repo_path}/commits/{payload['head']['sha']}/check-runs",
                headers={"Accept": "application/vnd.github+json"},
            ),
        )
        combined_status = cast(
            dict[str, Any],
            await self._get_json(f"{repo_path}/commits/{payload['head']['sha']}/status"),
        )
        return PullRequestRecord.from_github(
            self.repository.full_name,
            payload,
            reviews=reviews,
            check_runs=cast(list[dict[str, Any]], check_runs_response.get("check_runs", [])),
            combined_status=combined_status,
        )

    async def _get_json(
        self,
        path: str,
        *,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        token = await self._get_installation_token()
        response = await self._http.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {token}", **(headers or {})},
        )
        response.raise_for_status()
        return response.json()

    async def _get_installation_token(self) -> str:
        if (
            self._installation_token
            and self._installation_token_expires_at
            and datetime.now(UTC) < self._installation_token_expires_at
        ):
            return self._installation_token

        credentials = GitHubAppCredentials(
            app_id=self.settings.github_app_id or "",
            installation_id=self.settings.github_installation_id or "",
            private_key_pem=self._private_key_pem,
        )
        app_jwt = build_app_jwt(credentials)
        response = await self._http.post(
            f"/app/installations/{credentials.installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        self._installation_token = payload["token"]
        self._installation_token_expires_at = (
            datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")) - timedelta(minutes=1)
        )
        return self._installation_token

    @property
    def _private_key_pem(self) -> str:
        if self.settings.github_private_key_path:
            return Path(self.settings.github_private_key_path).read_text(encoding="utf-8")
        return ""


def has_relevant_label(labels: list[str]) -> bool:
    return AUTOMERGE_LABEL in labels or QUEUE_NOW_LABEL in labels
