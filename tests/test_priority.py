import asyncio

import pytest


async def test_priority_skips_preliminary_ci_and_restacks_existing_train(rig):
    for number in (1, 2):
        rig.api.add_pr(number)
        await rig.pr_event(number)
    await rig.complete(2)
    original = rig.api.add_pr(3)["head"]["sha"]

    await rig.pr_event(3, label="AutoMerge-Priority")

    assert rig.state()["order"] == [3, 1, 2]
    assert rig.entry(3)["head_sha"] == original
    assert rig.entry(3)["ci"] == "pending"
    assert "initial_ci_sha" not in rig.entry(3)
    assert rig.api.pull(3)["base"]["ref"] == "main"
    assert rig.api.pull(1)["base"]["ref"] == "pr-3"
    assert rig.api.pull(2)["base"]["ref"] == "pr-1"
    for number in (1, 2):
        assert rig.entry(number)["ci"] == "pending"
        files = rig.api.remote.git(rig.api.remote.path, "ls-tree", "--name-only", f"pr-{number}")
        assert "file-3.txt" in files
    assert not rig.api.merges

    await rig.complete(1)
    await rig.complete(2)
    await rig.complete(3)
    assert rig.api.merges == [3, 1, 2]


@pytest.mark.parametrize("already_queued", [False, True])
async def test_priority_promotes_existing_watcher_or_follower(rig, already_queued):
    for number in (1, 2):
        rig.api.add_pr(number)
        await rig.pr_event(number)
    if already_queued:
        await rig.complete(2)
    await rig.pr_event(2, label="automerge-priority")

    assert rig.state()["order"] == [2, 1]
    assert rig.api.pull(2)["base"]["ref"] == "main"
    files = rig.api.remote.git(rig.api.remote.path, "ls-tree", "--name-only", "pr-2")
    assert "file-2.txt" in files and "file-1.txt" not in files
    assert not rig.api.merges
    await rig.complete(2)
    assert rig.api.merges == [2]


async def test_priority_rebases_before_evaluating_old_ci(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)
    original = rig.api.add_pr(2)["head"]["sha"]
    rig.api.check(original, "failure")
    rig.api.remote.commit("main", {"new-main.txt": "new main\n"})

    await rig.pr_event(2, label="automerge-priority")

    assert rig.state()["order"] == [2, 1]
    assert rig.entry(2)["head_sha"] != original
    assert rig.entry(2)["ci"] == "pending"
    await rig.complete(2)
    assert rig.api.merges == [2]


async def test_new_priority_preempts_older_priority_once_and_survives_restart(rig):
    for number in (1, 2, 3):
        rig.api.add_pr(number)
        await rig.pr_event(number, label="automerge-priority")
    assert rig.state()["order"] == [3, 2, 1]
    heads = {n: rig.entry(n)["head_sha"] for n in (1, 2, 3)}
    pushes = len(rig.git.pushes)

    await rig.restart()
    await rig.idle()

    assert rig.state()["order"] == [3, 2, 1]
    assert {n: rig.entry(n)["head_sha"] for n in heads} == heads
    assert len(rig.git.pushes) == pushes


async def test_priority_draft_waits_then_takes_head_when_ready(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)
    rig.api.add_pr(2, draft=True)
    await rig.pr_event(2, label="automerge-priority")
    assert rig.entry(2)["phase"] == "watching"
    assert rig.state()["order"] == [1]
    rig.api.prs[2]["draft"] = False
    await rig.pr_event(2, "ready_for_review")
    assert rig.state()["order"] == [2, 1]


async def test_priority_failure_removes_both_admission_labels_and_repairs_train(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)
    rig.api.add_pr(2)
    await rig.pr_event(2)
    await rig.pr_event(2, label="automerge-priority")
    await rig.complete(2, "failure")
    assert rig.entry(2)["phase"] == "failed"
    assert rig.api.prs[2]["labels"] == [{"name": "chute-failed"}]
    assert rig.state()["order"] == [1]
    assert rig.api.pull(1)["base"]["ref"] == "main"
    await rig.restart()
    await rig.idle()
    assert rig.entry(2)["phase"] == "failed"


async def test_removing_priority_only_ejects_when_no_admission_label_remains(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1, label="automerge-priority")
    await rig.pr_event(1, label="automerge")
    await rig.pr_event(1, "unlabeled", label="automerge-priority")
    assert rig.state()["order"] == [1]
    await rig.pr_event(1, "unlabeled")
    assert rig.entry(1)["phase"] == "removed"
    assert rig.state()["order"] == []


async def test_startup_discovers_priority_without_webhook(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)
    rig.api.add_pr(2)
    rig.api.prs[2]["labels"] = [{"name": "automerge-priority"}]
    await rig.restart()
    await rig.idle()
    assert rig.state()["order"] == [2, 1]


async def test_priority_arriving_during_merge_guard_preempts_old_head(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)
    rig.api.add_pr(2)
    reached, release = asyncio.Event(), asyncio.Event()

    async def hold_review(request):
        reached.set()
        await release.wait()

    rig.api.overrides[("POST", "/graphql")] = hold_review
    await rig.complete(1, drain=False)
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await rig.pr_event(2, label="automerge-priority", drain=False)
    finally:
        release.set()
    await rig.idle()
    assert not rig.api.merges
    assert rig.state()["order"] == [2, 1]


async def test_restart_recovers_interrupted_priority_restack(rig):
    for number in (1, 2):
        rig.api.add_pr(number)
        await rig.pr_event(number)
    await rig.complete(2)

    def lost_response():
        raise RuntimeError("Lost priority push response")

    rig.git.after_push = lost_response
    await rig.pr_event(2, label="automerge-priority", allow_error=True)
    assert rig.entry(2)["pending"]
    pushes = len(rig.git.pushes)
    await rig.restart()
    await rig.idle()
    assert rig.state()["order"] == [2, 1]
    assert rig.entry(2)["pending"] is None
    assert len(rig.git.pushes) == pushes + 1  # Only the old head still needs restacking.
    assert rig.api.pull(2)["base"]["ref"] == "main"
    assert rig.api.pull(1)["base"]["ref"] == "pr-2"
