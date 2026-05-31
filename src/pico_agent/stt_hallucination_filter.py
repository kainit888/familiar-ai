"""Whisper STT 幻聴 (hallucination) フィルタ — Pi5 側テキスト層 (Phase C-13)。

Kotoba-Whisper (Whisper Large 系) は **無音 / 環境ノイズ** を入力されると、
学習データ (YouTube 等の動画字幕) に頻出する定型句を高確信度で出力する
「hallucination」を起こす。実機 (2026-05-31 朝、無音放置) で観測された例:

    🎤 ありがとうございました   (len=11, 09:45 周辺で 5 回以上連続)
    🎤 ごめん                  (len=3,  08:45 / 09:31)

これらはカイニットの発話ではなく、無音→定型句変換。VAD 強化
(`STT_VAD_NOISE_DB=-40dB` / `STT_VAD_MIN_SEGMENT_SEC=1.0`) でも頻度は
十分下がらないため、`on_speech()` コールバックへ届く **前** に
テキストレベルで silent-drop する二段目防御を置く。

`whisper_server.py` 側の `no_speech_prob` フィルタは別ワーカ (Cowork) の
担当で、本モジュールは Pi5 (`pico_agent`) 完結のテキスト層。

二層分離 (response_filter / self_model_filter と同型、厳守):
    - `familiar_agent` を一切 import しない (逆 import の片方向性維持)
    - 依存は os / re / unicodedata / loguru のみ
    - 防御的 (None / 空 / トグル無効 は素通し)

公開 I/F:
    is_whisper_hallucination(text: str) -> bool
        True  = 幻聴とみなし破棄
        False = 正当発話とみなし通過

判定順序 (この順番が仕様。allowlist が denylist に優先する):
    0. トグル (`STT_HALLUCINATION_FILTER`) が false → 常に False (素通し)
    1. 空文字 → False
    2. 正規化後が空 → False
    3. allowlist 完全一致 → False (最優先。「ありがとう」「はい」を救う)
    4. 正規化後 1 文字 → True (単独感嘆詞「あ」「う」「え」を破棄)
    5. denylist 完全一致 → True
    6. 短文 (正規化後 5〜15 文字) で次のいずれか → True:
        (i)  入力が denylist フレーズの **先頭一致** (prefix)。Whisper が
             アウトロを途中で打ち切った断片 (例「次の動画で」「ありがとう…」) を捕捉。
        (ii) 入力が **署名長 (≥ _CONTAIN_MIN_PHRASE_LEN 文字) の** denylist フレーズを
             包含する (例「えーっと、ありがとうございました」内の長フレーズ)。
    7. それ以外 → False

設計判断 (Phase C-13、カイニット承認済 + 派生改善):
    - **末尾一致 (suffix) は採用しない**。「〜ありがとうございました」で終わる
      丁寧な実発話を誤 drop しうるため。完全一致 + 短文 (先頭/包含) のみ。
    - **先頭一致 (prefix) は採用する**。suffix と違い「アウトロを途中で言い切った
      断片」を捕捉する低リスク signal で、丁寧な実発話の末尾を巻き込まない。
      副次効果として「ありがとう」が「ありがとうございました」の prefix として
      catch 対象になり、allowlist (rule 3) が初めて load-bearing になる。
    - **包含は署名長フレーズ (≥6 文字) のみ**。短い token (「ごめん」len3) を
      包含マッチに使うと「あ、ごめんね」のような実発話を誤 drop するため、
      短 token は完全一致だけで捕捉する。
    - **echo 検出 (直近 N ターンの同一フレーズ破棄) は見送り**。ステートフルな
      Filter インスタンスが必要になり `stt_kotoba` の各関数へ state を引き回す
      副作用が大きい一方、denylist+allowlist で実機観測値は全カバー済。将来
      誤 drop / すり抜けが顕在化したらここに `WhisperHallucinationFilter`
      クラス (deque で last-N 保持) を足すのが拡張点。
    - **INFO drop カウンタも見送り**。モジュールレベル可変状態は純関数性を壊す。
      drop ログは統合側 (`stt_kotoba._emit_segment`) で DEBUG のみ。
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Final

from loguru import logger  # noqa: F401  (将来の拡張点用に慣習で保持)

# ── denylist: 既知の幻聴フレーズ (生の表記で宣言、比較時に正規化) ─────────────
# 実機観測値 (「ありがとうございました」「ごめん」) を先頭付近に、残りは
# Whisper 日本語 hallucination として GitHub Issues / Reddit / 論文で
# 頻出報告される動画アウトロ定型句。保守性のためモジュール冒頭で集中宣言。
_DENYLIST: Final[tuple[str, ...]] = (
    # ── カイニット実機観測 (2026-05-31) ──
    "ありがとうございました",
    "ごめん",
    # ── 動画アウトロ定型句 (ご視聴系) ──
    "ご視聴ありがとうございました",
    "ご視聴いただきありがとうございました",
    "最後までご視聴いただきありがとうございました",
    "ご清聴ありがとうございました",
    "ありがとうございます",
    # ── お疲れ / 締め ──
    "お疲れさまでした",
    "お疲れ様でした",
    "ごめんなさい",
    # ── 次回予告 ──
    "次の動画でお会いしましょう",
    "またお会いしましょう",
    # ── チャンネル登録系 ──
    "チャンネル登録お願いします",
    "チャンネル登録よろしくお願いします",
    "いいねとチャンネル登録お願いします",
    "高評価とチャンネル登録お願いします",
    # ── 英語混入 (Tapo マイク経路で稀に混じる) ──
    "Thanks for watching",
    "Thank you for watching",
)

# ── allowlist: denylist の誤ヒットから守る正当発話 (完全一致のみ) ──────────
# 部分一致にすると「ありがとうございました」まで通ってしまうため exact only。
# denylist より **優先** (衝突したら通す)。
_ALLOWLIST: Final[tuple[str, ...]] = (
    "ありがとう",
    "ありがとうね",
    "ありがと",
    "はい",
    "うん",
    "ええ",
    "OK",
)

# ── 短文 (先頭/包含) マッチの適用レンジ (正規化後の文字数) ──────────────────
# これより長い文は完全一致のみで判定し、実文に埋まった部分文字列での
# 誤 drop を避ける (例: 長い丁寧文の末尾)。
_SHORT_CONTAIN_MIN: Final[int] = 5
_SHORT_CONTAIN_MAX: Final[int] = 15

# 包含マッチに使う denylist フレーズの最小長。これ未満の短 token
# (「ごめん」len3 等) は完全一致のみで判定し、「あ、ごめんね」のような実発話を
# 包含で誤 drop しない。
_CONTAIN_MIN_PHRASE_LEN: Final[int] = 6

# 正規化時に除去する空白・句読点・記号 (self_model_filter と同一方針)。
_STRIP_FOR_NORM: Final[re.Pattern[str]] = re.compile(
    r"[\s、。，．,.!！?？…「」『』\"'（）()\[\]【】・〜~ー\-—:：;；]+"
)


def _normalize(text: str) -> str:
    """NFKC 正規化 + 空白・句読点除去 + casefold で比較用文字列を作る。

    全半角ゆれ (ＯＫ→OK)、英大小 (OK→ok / Thanks→thanks)、句読点・空白を
    吸収し、denylist / allowlist / 文字数判定の単一の基準にする。
    """
    norm = unicodedata.normalize("NFKC", text)
    norm = _STRIP_FOR_NORM.sub("", norm)
    return norm.casefold()


# import 時に denylist / allowlist を正規化してキャッシュ (毎回正規化しない)。
_DENYLIST_NORM: Final[tuple[str, ...]] = tuple(_normalize(p) for p in _DENYLIST)
_ALLOWLIST_NORM: Final[frozenset[str]] = frozenset(_normalize(p) for p in _ALLOWLIST)


def _get_filter_enabled() -> bool:
    """`STT_HALLUCINATION_FILTER` を読む。既定 ON、false/0/no/off で OFF。

    stt_kotoba._get_* 慣習に倣い、未設定 / 不正値は既定 (True) にフォールバック。
    """
    raw = os.environ.get("STT_HALLUCINATION_FILTER", "").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    return True


def is_whisper_hallucination(text: str) -> bool:
    """text が Whisper 幻聴なら True (破棄)、正当発話なら False (通過)。

    判定順序はモジュール docstring を参照。allowlist 完全一致が最優先で
    denylist より先に評価されるため、「ありがとう」は denylist の
    「ありがとうございました」と衝突しても必ず通過する。
    """
    # 0. トグル無効なら全通し (debug 用)
    if not _get_filter_enabled():
        return False
    # 1. 空文字 (上流 _emit_segment でも弾くが防御的に)
    if not text:
        return False
    norm = _normalize(text)
    # 2. 正規化後が空 (記号 / 空白のみ)
    if not norm:
        return False
    # 3. allowlist 完全一致が最優先 (denylist より先)
    if norm in _ALLOWLIST_NORM:
        return False
    # 4. 単一文字の感嘆詞 (「あ」「う」「え」)。allowlist の 2-3 字は 3 で救済済
    if len(norm) == 1:
        return True
    # 5. denylist 完全一致
    if norm in _DENYLIST_NORM:
        return True
    # 6. 短文のみ: 先頭一致 (prefix) または署名長フレーズの包含 (末尾一致は不採用)
    if _SHORT_CONTAIN_MIN <= len(norm) <= _SHORT_CONTAIN_MAX:
        # (i) アウトロを途中で打ち切った断片 (入力が denylist フレーズの prefix)
        if any(d.startswith(norm) for d in _DENYLIST_NORM):
            return True
        # (ii) 署名長 (≥6 文字) フレーズを包含。短 token は完全一致に委ねる
        if any(
            len(d) >= _CONTAIN_MIN_PHRASE_LEN and d in norm for d in _DENYLIST_NORM
        ):
            return True
    # 7. 正当発話
    return False
