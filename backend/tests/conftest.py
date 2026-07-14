import numpy as np
import pytest
from fakeredis import aioredis as fakeaioredis

from sopilot.embeddings import FakeEmbeddings
from sopilot.pool import SessionPool
from sopilot.tenancy import Scope


@pytest.fixture
def scope_a() -> Scope:
    return Scope(tenant_id="tenantA", project_id="projA")


@pytest.fixture
def scope_b() -> Scope:
    return Scope(tenant_id="tenantB", project_id="projB")


@pytest.fixture
def embedder() -> FakeEmbeddings:
    return FakeEmbeddings(dim=32)


@pytest.fixture
async def redis():
    r = fakeaioredis.FakeRedis()
    yield r
    await r.aclose()


@pytest.fixture
def pool(redis) -> SessionPool:
    return SessionPool(redis, max_items=5, session_ttl_s=600)


@pytest.fixture
def unit_vec():
    def make(seed: int, dim: int = 32) -> np.ndarray:
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(dim).astype(np.float32)
        return v / np.linalg.norm(v)

    return make
