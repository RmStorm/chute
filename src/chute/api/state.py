from fastapi import APIRouter, Depends, HTTPException

from chute.api.deps import get_container

router = APIRouter(tags=["state"])


@router.get("/train")
async def train(container=Depends(get_container)) -> dict:
    journal = container.reconciler.store.load()
    entries = journal.get("entries", {})
    queue = [
        entries[str(number)]
        for number in journal.get("order", [])
        if entries[str(number)]["phase"] in {"queued", "merging"}
    ]
    return {
        "head": queue[0]["number"] if queue else None,
        "length": len(queue),
        "queue": queue,
        "watching": [e for e in entries.values() if e["phase"] == "watching"],
        "error": journal.get("error"),
        "policy": journal.get("policy"),
        "last_reconciled_at": journal.get("last_reconciled_at"),
    }


@router.get("/prs")
async def list_prs(container=Depends(get_container)) -> dict:
    return {"pull_requests": [row.model_dump(mode="json") for row in container.pr_repo.list_all()]}


@router.get("/prs/{number}")
async def get_pr(number: int, container=Depends(get_container)) -> dict:
    pr = container.pr_repo.get(container.github_client.repository.full_name, number)
    if pr is None:
        raise HTTPException(status_code=404, detail="Pull request not found")
    return {
        "pull_request": pr.model_dump(mode="json"),
        "train": container.reconciler.store.load().get("entries", {}).get(str(number)),
    }


@router.get("/events")
async def list_events(container=Depends(get_container)) -> dict:
    return {"events": [row.model_dump(mode="json") for row in container.event_repo.list_recent()]}


@router.get("/actions")
async def list_actions(container=Depends(get_container)) -> dict:
    return {"actions": [row.model_dump(mode="json") for row in container.action_repo.list_recent()]}
