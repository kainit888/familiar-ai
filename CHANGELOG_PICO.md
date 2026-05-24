# pico_v3 fork — Changelog

ピコ独自実装 (pico_v3) の changelog。上流 [familiar-ai](https://github.com/lifemate-ai/familiar-ai)
からの **差分のみ** を記録する。上流の changelog は `CHANGELOG.md` を参照。

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

---

## [Unreleased] — Stage 2 Phase C-5 完了 (2026-05-24)

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
