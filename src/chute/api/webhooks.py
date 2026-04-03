import hashlib
import hmac
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from chute.api.deps import get_container
from chute.core.enums import ActionStatus, ActionType
from chute.core.models import EventRecord
from chute.github.client import has_relevant_label


router = APIRouter(tags=["webhooks"])


def verify_signature(secret: str | None, body: bytes, signature_header: str | None) -> None:
    if not secret:
        return
    if not signature_header:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    response: Response,
    x_github_event: str = Header(...),
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str | None = Header(default=None),
    container=Depends(get_container),
) -> dict[str, str]:
    body = await request.body()
    verify_signature(container.settings.github_webhook_secret, body, x_hub_signature_256)

    payload = json.loads(body.decode("utf-8"))
    repo_full_name = payload.get("repository", {}).get("full_name")
    pr_number = payload.get("pull_request", {}).get("number")
    event_action = payload.get("action")
    received_at = datetime.now(UTC)

    container.event_repo.insert(
        EventRecord(
            delivery_id=x_github_delivery,
            event_type=x_github_event,
            action=event_action,
            repo=repo_full_name,
            pr_number=pr_number,
            payload=payload,
            received_at=received_at,
        )
    )

    labels = [label["name"] for label in payload.get("pull_request", {}).get("labels", [])]
    if repo_full_name and pr_number and (
        x_github_event != "pull_request" or has_relevant_label(labels) or event_action in {"closed", "unlabeled"}
    ):
        pr_payload = payload.get("pull_request", {})
        container.pr_repo.upsert_webhook_pull_request(
            repo=repo_full_name,
            payload=pr_payload,
            last_event_at=received_at,
        )

    container.action_repo.log(
        repo=repo_full_name or container.github_client.repository.full_name,
        pr_number=pr_number,
        action_type=ActionType.WEBHOOK_RECEIVED,
        status=ActionStatus.SUCCESS,
        message="Webhook accepted",
        metadata={"event_type": x_github_event, "action": event_action},
    )

    response.status_code = status.HTTP_202_ACCEPTED
    return {"status": "accepted"}
