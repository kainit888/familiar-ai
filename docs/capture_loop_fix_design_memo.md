# `_capture_loop` 黒画像問題 修正設計メモ

| 項目 | 内容 |
|---|---|
| 対象 | `src/familiar_agent/tools/camera.py` `CameraTool._capture_loop` |
| 関連 | `vision_black_capture_analysis.md`, `diagnose_rtsp_black.py`, `familiar_ai_overview.md:210-214` |
| 起票日 | 2026-05-19 |
| 着手 | 帰宅後 P0-3 (本タスク) |

> 本メモは 2026-05-17〜18 の不在期間に書かれた原本が失われたため、
> CHANGELOG_PICO.md / familiar_ai_overview.md / test_baseline.md の参照記述と
> `scripts/dev/diagnose_rtsp_black.py` の診断仕様から再構築したもの。

---

## 1. 症状

TUI を 15 時間以上連続稼働させると、`CameraTool._capture_loop` が
RTSP ストリームから取得するフレームがすべて **RGB(0,0,0) の黒画像** に
なる。`_cap.read()` は `ret=True` を返す (= 失敗扱いされない) が、
内容は zero buffer。

`~/.familiar_ai/captures/` の最新画像を PIL で読むと `mean ≈ 0`。
診断スクリプト (`diagnose_rtsp_black.py`) で `black_pixel_ratio ≥ 0.99`
を確認できる状態。

## 2. 真因 (推定)

- OpenCV/ffmpeg の RTSP デコーダ内部で **zero-frame buffer 状態に陥る**
- `_cap.read()` 自体は B コード経路で zero-filled frame を返してしまう
- 既存コードの retry 経路は `_cap.open(source)` のみ呼んでおり、
  `release()` していないため内部ハンドルを掴んだまま再オープンが
  失敗 (もしくは同じ壊れた buffer を引き継ぐ)

## 3. 修正方針 (案 1 + 案 2 の組み合わせ)

### 案 1: 明示的 release + reopen

`_cap.open(source)` ではなく **完全 release → 新規 VideoCapture 生成** に切り替える。
ffmpeg 内部ハンドルを確実に破棄するため。

```python
def _reset_capture(self) -> None:
    if self._cap is not None:
        try:
            self._cap.release()
        except Exception as e:
            logger.debug("Capture release errored (ignored): %s", e)
    time.sleep(2.0)
    self._cap = self._open_capture()
```

トリガー: `ret=False` または `frame is None` を検知したとき。

### 案 2: 黒画像検知 + 連続カウンタ

`_cap.read()` が `ret=True` を返しても、平均ピクセル値が極端に低い場合は
zero buffer pathology と判定して reset を起動する。

```python
_BLACK_MEAN_THRESHOLD = 5.0
_BLACK_CONSECUTIVE_LIMIT = 5

def _is_frame_black(frame, threshold: float = _BLACK_MEAN_THRESHOLD) -> bool:
    if frame is None:
        return False
    try:
        return 0.0 <= float(frame.mean()) < threshold
    except Exception:
        return False
```

ループ内処理:

```python
if _is_frame_black(frame):
    consecutive_black += 1
    if consecutive_black >= _BLACK_CONSECUTIVE_LIMIT:
        self._reset_capture()
        consecutive_black = 0
    continue  # _last_frame には commit しない
consecutive_black = 0
```

## 4. 閾値の根拠

| 値 | 根拠 |
|---|---|
| `BLACK_MEAN_THRESHOLD = 5.0` | `diagnose_rtsp_black.py:71` も `is_black = (b<5)&(g<5)&(r<5)` を使用。RTSP zero buffer は文字通り mean=0、通常の暗所撮影でもセンサーノイズで mean ≥ 10 になる |
| `BLACK_CONSECUTIVE_LIMIT = 5` | 5 フレーム連続で初めてリセット。RTSP 起動直後の warmup や瞬間的なフレーム欠落で誤発火しない |

## 5. False positive リスク

- **暗い部屋を黒画像と誤判定**: mean < 5 は通常の照明環境ではほぼ起きない。
  カーテンを閉めた寝室でも mean は 10〜30。
- **カメラレンズを完全に塞いだ場合**: 誤判定 (リセットしてしまう) するが、
  実害は ~5 秒のリセット遅延のみ。許容範囲。

## 6. 期待効果

- TUI 長期稼働で発生していた zero-buffer pathology が自動回復
- `_cap.read()` の `ret=False` 経路も同じ `_reset_capture()` に集約 (DRY)
- リセット時のサイドエフェクト (2 秒スリープ + 再オープン ~1-2 秒) は
  10 秒以内に新しいフレームが復帰する想定

## 7. 実装サイズ目安

- 追加: 約 30 行 (helper 抽出含む)
- 修正: `_capture_loop` 内の retry 経路 1 箇所 + 黒画像検知ブロック 1 箇所
- テスト: 8 件追加予定 (純粋関数 4, 統合 4)

## 8. テスト方針

1. `_is_frame_black` の純粋関数テスト (zero/normal/None/dark room)
2. `_reset_capture` の release + 再オープン挙動 (cv2.VideoCapture をモック)
3. 連続 N フレーム黒で reset 起動の検証
4. 散発的な黒フレームでは reset しないこと
5. `ret=False` 経路で reset 起動の検証

`time.sleep(2.0)` はテスト時 `monkeypatch.setattr("time.sleep", lambda _: None)` で短絡。

## 9. commit 分割

1. 本設計メモ追加
2. 案 1 実装 (`_open_capture` / `_reset_capture` 抽出 + `_capture_loop` 改修)
3. 案 2 実装 (`_is_frame_black` + 連続カウンタ)
4. テスト追加 (8 件)

## 10. 残課題 / 将来検討

- `_BLACK_CONSECUTIVE_LIMIT` / `_BLACK_MEAN_THRESHOLD` を環境変数で外出し
  するかは Phase D まで保留 (今は module 定数で固定)
- `ret=True` で zero-buffer になるパターンが ffmpeg バージョン依存なら、
  将来 OPENCV_FFMPEG_CAPTURE_OPTIONS の見直しもあり得る
- 黒画像が頻発するなら ONVIF SnapshotUri 経路へのフォールバック検討
  (Phase E 候補)
