"""Tests for CameraTool — OpenCV mocked, no real hardware required."""

from __future__ import annotations

import base64
import threading
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_camera_tool(host: str = "192.168.1.100"):
    """Create a CameraTool with the capture thread patched out."""
    from familiar_agent.tools.camera import CameraTool

    with patch.object(CameraTool, "start"):
        cam = CameraTool.__new__(CameraTool)
        cam.host = host
        cam.username = "admin"
        cam.password = "password"
        cam.port = 2020
        cam.preview = False
        cam.ptz_host = host
        cam.ptz_username = "admin"
        cam.ptz_password = "password"
        cam.ptz_port = 2020
        cam._cam_onvif = None
        cam._ptz = None
        cam._profile_token = None
        cam._cap = None
        cam._last_frame = None
        cam._running = False
        cam._thread = None
        cam._lock = threading.Lock()
    return cam


def _make_fake_frame(height: int = 480, width: int = 640) -> "np.ndarray":
    """Create a fake BGR numpy frame."""
    return np.zeros((height, width, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# Tests: capture()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_returns_none_when_no_frame():
    """capture() returns (None, None) when no frame has been grabbed yet."""
    cam = _make_camera_tool()
    cam._last_frame = None

    b64, path = await cam.capture()

    assert b64 is None
    assert path is None


@pytest.mark.asyncio
async def test_capture_returns_base64_jpeg_on_valid_frame():
    """capture() encodes a valid frame to base64 JPEG."""
    cam = _make_camera_tool()
    cam._last_frame = _make_fake_frame()

    b64, path = await cam.capture()

    assert b64 is not None
    # Should be valid base64
    decoded = base64.b64decode(b64)
    # JPEG magic bytes: FF D8 FF
    assert decoded[:2] == b"\xff\xd8"


@pytest.mark.asyncio
async def test_capture_saves_file_to_disk(tmp_path):
    """capture() writes the JPEG to disk and returns the path."""
    import familiar_agent.tools.camera as camera_module

    cam = _make_camera_tool()
    cam._last_frame = _make_fake_frame()

    original_dir = camera_module.CAPTURE_DIR
    camera_module.CAPTURE_DIR = tmp_path
    try:
        b64, path = await cam.capture()
    finally:
        camera_module.CAPTURE_DIR = original_dir

    assert path is not None
    assert b64 is not None
    assert (tmp_path / path.split("/")[-1]).exists()


@pytest.mark.asyncio
async def test_capture_resizes_large_frame():
    """capture() resizes frames taller than 640px."""
    cam = _make_camera_tool()
    cam._last_frame = _make_fake_frame(height=1080, width=1920)

    b64, _ = await cam.capture()

    assert b64 is not None
    # Decode and verify the JPEG was produced (resize didn't crash)
    decoded = base64.b64decode(b64)
    assert decoded[:2] == b"\xff\xd8"


# ---------------------------------------------------------------------------
# Tests: call()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_see_returns_image_on_success():
    """call('see', {}) returns (description, base64_image) on success."""
    cam = _make_camera_tool()

    async def _fake_capture():
        return "fakeb64", "/tmp/capture_123.jpg"

    cam.capture = _fake_capture

    result, img = await cam.call("see", {})

    assert img == "fakeb64"
    assert "saved to" in result


@pytest.mark.asyncio
async def test_call_see_returns_error_when_capture_fails():
    """call('see', {}) returns failure message when capture returns (None, None)."""
    cam = _make_camera_tool()

    async def _fake_capture():
        return None, None

    cam.capture = _fake_capture

    result, img = await cam.call("see", {})

    assert img is None
    assert "failed" in result.lower()


@pytest.mark.asyncio
async def test_call_look_delegates_to_move():
    """call('look', ...) delegates to move() and returns its result."""
    cam = _make_camera_tool()

    async def _fake_move(direction, degrees=30):
        return f"Moved {direction} by {degrees}°"

    cam.move = _fake_move

    result, img = await cam.call("look", {"direction": "left", "degrees": 45})

    assert "left" in result
    assert "45" in result
    assert img is None


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error():
    """call() with an unrecognized tool name returns an error string."""
    cam = _make_camera_tool()

    result, img = await cam.call("nonexistent", {})

    assert "Unknown" in result or "nonexistent" in result


# ---------------------------------------------------------------------------
# Tests: move() — PTZ direction sign convention (ONVIF / Tapo C220)
# ---------------------------------------------------------------------------


class _FakePTZ:
    """Minimal async PTZ stub that records RelativeMove arguments."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def RelativeMove(self, payload: dict) -> None:
        self.calls.append(payload)


def _attach_fake_ptz(cam) -> _FakePTZ:
    """Wire a fake PTZ service into a CameraTool and bypass ONVIF connect."""
    fake = _FakePTZ()
    cam._ptz = fake
    cam._profile_token = "Profile_1"

    async def _already_connected() -> bool:
        return True

    cam._ensure_connected = _already_connected  # type: ignore[method-assign]
    return fake


@pytest.mark.asyncio
async def test_move_up_sends_positive_tilt():
    """move('up') sends a positive y to ONVIF RelativeMove (Tapo C220 / ONVIF spec)."""
    cam = _make_camera_tool()
    fake = _attach_fake_ptz(cam)

    result = await cam.move("up", degrees=90)

    assert "up" in result
    assert len(fake.calls) == 1
    pan_tilt = fake.calls[0]["Translation"]["PanTilt"]
    assert pan_tilt["x"] == 0.0
    assert pan_tilt["y"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_move_down_sends_negative_tilt():
    """move('down') sends a negative y to ONVIF RelativeMove."""
    cam = _make_camera_tool()
    fake = _attach_fake_ptz(cam)

    result = await cam.move("down", degrees=90)

    assert "down" in result
    assert len(fake.calls) == 1
    pan_tilt = fake.calls[0]["Translation"]["PanTilt"]
    assert pan_tilt["x"] == 0.0
    assert pan_tilt["y"] == pytest.approx(-1.0)


@pytest.mark.asyncio
async def test_move_left_sends_positive_pan():
    """move('left') sends a positive x to ONVIF RelativeMove (unchanged behaviour)."""
    cam = _make_camera_tool()
    fake = _attach_fake_ptz(cam)

    await cam.move("left", degrees=180)

    pan_tilt = fake.calls[0]["Translation"]["PanTilt"]
    assert pan_tilt["x"] == pytest.approx(1.0)
    assert pan_tilt["y"] == 0.0


@pytest.mark.asyncio
async def test_move_right_sends_negative_pan():
    """move('right') sends a negative x to ONVIF RelativeMove (unchanged behaviour)."""
    cam = _make_camera_tool()
    fake = _attach_fake_ptz(cam)

    await cam.move("right", degrees=180)

    pan_tilt = fake.calls[0]["Translation"]["PanTilt"]
    assert pan_tilt["x"] == pytest.approx(-1.0)
    assert pan_tilt["y"] == 0.0


def test_ptz_params_fall_back_to_stream_url_credentials():
    cam = _make_camera_tool("rtsp://stream-user:stream-pass@192.168.1.206/live0")
    cam.username = ""
    cam.password = ""
    cam.ptz_host = cam.host
    cam.ptz_username = ""
    cam.ptz_password = ""

    host, username, password, port = cam._get_ptz_connection_params()

    assert host == "192.168.1.206"
    assert username == "stream-user"
    assert password == "stream-pass"
    assert port == 2020


def test_ptz_params_prefer_explicit_overrides():
    cam = _make_camera_tool("rtsp://stream-user:stream-pass@192.168.1.206/live0")
    cam.ptz_host = "192.168.1.145"
    cam.ptz_username = "ptz-user"
    cam.ptz_password = "ptz-pass"
    cam.ptz_port = 8899

    host, username, password, port = cam._get_ptz_connection_params()

    assert host == "192.168.1.145"
    assert username == "ptz-user"
    assert password == "ptz-pass"
    assert port == 8899


# ---------------------------------------------------------------------------
# Tests: close() — Phase C-1 引き継ぎ 1-2 ONVIF aiohttp cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_awaits_onvif_client_when_present():
    """close() は ONVIF クライアントが存在すれば await close() する (Unclosed session 防止)。"""
    from unittest.mock import AsyncMock

    cam = _make_camera_tool()
    fake_onvif = AsyncMock()
    fake_onvif.close = AsyncMock()
    cam._cam_onvif = fake_onvif
    cam._ptz = object()
    cam._profile_token = "Profile_1"

    await cam.close()

    fake_onvif.close.assert_awaited_once()
    assert cam._cam_onvif is None
    assert cam._ptz is None
    assert cam._profile_token is None


@pytest.mark.asyncio
async def test_close_is_safe_when_no_onvif_client():
    """ONVIF クライアントが None でも close() は例外を出さずに完走する。"""
    cam = _make_camera_tool()
    cam._cam_onvif = None

    # 例外が出なければ OK (戻り値なし)。
    result = await cam.close()
    assert result is None


# ---------------------------------------------------------------------------
# Tests: _is_frame_black() — RTSP zero-buffer pathology detection (案 2)
# ---------------------------------------------------------------------------


def test_is_frame_black_returns_true_for_zero_frame():
    """完全な zero frame (RTSP zero-buffer) は黒画像と判定される。"""
    from familiar_agent.tools.camera import _is_frame_black

    zero = np.zeros((10, 10, 3), dtype=np.uint8)
    assert _is_frame_black(zero) is True


def test_is_frame_black_returns_false_for_normal_frame():
    """明るい通常フレームは黒画像と判定されない。"""
    from familiar_agent.tools.camera import _is_frame_black

    bright = np.full((10, 10, 3), 128, dtype=np.uint8)
    assert _is_frame_black(bright) is False


def test_is_frame_black_returns_false_for_none():
    """None フレームは黒画像扱いしない (read 失敗経路で処理する)。"""
    from familiar_agent.tools.camera import _is_frame_black

    assert _is_frame_black(None) is False


def test_is_frame_black_returns_false_for_dark_room():
    """薄暗い部屋 (mean=20) は zero buffer と区別される (false positive 防止)。"""
    from familiar_agent.tools.camera import _is_frame_black

    dark = np.full((10, 10, 3), 20, dtype=np.uint8)
    assert _is_frame_black(dark) is False


# ---------------------------------------------------------------------------
# Tests: _reset_capture() + _capture_loop() — 案 1 release/reopen + 案 2 trigger
# ---------------------------------------------------------------------------


def test_reset_capture_releases_and_reopens(monkeypatch):
    """_reset_capture() は既存 VideoCapture を release し、新規 capture を開く。"""
    cam = _make_camera_tool()
    old_cap = MagicMock()
    cam._cap = old_cap

    new_cap = MagicMock()
    cam._open_capture = lambda: new_cap  # type: ignore[method-assign]

    monkeypatch.setattr("time.sleep", lambda _: None)

    cam._reset_capture()

    assert old_cap.release.called
    assert cam._cap is new_cap


def test_capture_loop_resets_after_consecutive_black_frames(monkeypatch):
    """連続 _BLACK_CONSECUTIVE_LIMIT 枚の黒画像で _reset_capture が起動する。"""
    import familiar_agent.tools.camera as camera_module

    black = np.zeros((50, 50, 3), dtype=np.uint8)
    normal = np.full((50, 50, 3), 128, dtype=np.uint8)

    cam = _make_camera_tool()
    cam._running = True

    cap1 = MagicMock()
    cap1.isOpened.return_value = True
    cap1.read.side_effect = [
        (True, black)
    ] * camera_module._BLACK_CONSECUTIVE_LIMIT

    cap2 = MagicMock()
    cap2.isOpened.return_value = True

    def cap2_read():
        cam._running = False
        return (True, normal)

    cap2.read.side_effect = cap2_read

    captures = iter([cap1, cap2])
    cam._open_capture = lambda: next(captures)  # type: ignore[method-assign]
    monkeypatch.setattr("time.sleep", lambda _: None)

    cam._capture_loop()

    # cap1 was released during reset (and again at loop exit if _cap is cap1,
    # but here _cap was switched to cap2 before exit)
    assert cap1.release.called
    # cap2 served the final normal frame → committed to _last_frame
    assert cam._last_frame is not None
    np.testing.assert_array_equal(cam._last_frame, normal)


def test_capture_loop_does_not_reset_on_isolated_black_frame(monkeypatch):
    """黒と正常が交互に来る場合は連続カウンタがリセットされ、_reset_capture は起動しない。"""
    import familiar_agent.tools.camera as camera_module

    black = np.zeros((50, 50, 3), dtype=np.uint8)
    normal = np.full((50, 50, 3), 128, dtype=np.uint8)

    cam = _make_camera_tool()
    cam._running = True

    # 交互 (black, normal) を LIMIT * 2 ペア繰り返してもリセットされないこと
    frames: list = []
    for _ in range(camera_module._BLACK_CONSECUTIVE_LIMIT * 2):
        frames.append((True, black))
        frames.append((True, normal))

    call_idx = [0]

    def scripted_read():
        if call_idx[0] >= len(frames):
            cam._running = False
            return (True, normal)
        result = frames[call_idx[0]]
        call_idx[0] += 1
        return result

    cap1 = MagicMock()
    cap1.isOpened.return_value = True
    cap1.read.side_effect = scripted_read

    # 2 つ目の capture を返せないので、reset が走ったらテストが StopIteration で失敗する。
    captures = iter([cap1])
    cam._open_capture = lambda: next(captures)  # type: ignore[method-assign]
    monkeypatch.setattr("time.sleep", lambda _: None)

    cam._capture_loop()

    # cap1 は最終 cleanup の 1 回のみ release される (reset は走らない)
    assert cap1.release.call_count == 1


def test_capture_loop_resets_on_read_failure(monkeypatch):
    """ret=False を検知したとき _reset_capture が起動する (案 1 経路)。"""
    normal = np.full((50, 50, 3), 128, dtype=np.uint8)

    cam = _make_camera_tool()
    cam._running = True

    cap1 = MagicMock()
    cap1.isOpened.return_value = True
    cap1.read.side_effect = [(False, None)]  # 単発の read 失敗

    cap2 = MagicMock()
    cap2.isOpened.return_value = True

    def cap2_read():
        cam._running = False
        return (True, normal)

    cap2.read.side_effect = cap2_read

    captures = iter([cap1, cap2])
    cam._open_capture = lambda: next(captures)  # type: ignore[method-assign]
    monkeypatch.setattr("time.sleep", lambda _: None)

    cam._capture_loop()

    assert cap1.release.called
    assert cam._last_frame is not None
