import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from core import x_media
from core.x_media import MediaPrepareContext, MediaSettings, STRATEGIES, cleanup_stale


def force_strategy(name, **updates):
    weights = {"weight_" + key: int(key == name) for key in STRATEGIES}
    return replace(MediaSettings(), **(weights | updates))


class XMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def image(self, fmt="PNG", mode="RGBA"):
        path = self.root / (fmt + ".jpg")  # Intentionally mismatched extension.
        color = (80, 100, 120, 90) if mode == "RGBA" else (80, 100, 120)
        image = Image.new(mode, (96, 64), color)
        image.save(
            path,
            format=fmt,
            **({"lossless": True, "exact": True} if fmt == "WEBP" else {}),
        )
        return path

    async def test_six_strategies_preserve_actual_format_and_source(self):
        for fmt in ("JPEG", "PNG", "WEBP"):
            source = self.image(fmt, "RGB" if fmt == "JPEG" else "RGBA")
            before = source.read_bytes()
            for strategy in STRATEGIES:
                with self.subTest(fmt=fmt, strategy=strategy):
                    context = MediaPrepareContext(
                        self.root / "temp", force_strategy(strategy), seed=7
                    )
                    prepared = await context.prepare(source)
                    self.assertNotEqual(prepared, source)
                    self.assertEqual(source.read_bytes(), before)
                    with Image.open(prepared) as image:
                        self.assertEqual(image.format, fmt)
                        w, h = image.size
                        if strategy == "border":
                            self.assertTrue(98 <= w <= 102 and 66 <= h <= 70)
                        elif strategy == "canvas":
                            self.assertTrue(97 <= w <= 102 and 65 <= h <= 70)
                        elif strategy == "crop":
                            self.assertTrue(
                                (94 <= w < 96 and h == 64) or (62 <= h < 64 and w == 96)
                            )
                        else:
                            self.assertEqual(image.size, (96, 64))
                        if fmt != "JPEG":
                            self.assertIn("A", image.getbands())
                    await context.cleanup()
                    self.assertFalse(prepared.exists())

    async def test_content_dedup_and_new_transactions(self):
        source = self.image()
        duplicate = self.root / "duplicate.png"
        duplicate.write_bytes(source.read_bytes())
        first = MediaPrepareContext(self.root / "temp", force_strategy("border"))
        a = await first.prepare(source)
        self.assertEqual(await first.prepare(duplicate), a)
        self.assertEqual(first.processed_count, 1)
        second = MediaPrepareContext(self.root / "temp", force_strategy("border"))
        b = await second.prepare(source)
        self.assertNotEqual(a, b)
        await first.cleanup()
        self.assertTrue(b.exists())
        await second.cleanup()

    async def test_animation_remains_byte_identical(self):
        for fmt in ("GIF", "PNG", "WEBP"):
            with self.subTest(fmt=fmt):
                path = self.root / ("animated-" + fmt + ".jpg")
                frames = [
                    Image.new("RGBA", (12, 12), color) for color in ("red", "blue")
                ]
                frames[0].save(
                    path,
                    format=fmt,
                    save_all=True,
                    append_images=frames[1:],
                    duration=100,
                    loop=0,
                )
                digest = hashlib.sha256(path.read_bytes()).digest()
                context = MediaPrepareContext(self.root / "temp", MediaSettings())
                self.assertEqual(await context.prepare(path), path)
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)
                self.assertEqual(context.processed_count, 0)
                await context.cleanup()

    async def test_invalid_image_zero_weights_and_tiny_image_fallback(self):
        bad = self.root / "bad.jpg"
        bad.write_bytes(b"broken")
        context = MediaPrepareContext(self.root / "temp", MediaSettings())
        self.assertEqual(await context.prepare(bad), bad)
        self.assertEqual(await context.prepare(bad), bad)
        await context.cleanup()
        source = self.image()
        context = MediaPrepareContext(
            self.root / "temp",
            replace(MediaSettings(), **{"weight_" + name: 0 for name in STRATEGIES}),
        )
        self.assertEqual(await context.prepare(source), source)
        await context.cleanup()
        tiny = self.root / "tiny.png"
        Image.new("RGB", (1, 1)).save(tiny)
        context = MediaPrepareContext(self.root / "temp", force_strategy("crop"))
        self.assertEqual(await context.prepare(tiny), tiny)
        await context.cleanup()

    async def test_lossless_webp_retains_transparent_rgb(self):
        source = self.image("WEBP")
        context = MediaPrepareContext(self.root / "temp", force_strategy("reencode"))
        output = await context.prepare(source)
        with Image.open(source) as original, Image.open(output) as prepared:
            self.assertEqual(original.tobytes(), prepared.tobytes())
        await context.cleanup()

    async def test_failed_save_removes_partial_output(self):
        source = self.image()
        context = MediaPrepareContext(self.root / "temp", force_strategy("reencode"))
        with patch.object(Image.Image, "save", side_effect=OSError("disk full")):
            self.assertEqual(await context.prepare(source), source)
        self.assertEqual(list(context.folder.glob("*")), [])
        await context.cleanup()

    async def test_repeated_cancellation_waits_for_late_worker_output(self):
        source = self.image()
        entered, release = threading.Event(), threading.Event()
        original = x_media._prepare

        def delayed(*args):
            entered.set()
            release.wait(2)
            return original(*args)

        context = MediaPrepareContext(self.root / "temp", force_strategy("border"))
        with patch.object(x_media, "_prepare", delayed):
            task = asyncio.create_task(context.prepare(source))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(list(context.folder.glob("*")), [])
        await context.cleanup()
        self.assertNotIn(context.folder, x_media._ACTIVE)
        self.assertFalse(context.folder.exists())

    async def test_stale_cleaner_preserves_active_transaction(self):
        source = self.image()
        context = MediaPrepareContext(
            self.root / "temp", force_strategy("border", cleanup_after_send=False)
        )
        output = await context.prepare(source)
        old = time.time() - 90000
        os.utime(context.folder, (old, old))
        self.assertEqual(cleanup_stale(self.root / "temp", 24), 0)
        self.assertTrue(output.exists())
        await context.cleanup()
        self.assertEqual(cleanup_stale(self.root / "temp", 24), 1)
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
