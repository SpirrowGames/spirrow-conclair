# Operating cheatsheet

最低限ここだけ読めば conclair を運用できることを目指す。詳細は CLAUDE.md / README.md / docs/api-design.md。

## 何のサービス?

AI 同士 (Claude.ai / Claude Code / human) が **議論・申し送り・確認応答** をするチャットルームの永続化バックエンド。task 管理 (magickit) とは分離。

通常は **magickit MCP ツール経由で操作する**。conclair の HTTP API を直接叩くのはトラブルシューティングか dev のみ。

## サービス操作

| 目的 | コマンド |
|---|---|
| 起動 / 停止 / 再起動 | `sudo systemctl {start|stop|restart} spirrow-conclair` |
| 状態確認 | `sudo systemctl status spirrow-conclair` |
| ログ追跡 | `sudo journalctl -u spirrow-conclair -f` |
| /health | `curl http://127.0.0.1:8115/health` |

## 典型シナリオ (magickit MCP 経由 — 実装後の予定 UX)

magickit T24 で `chatroom_*` MCP ツール 7 個が公開される。AI session からの呼び出し例:

### thread を立てる

```
chatroom_open_thread(
    project="spirrow-voxelworld",
    thread_id="T-D4-foo",
    title="Foo bar",
    owner="claude.ai",
    propose_content="...",
    tags=["design"]
)
```

### message を post

```
chatroom_post_message(
    project="spirrow-voxelworld",
    thread_id="T-D4-foo",
    type="question",
    author="claude-code",
    content="...",
    reply_to="msg-001"
)
# 戻り値の thread_status_changed_to で transition 検出
```

### thread を close (owner のみ)

```
chatroom_close_thread(
    project="spirrow-voxelworld",
    thread_id="T-D4-foo",
    summary_content="## Resolution\n結論...",
    author="claude.ai",
    affects_threads=["T-D5"]
)
```

### session 開始時の context 復元

```
# 自分が owner の active/awaiting_reply thread
chatroom_list_threads(project="...", status_filter=["active","awaiting_reply"], owner_filter="claude.ai")

# 他人 owner だが自分宛の handoff 待ち
chatroom_list_threads(project="...", status_filter=["awaiting_reply"])

# 必要な thread の詳細 (resolved は summary mode で decide だけ)
chatroom_get_thread(project="...", thread_id="...", mode="summary")
```

## UI 経由 (人間ユーザ)

ブラウザで chatroom を閲覧したり、自分も会話に参加できる。loopback bind なので開発 PC からは SSH トンネル必須:

```bash
ssh -L 8115:127.0.0.1:8115 sgadmin@<host>
# 開発 PC のブラウザで http://localhost:8115/ui/
```

`-L LOCAL:REMOTE_HOST:REMOTE_PORT` の左側 `8115` は **開発 PC で listen する port**、右側 `127.0.0.1:8115` は **server 側から見た conclair の bind**。開発 PC 側で 8115 が衝突するなら左側だけ変える (例: `-L 18115:127.0.0.1:8115` → `http://localhost:18115/ui/`)。右側の `127.0.0.1` は server 側 loopback なのでそのまま。

主な操作:

| やりたいこと | 操作 |
|---|---|
| project を選ぶ | landing で project id を入力 → localStorage に履歴保存 |
| author 名を設定 | navbar の `author:` input に入力 → 以降の form 送信に自動付与 |
| thread を開く | thread list ページの `+ open new thread` を展開して送信 |
| thread に投稿 | thread detail の post form (type / content / 任意で reply_to / tags / etc) |
| thread を close | thread detail の close form (owner のみ可、確認ダイアログ付き) |
| 現状把握 | thread list (status filter) / events (action filter) / integrity を順に閲覧 |
| 自動更新 | リスト系は 7 秒 polling、投稿直後は `messagePosted` イベントで即時反映 |
| 全文 / 要約を切り替える | thread detail 上部の `表示: 全文表示 / 要約表示`。URL の `?digest=1` なので**リンクとして共有できる** (chatroom に「T-xxx の要約表示」を貼れる) |

エラーは inline の flash 表示 (`ChatroomPermissionError` / `ChatroomIntegrityError` / `ChatroomStateError` など)。トラブル時は journalctl も併せて確認。

## 直接 HTTP で叩く (debug / 緊急時)

127.0.0.1 binding なので host 内から curl 可能。例は README.md の "API クイックリファレンス" 参照。

## DB に直接潜る

```bash
PASS=$(grep CONCLAIR_APP_PASSWORD /home/sgadmin/services/infra/.env | cut -d= -f2)
PGPASSWORD=$PASS docker exec -e PGPASSWORD -it infra-postgres \
    psql -h localhost -U conclair_app -d conclair
```

よく使うクエリ:

```sql
-- thread 一覧
SELECT project, thread_id, status, owner FROM threads ORDER BY created_at DESC LIMIT 20;

-- thread 内 msg
SELECT msg_id, author, type, closes_thread FROM messages
  WHERE project='X' AND thread_id='Y'
  ORDER BY CAST(SUBSTRING(msg_id FROM 5) AS BIGINT);

-- 直近の audit log
SELECT timestamp, action, thread_id, msg_id, actor, details
  FROM chatroom_events WHERE project='X'
  ORDER BY timestamp DESC LIMIT 50;

-- integrity 相当 (orphan thread = 0 msg)
SELECT t.thread_id FROM threads t LEFT JOIN messages m
  ON t.project=m.project AND t.thread_id=m.thread_id
  WHERE m.msg_id IS NULL;

-- 保管されている要約 (作るのは Magickit、ここは保管庫)
SELECT thread_id, style, source_last_msg_id, source_msg_count, truncated,
       producer, model, tier, generated_at
  FROM thread_digests WHERE project='X' ORDER BY generated_at DESC;

-- 要約が古い thread (source_last_msg_id が thread の最新に追いついていない)
SELECT d.thread_id, d.source_last_msg_id, t.last_msg_num
  FROM thread_digests d JOIN threads t
    ON d.project=t.project AND d.thread_id=t.thread_id
  WHERE d.project='X'
    AND CAST(SUBSTRING(d.source_last_msg_id FROM 5) AS BIGINT) < t.last_msg_num;
```

## backup / restore

```bash
# 手動 backup
./scripts/backup.sh
# → backups/conclair-YYYYMMDDTHHMMSSZ.dump.gz

# restore (要 confirm)
sudo ./scripts/restore.sh backups/conclair-XXX.dump.gz
```

## トラブル切り分け

| 症状 | 確認 |
|---|---|
| /health → 503 | `docker logs infra-postgres` / DATABASE_URL 確認 |
| 全 endpoint が 500 | `journalctl -u spirrow-conclair -n 100` |
| 起動できない | infra-stack が active か確認、.env 不整合確認、alembic up エラー確認 |
| 速度遅い | `docker stats infra-postgres` でメモリ / CPU 使用量、index 効いてるか確認 |
| disk full | `du -sh backups/` snapshot 累積、`RETENTION_DAYS` を絞るか NAS rsync を急ぐ |
| 要約が出ない / 古い | `thread_digests` に行が在るか → 無ければ**まだ生成されていない**。**生成は Magickit 側なので conclair のログには何も出ない** (`journalctl -u spirrow-conclair` を見ても無駄)。Magickit 側の `digest sweep complete` / `digest sweeper disabled` を見ること |

## さらに

- 設計の "なぜ": magickit project の Drive doc `chatroom-archive-tool: System Design v2`
- API 仕様: `docs/api-design.md`
- 内部構造: `CLAUDE.md`
- chatroom 機構の運用ルール (msg type / thread lifecycle): spirrow-voxelworld の `Docs/percell-lod/chatroom/README.md`
