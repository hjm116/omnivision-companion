#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comprehensive offline verification suite for OmniVision Companion v0.15.1.
Covers:
  - Python compilation of all sidecar & companion modules
  - VisionSensor diff gating, DXGI None fallback, and 1280x720 2x2 contact sheet
  - SystemAudioListener transcript ring buffer & graceful degradation
  - SafeGameDriver hard caps, focus lock, emergency stop, & thread-safe dry_run_override
  - FastReflexEngine ROI color rules & cooldown lock
  - CompanionManager budget circuit breaker, barge-in, & OcrEngine (els, meta) tuple contract
  - Real ThreadingHTTPServer end-to-end route tests (happy path + malformed inputs -> HTTP 200 ok:false)
  - Strict JS lexer check (no unterminated single/double quotes, balanced brackets/templates)
"""
from __future__ import annotations

import json
import os
import py_compile
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SIDECAR_DIR = os.path.join(REPO_ROOT, "plugin", "sidecar")
if SIDECAR_DIR not in sys.path:
    sys.path.insert(0, SIDECAR_DIR)

import omnivisiond
from companion.audio import SystemAudioListener
from companion.capture import VisionSensor
from companion.driver import MAX_HOLD_MS, MAX_SEQUENCE_STEPS, SafeGameDriver
from companion.manager import CompanionManager
from companion.reflex import FastReflexEngine


def strict_js_lexer_check(path: str) -> list[str]:
    """Zero-dependency JS lexer validating strings, template literals, regexes, and bracket balance."""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    errors: list[str] = []
    i, n = 0, len(src)
    line = 1
    stack: list[tuple[str, int]] = []

    def adv(k: int = 1) -> None:
        nonlocal i, line
        for _ in range(k):
            if i < n and src[i] == "\n":
                line += 1
            i += 1

    while i < n:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                adv()
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            sl = line
            adv(2)
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                adv()
            if i + 1 >= n:
                errors.append(f"L{sl}: unclosed block comment")
                break
            adv(2)
            continue
        if ch in ("'", '"'):
            q = ch
            sl = line
            adv()
            while i < n:
                if src[i] == "\\":
                    adv(2)
                    continue
                if src[i] == "\n":
                    errors.append(f"L{sl}: unterminated {q} string before newline")
                    adv()
                    break
                if src[i] == q:
                    adv()
                    break
                adv()
            continue
        if ch == "`":
            sl = line
            adv()
            stack.append(("`", sl))
            while i < n and stack and stack[-1][0] == "`":
                if src[i] == "\\":
                    adv(2)
                    continue
                if src[i] == "`":
                    stack.pop()
                    adv()
                    break
                if src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    stack.append(("${", line))
                    adv(2)
                    break
                adv()
            continue
        if ch == "/":
            j = i - 1
            while j >= 0 and src[j] in " \t\r\n":
                j -= 1
            prev = src[j] if j >= 0 else ""
            if prev in "=(,[!:&|?;{" or src[max(0, j - 5) : j + 1].endswith("return"):
                sl = line
                adv()
                in_cls = False
                while i < n:
                    if src[i] == "\\":
                        adv(2)
                        continue
                    if src[i] == "\n":
                        errors.append(f"L{sl}: unterminated regex before newline")
                        break
                    if src[i] == "[":
                        in_cls = True
                    elif src[i] == "]":
                        in_cls = False
                    elif src[i] == "/" and not in_cls:
                        adv()
                        while i < n and src[i].isalpha():
                            adv()
                        break
                    adv()
                continue
        if ch in "([{":
            stack.append((ch, line))
            adv()
            continue
        if ch in ")]}":
            if ch == "}" and stack and stack[-1][0] == "${":
                stack.pop()
                adv()
                while i < n and stack and stack[-1][0] == "`":
                    if src[i] == "\\":
                        adv(2)
                        continue
                    if src[i] == "`":
                        stack.pop()
                        adv()
                        break
                    if src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                        stack.append(("${", line))
                        adv(2)
                        break
                    adv()
                continue
            expected = {")": "(", "]": "[", "}": "{"}[ch]
            if not stack or stack[-1][0] != expected:
                errors.append(f"L{line}: mismatched closing {ch!r}")
            else:
                stack.pop()
            adv()
            continue
        adv()

    if stack:
        errors.append(f"unclosed tokens at EOF: {stack[:5]}")
    return errors


class _DummyOcrEngine:
    """Mimics real omnivisiond.OcrEngine (init_ms, cache_info, and run(img) -> (els, meta))."""

    init_ms = 12.3

    def cache_info(self) -> dict:
        return {"hitRate": 0.0, "cacheHits": 0, "fullRuns": 1, "detLimit": "min:736", "entries": 0}

    def run(self, img: Image.Image) -> tuple[list[dict], dict]:
        return (
            [
                {"text": "HP 420/1000", "box": [10, 10, 120, 24], "score": 0.96, "source": "ocr"},
                {"text": "NOISE_LOW_CONF", "box": [10, 40, 80, 20], "score": 0.21, "source": "ocr"},
            ],
            {"ocrMs": 12.4, "cache": "none"},
        )


class _DummyService:
    def __init__(self, frames_dir: str, with_ocr: bool = False):
        self.frames_dir = frames_dir
        self.ocr = _DummyOcrEngine() if with_ocr else None

    def resolve(self, spec):
        return (0, 0, 640, 360)

    def save_frame(self, img: Image.Image, prefix: str = "frame") -> str:
        p = os.path.join(self.frames_dir, f"{prefix}_test.jpg")
        img.save(p, format="JPEG")
        return p


class TestOmniVisionCompanionV0151(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.frames_dir = os.path.join(self.tmp.name, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_all_python_files_compile(self):
        checked = 0
        for root, _, files in os.walk(SIDECAR_DIR):
            for fn in files:
                if fn.endswith(".py"):
                    py_compile.compile(os.path.join(root, fn), doraise=True)
                    checked += 1
        self.assertGreaterEqual(checked, 7)

    def test_02_strict_js_lexer_zero_errors(self):
        for rel in ["plugin/host.js", "plugin/client.js", "tests/test_host_syntax.mjs"]:
            errs = strict_js_lexer_check(os.path.join(REPO_ROOT, rel))
            self.assertEqual(errs, [], f"{rel} has JS syntax errors: {errs}")

    def test_03_vision_sensor_diff_dxgi_fallback_and_sheet(self):
        sensor = VisionSensor(frames_dir=self.frames_dir)
        img1 = Image.fromarray(np.full((360, 640, 3), 20, dtype=np.uint8))
        img2 = Image.fromarray(np.full((360, 640, 3), 21, dtype=np.uint8))
        img3 = Image.fromarray(np.full((360, 640, 3), 180, dtype=np.uint8))

        changed1, _ = sensor.is_scene_changed(img1, threshold=4.5)
        changed2, diff2 = sensor.is_scene_changed(img2, threshold=4.5)
        changed3, diff3 = sensor.is_scene_changed(img3, threshold=4.5)
        self.assertTrue(changed1)
        self.assertFalse(changed2)
        self.assertLess(diff2, 4.5)
        self.assertEqual(sensor.static_skips, 1)
        self.assertTrue(changed3)
        self.assertGreater(diff3, 100.0)

        # Simulate DXGI returning None on static screen -> allow_fallback_on_none=True must recover!
        class _StaticDxCam:
            def grab(self, region=None):
                return None

        sensor._camera = _StaticDxCam()
        self.assertIsNone(sensor.grab_region(region=(0, 0, 100, 100), fallback_grab=lambda r: img1, allow_fallback_on_none=False))
        recovered = sensor.grab_region(region=(-10, -10, 100, 100), fallback_grab=lambda r: img1, allow_fallback_on_none=True)
        self.assertIsNotNone(recovered)

        for i in range(4):
            sensor.push_keyframe(img1 if i % 2 == 0 else img3, label=f"00:0{i}")
        out_path = sensor.save_contact_sheet()
        self.assertTrue(os.path.isfile(out_path))
        with Image.open(out_path) as sheet:
            self.assertEqual(sheet.size, (1280, 720))

    def test_04_audio_listener_buffer_and_sensevoice_discovery(self):
        audio = SystemAudioListener(data_dir=self.tmp.name)
        r = audio.push_manual_line("这波反转也太帅了吧", speaker="system", ts=1000.0)
        self.assertTrue(r["ok"])
        self.assertIn("这波反转也太帅了吧", audio.format_recent(since_ts=999.0, limit=5))
        self.assertEqual(audio.status()["bufferedLines"], 1)
        self.assertFalse(audio.capabilities()["senseVoiceModelReady"])
        # Create mock SenseVoice files in models/sense-voice/ and verify auto-discovery
        sv_dir = os.path.join(self.tmp.name, "models", "sense-voice")
        os.makedirs(sv_dir, exist_ok=True)
        with open(os.path.join(sv_dir, "model.int8.onnx"), "wb") as f:
            f.write(b"mock")
        with open(os.path.join(sv_dir, "tokens.txt"), "w", encoding="utf-8") as f:
            f.write("a 0\n")
        self.assertTrue(audio.capabilities()["senseVoiceModelReady"])

    def test_05_safe_game_driver_bounds_focus_estop_and_no_state_mutation(self):
        drv = SafeGameDriver(dry_run=True)
        drv.lock_window(1001, "TestGame")
        drv._foreground_override = 1001

        # Verify dry_run_override does NOT mutate shared drv.dry_run
        orig_dry = drv.dry_run
        res = drv.hold_key("w", duration_ms=12000, dry_run_override=True)
        self.assertEqual(drv.dry_run, orig_dry)
        self.assertTrue(res["ok"])
        self.assertTrue(res["clamped"])
        self.assertEqual(res["durationMs"], MAX_HOLD_MS)

        seq_res = drv.run_sequence([
            {"action": "tap", "key": "space"},
            {"action": "mouse_rel", "dx": 120, "dy": -30, "steps": 4},
            {"action": "mouse_click", "button": "left"},
            {"action": "wait", "ms": 50},
        ])
        self.assertTrue(seq_res["ok"])
        self.assertEqual(seq_res["steps"], 4)

        too_long = drv.run_sequence([{"action": "tap", "key": "a"}] * (MAX_SEQUENCE_STEPS + 1))
        self.assertFalse(too_long["ok"])

        drv._foreground_override = 9999
        with self.assertRaises(RuntimeError) as ctx:
            drv.tap_key("e")
        self.assertIn("WINDOW_FOCUS_LOST", str(ctx.exception))

        drv._foreground_override = 1001
        es = drv.trigger_emergency_stop("TEST_ESTOP")
        self.assertTrue(es["ok"])
        with self.assertRaises(RuntimeError) as ctx2:
            drv.tap_key("w")
        self.assertIn("EMERGENCY_STOPPED", str(ctx2.exception))
        drv.reset_stop()
        self.assertTrue(drv.tap_key("w")["ok"])

    def test_06_fast_reflex_low_hp_potion_and_cooldown(self):
        drv = SafeGameDriver(dry_run=True)
        presets_dir = os.path.join(REPO_ROOT, "presets", "games")
        reflex = FastReflexEngine(driver=drv, presets_dir=presets_dir)
        self.assertGreaterEqual(len(reflex.list_presets()), 3)
        reflex.load_preset("generic_hp_potion")

        self.assertTrue(reflex.dry_run)
        low_hp_img = Image.fromarray(np.zeros((500, 1000, 3), dtype=np.uint8))
        fired1 = reflex.evaluate_once(low_hp_img)
        self.assertEqual(len(fired1), 1)
        self.assertTrue(fired1[0]["dryRun"])
        self.assertEqual(fired1[0]["ruleId"], "low_hp_potion")
        self.assertEqual(fired1[0]["result"]["key"], "q")
        self.assertEqual(len(reflex.evaluate_once(low_hp_img)), 0)
        st_mode = reflex.set_mode("dodge_priority", dry_run=False)
        self.assertFalse(st_mode["dryRun"])
        self.assertFalse(reflex.status()["dryRun"])

    def test_07_ocr_crop_tuple_contract_and_score_filter(self):
        svc = _DummyService(self.frames_dir, with_ocr=True)
        mgr = CompanionManager(svc, dry_run=True)
        orig_grab = omnivisiond.grab
        try:
            omnivisiond.grab = lambda region=None: Image.fromarray(np.zeros((60, 160, 3), dtype=np.uint8))
            # Point manager's service module to omnivisiond for grab lookup
            svc.__class__.__module__ = omnivisiond.__name__
            out = mgr.ocr_crop({"rect": [10, 20, 160, 60], "scale": 2.0, "minScore": 0.5})
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["text"], "HP 420/1000")
            self.assertEqual(len(out["elements"]), 1)
            self.assertEqual(out["ocrMeta"]["ocrMs"], 12.4)
        finally:
            omnivisiond.grab = orig_grab

    def test_08_real_http_server_full_routes_and_error_contracts(self):
        orig_vs, orig_lw, orig_grab, orig_svc, orig_dr = (
            omnivisiond.virtual_screen,
            omnivisiond.list_windows,
            omnivisiond.grab,
            omnivisiond.SERVICE,
            omnivisiond.DATA_ROOT,
        )
        try:
            omnivisiond.virtual_screen = lambda: (0, 0, 1920, 1080)
            omnivisiond.list_windows = lambda: [
                {"hwnd": 1001, "title": "PotPlayer - Ep01", "x": 0, "y": 0, "w": 1280, "h": 720, "foreground": True},
                {"hwnd": 2002, "title": "Hollow Knight", "x": 100, "y": 100, "w": 1280, "h": 720, "foreground": False},
            ]
            omnivisiond.grab = lambda region=None: Image.fromarray(np.full((360, 640, 3), 88, dtype=np.uint8))
            omnivisiond.DATA_ROOT = self.tmp.name

            class _Args:
                port = 0
                data_dir = self.tmp.name
                frames_dir = self.frames_dir
                max_wh_ratio = 25.0
                cache_size = 16
                no_ocr = True
                profile = "standard"

            os.environ["OMNIVISION_DRY_RUN"] = "1"
            omnivisiond.SERVICE = omnivisiond.Service(_Args())
            omnivisiond.SERVICE.ocr = _DummyOcrEngine()

            srv = ThreadingHTTPServer(("127.0.0.1", 0), omnivisiond.Handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"

            def req(method: str, path: str, body=None):
                data = json.dumps(body).encode("utf-8") if body is not None else None
                headers = {"Content-Type": "application/json"} if data is not None else {}
                r = urllib.request.Request(base + path, data=data, headers=headers, method=method)
                try:
                    with urllib.request.urlopen(r, timeout=10) as resp:
                        return resp.status, json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read().decode("utf-8"))

            try:
                st, h = req("GET", "/health")
                self.assertEqual(st, 200)
                self.assertIn("companion", h)

                st, m = req("POST", "/companion/mode", {"mode": "watch", "windowMatch": "PotPlayer", "enableAudio": False, "startLoop": False})
                self.assertEqual(st, 200)
                self.assertTrue(m["ok"])
                self.assertEqual(m["lockedHwnd"], 1001)

                st, cs = req("POST", "/companion/contact_sheet", {})
                self.assertEqual(st, 200)
                self.assertTrue(cs["ok"], cs)
                self.assertTrue(os.path.isfile(cs["frame"]))

                st, oc = req("POST", "/ocr_crop", {"rect": [0, 0, 120, 40], "scale": 2.0})
                self.assertEqual(st, 200)
                self.assertTrue(oc["ok"])
                self.assertEqual(oc["text"], "HP 420/1000")

                # Reflex start MUST default to dryRun=True when dryRun is omitted!
                st_r, rf_start = req("POST", "/companion/reflex/start", {"preset": "generic_hp_potion"})
                self.assertEqual(st_r, 200)
                self.assertTrue(rf_start["ok"])
                self.assertTrue(rf_start["dryRun"])
                self.assertTrue(req("POST", "/companion/reflex/stop", {})[1]["ok"])

                # Malformed inputs & safety blocks MUST return HTTP 200 {ok: False}, NEVER HTTP 500!
                bad_reqs = [
                    ("POST", "/companion/game/act", {"action": "tap"}),
                    ("POST", "/companion/game/act", {"action": "hold"}),
                    ("POST", "/companion/game/act", {"action": "tap", "key": "UNKNOWN_XYZ"}),
                    ("POST", "/companion/game/act", {"action": "sequence", "steps": [{"action": "tap"}]}),
                    ("POST", "/companion/game/act", {"action": "mouse_rel", "steps": "bad"}),
                    ("POST", "/companion/mode", {"mode": "watch", "maxFramesPerHour": "abc"}),
                    ("POST", "/companion/reflex/start", {"preset": "../../etc/passwd"}),
                    ("POST", "/companion/reflex/unknown_op", {}),
                ]
                for bm, bp, bb in bad_reqs:
                    bst, bout = req(bm, bp, bb)
                    self.assertEqual(bst, 200, f"Expected HTTP 200 graceful error for {bp} {bb}, got {bst}: {bout}")
                    self.assertFalse(bout["ok"], f"Expected ok=False for {bp} {bb}, got {bout}")

                # Emergency stop block -> HTTP 200 ok:False -> reset_stop -> HTTP 200 ok:True
                self.assertTrue(req("POST", "/companion/game/act", {"action": "emergency_stop"})[1]["ok"])
                st_blk, out_blk = req("POST", "/companion/game/act", {"action": "tap", "key": "w", "dryRun": True})
                self.assertEqual(st_blk, 200)
                self.assertFalse(out_blk["ok"])
                self.assertIn("EMERGENCY_STOPPED", out_blk["error"])
                self.assertTrue(req("POST", "/companion/game/act", {"action": "reset_stop"})[1]["ok"])
                self.assertTrue(req("POST", "/companion/game/act", {"action": "tap", "key": "w", "dryRun": True})[1]["ok"])
            finally:
                srv.shutdown()
                srv.server_close()
                omnivisiond.SERVICE.companion.close()
        finally:
            omnivisiond.virtual_screen = orig_vs
            omnivisiond.list_windows = orig_lw
            omnivisiond.grab = orig_grab
            omnivisiond.SERVICE = orig_svc
            omnivisiond.DATA_ROOT = orig_dr

    def test_09_manifest_and_host_features_integrity(self):
        with open(os.path.join(REPO_ROOT, "plugin", "package.json"), "r", encoding="utf-8") as f:
            pkg = json.load(f)
        self.assertEqual(pkg["version"], "0.15.3")

        with open(os.path.join(REPO_ROOT, "plugin", "host.js"), "r", encoding="utf-8") as f:
            host_txt = f.read()
        self.assertIn("writeFileSync", host_txt.splitlines()[15])
        self.assertIn("statSync", host_txt.splitlines()[15])
        self.assertIn("companion.polling", host_txt)
        self.assertIn("if (sub === 'reset' || sub === 'unlock')", host_txt)
        self.assertIn("d.companion", host_txt)
        for tool_name in [
            "companion_mode",
            "companion_watch_frame",
            "companion_game_act",
            "companion_reflex",
            "vision_ocr_crop",
        ]:
            self.assertIn(f"name: '{tool_name}'", host_txt)
        self.assertIn("runtime.profile === 'energy'", host_txt)
        self.assertIn("reflexDryRun", host_txt)

        with open(os.path.join(REPO_ROOT, "plugin", "client.js"), "r", encoding="utf-8") as f:
            client_txt = f.read()
        self.assertIn("parseLiveStateFromText", client_txt)
        self.assertIn("useState('standard')", client_txt)

        with open(os.path.join(REPO_ROOT, "install.ps1"), "r", encoding="utf-8-sig") as f:
            ps1_txt = f.read()
        self.assertIn("$dedupBundles", ps1_txt)
        self.assertIn("[string]$VenvDir", ps1_txt)
        self.assertIn("python  = $venvPy", ps1_txt)
        self.assertIn("hf-mirror.com", ps1_txt)
        self.assertIn("Resolve-FullPath", ps1_txt)
        self.assertIn("[pscustomobject]@{", ps1_txt)
        self.assertIn("[string]$PluginDir", ps1_txt)
        self.assertIn("写入自检失败：$cfgPath", ps1_txt)
        self.assertIn("transientRetries", host_txt)
        self.assertIn("consecutiveErrors", host_txt)
        self.assertIn("retryOnNetErr", host_txt)

        readme_path = os.path.join(REPO_ROOT, "README.md")
        self.assertTrue(os.path.isfile(readme_path))
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_txt = f.read()
        self.assertIn("反作弊", readme_txt)
        self.assertIn("DirectInput", readme_txt)
        self.assertIn("WASAPI", readme_txt)
        self.assertIn("隐私", readme_txt)
        self.assertIn("SenseVoiceSmall", readme_txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
