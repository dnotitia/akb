# Orchard ADR-001: Write queue

This is a fictional benchmark fixture, not an AKB product specification.

## Decision
An incoming write waits as a coroutine before acquiring a database connection.
When capacity becomes available, it enters the write queue and acquires a connection.
If the queue wait expires, the API returns HTTP 429 with a Retry-After header.
It does not reject immediately merely because all write slots are busy.
