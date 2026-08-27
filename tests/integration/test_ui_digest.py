"""UI tests for the 全文 / 要約 toggle on the thread detail page.

Three of these are regression tests for traps that are invisible on a single
page load and only show up 7 seconds later, when HTMX replaces
``#messages``'s innerHTML:

- the toggle must live *outside* the swap target, or it disappears;
- ``#messages``'s own ``hx-get`` must carry ``digest=1``, or the poll reverts
  the view;
- the page render and the fragment render must agree, or the view changes
  shape on the first tick.
"""

from __future__ import annotations

from httpx import AsyncClient

PROJECT = "ui-digest"

DIGEST_TEXT = "Bohr が X 方式を提案、Heisenberg が実装。Einstein が Y を指摘。"


async def _open(client: AsyncClient, thread_id: str) -> str:
    r = await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": thread_id,
            "title": "digest ui",
            "owner": "tester",
            "propose_content": "kickoff body",
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["msg"]["msg_id"])


async def _post(client: AsyncClient, thread_id: str) -> str:
    r = await client.post(
        f"/v1/projects/{PROJECT}/threads/{thread_id}/messages",
        json={"type": "report", "author": "bob", "content": "後続メッセージ"},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["msg"]["msg_id"])


async def _store_digest(
    client: AsyncClient, thread_id: str, source_last_msg_id: str, **extra: object
) -> None:
    r = await client.put(
        f"/v1/projects/{PROJECT}/threads/{thread_id}/digest",
        json={
            "digest": DIGEST_TEXT,
            "source_last_msg_id": source_last_msg_id,
            "source_msg_count": 1,
            "producer": "magickit-digest-sweeper",
            "model": "Qwen3-32B",
            "tier": "light",
            **extra,
        },
    )
    assert r.status_code == 200, r.text


async def _page(client: AsyncClient, thread_id: str, **params: object) -> str:
    r = await client.get(
        f"/ui/projects/{PROJECT}/threads/{thread_id}", params=params
    )
    assert r.status_code == 200, r.text
    return r.text


async def _fragment(client: AsyncClient, thread_id: str, **params: object) -> str:
    r = await client.get(
        f"/ui/projects/{PROJECT}/threads/{thread_id}/_messages", params=params
    )
    assert r.status_code == 200, r.text
    return r.text


# ---- the toggle ---------------------------------------------------------


async def test_the_page_offers_both_views(client: AsyncClient) -> None:
    await _open(client, "T-1")

    body = await _page(client, "T-1")

    assert "全文表示" in body
    assert "要約表示" in body


async def test_the_toggle_is_not_inside_the_swap_target(client: AsyncClient) -> None:
    """It lives in the header card, outside ``#messages``.

    An ``innerHTML`` swap of ``#messages`` every 7 seconds would otherwise
    delete the control that got you there.
    """
    await _open(client, "T-1")

    page = await _page(client, "T-1")
    fragment = await _fragment(client, "T-1")

    assert "要約表示" in page
    assert "要約表示" not in fragment


async def test_digest_view_puts_digest_in_the_poll_url(client: AsyncClient) -> None:
    """The container carries its own ``hx-get``.

    ``hx-swap="innerHTML"`` leaves that URL in place, so if it lacked
    ``digest=1`` the next 7-second tick would silently swap the full message
    list back in under the 要約 heading.
    """
    await _open(client, "T-1")

    plain = await _page(client, "T-1")
    digested = await _page(client, "T-1", digest="1")

    assert "_messages?mode=full" in plain
    assert "digest=1" not in plain.split('id="messages"')[1].split(">")[0]
    assert "digest=1" in digested.split('id="messages"')[1].split(">")[0]


async def test_the_mode_links_preserve_the_digest_view(client: AsyncClient) -> None:
    """`mode` and the view are orthogonal, so neither link may drop the other."""
    await _open(client, "T-1")

    body = await _page(client, "T-1", digest="1")

    assert "?mode=full&amp;digest=1" in body
    assert "?mode=summary&amp;digest=1" in body


# ---- what the panel says -----------------------------------------------


async def test_not_generated_yet_says_so(client: AsyncClient) -> None:
    await _open(client, "T-1")

    body = await _page(client, "T-1", digest="1")

    assert "要約はまだ生成されていません" in body
    # And it says whose job generating is, since Conclair cannot do it.
    assert "Magickit" in body


async def test_a_digest_at_the_head_reports_reflected(client: AsyncClient) -> None:
    head = await _open(client, "T-1")
    await _store_digest(client, "T-1", head)

    body = await _page(client, "T-1", digest="1")

    assert DIGEST_TEXT in body
    assert "反映済み" in body
    assert "未反映" not in body
    # Provenance is visible: "why does this read badly" is answerable.
    assert "Qwen3-32B" in body
    assert "magickit-digest-sweeper" in body


async def test_a_stale_digest_names_the_shortfall(client: AsyncClient) -> None:
    head = await _open(client, "T-1")
    await _store_digest(client, "T-1", head)
    await _post(client, "T-1")
    await _post(client, "T-1")

    body = await _page(client, "T-1", digest="1")

    assert head in body
    assert "以降 2 件は未反映" in body
    assert "古い可能性があります" in body
    # The panel states the fact and stops. Whether re-generation is possible
    # depends on the entry point, and the control above already says so per
    # path -- claiming it here would contradict the button when it is drawn.
    assert "指示できません" not in body


async def test_a_truncated_digest_is_labelled(client: AsyncClient) -> None:
    head = await _open(client, "T-1")
    await _store_digest(client, "T-1", head, truncated=True)

    body = await _page(client, "T-1", digest="1")

    assert "中略あり" in body


async def test_full_view_shows_messages_not_the_digest(client: AsyncClient) -> None:
    head = await _open(client, "T-1")
    await _store_digest(client, "T-1", head)

    body = await _page(client, "T-1")

    assert "kickoff body" in body
    assert DIGEST_TEXT not in body


async def test_digest_view_replaces_the_message_list(client: AsyncClient) -> None:
    head = await _open(client, "T-1")
    await _store_digest(client, "T-1", head)

    body = await _page(client, "T-1", digest="1")

    assert DIGEST_TEXT in body
    assert "kickoff body" not in body


# ---- page and fragment must agree --------------------------------------


async def test_page_and_fragment_render_the_same_staleness_sentence(
    client: AsyncClient,
) -> None:
    """The fragment used to be handed a context missing ``mode``.

    A partial that branches on a key only one path supplies renders correctly
    on load and differently on every poll. Both paths now go through
    ``_messages_ctx``.
    """
    head = await _open(client, "T-1")
    await _store_digest(client, "T-1", head)
    await _post(client, "T-1")

    page = await _page(client, "T-1", digest="1")
    fragment = await _fragment(client, "T-1", digest="1")

    sentence = "以降 1 件は未反映"
    assert sentence in page
    assert sentence in fragment
    assert DIGEST_TEXT in fragment


async def test_the_fragment_is_a_fragment(client: AsyncClient) -> None:
    head = await _open(client, "T-1")
    await _store_digest(client, "T-1", head)

    fragment = await _fragment(client, "T-1", digest="1")

    assert "<!DOCTYPE html>" not in fragment


async def test_the_fragment_without_digest_still_lists_messages(
    client: AsyncClient,
) -> None:
    head = await _open(client, "T-1")
    await _store_digest(client, "T-1", head)

    fragment = await _fragment(client, "T-1")

    assert "kickoff body" in fragment
    assert DIGEST_TEXT not in fragment


# ---- the header count --------------------------------------------------


async def test_the_message_count_is_the_rollup_not_the_filtered_list(
    client: AsyncClient,
) -> None:
    """Under ``mode=summary`` the rendered list is one row; the thread is not."""
    await _open(client, "T-1")
    await _post(client, "T-1")
    close = await client.post(
        f"/v1/projects/{PROJECT}/threads/T-1/close",
        json={"author": "tester", "summary_content": "決定"},
    )
    # 201: close_thread creates a decide msg (api/threads.py sets
    # HTTP_201_CREATED), even though it also mutates the thread.
    assert close.status_code == 201, close.text

    body = await _page(client, "T-1", mode="summary")

    assert "(3)" in body
    assert "(1)" not in body


# ---- the generate button, and where it is honest about not working -----


async def test_the_button_appears_only_via_magickit(client: AsyncClient) -> None:
    """The POST is Magickit's route; on :8115 direct it would 404.

    A rendered button that 404s is the worst failure shape available here --
    it reads as a bug in the page rather than as the wrong entry point -- so
    Conclair draws it only when the proxy's header says the request came
    through Magickit.
    """
    await _open(client, "T-1")

    direct = await client.get(
        f"/ui/projects/{PROJECT}/threads/T-1", params={"digest": "1"}
    )
    proxied = await client.get(
        f"/ui/projects/{PROJECT}/threads/T-1",
        params={"digest": "1"},
        headers={"X-Spirrow-Via": "magickit"},
    )

    assert "要約を生成" not in direct.text
    # Instead of a dead button, the reason.
    assert "Magickit 経由" in direct.text
    assert "要約を生成" in proxied.text


async def test_the_button_posts_to_the_route_magickit_claims(
    client: AsyncClient,
) -> None:
    await _open(client, "T-1")

    body = await _page_via(client, "T-1")

    assert f"/ui/projects/{PROJECT}/threads/T-1/digest" in body


async def test_the_button_and_its_flash_are_outside_the_swap_target(
    client: AsyncClient,
) -> None:
    """Otherwise the 7-second poll deletes the flash that explains a refusal."""
    await _open(client, "T-1")

    page = await _page_via(client, "T-1")
    fragment = await _fragment(client, "T-1", digest="1")

    assert 'id="flash-digest"' in page
    assert 'id="flash-digest"' not in fragment
    assert "要約を生成" not in fragment


async def test_the_container_listens_for_the_digest_event(
    client: AsyncClient,
) -> None:
    """Magickit answers a successful generate with HX-Trigger: digestGenerated."""
    await _open(client, "T-1")

    body = await _page(client, "T-1", digest="1")

    assert "digestGenerated from:body" in body


async def test_the_button_is_absent_in_the_full_view(client: AsyncClient) -> None:
    """Nothing to generate for a view that is not showing a digest."""
    await _open(client, "T-1")

    body = await _page_via(client, "T-1", digest=None)

    assert "要約を生成" not in body


async def _page_via(
    client: AsyncClient, thread_id: str, *, digest: str | None = "1"
) -> str:
    params = {"digest": digest} if digest else {}
    r = await client.get(
        f"/ui/projects/{PROJECT}/threads/{thread_id}",
        params=params,
        headers={"X-Spirrow-Via": "magickit"},
    )
    assert r.status_code == 200, r.text
    return r.text
