"""Reindex one workspace's documents: delete each, then upload it again.

Deleting a document is what clears it. The worker removes the Qdrant points,
the artifacts and the PostgreSQL rows (the document row goes last, and its
cascade takes the chunks and the embedding ledger with it). Re-uploading then
extracts and embeds the file again, with whatever the pipeline looks like now.

The order matters. Uploading a file that is already in the workspace is
answered as a duplicate -- the existing document and its run come back and
nothing is extracted -- so the document has to be gone before the upload.

Usage:
    uv run python scripts/reindex_workspace.py --workspace <uuid> <file> ...
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8000"


async def token(client: httpx.AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/v1/auth/token", data={"username": username, "password": password}
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


async def documents(client: httpx.AsyncClient, workspace_id: str) -> list[dict]:
    response = await client.get(f"/v1/workspaces/{workspace_id}/documents")
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("items", payload.get("documents", [])))


async def delete_runs(client: httpx.AsyncClient, document_id: str) -> list[dict]:
    response = await client.get("/v1/processing-runs/recent")
    response.raise_for_status()
    return [
        run
        for run in response.json().get("items", [])
        if run.get("document_id") == document_id and run.get("job_kind") == "delete"
    ]


async def wait_for_delete(client: httpx.AsyncClient, document_id: str) -> None:
    """Wait until the delete run is gone, which is when the document is gone.

    The document leaves the list as soon as the delete is accepted, but the
    worker is still clearing the Qdrant points and the rows. The delete run
    row goes with the document, so its disappearance is the signal that the
    workspace is free for the same file again.
    """
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        if not await delete_runs(client, document_id):
            return
        await asyncio.sleep(3)
    raise TimeoutError(f"timed out waiting for the delete of {document_id}")


async def wait_for_ready(
    client: httpx.AsyncClient, workspace_id: str, document_id: str
) -> dict:
    """Wait until the document reports itself ready."""
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        for document in await documents(client, workspace_id):
            if document["id"] == document_id:
                if document.get("status") == "ready":
                    return document
                if document.get("status") in {"failed", "cancelled", "unsupported_format"}:
                    raise RuntimeError(
                        f"{document['original_filename']} ended as {document['status']}"
                    )
                break
        await asyncio.sleep(5)
    raise TimeoutError(f"timed out waiting for {document_id} to become ready")


async def reindex(client: httpx.AsyncClient, workspace_id: str, path: Path) -> None:
    listing = await documents(client, workspace_id)
    matches = [d for d in listing if d["original_filename"] == path.name]
    for document in matches:
        print(f"  {path.name}: deleting {document['id']} ({document['status']})")
        response = await client.delete(
            f"/v1/workspaces/{workspace_id}/documents/{document['id']}"
        )
        response.raise_for_status()
        await wait_for_delete(client, document["id"])
        print(f"  {path.name}: deleted, the row and its run are gone")

    with path.open("rb") as handle:
        response = await client.post(
            f"/v1/workspaces/{workspace_id}/documents",
            files={"file": (path.name, handle, "application/octet-stream")},
        )
    response.raise_for_status()
    document = response.json()
    print(
        f"  {path.name}: uploaded as {document['id']} "
        f"(status={document['status']}, duplicate={document.get('duplicate')})"
    )
    if document.get("duplicate"):
        raise RuntimeError(f"{path.name} came back as a duplicate; nothing was extracted")
    await wait_for_ready(client, workspace_id, document["id"])
    print(f"  {path.name}: ready")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="Admin12345678*")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120) as client:
        bearer = await token(client, args.username, args.password)
        client.headers["Authorization"] = f"Bearer {bearer}"
        for path in args.files:
            print(f"== {path.name}")
            await reindex(client, args.workspace, path)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
