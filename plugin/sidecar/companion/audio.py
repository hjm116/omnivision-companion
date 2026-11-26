# -*- coding: utf-8 -*-
"""Zero-virtual-cable Windows WASAPI Loopback capture + local ASR transcript ring buffer.
Prefers sherpa-onnx SenseVoiceSmall (int8, fast & accurate Chinese/English/Japanese/Cantonese),
with configurable faster-whisper fallback and graceful degradation when optional deps are absent."""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

import numpy as np

try:
    import pyaudiowpatch as pyaudio  # type: ignore
except Exception:
    pyaudio = None

try:
    import sherpa_onnx  # type: ignore
except Exception:
    sherpa_onnx = None

try:
    from faster_whisper import WhisperModel  # type: ignore
except Exception:
    WhisperModel = None

DEFAULT_WHISPER_PROMPT = (
    "以下是普通话或中英混合的日常对话、视频解说和游戏语音，常见词包括："
    "token、API、DeepSeek、Claude、Bug、Buff、Boss、双十一、弹幕、显卡、帧率。"
)


class SystemAudioListener:
    """Captures default Windows speaker loopback audio and transcribes speech locally."""

    def __init__(self, data_dir: str, max_lines: int = 80):
        self.data_dir = data_dir
        self.max_lines = max_lines
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        self.last_error = ""
        self.device_name = ""
        self.asr_engine = "none"
        self.asr_model_path = ""
        self.transcripts: deque[dict[str, Any]] = deque(maxlen=max_lines)
        self._seq = 0
        self._last_speech_at = 0.0
        self._rms_threshold = 0.012
        self._recognizer = None
        self._whisper = None

    def capabilities(self) -> dict:
        sv_model, sv_tokens = self._find_sensevoice_files()
        return {
            "windows": os.name == "nt",
            "wasapiLoopback": pyaudio is not None,
            "sherpaOnnx": sherpa_onnx is not None,
            "senseVoiceModelReady": bool(sv_model and sv_tokens),
            "fasterWhisper": WhisperModel is not None,
        }

    def _find_sensevoice_files(self, model_dir: str = "") -> tuple[str, str]:
        candidates = []
        if model_dir:
            candidates.append(model_dir)
        env_dir = os.environ.get("OMNIVISION_SENSEVOICE_DIR", "").strip()
        if env_dir:
            candidates.append(env_dir)
        models_root = os.path.join(self.data_dir, "models")
        candidates.extend([
            os.path.join(models_root, "sense-voice"),
            os.path.join(models_root, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"),
            os.path.join(models_root, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"),
        ])
        for cand in candidates:
            if not cand or not os.path.isdir(cand):
                continue
            tokens_txt = os.path.join(cand, "tokens.txt")
            for fn in ("model.int8.onnx", "model.onnx"):
                model_onnx = os.path.join(cand, fn)
                if os.path.isfile(model_onnx) and os.path.isfile(tokens_txt):
                    return model_onnx, tokens_txt
        return "", ""

    def push_manual_line(self, text: str, speaker: str = "system", ts: float | None = None) -> dict:
        """Allow testing or external subtitle injection (e.g., player SRT/Danmaku bridge)."""
        clean = str(text or "").strip()
        if not clean:
            return {"ok": False, "error": "empty text"}
        now = float(ts) if ts is not None else time.time()
        with self._lock:
            self._seq += 1
            item = {
                "seq": self._seq,
                "ts": round(now, 3),
                "timeStr": time.strftime("%H:%M:%S", time.localtime(now)),
                "speaker": speaker,
                "text": clean,
            }
            self.transcripts.append(item)
            self._last_speech_at = now
        return {"ok": True, "item": item}

    def recent_lines(self, since_ts: float = 0.0, limit: int = 12) -> list[dict]:
        with self._lock:
            items = [x for x in self.transcripts if x["ts"] > since_ts]
        return items[-max(1, int(limit)):]

    def format_recent(self, since_ts: float = 0.0, limit: int = 12) -> str:
        lines = self.recent_lines(since_ts=since_ts, limit=limit)
        if not lines:
            return ""
        return "\n".join(f"[{x['timeStr']}] {x['text']}" for x in lines)

    def _init_asr(self, model_dir: str = "") -> str:
        # 1) Prefer sherpa-onnx SenseVoiceSmall (fast on CPU, strong Chinese/English mix)
        if sherpa_onnx is not None:
            model_onnx, tokens_txt = self._find_sensevoice_files(model_dir=model_dir)
            if model_onnx and tokens_txt:
                try:
                    self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                        model=model_onnx,
                        tokens=tokens_txt,
                        num_threads=2,
                        use_itn=True,
                        language="auto",
                    )
                    self.asr_engine = "sherpa-sensevoice"
                    self.asr_model_path = model_onnx
                    return self.asr_engine
                except Exception as e:
                    self.last_error = f"sherpa init failed: {e}"
        # 2) Fallback to faster-whisper (default small -> base -> tiny)
        if WhisperModel is not None:
            req_model = os.environ.get("OMNIVISION_WHISPER_MODEL", "").strip()
            device = os.environ.get("OMNIVISION_WHISPER_DEVICE", "cpu").strip() or "cpu"
            compute = os.environ.get("OMNIVISION_WHISPER_COMPUTE", "int8").strip() or "int8"
            order = [req_model] if req_model else ["small", "base", "tiny"]
            for name in order:
                try:
                    self._whisper = WhisperModel(name, device=device, compute_type=compute)
                    self.asr_engine = f"faster-whisper-{name}"
                    self.asr_model_path = name
                    return self.asr_engine
                except Exception as e:
                    self.last_error = f"whisper({name}) init failed: {e}"
        self.asr_engine = "vad-only"
        self.asr_model_path = ""
        return self.asr_engine

    def _transcribe_16k(self, pcm_f32: np.ndarray) -> str:
        if pcm_f32.size < 3200:
            return ""
        if self._recognizer is not None:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(16000, pcm_f32.tolist())
            self._recognizer.decode_stream(stream)
            return str(getattr(stream.result, "text", "") or "").strip()
        if self._whisper is not None:
            lang = os.environ.get("OMNIVISION_WHISPER_LANG", "").strip() or None
            prompt = os.environ.get("OMNIVISION_WHISPER_PROMPT", DEFAULT_WHISPER_PROMPT)
            kwargs: dict[str, Any] = {
                "beam_size": 2,
                "vad_filter": True,
                "initial_prompt": prompt,
            }
            if lang:
                kwargs["language"] = lang
            segs, _ = self._whisper.transcribe(pcm_f32, **kwargs)
            return " ".join(s.text.strip() for s in segs if s.text and s.text.strip()).strip()
        return ""

    @staticmethod
    def _resample_to_16k_mono(raw: bytes, channels: int, rate: int) -> np.ndarray:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        if rate == 16000 or audio.size == 0:
            return audio
        target_len = max(1, int(round(audio.size * 16000.0 / float(rate))))
        x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)

    def start(self, model_dir: str = "", rms_threshold: float = 0.012) -> dict:
        if self.running:
            return {"ok": True, "alreadyRunning": True, **self.status()}
        self._rms_threshold = float(rms_threshold)
        if os.name != "nt" or pyaudio is None:
            self.last_error = "WASAPI loopback requires Windows + pyaudiowpatch (optional dependency)"
            return {"ok": False, "error": self.last_error, **self.status()}
        self._init_asr(model_dir=model_dir)
        self._stop_evt.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="omnivision-audio", daemon=True)
        self._thread.start()
        return {"ok": True, **self.status()}

    def stop(self) -> dict:
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.running = False
        return {"ok": True, **self.status()}

    def _loop(self) -> None:
        pa = None
        stream = None
        try:
            pa = pyaudio.PyAudio()
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_spk = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            loopback = None
            if default_spk.get("isLoopbackDevice"):
                loopback = default_spk
            else:
                for dev in pa.get_loopback_device_info_generator():
                    if default_spk["name"] in dev["name"]:
                        loopback = dev
                        break
            if loopback is None:
                self.last_error = "No WASAPI loopback device found"
                self.running = False
                return
            self.device_name = str(loopback.get("name", ""))
            channels = int(loopback["maxInputChannels"])
            rate = int(loopback["defaultSampleRate"])
            chunk = max(1024, int(rate * 0.2))
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                frames_per_buffer=chunk,
                input=True,
                input_device_index=int(loopback["index"]),
            )
            buf: list[np.ndarray] = []
            silent_chunks = 0
            while not self._stop_evt.is_set():
                data = stream.read(chunk, exception_on_overflow=False)
                pcm16k = self._resample_to_16k_mono(data, channels, rate)
                rms = float(np.sqrt(np.mean(pcm16k * pcm16k))) if pcm16k.size else 0.0
                if rms >= self._rms_threshold:
                    buf.append(pcm16k)
                    silent_chunks = 0
                    if len(buf) >= 30:
                        self._flush_buffer(buf)
                        buf.clear()
                elif buf:
                    silent_chunks += 1
                    buf.append(pcm16k)
                    if silent_chunks >= 4:
                        self._flush_buffer(buf)
                        buf.clear()
                        silent_chunks = 0
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            self.running = False
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if pa is not None:
                    pa.terminate()
            except Exception:
                pass

    def _flush_buffer(self, chunks: list[np.ndarray]) -> None:
        if not chunks:
            return
        audio = np.concatenate(chunks)
        if self.asr_engine == "vad-only":
            self.push_manual_line(f"[检测到系统音频片段 {len(audio)/16000:.1f}s，未装本地 ASR 模型]", speaker="vad")
            return
        text = self._transcribe_16k(audio)
        if text:
            self.push_manual_line(text, speaker="system")

    def status(self) -> dict:
        with self._lock:
            count = len(self.transcripts)
            latest = self.transcripts[-1] if count else None
        return {
            "running": self.running,
            "asrEngine": self.asr_engine,
            "asrModelPath": self.asr_model_path,
            "device": self.device_name,
            "bufferedLines": count,
            "latestLine": latest,
            "lastSpeechAt": self._last_speech_at,
            "lastError": self.last_error,
            "capabilities": self.capabilities(),
        }
