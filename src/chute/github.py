from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import jwt


class ConfigurationError(RuntimeError):
    pass


class GitHub:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.repo = f"{settings.github_owner}/{settings.github_repo}"
        self.root = f"/repos/{self.repo}"
        self.http = httpx.AsyncClient(
            base_url=settings.github_api_url.rstrip("/"),
            timeout=30,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "chute",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._token = None
        self._expires = datetime.min.replace(tzinfo=UTC)

    async def close(self):
        await self.http.aclose()

    async def token(self):
        now = datetime.now(UTC)
        if self._token and now < self._expires:
            return self._token
        s = self.settings
        if not all((s.github_app_id, s.github_installation_id, s.github_private_key_path)):
            raise ConfigurationError("GitHub App credentials are not configured")
        app_jwt = jwt.encode(
            {
                "iat": int((now - timedelta(seconds=30)).timestamp()),
                "exp": int((now + timedelta(minutes=9)).timestamp()),
                "iss": s.github_app_id,
            },
            s.github_private_key_path.read_text(),
            algorithm="RS256",
        )
        response = await self.http.post(
            f"/app/installations/{s.github_installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["token"]
        self._expires = datetime.fromisoformat(payload["expires_at"]) - timedelta(minutes=1)
        return self._token

    async def request(self, method: str, path: str, **kwargs):
        token = await self.token()
        response = await self.http.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code in {401, 403}:
            # Installation permissions may have changed since this token was minted.
            self._token = None
        response.raise_for_status()
        return response.json() if response.content else None

    async def pages(self, path: str, key: str | None = None, **params) -> list:
        result = []
        page = 1
        while True:
            payload = await self.request(
                "GET", path, params={**params, "per_page": 100, "page": page}
            )
            items = payload[key] if key else payload
            result.extend(items)
            if len(items) < 100:
                return result
            page += 1

    async def pull(self, number: int) -> dict:
        return await self.request("GET", f"{self.root}/pulls/{number}")

    async def branch(self, name: str) -> str:
        result = await self.request("GET", f"{self.root}/git/ref/heads/{quote(name, safe='')}")
        return result["object"]["sha"]

    async def policy(self, branch: str) -> dict:
        required = set()
        reviews = False
        try:
            protection = await self.request(
                "GET", f"{self.root}/branches/{quote(branch, safe='')}/protection"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise ConfigurationError(
                    "Cannot read branch protection; grant the GitHub App Administration: read."
                ) from exc
            if exc.response.json().get("message") != "Branch not protected":
                raise ConfigurationError(
                    "Branch protection lookup was denied or hidden; grant Administration: read."
                ) from exc
            # A protected branch returning 404 may mean insufficient permissions.
            info = await self.request("GET", f"{self.root}/branches/{quote(branch, safe='')}")
            protection = {}
            protected = info.get("protected", False)
        else:
            protected = False
        checks = protection.get("required_status_checks") or {}
        for check in checks.get("checks", []):
            required.add((check["context"], check.get("app_id") or -1))
        known = {name for name, _ in required}
        required.update((name, -1) for name in checks.get("contexts", []) if name not in known)
        review_rules = protection.get("required_pull_request_reviews") or {}
        reviews = bool(
            review_rules.get("required_approving_review_count")
            or review_rules.get("require_code_owner_reviews")
            or review_rules.get("require_last_push_approval")
        )
        try:
            rules = await self.pages(f"{self.root}/rules/branches/{quote(branch, safe='')}")
        except httpx.HTTPStatusError as exc:
            raise ConfigurationError(
                "Cannot read active repository rulesets; refusing to merge."
            ) from exc
        for rule in rules:
            parameters = rule.get("parameters", {})
            if rule["type"] == "required_status_checks":
                for check in parameters.get("required_status_checks", []):
                    required.add((check["context"], check.get("integration_id") or -1))
            if rule["type"] == "pull_request":
                reviews = reviews or bool(
                    parameters.get("required_approving_review_count")
                    or parameters.get("require_code_owner_review")
                    or parameters.get("require_last_push_approval")
                )
        if protected and not rules:
            raise ConfigurationError(
                "Branch is protected but its policy is unreadable; refusing to merge."
            )
        if not required:
            raise ConfigurationError(
                f"No required CI checks configured for {branch}. Chute cannot do its job. "
                "Gryffindors, stop trying to break the rules: configure required status checks."
            )
        repo = await self.request("GET", self.root)
        if not repo.get("allow_rebase_merge"):
            raise ConfigurationError("Enable rebase merging in repository settings.")
        return {"checks": sorted(required), "reviews": reviews, "clone_url": repo["clone_url"]}

    async def checks(
        self, sha: str, policy: dict, merge_sha: str | None = None, base_sha: str | None = None
    ) -> tuple[str, str]:
        runs = await self.pages(
            f"{self.root}/commits/{sha}/check-runs", "check_runs", filter="latest"
        )
        statuses = await self.pages(f"{self.root}/commits/{sha}/statuses")
        # GitHub Actions may report against refs/pull/N/merge. Prefer those
        # results when the current synthetic merge has the required context.
        merge_runs, merge_statuses = [], []
        if merge_sha and merge_sha != sha:
            try:
                commit = await self.request("GET", f"{self.root}/git/commits/{merge_sha}")
                parents = {p["sha"] for p in commit["parents"]}
                if sha not in parents or base_sha not in parents:
                    merge_sha = None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                merge_sha = None
        if merge_sha and merge_sha != sha:
            try:
                merge_runs = await self.pages(
                    f"{self.root}/commits/{merge_sha}/check-runs", "check_runs", filter="latest"
                )
                merge_statuses = await self.pages(f"{self.root}/commits/{merge_sha}/statuses")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        pending = []
        failed = []
        for context, app_id in policy["checks"]:
            source_runs = merge_runs if any(r["name"] == context for r in merge_runs) else runs
            source_statuses = (
                merge_statuses if any(s["context"] == context for s in merge_statuses) else statuses
            )
            matching = [
                r
                for r in source_runs
                if r["name"] == context and (app_id == -1 or r.get("app", {}).get("id") == app_id)
            ]
            # Multiple producers with the same required name must all pass. Within
            # each producer, only its latest attempt applies.
            latest = {}
            for run in sorted(matching, key=lambda r: r["id"]):
                latest[run.get("app", {}).get("id")] = run
            outcomes = []
            for run in latest.values():
                outcomes.append(
                    "pending"
                    if run["status"] != "completed"
                    else (
                        "passing"
                        if run["conclusion"] in {"success", "neutral", "skipped"}
                        else "failing"
                    )
                )
            matching_status = [s for s in source_statuses if s["context"] == context]
            if matching_status and app_id == -1:
                state = max(matching_status, key=lambda s: s["id"])["state"]
                outcomes.append({"success": "passing", "pending": "pending"}.get(state, "failing"))
            if "failing" in outcomes:
                failed.append(context)
            elif not outcomes or "pending" in outcomes:
                pending.append(context)
        if failed:
            return "failing", "Required CI failed: " + ", ".join(failed)
        if pending:
            return "pending", "Waiting for required CI: " + ", ".join(pending)
        return "passing", "Required CI passed"

    async def review_ready(self, number: int, policy: dict) -> bool:
        result = await self.request(
            "POST",
            "/graphql",
            json={
                "query": """query($owner:String!, $name:String!, $number:Int!) {
              repository(owner:$owner,name:$name) { pullRequest(number:$number) {
                reviewDecision mergeStateStatus
              } }
            }""",
                "variables": {
                    "owner": self.settings.github_owner,
                    "name": self.settings.github_repo,
                    "number": number,
                },
            },
        )
        if result.get("errors"):
            raise ConfigurationError("Cannot read GitHub merge/review eligibility.")
        pr = result["data"]["repository"]["pullRequest"]
        if pr["reviewDecision"] in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
            return False
        if policy["reviews"] and pr["reviewDecision"] != "APPROVED":
            return False
        return pr["mergeStateStatus"] in {"CLEAN", "UNSTABLE", "HAS_HOOKS"}

    async def retarget(self, number: int, branch: str):
        await self.request("PATCH", f"{self.root}/pulls/{number}", json={"base": branch})

    async def failure_label(self, number: int, label: str):
        try:
            await self.request("GET", f"{self.root}/labels/{label}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            try:
                await self.request(
                    "POST",
                    f"{self.root}/labels",
                    json={
                        "name": label,
                        "color": "b60205",
                        "description": "Removed from the Chute merge train",
                    },
                )
            except httpx.HTTPStatusError as create_exc:
                if create_exc.response.status_code != 422:
                    raise
        await self.request("POST", f"{self.root}/issues/{number}/labels", json={"labels": [label]})
        labels = await self.pages(f"{self.root}/issues/{number}/labels")
        for existing in labels:
            if existing["name"].casefold() == "automerge":
                try:
                    await self.request(
                        "DELETE",
                        f"{self.root}/issues/{number}/labels/{quote(existing['name'], safe='')}",
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
