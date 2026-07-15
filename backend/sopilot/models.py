"""SQLAlchemy models. Every runtime table carries the tenant→project spine; queries
must always filter on it (see tenancy.Scope helpers).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ---------- Tenancy spine ----------


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "slug"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    # D-9: which subsystems this project runs — "sop" | "retrieval" | "both".
    # Empty string means "use the deployment default" (settings.subsystems).
    subsystems: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex of the raw key
    label: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(16), default="runtime")  # runtime | admin
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------- SOPs (versioned, project-scoped) ----------


class Sop(Base):
    __tablename__ = "sops"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PromptBlock(Base):
    """D-7: reusable, separately-versioned prompt language (role framing, stage
    instructions, compliance boilerplate). SOP stages bind blocks by name; the
    binding is resolved to published versions and snapshotted at session start."""

    __tablename__ = "prompt_blocks"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32), default="stage")  # stage | compliance | role | escalation
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PromptBlockVersion(Base):
    __tablename__ = "prompt_block_versions"
    __table_args__ = (UniqueConstraint("block_id", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    block_id: Mapped[str] = mapped_column(ForeignKey("prompt_blocks.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published | retired
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SopVersion(Base):
    __tablename__ = "sop_versions"
    __table_args__ = (UniqueConstraint("sop_id", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    sop_id: Mapped[str] = mapped_column(ForeignKey("sops.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published | retired
    definition: Mapped[dict] = mapped_column(JSON)  # TaskDefinition payload
    # Provenance: the document this version was drafted from (extracted text) and
    # where it came from. Kept with the version — the SOP's auditable origin.
    source_document: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- Conversation runtime (audit + predictor substrate) ----------


class ConversationSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    sop_id: Mapped[str] = mapped_column(String(32), index=True)
    sop_version: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[str] = mapped_column(String(32), default="text")  # text | realtime_voice | bench
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | ended
    # D-7: prompt-block bindings snapshotted at session start —
    # {block_name: {"version": int, "content": str}}. A mid-conversation block
    # publish never lands mid-call.
    prompt_bindings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    terminal_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (UniqueConstraint("session_id", "turn_index"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    user_message: Mapped[str] = mapped_column(Text, default="")
    assistant_message: Mapped[str] = mapped_column(Text, default="")
    cohort: Mapped[str] = mapped_column(String(100), default="")
    mood: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(100), default="")
    instruction_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PrecedentTrace(Base):
    """One (situation → action → outcome) trace. The counting predictor's fuel.

    Denormalized with (session_id, turn_index) so the offset self-join needs no
    detour through `turns`.
    """

    __tablename__ = "precedent_traces"
    __table_args__ = (
        Index("ix_precedent_lookup", "sop_id", "action", "cohort"),
        Index("ix_precedent_session_turn", "session_id", "turn_index"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    sop_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(32))
    turn_index: Mapped[int] = mapped_column(Integer)
    cohort: Mapped[str] = mapped_column(String(100), default="")
    mood: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(100))
    immediate_state: Mapped[str] = mapped_column(String(100), default="")
    immediate_reward: Mapped[float] = mapped_column(Float, default=0.0)
    terminal_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    terminal_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    turn_distance_to_terminal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_text: Mapped[str] = mapped_column(Text, default="")
    situation_embedding = mapped_column(Vector(get_settings().embedding_dim), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ---------- Retrieval corpora (managed, per-project) ----------


class Corpus(Base):
    __tablename__ = "corpora"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    embedding_model: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CorpusDoc(Base):
    __tablename__ = "corpus_docs"
    __table_args__ = (UniqueConstraint("corpus_id", "doc_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    corpus_id: Mapped[str] = mapped_column(ForeignKey("corpora.id", ondelete="CASCADE"), index=True)
    doc_key: Mapped[str] = mapped_column(String(200))
    topic: Mapped[str] = mapped_column(String(200), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    text: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(get_settings().embedding_dim), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- Audit (the SLI substrate) ----------


class DataFetchAudit(Base):
    __tablename__ = "data_fetches"
    __table_args__ = (Index("ix_fetch_session", "session_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(32))
    dependency_name: Mapped[str] = mapped_column(String(200))
    action_name: Mapped[str] = mapped_column(String(100), default="")
    kind: Mapped[str] = mapped_column(String(16), default="mock")
    speculative: Mapped[bool] = mapped_column(Boolean, default=True)
    predictor_source: Mapped[str] = mapped_column(String(16), default="empirical")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    issued_at_turn: Mapped[int] = mapped_column(Integer, default=0)
    predicted_turn: Mapped[int] = mapped_column(Integer, default=0)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_hash: Mapped[str | None] = mapped_column(String(24), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetch_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    payload_summary: Mapped[str] = mapped_column(String(500), default="")
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    consumed_at_turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wasted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PoolPickAudit(Base):
    __tablename__ = "pool_picks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    turn_index: Mapped[int] = mapped_column(Integer, default=0)
    picked_item_ids: Mapped[list] = mapped_column(JSON, default=list)
    pool_size_at_pick: Mapped[int] = mapped_column(Integer, default=0)
    rationale: Mapped[str] = mapped_column(String(300), default="")
    pick_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
