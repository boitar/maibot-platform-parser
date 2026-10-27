"""Transaction-scoped X image preparation, before Base64 serialization.

The six configurable strategies follow the behavior of the reference X plugin.
This module uses only Pillow and the MaiBot-provided runtime data directory.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import math
from pathlib import Path
import random
import shutil
import time
import uuid

from PIL import Image

logger = logging.getLogger("plugin.multi_platform_parser.x_media")
STRATEGIES = ("reencode", "border", "crop", "canvas", "edge_noise", "pixel_noise")
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_ACTIVE: set[Path] = set()


@dataclass(frozen=True)
class MediaSettings:
    enabled: bool = True
    cleanup_after_send: bool = True
    cleanup_interval_minutes: int = 60
    cleanup_retention_hours: int = 24
    weight_reencode: int = 30
    weight_border: int = 20
    weight_crop: int = 15
    weight_canvas: int = 15
    weight_edge_noise: int = 10
    weight_pixel_noise: int = 10
    jpeg_quality: int = 95
    webp_quality: int = 95
    png_compress_level: int = 6
    border_min_px: int = 1
    border_max_px: int = 3
    crop_min_px: int = 1
    crop_max_px: int = 2
    canvas_min_px: int = 1
    canvas_max_px: int = 6
    noise_region_px: int = 2
    edge_noise_min_count: int = 2
    edge_noise_max_count: int = 20
    pixel_noise_min_count: int = 5
    pixel_noise_max_count: int = 30
    pixel_noise_delta_min: int = 1
    pixel_noise_delta_max: int = 3
    max_modified_ratio: float = 0.001
    border_color: str = "随机黑白"
    canvas_background: str = "随机黑白"
    noise_color: str = "随机黑白"


def _color(rng: random.Random, choice: str, mode: str) -> tuple:
    value = 0 if choice == "黑" else 255 if choice == "白" else rng.choice((0, 255))
    return (value, value, value, 255) if mode == "RGBA" else (value, value, value)


def _webp_lossless(source: Path) -> bool:
    with source.open("rb") as stream:
        header = stream.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WEBP":
            return False
        while chunk := stream.read(8):
            if len(chunk) != 8:
                return False
            kind = chunk[:4]
            if kind in (b"VP8L", b"VP8 "):
                return kind == b"VP8L"
            size = int.from_bytes(chunk[4:], "little")
            stream.seek(size + (size & 1), 1)
    return False


def _prepare(source: Path, folder: Path, cfg: MediaSettings, seed: int) -> Path | None:
    """Worker owns its partial/output files until the async caller adopts them."""
    outputs: list[Path] = []
    try:
        rng = random.Random(seed)
        with Image.open(source) as raw:
            fmt = raw.format
            if (
                fmt not in EXTENSIONS
                or getattr(raw, "is_animated", False)
                or getattr(raw, "n_frames", 1) > 1
            ):
                return None
            if raw.mode not in ("RGB", "RGBA", "P"):
                return None
            raw.load()
            mode = "RGBA" if raw.mode == "RGBA" or "transparency" in raw.info else "RGB"
            image = raw.convert(mode)
            width, height = image.size
            options = dict(
                exif=raw.info.get("exif"), icc_profile=raw.info.get("icc_profile")
            )
            options = {
                key: value for key, value in options.items() if isinstance(value, bytes)
            }
            if fmt == "JPEG":
                options.update(quality=cfg.jpeg_quality, subsampling=0)
            elif fmt == "PNG":
                options.update(compress_level=cfg.png_compress_level)
            else:
                options.update(
                    exact=True,
                    lossless=_webp_lossless(source),
                    quality=cfg.webp_quality,
                )

            weights = {
                name: max(0, getattr(cfg, "weight_" + name)) for name in STRATEGIES
            }
            for name in ("border", "crop", "canvas"):
                lo, hi = getattr(cfg, name + "_min_px"), getattr(cfg, name + "_max_px")
                if lo < 1 or hi < lo:
                    weights[name] = 0
            if max(width, height) <= cfg.crop_min_px:
                weights["crop"] = 0
            budget = (
                math.floor(width * height * cfg.max_modified_ratio)
                if 0 < cfg.max_modified_ratio <= 1
                else 0
            )
            if budget <= 0:
                weights["edge_noise"] = weights["pixel_noise"] = 0
            if (
                cfg.edge_noise_min_count < 1
                or cfg.edge_noise_max_count < cfg.edge_noise_min_count
            ):
                weights["edge_noise"] = 0
            if (
                cfg.pixel_noise_min_count < 1
                or cfg.pixel_noise_max_count < cfg.pixel_noise_min_count
                or cfg.pixel_noise_delta_min < 1
                or cfg.pixel_noise_delta_max < cfg.pixel_noise_delta_min
            ):
                weights["pixel_noise"] = 0
            eligible = [name for name in STRATEGIES if weights[name] > 0]
            if not eligible:
                return None
            strategy = rng.choices(eligible, [weights[name] for name in eligible], k=1)[
                0
            ]
            if strategy in ("border", "canvas"):
                lo, hi = (
                    getattr(cfg, strategy + "_min_px"),
                    getattr(cfg, strategy + "_max_px"),
                )
                if strategy == "border":
                    amount = rng.randint(lo, hi)
                    pad_w, pad_h, x, y = amount * 2, amount * 2, amount, amount
                    color = cfg.border_color
                else:
                    pad_w, pad_h = rng.randint(lo, hi), rng.randint(lo, hi)
                    x, y = rng.randint(0, pad_w), rng.randint(0, pad_h)
                    color = cfg.canvas_background
                canvas = Image.new(
                    mode, (width + pad_w, height + pad_h), _color(rng, color, mode)
                )
                canvas.paste(image, (x, y))
                image = canvas
            elif strategy == "crop":
                edges = [
                    edge
                    for edge, length in (
                        ("left", width),
                        ("right", width),
                        ("top", height),
                        ("bottom", height),
                    )
                    if length > cfg.crop_min_px
                ]
                edge = rng.choice(edges)
                length = width if edge in ("left", "right") else height
                amount = rng.randint(cfg.crop_min_px, min(cfg.crop_max_px, length - 1))
                boxes = {
                    "left": (amount, 0, width, height),
                    "right": (0, 0, width - amount, height),
                    "top": (0, amount, width, height),
                    "bottom": (0, 0, width, height - amount),
                }
                image = image.crop(boxes[edge])
            elif strategy in ("edge_noise", "pixel_noise"):
                band = max(1, cfg.noise_region_px)
                positions = []
                for y in range(height):
                    columns = (
                        range(width)
                        if y < band or y >= height - band
                        else (
                            list(range(min(band, width)))
                            + list(range(max(band, width - band), width))
                        )
                    )
                    positions.extend((x, y) for x in columns)
                prefix = "edge_noise" if strategy == "edge_noise" else "pixel_noise"
                count = min(
                    rng.randint(
                        getattr(cfg, prefix + "_min_count"),
                        getattr(cfg, prefix + "_max_count"),
                    ),
                    budget,
                    len(positions),
                )
                for x, y in rng.sample(positions, count):
                    pixel = image.getpixel((x, y))
                    if strategy == "edge_noise":
                        rgb = _color(rng, cfg.noise_color, "RGB")
                    else:
                        rgb = tuple(
                            max(
                                0,
                                min(
                                    255,
                                    value
                                    + rng.choice((-1, 1))
                                    * rng.randint(
                                        cfg.pixel_noise_delta_min,
                                        cfg.pixel_noise_delta_max,
                                    ),
                                ),
                            )
                            for value in pixel[:3]
                        )
                    image.putpixel((x, y), rgb + pixel[3:])

            folder.mkdir(parents=True, exist_ok=True)
            final = folder / (uuid.uuid4().hex + EXTENSIONS[fmt])
            partial = final.with_suffix(final.suffix + ".part")
            outputs.extend((partial, final))
            image.save(partial, format=fmt, **options)
            with Image.open(partial) as check:
                check.load()
                if (
                    check.format != fmt
                    or check.size != image.size
                    or getattr(check, "n_frames", 1) != 1
                ):
                    raise ValueError("prepared image verification failed")
            partial.replace(final)
            logger.debug(
                "X image prepared: strategy=%s format=%s size=%s",
                strategy,
                fmt,
                image.size,
            )
            return final
    except Exception as exc:
        for path in outputs:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning(
            "X image preparation failed; using original: %s", type(exc).__name__
        )
        return None


class MediaPrepareContext:
    def __init__(self, root: Path, settings: MediaSettings, *, seed: int | None = None):
        self.folder = root / uuid.uuid4().hex
        self.settings = settings
        self.rng = random.Random(seed)
        self._results: dict[str, Path | None] = {}
        self.processed_count = 0
        _ACTIVE.add(self.folder)

    async def prepare(self, source: Path) -> Path:
        if not self.settings.enabled:
            return source
        try:
            digest = await asyncio.to_thread(
                lambda: hashlib.sha256(source.read_bytes()).hexdigest()
            )
        except OSError:
            return source
        if digest in self._results:
            return self._results[digest] or source
        worker = asyncio.create_task(
            asyncio.to_thread(
                _prepare, source, self.folder, self.settings, self.rng.getrandbits(64)
            )
        )
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancelled = True
                # A thread cannot be cancelled: wait before removing its late output.
                if worker.done():
                    result = worker.result()
                    break
        if cancelled:
            if result:
                result.unlink(missing_ok=True)
            raise asyncio.CancelledError
        self._results[digest] = result
        if result:
            self.processed_count += 1
        return result or source

    async def cleanup(self) -> None:
        if self.settings.cleanup_after_send:
            worker = asyncio.create_task(
                asyncio.to_thread(shutil.rmtree, self.folder, True)
            )
            cancelled = False
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled = True
            worker.result()
            _ACTIVE.discard(self.folder)
            if cancelled:
                raise asyncio.CancelledError
        else:
            _ACTIVE.discard(self.folder)
        self._results.clear()


def cleanup_stale(root: Path, retention_hours: float) -> int:
    if not root.is_dir():
        return 0
    cutoff = time.time() - retention_hours * 3600
    removed = 0
    for path in root.iterdir():
        if path in _ACTIVE or path.is_symlink():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed += 1
        except OSError:
            logger.warning("X stale image cleanup failed: %s", path.name)
    return removed
