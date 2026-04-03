from pathlib import Path

from chute.config import Settings
from chute.github.client import GitHubClient


def test_private_key_pem_is_loaded_from_path(tmp_path: Path) -> None:
    pem_path = tmp_path / "github-app.pem"
    pem_path.write_text("test-private-key", encoding="utf-8")

    client = GitHubClient(
        Settings(
            github_owner="acme",
            github_repo="monorepo",
            github_app_id="123",
            github_installation_id="456",
            github_private_key_path=pem_path,
        )
    )

    try:
        assert client.is_configured is True
        assert client._private_key_pem == "test-private-key"
    finally:
        import asyncio

        asyncio.run(client.close())
