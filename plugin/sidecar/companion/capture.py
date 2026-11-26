# -*- coding: utf-8 -*-
"""DXGI screen capture (with PIL ImageGrab fallback), low-cost 64x40 frame-diff gating,
and 2x2 timestamped Contact Sheet builder for DeepSeek-V4.1-Flash native vision."""
from __future__ import annotations

import os
import time
from collections import deque
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw

try:
    import dxcam  # type: ignore
except Exception:
    dxcam = None

try:
    from PIL import ImageGrab
except Exception:
    ImageGrab = None


class VisionSensor:
    """Captures window/screen frames with minimal CPU overhead and builds 2x2 contact sheets."""

    def __init__(self, frames_dir: str, max_keyframes: int = 4):
        self.frames_dir = frames_dir
        os.makedirs(self.frames_dir, exist_ok=True)
        self.max_keyframes = max(1, min(4, int(max_keyframes)))
        self._camera = None
        self.backend = "pil"
        self.last_thumb: np.ndarray | None = None
        self.keyframes: deque[tuple[str, Image.Image]] = deque(maxlen=self.max_keyframes)
        self.total_grabs = 0
        self.static_skips = 0
        self._init_camera()

    def _init_camera(self) -> None:
        if dxcam is not None and os.name == "nt":
            try:
                self._camera = dxcam.create(output_color="RGB")
                if self._camera is not None:
                    self.backend = "dxcam"
                    return
            except Exception:
                self._camera = None
        self.backend = "pil"

    def grab_region(
        self,
        region: tuple[int, int, int, int] | None = None,
        fallback_grab: Callable[[tuple[int, int, int, int] | None], Image.Image] | None = None,
        allow_fallback_on_none: bool = False,
    ) -> Image.Image | None:
        """Grab (x, y, w, h) as RGB PIL.Image.
        When allow_fallback_on_none=False (background polling), DXGI returning None means
        the compositor presented no new frame -> returns None to save CPU.
        When allow_fallback_on_none=True (on-demand ocr_crop / snap), falls back to PIL/GDI
        so static screens always yield a valid image."""
        self.total_grabs += 1
        if self._camera is not None:
            try:
                dx_region = None
                if region is not None:
                    x, y, w, h = region
                    x1 = max(0, int(x))
                    y1 = max(0, int(y))
                    x2 = int(x) + int(w)
                    y2 = int(y) + int(h)
                    if x2 <= x1 or y2 <= y1:
                        raise ValueError("Invalid clamped DXGI region")
                    dx_region = (x1, y1, x2, y2)
                frame = self._camera.grab(region=dx_region)
                if frame is not None:
                    return Image.fromarray(frame)
                if not allow_fallback_on_none:
                    return None
            except Exception:
                pass
        if fallback_grab is not None:
            return fallback_grab(region)
        if ImageGrab is not None:
            bbox = None
            if region is not None:
                x, y, w, h = region
                bbox = (int(x), int(y), int(x + w), int(y + h))
            return ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
        return None

    @staticmethod
    def _to_thumb_gray(img: Image.Image) -> np.ndarray:
        small = img.convert("L").resize((64, 40), Image.Resampling.BILINEAR)
        return np.asarray(small, dtype=np.int16)

    def frame_diff_score(self, img: Image.Image) -> float:
        """Compute mean absolute pixel difference (0..255) on 64x40 grayscale thumbnail."""
        thumb = self._to_thumb_gray(img)
        if self.last_thumb is None:
            self.last_thumb = thumb
            return 255.0
        return float(np.mean(np.abs(thumb - self.last_thumb)))

    def is_scene_changed(self, img: Image.Image, threshold: float = 4.5) -> tuple[bool, float]:
        thumb = self._to_thumb_gray(img)
        if self.last_thumb is None:
            self.last_thumb = thumb
            return True, 255.0
        diff = float(np.mean(np.abs(thumb - self.last_thumb)))
        if diff >= threshold:
            self.last_thumb = thumb
            return True, round(diff, 3)
        self.static_skips += 1
        return False, round(diff, 3)

    def push_keyframe(self, img: Image.Image, label: str | None = None) -> None:
        ts = label or time.strftime("%H:%M:%S")
        self.keyframes.append((ts, img.copy()))

    def clear_keyframes(self) -> None:
        self.keyframes.clear()

    @staticmethod
    def build_contact_sheet(
        frames: list[tuple[str, Image.Image]],
        cell_size: tuple[int, int] = (640, 360),
    ) -> Image.Image:
        """Pack 1..4 timestamped frames into a single 2x2 (1280x720) contact sheet."""
        if not frames:
            raise ValueError("frames must not be empty")
        w, h = cell_size
        canvas = Image.new("RGB", (w * 2, h * 2), (16, 18, 22))
        draw = ImageDraw.Draw(canvas)
        positions = [(0, 0), (w, 0), (0, h), (w, h)]
        selected = frames[-4:]
        for idx, (ts, img) in enumerate(selected):
            resized = img.convert("RGB").resize((w, h), Image.Resampling.BILINEAR)
            px, py = positions[idx]
            canvas.paste(resized, (px, py))
            tag = f"#{idx + 1} {ts}"
            box_w = min(w - 16, max(108, len(tag) * 9 + 16))
            draw.rectangle([px + 8, py + 8, px + 8 + box_w, py + 32], fill=(0, 0, 0))
            draw.text((px + 14, py + 12), tag, fill=(255, 232, 92))
        return canvas

    def save_contact_sheet(
        self,
        frames: list[tuple[str, Image.Image]] | None = None,
        cell_size: tuple[int, int] = (640, 360),
        quality: int = 84,
    ) -> str:
        use_frames = frames if frames is not None else list(self.keyframes)
        sheet = self.build_contact_sheet(use_frames, cell_size=cell_size)
        fname = f"sheet_{int(time.time() * 1000)}.jpg"
        out_path = os.path.join(self.frames_dir, fname)
        sheet.save(out_path, format="JPEG", quality=quality, optimize=True)
        return out_path

    def status(self) -> dict:
        return {
            "backend": self.backend,
            "dxcamAvailable": dxcam is not None,
            "bufferedKeyframes": len(self.keyframes),
            "totalGrabs": self.total_grabs,
            "staticSkips": self.static_skips,
        }
