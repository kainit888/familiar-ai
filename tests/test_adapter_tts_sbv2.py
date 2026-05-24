"""Tests for pico_agent.adapters.tts_sbv2 (Phase C-5 v5 単一経路版)。

Style-BERT-VITS2 公式 API は GET /voice?text=...&model_name=... を返す
WAV bytes を返す前提 (Phase C-3 でカイニットの実機検証により確定) で、
aiohttp HTTP を mock 検証する。

go2rtc 連携は POST /api/streams?dst=...&src=ffmpeg:... 形式 (Phase C-3 確定)。

v5 (2026-05-24) で ``play_with_fallback`` (tapo → main_pc → rpi5 の 3 段
フォールバック) を撤廃。``speak(text, target="tapo_speaker")`` の単一経路
(go2rtc HTTP API → Tapo C210) に統一。``speak()`` は失敗時無音 (None 返却)、
フォールバックは設けない (設計書 14-5-11 / 14-5-12)。
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pico_agent.adapters import tts_sbv2


@pytest.fixture(autouse=True)
def _reset_warmup_state(monkeypatch, tmp_path):
    """各テストの前後で暖機状態をリセット。

    - モジュール変数 ``_WARMUP_DONE`` を毎テスト True にして、暖機 HTTP 呼び出しが
      副次的に発生しないようにする (個別テストで意図的に False に戻すこともできる)。
    - 暖機ファイル ``_WARMUP_WAV_PATH`` を tmp 配下に向けて、テスト間で実ファイルを
      触らないようにする。
    """
    warmup_path = tmp_path / "warmup.wav"
    warmup_path.write_bytes(b"WARMUP_PRESENT")  # 既存ファイル扱いにする
    monkeypatch.setattr(tts_sbv2, "_WARMUP_WAV_PATH", warmup_path)
    monkeypatch.setattr(tts_sbv2, "_WARMUP_DONE", True)
    yield
    # 終了時もリセット (他テスト汚染防止)
    monkeypatch.setattr(tts_sbv2, "_WARMUP_DONE", False)


def _make_mock_session(status: int = 200, audio: bytes = b"FAKE_WAV_BYTES", text_body: str = ""):
    """aiohttp.ClientSession の async context を組み立てて返す (GET 用)。"""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read = AsyncMock(return_value=audio)
    mock_resp.text = AsyncMock(return_value=text_body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _patch_go2rtc_chain(monkeypatch, tmp_path, post_status: int = 200):
    """tapo_speaker 経路の ffmpeg 前処理と go2rtc POST を mock する。

    Returns:
        captured dict: ``url`` / ``method`` (post/put) / ``post_count`` を記録する。
    """
    pre_path = str(tmp_path / "preprocessed.wav")
    (tmp_path / "preprocessed.wav").write_bytes(b"PREPROCESSED")

    async def fake_preprocess(_src):
        return pre_path

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )

    captured: dict = {"urls": [], "method": None, "post_count": 0}

    class _MockResp:
        status = post_status

        async def text(self):
            return "ok" if post_status < 400 else "error"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _MockSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, data=None, headers=None):
            captured["urls"].append(url)
            captured["method"] = "post"
            captured["post_count"] += 1
            return _MockResp()

        def put(self, url, data=None, headers=None):
            captured["method"] = "put"
            return _MockResp()

        def get(self, url):
            # 暖機が動いた場合に備えて (本テストでは事前に _WARMUP_DONE=True)
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"WAV")
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _MockSession
    )
    return captured


# ── speak() 基本動作 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_speak_returns_none(monkeypatch, tmp_path):
    """正常系: speak() は None を返す (v5 で bytes 返却から変更)。"""
    _patch_go2rtc_chain(monkeypatch, tmp_path)
    result = await tts_sbv2.speak("こんにちは", target="tapo_speaker")
    assert result is None


@pytest.mark.asyncio
async def test_speak_empty_text_returns_silently():
    """空テキストは silent fail で None (raise しない)。"""
    assert await tts_sbv2.speak("") is None
    assert await tts_sbv2.speak("   ") is None


@pytest.mark.asyncio
async def test_speak_http_error_returns_silently(monkeypatch, tmp_path):
    """SBV2 が 500 を返したら silent fail で None (フォールバックなし)。"""
    # ffmpeg は呼ばれない (SBV2 で失敗するので)
    pre_called: list = []

    async def fake_preprocess(_src):
        pre_called.append("called")
        return None

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )
    mock_session = _make_mock_session(status=500, text_body="server error")
    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        result = await tts_sbv2.speak("hello", target="tapo_speaker")
    assert result is None
    assert pre_called == []


@pytest.mark.asyncio
async def test_speak_network_exception_returns_silently():
    """接続失敗も silent fail で None。"""
    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        result = await tts_sbv2.speak("hi", target="tapo_speaker")
    assert result is None


@pytest.mark.asyncio
async def test_speak_splits_long_text_into_multiple_requests(monkeypatch, tmp_path):
    """100 文字超は句読点で分割 → 複数 GET になる。"""
    _patch_go2rtc_chain(monkeypatch, tmp_path)

    long_text = ("こんにちは。今日は良い天気ですね。" + "ピコは元気です、ええ、本当に元気です。") * 5
    mock_session = _make_mock_session(status=200, audio=b"WAV")

    # _post_to_go2rtc は _patch_go2rtc_chain で別 session を使うので、GET 専用に
    # ClientSession を差し替える必要がある (両方の呼び出しを 1 つでカバー)
    captured: dict = {"get_count": 0, "post_count": 0, "post_urls": []}

    class _CountingSession:
        def __init__(self, *_a, **_kw):
            pass

        def get(self, url):
            captured["get_count"] += 1
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"WAV")
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

        def post(self, url, data=None, headers=None):
            captured["post_count"] += 1
            captured["post_urls"].append(url)
            resp = MagicMock()
            resp.status = 200
            resp.text = AsyncMock(return_value="ok")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _CountingSession
    )
    del mock_session  # noqa: F841 - keep for symmetry

    await tts_sbv2.speak(long_text, target="tapo_speaker")
    # 複数チャンクに分かれて GET される。
    assert captured["get_count"] >= 2, f"expected >=2 GET requests, got {captured['get_count']}"
    # 単一の go2rtc POST (連結 WAV を一括送出)
    assert captured["post_count"] == 1


@pytest.mark.asyncio
async def test_speak_passes_speaker_id_to_query(monkeypatch, tmp_path):
    """speaker_id=3 が URL クエリに含まれる。"""
    captured: dict = {"get_url": None, "post_url": None}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, url):
            captured["get_url"] = url
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"WAV")
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

        def post(self, url, data=None, headers=None):
            captured["post_url"] = url
            resp = MagicMock()
            resp.status = 200
            resp.text = AsyncMock(return_value="ok")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

    async def fake_preprocess(_src):
        return str(tmp_path / "pre.wav")

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )
    (tmp_path / "pre.wav").write_bytes(b"PRE")
    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _CapturingSession):
        await tts_sbv2.speak("hi", target="tapo_speaker", speaker_id=3)

    assert captured["get_url"] is not None
    assert "speaker_id=3" in captured["get_url"]
    assert "text=hi" in captured["get_url"]
    # Phase C-3: model_name=jvnv-F1-jp が URL に含まれる
    assert "model_name=jvnv-F1-jp" in captured["get_url"]


@pytest.mark.asyncio
async def test_speak_emotion_affects_style_weight(monkeypatch, tmp_path):
    """emotion={'valence': 1.0} → style_weight=1.0 (高揚) がクエリに乗る。"""
    captured: dict = {"get_url": None}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, url):
            captured["get_url"] = url
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"WAV")
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

        def post(self, url, data=None, headers=None):
            resp = MagicMock()
            resp.status = 200
            resp.text = AsyncMock(return_value="ok")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

    async def fake_preprocess(_src):
        return str(tmp_path / "pre.wav")

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )
    (tmp_path / "pre.wav").write_bytes(b"PRE")
    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _CapturingSession):
        await tts_sbv2.speak("happy!", target="tapo_speaker", emotion={"valence": 1.0, "arousal": 0.5})

    assert captured["get_url"] is not None
    assert "style_weight=1.00" in captured["get_url"]


@pytest.mark.asyncio
async def test_speak_invalid_target_returns_silently_no_post(monkeypatch, tmp_path):
    """不正な target は warning だけ出して silently return (POST しない)。"""
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path)
    result = await tts_sbv2.speak("hi", target="bogus")
    assert result is None
    assert captured["post_count"] == 0


# ── speak() target ごとのスタブ動作 (v5 14-5-11) ────────────────────────


@pytest.mark.asyncio
async def test_speak_discord_vc_is_stub(monkeypatch, tmp_path):
    """target=discord_vc は warning + return (Phase D 未実装、SBV2 GET も POST も不発)。"""
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path)
    get_called: list = []

    def _no_session(*_a, **_kw):
        get_called.append("called")
        raise AssertionError("ClientSession must not be called for discord_vc stub")

    # SBV2 GET 経路も発火しないことを確認
    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", side_effect=_no_session):
        result = await tts_sbv2.speak("hi", target="discord_vc")

    assert result is None
    assert get_called == []
    assert captured["post_count"] == 0


@pytest.mark.asyncio
async def test_speak_obs_audio_is_stub(monkeypatch, tmp_path):
    """target=obs_audio は warning + return (Phase K 未実装)。"""
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path)
    get_called: list = []

    def _no_session(*_a, **_kw):
        get_called.append("called")
        raise AssertionError("ClientSession must not be called for obs_audio stub")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", side_effect=_no_session):
        result = await tts_sbv2.speak("hi", target="obs_audio")

    assert result is None
    assert get_called == []
    assert captured["post_count"] == 0


# ── mutation 検知用 (v5 設計の中核) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_speak_tapo_speaker_uses_http_api_only(monkeypatch, tmp_path):
    """mutation A 検知: tapo_speaker は go2rtc HTTP POST のみで、サブプロセス起動なし。

    検証項目:
        - SBV2 GET と go2rtc POST が呼ばれる
        - posted URL に dst=tapo_c210 が含まれる
        - subprocess.Popen / subprocess.run は呼ばれない (ローカル go2rtc バイナリ起動なし)
        - URL に embodied-claude / .cache が混入しない
    """
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path)

    with patch.object(subprocess, "Popen") as mock_popen, patch.object(
        subprocess, "run"
    ) as mock_run:
        await tts_sbv2.speak("テスト", target="tapo_speaker")

    # go2rtc HTTP POST が 1 回呼ばれた
    assert captured["post_count"] == 1
    assert captured["method"] == "post"
    posted_url = captured["urls"][0]
    assert "dst=tapo_c210" in posted_url
    # ローカル go2rtc バイナリ起動はゼロ
    mock_popen.assert_not_called()
    mock_run.assert_not_called()
    # URL に embodied-claude / .cache の混入なし
    for url in captured["urls"]:
        assert "embodied-claude" not in url
        assert ".cache" not in url


@pytest.mark.asyncio
async def test_speak_failure_returns_silently(monkeypatch):
    """mutation B 検知: SBV2 失敗時に mpv/ffplay/aplay/paplay を呼ばない (フォールバックなし)。"""
    mock_exec = AsyncMock()

    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ), patch.object(subprocess, "Popen") as mock_popen, patch.object(
        subprocess, "run"
    ) as mock_run, patch(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", mock_exec
    ):
        result = await tts_sbv2.speak("テスト", target="tapo_speaker")

    assert result is None
    mock_popen.assert_not_called()
    mock_run.assert_not_called()
    # mpv / ffplay / aplay / paplay へのフォールバック起動はゼロ
    for call in mock_exec.call_args_list:
        bin_name = os.path.basename(call.args[0]) if call.args else ""
        assert bin_name not in ("mpv", "ffplay", "aplay", "paplay"), (
            f"unexpected fallback binary {bin_name!r} invoked"
        )


@pytest.mark.asyncio
async def test_speak_ffmpeg_failure_returns_silently(monkeypatch, tmp_path):
    """ffmpeg 前処理が失敗したら go2rtc POST せず silently return。"""
    # SBV2 は成功
    captured: dict = {"post_count": 0}

    class _MockSession:
        def __init__(self, *_a, **_kw):
            pass

        def get(self, url):
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"WAV")
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

        def post(self, url, data=None, headers=None):
            captured["post_count"] += 1
            resp = MagicMock()
            resp.status = 200
            resp.text = AsyncMock(return_value="ok")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    # ffmpeg 前処理が None を返す (失敗)
    async def fake_preprocess(_src):
        return None

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _MockSession)

    result = await tts_sbv2.speak("テスト", target="tapo_speaker")
    assert result is None
    assert captured["post_count"] == 0  # go2rtc POST は呼ばれない


@pytest.mark.asyncio
async def test_speak_go2rtc_post_failure_returns_silently(monkeypatch, tmp_path):
    """go2rtc POST が 503 を返したら silently return (フォールバックなし)。"""
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path, post_status=503)
    mock_exec = AsyncMock()

    with patch.object(subprocess, "Popen") as mock_popen, patch(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", mock_exec
    ):
        result = await tts_sbv2.speak("テスト", target="tapo_speaker")

    assert result is None
    assert captured["post_count"] == 1  # POST は試行したが 503
    mock_popen.assert_not_called()  # フォールバックでローカル再生はしない


@pytest.mark.asyncio
async def test_speak_no_fallback_to_main_pc_or_rpi5(monkeypatch, tmp_path):
    """SBV2 失敗時に mpv/ffplay/aplay/paplay へのフォールバックは発生しない (v5)。"""
    mock_exec = AsyncMock()

    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=Exception("SBV2 down"),
    ), patch(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", mock_exec
    ):
        await tts_sbv2.speak("テスト", target="tapo_speaker")

    # フォールバックバイナリは一度も呼ばれない
    for call in mock_exec.call_args_list:
        bin_name = os.path.basename(call.args[0]) if call.args else ""
        assert bin_name not in ("mpv", "ffplay", "aplay", "paplay")


@pytest.mark.asyncio
async def test_speak_does_not_spawn_local_go2rtc_binary(monkeypatch, tmp_path):
    """ローカル go2rtc バイナリ起動 (subprocess.Popen / ~/.cache URL 参照) は一切ない。"""
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path)

    with patch.object(subprocess, "Popen") as mock_popen, patch.object(
        subprocess, "run"
    ) as mock_run:
        await tts_sbv2.speak("テスト", target="tapo_speaker")

    mock_popen.assert_not_called()
    mock_run.assert_not_called()
    # 全 URL に embodied-claude / .cache / go2rtc バイナリのパスは含まれない
    for url in captured["urls"]:
        assert "embodied-claude" not in url
        assert ".cache" not in url


# ── ターゲット parametrize (env override 5 件) ───────────────────────────


@pytest.mark.parametrize(
    "base_url_env,stream_env,expected_host,expected_stream",
    [
        ("http://192.168.10.104:1984", "tapo_c210", "192.168.10.104:1984", "tapo_c210"),
        ("http://10.0.0.1:2984", "my_stream", "10.0.0.1:2984", "my_stream"),
        ("http://localhost:1984/", "stream_x", "localhost:1984", "stream_x"),  # 末尾 / は rstrip
        ("http://example.com:9000", "alt_stream", "example.com:9000", "alt_stream"),
        ("http://192.168.10.104:1984", "custom_c210", "192.168.10.104:1984", "custom_c210"),
    ],
)
@pytest.mark.asyncio
async def test_speak_tapo_speaker_env_override(
    monkeypatch, tmp_path, base_url_env, stream_env, expected_host, expected_stream
):
    """GO2RTC_BASE_URL / TAPO_STREAM_NAME の env 上書きが POST URL に反映される。"""
    monkeypatch.setenv("GO2RTC_BASE_URL", base_url_env)
    monkeypatch.setenv("TAPO_STREAM_NAME", stream_env)
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path)

    await tts_sbv2.speak("テスト", target="tapo_speaker")

    assert captured["post_count"] == 1
    posted_url = captured["urls"][0]
    assert expected_host in posted_url
    assert f"dst={expected_stream}" in posted_url


# ── _post_to_go2rtc 単体テスト ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_to_go2rtc_success(monkeypatch, tmp_path):
    """_post_to_go2rtc が POST に成功すれば True を返す。"""
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path)
    result = await tts_sbv2._post_to_go2rtc(str(tmp_path / "pre.wav"))
    assert result is True
    assert captured["method"] == "post"


@pytest.mark.asyncio
async def test_post_to_go2rtc_http_error_returns_false(monkeypatch, tmp_path):
    """HTTP 4xx/5xx 応答なら False を返す。"""
    _patch_go2rtc_chain(monkeypatch, tmp_path, post_status=503)
    result = await tts_sbv2._post_to_go2rtc(str(tmp_path / "pre.wav"))
    assert result is False


@pytest.mark.asyncio
async def test_post_to_go2rtc_network_exception_returns_false(monkeypatch):
    """ネットワーク例外でも raise せず False を返す。"""

    class _MockSession:
        def __init__(self, *_a, **_kw):
            pass

        def post(self, url, data=None, headers=None):
            raise ConnectionRefusedError("go2rtc not running")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _MockSession)
    result = await tts_sbv2._post_to_go2rtc("/tmp/fake.wav")
    assert result is False


@pytest.mark.asyncio
async def test_post_to_go2rtc_uses_POST_not_PUT(monkeypatch, tmp_path):
    """_post_to_go2rtc は POST を使う (PUT ではない)。"""
    captured = _patch_go2rtc_chain(monkeypatch, tmp_path)
    result = await tts_sbv2._post_to_go2rtc(str(tmp_path / "pre.wav"))
    assert result is True
    assert captured["method"] == "post"  # PUT ではない


# ── _fetch_wav_bytes 単体テスト ────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_wav_bytes_returns_concatenated_bytes():
    """_fetch_wav_bytes は SBV2 から取得した WAV bytes を返す。"""
    mock_session = _make_mock_session(status=200, audio=b"RIFF\x00WAVE")
    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        result = await tts_sbv2._fetch_wav_bytes("hi", speaker_id=0, emotion=None)
    assert result == b"RIFF\x00WAVE"


@pytest.mark.asyncio
async def test_fetch_wav_bytes_failure_returns_empty():
    """_fetch_wav_bytes は失敗時に空 bytes を返す (silent fail)。"""
    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        result = await tts_sbv2._fetch_wav_bytes("hi", speaker_id=0, emotion=None)
    assert result == b""


# ──────────────────────────────────────────────────────────────────────
# Phase C-3 (維持): 分割 / ffmpeg / 暖機 / go2rtc URL / model_name / 旧キー
# ──────────────────────────────────────────────────────────────────────

# ── 分割 9 件 ─────────────────────────────────────────────────────────


def test_split_short_passthrough():
    """30 文字未満はそのまま 1 チャンク。"""
    result = tts_sbv2._split_chunks("こんにちは")
    assert result == ["こんにちは"]


def test_split_breaks_on_period():
    """30 文字超かつ「。」があれば「。」位置で切る。"""
    text = "あいうえおかきくけこさしすせそ。たちつてとなにぬねのはひふへほまみむめもやゆよらりるれろ"
    result = tts_sbv2._split_chunks(text)
    # 最初のチャンクは「。」までで終わる
    assert result[0].endswith("。")
    assert result[0] == "あいうえおかきくけこさしすせそ。"


def test_split_falls_back_to_kuten():
    """「。」「！」「？」が無く「、」だけある時は「、」で切る。"""
    text = "あいうえおかきくけこさしすせそたちつてと、なにぬねのはひふへほまみむめもやゆよらりるれろ"
    result = tts_sbv2._split_chunks(text)
    # 最初のチャンクは「、」までで終わる
    assert result[0].endswith("、")


def test_split_force_slice_when_no_delimiter():
    """30 文字を超え区切りが全く無ければ 30 文字で強制スライス。"""
    text = "あ" * 80  # 区切りなし、80 文字
    result = tts_sbv2._split_chunks(text)
    assert len(result) >= 3
    # 強制スライスでチャンクが 30 文字以下になっている
    for c in result:
        assert len(c) <= 30


def test_split_30char_strict():
    """ちょうど 30 文字なら 1 チャンク、31 文字なら分割される。"""
    text_30 = "あ" * 30
    result_30 = tts_sbv2._split_chunks(text_30)
    assert result_30 == [text_30]

    text_31 = "あ" * 31
    result_31 = tts_sbv2._split_chunks(text_31)
    assert len(result_31) >= 2


def test_split_period_over_question():
    """「。」が「？」より優先される (ウィンドウ内に両方ある場合)。"""
    # 30 文字ウィンドウ: "あいうえお？かきくけこさしすせそ。たちつてとなにぬねのはひふ" の手前 30 字
    text = "あいうえお？かきくけこ。さしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろ"
    result = tts_sbv2._split_chunks(text)
    # 「。」で切られているはず (「？」優先ではない)
    assert result[0] == "あいうえお？かきくけこ。"


def test_split_lstrip_after_cut():
    """切った後のチャンクは先頭空白が lstrip される。"""
    text = "短い文。  そのあとに続く長めの文で、しっかり 30 文字超え。"
    result = tts_sbv2._split_chunks(text)
    # 2 個目以降のチャンクは先頭の半角空白が削れている
    for c in result[1:]:
        assert not c.startswith(" ")
        assert not c.startswith("\t")


def test_split_empty_input():
    """空文字列は空リストを返す。"""
    assert tts_sbv2._split_chunks("") == []


def test_split_no_infinite_loop():
    """区切りが先頭にしかない病的入力でも無限ループしない。

    例: 「、」で始まる 100 文字 → 強制スライスで進む。
    """
    text = "、" + "あ" * 99  # 100 文字、先頭だけ「、」
    result = tts_sbv2._split_chunks(text)
    # 必ず有限の結果が返る
    assert len(result) >= 2
    assert len(result) < 100  # 1 文字ずつ進むなどの暴走はない


# ── ffmpeg 4 件 ───────────────────────────────────────────────────────


def test_ffmpeg_build_correct_args(monkeypatch):
    """_build_ffmpeg_args が想定の引数列を返す。"""
    monkeypatch.delenv("TTS_VOLUME", raising=False)
    monkeypatch.delenv("TTS_PRE_RESAMPLE", raising=False)
    monkeypatch.delenv("TTS_TAIL_SILENCE", raising=False)
    args = tts_sbv2._build_ffmpeg_args("/tmp/in.wav", "/tmp/out.wav")
    # 必須フラグの存在確認
    assert args[0] == "ffmpeg"
    assert "-y" in args
    assert "-loglevel" in args
    assert "error" in args
    assert "-i" in args
    assert "/tmp/in.wav" in args
    assert "-af" in args
    # デフォルトの volume=0.5, apad=pad_dur=0.5 が af フィルタに含まれる
    af_idx = args.index("-af")
    af_val = args[af_idx + 1]
    assert "volume=0.5" in af_val
    assert "apad=pad_dur=0.5" in af_val
    assert "-ar" in args
    assert "16000" in args
    assert "-ac" in args
    assert "1" in args
    assert "-f" in args
    assert "wav" in args
    assert "/tmp/out.wav" in args


@pytest.mark.asyncio
async def test_ffmpeg_returns_none_on_nonzero_rc(monkeypatch, tmp_path):
    """_preprocess_wav_with_ffmpeg は rc!=0 のとき None を返す。"""
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", lambda n: "/usr/bin/ffmpeg")

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"some ffmpeg error"))
        return proc

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", fake_exec
    )

    src = str(tmp_path / "src.wav")
    (tmp_path / "src.wav").write_bytes(b"FAKE")
    result = await tts_sbv2._preprocess_wav_with_ffmpeg(src)
    assert result is None


@pytest.mark.asyncio
async def test_ffmpeg_no_ffmpeg_passthrough(monkeypatch, tmp_path):
    """ffmpeg バイナリが PATH に無ければ None。"""
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", lambda n: None)
    src = str(tmp_path / "src.wav")
    (tmp_path / "src.wav").write_bytes(b"FAKE")
    result = await tts_sbv2._preprocess_wav_with_ffmpeg(src)
    assert result is None


def test_ffmpeg_uses_env_overrides(monkeypatch):
    """環境変数で volume / ar / apad が上書きされる。"""
    monkeypatch.setenv("TTS_VOLUME", "0.8")
    monkeypatch.setenv("TTS_PRE_RESAMPLE", "8000")
    monkeypatch.setenv("TTS_TAIL_SILENCE", "1.0")
    args = tts_sbv2._build_ffmpeg_args("/tmp/in.wav", "/tmp/out.wav")
    af_idx = args.index("-af")
    af_val = args[af_idx + 1]
    assert "volume=0.8" in af_val
    assert "apad=pad_dur=1.0" in af_val
    ar_idx = args.index("-ar")
    assert args[ar_idx + 1] == "8000"


# ── 暖機 4 件 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warmup_sets_flag_after_success(monkeypatch, tmp_path):
    """暖機が成功すると _WARMUP_DONE=True になりファイルが書かれる。"""
    warmup_path = tmp_path / "warmup_new.wav"
    monkeypatch.setattr(tts_sbv2, "_WARMUP_WAV_PATH", warmup_path)
    monkeypatch.setattr(tts_sbv2, "_WARMUP_DONE", False)

    # SBV2 が WAV を返すように mock
    mock_session = _make_mock_session(status=200, audio=b"WARMUP_WAV_BYTES")
    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        await tts_sbv2._warmup_once()

    assert tts_sbv2._WARMUP_DONE is True
    assert warmup_path.exists()
    assert warmup_path.read_bytes() == b"WARMUP_WAV_BYTES"


@pytest.mark.asyncio
async def test_warmup_skips_if_file_exists(monkeypatch, tmp_path):
    """暖機ファイルが既存なら SBV2 を呼ばずに _WARMUP_DONE のみ立つ。"""
    warmup_path = tmp_path / "warmup_exists.wav"
    warmup_path.write_bytes(b"EXISTING_WARMUP")
    monkeypatch.setattr(tts_sbv2, "_WARMUP_WAV_PATH", warmup_path)
    monkeypatch.setattr(tts_sbv2, "_WARMUP_DONE", False)

    # ClientSession が一切呼ばれないことを確認するため side_effect を仕込む
    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("ClientSession must not be called when warmup file exists")

    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=_should_not_be_called,
    ):
        await tts_sbv2._warmup_once()

    assert tts_sbv2._WARMUP_DONE is True
    # 既存ファイルは上書きされない
    assert warmup_path.read_bytes() == b"EXISTING_WARMUP"


@pytest.mark.asyncio
async def test_warmup_silent_fail_unreachable(monkeypatch, tmp_path):
    """SBV2 接続不可なら _WARMUP_DONE=False のまま (次回再試行)。"""
    warmup_path = tmp_path / "no_warmup.wav"
    monkeypatch.setattr(tts_sbv2, "_WARMUP_WAV_PATH", warmup_path)
    monkeypatch.setattr(tts_sbv2, "_WARMUP_DONE", False)

    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        await tts_sbv2._warmup_once()  # 例外を投げない

    assert tts_sbv2._WARMUP_DONE is False
    assert not warmup_path.exists()


@pytest.mark.asyncio
async def test_warmup_called_only_once(monkeypatch, tmp_path):
    """_WARMUP_DONE=True ならば 2 回目以降は SBV2 を呼ばない。"""
    warmup_path = tmp_path / "warmup_once.wav"
    monkeypatch.setattr(tts_sbv2, "_WARMUP_WAV_PATH", warmup_path)
    monkeypatch.setattr(tts_sbv2, "_WARMUP_DONE", True)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("Already warmed up; ClientSession must not be called")

    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=_should_not_be_called,
    ):
        await tts_sbv2._warmup_once()
        await tts_sbv2._warmup_once()

    assert tts_sbv2._WARMUP_DONE is True


# ── go2rtc URL 3 件 ──────────────────────────────────────────────────


def test_go2rtc_encodes_src_correctly(monkeypatch):
    """_build_go2rtc_url が ffmpeg: src を URL エンコードする。"""
    monkeypatch.setenv("GO2RTC_BASE_URL", "http://host:1984")
    monkeypatch.setenv("TAPO_STREAM_NAME", "tapo_c210")
    url = tts_sbv2._build_go2rtc_url("/tmp/audio.wav")
    # src には ffmpeg:<path>#audio=pcma#input=file がエンコードされて入る
    assert "dst=tapo_c210" in url
    assert "src=" in url
    # # は %23、: は %3A
    assert "%23audio%3Dpcma" in url
    assert "%23input%3Dfile" in url
    # ffmpeg: の : も %3A
    assert "ffmpeg%3A" in url


def test_go2rtc_uses_env_overrides(monkeypatch):
    """_build_go2rtc_url が env の上書きを反映する。"""
    monkeypatch.setenv("GO2RTC_BASE_URL", "http://192.168.10.104:9999")
    monkeypatch.setenv("TAPO_STREAM_NAME", "custom_stream")
    url = tts_sbv2._build_go2rtc_url("/tmp/in.wav")
    assert url.startswith("http://192.168.10.104:9999/api/streams?")
    assert "dst=custom_stream" in url


def test_go2rtc_url_env_override(monkeypatch):
    """GO2RTC_BASE_URL 環境変数が _get_go2rtc_base_url() に反映される。"""
    monkeypatch.setenv("GO2RTC_BASE_URL", "http://custom-host:5555")
    assert tts_sbv2._get_go2rtc_base_url() == "http://custom-host:5555"


def test_go2rtc_url_default(monkeypatch):
    """GO2RTC_BASE_URL 未設定なら 127.0.0.1:1984 がデフォルト。"""
    monkeypatch.delenv("GO2RTC_BASE_URL", raising=False)
    assert tts_sbv2._get_go2rtc_base_url() == "http://127.0.0.1:1984"


def test_go2rtc_stream_env_override(monkeypatch):
    """TAPO_STREAM_NAME 環境変数が _get_tapo_stream_name() に反映される。"""
    monkeypatch.setenv("TAPO_STREAM_NAME", "my_custom_stream")
    assert tts_sbv2._get_tapo_stream_name() == "my_custom_stream"


def test_go2rtc_stream_default(monkeypatch):
    """TAPO_STREAM_NAME 未設定なら tapo_c210 がデフォルト。"""
    monkeypatch.delenv("TAPO_STREAM_NAME", raising=False)
    assert tts_sbv2._get_tapo_stream_name() == "tapo_c210"


# ── model_name 2 件 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_name_query_includes_model_name(monkeypatch):
    """_fetch_wav_bytes の URL に model_name クエリが必ず含まれる (env override 確認)。"""
    monkeypatch.setenv("TTS_MODEL_NAME", "test_model_xyz")
    captured: dict = {}

    class _CapturingSession:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def get(self, url):
            captured["url"] = url
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"WAV")
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _CapturingSession):
        await tts_sbv2._fetch_wav_bytes("ピコ", speaker_id=0, emotion=None)

    assert "model_name=test_model_xyz" in captured["url"]


def test_model_name_default_jvnv_f1_jp(monkeypatch):
    """TTS_MODEL_NAME 未設定なら jvnv-F1-jp がデフォルト。"""
    monkeypatch.delenv("TTS_MODEL_NAME", raising=False)
    assert tts_sbv2._get_tts_model_name() == "jvnv-F1-jp"


# ── 旧キー廃止 2 件 ──────────────────────────────────────────────────


def test_legacy_GO2RTC_URL_ignored(monkeypatch):
    """旧キー GO2RTC_URL は無視される (新キー GO2RTC_BASE_URL のみ参照)。"""
    monkeypatch.setenv("GO2RTC_URL", "http://legacy-should-be-ignored:9999")
    monkeypatch.delenv("GO2RTC_BASE_URL", raising=False)
    # GO2RTC_URL に値があっても、新キー側のデフォルトが返る
    assert tts_sbv2._get_go2rtc_base_url() == "http://127.0.0.1:1984"


def test_legacy_GO2RTC_STREAM_ignored(monkeypatch):
    """旧キー GO2RTC_STREAM は無視される (新キー TAPO_STREAM_NAME のみ参照)。"""
    monkeypatch.setenv("GO2RTC_STREAM", "legacy_stream_name")
    monkeypatch.delenv("TAPO_STREAM_NAME", raising=False)
    assert tts_sbv2._get_tapo_stream_name() == "tapo_c210"


def test_legacy_GO2RTC_ENABLED_has_no_effect(monkeypatch):
    """旧キー GO2RTC_ENABLED は v5 で完全廃止。env に書いても speak() の挙動に影響しない。

    モジュールから ``_is_go2rtc_enabled`` / ``GO2RTC_ENABLED`` 参照が削除されていることを
    モジュール属性レベルで検証 (mutation 検知)。
    """
    monkeypatch.setenv("GO2RTC_ENABLED", "1")
    monkeypatch.setenv("GO2RTC_ENABLED", "true")
    # モジュール内に _is_go2rtc_enabled 関数 / GO2RTC_ENABLED 定数が存在しない
    assert not hasattr(tts_sbv2, "_is_go2rtc_enabled")


def test_retired_play_with_fallback_removed():
    """v5 で完全削除された旧 API がモジュールに残っていないこと。"""
    assert not hasattr(tts_sbv2, "play_with_fallback")
    assert not hasattr(tts_sbv2, "_play_via_go2rtc")
    assert not hasattr(tts_sbv2, "_play_via_main_pc")
    assert not hasattr(tts_sbv2, "_play_via_rpi5")
    assert not hasattr(tts_sbv2, "_BACKENDS")
    assert not hasattr(tts_sbv2, "_AUTO_FALLBACK_CHAIN")


def test_valid_targets_constant():
    """_VALID_TARGETS は v5 14-5-11 の 3 経路に再構成されている。"""
    assert tts_sbv2._VALID_TARGETS == ("tapo_speaker", "discord_vc", "obs_audio")
    # 旧 target (local_speaker / discord_vc 単独) が含まれないことを念のため
    assert "local_speaker" not in tts_sbv2._VALID_TARGETS
    assert "main_pc" not in tts_sbv2._VALID_TARGETS
    assert "rpi5" not in tts_sbv2._VALID_TARGETS


# ── env override parametrize (件数増 + mutation 検知補強) ─────────────


@pytest.mark.parametrize(
    "env_name,env_value,getter,expected",
    [
        ("TTS_BASE_URL", "http://sbv2-test:5000", "_get_base_url", "http://sbv2-test:5000"),
        ("TTS_BASE_URL", "http://sbv2-test:5000/", "_get_base_url", "http://sbv2-test:5000"),
        ("TTS_TIMEOUT_SEC", "30.5", "_get_timeout", 30.5),
        ("TTS_TIMEOUT_SEC", "not_a_number", "_get_timeout", 60.0),
        ("TTS_VOLUME", "0.7", "_get_tts_volume", 0.7),
        ("TTS_VOLUME", "bad", "_get_tts_volume", 0.5),
        ("TTS_PRE_RESAMPLE", "22050", "_get_tts_pre_resample", 22050),
        ("TTS_PRE_RESAMPLE", "abc", "_get_tts_pre_resample", 16000),
        ("TTS_TAIL_SILENCE", "1.5", "_get_tts_tail_silence", 1.5),
        ("TTS_TAIL_SILENCE", "x", "_get_tts_tail_silence", 0.5),
        ("TTS_CHUNK_MAX_CHARS", "50", "_get_chunk_max_chars", 50),
        ("TTS_CHUNK_MAX_CHARS", "bad", "_get_chunk_max_chars", 30),
        ("TTS_CHUNK_DELAY_MS", "100", "_get_chunk_delay_ms", 100),
        ("TTS_CHUNK_DELAY_MS", "x", "_get_chunk_delay_ms", 0),
        ("TTS_MODEL_NAME", "other_model", "_get_tts_model_name", "other_model"),
    ],
)
def test_env_accessor_override_and_invalid_fallback(monkeypatch, env_name, env_value, getter, expected):
    """環境変数アクセサは値を反映し、不正値はデフォルトにフォールバックする。"""
    monkeypatch.setenv(env_name, env_value)
    fn = getattr(tts_sbv2, getter)
    assert fn() == expected


@pytest.mark.parametrize(
    "valence,expected_weight",
    [
        (1.0, 1.0),
        (0.0, -1.0),
        (0.5, 0.0),
        (0.75, 0.5),
        (0.25, -0.5),
    ],
)
def test_emotion_to_style_weight_mapping(valence, expected_weight):
    """valence 0.0-1.0 → style_weight -1.0〜+1.0 の線形マップ確認。"""
    weight = tts_sbv2._emotion_to_style_weight({"valence": valence})
    assert abs(weight - expected_weight) < 1e-6


def test_emotion_to_style_weight_none_returns_neutral():
    """emotion=None は style_weight=0.0 (中立)。"""
    assert tts_sbv2._emotion_to_style_weight(None) == 0.0


def test_emotion_to_style_weight_invalid_valence_neutral():
    """valence が文字列等の不正型なら中立 (0.0) にフォールバック。"""
    assert tts_sbv2._emotion_to_style_weight({"valence": "bad"}) == 0.0


def test_emotion_to_style_weight_clamped_to_range():
    """valence 範囲外 (1.5 / -0.5) でも style_weight は -1.0〜+1.0 にクランプされる。"""
    assert tts_sbv2._emotion_to_style_weight({"valence": 1.5}) == 1.0
    assert tts_sbv2._emotion_to_style_weight({"valence": -0.5}) == -1.0
