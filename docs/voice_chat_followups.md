# voice_chat.py Follow-ups

voice_chat.py 周辺で、サイクル 2 evaluator が指摘した P2 級項目で、即時修正対象外のもの。今後の TTS / playback 改善 TODO もここに足し込んでいく。

## P2 #2: TTS タイムアウト枯渇懸念 (2026-05-22)

### 現状
- `voice_chat.py:42` `DEFAULT_TTS_TIMEOUT = 60.0`
- `voice_chat.py` 内 `speak_to_tapo` は `aiohttp.ClientSession(timeout=ClientTimeout(total=DEFAULT_TTS_TIMEOUT))` で 1 session を全チャンク共有。
- 1 チャンク POST + playback wait (個別 GET は `total=2.0` で session timeout から切り離し済) のサイクルが累積する。

### 想定リスク
- 長文応答 (10+ チャンク) で session 全体 timeout 60s に到達 → `asyncio.TimeoutError` で残りチャンクが無音化
- リトライなしのため UX 劣化

### 対応候補
1. `TTS_TIMEOUT_SEC` 環境変数化 (`phase_c3_migration_notes.md` 既存項目との重複/拡張要確認)
2. チャンク文字数に応じた動的タイムアウト (例: `max(30, len(chunk) * 0.5)`)
3. session をチャンクごとに分割 (`async with aiohttp.ClientSession(...) as session:` をループ内に移動)
4. SBV2 側でストリーミング合成対応 (上流変更)

### 測定すべき指標
- 1 チャンクあたりの実合成時間分布 (P50/P95/P99)
- 60s 超過頻度 (実機ログから)

### 優先度
P2 / 次サイクル以降 / **実測データ取得が先**。理論先行で env を増やしても誤チューニングのリスク。

### 関連
- 元 evaluator レポート (サイクル 1, 2026-05-22)
- `voice_chat.py:283` `speak_to_tapo`
- `docs/phase_c3_migration_notes.md` (TTS 系 env キー)
