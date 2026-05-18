# pytest baseline (件数推移)

**初版作成**: 2026-05-16 18:17 JST (Stage 1 Phase A 完了時)
**最終更新**: 2026-05-20 (外出期間 Day 3、候補 J-2 産物)
**ブランチ**: `pico-base` (`pico/pico-base` 同期、HEAD `89c78f5`)
**Python**: 3.11.15 (uv 自動取得)
**環境**: RPi5 (aarch64), Linux 6.12.62

## 1. 現状サマリ

| 項目 | 値 |
|---|---|
| **現在の総件数** | **1170 件** |
| 結果 | 1170 passed, 0 failed |
| 警告 | 5-7 件 (主に `AsyncMockMixin._execute_mock_call` coroutine never awaited、実害なし) |
| 所要 | 約 26 秒 (Phase C-1 以降、5/19) |

## 2. コマンド

```bash
cd /home/pico/pico_v3
uv run pytest --tb=no -q
```

カバレッジ確認:
```bash
uv run pytest --cov=src --cov-report=term-missing
```

## 3. 件数推移 (Phase 別)

| 計測日時 | Phase | 件数 | 差分 | 主な追加内容 |
|---|---|---|---|---|
| 2026-05-16 18:17 | Phase A 完了 | **883** | (初期) | familiar-ai 既存テストのみ |
| 2026-05-17 | Phase B 完了 | **887** | +4 | PTZ tilt invert テスト (`test_camera.py`) |
| 2026-05-17 22:00 | Phase C-0 完了 | **947** | +60 | response_filter (+52), agent_leakage (+7), system_prompt (+1) |
| 2026-05-18 朝 | Phase C-1 完了 | **981** | +34 | TTS/STT/Vision adapter テスト各々 |
| 2026-05-18 夜 | Day 1 タスク 1 sub-step 2 完了 | **1008** | +27 | response_filter 末尾ブロック algo + false positive |
| 2026-05-18 夜 | Day 1 タスク 3 完了 | **1028** | +20 | TTS フォールバック + STT RTSP スケルトン |
| 2026-05-18 夜 | Day 1 タスク 4 完了 | **1057** | +29 | discord_bridge スケルトン (availability/bot/text/voice) |
| 2026-05-18 夜 | Discord token 追加完了 | **1061** | +4 | DISCORD_TOKEN サポート + dotenv smoke |
| 2026-05-18 夜 | Day 1 タスク B 完了 | **1146** | +85 | filter false positive 27 + TTS fallback 31 + STT detail 20 + STT log 7 |
| 2026-05-18 夜 | Day 1 タスク C 完了 | **1170** | +24 | discord_bridge Phase D 本実装 (incoming_message_from_discord, mock client) |
| 2026-05-19 〜 5/20 | Day 2-3 (調査タスク中心) | **1170** | ±0 | 新規テストなし (調査と doc のみ) |

**累積**: 883 → **1170** (+287 件、グリーン維持)

## 4. テストファイル一覧 (Phase C-1 + Day 1-2 完了時点)

### familiar_agent 既存
```
tests/test_agent_*.py            (familiar-ai 由来、Phase A baseline)
tests/test_camera*.py            (PTZ 反転対応含む)
tests/test_backend.py
tests/test_backend_base_url.py   (Phase C-1 引き継ぎ、UTILITY_BASE_URL test)
tests/test_workspace.py
tests/test_prediction.py
... (familiar-ai 既存全件)
```

### pico_agent 系 (新規追加)
```
tests/test_response_filter.py          113 件 (末尾ブロック algo + 大量の false positive)
tests/test_agent_leakage.py              7 件 (memory/bus/TTS 経路への filter 流入)
tests/test_system_prompt.py            (+1 件は既存ファイルに追記)
tests/test_adapter_tts_sbv2.py          53 件 (基本 8 + フォールバック 14 + go2rtc 詳細 + パターン化)
tests/test_adapter_stt_kotoba.py        33 件 (基本 7 + RTSP スケルトン 13 + ヘルパー 8 + log 等)
tests/test_adapter_vision_qwen3vl.py   (Phase C-1 で追加)
tests/test_discord_bridge.py            57 件 (availability 3 + bot 16 + text 11 + voice 14 + dotenv 4 + ...)
```

## 5. 警告の内訳 (現状 5-7 件)

`tests/test_agent_morning.py` / `tests/test_agent_react_loop.py` 等で:
```
RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited
```

familiar-ai 本家由来 (上流のテストコード)。Phase 進行に伴って件数微増減 (5-7) する
が、実害なし。pico_agent 側で生成した warning はゼロ。

## 6. 回帰判定ルール (CLAUDE.md 完了条件 1)

- 新規テスト追加で件数増 → OK
- 既存テスト減 → 削除理由を Phase 計画書に記載
- 失敗発生 → 修正ループでグリーン復帰必須

**Phase C-1 / Day 1-2 期間中、テスト件数を一度も減らさず +287 件達成**。

## 7. 件数増加が止まる時期

Day 2 (タスク F+G) と Day 3 (候補 K+J+H 設計のみ) はテスト追加なし
(調査・設計ドキュメント中心の作業のため)。

Day 3 候補 I (テスト可読性改善、時間が許せば) で、件数を維持しつつ可読性を
向上する mock helper 共通化等を予定。

## 8. 帰宅後の予想件数増

| 帰宅後タスク | 予想件数増 |
|---|---|
| `_capture_loop` 修正 + テスト | +5〜10 件 (案 1+2 の各分岐検証) |
| Phase D 本番接続テスト追加 | +10〜20 件 (実 discord.py 経由の minimal smoke) |
| `.env` UTILITY_* 切替後の動作テスト | +0 件 (設定変更のみ、回帰テストでカバー) |

帰宅後完了時点で **約 1180〜1200 件** が目安。

## 9. パフォーマンス

| 計測時期 | 所要時間 |
|---|---|
| Phase A 完了 (883 件) | 57.88 秒 |
| Phase C-1 完了 (981 件) | 25.66 秒 |
| Day 1 タスク C 完了 (1170 件) | 27.18 秒 |
| Day 3 終了時 (1170 件) | 26.09 秒 |

時間軸での実行高速化は SSD/uv キャッシュ効果。1170 件で 26 秒は良好。
今後の追加でも 30 秒以内を維持目標。

## 10. 関連ドキュメント

- [`familiar_ai_overview.md`](./familiar_ai_overview.md) — 全体構成
- [`outside_period_summary.md`](./outside_period_summary.md) — 外出期間まとめ
- [`phase_c_v4.2_alignment.md`](./phase_c_v4.2_alignment.md) — v4.2 整合差分

---

*pytest baseline | 初版 2026-05-16, 最終更新 2026-05-20 (Day 3 朝、候補 J-2 産物)*
