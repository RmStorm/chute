"""External boundary simulator. No train/admission decisions belong here."""

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import httpx
import jwt

from chute.app import create_app
from chute.git import TrainGit

REPO = "test/train"
ROOT = f"/repos/{REPO}"
CLONE = "https://github.com/test/train.git"


class Remote:
    def __init__(self, root):
        self.path, self.work = root / "remote.git", root / "author"
        self.git(root, "init", "--bare", "--initial-branch=main", str(self.path))
        self.git(root, "clone", str(self.path), str(self.work))
        (self.work / "shared.txt").write_text("base\n")
        self.git(self.work, "add", ".")
        self.git(self.work, "commit", "-m", "initial")
        self.git(self.work, "push", "origin", "HEAD:main")

    def git(self, cwd, *args, committer="Developer"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Developer",
            GIT_AUTHOR_EMAIL="dev@example.invalid",
            GIT_COMMITTER_NAME=committer,
            GIT_COMMITTER_EMAIL="git@example.invalid",
            GIT_ALLOW_PROTOCOL="file",
        )
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise AssertionError(f"git {args}: {result.stderr}")
        return result.stdout.strip()

    def sha(self, ref):
        return self.git(self.path, "rev-parse", ref)

    def commit(self, branch, changes, base="main"):
        self.git(self.work, "fetch", "origin")
        self.git(self.work, "checkout", "-B", branch, self.sha(base))
        for name, content in changes.items():
            (self.work / name).write_text(content)
        self.git(self.work, "add", ".")
        self.git(self.work, "commit", "-m", f"change {branch}")
        self.git(self.work, "push", "origin", f"HEAD:refs/heads/{branch}")
        return self.sha(branch)

    def land(self, branch, base):
        self.git(self.work, "fetch", "origin")
        head, target = self.sha(branch), self.sha(base)
        # Approximate GitHub's rebase merge with real patch-equivalence and cherry-picks.
        own = {
            line[2:]
            for line in self.git(self.work, "cherry", target, head).splitlines()
            if line.startswith("+")
        }
        commits = self.git(self.work, "rev-list", "--reverse", f"{target}..{head}").splitlines()
        self.git(self.work, "checkout", "--detach", target)
        for commit in commits:
            if commit in own:
                self.git(self.work, "cherry-pick", commit, committer="GitHub")
        self.git(self.work, "push", "origin", f"HEAD:refs/heads/{base}")
        return self.sha(base)


class LocalGit(TrainGit):
    """Only reroute the fake HTTPS remote; execute every Git command for real."""

    def __init__(self, path, remote):
        super().__init__(path)
        self.remote = remote
        self.pushes = []
        self.before_push = None
        self.after_push = None

    async def run(self, *args, **kwargs):
        if args[0] == "push" and self.before_push:
            hook, self.before_push = self.before_push, None
            hook()
        if args[0] in {"fetch", "push"}:
            assert CLONE in args, f"Unexpected Git destination: {args}"
            args = tuple(str(self.remote.path) if a == CLONE else a for a in args)
        result = await super().run(*args, **kwargs)
        if args[0] == "push":
            self.pushes.append(args[-1])
            if self.after_push:
                hook, self.after_push = self.after_push, None
                hook()
        return result


class GitHubAPI:
    def __init__(self, remote, public_key):
        self.remote, self.public_key = remote, public_key
        self.prs, self.runs, self.statuses, self.parents = {}, {}, {}, {}
        self.labels = {"automerge"}
        self.protection = {"required_status_checks": {"checks": [{"context": "ci", "app_id": 7}]}}
        self.rules = []
        self.allow_rebase = True
        self.requests, self.merges, self.unexpected = [], [], []
        self.overrides = {}
        self.after_merge = None

    def add_pr(self, number, *, changes=None, base="main", draft=False):
        branch = f"pr-{number}"
        self.remote.commit(branch, changes or {f"file-{number}.txt": f"change {number}\n"}, base)
        self.prs[number] = {
            "number": number,
            "title": f"PR {number}",
            "user": {"login": "dev"},
            "branch": branch,
            "base_ref": base,
            "labels": [],
            "state": "open",
            "merged": False,
            "draft": draft,
            "merge_commit_sha": None,
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
        }
        return self.pull(number)

    def pull(self, number):
        pr = deepcopy(self.prs[number])
        pr["head"] = {
            "sha": self.remote.sha(pr["branch"]),
            "ref": pr["branch"],
            "repo": {"full_name": pr.get("head_repo", REPO)},
        }
        pr["base"] = {"sha": self.remote.sha(pr["base_ref"]), "ref": pr["base_ref"]}
        return pr

    def check(self, sha, conclusion="success", *, app=7, name="ci"):
        self.runs.setdefault(sha, []).append(
            {
                "id": sum(map(len, self.runs.values())) + 1,
                "name": name,
                "app": {"id": app},
                "status": "completed" if conclusion else "in_progress",
                "conclusion": conclusion,
            }
        )

    async def handle(self, request):
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else {}
        self.requests.append((method, path, body))
        override = self.overrides.get((method, path))
        if override is not None:
            if callable(override):
                result = await override(request)
                if result is not None:
                    return result
            else:
                return httpx.Response(override[0], json=override[1])
        if path == "/app/installations/1/access_tokens" and method == "POST":
            claims = jwt.decode(
                request.headers["authorization"].split()[1], self.public_key, algorithms=["RS256"]
            )
            assert claims["iss"] == "1"
            return httpx.Response(
                201,
                json={
                    "token": "test-installation-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        assert request.headers["authorization"] == "Bearer test-installation-token"
        data: Any = None
        if path == ROOT and method == "GET":
            data = {"clone_url": CLONE, "allow_rebase_merge": self.allow_rebase}
        elif path == f"{ROOT}/branches/main/protection":
            if self.protection is None:
                return httpx.Response(404, json={"message": "Branch not protected"})
            data = self.protection
        elif path == f"{ROOT}/branches/main":
            data = {"protected": bool(self.rules)}
        elif path == f"{ROOT}/rules/branches/main":
            data = self.rules
        elif path.startswith(f"{ROOT}/git/ref/heads/"):
            data = {"object": {"sha": self.remote.sha(path.split("/heads/")[1])}}
        elif path == f"{ROOT}/pulls" and method == "GET":
            data = [self.pull(n) for n, p in self.prs.items() if p["state"] == "open"]
        elif path.startswith(f"{ROOT}/pulls/"):
            number = int(path.split("/pulls/")[1].split("/")[0])
            pr = self.prs[number]
            if method == "GET":
                data = self.pull(number)
            elif method == "PATCH":
                pr["base_ref"] = body["base"]
                data = self.pull(number)
            elif method == "PUT" and path.endswith("/merge"):
                assert body["merge_method"] == "rebase"
                self.merges.append(number)
                if body["sha"] != self.remote.sha(pr["branch"]):
                    return httpx.Response(409, json={"message": "Head changed"})
                sha = self.remote.land(pr["branch"], pr["base_ref"])
                pr.update(merged=True, state="closed")
                for other in self.prs.values():
                    if other["base_ref"] == pr["branch"]:
                        other["base_ref"] = pr["base_ref"]
                if self.after_merge:
                    hook, self.after_merge = self.after_merge, None
                    hook()
                data = {"merged": True, "sha": sha}
        elif path.startswith(f"{ROOT}/commits/"):
            sha, kind = path.split("/commits/")[1].split("/")
            if kind == "check-runs":
                data = {"check_runs": self.runs.get(sha, [])}
            elif kind == "statuses":
                data = self.statuses.get(sha, [])
        elif path.startswith(f"{ROOT}/git/commits/"):
            sha = path.rsplit("/", 1)[1]
            if sha not in self.parents:
                return httpx.Response(404, json={"message": "Not Found"})
            data = {"parents": [{"sha": p} for p in self.parents[sha]]}
        elif path == "/graphql":
            pr = self.prs[body["variables"]["number"]]
            data = {
                "data": {
                    "repository": {
                        "pullRequest": {k: pr[k] for k in ("reviewDecision", "mergeStateStatus")}
                    }
                }
            }
        elif path == f"{ROOT}/labels" and method == "POST":
            self.labels.add(body["name"])
            data = body
        elif path.startswith(f"{ROOT}/labels/"):
            name = path.rsplit("/", 1)[1]
            return httpx.Response(200 if name in self.labels else 404, json={"name": name})
        elif path.startswith(f"{ROOT}/issues/"):
            parts = path.split("/issues/")[1].split("/")
            pr = self.prs[int(parts[0])]
            if method == "POST":
                pr["labels"] = [
                    {"name": n}
                    for n in sorted({l["name"] for l in pr["labels"]} | set(body["labels"]))
                ]
            elif method == "DELETE":
                pr["labels"] = [l for l in pr["labels"] if l["name"] != parts[2]]
            data = pr["labels"]
        if data is None:
            self.unexpected.append(f"{method} {path}")
            raise AssertionError(f"Unexpected GitHub request: {method} {path}")
        # Real pagination, including the empty page after exactly 100 items.
        page, size = (
            int(request.url.params.get("page", 1)),
            int(request.url.params.get("per_page", 100)),
        )
        if isinstance(data, list):
            data = data[(page - 1) * size : page * size]
        elif "check_runs" in data:
            checks = cast(dict[str, Any], data)["check_runs"]
            data = {"check_runs": checks[(page - 1) * size : page * size]}
        return httpx.Response(200, json=data)


class Harness:
    def __init__(self, settings, api):
        self.settings, self.api = settings, api
        self.git = LocalGit(settings.git_workdir, api.remote)
        self.app = self.client = self.lifespan = None

    @property
    def controller(self):
        assert self.app is not None
        return self.app.state.controller

    def state(self):
        return self.controller.store.load()

    def entry(self, number):
        return self.state()["entries"][str(number)]

    async def start(self):
        self.app = create_app(
            self.settings, github_transport=httpx.MockTransport(self.api.handle), git=self.git
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://chute.invalid"
        )
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        await asyncio.sleep(0)  # Let the new worker start before inspecting persisted state.
        await self.until(lambda s: bool(s.get("last_reconciled_at") or s.get("error")))
        await self.idle(allow_error=True)

    async def stop(self):
        if self.lifespan:
            await self.lifespan.__aexit__(None, None, None)
            self.lifespan = None
        if self.client:
            await self.client.aclose()
        if self.app:
            self.controller.store.engine.dispose()

    async def restart(self):
        await self.stop()
        await self.start()

    async def until(self, predicate):
        async def wait():
            while True:
                assert not self.api.unexpected, self.api.unexpected
                task = self.controller._task
                if task and task.done():
                    task.result()
                    raise AssertionError("Controller worker exited")
                state = self.state()
                if predicate(state):
                    return state
                await asyncio.sleep(0.01)

        try:
            return await asyncio.wait_for(wait(), 10)
        except TimeoutError:
            raise AssertionError(
                f"Controller did not settle; state={self.state()}; requests={self.api.requests[-8:]}"
            ) from None

    async def idle(self, *, allow_error=False):
        def settled(state):
            c = self.controller
            return (
                not c.running
                and not c._wake.is_set()
                and not c.store.events()
                and (state.get("error") or not (state.get("work") or state.get("inflight")))
            )

        state = await self.until(settled)
        if not allow_error:
            assert state.get("error") is None, state["error"]
        return state

    async def event(self, kind, payload, *, delivery=None, drain=True, allow_error=False):
        assert self.client is not None
        payload = {"repository": {"full_name": REPO}, **payload}
        body = json.dumps(payload).encode()
        signature = hmac.new(
            self.settings.github_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        response = await self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": kind,
                "X-GitHub-Delivery": delivery or str(uuid4()),
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )
        assert response.status_code == 202, response.text
        if drain:
            await self.idle(allow_error=allow_error)
        return response.json()

    async def pr_event(self, number, action="labeled", **kwargs):
        if action == "labeled":
            self.api.prs[number]["labels"] = [{"name": "automerge"}]
        elif action == "unlabeled":
            self.api.prs[number]["labels"] = []
        pr = self.api.pull(number)
        return await self.event(
            "pull_request",
            {
                "action": action,
                "label": {"name": "automerge"},
                "pull_request": pr,
                "after": pr["head"]["sha"],
            },
            **kwargs,
        )

    async def complete(self, number, conclusion="success", **kwargs):
        sha = self.api.pull(number)["head"]["sha"]
        self.api.check(sha, conclusion)
        return await self.event(
            "check_run",
            {
                "action": "completed",
                "check_run": {"head_sha": sha, "pull_requests": [{"number": number}]},
            },
            **kwargs,
        )
