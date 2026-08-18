---
status: proposal
stage: proposal
created: 2026-08-10
updated: 2026-08-10
method: 8-agent failure-semantics audit (65 CONFIRMED / 0 REFUTED) + 7-agent judge panel (3 architects → 2 judges → 1 hostile critic → synthesis) + independent Codex review
supersedes: nothing
relates:
  - docs/design/proposal/2026-07-27-multi-process-topology
  - docs/design/proposal/command-lane-write-path
  - docs/design/proposal/2026-08-10-auth-and-runtime-hardening-plan
---

# 워커 티어 분리 아키텍처 — 크래시 복구(D1)와 dual-write(D2) 결정

> **이 문서의 위치**: `2026-07-27-multi-process-topology`가 *진단*(무엇이 깨졌나)이라면,
> 이 문서는 그 위에서 **두 개의 미결 아키텍처 결정을 확정**한다:
> D1 = 워커가 작업을 집어간 뒤 크래시하면 안전하게 복구되는가,
> D2 = git과 PG의 dual-write를 어떻게 정합하게 만드는가.
>
> **근거**: 8-에이전트 실패의미론 감사(65 CONFIRMED / 0 REFUTED) + 7-에이전트 심사패널
> (아키텍트 3 → 심사역 2 → 적대적 비평 1 → 종합 1) + **Codex 독립 검토**.
> 전체 서사·도해는 gnu-weekly 볼트
> `weekly-updates/2026-08-11/akb/[설계] 워커 티어 분리 아키텍처` 에 있고,
> 이 README는 그 결정과 근거를 리포 설계 아이템 형식으로 정리한 것이다.

---

## 0. 한 장 요약

| | 결정 | 합의 수준 |
|---|---|---|
| **D1** 워커 크래시 복구 | **시간 리스 + rescuer**, attempt를 **클레임 UPDATE 안에서** 증가 | **만장일치** (Codex + 아키텍트 3 + 심사역 2) |
| **D2** dual-write | **하이브리드** — 다만 *이음매의 위치·순서*에서 **Codex와 패널이 갈림** | **갈림 → PM 결정 필요** |

**설계를 바꾼 세 가지 적대적 발견** (전부 CONFIRMED):
1. **고아 커밋은 드문 레이스가 아니라 설계된 경로다.** 클라이언트가 끊기면 100% 발생한다.
2. **CAS는 ref를 지키지 공유 worktree 인덱스를 지키지 않는다.** 두 번째 프로세스가 생기는 순간 CAS가 통과하면서 내용이 사라진다. → gitd 전 최우선 하드 게이트.
3. **공개 페이지가 blob 부재를 빈 페이지로 렌더하고 `lru_cache`에 고정한다.**

---

## 1. 왜 이 구조인가 — 업계 기준선

AKB의 문제는 특이하지 않다. 네 가지 확립된 답이 있고, AKB는 이미 그 형태에 가깝다.

| 문제 | 업계 표준 | AKB 현재 |
|---|---|---|
| 서빙 프로세스 안의 워커 | 워커는 서빙 루프 밖 별도 Deployment (FastAPI 공식) | ❌ 최대 16개 유닛이 서빙 루프에 동거 |
| 작업 큐 | PG + `FOR UPDATE SKIP LOCKED` + 별도 워커 티어 (Rails 8 Solid Queue, River, Procrastinate) | ✅ 큐는 이미 이 모양 — consumer만 옮기면 됨 |
| 크래시 회수 | heartbeat+reaper / 시간리스+rescuer / 세션락 중 택1 | ❌ 리스만 있고 rescuer도 attempt 회계도 없음 |
| 공유 FS git 쓰기 | 단일 writer 데몬 + CAS (Gitaly `expected_old_oid` 15.6+, MUTATOR 자동재시도 금지) | ❌ CAS 없음, 프로세스-로컬 락이 유일한 상호배제 |

**핵심**: 데이터 평면(큐)은 이미 멀티프로세스를 견딘다. 못 견디는 것은 git 파일시스템과
세션/카운터류 제어 평면이다. 그래서 이 설계는 큐를 갈아엎지 않는다.

목표 토폴로지: **무상태 API 파드 N개(PVC 마운트 없음, RollingUpdate) + gitd/워커 티어 1개
(git 쓰기 전담 = 단일 writer, 백그라운드 워커 전부, RWO PVC 소유, Recreate).**

---

## 2. D1 — 크래시 복구: 시간 리스 + rescuer (만장일치)

### 2.1 지금 무엇이 새는가

클레임 트랜잭션이 **리스만 커밋**하고 `retry_count`는 안 건드린다. 예외로 잡히면
`_mark_failure`가 카운터를 올리지만, **프로세스가 죽으면 `_mark_failure`가 실행되지 않아
카운터가 그대로**다. → 리스 만료 후 재클레임 → 또 사망 → `retry_count >= 8`에
**영원히 도달하지 못함**.

### 2.2 결정

**채택**: `FOR UPDATE SKIP LOCKED` + **클레임 UPDATE 안에서 attempt 증가**(Oban/River 방식) +
`claimed_at`/`abandoned_at` 컬럼 + `pg_try_advisory_lock`으로 보호되는 rescuer.

```sql
WITH pending AS (
  SELECT id FROM chunks
   WHERE vector_indexed_at IS NULL
     AND vector_abandoned_at IS NULL
     AND (vector_next_attempt_at IS NULL OR vector_next_attempt_at <= NOW())
     AND vector_retry_count < $2
   ORDER BY created_at DESC, id
   LIMIT $1 FOR UPDATE SKIP LOCKED
)
UPDATE chunks c
   SET vector_retry_count  = c.vector_retry_count + 1,       -- ★ 여기로 이동
       vector_claimed_at   = NOW(),
       vector_next_attempt_at = NOW() + make_interval(secs => $3)  -- 가시성 타임아웃
  FROM pending p WHERE c.id = p.id
RETURNING c.id, …, c.vector_retry_count;
```

**heartbeat 기각** — (1) `replicas:1 + Recreate`라 회수해줄 살아있는 피어가 없다.
(2) 결정적: 과거 P0가 **Kiwi GIL 이벤트루프 정지**였으므로 그 루프에서 나오는 heartbeat는
정확히 그 사고 때 마감을 놓친다 — heartbeat는 `/health`의 증상이지 제어 입력이 아니다.
**세션 advisory lock 기각** — `document_service.py:386-395`에 그 패턴이 일으킨 프로덕션
풀 데드락이 기록돼 있다.

### 2.3 순진한 수정이 위험한 지점

- **어떤 `_mark_success`도 카운터를 리셋하지 않는다** → attempt-at-claim이 이걸 평생 클레임
  카운터로 바꾼다. `chunks`는 DR 절차가 의도적으로 대량 재큐잉하는 테이블. → 모든
  `_mark_success`에 `retry_count = 0` + 1회 정리 UPDATE.
- **백오프 off-by-one** → `next_attempt_delay(retry_count - 1)`로 재기준.
- **배치 클레임 부수피해** — 1:16(embed)/1:64(events)라 죽는 행 하나가 무고한 행들의
  attempt를 태운다. → 미시도 배치 credit-back + 리퍼 **비파괴화(스탬프+경보)**.
- **`external_git_poller` 제외** — 레벨 기반 reconcile이고 `quarantined` 터미널 상태가
  있으며 성공 시 카운터를 리셋하는 리포 유일의 큐.

### 2.4 터미널 상태는 비파괴

`abandoned_at` 스탬프 + 경보. 두 아웃박스(`vector_delete_outbox`, `s3_delete_outbox`)의
포기 행은 **보존**한다 — 그 행이 고아 벡터 포인트/과금 S3 객체의 유일한 포인터다. `events`만
보존기간 후 삭제(Redis MAXLEN이 이미 지나감).

---

## 3. D2 — dual-write: 갈렸다 (PM 결정)

### 3.1 두 답변

| | Codex | 심사 패널 |
|---|---|---|
| 결론 | (c) PG-first 아웃박스 + 인라인 드레인 | (c) 하이브리드 — PG-first는 유예 |
| 이음매 | TX1(정본 revision+커맨드)→커밋→gitd 바운드 드레인(트랜잭션 없이)→TX2. 타임아웃이면 `archive_pending` | 커밋 멱등성 + 클라이언트 재시도 분류기 |
| Phase 3 | **순서를 뒤집어라.** PG-first가 로컬에서 되기 전엔 동기 gitd RPC에 트래픽 금지 | 잔재 생산자 차단→탐지기→doctor→gitd→그 다음 D2(b) |
| 근거 | "이미 복구 불가능한 로컬 dual-write를 복구 불가능한 **네트워크** dual-write로 만든다" | "`documents`에 본문 컬럼이 없고 6개 읽기 지점이 git에서 본문을 읽는다. 역사를 리포에서 가장 관측 불가능한 기계장치로 옮기는 건 순서가 틀렸다 — PrometheusRule·/metrics가 **하나도 없다**" |

### 3.2 두 판단이 일치하는 부분 (신뢰도 높음)

PG-first가 최종 목적지일 수 있으나 논쟁은 *타이밍*. CAS는 필요하되 불충분. 결정론적
커밋 메타데이터(`GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE`) + 멱등키/트레일러 필요.
**MCP 프록시 재시도 분류기가 위험**(양쪽이 독립적으로 `proxy.mjs`를 찾음). `documents`에
정본 본문이 없다는 게 PG-first의 진짜 비용.

### 3.3 코드베이스가 강제하는 제약 (PG-first가 작은 패치가 아닌 이유)

`documents`에 정본 본문 없음(`init.sql:174`) / GET은 `current_commit`, update·edit은
떠다니는 HEAD를 읽음 / `DocumentPutResponse.commit_hash`가 필수 필드 →
응답 유니온 필요 / `expected_commit` OCC 토큰이 git 지연 중 무효 → `revision_id` 필요 /
publication이 git에서 본문 읽음 / **히스토리 이미지 인가가 git 커밋 해시로 키잉**(migration
`062:46`, PR #344 기능) / 외부-git vault는 권위 방향이 반대.

> **역전 조건**: "모든 성공 변경이 동기적으로 git SHA를 반환해야 하고 git이 즉시
> 현재-콘텐츠 권위여야 하며 pending 쓰기 금지"가 제품 요구라면 D2 선택이 뒤집힌다 —
> 그 경우 git-first + 영수증/멱등키·CAS·지속 조정을 붙이되, **어떤 로컬 설계도 git과 PG를
> 원자적으로 커밋할 수 없다**는 점을 명시적으로 수용해야 한다.

---

## 4. 설계를 바꾼 적대적 발견 3건

### 4.1 고아 커밋은 설계된 경로다

클라이언트 끊김/300초 타임아웃 시 `write_lane._dispatch_to_pool`이 커밋을 중단하지 않고
끝까지 실행한 뒤(추가 취소도 흡수) 결과를 버리고 *"a stray commit may exist…"* 를 로그로
남기며 `CancelledError`를 재발생 → `_path_lock` 트랜잭션 언와인드 → PG 롤백. **100%, 매번,
설계상.** 여기에 `proxy.mjs`가 mutating 도구를 타임아웃에 재시도해 **중복 문서**를 만든다
(`akb_put`은 create-only, `-shortid` 접미). → D2 이음매를 통계 hoisting이 아니라
**커밋 멱등성 + 재시도 분류기**로 옮긴 이유.

### 4.2 CAS는 ref를 지키지 공유 인덱스를 지키지 않는다 — gitd 하드 게이트

두 프로세스가 공유 worktree를 쓸 때, B의 `reset --hard HEAD`가 A의 `write_text`와
`write_tree` 사이에 끼어들면 A의 파일이 지워진다. 그런데 A의 트리는 HEAD와 같고 부모도
HEAD라서 **CAS가 통과**하고, 내용 없는 커밋 sha가 PG `current_commit`에 기록된다.
**CAS가 구조적으로 탐지할 수 없는 발산.** 수정: 쓰기마다 사설 인덱스(`GIT_INDEX_FILE` +
`read-tree`, `reset --hard` 없음) 또는 크로스-프로세스 worktree 뮤텍스.

### 4.3 공개 페이지가 손상을 정상 렌더하고 캐시에 고정

`read_file`이 blob 부재 시 `None` 반환(예외 아님). 내부 독자는 전부 `NotFoundError`로
처리하지만 `publication_service.py:1167-1176`은 `if raw:`라 None이면 body=''로 남고
`content_unavailable=False`로 표시 → 404도 플레이스홀더도 로그도 없이 빈 공개 페이지 →
`lru_cache(maxsize=64)`에 프로세스 수명 내내 고정. drift의 실제 폭발 반경은 공개 페이지다.

---

## 5. 로드맵 (12단계, 각 단계 독립 출하 + 자체 롤백)

| # | 단계 | 롤백 |
|---|---|---|
| **P0** | 종료 정확성 (stop 이벤트 일괄 발사→gather, `terminationGracePeriodSeconds`) | 단순 revert |
| **P1** | 마이그레이션 065 (가산만, `claimed_at`/`abandoned_at` + 부분 인덱스) | DROP COLUMN |
| **P2** | 클레임 의미론 = F1 수정 (attempt 클레임 안으로, 6개 워커) | revert |
| **P3** | 터미널 상태 = F2 수정, 비파괴 | revert |
| **P4** | rescuer + 관측 + 실제 경보 경로 | revert |
| **P5** | P0 블로킹 I/O 번들 (08-10 계획의 P0) | revert |
| **P6** | git 쓰기 하드닝 + 재시도 분류기 | revert (CAS는 플래그 뒤) |
| **P7** | 증거 세탁 두 도구 정지 (resource_integrity, publication) | 2줄 revert |
| **P8** | drift 탐지기 (분류 포함, 허용목록·미러 규칙) | revert |
| **P9** | akb-doctor 복구 경로 (`revert`만 파괴적, fail-closed) | CLI만 |
| **P10** | gitd 선행조건 (§6 게이트 전부, 최우선은 §4.2 공유 인덱스) | — |
| **P11** | gitd Phase 1–4 → D2(b) 재개 | Phase 3부터 일방향 |

**P0와 P5는 다른 모든 결정과 무관하게 옳다** — 오늘 사용자에게 영향을 주는 결함이고
이미 08-10 계획의 P0다.

---

## 6. gitd 출하 전 하드 게이트

1. D1이 P1–P4까지 출하 + 최소 1회 전체 롤아웃 사이클 관측.
2. 양쪽 티어 `terminationGracePeriodSeconds` + `stop_workers` signal-all-then-gather.
3. **★ 트리가 CAS된 부모에서 파생 — 사설 인덱스 또는 worktree 뮤텍스** (§4.2).
4. worktree 브랜치 CAS 라이브 + `_commit_via_clone`은 명시적 non-forced push.
5. 결정론적 커밋 + write-id 트레일러 + 빈 트리 short-circuit + `proxy.mjs` 분류기 축소.
6. drift 탐지기 라이브 + §4.3 두 세탁 경로 먼저 수정.
7. `_CREATE_LOCKS`를 PG advisory lock으로.
8. **fail-closed 부트 단언**: `gitd_url` 빈 API 파드는 부팅 거부.
9. gitd 단일-writer 강제 명문화 — RWO는 노드 스코프이고 CephFS라 파일시스템 강제 없음.
   `ReadWriteOncePod` 검증 또는 명시적 수용 + PodDisruptionBudget.
10. 2-프로세스 레이스 하네스를 `check.sh`에 배선 + 수정 전 RED 시연.
11. MCP 세션 처리 해결 — 단 재시도 분류기 축소 이후에.
12. `deploy-internal.sh` kubectl 컨텍스트 가드.
13. 복구 훈련(restore drill) 또는 없다는 PM의 서면 수용.

---

## 7. 남는 위험

- 배치 클레임 부수피해는 설계 변경 없이는 완전 해소 불가.
- 하이브리드 이음매는 지배적 잔재원(§4.1)을 닫지 못한다.
- 탐지기 녹색은 무행동 정당화 불가 — drift 0은 레플리카가 하나여서다(gitd가 제거하는 조건).
- `vector_retry_count` 의미 변경("실패" → "마지막 성공 이후 시도").
- 두 아웃박스 포기 행 영구 보존(유일한 포인터) → 조치 없으면 무한 증가.
- **쓰기 HA는 이 계획 어디에도 없다.** Option B도 안 준다.
- **백업/복구 능력이 리포에도 클러스터에도 없다.**

---

## 8. 열린 PM 결정

1. **D2의 순서** — Codex(PG-first 먼저) vs 패널(gitd 먼저). 이 문서가 임의로 봉합하지 않는다.
2. **M1 네이티브 revision 원장은 프로덕션용인가 연구용인가?** 완전한 PG-네이티브 원장이
   이미 트리에 있다(`native_revision_service._publish_replace` — 매니페스트+revision+head+
   activity를 한 트랜잭션에, `mutation_id`와 재생 탐지까지, git을 0번 만짐). D2(b)의 절반이
   이미 구현돼 있을 수 있다.
3. **쓰기 HA가 실제 제품 요구인가?**
4. **소스오브트루스 선언을 바꾸는가?** D2(b)는 "PostgreSQL + Git are source of truth"를
   "PG=현재, git=역사"로 재정의해야 한다 — 서면 결정 사안.
5. `documents.content`를 가산 이중쓰기 컬럼으로 지금 잡을지 — 단 P8 이후에만.
6. 포기 아웃박스 행에 운영 SLA가 있는가?
7. `ReadWriteOncePod` 검증 또는 수용.
8. AKB는 무엇을, 어디로 페이징하는가? (경보 경로가 새 능력)

---

## 부록 — 참고

- `feedback/audit-2026-08-10.md` — 8-에이전트 실패의미론 감사(주장별 file:line, 65 CONFIRMED)
- `feedback/panel-and-codex-2026-08-10.md` — 심사패널 결론 + Codex 독립검토 대조
- `rounds/round-01-d1-d2-options.md` — D1/D2 각 선택지와 기각 근거
- gnu-weekly 볼트: `weekly-updates/2026-08-11/akb/[설계] 워커 티어 분리 아키텍처` (전체 도해)
