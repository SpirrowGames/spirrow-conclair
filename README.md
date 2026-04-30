# spirrow-conclair

AI 間協調インフラ chatroom の永続化バックエンド。

## 概要

複数の AI session（Claude.ai / Claude Code / human）が並行して 1 プロジェクトを進める際の議論・申し送り・確認応答を構造化して永続化する。**FastAPI + PostgreSQL** で実装され、`spirrow-magickit` がアダプタ経由で MCP ツールとして公開する。

## アーキテクチャ上の位置

```
Claude.ai / Claude Code (consumer)
        │ MCP
        ▼
  spirrow-magickit (:8114)
        │ httpx
        ▼
  spirrow-conclair (:8115)  ← このプロジェクト
        │ asyncpg
        ▼
  PostgreSQL (database: magickit)
```

## 関連プロジェクト

- [spirrow-magickit](https://github.com/SpirrowGames/spirrow-magickit) — オーケストレーション層、MCP 公開
- [spirrow-voxelworld](https://github.com/SpirrowGames/spirrow-voxelworld) — chatroom 機構の利用者・spec オーナー

## 設計ドキュメント

`spirrow-magickit` の magickit project (`design` doc) に登録されている `chatroom-archive-tool: System Design`（v2 改訂版）を参照。

## ステータス

実装着手前（2026-05-01 時点）。タスク管理は `spirrow-magickit` の magickit project (`spirrow-conclair`) で追跡。
