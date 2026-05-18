# pico_v3 ペルソナ オーバーライド一覧

| 項目 | 内容 |
|---|---|
| 用途 | pico_v3 fork が upstream familiar-ai (lifemate-ai) と分岐する **キャラ設定** 上の override 記録 |
| 起票日 | 2026-05-19 |
| 関連 | [`v4.3_proposal_persona.md`](./v4.3_proposal_persona.md) (設計書 v4.3 提案として正式化候補) |

このファイルは「pico_v3 が upstream のどこから、なぜ離れたか」を記録する
**運用メモ**。upstream と merge する都度ここに照らして整合性を確認する。

---

## 1. 一人称: 「ウチ」/「うち」 → 「私」/「自分」

### なぜ変更したか

- カイニットの `ME.md:22` 「話し方は標準語。関西弁とかは使わない」 に明示
- 不在期間 2026-05-17〜18 の `self_narrative.jsonl` (orphan) に「ウチ」が混入し、
  ME.md ポリシー違反が観測された
- ME.md は人間 (カイニット) が書いたピコのキャラ設計の **真のソース** で、
  実装プロンプトはこれに従うべき

### 変更箇所

| ファイル | 行 | 変更前 → 変更後 |
|---|---|---|
| `src/familiar_agent/agent.py` | 2095 | `ウチ自身の自己叙述` → `私自身の自己叙述` |
| `src/familiar_agent/agent.py` | 2100 | `一人称は『ウチ』` → `一人称は『私』` |
| `src/familiar_agent/agent.py` | 2280 | `ウチ（ここね）として` → `私として` |
| `src/familiar_agent/agent.py` | 2281 | `一人称は「ウチ」` → `一人称は「私」` |
| `src/familiar_agent/reflect.py` | 27 | `ウチの主観 / ウチらしかったか` → `私の主観 / 自分らしかったか` |
| `src/familiar_agent/self_narrative.py` | 98 | `過去のウチからの続き` → `過去の自分からの続き` |
| `src/familiar_agent/_i18n.py` | 2793 | `[昨日からのうち …]` → `[昨日からの私 …]` |
| `src/familiar_agent/locales/ja.json` | 55 | `[昨日からのうち …]` → `[昨日からの私 …]` |
| `src/familiar_agent/tools/memory.py` | 1127 | `[うちという存在 …]` → `[自分という存在 …]` |

### 触らなかった箇所

- `src/familiar_agent/agent.py:1944, 2264` — コメントの中の引用文
  (`# This is the thread that says "ウチはここにいた、今もいる."` 等)。
  詩的なドキュメント目的なので残置。
- `persona-template/ja.md:8` — 一人称テンプレで「うち」「ぼく」を例示。
  これは「自分に合う言葉に変えてください」と書かれているテンプレなので、
  ピコ専用ではない (他フォークも参照する)。

### upstream merge 時の注意

- 上流が新たに「ウチ」プロンプトを追加するなら、merge 時にこの表を更新して
  pico_v3 側で再度「私」へ
- 上流が `agent.py:1944, 2264` コメントを書き換えた場合は文脈次第で判断

## 2. 他の override (今後追加予定)

現時点では一人称のみ。将来追加するなら以下のような項目:

- 名前: 上流デフォルト `ここね` → pico_v3 では `ピコ` (一部プロンプトに
  残存、`agent.py:2280` の旧 `（ここね）` 表記など。本タスクのスコープ外)
- 言語: 上流は多言語サポート、pico_v3 は日本語固定 (`LANG=ja_JP.UTF-8`)
- ハードウェア前提: Tapo C210 (PTZ ONVIF/RTSP) を `ME.md` で固定

## 3. 適用 commit (Task 1 = 関西弁本体改変)

- `9772a63` feat(persona): switch self-narrative prompts from 「ウチ」 to 「私」
- `cf22904` feat(persona): remove 「ウチ」/「うち」 from reflect/memory prompts
- `12d52f7` feat(persona): replace 「うち」 with 「私」 in ja morning_header
- `5e0da68` docs: pico_v3 一人称ポリシーを v4.3 提案メモにまとめる
