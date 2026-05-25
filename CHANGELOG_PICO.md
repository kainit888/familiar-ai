# pico_v3 fork — Changelog

ピコ独自実装 (pico_v3) の changelog。上流 [familiar-ai](https://github.com/lifemate-ai/familiar-ai)
からの **差分のみ** を記録する。上流の changelog は `CHANGELOG.md` を参照。

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

---

## [Unreleased] — Stage 2 Phase C-8 完了 (2026-05-25)

### Phase C-8 (2026-05-25): TTS 複数チャンク音声途中切れバグ修正

長文を 30 文字分割して複数チャンクを SBV2 で合成したとき、音声が 1 個目のチャンクで
途中切れする不具合を修正。**原因**: 旧 `_fetch_wav_bytes` が複数チャンクの WAV を
`b"".join()` でバイト連結していたが、これだと WAV ヘッダの data サイズが第1チャンク分
しか宣言されず、ffmpeg が 1 個目だけ読んで打ち切っていた (実機 ffprobe で確認: byte
連結ファイル → 3.146s = chunk1 のみ / concat demuxer 結合 → 7.303s = 全チャンク)。
**修正**: バイト連結をやめ、各チャンクの WAV を list で返し、ffmpeg の **concat
demuxer** (`-f concat -safe 0 -i <list>`) で結合 + 前処理する単一経路に再設計。
Phase C-7 の HTTP pull アーキ (配信サーバ / 単一 POST / 遅延削除 / 新 env) は **一切不変**。

**部分失敗時の挙動 (採用方針: 部分再生)**: `_fetch_wav_parts` は空チャンク (`b""`) を
`if part:` で list から除外し、取得できたチャンクのみ concat する。一部チャンクが失敗
しても成功分は鳴らす (silent fail = 鳴る分は鳴らす方針に合致)。全チャンク失敗時のみ無音。

#### Fixed

- `src/pico_agent/adapters/tts_sbv2.py`
  - `_fetch_wav_bytes` → `_fetch_wav_parts` に改名・再設計。戻り値を `bytes` から
    `list[bytes]` へ。最後の `return b"".join(audio_parts)` を `return parts` に変更。
    空チャンクは `if part:` で除外、全滅 / 空入力 / session 例外時は `[]` を返す
  - `_write_tmp_wavs(parts: list[bytes]) -> list[str]` を新設 (各 part を `_write_tmp_wav`
    で書き出す)。`_write_tmp_wav` は据え置き
  - `_build_af_filter() -> str` を新設 (`volume=<TTS_VOLUME>,apad=pad_dur=<TTS_TAIL_SILENCE>`
    を切り出し、concat 経路と共有。パラメータ値は不変)
  - `_write_concat_list(src_paths) -> str` を新設。各 path を `os.path.abspath` + シングル
    クォートエスケープ (`'` → `'\''`) して `file '<path>'` 形式で 1 行ずつ tempfile API
    で書き出す (echo/redirect 不使用)
  - `_build_concat_ffmpeg_args(list_path, dst) -> list[str]` を新設
    (`ffmpeg -y -loglevel error -f concat -safe 0 -i <list> -af <filter> -ar <rate> -ac 1 -f wav <dst>`)
  - `_concat_and_preprocess(src_paths, out_dir=None) -> str | None` を新設。concat demuxer で
    全チャンクを結合 + 前処理。ffmpeg なし / 入力空 / rc!=0 / 例外で `None` + warning (silent
    fail)、`finally` で list ファイルを `_unlink_quiet`
  - `_preprocess_wav_with_ffmpeg` (単一 WAV 前処理) と `_build_ffmpeg_args` を削除
    (concat 経路に置換、`_build_af_filter` は残置)
  - `speak()` tapo 経路を組み替え: `_fetch_wav_parts` → `_write_tmp_wavs` →
    `_concat_and_preprocess(src_paths, out_dir=serve_dir)` → 単一 POST。`finally` で
    SBV2 生 WAV 群を即削除、配信 WAV は C-7 の遅延削除を維持

#### Tests (Phase C-8)

- `tests/test_adapter_tts_sbv2.py`: 既存書換 + 新規追加 (84 → 90 件)
  - 新規 `test_concat_list_contains_all_chunks` — list ファイルに両チャンク行が含まれ
    行数 == 入力数 (1個目だけ書く mutation を検知)
  - 新規 `test_concat_list_escapes_single_quotes` — シングルクォートエスケープ確認
  - 新規 `test_concat_args_use_concat_demuxer` — `-f concat` / `-safe 0` / `-i <list>` /
    `-af volume=...,apad=...` / `-ar 16000` / `-ac 1` / 末尾 `-f wav <dst>` (mutation 検知)
  - 新規 `test_concat_args_use_env_overrides` — 前処理パラメータの env 上書き維持確認
  - 新規 `test_speak_multi_chunk_writes_all_src_and_concats` — `_concat_and_preprocess` を
    スパイ化し渡る src_paths 長 >= 2 == チャンク数 + 単一 POST (byte-join 復活 / 1個目
    だけ渡す mutation を検知)
  - 新規 `test_fetch_wav_parts_returns_list` — 複数チャンクで list / len == チャンク数 /
    各要素 bytes (`b"".join` 復活を検知)
  - 新規 `test_fetch_wav_parts_excludes_empty_and_all_fail_returns_empty` — `b""` 除外 +
    全滅 `[]` (空除外削除を検知)
  - 移植 (旧 `_preprocess_wav_with_ffmpeg` 単体テスト → `_concat_and_preprocess` 側へ):
    `test_concat_returns_none_on_nonzero_rc` / `test_concat_no_ffmpeg_returns_none` /
    `test_concat_empty_src_returns_none` / `test_concat_exec_exception_returns_none`
  - 書換: `test_fetch_wav_bytes_*` → `test_fetch_wav_parts_*` (list 期待)、
    `test_model_name_query_includes_model_name` を `_fetch_wav_parts` 呼出へ、
    `_patch_go2rtc_chain` と各 `fake_preprocess` を `_concat_and_preprocess` の fake に差替
  - 緑維持確認: `test_speak_splits_long_text_into_multiple_requests` (複数 GET → 単一 POST)、
    C-7 系 (`test_serve_*` / `test_go2rtc_url_uses_http_url_not_local_path` 等)
- pytest 件数: 1367 → **1373** (グリーン、ruff/mypy クリーン)

### Phase C-7 (2026-05-25): go2rtc HTTP pull 方式に修正

go2rtc は `src=ffmpeg:<url>#input=file` で WAV を **HTTP pull** する。従来は Pi 上の
ローカルパス (`ffmpeg:/tmp/xxx.wav#input=file`) を渡していたが、go2rtc は **メイン PC**
で動くため Pi のローカルパスを開けず、音が鳴らなかった。本サイクルでは Pi 側に小さな
WAV 配信サーバ (aiohttp.web) を立て、go2rtc に Pi の HTTP URL を pull させる方式へ修正。

#### 修正 4: default go2rtc URL バグ

- `src/pico_agent/adapters/tts_sbv2.py:74`
  - `_DEFAULT_GO2RTC_BASE_URL = "http://127.0.0.1:1984"` → `"http://192.168.10.104:1984"`
  - go2rtc は Pi ローカルではなくメイン PC で常駐するため、`127.0.0.1` は誤り

#### 修正 5: 新規 env アクセサ + .env.example

- `src/pico_agent/adapters/tts_sbv2.py:82` 付近: 定数 3 件追加
  - `_DEFAULT_TTS_SERVE_PORT = 50021` / `_DEFAULT_TTS_PI_SELF_IP = "192.168.10.109"` /
    `_DEFAULT_TTS_DELETE_DELAY_SEC = 30.0`
- アクセサ 3 件追加 (`_get_*` 慣習: int/float は try/except フォールバック、str はそのまま)
  - `_get_serve_port() -> int` (env `TTS_SERVE_PORT`)
  - `_get_pi_self_ip() -> str` (env `TTS_PI_SELF_IP`)
  - `_get_delete_delay_sec() -> float` (env `TTS_DELETE_DELAY_SEC`、**カイニット提案で採用**)
- `.env.example` の go2rtc/TTS ブロック (L134 直後) に 3 キーをコメント付きで追記
  (`.env` 本体は無変更)

#### 修正 2: Pi 側 WAV HTTP 配信サーバ (aiohttp.web、新規依存なし)

- `src/pico_agent/adapters/tts_sbv2.py`: `from aiohttp import web` 追加 (L55)、
  module-level singleton (`_serve_runner` / `_serve_dir` / `_serve_lock`) +
  `_serve_handler` / `_ensure_http_server()` を新規追加
  - `_ensure_http_server()` は lazy init + singleton (lock 下で冪等起動)。
    `tempfile.mkdtemp` で配信 dir を作り `0.0.0.0:<TTS_SERVE_PORT>` に listen、
    `(root_dir, port)` を返す
  - `_serve_handler` は `Path(name).name` でトラバーサル無効化 + `.wav` 以外は 404 +
    `_serve_dir is None` も 404

#### 修正 1: src を HTTP URL に + 配信 dir への WAV 配置

- `src/pico_agent/adapters/tts_sbv2.py`
  - `_build_go2rtc_url(wav_path)` → `_build_go2rtc_url(wav_url)` (引数の意味を
    ローカルパス → HTTP URL へ。`ffmpeg:<wav_url>#audio=pcma#input=file`)
  - `_preprocess_wav_with_ffmpeg(src_path, out_dir: Path | None = None)`:
    `out_dir` 指定時はその dir 内に WAV を作る (配信 dir)。`None` は従来動作 (後方互換)
  - `speak()` tapo 経路: `_ensure_http_server()` → 配信 dir 取得 → ffmpeg 出力をその dir →
    `http://<TTS_PI_SELF_IP>:<port>/<name>` を組立 → `_post_to_go2rtc(wav_url)`
  - `#audio=pcma#input=file` の維持必須要素は不変

#### 修正 3: 遅延削除

- `src/pico_agent/adapters/tts_sbv2.py`: `_unlink_quiet(path)` / `_delayed_unlink(path, delay)`
  を新規追加。`speak()` の `finally` を変更:
  - SBV2 生 WAV (`src_path`) は **即削除** (`_unlink_quiet`)
  - 配信 WAV (`pre_path`) は go2rtc が pull し終える猶予のため
    `asyncio.create_task(_delayed_unlink(pre_path, _get_delete_delay_sec()))` で **遅延削除**

#### Tests (Phase C-7)

- `tests/test_adapter_tts_sbv2.py`: 既存テスト書換 (件数据え置き) + 新規 4 件
  - 書換: `test_go2rtc_url_default` / `test_legacy_GO2RTC_URL_ignored` を
    `192.168.10.104:1984` 期待へ、`test_go2rtc_encodes_src_correctly` /
    `test_go2rtc_uses_env_overrides` を HTTP URL 入力 + `ffmpeg%3Ahttp%3A` 期待へ
  - 共通モックヘルパ `_patch_go2rtc_chain` に `_ensure_http_server` fake と
    `_delayed_unlink` 即時化を追加 (実サーバ起動なしで全 tapo 経路テスト緑維持)。
    独自 session を立てる `test_speak_passes_speaker_id_to_query` /
    `test_speak_emotion_affects_style_weight` / `test_speak_ffmpeg_failure_returns_silently`
    にも `_ensure_http_server` fake を追加、`fake_preprocess` を `out_dir=None` 受容に
  - 新規 1: `test_go2rtc_url_uses_http_url_not_local_path` — 二重 unquote で
    `/tmp/` 不在 + `http://<ip>:<port>` 在 + `ffmpeg:http://` 在 + `#audio=pcma#input=file` 在
  - 新規 2: `test_serve_server_singleton` — `AppRunner`/`TCPSite` mock 化、`mkdtemp` 固定、
    singleton リセット。2 回呼んで TCPSite 構築回数==1・port 一致を assert
  - 新規 3: `test_serve_wav_not_deleted_immediately` — 段階1=`_delayed_unlink` スパイ化で
    speak() 直後に配信 WAV 存在 + 遅延 task へ `(pre_path, 30.0)` 渡し確認、
    段階2=`asyncio.sleep` 即 return 化し実 `_delayed_unlink` 直接 await で `not exists()`
  - 新規 4: `test_default_go2rtc_base_url_is_main_pc` — env 未設定で
    `_get_go2rtc_base_url() == "http://192.168.10.104:1984"` + 定数直接 assert
- pytest 件数: 1363 → **1367** (グリーン)

#### 設計書 v5 との齟齬 (v5.1 改訂予定)

- v5 14-5-2 / 14-5-5 節は `src=ffmpeg:<wav_path>#input=file` を **ローカルパス前提** で
  記述しているが、go2rtc がメイン PC で動く以上 Pi ローカルパスは開けない。本サイクルの
  HTTP pull 方式 (Pi 側 WAV 配信サーバ + Pi の HTTP URL を src に渡す) との齟齬は
  **v5.1 で改訂予定** (本サイクルでは CHANGELOG 記録のみ、設計書本体は無変更)

---

## [Released] — Stage 2 Phase C-6 完了 (2026-05-24)

### Phase C-6 (2026-05-24): 三点同時修正 (STT form key / loguru sink / go2rtc 起動撤廃)

このサイクルでは Stage 2 デバッグ調査 (Phase C-5.5 marker 投入) で判明した
3 件の独立バグを一括修正した。いずれも v5 設計書「14-4-7 STT 連携 (Kotoba-Whisper)」
「14-5-1 TTS 経路」「ロギング (TUI 衝突対策)」と整合する。

#### 修正 1: STT form key `"file"` → `"audio"`

- `src/pico_agent/adapters/stt_kotoba.py:287-293`
  - `aiohttp.FormData().add_field("file", audio_bytes, ...)` を `"audio"` に変更
- 受信側 `whisper_server.py:/transcribe` が `request.files["audio"]` を期待するため、
  従来の `"file"` キーは静かに 400 で落ちて空文字を返していた (silent fail)
- `sample_rate` の文字列同梱は **そのまま維持** (回帰防止テストあり)
- 上流 PR 候補度: **低** (pico_v3 独自層、whisper_server.py との結線専用)

#### 修正 2: loguru → app.log 専用 sink (二層ログ統合)

- `src/familiar_agent/main.py::setup_logging` に **+13 行** (loguru import + 4-6 行の sink 追加 + 関連定数)
  - `loguru_logger.remove()` でデフォルト stderr sink を除去 (TUI 汚染防止)
  - `loguru_logger.add(log_file, level=..., enqueue=True, encoding="utf-8")` で
    既存 `~/.cache/familiar-ai/app.log` へ enqueue (async-safe) で書き出す
- pico_agent.* モジュール (loguru 経由) と familiar_agent.* (stdlib logging 経由)
  の両系統が **同一ファイル** に出るようになる
- **InterceptHandler は意図的に追加しない** (二重ログ回避)。逆方向はそのまま分離
- 標準 logging 経路 (`logging.FileHandler` + 3rd party レベル絞込) は無変更
- 上流 PR 候補度: **中** (familiar_agent 側だけでは loguru 依存ファイルが無いため、
  v6 で「ロギング統合は pico オプション」として説明文を入れる前提)

#### 修正 3: `_ensure_go2rtc()` 呼出削除 (TTSTool.__init__)

- `src/familiar_agent/tools/tts.py:133` の `_ensure_go2rtc(self.go2rtc_url)` を削除
- 関数本体 (L77-108)、`_GO2RTC_BIN` / `_GO2RTC_CACHE` / `_GO2RTC_CONFIG` 定数、
  `TTSTool` クラスは **無変更で残置** (上流マージ性維持、二層分離原則)
- Phase C-5 で `say()` が `tts_sbv2.speak()` 単一経路に統一済みのため、
  go2rtc バイナリ起動は Pi 上で完全に不要 (HTTP API のみ使用)
- 起動時に出ていた `"go2rtc binary not found"` / `"go2rtc config not found"`
  WARNING ログが消える (回帰防止テストあり)
- 上流 PR 候補度: **低-中** (v5 14-5-1 単一経路化と整合、上流に提案する場合は
  「go2rtc サポート自体を deprecate するか option 化」が前提)

#### 14-4-7 節への v6 改訂要望 (planner 集約)

設計書 v6 改訂時、STT 連携節に以下を明文化する:

- multipart form field 名: `"audio"` (whisper_server.py I/F)
- `sample_rate` フィールドを文字列で同梱する
- `/transcribe` レスポンスは `application/json` / `text/plain` の両 Content-Type
  対応 (silent fail 設計)

#### Phase C-5.5 デバッグマーカーの扱い

- カイニット指示 **選択肢 A (残置)** で確定 — commit `e8728c7` で投入した
  `_stdlog.info("...ENTER...")` 系マーカーは簡潔化・revert いずれも実施しない
- loguru sink 追加後も二重ログにはならない (stdlib `_stdlog` と `loguru.logger`
  は別系統で event も別) — 新規テストで実装確認

#### Tests (Phase C-6)

- `tests/test_adapter_stt_kotoba.py`: 2 件追記
  - `test_stt_form_key_is_audio`: form field 名が `"audio"` で `"file"` は不在
  - `test_sample_rate_field_still_sent`: `sample_rate="16000"` の文字列同梱維持
- `tests/test_setup_logging_loguru.py` (新規): 1 件
  - `test_loguru_logs_reach_app_log`: loguru/stdlib 両系統が `app.log` に届く
- `tests/test_tts.py`: 1 件追記
  - `test_tts_tool_init_does_not_emit_go2rtc_warning`: `TTSTool()` init 時に
    `"go2rtc binary not found"` / `"go2rtc config not found"` WARNING が出ない
- pytest 件数: 1359 → **1363** (グリーン)

---

## [Released] — Stage 2 Phase C-5 完了 (2026-05-24)

### Phase C-5 (2026-05-24): TTS 単一経路化

#### 侵入: familiar_agent/tools/tts.py::say()

- `output` パラメータ (`"local"` / `"remote"` / `"both"`) を **deprecated 扱いで無視**、
  常に `tts_sbv2.speak(text, target="tapo_speaker")` を呼ぶよう変更
- 旧 `_play_local` / `_play_via_go2rtc` / `_write_tmp_audio` 系は
  **dead code として残置** (二層分離原則で削除せず、上流マージ性を維持)
- 設計書 v5 第 24-4 節「侵入点」表への該当行追加は将来判断 (本サイクルでは pico
  独自層からの最小侵入のみ)

#### pico_agent/adapters/tts_sbv2.py (独自層、丸ごと再設計)

- `play_with_fallback` / `_play_via_go2rtc` / `_play_via_main_pc` / `_play_via_rpi5`
  を **完全削除** (731 → 478 行、フォールバックチェーン撤廃)
- 新 `speak(text, target="tapo_speaker"|"discord_vc"|"obs_audio") -> None` で
  **HTTP API 単一経路** に再構成 (v5 14-5-11)
- `discord_vc` / `obs_audio` は Phase D / Phase K へ向けたスタブ (現状 no-op + warning)
- 失敗時は **無音 + `logger.warning`** で抜ける (例外を呼出側に漏らさない)
- `subprocess.Popen` / `subprocess.run` 呼出ゼロ (ローカル go2rtc バイナリ起動なし)

#### Phase C-1 由来 field の出自確認 (カイニット要求)

- `familiar_agent/config.py::TTSConfig.go2rtc_url` / `go2rtc_stream` は **上流由来**
  (commit `f952c18`, Kota Mizushima, 2026-02-22
  `feat: play TTS via go2rtc camera speaker with local fallback`)。
  **ピコ独自追加ではない**。
- 二層分離原則により本サイクルでは残置。将来 v6 設計書改訂時の「不要 field 整理」
  判断材料として記録。

#### Tests (Phase C-5)

- `tests/test_adapter_tts_sbv2.py`: 1376 → 1031 行、新 API テスト 80 件
  - `speak()` 基本 / target ごとのスタブ動作 / 失敗時無音 / retired symbol 不在
    assert / `Popen`/`run` 不呼出 assert を含む
- `tests/test_tts.py`: `say()` の output 無視・deprecation 挙動を 10 件で網羅
- pytest 件数: 1356 → **1359** (グリーン)

#### Configuration (Phase C-5)

- `.env.example`: `GO2RTC_ENABLED` を完全削除 (旧キーの存在自体を消す)。
  旧キー `GO2RTC_URL` / `GO2RTC_STREAM` は Phase C-3 で廃止済 (注釈のみ残る)

---

## [Pre-Phase-C-5] — Stage 2 Phase C-4 完了 (2026-05-19)

### Added (Phase C-4)

- `src/pico_agent/adapters/stt_kotoba.py`: `start_rtsp_subscription()` を
  **ffmpeg silencedetect ベースで本実装**。Phase C-1 のスケルトン (no-op) を置換。
  - VAD バックエンド: planner 確定で ffmpeg silencedetect 採用 (Phase C-3 で確定した
    `/usr/bin/ffmpeg` v7.1.3 をそのまま使う、追加 uv add 不要)
  - 1 ffmpeg プロセスから 2 出力フォーク: stdout に PCM s16le、stderr に silencedetect
  - `silence_end` イベントで PCM バッファを flush → `_wrap_pcm_to_wav` で WAV にラップ
    → `transcribe()` で書き起こし → `on_speech` callback 呼び出し
  - `STT_VAD_MAX_SEGMENT_SEC` (default 15s) 超過は強制 flush
  - `STT_VAD_MIN_SEGMENT_SEC` (default 0.3s) 未満は捨てる (ノイズ扱い)
  - ffmpeg 異常終了は `STT_FFMPEG_RESTART_BACKOFF_SEC` (default 5s) 待って再接続
  - cancel 時は ffmpeg を terminate → 3 秒待って kill、残った PCM は最後に 1 回 emit
  - RTSP URL: `STT_RTSP_URL` 優先、未設定なら `CAMERA_*` から組み立て、パスワードは URL-encode
  - ログ用 `_mask_rtsp_url()` で `rtsp://***@host/path` にマスク

### Changed (Phase C-4)

- 既存テスト 2 件を Phase C-4 の挙動に合わせて置換 (件数は据え置き):
  - `..._no_vad_returns_noop_task` → `..._no_vad_backend_returns_noop_task`
    (silero-vad monkeypatch → `STT_VAD_BACKEND=disabled` に変更)
  - `..._all_deps_present_still_noop` → `..._invokes_implementation_when_deps_present`
    ("no-op で抜ける" → "本実装が走り on_speech が呼ばれる" に変更)
- `_silero_vad_available()` は薄いラッパとして残し、現 VAD バックエンドの可用性を返す
  (Phase C-1 を直接 monkeypatch する既存テストの後方互換維持)
- 既存テストファイル全体に `autouse` fixture `_clean_rtsp_env` を追加し、
  シェル環境変数の `CAMERA_*` 混入から守る

### Tests (Phase C-4)

- `tests/test_adapter_stt_kotoba.py`: 33 件 → 74 件 (+41 件、内 2 件はリネーム+中身置換)
  - `_build_ffmpeg_rtsp_cmd` (5 件): コマンド構成検証
  - `_parse_silencedetect_line` (6 件): silencedetect 出力パース検証
  - `_wrap_pcm_to_wav` (3 件): WAV ヘッダ生成検証
  - `_emit_segment` (5 件): セグメント emit + silent fail 検証
  - env 値 (10 件): デフォルト・上書き・無効値フォールバック
  - RTSP URL 組み立て (4 件): CAMERA_* / STT_RTSP_URL 優先 / 部分 env / 記号 encode
  - URL マスク (2 件): 認証情報マスク / 認証なし URL は素通し
  - VAD backend (2 件): ffmpeg 可用性 / 未知バックエンドは False
  - `_subscription_loop` シナリオ (5 件): silence_end emit / 強制 flush / 短セグメント drop / cancel terminate / EOF 再起動

- pytest 件数: 1217 → 1258 (+41 件、グリーン維持)

### Configuration (Phase C-4)

- `.env.example` に新規 7 キー追記 (`.env` 本体への書き込みは禁止):
  - `STT_RTSP_URL` (完全上書き用)
  - `STT_VAD_BACKEND` (default: `ffmpeg`)
  - `STT_VAD_NOISE_DB` (default: `-30dB`)
  - `STT_VAD_MIN_SILENCE_SEC` (default: `0.5`)
  - `STT_VAD_MAX_SEGMENT_SEC` (default: `15.0`)
  - `STT_VAD_MIN_SEGMENT_SEC` (default: `0.3`)
  - `STT_FFMPEG_RESTART_BACKOFF_SEC` (default: `5.0`)

### Documentation (Phase C-4)

- `docs/phase_c4_migration_notes.md`: 移行ノート新規追加
- `docs/test_baseline.md`: 件数 1258 件に更新

### Deferred (Phase D へ持ち越し)

- `react_loop.py` / `agent.py` から `start_rtsp_subscription` を呼ぶ結線処理は
  Phase D で別チケット。Phase C-4 では adapter 層と関連テストのみ。
- systemd 再起動チェック (`pico_v3.service` restart) はカイニット帰宅後手動実施。

---

## [Pre-Phase-C-4] — Stage 2 Phase C-3 完了 (2026-05-19)

### Added (新規追加)

#### `src/pico_agent/` 層 (新規パッケージ)

ピコ独自実装は `src/familiar_agent/` に侵入せず、`src/pico_agent/` に新設。
上流マージ時のコンフリクトを最小化する設計。

- `pico_agent/response_filter.py`: ピコ応答末尾の内部メンタル状態スキャフォールディング除去
  - 末尾ブロック限定アルゴリズム (`:` マーカ or 連続漏出 2 行以上で削除確定)
  - 本文中・前段の単発パターンマッチは保護 (false positive 防止)
  - 漏出パターン: `[Mental state]`, S-expression, 数値内部状態, 英語ラベル箇条書き,
    行動メモ, 英語一人称, ToM 推論

- `pico_agent/adapters/tts_sbv2.py`: Style-BERT-VITS2 TTS adapter
  - `speak(text, speaker_id, emotion, target) -> bytes`: WAV bytes 取得
  - `play_with_fallback(text, target='auto') -> (success, played_via)`: 物理再生まで
  - フォールバックチェーン: `tapo_speaker` → `main_pc` → `rpi5`
  - `_play_via_go2rtc` (GO2RTC_ENABLED=1 で有効化) / `_play_via_main_pc`
    (mpv/ffplay) / `_play_via_rpi5` (aplay/paplay)

- `pico_agent/adapters/stt_kotoba.py`: Kotoba-Whisper STT adapter
  - `transcribe(audio_bytes, sample_rate) -> str`: HTTP POST
  - `start_rtsp_subscription(on_speech, rtsp_url, ...) -> asyncio.Task`:
    Tapo RTSP 音声トラック常駐購読のスケルトン (ffmpeg + silero-vad 揃わなければ no-op)

- `pico_agent/adapters/vision_qwen3vl.py`: qwen3-vl Vision adapter
  - `describe_scene(image_bytes, prompt) -> str`: 自然言語シーン記述
  - `parse_scene(image_bytes) -> dict`: joint_attention 用構造化 (実機で thinking mode 問題あり)
  - `/no_think` プレフィックスで thinking mode 抑制

- `pico_agent/discord_bridge/`: Discord 統合 (Phase D mock スケルトン)
  - `availability.py`: discord.py の動的可用性判定 + DiscordDisabledError
  - `bot.py`: PicoBot (start / stop / send_message / start_in_background、
    lazy import で discord.py 未インストール環境でも import 可能)
  - `text_channel.py`: TextChannelHandler + IncomingMessage + duck-typed 変換関数
  - `voice_channel.py`: VoiceChannelListener (join/leave/speak、FFmpegPCMAudio 再生)
  - 実 Discord 接続は **未テスト** (本番接続は帰宅後)

#### `src/familiar_agent/` への侵入 (最小限)

- `agent.py`: 末尾 2 箇所に `from pico_agent.response_filter import
  strip_internal_state_leakage` 経由のフィルタ適用 (end_turn パスと max-iter フォールバック)
- `tools/tts.py`: `pico_agent.adapters.tts_sbv2.speak` を経由する形に変更 (ElevenLabs 直叩きは
  dead code として残置)
- `tools/stt.py`: 同様に `pico_agent.adapters.stt_kotoba.transcribe` 経由化
- `tools/camera.py`: PTZ tilt 上下反転バグ修正 (Tapo C220 用)、ONVIF aiohttp session cleanup
- `backend.py`: `create_utility_backend` / `create_scene_backend` の openai branch で
  `UTILITY_BASE_URL` / `SCENE_BASE_URL` をサポート (Ollama 等のローカル endpoint 対応)
- `config.py`: `utility_base_url` / `scene_base_url` field 追加
- `settings_schema.py`: `SetupConfig` / `SettingField` 追加

### Changed (変更)

- 上流 familiar-ai の ElevenLabs 直叩き TTS 経路は **dead code 化** (削除はせず残置、
  上流マージ容易性のため)
- familiar-ai の `Camera._capture_loop` の PTZ tilt 方向を Tapo C220 向けに反転
  (上流は逆方向、Tapo C220 でのみ補正が必要)

### Fixed (バグ修正)

- PTZ tilt 上下反転バグ (commit 1733c7e) — Tapo C220 で `look up` / `look down` が
  逆方向だった問題を修正
- ONVIF aiohttp session leak (commit c8fdccb) — ONVIF client が複数 aiohttp session
  を生成して累積するリークを修正、move/cleanup 時に明示 close
- qwen3-vl の thinking mode で content が空になる問題 (commit 12126de) — describe_scene の
  prompt に `/no_think` プレフィックスを付与
- (帰宅後の P0-3 として残置) familiar-ai `_capture_loop` の長期稼働で `_last_frame` が
  zero buffer になる問題 — 設計メモのみ `docs/capture_loop_fix_design_memo.md`

### Tests (テスト追加)

外出期間中 (2026-05-18 〜 2026-05-21) の pytest 件数推移:

| 時点 | 件数 | 差分 |
|---|---|---|
| Phase A 完了 (2026-05-16) | 883 | (上流テストのみ) |
| Phase B 完了 (2026-05-17) | 887 | +4 (PTZ invert test) |
| Phase C-0 完了 (2026-05-17) | 947 | +60 (response_filter + leakage) |
| Phase C-1 完了 (2026-05-18 朝) | 981 | +34 (adapter テスト) |
| 外出期間 Day 1 タスク B-C 完了 | 1170 | +189 (false positive + fallback + Discord) |
| 外出期間最終 (2026-05-21 帰宅予定) | 1170 | (Day 2-3 は調査・設計のみ) |

**累積: 883 → 1170 (+287 件、グリーン維持)**

新規追加テストファイル:
- `tests/test_response_filter.py` (113 件、末尾ブロック algo + false positive 多数)
- `tests/test_agent_leakage.py` (7 件、memory/bus/TTS 経路への filter 流入確認)
- `tests/test_adapter_tts_sbv2.py` (53 件、基本 + フォールバック + go2rtc)
- `tests/test_adapter_stt_kotoba.py` (33 件、基本 + RTSP スケルトン)
- `tests/test_adapter_vision_qwen3vl.py` (Phase C-1 で追加)
- `tests/test_discord_bridge.py` (57 件、availability + bot + text + voice)
- `tests/test_backend_base_url.py` (6 件、UTILITY_BASE_URL / SCENE_BASE_URL test)

### Documentation (ドキュメント)

`docs/` 配下:
- `familiar_ai_overview.md`: コードベース全体像 (Phase A 〜 Phase C-1 完了時点まで更新)
- `test_baseline.md`: pytest 件数推移

別リポジトリ `pico_v3_docs/docs/` 配下:
- `outside_period_summary.md`: 外出期間 3 日間の総合まとめ (帰宅後の入口)
- `INDEX.md`: ドキュメント全体索引
- `vision_accuracy_issues.md` / `vision_black_capture_analysis.md`: Vision 黒画像問題
- `utility_llm_blocker_analysis.md`: UTILITY_LLM 切替手順
- `capture_loop_fix_design_memo.md`: `_capture_loop` 修正の詳細設計
- `phase_c_v4.2_alignment.md`: 設計書 v4.2 整合差分
- `phase_d_design_memo.md` / `phase_g_schema.md` / `phase_g_design_memo.md`: Phase D/G 準備
- `phase_e_design_memo.md` / `phase_f_design_memo.md`: Phase E/F 準備
- `outside_report_*.md`: 日次進捗報告

### Configuration (設定)

- `.env` (gitignored) に追加した環境変数:
  - `CAMERA_HOST` / `CAMERA_USERNAME` / `CAMERA_PASSWORD` / `CAMERA_ONVIF_PORT`
  - `UTILITY_PLATFORM` / `UTILITY_MODEL` / `UTILITY_API_KEY` / `UTILITY_BASE_URL`
  - `SCENE_PLATFORM` / `SCENE_MODEL` / `SCENE_API_KEY` / `SCENE_BASE_URL`
  - `STT_BASE_URL` / `STT_TIMEOUT_SEC` / `STT_RTSP_URL`
  - `TTS_BASE_URL` / `TTS_TIMEOUT_SEC`
  - `VISION_BASE_URL` / `VISION_MODEL` / `VISION_TIMEOUT_SEC`
  - `GO2RTC_ENABLED` / `GO2RTC_URL` / `GO2RTC_STREAM`
  - `DISCORD_TOKEN` / `DISCORD_OWNER_ID` / `DISCORD_GUILD_ID`

### Scripts (検証スクリプト)

`scripts/dev/`:
- `test_onvif.py`: ONVIF 認証検証 (Phase B 産物)
- `test_rtsp.py`: RTSP 1 フレーム取得検証 (Phase B 産物)
- `diagnose_rtsp_black.py`: RTSP 黒画像問題診断 (外出期間 Day 2 タスク F 産物)

### 既知の課題 (帰宅後対応)

| 優先度 | 課題 | 詳細ドキュメント |
|---|---|---|
| P0-1 | TUI 長期稼働で `_capture_loop` が黒画像化 | `vision_black_capture_analysis.md` |
| P0-2 | `.env` UTILITY_* がクラウド Gemini を指す (v4.2 設計と乖離) | `utility_llm_blocker_analysis.md` |
| P0-3 | `_capture_loop` のコード修正 (案 1+2) | `capture_loop_fix_design_memo.md` |
| P1 | discord.py の uv add + 実 Discord 接続テスト | `phase_d_design_memo.md` |
| P1 | go2rtc セットアップ (TP-Link クラウドパスワード必要) | 設計書 v4.2 14-5 |
| P2 | parse_scene の thinking mode 問題 (qwen3-vl JSON 指示で content 空) | `phase_c_handoff.md` 1-5 |

---

## fork の方針 (上流との関係)

### 上流マージ受入れポリシー

`upstream/main` の変更を定期的に取り込む:

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push pico main
git checkout pico-base
git merge main
```

侵入箇所が `src/familiar_agent/` の以下に限定されているため、上流マージは
通常容易:
- `agent.py` (2 箇所、response_filter 統合)
- `backend.py` (UTILITY_BASE_URL / SCENE_BASE_URL サポート)
- `config.py` (フィールド追加)
- `settings_schema.py` (フィールド追加)
- `tools/camera.py` (PTZ 反転 + cleanup)
- `tools/tts.py` (adapter 経由化)
- `tools/stt.py` (adapter 経由化)

ピコ独自実装は `src/pico_agent/` に集約され、上流とのコンフリクト発生源は
基本的に上記のみ。

### 上流 PR 候補

以下は ピコ専用ではなく、familiar-ai 全体に有用と思われる修正:

| 内容 | commit | PR 候補度 |
|---|---|---|
| UTILITY_BASE_URL / SCENE_BASE_URL サポート | f8bface | 高 (Ollama 等のローカル endpoint で必要) |
| ONVIF aiohttp session cleanup | c8fdccb | 高 (リーク防止、影響範囲広い) |
| `_capture_loop` の release + zero-frame 検知 | (未実装、P0-3) | 中 (Tapo 以外でも有用、要検証) |

その他 (response_filter、adapter 層、discord_bridge) は ピコ専用設計のため
上流 PR 対象外。

---

## ピコ独自設計の思想

### 「ルールと、目的は違う」 (CLAUDE.md より)

`prompts` や `policies` の値、`socialPolicy.toml` の閾値などは **ルールではなく目安**。
ピコは目的 (カイニットの睡眠を守る、関係を育てる、自分の経験から学ぶ) で判断する
余地を持つ。ME.md ペルソナは「ピコの現在の自己理解」であり、固定キャラ設定ではない。

### familiar-ai を尊重する

- 上流 familiar-ai (lifemate-ai) は別の作者の作品
- ピコ独自層は `src/pico_agent/` に分離、上流の干渉を最小化
- 上流マージは定期的に行い、上流の改善を享受
- 上流に PR できる修正は積極的に還元

---

*pico_v3 fork CHANGELOG | 初版 2026-05-20 (外出期間 Day 3 後の追加進行で作成)*
*次回更新: Phase D 本番接続テスト完了時 / Phase E 着手時*
