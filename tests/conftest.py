import discord.http
import pytest


@pytest.fixture(autouse=True)
def no_live_discord_requests(monkeypatch):
    attempted = []

    async def blocked(*args, **kwargs):
        attempted.append(True)
        raise RuntimeError(
            "Discord HTTP is disabled in the test suite; use a mock channel."
        )

    monkeypatch.setattr(discord.http.HTTPClient, "request", blocked)
    yield
    assert not attempted, (
        "A test attempted a real Discord HTTP request instead of using its mock."
    )
