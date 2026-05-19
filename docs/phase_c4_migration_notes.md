# Phase C-4: stt_kotoba RTSP 購読 本実装 移行ノート

**実装日:** 2026-05-19
**対象:** `/home/pico/pico_v3/src/pico_agent/adapters/stt_kotoba.py`
**計画書:** planner agentId `a66de5756d586a985`
**直前状態:** Phase C-3 完了 (commit `e015832`、worktree clean、1217 件グリーン)

---

## 1. 何が変わったか (Phase C-1 → C-4 差分)

Phase C-1 では `start_rtsp_subscription` は **スケルトン (no-op)** だった。
Phase C-4 で **ffmpeg silencedetect ベースの本実装** を投入する。

### 1-1. VAD バックエンドの決定

planner デフォルト案を採用:

| 候補 | 採否 | 理由 |
|---|---|---|
| **ffmpeg silencedetect** | **採用** | Phase C-3 で ffmpeg 依存確定済 (`/usr/bin/ffmpeg` v7.1.3)、追加 uv add 不要 |
| silero-vad / torch | 不採用 | aarch64 wheel 不確実、起動コスト重 |
| webrtcvad | 保留 | aarch64 wheel ビルド要、Phase C-5 以降の選択肢として残す |

`STT_VAD_BACKEND` 環境変数で将来切替可能な設計にしてある (default: `ffmpeg`)。
`disabled` を指定すると no-op に倒せる (本番停止用)。

### 1-2. ffmpeg コマンド構成 (1 入力 2 出力フォーク)

```
ffmpeg -nostdin -loglevel info -rtsp_transport tcp -stimeout 5000000 \
       -i <rtsp_url> \
       -map 0:a -ac 1 -ar 16000 -f s16le -acodec pcm_s16le pipe:1 \
       -map 0:a -af silencedetect=noise=<NOISE_DB>:d=<MIN_SILENCE_SEC> -f null /dev/null
```

設計ポイント:
- `-loglevel info` 必須 (warning だと silencedetect 出力が出ない)
- `-rtsp_transport tcp` で UDP より安定
- `-stimeout 5000000` (μs) で TCP timeout 5 秒
- `-map 0:a` を 2 回置いて音声トラックを 2 出力に分岐
  - 出力 1: stdout に PCM s16le 16kHz mono (発話セグメント切り出し用)
  - 出力 2: /dev/null + silencedetect filter (stderr に silence_start/end が出る)

### 1-3. silencedetect 出力パース

stderr に以下の形式で出る:
```
[silencedetect @ 0x...] silence_start: 2.345
[silencedetect @ 0x...] silence_end: 5.678 | silence_duration: 3.333
```

正規表現でパース:
```python
_SILENCE_RE = re.compile(
    r"silence_(start|end):\s*([0-9.]+)(?:\s*\|\s*silence_duration:\s*([0-9.]+))?"
)
```

イベント解釈:
- `silence_end` = 無音区間の終わり = 発話が再開した瞬間
- このタイミングで「直前まで貯めた PCM = 1 発話」とみなして flush 指示を出す
- `silence_start` は今は emit トリガに使っていない (将来「発話開始」検出に使える)

### 1-4. RTSP URL の解決順

1. `STT_RTSP_URL` が設定されていれば完全優先 (上書き)
2. 未設定なら `CAMERA_HOST` / `CAMERA_USERNAME` / `CAMERA_PASSWORD` から
   `rtsp://<user>:<pass>@<host>:554/stream1` を組み立て (path は `stream1` 固定)
3. CAMERA_* も揃わなければ `None` → no-op タスク

パスワードに含まれる `@` `/` `:` などの記号は `urllib.parse.quote(safe="")` で URL エンコードする (URL を壊さないため)。

### 1-5. ログ用 URL マスク

パスワードがログに出るのを防ぐため、`_mask_rtsp_url()` で
`rtsp://user:pass@host/path` → `rtsp://***@host/path` に変換してから logger に渡す。

### 1-6. セグメント切り出しと flush 条件

- `_pcm_reader`: ffmpeg stdout から PCM を逐次読んで `pcm_buffer` に蓄積
- `_stderr_reader`: ffmpeg stderr を行単位で読んで `silence_end` を検知 → `flush_event` を set
- flush_event が set されたら `_emit_segment` が `pcm_buffer` を whisper に投げて on_speech 呼び出し
- `STT_VAD_MAX_SEGMENT_SEC` (default 15s) を超えたら強制 flush (silence が来なくても切る)
- `STT_VAD_MIN_SEGMENT_SEC` (default 0.3s) 未満は捨てる (ノイズ扱い)

### 1-7. ffmpeg ライフサイクル管理

- `_run_one_ffmpeg_session`: 1 回 ffmpeg を起動 → stdout/stderr 読み込み → 終了処理
  - CancelledError 受信時は ffmpeg を `terminate()` → 3 秒待って `kill()`
  - finally で pcm_task / err_task を必ず cancel
  - 残った pcm_buffer は最後に 1 回 emit
- `_subscription_loop`: 無限ループで `_run_one_ffmpeg_session` を呼ぶ
  - 異常終了なら `STT_FFMPEG_RESTART_BACKOFF_SEC` (default 5s) 待ってから再接続

---

## 2. 環境変数の追加

`.env.example` に追記した 7 キー (`.env` 本体への書き込みは禁止):

| キー | 型 | デフォルト | 用途 |
|---|---|---|---|
| `STT_RTSP_URL` | str | (自動組み立て) | 完全上書き可、未設定なら CAMERA_* から組み立て |
| `STT_VAD_BACKEND` | str | `ffmpeg` | 将来 `webrtcvad` / `disabled` を増やす余地 |
| `STT_VAD_NOISE_DB` | str | `-30dB` | silencedetect の noise 閾値 |
| `STT_VAD_MIN_SILENCE_SEC` | float | `0.5` | silencedetect の d= パラメータ |
| `STT_VAD_MAX_SEGMENT_SEC` | float | `15.0` | 1 発話セグメント上限、超えたら強制 flush |
| `STT_VAD_MIN_SEGMENT_SEC` | float | `0.3` | これ未満は無音/ノイズ扱い、whisper に投げない |
| `STT_FFMPEG_RESTART_BACKOFF_SEC` | float | `5.0` | ffmpeg 異常終了時の再起動待機 |

---

## 3. 公開 API シグネチャ互換性

Phase C-1 のスケルトン I/F を **完全維持** (既存呼び出し側を壊さない):

```python
async def transcribe(audio_bytes: bytes, sample_rate: int = 16000) -> str
async def start_rtsp_subscription(
    on_speech: Callable[[str], Awaitable[None]],
    rtsp_url: str | None = None,
    *,
    vad_threshold: float = 0.5,
    min_silence_ms: int = 500,
    chunk_duration_ms: int = 30,
) -> asyncio.Task
```

引数 `vad_threshold` / `chunk_duration_ms` は silencedetect では未使用 (silero VAD 用の名残)。
将来 silero に切替えるときの予約パラメータとして残してある。

`min_silence_ms` は **env 未設定時のみ** 参照 (後方互換):
- `STT_VAD_MIN_SILENCE_SEC` が設定されていればそちらを優先
- 設定されていなければ `min_silence_ms / 1000.0` を採用

---

## 4. テスト追加・既存テストの扱い

### 4-1. 既存 33 件 → 41 件 (置換 + 追加)

| 旧テスト名 | 新テスト名 | 変更内容 |
|---|---|---|
| `test_start_rtsp_subscription_no_vad_returns_noop_task` | `test_start_rtsp_subscription_no_vad_backend_returns_noop_task` | `_silero_vad_available` monkeypatch → `STT_VAD_BACKEND=disabled` に置換 |
| `test_start_rtsp_subscription_all_deps_present_still_noop` | `test_start_rtsp_subscription_invokes_implementation_when_deps_present` | 「no-op で抜ける」→「本実装が走り on_speech が呼ばれる」に置換 |

その他 31 件は autouse fixture `_clean_rtsp_env` を導入したことで、シェル環境変数の `CAMERA_*` 混入から守られる形に統一。

### 4-2. 新規追加テスト (41 件)

- `_build_ffmpeg_rtsp_cmd` (5 件): URL 反映、noise_db / min_silence_sec 反映、2 map 出力、`-loglevel info`、`-rtsp_transport tcp`
- `_parse_silencedetect_line` (6 件): start / end / end+duration / 非マッチ / 空 / 異常 time
- `_wrap_pcm_to_wav` (3 件): 空 PCM / 16kHz mono s16 / 異なる sample_rate
- `_emit_segment` (5 件): 短すぎは drop / 正常呼び出し / transcribe 例外で silent fail / callback 例外で silent fail / 空 text は on_speech 呼ばない
- env 値 (10 件): VAD_NOISE_DB / MIN_SILENCE_SEC (default/env/invalid) / MAX_SEGMENT_SEC / MIN_SEGMENT_SEC / FFMPEG_RESTART_BACKOFF_SEC / VAD_BACKEND (default/env)
- RTSP URL 組み立て (4 件): CAMERA_* から組み立て / STT_RTSP_URL が CAMERA_* を上書き / 部分的 CAMERA_* は None / 記号入りパスワード URL-encode
- URL マスク (2 件): 認証情報マスク / 認証なし URL は素通し
- VAD backend 可用性 (2 件): `_vad_backend_available('ffmpeg')` / 未知バックエンドは False
- `_subscription_loop` シナリオ (5 件): silence_end で emit / max_segment 強制 flush / 短セグメント drop / cancel で terminate / EOF で再起動

### 4-3. 実 ffmpeg を呼ぶテストは入れない

`asyncio.create_subprocess_exec` を `_FakeFfmpegProcess` クラスで差し替え。
PCM を `StreamReader.feed_data` で流し込み、stderr に silencedetect 出力を仕込んだ上で `feed_eof` して終わらせる。`terminate` / `kill` / `wait` も持つ。

---

## 5. リスクと将来の論点

### 5-1. silencedetect の感度調整

`-30dB` は屋内静音環境向け。Tapo C210 マイクのノイズフロア次第で `-25dB` 〜 `-40dB`
の範囲で調整必要 → 実機 (Phase B 完了時) の現場で確認。

### 5-2. silence_end タイミングと末尾文字落ち

`silence_end` イベントを「直前発話の終わり」として flush するため、
silence_start から silence_end までの間の音声 (= 無音区間) も PCM バッファに混じる可能性。
whisper の頑健性で吸収できると想定するが、末尾に余計な無音が混じると認識精度が落ちる懸念。
帰宅後の実機ログで様子を見る。

### 5-3. 連続発話の境界

短い間 (< 0.5s) を空けて続けて喋ると 1 セグメントになる (`MIN_SILENCE_SEC=0.5` のため)。
カイニットの普段の喋り方に合わせて閾値を調整する余地あり。

### 5-4. on_speech からの react_loop 結線

Phase C-4 では adapter 層と関連テストのみ。`react_loop.py` / `agent.py` への
`start_rtsp_subscription` 呼び出しは **Phase D で別チケット**。本実装した関数は
Phase D で `react_loop.RtspBridge` (仮称) から呼ばれる前提。

---

## 6. 完了条件マッピング

| 完了条件 | 結果 |
|---|---|
| pytest 1217 件以上グリーン | **1258 passed** (+41) |
| 各実装に対応するテストが存在 | 上記 41 件のうち 39 件が新規 (2 件は既存リネーム+中身置換) |
| `uv run ruff check` パス | `All checks passed!` (uvx ruff) |
| `uv run mypy` パス | `Success: no issues found in 1 source file` |
| systemd 再起動チェック | **Phase C-4 では skip** (カイニット帰宅後手動) |

---

*Phase C-4 stt_kotoba 本実装移行ノート | 2026-05-19*
