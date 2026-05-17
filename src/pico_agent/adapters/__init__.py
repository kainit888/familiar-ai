"""pico_v3 adapter 層: TTS / STT / Vision 外部サービス統一窓口。

設計書 v4.0 第 7-2 / 7-3 / 7-4 章に対応。familiar-ai 本体は ElevenLabs を
直接叩いていたが、pico_v3 では以下の外部サーバに振り分ける:

- TTS: Style-BERT-VITS2 (メイン PC 192.168.10.104:5000)
- STT: Kotoba-Whisper (メイン PC 192.168.10.104:8765)
- Vision: qwen3-vl on Ollama (メイン PC 192.168.10.104:11434)

各 adapter は **例外を raise せず** に silent fail + logger.warning する
(設計書「LLM 呼び出しは core/llm_router.py 経由のみ」「TTS/STT/Vision は
adapters/ 配下経由のみ」原則に従う)。
"""

from __future__ import annotations

__all__ = ["tts_sbv2", "stt_kotoba", "vision_qwen3vl"]
