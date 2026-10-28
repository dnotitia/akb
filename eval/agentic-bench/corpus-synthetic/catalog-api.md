# Orchard Catalog API

This is a fictional benchmark fixture, not an AKB product specification.

## Purpose
The Catalog API stores demonstration fruit inventory records.

## Mutating request contract
Every mutating request MUST carry an Idempotency-Key, target an endpoint on
the published allowlist, and use credential references rather than raw secrets.
Read-only requests do not require an idempotency key.
