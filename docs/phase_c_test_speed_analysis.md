# Phase C テスト実行速度分析

## メタ情報

- 計測日: 2026-05-20
- 対象コミット: `500839e` (docs(upstream): add upstream/develop diff snapshot)
- 環境: Raspberry Pi 5 (8GB RAM, NVMe SSD), aarch64, 4 コア
- Python: 3.11.15
- pytest: 9.0.2
- pytest-asyncio: 1.3.0 (asyncio_mode は pyproject 既定値)
- 実行コマンド: `uv run pytest -q --tb=no` (single worker, デフォルト)
- 対象: Phase C-4 完了時点 (タスク 4 マージ済み、タスク 5/6/7 並行進行中)

## 1. 現状サマリ

- 総件数: **1258 件 passed** (collect も 1258 件、期待値通り)
- 失敗・スキップ: 0
- 警告: 6 件 (内容は `coroutine '...' was never awaited` 系 RuntimeWarning。テスト pass には影響しないが将来別タスクで掃除候補)
- 所要時間 (3 回試行、`uv run pytest` の自己報告秒数):

  | 試行 | pytest 報告 | `time` real |
  |------|------------|------------|
  | 1    | 25.15s     | 29.589s    |
  | 2    | 24.77s     | 29.254s    |
  | 3    | 24.55s     | 28.809s    |

  - 中央値: **24.77s** (pytest 報告) / 29.254s (`time` real)
  - 最短: 24.55s / 最長: 25.15s
  - ばらつき: ±2% 程度で極めて安定 (planner 想定の ±50% 超とは無縁)
  - `time real` と pytest 自己報告の差 ~4s は uv 起動・collect・終了処理時間

## 2. 遅いテスト top20 (`--durations=20`)

```
12.62s call     tests/test_memory_graph.py::test_memory_episodes_and_associative_recall_work
 1.13s call     tests/test_scene_backend.py::test_create_scene_backend_gemini
 0.47s call     tests/test_adaptive_thinking.py::TestBuildThinkingParams::test_disabled_returns_empty_dict
 0.46s call     tests/test_backend_base_url.py::test_create_utility_backend_openai_uses_custom_base_url
 0.40s call     tests/test_camera.py::test_move_right_sends_negative_pan
 0.40s call     tests/test_camera.py::test_move_left_sends_positive_pan
 0.40s call     tests/test_camera.py::test_move_down_sends_negative_tilt
 0.40s call     tests/test_camera.py::test_move_up_sends_positive_tilt
 0.31s call     tests/test_tui_interrupt_stress.py::test_rapid_inputs_and_escape_spam_keep_inputs_for_processing
 0.30s call     tests/test_tui_stt_interrupt_stress.py::test_stt_on_rapid_inputs_and_escape_spam_no_drop_no_crash
 0.15s call     tests/test_agent_react_loop.py::test_run_tool_results_added_to_messages
 0.14s call     tests/test_compaction.py::TestPostCompactionRecall::test_recall_n_larger_after_compact
 0.14s call     tests/test_agent_leakage.py::test_run_mental_state_bus_append_called_with_clean_response
 0.14s call     tests/test_agent_tool_routing.py::test_execute_tool_routes_walk_to_mobility
 0.10s call     tests/test_adapter_stt_kotoba.py::test_subscription_loop_force_flush_on_max_segment
 0.10s call     tests/test_adapter_stt_kotoba.py::test_subscription_loop_emits_segment_on_silence_end
 0.10s call     tests/test_adapter_stt_kotoba.py::test_subscription_loop_restarts_on_ffmpeg_eof
 0.10s call     tests/test_adapter_stt_kotoba.py::test_start_rtsp_subscription_invokes_implementation_when_deps_present
 0.07s call     tests/test_adapter_tts_sbv2.py::test_ffmpeg_returns_none_on_nonzero_rc
 0.05s setup    tests/test_prompt_cache_split.py::TestSystemPromptSplit::test_morning_ctx_takes_precedence_over_feelings
```

## 3. ボトルネック分析

### モジュール別合算 (top20 由来)

| モジュール | 件数 | 合算秒 | 備考 |
|---|---|---|---|
| `test_memory_graph.py`           | 1 | 12.62 | **単独で全 top20 の約 70%**、chromadb/SQLite/埋め込みベクトル系の重 I/O 想定 |
| `test_scene_backend.py`          | 1 |  1.13 | Gemini scene backend 初期化 (LLM クライアント mock 生成コスト) |
| `test_camera.py`                 | 4 |  1.60 | Tapo PTZ 4 方向、各 0.40s で揃っている (ONVIF クライアント mock のセットアップ重) |
| `test_adaptive_thinking.py`      | 1 |  0.47 | settings 周りの 1 回限り初期化と推定 |
| `test_backend_base_url.py`       | 1 |  0.46 | openai backend ファクトリ |
| `test_tui_*_stress.py`           | 2 |  0.61 | 連打 stress 系。asyncio sleep 含むため自然な実時間消費 |
| `test_agent_*` 系                | 3 |  0.43 | react_loop / leakage / tool_routing |
| `test_adapter_stt_kotoba.py`     | 4 |  0.40 | RTSP / ffmpeg subprocess mock |
| `test_compaction.py`             | 1 |  0.14 | post-compaction recall |
| `test_adapter_tts_sbv2.py`       | 1 |  0.07 |   |
| `test_prompt_cache_split.py`     | 1 |  0.05 | setup タイム |

top20 合計: 約 18.0s / pytest 全体 24.77s。**残り 6.8s が他 1238 件に薄く分布**。

### 偏りと傾向

- **極端な偏り 1 件**: `test_memory_episodes_and_associative_recall_work` (12.62s) が全体の約 51% を占有。chromadb もしくは memory_graph の埋め込み + 検索を実 backend で回している可能性が高い (詳細は別途要確認)。
- **0.4s 級が camera 4 件揃って top に立つ**のは ONVIF/PTZ mock の共通セットアップが重いだけで、並列化での効果はあるが投資対効果は中。
- I/O 系 / LLM mock 系 / asyncio.sleep 系の 3 グループに分類可能だが、**全体時間の支配項は memory_graph 1 件**である点が最重要所見。
- 残り 1238 件はミリ秒オーダーに収まっており、全体最適化の余地はそこに分散している (= xdist の典型的な得意領域)。

## 4. 並列化の候補と効果見積もり

### pytest-xdist 概要

- `pytest-xdist` は pytest の公式に近い並列実行プラグイン。`-n auto` で CPU コア数 worker、`-n 2` 等で指定可能。
- 各 worker は独立した Python プロセスを起動し、テストを round-robin で配分する (`--dist loadfile` 等で配分戦略変更可)。
- インストール: `uv add --group dev pytest-xdist` (本タスクでは **実行しない**)。

### RPi5 コア数

- `nproc` = 4
- top プロセス余地: Phase D で pico_v3.service 常駐予定だが現状は未稼働 → 4 worker 余地あり。

### 机上見積

理想 (CPU bound 完全並列): `24.77s / n` だが実際は以下の制約で頭打ち。

- **memory_graph 12.62s が単一テストなので並列化で短縮不可** (1 worker が必ずこの時間を負担)。
- 並列化後の理論下限 = max(12.62, (24.77 - 12.62) / n) + プロセス起動オーバヘッド。

| 構成 | 理論下限 | 期待 real | 改善率 |
|---|---|---|---|
| 1 worker (現状) | 24.77s | 29.3s | — |
| 2 worker        | 12.62 + 6.08 ≈ 18.7s | 23〜25s | 約 -20% |
| 4 worker        | 12.62 + 3.04 ≈ 15.7s | 20〜22s | 約 -30% |

**memory_graph がボトルネックなので 4 worker でも 16s が下限**。これを超える短縮には memory_graph 自体の見直しが必要。

### リスク

- **SQLite WAL / chromadb 共有**: ~/.pico_v3/memory.db や chromadb の collection がプロセス間で衝突する可能性。temp_path fixture が file scope か worker scope かを精査必要。
- **asyncio fixture 互換性**: pytest-asyncio 1.3.0 は xdist 互換だが、event_loop fixture の scope が "session" の場合は worker ごとに分かれるため副作用は少ない。
- **stt_kotoba / camera の mock**: subprocess / ONVIF mock が global state 触ってないか確認必須。
- **tui stress 系**: asyncio.sleep を含むテストは worker をブロックするだけで並列化の阻害にはならない (むしろ恩恵あり)。
- **ファイル I/O**: tmp_path は pytest が worker ごとに別ディレクトリを提供するので衝突しない (公式仕様)。
- **新規依存追加**: pyproject.toml / uv.lock 改変は Phase D 直前を避けたい (現在タスク 5/6/7 並行進行中で merge 競合リスク)。

## 5. 本タスクでの結論

- pytest-xdist 導入は **本タスクでは行わない (記録のみ)**。
- 理由:
  1. planner 計画書の制約 (pyproject.toml / uv.lock 不変)
  2. タスク 5/6 並行進行中の依存変更は merge 衝突リスクが高い
  3. **支配項が memory_graph 1 件**であり、xdist 投入よりも先に該当テストを単独で精査する方が ROI が高い可能性 (12.62s → 数 s に縮められれば xdist 不要かもしれない)
  4. Phase D で systemd 常駐すると CPU 余地が削られ、4 worker の効果が薄まる可能性
- 現状 24.77s は dev サイクルとして許容範囲。CI/手元 dev のいずれも 30s 以内で完走する。

## 6. 推奨アクション (帰宅後判断項目)

カイニットさんへの判断要請:

1. **`test_memory_episodes_and_associative_recall_work` の中身精査**を Phase D 前に別タスクで切り出すか？
   - もし実 chromadb / 実埋め込み backend を使っているなら mock 化で大幅短縮可能 (推定: 12.62s → 0.5s 以下)。
   - 現実テストとして残す価値があるなら `@pytest.mark.slow` でデフォルト除外も選択肢。
2. pytest-xdist の **導入タイミング**:
   - 案 A: Phase D 着手 **前** (タスク 5/6/7 完了後の静かなタイミングで `uv add --group dev pytest-xdist`)
   - 案 B: Phase D 着手 **後** (systemd 常駐の影響を計測してから判断)
   - 案 C: 1 の memory_graph 短縮結果次第で判断 (こちらが筋が良さそう)
3. **`@pytest.mark.serial` マーカー検討**:
   - 並列化導入時に「並列実行不可なテスト」をマーク。memory_graph / tui_stress / subprocess 系が候補。
   - xdist の `--dist loadgroup` と組み合わせると安全。
4. RuntimeWarning 6 件 (`coroutine was never awaited`) の掃除:
   - `tests/test_agent_morning.py` / `tests/test_agent_react_loop.py` の AsyncMock 呼び出しで await 漏れ。pytest 通過には影響しないが将来 1.4+ で fail 化する可能性あり。

## 7. 副次的確認

- **pytest-xdist 既存有無**: `uv run pip list | grep -i xdist` → **未インストール** (出力なし) を確認
- **systemd 再起動チェック**: pico_v3.service が Phase D 持ち越しで未作成のため、CLAUDE.md 完了条件 3 (`systemctl restart pico_v3.service`) は **本タスクでは skip** (planner 合意済み)
- **pyproject.toml / uv.lock 差分**: 本タスクで一切触っていない (`git status --short` に該当ファイル無し)
- **触禁ファイル無干渉**: `.env` / `ME.md` / secrets / v4.2 設計書 / familiar_agent/ / discord_bridge/ いずれも変更なし
- **件数固定**: 計測前後で 1258 件のまま (planner 制約クリア)

---

*Phase C テスト実行速度分析 | 2026-05-20 | Phase C-4 完了時点*
