# AKB Help Bootstrap 고도화 설계

작성일: 2026-05-06

## 1. 결론

AKB의 help는 단순한 tool reference가 아니라 **agent bootstrap protocol**이 되어야 한다.

1차 설계는 다음 세 가지 레이어로 고정한다.

```text
akb_help
  = agent에게 무엇을 먼저 읽을지 알려주는 router

REST Markdown endpoints
  = AKB가 생성·검증한 기준 guide 배포 채널

proxy / script materializer
  = REST guide bundle을 local workspace의 .akb/ 아래에 저장하는 실행 채널
```

핵심 결정:

| 영역 | 결정 |
|---|---|
| 전역 guide | `GET /api/v1/help/akb-agent-guide.md` |
| vault guide 원본 | 각 vault 안의 `overview/vault-guide.md` 문서 |
| vault guide REST | `GET /api/v1/vaults/{vault}/vault-guide.md` |
| optional focused guide | `overview/guides/vault-{feature}-guide.md` |
| collection guide | `{collection}/_guide.md` |
| local materialized bundle | workspace의 `.akb/` 디렉토리 |
| local entrypoint | `.akb/index.md` |
| local vault registry | `.akb/vaults/index.md` |
| MCP/proxy 편의 도구 | `akb_bootstrap(vault?, vaults?, output_dir?)` |
| CLI/script | `npx akb-mcp bootstrap --vault <vault> --out ./.akb` 또는 repeated `--vault` |

LLM이 헷갈리지 않도록 이름은 **역할 + scope**로 붙인다.

```text
akb-agent-guide.md
  AKB 전체 사용법. vault와 무관한 전역 guide.

vault-guide.md
  vault owner가 작성·관리하는 primary 운영 guide. 이 파일이 vault의 기준 contract다.

vault-{feature}-guide.md
  ingest, tables, relations, publishing처럼 특정 기능의 규칙이 길어질 때만 만드는 focused guide.

{collection}/_guide.md
  특정 collection의 쓰기 정책, 문서 타입, 태그, 예시를 설명하는 collection guide.

index.md
  local bundle의 시작점. 어떤 guide를 어떤 순서로 읽을지 알려주는 locator.

vaults/index.md
  local에 받아둔 여러 vault guide의 registry. 한 사용자가 여러 vault를 쓰는 것을 기본 전제로 둔다.
```

로컬 materialization은 단일 파일이 아니라 작은 guide bundle이다.

```text
.akb/
├── index.md
├── akb-agent-guide.md
└── vaults/
    ├── index.md
    ├── {vault-a}/
    │   └── vault-guide.md
    └── {vault-b}/
        ├── vault-guide.md
        ├── guides/                  # optional, explicitly referenced focused guides
        │   └── vault-{feature}-guide.md
        └── collections/             # optional, materialized collection guides
            └── {collection}-guide.md
```

agent는 항상 `.akb/index.md`를 먼저 읽는다. `index.md`는 전역 guide와 vault registry의 위치를 알려준다. 작업할 vault가 정해지면 `.akb/vaults/{vault}/vault-guide.md`를 읽고, 여러 vault가 후보이면 `.akb/vaults/index.md`로 현재 local cache에 있는 vault guide를 확인한다.

## 2. 문제 정의

현재 `akb_help`는 progressive disclosure 형태의 정적 help dictionary다.

장점:

- MCP tool만 있는 agent도 즉시 사용법을 알 수 있다.
- topic별로 짧게 drill-down할 수 있다.
- E2E 테스트가 쉽다.

한계:

- vault별 문맥이 없다.
- table schema, collection convention, relation convention이 동적으로 반영되지 않는다.
- agent가 매번 `akb_help`, `akb_browse`, `akb_search`, `akb_get`을 시행착오로 조합해야 한다.
- 긴 작업에서 help 내용을 local workspace에 안정적으로 고정하기 어렵다.
- `akb_help(topic="quickstart")`가 너무 길어지면 tool response가 부풀고, 너무 짧으면 실제 bootstrap 지시가 부족하다.

따라서 help의 역할을 바꾼다.

```text
기존:
  help = tool 설명서

목표:
  help = agent가 AKB를 안전하게 쓰기 위한 boot sequence
```

## 3. 설계 원칙

### 3.1 One Obvious Path

agent가 처음 AKB를 만났을 때는 선택지가 많으면 안 된다.

권장 순서는 항상 같다.

```text
1. akb_help(topic="quickstart")
2. 가능하면 global .akb/ guide cache를 materialize
3. .akb/index.md와 .akb/akb-agent-guide.md를 읽고 local guide cache 사용법 확인
4. 작업 vault가 정해지면 해당 vault guide를 local cache에 추가
5. .akb/vaults/{vault}/vault-guide.md 확인
6. vault-guide가 지시하는 focused guide 또는 collection guide만 추가로 읽는다
7. 동적 상태는 guide 파일이 아니라 akb_browse / akb_sql / akb_relations 같은 tool로 확인한다
```

`akb_help`는 "모든 내용을 기억하라"가 아니라 "이 문서를 가져가라"를 말한다.

### 3.2 Backend는 기준 제공, Proxy는 로컬 I/O

프로젝트 규칙상 로컬 파일 접근은 proxy-only다.

따라서 backend는 Markdown을 생성하고 반환한다.

```text
backend REST:
  Markdown 생성
  권한 검사
  vault guide 제공
  bootstrap index 생성

proxy / CLI:
  HTTP 호출
  local path 쓰기
  stale 여부 확인
```

backend가 local path를 받거나 파일을 쓰면 안 된다.

### 3.3 Vault Guide는 AKB 문서여야 한다

vault별 guide를 DB 외부 설정 파일로 두지 않는다.

`overview/vault-guide.md`는 일반 AKB document다.

이유:

- Git history가 남는다.
- `akb_get`, `akb_search`, `akb_browse`로 읽을 수 있다.
- owner/writer가 AKB tool로 수정할 수 있다.
- 다른 문서, 테이블, 파일과 relation을 맺을 수 있다.
- vault clone/mirror/export 시 함께 보존된다.

### 3.4 Focused Guide는 명시적이고 선택적이어야 한다

`vault-context.md`처럼 넓은 이름의 합성 문서는 만들지 않는다. 필요하면 목적이 드러나는 focused guide를 만든다.

원칙:

- `vault-guide.md`가 항상 primary contract다.
- focused guide는 `overview/guides/vault-{feature}-guide.md` 형식을 쓴다.
- collection guide는 기존 `{collection}/_guide.md` 형식을 유지한다.
- 전역 guide와 vault guide는 각 focused guide를 언제 읽어야 하는지만 알려준다.
- collection/table/file 목록과 schema 같은 동적 상태는 guide 파일에 snapshot으로 고정하지 않고 tool call로 확인한다.

## 4. Agent Bootstrap Contract

모든 agent-facing 문서와 `akb_help(topic="quickstart")`는 아래 contract를 공유한다.

```text
When using AKB:
1. If a local shell or proxy bootstrap tool is available, create/read .akb/index.md first.
2. Read .akb/akb-agent-guide.md for the local guide cache protocol.
3. If a target vault is known, make sure .akb/vaults/{vault}/vault-guide.md exists and read it.
4. If multiple vaults may be relevant, read .akb/vaults/index.md and pick the target vault before writing.
5. Treat overview/vault-guide.md as the vault owner's instructions.
6. Do not invent collection, table, tag, or relation conventions when the guide defines them.
7. Use akb_browse before writing into an unfamiliar vault.
8. Use akb_search for semantic discovery and akb_grep for exact identifiers.
9. Use akb_get or akb_drill_down before editing an existing document.
10. Use akb_edit for small exact edits; use akb_update only when replacing the whole document is intentional.
11. Link related resources with AKB URIs when the guide asks for provenance or dependency tracking.
12. If the guide is missing or stale, report that and offer to create/update overview/vault-guide.md.
```

이 contract는 `akb_help`, REST guide, CLI 출력, proxy tool 출력에 중복해서 들어간다. LLM은 같은 문장을 여러 채널에서 보아야 덜 흔들린다.

## 5. REST API 설계

### 5.1 `GET /api/v1/help/akb-agent-guide.md`

전역 AKB guide를 반환한다.

인증:

- 기본값: 인증 불필요
- 배포 설정에서 auth-required로 바꿀 수 있음
- 민감한 vault 정보는 절대 포함하지 않음

Response:

```http
HTTP/1.1 200 OK
Content-Type: text/markdown; charset=utf-8
ETag: "akb-help-v1-<hash>"
Cache-Control: public, max-age=300
Content-Disposition: inline; filename="akb-agent-guide.md"
```

본문 구조:

```markdown
# AKB Agent Guide

## Bootstrap
## Local Guide Cache Protocol
## Guide File Map
## Multi-Vault Workflow
## Resource Model
## Tool Selection
## Read Patterns
## Write Patterns
## Search Patterns
## Graph Patterns
## Local Materialization
## Troubleshooting
```

`akb-agent-guide.md`는 `akb_help` 이후의 second-stage bootstrap 문서다. `akb_help(topic="quickstart")`는 이 문서를 local로 내려받는 방법을 짧게 알려주고, 실제 local guide cache 운영법은 `akb-agent-guide.md`가 설명한다.

필수 내용:

- `.akb/index.md`를 먼저 읽는 규칙
- 여러 vault guide를 `.akb/vaults/{vault}/` 아래에 누적 cache하는 규칙
- 새 vault 작업을 시작할 때 `akb_bootstrap(vault="{vault}")` 또는 CLI repeated `--vault`를 쓰는 방법
- `include_guides=true`는 vault guide가 명시적으로 링크한 focused/collection guide만 추가로 내려받는다는 규칙
- 현재 상태는 local Markdown이 아니라 AKB tool로 refresh해서 확인한다는 규칙

`Guide File Map`은 전역 guide 안에서 반드시 유지한다. agent는 이 섹션만 읽어도 guide 파일들의 역할과 위치를 알 수 있어야 한다.

예시:

```markdown
## Guide File Map

| role | local path | source | when to read |
|---|---|---|---|
| entrypoint | `.akb/index.md` | `/api/v1/help/bootstrap-index.md?vault={vault}` | Always first |
| global guide | `.akb/akb-agent-guide.md` | `/api/v1/help/akb-agent-guide.md` | Learn AKB-wide rules |
| vault registry | `.akb/vaults/index.md` | generated by proxy/CLI from cached vault guides | When choosing among multiple vaults |
| vault guide | `.akb/vaults/{vault}/vault-guide.md` | `/api/v1/vaults/{vault}/vault-guide.md` | Learn owner-authored vault rules |
| focused guide | `.akb/vaults/{vault}/guides/vault-{feature}-guide.md` | `akb_get(vault, doc_id="overview/guides/vault-{feature}-guide.md")` | Only when vault-guide links it |
| collection guide | `.akb/vaults/{vault}/collections/{collection}-guide.md` | `akb_get(vault, doc_id="{collection}/_guide.md")` | Before writing to that collection |
```

### 5.2 `GET /api/v1/vaults/{vault}/vault-guide.md`

vault owner가 작성한 원본 guide를 반환한다.

인증:

- reader 이상 필요

Response:

```http
HTTP/1.1 200 OK
Content-Type: text/markdown; charset=utf-8
ETag: "akb-vault-guide-<vault>-<commit-or-hash>"
Cache-Control: private, max-age=60
Content-Disposition: inline; filename="vault-guide.md"
```

원본 문서가 없으면 404 대신 200으로 missing-guide template을 반환할지, 404로 명확히 실패시킬지 선택할 수 있다. 권장 default는 200 + missing guide 안내다. agent가 bootstrap 중 실패 대신 다음 행동을 얻을 수 있기 때문이다.

### 5.3 `GET /api/v1/help/bootstrap-index.md?vault={vault}`

선택 endpoint다. CLI와 proxy가 한 번만 호출해도 되도록 만든다.

동작:

- `vault` 없음: global bundle index를 반환
- `vault` 있음: global guide + primary vault guide 위치를 알려주는 index를 반환
- `vault`는 반복 가능하다. 예: `?vault=product&vault=seahorse-kb`
- focused guide와 collection guide는 `vault-guide.md`가 명시적으로 링크한 것만 index에 포함한다

이 endpoint는 사람이 직접 기억할 필요가 없다. script/proxy용 convenience path다.

## 6. Vault Guide와 Focused Guide 구성

`overview/vault-guide.md`가 vault의 primary guide다. 이 파일 하나가 agent에게 다음을 알려줘야 한다.

```text
1. 이 vault의 목적과 금지 범위
2. agent boot rules
3. 읽어야 할 optional focused guide 목록
4. collection별 쓰기 정책과 collection guide 위치
5. table / relation / file 사용 규칙을 확인하는 방법
6. 현재 상태를 확인할 tool call
7. 첫 행동 추천
```

`vault-context.md` 같은 넓은 합성 문서는 만들지 않는다. "context"는 너무 포괄적이라 LLM이 primary guide와 우선순위를 헷갈릴 수 있다.

### 6.1 Primary Vault Guide

`GET /api/v1/vaults/{vault}/vault-guide.md`는 `overview/vault-guide.md` 원문을 반환한다.

우선순위:

1. `overview/vault-guide.md`
2. legacy fallback: `overview/welcome.md`
3. generated missing-guide template

원칙:

- guide는 owner-authored contract다.
- dynamic snapshot을 자동 합성하지 않는다.
- 현재 collection/table/file/graph 상태는 tool call로 확인하게 지시한다.
- focused guide와 collection guide의 경로를 명시한다.

### 6.2 Focused Guide Naming

규칙이 길어질 때만 focused guide를 만든다.

경로:

```text
overview/guides/vault-{feature}-guide.md
```

예시:

| guide | when to create |
|---|---|
| `overview/guides/vault-ingest-guide.md` | source별 ingest, dedup, provenance 규칙이 길 때 |
| `overview/guides/vault-tables-guide.md` | table schema, SQL 예시, write policy가 길 때 |
| `overview/guides/vault-relations-guide.md` | relation type, graph provenance 규칙이 길 때 |
| `overview/guides/vault-publishing-guide.md` | publish/snapshot/password/embed 규칙이 길 때 |
| `overview/guides/vault-access-guide.md` | member role, public_access, grant/revoke 정책이 길 때 |

focused guide는 `vault-guide.md`의 `Guide Map`에 링크된 경우에만 agent가 읽는다.

### 6.3 Collection Guide Naming

collection별 guide는 기존 template 구조와 맞춰 `{collection}/_guide.md`를 유지한다.

예시:

```text
overview/_guide.md
artifacts/_guide.md
artifacts/proposals/_guide.md
atlassian-pages/_guide.md
```

로컬 bundle에 materialize할 때는 파일 시스템에서 다루기 쉬운 이름으로 변환한다.

```text
.akb/vaults/{vault}/collections/overview-guide.md
.akb/vaults/{vault}/collections/artifacts-guide.md
.akb/vaults/{vault}/collections/artifacts__proposals-guide.md
```

collection guide는 다음 경우에만 읽는다.

- `vault-guide.md`가 해당 collection guide를 명시적으로 지시할 때
- agent가 해당 collection에 쓰려고 할 때
- user가 특정 collection 작업을 요청했을 때

### 6.4 Dynamic State Retrieval

현재 상태는 guide 파일에 snapshot으로 박지 않는다.

agent는 작업 직전에 tool로 조회한다.

| Need | Tool pattern |
|---|---|
| accessible vaults | `akb_list_vaults()` |
| current collections/docs | `akb_browse(vault="{vault}", depth=2)` |
| current tables | `akb_browse(vault="{vault}", content_type="tables")` |
| table schema and rows | `akb_sql(vault="{vault}", sql="SELECT ...")` |
| current files | `akb_browse(vault="{vault}", content_type="files")` |
| exact guide/document path | `akb_grep(vault="{vault}", pattern="...")` |
| semantic source discovery | `akb_search(vault="{vault}", query="...")` |
| graph neighborhood | `akb_graph(vault="{vault}", resource_uri="...")` |
| one resource relations | `akb_relations(vault="{vault}", resource_uri="...")` |

이 방식은 stale snapshot 문제를 줄이고, guide 파일을 "정책과 탐색법"에 집중시킨다.

### 6.5 Recommended First Actions

`vault-guide.md`는 agent가 다음 tool call을 바로 고를 수 있게 끝나야 한다.

예시:

```markdown
## Recommended First Actions

If you are exploring:

1. akb_browse(vault="seahorse-kb", depth=2)
2. akb_search(vault="seahorse-kb", query="<your topic>")

If you are writing to a collection:

1. Read this `vault-guide.md`.
2. Read `{collection}/_guide.md` if present.
3. Search for source docs.
4. Create or edit the document.
5. Link sources with `derived_from` or `references` when required.
```

## 7. `overview/vault-guide.md` Vault Guide Template

새 vault 생성 시 template 여부와 무관하게 `overview/vault-guide.md`를 seed한다.

기본 template:

```markdown
# AKB Vault Guide

## Purpose

Describe what this vault is for and what it is not for.

## Agent Boot Rules

- Start by reading this guide.
- Browse before writing.
- Preserve source provenance when creating summaries, concept pages, or artifacts.
- Prefer exact edits with `akb_edit` for small changes.

## Guide Map

Read these only when relevant:

| guide | path | when to read |
|---|---|---|
| ingest guide | overview/guides/vault-ingest-guide.md | before importing or consolidating sources |
| table guide | overview/guides/vault-tables-guide.md | before creating, altering, or querying tables |
| relation guide | overview/guides/vault-relations-guide.md | before creating provenance or dependency edges |

## Collections

| collection | owner | write policy | purpose |
|---|---|---|---|
| overview | humans | edit carefully | vault operating docs and focused guides |

Before writing to a collection, read `{collection}/_guide.md` if it exists.

## Document Types And Tags

Use these document types:

| type | when to use |
|---|---|
| note | lightweight record |
| report | synthesized analysis |
| decision | durable decision with rationale |
| spec | technical or product specification |
| plan | future work |
| reference | stable reference material |

Tag conventions:

- `topic:<slug>` for concept grouping
- `source:<system>` for imported source families
- `area:<slug>` for organizational area

## Tables

| table | purpose | write policy |
|---|---|---|

Tables are dynamic. Before querying or writing table data, call:

```text
akb_browse(vault="<vault>", content_type="tables")
akb_help(topic="tables")
```

## Relation Rules

| relation | required when |
|---|---|
| derived_from | generated or curated work depends on source material |
| references | background citation |
| attached_to | file belongs to doc/table |
| depends_on | one doc cannot be understood without another |

## Workflows

### Explore

1. Browse the vault.
2. Search semantically.
3. Read the best source documents.

### Write

1. Confirm target collection.
2. Read `{collection}/_guide.md` if present.
3. Reuse document type and tag conventions.
4. Link sources.
5. State uncertainty.

## Do Not

- Do not create new top-level collections without updating this guide.
- Do not edit generated ingest collections unless this guide explicitly allows it.
- Do not drop or alter tables without admin approval.
```

## 8. `akb_help` 변경

### 8.1 Root help

`akb_help()`는 현재처럼 전체 tool map을 보여주되, 맨 위에 bootstrap 지시를 추가한다.

추가 문구:

```markdown
## Start Here

For a durable local guide bundle, ask the proxy/CLI to materialize `.akb/`.

- First bootstrap global guide: `akb_bootstrap(output_dir="./.akb")`
- Then read: `.akb/index.md` and `.akb/akb-agent-guide.md`
- Then add task vaults: `akb_bootstrap(vaults=["product", "seahorse-kb"], output_dir="./.akb")`
- Local entrypoint: `.akb/index.md`
- Global guide: `GET /api/v1/help/akb-agent-guide.md`
- Vault source guide: `GET /api/v1/vaults/{vault}/vault-guide.md`
- Multiple vaults: use `akb_bootstrap(vaults=[...])` or CLI repeated `--vault`
- Optional focused guides: linked from `overview/vault-guide.md`
- Collection guides: `{collection}/_guide.md`, read before writing to that collection
- MCP/proxy: `akb_bootstrap(vault="{vault}", output_dir="./.akb")`
- MCP/proxy multi-vault: `akb_bootstrap(vaults=["product", "seahorse-kb"], output_dir="./.akb")`
- CLI: `npx akb-mcp bootstrap --vault {vault} --out ./.akb`
- CLI multi-vault: `npx akb-mcp bootstrap --vault product --vault seahorse-kb --out ./.akb`

If you cannot download files, call `akb_help(topic="quickstart")` and then `akb_get(vault="{vault}", doc_id="overview/vault-guide.md")`.
```

### 8.2 `quickstart`

`akb_help(topic="quickstart")`는 긴 tutorial보다 boot sequence를 우선한다.

구성:

```markdown
# Quick Start

## Preferred Bootstrap
## Read akb-agent-guide.md
## Add One Or More Vault Guides
## MCP-only Bootstrap
## Read Before Write
## Common First Tasks
## When You Are Unsure
```

### 8.3 새 topic

추가 topic:

| topic | 내용 |
|---|---|
| `bootstrap` | local `.akb/` guide bundle 생성/갱신 방법 |
| `guide-files` | guide 파일 naming convention과 탐색 순서 |
| `vault-guide` | `overview/vault-guide.md` 작성 규칙 |
| `agent-contract` | agent용 행동 규칙만 압축 |

`akb_help(topic="vault")`는 ambiguous할 수 있으므로 쓰지 않는다. `vault-guide`를 공식 topic으로 쓴다.

## 9. Proxy / CLI 설계

### 9.1 MCP proxy tool: `akb_bootstrap`

proxy가 `tools/list`에 주입한다.

Schema:

```json
{
  "name": "akb_bootstrap",
  "description": "Download AKB agent guide files into a local .akb/ bundle.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vault": {
        "type": "string",
        "description": "Optional vault name. If set, downloads the primary vault guide."
      },
      "vaults": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional list of vault names for multi-vault users. Mutually compatible with vault; duplicates are ignored."
      },
      "include_guides": {
        "type": "boolean",
        "default": false,
        "description": "Also download focused guides and collection guides explicitly linked from the vault guide."
      },
      "output_dir": {
        "type": "string",
        "default": "./.akb",
        "description": "Local directory for the guide bundle. Proxy-only local filesystem access."
      },
      "force": {
        "type": "boolean",
        "default": false,
        "description": "Refresh generated files even when they already exist."
      }
    }
  }
}
```

Behavior:

```text
always:
  GET /api/v1/help/bootstrap-index.md?vault={vault?}
  GET /api/v1/help/akb-agent-guide.md
  write/update .akb/index.md
  write/update .akb/vaults/index.md

vault provided:
  GET /api/v1/vaults/{vault}/vault-guide.md

vaults provided:
  for each vault in vaults:
    GET /api/v1/vaults/{vault}/vault-guide.md

include_guides=true:
  follow guide links declared in vault-guide.md
  read focused guides with akb_get or REST document read
  read collection guides only when explicitly linked

write generated files under output_dir
return {output_dir, entrypoint, vault, files, refreshed_at}
```

Safety:

- reject directory traversal outside allowed local path policy if the proxy already has one
- only write files under `output_dir`
- overwrite generated files with AKB bootstrap markers; refuse to overwrite unrelated files unless `force=true`
- create parent directories for the guide bundle
- never send local file content back to backend

### 9.2 CLI

Add `bootstrap` subcommand to `akb-mcp`.

Examples:

```bash
npx akb-mcp bootstrap --out ./.akb
npx akb-mcp bootstrap --vault seahorse-kb --out ./.akb
npx akb-mcp bootstrap --vault product --vault seahorse-kb --out ./.akb
npx akb-mcp bootstrap --vault seahorse-kb --out ./docs/akb-guides --force
```

Environment:

```text
AKB_URL
AKB_PAT
```

Default:

```text
AKB_URL = https://akb.agent.seahorse.dnotitia.com
out = ./.akb
```

CLI output should be terse and machine-readable enough for agents.

```text
Wrote ./.akb guide bundle for vault seahorse-kb (3 files, plus linked guides if requested)
Next: read ./.akb/index.md before calling AKB write tools.
```

## 10. Local Guide Bundle과 Staleness Model

Local `.akb/` guide bundle can go stale. Make that visible without creating another complex sync system.

Bundle layout:

```text
.akb/
├── index.md
├── akb-agent-guide.md
└── vaults/
    ├── index.md
    ├── product/
    │   └── vault-guide.md
    └── seahorse-kb/
        ├── vault-guide.md
        ├── guides/
        │   └── vault-ingest-guide.md       # optional
        └── collections/
            └── artifacts-guide.md          # optional
```

`index.md`는 전역 guide 문서에서 모든 파일을 잘 찾아갈 수 있도록 만든 locator다.

예시:

```markdown
# AKB Guide Index

Generated: 2026-05-06T10:15:30+09:00
Target vault: seahorse-kb

Read in this order:

1. `./akb-agent-guide.md`
2. `./vaults/index.md` if more than one vault may be relevant
3. `./vaults/seahorse-kb/vault-guide.md`
4. Any focused or collection guide linked by the vault guide and relevant to the task

Remote / MCP sources:

| local file | source |
|---|---|
| `akb-agent-guide.md` | `/api/v1/help/akb-agent-guide.md` |
| `vaults/index.md` | generated by proxy/CLI from cached vault guides |
| `vaults/product/vault-guide.md` | `/api/v1/vaults/product/vault-guide.md` |
| `vaults/seahorse-kb/vault-guide.md` | `/api/v1/vaults/seahorse-kb/vault-guide.md` |
| `vaults/seahorse-kb/guides/vault-ingest-guide.md` | `akb_get(..., doc_id="overview/guides/vault-ingest-guide.md")` |
| `vaults/seahorse-kb/collections/artifacts-guide.md` | `akb_get(..., doc_id="artifacts/_guide.md")` |
```

`.akb/vaults/index.md` 예시:

```markdown
# AKB Vault Guide Registry

Generated: 2026-05-06T10:15:30+09:00

| vault | local guide | source | refreshed_at |
|---|---|---|---|
| product | `./product/vault-guide.md` | `/api/v1/vaults/product/vault-guide.md` | 2026-05-06T10:15:30+09:00 |
| seahorse-kb | `./seahorse-kb/vault-guide.md` | `/api/v1/vaults/seahorse-kb/vault-guide.md` | 2026-05-06T10:15:30+09:00 |
```

Every generated guide includes:

```markdown
Generated: 2026-05-06T10:15:30+09:00
Source ETag: ...
Vault guide commit: abc1234
```

Agent rule:

```text
If .akb/index.md is older than the current session or the user asks about a different vault, refresh the bundle.
```

Proxy behavior:

- If `output_dir` exists and generated files have matching ETag markers, return "already current".
- If ETag differs and `force=false`, either overwrite only generated file with AKB marker or ask caller to pass `force=true`.

Local file marker:

```markdown
<!-- akb-bootstrap:
vault=seahorse-kb
file_role=vault-guide
etag=...
generated_at=2026-05-06T10:15:30+09:00
do_not_edit=true
-->
```

로컬 `.akb/` 파일을 직접 고쳐도 기준 원본이 바뀌지 않는다. Vault 지시는 `overview/vault-guide.md`를 수정하고, AKB 전역 지시는 backend global guide template을 수정한다.

## 11. Missing Guide Behavior

If `overview/vault-guide.md` is missing, the vault guide endpoint still returns 200.

It includes a prominent warning:

```markdown
## Vault Guide Missing

This vault does not have `overview/vault-guide.md` yet.

Before doing non-trivial writes, create it with `akb_put` using the template below.
```

For readers:

- show the missing-guide warning
- say writes should wait until a writer creates the guide

For writers/admins:

- include ready-to-use `akb_put` call

Example:

```python
akb_put(
  vault="seahorse-kb",
  collection="overview",
  title="Vault Guide",
  type="reference",
  tags=["system:guide", "akb:bootstrap"],
  content="<template body>"
)
```

After creation, the path will be `overview/vault-guide.md` because title `Vault Guide` slugifies to `vault-guide`.

## 12. Implementation Plan

### Phase 1: Backend guide generation

- Add `backend/app/services/help_guide_service.py`.
- Move long-form global Markdown out of `backend/mcp_server/help.py` into reusable templates.
- Add REST routes:
  - `GET /api/v1/help/akb-agent-guide.md`
  - `GET /api/v1/help/bootstrap-index.md?vault={vault}`
  - `GET /api/v1/vaults/{vault}/vault-guide.md`
- Use existing services for:
  - vault access check
  - document lookup
- Add guide map conventions for focused guides and collection guides.
- Keep `akb_help` return shape unchanged: `{"help": "..."}`

### Phase 2: `akb_help` topic refresh

- Update root help start section.
- Replace quickstart with bootstrap-first guide.
- Add topics:
  - `bootstrap`
  - `guide-files`
  - `vault-guide`
  - `agent-contract`
- Keep existing topic names compatible.

### Phase 3: Vault guide seeding

- Update `DocumentService.create_vault`.
- For non-external-git vaults, always seed:
  - `.vault.yaml`
  - `overview/vault-guide.md`
- If a vault template is provided, include template collection registry in `overview/vault-guide.md`.
- Keep existing per-collection `_guide.md` behavior.

### Phase 4: Proxy and CLI materializer

- Add proxy-injected `akb_bootstrap`.
- Add `akb-mcp bootstrap` CLI subcommand.
- Reuse the same downloader/writer helper.
- Bump `packages/akb-mcp-client/package.json` version before publish.

### Phase 5: Tests

Backend E2E:

- `akb_help(topic="quickstart")` mentions `.akb/index.md`.
- `akb_help(topic="quickstart")` instructs agents to read `.akb/akb-agent-guide.md` after first bootstrap.
- `GET /api/v1/help/akb-agent-guide.md` returns Markdown and no vault data.
- `GET /api/v1/help/bootstrap-index.md?vault={vault}` returns file locator Markdown.
- `GET /api/v1/help/bootstrap-index.md?vault=a&vault=b` returns multi-vault locator Markdown.
- `GET /api/v1/vaults/{vault}/vault-guide.md` requires reader access.
- vault guide endpoint returns `overview/vault-guide.md` when present.
- vault guide endpoint includes missing-guide warning when absent.
- global guide explains focused guide and collection guide naming.
- vault guide template tells agents to use `akb_browse` / `akb_sql` for dynamic state.

Proxy E2E:

- `tools/list` includes `akb_bootstrap`.
- `akb_bootstrap(output_dir=...)` writes `.akb/index.md`.
- `akb_bootstrap(output_dir=...)` writes `.akb/akb-agent-guide.md`.
- `akb_bootstrap(vault=...)` writes global guide and primary vault guide.
- `akb_bootstrap(vaults=[...])` writes multiple vault guides and `.akb/vaults/index.md`.
- `akb_bootstrap(vault=..., include_guides=true)` writes explicitly linked focused guides when present.
- stale ETag path works.
- overwrite safety works.

CLI E2E:

- `npx akb-mcp bootstrap --out <tmp>` writes global guide bundle.
- `npx akb-mcp bootstrap --vault <vault> --out <tmp>` writes vault guide bundle.
- `npx akb-mcp bootstrap --vault a --vault b --out <tmp>` writes multi-vault guide bundle.

## 13. Acceptance Criteria

- A new agent can start from one local entrypoint: `.akb/index.md`.
- A multi-vault user can keep several vault guides under `.akb/vaults/{vault}/`.
- `.akb/vaults/index.md` lists cached vault guides and their refresh metadata.
- `akb_help(topic="quickstart")` tells the agent exactly how to create and read the `.akb/` guide bundle.
- Every newly created non-mirror vault has `overview/vault-guide.md`.
- Existing vaults without `overview/vault-guide.md` get a clear generated warning and creation template.
- Current collection/table/file/graph state is queried by tools, not frozen into a generated snapshot file.
- Optional focused guides follow `overview/guides/vault-{feature}-guide.md`.
- Collection guides remain `{collection}/_guide.md`.
- No backend code writes local user files.
- Proxy/script local writes are explicit and path-scoped.
- Existing `akb_help` E2E tests keep passing with compatible output.

## 14. Non-Goals

- Do not replace `akb_help` with REST only.
- Do not make the backend depend on local filesystem paths.
- Do not put vault-specific sensitive data in the global guide.
- Do not require agents to guess which generated file is the entrypoint; `.akb/index.md` is always first.
- Do not make `overview/vault-guide.md` a hidden dotfile; it should be browseable by ordinary AKB tools.
- Do not invent a full sync daemon for local `.akb/` in v1.

## 15. Open Questions

| Question | Recommended default |
|---|---|
| Should global guide require auth? | No, unless deployment config requires it. |
| Should `akb_bootstrap` overwrite existing guide files? | Only if generated marker is present or `force=true`. |
| Should focused guides be downloaded by default? | No. Download only primary guides by default; use `include_guides=true` for linked guides. |
| Should `overview/vault-guide.md` be editable by writers? | Yes, but guide should recommend owner/admin review for major convention changes. |
| Should MCP resources expose these guides too? | Later. REST + tool help + proxy materialization is enough for v1. |
