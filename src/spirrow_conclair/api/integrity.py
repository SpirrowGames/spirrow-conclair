"""Integrity audit endpoint (report; never raises)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from spirrow_conclair.config import SettingsDep
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
    project: ProjectPath, session: SessionDep, settings: SettingsDep
) -> IntegrityCheckResponse:
    report = await integrity_svc.audit_project(
        session,
        project=project,
        sanction_recording_since=settings.sanction_recording_since,
    )
    return IntegrityCheckResponse(
        issues=report.issues,
        issue_count=len(report.issues),
        checked_at=integrity_svc.now_utc(),
        sanctioned_counts=report.sanctioned_counts,
        unattributable=report.unattributable,
        # Echoed back so a reader can see, in the report itself, whether the
        # `closes_thread_by_non_owner` bucket is armed at all.
        sanction_recording_since=settings.sanction_recording_since,
    )
