# -*- coding: utf-8 -*-
"""SafeGameDriver — Hardware scan-code keyboard & relative 3D mouse driver for Windows games,
with 3-layer safety locks:
  1) Foreground HWND lock (auto-releases all keys if focus leaves target window)
  2) Hard duration caps (single key hold <= 5000ms, sequence total <= 30000ms, max 20 steps)
  3) Global F12 emergency stop poller + programmatic release_all()
Supports thread-safe per-call dry_run_override without mutating shared driver state."""
from __future__ import annotations

import ctypes
import os
import threading
import time
from typing import Any

SCAN_CODES: dict[str, int] = {
    "esc": 0x01, "escape": 0x01,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "-": 0x0C, "=": 0x0D, "backspace": 0x0E, "tab": 0x0F,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14,
    "y": 0x15, "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19,
    "[": 0x1A, "]": 0x1B, "enter": 0x1C, "return": 0x1C, "ctrl": 0x1D, "lctrl": 0x1D,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22,
    "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26, ";": 0x27, "'": 0x28, "`": 0x29,
    "shift": 0x2A, "lshift": 0x2A, "\\": 0x2B,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "n": 0x31, "m": 0x32, ",": 0x33, ".": 0x34, "/": 0x35, "rshift": 0x36,
    "alt": 0x38, "lalt": 0x38, "space": 0x39, "caps": 0x3A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F,
    "f6": 0x40, "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44,
    "f11": 0x57, "f12": 0x58,
    "up": 0xC8, "down": 0xD0, "left": 0xCB, "right": 0xCD,
}

EXTENDED_SCANS = {0xC8, 0xD0, 0xCB, 0xCD}

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
VK_F12 = 0x7B

MAX_HOLD_MS = 5000
MAX_SEQUENCE_MS = 30000
MAX_SEQUENCE_STEPS = 20

if os.name == "nt":
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]
else:
    _user32 = None


class SafeGameDriver:
    """Hardware scan-code game actuator with hard safety bounds and emergency stop."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = bool(dry_run or os.name != "nt")
        self.locked_hwnd: int | None = None
        self.locked_title: str = ""
        self.emergency_stop_event = threading.Event()
        self.held_scans: set[int] = set()
        self._lock = threading.Lock()
        self._stop_monitor = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self.action_log: list[dict[str, Any]] = []
        self._foreground_override: int | None = None
        if os.name == "nt" and not self.dry_run:
            self._start_f12_monitor()

    def _effective_dry(self, override: bool | None) -> bool:
        if override is None:
            return self.dry_run
        return bool(override) or (os.name != "nt")

    def _start_f12_monitor(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_monitor.clear()

        def _watch_f12() -> None:
            while not self._stop_monitor.is_set():
                try:
                    if _user32 is not None and (_user32.GetAsyncKeyState(VK_F12) & 0x8000):
                        self.trigger_emergency_stop("F12_HOTKEY")
                except Exception:
                    pass
                time.sleep(0.05)

        self._monitor_thread = threading.Thread(target=_watch_f12, name="omnivision-f12-estop", daemon=True)
        self._monitor_thread.start()

    def close(self) -> None:
        self._stop_monitor.set()
        self.release_all()

    def lock_window(self, hwnd: int | None, title: str = "") -> None:
        self.locked_hwnd = int(hwnd) if hwnd else None
        self.locked_title = str(title or "")

    def _current_foreground_hwnd(self) -> int | None:
        if self._foreground_override is not None:
            return self._foreground_override
        if _user32 is not None:
            try:
                return int(_user32.GetForegroundWindow())
            except Exception:
                return None
        return None

    def _assert_safe(self, check_focus: bool = True) -> None:
        if self.emergency_stop_event.is_set():
            self.release_all()
            raise RuntimeError("EMERGENCY_STOPPED: call reset_stop before resuming game input")
        if check_focus and self.locked_hwnd:
            fg = self._current_foreground_hwnd()
            if fg is not None and fg != self.locked_hwnd:
                self.release_all()
                raise RuntimeError(
                    f"WINDOW_FOCUS_LOST: locked hwnd={self.locked_hwnd} ({self.locked_title}), foreground={fg}"
                )

    def trigger_emergency_stop(self, reason: str = "MANUAL") -> dict:
        self.emergency_stop_event.set()
        released = self.release_all()
        rec = {"event": "emergency_stop", "reason": reason, "released": released, "ts": round(time.time(), 3)}
        self.action_log.append(rec)
        return {"ok": True, **rec}

    def reset_stop(self) -> dict:
        self.emergency_stop_event.clear()
        return {"ok": True, "emergencyStopped": False}

    @staticmethod
    def resolve_scan_code(key: str | int) -> int:
        if isinstance(key, int):
            if key <= 0 or key > 0xFF:
                raise ValueError(f"Scan code integer out of range (1..255): {key}")
            return key
        k = str(key or "").strip().lower()
        if not k:
            raise ValueError("key must not be empty")
        if k in SCAN_CODES:
            return SCAN_CODES[k]
        if k.startswith("0x"):
            val = int(k, 16)
            if val <= 0 or val > 0xFF:
                raise ValueError(f"Scan code hex out of range: {key!r}")
            return val
        raise ValueError(f"Unknown key name for scan code: {key!r}")

    def _emit_key(self, scan_code: int, key_up: bool = False, dry_run: bool | None = None) -> None:
        eff_dry = self.dry_run if dry_run is None else dry_run
        if eff_dry or _user32 is None:
            self.action_log.append(
                {"op": "key_up" if key_up else "key_down", "scan": hex(scan_code), "ts": round(time.time(), 4)}
            )
            return
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
        sc = scan_code
        if scan_code in EXTENDED_SCANS:
            flags |= KEYEVENTF_EXTENDEDKEY
            sc = scan_code & 0x7F
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.union.ki = _KEYBDINPUT(0, sc, flags, 0, None)
        _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def _emit_mouse(self, dx: int = 0, dy: int = 0, flags: int = MOUSEEVENTF_MOVE, dry_run: bool | None = None) -> None:
        eff_dry = self.dry_run if dry_run is None else dry_run
        if eff_dry or _user32 is None:
            self.action_log.append({"op": "mouse", "dx": dx, "dy": dy, "flags": flags, "ts": round(time.time(), 4)})
            return
        inp = _INPUT(type=INPUT_MOUSE)
        inp.union.mi = _MOUSEINPUT(int(dx), int(dy), 0, int(flags), 0, None)
        _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def release_all(self) -> list[str]:
        released: list[str] = []
        with self._lock:
            for sc in list(self.held_scans):
                try:
                    self._emit_key(sc, key_up=True, dry_run=self.dry_run)
                except Exception:
                    pass
                released.append(hex(sc))
            self.held_scans.clear()
        return released

    def hold_key(self, key: str | int, duration_ms: int = 120, dry_run_override: bool | None = None) -> dict:
        self._assert_safe()
        sc = self.resolve_scan_code(key)
        raw_ms = int(duration_ms)
        clamped_ms = max(20, min(raw_ms, MAX_HOLD_MS))
        eff_dry = self._effective_dry(dry_run_override)
        with self._lock:
            if not eff_dry:
                self.held_scans.add(sc)
            self._emit_key(sc, key_up=False, dry_run=eff_dry)
        try:
            sleep_s = 0.005 if eff_dry else (clamped_ms / 1000.0)
            end_t = time.perf_counter() + sleep_s
            while time.perf_counter() < end_t:
                self._assert_safe()
                time.sleep(min(0.02, max(0.001, end_t - time.perf_counter())))
        finally:
            with self._lock:
                self._emit_key(sc, key_up=True, dry_run=eff_dry)
                if not eff_dry:
                    self.held_scans.discard(sc)
        return {
            "ok": True,
            "action": "hold",
            "key": str(key),
            "scanCode": hex(sc),
            "durationMs": clamped_ms,
            "clamped": clamped_ms != raw_ms,
            "dryRun": eff_dry,
        }

    def tap_key(self, key: str | int, press_ms: int = 60, dry_run_override: bool | None = None) -> dict:
        res = self.hold_key(key, duration_ms=max(20, min(int(press_ms), 500)), dry_run_override=dry_run_override)
        res["action"] = "tap"
        return res

    def mouse_click(self, button: str = "left", hold_ms: int = 50, dry_run_override: bool | None = None) -> dict:
        self._assert_safe()
        btn = str(button or "left").lower()
        mapping = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }
        if btn not in mapping:
            raise ValueError(f"Unknown mouse button: {button}")
        down_f, up_f = mapping[btn]
        clamped = max(20, min(int(hold_ms), 2000))
        eff_dry = self._effective_dry(dry_run_override)
        self._emit_mouse(0, 0, down_f, dry_run=eff_dry)
        try:
            sleep_s = 0.005 if eff_dry else clamped / 1000.0
            end_t = time.perf_counter() + sleep_s
            while time.perf_counter() < end_t:
                self._assert_safe()
                time.sleep(min(0.02, max(0.001, end_t - time.perf_counter())))
        finally:
            self._emit_mouse(0, 0, up_f, dry_run=eff_dry)
        return {"ok": True, "action": "mouse_click", "button": btn, "holdMs": clamped, "dryRun": eff_dry}

    def move_mouse_rel(
        self, dx: int, dy: int, steps: int = 5, step_delay_ms: int = 10, dry_run_override: bool | None = None
    ) -> dict:
        self._assert_safe()
        steps = max(1, min(int(steps), 40))
        dx_i, dy_i = int(dx), int(dy)
        eff_dry = self._effective_dry(dry_run_override)
        rem_x, rem_y = dx_i, dy_i
        for i in range(steps):
            self._assert_safe()
            left = steps - i
            sx = int(round(rem_x / left))
            sy = int(round(rem_y / left))
            rem_x -= sx
            rem_y -= sy
            self._emit_mouse(sx, sy, MOUSEEVENTF_MOVE, dry_run=eff_dry)
            if i < steps - 1:
                time.sleep(0.002 if eff_dry else max(0.002, step_delay_ms / 1000.0))
        return {"ok": True, "action": "mouse_rel", "dx": dx_i, "dy": dy_i, "steps": steps, "dryRun": eff_dry}

    def run_sequence(self, steps: list[dict[str, Any]], dry_run_override: bool | None = None) -> dict:
        self._assert_safe()
        if not isinstance(steps, list) or not steps:
            return {"ok": False, "error": "sequence steps must be a non-empty list"}
        if len(steps) > MAX_SEQUENCE_STEPS:
            return {"ok": False, "error": f"sequence exceeds max {MAX_SEQUENCE_STEPS} steps"}

        eff_dry = self._effective_dry(dry_run_override)
        t0 = time.perf_counter()
        results: list[dict] = []
        for idx, st in enumerate(steps):
            if not isinstance(st, dict):
                return {"ok": False, "error": f"sequence step {idx} must be an object", "completed": results}
            if not eff_dry and (time.perf_counter() - t0) * 1000.0 >= MAX_SEQUENCE_MS:
                self.release_all()
                return {
                    "ok": False,
                    "error": f"SEQUENCE_TIMEOUT: exceeded {MAX_SEQUENCE_MS}ms at step {idx}",
                    "completed": results,
                }
            op = str(st.get("action") or st.get("op") or "").lower()
            if op == "tap":
                if "key" not in st:
                    return {"ok": False, "error": f"step {idx} (tap) missing required 'key'", "completed": results}
                results.append(
                    self.tap_key(st["key"], int(st.get("durationMs") or st.get("pressMs") or 60), dry_run_override=eff_dry)
                )
            elif op == "hold":
                if "key" not in st:
                    return {"ok": False, "error": f"step {idx} (hold) missing required 'key'", "completed": results}
                results.append(
                    self.hold_key(st["key"], int(st.get("durationMs") or 250), dry_run_override=eff_dry)
                )
            elif op == "mouse_rel":
                results.append(
                    self.move_mouse_rel(
                        int(st.get("dx") or 0),
                        int(st.get("dy") or 0),
                        int(st.get("steps") or 5),
                        dry_run_override=eff_dry,
                    )
                )
            elif op == "mouse_click":
                results.append(
                    self.mouse_click(
                        str(st.get("button") or "left"), int(st.get("holdMs") or 50), dry_run_override=eff_dry
                    )
                )
            elif op == "wait":
                ms = max(10, min(int(st.get("ms") or st.get("durationMs") or 100), 5000))
                time.sleep(0.005 if eff_dry else ms / 1000.0)
                results.append({"ok": True, "action": "wait", "ms": ms})
            else:
                return {"ok": False, "error": f"Unsupported sequence op at step {idx}: {op!r}", "completed": results}
        return {
            "ok": True,
            "action": "sequence",
            "steps": len(results),
            "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
            "results": results,
            "dryRun": eff_dry,
        }

    def status(self) -> dict:
        return {
            "dryRun": self.dry_run,
            "lockedHwnd": self.locked_hwnd,
            "lockedTitle": self.locked_title,
            "emergencyStopped": self.emergency_stop_event.is_set(),
            "heldKeys": [hex(x) for x in sorted(self.held_scans)],
            "f12MonitorActive": bool(self._monitor_thread and self._monitor_thread.is_alive()),
        }
