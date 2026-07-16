"""Autopilot A/B tests (Feature B): compare two SOP versions on simulated
customers through the real runtime. Runs as an in-process background task;
progress and results persist on the ab_tests row, so the UI can poll."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ABTest, Sop, SopVersion
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/abtests", tags=["abtests"])


class ABTestCreate(BaseModel):
    sop_id: str
    arm_a_version: int = 0  # 0 = newest published
    arm_b_version: int = 0  # 0 = newest version (draft or published)
    n_sessions: int = Field(4, ge=1, le=20, description="simulated sessions PER ARM")
    max_turns: int = Field(8, ge=2, le=16)
    name: str = ""


def _row(t: ABTest) -> dict:
    return {
        "id": t.id,
        "sop_id": t.sop_id,
        "name": t.name,
        "arm_a_version": t.arm_a_version,
        "arm_b_version": t.arm_b_version,
        "n_sessions": t.n_sessions,
        "max_turns": t.max_turns,
        "status": t.status,
        "progress": t.progress,
        "results": t.results,
        "error": t.error,
        "created_at": t.created_at.isoformat(),
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
    }


@router.post("")
async def start_abtest(
    req: ABTestCreate,
    request: Request,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    sop = (
        await db.execute(
            select(Sop).where(
                Sop.id == req.sop_id, Sop.tenant_id == scope.tenant_id, Sop.project_id == scope.project_id
            )
        )
    ).scalar_one_or_none()
    if sop is None:
        raise HTTPException(status_code=404, detail="SOP not found")

    versions = (
        (await db.execute(select(SopVersion).where(SopVersion.sop_id == sop.id).order_by(SopVersion.version)))
        .scalars()
        .all()
    )
    published = [v for v in versions if v.status == "published"]
    arm_a = req.arm_a_version or (published[-1].version if published else 0)
    arm_b = req.arm_b_version or versions[-1].version
    known = {v.version for v in versions}
    if arm_a not in known or arm_b not in known:
        raise HTTPException(status_code=404, detail=f"unknown SOP version (have: {sorted(known)})")
    if arm_a == arm_b:
        raise HTTPException(
            status_code=422,
            detail="both arms resolve to the same version — save a new draft first, or pass versions explicitly",
        )

    # the runner re-authenticates through the public API with the caller's own key
    auth = request.headers.get("authorization", "")
    bearer = auth.removeprefix("Bearer ").strip()
    if not bearer:
        raise HTTPException(status_code=401, detail="bearer key required")

    test = ABTest(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        sop_id=sop.id,
        name=req.name or f"{sop.name}: v{arm_a} vs v{arm_b}",
        arm_a_version=arm_a,
        arm_b_version=arm_b,
        n_sessions=req.n_sessions,
        max_turns=req.max_turns,
        progress={"completed": 0, "total": req.n_sessions * 2},
    )
    db.add(test)
    await db.commit()

    from ..abtest import run_abtest

    task = asyncio.create_task(run_abtest(test.id, bearer, request.headers.get("x-project", "")))
    tasks = getattr(request.app.state, "abtest_tasks", None)
    if tasks is None:
        tasks = request.app.state.abtest_tasks = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return _row(test)


@router.get("")
async def list_abtests(
    scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db), sop_id: str | None = None
) -> list[dict]:
    where = [ABTest.tenant_id == scope.tenant_id, ABTest.project_id == scope.project_id]
    if sop_id:
        where.append(ABTest.sop_id == sop_id)
    rows = (
        (await db.execute(select(ABTest).where(*where).order_by(ABTest.created_at.desc()).limit(20)))
        .scalars()
        .all()
    )
    return [_row(t) for t in rows]


@router.get("/{abtest_id}")
async def get_abtest(
    abtest_id: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    test = (
        await db.execute(
            select(ABTest).where(
                ABTest.id == abtest_id,
                ABTest.tenant_id == scope.tenant_id,
                ABTest.project_id == scope.project_id,
            )
        )
    ).scalar_one_or_none()
    if test is None:
        raise HTTPException(status_code=404, detail="A/B test not found")
    return _row(test)
