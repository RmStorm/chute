import asyncio

import httpx

from tests.harness import ROOT


async def test_restart_after_successful_push_with_lost_response(rig):
    for number in (1, 2):
        rig.api.add_pr(number)
        await rig.pr_event(number)

    def lost_response():
        raise RuntimeError("Lost push response")

    rig.git.after_push = lost_response
    await rig.complete(2, allow_error=True)
    pending = rig.entry(2)["pending"]
    assert pending and rig.api.remote.sha("pr-2") == pending["new"]
    assert rig.state()["inflight"]

    await rig.restart()
    await rig.idle()
    assert rig.entry(2)["pending"] is None
    assert rig.entry(2)["head_sha"] == pending["new"]
    assert rig.entry(2)["phase"] == "queued"
    assert rig.state()["order"] == [1, 2]
    assert len(rig.git.pushes) == 1
    await rig.complete(2)
    await rig.complete(1)
    assert rig.api.merges == [1, 2]


async def test_restart_after_successful_merge_with_lost_response(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)

    def lost_response():
        raise httpx.ReadTimeout("Lost merge response")

    rig.api.after_merge = lost_response
    await rig.complete(1, allow_error=True)
    assert rig.entry(1)["phase"] == "merging"
    assert rig.api.prs[1]["merged"]
    assert rig.state()["inflight"]
    await rig.restart()
    await rig.idle()
    assert rig.entry(1)["phase"] == "merged"
    assert rig.state()["order"] == []
    assert rig.api.merges == [1]


async def test_unlabel_delivered_during_merge_checks_prevents_merge(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)
    reached, release = asyncio.Event(), asyncio.Event()

    async def hold_review(request):
        reached.set()
        await release.wait()

    rig.api.overrides[("POST", "/graphql")] = hold_review
    await rig.complete(1, drain=False)
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await rig.pr_event(1, "unlabeled", drain=False)
    finally:
        release.set()
    await rig.idle()
    assert not rig.api.merges
    assert rig.entry(1)["phase"] == "removed"
    assert not rig.controller.store.events()


async def test_transient_api_failure_retries_durable_work_without_webhook(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)
    rig.api.overrides[("GET", f"{ROOT}/pulls/1")] = (503, {"message": "Unavailable"})
    await rig.complete(1, allow_error=True)
    assert "503" in rig.state()["error"]
    assert rig.state()["inflight"]["prs"] == [1]
    rig.api.overrides.clear()
    await rig.until(lambda s: s["entries"]["1"]["phase"] == "merged")
    await rig.idle()
    assert rig.api.merges == [1]
    assert rig.state().get("inflight") is None


async def test_completion_before_consistent_ci_read_retries_without_full_scan(rig):
    sha = rig.api.add_pr(1)["head"]["sha"]
    await rig.pr_event(1)
    path = f"{ROOT}/commits/{sha}/check-runs"
    rig.api.overrides[("GET", path)] = (200, {"check_runs": []})
    await rig.complete(1)
    assert rig.entry(1)["ci"] == "pending"
    assert rig.entry(1)["retry_at"]
    rig.api.overrides.clear()
    rig.api.requests.clear()
    await rig.until(lambda s: s["entries"]["1"]["phase"] == "merged")
    await rig.idle()
    assert rig.api.merges == [1]
    assert not any(path == f"{ROOT}/pulls" for _, path, _ in rig.api.requests)


async def test_startup_discovers_label_without_webhook(rig):
    sha = rig.api.add_pr(1)["head"]["sha"]
    rig.api.prs[1]["labels"] = [{"name": "Automerge"}]
    rig.api.check(sha)
    await rig.restart()
    await rig.idle()
    assert rig.entry(1)["phase"] == "merged"
    assert rig.api.merges == [1]
    assert not rig.git.pushes


async def test_force_with_lease_rejects_concurrent_user_push(rig):
    for number in (1, 2):
        rig.api.add_pr(number)
        await rig.pr_event(number)

    def concurrent_push():
        rig.api.remote.commit("pr-2", {"user.txt": "keep this\n"}, base="pr-2")

    rig.git.before_push = concurrent_push
    await rig.complete(2, allow_error=True)
    user_sha = rig.api.remote.sha("pr-2")
    assert rig.state()["error"]
    assert not rig.git.pushes
    assert user_sha not in rig.entry(2)["own_pushes"]
    await rig.restart()
    await rig.idle()
    assert rig.entry(2)["phase"] == "failed"
    assert rig.api.prs[2]["labels"] == [{"name": "chute-usererror"}]
    assert rig.state()["order"] == [1]
    assert rig.api.remote.git(rig.api.remote.path, "show", "pr-2:user.txt") == "keep this"


async def test_restart_after_cleanup_push_preserves_user_branch_and_ejection(rig):
    for number in (1, 2):
        rig.api.add_pr(number)
        await rig.pr_event(number)
    await rig.complete(2)

    def lost_response():
        raise RuntimeError("Lost cleanup push response")

    rig.git.after_push = lost_response
    await rig.complete(2, "failure", allow_error=True)
    assert rig.entry(2)["pending"]["cleanup"]
    pushed = rig.api.remote.sha("pr-2")
    await rig.restart()
    await rig.idle()
    assert rig.entry(2)["phase"] == "failed"
    assert rig.api.prs[2]["labels"] == [{"name": "chute-failed"}]
    assert rig.api.remote.sha("pr-2") == pushed
    assert len(rig.git.pushes) == 2
    assert rig.api.pull(2)["base"]["ref"] == "main"
    assert rig.state()["order"] == [1]
