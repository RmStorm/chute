import pytest

from tests.harness import ROOT


@pytest.mark.parametrize("failure", ["empty", "hidden", "forbidden", "rules", "rebase"])
async def test_policy_errors_fail_closed_and_manual_reconcile_recovers(rig, failure):
    sha = rig.api.add_pr(1)["head"]["sha"]
    rig.api.check(sha)
    original = rig.api.protection
    if failure == "empty":
        rig.api.protection = {}
    elif failure in {"hidden", "forbidden"}:
        code = 404 if failure == "hidden" else 403
        rig.api.overrides[("GET", f"{ROOT}/branches/main/protection")] = (
            code,
            {"message": "Not Found"},
        )
    elif failure == "rules":
        rig.api.overrides[("GET", f"{ROOT}/rules/branches/main")] = (403, {"message": "Forbidden"})
    else:
        rig.api.allow_rebase = False
    await rig.pr_event(1, allow_error=True)
    assert rig.state()["error"]
    if failure == "empty":
        assert "Gryffindors" in rig.state()["error"]
    assert (await rig.client.get("/readyz")).status_code == 503
    assert not rig.api.merges and not rig.git.pushes

    rig.api.protection = original
    rig.api.allow_rebase = True
    rig.api.overrides.clear()
    response = await rig.client.post("/reconcile", headers={"X-Chute-Action": "reconcile"})
    assert response.status_code == 202
    await rig.idle()
    assert rig.api.merges == [1]
    assert (await rig.client.get("/readyz")).status_code == 200


async def test_rulesets_require_matching_app_and_approved_review(rig):
    rig.api.protection = None
    rig.api.rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "ci", "integration_id": 7}]},
        },
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
    ]
    sha = rig.api.add_pr(1)["head"]["sha"]
    rig.api.check(sha, app=99)
    rig.api.prs[1]["reviewDecision"] = None
    await rig.pr_event(1)
    assert rig.entry(1)["ci"] == "pending"
    await rig.complete(1)
    assert rig.entry(1)["ci"] == "passing"
    assert not rig.api.merges
    rig.api.prs[1]["reviewDecision"] = "APPROVED"
    await rig.event("pull_request_review", {"action": "submitted", "pull_request": rig.api.pull(1)})
    assert rig.api.merges == [1]


@pytest.mark.parametrize(
    "decision,status",
    [
        ("CHANGES_REQUESTED", "CLEAN"),
        ("APPROVED", "BLOCKED"),
        ("APPROVED", "UNKNOWN"),
    ],
)
async def test_github_merge_gates_are_respected(rig, decision, status):
    sha = rig.api.add_pr(1)["head"]["sha"]
    rig.api.check(sha)
    rig.api.prs[1].update(reviewDecision=decision, mergeStateStatus=status)
    await rig.pr_event(1)
    assert not rig.api.merges
    assert "Waiting for GitHub" in rig.entry(1)["reason"]
    rig.api.prs[1].update(reviewDecision="APPROVED", mergeStateStatus="CLEAN")
    await rig.event("pull_request_review", {"action": "dismissed", "pull_request": rig.api.pull(1)})
    assert rig.api.merges == [1]


async def test_only_current_synthetic_merge_checks_count(rig):
    pr = rig.api.add_pr(1)
    synthetic = "a" * 40
    rig.api.prs[1]["merge_commit_sha"] = synthetic
    rig.api.parents[synthetic] = ["b" * 40, pr["head"]["sha"]]
    rig.api.check(synthetic)
    await rig.pr_event(1)
    assert rig.entry(1)["ci"] == "pending"
    assert not rig.api.merges
    rig.api.parents[synthetic] = [pr["base"]["sha"], pr["head"]["sha"]]
    await rig.event("check_suite", {"action": "completed", "check_suite": {"head_sha": synthetic}})
    assert rig.api.merges == [1]


async def test_legacy_status_context_uses_latest_attempt(rig):
    rig.api.protection = {"required_status_checks": {"contexts": ["ci"]}}
    sha = rig.api.add_pr(1)["head"]["sha"]
    rig.api.statuses[sha] = [
        {"id": 1, "context": "ci", "state": "failure"},
        {"id": 2, "context": "ci", "state": "pending"},
    ]
    await rig.pr_event(1)
    assert rig.entry(1)["ci"] == "pending"
    rig.api.statuses[sha].append({"id": 3, "context": "ci", "state": "success"})
    await rig.event("status", {"sha": sha, "state": "success"})
    assert rig.api.merges == [1]


async def test_check_runs_paginate_and_latest_attempt_wins(rig):
    sha = rig.api.add_pr(1)["head"]["sha"]
    for i in range(100):
        rig.api.check(sha, name=f"unrequired-{i}")
    rig.api.check(sha, "failure")
    rig.api.check(sha, None)
    await rig.pr_event(1)
    assert rig.entry(1)["ci"] == "pending"
    await rig.complete(1, "neutral")
    assert rig.api.merges == [1]


@pytest.mark.parametrize("invalid", ["fork", "base"])
async def test_unsupported_pr_gets_usererror_label(rig, invalid):
    if invalid == "base":
        rig.api.remote.commit("other", {"other.txt": "other\n"})
    rig.api.add_pr(1, base="other" if invalid == "base" else "main")
    if invalid == "fork":
        rig.api.prs[1]["head_repo"] = "someone/fork"
    await rig.pr_event(1)
    assert rig.entry(1)["phase"] == "failed"
    assert rig.api.prs[1]["labels"] == [{"name": "chute-usererror"}]
    assert not rig.api.merges and not rig.git.pushes
