# Phase C-3: tts_sbv2 本実装移行ノート

**実装日:** 2026-05-19
**対象:** `/home/pico/pico_v3/src/pico_agent/adapters/tts_sbv2.py`
**根拠:** カイニットの実機検証 (Tapo C210 + go2rtc 1.9 + SBV2 メイン PC)
**計画書:** planner agentId `a850eb289004278b8`

---

## 1. 何が変わったか (Phase C-1 → C-3 差分)

### 1-1. SBV2 リクエストパラメータ

| 項目 | Phase C-1 (旧) | Phase C-3 (確定) |
|---|---|---|
| モデル指定 | `model_id=0` クエリ | `model_name={TTS_MODEL_NAME}` クエリ |
| デフォルト model | (なし、id=0 のみ) | `jvnv-F1-jp` (env で上書き可) |

実機の SBV2 サーバが `model_name` で受ける仕様であることを確認。`model_id` クエリは無視される。

### 1-2. go2rtc 呼び出し方式

| 項目 | Phase C-1 (旧) | Phase C-3 (確定) |
|---|---|---|
| メソッド | `PUT /api/streams/{stream}/audio` | `POST /api/streams?dst=...&src=...` |
| Body | WAV バイナリ (Content-Type: audio/wav) | 空 |
| src 指定 | (Body 内) | `ffmpeg:<wav_path>#audio=pcma#input=file` を URL エンコード |
| 認証 | なし | なし (LAN 内、go2rtc 1.9 で確認) |
| 前処理 | なし | RPi5 ホスト側で ffmpeg 経由 (volume / 16kHz / mono / apad) |

go2rtc 公式 API は `POST /api/streams?dst=X&src=Y` で「Y を入力源として X に流す」セマンティクスを持つ。
src に `ffmpeg:` プレフィクスを付けることで go2rtc 内蔵の ffmpeg ラッパに渡せる。

### 1-3. テキスト分割アルゴリズム

| 項目 | Phase C-1 (旧) | Phase C-3 (確定) |
|---|---|---|
| 最大文字数 | 100 文字 | **30 文字** (`TTS_CHUNK_MAX_CHARS`) |
| 区切り優先度 | 正規表現で `[。、！？!?,.\n]` 全部 | `。` → `！` → `？` → `、` → `\n` の優先順 |
| アルゴリズム | 区切りで `split` → バッファに詰める | 30 字ウィンドウで `rfind`、見つかった区切りで切る |
| 強制スライス | あり (100 字超のとき) | あり (区切り無しのとき 30 字で強制) |

30 文字制限は SBV2 のレイテンシ最適化 + Discord VC で先頭から再生するため。

### 1-4. 暖機 (新規追加)

Phase C-1 にはなかったが、SBV2 の初回呼び出しは TTS モデルローディングで 5-10 秒かかる。
本実装ではプロセス起動後 1 回だけ「ん」1 文字を投げて `/tmp/pico_v3_warmup.wav` に保存する。

- モジュール変数 `_WARMUP_DONE: bool` で多重実行防止
- ファイル既存ならスキップ (再起動後の高速化)
- 失敗時は silent fail (logger.warning)、フラグは立てず次回再試行

### 1-5. ffmpeg 前処理 (新規追加)

go2rtc 経路でのみ実行する。Tapo C210 内蔵スピーカーは PCMA 16kHz mono を期待。

```
ffmpeg -y -loglevel error -i <src.wav> \
  -af "volume=0.5,apad=pad_dur=0.5" \
  -ar 16000 -ac 1 -f wav <dst.wav>
```

- ffmpeg は RPi5 ホストの `/usr/bin/ffmpeg` (v7.1.3 確認済み) を使う
- `asyncio.create_subprocess_exec` でブロッキング回避
- 失敗時は silent fail (None 返却) → go2rtc 呼び出しを諦めてフォールバックチェーンへ

---

## 2. 環境変数の追加・廃止

### 2-1. 新規 (追加)

| キー | 型 | デフォルト | 用途 |
|---|---|---|---|
| `GO2RTC_BASE_URL` | str | `http://127.0.0.1:1984` | go2rtc REST API ベース URL |
| `TAPO_STREAM_NAME` | str | `tapo_c210` | go2rtc.yaml の streams キー |
| `TTS_VOLUME` | float | `0.5` | ffmpeg volume フィルタ係数 |
| `TTS_PRE_RESAMPLE` | int | `16000` | ffmpeg `-ar` 再サンプリング rate |
| `TTS_TAIL_SILENCE` | float | `0.5` | ffmpeg apad pad_dur 秒 |
| `TTS_CHUNK_MAX_CHARS` | int | `30` | テキスト分割上限文字数 |
| `TTS_CHUNK_DELAY_MS` | int | `0` | チャンク間のディレイ ms |
| `TTS_MODEL_NAME` | str | `jvnv-F1-jp` | SBV2 model_name クエリ |

### 2-2. 廃止 (削除済み)

| 旧キー | 移行先 |
|---|---|
| `GO2RTC_URL` | `GO2RTC_BASE_URL` |
| `GO2RTC_STREAM` | `TAPO_STREAM_NAME` |

旧キーを env に書いても **無視される** (新キー側のデフォルトが返る)。テストでも検証済み:
- `test_legacy_GO2RTC_URL_ignored`
- `test_legacy_GO2RTC_STREAM_ignored`

### 2-3. 保持 (変更なし)

| キー | デフォルト |
|---|---|
| `TTS_BASE_URL` | `http://192.168.10.104:5000` |
| `TTS_TIMEOUT_SEC` | `60.0` |
| `GO2RTC_ENABLED` | `""` (空 = 無効、`1`/`true`/`yes` で有効) |

---

## 3. 公開 API シグネチャ (変更なし)

Phase C-1 から **シグネチャは一切変更していない**。
`voice_channel.tts_callable=speak` 互換を維持。

```python
async def speak(
    text: str,
    speaker_id: int = 0,
    emotion: dict[str, Any] | None = None,
    target: str = "discord_vc",
) -> bytes: ...

async def play_with_fallback(
    text: str,
    target: str = "auto",  # "tapo_speaker" | "main_pc" | "rpi5" | "auto"
    speaker_id: int = 0,
    emotion: dict[str, Any] | None = None,
) -> tuple[bool, str]: ...
```

フォールバックチェーン: `tapo_speaker → main_pc → rpi5` (順序維持)。
`_play_via_main_pc` / `_play_via_rpi5` は Phase C-1 から変更なし。

---

## 4. テスト件数の推移

- baseline (Phase C-1 後): `tests/test_adapter_tts_sbv2.py` 53 件
- Phase C-3 完了後: **77 件** (+24)

新規テストカテゴリ:
- 分割: 9 件 (passthrough / breaks_on_period / falls_back_to_kuten / force_slice / 30char_strict / period_over_question / lstrip_after_cut / empty_input / no_infinite_loop)
- ffmpeg: 4 件 (build_correct_args / returns_none_on_nonzero_rc / no_ffmpeg_passthrough / uses_env_overrides)
- 暖機: 4 件 (sets_flag_after_success / skips_if_file_exists / silent_fail_unreachable / called_only_once)
- go2rtc URL: 3 件 (encodes_src_correctly / uses_env_overrides / uses_POST_not_PUT)
- model_name: 2 件 (query_includes_model_name / default_jvnv-F1-jp)
- 旧キー廃止: 2 件 (GO2RTC_URL_ignored / GO2RTC_STREAM_ignored)

加えて既存 6 件のテストを本実装に合わせて修正:
- `test_speak_passes_speaker_id_to_query`: `model_name` クエリ検証を追加
- `test_play_via_go2rtc_enabled_makes_http_request`: PUT → POST、URL 形式変更
- `test_play_via_go2rtc_http_error_returns_false`: POST に変更
- `test_play_via_go2rtc_network_exception_returns_false`: POST に変更
- `test_go2rtc_url_*` / `test_go2rtc_stream_*`: 新キー名 + 新デフォルト値

`autouse` fixture `_reset_warmup_state` を追加し、テストごとに `_WARMUP_DONE=True` & 偽 warmup ファイルを置くことで、テスト間の状態汚染と意図しない SBV2 呼び出しを防止している。

総件数: 1170 → **1217** (+47、グリーン維持)。

---

## 5. ロールバック手順 (緊急時)

```bash
cd /home/pico/pico_v3
git checkout HEAD -- src/pico_agent/adapters/tts_sbv2.py
git checkout HEAD -- tests/test_adapter_tts_sbv2.py
git checkout HEAD -- .env.example
git checkout HEAD -- docs/test_baseline.md
rm -f docs/phase_c3_migration_notes.md
rm -f /tmp/pico_v3_warmup.wav
uv run pytest -q --tb=no
```

旧仕様 (PUT / model_id / 100 文字分割) に戻る。

---

## 6. 未対応事項 (Phase D 以降に持ち越し)

- **systemd 再起動チェック**: Phase C-3 では skip (カイニット確定)。Phase D の Discord 統合で `pico_v3.service` 起動と組み合わせて検証する。
- **実機 go2rtc 経路の E2E テスト**: 本実装はモック検証のみ。Phase D 着手時に Tapo C210 → 音声出力までの実機確認をカイニット手動で 1 回行う。
- **暖機ファイルの本番運用**: `/tmp/pico_v3_warmup.wav` は再起動で消える。Phase D で常駐ディレクトリ (`~/.pico_v3/` 配下) に切り替えるか検討。

---

*Phase C-3 migration notes | 2026-05-19*
*関連: 設計書_統合版_v4_pico_v3.md 第 7-2 / 第 14-5 / 第 14-7 章*
