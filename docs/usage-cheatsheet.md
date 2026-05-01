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

## さらに

- 設計の "なぜ": magickit project の Drive doc `chatroom-archive-tool: System Design v2`
- API 仕様: `docs/api-design.md`
- 内部構造: `CLAUDE.md`
- chatroom 機構の運用ルール (msg type / thread lifecycle): spirrow-voxelworld の `Docs/percell-lod/chatroom/README.md`
