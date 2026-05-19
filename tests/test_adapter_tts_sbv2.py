"""Tests for pico_agent.adapters.tts_sbv2 (Phase C-1 + C-3)。

Style-BERT-VITS2 公式 API は GET /voice?text=...&model_name=... を返す
WAV bytes を返す前提 (Phase C-3 でカイニットの実機検証により確定) で、
aiohttp HTTP を mock 検証する。

go2rtc 連携は POST /api/streams?dst=...&src=ffmpeg:... 形式 (Phase C-3 確定)。
"""

from __future__ import annotations

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
    """aiohttp.ClientSession の async context を組み立てて返す。"""
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


@pytest.mark.asyncio
async def test_speak_returns_wav_bytes_on_success():
    """正常系: WAV bytes が返る。"""
    mock_session = _make_mock_session(status=200, audio=b"RIFF\x24\x00\x00\x00WAVE")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        audio = await tts_sbv2.speak("こんにちは")

    assert audio == b"RIFF\x24\x00\x00\x00WAVE"
    mock_session.get.assert_called_once()


@pytest.mark.asyncio
async def test_speak_empty_text_returns_empty_bytes():
    """空テキストは silent fail で空 bytes (raise しない)。"""
    audio = await tts_sbv2.speak("")
    assert audio == b""

    audio = await tts_sbv2.speak("   ")
    assert audio == b""


@pytest.mark.asyncio
async def test_speak_http_error_returns_empty_bytes():
    """500 等は silent fail で空 bytes。"""
    mock_session = _make_mock_session(status=500, text_body="server error")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        audio = await tts_sbv2.speak("hello")

    assert audio == b""


@pytest.mark.asyncio
async def test_speak_network_exception_returns_empty_bytes():
    """接続失敗も silent fail。"""
    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        audio = await tts_sbv2.speak("hi")

    assert audio == b""


@pytest.mark.asyncio
async def test_speak_splits_long_text_into_multiple_requests():
    """100 文字超は句読点で分割 → 複数 GET になる。"""
    # 句読点ありで合計 250 文字程度。
    long_text = ("こんにちは。今日は良い天気ですね。" + "ピコは元気です、ええ、本当に元気です。") * 5
    mock_session = _make_mock_session(status=200, audio=b"WAV")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        audio = await tts_sbv2.speak(long_text)

    # 複数チャンクに分かれ、それぞれ b"WAV" が連結される。
    call_count = mock_session.get.call_count
    assert call_count >= 2, f"expected >=2 requests for long text, got {call_count}"
    assert audio == b"WAV" * call_count


@pytest.mark.asyncio
async def test_speak_passes_speaker_id_to_query():
    """speaker_id=3 が URL クエリに含まれる。"""
    captured: dict = {}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
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
        audio = await tts_sbv2.speak("hi", speaker_id=3)

    assert audio == b"WAV"
    assert "speaker_id=3" in captured["url"]
    assert "text=hi" in captured["url"]
    # Phase C-3: model_name=jvnv-F1-jp が URL に含まれる
    assert "model_name=jvnv-F1-jp" in captured["url"]


@pytest.mark.asyncio
async def test_speak_emotion_affects_style_weight():
    """emotion={'valence': 1.0} → style_weight=1.0 (高揚) がクエリに乗る。"""
    captured: dict = {}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
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
        await tts_sbv2.speak("happy!", emotion={"valence": 1.0, "arousal": 0.5})

    assert "style_weight=1.00" in captured["url"]


@pytest.mark.asyncio
async def test_speak_invalid_target_falls_back_silently():
    """不正な target は warning だけ出して discord_vc にフォールバック。"""
    mock_session = _make_mock_session(status=200, audio=b"WAV")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        audio = await tts_sbv2.speak("hi", target="bogus")

    # raise せず WAV を返す。
    assert audio == b"WAV"


# ── v4.2 14-5: play_with_fallback() のテスト ──────────────────────────


@pytest.mark.asyncio
async def test_play_with_fallback_empty_text():
    """空テキストは即 False を返す。"""
    ok, via = await tts_sbv2.play_with_fallback("")
    assert ok is False
    assert via == "empty_text"


@pytest.mark.asyncio
async def test_play_with_fallback_no_audio_from_sbv2():
    """SBV2 が空 bytes を返したら no_audio を返す。"""
    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b""),
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi")
    assert ok is False
    assert via == "no_audio"


@pytest.mark.asyncio
async def test_play_with_fallback_auto_tries_tapo_first():
    """target=auto では tapo_speaker → main_pc → rpi5 の順で試行する。"""
    call_order: list[str] = []

    async def fake_tapo(audio):
        call_order.append("tapo")
        return False  # tapo は失敗

    async def fake_main_pc(audio):
        call_order.append("main_pc")
        return True  # main_pc で成功

    async def fake_rpi5(audio):
        call_order.append("rpi5")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is True
    assert via == "main_pc"
    # 確認: rpi5 までは到達しない (main_pc で成功したので)
    assert call_order == ["tapo", "main_pc"]


@pytest.mark.asyncio
async def test_play_with_fallback_auto_all_failed():
    """全 backend が失敗したら all_failed を返す。"""

    async def always_fail(audio):
        return False

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {
            "tapo_speaker": always_fail,
            "main_pc": always_fail,
            "rpi5": always_fail,
        },
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is False
    assert via == "all_failed"


@pytest.mark.asyncio
async def test_play_with_fallback_explicit_target_no_chain():
    """target を明示指定したらフォールバックなし (その backend のみ試行)。"""
    call_order: list[str] = []

    async def fake_main_pc(audio):
        call_order.append("main_pc")
        return False

    async def fake_rpi5(audio):
        call_order.append("rpi5")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="main_pc")

    # main_pc 単独試行 → 失敗で all_failed
    assert ok is False
    assert via == "all_failed"
    assert call_order == ["main_pc"]
    # rpi5 にはフォールバックしない


@pytest.mark.asyncio
async def test_play_with_fallback_unknown_target_falls_back_to_auto():
    """未知の target は warning 後 auto チェーンにフォールバック。"""
    call_order: list[str] = []

    async def fake_tapo(audio):
        call_order.append("tapo")
        return True

    async def fake_main_pc(audio):
        call_order.append("main_pc")
        return True

    async def fake_rpi5(audio):
        call_order.append("rpi5")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="bogus_target")

    assert ok is True
    assert via == "tapo_speaker"  # auto チェーンで tapo 最初
    assert call_order == ["tapo"]


@pytest.mark.asyncio
async def test_play_with_fallback_backend_raising_exception_skipped():
    """backend が例外を投げても次の backend に移る (silent fail)。"""

    async def fake_tapo(audio):
        raise RuntimeError("simulated backend crash")

    async def fake_main_pc(audio):
        return True

    async def fake_rpi5(audio):
        return False

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is True
    assert via == "main_pc"


@pytest.mark.asyncio
async def test_play_via_go2rtc_skipped_when_disabled(monkeypatch):
    """GO2RTC_ENABLED が設定されていない時、_play_via_go2rtc は何もせず False。"""
    monkeypatch.delenv("GO2RTC_ENABLED", raising=False)
    result = await tts_sbv2._play_via_go2rtc(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_go2rtc_empty_bytes_returns_false():
    """空 bytes は GO2RTC_ENABLED に関わらず False を返す。"""
    result = await tts_sbv2._play_via_go2rtc(b"")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_main_pc_no_player_returns_false(monkeypatch):
    """mpv/ffplay のどちらも PATH になければ False。"""
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", lambda name: None)
    result = await tts_sbv2._play_via_main_pc(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_rpi5_no_player_returns_false(monkeypatch):
    """aplay/paplay のどちらも PATH になければ False。"""
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", lambda name: None)
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2.sys.platform", "linux")
    result = await tts_sbv2._play_via_rpi5(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_rpi5_non_linux_skipped(monkeypatch):
    """非 Linux プラットフォームでは _play_via_rpi5 は即 False。"""
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2.sys.platform", "darwin")
    result = await tts_sbv2._play_via_rpi5(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_main_pc_empty_bytes_returns_false():
    """空 bytes はバックエンド呼び出し前に False を返す。"""
    result = await tts_sbv2._play_via_main_pc(b"")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_rpi5_empty_bytes_returns_false():
    """空 bytes はバックエンド呼び出し前に False を返す。"""
    result = await tts_sbv2._play_via_rpi5(b"")
    assert result is False


# ── 外出期間タスク B: TTS フォールバックチェーン詳細テスト ─────────────


@pytest.mark.asyncio
async def test_play_with_fallback_full_chain_tapo_then_main_pc_then_rpi5():
    """tapo → main_pc → rpi5 の完全フォールバック (rpi5 だけ成功)。"""
    call_order: list[str] = []

    async def fake_tapo(audio):
        call_order.append("tapo")
        return False

    async def fake_main_pc(audio):
        call_order.append("main_pc")
        return False

    async def fake_rpi5(audio):
        call_order.append("rpi5")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is True
    assert via == "rpi5"
    # 順序の厳密検証: tapo → main_pc → rpi5
    assert call_order == ["tapo", "main_pc", "rpi5"]


@pytest.mark.asyncio
async def test_play_with_fallback_tapo_succeeds_no_fallback_attempted():
    """tapo が成功したら、main_pc と rpi5 は呼ばれない。"""
    call_order: list[str] = []

    async def fake_tapo(audio):
        call_order.append("tapo")
        return True

    async def fake_main_pc(audio):
        call_order.append("main_pc")
        return True

    async def fake_rpi5(audio):
        call_order.append("rpi5")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is True
    assert via == "tapo_speaker"
    assert call_order == ["tapo"]


@pytest.mark.asyncio
async def test_play_with_fallback_text_propagated_to_speak():
    """play_with_fallback の text 引数が speak() にそのまま渡る。"""
    speak_mock = AsyncMock(return_value=b"WAV")

    async def fake_backend(audio):
        return True

    with patch("pico_agent.adapters.tts_sbv2.speak", new=speak_mock), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS", {"main_pc": fake_backend}
    ):
        await tts_sbv2.play_with_fallback("こんにちは、ピコだよ", target="main_pc")

    speak_mock.assert_awaited_once()
    # call_args.kwargs に text が含まれる (キーワード渡し)
    assert speak_mock.call_args.kwargs["text"] == "こんにちは、ピコだよ"


@pytest.mark.asyncio
async def test_play_with_fallback_speaker_id_propagated_to_speak():
    """play_with_fallback の speaker_id が speak() にそのまま渡る。"""
    speak_mock = AsyncMock(return_value=b"WAV")

    async def fake_backend(audio):
        return True

    with patch("pico_agent.adapters.tts_sbv2.speak", new=speak_mock), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS", {"main_pc": fake_backend}
    ):
        await tts_sbv2.play_with_fallback("hi", target="main_pc", speaker_id=7)

    assert speak_mock.call_args.kwargs["speaker_id"] == 7


@pytest.mark.asyncio
async def test_play_with_fallback_emotion_propagated_to_speak():
    """play_with_fallback の emotion dict が speak() にそのまま渡る。"""
    speak_mock = AsyncMock(return_value=b"WAV")

    async def fake_backend(audio):
        return True

    emo = {"valence": 0.85, "arousal": 0.3}
    with patch("pico_agent.adapters.tts_sbv2.speak", new=speak_mock), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS", {"main_pc": fake_backend}
    ):
        await tts_sbv2.play_with_fallback("hi", target="main_pc", emotion=emo)

    assert speak_mock.call_args.kwargs["emotion"] == emo


@pytest.mark.asyncio
async def test_play_with_fallback_audio_bytes_passed_to_backend():
    """backend に SBV2 から取得した WAV bytes が正しく渡される。"""
    received: list[bytes] = []

    async def capturing_backend(audio):
        received.append(audio)
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"FAKE_WAV_BYTES_12345"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS", {"main_pc": capturing_backend}
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="main_pc")

    assert ok is True
    assert received == [b"FAKE_WAV_BYTES_12345"]


@pytest.mark.asyncio
async def test_play_with_fallback_whitespace_only_text_returns_empty():
    """空白のみのテキストも empty_text を返す。"""
    ok, via = await tts_sbv2.play_with_fallback("   \n\t  ")
    assert ok is False
    assert via == "empty_text"


@pytest.mark.asyncio
async def test_play_with_fallback_target_none_treated_as_auto():
    """target=None ではなく target='auto' でデフォルト動作 (default 値テスト)。

    現実装の signature は target: str = "auto" なので None は型に違反。
    ただし default 値で呼んだ時に auto と同じ挙動になることを確認。
    """
    call_order: list[str] = []

    async def succ_backend(audio):
        call_order.append("called")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": succ_backend, "main_pc": succ_backend, "rpi5": succ_backend},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi")  # target 省略 = auto

    assert ok is True
    assert via == "tapo_speaker"
    assert call_order == ["called"]


@pytest.mark.parametrize(
    "target_name",
    ["tapo_speaker", "main_pc", "rpi5"],
)
@pytest.mark.asyncio
async def test_play_with_fallback_each_explicit_target(target_name: str):
    """各 target を明示指定したら、その backend だけが呼ばれる。"""
    call_log: list[str] = []

    async def make_backend(name: str):
        async def _b(audio):
            call_log.append(name)
            return True
        return _b

    backends = {
        "tapo_speaker": await make_backend("tapo_speaker"),
        "main_pc": await make_backend("main_pc"),
        "rpi5": await make_backend("rpi5"),
    }

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS", backends
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target=target_name)

    assert ok is True
    assert via == target_name
    # 他の backend は呼ばれない
    assert call_log == [target_name]


@pytest.mark.asyncio
async def test_play_with_fallback_multiple_exceptions_skipped():
    """複数 backend で例外が出ても、最終 backend で成功なら True を返す。"""

    async def fake_tapo(audio):
        raise RuntimeError("tapo error")

    async def fake_main_pc(audio):
        raise ValueError("main_pc error")

    async def fake_rpi5(audio):
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is True
    assert via == "rpi5"


@pytest.mark.asyncio
async def test_play_via_main_pc_uses_mpv_if_available(monkeypatch, tmp_path):
    """mpv が PATH にあれば mpv が選ばれる (ffplay でなく)。"""

    def _which_mpv(name: str):
        return "/usr/bin/mpv" if name == "mpv" else None

    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", _which_mpv)

    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.append(args)
        # subprocess は実行しない、即 mock proc を返す
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", fake_exec
    )
    # tempfile 出力先を tmp_path 指定
    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._write_tmp_wav",
        lambda b: str(tmp_path / "fake.wav"),
    )

    result = await tts_sbv2._play_via_main_pc(b"WAV")
    assert result is True
    # mpv バイナリが選ばれている
    assert captured_args[0][0].endswith("mpv")
    assert "--no-terminal" in captured_args[0]


@pytest.mark.asyncio
async def test_play_via_main_pc_falls_back_to_ffplay(monkeypatch, tmp_path):
    """mpv が無くて ffplay があれば ffplay が選ばれる。"""

    def _which_ffplay(name: str):
        if name == "mpv":
            return None
        if name == "ffplay":
            return "/usr/bin/ffplay"
        return None

    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", _which_ffplay)

    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.append(args)
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._write_tmp_wav",
        lambda b: str(tmp_path / "fake.wav"),
    )

    result = await tts_sbv2._play_via_main_pc(b"WAV")
    assert result is True
    assert captured_args[0][0].endswith("ffplay")
    # ffplay のフラグが選ばれている
    assert "-nodisp" in captured_args[0]
    assert "-autoexit" in captured_args[0]


@pytest.mark.asyncio
async def test_play_via_main_pc_nonzero_exit_returns_false(monkeypatch, tmp_path):
    """subprocess が non-zero exit したら False を返す。"""

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._which", lambda n: "/usr/bin/mpv"
    )

    async def fake_exec(*args, **kwargs):
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc.wait = AsyncMock(return_value=1)  # non-zero
        return proc

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._write_tmp_wav",
        lambda b: str(tmp_path / "fake.wav"),
    )

    result = await tts_sbv2._play_via_main_pc(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_main_pc_exception_returns_false(monkeypatch):
    """subprocess 生成で例外が出ても False を返す (silent fail)。"""

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._which", lambda n: "/usr/bin/mpv"
    )

    async def fake_exec(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", fake_exec
    )

    result = await tts_sbv2._play_via_main_pc(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_rpi5_uses_aplay_first(monkeypatch, tmp_path):
    """aplay が PATH にあれば aplay が選ばれる (paplay より優先)。"""

    monkeypatch.setattr("pico_agent.adapters.tts_sbv2.sys.platform", "linux")

    def _which_aplay(name: str):
        return f"/usr/bin/{name}" if name in ("aplay", "paplay") else None

    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", _which_aplay)

    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.append(args)
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._write_tmp_wav",
        lambda b: str(tmp_path / "fake.wav"),
    )

    result = await tts_sbv2._play_via_rpi5(b"WAV")
    assert result is True
    # aplay が優先
    assert captured_args[0][0].endswith("aplay")


@pytest.mark.asyncio
async def test_play_via_go2rtc_enabled_makes_http_request(monkeypatch, tmp_path):
    """GO2RTC_ENABLED=1 のとき POST /api/streams?dst=...&src=... が実行される (200 OK)。"""
    monkeypatch.setenv("GO2RTC_ENABLED", "1")
    monkeypatch.setenv("GO2RTC_BASE_URL", "http://fake-go2rtc:1984")
    monkeypatch.setenv("TAPO_STREAM_NAME", "pico_test_stream")

    # ffmpeg 前処理をスタブ (実 ffmpeg を呼ばない)
    pre_path = str(tmp_path / "preprocessed.wav")
    (tmp_path / "preprocessed.wav").write_bytes(b"PREPROCESSED")

    async def fake_preprocess(_src):
        return pre_path

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )

    captured: dict = {}

    class _MockResp:
        status = 200

        async def text(self):
            return "ok"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _MockSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, data=None, headers=None):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return _MockResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _MockSession
    )

    result = await tts_sbv2._play_via_go2rtc(b"WAV_BYTES")
    assert result is True
    # POST URL の検証: /api/streams?dst=pico_test_stream&src=<URL-encoded ffmpeg ...>
    assert "fake-go2rtc:1984" in captured["url"]
    assert "/api/streams?" in captured["url"]
    assert "dst=pico_test_stream" in captured["url"]
    assert "src=" in captured["url"]
    # src には ffmpeg:<path>#audio=pcma#input=file が URL エンコードされて入る
    assert "ffmpeg" in captured["url"]


@pytest.mark.asyncio
async def test_play_via_go2rtc_http_error_returns_false(monkeypatch, tmp_path):
    """GO2RTC_ENABLED=1 でも HTTP 4xx/5xx 応答なら False を返す。"""
    monkeypatch.setenv("GO2RTC_ENABLED", "1")

    pre_path = str(tmp_path / "preprocessed.wav")
    (tmp_path / "preprocessed.wav").write_bytes(b"PREPROCESSED")

    async def fake_preprocess(_src):
        return pre_path

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )

    class _MockResp:
        status = 503

        async def text(self):
            return "Service Unavailable"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _MockSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, data=None, headers=None):
            return _MockResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _MockSession
    )

    result = await tts_sbv2._play_via_go2rtc(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_go2rtc_network_exception_returns_false(monkeypatch, tmp_path):
    """ネットワーク例外でも raise せず False を返す (silent fail)。"""
    monkeypatch.setenv("GO2RTC_ENABLED", "1")

    pre_path = str(tmp_path / "preprocessed.wav")
    (tmp_path / "preprocessed.wav").write_bytes(b"PREPROCESSED")

    async def fake_preprocess(_src):
        return pre_path

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )

    class _MockSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, data=None, headers=None):
            raise ConnectionRefusedError("go2rtc not running")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _MockSession
    )

    result = await tts_sbv2._play_via_go2rtc(b"WAV")
    assert result is False


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


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("anything-else", False),
    ],
)
def test_is_go2rtc_enabled_env_parsing(monkeypatch, env_value, expected):
    """GO2RTC_ENABLED の値が真偽値に正しく変換される。"""
    if env_value:
        monkeypatch.setenv("GO2RTC_ENABLED", env_value)
    else:
        monkeypatch.delenv("GO2RTC_ENABLED", raising=False)
    assert tts_sbv2._is_go2rtc_enabled() is expected


# ──────────────────────────────────────────────────────────────────────
# Phase C-3: 新規 23 件テスト
#   分割 (9) + ffmpeg (4) + 暖機 (4) + go2rtc URL (3) + model_name (2) + 旧キー廃止 (2) - 1 = 23
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


@pytest.mark.asyncio
async def test_go2rtc_uses_POST_not_PUT(monkeypatch, tmp_path):
    """_play_via_go2rtc は POST を使う (PUT ではない)。"""
    monkeypatch.setenv("GO2RTC_ENABLED", "1")

    pre_path = str(tmp_path / "pre.wav")
    (tmp_path / "pre.wav").write_bytes(b"PRE")

    async def fake_preprocess(_src):
        return pre_path

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2._preprocess_wav_with_ffmpeg", fake_preprocess
    )

    method_called: list[str] = []

    class _MockResp:
        status = 200

        async def text(self):
            return "ok"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _MockSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, data=None, headers=None):
            method_called.append("post")
            return _MockResp()

        def put(self, url, data=None, headers=None):
            method_called.append("put")
            return _MockResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _MockSession
    )

    result = await tts_sbv2._play_via_go2rtc(b"WAV")
    assert result is True
    assert method_called == ["post"]  # PUT は呼ばれない


# ── model_name 2 件 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_name_query_includes_model_name(monkeypatch):
    """speak() の URL に model_name クエリが必ず含まれる (env override 確認)。"""
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
        await tts_sbv2.speak("ピコ")

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
