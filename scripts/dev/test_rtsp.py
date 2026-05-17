"""RTSP 1 フレーム取得単体検証スクリプト (Phase B Step 6 の遺物)。

camera.py の `_get_stream_url` と同じ URL を組み立てて OpenCV で 1 フレーム取得し、
/tmp/phase_b_capture.jpg に保存する。.env から CAMERA_* を読み込む。

使い方:
    cd /home/pico/pico_v3
    uv run python scripts/dev/test_rtsp.py
"""

from __future__ import annotations

import os
import sys

import cv2  # type: ignore[import-untyped]
from dotenv import load_dotenv


STREAM_CANDIDATES = ["stream1", "stream2"]
OUT_PATH = "/tmp/phase_b_capture.jpg"


def try_stream(host: str, user: str, password: str, path: str) -> bool:
    url = f"rtsp://{user}:{password}@{host}:554/{path}"
    print(f"[try ] path={path}")
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f"[fail] VideoCapture cannot open {path}")
        cap.release()
        return False
    frame = None
    for i in range(30):
        ok, frame = cap.read()
        if ok and frame is not None:
            print(f"[ ok ] frame got at attempt {i + 1} shape={frame.shape}")
            break
    cap.release()
    if frame is None:
        print(f"[fail] no frame read from {path}")
        return False
    ok = cv2.imwrite(OUT_PATH, frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        print(f"[fail] cv2.imwrite failed for {OUT_PATH}")
        return False
    size = os.path.getsize(OUT_PATH)
    print(f"[ ok ] saved {OUT_PATH} size={size} bytes")
    return True


def main() -> int:
    load_dotenv()
    host = os.environ.get("CAMERA_HOST")
    user = os.environ.get("CAMERA_USERNAME")
    password = os.environ.get("CAMERA_PASSWORD")
    if not host or not user or not password:
        print("[ERR ] CAMERA_HOST / CAMERA_USERNAME / CAMERA_PASSWORD missing in .env")
        return 2

    for s in STREAM_CANDIDATES:
        if try_stream(host, user, password, s):
            return 0
    print("[ERR ] all streams failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
