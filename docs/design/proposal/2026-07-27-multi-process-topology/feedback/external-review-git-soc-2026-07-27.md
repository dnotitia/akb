---
created: 2026-07-27
source: 외부 리뷰어 피드백 (PM 전달) + 7-에이전트 코드 검증
scope: git/PG 관심사 분리, 이식성, 아키텍처 정리도
head: 12d8bc0
---

# 외부 리뷰 검증 — "git SoC가 안 되어 있고 DB와 혼재, git을 다른 곳에 import도 안 된다"

## 0. 검증 대상 원문

> *"AKB가 bare git을 쓰는 구조라서 git에 대한 SoC가 되어야 할 것 같은데, DB랑 혼재되어 있고,
> git을 다른 곳에 import도 안 되고, AKB 구조적 아키텍처가 정리가 잘 안 되어 있는 것 같다."*

**방법**: 5개 차원 병렬 감사(git 저장 내용 / PG-git 중첩 / 반출 / 반입 / SoC 경계) → 심사 + 아키텍트.
세 감사가 **각각 독립적으로** GitService의 저장 방식을 로컬에 재현하고 클론을 실행해 검증.

## 1. 판정 요약

**세 주장 중 하나만 맞고, 가장 확신 있게 진술된 것이 측정으로 반박됨.
다만 리뷰어가 감지한 냄새는 진짜였고, 진단만 틀렸다.**

| 주장 | 판정 |
|---|---|
| bare git이므로 SoC가 필요하다 | **맞지만 이미 되어 있음** (이 코드베이스에서 가장 잘 격리된 층) |
| git과 DB가 혼재되어 있다 | **틀림** (모듈 결합 기준) |
| git을 다른 곳에 import 못 한다 | **산출물로는 틀림 / 제품으로는 맞음** ← 유일하게 명백히 옳은 지점 |
| 구조적 아키텍처가 정리 안 됨 | **부분적으로 맞으나 층을 잘못 짚음** (git이 아니라 repository 층) |

## 2. "혼재" — 반박

- `git_service.py:23-36`이 import하는 것: logging / threading / time / datetime / pathlib / urllib / gitpython / app.config. **DB 모듈 0건.**
- `from git import` · `Repo(` · `GitError` · `GitCommandError` · `BadName` · `BadObject` 를 `app/`·`mcp_server/`·`scripts/` 전체에서 grep → **`git_service.py` 밖 0건.** 호출자는 str/dict/bytes/None만 받는다.
- 마크다운+frontmatter 직렬화는 `document_service`에 남고(`:179-208`, `:211-213`), GitService는 **이미 직렬화된 문자열**을 받는다.
- git 쓰기 **11곳 전부** `run_git_write` 단일 퍼널 (`document_service.py:491,850,1013,1225,1328,1738,1745,1879,2029`, `collection_service.py:416`, `access_service.py:1628`).
- `table_service` / `file_service` / `publication_service` / `todo_service` / `kg_service` — **git 호출 0건** (`grep -c git kg_service.py` → 0).

유일한 스키마 수준 결합은 `vaults.git_path` (`init.sql:135`, NOT NULL) — `document_service.py:1667`이 **private 메서드** `self.git._bare_path`로 채우는데, 전 리포 `.py/.sql/.ts/.tsx` grep 결과 **INSERT 외에 읽는 곳이 없다**. 결합이면서 동시에 죽은 코드 → 삭제 비용 0.

## 3. "git을 못 가져간다" — 두 뜻으로 갈린다

### 산출물로는 **틀림** (측정)

세 감사가 각각 `git init --bare`(`:178`) → `git worktree add`(`:131-169`) → `write-tree`/`commit-tree -p HEAD`/`update-ref`(`:97-129`)를 재현하고 `git clone` 실행:

- **exit 0, 전체 히스토리, 작성자 보존, AKB 커밋 트레일러 유지, `git fsck` clean**
- 클론에 `.git/worktrees` 메타데이터 안 딸려감, 라이브 worktree와 동시 읽기 가능
- 내용은 평범한 `.md` + YAML frontmatter, 단일 ref(`refs/heads/main`), 커스텀 오브젝트 포맷 없음

클론이 담는 것은 정확히 세 가지 경로 형태뿐:
```
.vault.yaml                    (생성 시 1회, 이후 갱신 없음)
{collection}/_guide.md         (템플릿 vault만, frontmatter 없음)
{collection}/{slug}.md         (frontmatter + 본문)
```

### 제품으로는 **맞음** — 유일하게 명백히 옳은 지적

`git-http-backend|upload-pack|receive-pack|git-daemon|/info/refs|gitweb` → **전 리포 0건.**
`app/api/routes/` 어디에도 repo를 서빙하는 라우트 없음. 일반 vault의 bare repo는 **remote조차 없다**(`Repo.init(bare=True)`, `:178` — origin은 미러에만, `:358`, 그것도 inbound 전용).

유일한 대량 반출 = `GET /api/v1/vaults/{vault}/export` → OKF zip. 이 번들이 **버리는 것**: 커밋 히스토리, 작성자, `content_hash`, 명시적 `akb_link` 엣지, todo, 발행, ACL, rename alias, 그리고 **git에는 있는** `depends_on`/`related_to`(`okf.py:400-416`이 고정 키 목록을 뜨면서 누락). 게다가 `include_archived=False`가 하드코딩(`knowledge_io.py:94`)이고 REST 라우트·MCP 툴 어디에도 오버라이드 파라미터가 없다.

> **bare-repo-per-vault 구조를 정당화하는 속성(내구성 있는 이식 가능한 이력)에 파드 접근 권한 없는 사람은 손댈 수 없다.**

## 4. 리소스 타입별 git 투영 — 6개 중 1개만

| 엔티티 | git | 비고 |
|---|---|---|
| `documents` | **있음** | 본문 + frontmatter |
| `edges` | **절반** | frontmatter 유래(`depends_on`/`related_to`/`implements`)만. **`akb_link` 엣지는 DB 전용** |
| `vault_tables` / `vault_table_rows` | 없음 | 물리 `vt_*` 테이블 |
| `vault_files` | 없음 | S3 (그래서 클론이 작다) |
| `publications` | 없음 | 스냅샷 본문은 S3 |
| `todos` | 없음 | |
| `collections` | 사실상 없음 | 문서 경로의 디렉터리로 암시. **빈 컬렉션은 클론에서 안 보임** |
| `vault_access` / ACL | 없음 | |

## 5. 리뷰어가 느꼈으나 명명하지 못한 진짜 문제 — **얽힘이 아니라 이음매 없음**

**git/PG 불변식을 소유하는 컴포넌트가 없다.**

- `DocumentService`: **git 먼저 → PG 나중** (`:491→516`, `:850→858`, `:1013→1033`, `:1225→1233`, `:1328→1368`), PG 작업은 아직 열려 있는 advisory-lock 트랜잭션 안
- `CollectionService`(`:392-430`), `AccessService`(`:1608-1628`): **PG 먼저 → git 나중** — 정반대 순서
- 복구 코드는 `move` 경로 하나뿐 (`document_service.py:1018-1030`)
- **drift 탐지기 부재**: `health.py:1-26`에 git 로직 0줄. `documents.path`↔트리, `current_commit` 도달성, 행 없는 blob, frontmatter↔PG 메타데이터 — 아무것도 확인 안 함
- `collection_service` 주석이 스스로 *"operator가 조정할 수 있게 크게 로깅"* 이라 적었으나 **조정 도구가 존재하지 않는다**

## 6. 신규 발견 — HIGH 2건 + 데이터 손실 P0 2건

### P0-A. 복구 도구가 손상을 세탁한다

`resource_integrity.py:104` → `document_hash_projection(raw or "", ...)`.
`git_service.read_file`은 blob이 없거나 커밋이 unreachable하면 **예외가 아니라 `None`을 반환**(`:494-513`).
→ `sha256("")` = `e3b0c442…b855` 를 `current_commit`에 대해 찍고 → `documents_repaired += 1` → 재선택 조건(`:156-161`)이 `content_hash IS NULL`이라 **그 행은 다시는 복구 큐에 안 잡힌다.**

> **git/PG 분기를 탐지할 유일한 도구가 증거를 지우고 성공을 보고한다.** operator가 장애 중 이걸 돌리면 내용이 사라진 문서에 대해 "N건 복구됨"을 본다. browse/search는 PG만 읽으므로(`search_service.py:780-860`) 그 문서는 **검색에는 계속 나오고 `akb_get`만 404**.

**수정**: `if raw is None: report.errors.append(...); continue` — blob 부재야말로 이 도구가 찾으라고 있는 것.

### P0-B. OKF 번들 경로 충돌로 문서가 조용히 덮인다

`okf.py:337,341-342`의 `_add()`가 `out[path] = content`로 **평면 dict**에 넣고, `build_bundle`이 documents → tables → files 순으로 처리(`:354-359`). 테이블/파일 경로는 `{collection}/{slugify(name)}`로 **같은 네임스페이스에** 독립 구성(`knowledge_io.py:52-56,70-74`).

> `notes` 컬렉션의 `users` **테이블**이 `notes/users.md` **문서를 조용히 덮어쓴다.** `index.md`에는 둘 다 나열된 채로.

**수정**: `path in out` 확인 → `-table`/`-file` 접미 → `collisions` 목록을 라우트/MCP 핸들러가 노출.

두 건 합쳐 **1 PR, 2 파일, ~15줄**. 회귀 테스트: 충돌하는 테이블+문서로 번들을 만들어 둘 다 생존 단언 / 가짜 `current_commit`으로 repair 호출 시 `documents_repaired`가 아니라 `report.errors`에 떨어짐 단언.

### HIGH-C. 미러 vault가 upstream force-push에 조용히 썩는다

blob이 안 바뀐 파일은 reconcile이 건너뛰므로(`external_git_service.py:199-200`) `current_commit`이 첫 동기화 시점에 고정된다. 그런데 `fetch_remote`는 브랜치 ref를 **강제 이동**시킨다(`git_service.py:395-404`). 그리고 **`git init --bare`는 `core.logAllRefUpdates`를 설정하지 않아 reflog가 없다**(확인함) → 밀려난 커밋이 즉시 unreachable.

> upstream squash-merge/rebase 후 **몇 주 뒤**, 미러 vault 문서가 검색·브라우즈는 되는데 `akb_get`이 404. **reset/resync API 없음.**

### HIGH-D. 미러를 얼려버리는 단일 파일

`_reindex_file`이 부모 디렉터리를 `normalize_collection_path`에 넣는데, 이것이 `{coll, doc, table, file}` 세그먼트를 거부(실행으로 확인: `doc`, `doc/api`, `src/doc` 모두 ValueError, `docs`는 통과). **최상위 `doc/` 디렉터리는 매우 흔한 관례다.**
→ reconcile이 커서를 안 옮기고 raise(`:236-249`) → 폴러가 백오프 8단계 소진 → `retry_count < MAX_RETRIES`에서 **영구 제외**. 첫 동기화면 `last_synced_sha`가 NULL이라 8회 전부 **전체 재클론**. `VaultExternalGitRepository`에 `retry_count`를 0으로 되돌리는 메서드가 없다.

### MEDIUM-E. PG 재구축 도구가 세 군데서 죽어 있다

`backend/scripts/reindex_all.py:75,103,126`이 `write_source_chunks`에 `embeddings=`를 넘기는데 현재 시그니처(`index_service.py:541-548`)에 그 파라미터도 `**kwargs`도 없다 → **첫 행에서 TypeError**. 별개로 `:166`이 `f.collection`을 SELECT하는데 그 컬럼은 migration 020이 **드롭**(`020_unify_collection_membership.py:25`) → 그 전에 UndefinedColumnError. 이 파일을 import하는 유일한 테스트가 `_e2e` 세트라 `pytest -k 'not _e2e'`가 제외 → 부식이 살아남음.

> **"청크는 파생이니 git에서 언제든 재구축한다"의 절반에 현재 동작하는 구현이 없다.** 그리고 반대 방향은 어떤 대가로도 불가 — 본문 원본을 담는 컬럼이 없고 `chunk_markdown`은 구조적으로 손실(`index_service.py:225-268`).

## 7. "정리 안 됨"이 실제로 맞는 층 — repository

직접 센 수치(테스트·`__pycache__` 제외):

```
app/services/      직접 asyncpg 호출 414건 / 31 파일
app/repositories/  직접 asyncpg 호출  79건 /  8 파일
app/api/routes/    12건
mcp_server/         4건
```

`access_service`(76) · `account_service`(42) · `role_sync`(39) · `kg_service`(38) · `publication_service` · `auth_service` — **repository 없이 SQL 직접 작성.** import-linter / deptry / 아키텍처 테스트 **없음**.

> 리뷰어는 이 코드베이스에서 **가장 깔끔하게 분리된** 저장 관심사를 보고, 낯섦(bare repo + linked worktree는 흔치 않은 구조)을 무질서로 오인했다. 진짜 무질서는 repository 층에 있다.

**이 항목은 이 설계 항목의 범위 밖이며 별도 로드맵 항목이어야 한다.** 위 어떤 옵션에도 밀어넣지 말 것.

## 8. 기록되지 않은 결정 — 리뷰어가 완전히 옳은 지점

git이 벡터스토어처럼 swappable 포트가 **아닌** 이유가 어디에도 안 적혀 있다.

벡터스토어: 299줄 Protocol(`vector_store/base.py`) + 설정 기반 팩토리 + accepted 설계 문서.
git: 없음. `docs/design/{accepted,proposal}`, `docs/prd`, `AGENTS.md`, `CLAUDE.md` 전부 검색 — 근거 없음.

그런데 **git은 포트가 될 수 없는 게 맞다**:
- 커밋 해시가 MCP/REST 공개 계약의 OCC 토큰 (`tools.py:198-200` `expected_commit`, `:236-238` `base_commit`, `HEX_COMMIT_RE`가 `api/routes/documents.py:111`·`mcp_server/server.py:431,838` 양쪽에서 검증, 프론트엔드 17곳 참조)
- `git log`가 곧 `akb_activity`/`akb_history` 기능 (`git_service.py:847-926`)
- unified diff가 곧 `akb_diff` (`:966-978`)
- write_lane admission 설계 전체가 "git 커밋이 vault당 직렬화된다"에서 나옴
- `ExternalGitService`는 정의상 git-to-git

`AGENTS.md:188-198`이 **메커니즘은** 설명하지만 **이식성이 목표인지 비목표인지 말하지 않는다.** 그 침묵이 외부 리뷰어가 결정을 사고로 읽게 만들었다.

## 9. 이 설계 항목(멀티 프로세스)과의 관계

### 선행조건 — 재분류 필요

이 항목 README의 P0-(1) 실패 시나리오는 *"두 프로세스가 같은 vault 다른 경로를 쓰다가 한쪽의 `reset --hard HEAD`가 다른 쪽 스테이징을 지우고, `write_tree`가 그 파일 없는 트리를 만들고, PG에는 `current_commit`이 기록 — 문서는 PG와 검색에는 있고 git 읽기에서는 404, 에러는 어디에도 안 난다"* 이다.

**그 상태가 §5의 상태와 바이트 단위로 동일하고, 현재 그것을 탐지하는 것이 아무것도 없다.** `health.py`에 git 프로브가 없고, 유일한 복구 도구는 그것을 `sha256("")`로 세탁하고 복구됐다고 보고한다(§6 P0-A).

> **→ 마이그레이션이 탐지 불가능한 폭발 반경을 가진 채 출시된다.**
> `repair_resource_hashes`의 None-blob 경로 수정 + git/PG drift 프로브 추가는 값싸고, 오늘도 유용하며,
> **토폴로지 변경보다 먼저 또는 함께 랜딩해야 한다** — 별도 정리 작업이 아니다.

README §4.2의 non-CAS `update-ref` 항목도 같은 이야기의 반대편이다.

### 동일 프로젝트 — 별도 노력 불필요

external-git 폴러가 clone(900s) / fetch(300s) / per-file `cat_blob`을 **공유 `asyncio.to_thread` executor**에서 돌리는 것(`external_git_service.py:161,174,187,266,299`)은 이 항목의 원인 ③(워커 11종이 API 프로세스 안)과 정확히 같다. **워커 티어 분리가 공짜로 해결한다.**
`git_service:43-53`의 프로세스-로컬 `threading.Lock`(write_lane docstring이 "last-line correctness guard"라 부르는 것)도 이미 이 항목의 P0-(1)로 스코프됨.

### 직교 — 이 항목과 무관

git egress 부재, 히스토리 보존 export, OKF 경로 충돌, `include_archived`, 미러 설정 불변성/PAT 로테이션, 미기록 ADR, `vaults.git_path`, `.vault.yaml` staleness, 커밋 트레일러 중복, repository 층. **멀티 프로세스에서 동작이 달라지지 않고 차단하지도 않는다.**

## 10. 권고 순서

| # | 항목 | 크기 | 근거 |
|---|---|---|---|
| 1 | **P0-A + P0-B 데이터 손실 2건** | XS (1 PR, 2 파일, ~15줄) | 아래 어떤 것도 이 위에 쌓으면 안 됨 |
| 2 | **git 정책 ADR** — "git은 제품 기능, swappable 포트 아님 + 이유" | XS (반나절) | **리뷰 피드백에 대한 진짜 답.** 코드는 방어 가능한데 기록이 없는 게 문제 |
| 3 | **`reindex_all.py` 소생** (3 kwargs + 드롭된 컬럼) + 비-e2e 시그니처 계약 테스트 | S | DR 스토리에 동작하는 구현이 없음. 계약 테스트는 워커 분리 작업도 보호 |
| 4 | **git/PG drift 프로브** — `current_commit` unreachable 행 수를 vault 헬스에 노출 | S | **§9에 따라 토폴로지 작업의 게이트.** 워커 티어의 자연스러운 첫 입주자 |
| 5 | `git bundle` 반출 (CLI 서브커맨드 먼저, `app/cli.py:119-166` 옆) | S | 리뷰어 지적 중 맞은 부분의 해결. **ACL은 admin 전용 — git 히스토리에는 문서별 ACL이 없고 삭제된 문서가 전부 남아 있음(PM 결정)** |
| 6 | 미러 reset/rotate/detach, export 충실도(`include_archived` 등) | M | |

**하지 말 것**: git에 `vector_store/base.py`식 전면 포트(XL, 얻는 것 0 — 위 §8의 이유로 swap 자체가 불가능). 테이블/파일/발행의 per-write git 투영(오늘 git 호출 0건인 서비스들을 커밋 executor에 올리면 503 마이그레이션이 덜어내려는 바로 그 티어에 부하 추가).

## 11. 신뢰도

- 모듈 결합 반박은 grep 재실행으로 **직접 재검증**
- 클론 이식성은 **세 감사가 각각 다른 스크립트로 독립 재현**
- 크래시 윈도우 열거는 **제어 흐름에서 도출**한 것이지 실제 프로세스를 죽여본 것이 아님
- 다섯 감사 중 어느 것도 **프로덕션 vault repo를 검사하거나 백엔드를 실행하지 못함** — "실제 repo에 이 세 경로 형태만 있다"는 관찰이 아니라 **호출 지점 전수 커버리지**에 근거
