import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.concurrency import run_in_threadpool

from chute.controller import ACTIVE, Controller
from chute.store import Action, Event


def snapshot(controller):
    state = controller.store.load()
    entries = state["entries"]

    def present(entry):
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
                "title",
                "author",
            )
        }

    return {
        "repo": controller.api.repo,
        "base": controller.settings.target_branch,
        "error": state.get("error"),
        "ready": bool(state.get("last_reconciled_at")) and not state.get("error"),
        "last_reconciled_at": state.get("last_reconciled_at"),
        "last_event_handled_at": state.get("last_event_handled_at"),
        "reconciling": controller.running,
        "sweep_interval_seconds": controller.interval_seconds,
        "checks": [check[0] for check in (state.get("policy") or {}).get("checks", [])],
        "queue": [
            present(entries[str(n)])
            for n in state["order"]
            if entries[str(n)]["phase"] in {"queued", "merging"}
        ],
        "waiting": [present(e) for e in entries.values() if e["phase"] == "watching"],
        "recent": [
            present(e) for e in reversed(list(entries.values())) if e["phase"] not in ACTIVE
        ][:12],
        "landed": sum(e["phase"] == "merged" for e in entries.values()),
        "actions": [
            {
                "id": a.id,
                "number": a.pr_number,
                "message": a.message,
                "status": a.status,
                "at": a.created_at.isoformat(),
            }
            for a in controller.store.recent(Action)
            if a.action_type not in {"webhook_received", "refresh_pr"}
        ][:20],
    }


def create_app(settings):
    controller = Controller(settings)

    @asynccontextmanager
    async def lifespan(app):
        await controller.start()
        try:
            yield
        finally:
            await controller.stop()

    app = FastAPI(title=settings.app_name, redoc_url=None, lifespan=lifespan)
    app.state.controller = controller

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(
            Path(__file__).parent / "static/index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(response: Response):
        state = controller.store.load()
        if state.get("error") or not state.get("last_reconciled_at"):
            response.status_code = 503
            return {
                "status": "blocked",
                "reason": state.get("error") or "Initial reconciliation pending",
            }
        return {"status": "ready"}

    @app.get("/train")
    def train():
        state = controller.store.load()
        entries = state["entries"]
        queue = [
            entries[str(n)]
            for n in state["order"]
            if entries[str(n)]["phase"] in {"queued", "merging"}
        ]
        return {
            "head": queue[0]["number"] if queue else None,
            "length": len(queue),
            "queue": queue,
            "watching": [e for e in entries.values() if e["phase"] == "watching"],
            **{
                key: state.get(key)
                for key in ("error", "policy", "last_reconciled_at", "last_event_handled_at")
            },
        }

    @app.get("/prs")
    def prs():
        return {"pull_requests": list(controller.store.load()["entries"].values())}

    @app.get("/prs/{number}")
    def pr(number: int):
        entry = controller.store.load()["entries"].get(str(number))
        if entry is None:
            raise HTTPException(404, "Pull request not found")
        return {"pull_request": entry}

    @app.get("/events")
    def events():
        return {"events": [e.model_dump(mode="json") for e in controller.store.recent(Event)]}

    @app.get("/actions")
    def actions():
        return {"actions": [a.model_dump(mode="json") for a in controller.store.recent(Action)]}

    @app.post("/reconcile", status_code=202)
    async def reconcile(request: Request, x_chute_action: str | None = Header(default=None)):
        # Custom header prevents cross-origin forms; no CORS origins are enabled.
        if x_chute_action != "reconcile" or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Use the Chute dashboard to request reconciliation")
        controller.wake("manual")
        return {"status": "queued"}

    @app.post("/webhooks/github", status_code=202)
    async def webhook(
        request: Request,
        x_github_event: str = Header(...),
        x_github_delivery: str = Header(...),
        x_hub_signature_256: str | None = Header(default=None),
    ):
        secret = settings.github_webhook_secret
        if not secret:
            raise HTTPException(503, "GitHub webhook secret is not configured")
        body = await request.body()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, x_hub_signature_256 or ""):
            raise HTTPException(401, "Invalid signature")
        payload = json.loads(body)
        if payload.get("repository", {}).get("full_name") != controller.api.repo:
            return {"status": "ignored"}
        inserted = controller.store.accept(x_github_delivery, x_github_event, payload)
        controller.wake("webhook")
        return {"status": "accepted" if inserted else "duplicate"}

    @app.get("/stream", response_class=EventSourceResponse, include_in_schema=False)
    async def stream(request: Request):
        yield ServerSentEvent(retry=3000)
        previous = None
        ticks = 0
        while not await request.is_disconnected():
            try:
                current = await run_in_threadpool(snapshot, controller)
            except Exception:  # noqa: BLE001 - keep stream alive after snapshot failures
                yield ServerSentEvent(event="unavailable", data={})
            else:
                if current != previous:
                    yield ServerSentEvent(data=current)
                    previous = current
                elif ticks % 8 == 0:
                    # Named heartbeat also refreshes the dashboard's stale-state indicator.
                    yield ServerSentEvent(event="heartbeat", data={})
            ticks += 1
            await asyncio.sleep(2)

    return app
