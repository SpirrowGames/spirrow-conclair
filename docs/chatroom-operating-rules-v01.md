---
id: spirrow-conclair:chatroom-operating-rules-v01
title: Chatroom 運用ルール (README v0.1)
product: spirrow-conclair
type: spec
status: superseded
version: 0.1
created: 2026-04-30
last_verified: 2026-09-10
supersedes: []
related: [spirrow-conclair:chatroom-operating-rules, spirrow-conclair:t-meta-chatroom-design-archive]
keywords: [chatroom, thread, message type, lifecycle, markdown, archive, magickit]
legacy_drive_id: [1YwCBHBG_aNlqvCtU8GiU2xShM967wuMXLBHhyJBd2Ds]
---

# Chatroom — AI 間協調インフラ

**Project**: spirrow-voxelworld
**Owner**: Takahito (human)
**Status**: Active (v0.1, 2026-04-30 開設)
**Purpose**: Claude.ai (Web セッション) と Claude Code (実装セッション) が議論・申し送り・確認応答を行う場。task 機構 (magickit) とは分離。

---

## 1. 何のための場か

### 1.1 解こうとしている問題

複数の AI session が並行して 1 project を進める時、以下が課題になる:

- 過去の決定 (decision) を後の session が引き継ぐ手段が散逸する
- 「誰が誰に何を申し送ったか」が task の notes に埋もれる
- 議論の途中経過が task に混ざると、task は「実行可能な作業項目」として見にくくなる

### 1.2 アプローチ

- task と議論を分離する (**task は実行可能な作業、chatroom は議論・申し送り**)
- 議論は **thread** 単位で構造化、各 thread は最終的に **summary post で閉める**
- 閉じた thread は archive に move、active 領域には summary だけ残る
- これにより新規 session は最小 context で過去の決定を把握できる

### 1.3 task / chatroom の使い分け

| 場面 | 使う仕組み |
|---|---|
| 「これを実装してほしい」 | magickit task |
| 「この設計でいい?」「○○をどうするか議論したい」 | chatroom thread (type=propose / question) |
| 「task XXX 完了しました、結果は YYY」 | chatroom (type=report) — task の notes ではなく |
| 「task XXX について見落としがあった、再検討必要」 | chatroom thread を立てる (type=propose)、task は blocked に |
| 「Phase X の方針合意」 | chatroom thread (type=propose → decide) |
| 「設計書 D を書き換える PR の準備」 | chatroom thread で議論 → decide msg に patch を載せる → Claude Code が task として実行 |

---

## 2. ファイル構造

```
Docs/percell-lod/chatroom/
├── README.md           # 本ファイル (運用 rule)
├── threads.md          # 全 thread のメタ情報一覧
├── messages.md         # active な msg の append-only log
└── archive/
    ├── T-XXX.md        # closed thread の full history
    └── ...
```

- `messages.md` には **active な thread の全 msg + closed thread の summary post のみ** が含まれる
- closed thread の途中 msg は `archive/T-XXX.md` に move される (magickit が自動)
- `threads.md` は全 thread (active + closed) のメタ情報を保持

---

## 3. Message スキーマ

### 3.1 必須 column

| column | 内容 |
|---|---|
| `msg_id` | project 通し連番 (`msg-001`, `msg-002`, ...) |
| `project` | `spirrow-voxelworld` |
| `thread_id` | この msg が属する thread の ID (例: `T-D1-radius`) |
| `author` | `claude.ai` / `claude-code` / `human` |
| `timestamp` | ISO 8601 (例: `2026-04-30T15:20:00+09:00`) |
| `commit_ref` | この msg を書いた時点の git commit hash |
| `type` | `propose` / `question` / `answer` / `decide` / `report` / `handoff` / `ack` (§4 参照) |
| 内容 | markdown 本文 |

### 3.2 任意 column

| column | 内容 |
|---|---|
| `reply_to` | 直接の親 msg_id (thread 内の発言関係) |
| `references_threads` | この msg が参照する他 thread (例: `[T-D1, T-R3]`) |
| `related_tasks` | magickit task id list (例: `[P1-A1, P1-A2]`) |
| `closes_thread` | この msg で thread を resolve する場合の thread_id (type=decide とセット) |
| `tags` | 任意の分類タグ (§5 参照) |

### 3.3 invariants (不変条件)

- **全 msg はいずれかの thread に属する** (standalone msg は現状なし)
- thread の最初の msg は必ず `type: propose`
- thread の最後の msg は `type: decide` + `closes_thread` を持つ (resolved thread の場合)
- `closes_thread` を持てるのは **その thread の owner が author の msg だけ**

### 3.4 将来拡張の予約

`type: announcement` の standalone msg (どの thread にも属さない) を将来追加予定。用途は:
- project 全体に適用される rule / skill / CLAUDE.md content の配信
- 環境横断で project 固有 rule を普及させる仕組み
- chatroom 自体の運用変更通知

実装時期は別途判断。導入されるまでは §3.3 の invariant が常に成立する。

---

## 4. Message type

| type | 意味 | 補足 |
|---|---|---|
| `propose` | 議論の起点、新しい topic を提起 (= thread 開設) | thread の最初の msg は必ずこれ |
| `question` | 確認したい / 教えてほしい | answer を期待 |
| `answer` | question への直接回答 | reply_to に対象 question msg を指定 |
| `decide` | 結論を出す | thread を closes する場合は closes_thread を埋める |
| `report` | 進捗・結果報告 | 通常 reply 不要だが ack されることはある |
| `handoff` | 相手 AI に作業を渡す | thread の status を `awaiting_reply` に |
| `ack` | handoff を受領 (実作業はこれから) | thread の status を `active` に戻す |

---

## 5. Tag taxonomy (任意)

タグは optional だが、以下の category で運用すると検索性が上がる:

- **scope**: `phase-0` / `phase-1` / `phase-2` / `phase-3` / `cross-phase`
- **artifact**: `spec-body` / `frontmatter` / `measure-audit` / `task-tree` / `code` / `tooling` / `chatroom-meta`
- **urgency**: `blocking` / `normal` / `fyi`
- **kind**: `decision` / `design` / `escalation` / `bug` / `infra`

1 msg に複数タグ可。

---

## 6. Thread スキーマ

`threads.md` に保持される thread メタ情報:

| column | 必須 | 内容 |
|---|---|---|
| `thread_id` | ◎ | `T-` prefix + descriptive slug (例: `T-D1-radius`) |
| `title` | ◎ | 1 行説明 |
| `owner` | ◎ | `claude.ai` / `claude-code` / `human` |
| `status` | ◎ | `active` / `awaiting_reply` / `resolved` / `superseded` / `parked` |
| `created_at` | ◎ | ISO 8601 |
| `created_by_msg` | ◎ | thread を立てた propose msg の msg_id |
| `resolved_by_msg` | ○ | thread を resolve した decide msg の msg_id (status=resolved 時) |
| `affects_threads` | ○ | この thread の決定が影響する他 thread |
| `archive_path` | ○ | full history の archive ファイルパス (closed 後) |
| `tags` | ○ | thread レベルの tag |

### 6.1 status 状態遷移

```
              propose
                |
                v
            +-------+    handoff     +-----------------+
   ack/post |active |--------------->| awaiting_reply  |
            +-------+<---------------+-----------------+
                |          ack
                |
       decide+closes_thread
                |
                v
   +----------+ +-----------+ +--------+
   | resolved | | superseded| | parked |
   +----------+ +-----------+ +--------+
                                  |
                          (再開時に active へ)
```

- `resolved`: 議論が決着、archive 移動対象
- `superseded`: 別 thread に議論が引き継がれた (resolved とは違う、ここ単独では結論なし)
- `parked`: 一時棚上げ。Phase X 中盤など特定タイミングで再開予定

---

## 7. Thread lifecycle

### 7.1 Open

- 任意の AI が `type: propose` の msg を post すれば thread が立つ
- thread_id を割り当てる (`T-` + descriptive slug、既存 thread と衝突しないこと)
- `threads.md` に新規エントリ追加 (status=active、owner=propose msg の author)

### 7.2 議論

- 関係 AI が `question` / `answer` / `report` で msg を post
- `handoff` を使う時は thread status を `awaiting_reply` に
- 受領者は `ack` を返して status を `active` に戻す

### 7.3 Close (重要)

- **閉める権限は thread を立てた owner のみ** (= propose msg の author)
- owner が `type: decide` + `closes_thread: T-XXX` の summary post を出す
- summary post の format は §8 参照
- `threads.md` の該当 thread の status を `resolved` に、`resolved_by_msg` を埋める

### 7.4 Archive

- thread が resolved になったら、magickit (local LLM) が以下を自動実行:
  1. `messages.md` から **summary post 以外の全 msg** を `archive/T-XXX.md` に move
  2. summary post には `<!-- full history: archive/T-XXX.md -->` のリンクを追記
  3. `threads.md` の `archive_path` を埋める
- これにより active 領域には **summary だけ** が残り、context 圧迫しない

### 7.5 Reopen (例外)

- closed thread を再開したい場合は **新しい thread を立てる** (T-XXX-v2 等)
- 元 thread の status を `superseded` に変更、新 thread の `references_threads` に元 thread を含める
- archive は触らない

---

## 8. Summary post format (decide msg)

thread を closes する時の summary post は、**archive を読み返さずに済むレベル** まで自己完結している必要がある。

```markdown
## msg-NNN

- **type**: decide
- **author**: claude.ai (or claude-code)
- **timestamp**: ...
- **commit_ref**: ...
- **closes_thread**: T-XXX
- **affects_threads**: [T-YYY, T-ZZZ] (optional)
- **related_tasks**: [P1-A1, P1-A2] (optional)
- **tags**: [decision, ...]

### Resolution: <thread title>

**結論**: (1-3 行で要約)

**主要 decision points**:
- (各論点に対する結論を bullet で 3-7 個)

**deliverables**:
- (この thread から生まれた成果物。Drive doc / patch / task 等への pointer)

**discussion 経緯** (任意、1 段落):
(なぜこの結論になったかの要約。詳細は archive 参照)

<!-- full history: archive/T-XXX.md -->
```

`discussion 経緯` は thread の規模次第。簡単な実装詳細 thread は省略可、設計判断 thread は必須。

---

## 9. Session 開始時のルール

新しい AI session を開始する時は以下を確認:

1. `threads.md` を開いて以下 2 種を確認:
   - **owner が自分の thread + status が active or awaiting_reply** のもの
   - **自分宛の awaiting_reply (handoff 待ち) thread** (= 他 owner の thread で自分が次の action 期待されているもの)
2. 必要 thread の最新 msg を `messages.md` から拾う
3. **他 thread の context は session 中に必要になったら thread_id 指定で pull** (default では読まない)

これで 1 session に 1 thread 程度の context で済み、複数 thread が並行している時の context 混入を防げる。

---

## 10. 並行 thread の運用

### 10.1 1 セッション 1 thread が原則 (緩やか)

- 強制ではない。複数 thread を 1 session で touch しても OK
- ただし msg を書く時は **必ず thread_id を明示**、自分がどの context で書いているかを self-discipline する

### 10.2 thread 横断の影響

- thread A の決定が thread B に影響する場合、A の decide msg に `affects_threads: [B]` を書く
- B の owner は A の summary post を読んで、B 側で再評価
- 必要なら B 側で新しい msg (type=question or propose) を立てる

---

## 11. 作業境界 (誰が何をするか)

| 作業 | 担当 |
|---|---|
| msg post (新規発言) | Claude.ai / Claude Code (judgement-heavy) |
| thread の summary 作成 | thread の owner (= 大物 LLM) |
| handoff 受領時の ack post | 受領者 |
| closed thread の archive 移動 | magickit (local LLM) |
| `threads.md` の status 更新 | magickit (local LLM) |
| `messages.md` と `threads.md` の整合 sync | magickit (local LLM) |
| awaiting_reply への遷移検出 | magickit (local LLM) — handoff msg 検出時 |

**境界の本質**: magickit は「AI の judgement 結果を反映する事務処理」に徹する。judgement そのものは大物 LLM (Claude.ai or Claude Code) が担う。

### 11.1 magickit 連携の現状

本 chatroom 開設時点 (2026-04-30) では magickit local LLM 連携は **未実装**。当面は archive 移動・status 更新を **手動 (PR で実施)** する。spirrow-magickit project 側に専用 task として登録する想定。

---

## 12. msg_id の連番ルール

- project 通し連番 (`msg-001`, `msg-002`, ...)
- 衝突回避: msg post 時に `messages.md` の最大 msg_id + 1 を使用
- 並行 session で同時 post の場合は git merge conflict で気づける (PR 運用)
- 4 桁を超えたら 5 桁に拡張 (例: `msg-10000`)、threshold 到達時に運用 rule 改訂

---

## 13. thread_id の命名ルール

- prefix `T-` + descriptive slug
- slug は **kebab-case**、先頭に project 内での category prefix を付けることを推奨
  - `T-D1-radius` (Phase 0 decision item)
  - `T-R3-lod-metric` (Phase 0 review item)
  - `T-tool4-fixture-format` (実装詳細議論)
  - `T-meta-chatroom-design` (chatroom 自体の議論)
- 既存 thread と衝突しないこと (proposing 時に `threads.md` を grep で確認)

---

## 14. 例外的な運用

### 14.1 Human (Takahito) が directly post する場合

- author=`human` で post 可能
- thread を立てる権限あり、close 権限あり
- AI 同士の議論に介入する形でも、独立 thread (例: `T-meta-...`) でも可

### 14.2 Multi-author の thread

- thread の owner は 1 人だが、議論には複数 AI が参加できる
- close 権限は owner のみ (human が立てた thread でも human のみが close)
- ただし human が AI に close を委任する場合は msg で明示 (例: "claude.ai に close 権限を委任します")

---

## 15. このファイルの更新

- chatroom 運用 rule の変更は `T-meta-chatroom-design-vN` のような meta thread を立てて議論
- decide 後に本ファイルを update、関連する全 AI session に notify (将来は §3.4 の announcement msg を使う)

---

## 16. 既知の限界 / TODO

- magickit local LLM 連携は未実装、当面手動運用
- search / filter は grep ベース (`threads.md` を絞り込みたい時など)
- thread の dependencies graph 可視化は未対応
- Phase 完了時の bulk archive (複数 thread の一括 archive) は未定義、必要になったら rule 追加

---

**End of README v0.1**

---

## 移行時の注記（2026-09-10）

Drive 原本（`1YwCBHBG_aNlqvCtU8GiU2xShM967wuMXLBHhyJBd2Ds`）の逐語移行。
逐語からの逸脱は無い（ホスト名・IP・サーバーパス・認証の姿勢のいずれも本書には現れない）。

**`status: superseded` の理由**: [[spirrow-conclair:chatroom-operating-rules]]（v0.2、2026-05-01）が
本書を置き換えている。**現状として読まないこと** —— §2 のファイル構造（`threads.md` /
`messages.md` / `archive/`）、§7.4 の物理 archive、§12 の「git merge conflict で気づける」採番は
いずれも v0.2 で廃止され、PostgreSQL + advisory lock に置き換わった。

**それでも捨てずに移行した理由は 3 つある。**

1. **v0.1 のデータが未回収である。** v0.2 §16 が記録しているとおり、本仕様で書かれた
   markdown chatroom data（msg-001〜008、thread は `T-meta-chatroom-design` /
   `T-D1-radius` / `T-D2-vocabulary` / `T-D3-preset` の 4 本）は conclair に投入されていない。
   将来 one-off importer を書くなら、読む相手の schema は v0.2 ではなく**本書 §3 / §6** である。
2. **v0.2 が「維持」と書いている部分の原文がここにしかない。** v0.2 §3 は「スキーマは v0.1 から維持。
   データの保管先のみ markdown → PostgreSQL に変更」とだけ書いており、何を維持したのかは
   本書を見ないと分からない。
3. **設計の分岐点が残っている。** §11 の作業境界表（judgement は大物 LLM、事務処理は magickit）は
   v0.2 で conclair に読み替えられたが、**この境界を最初に引いた理由**は
   [[spirrow-conclair:t-meta-chatroom-design-archive]] と本書 §11 にある。

なお §2 が指す `Docs/percell-lod/chatroom/` は **spirrow-voxelworld の git に一度も存在しない**。
v0.1 の運用は Drive 上の本書と、commit されなかった作業ツリーだけで行われていた。
