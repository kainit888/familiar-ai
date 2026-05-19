# familiar-ai upstream との diff 確認メモ

**作成日**: 2026-05-20
**計測対象**: `upstream/develop` ↔ `pico-base`
**共通先祖 (merge-base)**: `efdc9cd93aeb79fbdd6897fa37e0f0fcef62fca2`
**upstream/develop HEAD**: `efdc9cd` (= merge-base、共通先祖から進んでいない)
**pico-base HEAD**: `c0845a1`
**計測コマンド方針**: 計測のみ。merge / rebase / pull 系コマンド一切未実行。

> **要点先出し**:
> upstream/develop は共通先祖 `efdc9cd` から **0 commit** しか進んでいない。
> つまり今 merge しても **upstream 側からの取り込み変更はゼロ** で、コンフリクトは原理的に発生しない。
> ただし将来 upstream が進んだ際に備え、本記録は侵入点 7 ファイル + 設定ファイルを基準点として残す。

---

## 1. 差分サマリ

| 項目 | 値 |
|---|---|
| pico-base 側先行 commit | **45 件** |
| upstream/develop 側先行 commit | **0 件** |
| 変更ファイル総数 | **55 件** |
| insertions / deletions | **+10,818 / -380** |

`git diff --shortstat` 結果:
```
 55 files changed, 10818 insertions(+), 380 deletions(-)
```

---

## 2. pico-base 独自 commit (ピコ側 45 件)

`git log --oneline pico-base ^upstream/develop` 出力 (新しい順、全件):

```
c0845a1 docs(phase_c4): add VAD backend choice rationale memo
587dc78 docs(discord_bridge): タスク 4 コメント整備 (intents 説明、状態遷移、Phase D TODO)
e311ee9 refactor(adapters): タスク 4 型ヒント強化 (TypedDict, Literal, X | None 統一)
f340bc0 docs: add 期間 2 status notice to familiar_ai_prompt_review
8a097c5 docs(phase_c4): add migration notes
96ffcda docs(test_baseline): update to 1258 passes (Phase C-4 完了)
0f447b3 docs(env): add Phase C-4 env example keys
43419a0 feat(stt): Phase C-4 stt_kotoba RTSP subscription + ffmpeg silencedetect VAD
e015832 docs(persona): add familiar_ai_prompt_review (Phase C-2)
4b52a17 docs(phase_c3): add migration notes
8eed16e docs(test_baseline): update to 1217 passes (Phase C-3 完了)
4fea436 docs(env): add Phase C-3 env example keys
9eb8495 feat(tts): Phase C-3 tts_sbv2 implementation
52a07fe chore(gitignore): ignore .env backups and personal logs
817308e feat(persona): default_companion_name to カイニット, fix desire_rest dialect
ad903da feat(persona): rewrite distress repair prefix in standard Japanese
dd2d032 feat(persona): remove dialect/character-name from REFLECTION_PROMPT
334a593 docs: add pico_persona_overrides.md (operational override registry)
5e0da68 docs: pico_v3 一人称ポリシーを v4.3 提案メモにまとめる
12d52f7 feat(persona): replace 「うち」 with 「私」 in ja morning_header
cf22904 feat(persona): remove 「ウチ」/「うち」 from reflect/memory prompts
9772a63 feat(persona): switch self-narrative prompts from 「ウチ」 to 「私」
c87b8f9 test(camera): cover _is_frame_black and _capture_loop reset paths
144187b fix(camera): detect black frames and reset capture (案 2)
b374993 fix(camera): release+reopen VideoCapture on read failure (案 1)
c3dd83d docs: restore capture_loop_fix_design_memo
d89e06c chore: pico_v3 fork CHANGELOG + shared aiohttp mock helper
6b55462 docs: add Phase C-1 + Day 3 overview & test baseline docs
89c78f5 chore(dev): add RTSP black-image diagnostic script (outside task F)
349cb2b feat(discord_bridge): Phase D implementation (mock-tested, no live connect)
f0d174a test: expand coverage for filter/TTS/STT (outside_task task B)
40d78cc test(discord_bridge): add Discord credential smoke + dotenv tests
793bb03 feat(discord_bridge): support DISCORD_TOKEN with legacy fallback
9d90a8d feat(discord_bridge): add Phase D scaffolding (skeleton + tests)
a1c8d8e feat(adapters): add v4.2 14-4/14-5 scaffolding for Tapo audio I/O
87002a5 refactor(filter): change deletion algorithm to trailing-block-only
53f4b38 fix(filter): apply response filter before memory/bus/TTS流入
12126de fix(vision): disable qwen3-vl thinking mode with /no_think prefix
bb70118 refactor(tools): route TTS/STT through pico_agent adapters
8be41cd feat(pico_agent): add TTS/STT/Vision adapters (Phase C-1)
c8fdccb fix(camera): close ONVIF aiohttp session on shutdown
f8bface feat(backend): support UTILITY_BASE_URL / SCENE_BASE_URL for local Ollama
a8a1671 feat(pico_agent): add internal-state leakage suppression (Phase C-0)
4c53d82 chore(scripts): add Phase B ONVIF/RTSP verification scripts
1733c7e fix(camera): invert PTZ tilt direction for Tapo C220
```

主な系統:
- Phase C-0〜C-4 系: 内部状態遮蔽、adapter 層 (TTS/STT/Vision)、persona override
- Phase D 系: discord_bridge スカフォールディング + Phase D 実装
- camera 系: Tapo C220 PTZ 反転、黒画面検出+リセット、ONVIF セッション close
- backend 系: UTILITY_BASE_URL / SCENE_BASE_URL 二層対応

---

## 3. upstream/develop 独自 commit

**0 件**。`git log --oneline upstream/develop ^pico-base` 出力は空。

→ upstream/develop HEAD (`efdc9cd`) が merge-base そのもの。pico-base 分岐以降、本家側は進んでいない。

---

## 4. 変更ファイルの層別分類

### 4-1. `src/familiar_agent/` 配下 (コンフリクトリスク評価対象) — 13 ファイル

```
src/familiar_agent/_i18n.py
src/familiar_agent/agent.py
src/familiar_agent/backend.py
src/familiar_agent/config.py
src/familiar_agent/locales/ja.json
src/familiar_agent/meta_monitor.py
src/familiar_agent/reflect.py
src/familiar_agent/self_narrative.py
src/familiar_agent/settings_schema.py
src/familiar_agent/tools/camera.py
src/familiar_agent/tools/memory.py
src/familiar_agent/tools/stt.py
src/familiar_agent/tools/tts.py
```

### 4-2. `src/pico_agent/` 配下 (新規ディレクトリ) — 11 ファイル

```
src/pico_agent/__init__.py
src/pico_agent/adapters/__init__.py
src/pico_agent/adapters/stt_kotoba.py
src/pico_agent/adapters/tts_sbv2.py
src/pico_agent/adapters/vision_qwen3vl.py
src/pico_agent/discord_bridge/__init__.py
src/pico_agent/discord_bridge/availability.py
src/pico_agent/discord_bridge/bot.py
src/pico_agent/discord_bridge/text_channel.py
src/pico_agent/discord_bridge/voice_channel.py
src/pico_agent/response_filter.py
```

**upstream 側存在確認**: `git ls-tree -r upstream/develop --name-only | grep '^src/pico_agent/'` → **0 件**。
upstream には `src/pico_agent/` 配下が存在しない → 重大コンフリクトなし (新規ディレクトリ追加扱い)。

### 4-3. 設定ファイル (手動マージ要注意) — 3 ファイル

```
.env.example
pyproject.toml
uv.lock
```

### 4-4. その他 (docs / tests / scripts / その他) — 28 ファイル

```
.gitignore
CHANGELOG_PICO.md
docs/capture_loop_fix_design_memo.md
docs/familiar_ai_overview.md
docs/familiar_ai_prompt_review.md
docs/phase_c3_migration_notes.md
docs/phase_c4_migration_notes.md
docs/phase_c4_vad_choice.md
docs/pico_persona_overrides.md
docs/test_baseline.md
docs/v4.3_proposal_persona.md
scripts/dev/diagnose_rtsp_black.py
scripts/dev/test_onvif.py
scripts/dev/test_rtsp.py
tests/conftest.py
tests/test_adapter_stt_kotoba.py
tests/test_adapter_tts_sbv2.py
tests/test_adapter_vision_qwen3vl.py
tests/test_agent_leakage.py
tests/test_backend_base_url.py
tests/test_camera.py
tests/test_conftest_mock_helpers.py
tests/test_discord_bridge.py
tests/test_response_filter.py
tests/test_stt.py
tests/test_system_prompt.py
tests/test_tts.py
tests/test_tts_no_ffmpeg.py
```

---

## 5. 人格レイヤー保護観点のコンフリクト予測

### 設計書 v4.3 素案 24-4 章の侵入点 7 ファイル

各侵入ファイルについて `git log upstream/develop merge-base..upstream/develop -- <path>` で共通先祖以降の upstream 変更を確認。

| 侵入ファイル | 共通先祖以降の upstream 変更 | コンフリクト予測 |
|---|---|---|
| `src/familiar_agent/agent.py` | 0 件 | **低** (upstream 未更新) |
| `src/familiar_agent/backend.py` | 0 件 | **低** (upstream 未更新) |
| `src/familiar_agent/config.py` | 0 件 | **低** (upstream 未更新) |
| `src/familiar_agent/settings_schema.py` | 0 件 | **低** (upstream 未更新) |
| `src/familiar_agent/tools/camera.py` | 0 件 | **低** (upstream 未更新) |
| `src/familiar_agent/tools/tts.py` | 0 件 | **低** (upstream 未更新) |
| `src/familiar_agent/tools/stt.py` | 0 件 | **低** (upstream 未更新) |

**結論**: 現時点で侵入点 7 ファイル全件、upstream 側に共通先祖以降の更新なし。merge を行ってもこれらのファイルは衝突しない。

参考: upstream/develop 側で侵入点 7 ファイルが**直近**に変更された commit (共通先祖到達前のもの):
- `agent.py`: `efdc9cd fix(agent): add lightweight short-turn reply path (#166)` ← merge-base 自身
- `backend.py`: `f91939b feat(scene): Phase 5 — dedicated scene backend ... (#105)` ← merge-base より過去
- `tools/camera.py`: `bc1a6f3 fix: suppress ffmpeg SEI warnings ...` ← merge-base より過去

つまりピコ側分岐 (`efdc9cd` ベース) 時点で侵入点に必要な機能は既に取り込み済み。

### `src/pico_agent/` 衝突確認

`git ls-tree -r upstream/develop --name-only | grep '^src/pico_agent/'` → **0 件**。
upstream 側にディレクトリ自体が存在しないため、merge 時は新規ディレクトリ追加として扱われ衝突なし。

### 設定ファイル衝突予測

`pyproject.toml` / `uv.lock` / `.env.example` の 3 ファイルも upstream 側で共通先祖以降の更新なし (upstream 自体が進んでいないため)。merge を行えば pico 側変更がそのまま反映される。

---

## 6. merge 推奨タイミング (設計書 v4.3 素案 25-4 章準拠)

### 一般原則
- 重い Phase 完了直後 (Phase D 完了後など)
- 外出期間明けの帰宅後
- upstream のメジャー更新 release 直後

### 現状の判断

| 観点 | 状況 |
|---|---|
| pico 側 Phase 進捗 | Phase C-4 完了直後 (4-5 タスク中タスク 8 = upstream diff 確認に着手) |
| 外出期間 | 2026-05-19〜2026-05-22 (期間 2: 外出中、最小作業モード) |
| upstream 進行状況 | **共通先祖から 0 commit** = 取り込むものなし |

**結論**: **現時点での merge は不要**。upstream 側に取り込むべき変更がないため、merge 操作の便益はゼロ。コンフリクトリスクもゼロだが、本来 merge は upstream 取り込みのために行うものであり、本ケースでは作業を要求しない。

**次回 merge 検討タイミング**: 帰宅後 (期間 3: 2026-05-23 以降) または upstream/develop に新規 commit が出現した時点。本ファイル (`upstream_diff_2026-05-20.md`) を基準点として再計測 → 差分確認 → merge 判断。

---

## 7. 参照

- 設計書 v4.3 素案 第 24 章 (二層分離原則)、第 25 章 (upstream 同期戦略)
- 実装計画書 2-4-2 章 (upstream 同期手順)
- `docs/pico_persona_overrides.md` (侵入点ファイル運用ポリシー)
- `docs/v4.3_proposal_persona.md` (人格レイヤー保護方針)

---

## 8. 想定外パターン対応記録

| パターン | 該当 | 対応 |
|---|---|---|
| upstream/develop が大量進行 (commit 500 超) | ❌ 該当せず | — |
| 侵入点 7 ファイルが想定と違う | ❌ 該当せず | 設計書 v4.3 素案リストと完全一致 |
| `src/pico_agent/` が upstream 側に存在 | ❌ 該当せず | upstream に 0 件、新規ディレクトリ扱い |
| **upstream/develop が共通先祖から 0 commit** | ✅ **該当** | **取り込むものなし。merge 不要を本記録に明記。** |

---

*計測完了: 2026-05-20*
*HEAD 不変確認: `git reflog | head -5` で pico-base HEAD = `c0845a1` のまま、merge/rebase/pull 系コマンド未実行*
