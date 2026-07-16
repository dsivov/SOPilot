"""D-10 connector resolution: systems vs bindings.

A SOP dependency may name a project connector instead of carrying connection
details inline:

    {"name": "patient_record", "kind": "mock",           # kind is overridden
     "config": {"connector": "emr", "top_k": 2}, ...}    # by the connector's

The connector row supplies HOW to reach the system (kind + config + secret
refs); the dependency keeps WHAT the stage needs and may override tuning keys
(its config wins on collision). Unknown or disabled connectors raise — the
fetch lands in the audit trail as a failed fetch with a readable error, and
the live path degrades exactly like any other fetch failure.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import Connector
from .schemas import DataDependency
from .tenancy import Scope

CONNECTOR_KINDS = ("mcp", "rag", "http", "mock")


async def resolve_dependency(
    sessionmaker: async_sessionmaker, scope: Scope, dep: DataDependency
) -> tuple[DataDependency, str]:
    """Return (effective_dep, connector_name). No-op for inline-config deps."""
    name = (dep.config or {}).get("connector") or ""
    if not name:
        return dep, ""
    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(Connector).where(
                    Connector.tenant_id == scope.tenant_id,
                    Connector.project_id == scope.project_id,
                    Connector.name == name,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"connector '{name}' is not configured in this project")
    if not row.enabled:
        raise LookupError(f"connector '{name}' is disabled")
    merged = {**(row.config or {}), **{k: v for k, v in (dep.config or {}).items() if k != "connector"}}
    return dep.model_copy(update={"kind": row.kind, "config": merged}), name
