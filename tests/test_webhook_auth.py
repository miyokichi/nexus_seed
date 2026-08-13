"""AT8 (spec §58): a delivery that cannot prove itself gets nowhere.

An ingress endpoint is a way for the outside world to make NEXUS SEED believe
things and, downstream, act on them.  Rejecting an unauthenticated caller has
to happen *before* the envelope is built, so a forged body never reaches the
receipt table at all.
"""

from __future__ import annotations

from ingress_helpers import ingress

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.runtime.runtime import Runtime
from test_webhook_adapter import delivery, post_json

TOKEN = "test-shared-secret"


def hook(runtime, *, token=TOKEN) -> WebhookIngress:
    return WebhookIngress(ingress(runtime), token=token)


async def test_a_wrong_token_is_rejected_with_no_trace(tmp_path):
    runtime = Runtime(tmp_path / "auth.db")
    webhook = hook(runtime)

    response = await webhook.handle("webhook", delivery(), token="wrong-secret")

    assert response.status_code == 401
    assert response.body == {"error": "unauthorized"}
    assert runtime.event_store.all() == []
    assert runtime.get_ingress_receipts() == []
    runtime.close()


async def test_a_missing_token_is_rejected(tmp_path):
    runtime = Runtime(tmp_path / "auth.db")
    response = await hook(runtime).handle("webhook", delivery(), token=None)
    assert response.status_code == 401
    assert runtime.event_store.all() == []
    runtime.close()


async def test_the_correct_token_is_accepted(tmp_path):
    runtime = Runtime(tmp_path / "auth.db")
    webhook = hook(runtime)
    response = await webhook.handle("webhook", delivery(), token=TOKEN)
    await webhook.drain_pending()
    assert response.status_code == 202
    assert len(runtime.event_store.all()) == 1
    runtime.close()


async def test_a_near_miss_token_is_rejected(tmp_path):
    """Comparison is exact and constant-time; prefixes do not pass."""
    runtime = Runtime(tmp_path / "auth.db")
    webhook = hook(runtime)
    for candidate in (TOKEN[:-1], TOKEN + "x", TOKEN.upper(), ""):
        assert not webhook.authorize(candidate)
    assert webhook.authorize(TOKEN)
    runtime.close()


async def test_running_without_a_token_is_allowed_but_warned(tmp_path, caplog):
    """A closed test fixture may skip auth — but it should never be quiet about it."""
    runtime = Runtime(tmp_path / "open.db")
    webhook = hook(runtime, token=None)

    with caplog.at_level("WARNING"):
        response = await webhook.handle("webhook", delivery(), token=None)
    await webhook.drain_pending()

    assert response.status_code == 202
    assert any("without a token" in record.message for record in caplog.records)
    runtime.close()


async def test_auth_is_enforced_over_a_real_socket(tmp_path):
    runtime = Runtime(tmp_path / "auth_http.db")
    server = await WebhookServer(hook(runtime)).start()
    try:
        status, body = await post_json(
            server.bound_port, "/ingress/github", delivery(), token="nope"
        )
        assert status == 401
        assert body["error"] == "unauthorized"

        status, _ = await post_json(
            server.bound_port, "/ingress/github", delivery(), token=None
        )
        assert status == 401
    finally:
        await server.stop()

    assert runtime.event_store.all() == []
    assert runtime.get_ingress_receipts() == []
    runtime.close()
