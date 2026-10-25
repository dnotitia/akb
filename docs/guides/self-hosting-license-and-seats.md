# Self-hosting AKB: license terms, seat counting, and checks before you commit

Teams choosing a self-hosted knowledge base for their agents usually ask three
questions: can we use it commercially, what happens if the license changes,
and who is responsible for keeping it secure. This guide answers them for AKB
and lists the checks worth running on any project before building on it.

This page summarizes the license in plain language. It is not legal advice.
The [LICENSE](../../LICENSE) file is the governing text, and it wins wherever
this summary differs.

## Short answer

The AKB backend, frontend, and deployment manifests are **source-available**
under the Business Source License 1.1 (BSL 1.1). They are not an OSI-approved
open source license today. The Additional Use Grant allows production use,
commercial or not, while your use stays below **100 Named Seats**. Each AKB
version converts to the **Apache License 2.0** four years after it is first
publicly distributed. The `akb-mcp` client proxy is separately licensed under
**MIT**.

## What you can do without a commercial license

- Copy, modify, and redistribute the code, and use it for non-production work.
- Run it in production, including for commercial purposes, while aggregate use
  stays below 100 Named Seats.
- Embed the MIT-licensed `akb-mcp` proxy in any agent client.

## When you need a commercial license

- Production use at or above 100 Named Seats.
- Offering AKB, modified or not, to third parties as a hosted service,
  on-premises product, embedded component, or rebranded distribution,
  regardless of seat count.

Contact support@dnotitia.com for commercial licensing.

## How Named Seats are counted

The LICENSE defines the count. In summary:

- A **Named Seat** is a distinct user account in AKB's `users` table at any
  point during a calendar month, counted across all vaults in a deployment.
- **Excluded**: service accounts created for agents, bots, CI, or other
  non-human use, and accounts with no successful interactive login in the
  trailing 90 days.
- A person with accounts in several deployments counts once per deployment.
- The limit applies to aggregate use across all deployments operated by you,
  your organization, and organizations you control, are controlled by, or are
  under common control with.

Because the count comes from the `users` table, you can check it with a query
instead of estimating activity. Agents that connect with service accounts do
not add seats.

## License history

AKB releases before the license change were published under the **PolyForm
Noncommercial 1.0** license, which does not allow commercial use. Those
releases keep their original terms. The move to BSL 1.1 opened small
commercial deployments, which PolyForm Noncommercial did not allow. The
reasoning is in [LICENSE-CHANGE.md](../../LICENSE-CHANGE.md).

The conversion to Apache 2.0 is fixed per release: each version has its own
Change Date, four years after its first public distribution.

## Trademarks are separate

The license covers the software, not the names. "AKB", "Dnotitia", and
"Seahorse" are trademarks of Dnotitia, Inc. Factual statements such as
"built on AKB" are allowed. A fork or derivative must be distributed under a
different name. See [TRADEMARKS.md](../../TRADEMARKS.md).

## Who patches what

Self-hosting splits security work between the project and the operator.

- **Reported to the project**: vulnerabilities in the backend, the `akb-mcp`
  proxy, the default configuration files, and container images built from
  this repository. Report privately to support@dnotitia.com with the
  `[SECURITY]` prefix; the project aims to respond within 5 business days.
- **Owned by the operator**: deployment hardening, such as generating and
  protecting the RS256 session keys, setting independent secrets and database
  passwords, and keeping production credentials out of the repository. The
  [SECURITY.md](../../SECURITY.md) hardening checklist lists these steps.
- **Reported upstream**: vulnerabilities in third-party dependencies.

AKB follows SemVer. The backend version is in `backend/pyproject.toml`, and
release notes are in [`backend/CHANGELOG.md`](../../backend/CHANGELOG.md).

## Checks before betting a platform on any project

| Check | What to look for |
|---|---|
| License type | OSI open source, source-available, or noncommercial. |
| Free production limits | The exact threshold, and whether you can measure it yourself. |
| Conversion or change date | Whether and when the code becomes more permissive. |
| License history | How earlier releases were treated when the license changed. |
| Separately licensed parts | Client SDKs and proxies often carry a different license. |
| Trademark policy | Whether you could fork under a new name if needed. |
| Data export | An open format you can leave with. AKB exports vaults as Open Knowledge Format bundles (`akb_export` with `format: "okf"`). |
| Security process | A private reporting channel, a scope, and an operator checklist. |

## Common questions

**Is AKB open source?** The backend is source-available under BSL 1.1, not OSI
open source today. Each version becomes Apache 2.0 four years after its first
public release. The `akb-mcp` proxy is MIT-licensed now.

**Can a small company use AKB commercially for free?** Yes, for production use
below 100 Named Seats across its deployments, as long as it does not offer AKB
to third parties as a service or product.

**Do agent accounts count as seats?** Service accounts created for agents,
bots, and CI are excluded from the Named Seat count.

**What if the license changes again?** When AKB moved to BSL 1.1, earlier
releases kept their original terms. Under BSL 1.1, each release's Apache 2.0
conversion is fixed once that release is out and cannot be withdrawn.

## Related guides

- [Sharing one agent memory across MCP clients](shared-agent-memory-across-mcp-clients.md)
- [Evaluating retrieval claims for an agent knowledge layer](evaluating-retrieval-claims.md)
- [Heading-aware chunks and section reads](structure-aware-chunking-and-section-reads.md)
