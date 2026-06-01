"""Phase G: face recognition adapter tests (all mock; no real library/model).

mutation 対応 (必須4 のうち本ファイルが担う 2):
    - 顔ベクトル比較ロジックを no-op (常に None) → test_recognize_returns_match_for_known_face fail
    - tolerance しきい値を無視 (常に accept) → test_tolerance_threshold_rejects_far_match fail
(残り 2 は test_face_recognition_injection.py / test_face_recognition_config.py)
"""

from __future__ import annotations

import base64

import numpy as np
import pytest

from pico_agent.adapters import face_recognition as fr


# ── Fake recognizer (real face_recognition library の最小 I/F mock) ──────────────
class FakeRecognizer:
    """``face_recognition`` モジュールが公開する関数群を模す。

    face_distance は本物同様にユークリッド距離を返すので tolerance ロジックが
    実際に動く (mock で誤魔化さない)。
    """

    def __init__(self, query: np.ndarray | None, *, detect: bool = True) -> None:
        self._query = None if query is None else np.asarray(query, dtype=np.float64)
        self._detect = detect

    def load_image_file(self, fileobj):
        return "IMG"

    def face_locations(self, img):
        return [(0, 1, 1, 0)] if self._detect else []

    def face_encodings(self, img, boxes):
        if self._query is None:
            return []
        return [self._query]

    def face_distance(self, known_list, query):
        known = np.asarray(known_list[0], dtype=np.float64)
        q = np.asarray(query, dtype=np.float64)
        return np.array([float(np.linalg.norm(known - q))])


_KAINIT = np.zeros(8, dtype=np.float64)


def _known(vec: np.ndarray | None = None) -> dict[str, np.ndarray]:
    return {"kainit": _KAINIT if vec is None else np.asarray(vec, dtype=np.float64)}


def _far(distance: float) -> np.ndarray:
    """_KAINIT から指定ユークリッド距離だけ離れたベクトル。"""
    v = np.zeros(8, dtype=np.float64)
    v[0] = distance
    return v


@pytest.fixture(autouse=True)
def _reset_face(monkeypatch):
    for k in (
        "FACE_RECOGNITION_ENABLED",
        "FACE_RECOGNITION_TOLERANCE",
        "FACE_ENCODINGS_DIR",
        "FACE_SAMPLES_DIR",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fr, "_RECOGNIZER_OVERRIDE", None)
    monkeypatch.setattr(fr, "_RECOGNIZER_SINGLETON", None)
    monkeypatch.setattr(fr, "_RECOGNIZER_TRIED", False)
    monkeypatch.setattr(fr, "_ENCODINGS_OVERRIDE", None)
    monkeypatch.setattr(fr, "_ENCODINGS_CACHE", None)
    monkeypatch.setattr(fr, "_WARNED_ONCE", False)
    yield


# ── Group 1: 認識本体 ────────────────────────────────────────────────────────────
def test_recognize_returns_match_for_known_face():
    rec = FakeRecognizer(query=_KAINIT)
    m = fr.recognize(b"jpegbytes", recognizer=rec, known=_known())
    assert m is not None
    assert m.name == "kainit"
    assert 0.0 <= m.confidence <= 1.0
    assert m.distance == pytest.approx(0.0)


def test_recognize_returns_none_when_no_face_detected():
    rec = FakeRecognizer(query=_KAINIT, detect=False)
    assert fr.recognize(b"x", recognizer=rec, known=_known()) is None


def test_recognize_returns_none_when_no_library():
    # no override, library not installed on dev host → graceful None + warn
    assert fr.recognize(b"x") is None
    assert fr.is_available() is False


def test_recognize_returns_none_when_no_encodings(monkeypatch):
    monkeypatch.setattr(fr, "_ENCODINGS_OVERRIDE", {})
    rec = FakeRecognizer(query=_KAINIT)
    assert fr.recognize(b"x", recognizer=rec) is None


def test_confidence_decreases_with_distance():
    rec_near = FakeRecognizer(query=_far(0.1))
    rec_far = FakeRecognizer(query=_far(0.4))
    near = fr.recognize(b"x", recognizer=rec_near, known=_known())
    far = fr.recognize(b"x", recognizer=rec_far, known=_known())
    assert near is not None and far is not None
    assert near.confidence > far.confidence


def test_tolerance_threshold_rejects_far_match(monkeypatch):
    monkeypatch.setenv("FACE_RECOGNITION_TOLERANCE", "0.6")
    rec = FakeRecognizer(query=_far(0.9))  # distance 0.9 > tolerance 0.6
    assert fr.recognize(b"x", recognizer=rec, known=_known()) is None


def test_tolerance_env_override_changes_decision(monkeypatch):
    rec = FakeRecognizer(query=_far(0.7))  # distance 0.7
    monkeypatch.setenv("FACE_RECOGNITION_TOLERANCE", "0.6")
    assert fr.recognize(b"x", recognizer=rec, known=_known()) is None  # 0.7 > 0.6
    monkeypatch.setenv("FACE_RECOGNITION_TOLERANCE", "0.9")
    m = fr.recognize(b"x", recognizer=rec, known=_known())
    assert m is not None and m.name == "kainit"  # 0.7 <= 0.9


def test_confidence_clamped_0_1(monkeypatch):
    monkeypatch.setenv("FACE_RECOGNITION_TOLERANCE", "0.6")
    exact = fr.recognize(b"x", recognizer=FakeRecognizer(query=_KAINIT), known=_known())
    edge = fr.recognize(b"x", recognizer=FakeRecognizer(query=_far(0.59)), known=_known())
    assert exact is not None and exact.confidence == pytest.approx(1.0)
    assert edge is not None and 0.0 <= edge.confidence <= 1.0


def test_is_available_true_with_fakes(monkeypatch):
    monkeypatch.setattr(fr, "_RECOGNIZER_OVERRIDE", FakeRecognizer(query=_KAINIT))
    monkeypatch.setattr(fr, "_ENCODINGS_OVERRIDE", _known())
    assert fr.is_available() is True


def test_is_available_false_without_library():
    assert fr.is_available() is False


def test_broken_library_systemexit_degrades_to_none(monkeypatch):
    # A broken install (e.g. face_recognition_models missing pkg_resources) calls
    # quit() at import time → SystemExit (BaseException, not Exception). Must still
    # degrade to a no-op, not crash agent construction.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "face_recognition":
            raise SystemExit(None)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert fr._get_recognizer() is None  # no SystemExit propagates
    assert fr.recognize(b"x", known=_known()) is None


def test_warn_once_only_warns_one_time(monkeypatch):
    calls = []
    monkeypatch.setattr(fr.logger, "warning", lambda *a, **k: calls.append(1))
    fr._warn_once("first")
    fr._warn_once("second")
    assert len(calls) == 1


def test_encodings_cache_loaded_once(tmp_path, monkeypatch):
    monkeypatch.setenv("FACE_ENCODINGS_DIR", str(tmp_path))
    np.save(str(tmp_path / "kainit.npy"), _KAINIT)
    first = fr._get_known_encodings()
    assert "kainit" in first
    # delete the file; cache must still serve it (loaded once)
    (tmp_path / "kainit.npy").unlink()
    second = fr._get_known_encodings()
    assert "kainit" in second


def test_corrupt_npy_degrades_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("FACE_ENCODINGS_DIR", str(tmp_path))
    (tmp_path / "kainit.npy").write_bytes(b"not a real npy file")
    known = fr._get_known_encodings()
    assert known == {}  # corrupt skipped, no exception
    rec = FakeRecognizer(query=_KAINIT)
    assert fr.recognize(b"x", recognizer=rec) is None


def test_recognize_b64_decodes_and_runs():
    b64 = base64.b64encode(b"any-jpeg-bytes").decode("ascii")
    rec = FakeRecognizer(query=_KAINIT)
    m = fr.recognize_b64(b64, recognizer=rec, known=_known())
    assert m is not None and m.name == "kainit"
    assert fr.recognize_b64("", recognizer=rec, known=_known()) is None  # empty → None
