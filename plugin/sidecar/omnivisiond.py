#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""omnivision sidecar —— 通用视觉层的「眼睛」。

职责（只做这一件事）：
  一帧截图 → 文字层(RapidOCR/ONNX) → 统一元素清单(带坐标) → 场景判别 → 变化区域 → 场景指纹缓存

设计约束（来自 omnivision-brief.md）：
  - 不装 torch，全部 ONNX；
  - 模型在 F 盘 venv 里；
  - 出口是「文本清单 + 截图路径」，图像按需另取；
  - 同一界面第二次必须走缓存（0 OCR）。

进程模型：常驻，启动时把 RapidOCR 加载好并 warmup，之后按 HTTP 请求干活：
  GET  /health   → 版本/屏幕/缓存计数
  GET  /windows  → 可见窗口列表（给 window:关键词 用）
  POST /look     → 主入口
  POST /frame    → 只截图给路径（不看元素时用）

启动时会先往 stdout 打一行 JSON（{"port":..,"screen":[w,h]}），宿主据此连上来。
"""
from __future__ import annotations

import argparse
import atexit
import base64
import ctypes
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image, ImageDraw, ImageGrab

try:
    import cv2
except Exception:  # pragma: no cover - rapidocr 一定带 opencv
    cv2 = None

try:
    from companion import CompanionManager
except Exception:
    _sidecar_dir = os.path.dirname(os.path.abspath(__file__))
    if _sidecar_dir not in sys.path:
        sys.path.insert(0, _sidecar_dir)
    from companion import CompanionManager


# ------------------------------------------------------------------ 基础

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    with LOG_LOCK:
        sys.stderr.write(f"[omnivision] {msg}\n")
        sys.stderr.flush()


def dpi_aware() -> None:
    """不设为 DPI 感知的话，GetSystemMetrics/GetWindowRect 给的是逻辑坐标，
    与 ImageGrab 的物理像素对不上，点击就会偏。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ------------------------------------------------------------------ ONNX 线程预算

#: 当前档位：energy / standard / performance（--profile 或 OMNIVISION_PROFILE 指定）
PROFILE = (os.environ.get("OMNIVISION_PROFILE") or "standard").strip().lower()
if PROFILE not in ("energy", "standard", "performance"):
    PROFILE = "standard"


def onnx_threads() -> int:
    """ONNX 的 intra-op 线程数。

    默认不按「全核」跑：低端机（4 核）一次全屏 OCR 会把 CPU 吃满，风扇起飞。
    实测（12 核，全屏 2560x1440，同一张图）：
      threads=全核  1.08s / CPU 6.48s
      threads=4     1.12s / CPU 4.34s（慢 3.7%，CPU 少 33%）
      threads=3     1.25s / CPU 3.78s（慢 15%）
      threads=2     1.67s / CPU 3.30s（慢 54%）
    规格里写的是 min(4, 核数/2)，但 4 核机器按它算出来是 2，实测会慢 ~50%、超过 20% 验收；
    所以 4 核用 3（慢 ~11%、CPU 只到 3 核），2 核用 2，其余上限 4。
    要强制指定：环境变量 OMNIVISION_ONNX_THREADS（1–64）。
    """
    raw = (os.environ.get("OMNIVISION_ONNX_THREADS") or "").strip()
    if raw:
        try:
            v = int(float(raw))
            if v > 0:
                return max(1, min(64, v))
        except Exception:
            pass
    n = int(os.cpu_count() or 4)
    if PROFILE == "energy":
        return 1
    if PROFILE == "performance":
        return max(1, min(n, 64))
    return max(1, min(n, 4, max(2, n - 1)))


def onnx_thread_source() -> str:
    if (os.environ.get("OMNIVISION_ONNX_THREADS") or "").strip():
        return "env"
    return "profile" if PROFILE != "standard" else "auto"


# ------------------------------------------------------------------ 屏幕 / 窗口

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79


def virtual_screen() -> tuple[int, int, int, int]:
    u = ctypes.windll.user32
    return (
        int(u.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(u.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        int(u.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(u.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


SCREEN_LOCK = threading.Lock()


def grab(bbox: tuple[int, int, int, int] | None = None) -> Image.Image:
    """bbox 是虚拟屏幕绝对坐标 (x,y,w,h)；None = 全部屏幕。

    watch 线程和 /look 会并发抓屏，加同一把锁，避免抢屏导致其中一边拿到半帧。
    """
    with SCREEN_LOCK:
        if bbox is None:
            return ImageGrab.grab(all_screens=True)
        x, y, w, h = bbox
        return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)


def list_windows() -> list[dict]:
    """可见顶层窗口（带标题/矩形/是否前台）。给 region="window:关键词" 用。"""
    u = ctypes.windll.user32
    user32 = ctypes.windll.user32
    fg = int(user32.GetForegroundWindow())
    rows: list[dict] = []

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    def cb(hwnd, _lparam):
        h = int(hwnd)
        if not user32.IsWindowVisible(h):
            return True
        if user32.IsIconic(h):
            return True
        n = user32.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        r = RECT()
        if not user32.GetWindowRect(h, ctypes.byref(r)):
            return True
        w, hh = r.right - r.left, r.bottom - r.top
        if w < 40 or hh < 30:
            return True
        rows.append(
            {
                "hwnd": h,
                "title": title,
                "x": int(r.left),
                "y": int(r.top),
                "w": int(w),
                "h": int(hh),
                "foreground": h == fg,
            }
        )
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    rows.sort(key=lambda r: (not r["foreground"], -r["w"] * r["h"]))
    return rows


# ------------------------------------------------------------------ 场景指纹 / 差分

SMALL_DIV = 8  # 原图 8 像素 -> 小图 1 像素


def small_gray(img: Image.Image) -> np.ndarray:
    """把截图压成小灰度图：抓 diff 与指纹都用它（快，且能吃掉光标闪动）。"""
    g = img.convert("L")
    w, h = g.size
    sw, sh = max(1, w // SMALL_DIV), max(1, h // SMALL_DIV)
    return np.asarray(g.resize((sw, sh), Image.BILINEAR), dtype=np.uint8)


def dhash(small: np.ndarray, gw: int = 9, gh: int = 8) -> int:
    img = Image.fromarray(small).resize((gw, gh), Image.BILINEAR)
    a = np.asarray(img, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).reshape(-1)
    out = 0
    for b in bits:
        out = (out << 1) | (1 if b else 0)
    return out


def hamming(a: int, b: int) -> int:
    return int((a ^ b).bit_count())


def block_changes(prev: np.ndarray | None, cur: np.ndarray, region: tuple[int, int, int, int], threshold: float = 6.0) -> list[dict]:
    """小图逐格差 -> 原图坐标的变化框。tile = 4 个小图像素 = 32 原图像素。"""
    if prev is None or prev.shape != cur.shape:
        return []
    diff = np.abs(cur.astype(np.int16) - prev.astype(np.int16))
    th, tw = cur.shape[:2]
    tile = 4
    bt, bw = th // tile, tw // tile
    if bt == 0 or bw == 0:
        return []
    d = diff[: bt * tile, : bw * tile].reshape(bt, tile, bw, tile).mean(axis=(1, 3))
    ys, xs = np.where(d > threshold)
    out = []
    for y, x in zip(ys.tolist(), xs.tolist()):
        score = float(d[y, x]) / 255.0
        out.append(
            {
                "box": [
                    int(region[0] + x * tile * SMALL_DIV),
                    int(region[1] + y * tile * SMALL_DIV),
                    int(tile * SMALL_DIV),
                    int(tile * SMALL_DIV),
                ],
                "score": round(score, 3),
            }
        )
    out.sort(key=lambda r: -r["score"])
    return out


# ------------------------------------------------------------------ 余光（watch）：后台差分成事件流

WATCH_DEFAULTS = {
    "intervalMs": 2000,
    "threshold": 0.02,
    "textTrack": True,
    "textMinConf": 0.6,
    "textAreaMax": 0.5,
    "cooldownMs": 45000,
    "maxPerHour": 40,
    "maxFrames": 200,
    "windowMatch": "",
    "quietUntil": 0,
    "maxWidth": 0,
    "minChanged": 8,
}
WATCH_TILE_MEAN = 6.0  # 单块平均差（0..255）超过才算「这块变了」


def _foreground_title() -> str:
    u = ctypes.windll.user32
    h = int(u.GetForegroundWindow())
    if not h:
        return ""
    n = u.GetWindowTextLengthW(h)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(h, buf, n + 1)
    return buf.value


class Watcher:
    """后台线程：定时抓屏 → 缩略差分 → 过滤（静止/冷却/配额/窗口/静默）→ 入队。

    只做截图 + 差分，不跑 OCR、不调模型。事件由宿主轮询取走。
    """

    def __init__(self, svc) -> None:
        self.svc = svc
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_evt = threading.Event()
        self.cfg = dict(WATCH_DEFAULTS)
        self.events: deque = deque(maxlen=200)
        self.seq = 0
        self.event_times: deque = deque(maxlen=1000)
        self.frames: list[str] = []
        self.started_at = 0.0
        self.last_tick = 0.0
        self.last_score = 0.0
        self.last_changed = 0
        self.last_window = ""
        self.preview: np.ndarray | None = None
        self.prev_img = None
        self.prev_origin = (0, 0)
        self.prev_exp_w = 1
        self.text_memory = None  # (rect, {norm: item}) 上次事件区域的稳定文字集
        self.text_stats = {"events": 0, "ocrRuns": 0, "add": 0, "remove": 0, "update": 0, "skippedLarge": 0}
        self.stats = {
            "captures": 0,
            "events": 0,
            "droppedCooldown": 0,
            "droppedQuota": 0,
            "droppedWindow": 0,
            "droppedQuiet": 0,
            "droppedStatic": 0,
        }

    def _cfg(self, p: dict) -> dict:
        c = dict(WATCH_DEFAULTS)
        prof = str((p or {}).get("profile") or PROFILE).strip().lower()
        if prof not in ("energy", "standard", "performance"):
            prof = PROFILE
        c["profile"] = prof
        if prof == "energy":
            c["intervalMs"] = 5000
            c["textTrack"] = False
        elif prof == "performance":
            c["intervalMs"] = 1000
        for k in ("intervalMs", "threshold", "cooldownMs", "maxPerHour", "maxFrames", "windowMatch", "quietUntil", "maxWidth", "minChanged"):
            if isinstance(p, dict) and p.get(k) is not None:
                c[k] = p[k]
        c["intervalMs"] = max(300, min(60000, int(float(c["intervalMs"]))))
        c["threshold"] = max(0.0002, min(0.5, float(c["threshold"])))
        c["cooldownMs"] = max(0, min(3_600_000, int(float(c["cooldownMs"]))))
        c["maxPerHour"] = max(1, min(500, int(float(c["maxPerHour"]))))
        c["maxFrames"] = max(10, min(2000, int(float(c["maxFrames"]))))
        c["windowMatch"] = str(c["windowMatch"] or "").strip()
        c["quietUntil"] = int(float(c["quietUntil"] or 0))
        c["maxWidth"] = max(0, min(7680, int(float(c["maxWidth"] or 0))))
        c["minChanged"] = max(1, min(5000, int(float(c["minChanged"] or 8))))
        c["textTrack"] = bool(c.get("textTrack", True))
        c["textMinConf"] = max(0.1, min(0.95, float(c.get("textMinConf") or 0.6)))
        c["textAreaMax"] = max(0.05, min(0.95, float(c.get("textAreaMax") or 0.5)))
        return c

    def _target_rect(self) -> tuple[tuple[int, int, int, int], str] | None:
        """windowMatch 命中时只抓那个窗口（避开动态壁纸/别的窗口），同时带出它的标题。"""
        m = (self.cfg.get("windowMatch") or "").strip()
        if not m:
            return None
        for r in list_windows():
            if m.lower() in r["title"].lower():
                return (r["x"], r["y"], r["w"], r["h"]), r["title"]
        return None

    def _capture(self, rect: tuple[int, int, int, int] | None = None) -> tuple[Image.Image, np.ndarray]:
        img = grab(rect)
        if self.cfg["maxWidth"] and img.width > self.cfg["maxWidth"]:
            ratio = self.cfg["maxWidth"] / float(img.width)
            img = img.resize((self.cfg["maxWidth"], max(1, int(img.height * ratio))), Image.BILINEAR)
        return img, small_gray(img)

    # -- 文字变化：只为变化区域 OCR，稳定（两次识别一致）才报
    def _scale(self, img) -> float:
        sw = max(1, self.svc.screen[2])
        return img.width / float(sw)

    def _crop(self, img, rect):
        k = self._scale(img)
        sx, sy = self.svc.screen[0], self.svc.screen[1]
        box = (
            int((rect[0] - sx) * k),
            int((rect[1] - sy) * k),
            int((rect[0] - sx + rect[2]) * k),
            int((rect[1] - sy + rect[3]) * k),
        )
        return img.crop(box)

    @staticmethod
    def _line_overlap(a: dict, b: dict) -> float:
        ah, bh = a["box"][3], b["box"][3]
        ov = min(a["box"][1] + ah, b["box"][1] + bh) - max(a["box"][1], b["box"][1])
        return ov / float(max(1, min(ah, bh)))

    def _merge_lines(self, items: list[dict]) -> list[dict]:
        """同一行的碎片合并（hel + ello = hello）——先归组，组内按 x 排序再拼。

        之前按 OCR 返回顺序直接拼，遇到「大肥鱼」比「hello」先返回就会拼反。
        """
        if not items:
            return []
        # 1) 归组：纵向重叠 ≥50% 算同一行
        groups: list[list[dict]] = []
        for it in sorted(items, key=lambda a: (a["box"][1], a["box"][0])):
            placed = None
            for g in groups:
                gy0 = min(x["box"][1] for x in g)
                gy1 = max(x["box"][1] + x["box"][3] for x in g)
                ov = min(gy1, it["box"][1] + it["box"][3]) - max(gy0, it["box"][1])
                if ov >= 0.5 * min(gy1 - gy0, it["box"][3]):
                    placed = g
                    break
            if placed is None:
                groups.append([dict(it)])
            else:
                placed.append(dict(it))

        # 2) 组内按 x 排序后拼接
        lines: list[dict] = []
        for g in groups:
            g.sort(key=lambda a: a["box"][0])
            cur = dict(g[0])
            for it in g[1:]:
                gap = it["box"][0] - (cur["box"][0] + cur["box"][2])
                if gap < 0 or gap <= 12:
                    sep = ""
                elif gap <= 40:
                    sep = " "
                else:
                    sep = "  "
                cur["text"] = cur["text"] + sep + it["text"]
                x1 = max(cur["box"][0] + cur["box"][2], it["box"][0] + it["box"][2])
                y1 = min(cur["box"][1], it["box"][1])
                y2 = max(cur["box"][1] + cur["box"][3], it["box"][1] + it["box"][3])
                cur["box"] = [cur["box"][0], y1, x1 - cur["box"][0], y2 - y1]
                cur["conf"] = min(cur["conf"], it["conf"])
            cur["norm"] = re.sub(r"\s+", "", cur["text"])
            lines.append(cur)
        lines.sort(key=lambda a: (a["box"][1], a["box"][0]))
        return lines

    def _text_set(self, img, rect, already_cropped: bool = False, origin: tuple[int, int] | None = None, expected_width: float | None = None):
        """从一张图里读出 rect（屏幕坐标）范围内的文字。

        img 的原点必须显式给：全屏图是 screen 原点，窗口截图是窗口左上角。
        之前混用导致 before 永远是空的。
        """
        if self.svc.ocr is None:
            return []
        ox, oy = origin if origin is not None else (self.svc.screen[0], self.svc.screen[1])
        base_w = float(expected_width or img.width)
        if already_cropped:
            crop = img
            k = 1.0
        else:
            k = img.width / max(1.0, base_w)
            box = (
                int((rect[0] - ox) * k),
                int((rect[1] - oy) * k),
                int((rect[0] - ox + rect[2]) * k),
                int((rect[1] - oy + rect[3]) * k),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                return []
            crop = img.crop(box)
        raw, _ = self.svc.ocr.run(crop)
        items = []
        for e in raw or []:
            conf = float(e.get("score") or 0)
            if conf < self.cfg["textMinConf"]:
                continue
            text = str(e.get("text") or "").strip()
            if not text:
                continue
            bx, by, bw, bh = e["box"]
            items.append(
                {
                    "text": text,
                    "norm": re.sub(r"\s+", "", text),
                    "conf": round(conf, 3),
                    "box": [
                        int(bx / k) + rect[0],
                        int(by / k) + rect[1],
                        int(bw / k),
                        int(bh / k),
                    ],
                }
            )
        return self._merge_lines(items)

    def _text_changes(self, before_img, before_origin, before_exp_w, after_img, after_origin, changes):
        """返回 (textChanges, meta)。区域太大/没 OCR 就跳过，不假装。"""
        meta = {}
        if not self.cfg.get("textTrack", True):
            meta["textSkipped"] = "off"
            return [], meta
        if self.svc.ocr is None:
            meta["textSkipped"] = "no_ocr"
            return [], meta
        boxes = [c["box"] for c in (changes or [])[:6] if isinstance(c.get("box"), list)]
        if not boxes:
            return [], meta
        sx, sy, sw, sh = self.svc.screen
        pad = 40  # 让旧字也进同一块图，update 才配得上
        x0 = max(sx, min(b[0] for b in boxes) - pad)
        y0 = max(sy, min(b[1] for b in boxes) - pad)
        x1 = min(sx + sw, max(b[0] + b[2] for b in boxes) + pad)
        y1 = min(sy + sh, max(b[1] + b[3] for b in boxes) + pad)
        rect = (x0, y0, max(1, x1 - x0), max(1, y1 - y0))
        if (rect[2] * rect[3]) / float(max(1, sw * sh)) > self.cfg["textAreaMax"]:
            self.text_stats["skippedLarge"] += 1
            meta["textSkipped"] = "too_large"
            return [], meta
        try:
            self.text_stats["ocrRuns"] += 1
            before_map = None
            if self.text_memory is not None:
                old_rect, old_map = self.text_memory
                ox = max(0, min(old_rect[0] + old_rect[2], rect[0] + rect[2]) - max(old_rect[0], rect[0]))
                oy = max(0, min(old_rect[1] + old_rect[3], rect[1] + rect[3]) - max(old_rect[1], rect[1]))
                inter = ox * oy
                if inter > 0 and inter >= 0.5 * min(old_rect[2] * old_rect[3], rect[2] * rect[3]):
                    before_map = dict(old_map)
            if before_map is None:
                before_list = (
                    self._text_set(before_img, rect, False, before_origin, before_exp_w) if before_img is not None else []
                )
                before_map = {b["norm"]: b for b in before_list}
            before = list(before_map.values())
            # 两遍新鲜抓图取交集：同一次识别的裁剪容易撞上渲染中间态
            after1 = self._text_set(grab(rect), rect, True)
            time.sleep(0.12)
            after2 = self._text_set(grab(rect), rect, True)
        except Exception:
            log("watch 文字 OCR 出错：" + traceback.format_exc())
            meta["textSkipped"] = "ocr_error"
            return [], meta

        # 稳定化：两次识别都出现的文字才算「这一帧真实内容」
        stable = {}
        for a in after1:
            if any(b["norm"] == a["norm"] for b in after2):
                stable[a["norm"]] = a
        before_norm = {b["norm"]: b for b in before}
        adds = [a for n, a in stable.items() if n not in before_norm]
        removes = [b for n, b in before_norm.items() if n not in stable]
        ups = []
        for b in list(removes):
            best = None
            for a in adds:
                # 同一行（纵向重叠 ≥50%）就认为是一处改写，不再要求完全同位置
                if self._line_overlap(a, b) >= 0.5:
                    dist = abs(a["box"][0] - b["box"][0])
                    if best is None or dist < best[0]:
                        best = (dist, a)
            if best is not None:
                ups.append({"op": "update", "from": b["text"], "to": best[1]["text"], "conf": best[1]["conf"]})
                adds.remove(best[1])
                removes.remove(b)
        self.text_memory = (rect, dict(stable))
        out = ups[:4] + [{"op": "add", "text": a["text"], "conf": a["conf"]} for a in adds[:4]] + [
            {"op": "remove", "text": b["text"], "conf": b["conf"]} for b in removes[:4]
        ]
        for item in out:
            if item["op"] in self.text_stats:
                self.text_stats[item["op"]] += 1
        return out[:8], meta

    def _cleanup_frames(self) -> None:
        while len(self.frames) > self.cfg["maxFrames"]:
            old = self.frames.pop(0)
            try:
                os.remove(old)
            except Exception:
                pass

    def _loop(self) -> None:
        prev = None
        while not self.stop_evt.is_set():
            if self.stop_evt.wait(self.cfg["intervalMs"] / 1000.0):
                break
            try:
                matched = self._target_rect()
                if self.cfg["windowMatch"] and matched is None:
                    self.stats["droppedWindow"] += 1
                    prev = None
                    continue
                target = matched[0] if matched else None
                target_title = matched[1] if matched else ""
                img, cur = self._capture(target)
                origin = (target[0], target[1]) if target is not None else (self.svc.screen[0], self.svc.screen[1])
                exp_w = (target[2] if target is not None else self.svc.screen[2])
                now = time.time()
                self.stats["captures"] += 1
                self.last_tick = now
                if prev is None or prev.shape != cur.shape:
                    prev = cur
                    self.prev_img = img
                    self.prev_origin = origin
                    self.prev_exp_w = exp_w
                    self.last_score = 0.0
                    self.last_changed = 0
                    continue
                region = target if target is not None else self.svc.screen
                changes = block_changes(prev, cur, region, threshold=WATCH_TILE_MEAN)
                prev = cur
                th, tw = cur.shape[:2]
                total = max(1, (th // 4) * (tw // 4))
                score = round(len(changes) / float(total), 4)
                self.last_score = score
                self.last_changed = len(changes)

                if score < self.cfg["threshold"] or len(changes) < self.cfg["minChanged"]:
                    self.stats["droppedStatic"] += 1
                    self.prev_img = img
                    self.prev_origin = origin
                    self.prev_exp_w = exp_w
                    continue
                if self.cfg["quietUntil"] and now * 1000 < self.cfg["quietUntil"]:
                    self.stats["droppedQuiet"] += 1
                    continue
                if self.event_times and (now - self.event_times[-1]) * 1000 < self.cfg["cooldownMs"]:
                    self.stats["droppedCooldown"] += 1
                    continue
                cutoff = now - 3600
                while self.event_times and self.event_times[0] < cutoff:
                    self.event_times.popleft()
                if len(self.event_times) >= self.cfg["maxPerHour"]:
                    self.stats["droppedQuota"] += 1
                    continue
                win = target_title or _foreground_title()
                if self.cfg["windowMatch"] and self.cfg["windowMatch"].lower() not in win.lower():
                    self.stats["droppedWindow"] += 1
                    continue

                text_changes, text_meta = self._text_changes(self.prev_img, self.prev_origin, self.prev_exp_w, img, origin, changes)
                path = self.svc.save_frame(img, "watch")
                self.frames.append(path.replace("/", os.sep))
                self._cleanup_frames()
                self.event_times.append(now)
                with self.lock:
                    self.seq += 1
                    self.last_window = win
                    self.stats["events"] += 1
                    self.prev_img = img
                    self.prev_origin = origin
                    self.prev_exp_w = exp_w
                    self.events.append(
                        {
                            "seq": self.seq,
                            "ts": int(now * 1000),
                            "score": score,
                            "changed": len(changes),
                            "boxes": changes[:5],
                            "frame": path,
                            "window": win,
                            "intervalMs": self.cfg["intervalMs"],
                            "textChanges": text_changes,
                            **text_meta,
                        }
                    )
            except Exception:
                log("watch 循环出错：" + traceback.format_exc())
                time.sleep(0.5)

    def start(self, p: dict) -> dict:
        with self.lock:
            th = self.thread
        if th is not None and th.is_alive():
            self.stop()
        with self.lock:
            self.cfg = self._cfg(p or {})
            self.stop_evt = threading.Event()
            self.preview = None
            self.prev_img = None
            self.prev_origin = (0, 0)
            self.prev_exp_w = 1
            self.text_memory = None
            self.text_stats = {"events": 0, "ocrRuns": 0, "add": 0, "remove": 0, "update": 0, "skippedLarge": 0}
            self.started_at = time.time()
            self.thread = threading.Thread(target=self._loop, name="omnivision-watch", daemon=True)
            self.thread.start()
            cfg = dict(self.cfg)
        return {"ok": True, "running": True, "cfg": cfg, "seq": self.seq}

    def stop(self) -> dict:
        with self.lock:
            self.stop_evt.set()
            th = self.thread
        if th is not None:
            th.join(timeout=3)
        with self.lock:
            alive = bool(self.thread is not None and self.thread.is_alive())
            self.thread = None
        return {"ok": True, "running": alive}

    def status(self) -> dict:
        now = time.time()
        cutoff = now - 3600
        per_hour = sum(1 for t in self.event_times if t >= cutoff)
        alive = bool(self.thread is not None and self.thread.is_alive())
        return {
            "ok": True,
            "running": alive,
            "cfg": dict(self.cfg),
            "seq": self.seq,
            "queued": len(self.events),
            "perHour": per_hour,
            "startedAt": int(self.started_at * 1000),
            "lastTick": int(self.last_tick * 1000),
            "lastScore": self.last_score,
            "lastChanged": self.last_changed,
            "lastWindow": self.last_window,
            "stats": dict(self.stats),
            "textStats": dict(self.text_stats),
        }

    def poll(self, after: int = 0, limit: int = 10) -> dict:
        with self.lock:
            evs = [e for e in self.events if e["seq"] > after][: max(1, min(50, int(limit)))]
            seq = self.seq
        return {"ok": True, "events": evs, "seq": seq}

    def snap(self) -> dict:
        img, cur = self._capture()
        score = None
        if self.preview is not None and self.preview.shape == cur.shape:
            changes = block_changes(self.preview, cur, self.svc.screen, threshold=WATCH_TILE_MEAN)
            th, tw = cur.shape[:2]
            total = max(1, (th // 4) * (tw // 4))
            score = round(len(changes) / float(total), 4)
        self.preview = cur
        path = self.svc.save_frame(img, "snap")
        return {"ok": True, "frame": path, "score": score, "window": _foreground_title()}


# ------------------------------------------------------------------ 动作层：SendInput 原语（纯 ctypes，零依赖）

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort))


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.c_ulong), ("union", _INPUTUNION))


KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def click_at(x: int, y: int, button: str = "left", double: bool = False) -> None:
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.02)
    down = 0x0002 if button == "left" else 0x0008
    up = 0x0004 if button == "left" else 0x0010
    for _ in range(2 if double else 1):
        u.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.03)
        u.mouse_event(up, 0, 0, 0, 0)
        if double:
            time.sleep(0.08)


def _key_event(vk: int, up: bool = False) -> None:
    inp = _INPUT(type=1)
    inp.union.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def send_unicode(text: str) -> None:
    """按 Unicode 直接注入，不走输入法（中文/emoji 也能进）。"""
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i : i + 2], "little")
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = _INPUT(type=1)
            inp.union.ki = _KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=None)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(0.004)


def send_keys(combo: str) -> None:
    """支持 "enter" / "esc" / "ctrl+s" / "shift+tab" 这种组合。"""
    parts = [p.strip().lower() for p in str(combo or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("key 为空")
    vks = []
    for p in parts:
        if p in VK:
            vks.append(VK[p])
        elif len(p) == 1:
            vks.append(ord(p.upper()))
        else:
            raise ValueError(f"不认识的键名：{p}")
    for vk in vks:
        _key_event(vk, False)
        time.sleep(0.01)
    for vk in reversed(vks):
        _key_event(vk, True)
        time.sleep(0.01)


# ------------------------------------------------------------------ OCR

#: rec 阶段批内 max_wh_ratio 的上限。默认实现取「批内最宽那行」，
#: 一条 1581px 的宽行会把整批 padding 到 1581 宽，算力直接翻几倍。
#: 实测钳到 8：全屏 OCR 3.8s -> 2.1s，识别框数 112 -> 107。
MAX_WH_RATIO = 8.0


def _install_ratio_clamp(cap: float) -> None:
    from rapidocr_onnxruntime.ch_ppocr_rec import text_recognize as tr

    if getattr(tr.TextRecognizer.__call__, "_omni_clamped", False):
        return

    def __call__(self, img_list, return_word_box=False):
        if isinstance(img_list, np.ndarray):
            img_list = [img_list]
        width_list = [im.shape[1] / float(im.shape[0]) for im in img_list]
        indices = np.argsort(np.array(width_list))
        img_num = len(img_list)
        rec_res = [("", 0.0)] * img_num
        batch_num = self.rec_batch_num
        elapse = 0.0
        for beg in range(0, img_num, batch_num):
            end = min(img_num, beg + batch_num)
            imgC, imgH, imgW = self.rec_image_shape[:3]
            max_wh_ratio = imgW / imgH
            wh_ratio_list = []
            for ino in range(beg, end):
                h, w = img_list[indices[ino]].shape[0:2]
                wh = w * 1.0 / h
                max_wh_ratio = max(max_wh_ratio, wh)
                wh_ratio_list.append(wh)
            max_wh_ratio = min(max_wh_ratio, cap)
            batch = []
            for ino in range(beg, end):
                norm = self.resize_norm_img(img_list[indices[ino]], max_wh_ratio)
                batch.append(norm[np.newaxis, :])
            batch = np.concatenate(batch).astype(np.float32)
            t = time.time()
            preds = self.session(batch)[0]
            rec_result = self.postprocess_op(preds, return_word_box, wh_ratio_list=wh_ratio_list, max_wh_ratio=max_wh_ratio)
            for rno, one_res in enumerate(rec_result):
                rec_res[indices[beg + rno]] = one_res
            elapse += time.time() - t
        return rec_res, elapse

    __call__._omni_clamped = True
    tr.TextRecognizer.__call__ = __call__


class OcrEngine:
    """RapidOCR 封装 + 两层提速（方案 A，实测口径见 docs/DETAIL.md）。

    1) det 模式自适应（A1）：
       - ≤2MP（窗口/ROI）：`limit_type=min, limit_side_len=736`（保精度，原行为）；
       - >2MP（全屏）：`limit_type=max` —— RapidOCR 内部按长边自适应到 960/1500/2000，
         2560×1440 会缩到 2000×1125（长边 78%），实测两张全屏帧文字 100% 一致。
       注：`min` 模式只会放大小图、不会缩大图；所以大图必须切 `max` 才有提速。
    2) 框级签名缓存（A2）：
       - 每个结果框按内缩 10% 的灰度图缩到 24×8 算 FNV-1a；
       - 下一次同尺寸同粗签名时，**逐框重算签名验证**；全部一致 → 直接复用文本（0 次 OCR，几毫秒）；
       - 任一框签名变了 → 回落全量 OCR，不会拿旧文字充数；
       - 缓存上限 2000 条，满了清空。
    """

    BOX_CACHE_CAP = 2000

    def __init__(self, max_wh_ratio: float = MAX_WH_RATIO):
        _install_ratio_clamp(max_wh_ratio)
        from rapidocr_onnxruntime import RapidOCR

        t0 = time.perf_counter()
        n = onnx_threads()
        try:
            self.engine = RapidOCR(intra_op_num_threads=n, inter_op_num_threads=1)
        except TypeError:
            self.engine = RapidOCR()  # 老版本不支持这两个参数就退回默认
        self.lock = threading.Lock()
        blank = np.zeros((64, 320, 3), dtype=np.uint8)
        try:
            self.engine(blank)
        except Exception:
            pass
        self.init_ms = round((time.perf_counter() - t0) * 1000, 1)

        self.box_cache: dict = {}
        self.last_items = None
        self.last_coarse = None
        self.last_shape = None
        self.last_det_limit = None
        self.stats = {"fullRuns": 0, "cacheHits": 0, "cacheMisses": 0, "entries": 0}

    # -- 参数与签名 ----------------------------------------------------

    def _apply_det_mode(self, w: int, h: int) -> None:
        try:
            det = self.engine.text_det
            if w * h > 2_000_000:
                det.limit_type = "max"
                det.limit_side_len = 2000
                self.last_det_limit = 2000
            else:
                det.limit_type = "min"
                det.limit_side_len = 736
                self.last_det_limit = 736
        except Exception:
            pass

    @staticmethod
    def _fnv(data: bytes) -> int:
        h = 1469598103934665603
        for b in data:
            h ^= b
            h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        return h

    @staticmethod
    def _bbox(box) -> tuple:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x, y = int(round(min(xs))), int(round(min(ys)))
        return x, y, int(round(max(xs) - x)), int(round(max(ys) - y))

    def _coarse_sig(self, arr) -> int:
        try:
            small = cv2.resize(cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY), (16, 9), interpolation=cv2.INTER_AREA)
            return self._fnv(small.tobytes())
        except Exception:
            return 0

    def _box_sig(self, arr, x: int, y: int, w: int, h: int) -> int:
        try:
            dx, dy = int(w * 0.1), int(h * 0.1)
            x0, y0 = max(0, x + dx), max(0, y + dy)
            x1, y1 = min(arr.shape[1], x + w - dx), min(arr.shape[0], y + h - dy)
            if x1 <= x0 or y1 <= y0:
                return 0
            patch = arr[y0:y1, x0:x1]
            small = cv2.resize(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), (24, 8), interpolation=cv2.INTER_AREA)
            return self._fnv(small.tobytes())
        except Exception:
            return 0

    @staticmethod
    def _box_key(sig: int, w: int, h: int) -> tuple:
        return (sig, max(1, w // 4), max(1, h // 4))

    def _remember(self, arr, els) -> None:
        items = []
        for el in els:
            x, y, w, h = el["box"]
            sig = self._box_sig(arr, x, y, w, h)
            if len(self.box_cache) >= self.BOX_CACHE_CAP:
                self.box_cache.clear()
            self.box_cache[self._box_key(sig, w, h)] = (el["text"], el["score"])
            items.append({"text": el["text"], "score": el["score"], "box": [x, y, w, h], "sig": sig})
        self.last_items = items
        self.stats["entries"] = len(self.box_cache)

    def _elements(self, res) -> list:
        els = []
        for item in res or []:
            box, text, score = item[0], str(item[1]), float(item[2])
            x, y, w, h = self._bbox(box)
            if w < 6 or h < 6:
                continue
            els.append({"text": text, "box": [x, y, w, h], "score": round(score, 3), "source": "ocr"})
        return els

    # -- 主入口 --------------------------------------------------------

    def run(self, img: Image.Image) -> tuple[list[dict], dict]:
        arr = np.asarray(img.convert("RGB"))[:, :, ::-1].copy()  # RGB -> BGR
        h, w = arr.shape[:2]
        with self.lock:
            t0 = time.perf_counter()
            self._apply_det_mode(w, h)
            coarse = self._coarse_sig(arr)

            # 快路径：同尺寸 + 同粗签名 → 逐框验证签名，全对就复用
            if coarse == self.last_coarse and self.last_shape == (w, h) and self.last_items:
                ok = True
                for it in self.last_items:
                    x, y, bw, bh = it["box"]
                    if self._box_sig(arr, x, y, bw, bh) != it["sig"]:
                        ok = False
                        break
                if ok:
                    els = [
                        {"text": it["text"], "box": list(it["box"]), "score": it["score"], "source": "ocr"}
                        for it in self.last_items
                    ]
                    total = (time.perf_counter() - t0) * 1000
                    self.stats["cacheHits"] += 1
                    return els, {
                        "ocrMs": round(total, 2),
                        "detMs": 0.0,
                        "clsMs": None,
                        "recMs": 0.0,
                        "cache": "boxes",
                        "detLimit": self.last_det_limit,
                        "boxHit": len(els),
                    }
                self.stats["cacheMisses"] += 1

            res, elapse = self.engine(arr)
            total = (time.perf_counter() - t0) * 1000
            els = self._elements(res)
            self._remember(arr, els)
            self.stats["fullRuns"] += 1
            self.last_coarse = coarse
            self.last_shape = (w, h)
            return els, {
                "ocrMs": round(total, 1),
                "detMs": round(float(elapse[0]) * 1000, 1) if elapse else None,
                "clsMs": round(float(elapse[1]) * 1000, 1) if elapse else None,
                "recMs": round(float(elapse[2]) * 1000, 1) if elapse else None,
                "cache": "full",
                "detLimit": self.last_det_limit,
            }

    def cache_info(self) -> dict:
        s = dict(self.stats)
        total = s["cacheHits"] + s["cacheMisses"]
        s["hitRate"] = round(s["cacheHits"] / total * 100, 1) if total else 0.0
        s["detLimit"] = self.last_det_limit
        return s


# ------------------------------------------------------------------ 图标层（OmniParser ONNX 优先，OpenCV 兜底）

ICON_MODEL_ENV = "DSH_OMNIVISION_ICON_MODEL"
ICON_TARGET = 1280  # 640 对 2560 桌面太糊（只出一个满屏假框），1280 才正常
ICON_CONF = 0.3
ICON_IOU = 0.45

_ICON = {"path": None, "sess": None, "name": None}


def icon_model_path() -> str:
    explicit = (os.environ.get(ICON_MODEL_ENV) or "").strip()
    if explicit:
        return explicit
    return os.path.join(DATA_ROOT, "models", "omniparser-icon", "model.onnx")


def _icon_session(path: str):
    import onnxruntime as ort

    if _ICON["sess"] is not None and _ICON["path"] == path:
        return _ICON["sess"], _ICON["name"]
    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.intra_op_num_threads = onnx_threads()
    so.inter_op_num_threads = 1
    sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
    _ICON.update({"path": path, "sess": sess, "name": sess.get_inputs()[0].name})
    return sess, sess.get_inputs()[0].name


def _nms(boxes: "np.ndarray", scores: "np.ndarray", thr: float) -> list[int]:
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= thr]
    return keep


def _overlaps_text(x, y, w, h, ocr_boxes, ratio=0.35) -> bool:
    """给 OpenCV 兜底用：轮廓框和文字框大面积重叠的直接丢（模型路径改用 blend_icons 合并）。"""
    for bx, by, bw, bh in ocr_boxes:
        ix = max(0, min(x + w, bx + bw) - max(x, bx))
        iy = max(0, min(y + h, by + bh) - max(y, by))
        if ix * iy > ratio * (w * h):
            return True
    return False


def blend_icons(text_items: list[dict], icons: list[dict]) -> tuple[list[dict], dict]:
    """把模型框出的「可点控件」并到里面的文字元素上，元素不多一条、但 interactive/hitbox 保住。

    - 文字框 ≥85% 落在图标框里 → 给那条文字标 interactive=True + widget 框（按钮的点击范围）
    - 图标框里没有文字 → 作为独立的「无字图标」元素保留
    """
    kept: list[dict] = []
    blended = 0
    upgraded: set[int] = set()
    for ic in icons:
        bx, by, bw, bh = ic["box"]
        inside = []
        for idx, t in enumerate(text_items):
            tx, ty, tw, th = t["box"]
            if tw * th <= 0:
                continue
            ix = max(0, min(tx + tw, bx + bw) - max(tx, bx))
            iy = max(0, min(ty + th, by + bh) - max(ty, by))
            if (ix * iy) / float(tw * th) >= 0.85:
                inside.append(idx)
        if inside:
            for idx in inside:
                text_items[idx]["interactive"] = True
                text_items[idx]["widget"] = ic["box"]
                upgraded.add(idx)
            blended += 1
        else:
            kept.append(ic)
    return text_items + kept, {"iconsBlended": blended, "iconsStandalone": len(kept), "iconsInteresting": len(upgraded)}


def detect_icons_model(img: Image.Image, model_path: str | None = None, limit: int = 40) -> list[dict]:
    """OmniParser icon_detect（YOLOv8 ONNX）：框「可点的控件」。

    约束（实测）：输入宽高必须是 32 的倍数，否则 FPN 拼接直接报错。
    做法：等比缩到最长边 ICON_TARGET，再补灰边到 32 的倍数。
    这里不做「和文字重叠就丢」——那件事交给 blend_icons，丢框等于把可点信息也丢了。
    """
    path = model_path or icon_model_path()
    sess, iname = _icon_session(path)
    w, h = img.size
    scale = min(1.0, ICON_TARGET / max(w, h))
    nw, nh = max(32, int(round(w * scale))), max(32, int(round(h * scale)))
    resized = img.resize((nw, nh), Image.BILINEAR)
    pw, ph = math.ceil(nw / 32) * 32, math.ceil(nh / 32) * 32
    canvas = Image.new("RGB", (pw, ph), (114, 114, 114))
    canvas.paste(resized, (0, 0))
    arr = (np.asarray(canvas, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None, ...]
    out = sess.run(None, {iname: arr})[0]
    pred = out[0].T  # (N, 5) = cx, cy, w, h, score
    scores = pred[:, 4]
    m = scores > ICON_CONF
    if not m.any():
        return []
    p = pred[m]
    cx, cy, bw, bh = p[:, 0], p[:, 1], p[:, 2], p[:, 3]
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
    keep = _nms(boxes, scores[m], ICON_IOU)
    sx, sy = w / float(nw), h / float(nh)
    dets: list[dict] = []
    for i in keep:
        x1, y1, x2, y2 = boxes[i]
        bx, by = int(x1 * sx), int(y1 * sy)
        bwc, bhc = int((x2 - x1) * sx), int((y2 - y1) * sy)
        if bwc < 8 or bhc < 8:
            continue
        dets.append({"text": "", "box": [bx, by, bwc, bhc], "score": round(float(scores[m][i]), 3), "source": "icon"})
    dets.sort(key=lambda d: -d["score"])
    return dets[:limit]


def detect_icons(img: Image.Image, ocr_boxes: list[list[int]], limit: int = 30) -> list[dict]:
    """OpenCV 轮廓兜底：不下载模型也能给候选，但「框的是轮廓不是控件」，误报多。

    只有模型文件缺失 / 显式 iconEngine=opencv 时才走这里。
    """
    try:
        import cv2
    except Exception:
        return []

    arr = np.asarray(img.convert("RGB"))[:, :, ::-1].copy()
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cands: list[tuple[int, int, int, int, float]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < 14 or h < 14 or w > 220 or h > 220:
            continue
        ar = w / float(h)
        if ar < 0.5 or ar > 2.2:
            continue
        area = cv2.contourArea(c)
        fill = area / float(w * h) if w * h else 0
        if fill < 0.15:
            continue
        cands.append((x, y, w, h, fill))

    def overlaps_text(x, y, w, h):
        for bx, by, bw, bh in ocr_boxes:
            ix = max(0, min(x + w, bx + bw) - max(x, bx))
            iy = max(0, min(y + h, by + bh) - max(y, by))
            if ix * iy > 0.35 * (w * h):
                return True
        return False

    # 去重：与已选盒子大面积重叠的丢掉
    picked: list[tuple[int, int, int, int, float]] = []
    for cand in sorted(cands, key=lambda c: -c[4]):
        x, y, w, h, _f = cand
        if overlaps_text(x, y, w, h):
            continue
        dup = False
        for px, py, pw, ph, _ in picked:
            ix = max(0, min(x + w, px + pw) - max(x, px))
            iy = max(0, min(y + h, py + ph) - max(y, py))
            if ix * iy > 0.5 * min(w * h, pw * ph):
                dup = True
                break
        if dup:
            continue
        picked.append(cand)
        if len(picked) >= limit:
            break

    out = []
    for x, y, w, h, fill in picked:
        out.append(
            {
                "text": "",
                "box": [x, y, w, h],
                "score": round(min(0.95, 0.55 + fill), 3),
                "source": "icon",
            }
        )
    return out


# ------------------------------------------------------------------ 场景 / 元素语义

BUTTON_WORDS = (
    "确定", "取消", "关闭", "开始", "继续", "返回", "设置", "选项", "是", "否", "下一步",
    "购买", "邀请", "添加", "删除", "保存", "确认", "退出", "重试", "跳过", "领取", "开始游戏",
)
BATTLE_WORDS = ("战斗", "攻击", "技能", "回合", "手牌", "血量", "防御", "胜利", "失败", "速度骰", "卡牌", "闪避", "反击")
MENU_WORDS = ("设置", "选项", "存档", "读档", "主菜单", "开始游戏", "继续游戏", "音量", "画质", "按键", "分辨率")
DIALOG_WORDS = ("对话", "选择", "同意", "拒绝", "接受")


def _desktop_like(elements: list[dict], size: tuple[int, int]) -> bool:
    """桌面兜底特征：右下角有 HH:MM 时钟 / 左边缘一列图标 / 底部一排任务栏图标。"""
    w, h = size
    clock = left = bottom = 0
    for e in elements:
        x, y, ww, hh = e["box"]
        label = str(e.get("label", "")).strip()
        if re.match(r"^\d{1,2}[:：]\d{2}$", label) and (x > w * 0.6 or y > h * 0.85):
            clock += 1
        if x + ww < w * 0.05:
            left += 1
        if y > h * 0.93:
            bottom += 1
    return clock >= 1 or left >= 3 or bottom >= 5


def detect_scene(elements: list[dict], hint: str | None = None, screen_size: tuple[int, int] | None = None, is_full: bool = False) -> str:
    if hint:
        return hint
    if not elements:
        return "unknown"
    # 桌面特征优先：整屏 + 任务栏/桌面图标 → desktop（别被窗口里的「对话」之类的词带偏）
    if is_full and screen_size and _desktop_like(elements, screen_size):
        return "desktop"
    text = " ".join(str(e.get("label", "")) for e in elements)
    scores = {
        "battle": sum(1 for w in BATTLE_WORDS if w in text),
        "menu": sum(1 for w in MENU_WORDS if w in text),
        "dialog": sum(1 for w in DIALOG_WORDS if w in text),
    }
    best, n = max(scores.items(), key=lambda kv: kv[1])
    if n >= 1:
        return best
    return "unknown"


def mark_interactive(elements: list[dict]) -> None:
    for e in elements:
        t = str(e.get("label", "")).strip()
        x, y, w, h = e["box"]
        word_hit = any(word in t for word in BUTTON_WORDS)
        # 尺寸像按钮：中等高度、宽度不至于只有一个字（纯尺寸启发，仅作提示）
        shape_hit = 24 <= h <= 56 and 60 <= w <= 300 and len(t) >= 2
        # 模型已经把这个文字框认成「控件里面的字」时，别再被尺寸启发覆盖掉
        e["interactive"] = bool(e.get("interactive") or word_hit or shape_hit or e.get("source") == "icon")


def to_elements(raw: list[dict], region: tuple[int, int, int, int], max_n: int, min_score: float) -> list[dict]:
    els = []
    for e in raw:
        src = e.get("source", "ocr")
        # 图标候选的分数来自检测模型（0.3 起），不能拿 OCR 的 0.5 线卡它
        if src != "icon" and e["score"] < min_score:
            continue
        x, y, w, h = e["box"]
        el = {
            "label": e["text"],
            "box": [x + region[0], y + region[1], w, h],
            "score": e["score"],
            "source": src,
        }
        if e.get("interactive"):
            el["interactive"] = True
        if e.get("widget"):
            el["widget"] = list(e["widget"])
        els.append(el)
    # 按「行」排序：y 相近（同 12px 带内）视为一行，再按 x
    els.sort(key=lambda e: (round(e["box"][1] / 12), e["box"][0]))
    for i, e in enumerate(els, 1):
        e["id"] = f"e{i}"
        e["center"] = [e["box"][0] + e["box"][2] // 2, e["box"][1] + e["box"][3] // 2]
    mark_interactive(els)
    return els[:max_n]


def apply_semantic(elements: list[dict], sem: dict) -> None:
    """把模型给的「修订文字」按 id 合回元素。模型没提的元素保持原样，但标 confident=false。"""
    fixes: dict[str, tuple[str, bool]] = {}
    for item in sem.get("elements") or []:
        if not isinstance(item, dict):
            continue
        i = str(item.get("id") or "").strip()
        lab = item.get("label")
        if not i or not isinstance(lab, str) or not lab.strip():
            continue
        fixes[i] = (lab.strip()[:200].replace("\n", " "), bool(item.get("confident", True)))
    for e in elements:
        if e["id"] in fixes:
            label, conf = fixes[e["id"]]
            e["label"] = label
            e["confident"] = conf
        else:
            e["confident"] = False


# ------------------------------------------------------------------ 缓存

HASH_TOLERANCE = 5  # dHash 汉明距离 <= 5 视为同一界面（吃掉光标/抗锯齿抖动）


class FrameCache:
    def __init__(self, cap: int = 48):
        self.cap = cap
        self.items: "OrderedDict[int, dict]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def find(self, h: int, genre: tuple) -> dict | None:
        for key, item in list(self.items.items()):
            if key[1] != genre:
                continue
            if hamming(key[0], h) <= HASH_TOLERANCE:
                self.items.move_to_end(key)
                self.hits += 1
                return item
        self.misses += 1
        return None

    def put(self, h: int, genre: tuple, payload: dict) -> None:
        key = (h, genre)
        self.items[key] = {"genre": genre, "payload": payload, "at": time.time()}
        self.items.move_to_end(key)
        while len(self.items) > self.cap:
            self.items.popitem(last=False)

    def clear(self) -> int:
        n = len(self.items)
        self.items.clear()
        return n


# ------------------------------------------------------------------ 语义层（可插拔后端）

LLM_BASE = "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1"
LLM_DEFAULT_MODEL = "qwen4-max"
PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "understand.md")
SCENES = ("desktop", "browser", "editor", "game", "dialog", "unknown")

# 语义后端：local（默认，本地 llama-server）/ cloud（百炼）/ off（纯 OCR）
ENV_BACKEND = "DSH_OMNIVISION_BACKEND"
ENV_LOCAL_ENDPOINT = "DSH_OMNIVISION_LOCAL_ENDPOINT"
ENV_LOCAL_MODEL = "DSH_OMNIVISION_LOCAL_MODEL"
ENV_LLAMA_SERVER = "DSH_OMNIVISION_LLAMA_SERVER"
ENV_LLAMA_MODEL = "DSH_OMNIVISION_LLAMA_MODEL"
ENV_LLAMA_MMPROJ = "DSH_OMNIVISION_LLAMA_MMPROJ"

#: 数据根目录：--data-dir / DSH_OMNIVISION_DIR / 默认 ~/.dsh/omnivision（不写死任何盘符）
DATA_ROOT = os.environ.get("DSH_OMNIVISION_DIR") or os.path.join(os.path.expanduser("~"), ".dsh", "omnivision")
LOCAL_ENDPOINT_DEFAULT = "http://127.0.0.1:9883/v1"
LOCAL_MODEL_DEFAULT = "qwen2.5-vl-3b"
LOCAL_CROP_MAX = 20  # 本地 3B 一次最多读多少行裁图（太多会超出上下文）


def data_root() -> str:
    return DATA_ROOT


def frames_dir_default() -> str:
    return os.path.join(DATA_ROOT, "frames")


def backend_choice(p: dict) -> str:
    b = str(p.get("backend") or os.environ.get(ENV_BACKEND) or "local").strip().lower()
    return b if b in ("local", "cloud", "off") else "local"


def local_endpoint() -> str:
    return (os.environ.get(ENV_LOCAL_ENDPOINT) or LOCAL_ENDPOINT_DEFAULT).rstrip("/")


def local_model() -> str:
    return os.environ.get(ENV_LOCAL_MODEL) or LOCAL_MODEL_DEFAULT


def build_crop_grid(frame_path: str, elements: list[dict], offset: tuple[int, int], max_n: int = 20, target_h: int = 48) -> tuple[bytes, list[str]]:
    """把元素框裁出来放大拼成一列：3B 看小字看不清，但看放大后的单行能读准。"""
    src = Image.open(frame_path).convert("RGB")
    ox, oy = offset
    rows: list[tuple[str, Image.Image]] = []
    for e in elements[:max_n]:
        if len(str(e.get("label", "")).strip()) <= 1:
            continue  # 单字符/符号（"?" "口"）没校对价值，还容易被模型读成编号
        x, y, w, h = e["box"]
        box = (max(0, x - ox - 3), max(0, y - oy - 3), min(src.width, x - ox + w + 3), min(src.height, y - oy + h + 3))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            continue
        crop = src.crop(box)
        scale = max(1, int(round(target_h / max(1, crop.height))))
        if scale > 1:
            crop = crop.resize((min(1600, crop.width * scale), crop.height * scale), Image.LANCZOS)
        rows.append((str(e["id"]), crop))
    if not rows:
        raise RuntimeError("没有可裁的元素")
    label_w, gap = 90, 14
    width = label_w + max(c.width for _, c in rows) + 20
    height = sum(c.height + gap for _, c in rows) + gap
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    try:
        from PIL import ImageFont

        font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 22)
    except Exception:
        font = None
    y = gap
    for eid, crop in rows:
        if font is not None:
            draw.text((8, y + crop.height // 2 - 12), eid, fill=(200, 0, 0), font=font)
        else:
            draw.text((8, y + crop.height // 2 - 6), eid, fill=(200, 0, 0))
        canvas.paste(crop, (label_w, y))
        y += crop.height + gap
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=88)
    return buf.getvalue(), [eid for eid, _ in rows]


def read_local_prompt() -> str:
    path = os.path.join(os.path.dirname(PROMPT_PATH), "understand_local.md")
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return '读图里每一行的文字。只输出 JSON 数组：[{"id":"e3","text":"..."}]'


def read_ask_prompt() -> str:
    path = os.path.join(os.path.dirname(PROMPT_PATH), "ask.md")
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return "看截图回答问题。只根据看得到的作答，看不到就说看不出来。\n元素：{{ELEMENTS}}\n问题：{{QUESTION}}"


def build_ask_prompt(question: str, elements: list[dict]) -> str:
    tpl = read_ask_prompt()
    lines = []
    for e in elements[:24]:
        label = str(e.get("label", "")).replace("\n", " ")
        if not label.strip():
            continue
        lines.append(f'{e["id"]} | {",".join(str(v) for v in e["box"])} | {label}')
    body = "\n".join(lines) if lines else "（这一帧没识别出文字元素，请只看图）"
    return tpl.replace("{{ELEMENTS}}", body).replace("{{QUESTION}}", question.strip())


def extract_json_array(text: str) -> list:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        raise ValueError("模型没返回 JSON 数组")
    arr = json.loads(s[i: j + 1])
    return [x for x in arr if isinstance(x, dict)]


def load_dashscope_key() -> tuple[str, str]:
    """读 key，顺序照抄 qwen-ask.py：环境变量 -> ~/.dsh/.credentials.yaml 的 refs。只读，不改文件。"""
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if key:
        return key, "env"
    cred = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    if not os.path.exists(cred):
        return "", "no-credentials-file"
    try:
        import yaml

        data = yaml.safe_load(open(cred, encoding="utf-8")) or {}
        ref = (data.get("refs") or {}).get("DASHSCOPE_API_KEY")
        if isinstance(ref, str) and ref.strip():
            return ref.strip(), "credentials/refs"
    except Exception:
        pass
    try:
        for line in open(cred, encoding="utf-8"):
            if line.strip().startswith("DASHSCOPE_API_KEY:"):
                return line.split(":", 1)[1].strip(), "credentials/plain"
    except Exception:
        pass
    return "", "absent"


def make_thumbnail(frame_path: str, max_w: int = 800, quality: int = 70) -> tuple[str, int, int]:
    """截图缩到宽 ≤ max_w，jpg base64。一张约几百 image tokens。"""
    img = Image.open(frame_path).convert("RGB")
    w, h = img.size
    if w > max_w:
        img = img.resize((max_w, max(1, round(h * max_w / w))), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.size[0], img.size[1]


def build_understand_prompt(elements: list[dict], screen: tuple[int, int]) -> str:
    tpl = _read_prompt()
    lines = []
    for e in elements:
        label = str(e.get("label", "")).replace("\n", " ")
        lines.append(f'{e["id"]} | {",".join(str(v) for v in e["box"])} | {label}')
    body = "\n".join(lines) if lines else "（这一帧没识别出文字元素）"
    return tpl.replace("{{SCREEN}}", f"{screen[0]}x{screen[1]}").replace("{{ELEMENTS}}", body)


_PROMPT_CACHE = {"mtime": 0, "text": ""}


def _read_prompt() -> str:
    try:
        m = os.path.getmtime(PROMPT_PATH)
        if _PROMPT_CACHE["mtime"] != m:
            _PROMPT_CACHE["text"] = open(PROMPT_PATH, encoding="utf-8").read()
            _PROMPT_CACHE["mtime"] = m
        return _PROMPT_CACHE["text"]
    except Exception:
        return "纠正 OCR 文字、判断场景、写一句概括。只输出 JSON：{\"scene\":\"\",\"summary\":\"\",\"elements\":[{\"id\":\"\",\"label\":\"\",\"confident\":true}]}\n元素：\n{{ELEMENTS}}"


def extract_json(text: str) -> dict:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("模型没返回 JSON")
    return json.loads(s[i: j + 1])


def _vision_body(model: str, prompt: str, image_b64: str, extra: dict | None = None) -> dict:
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }
        ],
        "max_tokens": 2000,
        "temperature": 0.2,
    }
    if extra:
        body.update(extra)
    return body


# ---------------------------------------------------------------- 套餐用量（本地估算）

def _usage_path() -> str:
    """运行时算：main() 会把 DATA_ROOT 改成 --data-dir，模块级常量会拿到旧值。"""
    return os.path.join(DATA_ROOT, "usage.json")


def _usage_read() -> dict:
    u = {}
    try:
        with open(_usage_path(), "r", encoding="utf-8-sig") as f:
            u = json.load(f)
    except Exception:
        u = {}
    if not isinstance(u, dict):
        u = {}
    month = time.strftime("%Y-%m")
    if u.get("month") != month:
        u = {"month": month, "cloudCalls": 0, "promptTokens": 0, "completionTokens": 0, "totalTokens": 0, "lastAt": "", "lastModel": ""}
    return u


def _usage_write(u: dict) -> None:
    try:
        os.makedirs(DATA_ROOT, exist_ok=True)
        tmp = _usage_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(u, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _usage_path())
    except Exception:
        pass


def record_cloud_usage(usage: dict, model: str = "") -> None:
    """每次 cloud 调用累加；usage 缺失时也记一次调用（tokens 记 0）。"""
    u = _usage_read()
    u["cloudCalls"] = int(u.get("cloudCalls") or 0) + 1
    pt = int((usage or {}).get("prompt_tokens") or 0)
    ct = int((usage or {}).get("completion_tokens") or 0)
    tt = int((usage or {}).get("total_tokens") or (pt + ct))
    u["promptTokens"] = int(u.get("promptTokens") or 0) + pt
    u["completionTokens"] = int(u.get("completionTokens") or 0) + ct
    u["totalTokens"] = int(u.get("totalTokens") or 0) + tt
    u["lastAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if model:
        u["lastModel"] = model
    _usage_write(u)


def usage_budget() -> dict:
    u = _usage_read()
    try:
        credits = float(os.environ.get("DSH_OMNIVISION_MONTHLY_CREDITS") or 20000)
    except Exception:
        credits = 20000.0
    try:
        tpc = float(os.environ.get("DSH_OMNIVISION_TOKENS_PER_CREDIT") or 1000)
    except Exception:
        tpc = 1000.0
    est = (float(u.get("totalTokens") or 0) / tpc) if tpc > 0 else 0.0
    pct = (est / credits * 100.0) if credits > 0 else 0.0
    return {
        "month": u.get("month"),
        "cloudCalls": int(u.get("cloudCalls") or 0),
        "promptTokens": int(u.get("promptTokens") or 0),
        "completionTokens": int(u.get("completionTokens") or 0),
        "totalTokens": int(u.get("totalTokens") or 0),
        "lastAt": u.get("lastAt") or "",
        "lastModel": u.get("lastModel") or "",
        "budget": {
            "monthlyCredits": credits,
            "tokensPerCredit": tpc,
            "estimatedCredits": round(est, 2),
            "percent": round(pct, 1),
            "warnAt": 80,
            "warn": pct >= 80.0,
            "note": "本地估算：totalTokens/tokensPerCredit；官方无用量 API",
        },
    }


def _post_chat(base: str, body: dict, headers: dict, timeout: float, fallback_model: str = "") -> tuple[dict, dict]:
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode("utf-8"), headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上 {base}：{e.reason}")
    ms = round((time.perf_counter() - t0) * 1000, 1)
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not content.strip():
        u = data.get("usage") or {}
        raise RuntimeError(f"模型返回空正文（completion_tokens={u.get('completion_tokens')}）")
    return data, {
        "content": content,
        "usage": data.get("usage") or {},
        "latencyMs": ms,
        "model": data.get("model") or fallback_model,
    }


def call_llm(prompt: str, image_b64: str, model: str, timeout: float = 90.0) -> tuple[dict, dict]:
    """cloud 后端：百炼 Token Plan（qwen4-max）。"""
    key, src = load_dashscope_key()
    if not key:
        raise RuntimeError(f"找不到 DASHSCOPE_API_KEY（{src}）")
    body = _vision_body(model, prompt, image_b64, {"enable_thinking": False})
    data, meta = _post_chat(LLM_BASE, body, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout, model)
    try:
        record_cloud_usage(meta.get("usage") or {}, meta.get("model") or model)
    except Exception:
        pass
    return data, meta


def call_local_llm(prompt: str, image_b64: str, model: str, timeout: float = 120.0) -> tuple[dict, dict]:
    """local 后端：llama-server 的 OpenAI 兼容端点（GGUF + mmproj）。"""
    body = _vision_body(model, prompt, image_b64)
    return _post_chat(local_endpoint(), body, {"Content-Type": "application/json"}, timeout, model)


# ---------------------------------------------------------------- 本地模型进程

def _local_port() -> int:
    try:
        return int(urllib.parse.urlparse(local_endpoint()).port or 9883)
    except Exception:
        return 9883


def llama_healthy(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(local_endpoint() + "/models", timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


_LLAMA = {"proc": None, "bootMs": None, "lastError": "", "startedAt": 0.0}


def _stop_llama():
    proc = _LLAMA.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


atexit.register(_stop_llama)


def llama_paths() -> dict:
    return {
        "server": os.environ.get(ENV_LLAMA_SERVER) or os.path.join(DATA_ROOT, "llama", "llama-server.exe"),
        "model": os.environ.get(ENV_LLAMA_MODEL) or os.path.join(DATA_ROOT, "models", "qwen2.5-vl-3b", "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"),
        "mmproj": os.environ.get(ENV_LLAMA_MMPROJ) or os.path.join(DATA_ROOT, "models", "qwen2.5-vl-3b", "mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf"),
    }


def ensure_llama(timeout: float = 150.0) -> dict:
    """llama-server 没在跑就拉起来；已经在跑（比如上次留下的）就直接复用。"""
    if llama_healthy():
        return {"ok": True, "started": False, "proc": _LLAMA["proc"]}
    p = llama_paths()
    if not os.path.exists(p["server"]):
        raise RuntimeError(f"找不到 llama-server：{p['server']}")
    if not os.path.exists(p["model"]):
        raise RuntimeError(f"找不到本地模型：{p['model']}")
    if not os.path.exists(p["mmproj"]):
        raise RuntimeError(f"找不到 mmproj：{p['mmproj']}")
    log_dir = os.path.join(DATA_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "llama-server.log")
    args = [
        p["server"],
        "-m", p["model"],
        "--mmproj", p["mmproj"],
        "-ngl", "99",
        "-c", "4096",
        "--host", "127.0.0.1",
        "--port", str(_local_port()),
        "--alias", local_model(),
        "-np", "1",
    ]
    log("启动 llama-server：" + " ".join(args))
    with open(log_path, "ab") as lf:
        proc = subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT, cwd=os.path.dirname(p["server"]))
    _LLAMA.update({"proc": proc, "startedAt": time.time(), "lastError": ""})
    t0 = time.time()
    while time.time() - t0 < timeout:
        if llama_healthy(1.0):
            _LLAMA["bootMs"] = round((time.time() - t0) * 1000)
            return {"ok": True, "started": True, "bootMs": _LLAMA["bootMs"], "log": log_path, "proc": proc}
        if proc.poll() is not None:
            tail = ""
            try:
                with open(log_path, "rb") as f:
                    tail = f.read()[-1500:].decode("utf-8", "replace")
            except Exception:
                pass
            raise RuntimeError(f"llama-server 启动即退出（code={proc.returncode}）：{tail}")
        time.sleep(1.0)
    raise RuntimeError(f"llama-server 启动超时（{timeout}s），日志：{log_path}")


# ------------------------------------------------------------------ 场景图：关系 + 候选动作（纯几何计算）

REL_MAX_ELEMENTS = 30
ROLE_ACTION = {
    "Button": "click",
    "Hyperlink": "click",
    "MenuItem": "click",
    "Edit": "type",
    "ComboBox": "type",
    "CheckBox": "toggle",
    "RadioButton": "toggle",
    "TabItem": "select",
    "ListItem": "select",
    "TreeItem": "select",
    "Tab": "select",
    "Slider": "set",
    "Spinner": "set",
}


def compute_actions(elements: list[dict]) -> list[dict]:
    """每个可交互元素的候选动作；enabled=false 的元素标 blocked，不给动作。"""
    out = []
    for e in elements:
        act = ROLE_ACTION.get(e["role"], "click")
        if e.get("enabled") is False:
            out.append({"id": e["id"], "action": None, "blocked": True})
        else:
            out.append({"id": e["id"], "action": act})
    return out


def compute_relations(elements: list[dict], texts: list[dict]) -> tuple[list[dict], dict]:
    """几何关系：rightOf/below/labelFor。只算可交互元素，n>30 直接跳过。"""
    meta = {"skipped": None, "labelForUnresolved": []}
    if len(elements) > REL_MAX_ELEMENTS:
        meta["skipped"] = "too_many"
        return [], meta

    rels: list[dict] = []
    boxes = [(e, e["box"], e["center"]) for e in elements]
    for i, (a, ba, ca) in enumerate(boxes):
        for j, (b, bb, cb) in enumerate(boxes):
            if i >= j:
                continue
            # 横向关系：一个中心在另一个左侧，纵向有重叠，横向不相交
            y_ov = min(ba[1] + ba[3], bb[1] + bb[3]) - max(ba[1], bb[1])
            x_ov = min(ba[0] + ba[2], bb[0] + bb[2]) - max(ba[0], bb[0])
            if ca[0] < cb[0] and y_ov > 0 and x_ov <= 0:
                rels.append({"a": b["id"], "rel": "rightOf", "b": a["id"], "gap": max(0, bb[0] - (ba[0] + ba[2]))})
            if ca[1] < cb[1] and x_ov > 0 and y_ov <= 0:
                rels.append({"a": b["id"], "rel": "below", "b": a["id"], "gap": max(0, bb[1] - (ba[1] + ba[3]))})
            if len(rels) >= 200:
                break

    # labelFor：Text 标签 → Edit/ComboBox。识别不出来就记进 labelForUnresolved，绝不乱标。
    label_rels: list[dict] = []
    for e in elements:
        if e["role"] not in ("Edit", "ComboBox"):
            continue
        ex, ey, ew, eh = e["box"]
        ecy = ey + eh // 2
        best = None
        for t in texts:
            tx, ty, tw, th = t["box"]
            tcy = ty + th // 2
            if abs(tcy - ecy) <= 20 and tx + tw <= ex + 2:
                gap = ex - (tx + tw)
                if 0 <= gap <= 80 and (best is None or gap < best[0]):
                    best = (gap, t["name"])
            elif tx < ex + ew and tx + tw > ex and ty + th <= ey + 2:
                gap = ey - (ty + th)
                if 0 <= gap <= 80 and (best is None or gap < best[0]):
                    best = (gap, t["name"])
        if best is not None:
            label_rels.append({"b": e["id"], "rel": "labelFor", "label": best[1], "gap": best[0]})
        else:
            meta["labelForUnresolved"].append(e["id"])
    # labelFor 放最前：它比「谁在谁右边」重要，别被渲染截断掉
    return label_rels + rels, meta


# ------------------------------------------------------------------ UIA（结构层，纯本机）

UIA_PIN = "2.0.29"
UIA_INTERACTIVE = {
    "Button", "Edit", "ComboBox", "ListItem", "CheckBox", "RadioButton", "MenuItem",
    "Tab", "TabItem", "Hyperlink", "TreeItem", "Slider", "Spinner",
}
UIA_SELECTABLE = {"ListItem", "TabItem", "TreeItem", "MenuItem", "RadioButton", "CheckBox"}


def _uia_available() -> tuple[bool, str]:
    try:
        import uiautomation as auto  # noqa: F401

        ver = getattr(auto, "__version__", None) or "2.0.29"
        return True, str(ver)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _win_title(hwnd: int) -> str:
    u = ctypes.windll.user32
    n = u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _win_pid(hwnd: int) -> int:
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _proc_name(pid: int) -> str:
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _rect_of(ctrl) -> list[int]:
    try:
        r = ctrl.BoundingRectangle
        if r is None:
            return [0, 0, 0, 0]
        return [int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)]
    except Exception:
        return [0, 0, 0, 0]


def _uia_basic(ctrl) -> tuple[str, str]:
    """最便宜的两个字段：名字 + 角色。别的属性等确定要保留这个节点再取。"""
    try:
        name = (ctrl.Name or "").strip()
    except Exception:
        name = ""
    try:
        ctype = ctrl.ControlTypeName or ""
    except Exception:
        ctype = ""
    role = ctype[:-7] if ctype.endswith("Control") else ctype
    return name, role


def _uia_info(ctrl, name: str, role: str) -> dict:
    box = _rect_of(ctrl)
    try:
        enabled = bool(ctrl.IsEnabled)
    except Exception:
        enabled = None
    selected = None
    if role in UIA_SELECTABLE:
        for getter in ("GetSelectionItemPattern", "GetTogglePattern"):
            try:
                pat = getattr(ctrl, getter)()
                if pat is None:
                    break
                val = getattr(pat, "IsSelected", None)
                if val is None:
                    val = getattr(pat, "ToggleState", None)
                    if val is not None:
                        val = str(val) == "On"
                if val is not None:
                    selected = bool(val)
                break
            except Exception:
                continue
    focus = None
    try:
        focus = bool(ctrl.HasKeyboardFocus)
    except Exception:
        focus = None
    return {
        "name": name,
        "role": role,
        "box": box,
        "enabled": enabled,
        "selected": selected,  # UIA 给不了就保持 None（未知），不要假装 false
        "focus": focus,
    }


def _uia_scan(hwnds: list[int], depth: int, filt: str, budget_ms: float, max_nodes: int = 3000) -> dict:
    # uiautomation 在工作线程里必须自己 CoInitialize，否则 GetRootControl 直接抛
    # 「尚未调用 CoInitialize」并静默失败（表现为「无树」）。
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    except Exception:
        pass
    try:
        return _uia_scan_impl(hwnds, depth, filt, budget_ms, max_nodes)
    finally:
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass


def _uia_scan_impl(hwnds: list[int], depth: int, filt: str, budget_ms: float, max_nodes: int = 3000) -> dict:
    """两阶段：先扫内容区（Chromium 的 Chrome_RenderWidgetHostHWND / Document），再扫外壳。

    v0.4.0 的纯 DFS 先读外壳，收藏夹菜单一开就把 300ms 吃光，页面主体被截。
    现在内容区先扫（多给 6 层余量），外壳用剩下的预算；截断会标清楚在哪一层。
    """
    import uiautomation as auto

    auto.SetGlobalSearchTimeout(2)
    t0 = time.perf_counter()
    state: dict = {
        "nodes": 0,
        "els": [],
        "dropped": 0,
        "contentCut": False,
        "shellCut": False,
        "contentAbandoned": False,
        "contentNodes": 0,
        "contentInter": 0,
        "texts": [],  # 文本节点池：只用来算 labelFor，不进 elements
        "deadline": t0 + budget_ms / 1000.0,  # 搜索耗时会把截止时间往后顺延，不占扫描预算
    }

    def over() -> bool:
        return time.perf_counter() > state["deadline"] or state["nodes"] >= max_nodes

    def take(c, d: int, layer: str) -> None:
        state["nodes"] += 1
        name, role = _uia_basic(c)
        interactive = role in UIA_INTERACTIVE
        if layer == "content":
            state["contentNodes"] += 1
            if interactive:
                state["contentInter"] += 1
        if role == "Text" and name and len(state["texts"]) < 400:
            b = _rect_of(c)
            if b[2] > 0 and b[3] > 0:
                state["texts"].append({"name": name, "box": b})
        if filt != "all" and not interactive:
            return
        info = _uia_info(c, name, role)
        if info["box"][2] <= 0 or info["box"][3] <= 0:
            state["dropped"] += 1
            return
        info["_layer"] = layer
        state["els"].append(info)

    def scan_subtree(node, start_depth: int, max_depth: int, layer: str) -> bool:
        stack = [(node, start_depth)]
        while stack:
            if over():
                if layer == "content":
                    state["contentCut"] = True
                else:
                    state["shellCut"] = True
                return False
            c, d = stack.pop()
            take(c, d, layer)
            # 内容区早退：扫了 60 个节点还全是文本（记事本的正文就是这样），
            # 说明这块不是「可点的内容」，把剩下的预算还给外壳（菜单/工具栏）。
            if layer == "content" and filt != "all" and state["contentNodes"] >= 60 and state["contentInter"] == 0:
                state["contentAbandoned"] = True
                return False
            if d < max_depth:
                try:
                    kids = c.GetChildren()
                except Exception:
                    kids = []
                for k in reversed(kids):
                    stack.append((k, d + 1))
        return True

    def find_content_root(win):
        """服务端搜索，命中很快；找不到就 0.15s 放弃（别让非浏览器窗口白等）。

        顺序：先 Chromium 的渲染宿主（真正装网页内容的那层），再退回任意 Document。
        直接找 Document 会先撞上 Edge 的空白 WebView 壳，内容区反而选错。
        返回 (控件或 None, 搜索耗时ms)——搜索耗时不占扫描预算。
        """
        t_search = time.perf_counter()
        auto.SetGlobalSearchTimeout(0.15)
        found = None
        try:
            try:
                c = auto.Control(searchFromControl=win, ClassName="Chrome_RenderWidgetHostHWND", searchDepth=14)
                if c is not None:
                    box = _rect_of(c)
                    if box[2] > 0 and box[3] > 0:
                        found = c
            except Exception:
                pass
            if found is None:
                try:
                    c = auto.Control(searchFromControl=win, ControlType=auto.ControlType.DocumentControl, searchDepth=14)
                    if c is not None:
                        box = _rect_of(c)
                        if box[2] > 0 and box[3] > 0:
                            found = c
                except Exception:
                    pass
        finally:
            auto.SetGlobalSearchTimeout(2)
        return found, (time.perf_counter() - t_search) * 1000

    content_roots = 0
    content_complete = True
    shell_complete = True
    for hwnd in hwnds:
        try:
            root = auto.ControlFromHandle(hwnd)
        except Exception:
            continue
        if root is None:
            continue

        content, search_ms = find_content_root(root)
        state["deadline"] += search_ms / 1000.0  # 搜索耗时不计入扫描预算
        shell_depth = depth
        if content is not None:
            content_roots += 1
            ok = scan_subtree(content, 0, depth + 6, "content")
            if not ok and not state["contentAbandoned"]:
                content_complete = False
            # 内容区已经单独扫过；外壳（标题栏/工具栏/标签栏/菜单）都在浅层，
            # 只扫到 6 层就够，避免把内容区重复遍历一遍白烧预算。
            shell_depth = min(depth, 6)

        if state["contentCut"]:
            shell_complete = False
            break

        try:
            kids = root.GetChildren()
        except Exception:
            kids = []
        for k in reversed(kids):
            if over():
                state["shellCut"] = True
                shell_complete = False
                break
            if not scan_subtree(k, 1, shell_depth, "shell"):
                shell_complete = False
                break
        if state["shellCut"]:
            break

    # 去重（内容区先扫、外壳再扫会把内容区重复收一遍）
    seen: set = set()
    deduped = []
    for e in state["els"]:
        key = (e["name"], e["role"], tuple(e["box"]), e["enabled"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)

    truncated = bool(state["contentCut"] or state["shellCut"])
    where = "content" if state["contentCut"] else ("shell" if state["shellCut"] else None)
    content_elements = sum(1 for e in deduped if e.get("_layer") == "content")
    return {
        "elements": deduped,
        "nodes": state["nodes"],
        "texts": state["texts"],
        "truncated": truncated,
        "truncatedWhere": where,
        "contentElements": content_elements,
        "coverage": {
            "contentRoots": content_roots,
            "contentComplete": content_complete,
            "shellComplete": shell_complete,
            "contentAbandoned": state["contentAbandoned"],
        },
        "droppedNoRect": state["dropped"],
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _uia_scan_threaded(hwnds: list[int], depth: int, filt: str, budget_ms: float):
    """UIA 调用可能卡在某个窗口上，放到线程里并设硬上限，绝不卡死主请求。"""
    box: dict = {}

    def work():
        try:
            box["r"] = _uia_scan(hwnds, depth, filt, budget_ms)
        except Exception as e:
            box["e"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join((budget_ms + 2500) / 1000.0)
    if th.is_alive():
        return None, "uia 读取超时"
    if "e" in box:
        return None, box["e"]
    return box.get("r"), None


# ------------------------------------------------------------------ 主服务

class Service:
    def __init__(self, args):
        self.args = args
        self.cache = FrameCache(cap=args.cache_size)
        self.sem_cache = FrameCache(cap=args.cache_size)
        self.prev_small: np.ndarray | None = None
        self.prev_region: tuple | None = None
        self.ocr = OcrEngine(max_wh_ratio=args.max_wh_ratio) if not args.no_ocr else None
        self.frames_dir = args.frames_dir
        os.makedirs(self.frames_dir, exist_ok=True)
        self.screen = virtual_screen()
        self.calls = 0
        self.last_look = None
        self.started = time.time()
        self.model = (os.environ.get("DSH_OMNIVISION_MODEL") or LLM_DEFAULT_MODEL).strip()
        self.llm_calls = 0
        self.llm_errors = 0
        self.last_understand = None
        self.watcher = Watcher(self)
        self.last_elements: dict = {"at": 0.0, "source": "", "elements": []}
        self.companion = CompanionManager(self, dry_run=(os.environ.get("OMNIVISION_DRY_RUN") == "1"))
        atexit.register(self.companion.close)

    # -- 元素缓存：给 vision_action 的 id→坐标 用（5 秒过期）
    def _cache_elements(self, source: str, elements: list[dict], region_spec: str = "") -> None:
        self.last_elements = {
            "at": time.time(),
            "source": source,
            "region": region_spec,
            "elements": [
                {
                    "id": e.get("id"),
                    "box": e.get("box"),
                    "center": e.get("center"),
                    "name": e.get("name") or e.get("label") or "",
                    "role": e.get("role") or "",
                    "enabled": e.get("enabled"),
                }
                for e in (elements or [])
            ],
        }

    def latest(self) -> dict:
        d = self.last_elements or {}
        at = float(d.get("at") or 0)
        age_ms = int((time.time() - at) * 1000) if at else None
        return {
            "ok": True,
            "source": d.get("source") or "",
            "ageMs": age_ms,
            "expired": bool(age_ms is None or age_ms > 5000),
            "elements": d.get("elements") or [],
        }

    def _auto_rescan(self, want: dict, cache: dict) -> tuple[dict | None, str]:
        """id 过期时按元素名重扫一次；同名多个/没名字就放弃，绝不猜。"""
        name = str(want.get("name") or "").strip()
        if not name:
            return None, "元素没有名字，autoRescan 无法可靠匹配（不猜），请重新扫描后指定 id"
        source = (cache.get("source") or "uia").strip()
        region = (cache.get("region") or "foreground").strip()
        try:
            if source == "uia":
                res = self.ui_tree({"region": region, "filter": "interactive"})
            else:
                res = self.look({"region": region, "saveFrame": False})
        except Exception as e:
            return None, f"autoRescan 重扫失败：{type(e).__name__}: {e}"
        els = res.get("elements") or []
        cands = [e for e in els if str(e.get("name") or e.get("label") or "").strip() == name]
        role = str(want.get("role") or "").strip()
        if role:
            same = [e for e in cands if str(e.get("role") or "").strip() == role]
            if same:
                cands = same
        if not cands:
            return None, f"autoRescan 重扫后找不到「{name}」（界面可能已变，不猜坐标）"
        if len(cands) > 1:
            return None, f"autoRescan 重扫后有 {len(cands)} 个「{name}」，不猜是哪一个，请重新扫描后指定 id"
        return cands[0], ""

    # -- 动作层：定位 → 执行 → 验证
    def _act_once(self, p: dict) -> dict:
        t0 = time.perf_counter()
        action = str(p.get("action") or "").strip().lower()
        if action not in ("click", "double_click", "right_click", "type", "key"):
            return {"ok": False, "error": f"不支持的 action：{action}（click/double_click/right_click/type/key）"}
        verify = str(p.get("verify") or "auto").strip().lower()
        if verify not in ("auto", "none"):
            verify = "auto"
        wait_before = max(0, min(5000, int(float(p.get("waitMs") or 120))))

        el = None
        warning = ""
        x = y = None
        rescan_info = None
        if p.get("id"):
            cache = self.last_elements or {}
            els = cache.get("elements") or []
            if not els:
                return {"ok": False, "error": "还没有扫描记录：先调一次 vision_look 或 ui_tree，再用 id 操作"}
            age = time.time() - float(cache.get("at") or 0)
            el = next((e for e in els if str(e.get("id")) == str(p["id"])), None)
            rescan_info = None
            if el is None:
                return {"ok": False, "error": f"找不到元素 {p['id']}（最近一次扫描有 {len(els)} 个元素：" + ", ".join(str(e.get("id")) for e in els[:8]) + "…）"}
            if age > 5.0:
                if p.get("autoRescan") is not True:
                    return {"ok": False, "error": f"找不到元素 {p['id']}（最近一次扫描是 {age:.1f} 秒前，可能已过期，重新扫一次；或加 autoRescan:true 让它自动重扫）"}
                new_el, why = self._auto_rescan(el, cache)
                if new_el is None:
                    return {"ok": False, "error": f"{p['id']} 已过期（{age:.1f} 秒前），{why}"}
                rescan_info = {"used": True, "fromId": p["id"], "toId": new_el.get("id"), "name": new_el.get("name") or new_el.get("label") or ""}
                el = new_el
            center = el.get("center") or [0, 0]
            x, y = int(center[0]), int(center[1])
        elif p.get("x") is not None and p.get("y") is not None:
            x, y = int(p["x"]), int(p["y"])
        elif action != "key":
            return {"ok": False, "error": "要么给 id（来自最近一次扫描），要么给 x/y；action=key 可以都不给"}

        if x is not None:
            sx, sy, sw, sh = self.screen
            if not (sx <= x < sx + sw and sy <= y < sy + sh):
                return {"ok": False, "error": f"坐标 ({x},{y}) 超出屏幕范围 {list(self.screen)}"}
            try:
                fg = [r for r in list_windows() if r["foreground"]]
                if fg:
                    r = fg[0]
                    if not (r["x"] <= x <= r["x"] + r["w"] and r["y"] <= y <= r["y"] + r["h"]):
                        warning = f"目标位置不在前台窗口「{r['title'][:20]}」里，可能被遮挡（结果不可靠）"
            except Exception:
                pass

        # ---- 验证范围：动作目标附近（元素框外扩 120 / 点周围 320 / 前台窗口）----
        sx, sy, sw, sh = self.screen
        vr = None
        if el is not None and isinstance(el.get("box"), (list, tuple)) and len(el["box"]) == 4:
            bx, by, bw, bh = (int(v) for v in el["box"])
            pad = 120
            vr = (max(sx, bx - pad), max(sy, by - pad), min(sw, bw + 2 * pad), min(sh, bh + 2 * pad))
        elif x is not None:
            vr = (max(sx, x - 160), max(sy, y - 160), 320, 320)
        if vr is None:
            try:
                fg = [r for r in list_windows() if r["foreground"]]
                vr = (fg[0]["x"], fg[0]["y"], fg[0]["w"], fg[0]["h"]) if fg else self.screen
            except Exception:
                vr = self.screen

        def crop_small(img):
            if img is None:
                return None
            cx0 = max(0, vr[0] - sx)
            cy0 = max(0, vr[1] - sy)
            cx1 = min(sw, vr[0] - sx + vr[2])
            cy1 = min(sh, vr[1] - sy + vr[3])
            if cx1 <= cx0 or cy1 <= cy0:
                return None
            return small_gray(img.crop((cx0, cy0, cx1, cy1)))

        # ---- 验证准备：before 帧 + 动态背景噪声掩码 ----
        before_img = None
        before_small = None
        noise: set = set()
        before_path = after_path = None
        if verify != "none":
            try:
                before_img = grab(None)
                before_path = self.save_frame(before_img, "before")
                before_small = crop_small(before_img)
                time.sleep(0.06)
                ref = crop_small(grab(None))
                if before_small is not None and ref is not None and before_small.shape == ref.shape:
                    noise = {tuple(c["box"]) for c in block_changes(before_small, ref, vr, threshold=6.0)}
            except Exception:
                before_img = None
                before_small = None

        def measure(a, b):
            if a is None or b is None or a.shape != b.shape:
                return [], 0.0
            ch = block_changes(a, b, vr, threshold=6.0)
            ch = [c for c in ch if tuple(c["box"]) not in noise]  # 动态壁纸这类背景变化不算
            th, tw = b.shape[:2]
            total = max(1, (th // 4) * (tw // 4))
            return ch, len(ch) / float(total)

        during_img = None
        time.sleep(wait_before / 1000.0)
        try:
            u = ctypes.windll.user32
            if action == "click":
                u.SetCursorPos(x, y)
                time.sleep(0.03)
                u.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
                time.sleep(0.08)
                if verify != "none":
                    during_img = grab(None)  # 按下瞬间：按钮会显示 :active 反馈
                u.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
            elif action == "double_click":
                click_at(x, y, "left", True)
            elif action == "right_click":
                click_at(x, y, "right", False)
            elif action == "type":
                text = str(p.get("text") or "")
                if not text:
                    return {"ok": False, "error": "action=type 需要 text"}
                if x is not None:
                    click_at(x, y)
                    time.sleep(0.12)
                send_unicode(text)
            elif action == "key":
                send_keys(str(p.get("key") or ""))
        except Exception as e:
            return {"ok": False, "error": f"执行失败：{type(e).__name__}: {e}"}

        changed = False
        score = 0.0
        regions: list[list[int]] = []
        pressed_only = False
        if verify != "none" and before_small is not None:
            try:
                time.sleep(0.25)
                after_img = grab(None)
                after_path = self.save_frame(after_img, "after")
                ch_after, sc_after = measure(before_small, crop_small(after_img))
                ch_press, sc_press = measure(before_small, crop_small(during_img)) if during_img is not None else ([], 0.0)
                changed = bool(sc_after >= 0.005 or len(ch_after) >= 2 or sc_press >= 0.005 or len(ch_press) >= 2)
                pressed_only = changed and len(ch_after) < 2 and sc_after < 0.005
                if len(ch_after) >= len(ch_press):
                    regions = [c["box"] for c in ch_after[:3]]
                    score = round(sc_after, 4)
                else:
                    regions = [c["box"] for c in ch_press[:3]]
                    score = round(sc_press, 4)
            except Exception:
                log("act 验证出错：" + traceback.format_exc())

        note = ""
        if el is not None and el.get("enabled") is False:
            changed = False
            note = "元素是禁用状态（enabled=false），点了不会生效；画面那点变化是悬停/重绘"
        elif verify == "none":
            note = "已执行（verify=none，未验证变化）"
        elif changed and pressed_only:
            note = "按下时有视觉反馈，松手后恢复（点击已送达）"
        elif changed:
            note = "点到了，画面有变化"
        else:
            note = "点了没有可见变化（元素可点，但当前画面没响应）"
        return {
            "ok": True,
            "action": action,
            "element": el,
            "clicked": ({"x": x, "y": y} if x is not None else None),
            "changed": changed,
            "changeScore": score,
            "regions": regions,
            "before": before_path,
            "after": after_path,
            "note": note,
            "warning": warning,
            "rescan": rescan_info,
            "verify": verify,
            "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
        }

    # -- 动作包装：retryOnNoChange 单步重试 + batch 连招

    def _act_one(self, p: dict) -> dict:
        first = self._act_once(p)
        if p.get("retryOnNoChange") is not True or p.get("_retried") is True:
            return first
        if first.get("changed") is True or first.get("verify") == "none":
            return first
        el = first.get("element")
        if el is None:
            return first
        time.sleep(0.5)
        cache = self.last_elements or {}
        new_el, why = None, ""
        try:
            new_el, why = self._auto_rescan(el, cache)
        except Exception:
            new_el = None
        if new_el is None:
            first["retried"] = False
            first["retryNote"] = "重试前的重扫没找到同名元素：" + str(why or "")
            return first
        q = dict(p)
        q.pop("retryOnNoChange", None)
        q["_retried"] = True
        q["id"] = new_el.get("id")
        q["autoRescan"] = True
        second = self._act_once(q)
        second["retried"] = True
        second["firstAttempt"] = {
            "changed": first.get("changed"),
            "note": first.get("note"),
            "id": p.get("id"),
        }
        second["rescan"] = {
            "used": True,
            "fromId": p.get("id"),
            "toId": new_el.get("id"),
            "name": new_el.get("name") or new_el.get("label") or "",
        }
        return second

    def act(self, p: dict) -> dict:
        steps = p.get("batch")
        if not isinstance(steps, list) or not steps:
            return self._act_one(p)
        t0 = time.perf_counter()
        if len(steps) > 10:
            return {"ok": False, "error": f"batch 最多 10 步（给了 {len(steps)} 步）"}
        cont = p.get("continueOnError") is True
        gap = max(0, min(2000, int(float(p.get("stepWaitMs") or 150))))
        done = []
        failed_at = None
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                done.append({"index": i, "action": None, "ok": False, "changed": False, "error": "步骤不是对象"})
                failed_at = i
                break
            q = dict(step)
            if "verify" not in q:
                q["verify"] = "auto"
            try:
                r = self._act_one(q)
            except Exception as e:
                r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            item = {
                "index": i,
                "action": q.get("action"),
                "ok": bool(r.get("ok")),
                "changed": bool(r.get("changed")),
                "elapsedMs": r.get("elapsedMs"),
                "note": r.get("note") or r.get("error") or "",
            }
            if r.get("retried") is not None:
                item["retried"] = r.get("retried")
            if r.get("error"):
                item["error"] = r.get("error")
            done.append(item)
            step_failed = (not r.get("ok")) or (q.get("verify") != "none" and r.get("changed") is not True)
            if step_failed:
                failed_at = i
                if not cont:
                    break
            if i < len(steps) - 1:
                time.sleep(gap / 1000.0)
        return {
            "ok": failed_at is None,
            "batch": True,
            "steps": done,
            "failedAt": failed_at,
            "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
        }

    # -- 档位热切换（重建 OCR / 图标 session，不重启进程）
    def profile_set(self, p: dict) -> dict:
        global PROFILE
        prof = str((p or {}).get("profile") or "").strip().lower()
        if prof not in ("energy", "standard", "performance"):
            return {"ok": False, "error": f"不认识的档位：{prof}（energy / standard / performance）"}
        old_threads = onnx_threads()
        PROFILE = prof
        new_threads = onnx_threads()
        rebuilt = False
        if new_threads != old_threads and not getattr(self.args, "no_ocr", False):
            try:
                t0 = time.perf_counter()
                self.ocr = OcrEngine(max_wh_ratio=self.args.max_wh_ratio)
                log(f"档位切换重建 OCR：{old_threads} -> {new_threads} 线程，{round((time.perf_counter()-t0)*1000)}ms")
                rebuilt = True
            except Exception:
                log("档位切换重建 OCR 失败：" + traceback.format_exc())
        # 图标 session 的线程数也变了，清掉让它下次按新档位重建
        try:
            _ICON.update({"sess": None, "path": None, "name": None})
        except Exception:
            pass
        return {
            "ok": True,
            "profile": PROFILE,
            "threads": new_threads,
            "oldThreads": old_threads,
            "rebuilt": rebuilt,
        }

    # -- 余光（watch）转发
    def watch_start(self, p: dict) -> dict:
        return self.watcher.start(p or {})

    def watch_stop(self) -> dict:
        return self.watcher.stop()

    def watch_status(self) -> dict:
        return self.watcher.status()

    def watch_snap(self, p: dict | None = None) -> dict:
        return self.watcher.snap()

    def watch_events(self, p: dict | None = None) -> dict:
        p = p or {}
        return self.watcher.poll(int(p.get("after") or 0), int(p.get("limit") or 10))

    # -- region 解析
    def resolve(self, region) -> tuple[int, int, int, int]:
        sx, sy, sw, sh = self.screen
        if region is None or region in ("full", "screen", "all"):
            return (sx, sy, sw, sh)
        if region == "foreground":
            for r in list_windows():
                if r["foreground"]:
                    return (r["x"], r["y"], r["w"], r["h"])
            raise ValueError("当前没有前台窗口")
        if region == "smart":
            # 推荐用法：日常看当前界面用窗口（约省 60%），取不到前台窗口就回退全屏，不报错
            try:
                for r in list_windows():
                    if r["foreground"]:
                        if r["w"] >= 120 and r["h"] >= 120:
                            return (r["x"], r["y"], r["w"], r["h"])
            except Exception:
                pass
            return (sx, sy, sw, sh)
        if isinstance(region, str) and region.startswith("window:"):
            needle = region[len("window:"):].strip().lower()
            rows = list_windows()
            for r in rows:
                if needle in r["title"].lower():
                    return (r["x"], r["y"], r["w"], r["h"])
            raise ValueError(f"找不到标题包含「{needle}」的窗口。可见窗口：" + " | ".join(r["title"][:24] for r in rows[:8]))
        if isinstance(region, str):
            parts = [p for p in region.replace(" ", "").strip("[]").split(",") if p]
            if len(parts) == 4:
                x, y, w, h = (int(float(p)) for p in parts)
                return (x, y, w, h)
            raise ValueError(f"region 写法不对：{region}（可用 full / foreground / window:关键词 / [x,y,w,h]）")
        if isinstance(region, (list, tuple)) and len(region) == 4:
            return tuple(int(float(v)) for v in region)  # type: ignore[return-value]
        if isinstance(region, dict) and all(k in region for k in ("x", "y", "w", "h")):
            return (int(region["x"]), int(region["y"]), int(region["w"]), int(region["h"]))
        raise ValueError(f"region 无法识别：{region!r}")

    def save_frame(self, img: Image.Image, tag: str = "") -> str:
        ts = time.strftime("%Y%m%d-%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        name = f"{ts}{('_' + tag) if tag else ''}.jpg"
        path = os.path.join(self.frames_dir, name)
        img.save(path, "JPEG", quality=72)
        return path.replace("\\", "/")

    # -- 图标层：优先 OmniParser ONNX，模型缺失/报错才退 OpenCV（不做并集，避免把准的招牌弄脏）
    def icons_for(self, img, raw, engine: str = "auto", limit: int = 40):
        boxes = [e["box"] for e in raw]
        path = icon_model_path()
        if engine in ("auto", "model") and os.path.exists(path):
            try:
                t0 = time.perf_counter()
                dets = detect_icons_model(img, path, limit=limit)
                return dets, {"iconEngine": "omniparser-onnx", "iconsMs": round((time.perf_counter() - t0) * 1000, 1), "icons": len(dets)}
            except Exception:
                if engine == "model":
                    raise
                log("图标模型失败，退回 OpenCV：" + traceback.format_exc())
        t0 = time.perf_counter()
        dets = detect_icons(img, boxes, limit=min(limit, 30))
        return dets, {"iconEngine": "opencv-fallback", "iconsMs": round((time.perf_counter() - t0) * 1000, 1), "icons": len(dets)}

    # -- Set-of-Mark：另存一张带 id 标注的截图（原帧不动，OCR 早已完成）
    def mark_frame(self, frame_path: str, elements: list[dict], offset: tuple[int, int] = (0, 0)) -> str | None:
        try:
            img = Image.open(frame_path).convert("RGB")
            draw = ImageDraw.Draw(img)
            ox, oy = offset
            try:
                from PIL import ImageFont

                font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 14)
            except Exception:
                font = ImageFont.load_default()
            for e in elements:
                x, y, w, h = e["box"]
                # 元素是屏幕绝对坐标，截图是区域裁切：先减掉区域原点
                x, y = x - ox, y - oy
                draw.rectangle([x, y, x + w, y + h], outline=(255, 0, 0), width=2)
                label = str(e.get("id") or "")
                if not label:
                    continue
                tb = draw.textbbox((0, 0), label, font=font)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                bx, by = x, max(0, y - th - 6)
                draw.rectangle([bx, by, bx + tw + 6, by + th + 6], fill=(255, 255, 255))
                draw.text((bx + 3, by + 2), label, fill=(0, 0, 0), font=font)
            marked = os.path.splitext(frame_path)[0] + "-marked.jpg"
            img.save(marked, "JPEG", quality=72)
            return marked.replace("\\", "/")
        except Exception:
            log("mark_frame 失败：" + traceback.format_exc())
            return None

    # -- 主入口
    def look(self, p: dict) -> dict:
        t0 = time.perf_counter()
        self.calls += 1
        region = self.resolve(p.get("region"))
        use_ocr = bool(p.get("ocr", True)) and self.ocr is not None
        use_icons = bool(p.get("icons", False))
        save_frame = bool(p.get("saveFrame", True))
        max_n = int(p.get("maxElements", 60))
        min_score = float(p.get("minScore", 0.5))
        hint = p.get("sceneHint") or None

        tg = time.perf_counter()
        img = grab(region)
        grab_ms = round((time.perf_counter() - tg) * 1000, 1)
        small = small_gray(img)
        h = dhash(small)
        genre = (region, img.size, use_ocr, use_icons)

        changes = block_changes(self.prev_small if self.prev_region == region else None, small, region)
        self.prev_small, self.prev_region = small, region

        hit = self.cache.find(h, genre)
        if hit:
            payload = hit["payload"]
            els = to_elements(payload["raw"], region, max_n, min_score)
            scene = payload.get("scene") or detect_scene(els, hint, (self.screen[2], self.screen[3]), tuple(region) == tuple(self.screen))
            out = {
                "ok": True,
                "scene": scene,
                "elements": els,
                "changes": changes[:12],
                "frame": payload.get("frame") or (self.save_frame(img) if save_frame else ""),
                "cached": True,
                "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                "region": list(region),
                "screen": [self.screen[2], self.screen[3]],
                "hash": h,
                "stats": {"grabMs": grab_ms, "cacheAgeMs": round((time.time() - hit["at"]) * 1000, 0), "rawCount": payload.get("rawCount")},
            }
            self._cache_elements("look", els, str(p.get("region") or "full"))
            if p.get("markIds") and out.get("frame"):
                out["markedFrame"] = self.mark_frame(out["frame"], els, (region[0], region[1]))
            self.last_look = out
            return out

        raw: list[dict] = []
        timings: dict = {}
        if use_ocr:
            raw, timings = self.ocr.run(img)
        if use_icons:
            icons, ic_meta = self.icons_for(img, raw, str(p.get("iconEngine") or "auto"))
            timings.update(ic_meta)
            raw, bm = blend_icons(raw, icons)
            timings.update(bm)
        els = to_elements(raw, region, max_n, min_score)
        scene = detect_scene(els, hint, (self.screen[2], self.screen[3]), tuple(region) == tuple(self.screen))
        frame = self.save_frame(img) if save_frame else ""
        payload = {"raw": raw, "scene": scene, "frame": frame, "rawCount": len(raw)}
        self.cache.put(h, genre, payload)
        out = {
            "ok": True,
            "scene": scene,
            "elements": els,
            "changes": changes[:12],
            "frame": frame,
            "cached": False,
            "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
            "region": list(region),
            "screen": [self.screen[2], self.screen[3]],
            "hash": h,
            "stats": {"grabMs": grab_ms, **timings, "rawCount": len(raw)},
        }
        self._cache_elements("look", els, str(p.get("region") or "full"))
        if p.get("markIds") and out.get("frame"):
            out["markedFrame"] = self.mark_frame(out["frame"], els, (region[0], region[1]))
        self.last_look = out
        return out

    # -- 用已有截图「看」：不截屏，直接 OCR 这张图（复核历史帧 / 无屏幕权限时用）
    def look_frame(self, path: str, max_n: int, min_score: float, use_ocr: bool, use_icons: bool) -> dict:
        if not os.path.exists(path):
            raise ValueError(f"截图不存在：{path}")
        self.calls += 1
        img = Image.open(path)
        raw, timings = (self.ocr.run(img) if (use_ocr and self.ocr is not None) else ([], {}))
        if use_icons:
            icons, ic_meta = self.icons_for(img, raw, str(p.get("iconEngine") or "auto"))
            timings.update(ic_meta)
            raw, bm = blend_icons(raw, icons)
            timings.update(bm)
        region = (0, 0, img.size[0], img.size[1])
        els = to_elements(raw, region, max_n, min_score)
        return {
            "ok": True,
            "scene": detect_scene(els, None, (img.size[0], img.size[1]), True),
            "elements": els,
            "changes": [],
            "frame": path.replace("\\", "/"),
            "cached": False,
            "elapsedMs": 0,
            "region": list(region),
            "screen": [img.size[0], img.size[1]],
            "hash": dhash(small_gray(img)),
            "stats": {"rawCount": len(raw), **timings, "fromFrame": True},
        }

    # -- UIA：结构层（纯本机、零模型），无树时回退 OCR
    def _uia_targets(self, region_spec: str) -> list[int]:
        rs = (region_spec or "foreground").strip()
        if rs in ("foreground", "fg", "前台"):
            hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            return [hwnd] if hwnd else []
        if rs.startswith("window:"):
            needle = rs[7:].strip().lower()
            rows = list_windows()
            hit = [r for r in rows if needle and needle in r["title"].lower()]
            if not hit:
                return []
            hit.sort(key=lambda r: -(r["w"] * r["h"]))
            return [int(hit[0]["hwnd"])]
        if rs in ("full", "screen", "all"):
            rows = list_windows()
            return [int(r["hwnd"]) for r in rows if r["title"]][:10]
        return []

    def _uia_fallback(self, p: dict, base: dict, reason: str, t0: float) -> dict:
        """无树/失败：回退 OCR。不报错、不假装有树。"""
        try:
            look = self.look({"region": str(p.get("region") or "foreground"), "saveFrame": True})
            els = [{**e, "source": "ocr"} for e in (look.get("elements") or [])]
            base.update(
                {
                    "source": "ocr",
                    "elements": els,
                    "frame": look.get("frame"),
                    "truncated": True,
                    "fallback": reason,
                    "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                }
            )
        except Exception as e:
            base.update(
                {
                    "source": "ocr",
                    "elements": [],
                    "fallback": reason,
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
                    "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                }
            )
        self.last_understand = base
        return base

    def ui_tree(self, p: dict) -> dict:
        t0 = time.perf_counter()
        region_spec = str(p.get("region") or "foreground")
        depth = max(1, min(20, int(p.get("depth") or 12)))
        filt = str(p.get("filter") or "interactive").strip().lower()
        if filt not in ("interactive", "all"):
            filt = "interactive"
        budget = max(50.0, min(3000.0, float(p.get("budgetMs") or 300)))
        with_ocr = bool(p.get("withOcr", False))

        ok, ver = _uia_available()
        hwnds = self._uia_targets(region_spec) if ok else []
        win_info = {}
        if hwnds:
            hwnd = hwnds[0]
            pid = _win_pid(hwnd)
            win_info = {"hwnd": hwnd, "title": _win_title(hwnd), "pid": pid, "app": _proc_name(pid)}

        base = {
            "ok": True,
            "source": "uia",
            "window": win_info,
            "depth": depth,
            "filter": filt,
            "budgetMs": budget,
            "uiaVersion": ver,
        }

        if not ok:
            return self._uia_fallback(p, base, f"uiautomation 不可用：{ver}", t0)
        if not hwnds:
            return self._uia_fallback(p, base, f"找不到窗口：{region_spec}", t0)

        scan, err = _uia_scan_threaded(hwnds, depth, filt, budget)
        if scan is None:
            return self._uia_fallback(p, base, f"UIA 读取失败：{err}", t0)

        # Chromium 的无障碍树比页面渲染慢半拍：刚打开的页面第一次可能只有外壳。
        # 浏览器/Electron 窗口「内容区为空」就等 200ms 重试一次（只一次，不无限等）。
        app = str(win_info.get("app") or "").lower()
        browserish = any(k in app for k in ("msedge", "chrome", "electron", "firefox"))
        retried = False
        if browserish:
            cov0 = scan.get("coverage") or {}
            if cov0.get("contentRoots", 0) == 0 or scan.get("contentElements", 0) == 0:
                time.sleep(0.2)
                scan2, _err2 = _uia_scan_threaded(hwnds, depth, filt, min(budget, 260.0))
                if scan2 is not None:
                    cov2 = scan2.get("coverage") or {}
                    if cov2.get("contentRoots", 0) > 0 and scan2.get("contentElements", 0) > 0:
                        scan = scan2
                        retried = True
        base["retried"] = retried

        els = scan["elements"]
        if filt == "interactive" and len(els) == 0:
            return self._uia_fallback(p, base, "UIA 树里没有可交互控件（无树应用）", t0)

        out = []
        for i, e in enumerate(els, 1):
            box = e["box"]
            out.append(
                {
                    "id": f"u{i}",
                    "name": e["name"],
                    "role": e["role"],
                    "box": box,
                    "center": [box[0] + box[2] // 2, box[1] + box[3] // 2],
                    "enabled": e["enabled"],
                    "selected": e["selected"],  # null = UIA 不知道（上层可去问视觉/云端）
                    "focus": e["focus"],
                    "source": "uia",
                }
            )
        where = scan.get("truncatedWhere")
        cov = scan.get("coverage") or {}
        parts = []
        if retried:
            parts.append("首次读取内容区为空（无障碍树还没生成），已等 200ms 重试")
        if cov.get("contentAbandoned"):
            parts.append("内容区扫了 60 个节点仍无可交互控件，已跳过（预算留给外壳）")
        if scan["truncated"]:
            if where == "content":
                parts.append("截断在内容区内部（内容太多）；可以把 budgetMs 提到 400+")
            elif where == "shell":
                if cov.get("contentRoots") and not cov.get("contentAbandoned"):
                    parts.append("截断在外壳层（标签页/工具栏/菜单）；内容区已扫完")
                elif cov.get("contentAbandoned"):
                    parts.append("截断在外壳层；内容区已跳过")
                else:
                    parts.append("截断在外壳层，且没识别到内容区（可能不是浏览器窗口）")
            else:
                parts.append("超预算截断")
        note = "；".join(parts)

        self._cache_elements("uia", out, region_spec)

        want_relations = p.get("relations", True) is not False
        rels, rel_meta = compute_relations(out, scan.get("texts") or []) if want_relations else ([], {"skipped": "disabled", "labelForUnresolved": []})
        base["actions"] = compute_actions(out)
        base["relations"] = rels
        base["relationsSkipped"] = rel_meta["skipped"]
        base["labelForUnresolved"] = rel_meta["labelForUnresolved"]

        base.update(
            {
                "elements": out,
                "truncated": scan["truncated"],
                "truncatedWhere": where,
                "coverage": cov,
                "note": note,
                "stats": {
                    "nodes": scan["nodes"],
                    "ms": scan["ms"],
                    "droppedNoRect": scan["droppedNoRect"],
                    "contentElements": scan.get("contentElements", 0),
                },
                "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
            }
        )

        if with_ocr:
            try:
                look = self.look({"region": region_spec, "saveFrame": True})
                added = []
                for e in look.get("elements") or []:
                    bx, by, bw, bh = e["box"]
                    inside = False
                    for u in out:
                        ux, uy, uw, uh = u["box"]
                        ix = max(0, min(bx + bw, ux + uw) - max(bx, ux))
                        iy = max(0, min(by + bh, uy + uh) - max(by, uy))
                        if bw * bh > 0 and (ix * iy) / float(bw * bh) >= 0.6:
                            inside = True
                            break
                    if not inside:
                        added.append({**e, "source": "ocr"})
                base["elements"] = out + added
                base["stats"]["ocrAdded"] = len(added)
                base["frame"] = look.get("frame")
                base["elapsedMs"] = round((time.perf_counter() - t0) * 1000, 1)
            except Exception as e:
                base["stats"]["ocrError"] = f"{type(e).__name__}: {str(e)[:200]}"

        self.last_understand = base
        return base

    # -- 问答模式：只有 cloud 能答（local 3B 看缩略图不可靠，off 不联网），拒绝要说清楚
    def _ask_refuse(self, backend: str, reason: str, t0: float, question: str = "") -> dict:
        out = {
            "ok": True,
            "mode": "ask",
            "backend": backend,
            "question": question,
            "refused": True,
            "answer": "",
            "reason": reason,
            "elements": [],
            "cached": False,
            "degraded": False,
            "usage": None,
            "latencyMs": None,
            "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
        }
        self.last_understand = out
        return out

    def ask(self, p: dict, question: str) -> dict:
        t0 = time.perf_counter()
        max_n = int(p.get("maxElements", 40))
        min_score = float(p.get("minScore", 0.5))
        use_ocr = bool(p.get("ocr", True))
        use_icons = bool(p.get("icons", False))
        thumb_w = max(480, min(2560, int(p.get("thumbWidth", 1024) or 1024)))
        timeout = float(p.get("timeoutS", 90) or 90)
        model = (p.get("model") or self.model or LLM_DEFAULT_MODEL).strip()

        explicit = str(p.get("backend") or "").strip().lower()
        key, key_src = load_dashscope_key()
        if explicit in ("local", "off"):
            return self._ask_refuse(explicit, "问答只支持 cloud：本地 3B 看缩略图不可靠，off 模式不联网。请显式传 backend:\"cloud\"。", t0, question)
        if not key:
            return self._ask_refuse("cloud", f"问答需要云端模型，但没找到 DASHSCOPE_API_KEY（{key_src}）。", t0, question)

        frame_path = p.get("frame")
        if frame_path:
            look = self.look_frame(str(frame_path), max_n, min_score, use_ocr, use_icons)
            region = tuple(look["region"])
            region_key = ("frame", os.path.abspath(str(frame_path)))
        else:
            look = self.look({**p, "saveFrame": True})
            region = tuple(look.get("region") or (0, 0, 0, 0))
            region_key = region
        els = look.get("elements") or []
        h = int(look.get("hash") or 0)
        genre = (region_key, tuple(self.screen), use_ocr, use_icons, max_n, min_score, "ask", thumb_w, question)

        base = {
            "ok": True,
            "mode": "ask",
            "backend": "cloud",
            "question": question,
            "answer": "",
            "elements": els,
            "frame": look.get("frame"),
            "region": list(region),
            "screen": [self.screen[2], self.screen[3]],
            "refused": False,
            "degraded": False,
        }

        hit = self.sem_cache.find(h, genre)
        if hit:
            base.update(
                {
                    "answer": hit["payload"].get("answer", ""),
                    "cached": True,
                    "usage": None,
                    "latencyMs": 0,
                    "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                    "stats": {"cacheAgeMs": round((time.time() - hit["at"]) * 1000)},
                }
            )
            self.last_understand = base
            return base

        try:
            prompt = build_ask_prompt(question, els)
            b64, tw, th = make_thumbnail(look.get("frame") or "", max_w=thumb_w)
            self.llm_calls += 1
            _raw, resp = call_llm(prompt, b64, model, timeout=timeout)
            answer = (resp["content"] or "").strip()
            if not answer:
                raise RuntimeError("模型返回空回答")
            self.sem_cache.put(h, genre, {"answer": answer, "scene": look.get("scene"), "summary": ""})
            base.update(
                {
                    "answer": answer[:800],
                    "model": resp.get("model") or model,
                    "cached": False,
                    "usage": resp.get("usage") or {},
                    "latencyMs": resp.get("latencyMs"),
                    "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                    "thumb": [tw, th],
                    "stats": {"promptChars": len(prompt)},
                }
            )
            self.last_understand = base
            return base
        except Exception as e:
            self.llm_errors += 1
            log("ask 失败：" + traceback.format_exc())
            base.update(
                {
                    "cached": False,
                    "degraded": True,
                    "error": f"{type(e).__name__}: {str(e)[:300]}",
                    "usage": None,
                    "latencyMs": None,
                    "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                }
            )
            self.last_understand = base
            return base

    # -- 语义层：把元素清单交给多模态模型清洗 + 判场景（显式调用才走这里）
    def understand(self, p: dict) -> dict:
        question = str(p.get("question") or "").strip()
        if question:
            return self.ask(p, question)
        t0 = time.perf_counter()
        max_n = int(p.get("maxElements", 60))
        min_score = float(p.get("minScore", 0.5))
        use_ocr = bool(p.get("ocr", True))
        use_icons = bool(p.get("icons", False))
        model = (p.get("model") or self.model or LLM_DEFAULT_MODEL).strip()
        backend_pref = backend_choice(p)
        timeout = float(p.get("timeoutS", 90) or 90)
        thumb_w = max(480, min(2560, int(p.get("thumbWidth", 1024) or 1024)))

        frame_path = p.get("frame")
        if frame_path:
            look = self.look_frame(str(frame_path), max_n, min_score, use_ocr, use_icons)
            region = tuple(look["region"])
            region_key = ("frame", os.path.abspath(str(frame_path)))
        else:
            look = self.look({**p, "saveFrame": True})
            region = tuple(look.get("region") or (0, 0, 0, 0))
            region_key = region
        els = look.get("elements") or []
        genre = (region_key, tuple(self.screen), use_ocr, use_icons, max_n, min_score, model, thumb_w, backend_pref)
        h = int(look.get("hash") or 0)

        base = {
            "ok": True,
            "backend": backend_pref,
            "scene": look.get("scene"),
            "summary": "",
            "elements": els,
            "frame": look.get("frame"),
            "region": list(region),
            "screen": [self.screen[2], self.screen[3]],
            "model": model,
            "degraded": False,
        }

        hit = self.sem_cache.find(h, genre)
        if hit:
            sem = hit["payload"]
            apply_semantic(els, sem)
            base.update(
                {
                    "scene": sem.get("scene") or base["scene"],
                    "summary": sem.get("summary") or "",
                    "model": local_model() if backend_pref == "local" else model,
                    "cached": True,
                    "usage": None,
                    "latencyMs": 0,
                    "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                    "stats": {"cacheAgeMs": round((time.time() - hit["at"]) * 1000)},
                }
            )
            self.last_understand = base
            return base

        try:
            prompt = build_understand_prompt(els, (self.screen[2], self.screen[3]))
            b64, tw, th = make_thumbnail(look.get("frame") or "", max_w=thumb_w)

            backend_pref = backend_choice(p)
            chain = {
                "local": ["local", "cloud", "off"],
                "cloud": ["cloud", "local", "off"],
                "off": ["off"],
            }[backend_pref]
            attempts: list[dict] = []
            used = None
            resp = None
            sem = None
            for b in chain:
                if b == "off":
                    used = "off"
                    break
                try:
                    self.llm_calls += 1
                    if b == "local":
                        ensure_llama()
                        crop_bytes, _ids = build_crop_grid(
                            look.get("frame") or "", els, (region[0], region[1]), max_n=LOCAL_CROP_MAX
                        )
                        crop_b64 = base64.b64encode(crop_bytes).decode("ascii")
                        _raw, resp = call_local_llm(read_local_prompt(), crop_b64, local_model(), timeout=max(timeout, 60.0))
                        arr = extract_json_array(resp["content"])
                        sent = set(_ids)
                        fixes = []
                        for x in arr:
                            eid = str(x.get("id") or "").strip()
                            txt = x.get("text")
                            if not eid or eid not in sent or not isinstance(txt, str) or not txt.strip():
                                continue  # 只认我们真的裁过、发过去的 id
                            txt = txt.strip()[:200]
                            # 模型偶尔把左边的编号/示例文字当成正文读回来，这种要丢掉
                            if re.fullmatch(r"e\d+", txt.strip(), flags=re.IGNORECASE):
                                continue
                            if txt.strip() in ("读到的文字", "文字", "label", "text"):
                                continue
                            fixes.append({"id": eid, "label": txt, "confident": True})
                        parsed = {"scene": "", "summary": "", "elements": fixes}
                    else:
                        _raw, resp = call_llm(prompt, b64, model, timeout=timeout)
                        parsed = extract_json(resp["content"])
                    sem = {
                        "scene": str(parsed.get("scene") or "").strip().lower(),
                        "summary": str(parsed.get("summary") or "").strip()[:300],
                        "elements": [x for x in (parsed.get("elements") or []) if isinstance(x, dict)],
                    }
                    used = b
                    break
                except Exception as e:
                    attempts.append({"backend": b, "error": f"{type(e).__name__}: {str(e)[:240]}"})
                    log(f"语义后端 {b} 失败，降级下一个：{e}")

            if used == "off":
                if attempts:
                    self.llm_errors += 1
                base.update(
                    {
                        "backend": "off",
                        "cached": False,
                        "degraded": bool(attempts),
                        "attempts": attempts,
                        "error": attempts[-1]["error"] if attempts else None,
                        "usage": None,
                        "latencyMs": None,
                        "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                    }
                )
                self.last_understand = base
                return base

            if sem["scene"] not in SCENES:
                sem["scene"] = base["scene"]
            apply_semantic(els, sem)
            self.sem_cache.put(h, genre, sem)
            base.update(
                {
                    "backend": used,
                    "model": resp.get("model") or (local_model() if used == "local" else model),
                    "scene": sem["scene"],
                    "summary": sem["summary"],
                    "cached": False,
                    "usage": resp.get("usage") or {},
                    "latencyMs": resp.get("latencyMs"),
                    "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                    "thumb": [tw, th],
                    "attempts": attempts,
                    "stats": {"promptChars": len(prompt)},
                }
            )
            self.last_understand = base
            return base
        except Exception as e:
            self.llm_errors += 1
            log("understand 降级：" + traceback.format_exc())
            base.update(
                {
                    "backend": "off",
                    "cached": False,
                    "degraded": True,
                    "error": f"{type(e).__name__}: {str(e)[:300]}",
                    "usage": None,
                    "latencyMs": None,
                    "elapsedMs": round((time.perf_counter() - t0) * 1000, 1),
                }
            )
            self.last_understand = base
            return base

    def health(self) -> dict:
        key, key_src = load_dashscope_key()
        return {
            "ok": True,
            "screen": [self.screen[2], self.screen[3]],
            "origin": [self.screen[0], self.screen[1]],
            "python": sys.version.split()[0],
            "ocr": {"ready": self.ocr is not None, "initMs": self.ocr.init_ms if self.ocr else None, "maxWhRatio": self.args.max_wh_ratio, **({"cache": self.ocr.cache_info()} if self.ocr is not None else {})},
            "icons": {"modelPath": icon_model_path(), "modelPresent": os.path.exists(icon_model_path()), "loaded": _ICON["sess"] is not None},
            "uia": {"available": _uia_available()[0], "version": _uia_available()[1], "pin": UIA_PIN},
            "profile": PROFILE,
            "usage": usage_budget(),
            "onnx": {"threads": onnx_threads(), "source": onnx_thread_source(), "cpuCount": os.cpu_count()},
            "cache": {"size": len(self.cache.items), "cap": self.cache.cap, "hits": self.cache.hits, "misses": self.cache.misses},
            "semantic": {
                "backend": backend_choice({}),
                "model": self.model,
                "keySource": key_src,
                "keyPresent": bool(key),
                "calls": self.llm_calls,
                "errors": self.llm_errors,
                "local": {
                    "endpoint": local_endpoint(),
                    "healthy": llama_healthy(0.8),
                    "model": local_model(),
                    "bootMs": _LLAMA.get("bootMs"),
                    "paths": llama_paths(),
                },
                "cache": {
                    "size": len(self.sem_cache.items),
                    "cap": self.sem_cache.cap,
                    "hits": self.sem_cache.hits,
                    "misses": self.sem_cache.misses,
                },
                "promptFile": PROMPT_PATH,
            },
            "calls": self.calls,
            "uptimeS": round(time.time() - self.started, 1),
            "companion": self.companion.status(),
            "framesDir": self.frames_dir.replace("\\", "/"),
        }


# ------------------------------------------------------------------ HTTP

SERVICE: Service | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音，避免刷爆 stderr
        return

    def _send(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if self.path.startswith("/health"):
                return self._send(SERVICE.health())
            if parsed.path == "/watch/status":
                return self._send(SERVICE.watch_status())
            if parsed.path == "/latest":
                return self._send(SERVICE.latest())
            if parsed.path == "/watch/events":
                return self._send(
                    SERVICE.watch_events(
                        {
                            "after": (qs.get("after") or ["0"])[0],
                            "limit": (qs.get("limit") or ["10"])[0],
                        }
                    )
                )
            if self.path.startswith("/windows"):
                return self._send({"ok": True, "windows": list_windows()})
            if self.path.startswith("/stats"):
                return self._send(SERVICE.health())
            if parsed.path == "/companion/status":
                return self._send({"ok": True, **SERVICE.companion.status()})
            if parsed.path == "/companion/events":
                return self._send(
                    SERVICE.companion.poll_events(
                        after=int((qs.get("after") or ["0"])[0]),
                        limit=int((qs.get("limit") or ["5"])[0]),
                    )
                )
            if parsed.path == "/companion/reflex/presets":
                return self._send(SERVICE.companion.reflex_command("list"))
            return self._send({"ok": False, "error": f"no route {self.path}"}, 404)
        except Exception as e:
            return self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        try:
            body = self._body()
            if self.path.startswith("/look"):
                return self._send(SERVICE.look(body))
            if self.path.startswith("/understand"):
                return self._send(SERVICE.understand(body))
            if self.path.startswith("/ui_tree"):
                return self._send(SERVICE.ui_tree(body))
            if self.path.startswith("/profile"):
                return self._send(SERVICE.profile_set(body))
            if self.path.startswith("/watch/start"):
                return self._send(SERVICE.watch_start(body))
            if self.path.startswith("/watch/stop"):
                return self._send(SERVICE.watch_stop())
            if self.path.startswith("/watch/status"):
                return self._send(SERVICE.watch_status())
            if self.path.startswith("/watch/events"):
                return self._send(SERVICE.watch_events(body))
            if self.path.startswith("/watch/snap"):
                return self._send(SERVICE.watch_snap(body))
            if self.path.startswith("/latest"):
                return self._send(SERVICE.latest())
            if self.path.startswith("/act"):
                return self._send(SERVICE.act(body))
            if self.path.startswith("/cache/clear"):
                return self._send({"ok": True, "cleared": SERVICE.cache.clear()})
            if self.path.startswith("/companion/mode"):
                return self._send(SERVICE.companion.set_mode(body))
            if self.path.startswith("/companion/status"):
                return self._send({"ok": True, **SERVICE.companion.status()})
            if self.path.startswith("/companion/events"):
                return self._send(
                    SERVICE.companion.poll_events(
                        after=int(body.get("after") or 0),
                        limit=int(body.get("limit") or 5),
                    )
                )
            if self.path.startswith("/companion/contact_sheet"):
                return self._send(SERVICE.companion.capture_contact_sheet_now(body))
            if self.path.startswith("/companion/audio/push"):
                return self._send(
                    SERVICE.companion.audio.push_manual_line(
                        text=str(body.get("text") or ""),
                        speaker=str(body.get("speaker") or "system"),
                    )
                )
            if self.path.startswith("/companion/barge_in"):
                return self._send(
                    SERVICE.companion.barge_in(
                        action=str(body.get("action") or "pause"),
                        duration_s=float(body.get("durationS") or 20.0),
                    )
                )
            if self.path.startswith("/companion/game/act"):
                return self._send(SERVICE.companion.game_act(body))
            if self.path.startswith("/companion/reflex/"):
                op = self.path.split("/companion/reflex/", 1)[1].split("?", 1)[0].strip("/")
                return self._send(SERVICE.companion.reflex_command(op, body))
            if self.path.startswith("/ocr_crop"):
                return self._send(SERVICE.companion.ocr_crop(body))
            if self.path.startswith("/frame"):
                region = SERVICE.resolve(body.get("region"))
                img = grab(region)
                path = SERVICE.save_frame(img, "frame")
                return self._send({"ok": True, "frame": path, "region": list(region)})
            return self._send({"ok": False, "error": f"no route {self.path}"}, 404)
        except Exception as e:
            log("POST 失败：" + traceback.format_exc())
            return self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="0=随机端口（推荐），宿主从 stdout 第一行读端口")
    ap.add_argument("--data-dir", default="", help="数据根目录（默认 ~/.dsh/omnivision）")
    ap.add_argument("--frames-dir", default="", help="截图目录（默认 <data-dir>/frames）")
    ap.add_argument("--max-wh-ratio", type=float, default=MAX_WH_RATIO)
    ap.add_argument("--cache-size", type=int, default=48)
    ap.add_argument("--no-ocr", action="store_true", help="只做差分/指纹，不加载 OCR（排查用）")
    ap.add_argument("--profile", default="", help="性能档位：energy / standard / performance")
    args = ap.parse_args()

    global DATA_ROOT, PROFILE
    if args.profile:
        PROF = args.profile.strip().lower()
        if PROF in ("energy", "standard", "performance"):
            PROFILE = PROF
    if args.data_dir:
        DATA_ROOT = os.path.abspath(args.data_dir)
    if not args.frames_dir:
        args.frames_dir = frames_dir_default()
    os.makedirs(DATA_ROOT, exist_ok=True)

    dpi_aware()
    global SERVICE
    t0 = time.perf_counter()
    SERVICE = Service(args)
    boot_ms = round((time.perf_counter() - t0) * 1000, 1)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = srv.server_address[1]
    sys.stdout.write(
        json.dumps(
            {
                "port": port,
                "screen": [SERVICE.screen[2], SERVICE.screen[3]],
                "bootMs": boot_ms,
                "codeMtime": round(os.path.getmtime(__file__), 1),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    sys.stdout.flush()
    log(f"ready on 127.0.0.1:{port} screen={SERVICE.screen} boot={boot_ms}ms")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
