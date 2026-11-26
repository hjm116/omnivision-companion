# -*- coding: utf-8 -*-
"""FastReflexEngine — Local 20-60 FPS fast-system reflex loop for action games.
Evaluates declarative JSON ROI color-ratio rules from presets/games/*.json and triggers
SafeGameDriver actions within ~10-30ms without waiting on cloud LLM roundtrips.
Defaults to dry_run=True (log & record only, no real key presses) unless explicitly enabled live."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

import numpy as np
from PIL import Image

from .driver import SafeGameDriver


def eval_color_ratio_rule(img: Image.Image, rule: dict[str, Any]) -> tuple[bool, float]:
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    h, w, _ = arr.shape
    rx, ry, rw, rh = rule.get("roi", [0.0, 0.0, 1.0, 1.0])
    x0 = max(0, min(w - 1, int(rx * w)))
    y0 = max(0, min(h - 1, int(ry * h)))
    x1 = max(x0 + 1, min(w, int((rx + rw) * w)))
    y1 = max(y0 + 1, min(h, int((ry + rh) * h)))
    patch = arr[y0:y1, x0:x1, :]

    rmin, gmin, bmin = rule.get("rgbMin", [0, 0, 0])
    rmax, gmax, bmax = rule.get("rgbMax", [255, 255, 255])
    mask = (
        (patch[:, :, 0] >= rmin) & (patch[:, :, 0] <= rmax)
        & (patch[:, :, 1] >= gmin) & (patch[:, :, 1] <= gmax)
        & (patch[:, :, 2] >= bmin) & (patch[:, :, 2] <= bmax)
    )
    ratio = float(np.mean(mask))
    op = str(rule.get("operator", "lt")).lower()
    thr = float(rule.get("threshold", 0.3))
    matched = (
        (ratio < thr) if op == "lt"
        else (ratio <= thr) if op == "lte"
        else (ratio > thr) if op == "gt"
        else (ratio >= thr) if op == "gte"
        else False
    )
    return matched, round(ratio, 4)


class FastReflexEngine:
    """Runs local high-frequency ROI reflex rules while the LLM slow system handles macro strategy."""

    def __init__(self, driver: SafeGameDriver, presets_dir: str):
        self.driver = driver
        self.presets_dir = presets_dir
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        self.preset_name = ""
        self.preset: dict[str, Any] = {}
        self.reflex_mode = "normal"
        self.dry_run = True  # Safe default: observe & record without sending real keys
        self.fps = 20
        self._cooldowns: dict[str, float] = {}
        self.triggers = 0
        self.last_trigger: dict[str, Any] | None = None
        self.last_error = ""

    def list_presets(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not os.path.isdir(self.presets_dir):
            return out
        for fn in sorted(os.listdir(self.presets_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.presets_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                out.append({
                    "id": os.path.splitext(fn)[0],
                    "name": data.get("name") or fn,
                    "genre": data.get("genre") or "custom",
                    "description": data.get("description") or "",
                    "rules": len(data.get("rules") or []),
                })
            except Exception:
                pass
        return out

    def load_preset(self, preset_id: str) -> dict[str, Any]:
        clean = os.path.basename(str(preset_id or "").strip())
        if not clean.endswith(".json"):
            clean += ".json"
        path = os.path.join(self.presets_dir, clean)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Game preset not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.preset = data
        self.preset_name = os.path.splitext(clean)[0]
        self.fps = max(5, min(60, int(data.get("fps") or 20)))
        self._cooldowns.clear()
        return data

    def evaluate_once(self, img: Image.Image, dry_run: bool | None = None) -> list[dict[str, Any]]:
        if self.reflex_mode == "paused":
            return []
        eff_dry = self.dry_run if dry_run is None else bool(dry_run)
        fired: list[dict[str, Any]] = []
        now = time.time()
        for rule in self.preset.get("rules") or []:
            modes = rule.get("modes")
            if modes and self.reflex_mode not in modes:
                continue
            rid = str(rule.get("id") or rule.get("name") or "rule")
            cd_s = float(rule.get("cooldownMs") or 1000) / 1000.0
            if now - self._cooldowns.get(rid, 0.0) < cd_s:
                continue
            matched, ratio = eval_color_ratio_rule(img, rule)
            if not matched:
                continue
            self._cooldowns[rid] = now
            act = rule.get("action") or {}
            res = None
            if act.get("type") == "tap" and act.get("key"):
                res = self.driver.tap_key(act["key"], int(act.get("durationMs") or 60), dry_run_override=eff_dry)
            elif act.get("type") == "hold" and act.get("key"):
                res = self.driver.hold_key(act["key"], int(act.get("durationMs") or 200), dry_run_override=eff_dry)
            elif act.get("type") == "sequence" and act.get("steps"):
                res = self.driver.run_sequence(act["steps"], dry_run_override=eff_dry)
            rec = {
                "ruleId": rid,
                "ratio": ratio,
                "dryRun": eff_dry,
                "action": act,
                "result": res,
                "ts": round(now, 3),
            }
            self.triggers += 1
            self.last_trigger = rec
            fired.append(rec)
        return fired

    def start(
        self,
        preset_id: str,
        grab_fn: Callable[[], Image.Image | None],
        mode: str = "normal",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        self.stop()
        self.load_preset(preset_id)
        self.reflex_mode = mode or "normal"
        self.dry_run = bool(dry_run)
        self.last_error = ""
        self._stop_evt.clear()
        self.running = True

        def _loop() -> None:
            interval = 1.0 / float(self.fps)
            while not self._stop_evt.is_set():
                t0 = time.perf_counter()
                try:
                    if not self.driver.emergency_stop_event.is_set():
                        img = grab_fn()
                        if img is not None:
                            self.evaluate_once(img)
                except RuntimeError as e:
                    self.last_error = str(e)
                    time.sleep(0.25)
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    time.sleep(0.25)
                dt = time.perf_counter() - t0
                if dt < interval:
                    time.sleep(interval - dt)
            self.running = False

        self._thread = threading.Thread(target=_loop, name="omnivision-reflex", daemon=True)
        self._thread.start()
        return {"ok": True, **self.status()}

    def stop(self) -> dict[str, Any]:
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.running = False
        return {"ok": True, **self.status()}

    def set_mode(self, mode: str, dry_run: bool | None = None) -> dict[str, Any]:
        self.reflex_mode = str(mode or "normal").strip()
        if dry_run is not None:
            self.dry_run = bool(dry_run)
        return {"ok": True, "reflexMode": self.reflex_mode, "dryRun": self.dry_run}

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "preset": self.preset_name,
            "reflexMode": self.reflex_mode,
            "dryRun": self.dry_run,
            "fps": self.fps,
            "triggers": self.triggers,
            "lastTrigger": self.last_trigger,
            "lastError": self.last_error,
        }
