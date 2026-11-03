import json
import os
import socket

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from chute.config import Settings
from tests.harness import GitHubAPI, Harness, Remote


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not connect to the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    for name in list(os.environ):
        if name.startswith("CHUTE_"):
            monkeypatch.delenv(name)


@pytest.fixture
async def rig(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "app.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "chute.sqlite3",
        git_workdir=tmp_path / "train",
        github_owner="test",
        github_repo="train",
        github_api_url="https://api.github.invalid",
        github_app_id="1",
        github_installation_id="1",
        github_private_key_path=pem,
        github_webhook_secret="test-webhook-secret",
        reconcile_interval_seconds=3600,
    )
    api = GitHubAPI(Remote(tmp_path), key.public_key())
    harness = Harness(settings, api)
    try:
        await harness.start()
        yield harness
    finally:
        await harness.stop()
        (tmp_path / "requests.json").write_text(json.dumps(api.requests, indent=2))
        (tmp_path / "git-graph.txt").write_text(
            api.remote.git(api.remote.path, "log", "--all", "--graph", "--oneline")
        )
        assert not api.unexpected, api.unexpected
