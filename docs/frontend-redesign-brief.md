# AKB Frontend 재설계 브리프

> 작성일: 2026-04-22
> **버전 맥락: 프론트엔드 v0.5 → v1.0 재설계 진행 중**
>   - **v0.5 (현재)**: React 19 + Tailwind 4 기반 "Schematic Editorial" 프로토타입. 읽기·브라우즈·PAT·퍼블리케이션·그래프 뷰까지 구현. 편집/업로드/테이블쓰기 등은 Agent(MCP) 경로로만 동작.
>   - **v1.0 (목표)**: 본 문서의 §3~§8이 재설계 스코프 정의. 핵심 결정은 [§1 Agent-first 3갈래](#1-설계-철학-agent-first)와 [부록 B 결정 대기 항목](#부록-b-확인이-필요한-항목).
> 목적: 프론트엔드 재설계를 위한 시스템·백엔드·데이터·API 전수 분석
> 현재 main: `ebfad5c` (external-git mirror + cross-encoder rerank + admin users 반영)
> 관련 문서: [`handover-analysis.md`](./handover-analysis.md) (2026-04-20, 전반 인수인계 문서) — 본 문서는 **재설계 관점**으로 보강

---

## 목차

0. [한눈에 보기](#0-한눈에-보기)
1. [설계 철학: Agent-first](#1-설계-철학-agent-first) ⭐ v1.0 스코프를 가르는 결정
2. [시스템 아키텍처](#2-시스템-아키텍처)
3. [핵심 개념 & 도메인 모델](#3-핵심-개념--도메인-모델)
4. [백엔드 REST API 전체 맵](#4-백엔드-rest-api-전체-맵)
5. [MCP 도구 표면 (참고)](#5-mcp-도구-표면-참고)
6. [현재 프론트엔드(v0.5) 구조](#6-현재-프론트엔드v05-구조)
7. [주요 사용자 여정 & 화면 매핑](#7-주요-사용자-여정--화면-매핑)
8. [v1.0 재설계 고려사항](#8-v10-재설계-고려사항)
9. [최근 13커밋 변경 요약](#9-최근-13커밋-변경-요약)
10. [v1.0 디자인 시스템 방향](#10-v10-디자인-시스템-방향) ⭐ 컨셉·컬러·타이포 권고

---

## 0. 한눈에 보기

**AKB (Agent Knowledgebase)** — Confluence/Notion을 대체하는 **Git 네이티브 + RAG + MCP 퍼스트** 조직 메모리.

- **두 종류 사용자**: (1) **사람** — 웹 UI / (2) **AI 에이전트** — MCP 스트리밍 HTTP
- **세 종류 콘텐츠**: **Document (Markdown)**, **Table (구조화 데이터, 실제 PG 테이블)**, **File (바이너리, S3)** — 모두 하나의 검색 인덱스에 공존
- **하나의 권한 경계**: **Vault** — Git bare repo + RBAC 적용 단위
- **저장 계층**: PG=진실의 원천 · Qdrant=파생 인덱스 · Git=버전 관리 · S3=바이너리
- **프론트엔드는 REST-only** (MCP는 에이전트 전용, 웹 UI는 건드리지 않음)

### 프론트엔드 재설계가 다뤄야 할 주된 주체

| 엔티티 | UI 대표 표현 (현재) | 가능한 개선 축 |
|---|---|---|
| Vault | 사이드바 트리 + `/vault/:name` | 권한/공개설정/external-git/아카이브 상태 뱃지 |
| Collection (디렉토리) | 트리 노드 | 빈 컬렉션 UX, 다중 선택 |
| Document | 단일 페이지 뷰어 + outline | 편집 UI 부재, frontmatter 미노출 |
| Table | 스키마 + SELECT * 50rows | DDL/DML UI 전무 (akb_sql만 존재) |
| File | 메타데이터 + 다운로드 | 인라인 프리뷰 부분구현, 업로드 UI 전무 |
| Publication | 문서 페이지 내 토글 | 목록·관리·snapshot UI 전무 |
| Edge (Knowledge Graph) | Force-graph 뷰 | 관계 CRUD UI 전무 (MCP만) |
| Session/Todo/Memory | UI 전무 | 에이전트 전용으로 남겨야 할지? |

---

## 1. 설계 철학: Agent-first

**재설계를 시작하기 전에 반드시 합의해야 할 전제.**

AKB의 근본 명제는 **"Agent가 쓰고, 사람은 (주로) 읽는다"**. 즉, 현재 UI의 쓰기 기능 공백은 **버그가 아니라 의도된 범위**다.

### 근거

**`README.md` line 3**:
> "AI 에이전트가 직접 지식을 저장하고 조회하는 Git 기반 지식베이스"

**`README.md` line 25** (Document 정의):
> "에이전트가 읽고 쓰기에 최적화된 형식"

**`docs/collaboration-design.md`** 시나리오 A~F 전부 agent가 MCP 도구를 호출하는 구조:
```
[영로 에이전트] "engineering에 API 리뷰 결과 저장해줘"
  → akb_put(vault="engineering", ...)    ← agent가 호출
  → 내부에서 Git commit + PG insert + 인덱싱 자동
```

### 쓰기 경로 자동화 내역 (agent가 `akb_put` **한 번** 호출하면 전부 자동)

1. **Git commit** — worktree에서 per-vault lock으로 직렬화
2. **PG `documents` + `chunks` INSERT** (embedding=NULL 플래그)
3. **frontmatter 파싱 → edges 자동 추출** (`depends_on`/`related_to`/`implements` + markdown 링크 + `akb://` URI)
4. **백그라운드**: `embed_worker` → 임베딩 → `vector_indexer` → Qdrant 업서트
5. **External-git 볼트**: `metadata_worker`가 LLM으로 `summary`/`tags`/`doc_type`/`domain` 자동 채움

→ **사람이 `git commit`·`push`를 하지 않음**. bare repo가 서버 안에 있고 worktree가 연결돼 있어 agent 호출 = 즉시 반영.

### 이것이 UI 재설계에 주는 함의

현재 UI에 "없는 것"들의 실제 의미:

| 기능 | "UI 없음"의 원래 의도 |
|---|---|
| 문서 편집 | Agent에게 자연어로 요청 → `akb_update`/`akb_edit` |
| 파일 업로드 | Agent에게 "X파일 올려줘" → `akb_put_file` (proxy가 S3 직접) |
| 테이블 쓰기 | Agent에게 "이 데이터 추가" → `akb_create_table`/`akb_sql` |
| 관계 생성 | frontmatter에 쓰면 자동, 혹은 `akb_link` |
| Git 커밋/푸시 | 서버 내부 bare repo로 자동 |

### 재설계 시 세 갈래 (기술적 결정)

| 방향 | 설명 | Pros | Cons |
|---|---|---|---|
| **A. Read-only UI 고수** | 지금처럼 UI = 읽기·브라우즈·설정·공유. 쓰기는 전부 Agent | 설계 철학 일관, 구현 최소 | Agent 없는 사용자 배제 |
| **B. 동등한 쓰기 UI 추가** | 편집기·업로드·테이블 CRUD·관계 생성 모두 UI로 | 사람 UX 친숙, 자립 가능 | 두 진입점 동기화 비용, 디자인 복잡도↑ |
| **C. Agent 통합 UI** | UI 안에 프롬프트 입력 → Agent에 위임 (예: "이 섹션 다듬어줘" 버튼) | 철학 유지 + 접근성↑ | Agent 런타임 통합 필요 (웹에서 Anthropic/OpenRouter 호출 구조) |

→ **이 결정이 v1.0 전체 재설계 스코프를 결정**. 부록 B의 #7 결정 대기 항목과 직결.

### 관련 문서 지도

| 문서 | 대상 | 비고 |
|---|---|---|
| `README.md` | 전체 개요 | 설계 철학·MCP 도구 요약 |
| `CLAUDE.md` | 개발자 | 프로젝트 규칙 (프록시 vs 백엔드 분리 원칙 포함) |
| `docs/collaboration-design.md` | 설계 | Agent 협업 시나리오 A~F |
| `docs/handover-analysis.md` | 인수인계 | 전영역 상세 분석 |
| `docs/benchmark-research.md` | 기획 | 경쟁 제품 비교 (Confluence/Notion 등) |
| **`akb_help` MCP 도구** | **Agent 런타임** | Progressive-disclosure 헬프 — 카테고리(quickstart/documents/search/tables/files/access/memory/sessions/publishing/relations/todos) + 워크플로(research/onboarding/data-tracking/link-resources) + 도구별 상세. **에이전트가 외부 문서 없이 self-contained로 사용법 학습** |
| `templates/` | 초기 데이터 | vault 템플릿 7종 + 문서 템플릿 10종(PRD/runbook/OKR/ADR 등) |

---

## 2. 시스템 아키텍처

### 2.1 4-Layer (최신 상태)

```
┌──────────────────────────────────────────────────────────────────┐
│ Access Layer                                                     │
│  Web UI (React 19)  │  REST /api/v1/*  │  MCP /mcp/ (Streamable) │
├──────────────────────────────────────────────────────────────────┤
│ MCP Proxy (client side, Node ESM)                                │
│  akb-mcp npm — local FS / S3 presigned I/O                       │
├──────────────────────────────────────────────────────────────────┤
│ Core Services (Python 3.11 FastAPI)                              │
│  document · search · rerank · index · git · kg · publication     │
│  table · file · access · auth · external-git                     │
│  + Workers: embed / vector_indexer / metadata / external_git_poll│
├──────────────────────────────────────────────────────────────────┤
│ Storage Layer                                                    │
│  PostgreSQL 16 + pgvector │ Qdrant (hybrid) │ Git (bare+worktree)│
│  S3 (파일 바이너리) │ OpenRouter (LLM·embedding·rerank)          │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 중요한 설계 원칙 (UI 재설계에도 영향)

1. **PG=진실, Qdrant=파생** — 쓰기 경로에서 Qdrant는 절대 동기로 건드리지 않음. 즉, `POST /documents`가 **즉시 반환**되고 이후 백그라운드에서 임베딩·벡터·메타데이터 채움 → UI에 "인덱싱 상태" 힌트를 줄 수 있음 (`/health`의 `pending` 카운트).
2. **소스가 다양해도 chunks는 하나** — 새 스키마는 `source_type` ∈ {document, table, file} + `source_id`. **검색 결과가 이질 타입 혼재**로 돌아옴. 프론트는 타입별 아이콘/액션 분기 필수.
3. **External-git 볼트는 read-only** — `akb_create_vault(external_git={...})`로 만든 볼트는 모든 쓰기 도구가 403. UI는 이걸 명확히 표시 + 쓰기 컨트롤 숨겨야 함.
4. **Crash-safe ordering** — 모든 워커가 `FOR UPDATE SKIP LOCKED` + NULL 플래그 드레인 구조. UI 낙관 업데이트도 안전.
5. **파일 I/O는 무조건 프록시** — 백엔드는 바이너리 접근 안 함. 웹 UI도 S3 presigned URL 사용 (업로드는 `/api/v1/files/.../upload` → PUT to presigned → `/confirm`).

### 2.3 기술 스택 (현행 확정)

| 영역 | 기술 | 비고 |
|---|---|---|
| Backend | Python 3.11, FastAPI, asyncpg, httpx | async-first |
| LLM / Embedding / Rerank | **OpenRouter** (Qwen3.5-35B-A3B / Qwen3-embed-8b / Cohere rerank v3.5) | 내부 GW에서 이전 (70958dd, cc53779) |
| DB | PostgreSQL 16 + pgvector 4096d | IVFFLAT 미사용, 벡터는 Qdrant 전담 |
| Vector | Qdrant v1.12.4 | dense cosine + sparse BM25 + RRF + cross-encoder rerank |
| Git | bare repo + persistent worktree + per-vault lock | `asyncio.to_thread` |
| Auth | JWT (24h) + PAT (`akb_*`, sha256) | |
| Frontend | React 19, Router 7, Vite, Tailwind 4, Radix UI | shadcn 베이스 |
| Viz | react-force-graph-2d | 지식 그래프 |
| Proxy | Node 20 ESM, zero deps | v0.5.1 on npm |

---

## 3. 핵심 개념 & 도메인 모델

### 3.1 계층 (4-Level)

```
Vault (L0) — 권한 경계, Git repo, 외부 git 미러 여부
└─ Collection (L1) — 디렉토리 (문서 그루핑, 경로)
   ├─ Document (L2) — Markdown + YAML frontmatter, 버전관리
   │  └─ Chunk (L3) — heading 분할, embedding vector, BM25 sparse
   ├─ Table (L2) — 실제 PG 테이블, 스키마 + rows
   └─ File (L2) — S3에 저장된 바이너리
```

### 3.2 불변 식별자 규칙

- **Vault**: `name` (lowercase, 하이픈·숫자 허용). UI URL도 이 이름 사용.
- **Document**: DB PK=UUID, 사용자 노출 ID=`d-{8-hex}` (예: `d-94d8657f`). 조회는 `document_repo.find_by_ref()`가 **UUID / short ID / path substring** 모두 매칭 → 프론트는 뭐든 넘겨도 됨.
- **Table**: `name` per vault.
- **File**: UUID(file_id).
- **Publication**: 짧은 **slug** + UUID(publication_id).
- **Edge**: AKB URI 형식 `akb://{vault}/doc/{path}`, `akb://{vault}/table/{name}`, `akb://{vault}/file/{id}`.

### 3.3 Document 타입·상태

- **type**: `note` (default) / `report` / `decision` / `spec` / `plan` / `session` / `task` / `reference`
- **status**: `draft` → `active` → `archived` | `superseded`
- **tags**: 자유 문자열 배열, PG GIN 인덱스
- **domain**: 자유 문자열 (engineering/product/ops/legal…) — 현재 UI에서 거의 안 씀
- **summary**: 자동 생성(LLM) 또는 수동

### 3.4 관계 (Edges)

**6종 관계 타입** (enum):
- `depends_on` / `related_to` / `implements` / `references` / `attached_to` / `derived_from`

**크로스 타입 링크**: doc↔doc, doc↔table, doc↔file, table↔file 등 모두 가능 (URI 기반).

**자동 추출**: Document frontmatter의 `depends_on`/`related_to` + markdown 링크 + `akb://` URI를 put/update 시 자동 파싱해서 edges 생성.

### 3.5 RBAC

```
ROLE_HIERARCHY = { owner: 4, admin: 3, writer: 2, reader: 1 }
```

판정 순서: (1) `is_admin` bypass → (2) `vault.owner_id` → (3) `public_access` → (4) `vault_access` 테이블.

**추가 제약**:
- Archived 볼트: 모든 쓰기 차단
- External-git 볼트: writer 역할도 쓰기 차단 (read-only)

### 3.6 Public Access

1. **Vault `public_access`**: `none` / `reader` / `writer` — 로그인 없이 vault 접근
2. **Publication (slug 기반)**: document / table_query / file 단위로 퍼블릭 링크 발급
   - 옵션: `password`(bcrypt), `max_views`, `expires_in`(1h/7d/30d/never), `mode`(live|snapshot), `allow_embed`

### 3.7 External Git 미러 (신규)

- **생성 시점**: `akb_create_vault(external_git={url, branch, auth_token, poll_interval_secs})`
- **동작**: 포ller 워커가 주기적으로 `ls-remote HEAD` 체크 → 변경 감지 시 `git fetch` + **blob-sha 기반 reconcile** (diff 파싱 아님 → force-push/rebase도 수렴)
- **인덱싱**: 일반 문서처럼 chunks INSERT (embedding NULL) → embed_worker → vector_indexer. 추가로 `metadata_worker`가 LLM으로 summary/tags/doc_type/domain 채움
- **UI 시사점**:
  - 볼트 생성 UI에 "외부 Git 연결" 섹션 추가 필요
  - 볼트 뷰에 미러 상태 표시 (upstream URL, 마지막 동기화, 인덱싱 진행률)
  - 쓰기 버튼들 모두 disable + 툴팁 ("이 볼트는 외부 Git 미러입니다")

---

## 4. 백엔드 REST API 전체 맵

모든 엔드포인트는 `/api/v1` prefix. `Authorization: Bearer <jwt|pat>` 헤더 사용.

### 4.1 Auth & PAT

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| POST | `/auth/register` | public | 회원가입 (username, email, password, display_name?) |
| POST | `/auth/login` | public | 로그인 → JWT 반환 |
| GET | `/auth/me` | 인증 | 현재 사용자 정보 (`is_admin` 포함) |
| POST | `/auth/tokens` | 인증 | PAT 생성 (`akb_*`, scopes, expires_days) — **raw token은 여기서만 노출** |
| GET | `/auth/tokens` | 인증 | PAT 목록 (prefix만, 원본 복구 불가) |
| DELETE | `/auth/tokens/{id}` | 인증 | PAT 폐기 |

### 4.2 Vault & Access

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| GET | `/my/vaults` | 인증 | 내가 접근 가능한 볼트 목록 (+role, public_access) |
| GET | `/vaults` | 인증 | 동일 |
| POST | `/vaults` | 인증 | 볼트 생성 (query: name, description?, template?, public_access?) |
| GET | `/vaults/{name}/info` | reader | 상세 정보 (owner, 문서수, 크기, 역할) |
| GET | `/vaults/{name}/members` | reader | 멤버 목록 + 역할 |
| POST | `/vaults/{name}/grant` | admin | 사용자에게 역할 부여 (body: user, role) |
| POST | `/vaults/{name}/revoke` | admin | 접근 회수 |
| POST | `/vaults/{name}/transfer` | admin | 소유권 이전 (body: new_owner) |
| POST | `/vaults/{name}/archive` | admin | 아카이브 → 쓰기 차단 |
| GET | `/users/search` | 인증 | 유저 검색 (query: q, limit) — grant 전 사용 |
| DELETE | `/my/account` | 인증 | 본인 계정 삭제 (소유 볼트 cascade) |
| GET | `/admin/users` | **admin flag** | 전체 유저 목록 |
| DELETE | `/admin/users/{user_id}` | **admin flag** | 다른 유저 삭제 (self-delete는 400) |

> ℹ️ **`akb_create_vault` MCP 도구는 `external_git` 파라미터를 지원**하지만, REST `POST /vaults`는 현재 query-param 기반이라 external-git 생성을 REST로 하려면 확장이 필요.

### 4.3 Documents

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| POST | `/documents` | writer | 문서 생성/업서트 (vault, collection, title, content, type, tags, depends_on, related_to, metadata) |
| GET | `/documents/{vault}/{doc_id}` | reader | 문서 전체 조회 (content + metadata) |
| PATCH | `/documents/{vault}/{doc_id}` | writer | 부분 업데이트 (제공된 필드만) + custom commit message |
| DELETE | `/documents/{vault}/{doc_id}` | writer | 삭제 (Git commit + 벡터 제거) |

### 4.4 Browse & Search

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| GET | `/browse/{vault}` | reader | 계층 브라우징 (query: collection?, depth 1-2) → **items는 collection\|document\|table\|file 혼재** |
| GET | `/search` | 인증 | **하이브리드 검색** (dense+BM25 RRF + cross-encoder rerank) — query: q, vault?, collection?, type?, tags?, limit. 결과는 `source_type`+`source_id` 포함 |
| GET | `/grep` | 인증 | 리터럴/정규식 검색 (q, regex?, case_sensitive?) — line-level matches |
| GET | `/drill-down/{vault}/{doc_id}` | reader | 헤딩 기반 섹션 분할 반환 |

### 4.5 Knowledge Graph

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| GET | `/relations/{vault}/{doc_id}` | reader | 1-hop 관계 (direction, type filter) |
| GET | `/graph/{vault}` | reader | 볼트 또는 문서 중심 BFS 그래프 (depth 1-5, limit ≤200) |
| GET | `/provenance/{doc_id}` | 인증 | 생성자·수정자·Git 버전 히스토리 |

### 4.6 Activity & History

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| GET | `/recent` | 인증 | 최근 문서 변경 (vault? 필터) |
| GET | `/activity/{vault}` | reader | Git log (author, since, collection 필터) |
| GET | `/diff/{vault}/{doc_id}?commit={hash}` | reader | 특정 커밋 diff (unified format) |

### 4.7 Sessions & Memory

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| POST | `/sessions/start` | 인증 | 에이전트 작업 세션 시작 (vault, agent_id, context?) |
| POST | `/sessions/{id}/end` | 인증 | 세션 종료 + summary |
| POST | `/memory` | 인증 | 메모리 저장 (content, category) — 벡터 인덱싱됨 |
| GET | `/memory` | 인증 | 메모리 회수 (category?, limit) |
| DELETE | `/memory/{id}` | 인증 | 메모리 삭제 |
| DELETE | `/memory/category/{cat}` | 인증 | 카테고리 일괄 삭제 |

### 4.8 Tables

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| POST | `/tables/{vault}` | writer | 테이블 생성 (columns: name/type/nullable/primary_key/default) |
| GET | `/tables/{vault}` | reader | 테이블 목록 + row_count + 스키마 |
| POST | `/tables/{vault}/sql` | **reader (SELECT) / writer (DML) / admin (DDL)** | SQL 실행 (vaults 배열로 크로스-볼트) |
| DELETE | `/tables/{vault}/{table}` | admin | 테이블 drop |

> ℹ️ 현재 프론트엔드는 `POST /tables/{vault}/sql`로 `SELECT * LIMIT 50`만 실행. DDL/DML UI는 없음.

### 4.9 Files

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| POST | `/files/{vault}/upload` | writer | **presigned URL 발급** (query: filename, collection, mime_type) → 클라이언트가 직접 S3 PUT |
| POST | `/files/{vault}/{file_id}/confirm` | writer | 업로드 완료 확인 → DB 상태 변경 + 인덱싱 트리거 |
| GET | `/files/{vault}/{file_id}/download` | reader | **presigned GET URL 발급** |
| GET | `/files/{vault}` | reader | 파일 목록 |
| DELETE | `/files/{vault}/{file_id}` | writer | 파일 삭제 |

### 4.10 Publications (퍼블릭 공유)

**관리 API** (인증 필요):

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| POST | `/publications/{vault}/create` | writer | 퍼블리케이션 생성 (resource_type: document\|table_query\|file, password?, max_views?, expires_in?, mode, allow_embed) |
| GET | `/publications/{vault}` | reader | 볼트의 퍼블리케이션 목록 |
| DELETE | `/publications/{vault}/{pub_id}` | writer | 삭제 |
| POST | `/publications/{vault}/{pub_id}/snapshot` | writer | table_query 스냅샷 freeze |

**퍼블릭 API** (인증 불필요):

| 메서드 | 경로 | 권한 | 기능 |
|---|---|---|---|
| POST | `/public/{slug}/auth` | public | 패스워드 검증 → HMAC 토큰 (1h TTL) 발급 |
| GET | `/public/{slug}/meta` | public | 메타데이터 조회 (view_count는 증가 안 함) |
| GET | `/public/{slug}` | public | 리소스 렌더 (doc/table/file dispatch) — view_count 증가 |
| GET | `/public/{slug}/embed` | public | iframe-friendly 버전 |
| GET | `/public/{slug}/raw` | public | 파일/텍스트 스트리밍 (≤5MB) |
| GET | `/public/{slug}/download` | public | 첨부 다운로드 |
| GET | `/oembed?url=...` | public | oEmbed 응답 (Slack/Discord unfurl) |

### 4.11 Health

| 경로 | 용도 | 반환 |
|---|---|---|
| `/livez` | kubelet liveness | `{status: "alive"}` (즉시 200) |
| `/readyz` | Service readiness | DB(hard) + Qdrant(soft). 30s 캐시 |
| `/health` | 대시보드 | **워커 pending 상태 포함**: embed_backfill / external_git / metadata_backfill / qdrant.backfill + bm25_vocab_size |

→ **UI에서 "인덱싱 진행 중" 배지를 만들 때 이 엔드포인트 유용**. 볼트 생성 직후, external-git 볼트의 초기 클론, 대량 업로드 후 상태 표시 등에 쓸 수 있음.

---

## 5. MCP 도구 표면 (참고)

MCP는 AI 에이전트 전용이지만, 재설계 관점에서 **"에이전트가 하는 일을 사람이 UI로도 할 수 있어야 하는가?"** 판단 근거로 중요.

### 5.1 총 58개 도구 = 54 backend-native + 4 proxy-injected

| 카테고리 | 도구 수 | 예시 |
|---|---:|---|
| Vault | 5 | `akb_list_vaults`, `akb_create_vault`(external_git 지원), `akb_vault_info`, `akb_vault_members`, `akb_delete_vault` |
| Document | 8 | `akb_put`, `akb_get`, `akb_update`, `akb_edit`(정확 문자열 치환), `akb_delete`, `akb_browse`, `akb_drill_down` |
| Search | 3 | `akb_search`(하이브리드+rerank), `akb_grep`(+replace 모드) |
| Relations/Graph | 4 | `akb_link`, `akb_unlink`, `akb_relations`, `akb_graph`, `akb_provenance` |
| Tables | 4 | `akb_create_table`, `akb_sql`, `akb_alter_table`, `akb_drop_table` |
| Files | **3 (proxy)** + content in browse | `akb_put_file`, `akb_get_file`, `akb_delete_file` (+ `file` param in `akb_put`/`akb_update`) |
| Publications | 4 | `akb_publish`, `akb_unpublish`, `akb_publications`, `akb_publication_snapshot` |
| Sessions/Activity | 5 | `akb_session_start/end`, `akb_activity`, `akb_diff`, `akb_history` |
| Todos | 3 | `akb_todo`, `akb_todos`, `akb_todo_update` |
| Memory | 3 | `akb_remember`, `akb_recall`, `akb_forget` |
| Admin | 10 | `akb_grant`, `akb_revoke`, `akb_search_users`, `akb_whoami`, `akb_transfer_ownership`, `akb_archive_vault`, `akb_set_public`, ... |
| Help | 1 | `akb_help(topic?)` — 에이전트용 문서 |

### 5.2 "MCP에만 있고 UI에 없는 것" (재설계에서 추가 검토 대상)

1. **`akb_link` / `akb_unlink` (관계 CRUD)** — 현재 UI는 그래프 뷰어뿐, 사람이 관계를 만들/끊을 방법 없음
2. **`akb_grep --replace` (문서 일괄 치환)** — 리팩토링성 작업
3. **`akb_edit` (정확 문자열 치환)** — 부분 편집 UX가 가능
4. **`akb_todos`, `akb_remember`/`akb_recall`** — 사람이 쓰는 UI 가치가 애매하면 에이전트 전용으로 남김
5. **`akb_session_start/end` + `akb_activity`** — UI에 "활동 타임라인" 도입 여지
6. **`akb_create_table` / `akb_alter_table` / `akb_sql` 쓰기** — 현재 읽기만 가능
7. **`akb_put_file`** — 웹 업로드 UI 전무 (현재 REST는 presigned 흐름 가능하지만 프론트 미구현)

---

## 6. 현재 프론트엔드(v0.5) 구조

### 6.1 디렉토리

```
frontend/src/
├── main.tsx                  # 라우터 진입점
├── index.css                 # Tailwind 4 + 커스텀 토큰
├── pages/                    # 11개 페이지
│   ├── home.tsx             # 대시보드 + PAT + MCP 클라이언트 설정
│   ├── auth.tsx             # 로그인/회원가입
│   ├── vault.tsx, vault-new.tsx
│   ├── document.tsx, table.tsx, file.tsx
│   ├── graph.tsx
│   ├── search.tsx
│   ├── settings.tsx         # PAT 관리 + admin user 관리
│   └── public-publication.tsx
├── components/
│   ├── layout.tsx, vault-shell.tsx, vault-explorer.tsx
│   ├── doc-outline.tsx
│   ├── password-gate.tsx
│   ├── table-viewer.tsx, file-viewer.tsx, json-tree.tsx
│   └── ui/                  # shadcn 베이스
├── hooks/
│   ├── use-vault-tree.ts    # browse(depth=2) 한 방 + 트리 빌드
│   ├── use-doc-outline.ts   # 스크롤 스파이
│   └── use-measured-height.ts
└── lib/
    ├── api.ts               # fetch 래퍼 + 전체 엔드포인트
    ├── markdown.ts, tree-route.ts, utils.ts
```

### 6.2 라우트

```
/auth                                   → AuthPage (public)
/p/:slug                                → PublicationPage (public)

                  [Layout, JWT 필수]
/                                       → HomePage
/vault/new                              → VaultNewPage
/vault/:name/graph                      → GraphPage
/search                                 → SearchPage
/settings                               → SettingsPage

                  [VaultShell (좌측 트리)]
/vault/:name                            → VaultPage
/vault/:name/doc/:id                    → DocumentPage
/vault/:name/table/:table               → TablePage
/vault/:name/file/:id                   → FilePage
```

### 6.3 상태 관리

- **React hooks only** (Redux/Zustand/React Query 미사용)
- **인증 토큰**: 메모리 캐시 + `localStorage["akb_token"]`, 401 시 자동 `/auth` 리다이렉트
- **트리 확장 상태**: `localStorage["akb-explorer-expanded:{vault}"]`
- **퍼블리케이션 토큰**: `sessionStorage` (탭 단위)
- **검색 상태**: URL query param (`?q=...&mode=...&v=...`)

### 6.4 디자인 시스템 (v0.5 현재)

> ⚠️ **전용 디자인 시스템 문서 없음**. 토큰·유틸은 `frontend/src/index.css` (277줄)에만 존재. Storybook·Figma·컴포넌트 카탈로그 없음.
> "Schematic Editorial"은 `index.css:7` CSS 주석 한 줄이 근거의 전부 — 공식 디자인 언어로 문서화되지 않음. v1.0 방향 권고는 [§10](#10-v10-디자인-시스템-방향) 참조.

- **팔레트**: paper `#faf9f5` / ink `#0a0908` / smoke `#75716b` / whisper `#ecebe6` / spark `#ff4d12` / ember `#c63d09`
- **타이포그래피**: Fraunces (display, serif) + IBM Plex Sans (body) + IBM Plex Mono (코드·좌표)
- **특수 클래스**: `.coord` (10px mono uppercase), `.fade-up`, `.stagger`, `.dotted`, `.grain`, `.marquee-track`, `.prose`
- **radius**: `--radius-sm/md/lg: 0` — 모서리 샤프가 규약
- **다크모드**: CSS `@custom-variant dark` 선언만 있음, **실제 색 매핑·토글 UI 모두 없음**

### 6.5 식별된 한계 (v0.5 현재)

> ⚠️ 항목 1~5는 **Agent-first 설계의 의도된 범위**일 수 있음 ([§1 참조](#1-설계-철학-agent-first)). "버그"인지 "v0.5 스코프"인지 v1.0 재설계 결정에 달림.

1. **문서 편집 UI 전무** — 읽기 + publish 토글만. `akb_put/update/edit` API는 UI에서 호출 안 함. *(원래 Agent 영역)*
2. **파일 업로드 UI 전무** — REST `/files/.../upload` 흐름 미구현. *(원래 Agent 영역 — `akb_put_file`)*
3. **파일 프리뷰 페이지는 메타데이터만** (`file.tsx`) — `FileViewer` 컴포넌트는 있으나 퍼블리케이션에서만 사용
4. **테이블 DDL/DML UI 전무** — 스키마 보기 + `SELECT *` 50rows만. *(원래 Agent 영역 — `akb_sql`)*
5. **그래프는 읽기 전용** — 관계 생성/삭제 UI 없음. *(원래 Agent 영역 — `akb_link`/frontmatter)*
6. **Vault 생성 시 external_git 옵션 없음** — REST 자체가 query param만 받는 구조
7. **`/browse`는 depth=2 한 방 로드 가정** — 큰 볼트에선 lazy-load 필요
8. **다크모드 토글 없음**
9. **i18n 없음** — 모든 스트링 영어 하드코딩
10. **페이지네이션 없음** — 검색/리스트 모두 고정 limit
11. **Optimistic update 없음** — 모든 작업이 refetch 기반
12. **모바일 대응 미흡** — desktop-first 레이아웃
13. **Frontmatter 편집 UI 없음** — tags, domain, status 등을 사람이 바꿀 방법 없음

---

## 7. 주요 사용자 여정 & 화면 매핑

### 7.1 에이전트 도구 설정 (Agent Onboarding)

**현재**: Home 페이지 내 탭 그룹 (Cursor/Windsurf/Gemini/Claude Desktop/VSCode). PAT 발급 → 복사 → 각 도구 config 경로 제시.

**재설계 고려**:
- PAT 생성과 설정 스니펫을 한 흐름으로 (생성 → "이 JSON을 여기에 붙여넣으세요" 원클릭)
- "토큰을 이미 설정했나요?" 검증 UX (옵션)

### 7.2 볼트 브라우징 + 문서 읽기

**현재**: `/vault/:name` → 트리 선택 → `/vault/:name/doc/:id` → 마크다운 + outline + publish 버튼

**재설계 고려**:
- **type/status/tags 필터 바** (현재 검색으로만 가능)
- **frontmatter 인라인 편집 패널** (우측 레일에 통합)
- **최근 방문** 간단 리스트 (localStorage)
- **인덱싱 상태 배지** (embedding pending → "색인 중")

### 7.3 검색 (하이브리드 + 리터럴)

**현재**: `/search?q=...` — 두 개 탭(semantic/literal) 토글, 결과에 `source_type`이 섞여 나옴.

**재설계 고려**:
- **결과 타입별 섹션** (📄 문서 · 📊 테이블 · 📁 파일) 또는 탭
- **필터 사이드바** (vault/collection/type/tags/author)
- **검색어 하이라이트** (현재 `matched_section` 존재, 활용도 낮음)
- **최근 검색어** (localStorage)
- **저장된 검색** (→ 추후 MCP `akb_memory`와 연동 여지)
- cross-encoder rerank 덕에 quality는 좋음 → **점수 신뢰도 제시** (예: "semantic match 0.87")

### 7.4 공유 (Publication)

**현재**: Document 페이지 내 토글 ("Share publicly") → slug + URL. 파일은 public-publication에서만 렌더.

**재설계 고려**:
- **Publication 관리 페이지** 도입 (볼트별 목록, 뷰카운트, 만료, 재생성, 패스워드 변경)
- **공유 다이얼로그** (패스워드/만료/embed 설정)
- **테이블 쿼리 발행 UI** (현재 MCP만 가능)
- **스냅샷 freeze** UI (mode=snapshot 전환 버튼)

### 7.5 협업 (Vault 멤버 관리)

**현재**: Settings에서 본인 것만. 볼트별 멤버 관리 UI는 없음 (REST는 존재).

**재설계 고려**:
- `/vault/:name/members` 페이지 신설 — 멤버 리스트, 역할 드롭다운, grant/revoke/transfer
- 본인이 admin인 볼트에서만 보이게

### 7.6 외부 Git 미러 (신규 기능 지원)

**현재**: UI 지원 없음 (REST도 vault 생성 api 확장 필요).

**재설계 고려**:
- VaultNew 페이지에 "외부 Git 연결" 섹션 추가 (url, branch, auth_token, poll interval)
- 볼트 페이지에 미러 상태 카드 (upstream URL, 마지막 sync, pending 파일 수 — `/health` 활용)
- 모든 쓰기 컨트롤에 조건 분기 (`vault_external_git` 존재 시 비활성)

### 7.7 관리자 도구 (신규)

**현재**: Settings 02.03 섹션에 `GET /admin/users` + `DELETE /admin/users/{id}`. 간단한 리스트 + destructive confirm.

**재설계 고려**:
- 유저별 볼트/문서 카운트 + 마지막 로그인 시각
- 시스템 통계 (`/health`의 pending 합계, 볼트 총 수 등) 대시보드
- 감사 로그 (현재는 backend에도 없음 → 스펙 필요)

### 7.8 활동/히스토리

**현재**: Home에 `/recent` 최근 변경 일부. Activity(`/activity/{vault}`)와 Diff(`/diff/...`)는 UI 전무.

**재설계 고려**:
- 볼트 페이지에 "활동 타임라인" (git log 시각화, author 필터, 클릭 → diff 뷰)
- 문서 페이지에 "변경 이력" 드로어 (`akb_history` 등가)
- Diff 뷰어 (현재 API는 unified diff 반환, 전용 뷰어 필요)

---

## 8. v1.0 재설계 고려사항

### 8.1 정보 아키텍처 수준의 결정 포인트

1. **"문서-중심" vs "볼트-중심" 네비게이션**
   - 현재: 볼트-중심 (좌측 트리가 항상 볼트 컨텍스트)
   - 대안: 상단에 **크로스-볼트 검색·최근활동**을 전면 배치, 좌측 트리는 접을 수 있게 (개선 방향 — 유저가 여러 볼트를 왕래하는 워크플로우에 유리)

2. **검색이 홈 수준인지, 부가 기능인지**
   - 현재: 헤더에 검색창 + `/search` 페이지. 홈은 대시보드
   - 대안: 홈 = 검색 (Linear, Raycast 스타일). 볼트 트리는 사이드바

3. **편집 UX**
   - 현재: 편집 UI 전무 — **[§1의 A/B/C 결정](#1-설계-철학-agent-first)이 선행**
   - B/C 선택 시 옵션: (a) split-pane markdown editor (전통), (b) 블록 에디터 (Notion 스타일), (c) 인라인 편집 + 자동저장. **API는 `PATCH /documents` 준비됨**. `akb_edit`의 정확 문자열 치환을 활용하면 충돌 최소화 가능
   - C 선택 시: 편집 UI 없이 "이 섹션 다듬어줘" 같은 프롬프트 입력만 제공. Agent가 `akb_edit` 호출

4. **관계 UI의 위상**
   - 현재: 그래프 뷰(보기 전용) + document 페이지 우측 relation 섹션
   - 개선: 링크 생성 어포던스(문서 페이지에서 "관계 추가" 버튼 → URI 픽커) / 그래프에서 edge 드래그로 생성

5. **알림/실시간성**
   - 현재: 폴링도 아니고 1회 fetch
   - 옵션: WebSocket/SSE 필요한가? 대부분 단일 작업자 시나리오라면 수동 refresh 버튼 + `/health` 기반 배지로 충분할 수도

6. **모바일/태블릿 대응 정책**
   - 현재: 암시적 desktop-only
   - 결정 필요: 읽기만 모바일 지원? 편집까지? 네이티브 앱? (현재 권장: 읽기+공유만 모바일 1급, 편집은 데스크톱)

### 8.2 기술 스택 판단

**유지 권장**
- React 19 + Router 7 + Tailwind 4: 최신이고 충분
- shadcn 베이스: 재사용성 좋음

**도입 검토**
- **React Query (TanStack Query)** — 현재 수동 fetch, 캐싱/리페치/낙관업데이트에 유리. 이미 패키지는 설치되어 있음 (현재 미사용)
- **상태 관리 라이브러리** (Zustand/Jotai) — 인증 토큰·트리 확장상태 같은 공유 상태가 많아지면 필요. 현재 규모면 Context + hooks로도 됨
- **에디터**: `@uiw/react-md-editor` (전통), `lexical` (블록), `novel` (Tiptap 기반 Notion 스타일), 또는 Monaco (CodeMirror 대비 무겁지만 친숙)
- **키보드 팔레트**: `cmdk` — Raycast 스타일 빠른 네비게이션
- **i18n**: `react-i18next` 또는 `@lingui/react`. 한국어/영어 토글 필수 여부 확인 필요
- **테스트**: 현재 Vitest 단위만. Playwright E2E 도입 권장

**주의**
- **Server Components는 Vite에서 애매** — 유지하려면 CSR 모델 그대로. Next.js로 이전은 별건
- **Bundle 크기**: react-force-graph-2d가 무거움. 라우트 레벨 lazy import 권장

### 8.3 데이터 모델-UI 갭

| 백엔드가 제공하는데 UI에 없는 것 | 우선순위 | 비고 |
|---|---|---|
| frontmatter 편집 (tags, type, status, domain, summary) | 🔴 높음 | 중요한 메타데이터 |
| 문서 편집 자체 | 🔴 높음 | 핵심 기능 |
| 파일 업로드 | 🔴 높음 | 기본 CRUD |
| 볼트 멤버 관리 | 🟡 중간 | admin만 보이면 됨 |
| Publication 관리 페이지 | 🟡 중간 | 현재는 개별 문서에서만 |
| 관계 CRUD UI | 🟡 중간 | 그래프 viewer는 있음 |
| External-git 설정 UI | 🟡 중간 | 최근 신규 기능 |
| 테이블 DDL/DML | 🟢 낮음 | akb_sql로 가능, SQL 리터러시 요구 |
| 세션 타임라인 | 🟢 낮음 | 감사/디버깅 용도 |
| 메모리·Todo UI | 🟢 낮음 | 에이전트 전용으로 유지해도 됨 |

### 8.4 퍼포먼스 고려

- **`browse(depth=2)` 한 방 로드**: 500 문서 이하 볼트는 OK, 그 이상은 lazy-load 필요
- **검색 latency**: rerank 포함 warm ~2.5s. 스켈레톤/프로그레시브 렌더 필요
- **그래프**: 100 노드 이상이면 force simulation 느려짐. 클러스터링/필터링 UX 필요
- **인덱싱 pending 가시화**: `/health`를 주기 폴링(예: 15s)해서 볼트별 진행률 표시 — UX 신뢰도 +

### 8.5 보안·권한 UI 체크포인트

- **401 처리** (구현됨: 자동 `/auth` 리다이렉트)
- **403 처리** (현재: 에러 메시지만. 개선: 읽기 전용 모드로 fallback)
- **archived 볼트 표시** (현재 UI 미노출)
- **public_access 레벨 표시** (volta 목록에 뱃지)
- **본인이 수정 중인 문서의 lock / 충돌** (현재 없음; Git 기반이라 last-write-wins. 프론트에서 `current_commit` 체크 패턴 가능)

### 8.6 접근성

- 현재 키보드 네비 (트리 컴포넌트)는 상당히 잘 돼 있음 (arrow/home/end/pageup/pgdn/typeahead)
- **개선 필요**: 모달 focus trap, 스크린 리더 landmark, 색상 대비 (spark orange가 AAA 기준 못 맞출 수 있음), Reduced motion 옵션

---

## 9. 최근 13커밋 변경 요약

프론트엔드 재설계에 영향이 있는 변경 위주.

| 커밋 | 프론트 영향 |
|---|---|
| `70958dd` LLM/임베딩 OpenRouter 전환 | 없음 (백엔드 투명) |
| `cc53779` cross-encoder rerank 도입 | 검색 품질↑, **latency 2.5s warm** — 로딩 UX 중요도 상승 |
| `c4e9797`, `e4bf5b9` httpx/예외 정리 | 없음 |
| `ab87195` chunks → tables/files 일반화 | **검색 결과 UI가 source_type 분기 필요** |
| `90c1aeb` outbox sweep 인-워커 | `/health` 필드 변화 가능성 |
| `3a821e1` `document_id` drop, service rename | **검색 결과 필드 변경: `doc_id` → `source_id` + `source_type`** (api.ts 업데이트 필요 여부 점검) |
| `47ddfbb` `qdrant_*` → `vector_*` | 프론트 영향 없음 (백엔드 스키마) |
| `270aa4d` `akb_help` 업데이트 | 에이전트용 |
| `0917b25` **external-git 미러** | **vault 생성 UI + 볼트 뷰에 신규 상태 대응 필요** |
| `dfe338b` admin user 관리 | **신규 UI 섹션 이미 settings에 추가됨** |
| `9964fbb` PasswordCredential 저장 | 로그인 UX 개선됨 (재입력 최소화) |
| `ebfad5c` "DENSE" → "SEMANTIC" 칩, MCP 설정 탭 확장 | 마이너 폴리시 |

### 주목할 실질적 변경

1. **검색 결과 스키마가 바뀜** — `doc_id` 대신 `source_type` + `source_id`. 현재 `api.ts`의 `SearchDoc` 타입을 맞춰 확인/수정 필요 (이미 반영됐는지 확인 포인트)
2. **External-git 볼트 생성 UX** — 완전히 새 플로우. UI 설계에 넣어야 함
3. **Admin 화면** — 이미 간단한 버전 있음. 재설계 시 "시스템 상태 대시보드"로 확장 가능

---

## 10. v1.0 디자인 시스템 방향

> UI/UX Pro Max(디자인 인텔리전스 라이브러리) 기반 컨셉 추천. 161개 컬러 팔레트·57개 폰트 페어링·50+개 스타일 카탈로그에서 AKB 맥락(Agent-first · 개발자 · 읽기 중심 · Git 네이티브)에 매칭된 결과.

### 10.1 진단: 현재 컨셉의 문제

"Schematic Editorial" (Fraunces + Plex + spark orange + radius 0)은 personality는 강하나 **product ↔ style 미스매치**:

- 제품 본질: 개발자·AI 에이전트용 **기술적 생산성 툴** (검색·브라우징·그래프·테이블 중심)
- 적용 스타일: **Editorial Grid / Magazine** (매거진·장문 기사용)

→ 대부분 UI가 대시보드·네비게이션·데이터인데 매거진 타이포를 쓰니 "읽기 좋다"가 아니라 "장식적이다"로 읽힘.

### 10.2 후보 3안 비교

| 기준 | 안 A. IDE-Native | 안 B. Editorial Preserved | 안 C. Monochrome Editorial |
|---|---|---|---|
| **Style 기반** | Swiss Modernism 2.0 + Minimalism | 현재 Schematic Editorial 진화형 | Minimalist Monochrome |
| **컨셉** | 개발자 IDE·대시보드 | 각진 매거진 UI 유지 | Are.na·Readwise식 라이브러리 |
| **다크모드** | ⭐ 네이티브 우선 | 추가 필요 | 자연스럽게 흑백 반전 |
| **레퍼런스** | Linear · Raycast · Notion-for-Eng | 현재 v0.5 | Are.na · Readwise · NYT 앱 |
| **타이포** | IBM Plex Sans + JetBrains Mono | Fraunces + Plex 유지 | Playfair + Source Serif + JetBrains |
| **Product 적합도** | 🟢 높음 | 🟡 중간 | 🟡 중간 (에디토리얼 강) |
| **자산 재사용** | 🟢 Plex·spark 유지 | 🟢 전량 | 🔴 타이포 전면교체 |
| **차별화** | 🟡 같은 계열 경쟁자 존재 | 🟡 현행 유지 | 🟢 경쟁제품 대비 확실 |
| **운영 복잡도** | 🟢 낮음 | 🟢 낮음 | 🔴 triple serif 스택 |

### 10.3 ⭐ 권고: **안 A + 안 C 요소 하이브리드**

**전략**: 내부 UI는 안 A(개발자 친화)로 전환, "에디토리얼" 정체성은 외부 Publication에 농축.

- **내부 UI** (볼트 브라우저·문서뷰어·검색·테이블·그래프·설정): 안 A
  - 다크모드 네이티브, Developer Mono 타이포, IDE-familiar
- **외부 Publication** (`/p/:slug`): 안 C 요소
  - Fraunces/serif 보존, "편집된 지식물" 느낌, 받는 사람이 개발자 아닐 수 있음 감안
- **브랜드 DNA 보존**: radius 0, `.coord` 좌표형 레이블, spark orange `#ff4d12`

### 10.4 구체 스펙

#### 컬러 토큰

```css
/* ── 다크 모드 (기본) — Developer Tool 팔레트 기반 ───────── */
--color-background:  #0F172A;  /* slate-900 */
--color-surface:     #1B2336;
--color-border:      #334155;
--color-border-strong: #475569;
--color-foreground:  #F8FAFC;
--color-muted:       #272F42;
--color-muted-fg:    #94A3B8;
--color-primary:     #F8FAFC;
--color-accent:      #FF4D12;  /* ← 기존 spark 보존 (브랜드) */
--color-success:     #22C55E;  /* run green (인덱싱 완료 등) */
--color-destructive: #EF4444;
--color-ring:        #FF4D12;

/* ── 라이트 모드 — 기존 paper/ink 보존 ──────────────────── */
--color-background:  #FAF9F5;  /* ← 기존 paper */
--color-foreground:  #0A0908;  /* ← 기존 ink */
--color-surface:     #FFFFFF;
--color-border:      #0A0908;
--color-muted:       #ECEBE6;  /* ← 기존 whisper */
--color-muted-fg:    #75716B;  /* ← 기존 smoke */
--color-accent:      #FF4D12;  /* ← 기존 spark */
--color-success:     #16A34A;
--color-destructive: #C63D09;  /* ← 기존 ember */
```

#### 타이포그래피 매핑

**기반 페어링**: `Developer Mono` (IBM Plex Sans + JetBrains Mono) — 현재 자산 최대 재사용

| 용도 | 폰트 | 비고 |
|---|---|---|
| UI 전반 (버튼/라벨/네비/본문) | IBM Plex Sans | **보존** |
| 코드·ID·좌표·토큰 | **JetBrains Mono** 🆕 | 현 IBM Plex Mono를 대체 또는 공존. `d-94d8657f`, commit hash, `akb://` URI, PAT prefix, `/health` 수치 등 |
| `.coord` 유틸 | JetBrains Mono | 좌표형 label 정체성 보존 |
| Publication prose (`/p/:slug`) | **Fraunces** | ⭐ 외부 공유 문서에만 한정 — "내부는 도구, 밖으로는 에디토리얼" |
| 내부 문서 뷰어 본문 | IBM Plex Sans + `.prose` | Fraunces 제거 (장식적 미스매치 해결) |

**tabular-nums**: 메타데이터 테이블·타임라인·인덱싱 진행률에 적용 (숫자 정렬 정렬).

#### 레이아웃·모션

- **Grid**: 엄격한 12-컬럼 + 4/8px 스페이싱 시스템
- **Radius**: **0 보존** (`--radius-*: 0`) — 브랜드 DNA
- **Shadow**: 금지. Elevation은 border로 표현 (라이트: `border-ink`, 다크: `border-slate-700`)
- **모션**: 150–300ms, `transform`/`opacity`만, `prefers-reduced-motion` 존중
- **Focus ring**: 2–4px, `--color-ring` (spark orange) — 접근성 & 브랜드 동시

### 10.5 보존 vs 변경 체크리스트

**🟢 보존 (v0.5 자산 재사용)**:
- [x] IBM Plex Sans (UI 본문)
- [x] spark orange `#ff4d12` (accent·focus ring·브랜드)
- [x] paper/ink (라이트 모드 배경·전경)
- [x] radius 0 규약
- [x] `.coord` 좌표형 레이블 유틸
- [x] 키보드 네비(트리)·`.fade-up` 모션 클래스

**🆕 추가**:
- [ ] 다크모드 **실제 색 매핑** + 토글 UI (시스템 연동 + 수동 override)
- [ ] JetBrains Mono (코드·ID·좌표 전용)
- [ ] Developer Tool 색 팔레트 (다크모드 배경·surface·muted)
- [ ] semantic color 토큰 확장 (success/warning/info)
- [ ] 시스템 상태 배지 (인덱싱 pending, external-git sync 등)
- [ ] `docs/design-system.md` 또는 **Storybook** — 토큰·컴포넌트·패턴 공식화

**🔴 축소·제거**:
- [ ] Fraunces (Display serif) — **내부 UI에서 제거**, Publication에만 잔류
- [ ] 장식적 유틸 (`.marquee-track`, `.grain`, `.dotted`) — 랜딩/홈 hero 외 제거
- [ ] 매거진식 우수한 타이포 흐름 — IDE식 정보 밀도로 대체

### 10.6 레퍼런스

- **Linear** (linear.app): 다크모드 네이티브 IDE-friendly 생산성 툴의 표준
- **Raycast** (raycast.com): 명령-중심 UX + 좌표형 레이블
- **Notion for Engineers**, **Vercel Dashboard**: 개발자 타깃 대시보드의 타이포/여백
- **Are.na**, **Readwise**: 안 C 요소 참고용 (Publication 외부 공유 톤)

### 10.7 접근성 & 품질 가드레일 (UI/UX Pro Max §1~§3 기준)

- **대비비**: 라이트/다크 **각각 독립 검증** (본문 4.5:1, 제목 3:1 이상) — `prefer-color-scheme` 감지
- **Focus ring**: 2–4px, 제거 절대 금지 (현재 auth 페이지 스크린샷 이슈 근거)
- **Touch/Click target**: 최소 44×44px
- **Motion**: 150–300ms, `prefers-reduced-motion` 존중, `transform`/`opacity`만
- **Performance**:
  - `react-force-graph-2d` 라우트 레벨 lazy import
  - 검색 rerank latency 2.5s → **스켈레톤 필수**
  - 이미지 WebP + `loading="lazy"` + aspect-ratio 예약
  - `/health` 폴링은 15s 이상, 볼트 뷰에 있을 때만

---

## 부록 A. 권장 화면 리스트 (재설계 출발점)

| # | 화면 | 우선순위 | 핵심 데이터 소스 |
|---|---|---|---|
| A1 | 로그인/회원가입 | 🔴 | POST `/auth/*` |
| A2 | 홈 (전역 검색 + 최근 + 볼트 목록) | 🔴 | `/my/vaults`, `/recent`, `/search` |
| A3 | 볼트 개요 (with external-git 상태) | 🔴 | `/vaults/{v}/info`, `/health` |
| A4 | 볼트 브라우저 (트리 + 필터) | 🔴 | `/browse/{v}` |
| A5 | 문서 뷰어 (+outline, relations, publish) | 🔴 | `/documents/{v}/{id}`, `/relations/...` |
| A6 | **문서 에디터** (신규) | 🔴 | `PATCH /documents`, frontmatter 편집 |
| A7 | 검색 결과 (source_type 분기, 필터) | 🔴 | `/search`, `/grep` |
| A8 | **파일 업로드 UI** (신규) | 🔴 | `/files/{v}/upload` + presigned PUT |
| A9 | 파일 프리뷰 (현재 부분구현) | 🟡 | `/files/{v}/{id}/download` |
| A10 | 테이블 뷰어 + 쿼리 | 🟡 | `/tables/{v}`, `/tables/{v}/sql` |
| A11 | 지식 그래프 (+관계 CRUD) | 🟡 | `/graph/{v}`, `akb_link` 등가 REST 필요 |
| A12 | Publication 관리 페이지 (신규) | 🟡 | `/publications/{v}` |
| A13 | 퍼블릭 렌더 (`/p/:slug`) | 🔴 | `/public/{slug}/*` |
| A14 | **볼트 멤버 관리** (신규) | 🟡 | `/vaults/{v}/members`, `grant/revoke` |
| A15 | Vault 생성 (+external-git) | 🔴 | `POST /vaults` (확장 필요) |
| A16 | 설정 (프로필·PAT·admin) | 🔴 | `/auth/*`, `/admin/users` |
| A17 | 활동 타임라인 + Diff 뷰어 (신규) | 🟢 | `/activity/{v}`, `/diff/...` |
| A18 | 시스템 상태 대시보드 (admin) | 🟢 | `/health`, `/admin/users` |

---

## 부록 B. 확인이 필요한 항목

프론트엔드 재설계 작업 들어가기 전에 결정할 것:

1. **문서 에디터 방식**: split-pane markdown? 블록 에디터? 인라인? → UX 방향성
2. **External-git 볼트 UX**: "연결" / "동기화" / "업스트림" 같은 용어 결정
3. **다국어 지원 필요 여부**: 한국어만? 영/한 토글?
4. **모바일 지원 범위**: 읽기만? 편집까지?
5. **다크모드**: 1일 내내 고정? 시스템 연동? 수동 토글?
6. **실시간성 필요 범위**: 폴링으로 충분? WebSocket 도입?
7. **Agent-first vs 동등 UI vs Agent 통합** ([§1의 A/B/C](#1-설계-철학-agent-first)): v1.0 최대 스코프 결정. 이것이 정해져야 4~6, 8이 의미 있음
8. **에이전트 전용 기능**: todos / memory / session을 UI에도 노출?
9. **관리자 화면 범위**: 현재 user 관리 수준? 감사 로그·시스템 상태 포함?
10. **Publication 관리**: 공유 링크의 수명 주기 UX — 목록, 만료 알림, 재발급
11. **브랜딩 방향**: [§10 권고](#10-v10-디자인-시스템-방향)의 A+C 하이브리드 수용? 아니면 안 B(현행 유지) 선택?

---

*이 문서는 2026-04-22 기준 `ebfad5c` 커밋 상태를 반영합니다. 주요 출처: `backend/app/api/routes/`, `backend/mcp_server/`, `frontend/src/`, `packages/akb-mcp-client/`, `CLAUDE.md`, 그리고 `handover-analysis.md`.*
