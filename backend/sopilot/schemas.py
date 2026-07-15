"""SOP schema (TaskDefinition) — ported from the MCPlanner POC, minus the research
knobs that the ablations retired (Thompson exploration, MCTS-on-path config, rollout
policies). What remains is the locked production shape.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    name: Optional[str] = None
    description: str = ""
    demographics: dict[str, str] = Field(default_factory=dict)


class ConversationProfile(BaseModel):
    agent_role: str = ""
    goal: str = ""
    success_markers: list[str] = Field(default_factory=list)  # user_state names → terminal success
    failure_markers: list[str] = Field(default_factory=list)  # user_state names → terminal failure
    knowledge: str = ""


class NamedItem(BaseModel):
    name: str
    description: str = ""
    must_say: list[str] = Field(default_factory=list)
    must_not_say: list[str] = Field(default_factory=list)
    data_dependencies: list[str] = Field(default_factory=list)
    # D-7: names of PromptBlocks whose published content is injected when this
    # action/stage runs. Resolved + snapshotted at session start.
    prompt_blocks: list[str] = Field(default_factory=list)


class CohortMood(BaseModel):
    name: str
    description: str = ""
    prior: float = 1.0


class CohortItem(NamedItem):
    moods: list[CohortMood] = Field(default_factory=list)


class DataDependency(BaseModel):
    """An external lookup an agent_action needs at execution time.

    Mutating operations MUST set idempotent=False — the prefetch scheduler refuses
    to fire them speculatively.
    """

    name: str
    description: str = ""
    kind: Literal["mock", "rag", "kg", "db", "api", "mcp"] = "mock"
    config: dict = Field(default_factory=dict)
    expected_latency_ms: int = 1000
    cache_ttl_s: int = 300
    idempotent: bool = True
    # Rendered with {user_text}/{cohort}/{mood}/{state}/{action} at plan-build time;
    # absent → action-keyed fetch.
    query_template: Optional[str] = None


EdgeDir = Literal["forward", "backward", "both"]


class SOPEdge(BaseModel):
    src: str
    dst: str
    direction: EdgeDir = "forward"
    note: str = ""


class SOPGraphSchema(BaseModel):
    nodes: list[str] = Field(default_factory=list)
    edges: list[SOPEdge] = Field(default_factory=list)


class TaskDefinition(BaseModel):
    """The full SOP definition for one task."""

    name: str = "Untitled SOP"
    description: str = ""
    user_profile: UserProfile = Field(default_factory=UserProfile)
    conversation_profile: ConversationProfile = Field(default_factory=ConversationProfile)
    agent_actions: list[NamedItem] = Field(default_factory=list)
    user_states: list[NamedItem] = Field(default_factory=list)
    cohorts: list[CohortItem] = Field(default_factory=list)
    data_dependencies: list[DataDependency] = Field(default_factory=list)
    sop: SOPGraphSchema = Field(default_factory=SOPGraphSchema)


# ---------- API payloads ----------


class TenantCreateRequest(BaseModel):
    slug: str
    name: str = ""


class TenantCreateResponse(BaseModel):
    tenant_id: str
    slug: str
    api_key: str  # returned exactly once


class ProjectCreateRequest(BaseModel):
    slug: str
    name: str = ""
    # D-9: "sop" | "retrieval" | "both"; empty = deployment default.
    subsystems: str = ""


class SopSaveRequest(BaseModel):
    definition: TaskDefinition


class SopMeta(BaseModel):
    id: str
    name: str
    latest_version: int
    updated_at: str


class SessionStartRequest(BaseModel):
    sop_id: str
    channel: Literal["text", "realtime_voice", "bench"] = "text"
    # D-9 override for THIS session only; empty = project default.
    subsystems: Literal["", "sop", "retrieval", "both"] = ""


class SessionStartResponse(BaseModel):
    session_id: str
    sop_version: int
    definition: TaskDefinition
