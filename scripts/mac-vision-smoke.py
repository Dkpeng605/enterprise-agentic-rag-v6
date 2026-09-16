#!/usr/bin/env python3
"""Verify the configured Mac Vision path without printing secrets or caption text.

This smoke intentionally exercises the real OpenAI-compatible endpoint, image
enrichment, and the Splitter boundary where a caption becomes retrieval text.
It does not create a database document or modify the Mac workspace.
"""

import asyncio
import hashlib
import io
import json
import tempfile
from pathlib import Path
from uuid import UUID

from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.config import load_settings
from enterprise_rag.domain import RootKind
from enterprise_rag.ports import CleanRoot, IngestionContext, LoadedImage
from enterprise_rag.services import ImageEnricher
from enterprise_rag.services.vision_provider import build_vision_provider
from PIL import Image, ImageDraw


async def run() -> dict[str, object]:
    settings = load_settings()
    if settings.providers.vision != "openai_compatible":
        raise RuntimeError(
            "Mac Vision is disabled; set ENTERPRISE_RAG__PROVIDERS__VISION=openai_compatible"
        )
    provider = build_vision_provider(settings, reuse_llm_credentials=True)
    image_data = _sample_png()
    image = LoadedImage(
        page=1,
        ordinal=0,
        name="vision-smoke.png",
        media_type="image/png",
        sha256=hashlib.sha256(image_data).hexdigest(),
        width=96,
        height=64,
        data=image_data,
    )
    with tempfile.TemporaryDirectory(prefix="enterprise-rag-vision-smoke-") as directory:
        root = Path(directory)
        store = LocalObjectStore(root / "objects")
        try:
            enriched = await ImageEnricher(store, provider).enrich((image,))
            caption = enriched.images[0].caption
            if not caption:
                raise RuntimeError("Vision returned an empty caption")
            split = await StructureAwareSplitter(
                target_tokens=350,
                max_tokens=480,
                overlap_tokens=0,
            ).split(
                CleanRoot(
                    ordinal=0,
                    kind=RootKind.SECTION,
                    source_locator={"smoke": "vision"},
                    raw_text="图片输入验收。",
                    clean_text="图片输入验收。",
                    metadata={"image_captions": (caption,)},
                ),
                IngestionContext(
                    tenant_id=UUID("01900000-0000-7000-8000-000000001001"),
                    document_id=UUID("01900000-0000-7000-8000-000000001002"),
                    version_id=UUID("01900000-0000-7000-8000-000000001003"),
                    temporary_directory=root,
                    index_revision="vision-smoke-v1",
                ),
            )
            retrieval_text = split.leaves[0].retrieval_text
            return {
                "provider": provider.info().name,
                "model": provider.info().version,
                "image_bytes": len(image_data),
                "caption_status": enriched.images[0].caption_status.value,
                "caption_chars": len(caption),
                "reasoning_wrapper_removed": "<think>" not in caption.lower(),
                "object_stored": await store.exists(enriched.images[0].object_key),
                "caption_in_retrieval_text": caption in retrieval_text,
                "leaf_token_count": split.leaves[0].token_count,
            }
        finally:
            await store.aclose()
            await provider.aclose()


def _sample_png() -> bytes:
    image = Image.new("RGB", (96, 64), "white")
    ImageDraw.Draw(image).rectangle((8, 8, 88, 56), outline="black", width=2)
    ImageDraw.Draw(image).text((18, 24), "RAG", fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, sort_keys=True))
