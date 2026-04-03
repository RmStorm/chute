from fastapi import APIRouter, Depends, HTTPException

from chute.api.deps import get_container

router = APIRouter(tags=["state"])


@router.get("/train")
async def train(container=Depends(get_container)) -> dict:
    queue = [row.model_dump(mode="json") for row in container.queue_repo.list_active()]
    return {
        "head": queue[0]["pr_number"] if queue else None,
        "length": len(queue),
        "queue": queue,
    }


@router.get("/prs")
async def list_prs(container=Depends(get_container)) -> dict:
    return {"pull_requests": [row.model_dump(mode="json") for row in container.pr_repo.list_all()]}


@router.get("/prs/{number}")
async def get_pr(number: int, container=Depends(get_container)) -> dict:
    pr = container.pr_repo.get(container.github_client.repository.full_name, number)
    if pr is None:
        raise HTTPException(status_code=404, detail="Pull request not found")
    return {"pull_request": pr.model_dump(mode="json")}


@router.get("/events")
async def list_events(container=Depends(get_container)) -> dict:
    return {"events": [row.model_dump(mode="json") for row in container.event_repo.list_recent()]}


@router.get("/actions")
async def list_actions(container=Depends(get_container)) -> dict:
    return {"actions": [row.model_dump(mode="json") for row in container.action_repo.list_recent()]}
