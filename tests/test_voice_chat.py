"""Tests for /home/pico/pico_v3/voice_chat.py.

voice_chat.py はプロジェクトルート (src/ 外) に置かれた単体スクリプトのため、
import 前に sys.path に project root を追加してから import する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import voice_chat  # noqa: E402  (sys.path tweak required first)


# ── TTS chunk env のクリーンアップ (テスト間の独立性確保) ──────────────────
@pytest.fixture(autouse=True)
def _clean_tts_chunk_env(monkeypatch):
    for key in (
        "TTS_CHUNK_MAX_CHARS",
        "TTS_CHUNK_DELAY_MS",
        "TTS_PLAYBACK_STABLE_THRESHOLD",
        "TTS_PLAYBACK_MAX_WAIT_MS",
        "TTS_PLAYBACK_POLL_INTERVAL_MS",
    ):
        monkeypatch.delenv(key, raising=False)


# ── record_from_tapo ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_from_tapo_invokes_ffmpeg_with_expected_args(monkeypatch):
    """ffmpeg が rtsp_transport=tcp、-i URL、-t duration、16kHz mono で呼ばれる。"""
    captured: dict[str, tuple] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"RIFFWAVEDATA", b""))
        proc.returncode = 0
        return proc

    monkeypatch.setattr(voice_chat.asyncio, "create_subprocess_exec", fake_exec)

    out = await voice_chat.record_from_tapo("rtsp://user:pass@192.168.10.110:554/stream1", duration=3.0)

    assert out == b"RIFFWAVEDATA"
    args = captured["args"]
    assert args[0] == "ffmpeg"
    assert "-rtsp_transport" in args
    assert "tcp" in args
    assert "-i" in args
    assert "rtsp://user:pass@192.168.10.110:554/stream1" in args
    assert "-t" in args
    assert "3.0" in args
    assert "-ar" in args
    assert "16000" in args
    assert "-ac" in args
    assert "1" in args


@pytest.mark.asyncio
async def test_record_from_tapo_empty_url_returns_empty():
    out = await voice_chat.record_from_tapo("", duration=5.0)
    assert out == b""


@pytest.mark.asyncio
async def test_record_from_tapo_ffmpeg_nonzero_returns_empty(monkeypatch):
    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"some ffmpeg error"))
        proc.returncode = 1
        return proc

    monkeypatch.setattr(voice_chat.asyncio, "create_subprocess_exec", fake_exec)
    out = await voice_chat.record_from_tapo("rtsp://x", duration=1.0)
    assert out == b""


@pytest.mark.asyncio
async def test_record_from_tapo_handles_ffmpeg_missing(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(voice_chat.asyncio, "create_subprocess_exec", fake_exec)
    out = await voice_chat.record_from_tapo("rtsp://x", duration=1.0)
    assert out == b""


# ── transcribe ──────────────────────────────────────────────────────────────


class _FakeFormData:
    """aiohttp.FormData の最小代替: add_field の name/kw を記録するだけ。"""

    instances: list["_FakeFormData"] = []

    def __init__(self) -> None:
        self.fields: list[dict] = []
        _FakeFormData.instances.append(self)

    def add_field(self, name, value, **kw):
        self.fields.append({"name": name, "value": value, **kw})


def _build_mock_session(status: int = 200, json_payload: dict | None = None, text_body: str = ""):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_payload or {})
    mock_resp.text = AsyncMock(return_value=text_body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_transcribe_posts_audio_field_not_file(monkeypatch):
    """フィールド名は ``audio``。adapter の ``file`` バグを踏まないか検証。"""
    _FakeFormData.instances.clear()
    monkeypatch.setattr(voice_chat.aiohttp, "FormData", _FakeFormData)

    session = _build_mock_session(
        status=200, json_payload={"text": "ピコさんこんにちは"}
    )
    with patch("voice_chat.aiohttp.ClientSession", return_value=session):
        text = await voice_chat.transcribe(b"WAVDATA", "http://example/transcribe")

    assert text == "ピコさんこんにちは"
    assert _FakeFormData.instances, "FormData was not instantiated"
    field_names = [f["name"] for f in _FakeFormData.instances[-1].fields]
    assert "audio" in field_names, f"expected 'audio' field, got {field_names!r}"
    assert "file" not in field_names, "transcribe must not send a 'file' field"


@pytest.mark.asyncio
async def test_transcribe_empty_audio_returns_empty_without_calling_session(monkeypatch):
    called = {"count": 0}

    def fake_session(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("ClientSession should not be created on empty input")

    monkeypatch.setattr(voice_chat.aiohttp, "ClientSession", fake_session)
    result = await voice_chat.transcribe(b"", "http://example/transcribe")
    assert result == ""
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_transcribe_non_200_returns_empty():
    session = _build_mock_session(status=500, text_body="boom")
    with patch("voice_chat.aiohttp.ClientSession", return_value=session):
        text = await voice_chat.transcribe(b"WAVDATA", "http://example/transcribe")
    assert text == ""


@pytest.mark.asyncio
async def test_transcribe_exception_returns_empty(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(voice_chat.aiohttp, "ClientSession", boom)
    text = await voice_chat.transcribe(b"WAVDATA", "http://example/transcribe")
    assert text == ""


# ── speak_to_tapo ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_speak_to_tapo_builds_pcma_pico_url():
    captured: dict[str, str] = {}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value="ok")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    get_resp = MagicMock()
    get_resp.status = 200
    get_resp.json = AsyncMock(return_value={"consumers": []})
    get_resp.__aenter__ = AsyncMock(return_value=get_resp)
    get_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()

    def fake_post(url, *args, **kwargs):
        captured["url"] = url
        return mock_resp

    mock_session.post = MagicMock(side_effect=fake_post)
    mock_session.get = MagicMock(return_value=get_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("voice_chat.aiohttp.ClientSession", return_value=mock_session):
        ok = await voice_chat.speak_to_tapo(
            "こんにちは",
            tts_base_url="http://192.168.10.104:5000",
            tts_model="jvnv-F1-jp",
            go2rtc_base_url="http://192.168.10.104:1984",
            stream_name="tapo_c210",
        )

    assert ok is True
    url = captured["url"]
    assert url.startswith("http://192.168.10.104:1984/api/streams")
    assert "dst=tapo_c210" in url
    decoded = unquote(unquote(url))
    assert "ffmpeg:http://192.168.10.104:5000/voice" in decoded
    assert "#audio=pcma_pico" in decoded
    assert "model_name=jvnv-F1-jp" in decoded
    assert "#input=file" in decoded


@pytest.mark.asyncio
async def test_speak_to_tapo_empty_text_returns_false(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ClientSession should not be created on empty text")

    monkeypatch.setattr(voice_chat.aiohttp, "ClientSession", boom)
    ok = await voice_chat.speak_to_tapo(
        "   ",
        tts_base_url="http://x",
        tts_model="m",
        go2rtc_base_url="http://y",
        stream_name="z",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_speak_to_tapo_missing_config_returns_false(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ClientSession should not be created on missing config")

    monkeypatch.setattr(voice_chat.aiohttp, "ClientSession", boom)
    ok = await voice_chat.speak_to_tapo(
        "hi",
        tts_base_url="",
        tts_model="m",
        go2rtc_base_url="http://y",
        stream_name="z",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_speak_to_tapo_http_error_returns_false():
    session = _build_mock_session(status=502, text_body="bad gateway")
    with patch("voice_chat.aiohttp.ClientSession", return_value=session):
        ok = await voice_chat.speak_to_tapo(
            "hello",
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )
    assert ok is False


# ── チャンク分割ロジック (SBV2 長文 4XX 対策) ───────────────────────────────


def _build_capturing_session(statuses: list[int]):
    """順番に statuses[i] を返す mock session。statuses 不足なら最後の値で埋める。

    GET (`_wait_for_playback_done` 用) は consumers=[] (= 即完了) を返す。
    """
    captured_urls: list[str] = []

    def _make_resp(status: int):
        r = MagicMock()
        r.status = status
        r.text = AsyncMock(return_value="ok" if status < 400 else "err")
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    def _make_get_resp():
        r = MagicMock()
        r.status = 200
        r.json = AsyncMock(return_value={"consumers": []})
        r.text = AsyncMock(return_value="{}")
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    call_count = {"n": 0}

    def fake_post(url, *args, **kwargs):
        captured_urls.append(url)
        idx = call_count["n"]
        call_count["n"] += 1
        status = statuses[idx] if idx < len(statuses) else statuses[-1]
        return _make_resp(status)

    def fake_get(url, *args, **kwargs):
        return _make_get_resp()

    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=fake_post)
    mock_session.get = MagicMock(side_effect=fake_get)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session, captured_urls


@pytest.mark.asyncio
async def test_speak_to_tapo_splits_long_text_into_chunks_by_punctuation():
    """句読点で複数チャンクに分割され、それぞれ POST される。"""
    session, urls = _build_capturing_session([200, 200, 200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session):
        ok = await voice_chat.speak_to_tapo(
            "こんにちは。今日はいい天気ですね。元気ですか？",
            tts_base_url="http://x:5000",
            tts_model="m",
            go2rtc_base_url="http://y:1984",
            stream_name="z",
        )

    assert ok is True
    assert session.post.call_count == 3
    decoded_joined = " | ".join(unquote(unquote(u)) for u in urls)
    assert "こんにちは。" in decoded_joined
    assert "今日はいい天気ですね。" in decoded_joined
    assert "元気ですか？" in decoded_joined


@pytest.mark.asyncio
async def test_speak_to_tapo_short_text_single_chunk():
    """``TTS_CHUNK_MAX_CHARS`` 内の短文は 1 回しか POST されない。"""
    session, _ = _build_capturing_session([200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session):
        ok = await voice_chat.speak_to_tapo(
            "やあ。",
            tts_base_url="http://x:5000",
            tts_model="m",
            go2rtc_base_url="http://y:1984",
            stream_name="z",
        )

    assert ok is True
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_speak_to_tapo_respects_tts_chunk_max_chars(monkeypatch):
    """``TTS_CHUNK_MAX_CHARS=10`` で 50 文字テキストが 5 チャンクに分かれる。"""
    monkeypatch.setenv("TTS_CHUNK_MAX_CHARS", "10")
    session, _ = _build_capturing_session([200] * 5)

    with patch("voice_chat.aiohttp.ClientSession", return_value=session):
        ok = await voice_chat.speak_to_tapo(
            "あ" * 50,
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )

    assert ok is True
    assert session.post.call_count == 5


@pytest.mark.asyncio
async def test_speak_to_tapo_continues_after_chunk_failure():
    """中間チャンクが 500 でも次以降の POST は実行され、戻り値は False。"""
    session, _ = _build_capturing_session([200, 500, 200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session):
        ok = await voice_chat.speak_to_tapo(
            "一つ目。二つ目。三つ目。",
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )

    assert ok is False
    assert session.post.call_count == 3


@pytest.mark.asyncio
async def test_speak_to_tapo_splits_text_without_terminal_punct():
    """句読点が無い長文も ``TTS_CHUNK_MAX_CHARS`` で強制分割される (35 chars / 30 → 2 チャンク)。"""
    session, _ = _build_capturing_session([200, 200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session):
        ok = await voice_chat.speak_to_tapo(
            "あいうえお" * 7,  # 35 chars, no punctuation
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )

    assert ok is True
    assert session.post.call_count == 2


@pytest.mark.asyncio
async def test_split_helper_returns_empty_for_blank_text():
    """``_split_text_for_tts`` 単体テスト: 空 / 空白のみは [] を返す。"""
    assert voice_chat._split_text_for_tts("", max_chars=30) == []
    assert voice_chat._split_text_for_tts("   \n\t", max_chars=30) == []


# ── _extract_consumers 単体テスト ────────────────────────────────────────────


def test_extract_consumers_pattern_direct_dict():
    """`?src=<name>` で単一 stream が返るパターン: payload['consumers'] を直接読む。"""
    payload = {"consumers": [{"id": 1}]}
    result = voice_chat._extract_consumers(payload, "tapo_c210")
    assert result == [{"id": 1}]


def test_extract_consumers_pattern_streams_map():
    """`streams` キー配下に stream_name → consumers がぶら下がる形式。"""
    payload = {"streams": {"s1": {"consumers": [{"id": 1}]}}}
    result = voice_chat._extract_consumers(payload, "s1")
    assert result == [{"id": 1}]


def test_extract_consumers_pattern_top_level_map():
    """payload トップに stream_name → consumers がぶら下がる形式。"""
    payload = {"s1": {"consumers": [{"id": 1}]}}
    result = voice_chat._extract_consumers(payload, "s1")
    assert result == [{"id": 1}]


def test_extract_consumers_missing_returns_none():
    """consumers キーが全く無いペイロードは None を返す。"""
    payload = {"producers": []}
    assert voice_chat._extract_consumers(payload, "s1") is None


def test_extract_consumers_invalid_types_return_none():
    """payload が dict 以外、または consumers が list 以外なら None。"""
    assert voice_chat._extract_consumers("string", "s1") is None
    assert voice_chat._extract_consumers(None, "s1") is None
    assert voice_chat._extract_consumers({"consumers": {"not": "list"}}, "s1") is None
    assert voice_chat._extract_consumers({"streams": "not_dict"}, "s1") is None


# ── _sum_sender_bytes 単体テスト ─────────────────────────────────────────────


def test_sum_sender_bytes_simple():
    consumers = [{"senders": [{"bytes": 100}, {"bytes": 200}]}]
    assert voice_chat._sum_sender_bytes(consumers) == 300


def test_sum_sender_bytes_missing_keys():
    """senders 欠落 / bytes 欠落 / senders が list で無い場合は 0 扱い。"""
    consumers = [
        {},  # senders 無し
        {"senders": [{}]},  # bytes 無し
        {"senders": "not_a_list"},
        {"senders": [{"bytes": 50}]},
    ]
    assert voice_chat._sum_sender_bytes(consumers) == 50


def test_sum_sender_bytes_non_int_bytes():
    """bytes が int 以外なら 0 扱い。"""
    consumers = [
        {"senders": [{"bytes": "100"}, {"bytes": None}, {"bytes": 1.5}, {"bytes": 42}]}
    ]
    assert voice_chat._sum_sender_bytes(consumers) == 42


# ── _wait_for_playback_done テスト ───────────────────────────────────────────


def _build_session_with_get_sequence(get_payloads, get_statuses=None):
    """`session.get` が呼ばれるたび順番に payload/status を返す mock。

    aiohttp の `async with session.get(...)` パターンに合わせて
    MagicMock + AsyncMock の `__aenter__/__aexit__` を構成する。
    """
    if get_statuses is None:
        get_statuses = [200] * len(get_payloads)
    call_count = {"n": 0}

    def fake_get(url, *args, **kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        payload = get_payloads[idx] if idx < len(get_payloads) else get_payloads[-1]
        status = get_statuses[idx] if idx < len(get_statuses) else get_statuses[-1]
        r = MagicMock()
        r.status = status
        r.json = AsyncMock(return_value=payload)
        r.text = AsyncMock(return_value="")
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=fake_get)
    return mock_session, call_count


@pytest.mark.asyncio
async def test_wait_for_playback_done_returns_true_when_consumers_empty(monkeypatch):
    monkeypatch.setattr(voice_chat.asyncio, "sleep", AsyncMock())
    session, _ = _build_session_with_get_sequence([{"consumers": []}])

    done = await voice_chat._wait_for_playback_done(
        session,
        go2rtc_base_url="http://y:1984",
        stream_name="tapo_c210",
        max_wait_ms=5000,
        stable_threshold=3,
        poll_interval_ms=100,
    )

    assert done is True


@pytest.mark.asyncio
async def test_wait_for_playback_done_returns_true_on_stable_bytes(monkeypatch):
    """bytes が 100, 200, 300, 300, 300 (threshold=3) → 5 回目で True。"""
    monkeypatch.setattr(voice_chat.asyncio, "sleep", AsyncMock())
    payloads = [
        {"consumers": [{"senders": [{"bytes": n}]}]} for n in (100, 200, 300, 300, 300)
    ]
    session, calls = _build_session_with_get_sequence(payloads)

    done = await voice_chat._wait_for_playback_done(
        session,
        go2rtc_base_url="http://y:1984",
        stream_name="tapo_c210",
        max_wait_ms=10000,
        stable_threshold=3,
        poll_interval_ms=100,
    )

    assert done is True
    # 5 回目までで判定 (1: 比較対象なし → prev_bytes=100, 2: 200 != 100 reset, 3: 300!=200, 4: 300==300 count=1, 5: 300==300 count=2 → 仕様: prev_bytes 初回スキップなので count は 4 と 5 で 1,2,さらに 6 回目で 3 になる)
    # 厳密には A) consumers empty 経由でも True になり得るが、本テストは bytes 安定の流れを確認
    assert calls["n"] >= 4


@pytest.mark.asyncio
async def test_wait_for_playback_done_resets_stable_count_when_bytes_grow(monkeypatch):
    """100,100 で count=1、200 でリセット、200,200,200 で count=1,2,3 → 6 回目で True。

    リセットが行われない (= mismatch 時に count を 0 に戻さない) regression を
    検知するため、6 回目到達を厳密に検査する。バグ版は iter 5 で count==3 に
    早期到達し ``calls["n"] == 5`` となるため ``== 6`` で確実に分離できる。
    """
    monkeypatch.setattr(voice_chat.asyncio, "sleep", AsyncMock())
    payloads = [
        {"consumers": [{"senders": [{"bytes": n}]}]} for n in (100, 100, 200, 200, 200)
    ]
    # iter 6 のセーフティ: バグ版が暴走した場合に loop を止める consumers 空
    payloads.append({"consumers": []})
    session, calls = _build_session_with_get_sequence(payloads)

    done = await voice_chat._wait_for_playback_done(
        session,
        go2rtc_base_url="http://y:1984",
        stream_name="tapo_c210",
        max_wait_ms=10000,
        stable_threshold=3,
        poll_interval_ms=100,
    )

    assert done is True
    assert calls["n"] == 6


@pytest.mark.asyncio
async def test_wait_for_playback_done_times_out(monkeypatch):
    """毎回 bytes が増え続け、max_wait_ms=300 / poll=100 で時間切れ。"""
    monkeypatch.setattr(voice_chat.asyncio, "sleep", AsyncMock())

    fake_clock = {"t": 1000.0}

    def fake_monotonic():
        # 呼ばれるたび 0.1 秒進める (poll_interval_ms=100 に揃える)
        fake_clock["t"] += 0.1
        return fake_clock["t"]

    monkeypatch.setattr(voice_chat.time, "monotonic", fake_monotonic)

    payloads = [
        {"consumers": [{"senders": [{"bytes": n}]}]} for n in range(100, 100000, 100)
    ]
    session, _ = _build_session_with_get_sequence(payloads)

    done = await voice_chat._wait_for_playback_done(
        session,
        go2rtc_base_url="http://y:1984",
        stream_name="tapo_c210",
        max_wait_ms=300,
        stable_threshold=3,
        poll_interval_ms=100,
    )

    assert done is False


@pytest.mark.asyncio
async def test_wait_for_playback_done_http_error_returns_true(monkeypatch):
    """GET 500 は「待たない」ので True。"""
    monkeypatch.setattr(voice_chat.asyncio, "sleep", AsyncMock())
    session, _ = _build_session_with_get_sequence([{}], get_statuses=[500])

    done = await voice_chat._wait_for_playback_done(
        session,
        go2rtc_base_url="http://y:1984",
        stream_name="tapo_c210",
        max_wait_ms=5000,
        stable_threshold=3,
        poll_interval_ms=100,
    )

    assert done is True


@pytest.mark.asyncio
async def test_wait_for_playback_done_request_exception_returns_true(monkeypatch):
    """session.get 自体が例外 → 待たない (True)。"""
    monkeypatch.setattr(voice_chat.asyncio, "sleep", AsyncMock())

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=boom)

    done = await voice_chat._wait_for_playback_done(
        mock_session,
        go2rtc_base_url="http://y:1984",
        stream_name="tapo_c210",
        max_wait_ms=5000,
        stable_threshold=3,
        poll_interval_ms=100,
    )

    assert done is True


@pytest.mark.asyncio
async def test_wait_for_playback_done_polls_correct_url(monkeypatch):
    """GET URL に /api/streams と ?src=tapo_c210 (URL-encoded) が含まれる。"""
    monkeypatch.setattr(voice_chat.asyncio, "sleep", AsyncMock())
    captured: dict[str, str] = {}

    def fake_get(url, *args, **kwargs):
        captured["url"] = url
        r = MagicMock()
        r.status = 200
        r.json = AsyncMock(return_value={"consumers": []})
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=fake_get)

    await voice_chat._wait_for_playback_done(
        mock_session,
        go2rtc_base_url="http://y:1984",
        stream_name="tapo_c210",
        max_wait_ms=5000,
        stable_threshold=3,
        poll_interval_ms=100,
    )

    assert "/api/streams" in captured["url"]
    assert "src=tapo_c210" in captured["url"]


# ── speak_to_tapo × _wait_for_playback_done 統合テスト ──────────────────────


@pytest.mark.asyncio
async def test_speak_to_tapo_calls_wait_for_each_chunk():
    """3 チャンク全成功 → wait は 3 回呼ばれる。"""
    session, _ = _build_capturing_session([200, 200, 200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session), patch(
        "voice_chat._wait_for_playback_done", new=AsyncMock(return_value=True)
    ) as wait_mock:
        ok = await voice_chat.speak_to_tapo(
            "一つ目。二つ目。三つ目。",
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y:1984",
            stream_name="tapo_c210",
        )

    assert ok is True
    assert wait_mock.await_count == 3


@pytest.mark.asyncio
async def test_speak_to_tapo_calls_wait_after_last_chunk():
    """単独チャンク (最後 = 唯一) でも wait は呼ばれる。"""
    session, _ = _build_capturing_session([200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session), patch(
        "voice_chat._wait_for_playback_done", new=AsyncMock(return_value=True)
    ) as wait_mock:
        ok = await voice_chat.speak_to_tapo(
            "やあ。",
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )

    assert ok is True
    assert wait_mock.await_count == 1


@pytest.mark.asyncio
async def test_speak_to_tapo_skips_wait_when_chunk_post_fails():
    """成功 + 失敗 + 成功 = 3 チャンクでも wait は成功分の 2 回のみ。"""
    session, _ = _build_capturing_session([200, 500, 200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session), patch(
        "voice_chat._wait_for_playback_done", new=AsyncMock(return_value=True)
    ) as wait_mock:
        ok = await voice_chat.speak_to_tapo(
            "一つ目。二つ目。三つ目。",
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )

    assert ok is False  # 1 チャンク失敗
    assert wait_mock.await_count == 2


@pytest.mark.asyncio
async def test_speak_to_tapo_wait_timeout_does_not_fail_function():
    """wait が False (タイムアウト) を返しても POST 全成功なら戻り値は True。"""
    session, _ = _build_capturing_session([200, 200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session), patch(
        "voice_chat._wait_for_playback_done", new=AsyncMock(return_value=False)
    ):
        ok = await voice_chat.speak_to_tapo(
            "一つ目。二つ目。",
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )

    assert ok is True


@pytest.mark.asyncio
async def test_speak_to_tapo_respects_tts_playback_max_wait_ms_env(monkeypatch):
    monkeypatch.setenv("TTS_PLAYBACK_MAX_WAIT_MS", "2000")
    session, _ = _build_capturing_session([200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session), patch(
        "voice_chat._wait_for_playback_done", new=AsyncMock(return_value=True)
    ) as wait_mock:
        await voice_chat.speak_to_tapo(
            "やあ。",
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )

    assert wait_mock.await_count == 1
    _, kwargs = wait_mock.await_args
    assert kwargs["max_wait_ms"] == 2000


@pytest.mark.asyncio
async def test_speak_to_tapo_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TTS_PLAYBACK_MAX_WAIT_MS", "abc")
    session, _ = _build_capturing_session([200])

    with patch("voice_chat.aiohttp.ClientSession", return_value=session), patch(
        "voice_chat._wait_for_playback_done", new=AsyncMock(return_value=True)
    ) as wait_mock:
        await voice_chat.speak_to_tapo(
            "やあ。",
            tts_base_url="http://x",
            tts_model="m",
            go2rtc_base_url="http://y",
            stream_name="z",
        )

    _, kwargs = wait_mock.await_args
    assert kwargs["max_wait_ms"] == voice_chat.DEFAULT_TTS_PLAYBACK_MAX_WAIT_MS
    assert kwargs["max_wait_ms"] == 10000
