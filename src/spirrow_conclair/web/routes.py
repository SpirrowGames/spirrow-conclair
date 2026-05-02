"""UI routes (Jinja2 + HTMX). Mounted under /ui in main.py.

Each page route awaits the corresponding /v1 API handler directly (same
process, same session) and renders a Jinja2 template. Fragment routes
return partial HTML for HTMX polling / swap targets.

`ChatroomError` raised by the underlying handler is converted to a flash
partial — list endpoints don't normally raise (audit returns 200, list
returns empty), so this matters mostly for `get_thread` not-found.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from spirrow_conclair import __version__
from spirrow_conclair.api.events import list_events as api_list_events
from spirrow_conclair.api.integrity import check_integrity as api_check_integrity
from spirrow_conclair.api.threads import (
    get_thread as api_get_thread,
    list_threads as api_list_threads,
)
from spirrow_conclair.db import SessionDep
from spirrow_conclair.exceptions import ChatroomError, ChatroomNotFoundError
from spirrow_conclair.schemas import ThreadStatus
from spirrow_conclair.web.deps import get_templates

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
) -> HTMLResponse:
    ctx = _base_ctx(project, "threads")
    ctx.update({"thread_id": thread_id, "mode": mode})

    try:
        view = await api_get_thread(
            project=project, thread_id=thread_id, session=session, mode=mode
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

    ctx["view"] = view
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
) -> HTMLResponse:
    try:
        view = await api_get_thread(
            project=project, thread_id=thread_id, session=session, mode=mode
        )
    except ChatroomNotFoundError as e:
        return _flash_response(request, e, status_code=404)

    return _render(
        request,
        "partials/message_list.html",
        {"project": project, "view": view},
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
    result = await api_check_integrity(project=project, session=session)
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
    result = await api_check_integrity(project=project, session=session)
    return _render(
        request,
        "partials/integrity_body.html",
        {"project": project, "result": result},
    )


# Re-exported so T17 can attach POST endpoints + reuse helpers.
__all__ = [
    "router",
    "_render",
    "_flash_response",
    "_base_ctx",
]
