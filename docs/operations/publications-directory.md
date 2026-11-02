# Publications directory

The authenticated `/publications` page manages existing publications across
the caller's readable Vaults. Its Vault selector, name search, availability
and expiry filters are URL-backed. Scrolling appends another server page;
changing a filter queries the complete accessible inventory before paging.
Create a publication from its source document, table or file page.

## API

`GET /api/v1/publications` requires an authenticated credential with read
scope. It returns publications only from Vaults readable by the current
account, including signed-in default access. Each request rechecks current
account authority. A credential's Vault scope limits writes, following the
existing token contract; it does not narrow reads.

| Parameter | Values and default |
| --- | --- |
| `vault` | Optional exact Vault name; absent selects all readable Vaults |
| `q` | Case-insensitive literal search of title, path, slug and Vault name; up to 200 characters |
| `status` | `active` (default), `all`, `unavailable` |
| `expiry` | `any` (default), `none`, `soon`, `long-running` |
| `resource_type` | Optional `document`, `table_query`, or `file` |
| `page_size` | 1–100, default 30 |
| `cursor` | Opaque continuation from the previous response |

`soon` means an expiry in the next seven days. `long-running` selects active
publications created at least 30 days ago. An unavailable publication is
expired or has reached its view limit. Password protection is independent
of these availability states.

The response contains `publications`, `total`, `complete`, `next_cursor` and
`observed_at`. `total` counts matching rows before the continuation boundary;
`complete` is true when no continuation remains. Each row retains the normal
publication response and adds `resource_path`, `availability`, `can_unpublish`
and `unpublish_disabled_reason`. Storage keys and password hashes are omitted.
Native documents resolve their current path by resource identity, avoiding a
different document that later reuses an old path.

Continuations use creation time and slug as stable sort keys. A signed cursor
binds the account, credential and filters, and expires after 30 minutes.
Invalid, expired or mismatched cursors return 400; restart from the first page.
The observation time excludes newly created publications during traversal.
Each page rechecks ACLs and management authority; a cursor never grants access
after a membership is revoked. Counts and availability can change as resources
are updated or removed between requests.

`can_unpublish` is an observation for rendering controls. Actual deletion still
uses `DELETE /api/v1/publications/{vault}/{slug}` with its existing permission,
archive, mirror, managed-Vault and credential checks. The UI confirms this action
and refreshes the directory after it succeeds.

The existing `GET /api/v1/publications/{vault}` retains its complete-response
contract. The global directory is additive. Deploy the backend supporting the
new route together with the frontend that exposes the global page; no schema
migration or change to anonymous publication URLs is required.

## Validation

`backend/tests/test_publication_directory.py` creates disposable databases and
checks ACL visibility, management capability, filters before pagination,
literal search, cursor binding and expiry, native identity, safe responses and
HTTP authentication. It runs in the required live-PostgreSQL CI lane.
`frontend/e2e/public-links-directory.spec.ts` uses isolated HTTP fixtures for
scope/filter history, automatic paging, failure recovery and responsive layout.
