"""Narrow synchronous transport boundary for the Phase 9C retrieval adapter."""

from typing import Any, Protocol


class ZillizBm25Transport(Protocol):
    def search(
        self,
        *,
        endpoint: str,
        credential: str,
        collection_id: str,
        anns_field: str,
        query: str,
        limit: int,
        output_fields: tuple[str, ...],
        timeout_seconds: float,
    ) -> list[dict[str, Any]]: ...
