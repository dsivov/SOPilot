from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOPILOT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sopilot:sopilot@127.0.0.1:5433/sopilot"
    redis_url: str = "redis://127.0.0.1:6380/0"

    admin_token: str = ""

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

    # PASTE-style scheduler: max concurrent speculative LLM calls per worker.
    speculative_budget: int = 4

    # Session pool (validated POC values: 30-item cap, lowest-confidence eviction).
    pool_max_items: int = 30
    session_ttl_s: int = 7200

    # Empirical predictor defaults (locked config from the research).
    predictor_recency_half_life_days: float = 30.0
    predictor_shrinkage_kappa: float = 2.0
    predictor_min_supporting: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
