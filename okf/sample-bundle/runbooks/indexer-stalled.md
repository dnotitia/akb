---
type: runbook
title: Indexer stalled
description: Recover a stuck embedding worker.
resource: akb://acme-analytics/coll/runbooks/doc/indexer-stalled.md
tags:
- ops
- indexing
timestamp: "2026-06-10T11:00:00+00:00"
status: active
---

# Indexer stalled

Symptom: documents written but `vector_indexed_at` stays `NULL`.

1. Check the worker is draining the queue.
2. Confirm the vector store is reachable.
3. Re-enqueue the affected chunks.

`type: runbook` is a producer-defined value — OKF requires no central type
registry, and a consumer must accept unknown types.
