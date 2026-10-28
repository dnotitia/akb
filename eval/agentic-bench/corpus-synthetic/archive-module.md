# Orchard Archive module

This is a fictional benchmark fixture, not an AKB product specification.

## Purpose
The Archive module stores demonstration inventory snapshots.

## Module contract
The module MUST verify a checksum before accepting each snapshot.
It MUST return a receipt containing the checksum after successful storage.
It must NEVER upload local files without an explicit caller request.
It must NEVER accept a snapshot whose checksum fails verification.
