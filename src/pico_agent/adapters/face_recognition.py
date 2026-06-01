"""Phase G: 顔認識 (face recognition) — Pi5 側の視覚 identity 推論層。

カメラフレーム (``see()`` の base64 JPEG) に映る人物が既知の人物 (カイニット) に
似ているかを 128 次元の顔ベクトル照合で判定する。判定結果 (name, confidence) を
familiar_agent 側へ「らしさ」のヒントとして渡し、最終的な「カイニットだ / 別人だ」
の判断はピコの LLM judgment に委ねる (Problem-1 の autonomy を維持)。

設計思想:
    顔認識は「この顔は誰かに似ている」という bottom-up な手掛かりにすぎない。
    identity を上書きせず confidence と共にピコへ渡し、ToM の default は
    ``unknown_person`` のまま据え置く。誤認 (別人をカイニット断定) を避けるため、
    tolerance しきい値で「言及するか否か」を絞り、confidence をそのまま見せて
    judgment を委ねる。

二層分離 (絶対遵守、audio_event / stt_kotoba と同型):
    - ``familiar_agent`` を一切 import しない
    - 依存は stdlib + numpy + loguru + (遅延) face_recognition のみ
    - 例外は raise せず silent-fail + ``logger.warning``
    - face_recognition ライブラリ / 顔エンコーディング欠落 → ``recognize_b64`` は
      None を返し全機能 no-op (既存 see() 挙動には一切影響しない、graceful degrade)

公開 I/F:
    class FaceMatch
    def recognize(image_bytes, *, recognizer=None, known=None) -> FaceMatch | None
    def recognize_b64(image_b64, *, recognizer=None, known=None) -> FaceMatch | None
    def is_available() -> bool

顔ベクトルは **repo 非同梱**。``FACE_SAMPLES_DIR`` に顔写真を置き
``scripts/enroll_face.py`` で ``FACE_ENCODINGS_DIR/<name>.npy`` を生成する
(docs/FACE_RECOGNITION_SETUP.md 参照)。実機精度の確認はカイニットが行う
(本層は mock テスト + graceful degrade のみ)。ライブラリ (dlib) のビルドが重い
ため optional extra ``familiar-ai[face_recognition]`` として分離している。
"""

from __future__ import annotations

import base64
import glob
import io
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
from loguru import logger

# ── 既定値 (環境変数で上書き可) ──────────────────────────────────────────────
_DEFAULT_TOLERANCE = 0.6  # dlib の既定。小さいほど厳格 (誤認しにくいが取りこぼす)
_DEFAULT_ENCODINGS_DIR = "~/.familiar_ai/face_encodings"

_FALSY = ("0", "false", "no", "off")

# ── 内部状態 (lazy singleton + mock seam、audio_event と同型) ─────────────────
_RECOGNIZER_OVERRIDE: Any | None = None  # tests がここに FakeRecognizer を入れる
_RECOGNIZER_SINGLETON: Any | None = None
_RECOGNIZER_TRIED = False
_ENCODINGS_OVERRIDE: dict[str, np.ndarray] | None = None  # tests が顔ベクトルを注入
_ENCODINGS_CACHE: dict[str, np.ndarray] | None = None
_WARNED_ONCE = False


@dataclass(frozen=True)
class FaceMatch:
    """1 件の顔照合結果 (二層境界を渡る型)。"""

    name: str
    confidence: float  # ∈ [0, 1]。tolerance に対する近さ。ピコへそのまま渡す
    distance: float  # 生の顔ベクトル距離 (小さいほど類似)


# ── 環境変数アクセサ ─────────────────────────────────────────────────────────
def _get_enabled() -> bool:
    """master gate は familiar_agent 側 (config.face_recognition_enabled)。

    本層の env は「ライブラリ層を試すか」のみを司り、既定は有効 (audio_event と同型)。
    呼び出し側が flag off の場合はそもそも本モジュールを呼ばない。
    """
    raw = os.environ.get("FACE_RECOGNITION_ENABLED", "").strip().lower()
    if raw in _FALSY:
        return False
    return True


def _get_tolerance() -> float:
    raw = os.environ.get("FACE_RECOGNITION_TOLERANCE", "")
    try:
        t = float(raw) if raw else _DEFAULT_TOLERANCE
    except ValueError:
        return _DEFAULT_TOLERANCE
    return t if t > 0 else _DEFAULT_TOLERANCE


def _get_encodings_dir() -> str:
    return os.path.expanduser(
        os.environ.get("FACE_ENCODINGS_DIR", _DEFAULT_ENCODINGS_DIR).strip()
        or _DEFAULT_ENCODINGS_DIR
    )


def _warn_once(msg: str) -> None:
    global _WARNED_ONCE
    if not _WARNED_ONCE:
        logger.warning(msg)
        _WARNED_ONCE = True


# ── recognizer (lazy singleton) ──────────────────────────────────────────────
def _get_recognizer() -> Any | None:
    """``face_recognition`` ライブラリモジュールを遅延ロード (無ければ None)。

    返すのはライブラリモジュール自身 (face_locations / face_encodings /
    face_distance / load_image_file を持つ)。FakeRecognizer は同じ I/F を満たす。
    """
    global _RECOGNIZER_SINGLETON, _RECOGNIZER_TRIED
    if not _get_enabled():
        return None
    if _RECOGNIZER_TRIED:
        return _RECOGNIZER_SINGLETON
    _RECOGNIZER_TRIED = True
    try:
        import face_recognition as _lib  # type: ignore[import-not-found]
    except Exception:
        _warn_once(
            "face_recognition: library not installed; face recognition disabled "
            "(install 'familiar-ai[face_recognition]')"
        )
        return None
    _RECOGNIZER_SINGLETON = _lib
    logger.info("face_recognition: library loaded")
    return _RECOGNIZER_SINGLETON


# ── 既知エンコーディングのロード (npy、override seam 付き) ─────────────────────
def _get_known_encodings() -> dict[str, np.ndarray]:
    """``FACE_ENCODINGS_DIR/<name>.npy`` を全ロードして {name: vec} を返す。

    壊れた / 読めない npy はスキップ (graceful)。テストは _ENCODINGS_OVERRIDE で注入。
    """
    global _ENCODINGS_CACHE
    if _ENCODINGS_OVERRIDE is not None:
        return _ENCODINGS_OVERRIDE
    if _ENCODINGS_CACHE is not None:
        return _ENCODINGS_CACHE
    known: dict[str, np.ndarray] = {}
    directory = _get_encodings_dir()
    try:
        paths = sorted(glob.glob(os.path.join(directory, "*.npy")))
    except Exception:
        paths = []
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            vec = np.load(path)
            if vec is not None and np.asarray(vec).size:
                known[name] = np.asarray(vec, dtype=np.float64).reshape(-1)
        except Exception:
            # 壊れた npy は無視して他を読む (no-op degrade)
            continue
    _ENCODINGS_CACHE = known
    return known


def _resolve_known(known: dict[str, np.ndarray] | None) -> dict[str, np.ndarray]:
    if known is not None:
        return known
    if _ENCODINGS_OVERRIDE is not None:
        return _ENCODINGS_OVERRIDE
    return _get_known_encodings()


def is_available() -> bool:
    """ライブラリがロードでき、かつ既知エンコーディングが 1 件以上あるか。

    config.resolve_tom_default_person() の警告判定に使う (Problem-1 整合)。

    注: recognizer / encodings は lazy singleton + キャッシュ (audio_event と同型)
    なので、プロセス起動後にライブラリ導入や顔登録を行った場合は再起動まで結果が
    更新されない。登録 (enroll) は起動前のオフライン作業なので運用上は問題ない。
    """
    try:
        rec = _RECOGNIZER_OVERRIDE or _get_recognizer()
        if rec is None:
            return False
        return bool(_resolve_known(None))
    except Exception:
        return False


# ── 照合本体 ─────────────────────────────────────────────────────────────────
def recognize(
    image_bytes: bytes,
    *,
    recognizer: Any | None = None,
    known: dict[str, np.ndarray] | None = None,
) -> FaceMatch | None:
    """画像 bytes から最良の顔照合を返す。識別不能なら None (graceful)。"""
    rec = recognizer or _RECOGNIZER_OVERRIDE or _get_recognizer()
    if rec is None:
        return None
    known_enc = _resolve_known(known)
    if not known_enc:
        return None
    try:
        img = rec.load_image_file(io.BytesIO(image_bytes))
        boxes = rec.face_locations(img)
        if not boxes:
            return None  # 顔 (人物) 不在 → 無注入
        encs = rec.face_encodings(img, boxes)
        if not encs:
            return None
        query = np.asarray(encs[0], dtype=np.float64).reshape(-1)

        best_name: str | None = None
        best_dist = float("inf")
        for name, vec in known_enc.items():
            dist = float(rec.face_distance([np.asarray(vec, dtype=np.float64).reshape(-1)], query)[0])
            if dist < best_dist:
                best_dist = dist
                best_name = name

        if best_name is None:
            return None
        tolerance = _get_tolerance()
        if best_dist > tolerance:
            return None  # しきい値超え → 別人扱い (誤認回避)
        confidence = max(0.0, min(1.0, 1.0 - best_dist / tolerance))
        return FaceMatch(name=best_name, confidence=confidence, distance=best_dist)
    except Exception as e:
        logger.warning("face_recognition.recognize failed: {}", e)
        return None


def recognize_b64(
    image_b64: str,
    *,
    recognizer: Any | None = None,
    known: dict[str, np.ndarray] | None = None,
) -> FaceMatch | None:
    """base64 JPEG (see() の戻り) をデコードして照合。デコード失敗時は None。"""
    if not image_b64:
        return None
    try:
        raw = base64.b64decode(image_b64)
    except Exception as e:
        logger.warning("face_recognition.recognize_b64 decode failed: {}", e)
        return None
    return recognize(raw, recognizer=recognizer, known=known)
