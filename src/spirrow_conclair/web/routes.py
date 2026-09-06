"""UI routes (Jinja2 + HTMX). Mounted under /ui in main.py.

Each page route awaits the corresponding /v1 API handler directly (same
process, same session) and renders a Jinja2 template. Fragment routes
return partial HTML for HTMX polling / swap targets.

`ChatroomError` raised by the underlying handler is converted to a flash
partial — list endpoints don't normally raise (audit returns 200, list
returns empty), so this matters mostly for `get_thread` not-found.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Form, Path, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from spirrow_conclair import __version__
from spirrow_conclair.api.control import (
    get_control as api_get_control,
    get_control_history as api_get_control_history,
    set_control as api_set_control,
)
from spirrow_conclair.api.events import list_events as api_list_events
from spirrow_conclair.api.integrity import check_integrity as api_check_integrity
from spirrow_conclair.api.messages import post_message as api_post_message
from spirrow_conclair.api.threads import (
    close_thread as api_close_thread,
    get_thread as api_get_thread,
    list_threads as api_list_threads,
    open_thread as api_open_thread,
)
from spirrow_conclair.config import get_settings
from spirrow_conclair.db import SessionDep
from spirrow_conclair.exceptions import ChatroomError, ChatroomNotFoundError
from spirrow_conclair.schemas import (
    CloseThreadRequest,
    OpenThreadRequest,
    PostMessageRequest,
    SetControlRequest,
    ThreadStatus,
)
from spirrow_conclair.web.deps import get_templates
from spirrow_conclair.web.forms import parse_csv

router = APIRouter(prefix="/ui", tags=["ui"])

ProjectPath = Annotated[str, Path(min_length=1, max_length=200)]
ThreadIdPath = Annotated[str, Path(min_length=1, max_length=200)]


def _base_ctx(project: str | None, active_page: str) -> dict[str, Any]:
    """Common template context (navbar, version)."""
    return {
        "project": project,
        "active_page": active_page,
        "version": __version__,
    }


def _render(
    request: Request,
    name: str,
    extra: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    templates: Jinja2Templates = get_templates()
    return templates.TemplateResponse(
        request, name, extra or {}, status_code=status_code, headers=headers
    )


#: Header Magickit's ``/ui`` proxy sets on every forwarded request.
#:
#: The 要約生成 button posts to a route **Magickit** claims (it is the
#: producer; Cognilens and the GPU are on that side), so through a direct
#: :8115 tunnel that POST would 404. Conclair therefore renders the button
#: only when it can see the request came through Magickit. A config flag
#: here could not do it: the same process serves both paths at once.
#:
#: Spoofable, and that is fine -- it decides what to *render*, not what is
#: allowed, the same stance as ``actor`` in loop control.
VIA_HEADER = "X-Spirrow-Via"
VIA_MAGICKIT = "magickit"


def _via_magickit(request: Request) -> bool:
    return request.headers.get(VIA_HEADER, "").lower() == VIA_MAGICKIT


def _messages_ctx(
    project: str,
    thread_id: str,
    view: Any,
    *,
    mode: str,
    digest: bool,
) -> dict[str, Any]:
    """The context ``partials/message_list.html`` needs, built once.

    Both render paths go through here — the page's ``{% include %}`` and the
    7-second fragment poll. They used to build their own dicts, and the
    fragment's omitted ``mode`` and ``thread_id``; that was harmless only
    while the partial referenced neither. A partial that branches on a key
    one path does not supply renders correctly on load and then differently
    every 7 seconds, which is a hard bug to see and an easy one to
    reintroduce.
    """
    return {
        "project": project,
        "thread_id": thread_id,
        "view": view,
        "mode": mode,
        "digest": digest,
        # Named separately from `digest` (the requested view) so the partial
        # reads as "which view am I rendering" rather than re-deriving it.
        "digest_view": digest,
    }


def _flash_response(
    request: Request,
    err: ChatroomError,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a flash partial for HTMX swap targets (T17 will reuse this)."""
    return _render(
        request,
        "partials/flash.html",
        {
            "error_type": type(err).__name__,
            "error": err.message,
            "details": err.details,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, summary="Landing page (project picker)")
async def landing(request: Request) -> HTMLResponse:
    return _render(request, "landing.html", _base_ctx(None, "landing"))


# ---------------------------------------------------------------------------
# Loop control (HOLD / RESUME)
# ---------------------------------------------------------------------------
#
# The widget lives at the top of the thread list page: that route already
# carries the project scope, and it is the screen a human is on when they
# decide to stop something.
#
# One template renders the widget for all three entry points (page
# include, 7s poll, button post), so "what the widget looks like" has a
# single definition. Errors render *inside* it rather than replacing it —
# swapping a flash partial over the widget would take the buttons and the
# poll trigger off the page, leaving no way to retry.

#: How long observed_at may go unrefreshed before the widget says the
#: loop might be down. Long enough to sit through an implementation turn,
#: short enough to notice a sweep that never started.
CONTROL_STALE_MINUTES = 15


async def _control_ctx(
    project: str,
    session: SessionDep,
    *,
    flash_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    control = await api_get_control(project=project, session=session)
    history = await api_get_control_history(project=project, session=session, limit=3)

    # "Pending" only makes sense against a setting someone actually made.
    # An unconfigured project is running on the default; there is nothing
    # for the loop to catch up to, even though observed_state is null.
    diverged = control.configured and control.observed_state != control.desired_state

    stale = False
    if control.observed_at is not None:
        observed_at = control.observed_at
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - observed_at).total_seconds()
        stale = age > CONTROL_STALE_MINUTES * 60

    return {
        "project": project,
        "control": control,
        "history": history,
        "diverged": diverged,
        "stale": stale,
        "stale_minutes": CONTROL_STALE_MINUTES,
        "flash_error": flash_error,
    }


@router.get(
    "/projects/{project}/control/_widget",
    response_class=HTMLResponse,
    summary="Loop control widget partial (HTMX polling target)",
)
async def control_widget_fragment(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
) -> HTMLResponse:
    return _render(
        request,
        "partials/control_widget.html",
        await _control_ctx(project, session),
    )


@router.post(
    "/projects/{project}/control",
    response_class=HTMLResponse,
    summary="Set the desired loop control state (form post)",
)
async def control_set(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
    state: Annotated[str, Form()],
    # conclair.js injects the navbar's author into every HTMX request as
    # `author`, so the widget needs no field of its own. It lands in
    # `actor` because that is what the record is: who says they did this.
    author: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        body = SetControlRequest(state=state, actor=author)  # type: ignore[arg-type]
    except ValidationError:
        # Both failure modes are the operator's, and both are fixable
        # from this screen, so they get one sentence rather than a
        # pydantic dump: an unknown state can only come from a tampered
        # request, and a blank actor means the navbar field is empty.
        detail = (
            f"未知の state '{state}' です。"
            if state not in ("run", "supervised", "hold")
            else "author を入れてください (navbar 右上)。"
        )
        ctx = await _control_ctx(
            project,
            session,
            flash_error={"error_type": "ValidationError", "error": detail},
        )
        return _render(request, "partials/control_widget.html", ctx)

    try:
        await api_set_control(project=project, body=body, session=session)
    except ChatroomError as e:
        ctx = await _control_ctx(
            project,
            session,
            flash_error={"error_type": type(e).__name__, "error": e.message},
        )
        return _render(request, "partials/control_widget.html", ctx)

    return _render(
        request,
        "partials/control_widget.html",
        await _control_ctx(project, session),
    )


# ---------------------------------------------------------------------------
# Threads — list + detail
# ---------------------------------------------------------------------------


def _parse_status_filter(status_filter: list[str] | None) -> list[ThreadStatus] | None:
    """Strip blanks (HTML <select multiple> often submits empty options)."""
    if not status_filter:
        return None
    filtered = [s for s in status_filter if s]
    return filtered or None  # type: ignore[return-value]


@router.get(
    "/projects/{project}/threads",
    response_class=HTMLResponse,
    summary="Thread list page",
)
async def threads_page(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
    status: Annotated[list[str] | None, Query()] = None,
    owner: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    parsed_status = _parse_status_filter(status)
    result = await api_list_threads(
        project=project,
        session=session,
        status_filter=parsed_status,
        owner=owner or None,
        limit=limit,
        offset=offset,
    )
    ctx = _base_ctx(project, "threads")
    ctx.update(
        {
            "result": result,
            "filter_status": parsed_status or [],
            "filter_owner": owner or "",
            "filter_limit": limit,
            "filter_offset": offset,
        }
    )
    # The control widget is part of this page's first paint rather than a
    # post-load fetch: an operator arriving to stop something should see
    # the current state immediately, not a blank box for one poll cycle.
    ctx.update(await _control_ctx(project, session))
    return _render(request, "thread_list.html", ctx)


@router.get(
    "/projects/{project}/threads/_rows",
    response_class=HTMLResponse,
    summary="Thread list rows (HTMX polling target)",
)
async def threads_fragment(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
    status: Annotated[list[str] | None, Query()] = None,
    owner: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    result = await api_list_threads(
        project=project,
        session=session,
        status_filter=_parse_status_filter(status),
        owner=owner or None,
        limit=limit,
        offset=offset,
    )
    return _render(
        request,
        "partials/thread_rows.html",
        {"project": project, "result": result},
    )


@router.get(
    "/projects/{project}/threads/{thread_id}",
    response_class=HTMLResponse,
    summary="Thread detail page",
)
async def thread_detail_page(
    request: Request,
    project: ProjectPath,
    thread_id: ThreadIdPath,
    session: SessionDep,
    mode: Annotated[Literal["full", "summary"], Query()] = "full",
    digest: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    ctx = _base_ctx(project, "threads")
    ctx.update(
        {
            "thread_id": thread_id,
            "mode": mode,
            "digest": digest,
            "can_generate_digest": _via_magickit(request),
        }
    )

    try:
        view = await api_get_thread(
            project=project,
            thread_id=thread_id,
            session=session,
            mode=mode,
            include_digest=digest,
        )
    except ChatroomNotFoundError as e:
        ctx.update(
            {
                "view": None,
                "error_type": type(e).__name__,
                "error": e.message,
                "details": e.details,
            }
        )
        return _render(request, "thread_detail.html", ctx, status_code=404)

    ctx.update(_messages_ctx(project, thread_id, view, mode=mode, digest=digest))
    return _render(request, "thread_detail.html", ctx)


@router.get(
    "/projects/{project}/threads/{thread_id}/_messages",
    response_class=HTMLResponse,
    summary="Thread messages partial (HTMX polling target)",
)
async def thread_messages_fragment(
    request: Request,
    project: ProjectPath,
    thread_id: ThreadIdPath,
    session: SessionDep,
    mode: Annotated[Literal["full", "summary"], Query()] = "full",
    digest: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    try:
        view = await api_get_thread(
            project=project,
            thread_id=thread_id,
            session=session,
            mode=mode,
            include_digest=digest,
        )
    except ChatroomNotFoundError as e:
        return _flash_response(request, e, status_code=404)

    return _render(
        request,
        "partials/message_list.html",
        _messages_ctx(project, thread_id, view, mode=mode, digest=digest),
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


_EVENT_ACTIONS = ("open_thread", "post_message", "status_transition")


def _parse_action(action: str | None) -> str | None:
    if not action:
        return None
    if action not in _EVENT_ACTIONS:
        return None
    return action


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # accept both "Z" suffix and naive ISO; normalize to aware UTC handled
        # by handler.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get(
    "/projects/{project}/events",
    response_class=HTMLResponse,
    summary="Events timeline page",
)
async def events_page(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
    thread_id: Annotated[str | None, Query(max_length=200)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[str | None, Query(max_length=64)] = None,
    until: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    parsed_action = _parse_action(action)
    result = await api_list_events(
        project=project,
        session=session,
        thread_id=thread_id or None,
        action=parsed_action,  # type: ignore[arg-type]
        since=_parse_dt(since),
        until=_parse_dt(until),
        limit=limit,
        offset=offset,
    )
    ctx = _base_ctx(project, "events")
    ctx.update(
        {
            "result": result,
            "filter_thread_id": thread_id or "",
            "filter_action": parsed_action or "",
            "filter_since": since or "",
            "filter_until": until or "",
            "filter_limit": limit,
            "filter_offset": offset,
            "actions": _EVENT_ACTIONS,
        }
    )
    return _render(request, "events.html", ctx)


@router.get(
    "/projects/{project}/events/_rows",
    response_class=HTMLResponse,
    summary="Event rows partial (HTMX polling target)",
)
async def events_fragment(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
    thread_id: Annotated[str | None, Query(max_length=200)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[str | None, Query(max_length=64)] = None,
    until: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    result = await api_list_events(
        project=project,
        session=session,
        thread_id=thread_id or None,
        action=_parse_action(action),  # type: ignore[arg-type]
        since=_parse_dt(since),
        until=_parse_dt(until),
        limit=limit,
        offset=offset,
    )
    return _render(
        request,
        "partials/event_rows.html",
        {"project": project, "result": result},
    )


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project}/integrity",
    response_class=HTMLResponse,
    summary="Integrity audit page",
)
async def integrity_page(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
) -> HTMLResponse:
    result = await api_check_integrity(
        project=project, session=session, settings=get_settings()
    )
    ctx = _base_ctx(project, "integrity")
    ctx["result"] = result
    return _render(request, "integrity.html", ctx)


@router.get(
    "/projects/{project}/integrity/_body",
    response_class=HTMLResponse,
    summary="Integrity audit body partial (HTMX polling target)",
)
async def integrity_fragment(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
) -> HTMLResponse:
    result = await api_check_integrity(
        project=project, session=session, settings=get_settings()
    )
    return _render(
        request,
        "partials/integrity_body.html",
        {"project": project, "result": result},
    )


# ---------------------------------------------------------------------------
# Form posts — open / post / close
# ---------------------------------------------------------------------------
#
# Each POST endpoint:
#   1. parses Form(...) values, builds a pydantic Request schema
#   2. catches pydantic ValidationError → flash partial (200 OK)
#   3. awaits the corresponding /v1 handler in-process
#   4. catches ChatroomError → flash partial (200 OK)
#   5. returns success flash + an HX-* response header to drive the UI:
#       - HX-Redirect: full navigation (open_thread → detail)
#       - HX-Trigger: messagePosted → forces #messages partial re-fetch
#       - HX-Refresh: full reload (close → reflect resolved status)
#
# We always return status 200 + flash partial because HTMX swap behaviour on
# 4xx is server-defined and surfacing the error inline is more useful than
# letting the browser show a default error overlay.


def _validation_flash(request: Request, err: ValidationError) -> HTMLResponse:
    return _render(
        request,
        "partials/flash.html",
        {
            "error_type": "ValidationError",
            "error": "Form validation failed",
            "details": {"errors": err.errors()},
        },
    )


# Claims these handlers are structurally unable to honour. `role` is only
# meaningful once it has been checked against the identity's `allowed_roles`,
# and the two override flags only once the author has been confirmed human --
# all of which lives in Magickit, which owns identity. Conclair validates
# nothing by design.
_GATED_CLAIM_FIELDS = ("role", "owner_override_reason", "naysayer_override_reason")


def _gated_claim_flash(request: Request, supplied: list[str]) -> HTMLResponse:
    """Refuse a claim this path cannot validate, instead of dropping it.

    The forms carry these fields because the Magickit-served UI enforces them.
    Reaching Conclair directly (loopback :8115) bypasses that enforcement, so
    silently ignoring the field would be the worse failure: the post would
    succeed while `messages.role` stayed null, and the invariant "role is
    non-null <-> it passed allowed_roles validation" would look satisfied
    while the user believed they had declared one. Refusing says which door
    to use.
    """
    return _render(
        request,
        "partials/flash.html",
        {
            "error_type": "UngatedClaimRejected",
            "error": (
                "この経路 (conclair 直, :8115) は role / override を検証できません。"
                "Magickit 経由の UI から投稿してください。"
            ),
            "details": {"supplied_fields": supplied},
        },
    )


def _gated_claims(**fields: str) -> list[str]:
    """Names of the supplied claim fields, in declaration order."""
    return [name for name in _GATED_CLAIM_FIELDS if fields.get(name)]


@router.post(
    "/projects/{project}/threads",
    response_class=HTMLResponse,
    summary="Open a new thread (form post)",
)
async def threads_open(
    request: Request,
    project: ProjectPath,
    session: SessionDep,
    thread_id: Annotated[str, Form()],
    title: Annotated[str, Form()],
    owner: Annotated[str, Form()],
    propose_content: Annotated[str, Form()],
    tags: Annotated[str, Form()] = "",
    commit_ref: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
) -> HTMLResponse:
    supplied = _gated_claims(role=role)
    if supplied:
        return _gated_claim_flash(request, supplied)

    try:
        body = OpenThreadRequest(
            thread_id=thread_id,
            title=title,
            owner=owner,
            propose_content=propose_content,
            tags=parse_csv(tags),
            commit_ref=commit_ref or None,
        )
    except ValidationError as e:
        return _validation_flash(request, e)

    try:
        result = await api_open_thread(project=project, body=body, session=session)
    except ChatroomError as e:
        return _flash_response(request, e)

    target = f"/ui/projects/{quote(project, safe='')}/threads/{quote(result.thread.thread_id, safe='')}"
    return _render(
        request,
        "partials/flash.html",
        {"message": f"opened thread '{result.thread.thread_id}' — redirecting…"},
        headers={"HX-Redirect": target},
    )


@router.post(
    "/projects/{project}/threads/{thread_id}/messages",
    response_class=HTMLResponse,
    summary="Post a message in a thread (form post)",
)
async def messages_post(
    request: Request,
    project: ProjectPath,
    thread_id: ThreadIdPath,
    session: SessionDep,
    type: Annotated[str, Form()],
    author: Annotated[str, Form()],
    content: Annotated[str, Form()],
    reply_to: Annotated[str, Form()] = "",
    references_threads: Annotated[str, Form()] = "",
    related_tasks: Annotated[str, Form()] = "",
    closes_thread: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    commit_ref: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    owner_override_reason: Annotated[str, Form()] = "",
    naysayer_override_reason: Annotated[str, Form()] = "",
) -> HTMLResponse:
    supplied = _gated_claims(
        role=role,
        owner_override_reason=owner_override_reason,
        naysayer_override_reason=naysayer_override_reason,
    )
    if supplied:
        return _gated_claim_flash(request, supplied)

    try:
        body = PostMessageRequest(
            type=type,  # type: ignore[arg-type]
            author=author,
            content=content,
            reply_to=reply_to or None,
            references_threads=parse_csv(references_threads),
            related_tasks=parse_csv(related_tasks),
            closes_thread=closes_thread or None,
            tags=parse_csv(tags),
            commit_ref=commit_ref or None,
        )
    except ValidationError as e:
        return _validation_flash(request, e)

    try:
        result = await api_post_message(
            project=project, thread_id=thread_id, body=body, session=session
        )
    except ChatroomError as e:
        return _flash_response(request, e)

    msg = f"posted {result.msg.msg_id} ({result.msg.type})"
    if result.thread_status_changed_to:
        msg += f" — status → {result.thread_status_changed_to}"

    return _render(
        request,
        "partials/flash.html",
        {"message": msg},
        headers={"HX-Trigger": "messagePosted"},
    )


@router.post(
    "/projects/{project}/threads/{thread_id}/close",
    response_class=HTMLResponse,
    summary="Close a thread (owner-only, form post)",
)
async def threads_close(
    request: Request,
    project: ProjectPath,
    thread_id: ThreadIdPath,
    session: SessionDep,
    author: Annotated[str, Form()],
    summary_content: Annotated[str, Form()],
    affects_threads: Annotated[str, Form()] = "",
    related_tasks: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    commit_ref: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    owner_override_reason: Annotated[str, Form()] = "",
    naysayer_override_reason: Annotated[str, Form()] = "",
) -> HTMLResponse:
    supplied = _gated_claims(
        role=role,
        owner_override_reason=owner_override_reason,
        naysayer_override_reason=naysayer_override_reason,
    )
    if supplied:
        return _gated_claim_flash(request, supplied)

    try:
        body = CloseThreadRequest(
            author=author,
            summary_content=summary_content,
            affects_threads=parse_csv(affects_threads),
            related_tasks=parse_csv(related_tasks),
            tags=parse_csv(tags),
            commit_ref=commit_ref or None,
        )
    except ValidationError as e:
        return _validation_flash(request, e)

    try:
        await api_close_thread(
            project=project, thread_id=thread_id, body=body, session=session
        )
    except ChatroomError as e:
        return _flash_response(request, e)

    return _render(
        request,
        "partials/flash.html",
        {"message": f"closed thread '{thread_id}'"},
        headers={"HX-Refresh": "true"},
    )


# Re-exported so future modules can attach more endpoints + reuse helpers.
__all__ = [
    "router",
    "_render",
    "_flash_response",
    "_base_ctx",
]
