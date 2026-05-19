# pytest 速度改善 提案メモ

**作成**: 2026-05-20 (外出期間 2 Day 2 / タスク 13)
**前提**: `phase_c_test_speed_analysis.md` (Day 2 朝、commit `2533841`) の続編。`--durations=30` 再実行と、**実装可能な安全範囲** の提案を整理する。
**位置づけ**: タスク 13 (Day 3 予定) の前倒し成果。実装は最小限 (グリーン維持・件数維持) に留め、踏み込んだ最適化はカイニット帰宅後の判断に委ねる。

---

## 0. TL;DR

| 観点 | 結論 |
|---|---|
| 全体所要 | **24.75s** (1260 件、Phase C-4 + タスク 12 後) |
| 支配項 | `test_memory_episodes_and_associative_recall_work` 1 件で **12.78s = 51.6%** |
| 残り 1259 件 | 11.97s 合計 (1 件あたり平均 9.5ms、健全) |
| 支配項の中身 | 約 10s = SentenceTransformer (multilingual-e5-small) 初回ロード、約 2s = 実埋め込み計算 + SQLite |
| **本タスクで実装した最適化** | **なし** — 安全に高速化できる経路がない (詳細 §4) |
| 提案 (帰宅後判断) | A) `@pytest.mark.slow` マーカー導入、B) 真の埋め込み確認をスモークテスト化、C) sentence-transformers キャッシュ事前ウォーム、D) xdist 並列化 |

→ **Day 2 朝時点の結論「並列化より単独遅テストの最適化が ROI 高い」は半分正しく半分誤り**。最適化の "可能性" は ROI 高いが、**「安全な実装」の選択肢が乏しい** ことが判明。

---

## 1. --durations=30 (2026-05-20 05:38 JST 計測)

```
============================= slowest 30 durations =============================
12.78s call     tests/test_memory_graph.py::test_memory_episodes_and_associative_recall_work
 1.09s call     tests/test_scene_backend.py::test_create_scene_backend_gemini
 0.62s call     tests/test_setup_wizard.py::test_validate_camera_connection_success
 0.49s call     tests/test_adaptive_thinking.py::TestBuildThinkingParams::test_disabled_returns_empty_dict
 0.43s call     tests/test_backend_base_url.py::test_create_utility_backend_openai_uses_custom_base_url
 0.40s call     tests/test_camera.py::test_move_right_sends_negative_pan
 0.40s call     tests/test_camera.py::test_move_up_sends_positive_tilt
 0.40s call     tests/test_camera.py::test_move_down_sends_negative_tilt
 0.40s call     tests/test_camera.py::test_move_left_sends_positive_pan
 0.31s call     tests/test_tui_interrupt_stress.py::test_rapid_inputs_and_escape_spam_keep_inputs_for_processing
 0.30s call     tests/test_tui_stt_interrupt_stress.py::test_stt_on_rapid_inputs_and_escape_spam_no_drop_no_crash
 0.16s call     tests/test_agent_react_loop.py::test_run_tool_results_added_to_messages
 0.15s call     tests/test_agent_tool_routing.py::test_execute_tool_routes_walk_to_mobility
 0.14s call     tests/test_compaction.py::TestPostCompactionRecall::test_recall_n_larger_after_compact
 0.14s call     tests/test_agent_leakage.py::test_run_mental_state_bus_append_called_with_clean_response
 0.10s call     tests/test_adapter_stt_kotoba.py::test_subscription_loop_restarts_on_ffmpeg_eof
 0.10s call     tests/test_adapter_stt_kotoba.py::test_subscription_loop_force_flush_on_max_segment
 0.10s call     tests/test_adapter_stt_kotoba.py::test_start_rtsp_subscription_invokes_implementation_when_deps_present
 0.10s call     tests/test_adapter_stt_kotoba.py::test_subscription_loop_emits_segment_on_silence_end
 0.08s call     tests/test_adapter_tts_sbv2.py::test_ffmpeg_returns_none_on_nonzero_rc
 0.05s setup    tests/test_prompt_cache_split.py::TestSystemPromptSplit::test_morning_ctx_takes_precedence_over_feelings
 0.05s call     tests/test_memory_prewarm.py::TestEmbeddingModelPreWarm::test_load_is_idempotent_with_lock
 0.05s call     tests/test_realtime_stt_session.py::test_committed_relay_keeps_different_texts
 0.05s call     tests/test_adapter_stt_kotoba.py::test_subscription_loop_cancel_terminates_ffmpeg
 0.05s call     tests/test_realtime_stt_session.py::test_committed_relay_drops_same_text_within_dedupe_window
 0.05s call     tests/test_realtime_stt_session.py::test_committed_relay_drops_recent_tts_echo
 0.05s call     tests/test_realtime_stt_session.py::test_committed_relay_schedules_restart_after_loop_watchdog
 0.05s call     tests/test_realtime_stt_session.py::test_partial_relay_drops_audio_event_tags
 0.04s call     tests/test_companion_mood.py::TestFrustratedBoostsDesire::test_frustrated_boosts_worry_companion
 0.04s call     tests/test_agent_react_loop.py::test_run_passes_latest_pre_see_action_into_scene_update
============================= 1260 passed, 6 warnings in 24.75s =============================
```

Day 2 朝計測 (24.77s、12.62s) との比較:
- 支配項 (`test_memory_episodes_and_associative_recall_work`): **12.62s → 12.78s** (+0.16s、誤差)
- 全体: **24.77s → 24.75s** (-0.02s、誤差)
- → 観測値は安定。タスク 12 で +2 件追加したが時間影響は無視できる範囲。

### 1-1. 上位 10 件で全体の何 %?

- Top 1 (memory_graph) = 12.78s = 51.6%
- Top 1+2 (gemini backend) = 13.87s = 56.1%
- Top 10 = 16.79s = 67.8%
- 残り 1250 件 = 7.96s = 32.2% (1 件あたり平均 6.4ms、非常に健全)

→ **支配項 1 件の改善が圧倒的に効く**。残り 1250 件の最適化は ROI ほぼゼロ。

---

## 2. 支配項の解剖

### 2-1. テスト内容

`tests/test_memory_graph.py::test_memory_episodes_and_associative_recall_work` (14 行):

```python
def test_memory_episodes_and_associative_recall_work(tmp_path):
    mem = _memory(tmp_path)
    first_id, ok1 = mem.save_with_id("雨の散歩のことを覚えておく", kind="conversation")
    second_id, ok2 = mem.save_with_id("散歩中に古い喫茶店を見つけた", kind="observation")
    # ... episode 作成、link 作成
    recalled = mem.recall_divergent("雨 散歩", n=4)
    assert any(item.get("memory_id") == second_id for item in recalled)
    assert any(item.get("episode_id") == episode_id for item in recalled)
    working = mem.refresh_working_memory("雨 散歩", n=4)
```

→ **実際の SentenceTransformer + 実埋め込み計算** で「『雨 散歩』クエリが 2 つの保存メモリと意味的に近い」ことを検証する。

### 2-2. 単独実行時の所要

```bash
$ uv run pytest tests/test_memory_graph.py::test_memory_episodes_and_associative_recall_work --durations=10 -q
11.93s total, 11.53s call
```

→ 12s 弱の所要、call フェーズが支配 (setup/teardown は < 0.005s)

### 2-3. 同一ファイル内の 2 件目は瞬殺

```bash
$ uv run pytest tests/test_memory_graph.py --durations=10 -q
11.46s test_memory_episodes_and_associative_recall_work
 0.01s test_conflicting_semantic_facts_create_revisions
1.87s total
```

→ 2 件目は SentenceTransformer ロード済キャッシュを使うため瞬時に完了。
→ 結論: **モデル初回ロード = ~10s が支配項**、実埋め込み計算は ~1-2s。

### 2-4. 他のメモリテストは < 0.01s

```bash
$ uv run pytest tests/test_memory_event_log.py tests/test_memory_recall_metadata.py \
                 tests/test_memory_projection.py tests/test_memory_worker.py --durations=10 -q
15 passed in 0.66s (各 0.01s 以下)
```

→ これらは `patch.object(_EmbeddingModel, "encode_document", return_value=[[1.0, 0.0, 0.0]])` で **埋め込み計算自体を mock している**。SentenceTransformer をロードしない。

---

## 3. 支配項を「安全に高速化できない」理由

### 3-1. テストの本質的検証内容

`test_memory_episodes_and_associative_recall_work` は名前の通り **associative recall** を検証する:
- 「雨 散歩」クエリが「雨の散歩のことを覚えておく」と「散歩中に古い喫茶店を見つけた」の両方を引き当てる
- これは **multilingual-e5-small が日本語の意味的近さを正しく計算できる** ことが前提

ここで `encode_document` / `encode_query` を mock すると:
- ❌ 「雨 散歩」と「雨の散歩」の意味的近さが失われる
- ❌ associative_recall の本質的な検証が無効化される (機械的なメモリ追加・取得のみが残る)
- ❌ 「e5-small が日本語をハンドルできる」というアサーションが消える

→ **mock による高速化は、本テストの検証目的そのものを破壊する**。安全範囲外。

### 3-2. session-scoped fixture も助けにならない

`SentenceTransformer` インスタンスは `_EmbeddingModel._model` というモジュールグローバルに保持され、**プロセス内で 1 回しかロードされない**。pytest セッション中に同じ Python プロセスで動くテストは自動的に共有する。

問題は **本テストが SentenceTransformer をロードする唯一のテスト** だということ。session-scoped fixture を導入しても他テストが恩恵を受けないので、合計時間は変わらない。

### 3-3. multilingual-e5-small より小さいモデルへの差し替え

- ピコ本番では multilingual-e5-small 採用が確定 (設計書 4-1 章)
- テスト用に別モデルを使うと、本番との差異が出る
- e5-small はすでに小さい (~118M params)、これより小さい多言語モデルは品質が落ちる
- → 採用しない

### 3-4. sentence-transformers のキャッシュ事前ウォーム

`sentence-transformers` は HuggingFace Hub から **モデル重みを `~/.cache/huggingface/` にダウンロード** する。これは初回のみ実行され、2 回目以降はディスクから読み込み。RPi5 の現状は既にキャッシュ済 (Phase A 以降何度もロード)、なのでネットワーク経由のダウンロード時間は発生していない。

→ **ロード時間 ~10s はディスク読み込み + torch 初期化 + Transformer 重みのメモリ展開** が支配項。これは sentence-transformers / torch の構造的コスト、外から削減困難。

---

## 4. 採用見送り (本タスクで実装しなかった案)

| 案 | 採用判定 | 理由 |
|---|---|---|
| 支配項を `@pytest.mark.slow` でマークし `pytest -m "not slow"` で除外 | ❌ | 件数 1260 → 1259 件相当に見える (CI fast path で抜ける)、ベースライン維持ルール違反 |
| 支配項のロジックを 2 件に分割 (save/recall + episode/link を別テスト) | ❌ | SentenceTransformer 初回ロードはどちらかで走る、所要合計は変わらない |
| `encode_document` mock 化 (固定ベクトル) | ❌ | テスト本質 (associative recall) を破壊 (§3-1) |
| session-scoped fixture で `_EmbeddingModel` 共有 | ❌ | 他テストが mock 経路、共有メリットなし (§3-2) |
| テスト用に小型モデル (`paraphrase-MiniLM-L3-v2` 等) 差し替え | ❌ | 本番との差異、e5-small は既に小さい (§3-3) |
| pytest-xdist で並列化 | ⚠️ | 12s 単独テストが worker 1 で走るので worker 並列でも 12s 以下にならない |
| sentence-transformers の `_model` 永続化 (FS pickle) | ❌ | 仕組み上できるが、ロード時間の支配項は重みのメモリ展開 (torch 経由)、pickle 経由でも展開コスト変わらない |

---

## 5. 採用候補 (帰宅後判断、本タスクでは未実装)

### 5-1. 【優先度 中】案 A: `@pytest.mark.slow` マーカー導入

#### 5-1-1. 実装案

```python
# pyproject.toml
[tool.pytest.ini_options]
markers = [
    "slow: 単独で 5 秒以上かかるテスト (CI 高速モードで除外可能)",
]
```

```python
# tests/test_memory_graph.py
@pytest.mark.slow
def test_memory_episodes_and_associative_recall_work(tmp_path: Path) -> None:
    ...
```

#### 5-1-2. 使い分け

- **デフォルト**: `uv run pytest` → 1260 件全部走る (件数維持ルール遵守)
- **fast モード**: `uv run pytest -m "not slow"` → 1259 件、~12s 短縮 (開発時の高速反復用)
- **slow only**: `uv run pytest -m "slow"` → 支配項のみ、~12s (CI で別ジョブ化可能)

#### 5-1-3. メリット / デメリット

- ✅ 件数維持: デフォルト実行で 1260 件、ベースライン違反なし
- ✅ 開発時の反復速度向上: `-m "not slow"` で 12s → 1.5s
- ✅ 将来 slow テストが増えた時の集約点
- ❌ 開発者が `-m "not slow"` を使い忘れると効果なし
- ❌ slow マーカーが他の slow になりそうなテストに増殖する保守コスト

#### 5-1-4. 採用条件

- カイニットが「開発時の反復速度を上げたい」と判断したとき
- 帰宅後に Phase D 着手と合わせて判断 (Phase D の新規テストが追加で slow になるか観察してから)

### 5-2. 【優先度 低】案 B: 真の埋め込み確認をスモークテスト化

#### 5-2-1. 実装案

- 既存テストを mock 化して高速化 (associative recall の確認は mock 経路で機械的にチェック)
- 新規 `test_real_embedding_smoke.py` を `@pytest.mark.slow + @pytest.mark.smoke` で別途配置 (CI で日次のみ実行)

#### 5-2-2. メリット / デメリット

- ✅ 開発反復が高速
- ❌ **既存テストの検証性質を変える** (mock vs 実埋め込み)
- ❌ smoke と非 smoke の二重構造、保守コスト
- ❌ ベースライン件数が `+1` (smoke 追加)、ただしマーカー除外で実質維持にもできる

#### 5-2-3. 採用条件

- 開発の反復頻度が極端に高くなった場合 (現状そこまでではない)
- 他に SentenceTransformer 使用テストが増えた時 (現状 1 件のみ)

### 5-3. 【優先度 低】案 C: SentenceTransformer 事前ウォーム fixture (session-scoped)

#### 5-3-1. 実装案

```python
# tests/conftest.py
@pytest.fixture(scope="session", autouse=False)
def _embedding_model_warmed():
    """セッション開始時に SentenceTransformer をロードしておく fixture。

    Phase E 以降で実埋め込みを使うテストが増えた時に、
    各テストで 10s ロード時間が積み重ならないようにする。
    現状は test_memory_episodes_and_associative_recall_work のみが
    使うので恩恵なし。複数の slow テストが追加された時に有効化する。
    """
    from familiar_agent.tools.memory import _EmbeddingModel
    model = _EmbeddingModel()
    model._load()
    yield model
```

#### 5-3-2. 採用条件

- Phase E / F / G で実埋め込みを使うテストが 2 件以上に増えた時
- 現状は単独 1 件、ロードコストは pytest セッション内で 1 回しか発生しない (恩恵なし)

### 5-4. 【優先度 低】案 D: pytest-xdist 並列化

#### 5-4-1. 実装案

```bash
uv add --dev pytest-xdist
uv run pytest -n auto
```

#### 5-4-2. 期待効果

| 構成 | 理論所要 | 制約 |
|---|---|---|
| `-n 1` (直列) | 24.75s | 現状 |
| `-n 2` | ~13s | worker 1 が 12s 単独テスト持つ、worker 2 が残り |
| `-n 4` (RPi5 全コア) | ~12s (下限) | 単独支配テストでブロック |
| `-n 8` (オーバーサブ) | ~12s (頭打ち) | RPi5 4 コア、コンテキストスイッチコスト |

→ **どんなに並列化しても 12s 以下にはならない** (単独テストの所要が下限)。

#### 5-4-3. メリット / デメリット

- ✅ Phase D 以降テストが +50〜+100 件増えたら 2-3x 効果が期待できる
- ❌ 現状の +2.5% (24.75 → 12) では実装コストに見合わない (xdist の挙動デバッグ、SQLite テストの分離等)
- ❌ pytest-xdist はテスト分離不変の前提があり、`tmp_path` 共有や module-scoped fixture を疑う必要

#### 5-4-4. 採用条件

- Phase D / E 完了後、テスト件数 1400 件以上 + 所要 40s 以上になった時点で再判断
- 現状 1260 件 / 25s なら見送り

---

## 6. 結論

### 6-1. 本タスク (タスク 13) で実装した最適化

**なし**。安全範囲内で実装可能な最適化が存在しないため、提案メモのみを残す。

### 6-2. 即座に実装するなら案 A (`@pytest.mark.slow`)

- 件数維持 ✓ (デフォルト実行で 1260 件)
- 開発反復速度向上 ✓ (`-m "not slow"` で 12s → 1.5s)
- 1 ファイル変更、1 行マーカー追加、リスク極小

ただし **本タスクでは見送り** (絶対禁止リストではないが、ベースラインに変更を加えるのは帰宅後判断が筋)。

### 6-3. テスト所要 25s は健全な水準

- 1260 件 / 25s = 1 件あたり 20ms (RPi5 aarch64 環境としては良好)
- 12s 支配項は **本番に必要な検証** (associative recall のリアル動作)
- 開発反復で困るほどではない (Phase D 着手時の新規テスト追加で再評価)

### 6-4. 帰宅後の判断項目

| 項目 | 判断タイミング |
|---|---|
| 案 A `@pytest.mark.slow` 導入 | Phase D-7 (テスト追加完了) で再判断、slow が 1-2 件で済むなら見送りも |
| 案 D pytest-xdist 導入 | Phase E or Phase G 完了時、件数 1400 以上で再判断 |
| 案 C session-scoped fixture | Phase E / F で実埋め込みテストが追加された時 |
| 案 B 真埋め込みスモーク分離 | 採用しない (保守コスト > 高速化メリット) |

---

## 7. 関連ドキュメント

- [`phase_c_test_speed_analysis.md`](./phase_c_test_speed_analysis.md) — Day 2 朝の初回計測 (commit `2533841`)
- [`test_baseline.md`](./test_baseline.md) — pytest 件数推移
- [`phase_d_implementation_plan.md`](./phase_d_implementation_plan.md) — Phase D で +28 件のテスト追加予定 (D-2 +3, D-3 +8, D-4 +10, D-6 +2, D-7 +5)

---

*pytest 速度改善 提案メモ | 2026-05-20 タスク 13 完了 | 実装最適化 0 件、提案 4 件 (A/C/D 候補、B 不採用) | 帰宅後判断*
