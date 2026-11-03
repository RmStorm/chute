from tests.harness import ROOT


async def test_empty_train_preserves_sha_and_ci_completion_merges(rig):
    original = rig.api.add_pr(1)["head"]["sha"]
    await rig.pr_event(1)
    assert rig.state()["order"] == [1]
    assert rig.entry(1)["ci"] == "pending"
    assert rig.git.pushes == []

    last_sweep = rig.state()["last_reconciled_at"]
    rig.api.requests.clear()
    await rig.complete(1)

    assert rig.entry(1)["phase"] == "merged"
    assert rig.state()["order"] == []
    assert rig.api.merges == [1]
    assert rig.git.pushes == []
    assert rig.state()["last_reconciled_at"] == last_sweep
    assert not any(path == f"{ROOT}/pulls" for _, path, _ in rig.api.requests)
    merge = next(body for method, _, body in rig.api.requests if method == "PUT")
    assert merge == {"sha": original, "merge_method": "rebase"}
    assert rig.api.remote.sha("main") != original
    assert rig.api.remote.sha("main^{tree}") == rig.api.remote.sha(f"{original}^{{tree}}")


async def test_follower_needs_two_green_shas_then_promotes_without_push(rig):
    rig.api.add_pr(1)
    original = rig.api.add_pr(2)["head"]["sha"]
    await rig.pr_event(1)
    await rig.pr_event(2)
    assert rig.entry(2)["phase"] == "watching"
    assert not rig.git.pushes

    await rig.complete(2)
    rebased = rig.api.pull(2)["head"]["sha"]
    assert rebased != original
    assert rig.state()["order"] == [1, 2]
    assert rig.entry(2)["initial_ci_sha"] == original
    assert rig.entry(2)["ci"] == "pending"
    assert rig.api.pull(2)["base"]["ref"] == "pr-1"
    assert len(rig.git.pushes) == 1

    await rig.pr_event(2, "synchronize")  # Our own push is not a user violation.
    await rig.complete(2)
    assert rig.entry(2)["ci"] == "passing"
    assert not rig.api.merges
    await rig.complete(1)

    assert rig.api.merges == [1, 2]
    assert rig.state()["order"] == []
    assert rig.api.pull(2)["head"]["sha"] == rebased
    assert len(rig.git.pushes) == 1
    for number in (1, 2):
        assert (
            rig.api.remote.git(rig.api.remote.path, "show", f"main:file-{number}.txt")
            == f"change {number}"
        )


async def test_user_push_ejects_waiting_pr_and_can_be_relabelled(rig):
    for number in (1, 2):
        rig.api.add_pr(number)
        await rig.pr_event(number)
    rig.api.remote.commit("pr-2", {"extra.txt": "user edit\n"}, base="pr-2")
    await rig.pr_event(2, "synchronize")
    assert rig.entry(2)["phase"] == "failed"
    assert rig.api.prs[2]["labels"] == [{"name": "chute-usererror"}]
    assert rig.state()["order"] == [1]
    assert not rig.git.pushes

    await rig.pr_event(2)
    assert rig.entry(2)["phase"] == "watching"
    assert rig.entry(2)["head_sha"] == rig.api.remote.sha("pr-2")


async def test_failed_middle_pr_is_cleaned_up_and_descendant_rebased(rig):
    for number in (1, 2, 3):
        rig.api.add_pr(number)
        await rig.pr_event(number)
        if number > 1:
            await rig.complete(number)
    before = rig.api.remote.sha("pr-3")
    await rig.complete(2, "failure")

    assert rig.entry(2)["phase"] == "failed"
    assert rig.api.prs[2]["labels"] == [{"name": "chute-failed"}]
    assert rig.api.pull(2)["base"]["ref"] == "main"
    assert rig.state()["order"] == [1, 3]
    assert rig.api.pull(3)["base"]["ref"] == "pr-1"
    assert rig.api.remote.sha("pr-3") != before
    assert rig.entry(3)["ci"] == "pending"
    files = rig.api.remote.git(rig.api.remote.path, "ls-tree", "--name-only", "pr-3")
    assert "file-1.txt" in files and "file-3.txt" in files
    assert "file-2.txt" not in files
    assert not rig.api.merges


async def test_conflicting_follower_does_not_damage_head(rig):
    first = rig.api.add_pr(1, changes={"shared.txt": "first\n"})["head"]["sha"]
    rig.api.add_pr(2, changes={"shared.txt": "second\n"})
    await rig.pr_event(1)
    await rig.pr_event(2)
    await rig.complete(2)

    assert rig.entry(2)["phase"] == "failed"
    assert "conflict" in rig.entry(2)["reason"].lower()
    assert rig.api.prs[2]["labels"] == [{"name": "chute-failed"}]
    assert rig.state()["order"] == [1]
    assert rig.api.remote.sha("pr-1") == first
    await rig.complete(1)
    assert rig.api.merges == [1]


async def test_main_push_invalidates_head_ci_and_requires_fresh_checks(rig):
    original = rig.api.add_pr(1)["head"]["sha"]
    rig.api.prs[1]["reviewDecision"] = "REVIEW_REQUIRED"
    await rig.pr_event(1)
    await rig.complete(1)
    assert not rig.api.merges
    main = rig.api.remote.commit("main", {"main-edit.txt": "new main\n"})
    await rig.event("push", {"ref": "refs/heads/main", "after": main})
    assert rig.entry(1)["head_sha"] != original
    assert rig.entry(1)["ci"] == "pending"
    assert len(rig.git.pushes) == 1
    rig.api.prs[1]["reviewDecision"] = "APPROVED"
    await rig.complete(1)
    assert rig.api.merges == [1]


async def test_draft_waits_and_closed_head_promotes_waiter(rig):
    rig.api.add_pr(1, draft=True)
    await rig.pr_event(1)
    assert rig.entry(1)["phase"] == "watching"
    rig.api.prs[1]["draft"] = False
    await rig.pr_event(1, "ready_for_review")
    assert rig.state()["order"] == [1]
    rig.api.add_pr(2)
    await rig.pr_event(2)
    rig.api.prs[1]["state"] = "closed"
    await rig.pr_event(1, "closed")
    assert rig.entry(1)["phase"] == "removed"
    assert rig.state()["order"] == [2]
    assert not rig.git.pushes


async def test_unlabel_cleans_stack_without_failure_label(rig):
    for number in (1, 2):
        rig.api.add_pr(number)
        await rig.pr_event(number)
    await rig.complete(2)
    await rig.pr_event(2, "unlabeled")
    assert rig.entry(2)["phase"] == "removed"
    assert rig.api.prs[2]["labels"] == []
    assert rig.api.pull(2)["base"]["ref"] == "main"
    files = rig.api.remote.git(rig.api.remote.path, "ls-tree", "--name-only", "pr-2")
    assert "file-1.txt" not in files
    assert "file-2.txt" in files
