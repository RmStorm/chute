import asyncio
import fcntl
import logging
from contextlib import suppress
from datetime import UTC, datetime

import httpx

from chute.core.enums import ActionStatus, ActionType, AdmissionMode, ChecksState, PullRequestState
from chute.core.models import PullRequestRecord
from chute.github.git import RebaseConflict, TrainGit
from chute.github.train import ConfigurationError, TrainGitHub
from chute.reconcile.store import TrainStore

logger = logging.getLogger(__name__)
ACTIVE = {"watching", "queued", "merging", "ejecting"}


def now():
    return datetime.now(UTC).isoformat()


def labeled(pr):
    return any(label["name"].casefold() == "automerge" for label in pr.get("labels", []))


class Reconciler:
    def __init__(self, pr_repo, queue_repo, action_repo, github_client, interval_seconds):
        self.pr_repo, self.queue_repo, self.action_repo = pr_repo, queue_repo, action_repo
        self.github_client = github_client
        self.settings = github_client.settings
        self.interval_seconds = interval_seconds
        self.api = TrainGitHub(github_client)
        self.git = TrainGit(self.settings.git_workdir)
        self.store = TrainStore(pr_repo.engine, github_client.repository.full_name)
        self.state = self.store.load()
        self._task = None
        self._wake = asyncio.Event()
        self._serial = asyncio.Lock()
        self._lock_file = None

    def wake(self):
        self._wake.set()

    async def start(self):
        if self._task:
            return
        self._lock_file = self.settings.database_path.with_suffix(".controller.lock").open("a")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise RuntimeError("Another Chute controller owns this database") from None
        self._task = asyncio.create_task(self._run(), name="chute-reconciler")

    async def stop(self):
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._lock_file:
            self._lock_file.close()

    def save(self):
        self.store.save(self.state)

    def log(self, number, message, error=False, action=ActionType.RECONCILE_PR):
        self.action_repo.log(
            repo=self.github_client.repository.full_name,
            pr_number=number,
            action_type=action,
            status=ActionStatus.ERROR if error else ActionStatus.SUCCESS,
            message=message,
        )
        (logger.error if error else logger.info)("%s pr=%s", message, number)

    async def _run(self):
        while True:
            self._wake.clear()
            try:
                await self.run_once()
            except Exception as exc:
                message = (
                    f"GitHub HTTP {exc.response.status_code}: {exc.request.url.path}"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else str(exc)
                )
                if self.state.get("error") != message:
                    self.log(None, message, error=True)
                self.state["error"] = message
                self.save()
                logger.exception("reconcile_failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    def watch(self, pr, observed=None):
        self.state["entries"][str(pr["number"])] = {
            "number": pr["number"],
            "phase": "watching",
            "watched_sha": pr["head"]["sha"],
            "head_sha": pr["head"]["sha"],
            "branch": pr["head"]["ref"],
            "original_base": pr["base"]["ref"],
            "since": observed or now(),
            "boundary": None,
            "target_sha": None,
            "target_ref": None,
            "own_pushes": [],
            "pending": None,
            "reason": "Waiting for initial CI against main",
            "ci": "pending",
            "queued_at": None,
            "label_absent": False,
        }
        self.save()
        self.log(pr["number"], "Watching labeled PR; initial CI must pass before admission")

    def eject(self, entry, reason, label):
        entry.update(phase="ejecting", reason=reason, failure_label=label)
        self.save()
        self.log(entry["number"], reason, error=bool(label), action=ActionType.DEQUEUE_PR)

    def consume_events(self):
        for event in self.store.events():
            payload = event.payload
            pr = payload.get("pull_request") or {}
            entry = self.state["entries"].get(str(pr.get("number")))
            if event.event_type == "pull_request" and pr.get("head"):
                label = payload.get("label", {}).get("name", "").casefold()
                if event.action == "labeled" and label == "automerge":
                    if entry is None or entry["phase"] not in ACTIVE:
                        self.watch(pr, event.received_at.replace(tzinfo=UTC).isoformat())
                elif entry and entry["phase"] in {"watching", "queued"}:
                    if event.action == "unlabeled" and label == "automerge":
                        self.eject(entry, "Automerge label removed", None)
                    elif event.action == "synchronize":
                        after = payload.get("after", pr["head"]["sha"])
                        if event.received_at.replace(tzinfo=UTC).isoformat() >= entry[
                            "since"
                        ] and after not in [entry["watched_sha"], *entry["own_pushes"]]:
                            self.eject(
                                entry, "User pushed after adding automerge", "chute-usererror"
                            )
            self.store.save(self.state, event.delivery_id)

    async def run_once(self):
        async with self._serial:
            await self._reconcile()

    async def check_entry(self, entry, policy):
        pr = await self.api.pull(entry["number"])
        if pr["head"]["sha"] != entry["head_sha"]:
            self.eject(entry, "User pushed after adding automerge", "chute-usererror")
            self.wake()
            return "pending", entry["reason"]
        if pr["state"] != "open" or not labeled(pr):
            self.wake()
            return "pending", "PR changed while checking CI"
        return await self.api.checks(
            entry["head_sha"], policy, pr.get("merge_commit_sha"), pr["base"]["sha"]
        )

    async def _reconcile(self):
        if not self.github_client.is_configured:
            raise ConfigurationError("GitHub App credentials are not configured")
        self.consume_events()
        pulls = await self.api.pages(
            f"{self.api.root}/pulls", state="open", sort="created", direction="asc"
        )
        for pr in pulls:
            entry = self.state["entries"].get(str(pr["number"]))
            if labeled(pr) and (
                entry is None or (entry["phase"] not in ACTIVE and entry.get("label_absent"))
            ):
                self.watch(pr)
            elif entry and not labeled(pr) and entry["phase"] not in ACTIVE:
                entry["label_absent"] = True
        self.save()
        policy = await self.api.policy(self.settings.target_branch)
        self.state.update(
            error=None, policy={"checks": policy["checks"], "reviews": policy["reviews"]}
        )
        await self.git.fetch(
            policy["clone_url"], await self.github_client._get_installation_token()
        )
        main_sha = await self.api.branch(self.settings.target_branch)
        snapshots = {}
        for entry in list(self.state["entries"].values()):
            if entry["phase"] not in ACTIVE:
                continue
            pr = await self.api.pull(entry["number"])
            snapshots[entry["number"]] = pr
            if pr.get("merged") or pr["state"] != "open":
                entry.update(
                    phase="merged" if pr.get("merged") else "removed",
                    reason="PR closed on GitHub",
                    pending=None,
                )
                self.save()
                continue
            if entry["phase"] == "ejecting":
                await self.finish_ejection(entry, pr, policy, main_sha)
                continue
            if not labeled(pr):
                self.eject(entry, "Automerge label removed", None)
                await self.finish_ejection(entry, pr, policy, main_sha)
                continue
            if entry.get("pending"):
                if not await self.finish_push(entry, pr, policy):
                    continue
                pr = await self.api.pull(entry["number"])
                snapshots[entry["number"]] = pr
            if pr["head"]["sha"] != entry["head_sha"]:
                self.eject(entry, "User pushed after adding automerge", "chute-usererror")
                await self.finish_ejection(entry, pr, policy, main_sha)
                continue
            if (pr["head"].get("repo") or {}).get(
                "full_name"
            ) != self.github_client.repository.full_name:
                self.eject(entry, "Only same-repository branches are supported", "chute-usererror")
                await self.finish_ejection(entry, pr, policy, main_sha)
                continue
            if entry["phase"] == "merging":
                entry["phase"] = "queued"  # A timed-out merge must be refreshed before retrying.
        self.state["order"] = [
            n for n in self.state["order"] if self.state["entries"][str(n)]["phase"] == "queued"
        ]
        self.save()
        target_ref, target_sha = self.settings.target_branch, main_sha
        for number in self.state["order"]:
            entry = self.state["entries"][str(number)]
            if not await self.prepare(entry, snapshots[number], target_ref, target_sha, policy):
                self.publish(snapshots)
                self.wake()
                return
            target_ref, target_sha = entry["branch"], entry["head_sha"]
        watchers = sorted(
            (e for e in self.state["entries"].values() if e["phase"] == "watching"),
            key=lambda e: (e["since"], e["number"]),
        )
        for entry in watchers:
            pr = snapshots[entry["number"]]
            if pr["base"]["ref"] != self.settings.target_branch:
                self.eject(entry, "PR must start against main", "chute-usererror")
                self.wake()
                continue
            if pr.get("draft"):
                entry["reason"] = "Draft PR is not eligible"
                continue
            entry["ci"], entry["reason"] = await self.check_entry(entry, policy)
            if entry["phase"] != "watching":
                continue
            if entry["ci"] == "failing":
                self.eject(entry, entry["reason"], "chute-failed")
                self.wake()
                continue
            first_head = not self.state["order"]
            if not first_head and entry["ci"] != "passing":
                continue
            entry["boundary"] = await self.git.boundary(entry["head_sha"], main_sha)
            entry.update(phase="queued", queued_at=now())
            if entry["ci"] == "passing":
                entry["initial_ci_sha"] = entry["head_sha"]
            self.state["order"].append(entry["number"])
            self.save()
            self.log(
                entry["number"],
                "Took the empty train's head position; waiting for merge requirements"
                if first_head
                else "Initial CI passed; admitted to train",
                action=ActionType.ENQUEUE_PR,
            )
            if not await self.prepare(entry, pr, target_ref, target_sha, policy):
                self.publish(snapshots)
                self.wake()
                return
            target_ref, target_sha = entry["branch"], entry["head_sha"]
        for number in self.state["order"]:
            entry = self.state["entries"][str(number)]
            entry["ci"], entry["reason"] = await self.check_entry(entry, policy)
            if entry["phase"] != "queued":
                self.publish(snapshots)
                return
            if entry["ci"] == "failing":
                self.eject(entry, entry["reason"], "chute-failed")
                self.publish(snapshots)
                self.wake()
                return
        self.publish(snapshots)
        if self.state["order"]:
            await self.merge_head(policy, main_sha)
        self.state["last_reconciled_at"] = now()
        self.save()

    async def prepare(self, entry, pr, target_ref, target_sha, policy):
        if entry["head_sha"] != pr["head"]["sha"]:
            self.eject(entry, "User pushed while preparing stack", "chute-usererror")
            return False
        if entry["target_sha"] != target_sha:
            # The first head already containing main needs no code rewrite or
            # second CI run. Its existing checks still refer to the exact head SHA.
            unchanged_head = (
                entry["target_sha"] is None
                and target_ref == self.settings.target_branch
                and entry["boundary"] == target_sha
            )
            # Preserve the tested commit when a landed predecessor has identical
            # content despite GitHub rewriting its commit metadata.
            preserve = entry["target_sha"] and await self.git.same_tree(
                entry["boundary"], target_sha
            )
            if unchanged_head or preserve:
                entry["target_sha"] = target_sha
                if unchanged_head:
                    self.log(
                        entry["number"], "Head already includes main; preserving commit and CI"
                    )
            else:
                try:
                    new_sha = await self.git.rebase(
                        entry["number"], entry["head_sha"], entry["boundary"], target_sha
                    )
                except RebaseConflict as exc:
                    self.eject(entry, str(exc), "chute-failed")
                    return False
                if new_sha == entry["head_sha"]:
                    raise ConfigurationError(
                        "Rebase did not produce a new commit for second-stage CI"
                    )
                entry["pending"] = {
                    "old": entry["head_sha"],
                    "new": new_sha,
                    "target_sha": target_sha,
                    "target_ref": target_ref,
                }
                entry["own_pushes"].append(new_sha)
                self.save()
                if not await self.finish_push(entry, await self.api.pull(entry["number"]), policy):
                    return False
        fresh = await self.api.pull(entry["number"])
        if fresh["head"]["sha"] != entry["head_sha"] or not labeled(fresh):
            return False
        if fresh["base"]["ref"] != target_ref:
            await self.api.retarget(entry["number"], target_ref)
            self.log(entry["number"], f"Retargeted to {target_ref}", action=ActionType.RETARGET_PR)
        entry["target_ref"] = target_ref
        self.save()
        return True

    async def finish_push(self, entry, pr, policy):
        pending = entry["pending"]
        actual = pr["head"]["sha"]
        if actual == pending["old"]:
            if entry["phase"] != "ejecting" and not labeled(pr):
                entry["pending"] = None
                self.eject(entry, "Automerge label removed before rebase push", None)
                return False
            if pr["base"]["ref"] != pending["target_ref"]:
                await self.api.retarget(entry["number"], pending["target_ref"])
            await self.git.push(
                policy["clone_url"],
                entry["branch"],
                pending["old"],
                pending["new"],
                await self.github_client._get_installation_token(),
            )
        elif actual != pending["new"]:
            entry["pending"] = None
            self.eject(entry, "User pushed during Chute rebase", "chute-usererror")
            return False
        entry.update(
            head_sha=pending["new"],
            boundary=pending["target_sha"],
            target_sha=pending["target_sha"],
            target_ref=pending["target_ref"],
            pending=None,
            ci="pending",
            reason="Waiting for CI after rebase",
        )
        if pending.get("cleanup"):
            entry["cleanup_done"] = True
        self.save()
        self.log(entry["number"], "Rebased and pushed with an explicit force-with-lease")
        return True

    async def finish_ejection(self, entry, pr, policy, main_sha):
        if entry.get("failure_label"):
            await self.api.failure_label(entry["number"], entry["failure_label"])
        reason = entry["reason"]
        if entry.get("pending"):
            pending = entry["pending"]
            if pending.get("cleanup") and pr["head"]["sha"] in {pending["old"], pending["new"]}:
                if not await self.finish_push(entry, pr, policy):
                    return
                pr = await self.api.pull(entry["number"])
            elif (
                pr["head"]["sha"] == pending["new"]
                or await self.git.boundary(pending["new"], pr["head"]["sha"]) == pending["new"]
            ):
                entry.update(boundary=pending["target_sha"], head_sha=pending["new"])
            entry["pending"] = None
            self.save()
        if entry["boundary"] and not entry.get("cleanup_done"):
            try:
                new_sha = await self.git.rebase(
                    entry["number"], pr["head"]["sha"], entry["boundary"], main_sha
                )
            except RebaseConflict:
                reason += "; cleanup conflicts: branch retained for manual repair"
            else:
                entry["pending"] = {
                    "old": pr["head"]["sha"],
                    "new": new_sha,
                    "target_sha": main_sha,
                    "target_ref": self.settings.target_branch,
                    "cleanup": True,
                }
                entry["own_pushes"].append(new_sha)
                self.save()
                if not await self.finish_push(entry, pr, policy):
                    return
        if pr["base"]["ref"] != self.settings.target_branch:
            await self.api.retarget(entry["number"], self.settings.target_branch)
        entry.update(
            phase="failed" if entry.get("failure_label") else "removed",
            reason=reason,
            pending=None,
            label_absent=True,
        )
        if entry["number"] in self.state["order"]:
            self.state["order"].remove(entry["number"])
        self.save()
        self.wake()

    async def merge_head(self, policy, main_sha):
        number = self.state["order"][0]
        entry = self.state["entries"][str(number)]
        if entry["phase"] != "queued" or entry["ci"] != "passing":
            return
        fresh = await self.api.pull(number)
        if (
            fresh["state"] != "open"
            or fresh.get("draft")
            or not labeled(fresh)
            or fresh["head"]["sha"] != entry["head_sha"]
            or fresh["base"]["ref"] != self.settings.target_branch
        ):
            self.wake()
            return
        if await self.api.branch(self.settings.target_branch) != main_sha:
            self.wake()
            return
        policy = await self.api.policy(self.settings.target_branch)
        if (await self.check_entry(entry, policy))[0] != "passing":
            return
        if not await self.api.review_ready(number, policy):
            entry["reason"] = "Waiting for GitHub review/merge requirements on main"
            self.save()
            return
        self.consume_events()
        if entry["phase"] != "queued":
            return
        entry["phase"] = "merging"
        self.save()
        result = await self.api.request(
            "PUT",
            f"{self.api.root}/pulls/{number}/merge",
            json={
                "merge_method": "rebase",
                "sha": entry["head_sha"],
            },
        )
        if not result.get("merged"):
            entry.update(phase="queued", reason=result.get("message", "GitHub declined merge"))
            self.save()
            return
        entry.update(
            phase="merged", reason="Rebase-and-merge completed", merge_sha=result.get("sha")
        )
        self.state["order"].remove(number)
        self.save()
        self.log(number, "Head landed using GitHub rebase-and-merge", action=ActionType.MERGE_HEAD)
        self.wake()

    def publish(self, snapshots):
        order = [
            n for n in self.state["order"] if self.state["entries"][str(n)]["phase"] == "queued"
        ]
        existing = [row.pr_number for row in self.queue_repo.list_active()]
        if order != existing:
            self.queue_repo.replace_active(
                self.github_client.repository.full_name,
                order,
                {n: AdmissionMode.AUTOMERGE for n in order},
            )
        for entry in self.state["entries"].values():
            pr = snapshots.get(entry["number"])
            if not pr:
                continue
            record = PullRequestRecord.from_github(
                self.github_client.repository.full_name,
                pr,
                reviews=[],
                check_runs=[],
                combined_status={},
            )
            record.state = {
                "watching": PullRequestState.ARMED,
                "queued": PullRequestState.QUEUED,
                "merged": PullRequestState.MERGED,
                "failed": PullRequestState.FAILED,
                "removed": PullRequestState.REMOVED,
                "ejecting": PullRequestState.FAILED,
            }.get(entry["phase"], PullRequestState.HEAD)
            record.head_sha = entry["head_sha"]
            if entry.get("target_ref") and entry["phase"] == "queued":
                record.base_ref = entry["target_ref"]
            record.is_queued = entry["number"] in order
            record.is_head = bool(order and order[0] == entry["number"])
            record.checks_state = ChecksState(entry["ci"])
            record.dirty = False
            if record.is_head:
                record.state = (
                    PullRequestState.READY
                    if entry["ci"] == "passing"
                    else PullRequestState.WAITING_CHECKS
                )
            self.pr_repo.upsert(record)
        self.save()
