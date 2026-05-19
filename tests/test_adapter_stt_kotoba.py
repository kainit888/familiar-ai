"""Tests for pico_agent.adapters.stt_kotoba (Phase C-1 → Phase C-4 本実装)。

whisper_server.py の /transcribe エンドポイントを aiohttp で叩く想定。
JSON / text/plain 両レスポンスをサポートし、silent fail で空文字を返す。

Phase C-4 で start_rtsp_subscription を ffmpeg silencedetect ベースに本実装。
依存欠落 (URL なし / ffmpeg なし / VAD backend disabled) なら no-op タスクを返す。
"""

from __future__ import annotations

import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pico_agent.adapters import stt_kotoba


# ── 共通 fixture: CAMERA_* / STT_RTSP_URL を必ず未設定状態にして起動 ──────
#
# テスト実行時のシェル環境変数 (.env) に CAMERA_HOST 等が混ざっていると、
# _get_rtsp_url() が組み立てに成功してしまい no-op パスが壊れる。
# autouse でクリーンアップして決定論性を保つ。


@pytest.fixture(autouse=True)
def _clean_rtsp_env(monkeypatch):
    """各テスト開始時に RTSP 関連 env を一旦剥がす。"""
    for key in (
        "STT_RTSP_URL",
        "CAMERA_HOST",
        "CAMERA_USERNAME",
        "CAMERA_PASSWORD",
        "STT_VAD_BACKEND",
        "STT_VAD_NOISE_DB",
        "STT_VAD_MIN_SILENCE_SEC",
        "STT_VAD_MAX_SEGMENT_SEC",
        "STT_VAD_MIN_SEGMENT_SEC",
        "STT_FFMPEG_RESTART_BACKOFF_SEC",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_mock_session(
    status: int = 200,
    json_payload: dict | None = None,
    text_body: str = "",
    content_type: str = "application/json",
):
    """aiohttp.ClientSession の async context をスタブで用意。"""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_payload or {})
    mock_resp.text = AsyncMock(return_value=text_body)
    mock_resp.headers = {"Content-Type": content_type}
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_transcribe_returns_text_from_json_response():
    """正常系: {"text": "..."} を str で返す。"""
    mock_session = _make_mock_session(
        status=200,
        json_payload={"text": "おはよう、ピコ。"},
        content_type="application/json",
    )

    with patch("pico_agent.adapters.stt_kotoba.aiohttp.ClientSession", return_value=mock_session):
        result = await stt_kotoba.transcribe(b"WAVDATA")

    assert result == "おはよう、ピコ。"
    mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_transcribe_supports_plain_text_response():
    """Content-Type: text/plain でも結果テキストを取り出せる。"""
    mock_session = _make_mock_session(
        status=200,
        text_body="hello world\n",
        content_type="text/plain",
    )

    with patch("pico_agent.adapters.stt_kotoba.aiohttp.ClientSession", return_value=mock_session):
        result = await stt_kotoba.transcribe(b"WAVDATA")

    assert result == "hello world"


@pytest.mark.asyncio
async def test_transcribe_empty_audio_returns_empty_silently():
    """空 audio_bytes は silent fail で空文字。"""
    result = await stt_kotoba.transcribe(b"")
    assert result == ""


@pytest.mark.asyncio
async def test_transcribe_http_error_returns_empty():
    """500 等は silent fail で空文字。"""
    mock_session = _make_mock_session(
        status=503,
        text_body="busy",
        content_type="text/plain",
    )

    with patch("pico_agent.adapters.stt_kotoba.aiohttp.ClientSession", return_value=mock_session):
        result = await stt_kotoba.transcribe(b"WAVDATA")

    assert result == ""


@pytest.mark.asyncio
async def test_transcribe_network_exception_returns_empty():
    """aiohttp 接続失敗時は silent fail で空文字 (raise しない)。"""
    with patch(
        "pico_agent.adapters.stt_kotoba.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        result = await stt_kotoba.transcribe(b"WAVDATA")

    assert result == ""


@pytest.mark.asyncio
async def test_transcribe_uses_env_base_url(monkeypatch):
    """STT_BASE_URL が反映される。"""
    captured: dict = {}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, data=None):
            captured["url"] = url
            captured["data"] = data
            resp = MagicMock()
            resp.status = 200
            resp.json = AsyncMock(return_value={"text": "ok"})
            resp.text = AsyncMock(return_value="")
            resp.headers = {"Content-Type": "application/json"}
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

    monkeypatch.setenv("STT_BASE_URL", "http://example.test:9000")
    monkeypatch.setattr(stt_kotoba.aiohttp, "ClientSession", _CapturingSession)

    result = await stt_kotoba.transcribe(b"AUDIO", sample_rate=16000)

    # /transcribe が末尾に自動補完される。
    assert captured["url"] == "http://example.test:9000/transcribe"
    assert result == "ok"


@pytest.mark.asyncio
async def test_transcribe_includes_sample_rate_field(monkeypatch):
    """sample_rate=22050 が FormData の add_field で送られる。"""
    captured_fields: list = []

    class _FakeForm:
        def __init__(self):
            self._fields = captured_fields

        def add_field(self, name, value, **kwargs):
            self._fields.append((name, value, kwargs))

    monkeypatch.setattr(stt_kotoba.aiohttp, "FormData", _FakeForm)

    mock_session = _make_mock_session(
        status=200,
        json_payload={"text": "ok"},
        content_type="application/json",
    )
    with patch("pico_agent.adapters.stt_kotoba.aiohttp.ClientSession", return_value=mock_session):
        await stt_kotoba.transcribe(b"AUDIO", sample_rate=22050)

    sr_fields = [f for f in captured_fields if f[0] == "sample_rate"]
    assert len(sr_fields) == 1
    assert sr_fields[0][1] == "22050"


# ── start_rtsp_subscription() no-op / 起動パスのテスト ────────────────────


@pytest.mark.asyncio
async def test_start_rtsp_subscription_no_url_returns_noop_task():
    """STT_RTSP_URL 未設定・CAMERA_* 未設定なら no-op タスクを返す。"""

    async def _dummy_on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_dummy_on_speech)
    # no-op タスクは即終了する
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_no_ffmpeg_returns_noop_task(monkeypatch):
    """ffmpeg が PATH になければ no-op タスクを返す。"""
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://fake:fake@example/stream1")
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda name: None,
    )

    async def _dummy_on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_dummy_on_speech)
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_no_vad_backend_returns_noop_task(monkeypatch):
    """STT_VAD_BACKEND=disabled で no-op タスクを返す (Phase C-1 の silero 経路置換)。

    Phase C-1 では silero-vad 未インストール → no-op だったが、Phase C-4 で
    ffmpeg silencedetect に切替えたため、対応する 'バックエンド無効化' フラグは
    STT_VAD_BACKEND=disabled になった。
    """
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://fake:fake@example/stream1")
    monkeypatch.setenv("STT_VAD_BACKEND", "disabled")
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )

    async def _dummy_on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_dummy_on_speech)
    await task
    assert task.done()


class _FakeFfmpegProcess:
    """asyncio.create_subprocess_exec の戻り値を fake 化するヘルパ。

    指定された PCM bytes を stdout に流し、stderr に silencedetect 出力を
    流して即 EOF にする。terminate/kill/wait も持つ。
    """

    def __init__(self, pcm_bytes: bytes, stderr_lines: list[bytes]) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(pcm_bytes)
        self.stdout.feed_eof()
        for line in stderr_lines:
            self.stderr.feed_data(line)
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.asyncio
async def test_start_rtsp_subscription_invokes_implementation_when_deps_present(monkeypatch):
    """全依存揃いなら本実装が走り、stub ffmpeg からの 1 発話で on_speech が呼ばれる。

    Phase C-1 の `..._all_deps_present_still_noop` を Phase C-4 用に置き換え。
    """
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://fake:fake@example/stream1")
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba._get_ffmpeg_restart_backoff_sec",
        lambda: 0.01,
    )

    pcm = b"\x00\x01" * 16000  # 1 秒分の dummy PCM
    stderr_lines = [
        b"[silencedetect @ 0x1] silence_start: 1.0\n",
        b"[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 1.0\n",
    ]

    async def _fake_exec(*_args, **_kwargs):
        return _FakeFfmpegProcess(pcm, stderr_lines)

    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    async def _fake_transcribe(_wav_bytes, sample_rate=16000):
        return "おはよう"

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    task = await stt_kotoba.start_rtsp_subscription(_on_speech)
    # 1 ループ分動かしてから cancel して終わる (再起動ループに入る前で)
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert "おはよう" in calls


@pytest.mark.asyncio
async def test_start_rtsp_subscription_returns_task_object(monkeypatch):
    """戻り値は asyncio.Task インスタンス (cancel 可能であること)。"""

    async def _dummy_on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_dummy_on_speech)
    assert isinstance(task, asyncio.Task)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_start_rtsp_subscription_with_explicit_url(monkeypatch):
    """rtsp_url を明示指定したら環境変数より優先される (依存チェックは通る前提)。"""
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda name: None,  # ffmpeg なし → no-op
    )

    async def _dummy_on_speech(text: str) -> None:
        pass

    # rtsp_url 指定で URL チェックは突破、ffmpeg チェックで no-op
    task = await stt_kotoba.start_rtsp_subscription(
        _dummy_on_speech,
        rtsp_url="rtsp://explicit:url@example/stream1",
    )
    await task
    assert task.done()


# ── 外出期間タスク B: STT RTSP 購読スケルトン詳細テスト (Phase C-1) ──────


# 環境変数とヘルパー関数 ──────────────────────────────


def test_get_rtsp_url_from_env(monkeypatch):
    """STT_RTSP_URL から URL を取得できる。"""
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://test:test@host/stream")
    assert stt_kotoba._get_rtsp_url() == "rtsp://test:test@host/stream"


def test_get_rtsp_url_none_when_unset(monkeypatch):
    """STT_RTSP_URL 未設定 + CAMERA_* も未設定なら None。"""
    assert stt_kotoba._get_rtsp_url() is None


def test_get_rtsp_url_none_when_empty_string(monkeypatch):
    """STT_RTSP_URL 空文字 + CAMERA_* も未設定なら None。"""
    monkeypatch.setenv("STT_RTSP_URL", "")
    assert stt_kotoba._get_rtsp_url() is None


def test_get_rtsp_url_whitespace_only_treated_as_none(monkeypatch):
    """STT_RTSP_URL が空白のみ + CAMERA_* 未設定なら None。"""
    monkeypatch.setenv("STT_RTSP_URL", "   \t  ")
    assert stt_kotoba._get_rtsp_url() is None


def test_get_rtsp_url_built_from_camera_env(monkeypatch):
    """STT_RTSP_URL 未設定でも CAMERA_* が揃えば組み立て成功。"""
    monkeypatch.setenv("CAMERA_HOST", "192.168.10.110")
    monkeypatch.setenv("CAMERA_USERNAME", "pico_cam")
    monkeypatch.setenv("CAMERA_PASSWORD", "secret")
    result = stt_kotoba._get_rtsp_url()
    assert result is not None
    assert result.startswith("rtsp://")
    assert "192.168.10.110:554/stream1" in result
    assert "pico_cam" in result


def test_get_rtsp_url_stt_rtsp_url_overrides_camera_env(monkeypatch):
    """STT_RTSP_URL が設定されていれば CAMERA_* を完全無視して優先。"""
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://override@host/path")
    monkeypatch.setenv("CAMERA_HOST", "192.168.10.110")
    monkeypatch.setenv("CAMERA_USERNAME", "u")
    monkeypatch.setenv("CAMERA_PASSWORD", "p")
    assert stt_kotoba._get_rtsp_url() == "rtsp://override@host/path"


def test_get_rtsp_url_partial_camera_env_returns_none(monkeypatch):
    """CAMERA_HOST だけあって USERNAME/PASSWORD が無ければ組み立てない。"""
    monkeypatch.setenv("CAMERA_HOST", "192.168.10.110")
    # USERNAME/PASSWORD は autouse fixture で剥がれている
    assert stt_kotoba._get_rtsp_url() is None


def test_get_rtsp_url_url_encodes_special_chars(monkeypatch):
    """記号入りパスワードが URL-encode される (URL 全体を壊さない)。"""
    monkeypatch.setenv("CAMERA_HOST", "h")
    monkeypatch.setenv("CAMERA_USERNAME", "u")
    monkeypatch.setenv("CAMERA_PASSWORD", "p@ss/word:!")
    result = stt_kotoba._get_rtsp_url()
    assert result is not None
    # @ / : が URL エンコードされていること (生 @ は URL を壊す)
    assert "p%40ss" in result
    assert "%2F" in result


def test_mask_rtsp_url_masks_credentials():
    """パスワード入り URL は ***@host に置換されてログに出る。"""
    masked = stt_kotoba._mask_rtsp_url("rtsp://user:pass@192.168.10.110:554/stream1")
    assert "user:pass" not in masked
    assert "***@192.168.10.110:554/stream1" in masked


def test_mask_rtsp_url_keeps_url_without_creds():
    """認証なし URL はそのまま返す。"""
    masked = stt_kotoba._mask_rtsp_url("rtsp://192.168.10.110/stream1")
    assert masked == "rtsp://192.168.10.110/stream1"


def test_ffmpeg_available_returns_bool(monkeypatch):
    """_ffmpeg_available() は bool を返す。"""
    assert isinstance(stt_kotoba._ffmpeg_available(), bool)


def test_ffmpeg_available_true_when_in_path(monkeypatch):
    """shutil.which が ffmpeg のパスを返したら True。"""
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None,
    )
    assert stt_kotoba._ffmpeg_available() is True


def test_ffmpeg_available_false_when_not_in_path(monkeypatch):
    """shutil.which が None を返したら False。"""
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which", lambda n: None
    )
    assert stt_kotoba._ffmpeg_available() is False


def test_silero_vad_available_returns_bool():
    """_silero_vad_available() は bool を返す (Phase C-4 では VAD backend 可用性のラッパ)。"""
    assert isinstance(stt_kotoba._silero_vad_available(), bool)


def test_vad_backend_available_ffmpeg(monkeypatch):
    """_vad_backend_available('ffmpeg') は _ffmpeg_available と一致。"""
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None,
    )
    assert stt_kotoba._vad_backend_available("ffmpeg") is True
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which", lambda n: None
    )
    assert stt_kotoba._vad_backend_available("ffmpeg") is False


def test_vad_backend_available_unknown_returns_false():
    """未知のバックエンドは False (no-op に倒す)。"""
    assert stt_kotoba._vad_backend_available("imaginary") is False
    assert stt_kotoba._vad_backend_available("disabled") is False


# _noop_subscription_loop 単体 ─────────────────────────


@pytest.mark.asyncio
async def test_noop_subscription_loop_completes_immediately():
    """_noop_subscription_loop は副作用なく即終了する。"""
    await stt_kotoba._noop_subscription_loop("test reason")
    # 例外を投げず、await が即返ること


@pytest.mark.asyncio
async def test_noop_subscription_loop_accepts_arbitrary_reason():
    """reason 引数に任意の文字列を渡せる。"""
    for reason in ("ffmpeg missing", "STT_RTSP_URL not set", "依存欠落", ""):
        await stt_kotoba._noop_subscription_loop(reason)


# start_rtsp_subscription の動作詳細 ──────────────────


@pytest.mark.asyncio
async def test_start_rtsp_subscription_env_url_used_when_no_arg(monkeypatch):
    """rtsp_url 引数省略時、環境変数 STT_RTSP_URL が使われる。"""
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://env:env@host/stream")
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which", lambda n: None
    )  # ffmpeg なし → no-op で抜ける

    async def _on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_on_speech)
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_explicit_url_overrides_env(monkeypatch):
    """rtsp_url 引数が指定されたら環境変数より優先 (実装は URL チェックだけ突破)。"""
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://env_url@host/stream")
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which", lambda n: None
    )

    async def _on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(
        _on_speech, rtsp_url="rtsp://explicit_url@host/stream"
    )
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_kwargs_accepted(monkeypatch):
    """vad_threshold / min_silence_ms / chunk_duration_ms キーワード引数を受け取れる。"""

    async def _on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(
        _on_speech,
        rtsp_url=None,  # 結果として no-op
        vad_threshold=0.7,
        min_silence_ms=800,
        chunk_duration_ms=20,
    )
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_returns_asyncio_task_no_url(monkeypatch):
    """URL 未設定でも返り値は asyncio.Task で、cancel/await が可能。"""

    async def _on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_on_speech)
    assert isinstance(task, asyncio.Task)


@pytest.mark.asyncio
async def test_start_rtsp_subscription_returns_asyncio_task_no_ffmpeg(monkeypatch):
    """ffmpeg なしの no-op パスでも返り値は asyncio.Task。"""
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://fake/stream")
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which", lambda n: None
    )

    async def _on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_on_speech)
    assert isinstance(task, asyncio.Task)


@pytest.mark.asyncio
async def test_start_rtsp_subscription_does_not_call_on_speech_during_noop(monkeypatch):
    """no-op タスクは on_speech callback を呼ばない。"""

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    task = await stt_kotoba.start_rtsp_subscription(_on_speech)
    await task
    assert calls == []


@pytest.mark.asyncio
async def test_start_rtsp_subscription_task_can_be_cancelled_after_completion(monkeypatch):
    """no-op タスクが終了した後でも cancel() は安全に呼べる。"""

    async def _on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_on_speech)
    await task
    # 終了済 task の cancel() は no-op
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_start_rtsp_subscription_concurrent_invocations_each_get_own_task(monkeypatch):
    """複数回呼び出すと、それぞれ別の Task インスタンスが返る。"""

    async def _on_speech(text: str) -> None:
        pass

    task1 = await stt_kotoba.start_rtsp_subscription(_on_speech)
    task2 = await stt_kotoba.start_rtsp_subscription(_on_speech)
    assert task1 is not task2
    await task1
    await task2


@pytest.mark.asyncio
async def test_start_rtsp_subscription_no_url_message(monkeypatch, caplog):
    """URL 未設定時の warning ログに STT-related キーワードが含まれる。"""
    import logging

    from loguru import logger as loguru_logger

    handler_id = loguru_logger.add(
        lambda msg: logging.getLogger("loguru").warning(msg.strip()),
        level="WARNING",
        format="{message}",
    )
    try:
        async def _on_speech(text: str) -> None:
            pass

        with caplog.at_level(logging.WARNING, logger="loguru"):
            task = await stt_kotoba.start_rtsp_subscription(_on_speech)
            await task

        messages = [r.message for r in caplog.records]
        assert any(
            "STT_RTSP_URL" in m or "STT live capture" in m or "CAMERA_" in m
            for m in messages
        ), f"expected STT-related warning, got: {messages}"
    finally:
        loguru_logger.remove(handler_id)


# ── Phase C-4 追加: 内部ヘルパ単体テスト ─────────────────────────────────


# _build_ffmpeg_rtsp_cmd ─────────────────────────────


def test_build_ffmpeg_rtsp_cmd_contains_url():
    """組み立てたコマンドに RTSP URL が入っている。"""
    cmd = stt_kotoba._build_ffmpeg_rtsp_cmd(
        "rtsp://u:p@h/s", noise_db="-30dB", min_silence_sec=0.5
    )
    assert "rtsp://u:p@h/s" in cmd


def test_build_ffmpeg_rtsp_cmd_uses_noise_db_and_min_silence():
    """noise_db と min_silence_sec が silencedetect 引数に反映される。"""
    cmd = stt_kotoba._build_ffmpeg_rtsp_cmd(
        "rtsp://x", noise_db="-25dB", min_silence_sec=0.8
    )
    af_idx = cmd.index("-af")
    af_value = cmd[af_idx + 1]
    assert "noise=-25dB" in af_value
    assert "d=0.8" in af_value


def test_build_ffmpeg_rtsp_cmd_has_two_map_outputs():
    """-map 0:a が 2 回登場 (PCM 出力 + silencedetect 出力)。"""
    cmd = stt_kotoba._build_ffmpeg_rtsp_cmd(
        "rtsp://x", noise_db="-30dB", min_silence_sec=0.5
    )
    map_count = sum(1 for i, v in enumerate(cmd) if v == "-map" and cmd[i + 1] == "0:a")
    assert map_count == 2


def test_build_ffmpeg_rtsp_cmd_uses_loglevel_info():
    """-loglevel info が含まれる (warning だと silencedetect 出力が出ない)。"""
    cmd = stt_kotoba._build_ffmpeg_rtsp_cmd(
        "rtsp://x", noise_db="-30dB", min_silence_sec=0.5
    )
    idx = cmd.index("-loglevel")
    assert cmd[idx + 1] == "info"


def test_build_ffmpeg_rtsp_cmd_uses_tcp_transport():
    """-rtsp_transport tcp が含まれる (UDP より安定)。"""
    cmd = stt_kotoba._build_ffmpeg_rtsp_cmd(
        "rtsp://x", noise_db="-30dB", min_silence_sec=0.5
    )
    idx = cmd.index("-rtsp_transport")
    assert cmd[idx + 1] == "tcp"


# _parse_silencedetect_line ──────────────────────────


def test_parse_silencedetect_line_silence_start():
    """silence_start 行をパースして event='start' を返す。"""
    line = "[silencedetect @ 0x55fdcafebeef] silence_start: 2.345"
    parsed = stt_kotoba._parse_silencedetect_line(line)
    assert parsed is not None
    assert parsed["event"] == "start"
    assert parsed["time"] == pytest.approx(2.345)
    assert parsed["duration"] is None


def test_parse_silencedetect_line_silence_end_with_duration():
    """silence_end + silence_duration をパースして両方取れる。"""
    line = "[silencedetect @ 0x1] silence_end: 5.678 | silence_duration: 3.333"
    parsed = stt_kotoba._parse_silencedetect_line(line)
    assert parsed is not None
    assert parsed["event"] == "end"
    assert parsed["time"] == pytest.approx(5.678)
    assert parsed["duration"] == pytest.approx(3.333)


def test_parse_silencedetect_line_silence_end_without_duration():
    """silence_end のみで duration がない場合は duration=None。"""
    line = "[silencedetect @ 0x1] silence_end: 5.678"
    parsed = stt_kotoba._parse_silencedetect_line(line)
    assert parsed is not None
    assert parsed["event"] == "end"
    assert parsed["duration"] is None


def test_parse_silencedetect_line_non_matching_returns_none():
    """無関係な ffmpeg ログ行は None。"""
    line = "Stream #0:0: Audio: pcm_mulaw, 8000 Hz"
    assert stt_kotoba._parse_silencedetect_line(line) is None


def test_parse_silencedetect_line_empty_string_returns_none():
    """空文字は None。"""
    assert stt_kotoba._parse_silencedetect_line("") is None


def test_parse_silencedetect_line_invalid_time_returns_none():
    """time が数値でない異常パターンは None (silent fail)。"""
    line = "silence_start: oops"
    assert stt_kotoba._parse_silencedetect_line(line) is None


# _wrap_pcm_to_wav ──────────────────────────────────


def test_wrap_pcm_to_wav_empty_pcm():
    """空 PCM でも WAV ヘッダ付きの bytes を返す (RIFF として valid)。"""
    wav = stt_kotoba._wrap_pcm_to_wav(b"")
    assert wav.startswith(b"RIFF")
    assert b"WAVE" in wav[:12]


def test_wrap_pcm_to_wav_16khz_mono_s16():
    """通常の 1 秒 PCM をラップして wave で読み戻せる。"""
    pcm = b"\x00\x01" * 16000  # 1 秒分の 16kHz mono s16
    wav = stt_kotoba._wrap_pcm_to_wav(pcm, sample_rate=16000)
    with wave.open(io.BytesIO(wav), "rb") as rf:
        assert rf.getnchannels() == 1
        assert rf.getsampwidth() == 2
        assert rf.getframerate() == 16000
        assert rf.getnframes() == 16000


def test_wrap_pcm_to_wav_custom_sample_rate():
    """sample_rate=8000 を指定したら header にも反映される。"""
    pcm = b"\x00\x01" * 8000
    wav = stt_kotoba._wrap_pcm_to_wav(pcm, sample_rate=8000)
    with wave.open(io.BytesIO(wav), "rb") as rf:
        assert rf.getframerate() == 8000


# _emit_segment ─────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_segment_drops_too_short_segments(monkeypatch):
    """min_segment_sec 未満は捨てる (whisper も on_speech も呼ばない)。"""
    transcribe_called = False

    async def _fake_transcribe(*_args, **_kwargs):
        nonlocal transcribe_called
        transcribe_called = True
        return "should not happen"

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    # 0.1 秒分しかない (16000 * 2 * 0.1 = 3200 bytes)
    short_buf = bytearray(b"\x00\x01" * 1600)
    await stt_kotoba._emit_segment(short_buf, _on_speech, min_segment_sec=0.3)
    assert transcribe_called is False
    assert calls == []


@pytest.mark.asyncio
async def test_emit_segment_calls_on_speech_with_transcribed_text(monkeypatch):
    """十分長いセグメントは transcribe → on_speech に流れる。"""

    async def _fake_transcribe(_wav_bytes, sample_rate=16000):
        return "こんにちは"

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    # 1 秒分の PCM
    buf = bytearray(b"\x00\x01" * 16000)
    await stt_kotoba._emit_segment(buf, _on_speech, min_segment_sec=0.3)
    assert calls == ["こんにちは"]


@pytest.mark.asyncio
async def test_emit_segment_silent_fail_on_transcribe_exception(monkeypatch):
    """transcribe が例外を投げても raise 伝播せず on_speech も呼ばれない。"""

    async def _fake_transcribe(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    buf = bytearray(b"\x00\x01" * 16000)
    # raise しない
    await stt_kotoba._emit_segment(buf, _on_speech, min_segment_sec=0.3)
    assert calls == []


@pytest.mark.asyncio
async def test_emit_segment_silent_fail_on_callback_exception(monkeypatch):
    """on_speech が例外を投げても raise 伝播しない。"""

    async def _fake_transcribe(*_args, **_kwargs):
        return "テキスト"

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    async def _bad_on_speech(text: str) -> None:
        raise RuntimeError("callback error")

    buf = bytearray(b"\x00\x01" * 16000)
    await stt_kotoba._emit_segment(buf, _bad_on_speech, min_segment_sec=0.3)
    # 例外が伝播しなければ OK


@pytest.mark.asyncio
async def test_emit_segment_skips_empty_transcribe(monkeypatch):
    """transcribe が空文字を返したら on_speech は呼ばれない。"""

    async def _fake_transcribe(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    buf = bytearray(b"\x00\x01" * 16000)
    await stt_kotoba._emit_segment(buf, _on_speech, min_segment_sec=0.3)
    assert calls == []


# env 値の反映 ───────────────────────────────────────


def test_get_vad_noise_db_default():
    """env 未設定なら '-30dB' (default)。"""
    assert stt_kotoba._get_vad_noise_db() == "-30dB"


def test_get_vad_noise_db_from_env(monkeypatch):
    """STT_VAD_NOISE_DB が反映される。"""
    monkeypatch.setenv("STT_VAD_NOISE_DB", "-40dB")
    assert stt_kotoba._get_vad_noise_db() == "-40dB"


def test_get_vad_min_silence_sec_default():
    """env 未設定なら 0.5 (default)。"""
    assert stt_kotoba._get_vad_min_silence_sec() == pytest.approx(0.5)


def test_get_vad_min_silence_sec_from_env(monkeypatch):
    """STT_VAD_MIN_SILENCE_SEC が反映される。"""
    monkeypatch.setenv("STT_VAD_MIN_SILENCE_SEC", "1.25")
    assert stt_kotoba._get_vad_min_silence_sec() == pytest.approx(1.25)


def test_get_vad_min_silence_sec_invalid_falls_back_to_default(monkeypatch):
    """無効値 (非数) はデフォルト 0.5 にフォールバック (silent fail)。"""
    monkeypatch.setenv("STT_VAD_MIN_SILENCE_SEC", "not-a-number")
    assert stt_kotoba._get_vad_min_silence_sec() == pytest.approx(0.5)


def test_get_vad_max_segment_sec_from_env(monkeypatch):
    """STT_VAD_MAX_SEGMENT_SEC が反映される。"""
    monkeypatch.setenv("STT_VAD_MAX_SEGMENT_SEC", "20.0")
    assert stt_kotoba._get_vad_max_segment_sec() == pytest.approx(20.0)


def test_get_vad_min_segment_sec_from_env(monkeypatch):
    """STT_VAD_MIN_SEGMENT_SEC が反映される。"""
    monkeypatch.setenv("STT_VAD_MIN_SEGMENT_SEC", "0.1")
    assert stt_kotoba._get_vad_min_segment_sec() == pytest.approx(0.1)


def test_get_ffmpeg_restart_backoff_sec_from_env(monkeypatch):
    """STT_FFMPEG_RESTART_BACKOFF_SEC が反映される。"""
    monkeypatch.setenv("STT_FFMPEG_RESTART_BACKOFF_SEC", "2.0")
    assert stt_kotoba._get_ffmpeg_restart_backoff_sec() == pytest.approx(2.0)


def test_get_vad_backend_default():
    """env 未設定なら 'ffmpeg'。"""
    assert stt_kotoba._get_vad_backend() == "ffmpeg"


def test_get_vad_backend_from_env(monkeypatch):
    """STT_VAD_BACKEND が反映される。大文字小文字を吸収。"""
    monkeypatch.setenv("STT_VAD_BACKEND", "DISABLED")
    assert stt_kotoba._get_vad_backend() == "disabled"


# _subscription_loop シナリオ (fake ffmpeg で結合) ──────────────────


@pytest.mark.asyncio
async def test_subscription_loop_emits_segment_on_silence_end(monkeypatch):
    """silence_end イベントで PCM バッファが flush され on_speech が呼ばれる。"""
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba._get_ffmpeg_restart_backoff_sec",
        lambda: 100.0,  # 再起動は実質しない
    )

    pcm = b"\x00\x01" * 16000  # 1 秒分
    stderr_lines = [
        b"[silencedetect @ 0x1] silence_start: 1.0\n",
        b"[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 1.0\n",
    ]

    async def _fake_exec(*_args, **_kwargs):
        return _FakeFfmpegProcess(pcm, stderr_lines)

    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    async def _fake_transcribe(_wav, sample_rate=16000):
        return "テキスト1"

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    task = asyncio.create_task(
        stt_kotoba._subscription_loop(
            "rtsp://fake",
            _on_speech,
            noise_db="-30dB",
            min_silence_sec=0.5,
            max_segment_sec=15.0,
            min_segment_sec=0.3,
            restart_backoff_sec=100.0,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert "テキスト1" in calls


@pytest.mark.asyncio
async def test_subscription_loop_force_flush_on_max_segment(monkeypatch):
    """max_segment_sec を超える PCM が来たら、silence なしでも強制 flush。"""
    pcm = b"\x00\x01" * 16000 * 5  # 5 秒分

    async def _fake_exec(*_args, **_kwargs):
        # silencedetect は何も出さない
        return _FakeFfmpegProcess(pcm, [])

    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    async def _fake_transcribe(_wav, sample_rate=16000):
        return "long"

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    task = asyncio.create_task(
        stt_kotoba._run_one_ffmpeg_session(
            "rtsp://fake",
            _on_speech,
            noise_db="-30dB",
            min_silence_sec=0.5,
            max_segment_sec=2.0,  # 2 秒で強制 flush
            min_segment_sec=0.3,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # 5 秒分 → 2 秒 + 2 秒 + 1 秒 (最後は terminate 時の最終 flush)
    # 最低でも 1 回は呼ばれる (強制 flush で 2 秒分が出る)
    assert len(calls) >= 1


@pytest.mark.asyncio
async def test_subscription_loop_drops_too_short_final_segment(monkeypatch):
    """min_segment_sec 未満の最終セグメントは捨てられる。"""
    # 0.1 秒分しかない PCM
    pcm = b"\x00\x01" * 1600

    async def _fake_exec(*_args, **_kwargs):
        return _FakeFfmpegProcess(pcm, [])

    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    async def _fake_transcribe(_wav, sample_rate=16000):
        return "ignored"

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    await stt_kotoba._run_one_ffmpeg_session(
        "rtsp://fake",
        _on_speech,
        noise_db="-30dB",
        min_silence_sec=0.5,
        max_segment_sec=15.0,
        min_segment_sec=0.5,  # 0.1s < 0.5s なので捨てる
    )
    assert calls == []


@pytest.mark.asyncio
async def test_subscription_loop_cancel_terminates_ffmpeg(monkeypatch):
    """cancel が呼ばれたら fake ffmpeg の terminate が走る。"""
    # 終わらない stdout を作る (feed_eof しない)
    proc = _FakeFfmpegProcess(b"", [])
    # stdout/stderr を「絶対 EOF にならない」状態にする
    proc.stdout = asyncio.StreamReader()  # データ無し・EOF 無し → read で待つ
    proc.stderr = asyncio.StreamReader()

    async def _fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    async def _on_speech(text: str) -> None:
        pass

    task = asyncio.create_task(
        stt_kotoba._run_one_ffmpeg_session(
            "rtsp://fake",
            _on_speech,
            noise_db="-30dB",
            min_silence_sec=0.5,
            max_segment_sec=15.0,
            min_segment_sec=0.3,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert proc.terminated is True or proc.killed is True


@pytest.mark.asyncio
async def test_subscription_loop_restarts_on_ffmpeg_eof(monkeypatch):
    """ffmpeg が EOF で終わったら backoff 後に再起動する。"""
    spawn_count = 0

    async def _fake_exec(*_args, **_kwargs):
        nonlocal spawn_count
        spawn_count += 1
        # 即 EOF (PCM も silencedetect も空)
        return _FakeFfmpegProcess(b"", [])

    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    async def _on_speech(text: str) -> None:
        pass

    task = asyncio.create_task(
        stt_kotoba._subscription_loop(
            "rtsp://fake",
            _on_speech,
            noise_db="-30dB",
            min_silence_sec=0.5,
            max_segment_sec=15.0,
            min_segment_sec=0.3,
            restart_backoff_sec=0.01,  # 高速再起動
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert spawn_count >= 2  # 少なくとも 1 回は再起動した
