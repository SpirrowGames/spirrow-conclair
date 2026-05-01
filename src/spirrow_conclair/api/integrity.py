"""Integrity audit endpoint (report; never raises)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from spirrow_conclair.db import SessionDep
from spirrow_conclair.schemas import IntegrityCheckResponse
from spirrow_conclair.services import integrity as integrity_svc

router = APIRouter(prefix="/v1/projects/{project}/integrity", tags=["integrity"])

ProjectPath = Annotated[str, Path(min_length=1, max_length=200)]


@router.get(
    "",
    response_model=IntegrityCheckResponse,
    summary="Audit chatroom invariants for a project (returns 200 with issue list)",
)
async def check_integrity(
    project: ProjectPath, session: SessionDep
) -> IntegrityCheckResponse:
    issues = await integrity_svc.audit_project(session, project=project)
    return IntegrityCheckResponse(
        issues=issues,
        issue_count=len(issues),
        checked_at=integrity_svc.now_utc(),
    )
