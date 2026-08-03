"""End-to-end tests for the loop control endpoints (HOLD / RESUME)."""

from __future__ import annotations

from httpx import AsyncClient

PROJECT = "ctl-proj"


async def _get(client: AsyncClient, project: str = PROJECT) -> tuple[int, dict]:
    r = await client.get(f"/v1/projects/{project}/control")
    return r.status_code, r.json()


async def _put(client: AsyncClient, project: str = PROJECT, **body) -> tuple[int, dict]:
    r = await client.put(f"/v1/projects/{project}/control", json=body)
    return r.status_code, r.json()


async def _observed(
    client: AsyncClient, project: str = PROJECT, **body
) -> tuple[int, dict]:
    r = await client.post(f"/v1/projects/{project}/control/observed", json=body)
    return r.status_code, r.json()


async def _history(
    client: AsyncClient, project: str = PROJECT, **params
) -> tuple[int, dict]:
    r = await client.get(f"/v1/projects/{project}/control/history", params=params)
    return r.status_code, r.json()


# --- unconfigured projects -----------------------------------------------


async def test_unset_project_returns_run_default_not_404(client: AsyncClient) -> None:
    """A project nobody configured is `run`, reported as 200.

    404 here would be read by the loop as "the read failed", which INV-2
    turns into `hold` — every project would stop.
    """
    code, body = await _get(client, "never-touched")

    assert code == 200, body
    assert body["configured"] is False
    assert body["desired_state"] == "run"
    assert body["desired_actor"] is None
    assert body["desired_at"] is None
    assert body["observed_state"] is None


async def test_observed_on_unconfigured_project_keeps_it_unconfigured(
    client: AsyncClient,
) -> None:
    """The loop reporting observed must not look like someone set a state."""
    code, body = await _observed(
        client, "never-touched", state="run", actor="mindwire-conductor"
    )

    assert code == 200, body
    assert body["configured"] is False
    assert body["desired_state"] == "run"
    assert body["desired_actor"] is None
    assert body["observed_state"] == "run"
    assert body["observed_actor"] == "mindwire-conductor"


# --- PUT (desired) --------------------------------------------------------


async def test_put_sets_desired_and_marks_configured(client: AsyncClient) -> None:
    code, body = await _put(client, state="hold", actor="human", note="出先で止めた")

    assert code == 200, body
    assert body["configured"] is True
    assert body["desired_state"] == "hold"
    assert body["desired_actor"] == "human"
    assert body["desired_at"] is not None

    # And it latches: a plain GET returns the same value.
    code, body = await _get(client)
    assert code == 200, body
    assert body["desired_state"] == "hold"


async def test_put_rejects_unknown_state_with_422(client: AsyncClient) -> None:
    code, body = await _put(client, state="paused", actor="human")
    assert code == 422, body

    # And the rejected value did not land.
    _, state = await _get(client)
    assert state["configured"] is False


async def test_put_rejects_empty_actor(client: AsyncClient) -> None:
    code, body = await _put(client, state="hold", actor="")
    assert code == 422, body


# --- INV-4: the two writers stay out of each other's columns -------------


async def test_observed_does_not_change_desired(client: AsyncClient) -> None:
    """Regression test for INV-4.

    If a loop's observed report could move `desired`, a project someone
    stopped would silently resume — and the audit trail would show the
    human's HOLD as the last operator action while the value read `run`.
    """
    await _put(client, state="hold", actor="human")

    code, body = await _observed(client, state="run", actor="mindwire-conductor")

    assert code == 200, body
    assert body["desired_state"] == "hold"
    assert body["desired_actor"] == "human"
    assert body["observed_state"] == "run"
    assert body["observed_actor"] == "mindwire-conductor"

    # No history row either — observed reports are not operator actions.
    _, hist = await _history(client)
    assert [h["state"] for h in hist["items"]] == ["hold"]


async def test_put_does_not_clear_observed(client: AsyncClient) -> None:
    """The mirror image: setting a new desired must not erase what the
    loop last reported, or the UI could never show "反映待ち"."""
    await _observed(client, state="run", actor="mindwire-conductor")

    code, body = await _put(client, state="hold", actor="human")

    assert code == 200, body
    assert body["desired_state"] == "hold"
    assert body["observed_state"] == "run"
    assert body["observed_actor"] == "mindwire-conductor"


async def test_observed_rejects_unknown_state(client: AsyncClient) -> None:
    code, body = await _observed(client, state="paused", actor="mindwire-conductor")
    assert code == 422, body


# --- history --------------------------------------------------------------


async def test_put_appends_one_history_row(client: AsyncClient) -> None:
    await _put(client, state="hold", actor="human", note="n1")

    code, body = await _history(client)

    assert code == 200, body
    assert body["total"] == 1
    assert body["items"][0]["state"] == "hold"
    assert body["items"][0]["actor"] == "human"
    assert body["items"][0]["note"] == "n1"


async def test_same_value_put_still_appends_history(client: AsyncClient) -> None:
    """Pressing HOLD on an already-held project is still an operator
    action, and the log should say it happened."""
    await _put(client, state="hold", actor="human")
    await _put(client, state="hold", actor="takahito")

    code, body = await _history(client)

    assert code == 200, body
    assert body["total"] == 2
    # Newest first.
    assert body["items"][0]["actor"] == "takahito"
    assert body["items"][1]["actor"] == "human"


async def test_history_is_newest_first_and_limited(client: AsyncClient) -> None:
    for state in ("hold", "run", "supervised", "hold"):
        await _put(client, state=state, actor=f"a-{state}")

    code, body = await _history(client, limit=2)

    assert code == 200, body
    assert body["limit"] == 2
    assert body["total"] == 4  # total counts all rows, not the page
    assert [h["state"] for h in body["items"]] == ["hold", "supervised"]


async def test_history_is_project_scoped(client: AsyncClient) -> None:
    await _put(client, "proj-a", state="hold", actor="human")
    await _put(client, "proj-b", state="run", actor="human")

    _, body = await _history(client, "proj-a")

    assert body["total"] == 1
    assert body["items"][0]["state"] == "hold"


async def test_history_of_unknown_project_is_empty_not_404(client: AsyncClient) -> None:
    code, body = await _history(client, "never-touched")
    assert code == 200, body
    assert body["items"] == []
    assert body["total"] == 0
