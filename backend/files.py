"""File-attachment helpers.

Routes user-uploaded files into the Anthropic Files API and turns them into
the right content blocks:

- PDFs become native `document` blocks (Claude reads PDFs natively, including
  layout and images — better than pushing them through code_execution).
- Everything else becomes a `container_upload` block so the file lands in the
  code_execution sandbox at /mnt/user-data/uploads/<filename>, where the model
  can run pandas / openpyxl / python-docx / python-pptx on it.

Each file block is paired with a small `text` "[Attached: …]" block so the
model has the filename in plain language, and we annotate the file block with
a leading-underscore `_filename` hint so the UI can render a chip on refresh.
The agent strips underscore-prefixed keys before sending to the API.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from anthropic import AsyncAnthropic
from fastapi import HTTPException, UploadFile, status

logger = logging.getLogger(__name__)

# Per-file and per-message size caps. Anthropic's Files API allows larger
# uploads but we keep it conservative for a learning project.
MAX_FILE_BYTES = 10 * 1024 * 1024          # 10 MB
MAX_TOTAL_BYTES = 25 * 1024 * 1024         # 25 MB across all attachments

# Extension → MIME type. Anthropic's Files API requires a content-type for
# every upload; the model also uses it to decide how to handle the file.
EXTENSION_MIME = {
    ".pdf":  "application/pdf",
    ".csv":  "text/csv",
    ".json": "application/json",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

FILES_API_BETA = "files-api-2025-04-14"


def _ext(filename: str) -> str:
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


def _mime_for(filename: str) -> str:
    ext = _ext(filename)
    if ext not in EXTENSION_MIME:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type: {filename!r}. "
                f"Allowed: {', '.join(sorted(EXTENSION_MIME))}"
            ),
        )
    return EXTENSION_MIME[ext]


async def read_and_validate(uploads: Iterable[UploadFile]) -> list[tuple[str, bytes, str]]:
    """Read each upload into memory, validate type + size, return
    (filename, bytes, mime) tuples. Raises 400 on any violation.
    """
    out: list[tuple[str, bytes, str]] = []
    total = 0
    for uf in uploads:
        # uf.filename can be None for malformed multipart parts.
        name = uf.filename or ""
        mime = _mime_for(name)
        data = await uf.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{name!r} is {len(data):,} bytes — exceeds the "
                    f"{MAX_FILE_BYTES:,}-byte per-file limit."
                ),
            )
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Combined attachments exceed {MAX_TOTAL_BYTES:,} bytes."
                ),
            )
        out.append((name, data, mime))
    return out


async def upload_one(client: AsyncAnthropic, name: str, data: bytes, mime: str) -> str:
    """Upload a single file to the Anthropic Files API. Returns the file_id."""
    meta = await client.beta.files.upload(
        file=(name, data, mime),
        betas=[FILES_API_BETA],
    )
    return meta.id


def file_block(name: str, mime: str, file_id: str) -> dict[str, Any]:
    """Build the content block for a single file.

    PDFs use the native `document` block (Claude reads them directly).
    Everything else uses `container_upload` so code_execution can open them.

    Both block types carry a leading-underscore `_filename` hint that the
    agent strips before sending to the API; the UI uses it to render a chip
    when re-displaying the message after a page refresh.
    """
    if mime == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "file", "file_id": file_id},
            "_filename": name,
        }
    return {
        "type": "container_upload",
        "file_id": file_id,
        "_filename": name,
        "_mime": mime,
    }


async def build_user_content(
    client: AsyncAnthropic,
    text: str,
    files: list[tuple[str, bytes, str]],
) -> list[dict[str, Any]]:
    """Build the user message content list: text first, then for each file a
    "[Attached: name]" text block followed by its file block.

    Uploads files concurrently to keep latency down for multi-file sends.
    """
    import asyncio

    blocks: list[dict[str, Any]] = []
    if text.strip():
        blocks.append({"type": "text", "text": text})

    if not files:
        return blocks

    file_ids = await asyncio.gather(
        *(upload_one(client, n, d, m) for n, d, m in files)
    )
    for (name, _, mime), fid in zip(files, file_ids):
        blocks.append({"type": "text", "text": f"[Attached: {name}]"})
        blocks.append(file_block(name, mime, fid))
    return blocks
