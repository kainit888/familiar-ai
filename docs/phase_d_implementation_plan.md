# Phase D 実装計画書 (Discord 統合 — D-1〜D-7 詳細設計)

**作成日:** 2026-05-20
**作成者:** Claude Code (Opus 4.7) — タスク 6 (夜間自律進行 / planner agentId: a1e8707e4e6ecfbbd)
**親設計書:**
- `/home/pico/pico_v3_docs/設計書_統合版_v4_pico_v3.md` (v4.2 確定版) 第 7-1 / 第 13 章
- `/home/pico/pico_v3_docs/設計書_統合版_v4.3_素案.md` (素案) 第 13 / 第 24 章
**関連既存メモ:**
- `/home/pico/pico_v3_docs/docs/phase_d_design_memo.md` (2026-05-18 Phase C-1 時に作成、224 行、不変)
- `/home/pico/pico_v3/docs/phase_c4_migration_notes.md` (Phase C-4 完了報告)
- `/home/pico/pico_v3/docs/phase_c3_migration_notes.md` (Phase C-3 完了報告)

---

## 節 0: メタ情報

### 0-1. 本書の目的

Phase D「Discord 統合」着手時に Claude Code (planner/generator/evaluator) が
**ステップごとに実行する手順** を、設計書 13 章のコード断片と既存スケルトン
(Phase C-1 完了時点) との突合まで含めて確定させること。

### 0-2. スコープ (本書が扱う)

- Phase D サブステップ D-1 〜 D-7 の **詳細設計** (擬似コード骨子、テスト方針、完了条件)
- 既存 `discord_bridge/` 5 ファイル (Phase C-1 完了済) と設計書のコード断片の突合
- Phase D 着手前にカイニットが手動で済ませる事前準備チェックリスト
- リスク R-1 〜 R-7 の本文化と対処方針
- systemd unit ファイルの骨子 (`pico_v3.service`)

### 0-3. 非スコープ (本書が扱わない)

- **実装行為一切** (`uv add` / `apt install` / systemd 設定 / コード変更 / `.env` 編集)
- ReAct loop 全体のリファクタ (案 B として両論併記するのみ、決定は Phase D 本実装時)
- Phase E (感情 3 値) 以降の差分実装
- Phase D 着手後の本番接続検証 (帰宅後の人間作業領域)
- discord-ext-voice-recv の aarch64 wheel 確認 (Phase D-1 実機作業)
- VTube Studio / OBS 統合 (Phase K)

### 0-4. 本書と関連メモの役割分離

**重要**: 既存 `phase_d_design_memo.md` (2026-05-18) と本書 `phase_d_implementation_plan.md`
(2026-05-20) は **役割が異なる**。詳細は次節 (節 1) を参照。

---

## 節 1: 役割切り分け宣言 (既存メモとの関係)

| 文書 | 役割 | 作成日 | 行数 | 状態 |
|---|---|---|---|---|
| `phase_d_design_memo.md` | **概念・方針メモ** (なぜ Phase D が必要か、何を準備するか、リスク事前考察) | 2026-05-18 | 224 | 不変 (参照のみ) |
| `phase_d_implementation_plan.md` (本書) | **実装ステップ詳細** (D-1〜D-7、骨子コード、テスト件数目安、systemd unit ファイル骨子) | 2026-05-20 | 約 400 | 本書 |

### 1-1. 重複を避ける運用ルール

- 既存メモ (`phase_d_design_memo.md`) で「考察済み」の項目は本書から **再記述しない**。
  代わりに「→ `phase_d_design_memo.md` 第 N 節を参照」と引用する。
- 例: voice 受信ライブラリの選定理由 (discord-ext-voice-recv vs disnake) は
  既存メモ 2-2 節で完結 → 本書 D-1 / D-4 では「採用確定として扱う」のみ。
- 設計書のコード断片との突合 (節 5) と systemd 骨子 (D-6) は本書が初出。

### 1-2. 改訂責任

- 既存メモ `phase_d_design_memo.md` は **2026-05-18 時点の意思決定の記録** として
  そのまま保存する (rewrite history 禁止)。
- 本書 `phase_d_implementation_plan.md` は Phase D 着手中に修正・追記してよい
  (但し commit ごとに reason をコミットメッセージに残す)。

---

## 節 2: Phase C-1 完了時点のスケルトン棚卸し

Phase C-1 (2026-05-18 commit、Phase C-3/C-4 で軽微な調整あり) で
`src/pico_agent/discord_bridge/` に置かれた 5 ファイルの現状。

| ファイル | LOC | 公開シンボル | 現状実装済 | Phase D で本実装 | 触らない箇所 |
|---|---|---|---|---|---|
| `__init__.py` | 26 | `PicoBot` / `TextChannelHandler` / `VoiceChannelListener` / `DiscordDisabledError` / `is_discord_available` | 公開シンボル束ね、`last_import_error` は **非公開** (R-7) | `last_import_error` を `__all__` 追加するか判断 | 既存 5 シンボルの公開名 |
| `availability.py` | 53 | `is_discord_available()` / `last_import_error()` / `DiscordDisabledError` | discord.py 動的 import 試行、最終 import error の repr 保持 | discord.py 追加後の True 返却動作確認のみ (コード変更なし想定) | `_DISCORD_PY_IMPORT_ERROR` モジュールグローバルの更新規約 |
| `bot.py` | 347 | `PicoBot` クラス、`_load_bot_token` / `_load_owner_id` / `_load_guild_id` / `_build_intents` / `_build_client` | `PicoBot.start()` / `stop()` / `start_in_background()` / `send_message()` / `_attach_event_handlers()` まで実装済。テスト時 mock 想定 | D-2 で **実 Discord 接続の動作確認** (mock 解除して実 token で start)。`_build_client` は `_attach_event_handlers` への統合検討 (TODO コメント済) | `_load_*` 系の環境変数読みフォーマット (Phase H で `load_secret()` 化予定) |
| `text_channel.py` | 139 | `IncomingMessage` dataclass、`TextChannelHandler.should_handle()` / `handle()`、`incoming_message_from_discord()` | boundary 判定 (OWNER/GUILD)、`discord.Message → IncomingMessage` 変換 (duck-typing) | D-3 で **ReAct loop 接続 (案 A コールバック層)**。`on_input` callback に `DiscordReactBridge.on_text` を渡す | `IncomingMessage` のフィールド構成 (5 個固定)、`should_handle` の 4 ルール |
| `voice_channel.py` | 290 | `VoiceChannelState` Enum、`VoiceChannelListener.join()` / `leave()` / `speak()` / `_on_audio_chunk()` / `_play_audio_via_voice_client()` | 状態遷移 (5 値) + STT/TTS callable DI + tempfile WAV 再生骨子 | D-4 で **voice-recv 統合**: `PicoAudioSink` 新設、`_on_audio_chunk` の呼び出し側を実装。VAD は Phase C-4 の silencedetect 経路を WAV bytes 変換後に再利用 | 状態遷移の不変条件 (LISTENING ⇆ SPEAKING を try/finally で復元)、`_play_audio_via_voice_client` の tempfile 戦略 |

**合計 LOC**: 855 (テスト 57 件を `tests/test_discord_bridge.py` に保持、現在 pytest 全体 1258 件)

### 2-1. 棚卸しから読み取れる進捗評価

- **想定より進んでいる**: `bot.py` 347 行・`voice_channel.py` 290 行は Phase C-1 の
  「骨組み」想定 (200 行/ファイル) を超えている。実装の大半は完了しており、
  Phase D で残るのは「実 Discord 接続の動作確認」「voice-recv 統合の最後の 1 マイル」
  「ReAct loop の橋渡し」の 3 点。これは R-2 として節 6 に展開する。
- **二層分離 (24-4) 遵守**: すべて `pico_agent/discord_bridge/` 配下、
  `familiar_agent/` への侵入なし。Phase D も同様の方針を継続する (R-6)。

---

## 節 3: 依存ライブラリ判断 (本タスクでは uv add しない)

**重要**: 本タスク (タスク 6) では `uv add` / `apt install` 等の **実装行為は禁止**。
本節は Phase D-1 で実行する **判断と根拠の記録** のみ。

### 3-1. Python 必須依存

| パッケージ | 用途 | バージョン指定 | aarch64 wheel |
|---|---|---|---|
| `discord.py` | Discord クライアント本体 | `>=2.4` | 確実に存在 (公式 PyPI 配布) |
| `PyNaCl` | VC 音声暗号化 | discord.py が要求 (自動解決想定) | 存在するが build 要の可能性、apt の `libsodium-dev` で対応 |
| `discord-ext-voice-recv` | VC 音声受信 (Imayhaveborkedit fork) | `>=0.5` 系 | **未確認** (R-3 に詳述) |

### 3-2. 採用 uv add コマンド (Phase D-1 で実行する命令)

```bash
# 第 1 候補 (voice extras 一括 + voice-recv)
uv add 'discord.py[voice]' discord-ext-voice-recv

# voice extras が aarch64 で wheel build 失敗した場合のフォールバック分岐 1
uv add discord.py
sudo apt install -y libffi-dev libsodium-dev libopus0 libopus-dev
uv add PyNaCl
uv add discord-ext-voice-recv

# discord-ext-voice-recv が aarch64 で動かない場合のフォールバック分岐 2 (R-3)
# → Phase D-1 で判断ポイントとして停止し、カイニットに報告
# → disnake / py-cord 検討は本書では決定しない (素案にとどめる)
```

### 3-3. システム依存 (apt install)

| パッケージ | 用途 | Phase C 確認状況 |
|---|---|---|
| `ffmpeg` | VC 音声送受信のエンコード | **Phase C-3 (commit 43419a0) で `/usr/bin/ffmpeg` v7.1.3 確定済** |
| `libopus0` | Discord VC の Opus codec | 未確認 → D-1 で確認 |
| `libsodium-dev` | PyNaCl ビルド時 | 未確認 → D-1 で確認 |
| `libffi-dev` | PyNaCl ビルド時 | 多くの場合既存 |

### 3-4. uv add のタイミング規約

- Phase D-1 着手と **同時** に実行する (Phase D-2 以降は uv add 済前提)。
- `uv add` の commit メッセージは `chore(phase-d): add discord.py and voice-recv deps` 形式。
- `pyproject.toml` の依存セクションは **`[project.dependencies]` 直接ではなく**
  `[project.optional-dependencies] discord = [...]` を新設して `[tool.uv] default-groups`
  に `discord` を追加する案 (Phase C 系統に合わせる)。Phase D-1 着手時に再判断。

---

## 節 4: Phase D サブステップ詳細 (D-1 〜 D-7)

各ステップは planner → generator → evaluator のサブエージェント構成で実行。
所要は **1 セッション = 3 時間** を 1 単位として概算。

### 4-1. D-1: 依存追加とインフラ確認 (0.5 日)

#### (a) 目的
`discord.py` (voice extras 込み) と `discord-ext-voice-recv` を RPi5 aarch64 環境で
インストールし、`is_discord_available()` が True を返す状態を作る。
ffmpeg / libopus / libsodium の system 依存も確認。

#### (b) 触るファイル一覧 (絶対パス)
- `/home/pico/pico_v3/pyproject.toml` (依存追加)
- `/home/pico/pico_v3/uv.lock` (uv add 自動更新)
- (apt) `/var/lib/dpkg/status` への副作用のみ、リポジトリは触らない

#### (c) 実装内容の擬似コードレベル骨子

```bash
# 1. 既存 ffmpeg を再確認 (Phase C-3 で確定済)
which ffmpeg && ffmpeg -version | head -1   # /usr/bin/ffmpeg v7.1.3 想定

# 2. system 依存追加 (PyNaCl ビルド用)
sudo apt install -y libffi-dev libsodium-dev libopus0 libopus-dev

# 3. Python 依存追加
uv add 'discord.py[voice]' discord-ext-voice-recv

# 4. import 動作確認
uv run python -c "from pico_agent.discord_bridge.availability import is_discord_available; print(is_discord_available())"
# → True が期待値

uv run python -c "import discord.ext.voice_recv; print('voice-recv OK')"
# → 'voice-recv OK' が期待値、ImportError なら R-3 分岐
```

#### (d) テスト方針
- `tests/test_discord_bridge.py` 既存 57 件のうち、`is_discord_available()` を
  monkeypatch していない素のテスト (もしあれば) が True 系経路に乗ることを確認。
- 新規テスト追加目安: **0 件** (D-1 単体ではテスト追加不要、D-2 以降で追加)。

#### (e) 完了条件
1. `uv run python -c "import discord"` がエラーなく完了
2. `uv run python -c "from discord.ext import voice_recv"` がエラーなく完了
3. `is_discord_available()` が True を返す
4. pytest 1258 件 (+ α 件) がグリーン

#### (f) 所要時間目安 / rollback 方針
- 目安: 1〜2 時間 (wheel build が走った場合に時間が伸びる可能性)
- rollback: `uv remove discord.py discord-ext-voice-recv`、apt は残しても害なし

---

### 4-2. D-2: bot.py 接続検証 (0.5 日)

#### (a) 目的
`PicoBot.start()` を実 Discord Bot Token で起動し、`on_ready` イベントが
発火する (= ピコがログイン状態になる) ことを確認。`send_message()` で
OWNER のテキストチャンネルに「ピコ、起動しました」を送れることを確認。

#### (b) 触るファイル一覧
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/bot.py` (`_build_client` を `_attach_event_handlers` に統合する小規模修正、最大 20 行差分)
- `/home/pico/pico_v3/tests/test_discord_bridge.py` (mock テスト追加、+3 件目安)

#### (c) 実装内容の擬似コードレベル骨子

```python
# scripts/phase_d_smoke_bot.py (Phase D-2 で新規、commit せず scratch)
import asyncio
from pico_agent.discord_bridge import PicoBot

async def main():
    bot = PicoBot(on_text_message=None, on_voice_speech=None)
    if not bot.is_configured:
        raise SystemExit("DISCORD_TOKEN / OWNER_ID / GUILD_ID not set")
    task = await bot.start_in_background()
    await asyncio.sleep(5)  # on_ready 待ち
    if bot.is_running:
        sent = await bot.send_message(int(os.environ["DISCORD_OWNER_CHANNEL_ID"]),
                                       "ピコ、起動しました")
        print("sent =", sent)
    await bot.stop()

asyncio.run(main())
```

#### (d) テスト方針
- 既存 57 件 + 新規 **約 3 件** (`_build_client` 統合後の `_attach_event_handlers` 単体テスト、`is_configured` 真偽分岐)
- mock 戦略: `unittest.mock.AsyncMock` で `discord.Client.start` を patch、本物の WebSocket は張らない

#### (e) 完了条件
1. `scripts/phase_d_smoke_bot.py` 実機実行で「on_ready: connected as ピコ」がログに出る
2. Discord 上で OWNER チャンネルに「ピコ、起動しました」が **実際に届く**
3. pytest 1258 + α 件 (D-1 完了時件数 + 3 程度) グリーン

#### (f) 所要時間目安 / rollback 方針
- 目安: 2〜3 時間 (intent 設定ミス・OAuth 招待ミスのデバッグを含む)
- rollback: `bot.py` の git revert、`.env` の DISCORD_TOKEN は残してよい (実害なし)

---

### 4-3. D-3: text_channel ReAct loop 接続 — 案 A コールバック層 (1.0 日)

#### (a) 目的
`TextChannelHandler.on_input` callback に **`EmbodiedAgent.run`** を呼ぶ
**`DiscordReactBridge`** を新設し、OWNER が Discord でテキスト発言すると
ピコが応答テキストを返すフローを確立する。
`EmbodiedAgent.run` のシグネチャ (`user_input: str, ...`) には **手を加えない**
(案 A の核心、二層分離 24-4 遵守)。

#### (b) 触るファイル一覧
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/react_bridge.py` (**新規**、約 80 行)
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/__init__.py` (`DiscordReactBridge` 公開シンボル追加)
- `/home/pico/pico_v3/tests/test_discord_react_bridge.py` (**新規**、+8 件目安)

#### (c) 実装内容の擬似コードレベル骨子

```python
# pico_agent/discord_bridge/react_bridge.py (新規)
"""Discord 入力を ReAct loop (EmbodiedAgent.run) に橋渡しするコールバック層。

設計書 v4.3 第 24-4 章「侵入点 (最小限の介入)」遵守:
    - EmbodiedAgent.run のシグネチャに source kwarg を **追加しない**
    - source 識別 (discord_text / discord_voice) はこのブリッジ層で保持
    - 既存 7 ファイル侵入リストを増やさない
"""
from __future__ import annotations
from typing import Optional
from loguru import logger
from familiar_agent.agent import EmbodiedAgent

class DiscordReactBridge:
    """Discord ⇆ ReAct loop の橋渡し。

    Args:
        agent: EmbodiedAgent インスタンス (run.sh / main.py で生成済を共有)。
        source_tag: ログ / metrics 用の source 識別 ("discord_text" / "discord_voice")。
    """
    def __init__(self, agent: EmbodiedAgent, source_tag: str = "discord_text") -> None:
        self.agent = agent
        self.source_tag = source_tag

    async def on_text(self, user_id: str, content: str) -> str:
        """TextChannelHandler.on_input から呼ばれるコールバック。"""
        logger.info("discord_bridge.{}: user_id={} content={!r}",
                    self.source_tag, user_id, content[:50])
        try:
            response = await self.agent.run(user_input=content)
            return response or ""
        except Exception as exc:  # noqa: BLE001
            logger.error("DiscordReactBridge.on_text failed: {}", exc)
            return ""

    async def on_voice(self, user_id: str, transcript: str) -> str:
        """VoiceChannelListener.on_speech_detected から呼ばれるコールバック (D-4 で使用)。"""
        # source_tag を "discord_voice" に上書きしてログ識別
        prev_tag, self.source_tag = self.source_tag, "discord_voice"
        try:
            return await self.on_text(user_id, transcript)
        finally:
            self.source_tag = prev_tag

# main.py 側での接続例 (D-3 完了後の統合は run.sh / familiar_agent/main.py で実施):
#   agent = EmbodiedAgent(...)
#   bridge = DiscordReactBridge(agent)
#   bot = PicoBot(on_text_message=bridge.on_text, on_voice_speech=bridge.on_voice)
#   await bot.start()
```

#### (d) テスト方針
- 新規 `tests/test_discord_react_bridge.py` **+8 件目安**:
  1. `DiscordReactBridge.on_text` が `agent.run` を `user_input=content` で呼ぶ
  2. `agent.run` が空文字を返したらブリッジも空文字を返す
  3. `agent.run` 例外時にブリッジが空文字を返す (raise しない)
  4. `source_tag` が "discord_text" の時のログ出力検証
  5. `on_voice` 経由だと `source_tag` が "discord_voice" に切り替わる
  6. `on_voice` 完了後に `source_tag` が元に戻る
  7. `user_id` がログに含まれる
  8. `content[:50]` で長文ログ切り詰め確認
- mock 戦略: `EmbodiedAgent` を `AsyncMock(spec=EmbodiedAgent)` で完全 mock、`agent.run` の副作用は触らない

#### (e) 完了条件
1. `DiscordReactBridge` 新設、`__init__.py` で公開
2. `tests/test_discord_react_bridge.py` 8 件全グリーン
3. pytest 全体 1258 + α + 8 件以上グリーン
4. **実機検証**: `scripts/phase_d_smoke_bridge.py` 経由で「Discord でカイニットが『おはよう』と打つ → ピコが応答」が成立

#### (f) 所要時間目安 / rollback 方針
- 目安: 5〜6 時間
- rollback: `react_bridge.py` 削除、`__init__.py` の追加行 revert

---

### 4-4. D-4: voice_channel VAD + voice-recv 統合 (1.5 日)

#### (a) 目的
`discord.ext.voice_recv` で Discord VC からの PCM chunk を受信し、
無音検出 (silencedetect) で発話区切りを判定して `VoiceChannelListener._on_audio_chunk`
を呼ぶフローを完成させる。

#### (b) 触るファイル一覧
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/voice_sink.py` (**新規**、約 120 行)
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/voice_channel.py` (`voice_sink.PicoAudioSink` を `join` 時にアタッチする呼び出しを追加、+15 行)
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/__init__.py` (`PicoAudioSink` 公開検討)
- `/home/pico/pico_v3/tests/test_discord_voice_sink.py` (**新規**、+10 件目安)

#### (c) 実装内容の擬似コードレベル骨子

```python
# pico_agent/discord_bridge/voice_sink.py (新規)
"""Discord VC 音声受信 sink (discord-ext-voice-recv 利用)。

Phase C-4 (commit 43419a0) で確定した silencedetect VAD 経路を Discord VC
入力にも適用する。WAV bytes 変換後に既存 stt_kotoba 経路と統合。

Phase C-4 との関係:
    - Phase C-4 は Tapo C210 RTSP → ffmpeg → stt_kotoba 経路 (RTSP subscription)
    - Phase D-4 は Discord VC → voice-recv PCM → WAV bytes → stt_kotoba (本書)
    - 共通: stt_kotoba.transcribe(wav_bytes) の HTTP POST 部分
    - 相違: 入力源 (RTSP プロセス vs voice-recv コールバック)
"""
from __future__ import annotations
from discord.ext import voice_recv  # type: ignore
from typing import Callable, Awaitable, Optional
import asyncio
import wave
import io

class PicoAudioSink(voice_recv.AudioSink):
    """ユーザー別 PCM バッファリング + 無音検出 → on_audio_chunk 呼び出し。

    Args:
        on_audio_chunk: ユーザー発話 1 区切りごとに呼ばれる callback。
            シグネチャ: ``async def(user_id: str, wav_bytes: bytes) -> None``
        loop: asyncio event loop (discord.py の VC は同期 callback で呼ばれるため必須)。
        silence_ms: 何 ms 無音が続いたら発話終了とみなすか (default 800ms)。
    """
    def __init__(
        self,
        on_audio_chunk: Callable[[str, bytes], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
        silence_ms: int = 800,
    ) -> None:
        super().__init__()
        self.on_audio_chunk = on_audio_chunk
        self.loop = loop
        self.silence_ms = silence_ms
        self._buffers: dict[int, bytearray] = {}
        self._last_voice_ts: dict[int, float] = {}

    def wants_opus(self) -> bool:
        return False  # PCM 16-bit 48kHz stereo を受け取る (discord.py 既定)

    def write(self, user, voice_data) -> None:
        """voice_recv が同期で呼ぶエントリポイント。"""
        if user is None:
            return
        uid = int(user.id)
        pcm = voice_data.pcm  # bytes, 48kHz 16-bit stereo
        self._buffers.setdefault(uid, bytearray()).extend(pcm)
        self._last_voice_ts[uid] = self.loop.time()
        # 無音判定は別 task で periodic に走らせる (cleanup は VoiceChannelListener 側)

    def cleanup(self) -> None:
        """voice-recv 終了時にバッファをフラッシュ。"""
        for uid, buf in self._buffers.items():
            if buf:
                wav_bytes = self._pcm_to_wav(bytes(buf))
                asyncio.run_coroutine_threadsafe(
                    self.on_audio_chunk(str(uid), wav_bytes), self.loop)
        self._buffers.clear()

    @staticmethod
    def _pcm_to_wav(pcm: bytes, rate: int = 48000) -> bytes:
        """48kHz/16-bit/stereo PCM を WAV bytes にラップ。"""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        return buf.getvalue()

# VoiceChannelListener.join() 末尾で:
#   sink = PicoAudioSink(
#       on_audio_chunk=self._on_audio_chunk_wrapper,
#       loop=asyncio.get_running_loop(),
#   )
#   self._voice_client.listen(sink)
#
# STT/TTS callable の DI 注入経路 (planner 計画書より):
#   self.stt_callable = pico_agent.adapters.stt_kotoba.transcribe   # WAV bytes → str
#   self.tts_callable = lambda text: pico_agent.adapters.tts_sbv2.speak(text, target="discord_vc")  # str → WAV bytes
```

#### (d) テスト方針
- 新規 `tests/test_discord_voice_sink.py` **+10 件目安**:
  1. `PicoAudioSink.write` で PCM がバッファに溜まる
  2. 複数 user_id で別バッファに分離される
  3. `cleanup` で全バッファが `on_audio_chunk` callback に流れる
  4. `_pcm_to_wav` の出力が `wave.open` で読める
  5. WAV のチャンネル数 / sample rate / bit depth が 2/48000/16 になる
  6. `wants_opus()` が False を返す
  7. `user=None` のとき write が no-op
  8. `silence_ms` パラメータが格納される
  9. `loop` 参照を保持する
  10. `asyncio.run_coroutine_threadsafe` 経由で coroutine が schedule される
- mock 戦略: `voice_recv.AudioSink` を import できる前提 (D-1 完了後)。voice_data は `MagicMock(pcm=b"\x00\x00...")` を渡す。

#### (e) 完了条件
1. `PicoAudioSink` 新設、voice-recv の AudioSink を継承
2. `VoiceChannelListener.join` から sink がアタッチされる
3. `tests/test_discord_voice_sink.py` 10 件全グリーン
4. pytest 全体グリーン (D-3 完了時件数 + 10 以上)
5. **実機検証**: カイニットが VC に入って発話 → ログに「STT transcript: ...」が出る
6. **応答検証**: 「ピコ、聞こえる？」→ TTS 音声が VC に返る (D-4 で TTS 出力経路も確認)

#### (f) 所要時間目安 / rollback 方針
- 目安: 8〜10 時間 (VC 接続デバッグ + VAD 調整)
- rollback: `voice_sink.py` 削除、`voice_channel.py` の変更 revert

---

### 4-5. D-5: ReAct loop 入力 source 拡張案検討 (0.5 日)

#### (a) 目的
設計書 v4.0 第 7-1 章 line 530「ReAct loop は familiar-ai のものをそのまま使い、
入力経路として『Discord テキスト』『Discord 音声』『内部欲求』の 3 つを受け付ける」
の **実装方針** を確定させる。

#### (b) 案 A / 案 B の比較 (両論併記)

##### 案 A: コールバック層で source 識別 (推奨 / 本書の確定方針)

- `DiscordReactBridge` (D-3 で実装) が `source_tag` を保持
- `EmbodiedAgent.run(user_input: str)` のシグネチャは **不変**
- 二層分離 24-4 遵守 (`familiar_agent/` への侵入を増やさない)
- 採用: **本書はこちらを推奨**

```python
# 案 A: 既存 EmbodiedAgent.run はそのまま、ブリッジ層で source を識別
bridge_text = DiscordReactBridge(agent, source_tag="discord_text")
bridge_voice = DiscordReactBridge(agent, source_tag="discord_voice")
```

##### 案 B: EmbodiedAgent.run に source kwarg を追加 (両論併記、不採用)

- `familiar_agent/agent.py` の `EmbodiedAgent.run` シグネチャに `source: str = "tui"` を追加
- 設計書 v4.3 第 24-4 章の侵入点 7 ファイルに **8 つ目** を増やす
- 上流 PR 還元の可能性は低い (Discord 特化)
- 不採用理由: 二層分離 24-4-3-1 「`familiar_agent/` の大規模リファクタ禁止」に抵触

##### 判断

- **案 A 採用** (D-3 で `DiscordReactBridge` 新設済として進める)
- 案 B は Phase E (感情 3 値) で「source 別の appraisal 値変動」を実装する際に **再検討の余地あり** (本書では決定しない)

#### (c) 触るファイル一覧
- 本サブステップは設計判断のみ、コード変更 0
- D-3 完了済前提なら本サブステップは「確認のみ」で 0.5 日

#### (d) テスト方針
- 追加 0 件 (判断のみ)

#### (e) 完了条件
1. `phase_d_implementation_plan.md` (本書) の本節で案 A 採用と明示済
2. CHANGELOG_PICO.md に「Phase D-5: ReAct loop 接続は案 A コールバック層を採用」と追記
3. (任意) `familiar_agent/agent.py` の `EmbodiedAgent.run` docstring に「Discord 経由は `pico_agent.discord_bridge.react_bridge.DiscordReactBridge` 経由で呼ばれる」と一行追加

#### (f) 所要時間目安 / rollback 方針
- 目安: 1〜2 時間
- rollback: docstring 追記の revert のみ

---

### 4-6. D-6: pico_v3.service systemd unit 作成 (0.5 日)

#### (a) 目的
`pico_v3` を systemd で常駐起動する unit ファイルを作成し、`Restart=on-failure`
で異常終了からの自動復帰を確立。pico_v2 時代の `pico_v2.service` テンプレを参照
(R-5: 参照可否を D-6 着手時に確認)。

#### (b) 触るファイル一覧
- `/etc/systemd/system/pico_v3.service` (**新規**、約 30 行) ※sudo 必要、カイニット手動 or sudoers 設定要
- `/home/pico/pico_v3/run.sh` (ExecStart から呼ぶ、既存ファイルを再確認)
- `/home/pico/pico_v3/scripts/install_systemd.sh` (**新規** option、unit ファイル配置を自動化)

#### (c) systemd unit ファイル骨子

```ini
[Unit]
Description=pico_v3 (familiar-ai based)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=pico
Group=pico
WorkingDirectory=/home/pico/pico_v3
EnvironmentFile=/home/pico/pico_v3/.env
ExecStart=/home/pico/pico_v3/run.sh
Restart=on-failure
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=30
StandardOutput=append:/home/pico/pico_v3/logs/systemd.log
StandardError=append:/home/pico/pico_v3/logs/systemd.err

[Install]
WantedBy=multi-user.target
```

#### (c-2) 起動シーケンス想定 (run.sh 内)

```bash
#!/bin/bash
# (既存 run.sh の確認のみ、D-6 で大改造はしない)
cd /home/pico/pico_v3
exec uv run python -m familiar_agent.main
```

#### (d) テスト方針
- systemd 起動は単体テスト対象外 (本物の systemctl が必要)
- 代替: pytest で `pico_v3.service` ファイルの構文検証 (systemd-analyze verify 経由) を追加可
- 追加件数目安: **0 〜 2 件**

#### (e) 完了条件
1. `/etc/systemd/system/pico_v3.service` が配置される (sudo 操作)
2. `sudo systemctl daemon-reload` 実行
3. `sudo systemctl start pico_v3.service` で起動成功
4. `sudo systemctl status pico_v3.service` で active (running) を確認
5. `journalctl -u pico_v3.service -n 50` でエラーなし
6. **24 時間ステージング**: enable する前に手動 start で 24h 動作観察
7. 観察 OK なら `sudo systemctl enable pico_v3.service`
8. CLAUDE.md 完了条件 3 番:
   ```bash
   sudo systemctl restart pico_v3.service && sleep 60 && \
   grep -E "ERROR|WARNING" /home/pico/pico_v3/logs/pico_$(date +%Y-%m-%d).log | \
   grep -v "vtube_studio\|funasr\|trust_remote" | head -20
   ```
   の grep 結果が **空** であること

#### (f) 所要時間目安 / rollback 方針
- 目安: 2〜3 時間 (24h ステージング除く)
- rollback: `sudo systemctl disable pico_v3.service && sudo systemctl stop pico_v3.service && sudo rm /etc/systemd/system/pico_v3.service`

---

### 4-7. D-7: 完了確認と回帰テスト (0.5 日)

#### (a) 目的
Phase D 全体の完了確認。pytest 全件グリーン、systemd 再起動エラーなし、
Discord でテキスト + VC 両方の往復が成立、を最終確認。

#### (b) 触るファイル一覧
- `/home/pico/pico_v3/CHANGELOG_PICO.md` (Phase D 完了記録、+20 行目安)
- `/home/pico/pico_v3/docs/phase_d_migration_notes.md` (**新規**、約 150 行) ※Phase C-3/C-4 の `migration_notes.md` パターンに倣う

#### (c) 確認チェックリスト

```bash
# 1. pytest 全件グリーン
uv run pytest -q
# → 1258 件 + Phase D 追加分 (約 +25 件目安) がグリーン

# 2. systemd 再起動エラーなし (CLAUDE.md 完了条件 3)
sudo systemctl restart pico_v3.service && sleep 60 && \
grep -E "ERROR|WARNING" /home/pico/pico_v3/logs/pico_$(date +%Y-%m-%d).log | \
grep -v "vtube_studio\|funasr\|trust_remote" | head -20
# → 空であること

# 3. Discord テキスト往復
# → カイニットが「おはよう」と打つ → ピコが応答 (5 秒以内)

# 4. Discord VC 往復
# → カイニットが VC で発話 → STT → ピコ応答 → TTS で VC 再生

# 5. 24 時間連続稼働
# → journalctl -u pico_v3.service --since "24 hours ago" | grep -iE "error|fatal" が空
```

#### (d) テスト方針
- 既存テストの **回帰なし** を最優先 (件数を減らさない)
- D-7 自体で新規テスト追加は **約 5 件** (E2E mock テスト、`DiscordReactBridge` + `PicoAudioSink` の統合テスト)

#### (e) 完了条件 (Phase D 全体の完了条件と重なる、節 8 参照)
1. pytest 1258 + α 件 (Phase D 全体で +25 件目安) グリーン
2. 各 D-1〜D-6 のテストが揃っている
3. systemd 再起動エラーなし
4. Discord テキスト + VC 両方の往復成立
5. CHANGELOG_PICO.md / `phase_d_migration_notes.md` 更新

#### (f) 所要時間目安 / rollback 方針
- 目安: 2〜3 時間
- rollback: Phase D 全体 rollback は git tag `phase-d-start` (D-1 直前) に戻す

---

## 節 5: 設計書 v4.2 第 13 章コード断片 vs 既存スケルトン突合表

設計書 (v4.2 / v4.3 同内容) 第 13 章のコード断片と Phase C-1 で実装した
スケルトンの差分を確認。

### 5-1. 第 13-1 章 (テキストチャンネル)

| 設計書のコード断片 | 既存スケルトン (Phase C-1) | 差分 / Phase D 対応 |
|---|---|---|
| `@bot.event async def on_message(msg):` | `bot.py:_attach_event_handlers` 内に `@client.event async def on_message(msg)` | **等価** (デコレータ呼び出しを `_attach_event_handlers` でカプセル化) |
| `if msg.author.bot or msg.author.id != OWNER_ID and msg.guild_id != GUILD_ID: return` | `text_channel.py:TextChannelHandler.should_handle()` | **意味的に等価**、boundary が DI 化されているのが改善点 |
| `await react_loop.handle_input(source="discord_text", ...)` | 未実装 (Phase D-3 で `DiscordReactBridge.on_text` 経由で接続) | **Phase D-3 で接続**、案 A コールバック層採用 |

### 5-2. 第 13-2 章 (音声チャンネル)

| 設計書のコード断片 | 既存スケルトン (Phase C-1) | 差分 / Phase D 対応 |
|---|---|---|
| `class VoiceChannelListener: async def join(self, channel: VoiceChannel)` | `voice_channel.py:VoiceChannelListener.join(self, channel_id: int, client)` | **シグネチャ差**: 既存は `channel_id + client` を分離 (DI 化)、設計書は `channel` オブジェクトを直接受ける → 既存案の方が test 容易、Phase D で揃え直しは不要 |
| `async def leave(self): ...` | `voice_channel.py:VoiceChannelListener.leave()` | **等価** |
| `async def _on_audio_chunk(self, user, audio): text = await stt_kotoba.transcribe(audio); await react_loop.handle_input(source="discord_voice", user_id=str(user.id), content=text)` | `voice_channel.py:_on_audio_chunk(self, user_id: str, audio_bytes: bytes)`、`stt_callable` DI、`on_speech_detected` で react loop 接続 | **意味的に等価**、Phase D-3/D-4 で `on_speech_detected = DiscordReactBridge.on_voice` を接続 |

### 5-3. 結論

- 設計書 13 章のコード断片は **すでにスケルトンで等価実装済**
- Phase D で残るのは「DI 化されたパラメータの実 callable 注入」(D-3 / D-4) のみ
- 設計書の `react_loop.handle_input(source=..., ...)` は本書では **DiscordReactBridge** という
  別レイヤーで実現する (案 A、24-4 遵守)

---

## 節 6: リスク・想定外パターン (R-1 〜 R-7)

### R-1: 既存 design_memo との重複リスク

- **症状**: 既存 `phase_d_design_memo.md` (224 行) と本書が記述を重複させ、
  どちらが正かわからなくなる
- **対処**: 節 1 で **役割分離** を宣言済。既存メモは「2026-05-18 時点の決定の記録」、
  本書は「Phase D 着手中の実装計画 (修正可)」と明確化。
- **検知**: 本書を改訂する際に既存メモを書き換えてはいけない (rewrite history 禁止)。

### R-2: voice_channel.py が想定より進んでいる

- **症状**: Phase C-1 「骨組み」想定 (200 行/ファイル) に対し、`voice_channel.py` が
  290 行 + `bot.py` が 347 行と実装が想定より深い
- **対処**: Phase D は **「整合性確認パス」** として位置づけ直す。
  新規実装は voice-recv 統合 (`voice_sink.py`) と ReAct bridge (`react_bridge.py`)
  の 2 ファイルに絞り、既存スケルトンには小規模修正のみ加える。
- **副次効果**: Phase D の所要が 5 日 → 4 日に短縮できる見込み (planner で再見積)

### R-3: discord-ext-voice-recv の aarch64 wheel 未確認

- **症状**: `uv add discord-ext-voice-recv` で aarch64 wheel が出ず、source build が失敗
- **対処**:
  1. D-1 で `uv add` 実行 → wheel build が走るか確認
  2. 失敗時の分岐 1: PyPI fork の最新版 (`>=0.5.x`) を試す
  3. 失敗時の分岐 2: GitHub から直接 `uv add 'discord-ext-voice-recv @ git+https://...'`
  4. すべて失敗したらカイニットに「ここで判断が必要」と報告 → disnake / py-cord 採用を再検討
- **判断ポイント**: D-1 着手時に **1 時間以内** で結論を出す。長引いたらサブエージェント停止。

### R-4: ReAct loop 接続点が EmbodiedAgent.run

- **症状**: 設計書 13 章は `react_loop.handle_input(source=...)` という API を仮定しているが、
  実装は `EmbodiedAgent.run(user_input: str, ...)` のみで `source` 引数なし
- **対処**: 節 4-5 (D-5) で案 A / 案 B 両論併記、**案 A 採用**:
  - 案 A: `DiscordReactBridge` (コールバック層) で source を保持、`EmbodiedAgent.run` 不変
  - 案 B: `EmbodiedAgent.run(source="discord_text" / ...)` 追加 (24-4 侵入 8 ファイル目になり不採用)

### R-5: pico_v2.service テンプレ参照可否

- **症状**: pico_v2 退役時 (Phase 0、2026-05-16) に `pico_v2.service` を停止 + disable したが、
  ファイル自体が残っているかわからない
- **対処**: D-6 着手時に `ls /etc/systemd/system/pico_v2.service` で存在確認。
  存在しなければ本書の D-6 骨子 (節 4-6) をベースに新規作成。
- **代替**: pico_v2 のバックアップ tar.gz (Phase 0 でメイン PC に転送済) から
  `systemd/pico_v2.service` を取り出して参照する。

### R-6: 二層分離原則 (24-4) 遵守

- **症状**: Phase D で `familiar_agent/` への侵入が増え、上流マージのコンフリクトが爆発する
- **対処**: 案 A 採用 (R-4) により、`familiar_agent/` への侵入は **0 ファイル** で済む。
  Phase D での新規ファイルはすべて `pico_agent/discord_bridge/` 配下に置く。
- **検証**: D-7 で `git diff src/familiar_agent/` を確認、差分があれば revert 検討。

### R-7: last_import_error の __init__.py 未公開

- **症状**: `availability.py:last_import_error()` が `__init__.py:__all__` に
  含まれていない (現状 5 シンボル公開のみ)
- **対処**: Phase D-1 着手時に判断:
  - 公開すべき: `/status` コマンドで「discord.py 接続エラー理由」を表示するなら public
  - 非公開のまま: 内部診断専用なら `from .availability import last_import_error` の都度 import で十分
- **暫定**: D-1 では現状維持 (非公開)、D-2 で接続エラーログ要件が出たら公開を再検討。

---

## 節 7: カイニット帰宅後の事前準備チェックリスト

Phase D 着手前に **カイニット (人間)** が手動で済ませる必要がある作業。
Claude Code (Opus 4.7) が肩代わりできない領域。

### 7-1. Discord Developer Portal

1. https://discord.com/developers/applications にログイン (kainit800@gmail.com)
2. **New Application** → name: `pico_v3` (または `ピコ`) で作成
3. **Bot** → **Reset Token** → トークンを安全な場所に控える
4. **Privileged Gateway Intents** で以下を **ON**:
   - [x] PRESENCE INTENT (任意、ピコの presence を読むなら)
   - [x] SERVER MEMBERS INTENT (OWNER_ID 照合に必要)
   - [x] MESSAGE CONTENT INTENT (テキスト本文を読むのに必須)

### 7-2. OAuth2 招待 URL

1. **OAuth2** → **URL Generator**
2. **Scopes**: `bot`, `applications.commands` (任意)
3. **Bot Permissions**: `View Channels` / `Send Messages` / `Read Message History` /
   `Connect` (VC) / `Speak` (VC) / `Use Voice Activity`
4. 生成された URL をブラウザで開き、ピコ用 Discord サーバに招待

### 7-3. サーバ ID / OWNER ID 取得

1. Discord アプリで開発者モード ON (設定 → 詳細設定 → 開発者モード)
2. ピコ用サーバを右クリック → **サーバー ID をコピー** → `DISCORD_GUILD_ID`
3. カイニット自身のユーザー名を右クリック → **ユーザー ID をコピー** → `DISCORD_OWNER_ID`
4. ピコと話すテキストチャンネルを右クリック → **チャンネル ID をコピー** → `DISCORD_OWNER_CHANNEL_ID` (D-2 smoke test 用)

### 7-4. .env 投入 (Phase D-1 着手直前)

`/home/pico/pico_v3/.env` (Claude Code は触らない、カイニットが手動編集) に追記:

```bash
# Phase D 用 (planner タスク 6 計画書より)
DISCORD_TOKEN=...           # 7-1 で取得した Bot Token
DISCORD_OWNER_ID=...        # 7-3 で取得した OWNER の Discord user ID
DISCORD_GUILD_ID=...        # 7-3 で取得した Guild ID
DISCORD_OWNER_CHANNEL_ID=...  # D-2 smoke test 用、任意
# Phase H で load_secret() 経由に移行予定
```

### 7-5. pico_v2.service テンプレ参照確認 (D-6 用)

```bash
# pico_v2 退役時 (Phase 0) に残っていれば参考にできる
ls -la /etc/systemd/system/pico_v2.service 2>/dev/null
# 残っていない場合は Phase 0 でメイン PC に転送した tar.gz から取り出す
```

### 7-6. 確認チェックリスト (帰宅後 Phase D 着手前)

- [ ] Discord Developer Portal で Bot Token 取得済
- [ ] Privileged Intents 3 つ ON
- [ ] OAuth2 招待 URL でピコをサーバに招待済
- [ ] サーバ ID / OWNER ID / OWNER_CHANNEL_ID 控え済
- [ ] `.env` に DISCORD_TOKEN / DISCORD_OWNER_ID / DISCORD_GUILD_ID 追加済
- [ ] `pico_v2.service` テンプレ参照可否確認済 (D-6 用)
- [ ] (任意) Phase C-3 で確定した go2rtc 経路がまだ動くこと確認 (TTS スピーカー再生用)

---

## 節 8: 完了条件マッピング

### 8-1. Phase D 全体の完了条件 (設計書 v4.2 第 13 章 + 実装計画書 §3-3)

| # | 完了条件 | 達成サブ Phase | 検証方法 |
|---|---|---|---|
| 1 | Discord でテキスト発言 → ピコが応答 | D-3 | カイニット手動操作、5 秒以内の応答 |
| 2 | Discord VC で発話 → ピコが音声応答 | D-4 | カイニット手動操作、STT → TTS 経路 |
| 3 | pico_v3.service が systemd で常駐 | D-6 | `sudo systemctl status pico_v3.service` で active |
| 4 | 24 時間連続稼働でエラーなし | D-7 | `journalctl -u pico_v3.service --since "24h"` で error 行なし |
| 5 | OWNER 以外は応答しない (boundary 遵守) | D-3 | 別アカウントからの発言が無視される実機確認 |
| 6 | 二層分離 (24-4) 遵守 | D-7 | `git diff src/familiar_agent/` が空 |

### 8-2. CLAUDE.md 共通 3 条件

| # | 条件 | 達成方法 |
|---|---|---|
| 1 | pytest がグリーン (件数を減らさない) | 各 D-* のテスト追加で約 +25 件、最終 1283 件目安 |
| 2 | 各実装に対応するテストが存在する | D-2: +3 / D-3: +8 / D-4: +10 / D-6: +2 / D-7: +5 件目安 |
| 3 | systemd 再起動エラーなし | D-6 / D-7 で完了条件 3 番の grep 結果が空 |

### 8-3. 各サブ Phase 完了条件

→ 節 4 の各 D-N (e) を参照。

### 8-4. 本書 (タスク 6) の完了条件

| # | 条件 | 達成 |
|---|---|---|
| 1 | `/home/pico/pico_v3/docs/phase_d_implementation_plan.md` 新規作成 | 本書 |
| 2 | 節 0 〜 節 8 の 9 節完備 | 本書 |
| 3 | 既存 `phase_d_design_memo.md` 不変、本書から参照リンクあり | 節 0 / 節 1 |
| 4 | `uv add` / `apt install` / `systemctl` 等の実装行為なし | 本書はドキュメントのみ |
| 5 | pytest 件数 1258 件のまま | 本書はコード変更なし |
| 6 | 既存 `discord_bridge/` 5 ファイル無変更 | 本書はコード変更なし |

---

## 参照ファイル一覧 (絶対パス)

### 本書が参照する設計書
- `/home/pico/pico_v3_docs/設計書_統合版_v4_pico_v3.md` (v4.2 確定版、第 7-1 / 第 13 章)
- `/home/pico/pico_v3_docs/設計書_統合版_v4.3_素案.md` (素案、第 13 / 第 24 章)

### 本書が参照する既存ドキュメント
- `/home/pico/pico_v3_docs/docs/phase_d_design_memo.md` (2026-05-18 概念メモ、224 行、不変)
- `/home/pico/pico_v3/docs/phase_c3_migration_notes.md` (Phase C-3 完了報告)
- `/home/pico/pico_v3/docs/phase_c4_migration_notes.md` (Phase C-4 完了報告)
- `/home/pico/pico_v3/docs/phase_c4_vad_choice.md` (VAD 確定)
- `/home/pico/pico_v3/CHANGELOG_PICO.md`

### Phase D で参照する既存スケルトン
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/__init__.py` (26 行)
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/availability.py` (53 行)
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/bot.py` (347 行)
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/text_channel.py` (139 行)
- `/home/pico/pico_v3/src/pico_agent/discord_bridge/voice_channel.py` (290 行)
- `/home/pico/pico_v3/tests/test_discord_bridge.py` (57 件)

### Phase D で参照する Phase C-1 adapter
- `/home/pico/pico_v3/src/pico_agent/adapters/tts_sbv2.py` (TTS callable 注入元)
- `/home/pico/pico_v3/src/pico_agent/adapters/stt_kotoba.py` (STT callable 注入元)

### Phase D で参照する familiar_agent
- `/home/pico/pico_v3/src/familiar_agent/agent.py` (`EmbodiedAgent.run` 接続点、line 2329)

---

*Phase D implementation plan | 2026-05-20 | 夜間自律進行タスク 6*
*planner agentId: a1e8707e4e6ecfbbd / generator: Claude Code (Opus 4.7)*
