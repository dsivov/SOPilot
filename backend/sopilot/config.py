import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_env_file() -> None:
    """Export .env entries (notably OPENAI_API_KEY) into the process environment.
    pydantic-settings only consumes SOPILOT_* keys; the OpenAI SDK reads os.environ."""
    env_path = Path(".env")
    if not env_path.exists():
        env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOPILOT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sopilot:sopilot@127.0.0.1:5433/sopilot"
    redis_url: str = "redis://127.0.0.1:6380/0"

    admin_token: str = ""
    # Deployment key for encrypting tenant connector secrets at rest (Fernet).
    # Empty = dev-only fallback key (a startup warning is logged).
    secret_key: str = ""

    # Which subsystems new projects run by default: "sop" (prompt/instruction
    # management only, live data resolution), "retrieval" (prediction + prefetch +
    # context selection only), or "both". Overridable per project (D-9).
    subsystems: str = "both"

    # D-1: run a supervisor consumer inside the API process (dev convenience).
    # Production runs `sopilot-supervisor` as its own deployment.
    embedded_supervisor: bool = False
    supervisor_group: str = "supervisor"
    supervisor_block_ms: int = 1000
    supervisor_batch: int = 10
    supervisor_autoclaim_idle_ms: int = 60000

    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    # Authoring-time model (SOP ingestion + conversational builder). Never on the turn path.
    builder_model: str = "gpt-4o"
    # Runtime classify/respond model for the TEXT channel. Strong by default — the
    # research (and our bench rerun) showed cheap classify models collapsing success.
    runtime_model: str = "gpt-4o"
    # Voice channel: OpenAI Realtime speech model + voice. The supervisor steers it
    # per turn via session.update; it never sees the SOP whole.
    # D-11 router: routes intake sessions to an SOP and checks mid-call
    # switches. Measured on the AENA corpus: cheap model ≈ strong oracle (88%).
    router_model: str = "gpt-4o-mini"
    realtime_model: str = "gpt-realtime"
    realtime_voice: str = "marin"

    # PASTE-style scheduler: max concurrent speculative LLM calls per worker.
    speculative_budget: int = 4

    # Session pool (validated POC values: 30-item cap, lowest-confidence eviction).
    pool_max_items: int = 30
    session_ttl_s: int = 7200

    # Per-tenant turn-rate quota (fixed one-minute window, Redis-counted).
    # 0 disables. One tenant's burst must not starve another's supervisor lane.
    quota_turns_per_min: int = 120

    # D-12 staleness gate: a pooled data item is served only if its content
    # embedding is close enough to the CURRENT utterance (cosine); below the
    # floor the runtime re-fetches live with the real query instead. 0 disables.
    consume_stale_min_cos: float = 0.30

    # Milestone B: instruction pre-generation (the pre-committed-criteria bet:
    # ship-claim requires hit>=70% + no success regression; audit rows measure it).
    instruction_prefetch: bool = True
    instruction_prefetch_max_pergen: int = 3  # (action,state) drafts per turn event
    instruction_ttl_s: int = 180

    # Empirical predictor defaults (locked config from the research).
    predictor_recency_half_life_days: float = 30.0
    predictor_shrinkage_kappa: float = 2.0
    predictor_min_supporting: int = 3


@lru_cache
def get_settings() -> Settings:
    _load_env_file()
    return Settings()
