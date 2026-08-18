---
status: proposal
stage: proposal
created: 2026-07-27
updated: 2026-07-27
head: 12d8bc0
method: 13-agent parallel code audit (8 dimensions -> 3 architectures -> judge + adversarial critic) + independent Codex review
---

# 단일 프로세스 구조 탈출 — 멀티 프로세스/멀티 레플리카 토폴로지

## 0. 한 장 요약

> **`replicas: 1`은 보수적인 용량 설정이 아니라 정확성 경계(correctness
> boundary)다.** 오늘 `--workers N`을 켜거나 `replicas`를 올리면 성능이
> 나빠지는 게 아니라 **데이터가 조용히 사라진다.**
>
> 근본 원인은 하나가 아니라 세 겹이다:
> ① git worktree/ref 직렬화가 **프로세스-로컬 락**이고 ref 이동이 **비-CAS**,
> ② MCP 세션이 **프로세스 메모리**에 있고 ingress에 affinity가 없으며,
> ③ 모든 백그라운드 워커가 **서빙 이벤트 루프와 같은 프로세스**에서 돈다.
>
> ③이 실제 프로덕션 장애(503 flapping)의 원인이었고, ①②는 그걸 고치려고
> 프로세스를 늘리는 순간 터진다.

| | 오늘 | 목표 |
|---|---|---|
| 프로세스 | uvicorn 1 + Kiwi 자식 4 | API 티어 N 파드 + git/worker 티어 1 |
| 배포 | `Recreate` = 매 배포가 다운타임 | API는 `RollingUpdate` |
| 노드 장애 | 전체 서비스 손실 | API는 생존, git 쓰기만 중단 |
| 워커 | 서빙 루프와 동거 | 별도 티어 |
| 볼륨 | RWO PVC를 전체 백엔드가 소유 | RWO PVC를 git 티어만 소유 |

---

## 1. 문제 — 오늘 무엇이 프로세스를 1개로 못박는가

### 1.1 확인된 사실 (커밋 `12d8bc0`)

| 사실 | 근거 |
|---|---|
| uvicorn `--workers` 없음 → 프로세스 1, 이벤트 루프 1 | `backend/Dockerfile:17` |
| `replicas: 1` + `strategy: Recreate` + RWO PVC | `deploy/k8s/backend.yaml:78`, `:85-86`, `:172-173` |
| 모든 워커(11개)가 API 프로세스 안에서 기동 | `app/services/lifecycle.py:115-188`, `app/main.py:41` |
| **`AKB_DISABLE_WORKERS`와 `app.worker_main`은 존재하지 않는다** | `lifecycle.py:3` docstring이 유일한 언급. 리포 전체 grep 1건, `ls app/`에 파일 없음 |

마지막 항목이 중요하다. 티어 분리를 "설정으로 켤 수 있다"고 오해하기 쉬운
docstring이 3년째 코드베이스에 남아 있지만, **그 스위치는 구현된 적이 없다.**
어떤 안을 고르든 코드가 필요하다.

### 1.2 P0 — 중복되면 정확성이 깨지는 것

#### (1) git worktree 공유 + 프로세스-로컬 락

vault마다 bare repo 1개(`/data/vaults/{vault}.git`)와 **공유 persistent
worktree 1개**(`/data/vaults/_worktrees/{vault}`)가 있다. 쓰기 직렬화는
모듈 전역 `threading.Lock` — 인터프리터 1개 안에서만 유효하다
(`git_service.py:43-53`).

락 안의 첫 문장이 `reset --hard HEAD`다 (`git_service.py:589`).

DB 가드는 이걸 못 막는다. `_path_lock`은 `pg_advisory_xact_lock(vault_id,
path)` — **경로 단위**다 (`document_repo.py:33`, `document_service.py:338-343`).
즉 **같은 vault의 다른 경로 쓰기는 설계상 동시 실행**된다. 오늘 안전한 이유는
오직 그 둘이 같은 프로세스에 떨어져 `_vault_lock`에서 만나기 때문이다.

> **실패 시나리오**: 프로세스 P1이 `notes/a.md`를, P2가 `specs/b.md`를 같은
> vault에 동시 PUT. P1이 `reset --hard` → 파일 쓰기 → `git add`까지 진행한
> 순간, 다른 경로 락을 쥔 P2가 자기 `_vault_lock`을 통과해 `reset --hard
> HEAD`를 실행하고 **P1이 스테이징한 `a.md`를 지운다.** P1의 `write_tree`는
> `a.md`가 빠진 트리를 만들지만 PG에는 `documents.current_commit`이 기록된다.
> 문서는 PG와 검색에는 있고 git 읽기에서는 404. **에러는 어디에도 안 난다.**

#### (2) `update-ref`에 CAS 없음

```python
# git_service.py:113-129
tree_sha = work_repo.git.write_tree(...)
if parent_required:
    work_repo.git.rev_parse("--verify", "HEAD", ...)   # ← 반환값을 버린다
    parent_args = ["-p", "HEAD"]
commit_sha = work_repo.git.commit_tree(... *parent_args ...)
work_repo.git.update_ref("HEAD", commit_sha, ...)      # ← oldvalue 없음
```

전형적인 read-modify-write에 낙관적 동시성 체크가 없다. 두 프로세스가 같은
부모 X에서 각각 Y, Z를 만들면 `update-ref`가 **non-fast-forward를 무조건
수락**하고, 먼저 쓴 커밋은 브랜치에서 unreachable이 된다. PG의
`current_commit`은 그 고아 커밋을 가리킨다.

> **정정 (적대적 검증에서 발견)**: 세 개 설계안이 공통으로 제시한 수정
> 레시피 — "`:116`이 이미 읽은 sha를 캡처해 oldvalue로 넘겨라" — 는 **코드
> 그대로에는 적용되지 않는다.** `:116`은 반환값을 변수에 담지 않으므로 새
> 할당이 필요하고, 그 줄은 `if parent_required:` 안에 있다. `parent_required
> =False` 분기(빈 브랜치 첫 커밋, `_commit_via_clone`의 형제 경로
> `git_service.py:741`)에서는 `update-ref HEAD <new> ""` (must-not-exist 형태)가
> 맞는 CAS이며, **어떤 안도 이걸 다루지 않았다.** 그대로 구현하면 no-parent
> 분기에서 NameError가 나거나 비-CAS가 조용히 남는다.

#### (3) MCP 세션이 프로세스-로컬 dict + POST가 미지 세션을 새로 만든다

```python
# mcp_server/http_app.py:52-55
_transports: dict[str, StreamableHTTPServerTransport] = {}
_server_tasks: dict[str, asyncio.Task] = {}
_session_users: dict[str, object] = {}
```

POST 경로(`:139-151`)는 모르는 `mcp-session-id`를 만나면 404가 아니라
**새 uuid로 initialize된 적 없는 transport를 새로 만들어** 요청을 넣는다.
GET 경로(`:159-163`)는 반대로 404를 낸다. **두 경로가 서로 다르게 동작한다.**

`grep -rn 'affinity|sessionAffinity|upstream-hash' deploy/` → **0건.**

Node 프록시는 세션 에러/404/연결 실패에 재-initialize 후 **원래 호출을
재생(replay)** 한다(`packages/akb-mcp-client/lib/proxy.mjs:679-717`). 즉
가용성 결함이 **중복 변경(mutation)** 으로 번역될 수 있고, 백엔드에 MCP 요청
멱등키/중복 제거 원장은 없다.

#### (4) 동일 이름 vault 생성/삭제 경쟁

`_CREATE_LOCKS`는 `asyncio.Lock` 맵(`document_service.py:146-161`). 자기
docstring이 "직렬화가 없으면 패자 B가 승자 A의 디렉터리를 보고 보상
로직에서 A의 repo를 지운다"고 명시한다. 프로세스가 둘이면 그 전제가 깨진다.

### 1.3 P1

| 항목 | 근거 | 결과 |
|---|---|---|
| publication 비밀번호 throttle이 프로세스-로컬 dict — docstring이 "백엔드는 단일 레플리카라 모듈 전역 dict가 authoritative"라고 명시 | `publication_rate_limit.py:1-8`, `:26-41`, `public.py:433-485` | 허용량 N배 + 락아웃 우회. **캐시 stale이 아니라 보안 통제 실패** |
| audit 해시체인이 프로세스별 `_seq`/`_prev`로 같은 날짜 파일에 append | `audit_log.py:80-98`, `:105-135` | 체인 검증 실패 = SIEM이 "변조됨"으로 보고. S3 업로드 키도 충돌 |
| startup 마이그레이션에 전역 직렬화 없음 | `db/postgres.py:123-174` | `045`는 `pg_constraint` 확인 후 ADD — 두 프로세스가 동시 통과 가능 |
| write_lane의 per-vault 게이트/글로벌 세마포어가 프로세스-로컬 | `write_lane.py:64-109` | vault당 동시 쓰기 N, 글로벌 캡 N×8 — admission control이 시스템을 못 지킴 |

### 1.4 이미 크로스-프로세스 안전한 것 (좋은 소식)

| 메커니즘 | 근거 |
|---|---|
| 같은 경로 문서 쓰기 | `pg_advisory_xact_lock` — `document_repo.py:15-36` |
| embed / delete / s3_delete / metadata / events 워커 클레임 | `FOR UPDATE SKIP LOCKED` + 10분 리스 |
| BM25 재계산 | `pg_try_advisory_lock` — `sparse_encoder.py:564-600` |
| pgvector 스키마 셋업 | advisory transaction lock — `pgvector.py:211-286` |
| OIDC state / 교환 코드 | PG + 원자적 `DELETE … RETURNING` — `keycloak_oidc.py:55-94` |

**즉 데이터 평면의 큐 기질은 이미 멀티 프로세스를 견딘다. 못 견디는 것은
git 파일시스템과 세션/카운터류 제어 평면이다.**

### 1.5 이벤트 루프에 아직 남은 블로킹 (Kiwi 수정 이후)

| 심각도 | 항목 | 근거 |
|---|---|---|
| Critical | 사용자 제공 정규식을 타임아웃 없이 루프에서 `re.search`/`re.sub` | `search_service.py:780-834`, `:920-957` |
| High | boto3 동기 호출이 루프 위 — `s3_delete_worker`는 틱당 16회 블로킹 delete | `s3_delete_worker.py:115`, `access_service.py:1541`, `:1565`, `file_service.py:254`, `:336` |
| High | 문서 본문 길이 상한 없음 + ingress `proxy-body-size: 500m` | `models/document.py:40-77`, `ingress.yaml:10-12` |
| High | MCP가 전체 SQL 결과를 stdlib `json.dumps` 한 방에 직렬화 | `mcp_server/server.py:1397-1409` |
| Medium | `vault_exists()`가 `to_thread` 없이 PVC에 `stat()` | `document_service.py:1734`, `:1874` |

`file_service.py:336`의 블로킹 `s3_adapter.head()` **11줄 아래**(`:347`)에서
훨씬 싼 해시 계산은 이미 `to_thread`로 감싸고 있다 — 설계가 아니라 누락이다.

---

## 2. 검토한 세 가지 아키텍처

세 명의 아키텍트가 독립적으로 설계하고, 독립 심사역이 5개 축으로 채점했다.
전체 비교는 `rounds/round-01-option-comparison.md`.

| 안 | 요지 | 제약제거 | 라이브위험 | 노력대효과 | 적합성 | 되돌리기 | 합계 |
|---|---|---|---|---|---|---|---|
| **A** 파드 내 스케일업 | supervisor + N API 프로세스 + flock | 5 | 5 | 4 | 4 | 7 | **25** |
| **B** 티어 분리 | stateless API N + `gitd` 1 (볼륨 소유) | 6 | 7 | 7 | 9 | 8 | **37** |
| **C** RWX 대칭 | CephFS RWX + PG advisory lock, 대칭 파드 N | 9 | 2 | 3 | 6 | 3 | **23** |

### 왜 B인가

- **스토리지 기질을 건드리지 않는다.** RWO 유지, RWX 없음, CephFS 없음.
  이 클러스터에는 CephFS/MDS 장애 전력이 있고, 그건 팀이 통제할 수 없는
  부분이다.
- **이음매가 이미 있다.** `run_git_write`(`write_lane.py:188`)가 git 변경의
  단일 async 퍼널이고, `GitService()` 생성 지점은 13곳뿐이며 그중 둘
  (`document_service.py:266`, `external_git_service.py:76`)은 이미 주입을
  받는다.
- **`gitd`가 인터프리터 1개이므로 가장 위험한 파일을 다시 쓸 필요가 없다.**
  `git_service.py:43-53`의 `threading.Lock`이 다시 **올바른** vault 뮤텍스가
  되고, `reset --hard`의 폭발 반경이 다시 단일 writer로 좁혀지며,
  `cleanup_stale_locks`의 안전성 docstring이 다시 참이 된다.
- **싱글턴이 필요한 워커 3종**(audit 체인, role_sync reconcile, audit
  uploader)이 `gitd`의 `replicas: 1`에서 **코드 없이** 싱글턴이 된다.
- **가장 큰 이득이 replica를 올리기 전에 도착한다.** 모든 워커가 서빙 루프
  밖으로 나가는 Phase 3이 API 티어가 아직 `replicas: 1`일 때 끝난다.

### B의 정직한 한계 (PM이 받아들여야 할 것)

- **쓰기 확장성 0, 쓰기 HA 0.** 4단계를 다 해도 모든 쓰기는 여전히 한
  인터프리터의 `threading.Lock` 하나, RWO PVC 하나를 통과한다. `gitd` 롤아웃은
  여전히 쓰기 중단 구간이다(오늘 `Recreate`도 그러하니 본전).
- **새로운 결합이 생긴다.** Phase 3 이후 `_path_lock`이 연 트랜잭션 안에서
  `run_git_write`가 **네트워크 왕복**이 된다. 느린 `gitd`가 모든 API 레플리카의
  풀 점유와 `idle_in_transaction_session_timeout` 위험으로 직결된다.
- **`gitd`는 ACL을 검사하지 않는다.** 인증은 API 티어에 남는다. ClusterIP +
  베어러 토큰 + NetworkPolicy가 하드닝이 아니라 **정확성 요건**이다.

### C를 기각하되 기록으로 남기는 이유

C는 유일하게 제약을 진짜로 제거하는 안이고 advisory-lock 관용구에도 가장 잘
맞는다. 그러나 **RWX = 이 클러스터에서는 CephFS**이고, MDS 스톨 시
`stat()`/`open()`이 D-state로 블록된다. `git_write_timeout_secs`가 보내는
시그널을 D-state 프로세스는 받을 수 없으므로 **N개 이벤트 루프가 함께 멈추고,
N개 프로브가 함께 실패하고, kubelet이 함대 전체를 SIGKILL하고, 재시작하는
모든 파드가 `cleanup_stale_locks`에서 그 멈춘 마운트를 다시 걷는다.** 독립
장애를 N중 상관 장애로 바꾸고, 사고 기록이 경고한 "stateful 파드를 재시작하지
말 것"을 자동화한다. 이 분석은 **RWO를 유지하는 이유의 근거 문서로 보존**한다.

---

## 3. 롤아웃 (안 B)

| Phase | 내용 | 토폴로지 변화 | 검증 |
|---|---|---|---|
| **0** | 토폴로지 무관 결함 수정 (§4 참조) | 없음 | 아래 §4 |
| **1** | `get_git()` 팩토리 + `GitClient` + `app/gitd_main.py` 신설, 13개 `GitService()` 생성 지점 교체, `main.py:41`에 `run_workers` 게이트, 토크나이저/커밋 풀을 `start_workers`에서 `init_storage`로 이동 | **없음** — `gitd_url` 비면 런타임 바이트 동일 | `scripts/check.sh`, `pytest -k 'not _e2e'`, 전체 e2e |
| **2** | API 티어 멀티프로세스 위험 제거: `_CREATE_LOCKS`→PG advisory, publication throttle→PG, MCP POST 404 + ingress affinity, `_apply_migrations` advisory lock | 여전히 `replicas: 1` | 2-프로세스 레이스 테스트(§5) |
| **3** | `gitd`를 두 번째 Deployment로 배포. API가 PVC 마운트를 버리고 `RollingUpdate`로 전환 | API 1 + gitd 1 | 무중단 배포, gitd 재시작 시 git 의존 경로만 실패 |
| **4** | API 티어 `replicas` 상향 + 풀 예산 재배분 | API N + gitd 1 | 파드 kill 시 MCP/REST 생존 |

**Phase 1은 다크 배포다** — `gitd_url`이 비어 있으면 `get_git()`이 로컬
`GitService()`를 반환하므로 런타임 경로가 오늘과 완전히 동일하다. 세 안 중
가장 안전한 첫 발이다.

단, **fail-closed 부트 단언이 필수**다: API 티어인데 `gitd_url`이 비면
**부팅을 거부**해야 한다. 볼륨이 없는 파드에서 로컬 `GitService()`로 조용히
폴백하면 빈 `/data/vaults`를 만들고 모든 문서 본문에 404를 서빙한다 — 가능한
최악의 실패 방향이다.

---

## 4. Phase 0 — 어떤 안을 고르든 지금 해야 하는 것

토폴로지 결정과 **무관하게** 오늘 `replicas: 1`에서도 결함인 항목들.

### 4.1 첫 PR (권고): 블로킹 S3/파일시스템 I/O를 이벤트 루프 밖으로

7개 호출 지점, 기계적 변환, 아키텍처 커밋 없음. A/B/C 어느 안에서도, 심지어
"아무것도 안 함"에서도 옳다.

| # | 위치 | 내용 |
|---|---|---|
| 1 | `s3_delete_worker.py:115` | 클레임된 행마다 동기 `s3_adapter.delete()`, `BATCH_SIZE=16` |
| 2 | `access_service.py:1541` | `delete_vault`가 모든 `vault_files` 행에 대해 무제한 루프, 풀 커넥션 점유 중 |
| 3 | `access_service.py:1565` | publication 스냅샷 키에 동일 루프 |
| 4 | `file_service.py:336` | 루프 위 `s3_adapter.head()` — 11줄 아래는 이미 `to_thread` |
| 5 | `file_service.py:254` | `ensure_bucket()`; `_bucket_verified`는 성공 시에만 set → S3 다운 중 매 업로드가 블로킹 `head_bucket` 재발행 |
| 6-7 | `document_service.py:1734`, `:1874` | `vault_exists()` = PVC에 대한 `Path.exists()` |

**왜 이게 1번인가**: 공격자도, 두 번째 프로세스도, 코드 변경도 필요 없다.
느린 의존성 하나면 된다. `s3_delete_worker` 틱 하나가 최대 ~144초 루프
동결이고 즉시 다음 틱을 돈다. `/readyz`가 3×8s×15s 간격으로 실패해 **~45초 뒤
유일한 레플리카가 Service에서 빠지고**(100% 아웃티지), ~3분 뒤 liveness가
SIGKILL한다 — 요청 트래픽과 무관한 백그라운드 워커가 자초하는 재시작 루프다.
그리고 CephFS 스톨 중 SIGKILL은 사고 기록이 "하지 말라"고 한 바로 그 확대다.

이건 B의 크리티컬 패스이기도 하다 — B 자체 분석이 "이 블로킹 호출을 고치지
않으면 Phase 3은 503을 제거하는 게 아니라 `gitd`로 이전할 뿐"이라고 인정한다.

### 4.2 나머지 Phase 0 항목

| 항목 | 근거 | 왜 지금 |
|---|---|---|
| `update-ref` CAS (**no-parent 분기 포함**) | `git_service.py:113-129`, `:741` | 단일 writer 불변식이 실제로 성립하는지 확인하는 단언. 조용한 고아 커밋을 재시도 가능한 409로 바꾼다 |
| RoleSync 스냅샷 수정 | `role_sync.py:642,690,694,700,743,969` | **오늘 `replicas:1`에서도 라이브 버그.** `wanted`/`existing`을 별개 autocommit 스냅샷으로 읽어, 그 사이 생성된 role을 orphan으로 분류해 DROP → 해당 사용자의 `akb_sql`이 최대 1시간 죽는다 |
| `index_table_metadata`/`index_file_metadata`에 트랜잭션 + advisory lock | `table_service.py:538`, `file_service.py:646` | **오늘도 BREAKS_CORRECTNESS.** 트랜잭션 없는 맨 커넥션이라 `_drop_source_chunks_with_outbox`의 원자성 요건(`index_service.py:476-492`)이 깨지고, 반쯤 커밋된 outbox 때문에 delete_worker가 **살아있는 벡터 포인트를 삭제** |
| 마이그레이션 per-migration 트랜잭션 + `QueryCanceledError`를 락 충돌로 취급하지 말 것 | `db/postgres.py:140-157`, `:60` | 30s `statement_timeout`이 같은 예외를 던져서 느린 데이터 마이그레이션이 반쯤 적용된 뒤 10번 재실행된다 |
| `terminationGracePeriodSeconds` | `deploy/` 전체에 부재 (확인함) | k8s 기본 30s vs `stop_workers()`의 워커당 최대 120s 예산 → **매 롤아웃이 셧다운 도중 SIGKILL**. `cleanup_stale_locks`가 존재해야 하는 이유가 이것 |
| `Kiwi(num_workers=1)` 명시 | `sparse_encoder.py:218` | 인자 없으면 `os.cpu_count()` 네이티브 스레드를 쓰는데 cgroup 쿼터를 존중하지 않고 CPU limit도 없다 |
| `vault_backfill` 배치 CTE에 `FOR UPDATE SKIP LOCKED` | `vault_backfill.py:127` | 유일하게 빠진 큐 워커 |

### 4.3 게이트 — drift 탐지기 (외부 리뷰 검증에서 승격, 2026-07-27)

> 상세: `feedback/external-review-git-soc-2026-07-27.md` §9

§1.2(1)의 P0 실패 시나리오 — *"P2의 `reset --hard`가 P1의 스테이징을 지우고, PG에는
`current_commit`이 기록되어 문서가 PG·검색에는 있고 git 읽기에서는 404, 에러는 어디에도
안 남"* — 를 **탐지하는 것이 코드베이스에 하나도 없다.**

- `health.py:1-26`에 git 로직 **0줄**. `documents.path`↔트리, `current_commit` 도달성,
  행 없는 blob, frontmatter↔PG 메타데이터 — 아무것도 확인 안 함
- 그리고 유일한 복구 도구가 **그 상태를 세탁한다**: `resource_integrity.py:104`가
  `document_hash_projection(raw or "", ...)` — `read_file`은 blob 부재 시 예외가 아니라
  `None`을 반환하므로(`git_service.py:494-513`) `sha256("")`을 찍고
  `documents_repaired += 1`로 세고, 재선택 조건(`:156-161`)이 `content_hash IS NULL`이라
  **그 행은 다시는 복구 큐에 안 잡힌다**

> **→ 이 마이그레이션은 지금 상태로는 탐지 불가능한 폭발 반경을 가진 채 출시된다.**

**따라서 다음 두 가지는 정리 작업이 아니라 토폴로지 변경의 게이트다** — Phase 3 이전 또는 동시:

| 항목 | 크기 | 내용 |
|---|---|---|
| `repair_resource_hashes` None-blob 경로 | **XS** | `if raw is None: report.errors.append(...); continue`. blob 부재야말로 이 도구가 찾으라고 있는 것 |
| git/PG drift 프로브 | **S** | `current_commit`이 unreachable한 `documents` 행 수를 vault 헬스에 노출. **워커 티어의 자연스러운 첫 입주자로 만들 것** (요청 경로 스캔이 아니라) |

§4.2의 `update-ref` CAS 항목은 같은 이야기의 반대편이다 — CAS는 조용한 유실을 **시끄러운
409**로 바꾸고, drift 프로브는 **이미 벌어진 유실**을 보이게 한다. 둘 다 필요하다.

---

## 5. 검증 공백 — 이게 가장 위험하다

**오늘 두 번째 writer가 나타나도 빨개지는 테스트가 하나도 없다.**

`backend/tests/concurrency/`에 `repro_e01_multivault.py`,
`repro_e02_samepath.py`, `repro_e03_update_race.py`,
`repro_e05_e06_delete_race.py`, `test_invariants_unit.py` 등 하네스 선례는
있다. 그러나 모든 e2e가 백엔드 **하나**를 겨냥하고(`BASE_URL="${AKB_URL:-
http://localhost:8000}"`), 두 번째 프로세스를 띄우는 스크립트는 없다.

세 안 모두 "2-프로세스 레이스 테스트를 추가한다"고 했지만 **어느 안도 CI
배선을 제안하지 않았다.** 실행되지 않는 테스트는 없는 테스트와 같고, 두 안은
그걸 Phase 1의 유일한 게이트로 삼았다.

**요건**: 2-프로세스 픽스처(하나의 PG + 하나의 `git_storage_path`)와
`scripts/run_regression.sh`(또는 `check.sh`) 호출을 **테스트와 같은 변경에**
넣고, 수정 전 커밋에서 RED임을 시연할 것.

검증 항목: 같은 vault 다른 경로 동시 쓰기 / 같은 이름 vault 동시 생성 /
생성·삭제·쓰기 레이스 / `git fsck --full` / 모든 `documents.current_commit`이
브랜치에서 reachable / `current_commit`의 내용이 `content_hash`와 일치 /
커밋 생성 후 PG 커밋 전 kill 주입.

---

## 6. PM 결정 필요

1. **목표가 가용성인가, 503 제거인가.** B는 API HA와 무중단 배포를 사주지만
   **쓰기 확장성과 쓰기 HA는 0**이다. 노드 장애에서 **쓰기**가 살아남는 게
   진짜 요건이라면 세 안 중 어느 것도 CephFS 베팅 없이는 못 준다. Phase 4에서
   발견할 게 아니라 지금 명시적으로 정해야 한다.

2. ~~**`akb-vaultdata` PV의 실제 백엔드가 무엇인가.**~~ → **§6.5에서 해소됨.
   이미 CephFS다.**

3. ~~**원격 MCP가 실제 프로덕션에서 쓰이는가.**~~ → **§6.5에서 해소됨. 쓰인다.**

4. **새 크로스-티어 결합을 수용하는가.** Phase 3 이후 `_path_lock`이 연
   트랜잭션 + advisory lock을 쥔 채 `run_git_write`가 네트워크 왕복이 된다.
   느린 `gitd`가 모든 API 레플리카의 풀 점유로 직결된다. 놀람이 아니라
   **합의된 트레이드오프**여야 한다.

5. **`gitd`의 무권한 설계를 수용하는가.** 모든 vault에 대한 무제한 쓰기
   권한을 갖고 ACL 검사를 전혀 하지 않는다. **이 클러스터에서 NetworkPolicy를
   실제로 강제할 수 있는지** Phase 3 전에 확인 필요. 또한
   `test_pg_rbac_e2e.sh`의 44개 크로스-vault 격리 테스트가 이 새 표면을
   덮지 않는다.

6. **마이그레이션을 요청 경로에서 빼서 k8s Job/initContainer로 옮길 것인가.**
   B는 제자리에 advisory lock만 건다. 옮기면 느린 데이터 마이그레이션을
   취소하는 30s `statement_timeout` 제약도 함께 사라진다.

7. **audit을 켤 것인가.** `audit.enabled` 기본값이 False라 해시체인/버킷키
   충돌은 오늘 무해하다. 컴플라이언스로 켜야 한다면 파드 판별자 수정 **과**
   `/data/audit` volumeMount(`config.py:41-42`가 지시하지만
   `backend.yaml:101-111`에 없음)가 먼저 들어가야 한다. 안 그러면 첫 활성화가
   SIEM이 "변조됨"으로 보고할 감사 기록을 만든다.

8. **배포 안전 선행조건.** `deploy-internal.sh`에는 kubectl 컨텍스트 가드가
   없고 과거에 전체 스택을 엉뚱한 클러스터로 오발사한 적이 있다. B는 **두
   번째 Deployment와 PVC 소유권 이전**을 도입한다. 2티어 배포 **전에** 가드를
   넣어야 한다.

---

## 6.5 클러스터 실측 (2026-07-27, `kubernetes-admin@kubernetes`, 읽기 전용)

리포지토리로는 답할 수 없던 것을 클러스터에서 확인했다. **세 가지 사실이
분석을 바꾼다.**

### (1) `akb-vaultdata`는 **이미 CephFS**다

```
NAME            SC              MODE            SIZE
akb-vaultdata   csi-cephfs-sc   ReadWriteOnce   20Gi
provisioner: cephfs.csi.ceph.com   fsName: cephfs
```

`csi-cephfs-sc`는 **클러스터 기본 StorageClass**이고, `csi-rbd-sc`도 따로
있다. AKB의 모든 PVC(vaultdata, pgdata, qdrant, redis)가 CephFS 위에 있다.

**함의 셋:**

- **`ReadWriteOnce`는 물리적 보호가 아니다.** CephFS는 본질적으로 다중 노드
  마운트가 가능하다. RWO는 k8s의 attach 로직이 강제하는 **선언**일 뿐,
  파일시스템이 강제하는 상호배제가 아니다. 즉 오늘 "안전한" 이유는
  **`replicas: 1` 하나뿐**이다.
- **C안의 인프라 전제는 이미 충족돼 있다.** RWX는 조달 문제가 아니라
  `accessModes` 변경(PVC는 immutable이므로 신규 PVC + 데이터 이전) 문제다.
  **그럼에도 C안 기각 판단은 유지한다** — 아래 이유로 오히려 강화된다.
- **MDS 스톨 위험은 가설이 아니라 오늘의 현실이다.** `document_service.py:1734`,
  `:1874`의 `to_thread` 없는 `vault_exists()`는 **CephFS에 대한 `stat()`** 이다.
  MDS가 멈추면 D-state로 블록되고, 유일한 이벤트 루프가 그대로 정지한다.
  → §4.1 첫 PR의 우선순위를 **가설적 하드닝에서 실측 기반 사고 예방으로**
  격상시킨다.

C안 기각 근거는 그대로 유효하다. 오늘은 MDS 스톨이 **파드 하나**의 문제지만,
C안은 N개 파드를 같은 MDS에 묶어 **상관 장애**로 만든다. 이미 CephFS 위에
있다는 사실은 "그러니 C안도 괜찮다"가 아니라 **"그 위험이 이미 있으니 늘리지
말라"** 는 뜻이다.

### (2) 원격 MCP는 **실제로 프로덕션에서 쓰인다**

최근 로그 2만 줄에 `MCP session started` **16건, 서로 다른 사용자 5명**
(admin 11 + 4명). 즉 `http_app.py`의 프로세스-로컬 세션 문제는 이론이 아니라
**API 레플리카 증설의 실질 게이트**다. Phase 2의 MCP 수정은 선택이 아니다.

### (3) 백엔드가 3일 전 **OOMKilled** 됐다

```
lastState.terminated: exitCode 137, reason OOMKilled
finishedAt: 2026-07-24T11:21:11Z   (uptime 3d before that)
resources: requests {cpu:1, memory:1Gi} / limits {memory:4Gi}
terminationGracePeriodSeconds: 30   ← 매니페스트 부재 → k8s 기본값 확인됨
```

- 4Gi 한도가 **이미 빠듯하다.** 프로세스를 늘리는 어떤 방안도 메모리 재측정
  없이는 불가하다. `--workers N`은 확정적으로 배제된다 (§2 A안 참조).
- `terminationGracePeriodSeconds: 30`이 **라이브에서 확인**됐다.
  `stop_workers()`의 워커당 최대 120초 예산과 충돌하므로 **매 롤아웃이 셧다운
  도중 SIGKILL**이다 — `cleanup_stale_locks`가 존재해야만 하는 이유.
- 메모리 수치를 darwin/arm64에서 측정한 문제(§7)는 이제 실측 대상이 명확하다:
  이 파드에서 `kubectl top pod` + cgroup `memory.current`.

### 이 실측이 바꾸는 것

| 항목 | 실측 전 | 실측 후 |
|---|---|---|
| PM 결정 2 (PV 백엔드) | 미해결, C안의 전제 | **해소** — 이미 CephFS. C안 기각 근거는 강화 |
| PM 결정 3 (원격 MCP) | 미해결, Phase 4 비용 변수 | **해소** — 사용 중. MCP 수정이 필수 게이트 |
| §4.1 첫 PR 우선순위 | "느린 의존성 대비 하드닝" | **실측된 CephFS + 실측된 OOM 위에서의 사고 예방** |
| A안 (`--workers N`) | 채점 25점, 이론적 배제 | **OOMKilled 실측으로 확정 배제** |

---

## 7. 미해결 / 조사 필요

- 관측성: **어떤 레플리카가 요청을 처리했는지 알 수 없다.** 로그에 인스턴스
  식별자가 없다. 멀티 레플리카의 선행 요건.
- 프론트엔드가 `/health`를 15초 주기로 폴링하는데(`use-health.ts:16,45`,
  `use-vault-health.ts:5,13,48`) 그 응답에 **프로세스-로컬 값**(write_lane
  스냅샷, role_sync 지표, backfill 준비 상태)이 섞여 있다. 라운드로빈 뒤에서는
  UI 값이 15초마다 진동한다. 세 안 모두 프론트엔드를 언급하지 않았다.
- 백업/복구 도구가 없다. RWO PVC 하나가 git 소스오브트루스 전체다.
- `RollingUpdate` 전환은 **구·신 스키마가 동시에 도는** 상황을 만든다 —
  `strategy: Recreate`가 조용히 사주고 있던 것.
- 다운그레이드 경로가 어느 안에도 명시되지 않았다. 특히 C의 git 레이아웃
  변경은 **일방향**인데 되돌릴 수 있는 것처럼 제시됐다.
- 메모리 수치(Kiwi ~550MB/프로세스)가 darwin/arm64 CPython 3.11에서 측정됐다.
  `backend/Dockerfile:1`은 `python:3.14-slim` on linux/amd64. **in-cluster
  재측정이 어떤 안이든 requests/limits 수정의 하드 게이트**여야 한다.
- `gitd`의 와이어 포맷: `cat_blob`은 raw `bytes`를 반환하므로(`git_service.py:483`)
  JSON POST에 base64 프레이밍이 필요하다. `clone_mirror`/`fetch_remote`는
  `auth_token`을 받으므로 내부 홉에 vault git 자격증명이 흐른다.

---

## 부록 — 참고 문서

- `docs/design/proposal/command-lane-write-path/` §2.5 "확장 경로"가 이미
  이 방향을 예고했다: *"write-behind 자체가 확장의 열쇠: 읽기가 PG로 가면
  git PVC가 필요한 것은 아카이버뿐 — API 파드는 자유롭게 수평 확장."*
  그 문서의 **Phase 2(PG-first git)** 는 여기 안 B의 자연스러운 후속이며,
  둘을 합치면 쓰기 HA까지 도달한다. 두 문서는 **경쟁이 아니라 순서**다.
- `rounds/round-01-option-comparison.md` — 세 안 상세 + 심사 채점
- `feedback/codex-2026-07-27.md` — Codex 독립 리뷰 전문
- `feedback/adversarial-2026-07-27.md` — 적대적 반박 5건 + 공백 10건
- `feedback/external-review-git-soc-2026-07-27.md` — **외부 리뷰("git SoC/DB 혼재/이식 불가")
  검증.** 별개 축이지만 §9에서 이 항목과 교차한다: drift 탐지기 + repair 수정이 **선행조건으로
  승격**(→ §4.3), external-git 폴러의 무거운 git I/O는 **워커 티어 분리가 공짜로 해결**(동일
  프로젝트), 나머지 이식성 항목은 직교. 신규 P0 2건(복구 도구의 손상 세탁, OKF 번들 경로
  충돌로 문서 덮어쓰기)과 미기록 ADR("git은 제품 기능이지 swappable 포트가 아니다") 포함.
