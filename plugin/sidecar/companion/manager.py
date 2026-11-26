# -*- coding: utf-8 -*-
"""CompanionManager — 4-mode state machine (passive / watch / game_turn / game_action),
window whitelist lock, hourly token/frame budget breaker, Barge-in pause, and on-demand ocr_crop."""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

from .audio import SystemAudioListener
from .capture import VisionSensor
from .driver import SafeGameDriver
from .reflex import FastReflexEngine

VALID_MODES = ("passive", "watch", "game_turn", "game_action")

MODE_REASONING_EFFORT = {
    "passive": 10,
    "watch": 2,
    "game_turn": 20,
    "game_action": 8,
}


class CompanionManager:
    """Integrates VisionSensor, SystemAudioListener, SafeGameDriver, and FastReflexEngine."""

    def __init__(self, service: Any, dry_run: bool = False):
        self.service = service
        data_dir = os.path.dirname(getattr(service, "frames_dir", ".")) or "."
        frames_dir = getattr(service, "frames_dir", os.path.join(data_dir, "frames"))
        here = os.path.dirname(os.path.abspath(__file__))
        cand_presets = [
            os.path.abspath(os.path.join(here, "..", "..", "..", "presets", "games")),
            os.path.join(data_dir, "presets", "games"),
        ]
        presets_dir = next((p for p in cand_presets if os.path.isdir(p)), cand_presets[0])

        self.sensor = VisionSensor(frames_dir=frames_dir)
        self.audio = SystemAudioListener(data_dir=data_dir)
        self.driver = SafeGameDriver(dry_run=dry_run)
        self.reflex = FastReflexEngine(driver=self.driver, presets_dir=presets_dir)

        self._lock = threading.Lock()
        self.mode = "passive"
        self.window_match = ""
        self.locked_hwnd: int | None = None
        self.locked_title = ""
        self.barge_in_paused = False
        self.barge_in_until = 0.0

        self.sheet_interval_s = 14.0
        self.diff_threshold = 4.5
        self.max_frames_per_hour = 120
        self._sent_timestamps: deque[float] = deque()
        self.total_events_emitted = 0
        self.budget_skips = 0
        self._event_seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=64)
        self._last_transcript_ts = 0.0
        self._last_emit_at = 0.0

        self._stop_evt = threading.Event()
        self._worker: threading.Thread | None = None

    def close(self) -> None:
        self._stop_loop()
        self.reflex.stop()
        self.audio.stop()
        self.driver.close()

    def _prune_hour_window(self, now: float) -> int:
        cutoff = now - 3600.0
        while self._sent_timestamps and self._sent_timestamps[0] < cutoff:
            self._sent_timestamps.popleft()
        return len(self._sent_timestamps)

    def can_emit_frame(self, now: float | None = None) -> tuple[bool, int]:
        t = time.time() if now is None else now
        used = self._prune_hour_window(t)
        return used < self.max_frames_per_hour, used

    def record_frame_emitted(self, now: float | None = None) -> int:
        t = time.time() if now is None else now
        self._sent_timestamps.append(t)
        self.total_events_emitted += 1
        return self._prune_hour_window(t)

    def _resolve_target_region(self) -> tuple[int, int, int, int] | None:
        if self.window_match:
            return self.service.resolve(f"window:{self.window_match}")
        return self.service.resolve("foreground")

    def _resolve_target_hwnd(self) -> tuple[int | None, str]:
        if not self.window_match:
            return None, ""
        try:
            mod = __import__(self.service.__class__.__module__)
            list_fn = getattr(mod, "list_windows", None)
            if callable(list_fn):
                kw = self.window_match.lower()
                for w in list_fn():
                    title = str(w.get("title") or "")
                    if kw in title.lower():
                        return int(w.get("hwnd") or 0) or None, title
        except Exception:
            pass
        return None, self.window_match

    def set_mode(self, body: dict[str, Any]) -> dict[str, Any]:
        b = body if isinstance(body, dict) else {}
        mode = str(b.get("mode") or "passive").strip().lower()
        if mode not in VALID_MODES:
            return {"ok": False, "error": f"mode must be one of {VALID_MODES}"}

        try:
            new_max_hr = self.max_frames_per_hour
            new_interval = self.sheet_interval_s
            new_diff = self.diff_threshold
            if b.get("maxFramesPerHour") is not None:
                new_max_hr = max(1, min(600, int(b["maxFramesPerHour"])))
            if b.get("sheetIntervalS") is not None:
                new_interval = max(4.0, min(120.0, float(b["sheetIntervalS"])))
            if b.get("diffThreshold") is not None:
                new_diff = max(0.5, min(50.0, float(b["diffThreshold"])))
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": f"invalid numeric parameter: {e}"}

        self._stop_loop()
        if mode == "passive":
            self.reflex.stop()
            self.audio.stop()
            self.driver.release_all()
            self.mode = "passive"
            self.window_match = ""
            self.locked_hwnd = None
            self.locked_title = ""
            self.driver.lock_window(None, "")
            return {"ok": True, **self.status()}

        self.max_frames_per_hour = new_max_hr
        self.sheet_interval_s = new_interval
        self.diff_threshold = new_diff
        self.window_match = str(b.get("windowMatch") or self.window_match or "").strip()

        hwnd, title = self._resolve_target_hwnd()
        self.locked_hwnd = hwnd
        self.locked_title = title or self.window_match
        self.driver.lock_window(self.locked_hwnd, self.locked_title)

        self.mode = mode
        audio_res = None
        if mode == "watch" and b.get("enableAudio") is not False:
            audio_res = self.audio.start()
        elif mode != "watch":
            self.audio.stop()

        reflex_err = ""
        if mode != "game_action":
            self.reflex.stop()
        elif b.get("reflexPreset"):
            try:
                self.reflex.start(
                    str(b["reflexPreset"]),
                    grab_fn=self._grab_current_image,
                    mode=str(b.get("reflexMode") or "normal"),
                    dry_run=bool(b.get("reflexDryRun", True)),
                )
            except Exception as e:
                reflex_err = str(e)

        if b.get("startLoop", True):
            self._start_loop()

        out = {"ok": True, **self.status()}
        if audio_res is not None:
            out["audioStart"] = audio_res
        if reflex_err:
            out["reflexWarning"] = reflex_err
        return out

    def _grab_current_image(self, allow_fallback_on_none: bool = False):
        region = self._resolve_target_region()
        mod = __import__(self.service.__class__.__module__)
        grab_fn = getattr(mod, "grab", None)
        return self.sensor.grab_region(
            region=region,
            fallback_grab=grab_fn,
            allow_fallback_on_none=allow_fallback_on_none,
        )

    def _start_loop(self) -> None:
        self._stop_evt.clear()
        self._last_emit_at = 0.0
        self.sensor.clear_keyframes()
        self._worker = threading.Thread(target=self._loop, name="omnivision-companion", daemon=True)
        self._worker.start()

    def _stop_loop(self) -> None:
        self._stop_evt.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self._worker = None

    def try_emit_event(self, payload: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
        t = time.time() if now is None else now
        ok_budget, used = self.can_emit_frame(t)
        if not ok_budget:
            self.budget_skips += 1
            return None
        self.record_frame_emitted(t)
        return self._push_event({
            **payload,
            "budgetUsed": used + 1,
            "budgetMax": self.max_frames_per_hour,
        })

    def _loop(self) -> None:
        while not self._stop_evt.is_set() and self.mode != "passive":
            now = time.time()
            if self.barge_in_paused and (self.barge_in_until == 0.0 or now < self.barge_in_until):
                time.sleep(0.3)
                continue
            elif self.barge_in_paused and self.barge_in_until > 0.0 and now >= self.barge_in_until:
                self.barge_in_paused = False

            try:
                img = self._grab_current_image(allow_fallback_on_none=False)
                if img is not None:
                    changed, _ = self.sensor.is_scene_changed(img, threshold=self.diff_threshold)
                    if changed:
                        self.sensor.push_keyframe(img)

                interval = self.sheet_interval_s if self.mode == "watch" else max(2.0, min(self.sheet_interval_s, 4.0))
                if now - self._last_emit_at >= interval:
                    has_frames = len(self.sensor.keyframes) > 0
                    transcript = self.audio.format_recent(since_ts=self._last_transcript_ts, limit=12)
                    if has_frames or transcript:
                        ok_budget, _ = self.can_emit_frame(now)
                        if not ok_budget:
                            self.budget_skips += 1
                            self._last_emit_at = now
                        else:
                            sheet_path = ""
                            if has_frames:
                                if self.mode == "watch":
                                    sheet_path = self.sensor.save_contact_sheet()
                                else:
                                    _, latest_img = self.sensor.keyframes[-1]
                                    sheet_path = self.service.save_frame(latest_img, "game")
                                self.sensor.clear_keyframes()
                            self._last_emit_at = now
                            if transcript:
                                self._last_transcript_ts = now
                            self.try_emit_event({
                                "mode": self.mode,
                                "frame": sheet_path,
                                "isContactSheet": self.mode == "watch",
                                "transcript": transcript,
                                "window": self.locked_title or self.window_match or "foreground",
                                "reasoningEffort": MODE_REASONING_EFFORT.get(self.mode, 10),
                            }, now=now)
            except Exception:
                pass
            time.sleep(1.0 if self.mode == "watch" else 0.5)

    def _push_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._event_seq += 1
            ev = {"seq": self._event_seq, "ts": round(time.time(), 3), **payload}
            self._events.append(ev)
        return ev

    def poll_events(self, after: int = 0, limit: int = 5) -> dict[str, Any]:
        after_i = int(after or 0)
        lim = max(1, min(20, int(limit or 5)))
        with self._lock:
            evs = [e for e in self._events if e["seq"] > after_i][:lim]
            cur_seq = self._event_seq
        return {"ok": True, "seq": cur_seq, "events": evs}

    def capture_contact_sheet_now(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        b = body if isinstance(body, dict) else {}
        try:
            img = self._grab_current_image(allow_fallback_on_none=True)
            if img is not None:
                self.sensor.push_keyframe(img)
        except Exception as e:
            if not self.sensor.keyframes:
                return {"ok": False, "error": f"capture failed: {e}"}
        if not self.sensor.keyframes:
            return {"ok": False, "error": "No frame captured yet"}
        path = self.sensor.save_contact_sheet()
        since_ts = float(b.get("sinceTs") or 0.0)
        transcript = self.audio.format_recent(since_ts=since_ts, limit=int(b.get("limit") or 12))
        return {
            "ok": True,
            "mode": self.mode,
            "frame": path,
            "framesPacked": len(self.sensor.keyframes),
            "transcript": transcript,
            "window": self.locked_title or self.window_match or "foreground",
            "reasoningEffort": MODE_REASONING_EFFORT.get(self.mode, 2),
        }

    def barge_in(self, action: str = "pause", duration_s: float = 20.0) -> dict[str, Any]:
        op = str(action or "pause").lower()
        if op == "pause":
            self.barge_in_paused = True
            self.barge_in_until = (time.time() + float(duration_s)) if duration_s > 0 else 0.0
        else:
            self.barge_in_paused = False
            self.barge_in_until = 0.0
        return {"ok": True, "paused": self.barge_in_paused, "until": self.barge_in_until}

    def game_act(self, body: dict[str, Any]) -> dict[str, Any]:
        b = body if isinstance(body, dict) else {}
        op = str(b.get("action") or "").strip().lower()
        dry_override = b.get("dryRun")
        if dry_override is not None:
            dry_override = bool(dry_override)
        try:
            if op == "emergency_stop":
                self.reflex.stop()
                return self.driver.trigger_emergency_stop(str(b.get("reason") or "API"))
            if op == "reset_stop":
                return self.driver.reset_stop()
            if op == "release_all":
                return {"ok": True, "released": self.driver.release_all()}
            if op == "tap":
                if "key" not in b or not str(b.get("key") or "").strip():
                    return {"ok": False, "error": "action=tap requires 'key'"}
                return self.driver.tap_key(b["key"], int(b.get("durationMs") or 60), dry_run_override=dry_override)
            if op == "hold":
                if "key" not in b or not str(b.get("key") or "").strip():
                    return {"ok": False, "error": "action=hold requires 'key'"}
                return self.driver.hold_key(b["key"], int(b.get("durationMs") or 250), dry_run_override=dry_override)
            if op == "mouse_rel":
                return self.driver.move_mouse_rel(
                    int(b.get("dx") or 0),
                    int(b.get("dy") or 0),
                    int(b.get("steps") or 5),
                    dry_run_override=dry_override,
                )
            if op == "mouse_click":
                return self.driver.mouse_click(
                    str(b.get("button") or "left"), int(b.get("holdMs") or 50), dry_run_override=dry_override
                )
            if op == "sequence":
                return self.driver.run_sequence(b.get("steps") or [], dry_run_override=dry_override)
            return {"ok": False, "error": f"Unknown game_act action: {op!r}"}
        except (KeyError, ValueError, TypeError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}

    def reflex_command(self, op: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        b = body if isinstance(body, dict) else {}
        action = str(op or "status").strip().lower()
        try:
            if action == "list":
                return {"ok": True, "presets": self.reflex.list_presets()}
            if action == "start":
                preset_id = str(b.get("preset") or b.get("id") or "").strip()
                if not preset_id:
                    return {"ok": False, "error": "preset id is required"}
                dry_val = b.get("dryRun", True)
                return self.reflex.start(
                    preset_id,
                    grab_fn=self._grab_current_image,
                    mode=str(b.get("mode") or "normal"),
                    dry_run=bool(dry_val),
                )
            if action == "stop":
                return self.reflex.stop()
            if action == "set_mode":
                dry_opt = None if "dryRun" not in b else bool(b["dryRun"])
                return self.reflex.set_mode(str(b.get("mode") or "normal"), dry_run=dry_opt)
            if action == "status":
                return {"ok": True, **self.reflex.status()}
            return {"ok": False, "error": f"Unknown reflex command: {action!r}"}
        except (FileNotFoundError, ValueError, TypeError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}

    def ocr_crop(self, body: dict[str, Any]) -> dict[str, Any]:
        b = body if isinstance(body, dict) else {}
        rect = b.get("rect")
        if not (isinstance(rect, list) and len(rect) == 4):
            return {"ok": False, "error": "rect [x, y, w, h] is required"}
        try:
            x, y, w, h = [int(float(v)) for v in rect]
            scale = max(1.0, min(3.0, float(b.get("scale") or 2.0)))
            min_score = float(b.get("minScore") or 0.45)
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": f"invalid rect/scale/minScore: {e}"}
        if w <= 2 or h <= 2:
            return {"ok": False, "error": "rect width and height must be > 2"}
        try:
            mod = __import__(self.service.__class__.__module__)
            grab_fn = getattr(mod, "grab", None)
            img = self.sensor.grab_region(
                region=(x, y, w, h),
                fallback_grab=grab_fn,
                allow_fallback_on_none=True,
            )
        except Exception as e:
            return {"ok": False, "error": f"failed to capture crop region: {e}"}
        if img is None:
            return {"ok": False, "error": "failed to capture crop region"}
        if scale > 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)), resample=3)
        crop_path = self.service.save_frame(img, "ocr_crop")
        if self.service.ocr is None:
            return {"ok": True, "frame": crop_path, "text": "", "elements": [], "note": "OCR engine disabled"}
        try:
            res = self.service.ocr.run(img)
            if isinstance(res, tuple) and len(res) == 2:
                raw_els, ocr_meta = res
            else:
                raw_els, ocr_meta = res, {}
            filtered = [
                e for e in (raw_els or [])
                if isinstance(e, dict) and float(e.get("score") if e.get("score") is not None else 1.0) >= min_score
            ]
        except Exception as e:
            return {"ok": False, "error": f"OCR failed: {e}", "frame": crop_path}
        texts = [str(e.get("text") or e.get("label") or "") for e in filtered if e.get("text") or e.get("label")]
        return {
            "ok": True,
            "rect": [x, y, w, h],
            "scale": scale,
            "frame": crop_path,
            "text": " ".join(texts),
            "elements": filtered,
            "ocrMeta": ocr_meta,
        }

    def status(self) -> dict[str, Any]:
        used = self._prune_hour_window(time.time())
        est_tokens_hr = used * 1224
        return {
            "mode": self.mode,
            "recommendedReasoningEffort": MODE_REASONING_EFFORT.get(self.mode, 10),
            "windowMatch": self.window_match,
            "lockedHwnd": self.locked_hwnd,
            "lockedTitle": self.locked_title,
            "bargeInPaused": self.barge_in_paused,
            "sheetIntervalS": self.sheet_interval_s,
            "diffThreshold": self.diff_threshold,
            "budget": {
                "usedThisHour": used,
                "maxPerHour": self.max_frames_per_hour,
                "budgetSkips": self.budget_skips,
                "totalEmitted": self.total_events_emitted,
                "estTokensThisHour": est_tokens_hr,
            },
            "sensor": self.sensor.status(),
            "audio": self.audio.status(),
            "driver": self.driver.status(),
            "reflex": self.reflex.status(),
            "eventSeq": self._event_seq,
        }
