"""Leaf module: the wire vocabulary for a message's ``disposition``.

Same rules as ``close_sanction_vocab``: this file imports nothing but
``typing`` and declares names only. ``schemas/message.py`` builds the wire
models from these Literals, and the migration's CHECK spells the ``kind``
values out literally (a migration must not depend on the current code).

The values mirror what Magickit forwards (spirrow-magickit
``src/magickit/mcp/disposition.py`` at ``2e29c31``: ``KIND_DONE`` /
``KIND_BLOCKED_ON``, ``AGENT_ARMS`` + ``HUMAN_ARM``). Magickit is the gate
-- it decides who may name ``human`` and whether a ``wake`` resolves to an
identity. Conclair checks shape only (D-3: no cross-service identity
lookups), so a vocabulary drift between the two shows up here as a 422 on a
value Magickit accepted, never as a silent drop.
"""

from __future__ import annotations

from typing import Literal

#: ``disposition.kind``. ``malformed`` is deliberately absent: a malformed
#: STOP line is a client error, not a thread lifecycle state.
DispositionKind = Literal["done", "blocked_on"]

#: ``disposition.trigger.arm`` for ``kind='blocked_on'``.
DispositionArm = Literal["thread", "pr", "deploy", "queue-empty", "human"]

#: ``disposition.fallback.reason``. The only system rewrite defined so far is
#: D-8 (3): a ``done`` whose thread owner does not resolve to an identity is
#: stored as ``blocked_on(human)`` with the original claim kept in
#: ``fallback``. Magickit performs the rewrite; Conclair stores it.
DispositionFallbackReason = Literal["owner_unresolved"]
