import asyncio
import json

from chute.store import Event


async def test_webhook_authentication_and_repository_isolation(rig):
    headers = {"X-GitHub-Event": "ping", "X-GitHub-Delivery": "unsigned"}
    response = await rig.client.post("/webhooks/github", json={}, headers=headers)
    assert response.status_code == 401
    rig.settings.github_webhook_secret = None
    response = await rig.client.post("/webhooks/github", json={}, headers=headers)
    assert response.status_code == 503
    rig.settings.github_webhook_secret = "test-webhook-secret"
    assert await rig.event("ping", {"repository": {"full_name": "another/repo"}}) == {
        "status": "ignored"
    }
    assert not rig.controller.store.recent(Event)


async def test_duplicate_delivery_is_durable_and_irrelevant_ci_does_no_work(rig):
    rig.api.add_pr(1)
    rig.api.prs[1]["labels"] = [{"name": "automerge"}]
    payload = {"action": "labeled", "pull_request": rig.api.pull(1)}
    assert await rig.event("pull_request", payload, delivery="one") == {"status": "accepted"}
    await rig.restart()
    rig.api.requests.clear()
    assert await rig.event("pull_request", payload, delivery="one") == {"status": "duplicate"}
    assert not rig.api.requests
    events = rig.controller.store.recent(Event)
    assert len(events) == 1 and events[0].processed_at is not None
    await rig.event("check_run", {"action": "completed", "check_run": {"head_sha": "unrelated"}})
    assert not rig.api.requests
    assert rig.state()["order"] == [1]


async def test_read_endpoints_and_manual_reconcile_guard(rig):
    rig.api.add_pr(1)
    await rig.pr_event(1)
    assert (await rig.client.get("/")).status_code == 200
    assert (await rig.client.get("/healthz")).json() == {"status": "ok"}
    assert (await rig.client.get("/readyz")).json() == {"status": "ready"}
    assert (await rig.client.get("/train")).json()["head"] == 1
    assert len((await rig.client.get("/prs")).json()["pull_requests"]) == 1
    assert (await rig.client.get("/prs/1")).json()["pull_request"]["phase"] == "queued"
    assert (await rig.client.get("/prs/404")).status_code == 404
    assert (await rig.client.get("/actions")).json()["actions"]
    assert (await rig.client.get("/events")).json()["events"]
    assert (await rig.client.post("/reconcile")).status_code == 403
    assert (
        await rig.client.post(
            "/reconcile", headers={"X-Chute-Action": "reconcile", "Sec-Fetch-Site": "cross-site"}
        )
    ).status_code == 403


async def test_dashboard_assets_are_served_without_exposing_other_files(rig):
    page = await rig.client.get("/")
    assert 'href="/favicon.ico"' in page.text
    icon = await rig.client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"
    assert icon.content.startswith(b"\x89PNG\r\n\x1a\n")
    for name in ("tailwind-3.4.17.js", "alpinejs-3.14.9.js"):
        response = await rig.client.get(f"/static/{name}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert response.content
    for name in ("index.html", ".env", "unknown.js"):
        assert (await rig.client.get(f"/static/{name}")).status_code == 404


async def test_sse_stream_publishes_initial_and_changed_journal(rig):
    # Drive ASGI directly: HTTPX's in-process transport buffers infinite streams.
    incoming, snapshots = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b""})
    buffer = ""

    async def send(message):
        nonlocal buffer
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        elif message["type"] == "http.response.body":
            buffer += message.get("body", b"").decode().replace("\r\n", "\n")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                for line in event.splitlines():
                    if line.startswith("data: ") and "event:" not in event:
                        await snapshots.put(json.loads(line[6:]))

    task = asyncio.create_task(
        rig.app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/stream",
                "query_string": b"",
                "headers": [],
                "client": ("test", 1),
                "server": ("test", 80),
            },
            incoming.get,
            send,
        )
    )
    try:
        initial = await asyncio.wait_for(snapshots.get(), 5)
        assert initial["ready"] and initial["queue"] == []
        rig.api.add_pr(1)
        await rig.pr_event(1)
        changed = await asyncio.wait_for(snapshots.get(), 5)
        assert changed["queue"][0]["number"] == 1
        assert changed["checks"] == ["ci"]
    finally:
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 5)
