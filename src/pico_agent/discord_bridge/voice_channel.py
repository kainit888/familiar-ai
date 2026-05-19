"""Discord 音声チャンネル常駐リスナ (設計書 v4.0 第 7-1 / 第 13-2 章)。

Phase D 着手時に discord.py の VoiceClient + discord-ext-voice-recv で
本実装する。Phase C-1 完了時点では状態遷移 (joined / listening / speaking)
とインターフェース定義のみ。
"""

from __future__ import annotations

from enum import Enum
from typing import Awaitable, Callable, Optional

from loguru import logger

from .availability import DiscordDisabledError, is_discord_available


class VoiceChannelState(Enum):
    """VC リスナの状態遷移。

    ::

        ┌──────────────┐
        │ DISCONNECTED │ ◄──────────────────────────────┐
        └──────┬───────┘                                │
               │ join(channel_id, client)               │
               ▼                                        │
        ┌──────────────┐                                │
        │   JOINING    │ ── 失敗 ──────────────────────►┤
        └──────┬───────┘                                │
               │ channel.connect() 成功                 │
               ▼                                        │
        ┌──────────────┐  ─── speak(text) ───►  ┌──────────────┐
        │  LISTENING   │                        │   SPEAKING   │
        └──────┬───────┘  ◄── finally ────────  └──────────────┘
               │ leave()
               ▼
        ┌──────────────┐
        │   LEAVING    │ ── disconnect 完了 ────────────┐
        └──────────────┘                                │
                                                        ▼
                                                (DISCONNECTED へ戻る)

    遷移の不変条件:
        - LISTENING ⇆ SPEAKING は ``speak()`` の try/finally で必ず復元される
        - LEAVING は ``leave()`` 内のごく短期 (disconnect の await 中) でのみ滞在
        - JOINING で失敗した場合は直接 DISCONNECTED に戻す (LEAVING は経由しない)
    """

    DISCONNECTED = "disconnected"
    JOINING = "joining"
    LISTENING = "listening"
    SPEAKING = "speaking"
    LEAVING = "leaving"


class VoiceChannelListener:
    """VC に常駐、VAD で発話検出 → STT → ReAct loop → TTS → VC 再生。

    Phase C-1 では「状態遷移と判定ロジックのみ」を実装し、actual な
    discord.VoiceClient 接続や音声 chunk 処理は Phase D に持ち越し。

    Args:
        on_speech_detected: 発話 (STT 後テキスト) を受け取って応答テキストを返す
            async callback。シグネチャ:
            ``async def on_speech(user_id: str, transcript: str) -> str``
        stt_callable: 音声 bytes を transcript に変換する async 関数 (デフォルトは
            stt_kotoba.transcribe)。
        tts_callable: 応答テキストから WAV bytes を生成する async 関数 (デフォルトは
            tts_sbv2.speak with target="discord_vc")。
    """

    def __init__(
        self,
        on_speech_detected: Optional[Callable[[str, str], Awaitable[str]]] = None,
        stt_callable: Optional[Callable[..., Awaitable[str]]] = None,
        tts_callable: Optional[Callable[..., Awaitable[bytes]]] = None,
    ) -> None:
        self.on_speech_detected = on_speech_detected
        self.stt_callable = stt_callable
        self.tts_callable = tts_callable
        self._state = VoiceChannelState.DISCONNECTED
        self._current_channel_id: Optional[int] = None
        self._voice_client = None  # discord.VoiceClient (Phase D)

    @property
    def state(self) -> VoiceChannelState:
        return self._state

    @property
    def current_channel_id(self) -> Optional[int]:
        return self._current_channel_id

    async def join(self, channel_id: int, client: Any = None) -> bool:
        """指定 VC に参加する (Phase D 実装版)。

        Args:
            channel_id: 接続先 Discord VC チャンネル ID。
            client: discord.Client インスタンス (PicoBot._client を渡す想定)。
                None なら DiscordDisabledError。
                型は ``Any`` だが、これは discord.py が **lazy import** されるため
                discord.Client を import 時点で参照できないことに起因する。実体は
                ``discord.Client`` (Phase D で uv add 済み)。Phase D 本実装で
                ``TYPE_CHECKING`` ブロック付きの Protocol 化を検討する。

        Returns:
            参加成功なら True、チャンネル取得失敗なら False。

        Raises:
            DiscordDisabledError: discord.py 未インストール or client 未指定。
        """
        if not is_discord_available():
            raise DiscordDisabledError(
                "discord.py is not installed; cannot join VC"
            )
        if client is None:
            raise DiscordDisabledError(
                "VoiceChannelListener.join: discord.Client instance is required"
            )
        if self._state != VoiceChannelState.DISCONNECTED:
            logger.warning(
                "voice_channel.join: already in state={}, leaving first",
                self._state.value,
            )
            await self.leave()

        self._state = VoiceChannelState.JOINING
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)
            if channel is None:
                logger.warning(
                    "voice_channel.join: channel_id={} not found",
                    channel_id,
                )
                self._state = VoiceChannelState.DISCONNECTED
                return False
            self._voice_client = await channel.connect()
            self._current_channel_id = channel_id
            self._state = VoiceChannelState.LISTENING
            logger.info(
                "voice_channel.join: connected to channel_id={}", channel_id
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "voice_channel.join: failed channel_id={} err={}",
                channel_id,
                exc,
            )
            self._state = VoiceChannelState.DISCONNECTED
            self._voice_client = None
            self._current_channel_id = None
            return False

    async def leave(self) -> None:
        """VC から退出する (Phase D 実装版)。

        voice_client があれば disconnect() を await、状態を DISCONNECTED に戻す。
        """
        if self._state == VoiceChannelState.DISCONNECTED:
            return
        self._state = VoiceChannelState.LEAVING
        if self._voice_client is not None:
            try:
                await self._voice_client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("voice_channel.leave: disconnect failed: {}", exc)
        self._current_channel_id = None
        self._voice_client = None
        self._state = VoiceChannelState.DISCONNECTED
        logger.info("voice_channel.leave: disconnected")

    async def speak(self, text: str) -> bool:
        """TTS で text を WAV にして VC に流す (Phase D 実装版)。

        voice_client が接続済みなら discord.FFmpegPCMAudio で再生。
        VAD 受信や送信中バッファ等の詳細は帰宅後の本番接続テストで詰める。

        状態遷移:
            前提: ``self._state == LISTENING`` でないと早期 return False
                  (ピコが既に喋っている / VC 未参加なら新規再生は受け付けない)。
            遷移: ``LISTENING → SPEAKING`` を try 開始直後にセットし、
                  ``finally`` で必ず ``SPEAKING → LISTENING`` に戻す。
                  TTS 失敗 / 再生失敗の例外パスでも復帰は保証される。

        Returns:
            再生成功なら True、VC 未接続 / TTS 失敗 / 再生失敗で False。
        """
        if self._state != VoiceChannelState.LISTENING:
            logger.warning(
                "voice_channel.speak: state={} (not LISTENING), skipping",
                self._state.value,
            )
            return False
        if self.tts_callable is None:
            logger.warning("voice_channel.speak: tts_callable not configured")
            return False

        try:
            self._state = VoiceChannelState.SPEAKING
            audio = await self.tts_callable(text)
            if not audio:
                return False

            if self._voice_client is None:
                logger.warning(
                    "voice_channel.speak: no voice_client, skipping playback "
                    "(would play {} bytes)",
                    len(audio),
                )
                return False

            # discord.py の FFmpegPCMAudio で WAV bytes をパイプ再生
            # 詳細は帰宅後の本番接続テストで詰める
            try:
                played = await self._play_audio_via_voice_client(audio)
                return played
            except Exception as exc:  # noqa: BLE001
                logger.warning("voice_channel.speak: playback failed: {}", exc)
                return False
        finally:
            self._state = VoiceChannelState.LISTENING

    async def _play_audio_via_voice_client(self, wav_bytes: bytes) -> bool:
        """discord.VoiceClient.play(AudioSource) で WAV を再生する内部メソッド。

        discord.FFmpegPCMAudio は外部 ffmpeg バイナリを起動するため、
        WAV bytes を一時ファイルに書き出してパスを渡す。

        Args:
            wav_bytes: 再生する WAV バイナリ。

        Returns:
            再生スケジュールに成功したら True。play() 自体は非同期で完了する
            ため、playback 完了まで wait しない (caller は別途待機を考える)。
        """
        if not is_discord_available():
            logger.warning(
                "voice_channel._play_audio: discord.py not available"
            )
            return False
        import discord  # type: ignore
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp_path = f.name

        try:
            source = discord.FFmpegPCMAudio(tmp_path)
            self._voice_client.play(source)
            # play() は同期 schedule、completion 検知は voice_client.is_playing() で
            # ループする方式が標準だが、テスト容易性のため Schedule のみで True を返す
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("voice_channel._play_audio: play() failed: {}", exc)
            return False
        finally:
            # tmp_path は再生中ロックされる可能性があるので即削除はしない
            # (Phase D 帰宅後のテストで cleanup 戦略を決める)
            pass

    async def _on_audio_chunk(self, user_id: str, audio_bytes: bytes) -> None:
        """VAD トリガで STT へ流す内部ハンドラ (Phase D で呼び出される)。

        Phase C-1 では「stt_callable を呼んで on_speech_detected を呼ぶ」
        フローのテストだけ可能。

        Phase D 持ち越し範囲 (本実装で追加するもの):
            - discord-ext-voice-recv による PCM chunk 受信フックの実装
              (この関数を呼び出す側)。現状は単体テストから直接 await して
              フローを検証するのみ。
            - user_id 別バッファリング (複数人 VC 想定、現状は単一発話前提)。
            - VAD (silero / webrtcvad) と無音判定 (stt_kotoba と共通化検討)。
            - 同一発話中の重複転写抑制 (50% overlap window 等)。
        """
        if self.stt_callable is None:
            logger.warning("voice_channel._on_audio_chunk: stt_callable not configured")
            return
        transcript = await self.stt_callable(audio_bytes)
        if not transcript or not transcript.strip():
            return
        if self.on_speech_detected is None:
            logger.debug("voice_channel: no on_speech_detected callback")
            return
        response = await self.on_speech_detected(user_id, transcript)
        if response:
            await self.speak(response)
