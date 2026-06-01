# pico_v3 fork — Changelog

ピコ独自実装 (pico_v3) の changelog。上流 [familiar-ai](https://github.com/lifemate-ai/familiar-ai)
からの **差分のみ** を記録する。上流の changelog は `CHANGELOG.md` を参照。

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

---

## [Unreleased] — Stage 2 Phase C-11 完了 (2026-05-26)

### 技術債清算 (2026-06-01): qwen2.5:1.5b 運用停止

Phase C-11 で utility 経路を Pi5 LiteRT Gemma 4 E2B (localhost:11435) に本番
カットオーバー後、並列維持していた Ollama `qwen2.5:1.5b` を削除 (VRAM 整理 +
起動短縮)。ランタイムは既に `UTILITY_MODEL=gemma-4-e2b` で稼働しており、
qwen2.5:1.5b への active な依存は無い (参照はコメント/docstring/テスト fixture
文字列のみ)。

- 実機: `ollama rm qwen2.5:1.5b` (986MB)。他モデル (moondream / qwen2.5-coder
  1.5b・3b / qwen2.5:3b) は据置。
- `.env.example`: Ollama 利用例の `UTILITY_MODEL`/`SCENE_MODEL` を
  `qwen2.5:1.5b` → `qwen2.5:3b` (まだ存在) へ更新。
- コード/テスト変更なし。必要なら `ollama pull qwen2.5:1.5b` で再導入可。

### Problem-1 後処理 (2026-06-01): 既存記憶 DB の誤認監査スクリプト

**目的**: Problem-1 修正 (e59abba) 前に蓄積した vision 由来 identity 誤認の
observation を精査・是正する dev スクリプト。ランタイム無変更 (DB メンテ専用)。

**実データ所見 (重要)**: 実 DB (78 obs) を dry-run 監査した結果、**vision 由来
identity 誤認の候補は 0 件**。前向きの Problem-1 修正だけで十分だった。内訳:
not-vision 76 / stt-confirmed 1 (`4e5fad13` は画像有だが「聞こえてる？」と STT 確認
同時) / uncertainty 1 (`6c2cccfa` は「一緒に見てるのかな」と hedge 済)。「カイニットが
『いい/はい』と言って」系は STT 幻聴 (C-13) 由来で vision でないため対象外・keep。

**実装** (`scripts/dev/cleanup_observations_problem1.py`、stdlib のみ):
- 純関数 `classify_row`: 保守的順序 (非vision / companion非断定 / 不確実性 /
  STT確認 → keep、それ以外 → rewrite)。hedge 済み記憶を誤って書き換えない。
- 既定 `--dry-run` (DB 無変更)。`--apply` で実行、変更前に **SQLite backup API**
  で WAL-safe スナップショット (`observations.db.bak_problem1_<date>`、cp は使わない)。
- 既定 `--mode rewrite` (companion 名→中立ラベル上書き、非破壊: obs_embeddings/
  memory_links の CASCADE を避け記憶・リンク・埋め込みを保持)。`--mode remove` も可。
- `--companion`/`--label`/`--backup-suffix` 設定可。
- テスト +6 (`test_cleanup_observations_problem1.py`: classify 各種 / dry-run 無変更 /
  rewrite / remove / STT・uncertainty 保持 / backup 先行作成)。pytest 1547 → 1553 緑。

**実 DB への `--apply` は現状 no-op** (0 候補)。範囲外: STT 幻聴由来誤記憶、
self_narrative.jsonl、顔認識実装。

### Problem-1 修正 (2026-06-01): 視覚 identity の誤認 (無条件「カイニット」)

**原因**: 顔認識が無く、`agent.py:873` が `ToMTool(default_person=config.companion_name)`
で ToM のデフォルト人物を companion 名に固定 + LLM が ME.md/companion 文脈に誘導され、
カメラに映った誰でも「あ、カイニットだ」と決め打ちしていた (Phase X Stage A 実機で発覚)。
`see()` は identity を出さない。Phase X とは独立の既存バグ。

**修正** (`familiar_agent`、最小侵入。顔認識実装は範囲外):
- `config.AgentConfig` に 2 フィールド追加: `tom_default_person`
  (env `TOM_DEFAULT_PERSON_LABEL`、既定 `"unknown_person"`) / `face_recognition_enabled`
  (env `FACE_RECOGNITION_ENABLED`、既定 false、将来の顔認識用予約)。
- `resolve_tom_default_person()`: フラグ true でも認識器が無いので **warning + ラベルを返す**
  (companion_name へは戻さない = バグ再発防止)。
- `agent.py:873`: `default_person=config.companion_name` → `config.resolve_tom_default_person()`。
- `agent.py:1450`: system prompt の stable 部に `_t("identity_uncertainty_guidance")` を注入。
- locale `identity_uncertainty_guidance` を ja/en に追加: 「人物を断定するな / 同居でも
  相手本人と決めつけるな / ただし会話・名乗り(『ただいま』『私だよ』)・時間帯・声から
  **文脈推論してよい** / 不確実なら『誰かいる』とだけ言う」。他言語は en フォールバック。
- `companion_name` の他 28 箇所 (UI 表示 / desires の owner 関係 / config) は **無変更**
  (視覚 identity ではない、grep 確認済)。`tom.py` も無変更 (本番ラベルは resolver 経由)。
- テスト +8 (`test_tom_default_person.py`: ラベル既定/env 上書き/companion_name 不使用/
  フラグ既定false/フラグtrueでもラベル+warning/locale 識別不確実性・文脈推論・en フォールバック)。
  pytest 1539 → 1547 緑、regression なし。

**実機検証 (Stage D)**: ピコが無条件「カイニットだ」でなく「誰かいる」or 挨拶文脈から
カイニット推論する確認。既存記憶 DB の誤認データ cleanup は別タスク (範囲外)。

### Phase X Stage C (2026-05-31): Tapo ONVIF event 受信層

**目的**: Tapo C210 自身の motion/person 検知 (ONVIF event) を視覚自走の
bottom-up trigger に使う。polling せず、「event ごとに喋れ」とも命令しない。

**設計**: STT 常時購読と同型。新 `src/pico_agent/adapters/tapo_event.py` が
ONVIF event を受信し `on_event(TapoEvent)` callback へ流す。**軽量方式 (D1)**:
event → familiar_agent 側で `desires.boost("look_around", 0.25, visual=True)`
(person は `greet_companion` も) → 次の idle desire tick で Stage B の視覚変化
prompt が立ち、ピコが自分で see/判断 (同期 see/scene.update は呼ばない)。Tapo=
知覚、desire=注意、LLM=判断 の三層分離。

**実装** (`tapo_event.py`、pico_agent 完結・familiar_agent 非 import):
- `start_event_subscription(on_event, *, host=None) -> asyncio.Task`。依存欠落 /
  mode=disabled / webhook で host 未設定 なら no-op task (raise しない)。
- `TapoEvent` dataclass (event_type / timestamp=Pi受信時刻 / topic / raw)。
- モード (D2/D3、`TAPO_EVENT_MODE` 既定 **pullpoint=ON**):
  - **pullpoint**: `create_pullpoint_manager`(TTL 自動更新) → `PullMessages` ループ。
  - **webhook**: Pi 側 aiohttp サーバ + `create_notification_manager` で push 購読
    (PullPoint が firmware で不動な C210 向けフォールバック)。`TAPO_EVENT_WEBHOOK_HOST` 必須。
  - **disabled**: no-op。
- `_classify(topic, simple_items)` 純関数 (person 優先、motion 立下りは無視)、
  `_parse_soap_notifications` (webhook SOAP)、`_extract_*` (zeep 防御抽出)。
- 30s debounce (`TAPO_EVENT_DEBOUNCE_SEC`)、person_only filter、reconnect backoff、
  callback 例外を握って購読継続 (STT 同型)。
- **TUI 配線** (`tui.py`、5 点 mirror STT): import / `_tapo_events_enabled_default` /
  `__init__` task field / on_mount worker / `_on_tapo_event`+`_start`+`_stop` /
  action_quit で cancel。`_on_tapo_event` が `desires.boost(visual=True)`。
- env: `TAPO_EVENT_MODE`/`DEBOUNCE_SEC=30`/`PERSON_ONLY=false`/`BOOST_AMOUNT=0.25`/
  webhook 用 `WEBHOOK_HOST`/`PORT`。CAMERA_* 既存再利用。`.env.example` 追記。
- テスト +31 (`test_tapo_event.py` 22: classify/SOAP/debounce/emit/no-op/PullPoint mock、
  `test_tui_tapo_event.py` 9: 配線/boost visual=True/disabled ゲート)。1508 → 1539 緑。

**実機検証 (Stage D) 必須** (unit test は ONVIF を mock で回避):
- C210 が PullPoint を実確立し message 配信するか (firmware 依存、不可なら webhook)
- 実際の ONVIF Topic 文字列 / person event 対応有無 (`TapoEvent.topic` から採取)
- 誤発火頻度 → DEBOUNCE / PERSON_ONLY / BOOST_AMOUNT で調整

### Phase X Problem-2 修正 B (2026-05-31): say blind-retry 抑止

**修正** (`familiar_agent/agent.py` ReAct ループ、agent.py 完結):
- per-turn ローカル `timed_out_tools: set[str]` を導入 (run() ごと=1ターンごとに
  リセット → ターン跨ぎは無影響)。tool が `asyncio.TimeoutError` で timeout したら
  その tool 名を記録。
- 同一ターン内で既に timeout した tool が再呼出されたら **実行せず** 終端結果
  「Tool X timed out earlier this turn and was not retried. Do not retry…」を返す
  (tool_use ごとに tool_result を必ず対にする API 制約を満たすため collected に append)。
- **tool 名 key** で判定 (ハングはサーバ状態依存で入力非依存 → 同 tool の再試行は
  どのテキストでも再ハングする)。他 tool は無影響、timeout してない通常の複数 say は
  両方実行される。
- 修正 A (15s fast-fail) と二重防御: A で各試行が 15s で諦め、B で再試行自体を止める。
- テスト +4 (timeout 後同一 say 非実行 / 別 tool は実行 / 次ターンの say は通常実行 /
  timeout 無しの複数 say は両方実行)。pytest 1496 → 1508 緑、regression なし。

### Phase X Problem-2 修正 A (2026-05-31): SBV2 speak fast-fail

**背景**: Stage A 実機で「同一発話3連発」発生。原因は (A) `tts_sbv2.speak()` が
SBV2 ハング時 ~60s 無応答 → agent の 60s say ツール timeout → (B) LLM が同一 say を
blind retry。本コミットは A (TTS 層 fast-fail)。B (retry 抑止) は別コミット。

**修正** (`pico_agent/adapters/tts_sbv2.py`、pico_agent 完結):
- `_DEFAULT_TIMEOUT_SEC` 60→15。新 `_build_sbv2_timeout()` =
  `ClientTimeout(total=15, connect=5, sock_read=10)` を SBV2 `/voice` fetch
  (`_fetch_wav_parts`) と `_warmup_once` に適用。go2rtc POST は LAN なので plain total。
  **構造化 timeout なので `TTS_TIMEOUT_SEC` が大きくても connect/read で fast-fail する**
  (ハングは接続後の無受信 → sock_read=10s で諦める)。
- 新 env `TTS_CONNECT_TIMEOUT_SEC`(既定5) / `TTS_READ_TIMEOUT_SEC`(既定10)。
- `_fetch_one_chunk` に `except asyncio.TimeoutError` / `aiohttp.ClientConnectorError`
  を追加し WARNING `SBV2 timeout after Xs, returning empty wav` / `connection refused`。
  制御フロー不変 (空 parts → `speak()` は `EXIT sbv2_failed` で即 None)。
- `.env.example` に新 knob 追記 (既定 15)。`.env` 本体はカイニット手動だが、構造化
  timeout のため `TTS_TIMEOUT_SEC=60` のままでも fast-fail する。
- テスト +6 (default 15 / 構造化 timeout 値 / connect・read env override /
  speak timeout fast-fail / connection refused)。既存の 60.0 固定 assertion を 15.0 に更新。

### Phase X Stage B (2026-05-31): 視覚変化専用 inner_voice prompt

**目的**: scene/視覚変化が起点の desire turn のときだけ、専用 inner_voice prompt
(カイニット指定:「気になる事はありましたか？現在の状況を判断し、見えたものは
無視しても構いません。」=沈黙の自由を明示)を注入する。通常の look_around 等
(視覚起点でない)は既存の汎用 prompt のまま。

**マーカー設計** (`familiar_agent/desires.py`):
- `DesireSystem._visual_change_armed_at: dict[drive→time.time()]`。`boost()` に
  キーワード専用 `visual: bool=False` を追加し、`visual=True` の boost が
  (Stage A の disabled ガードを通過した上で) 当該 drive を arm する。
- `_consume_visual_change(name)` … `pop` で **1 回だけ消費** + `VISUAL_CHANGE_TTL_SECONDS`
  (30s) 以内のみ有効。古い arm は pop だけして無効扱い (後続ターンを誤誘導しない)。
- `dominant_as_prompt(consume_visual=True)` 先頭で dominant drive が consume
  できたら `_t("inner_voice_visual_change")` を返す (drive 汎用 prompt を **置換**)。
  `as_coalition()` は `consume_visual=False` で **peek** (one-shot を奪わない) —
  視覚 boost と workspace coalition が同一 `_run_post_response_pipeline` で走るため、
  consume すると次 idle tick の inner_voice が視覚 prompt を取り損ねる (evaluator 指摘)。
  置換理由: look_around の「see() で見てみる/無視しない」系が「無視しても構わない」
  と矛盾するため。`inner_voice_label`/`directive` のラップ (agent.py:1479) は不変。
- **per-drive キー**なので視覚起点でない dominant (rest / 沈黙タイムアウト greet 等)
  は専用 prompt を拾わない。`visual=False` 既定で既存 caller・1487 baseline は不変。

**visual=True を渡す箇所** (`familiar_agent/agent.py`、真に視覚起点の 3 経路):
- `look_around` novelty boost (agent.py:1010)
- `_react_to_scene_events`: person appeared → greet_companion / disappeared → worry_companion

**locale**: `inner_voice_visual_change` を `ja.json` + `en.json` に追加。他 85 言語は
`_t()` の en フォールバック (`_i18n.py:3224`)。i18n parity テストは `_T` キーに対する
missing チェックのみで、追加キーは安全。

**テスト** (+8): arm→専用prompt / マーカー無→汎用 / 1-shot consume / TTL staleness /
per-drive / disabled は arm されない / locale 解決 (ja+en) / scene-event 配線
(test_scene_greeting)。pytest 1487 → 1495 緑、regression なし。

**既知 blocker**: Stage A 実機検証で判明した Problem-2 (say タイムアウト→ReAct
リトライループ + TTS ハング、いずれも既存バグ) は別タスクで対処予定。inner_voice は
say の前に system prompt へ注入・ログされるため、Stage B prompt 検証は TTS 完了と
無関係に app.log grep で可能。

### Phase X Stage A (2026-05-31): auto_desire 解放 + 個別 drive 無効化フラグ

**目的**: 視覚自走 (Phase X) の最小 viable。既存の desire 自走モデル
(idle で drive が成長 → `get_dominant()` で発火 → `agent.run("", inner_voice=...)`)
を有効化する。Tapo event はまだ繋がない (Stage C)。master ゲート
`FAMILIAR_AUTO_DESIRE=true` + 暴走時に個別 drive を切れる仕組みを用意。

**設計書からの訂正 (実測)**:
- auto_desire の env は `AUTO_DESIRE` ではなく既存の **`FAMILIAR_AUTO_DESIRE`**
  (`config.py:237` で既に読込済) → config.py 改修不要。
- 個別フラグは設計書案の「`_react_to_scene_events` でチェック」では不十分。
  同関数は scene イベント由来 boost だけを扱い、`look_around`/`explore` の
  時間成長発火 (`tick()`→`get_dominant()`) を通らない。→ enforce は `desires.py`
  の選択経路に置く。

**実装** (`familiar_agent/desires.py` のみ、additive):
- `_parse_disabled_drives(raw)` … `FAMILIAR_DISABLED_DRIVES` のカンマリストを
  strip/lower/空捨てで `frozenset` 化。空入力 → 空集合 (= 全 drive 有効、既定)。
- `DesireSystem.__init__` に `disabled_drives` kwarg 追加 (明示指定優先、
  なければ env から)。`self._disabled_drives` 保持。
- `_effective_score()` 先頭で無効 drive は `return 0.0` → `get_dominant`・
  `as_coalition`・workspace・inner_voice prompt から一括除外 (1 箇所で全経路カバー)。
- `boost()` 先頭で無効 drive は no-op → scene イベント由来の加算も抑止。
- `tick()` は非ゲート (level は成長するがスコア 0 で不可視、hot loop 非介入)。
- config.py / main.py / tui.py / agent.py / `get_dominant`・`as_coalition` の
  シグネチャは無改修。kwarg 既定 None で既存 16 箇所の呼出は不変 → regression なし。

**環境変数** (`.env.example` に追記、`.env` 本体はカイニット手動):
- `FAMILIAR_AUTO_DESIRE=true` (自走有効化)
- `FAMILIAR_DISABLED_DRIVES=look_around,explore` (個別無効化、既定空)

**テスト** (`tests/test_desires.py` +7): 無効 drive が get_dominant に出ない
(level 強制 high でも) / boost no-op / coalition 除外 / 有効 drive は通常発火 /
既定で全有効 / env parse 正規化 / env 読込。mutation 対応明記。
pytest 1480 → 1487 緑、regression なし。

**範囲外** (後続 Stage): 視覚変化専用 inner_voice prompt (Stage B)、Tapo ONVIF
event 受信層 (Stage C)、実機誤発火チューニング (Stage D)。

### say 登録ゲートを独自 TTS 設定へ切替 (2026-05-31, Phase X 前段)

**背景**: `TTSTool.say()` は Phase C-1/C-5 で既に `pico_agent.adapters.tts_sbv2`
→ go2rtc → Tapo C210 経路に固定されており、ElevenLabs は dead code
(`self.api_key` 未使用)。にもかかわらず say ツールの**登録**条件は
`agent.py` `__init__` の `if tts.elevenlabs_api_key:` のままで、ElevenLabs
キーが無いと say がツール定義に現れなかった。Phase X(自発発話)で
`.env` から `ELEVENLABS_API_KEY` を外しても声を保てるよう、ゲートを
独自 TTS 経路 (go2rtc) の有無へ切り替える。

**修正**:
- `config.TTSConfig` に `has_voice_output() -> bool` を追加。
  `bool(self.elevenlabs_api_key or self.go2rtc_url)` を返す。go2rtc_url は
  既定 `http://localhost:1984` で非空のため、実質 say は常に登録される
  (`GO2RTC_URL=""` を明示した場合のみ無効)。ElevenLabs キーは upstream
  (backend-agnostic) 互換のため OR の片側として維持。
- `agent.py` の登録条件を `if tts.elevenlabs_api_key:` →
  `if tts.has_voice_output():` へ変更。`TTSTool` の引数は不変
  (`tts.elevenlabs_api_key` は STT/voice_id 等と共に渡すが say では未使用)。
- テスト `tests/test_tts_config_gate.py` 7 件追加 (ゲートマトリクス +
  ElevenLabs キー無し + go2rtc ありで登録される確認、mutation 対応明記)。
  pytest 1473 → 1480 緑。

**二層分離に関する注記**: 本変更は例外的に `familiar_agent`
(`config.py` + `agent.py`) に触れる。say 登録ゲートは agent の `__init__`
配線そのもので pico_agent 側にフックが無く、回避不能。ただし追加した
`has_voice_output()` は backend-agnostic な述語であり pico 固有ロジックの
流入ではなく、OR 追加の additive 変更で上流マージ性も維持。

**注記**: `ELEVENLABS_API_KEY` は STT (Scribe v2 Realtime, `STTConfig`) では
引き続き利用される。本変更は say **登録**ゲートのみに影響し、STT や実音声
経路 (go2rtc→Tapo) は不変。

### Phase C-13a (2026-05-31): 幻聴 denylist 追加 + allowlist 見直し

**原因**: C-13 (commit 1717a12) のフィルタ実装後、実機運用 (2026-05-31 11:21
セッション) で短い幻聴が残存。app.log の `transcribe: EXIT text_len` と、ピコの
`remember`/`say` が引用した発話から確認:
- 「いい」(len=2) … 無音由来。ピコが「『いい』だなんて、何かいいことあった？」と
  誤応答し observation 保存 (id ffb2069e)。
- 「はい」(len=2) … C-13 で allowlist にあったため素通り、ピコが「はい！お返事
  ありがとう」と誤応答 (observation id 51a9094f)。

**修正**:
- denylist に「いい」「はい」を追加 (いずれも len=2、完全一致のみで捕捉)。
  len<5 で prefix/包含レンジに入らず、包含署名長ゲート (≥6) にも掛からないため
  「いいね」「いいよ」「いいですね」「はいはい」「はい、そうです」等の実発話は
  巻き込まない (C-13 で「ごめん」包含が実発話を誤 drop した教訓を踏襲)。
- 「はい」を allowlist から削除し denylist へ移動。常時 ON STT では単独「はい」の
  情報量が小さく誤 drop コストが低い一方、素通しは無音への誤応答 (本バグ) を招く。
  カイニットが実機で単独「はい」の取りこぼしを問題視すれば再考可能 (可逆)。
- 「うん」「ええ」「OK」は幻聴の実観測が無いため allowlist 保持 (誤 drop 回避の保険)。
- テスト 4 件純増 (test_drops_ii / test_drops_hai / test_passes_ii_compounds /
  test_passes_hai_compounds / test_passes_un_still_allowlisted、旧 test_passes_hai
  は test_drops_hai へ置換)。pytest 1469 → 1473 緑、regression なし。

### Phase C-13 (2026-05-31): Whisper STT 幻聴フィルタ (Pi5 テキスト層)

**原因**: Kotoba-Whisper (Whisper Large 系) は無音/環境ノイズを入力されると、
学習データ (動画字幕) に頻出する定型句を高確信度で出力する hallucination を
起こす。実機 (2026-05-31 朝、無音放置) で「ありがとうございました」(len=11、
09:45 周辺で 5 回以上連続) / 「ごめん」(len=3) が頻発し、ピコが
「どういたしまして」と誤応答する事象を確認。VAD 強化 (`STT_VAD_NOISE_DB=-40dB`
/ `STT_VAD_MIN_SEGMENT_SEC=1.0`) でも頻度は十分下がらず。

**修正**: `on_speech()` コールバックへ届く **前** にテキストレベルで silent-drop
する二段目防御を追加。

- 新モジュール `src/pico_agent/stt_hallucination_filter.py`、純関数
  `is_whisper_hallucination(text) -> bool` (True=破棄)。
  - denylist ~18 句 (実機観測「ありがとうございました」「ごめん」+ 動画アウトロ
    定型句「ご視聴ありがとうございました」「チャンネル登録お願いします」等 +
    英語「Thanks for watching」)。
  - allowlist (完全一致のみ、denylist より優先): 「ありがとう」「はい」「うん」
    「ええ」「OK」等。「ありがとう」は denylist「ありがとうございました」と
    衝突しても通過する。
  - 正規化: NFKC + 句読点/空白除去 + casefold (全半角・英大小・記号ゆれ吸収)。
  - 判定順: トグル → allowlist 完全一致 → 単一文字 drop → denylist 完全一致 →
    短文 (5〜15 字) の **先頭一致 (prefix)** または **署名長 (≥6 字) フレーズ包含**。
    **末尾一致 (suffix) は不採用** (「〜ありがとうございました」で終わる丁寧な実発話の
    誤 drop を避ける)。prefix は逆にアウトロ断片 (「次の動画で」「ありがとう…」) を
    捕捉する低リスク signal で、副次効果として「ありがとう」が prefix catch 対象に
    なり allowlist が load-bearing になる。包含を署名長フレーズに限ることで短 token
    (「ごめん」) による「あ、ごめんね」等の実発話誤 drop を防ぐ。
- 統合: `stt_kotoba._emit_segment()` の `if not text: return` 直後でチェックし、
  幻聴は `logger.debug("stt_kotoba: dropped hallucination ...")` のみで silent-drop。
  通常テキストは現状どおり `on_speech()` へ。
- 環境変数 `STT_HALLUCINATION_FILTER` (既定 ON、`false`/`0`/`no`/`off` で無効化)。
  `.env.example` に追記。
- **二層分離**: `pico_agent` 完結 (`familiar_agent` 0 行)。`response_filter` /
  `self_model_filter` と同型 (deps = os/re/unicodedata/loguru)。
- echo 検出 (ステートフル) と INFO drop カウンタは複雑度/純関数性のため見送り
  (将来誤 drop が顕在化したら `WhisperHallucinationFilter` クラスを拡張点に)。
- テスト 16 件 (18 ケース) 追加 (denylist/allowlist/prefix/単一文字/統合/トグル、
  mutation 対応明記)。pytest 1451 → 1469 緑、regression なし。
- 範囲外: `whisper_server.py` の `no_speech_prob` フィルタ (別ワーカ担当)、
  VAD しきい値調整 (.env で対症済)、Whisper モデル変更。

### Fixed (2026-05-31): STT 常時 ON が ffmpeg 7.x で即終了する不具合

- `stt_kotoba._build_ffmpeg_rtsp_cmd()` の `-stimeout 5000000` を `-timeout 5000000`
  へ置換。`-stimeout` は ffmpeg 5.0 で削除済 (RTSP demuxer の旧オプション) で、
  Pi5 の ffmpeg 7.1.3 では "Unrecognized option 'stimeout'" で即終了し、Phase C-11+
  で配線した STT 常時 ON が完全に動作していなかった (ffmpeg が 150ms で ended し
  5 秒間隔で再起動ループ)。`-timeout` は同義の置換で TCP I/O timeout 5 秒を維持。

### Phase C-8.1 (2026-05-26): TTS 配信 WAV 削除レース修正 (動的 delete delay)

**原因**: `tts_sbv2.speak()` の finally は配信 WAV を固定 30s 後に削除していた
(`_delayed_unlink(pre_path, _get_delete_delay_sec())`)。長文 TTS は連結後の再生
時間が 30s を超え得るため、go2rtc がメイン PC 側で WAV を pull / 再生し終える前に
Pi 側で WAV が消え、「not found」レースで音声が途切れる / 鳴らない事象が起きていた。

**修正**: 削除 delay を再生時間ベースで動的算出する。
- 新 helper `_wav_duration_sec(path) -> float | None` … 標準 `wave` の
  `getnframes()/getframerate()` で再生秒を求める。読めない / 壊れ WAV は `None`。
- 新 helper `_compute_delete_delay(pre_path) -> float` …
  `dur = _wav_duration_sec(pre_path); floor = _get_delete_delay_sec();
  return floor if dur is None else max(floor, dur*_get_delete_safety()+_get_delete_margin_sec())`。
- 新 env アクセサ `_get_delete_safety()` (`TTS_DELETE_SAFETY`, 既定 1.5) /
  `_get_delete_margin_sec()` (`TTS_DELETE_MARGIN_SEC`, 既定 10.0)。既存 `_get_*`
  慣習どおり try/except で不正値はデフォルトにフォールバック。
- `speak()` finally を `_delayed_unlink(pre_path, _compute_delete_delay(pre_path))`
  へ差替。`TTS_DELETE_DELAY_SEC=30` は**下限 (floor)** として維持 (後方互換)。
- `.env.example` に `TTS_DELETE_SAFETY` / `TTS_DELETE_MARGIN_SEC` 追記。

**二層分離**: pico_agent 完結 (familiar_agent 0 行)。設計書 24 章侵入点の追加なし。

#### Added (C-8.1)

- `tests/test_adapter_tts_sbv2.py` に 8 件追加 (実 I/O なし、tmp に実 WAV を書く)
  - `_wav_duration_sec` の既知長 WAV 検証 + 壊れ WAV → None
  - `_compute_delete_delay` 長尺 (90s) で floor 超 / 短尺 (2s) で floor=30 /
    壊れ → floor / env 上書き (safety=2.0, margin=5.0) / 不正値フォールバック
  - `speak()` 長文 (80s WAV) で `_delayed_unlink` に渡る delay が再生時間以上
    (mutation 検知: `_compute_delete_delay` を `return floor` 固定に戻すと長尺
    テストが fail)

### Phase C-10a (2026-05-26): Vision 再質問時の see 強制 soft nudge

**原因**: ユーザーが「もう一回見て」「今どう?」と**新しい観察**を求めても、モデルが
会話履歴に残った前回 `see` の画像 (tool_result) をそのまま再利用して答え、カメラを
今撮り直さないことがあった。

**修正**: vision 再質問を検出して user メッセージへ soft nudge を注入する。
- 新設 `src/pico_agent/vision_reprompt.py` (response_filter / self_model_filter と
  同型、familiar_agent を import しない、re / loguru のみ)。
  - `needs_force_see(user_input, messages) -> bool` … vision 関連キーワード
    (見て / 何が見える / カメラ / もう一回見て / 今どう 等、保守的) を含み、**かつ
    履歴の直近に see 痕跡** (assistant の `tool_use name="see"` または画像付き
    `tool_result`) があれば True。AND ゲートで初回観察・非 vision 入力では注入しない。
  - `force_see_suffix() -> str` … 「[VISION] これは新しい観察の要求です。履歴の前回
    画像を再利用せず、必ず see() を今すぐ呼んでから答えること。」
- `src/familiar_agent/agent.py`: import 1 行 + `make_user_message` 直前で
  `if needs_force_see(user_input, self.messages): user_input_with_ctx += force_see_suffix()`
  の 2-3 行のみ (合計 +7 行)。他の familiar_agent 変更なし。
- これは**プロンプト注入 (soft nudge)** であり「都度 see を強制」する hard 保証では
  ない (backend 非依存を優先)。実世界の追従はカイニット実機確認範囲。

**二層分離**: 本体は pico_agent/vision_reprompt.py。agent.py 侵入は import + 2-3 行。
familiar_agent→pico_agent 一方向 import (24 章侵入点と整合: 既存 response_filter /
self_model_filter import と同列の最小注入)。

#### Added (C-10a)

- `src/pico_agent/vision_reprompt.py` 新設 (vision 再質問判定 + nudge テキスト)
- `tests/test_vision_reprompt.py` 新設 (判定ロジック、suffix 内容、二層分離 import 検査)
  - mutation 検知: `_VISION_PATTERNS` を空に差し替えると vision 入力でも未検出
- `tests/test_agent_react_loop.py` に統合 2 件追加 (camera.call mock、実カメラ非干渉)
  - vision 再質問で see 撮影が 2 回 / user メッセージに `[VISION]` nudge 注入
  - 非 vision 入力では nudge 未注入・see 1 回のみ (回帰)

### Phase C-Ctrl+T 常時化 (2026-05-26): STT 常時 ON 化 (Tapo RTSP / Kotoba-Whisper)

**原因**: Phase C-4 で `stt_kotoba.start_rtsp_subscription` (Tapo C210 RTSP 音声
トラックを常時購読し無音区切りで Kotoba-Whisper に投げる VAD 購読) を実装済みだったが
**TUI から未配線**で、TUI の音声入力は Ctrl+T の一発録音 (ElevenLabs) のみだった。

**修正**: 常時 RTSP STT を UI 層から配線し、Ctrl+T を一時停止/再開トグルに再定義。
- `src/familiar_agent/tui.py`:
  - import 1 行 `from pico_agent.adapters import stt_kotoba` (realtime_stt_session
    import と同型)。
  - `__init__`: `self._continuous_stt_task: asyncio.Task | None = None` /
    `self._continuous_stt_enabled: bool`。env `CONTINUOUS_STT` 既定 ON
    (`_continuous_stt_enabled_default()`、false/0/no/off で OFF)。
  - `on_mount`: `CONTINUOUS_STT` ON なら `run_worker(_start_continuous_stt())`。
    `_start_continuous_stt` が `stt_kotoba.start_rtsp_subscription(on_speech=...)`
    を起動し task を保持。callback `_continuous_stt_on_speech` は committed 相当
    (`_input_queue.put(text)` + ログ + `_last_interaction` 更新)。依存未満なら
    stt_kotoba 側で no-op task が返るため安全。
  - `action_toggle_listen` (Ctrl+T) を**常時購読の一時停止/再開トグル**に再定義
    (デフォルト ON、Ctrl+T で OFF↔ON)。一発録音 `_do_record` との二重起動は
    `_continuous_stt_enabled` で排他。Ctrl+T binding ラベルは維持、Space PTT は不変。
  - `action_quit` で `_stop_continuous_stt()` (task cancel) を追加。
- `.env.example` に `CONTINUOUS_STT` 追記。

**二層分離**: 本体 `start_rtsp_subscription` は pico_agent。tui.py は既に
realtime_stt_session を import 済の UI 層で、STT wiring は UI 責務 (pico ラップ不可)。
familiar_agent→pico_agent 一方向 import 厳守。

#### Added (Ctrl+T 常時化)

- `tests/test_tui_continuous_stt.py` 新設 (22 件、実 mic/RTSP/ffmpeg/ElevenLabs
  非干渉。`start_rtsp_subscription` を AsyncMock 化)
  - `__init__` 属性 / `CONTINUOUS_STT` デフォルト ON・OFF 値 (parametrize)
  - on_mount→`_start_continuous_stt` が on_speech 付きで subscription を呼ぶ /
    `on_speech("こんにちは")` 直呼びで `_input_queue` に積まれる / 空発話無視 /
    無効時スキップ / 二重起動防止
  - `action_toggle_listen` で task cancel↔再起動・`_continuous_stt_enabled` 遷移 /
    デバウンス / `_stop_continuous_stt` の cancel / Ctrl+T・Space binding 維持

### Phase C-11 (2026-05-26): LiteRT-LM + Gemma 4 E2B utility ラッパー本実装

utility backend (day summary / emotion / self-model / compaction) を qwen2.5:1.5b
(Ollama) から **Gemma 4 E2B / LiteRT-LM** に差し替え可能にする。前段ベンチで E2B が
1.5b より日本語抽象化・指示追従ともに優位と確定したことを受けた本実装。

**方針 α (familiar_agent 0 行改変, カイニット承認)**: pico_agent に OpenAI 互換
FastAPI ラッパー `litert_server.py` を新設。familiar-ai の utility 経路 (backend.py の
OpenAI 互換 `complete(prompt, max_tokens)`) は単一 user メッセージ
`[{"role":"user","content":prompt}]` を `POST /v1/chat/completions` に送り
`resp.choices[0].message.content` だけ読むため、`.env` の `UTILITY_BASE_URL` を
`http://localhost:11435/v1` に切り替えるだけで familiar_agent を一切変えずに差し替わる。
`response_filter` / `self_model_filter` と同型の二層分離を厳守 (litert_server は
familiar_agent を import しない、pico_agent 自己完結)。

LiteRT Engine / Conversation は並行駆動安全でないため:
- Engine は `lifespan` で `asyncio.to_thread(_engine_factory, model_path)` により
  起動時に **1 回だけ warm** し `app.state.engine` に保持する singleton。
- 推論は `asyncio.Lock` で直列化 (`async with lock: await to_thread(_run)`)。
- `max_num_tokens=2048` (前段 probe で 2048 未満は長プロンプトで
  DYNAMIC_UPDATE_SLICE クラッシュと確定)。
- `_engine_factory` / `_build_sampler` は module レベル変数 = DI 差し込み点。
  本番実装 (`_real_engine_factory` / `_real_build_sampler`) は `litert_lm` を
  **関数内で遅延 import** し、テストはここを monkeypatch して実 Gemma ロードを回避。

エンドポイント:
- `GET /health`: engine None (ロード中 / 失敗) → 503、ロード済 → 200。
- `POST /v1/chat/completions`: messages 空 → 400 / engine None → 503 /
  推論例外 → 500 (detail に `inference failed`)。レスポンスは
  `{"choices":[{"index":0,"message":{"role":"assistant","content":text},"finish_reason":"stop"}], ...}`。

**依存**: Python 3.11 (project uv env) で `litert-lm-api>=0.12.0` の import を実機確認
(wheel は `py3-none-manylinux_2_27_aarch64` = ABI 非依存。probe は 3.13 だったが
3.11 でも import OK)。本体 `[project.dependencies]` に `fastapi>=0.115.0` /
`uvicorn>=0.34.0` / `litert-lm-api>=0.12.0` を追加 (optional group 退避は不要)。

#### Added

- `src/pico_agent/litert_server.py` 新設 (FastAPI OpenAI 互換 utility サーバ)
  - `create_app() -> FastAPI` / `lifespan` singleton warm / `asyncio.Lock` 直列化
  - DI factory `_engine_factory` / `_build_sampler` (遅延 import + monkeypatch 点)
  - helper `_flatten_content` / `_messages_to_prompt` / `_extract_text` (PoC 流用)
  - `main()` = `uvicorn.run(app, host="127.0.0.1", port=LITERT_LISTEN_PORT or 11435)`
- `tests/test_litert_server_c11.py` 新設 (7 件, 全て engine mock = 実 Gemma ロードなし)
  - (a) OpenAI shape (role=assistant / content str) / (b) Lock 直列化 (max_concurrent==1)
    / (c) singleton (factory 呼出 1 回) / (d) health 503→200 / (e) messages 空 400・欠落 422
    / (f) 推論例外 500 / (g) `_real_engine_factory` の max_num_tokens>=2048 (Engine を
    MagicMock 化)
- `deploy/systemd/litert_server.service` 新設 (設置のみ、enable/start はカイニット手動)
- `.env.example`: utility を Gemma/LiteRT に向ける例 + `LITERT_MODEL_PATH` /
  `LITERT_LISTEN_PORT` を追記
- `pyproject.toml [project.dependencies]`: fastapi / uvicorn / litert-lm-api 追加

#### Tests (Phase C-11)

- mutation 対応: Lock 削除 → (b) fail / singleton 削除 → (c) fail / 例外捕捉削除 →
  (f) fail / health 503 削除 → (d) fail / max_num_tokens 緩め → (g) fail。
  Lock 削除 mutation を実際に注入し (b) が `concurrent send_message detected` で
  fail することを確認後 revert。
- フルスイート 1407 passed (C-10 時 1400 → +7)。

---

## Phase C-10 (2026-05-25): self_model 汚染ループ修正

`_update_self_model` (familiar_agent/agent.py) が utility backend qwen2.5:1.5b に
自己洞察を作らせるが、1.5b が抽象化に失敗し応答テキストを **verbatim 反射** する。
その verbatim 行が verbatim ガード無しで `kind='self_model'` として保存され、次セッション
1 ターン目の morning reconstruction で system prompt に注入され、Gemini がそれをエコー
していた (「同じ応答が繰り返される」C-9 バグ)。追加汚染パターン: プロンプトのラベルリーク
(`良い例:\n私は…`)、韓国語 (ハングル) / 中国語混入、英語 verbatim 反射。

**修正 (アプローチ Y, カイニット承認)**: pico_agent に保存前検証フィルタを新設し、
agent.py から import + 呼び出し (2 箇所のみ侵入)。前例 `response_filter` と同型の二層
分離を厳守 (filter は familiar_agent を import せず、依存は re / unicodedata / loguru のみ)。
加えて既存 DB の汚染行を同フィルタの判定で削除するクリーンアップ script を新設・実行。

判定 (`is_valid_self_model_insight(insight, final_text)`, True=保存可):
- (a) verbatim / 高類似: NFKC 正規化 + 空白句読点除去後の部分包含 (norm_i >= 12) /
  文字 bigram Jaccard >= 0.8 / bigram overlap 係数 >= 0.8 (冒頭差異で厳密包含が崩れた
  反射も捕捉)。`final_text` が空なら (a) をスキップ → DB クリーンアップ用途
- (a 補完) 会話エコー: 応答調末尾 AND 会話フィラーの反射文を final_text 不在でも弾く
- (b) 一人称「私」要件 (欠落なら破棄)
- (c) ラベルリーク (`良い例` / `条件[:：]` / `一文だけ` / `nothing` 等を re.IGNORECASE)
- (d) 異言語スクリプト (`unicodedata.name(c)` が `HANGUL` 等で始まる文字混入)
- (d 補完) 中国語混入 (CJK 漢字 >= 4 AND 仮名ゼロ)
- 長さ: `_MIN_LEN=4` / `_MAX_LEN=200`。各破棄理由を `logger.warning` で記録

既存行クリーンアップ (`is_contaminated_existing_row`): final_text 不在前提なので
(c) ラベル OR (d) 異言語 (ハングル + 中国語混入 + ASCII 過多=英語 verbatim 反射)
OR 過長 OR 会話エコー OR ((b) 一人称欠落 AND 応答調末尾) を汚染と判定。
一人称欠落単独では削除しない (正常 self_model の誤削除防止)。

**第2イテレーション強化 (会話エコー + 中国語混入)**: 第1イテレーションの口語反射
判定は「一人称欠落 AND 応答調末尾」の AND だったため、**一人称『私』を含む会話
エコー行** (例「私、元気だよ！話しかけてくれて嬉しいな。そっちはどう？」) を取り
こぼし、これらが `recall_self_model` の `ORDER BY timestamp DESC LIMIT 5` で次回
morning に再注入され C-9 バグを再発させる欠陥があった。対策として **一人称の有無に
依らない会話エコー検出** を追加: **応答調末尾 (`_RESPONSE_TONE_PATTERNS`: だよ/だね/
んだ/です/ます/かな/どう？/嬉しいな 等) AND 会話フィラー (`_CONVERSATIONAL_ECHO_MARKERS`:
うん、/そうだね/元気だよ/話しかけてくれて/そっちは/どう？/分かりません 等)** の AND
ゲート (reason=`conversational_echo`)。AND ゲートにより、フィラーを持たず断定/内省
末尾 (〜られた。/〜ている。/〜惹かれる。) で終わる genuine な一人称内省は保持される。
加えて **中国語混入** (CJK 漢字 >= 4 AND 仮名ゼロ。自然な日本語は必ず仮名を含む)
を `chinese_mixin` として検出 (ASCII 比率も表音文字判定も漢字のみの中国語を取り
こぼすため)。`is_valid_self_model_insight` (保存前) にも会話エコー / 中国語混入を
(a)(d) の補完として組み込み、final_text 不在でも弾けるよう既存行判定と整合させた。

#### Added

- `src/pico_agent/self_model_filter.py` 新設
  - `is_valid_self_model_insight(insight, final_text) -> bool` (保存前フィルタ)
  - `is_contaminated_existing_row(content) -> bool` / `contamination_reason(content) -> str`
    (既存行クリーンアップの単一真実源)
- `scripts/dev/cleanup_self_model_c10.py` 新設 (既存 DB 汚染行クリーンアップ)
  - argparse `--db` / `--dry-run` (既定) / `--apply`、判定は self_model_filter を import
    再利用、DELETE のみ (DROP 禁止)、フィルタ通過行は残すので冪等

#### Fixed

- `src/familiar_agent/agent.py` (2 箇所のみ侵入、二層分離厳守)
  - import 行追加 (`from pico_agent.self_model_filter import is_valid_self_model_insight`)
  - `_update_self_model` の `if insight and insight.lower() != "nothing":` ブロック内、
    `_has_unexpected_language` チェックの直前に検証ガード分岐を追加。reject 時 warning +
    early return。既存 `_has_unexpected_language` は据え置き (新フィルタは上乗せ)

#### DB cleanup (実行結果, 全数値は SELECT / pytest で実測)

- バックアップ:
  - 第1イテレーション (元状態): `~/.familiar_ai/observations.db.bak_c10` = **23 行** (SELECT 実測)
  - 第2イテレーション再 backup: `~/.familiar_ai/observations.db.bak_c10b` = **6 行**
    (sqlite backup API で WAL 込みの live state を取得。`cp` は WAL 未反映の .db 本体
    23 行を写してしまうため不可)
- 削除前 self_model 件数: **6** → 削除後: **1** (この第2イテレーションで **5 行**削除。
  `--apply` 出力 `before=6 after=1` + 独立 SELECT で確認)
- 第2イテレーション削除内訳: chinese_mixin (中国語混入) 1 / conversational_echo
  (一人称含む会話エコー 4: 「私、元気だよ！…そっちはどう？」「うん、元気だよ！…
  話しているんだ。」「うん、そうだね！…話しているんだよ。」「そのような状況では
  私には…分かりません。」)
- 累積 (両イテレーション合計): 元 23 行は **全 23 行が汚染**で削除済 (第1で 18 / 第2で 5)。
  内訳 (強化フィルタを元 23 行に適用した実測): ascii_heavy_wrong_language 9 /
  conversational_echo 6 / no_first_person_response_tone 4 / label_leak 2 /
  chinese_mixin 1 / unexpected_script 1
- 最終保持 **1 行**: `私はアニメの映像と…その不思議な融合体に魅せられた。`
  (id=c10352e8, **backup 後に新規生成された genuine な日本語一人称内省**。元 23 行とは
  別個体)。timestamp DESC 上位 5 (次回 morning の n=5 窓) に**会話エコー / 中国語混入は
  皆無**であることを SELECT で実証
- 冪等確認: `--apply` 再実行で削除候補 0 / 削除 0 件 (`before=1 after=1`, no-op)

#### Tests

- `tests/test_self_model_filter_c10.py` **24 ケース** (verbatim 部分包含 / Jaccard 近似
  コピー / overlap 反射 / ラベルリーク / ハングル / 一人称欠落 / 空・None・短文・過長 reject、
  正常 insight accept、final_text 空時の (a) スキップ、`is_contaminated_existing_row` の
  汚染検出 + 正常行保持 + クリーンアップ冪等)。**第2イテレーションで +5 ケース追加**:
  `test_contaminated_first_person_conversational_echo` (一人称含む会話エコー→True) /
  `test_contaminated_clip_echo_disclaimer_row` (clip echo→True) /
  `test_genuine_first_person_insight_preserved` (genuine 一人称内省 2 例→False、誤削除
  防止 pin) / `test_contaminated_chinese_mixin_row` (中国語混入→True) /
  `test_save_time_conversational_echo_rejected_empty_final` (保存前 final 空でもエコー
  reject)。mutation 対応: 会話エコー検出削除で echo テストが fail、AND ゲートを緩める /
  genuine 誤検出で preserved テストが fail、中国語検出削除で chinese テストが fail
- `tests/test_self_model_pollution_c9.py` **3 ケース** (C-10 ガード成立を assert 追加
  `is_valid_self_model_insight(verbatim_echo, final_text=verbatim_echo) is False`、
  汚染メカニズムの pin は残置)
- `tests/test_agent_morning.py` 更新: 既存 `test_update_self_model_discards_non_japanese_response`
  の log メッセージ assert に新フィルタの reject メッセージを追加 (機能 assert は不変)
- self_model 関連テスト合計 **c10=24 + c9=3 = 27**
- フルスイート: **1400 passed** (実測 tail `1400 passed, 9 warnings`)、ruff / mypy クリーン

#### 設計書との齟齬 (v6 改訂予定)

設計書 v5 は DB パスを `~/.pico_v3/memory.db` と記載するが、実体は
`~/.familiar_ai/observations.db` (テーブル `observations`, 列 `kind`)。本フェーズの
実装・クリーンアップは実体に合わせた。設計書 v5 本体は本フェーズでは触らず、v6 改訂で
反映予定。

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
