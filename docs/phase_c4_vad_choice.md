# Phase C-4 VAD バックエンド採用判断メモ

**作成日**: 2026-05-20
**対象 Phase**: C-4 (STT adapter `stt_kotoba.py` 本実装)
**対象 commit**: `43419a0` (`feat(stt): Phase C-4 stt_kotoba RTSP subscription + ffmpeg silencedetect VAD`)
**位置づけ**: 「何を実装したか」は `phase_c4_migration_notes.md` 側。本メモは **「なぜ ffmpeg silencedetect を選び、silero-vad / webrtcvad を退けたか」** の判断軸を残すためのもの。

---

## 1. 結論サマリ

**ffmpeg silencedetect を採用、silero-vad / webrtcvad は不採用 (webrtcvad は将来候補として残す)。**

設計書 v4.3 素案 第 14-4-2 章で確定済み。本メモはその判断根拠を Phase C-4 実装直後の視点でアーカイブする。

---

## 2. 候補と評価表

| 候補 | 依存サイズ | aarch64 (RPi5) 適合性 | 追加検証コスト | 採否 |
|---|---|---|---|---|
| **ffmpeg silencedetect** | 0 (`/usr/bin/ffmpeg` v7.1.3、Phase C-3 で確定済) | OS 提供バイナリで問題なし | ほぼ 0 (既存依存の機能を 1 つ追加で使うだけ) | **採用** |
| silero-vad + torch | 1GB 超 (torch 本体) | aarch64 wheel が公式提供されているか不確実、ビルドが必要な可能性 | 高 (新規 uv add、実機ビルド確認、起動コスト計測) | **不採用** |
| webrtcvad | 小 (C 拡張、数 MB) | aarch64 wheel ビルドが必要な可能性あり | 中 (新規 uv add、ビルド確認) | **不採用** (将来候補) |
| エネルギー閾値の自前実装 | 0 | 問題なし | 中 (パース・閾値調整・テスト作成) | **不採用** (silencedetect で足りるため車輪の再発明) |

---

## 3. 採用基準

判断は以下 4 つの基準に基づく:

1. **外出期間中の自律進行制約**
   カイニットが外出中で、新規 Python 依存の追加検証 (`uv add` → ビルド → 動作確認) は実機操作と手戻りリスクが高い。Phase C-4 は「外出期間中に進められる範囲」で完結させる必要があった。

2. **aarch64 (RPi5) 環境での実行**
   torch 系のパッケージは aarch64 wheel の有無・サイズ・起動コストが unstable。RPi5 (8GB RAM, NVMe SSD) にとって 1GB 超のディスク消費と起動オーバヘッドは無視できない。

3. **Phase C-3 で確定済みの ffmpeg を流用**
   Phase C-3 (TTS adapter) で `/usr/bin/ffmpeg` v7.1.3 を依存として確定済。silencedetect は ffmpeg 標準フィルタなので、追加インストール不要・追加検証不要。

4. **torch 系 1GB 超パッケージは禁止リスト入り**
   設計書 v4.3 素案 14-4-2 章で「torch 不要」を明文化済。pico_v3 のディスク方針 (~/.pico_v3/ 配下のサイズ制御) からも、torch のような巨大依存は避ける。

---

## 4. 結論詳細

### 4-1. ffmpeg silencedetect の利点

- **追加 uv add 不要**: Phase C-3 で ffmpeg 依存確定済。`/usr/bin/ffmpeg` を `asyncio.create_subprocess_exec` で呼ぶだけ。
- **aarch64 問題なし**: Raspberry Pi OS 提供の標準パッケージで完結。wheel ビルド問題が発生しえない。
- **1 プロセスで PCM + 無音検出を同時取得**: `-map 0:a` を 2 回置いて 1 入力 2 出力フォークが可能 (stdout に PCM s16le、stderr に silencedetect イベント)。Python 側の同時購読プロセスを増やさずに済む。
- **テスト容易性**: stderr の出力フォーマット (`silence_start: X.XX` / `silence_end: X.XX | silence_duration: X.XX`) が安定しており、正規表現でパース可能。`_FakeFfmpegProcess` で完全モック可。

### 4-2. ffmpeg silencedetect の弱点 (既知の課題)

- **感度調整は環境依存**: `STT_VAD_NOISE_DB` (default `-30dB`) は屋内静音環境向け。Tapo C210 マイクのノイズフロア次第で `-25dB` 〜 `-40dB` の範囲で調整が必要。詳細は `phase_c4_migration_notes.md` 5-1 章参照。
- **silence_end タイミングと末尾混じり**: `silence_end` イベントで flush するため、silence_start から silence_end までの無音区間が PCM バッファに混じる。whisper の頑健性で吸収できると想定するが、認識精度低下の懸念あり。詳細は `phase_c4_migration_notes.md` 5-2 章参照。
- **連続発話の境界判定**: `MIN_SILENCE_SEC=0.5` のため、0.5 秒未満の間で続けて喋ると 1 セグメントになる。カイニットの普段の喋り方に合わせて閾値を調整する余地あり (`phase_c4_migration_notes.md` 5-3 章)。

これらは「採用しない理由」ではなく「採用後にチューニングで詰める課題」として扱う。

### 4-3. silero-vad を退けた理由 (詳細)

- torch 本体が 1GB 超のディスク消費。RPi5 のディスク方針と衝突。
- aarch64 wheel の有無が公式に保証されていない。ビルドが必要な場合、外出期間中の自律進行制約に反する。
- 起動コストが重い (モデルロード時間)。常駐サービスとはいえ、systemd 再起動時の latency が積み重なる。
- silencedetect で足りる現状、torch を入れる便益が見合わない。

### 4-4. webrtcvad を退けた理由 (詳細)

- C 拡張で aarch64 wheel が公式提供されている保証がない。ビルドが必要な可能性あり、外出期間中の検証が困難。
- silencedetect と比べて精度面の優位性が **現時点では未検証**。Tapo C210 RTSP の実音声で比較計測しないと判断できない。
- 将来 silencedetect の感度・末尾混じり問題が運用上致命的になった場合、`STT_VAD_BACKEND=webrtcvad` で切り替えられる余地は残す (Phase C-5 以降の保留候補)。

---

## 5. 将来の選択肢

- **webrtcvad を Phase C-5 以降の保留候補に**
  silencedetect の感度・末尾混じりが実機運用で問題化した場合、webrtcvad を `uv add` して比較検証する。判断は実機ログ蓄積 (Phase D 以降の Tapo C210 接続後) を経てから。
- **`STT_VAD_BACKEND` env で切替可能な設計**
  `stt_kotoba.py` は `STT_VAD_BACKEND` 環境変数で backend を切り替える設計 (default: `ffmpeg`、`disabled` で no-op に倒せる)。将来 `webrtcvad` を追加する際は、この env の case 分岐に 1 行追加するだけで済む。実装は commit `43419a0` 参照。
- **silero-vad は当面採用しない**
  torch 依存のコスト見合いが改善するか、silencedetect で本質的に対処できない事態が発生しない限り、silero-vad には戻らない。

---

## 6. 参照

- commit `43419a0` — Phase C-4 stt_kotoba 本実装 (silencedetect 採用)
- `/home/pico/pico_v3/docs/phase_c4_migration_notes.md` 1-1 章 (VAD バックエンド決定テーブル) / 5-1〜5-3 章 (silencedetect の弱点)
- `/home/pico/pico_v3/CHANGELOG_PICO.md` `[Unreleased]` Phase C-4 セクション
- `/home/pico/pico_v3_docs/設計書_統合版_v4.3_素案.md` 第 14-4-2 章 (VAD バックエンドの決定 v4.3 確定)

---

*Phase C-4 VAD バックエンド採用判断メモ | 2026-05-20*
