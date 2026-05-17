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

    DISCONNECTED → JOINING → LISTENING → SPEAKING ⇆ LISTENING → LEAVING
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

    async def join(self, channel_id: int) -> bool:
        """指定 VC に参加する。

        Phase C-1: discord.py 未インストールなら DiscordDisabledError。
        Phase D: 実 discord.VoiceClient で接続する。

        Returns:
            参加成功なら True。

        Raises:
            DiscordDisabledError: Phase C-1 完了時点では常に発火 (Phase D で本実装)。
        """
        if not is_discord_available():
            raise DiscordDisabledError(
                "discord.py is not installed; cannot join VC"
            )
        # Phase D で実装。ここに到達したら Phase D で詰める。
        raise DiscordDisabledError(
            "VoiceChannelListener.join() implementation pending (Phase D)"
        )

    async def leave(self) -> None:
        """VC から退出する (Phase C-1 では no-op)。"""
        if self._state == VoiceChannelState.DISCONNECTED:
            return
        self._state = VoiceChannelState.LEAVING
        self._current_channel_id = None
        self._voice_client = None
        self._state = VoiceChannelState.DISCONNECTED
        logger.info("voice_channel.leave() called (Phase D pending)")

    async def speak(self, text: str) -> bool:
        """TTS で text を WAV にして VC に流す。

        Phase C-1: 状態遷移チェックと tts_callable 呼び出しまで実装、
        実際の VC 再生は Phase D に持ち越し。

        Returns:
            再生成功なら True。VC 未接続や tts 失敗なら False。
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
            # Phase D: discord.VoiceClient.play(AudioSource) を呼ぶ
            logger.info(
                "voice_channel.speak: would play {} bytes via VC "
                "(Phase D pending)",
                len(audio),
            )
            return True
        finally:
            self._state = VoiceChannelState.LISTENING

    async def _on_audio_chunk(self, user_id: str, audio_bytes: bytes) -> None:
        """VAD トリガで STT へ流す内部ハンドラ (Phase D で呼び出される)。

        Phase C-1 では「stt_callable を呼んで on_speech_detected を呼ぶ」
        フローのテストだけ可能。
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
