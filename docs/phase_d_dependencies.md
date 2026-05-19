# Phase D 依存追加 手順書

**作成**: 2026-05-20 (外出期間 2 Day 2 / タスク 12)
**位置づけ**: Phase D-1 着手時に **そのまま実行できるコマンド集**。判断根拠は `phase_d_implementation_plan.md` 節 3 を参照。本書は **チートシート** に徹する。
**実行タイミング**: カイニット帰宅後の Phase D-1 着手と同時 (絶対禁止リスト「torch / tensorflow / 1GB 超パッケージの uv add」とは無関係、discord.py 系は軽量)。
**実行者**: カイニット手動 (外出期間中の自律実行禁止対象)。

---

## 0. 前提条件 (実行前に必ず確認)

### 0-1. ブランチ・git 状態

```bash
cd /home/pico/pico_v3
git status                                 # worktree clean を確認
git branch --show-current                  # pico-base であることを確認
git fetch pico && git pull --ff-only pico pico-base   # remote と同期
```

### 0-2. pytest baseline 確認

```bash
uv run pytest --tb=no -q | tail -3
# 期待: "1260 passed, 6 warnings in ~25s"
#       (Phase C-4 完了時点 + タスク 12 で discord_bridge 2 件追加後)
```

`1260` 未満なら **退却**。何かが壊れている、Phase D-1 着手前に原因究明。

### 0-3. ffmpeg / Python 確認 (Phase C で確認済だが再チェック)

```bash
which ffmpeg && ffmpeg -version 2>&1 | head -1   # /usr/bin/ffmpeg, v7.1.3
uv run python --version                          # Python 3.11.x
uv --version                                     # >= 0.4 推奨
```

### 0-4. .env の Discord 関連キー確認 (Phase C-1 で追加済、Phase D 開始前)

```bash
grep -E "^DISCORD_TOKEN=|^DISCORD_GUILD_ID=|^DISCORD_OWNER_ID=" .env | wc -l
# 期待: 3 (3 キーすべて存在)
```

3 行揃っていない場合は **退却** → `.env.example` を参考にカイニットが手動で追加してから Phase D-1 再開。

### 0-5. ディスク空き確認

```bash
df -h / | tail -1
# 期待: Use% < 80%
```

`discord.py + PyNaCl + voice-recv` は合計 ~10-15 MB の追加。1GB 超パッケージではないので絶対禁止リスト「torch / tensorflow / 1GB 超」には抵触しない。

---

## 1. 採用コマンド (第 1 候補)

**1 行で完結** (voice extras 一括 + voice-recv):

```bash
cd /home/pico/pico_v3
uv add 'discord.py[voice]' discord-ext-voice-recv
```

**期待動作**:
- `discord.py[voice]` extras → `PyNaCl` + `libnacl` 系自動解決
- `discord-ext-voice-recv` → Imayhaveborkedit fork の最新版
- `pyproject.toml` / `uv.lock` 更新
- 所要 30 秒〜2 分 (aarch64 wheel 取得次第)

**成功判定**:

```bash
uv run python -c "import discord; print(discord.__version__)"
# 期待: 2.4.0 以上

uv run python -c "from discord.ext import voice_recv; print('voice_recv OK')"
# 期待: voice_recv OK

uv run python -c "import nacl; print('PyNaCl OK')"
# 期待: PyNaCl OK
```

3 つすべて通れば **第 1 候補成功 → 節 4 へ**。

---

## 2. フォールバック分岐 1 (voice extras wheel build 失敗時)

第 1 候補で `Failed building wheel for PyNaCl` 等が出た場合:

### 2-1. システム依存追加 (sudo 必要、カイニット手動)

```bash
sudo apt update
sudo apt install -y libffi-dev libsodium-dev libopus0 libopus-dev
```

**所要**: 30 秒〜1 分

### 2-2. 依存を分割で追加

```bash
cd /home/pico/pico_v3
uv add discord.py                          # PyNaCl 抜き
uv add PyNaCl                              # build できるはず
uv add discord-ext-voice-recv
```

### 2-3. 成功判定

```bash
uv run python -c "import discord; print(discord.__version__)"
uv run python -c "from discord.ext import voice_recv; print('voice_recv OK')"
uv run python -c "import nacl; print('PyNaCl OK')"
```

3 つ通れば **分岐 1 成功 → 節 4 へ**。

---

## 3. フォールバック分岐 2 (discord-ext-voice-recv が aarch64 で動かない時)

`discord-ext-voice-recv` の `import` で `OSError: libopus.so.0` 等が出る場合:

### 3-1. libopus 確認

```bash
ldconfig -p | grep libopus
# 期待: libopus.so.0 が表示される
```

未表示なら:

```bash
sudo apt install -y libopus0
ldconfig -p | grep libopus   # 再確認
```

### 3-2. それでもダメな時は **Phase D-1 停止、カイニット判断**

以下のどれかに進む (素案ベース):

- **代替 1**: `disnake` (discord.py fork) — VC 受信機能を別 API で持つ。`uv remove discord-ext-voice-recv && uv add disnake`
- **代替 2**: `py-cord` (discord.py fork、VC ネイティブサポート) — `uv remove discord.py discord-ext-voice-recv && uv add py-cord`
- **代替 3**: Phase D-4 の voice 部分を **後送り** にして、まず D-2/D-3 (テキストチャンネル) を完成させる

→ いずれも Phase D-1 着手後の判断、外出期間中の自律実行禁止対象。

---

## 4. uv add 後の verification (commit 前)

### 4-1. pyproject.toml 確認

```bash
grep -A 30 "^\[project\]" pyproject.toml | head -40
# 期待: dependencies に discord-py, discord-ext-voice-recv, PyNaCl が追加されている
```

### 4-2. uv.lock 確認

```bash
git diff uv.lock | grep -E "^[+-]name = " | head -20
# 期待: + で discord-py / discord-ext-voice-recv / PyNaCl / その他依存が追加されている
```

### 4-3. discord.py のバージョン

```bash
uv run python -c "import discord; print(discord.__version__, discord.__file__)"
# 期待: 2.4.0 以上、site-packages 配下
```

### 4-4. pytest 全件グリーン (重要)

```bash
uv run pytest --tb=no -q | tail -3
# 期待: 1260 passed (Phase D-1 着手直後、テスト追加なしの状態)
```

**1260 未満になっていたら退却** (uv add で既存テストが落ちている → 依存解決で何かが壊れた)。`uv remove` で巻き戻して原因究明。

### 4-5. discord.py の availability チェック

```bash
uv run python -c "
from pico_agent.discord_bridge import is_discord_available, last_import_error
print('available:', is_discord_available())
print('last_error:', last_import_error())
"
# 期待:
#   available: True
#   last_error: None
```

これが True になることが Phase D-1 完了の **本質的な成功判定**。`is_discord_available()` が False のままだと Phase D-2 以降の全テストが skip され、本番化が空回りする。

---

## 5. commit

```bash
git add pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
chore(phase-d): add discord.py[voice] and voice-recv deps (Phase D-1)

Phase D-1 着手と同時に Discord 接続用ライブラリを追加。
- discord.py[voice] (>= 2.4): VC 音声暗号化込み
- discord-ext-voice-recv (>= 0.5): VC 音声受信 (Imayhaveborkedit fork)
- PyNaCl は discord.py[voice] が自動解決

verification:
- pytest 1260 passed (Phase C-4 baseline + タスク 12 追加 2 件 維持)
- is_discord_available() = True
- last_import_error() = None

Phase D-2 (bot.py 本接続) から本実装着手。
EOF
)"
git push pico pico-base
```

---

## 6. uv add しない判断条件 (Phase D-1 撤退)

以下のいずれかに該当する場合は `uv add` を **実行せず、カイニットに判断を仰ぐ**:

| 条件 | 理由 |
|---|---|
| pytest baseline が 1260 未満 | 既存実装が壊れている、Phase D 着手前に原因究明が必要 |
| Discord 3 キー (`DISCORD_TOKEN` / `DISCORD_GUILD_ID` / `DISCORD_OWNER_ID`) が .env に揃っていない | 接続テストが空回りする |
| ディスク空き 1GB 未満 | uv add 自体は通るが、その後の wheel cache / pyc 蓄積で破綻リスク |
| RPi5 が swap 多用状態 (free -h で Swap used が RAM の 50% 超) | 不安定、uv 操作中に OOM の懸念 |
| Tapo C210 / メイン PC との接続疎通が不安定 | Phase D-2 以降の実機検証で詰む |

---

## 7. Phase D-1 完了の定義 (Done criteria)

以下 **すべて** を満たして Phase D-1 完了:

1. ✅ `uv add` 成功 (節 1 第 1 候補 or 節 2 / 節 3 のフォールバック分岐)
2. ✅ `is_discord_available()` が True を返す
3. ✅ `last_import_error()` が None を返す
4. ✅ pytest 1260 件以上、グリーン維持
5. ✅ commit + push 済
6. ✅ `pyproject.toml` / `uv.lock` の差分が `git log -1` で確認できる
7. ✅ カイニットへ Phase D-2 着手の許可を仰ぐ報告

→ ここまで完了したら、`phase_d_implementation_plan.md` 節 4 の **D-2 (bot.py 本接続実装)** に進む。

---

## 8. リスク・既知の落とし穴

### 8-1. discord.py の voice extras と PyNaCl の build 競合

aarch64 環境で `PyNaCl` の wheel が見つからない場合、`pip install` が C build にフォールバックする。`libsodium-dev` が無いと失敗する。→ 節 2-1 で `apt install libsodium-dev` を先行。

### 8-2. discord-ext-voice-recv の Imayhaveborkedit fork

公式 `discord.py` には voice 受信がない (送信のみ)。Imayhaveborkedit が fork してメンテしているが、**aarch64 wheel は未確認**。Phase D-1 で実機検証して、ダメなら節 3-2 のフォールバックに進む。

### 8-3. uv add 後の .env 検証順序

`uv add` 自体は `.env` を読まない。だが `is_discord_available()` の戻り値だけ確認しても、`DISCORD_TOKEN` 等の .env キーが揃っていないと Phase D-2 で空回りする。→ 節 0-4 を必ず先に確認。

### 8-4. discord.py 2.4 系の breaking change

discord.py 2.0 → 2.4 で `discord.Bot` → `discord.Client` API 変更が一部あり。bot.py の `_build_client` は 2.4 系を前提に書いてある (Phase C-1 commit `9d90a8d` 時点)。2.5 系で更に変わる可能性 → `uv add 'discord.py[voice]>=2.4,<3.0'` のように上限を切る選択肢もある (Phase D-1 で再判断)。

### 8-5. systemd 起動時の .venv パス

`pico_v3.service` (Phase D-6 で作成予定) で `uv run python -m ...` を起動する際、`.venv/lib/python3.11/site-packages/discord` のパスが正しく解決される必要がある。systemd の `WorkingDirectory=/home/pico/pico_v3` と `User=pico` の組合せで .venv 認識される設計だが、Phase D-6 着手時に再確認。

---

## 9. 関連ドキュメント

- [`phase_d_implementation_plan.md`](./phase_d_implementation_plan.md) — Phase D 全体 (D-1〜D-7) の実装ステップ詳細 (872 行)
- [`phase_d_design_memo.md`](./phase_d_design_memo.md) — Phase C-1 時点の方針メモ (224 行、概念寄り)
- [`test_baseline.md`](./test_baseline.md) — pytest 件数推移
- `pyproject.toml` — 現在の依存セクション

---

*Phase D 依存追加 手順書 | 2026-05-20 タスク 12 作成 | Phase D-1 着手時に節 0 から順に実行*
