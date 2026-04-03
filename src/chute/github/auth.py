from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt


@dataclass(slots=True)
class GitHubAppCredentials:
    app_id: str
    installation_id: str
    private_key_pem: str


def build_app_jwt(credentials: GitHubAppCredentials) -> str:
    now = datetime.now(UTC)
    payload = {
        "iat": int((now - timedelta(seconds=30)).timestamp()),
        "exp": int((now + timedelta(minutes=9)).timestamp()),
        "iss": credentials.app_id,
    }
    return jwt.encode(payload, credentials.private_key_pem, algorithm="RS256")
