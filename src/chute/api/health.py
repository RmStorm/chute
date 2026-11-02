from fastapi import APIRouter, Depends, Response

from chute.api.deps import get_container


router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response, container=Depends(get_container)) -> dict:
    state = container.reconciler.store.load()
    if state.get("error") or not state.get("last_reconciled_at"):
        response.status_code = 503
        return {
            "status": "blocked",
            "reason": state.get("error") or "Initial reconciliation pending",
        }
    return {"status": "ready"}
