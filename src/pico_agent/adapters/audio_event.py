"""B4: 環境音認識 (YAMNet) — Pi5 側の音響イベント推論層。

常時 STT の「発話でないセグメント」(Whisper が空を返した音) を YAMNet (521
AudioSet クラス) で分類し、典型イベント (doorbell / glass / alarm 等) を
``on_audio_event`` callback へ流す。Phase X Stage C (Tapo motion event) の
「event → drive boost → judgment」パターンの音声版。

設計思想:
    音響イベントは「何か聞こえた」という bottom-up 知覚。重要な音 (確信度>閾値 かつ
    重要ラベル) のみ desire を boost し、ピコは記憶として持って heartbeat 発火時の
    判断材料にする。専用の自発発話経路は持たない (発話は heartbeat 経由のみ)。

二層分離 (絶対遵守、tapo_event / stt_kotoba と同型):
    - ``familiar_agent`` を一切 import しない
    - 依存は stdlib + numpy + loguru + (遅延) tflite_runtime のみ
    - 例外は raise せず silent-fail + ``logger.warning``
    - tflite_runtime / モデルファイル欠落 → ``classify`` は None を返し全機能 no-op
      (既存 STT には一切影響しない、graceful degradation)

公開 I/F:
    class AudioEvent
    def classify(wav_bytes, sample_rate=16000, *, interpreter=None) -> AudioEvent | None
    def is_important_label(label, conf) -> bool
    async def maybe_emit_audio_event(wav_bytes, on_audio_event) -> None

モデル / クラスマップは **repo 非同梱** (docs/AUDIO_EVENT_SETUP.md 参照)。
``YAMNET_MODEL_PATH`` / ``YAMNET_CLASS_MAP_PATH`` で配置先を指定。実機 YAMNet
精度の確認は Stage D (カイニット) で行う (本層は mock テスト + graceful degrade)。
"""

from __future__ import annotations

import csv
import io
import os
import time
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from loguru import logger

# ── 既定値 (環境変数で上書き可) ──────────────────────────────────────────────
_DEFAULT_MODEL_PATH = "~/yamnet_models/yamnet.tflite"
_DEFAULT_CONFIDENCE_THRESHOLD = 0.5
_DEFAULT_TOP_K = 5
_DEFAULT_IMPORTANT_LABELS = "doorbell,glass,alarm,fire alarm,smoke detector,scream,shout,siren"
_SAMPLE_RATE = 16000

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")

# ── 内部状態 (lazy singleton + mock seam) ────────────────────────────────────
_INTERPRETER_OVERRIDE: Any | None = None  # tests がここに FakeInterpreter を入れる
_INTERPRETER_SINGLETON: Any | None = None
_INTERPRETER_TRIED = False
_CLASSES_OVERRIDE: tuple[str, ...] | None = None  # tests がクラス名を注入する
_CLASSES_CACHE: tuple[str, ...] | None = None
_WARNED_ONCE = False


@dataclass(frozen=True)
class AudioEvent:
    """1 音響イベント (二層境界を渡る型)。"""

    top_label: str
    top_confidence: float
    labels: tuple[tuple[str, float], ...]  # 上位 k 個 (label, conf) 降順
    is_important: bool
    timestamp: float


# ── 環境変数アクセサ ─────────────────────────────────────────────────────────
def _get_model_path() -> str:
    return os.path.expanduser(os.environ.get("YAMNET_MODEL_PATH", _DEFAULT_MODEL_PATH))


def _get_class_map_path() -> str:
    raw = os.environ.get("YAMNET_CLASS_MAP_PATH", "").strip()
    if raw:
        return os.path.expanduser(raw)
    # 既定: モデルと同じディレクトリの yamnet_class_map.csv
    return os.path.join(os.path.dirname(_get_model_path()), "yamnet_class_map.csv")


def _get_confidence_threshold() -> float:
    raw = os.environ.get("YAMNET_CONFIDENCE_THRESHOLD", "")
    try:
        return float(raw) if raw else _DEFAULT_CONFIDENCE_THRESHOLD
    except ValueError:
        return _DEFAULT_CONFIDENCE_THRESHOLD


def _get_top_k() -> int:
    raw = os.environ.get("YAMNET_TOP_K", "")
    try:
        return int(raw) if raw else _DEFAULT_TOP_K
    except ValueError:
        return _DEFAULT_TOP_K


def _get_important_labels() -> tuple[str, ...]:
    raw = os.environ.get("YAMNET_IMPORTANT_LABELS", _DEFAULT_IMPORTANT_LABELS)
    return tuple(t.strip().lower() for t in raw.split(",") if t.strip())


def _get_enabled() -> bool:
    raw = os.environ.get("YAMNET_ENABLED", "").strip().lower()
    if raw in _FALSY:
        return False
    return True


def _warn_once(msg: str) -> None:
    global _WARNED_ONCE
    if not _WARNED_ONCE:
        logger.warning(msg)
        _WARNED_ONCE = True


# ── クラス名ロード (csv、override seam 付き) ──────────────────────────────────
def _get_classes() -> tuple[str, ...]:
    """YAMNet クラス名 (順序付き) を class_map.csv からロード・キャッシュ。

    csv 形式: ``index,mid,display_name`` (ヘッダ有)。テストは _CLASSES_OVERRIDE で注入。
    """
    global _CLASSES_CACHE
    if _CLASSES_OVERRIDE is not None:
        return _CLASSES_OVERRIDE
    if _CLASSES_CACHE is not None:
        return _CLASSES_CACHE
    path = _get_class_map_path()
    names: list[str] = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if rows and rows[0] and rows[0][0].strip().lower() == "index":
            rows = rows[1:]
        for row in rows:
            if len(row) >= 3:
                names.append(row[2].strip())
    except Exception:
        names = []
    _CLASSES_CACHE = tuple(names)
    return _CLASSES_CACHE


# ── interpreter (lazy singleton) ─────────────────────────────────────────────
def _get_interpreter() -> Any | None:
    global _INTERPRETER_SINGLETON, _INTERPRETER_TRIED
    if not _get_enabled():
        return None
    if _INTERPRETER_TRIED:
        return _INTERPRETER_SINGLETON
    _INTERPRETER_TRIED = True
    try:
        from tflite_runtime.interpreter import Interpreter  # type: ignore[import-not-found]
    except Exception:
        _warn_once("audio_event: tflite_runtime not installed; YAMNet disabled")
        return None
    path = _get_model_path()
    if not os.path.exists(path):
        _warn_once(f"audio_event: YAMNet model not found at {path}; YAMNet disabled")
        return None
    try:
        interp = Interpreter(model_path=path)
        interp.allocate_tensors()
        _INTERPRETER_SINGLETON = interp
        logger.info("audio_event: YAMNet interpreter loaded ({})", path)
    except Exception as e:
        _warn_once(f"audio_event: failed to load YAMNet ({e}); disabled")
        _INTERPRETER_SINGLETON = None
    return _INTERPRETER_SINGLETON


# ── WAV デコード + 推論 ──────────────────────────────────────────────────────
def _decode_wav(wav_bytes: bytes) -> np.ndarray:
    """WAV bytes → float32 mono 16k samples ∈ [-1, 1]。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nchan = wf.getnchannels()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if nchan == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    if rate != _SAMPLE_RATE and samples.size:
        # 単純な線形リサンプル (安全分岐。実運用は 16k 供給、Stage D で検証)。
        n_out = int(round(samples.size * _SAMPLE_RATE / rate))
        samples = np.interp(
            np.linspace(0.0, samples.size, n_out, endpoint=False),
            np.arange(samples.size),
            samples,
        ).astype(np.float32)
    return samples


def _run_interpreter(interp: Any, samples: np.ndarray) -> np.ndarray:
    """YAMNet を 1 回呼び、521 クラスの平均スコアベクトルを返す。"""
    inp = interp.get_input_details()
    out = interp.get_output_details()
    try:
        interp.resize_tensor_input(inp[0]["index"], [int(samples.size)])
        interp.allocate_tensors()
    except Exception:
        pass  # 固定 shape のモデル / fake では不要
    interp.set_tensor(inp[0]["index"], samples)
    interp.invoke()
    scores = np.asarray(interp.get_tensor(out[0]["index"]), dtype=np.float32)
    if scores.ndim == 2:  # [frames, classes] → クラス軸で平均
        scores = scores.mean(axis=0)
    return scores.reshape(-1)


def classify(
    wav_bytes: bytes, sample_rate: int = _SAMPLE_RATE, *, interpreter: Any | None = None
) -> AudioEvent | None:
    """音響セグメントを分類して AudioEvent を返す。推論不能なら None (graceful)。"""
    interp = interpreter or _INTERPRETER_OVERRIDE or _get_interpreter()
    if interp is None:
        return None
    try:
        classes = _get_classes()
        if not classes:
            return None
        samples = _decode_wav(wav_bytes)
        if samples.size == 0:
            return None
        scores = _run_interpreter(interp, samples)
        if scores.size != len(classes):
            return None
        k = max(1, _get_top_k())
        order = np.argsort(scores)[::-1][:k]
        labels = tuple((classes[i], float(scores[i])) for i in order)
        top_label, top_conf = labels[0]
        return AudioEvent(
            top_label=top_label,
            top_confidence=top_conf,
            labels=labels,
            is_important=is_important_label(top_label, top_conf),
            timestamp=time.time(),
        )
    except Exception as e:
        logger.warning("audio_event.classify failed: {}", e)
        return None


def is_important_label(label: str, conf: float) -> bool:
    """重要ラベル (確信度 ≥ 閾値 かつ 重要リストに部分一致) か。"""
    if conf < _get_confidence_threshold():
        return False
    ll = (label or "").lower()
    return any(token in ll for token in _get_important_labels())


async def maybe_emit_audio_event(
    wav_bytes: bytes,
    on_audio_event: Callable[[AudioEvent], Awaitable[None]] | None,
) -> None:
    """発話でないセグメントを分類し、イベントがあれば callback へ流す (silent-fail)。"""
    if on_audio_event is None:
        return
    try:
        evt = classify(wav_bytes)
        if evt is not None:
            await on_audio_event(evt)
    except Exception as e:
        logger.warning("audio_event.maybe_emit: {}", e)
