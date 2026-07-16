from .base import BaseFetcher, FetchOutcome, get_fetcher, register_fetcher
from .mock import MockFetcher
from .rag import PgVectorRagFetcher


def register_default_fetchers(sessionmaker, embedder) -> None:
    """One registration path for BOTH deployables (api + standalone supervisor).
    The standalone lane previously registered nothing — every fetch would fail."""
    from .http import HttpFetcher
    from .mcp import McpFetcher

    register_fetcher("mock", MockFetcher())
    register_fetcher("rag", PgVectorRagFetcher(sessionmaker, embedder))
    register_fetcher("mcp", McpFetcher(sessionmaker))
    http_fetcher = HttpFetcher(sessionmaker)
    register_fetcher("http", http_fetcher)
    register_fetcher("api", http_fetcher)
    for kind in ("kg", "db"):
        register_fetcher(kind, MockFetcher())  # dedicated fetchers not built yet


__all__ = [
    "BaseFetcher",
    "FetchOutcome",
    "get_fetcher",
    "register_fetcher",
    "register_default_fetchers",
    "MockFetcher",
    "PgVectorRagFetcher",
]
