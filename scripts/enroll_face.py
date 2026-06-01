#!/usr/bin/env python3
"""Phase G: 顔エンコーディング登録スクリプト (カイニット手動運用)。

``FACE_SAMPLES_DIR/<name>/*.jpg`` (既定 ``~/.familiar_ai/face_samples/<name>/``)
の顔写真から 128 次元エンコーディングを計算し、平均ベクトルを
``FACE_ENCODINGS_DIR/<name>.npy`` (既定 ``~/.familiar_ai/face_encodings/``) に保存する。

ピコ本体 (agent) からは独立。登録は人間が明示的に実行する一度きりの作業で、
本スクリプトが observations.db や稼働中の agent に触れることはない。

使い方:
    # 1. ~/.familiar_ai/face_samples/kainit/ に顔写真を数枚置く (正面・明るめ推奨)
    # 2. optional extra をインストール: uv pip install 'familiar-ai[face_recognition]'
    # 3. 実行:
    uv run python scripts/enroll_face.py            # name=kainit (既定)
    uv run python scripts/enroll_face.py alice       # 別名で登録

詳細は docs/FACE_RECOGNITION_SETUP.md。
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np

_DEFAULT_NAME = "kainit"
_SAMPLES_DIR = os.path.expanduser(
    os.environ.get("FACE_SAMPLES_DIR", "~/.familiar_ai/face_samples")
)
_ENCODINGS_DIR = os.path.expanduser(
    os.environ.get("FACE_ENCODINGS_DIR", "~/.familiar_ai/face_encodings")
)
_IMAGE_GLOBS = ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG")


def _load_library():
    try:
        import face_recognition  # type: ignore[import-not-found]

        return face_recognition
    except Exception:
        print(
            "ERROR: the 'face_recognition' library is not installed.\n"
            "       Install it with:  uv pip install 'familiar-ai[face_recognition]'\n"
            "       (dlib build can take 30+ minutes on a Raspberry Pi 5.)",
            file=sys.stderr,
        )
        return None


def enroll(name: str) -> int:
    """Compute and save the averaged face encoding for ``name``. Returns exit code."""
    fr = _load_library()
    if fr is None:
        return 2

    person_dir = os.path.join(_SAMPLES_DIR, name)
    paths: list[str] = []
    for pattern in _IMAGE_GLOBS:
        paths.extend(glob.glob(os.path.join(person_dir, pattern)))
    paths = sorted(set(paths))
    if not paths:
        print(
            f"ERROR: no sample images found in {person_dir}\n"
            f"       Put a few clear, front-facing photos of '{name}' there first.",
            file=sys.stderr,
        )
        return 3

    encodings: list[np.ndarray] = []
    for path in paths:
        try:
            image = fr.load_image_file(path)
            boxes = fr.face_locations(image)
            if not boxes:
                print(f"  - {os.path.basename(path)}: no face detected, skipped")
                continue
            encs = fr.face_encodings(image, boxes)
            if encs:
                encodings.append(np.asarray(encs[0], dtype=np.float64).reshape(-1))
                print(f"  - {os.path.basename(path)}: ok")
        except Exception as e:  # noqa: BLE001 - report and continue
            print(f"  - {os.path.basename(path)}: error ({e}), skipped")

    if not encodings:
        print(
            f"ERROR: no usable faces extracted from {len(paths)} image(s) in {person_dir}.",
            file=sys.stderr,
        )
        return 4

    averaged = np.mean(np.stack(encodings, axis=0), axis=0)
    os.makedirs(_ENCODINGS_DIR, exist_ok=True)
    out_path = os.path.join(_ENCODINGS_DIR, f"{name}.npy")
    np.save(out_path, averaged)
    print(
        f"\nSaved encoding for '{name}' from {len(encodings)} face(s) -> {out_path}\n"
        f"Set FACE_RECOGNITION_ENABLED=true in .env to let ピコ use it."
    )
    return 0


def main() -> int:
    name = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else _DEFAULT_NAME
    print(f"Enrolling face for '{name}'")
    print(f"  samples:   {os.path.join(_SAMPLES_DIR, name)}")
    print(f"  encodings: {_ENCODINGS_DIR}\n")
    return enroll(name)


if __name__ == "__main__":
    raise SystemExit(main())
