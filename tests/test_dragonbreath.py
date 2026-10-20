import json

import httpx
import pytest
import respx

from bambuddy_dragonbreath.dragonbreath import DragonBreathClient, DragonBreathError

URL = "http://db.local"


def state(rev=1, **kw):
    return {"state_revision": rev, "mode": "off", **kw}


@pytest.fixture
def client():
    return DragonBreathClient(URL, "secret", "bambuddy")


@respx.mock
async def test_power_on_sends_contract_and_returns_lease(client):
    respx.get(f"{URL}/api/v2/state").respond(json=state(rev=7))
    route = respx.post(f"{URL}/api/v2/command").respond(json={"ok": True, "lease_id": "a" * 32, "state": {}})
    assert await client.power_on(45) == "a" * 32
    req = route.calls.last.request
    body = json.loads(req.content)
    assert req.headers["X-DragonBreath-Auth"] == "secret"
    assert body["api_version"] == 2
    assert body["expected_revision"] == 7
    assert body["actor"] == {"kind": "web", "id": "bambuddy"}
    assert body["command"] == {"name": "power_on", "target_c": 45}
    assert len(body["request_id"]) == 32


@respx.mock
async def test_power_on_retries_once_on_revision_conflict(client):
    respx.get(f"{URL}/api/v2/state").mock(side_effect=[httpx.Response(200, json=state(1)), httpx.Response(200, json=state(2))])
    conflict = httpx.Response(409, json={"ok": False, "error": "revision_conflict"})
    ok = httpx.Response(200, json={"ok": True, "lease_id": "b" * 32})
    route = respx.post(f"{URL}/api/v2/command").mock(side_effect=[conflict, ok])
    assert await client.power_on(50) == "b" * 32
    assert json.loads(route.calls.last.request.content)["expected_revision"] == 2


@respx.mock
async def test_power_on_gives_up_after_second_conflict(client):
    respx.get(f"{URL}/api/v2/state").respond(json=state(1))
    respx.post(f"{URL}/api/v2/command").respond(409, json={"ok": False, "error": "revision_conflict"})
    with pytest.raises(DragonBreathError) as e:
        await client.power_on(50)
    assert e.value.code == "revision_conflict"


@respx.mock
async def test_fault_latched_error_carries_state(client):
    respx.get(f"{URL}/api/v2/state").respond(json=state(1))
    respx.post(f"{URL}/api/v2/command").respond(
        409, json={"ok": False, "error": "fault_latched", "state": {"safety": {"fault_latched": True}}}
    )
    with pytest.raises(DragonBreathError) as e:
        await client.power_on(50)
    assert e.value.code == "fault_latched"
    assert e.value.state["safety"]["fault_latched"]


@respx.mock
async def test_heartbeat_and_stale_lease(client):
    route = respx.post(f"{URL}/api/v2/heartbeat")
    route.respond(json={"ok": True, "expires_in_ms": 299000})
    assert await client.heartbeat("c" * 32) == 299000
    assert json.loads(route.calls.last.request.content) == {"api_version": 2, "lease_id": "c" * 32}
    route.respond(409, json={"ok": False, "error": "stale_lease"})
    with pytest.raises(DragonBreathError) as e:
        await client.heartbeat("c" * 32)
    assert e.value.code == "stale_lease"


@respx.mock
async def test_off_has_no_revision(client):
    route = respx.post(f"{URL}/api/v2/command").respond(json={"ok": True})
    await client.off()
    body = json.loads(route.calls.last.request.content)
    assert body["command"] == {"name": "off"}
    assert "expected_revision" not in body


async def test_dry_run_sends_nothing():
    c = DragonBreathClient(URL, "t", dry_run=True)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{URL}/api/v2/state").respond(json=state(1))
        cmd = mock.post(f"{URL}/api/v2/command")
        assert len(await c.power_on(40)) == 32
        await c.off()
        assert not cmd.called
