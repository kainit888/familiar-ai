"""RTSP 黒画像問題の診断スクリプト (タスク F の産物)。

`/home/pico/.familiar_ai/captures/` の画像が全て RGB(0,0,0) になっている
現象 (vision_accuracy_issues.md 参照) の原因切り分け用。

実行する検査:
    1. stream1 / stream2 それぞれで RTSP 接続を試行
    2. 各 stream で複数フレーム取得 (warmup → steady state)
    3. 取得した各フレームの shape / dtype / min / max / mean を記録
    4. UDP / TCP 両方の transport で試行
    5. 1 秒スリープを挟んだ取得 (バッファ問題切り分け)
    6. ONVIF の SnapshotUri 経由 (RTSP とは別経路) でも試行
    7. 結果を JSON 風サマリで出力

使い方:
    cd /home/pico/pico_v3
    uv run python scripts/dev/diagnose_rtsp_black.py

出力先: /tmp/diagnose_rtsp_black/  (各 stream の 1 枚目フレームと診断ログ)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2  # type: ignore[import-untyped]
import numpy as np  # type: ignore[import-untyped]
from dotenv import load_dotenv

OUT_DIR = Path("/tmp/diagnose_rtsp_black")
STREAM_CANDIDATES = ["stream1", "stream2"]
TRANSPORT_CANDIDATES = ["tcp", "udp"]
FRAMES_PER_STREAM = 10           # warmup + steady state
SLEEP_BETWEEN_FRAMES_SEC = 0.5   # バッファ問題切り分け
TIMEOUT_PER_OPEN_SEC = 10        # cv2 内部でハンドリングされる目安


def _frame_stats(frame: "np.ndarray | None") -> dict:
    """フレームの shape / dtype / 最小/最大/平均ピクセル値を辞書で返す。"""
    if frame is None:
        return {"frame": "None"}
    stats: dict = {
        "shape": list(frame.shape),
        "dtype": str(frame.dtype),
        "size_bytes": int(frame.nbytes),
        "min": int(frame.min()),
        "max": int(frame.max()),
        "mean": round(float(frame.mean()), 3),
    }
    # チャネル別統計 (BGR 想定)
    if frame.ndim == 3 and frame.shape[2] >= 3:
        b, g, r = frame[:, :, 0], frame[:, :, 1], frame[:, :, 2]
        stats["channel_means"] = {
            "B": round(float(b.mean()), 3),
            "G": round(float(g.mean()), 3),
            "R": round(float(r.mean()), 3),
        }
        # 黒画素の割合 (全 BGR < 5)
        is_black = (b < 5) & (g < 5) & (r < 5)
        stats["black_pixel_ratio"] = round(float(is_black.mean()), 4)
    return stats


def try_stream(
    host: str,
    user: str,
    password: str,
    stream_path: str,
    transport: str,
) -> dict:
    """1 stream × 1 transport で複数フレーム取得を試行、診断結果を返す。"""
    label = f"{stream_path}/{transport}"
    print(f"\n=== [{label}] ===")
    result: dict = {"stream": stream_path, "transport": transport, "frames": []}

    url = f"rtsp://{user}:{password}@{host}:554/{stream_path}"
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{transport}"

    t0 = time.time()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    open_time = round(time.time() - t0, 3)
    is_opened = cap.isOpened()
    result["open_time_sec"] = open_time
    result["is_opened"] = is_opened

    if not is_opened:
        print(f"[fail] cv2.VideoCapture cannot open (open_time={open_time}s)")
        cap.release()
        result["error"] = "cv2.VideoCapture.isOpened() returned False"
        return result

    print(f"[ ok ] opened in {open_time}s")

    # capture properties (cv2 経由で取れる stream metadata)
    try:
        result["properties"] = {
            "CAP_PROP_FRAME_WIDTH": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "CAP_PROP_FRAME_HEIGHT": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "CAP_PROP_FPS": round(float(cap.get(cv2.CAP_PROP_FPS)), 2),
            "CAP_PROP_FORMAT": int(cap.get(cv2.CAP_PROP_FORMAT)),
            "CAP_PROP_FOURCC": int(cap.get(cv2.CAP_PROP_FOURCC)),
        }
    except Exception as e:
        result["properties_error"] = str(e)

    for i in range(FRAMES_PER_STREAM):
        t_read_start = time.time()
        ok, frame = cap.read()
        read_time = round(time.time() - t_read_start, 3)
        info: dict = {
            "frame_index": i,
            "ok": ok,
            "read_time_sec": read_time,
        }
        if not ok:
            info["error"] = "cap.read() returned False"
        else:
            info["stats"] = _frame_stats(frame)
            # 1 枚目のフレームだけ保存
            if i == 0 or (i == FRAMES_PER_STREAM - 1):  # 1 枚目と最後
                OUT_DIR.mkdir(parents=True, exist_ok=True)
                fname = f"{stream_path}_{transport}_frame{i:02d}.jpg"
                save_path = OUT_DIR / fname
                cv2.imwrite(str(save_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                info["saved_to"] = str(save_path)
                info["saved_bytes"] = save_path.stat().st_size
        result["frames"].append(info)
        # 短いログ出力
        if ok and frame is not None:
            print(
                f"[frame {i:02d}] ok shape={frame.shape} "
                f"min={frame.min()} max={frame.max()} mean={frame.mean():.1f}"
            )
        else:
            print(f"[frame {i:02d}] FAIL")
        time.sleep(SLEEP_BETWEEN_FRAMES_SEC)

    cap.release()
    return result


def try_onvif_snapshot(host: str, user: str, password: str) -> dict:
    """ONVIF SnapshotUri 経由で 1 枚画像を取得 (RTSP とは別経路)。"""
    print("\n=== [ONVIF SnapshotUri] ===")
    result: dict = {"method": "ONVIF/SnapshotUri"}

    try:
        import asyncio
        from onvif import ONVIFCamera  # type: ignore[import-untyped]
        import aiohttp

        async def _go() -> dict:
            ports_to_try = [2020, 8080, 80]
            for port in ports_to_try:
                try:
                    onvif_dir = os.path.dirname(__import__("onvif").__file__)
                    wsdl_dir = os.path.join(onvif_dir, "wsdl")
                    if not os.path.isdir(wsdl_dir):
                        wsdl_dir = os.path.join(os.path.dirname(onvif_dir), "wsdl")
                    cam = ONVIFCamera(host, port, user, password, wsdl_dir=wsdl_dir)
                    await cam.update_xaddrs()
                    media = await cam.create_media_service()
                    profiles = await media.GetProfiles()
                    profile_token = profiles[0].token if profiles else None
                    if not profile_token:
                        await cam.close()
                        continue
                    snap_uri_obj = await media.GetSnapshotUri(
                        {"ProfileToken": profile_token}
                    )
                    snap_uri = snap_uri_obj.Uri if snap_uri_obj else None
                    await cam.close()
                    return {
                        "onvif_port": port,
                        "profile_token": profile_token,
                        "snapshot_uri": snap_uri,
                    }
                except Exception as e:
                    print(f"[onvif:{port}] failed: {e}")
                    continue
            return {"error": "ONVIF connection failed on all ports"}

        loop_info = asyncio.run(_go())
        result.update(loop_info)

        snap_uri = result.get("snapshot_uri")
        if not snap_uri:
            print("[onvif] no SnapshotUri available")
            return result

        # SnapshotUri を HTTP GET
        async def _fetch() -> dict:
            timeout = aiohttp.ClientTimeout(total=10)
            auth = aiohttp.BasicAuth(user, password)
            async with aiohttp.ClientSession(timeout=timeout, auth=auth) as session:
                async with session.get(snap_uri) as resp:
                    body = await resp.read()
                    return {"status": resp.status, "bytes": len(body), "body": body}

        fetch_result = asyncio.run(_fetch())
        result["http_status"] = fetch_result["status"]
        result["bytes"] = fetch_result["bytes"]

        if fetch_result["status"] == 200 and fetch_result["bytes"] > 0:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            save_path = OUT_DIR / "onvif_snapshot.jpg"
            save_path.write_bytes(fetch_result["body"])
            # 取得画像の pixel stats
            img = cv2.imread(str(save_path))
            if img is not None:
                result["frame_stats"] = _frame_stats(img)
            result["saved_to"] = str(save_path)
            print(
                f"[onvif] saved {save_path} bytes={fetch_result['bytes']} "
                f"shape={img.shape if img is not None else None}"
            )
    except Exception as e:
        result["error"] = f"unexpected error: {e}"
        print(f"[onvif] error: {e}")

    return result


def main() -> int:
    load_dotenv()
    host = os.environ.get("CAMERA_HOST")
    user = os.environ.get("CAMERA_USERNAME")
    password = os.environ.get("CAMERA_PASSWORD")
    if not host or not user or not password:
        print("[ERR ] CAMERA_HOST / CAMERA_USERNAME / CAMERA_PASSWORD missing in .env")
        return 2

    print(f"Camera host: {host}")
    print(f"Camera user: {user[:3]}***")
    print(f"Output dir : {OUT_DIR}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    diagnosis: dict = {
        "host": host,
        "out_dir": str(OUT_DIR),
        "streams": {},
        "onvif": None,
    }

    for stream in STREAM_CANDIDATES:
        for transport in TRANSPORT_CANDIDATES:
            try:
                key = f"{stream}/{transport}"
                diagnosis["streams"][key] = try_stream(
                    host, user, password, stream, transport
                )
            except Exception as e:
                print(f"[ERR ] {stream}/{transport}: {e}")
                diagnosis["streams"][f"{stream}/{transport}"] = {"error": str(e)}

    try:
        diagnosis["onvif"] = try_onvif_snapshot(host, user, password)
    except Exception as e:
        diagnosis["onvif"] = {"error": str(e)}

    # 統合サマリ JSON
    summary_path = OUT_DIR / "diagnosis.json"
    summary_path.write_text(
        json.dumps(diagnosis, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n[ ok ] full diagnosis saved to {summary_path}")

    # 一行サマリ
    print("\n=== サマリ (1 行ずつ) ===")
    for key, info in diagnosis["streams"].items():
        if isinstance(info, dict) and info.get("is_opened"):
            frames = info.get("frames", [])
            ok_count = sum(1 for f in frames if f.get("ok"))
            means = [
                f.get("stats", {}).get("mean", -1)
                for f in frames
                if f.get("ok")
            ]
            print(
                f"  {key:20s}: opened, {ok_count}/{len(frames)} frames ok, "
                f"means={means}"
            )
        else:
            err = info.get("error", "unknown") if isinstance(info, dict) else "type"
            print(f"  {key:20s}: FAILED ({err})")

    onvif = diagnosis.get("onvif", {})
    if isinstance(onvif, dict):
        if "frame_stats" in onvif:
            print(
                f"  onvif/snapshot      : HTTP {onvif.get('http_status')}, "
                f"bytes={onvif.get('bytes')}, "
                f"mean={onvif['frame_stats'].get('mean', '?')}"
            )
        else:
            print(f"  onvif/snapshot      : FAILED ({onvif.get('error', 'unknown')})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
