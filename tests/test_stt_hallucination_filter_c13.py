"""Phase C-13 / C-13a: Whisper STT 幻聴フィルタのテスト。

`pico_agent.stt_hallucination_filter` の純ロジックと、
`pico_agent.adapters.stt_kotoba._emit_segment` への統合を検証する。
familiar_agent には依存しない (二層分離の確認も兼ねる)。

mutation 対応 (各テストが捕捉する破壊):
    - denylist を空にする → test_drops_* (denylist 系) が fail
    - C-13a の「いい」を denylist から外す → test_drops_ii が fail
    - C-13a の「はい」を denylist から外す → test_drops_hai が fail
    - allowlist 完全一致優先を削除 → test_passes_arigatou が fail
      (「ありがとう」は denylist「ありがとうございました」の prefix として rule 6 で
       catch されるため、allowlist (rule 3) が唯一の救済経路 = load-bearing)
    - 先頭一致 (prefix) 規則を削除 → test_drops_truncated_outro が fail
    - 単一文字ドロップ規則を削除 → test_drops_single_char が fail
    - 正規化 (空白除去) を削除 → test_drops_with_trailing_whitespace が fail
    - 包含マッチを過度に広げる (長文も対象) → test_passes_real_speech_pico_san が fail
    - 包含の署名長ゲート (≥6 文字) を外す → test_passes_gomen_in_context が fail
    - 「いい」/「はい」を完全一致でなく包含で扱う → test_passes_ii_compounds /
      test_passes_hai_compounds が fail (「いいね」「はいはい」を巻き込む)
    - 統合の幻聴チェックを削除 → test_filter_called_in_emit_segment が fail
    - トグル方向を逆転 / 既定を OFF にする → test_filter_disabled_via_env が fail
    - 統合が正当発話まで落とす → test_normal_text_reaches_on_speech が fail

注: allowlist が decisive (= 削除で結果が変わる) なのは test_passes_arigatou のみ
(「ありがとう」は denylist フレーズの prefix で catch されかけるのを allowlist が
救う)。「うん」「ええ」「OK」は他のどの規則にも掛からないため allowlist が無くても
通過する = 防御的 (将来 denylist 拡張への保険)。「はい」は C-13a で allowlist から
denylist へ移動済 (実機で幻聴頻出)。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pico_agent.adapters import stt_kotoba
from pico_agent.stt_hallucination_filter import is_whisper_hallucination


@pytest.fixture(autouse=True)
def _filter_on(monkeypatch):
    """既定 ON を明示 (.env / 外部環境の汚染から保護)。"""
    monkeypatch.delenv("STT_HALLUCINATION_FILTER", raising=False)


# ── (a) denylist 検出系 ──────────────────────────────────────────────────────


def test_drops_thank_you_for_watching():
    assert is_whisper_hallucination("ご視聴ありがとうございました") is True


def test_drops_short_arigatou():
    # カイニット実機観測値 (len=11、09:45 周辺で 5 回以上)
    assert is_whisper_hallucination("ありがとうございました") is True


def test_drops_with_trailing_whitespace():
    # 正規化 (空白除去) が効かないと完全一致しなくなる
    assert is_whisper_hallucination("ありがとうございました  \n") is True


def test_drops_gomen():
    # カイニット実機観測値 (len=3、08:45 / 09:31)
    assert is_whisper_hallucination("ごめん") is True


def test_drops_english_thanks_for_watching():
    assert is_whisper_hallucination("Thanks for watching") is True


def test_drops_truncated_outro():
    # アウトロを途中で打ち切った断片。「次の動画で」は
    # 「次の動画でお会いしましょう」の prefix (len=5、prefix 規則で捕捉)
    assert is_whisper_hallucination("次の動画で") is True


# ── (b) allowlist 通過系 ─────────────────────────────────────────────────────


def test_passes_arigatou():
    # denylist「ありがとうございました」の前方一致だが allowlist 優先で通過
    assert is_whisper_hallucination("ありがとう") is False


def test_drops_ii():
    # C-13a 実機観測 (len=2、2026-05-31 11:21、無音由来でピコが誤応答)
    assert is_whisper_hallucination("いい") is True


def test_drops_hai():
    # C-13a: 実機で幻聴頻出 (len=2)。allowlist から denylist へ移動
    assert is_whisper_hallucination("はい") is True


def test_passes_ii_compounds():
    # 「いい」完全一致のみ。実発話の複合語は巻き込まない (誤 drop 厳禁)
    assert is_whisper_hallucination("いいね") is False
    assert is_whisper_hallucination("いいよ") is False
    assert is_whisper_hallucination("いいですね") is False


def test_passes_hai_compounds():
    # 「はい」完全一致のみ。複合・長文は通過
    assert is_whisper_hallucination("はいはい") is False
    assert is_whisper_hallucination("はい、そうです") is False


def test_passes_un_still_allowlisted():
    # 「うん」は幻聴観測が無いため allowlist 保持 (C-13a で維持)
    assert is_whisper_hallucination("うん") is False


def test_passes_normal_sentence():
    assert is_whisper_hallucination("ピコ、聞こえてる？") is False


def test_passes_real_speech_pico_san():
    # CRITICAL: 実発話 (len=9)。誤フィルタ厳禁
    assert is_whisper_hallucination("ピコさん聞こえてる") is False


def test_passes_gomen_in_context():
    # 「あ、ごめんね」は実発話。短 token「ごめん」は包含マッチ対象外
    # (署名長 ≥6 ゲート) なので通過する。完全一致の「ごめん」だけ drop。
    assert is_whisper_hallucination("あ、ごめんね") is False


def test_passes_onegaishimasu():
    # 「お願いします」は denylist フレーズ末尾だが、末尾一致は不採用 + prefix でも
    # ないため通過 (実発話の頻出語、誤 drop 厳禁)
    assert is_whisper_hallucination("お願いします") is False


# ── (c) 極短 / 感嘆詞系 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("ch", ["あ", "う", "え"])
def test_drops_single_char(ch):
    assert is_whisper_hallucination(ch) is True


# ── (d) 統合系 (_emit_segment) ──────────────────────────────────────────────


def _pcm_above_min(min_segment_sec: float = 0.3) -> bytearray:
    """min_segment_sec ガードを確実に越える PCM バッファを作る。"""
    n_bytes = int(min_segment_sec * stt_kotoba._PCM_BYTES_PER_SEC) + 2000
    return bytearray(b"\x00" * n_bytes)


@pytest.mark.asyncio
async def test_filter_called_in_emit_segment(monkeypatch):
    """幻聴テキストは on_speech() に到達しない。"""

    async def _fake_transcribe(audio_bytes, sample_rate=16000):
        return "ありがとうございました"

    received: list[str] = []

    async def _on_speech(text: str) -> None:
        received.append(text)

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)
    await stt_kotoba._emit_segment(
        _pcm_above_min(), _on_speech, min_segment_sec=0.3
    )
    assert received == []  # silent drop


@pytest.mark.asyncio
async def test_normal_text_reaches_on_speech(monkeypatch):
    """正当発話は通常どおり on_speech() へ届く (統合が全 drop しない保証)。"""

    async def _fake_transcribe(audio_bytes, sample_rate=16000):
        return "ピコさん聞こえてる"

    received: list[str] = []

    async def _on_speech(text: str) -> None:
        received.append(text)

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)
    await stt_kotoba._emit_segment(
        _pcm_above_min(), _on_speech, min_segment_sec=0.3
    )
    assert received == ["ピコさん聞こえてる"]


@pytest.mark.asyncio
async def test_filter_disabled_via_env(monkeypatch):
    """STT_HALLUCINATION_FILTER=false なら幻聴も素通し。"""
    monkeypatch.setenv("STT_HALLUCINATION_FILTER", "false")
    assert is_whisper_hallucination("ありがとうございました") is False

    # 統合経路でも届く
    async def _fake_transcribe(audio_bytes, sample_rate=16000):
        return "ありがとうございました"

    received: list[str] = []

    async def _on_speech(text: str) -> None:
        received.append(text)

    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)
    await stt_kotoba._emit_segment(
        _pcm_above_min(), _on_speech, min_segment_sec=0.3
    )
    assert received == ["ありがとうございました"]


# ── 通過テキストの DEBUG ログ化 (Phase C-13 後処理) ─────────────────────────────
#
# mutation 対応:
#   - transcribed DEBUG ログを消す → test_transcribed_text_logged_when_passes が fail
#   - drop 経路でも transcribed を出す → test_transcribed_text_not_logged_when_dropped が fail


def _debug_msgs(mock_logger) -> list[str]:
    """MagicMock 化した logger.debug の呼び出しメッセージ (第1引数) を集める。"""
    return [c.args[0] for c in mock_logger.debug.call_args_list if c.args]


async def _run_emit_with_logger(monkeypatch, text: str):
    async def _fake_transcribe(audio_bytes, sample_rate=16000):
        return text

    received: list[str] = []

    async def _on_speech(t: str) -> None:
        received.append(t)

    mock_logger = MagicMock()
    monkeypatch.setattr(stt_kotoba, "transcribe", _fake_transcribe)
    monkeypatch.setattr(stt_kotoba, "logger", mock_logger)
    await stt_kotoba._emit_segment(_pcm_above_min(), _on_speech, min_segment_sec=0.3)
    return mock_logger, received


@pytest.mark.asyncio
async def test_transcribed_text_logged_when_passes(monkeypatch):
    mock_logger, received = await _run_emit_with_logger(monkeypatch, "ピコさん聞こえてる")
    msgs = _debug_msgs(mock_logger)
    assert any("transcribed" in m for m in msgs)
    assert received == ["ピコさん聞こえてる"]  # 通過もしている


@pytest.mark.asyncio
async def test_transcribed_text_not_logged_when_empty(monkeypatch):
    mock_logger, received = await _run_emit_with_logger(monkeypatch, "")
    msgs = _debug_msgs(mock_logger)
    assert not any("transcribed" in m for m in msgs)
    assert received == []


@pytest.mark.asyncio
async def test_transcribed_text_not_logged_when_dropped(monkeypatch):
    # 幻聴は drop ログのみ、transcribed ログは出さない
    mock_logger, received = await _run_emit_with_logger(monkeypatch, "ありがとうございました")
    msgs = _debug_msgs(mock_logger)
    assert any("dropped hallucination" in m for m in msgs)
    assert not any("transcribed" in m for m in msgs)
    assert received == []
