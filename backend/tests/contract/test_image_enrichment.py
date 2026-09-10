import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.vision import NoopVisionProvider
from enterprise_rag.ports import (
    CaptionStatus,
    LoadedImage,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
)
from enterprise_rag.ports.vision import VisionImage
from enterprise_rag.services import ImageEnricher


class FakeVision:
    def __init__(self, caption: str = "架构示意图", *, fail: bool = False) -> None:
        self.value = caption
        self.fail = fail
        self.calls = 0

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.VISION,
            "fake",
            "1",
            frozenset({"caption"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def caption(self, image: VisionImage) -> str | None:
        self.calls += 1
        assert image.media_type == "image/png"
        if self.fail:
            raise RuntimeError("secret provider response")
        return self.value

    async def aclose(self) -> None:
        return None


def loaded_image(*, ordinal: int = 0) -> LoadedImage:
    output = io.BytesIO()
    Image.new("RGB", (20, 10), "green").save(output, format="PNG")
    data = output.getvalue()
    return LoadedImage(
        page=1,
        ordinal=ordinal,
        name="diagram.png",
        media_type="image/png",
        sha256=hashlib.sha256(data).hexdigest(),
        width=20,
        height=10,
        data=data,
    )


@pytest.mark.anyio
async def test_images_are_content_addressed_and_captioned(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    vision = FakeVision()
    image = loaded_image()

    result = await ImageEnricher(store, vision).enrich((image, loaded_image(ordinal=1)))

    assert not result.degraded
    assert [item.caption_status for item in result.images] == [
        CaptionStatus.CREATED,
        CaptionStatus.CREATED,
    ]
    assert result.images[0].caption == "架构示意图"
    assert result.images[0].object_key == result.images[1].object_key
    assert await store.list_keys() == (result.images[0].object_key,)
    assert vision.calls == 2


@pytest.mark.anyio
async def test_noop_vision_skips_caption_without_failing_ingestion(tmp_path: Path) -> None:
    result = await ImageEnricher(
        LocalObjectStore(tmp_path / "objects"), NoopVisionProvider()
    ).enrich((loaded_image(),))

    assert not result.degraded
    assert result.images[0].caption is None
    assert result.images[0].caption_status is CaptionStatus.SKIPPED
    assert result.images[0].caption_error_code is None


@pytest.mark.anyio
async def test_vision_failure_is_sanitized_and_preserves_stored_image(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    result = await ImageEnricher(store, FakeVision(fail=True)).enrich((loaded_image(),))

    assert result.degraded
    assert result.images[0].caption_status is CaptionStatus.DEGRADED
    assert result.images[0].caption_error_code == "VISION_CAPTION_FAILED"
    assert "secret" not in repr(result.images[0])
    assert await store.exists(result.images[0].object_key)


@pytest.mark.anyio
async def test_empty_input_and_noop_provider_close_are_idempotent(tmp_path: Path) -> None:
    provider = NoopVisionProvider()
    result = await ImageEnricher(LocalObjectStore(tmp_path / "objects"), provider).enrich(())
    assert result.images == () and not result.degraded

    await provider.aclose()
    await provider.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await provider.caption(
            VisionImage(
                name="x.png",
                media_type="image/png",
                sha256="a" * 64,
                width=1,
                height=1,
                data=b"x",
            )
        )
