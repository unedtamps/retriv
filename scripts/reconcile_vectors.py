#!/usr/bin/env python
"""Remove vector points that no active processing run can serve.

Retrieval validates every vector match against PostgreSQL before using it, and
skips a match whose chunk is not part of the document's active run. The points
themselves, though, stay in the collection: a reindex writes a new run's
points and leaves the old run's points behind, because the supersede sweep
used to cover only the artifact bucket.

The cost of leaving them is not just wasted space. The workspace filter is
carried in the point payload, so each superseded point still answers searches
for the document's workspace; and the vector search fills its whole limit from
whatever it finds, so those points crowd out matches that could have been
served. They are all discarded afterwards, which means a search can come back
with fewer sources than it asked for, or with none.

This script reconciles the collection against the ledger: every point whose
chunk is not part of an active run is deleted.

Usage:
    uv run python scripts/reconcile_vectors.py            # report only
    uv run python scripts/reconcile_vectors.py --apply    # delete the orphans

The dry run is the default and prints the same summary, so the two runs can be
compared before anything is removed. Points are deleted by id in batches, so an
interrupted run can simply be repeated: the second pass finds only what is
still orphaned.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402

SERVABLE_CHUNKS = text("""
    SELECT chunk.id
    FROM document_chunks AS chunk
    JOIN documents AS document
      ON document.id = chunk.document_id
     AND document.active_processing_run_id = chunk.processing_run_id
    WHERE chunk.id = ANY(CAST(:chunk_ids AS uuid[]))
      AND document.deleted_at IS NULL
""")


@dataclass(frozen=True)
class CollectionPoint:
    point_id: str
    chunk_id: str
    processing_run_id: str | None


async def iter_points(
    client: httpx.AsyncClient,
    collection: str,
    *,
    page_size: int,
    profile: str,
) -> list[CollectionPoint]:
    """Every point in the collection, with its chunk and run, via scroll.

    Scroll rather than a filtered query: the points to drop are exactly the
    ones the normal search filters would not return consistently, and a filter
    on ``processing_run_id`` cannot express "not the active run" without first
    learning every active run's id.
    """
    found: list[CollectionPoint] = []
    offset: str | int | None = None
    while True:
        body: dict[str, object] = {
            "limit": page_size,
            "with_payload": ["chunk_id", "processing_run_id", "document_id"],
            "with_vector": False,
            "filter": {
                "must": [
                    {"key": "embedding_profile", "match": {"value": profile}},
                ]
            },
        }
        if offset is not None:
            body["offset"] = offset
        response = await client.post(
            f"collections/{collection}/points/scroll",
            json=body,
        )
        response.raise_for_status()
        result = response.json()["result"]
        for point in result.get("points", []):
            payload = point.get("payload") or {}
            found.append(
                CollectionPoint(
                    point_id=str(point["id"]),
                    chunk_id=str(payload.get("chunk_id")),
                    processing_run_id=payload.get("processing_run_id"),
                )
            )
        offset = result.get("next_page_offset")
        if offset is None:
            break
    return found


async def servable_chunk_ids(engine: AsyncEngine, chunk_ids: list[UUID]) -> set[UUID]:
    """The chunk ids PostgreSQL still vouches for, in the active-run sense."""
    if not chunk_ids:
        return set()
    async with engine.connect() as connection:
        result = await connection.execute(
            SERVABLE_CHUNKS, {"chunk_ids": [str(value) for value in chunk_ids]}
        )
        return {row[0] for row in result}


async def delete_points(
    client: httpx.AsyncClient, collection: str, point_ids: list[str]
) -> None:
    response = await client.post(
        f"collections/{collection}/points/delete",
        params={"wait": "true"},
        json={"points": point_ids},
    )
    response.raise_for_status()
    if (response.json().get("result") or {}).get("status") != "completed":
        raise RuntimeError(f"Point deletion did not complete: {response.text}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="delete the orphaned points")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    settings = Settings()
    engine = create_async_engine(settings.database_url)
    async with httpx.AsyncClient(
        base_url=f"{settings.qdrant_url.rstrip('/')}/",
        headers={"api-key": settings.qdrant_api_key.get_secret_value()},
        timeout=settings.qdrant_timeout_seconds,
    ) as client:
        try:
            points = await iter_points(
                client,
                settings.qdrant_collection,
                page_size=args.page_size,
                profile=settings.embedding_profile,
            )
        except httpx.HTTPError as exc:
            print(f"Could not read the collection: {exc}", file=sys.stderr)
            return 1

        chunk_ids: list[UUID] = []
        for point in points:
            try:
                chunk_ids.append(UUID(point.chunk_id))
            except (ValueError, TypeError):
                chunk_ids.append(UUID(int=0))

        servable = await servable_chunk_ids(engine, chunk_ids)

        orphans: list[str] = []
        for point, chunk_id in zip(points, chunk_ids, strict=True):
            if chunk_id not in servable:
                orphans.append(point.point_id)

        total = len(points)
        print(f"collection={settings.qdrant_collection} profile={settings.embedding_profile}")
        print(f"  points scanned : {total}")
        print(f"  servable       : {total - len(orphans)}")
        print(f"  orphaned       : {len(orphans)}")

        if not orphans:
            print("Nothing to reconcile.")
            await engine.dispose()
            return 0
        if not args.apply:
            print("\nDry run. Re-run with --apply to delete the orphaned points.")
            await engine.dispose()
            return 0

        deleted = 0
        for start in range(0, len(orphans), args.batch_size):
            batch = orphans[start : start + args.batch_size]
            await delete_points(client, settings.qdrant_collection, batch)
            deleted += len(batch)
            print(f"  deleted {deleted}/{len(orphans)}")

    await engine.dispose()
    print(f"\nRemoved {deleted} orphaned point(s).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
