# familiar-ai 概観 (pico_v3 拡張後)

**初版作成**: 2026-05-16 (Stage 1 Phase A 完了時、ピコ独自改造前)
**最終更新**: 2026-05-20 (外出期間 Day 3、Phase C-1 + Discord Phase D mock スケルトン完了時点)
**ブランチ**: `pico-base` (`pico/pico-base` リモート同期、HEAD: `89c78f5`)
**pytest 件数**: **1170 件 グリーン**

## 1. 全体構成 (familiar_agent + pico_agent)

ピコ v3 は **familiar-ai (上流) を本体として尊重しつつ、ピコ独自層を `src/pico_agent/` に追加** する設計。
familiar-ai の `src/familiar_agent/` への侵入は最小限に留め、上流マージコンフリクトのリスクを抑える。

```
src/
├── familiar_agent/        # 上流 (lifemate-ai/familiar-ai fork)
│   ├── agent.py           # 中核 ReAct ループ (pico 改造: response_filter 統合のみ)
│   ├── backend.py         # LLM バックエンド抽象 (pico 改造: UTILITY_BASE_URL/SCENE_BASE_URL 対応)
│   ├── tools/
│   │   ├── camera.py      # Tapo C210 ONVIF/RTSP (pico 改造: PTZ tilt 反転バグ修正、ONVIF cleanup)
│   │   ├── tts.py         # TTS tool (pico 改造: adapter 経由化)
│   │   ├── stt.py         # STT tool (pico 改造: adapter 経由化)
│   │   └── (他多数)
│   ├── interoception.py
│   ├── mental_state.py
│   ├── relationship.py    # single-user trust/intimacy
│   ├── self_narrative.py  # 短文の自己語り
│   ├── desires.py         # 4 欲求モデル (look_outside/observe_room/miss_companion/browse_curiosity)
│   └── (他多数、計 52 ファイル)
│
└── pico_agent/            # ピコ独自層 (新規追加)
    ├── __init__.py
    ├── response_filter.py # 漏出フィルタ (Phase C-0、末尾ブロック限定 algo)
    ├── adapters/
    │   ├── __init__.py
    │   ├── tts_sbv2.py    # Style-BERT-VITS2 (Phase C-1 + v4.2 14-5 フォールバック)
    │   ├── stt_kotoba.py  # Kotoba-Whisper (Phase C-1 + v4.2 14-4 RTSP スケルトン)
    │   └── vision_qwen3vl.py  # qwen3-vl on Ollama (Phase C-1)
    └── discord_bridge/    # Phase D スケルトン + mock 実装
        ├── __init__.py
        ├── availability.py
        ├── bot.py
        ├── text_channel.py
        └── voice_channel.py
```

---

## 2. clone/起動の動線

```
git clone git@github.com:kainit888/familiar-ai.git .   # ピコ用 fork (本家は upstream remote)
git checkout pico-base
uv sync                                                 # Python 3.11.15 + 全依存
./run.sh   →   uv run familiar   →   textual TUI 起動
```

`run.sh` は 4 行のシンプルな wrapper:

```bash
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec uv run familiar "$@"
```

⚠️ **Phase C-1 完了時点では `pico_v3.service` (systemd) 設定なし**。TUI は手動起動。
Phase D 本番接続テスト完了後に systemd 設定を着手する想定。

---

## 3. モジュール構成

### 3-1. familiar_agent (52 ファイル、52 .py)

#### コア
- `agent.py`: 中核オーケストレータ (約 3000 行、pico 改造で response_filter 統合 2 箇所追加)
- `backend.py`: LLM バックエンド抽象化 (PLATFORM=openai/anthropic/gemini/kimi/glm)
  - **pico 改造 (commit f8bface)**: `create_utility_backend` / `create_scene_backend` で
    `UTILITY_BASE_URL` / `SCENE_BASE_URL` を openai branch でサポート
- `bootstrap.py`: 起動初期化
- `main.py`: CLI エントリポイント
- `config.py`: 設定読み込み

#### UI
- `tui.py`: textual ベースの TUI
- `gui.py`: GUI
- `_ui_helpers.py`, `_i18n.py`: UI 補助 / 多言語対応 (locales/ に翻訳)

#### 認知/感情系
- `interoception.py`: 内受容感覚 (Phase E で sensation_text + body_temp 拡張予定)
- `appraisal.py`: 評価/appraisal
- `mental_state.py`: 心的状態 (~/.familiar_ai/mental_state.jsonl)
- `self_state.py`: 自己状態 (~/.familiar_ai/self_state.json)
- `attention_schema.py`: アテンションスキーマ
- `prediction.py`: 予測
- `meta_monitor.py`: メタ監視 (設計書 7-6 章メタ認知エンジンと類似)

#### メモリ
- `memory_worker.py`: メモリ書き込みワーカー
- `tape.py`: イベントテープ (≈ daybook 素材)
- `scene.py`: 場面切り出し (`scene_entities`, `scene_events` テーブル)
- `relationship.py`: 関係性 (`RelationshipTracker` クラス、single-user trust/intimacy)
- `self_narrative.py`: 自己語り (~/.familiar_ai/self_narrative.jsonl、write/read_recent のみ)
- `sqlite_migrations.py`: SQLite スキーマ移行

#### 欲求/自律
- `desires.py`: 4 欲求モデル (Phase H でピコ独自追加予定: heartbeat/daybook/reflect/vision_scheduler)
- `heartbeat.py`: heartbeat ループ (~/.familiar_ai/heartbeat_state.json)
- `concern_engine.py`: 気がかりエンジン (~/.familiar_ai/active_concerns.json)
- `exploration.py`: 探索 (`exploration_state` テーブル)
- `default_mode.py`: デフォルトモード
- `routines.py`: 日課
- `reflect.py`: 振り返り
- `voice_guard.py`: 音声ガード
- `intervention_policy.py`: 介入ポリシー

#### 社会性
- `social_policy.py`: 社会ポリシー (Phase G で 5 サブモジュール `pico_agent/sociality/` に分解予定)

#### I/O
- `camera_discovery.py`: ONVIF カメラ自動検出 (Phase B で Tapo C210 接続)
- `realtime_stt_session.py`: リアルタイム STT (Phase C-1 で Kotoba-Whisper adapter に差替済)
- `mcp_client.py`: MCP プロトコルクライアント (`~/.familiar-ai.json`)
- `event_bus.py`: イベントバス
- `workspace.py`: ワークスペース

#### サブパッケージ
- `tools/`: 各種ツール (camera/tts/stt 等)
- `locales/`: 多言語 (ja, en, fr, de, zh)

### 3-2. pico_agent (Phase C-0 〜 Phase D mock まで完成)

#### `response_filter.py` (Phase C-0)
- ピコ応答末尾の内部メンタル状態スキャフォールディングを除去
- アルゴリズム: 末尾ブロック限定 (`:` マーカ or 漏出 2 行以上で削除確定、本文中は保護)
- パターン: `[Mental state]`, S-expression `(interoception ...)`, 数値内部状態 (`arousal: 0.85`),
  英語ラベル箇条書き (`- companion: ...`), 行動メモ (`- remember ...`), 英語一人称 (`- I feel ...`),
  ToM 推論 (`- 不安 (0.8)`) 等

#### `adapters/tts_sbv2.py` (Phase C-1 + v4.2 14-5)
- `speak()`: SBV2 (port 5000) に GET /voice で WAV bytes 取得 (既存)
- `play_with_fallback(text, target='auto')`: WAV 取得 + 物理再生まで実行 (新)
- backend: `_play_via_go2rtc` (stub、`GO2RTC_ENABLED=1` のときのみ) /
  `_play_via_main_pc` (mpv/ffplay) / `_play_via_rpi5` (aplay/paplay)
- フォールバック: tapo_speaker → main_pc → rpi5

#### `adapters/stt_kotoba.py` (Phase C-1 + v4.2 14-4)
- `transcribe(audio_bytes, sample_rate)`: whisper_server に POST (既存)
- `start_rtsp_subscription(on_speech, rtsp_url, ...)`: RTSP 常駐タスクのスケルトン (新)
- 依存判定: ffmpeg + silero-vad、揃わなければ no-op タスクを返す

#### `adapters/vision_qwen3vl.py` (Phase C-1)
- `describe_scene(image_bytes, prompt)`: qwen3-vl:4b に投げて自然言語記述
- `parse_scene(image_bytes)`: 構造化 dict (parser テスト合格、但し実機では thinking mode 問題で空 dict)
- `/no_think` プレフィックスで thinking mode 抑制 (commit 12126de)

#### `discord_bridge/` (Phase D mock)
- `availability.py`: discord.py の動的可用性判定 + DiscordDisabledError
- `bot.py`: PicoBot (start/stop/send_message/start_in_background、lazy import で本体未インストール対応)
- `text_channel.py`: TextChannelHandler + IncomingMessage + incoming_message_from_discord 変換
- `voice_channel.py`: VoiceChannelListener (join/leave/speak、FFmpegPCMAudio で WAV 再生)
- discord.py は **uv add していない** (Phase D 帰宅後の本番接続テスト時に追加)

---

## 4. データ保存先 (重要)

**ハードコード**: `Path.home() / ".familiar_ai"` 配下 (= `~/.familiar_ai/`)。
環境変数で差し替え不可。変更にはソース修正が必要。

| ファイル/DB | 用途 | サイズ目安 |
|---|---|---|
| `~/.familiar_ai/observations.db` (SQLite + WAL) | 観察ログ・関係性・scene 等 | 数百 KB - 数 MB |
| `~/.familiar_ai/mental_state.jsonl` | 心的状態履歴 (append-only) | TUI 稼働時間に比例 |
| `~/.familiar_ai/self_state.json` | 自己状態スナップショット | 数百 B |
| `~/.familiar_ai/heartbeat_state.json` | heartbeat 状態 | 数百 B |
| `~/.familiar_ai/active_concerns.json` | 気がかり | 数百 B |
| `~/.familiar_ai/captures/` | Vision キャプチャ | 大量 (TUI 稼働時間に比例) |
| `~/.familiar_ai/self_narrative.jsonl` | 自己語り | append-only |
| `~/.familiar_ai/desires.json` | 欲求 state | 数 KB |
| `~/.familiar_ai/schedule.conf` | スケジュール設定 | 数百 B |
| `~/.familiar-ai.json` | MCP 設定 | — |
| `~/.cache/familiar-ai/` | ログ | 起動回数に比例 |

実 DB スキーマ (Phase B 検証時の `observations.db`):
- familiar-ai 標準: `observations`, `episodes`, `episode_memories`, `obs_embeddings`,
  `semantic_facts`, `scene_entities`, `scene_events`, `memory_*`, `relationship_state`,
  `behavior_policies`, `exploration_state`, `unfinished_business`
- Phase G で追加予定: `sociality_users`, `sociality_commitments`,
  `sociality_shared_topics`, `sociality_consent_log`, `sociality_daybook`,
  `sociality_arcs`, `sociality_reflections`, `sociality_joint_focus`,
  `sociality_social_events`, `sociality_familiarity_evidence`

詳細: [`phase_g_schema.md`](./phase_g_schema.md)

---

## 5. 起動時の挙動 (Phase C-1 完了時点)

1. `./run.sh` → `uv run familiar` → textual TUI 起動 (1-2 秒)
2. ヘッダー: `🦉 Familiar AI version v0.1` `あなたのそばに暮らすAI 🐾`
3. ステータス行: `familiar-ai 起動。/quit で終了、Ctrl+L で履歴クリア。ログ: ...`
4. 「初期化中... (0s)」 → カウントアップ
5. background thread として `_capture_loop` 起動 (Tapo C210 への RTSP 接続)
6. 「メッセージ >」入力プロンプト
7. ~/.familiar_ai/ に state ファイル群が即座に作成される

### ⚠️ 長期稼働時の既知問題 (Day 2 タスク F で発見)

**TUI を 15 時間以上連続稼働させると `_capture_loop` が黒画像を返し続ける**。
詳細・修正案: [`vision_black_capture_analysis.md`](./vision_black_capture_analysis.md) +
[`capture_loop_fix_design_memo.md`](./capture_loop_fix_design_memo.md)

帰宅後 P0-3 タスクで修正予定 (案 1+2 の組み合わせ、約 12 行追加)。

---

## 6. v4.2 設計書との整合状況 (要点抜粋)

詳細: [`phase_c_v4.2_alignment.md`](./phase_c_v4.2_alignment.md) 第 II 部

| 設計書章 | 整合状況 | 緊急性 |
|---|---|---|
| 4 章 LLM ルーティング | UTILITY_LLM が誤って Gemini に向く (`.env` 修正のみ) | 🔴 BLOCKER (差分 4-A) |
| 7 章 adapter (TTS/STT/Vision) | ほぼ実装済 (P2: SCENE_* 設定追記推奨) | 🟠 PHASE-D |
| 8 章 メモリ | 既存 familiar-ai スキーマで OK (設計書の memories → 3 テーブル分割と読替) | 🟡 OBSERVED |
| 10 章 sociality | 全 5 モジュール未着手 | 🟡 STAGE-3 |
| 11 章 autonomy | familiar-ai 4 欲求のみ、ピコ独自 4 欲求未追加 | 🟡 STAGE-3 |
| 12 章 音声パイプライン | Phase D で実装 | 🟠 PHASE-D |
| 14 章 Tapo 統合 | カメラ動作確認済、TTS/STT adapter 経路は scaffolding まで | 🟠 PHASE-D |

---

## 7. 既知の警告 / 既知の問題

### 解決済
- ✅ PTZ tilt 上下反転 (commit 1733c7e)
- ✅ ONVIF aiohttp session cleanup (commit c8fdccb)
- ✅ qwen3-vl thinking mode (commit 12126de、describe_scene のみ)
- ✅ response_filter 漏出 (commits 53f4b38 + 87002a5)
- ✅ DISCORD_TOKEN 環境変数サポート (commit 793bb03)

### 帰宅後対応 (P0)
- ⏸ TUI 長期稼働で `_capture_loop` 黒画像化
- ⏸ `.env` UTILITY_* がクラウド Gemini を指している (v4.2 では openai+ollama)
- ⏸ Day 2 朝発見: TUI が外出中も稼働しっぱなし (15 時間連続)

### 持ち越し
- ⏸ pytest 5-7 warnings: `AsyncMockMixin._execute_mock_call` coroutine never awaited
  (本家テストコード由来、実害なし)
- ⏸ parse_scene 実機未動作 (qwen3-vl の thinking mode JSON 問題、Phase G で対応)
- ⏸ タスク 1 sub-step 3 commit 構成 (前夜から持ち越し、3 commit 分割の指示違反)

---

## 8. テスト件数推移

| 時点 | 件数 | 主な追加 |
|---|---|---|
| Phase A 完了 (2026-05-16) | 883 | familiar-ai 既存テストのみ |
| Phase B 完了 (2026-05-17) | 887 | +4 PTZ tilt invert テスト |
| Phase C-0 完了 (2026-05-17) | 947 | +60 response_filter / leakage テスト |
| Phase C-1 完了 (2026-05-18 朝) | 981 | +34 adapter テスト |
| **外出期間終了 (2026-05-20)** | **1170** | +189 (filter false positive +27 / TTS fallback +31 / STT +20 / discord_bridge +57 / その他 +54) |

詳細: [`test_baseline.md`](./test_baseline.md)

---

## 9. Stage 2 で改造済 / 未着手の領域 (設計書 v4.0/4.2 対応)

| 設計書章 | familiar-ai 該当 | 状況 |
|---|---|---|
| 5 章 adapter | (なし、本家は ElevenLabs 直結) | ✅ `src/pico_agent/adapters/` 新設、ElevenLabs 直叩き dead-code 化 |
| 9 章 感情 3 値 | `interoception.py` 拡張予定 | ⏸ Phase E で着手 |
| 10 章 sociality | `social_policy.py` (単一) | ⏸ Phase G で 5 サブモジュール分解 (`pico_agent/sociality/`) |
| 11 章 autonomy | `desires.py` + `heartbeat.py` | ⏸ Phase H でピコ独自追加 |
| 13 章 Discord | (なし) | ✅ `pico_agent/discord_bridge/` スケルトン + mock 完了、Phase D 本番接続は帰宅後 |
| 14 章 Tapo | `camera_discovery.py`, `camera.py` | ✅ adapter 経由化済、🔴 `_capture_loop` 長期稼働バグあり |
| 8 章 メモリ | `relationship.py` + `observations.db` | ⏸ Phase F (ChromaDB 移行は元データ消失で実質不要、Phase G で sociality_* 追加) |
| 7-6 章 メタ認知 | `meta_monitor.py` + `attention_schema.py` 等 | ⏸ Stage 3 で本格化 |

---

## 10. リファレンス

### 設計書 (読み取り専用)
- [`設計書_統合版_v4_pico_v3.md`](./../設計書_統合版_v4_pico_v3.md) v4.2

### 関連ドキュメント
- [`outside_period_summary.md`](./outside_period_summary.md) — 外出期間 (Day 1-3) 総合まとめ
- [`phase_c_v4.2_alignment.md`](./phase_c_v4.2_alignment.md) — v4.2 整合差分
- [`phase_d_design_memo.md`](./phase_d_design_memo.md) — Phase D 実装ステップ
- [`phase_g_schema.md`](./phase_g_schema.md) — Phase G テーブル定義
- [`phase_g_design_memo.md`](./phase_g_design_memo.md) — Phase G 関数シグネチャ
- [`vision_black_capture_analysis.md`](./vision_black_capture_analysis.md) — 黒画像問題の真因
- [`capture_loop_fix_design_memo.md`](./capture_loop_fix_design_memo.md) — `_capture_loop` 修正設計
- [`utility_llm_blocker_analysis.md`](./utility_llm_blocker_analysis.md) — UTILITY_LLM 切替手順
- [`test_baseline.md`](./test_baseline.md) — pytest 件数推移
- [`INDEX.md`](./INDEX.md) — ドキュメント全体索引

### Commit history
全外出期間中の commit はすべて `pico/pico-base` に push 済み (`https://github.com/kainit888/familiar-ai/tree/pico-base`)。

---

*familiar-ai overview | 初版 2026-05-16, 最終更新 2026-05-20 (Day 3 朝、候補 J-1 産物)*
