import asyncio
import base64
import os
from pathlib import Path
from urllib.parse import urlsplit


class GitError(RuntimeError):
    pass


class RebaseConflict(GitError):
    pass


class TrainGit:
    """A dedicated checkout. Never runs hooks or repository-provided programs."""

    def __init__(self, path: Path):
        self.path = path.resolve()

    async def run(self, *args: str, token: str | None = None, allowed=(0,)) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_EDITOR": "true",
                "GIT_COMMITTER_NAME": "Chute",
                "GIT_COMMITTER_EMAIL": "chute@localhost",
            }
        )
        if token:
            auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
                }
            )
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            *args,
            cwd=self.path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode not in allowed:
            # Auth headers and remote responses never enter logs or the public API.
            raise GitError(f"git {args[0]} failed (exit {process.returncode})")
        return stdout.decode().strip()

    @staticmethod
    def check_remote(url: str):
        # The installation token is sent to this URL; only ever hand it to github.com.
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.port is not None
            or parsed.username
            or parsed.password
        ):
            raise GitError("Git remote must be https://github.com without embedded credentials")

    async def fetch(self, url: str, token: str):
        self.check_remote(url)
        self.path.mkdir(parents=True, exist_ok=True)
        if not (self.path / ".git").exists():
            await self.run("init", ".")
        await self.run(
            # Deleted branches can conflict with new names under the same prefix.
            # Only remote-tracking refs are pruned; refs/chute preserves train history.
            "fetch",
            "--prune",
            "--no-tags",
            url,
            "+refs/heads/*:refs/remotes/origin/*",
            token=token,
        )

    async def boundary(self, head: str, base: str) -> str:
        return await self.run("merge-base", head, base)

    async def same_tree(self, left: str, right: str) -> bool:
        return await self.run("rev-parse", f"{left}^{{tree}}") == await self.run(
            "rev-parse", f"{right}^{{tree}}"
        )

    async def rebase(self, number: int, head: str, boundary: str, target: str) -> str:
        # Keep old boundaries reachable even after upstream branches disappear.
        await self.run("update-ref", f"refs/chute/{number}/boundary", boundary)
        await self.run("update-ref", f"refs/chute/{number}/before", head)
        await self.run("rebase", "--abort", allowed=(0, 128))
        await self.run("checkout", "--detach", "--force", head)
        try:
            await self.run("rebase", "--force-rebase", "--empty=drop", "--onto", target, boundary)
        except GitError:
            conflicts = await self.run("diff", "--name-only", "--diff-filter=U")
            await self.run("rebase", "--abort", allowed=(0, 128))
            if conflicts:
                raise RebaseConflict("Rebase conflicts require manual resolution") from None
            raise
        new_sha = await self.run("rev-parse", "HEAD")
        if new_sha == target:
            raise RebaseConflict("PR has no remaining changes after rebasing")
        await self.run("update-ref", f"refs/chute/{number}/after", new_sha)
        return new_sha

    async def push(self, url: str, branch: str, old: str, new: str, token: str):
        self.check_remote(url)
        await self.run("check-ref-format", f"refs/heads/{branch}")
        await self.run(
            "push",
            f"--force-with-lease=refs/heads/{branch}:{old}",
            url,
            f"{new}:refs/heads/{branch}",
            token=token,
        )
