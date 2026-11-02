import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from chute.api.deps import get_container

router = APIRouter(tags=["dashboard"])
PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"


@router.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(PAGE, headers={"Cache-Control": "no-cache"})


def snapshot(container) -> dict:
    journal = container.reconciler.store.load()
    records = {pr.number: pr for pr in container.pr_repo.list_all()}

    def present(entry):
        pr = records.get(entry["number"])
        return {
            key: entry.get(key)
            for key in (
                "number",
                "phase",
                "branch",
                "head_sha",
                "target_ref",
                "reason",
                "ci",
                "queued_at",
            )
        } | {"title": pr.title if pr else entry["branch"], "author": pr.author if pr else None}

    entries = journal.get("entries", {})
    active = {"queued", "merging", "watching", "ejecting"}
    return {
        "repo": container.github_client.repository.full_name,
        "base": container.settings.target_branch,
        "error": journal.get("error"),
        "ready": bool(journal.get("last_reconciled_at")),
        "last_reconciled_at": journal.get("last_reconciled_at"),
        "checks": [check[0] for check in (journal.get("policy") or {}).get("checks", [])],
        "queue": [
            present(entries[str(n)])
            for n in journal.get("order", [])
            if entries[str(n)]["phase"] in {"queued", "merging"}
        ],
        "waiting": [present(e) for e in entries.values() if e["phase"] == "watching"],
        "recent": [
            present(e) for e in reversed(list(entries.values())) if e["phase"] not in active
        ][:12],
        "landed": sum(e["phase"] == "merged" for e in entries.values()),
        "actions": [
            {
                "id": a.id,
                "number": a.pr_number,
                "message": a.message,
                "status": a.status.value,
                "at": a.created_at.isoformat() + ("Z" if a.created_at.tzinfo is None else ""),
            }
            for a in container.action_repo.list_recent(limit=50)
            if a.action_type.value not in {"webhook_received", "refresh_pr"}
        ][:20],
    }


@router.get("/stream", include_in_schema=False)
async def stream(request: Request, container=Depends(get_container)):
    async def events():
        yield "retry: 3000\n\n"
        previous = None
        ticks = 0
        while not await request.is_disconnected():
            try:
                current = json.dumps(await run_in_threadpool(snapshot, container), sort_keys=True)
            except Exception:
                yield "event: unavailable\ndata: {}\n\n"
            else:
                if current != previous:
                    yield f"data: {current}\n\n"
                    previous = current
                elif ticks % 8 == 0:
                    yield "event: heartbeat\ndata: {}\n\n"
            ticks += 1
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )
