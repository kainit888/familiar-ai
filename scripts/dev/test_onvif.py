"""ONVIF 認証単体検証スクリプト (Phase B Step 5 の遺物)。

camera.py の `_ensure_connected` と同じロジックで ONVIF 認証のみ試行する。
capture スレッドは起動しない。.env を読み込んで CAMERA_* を参照する。

使い方:
    cd /home/pico/pico_v3
    uv run python scripts/dev/test_onvif.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import onvif
from dotenv import load_dotenv
from onvif import ONVIFCamera


async def main() -> int:
    load_dotenv()

    host = os.environ.get("CAMERA_HOST")
    user = os.environ.get("CAMERA_USERNAME")
    password = os.environ.get("CAMERA_PASSWORD")
    onvif_port_str = os.environ.get("CAMERA_ONVIF_PORT", "2020")

    if not host or not user or not password:
        print("[ERR ] CAMERA_HOST / CAMERA_USERNAME / CAMERA_PASSWORD missing in .env")
        return 2

    onvif_port = int(onvif_port_str)

    onvif_dir = os.path.dirname(onvif.__file__)
    wsdl_dir = os.path.join(onvif_dir, "wsdl")
    if not os.path.isdir(wsdl_dir):
        wsdl_dir = os.path.join(os.path.dirname(onvif_dir), "wsdl")
    print(f"[info] wsdl_dir={wsdl_dir} exists={os.path.isdir(wsdl_dir)}")

    ports_to_try = [onvif_port]
    for fallback in (8080, 80):
        if fallback != onvif_port:
            ports_to_try.append(fallback)
    print(f"[info] ports_to_try={ports_to_try}")

    last_error: Exception | None = None
    for try_port in ports_to_try:
        try:
            print(f"[try ] port={try_port}")
            cam = ONVIFCamera(host, try_port, user, password, wsdl_dir=wsdl_dir)
            await cam.update_xaddrs()
            media = await cam.create_media_service()
            profiles = await media.GetProfiles()
            profile_token = profiles[0].token if profiles else "Profile_1"
            ptz = await cam.create_ptz_service()
            print(f"[ ok ] Camera PTZ connected via ONVIF: {host} (port {try_port})")
            print(f"[info] profile_count={len(profiles)} profile_token={profile_token}")
            print(f"[info] ptz_service={ptz!r}")
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[fail] port={try_port} error={type(e).__name__}: {e}")
            last_error = e

    print(f"[ERR ] ONVIF PTZ unavailable. last_error={last_error!r}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
