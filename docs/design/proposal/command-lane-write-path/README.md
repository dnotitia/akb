# Command-lane 쓰기 경로: 문서별 lane + PG-first git + overflow 프로토콜

**상태**: Proposal (round-04까지 반영)
**날짜**: 2026-06-10
**목표**: 동시 ~1000 에이전트. 특정 문서에 쓰기가 몰리는 폭주(~80 writes/s)
에서도 피해가 **그 문서 하나로 한정**되어야 한다 — 지금은 서비스 전체가 멈춘다.

---

## 0. 한 장 요약

> **문제**: 한 문서의 쓰기 한도는 초당 10~25건인데, 그걸 넘는 순간 느려지는
> 게 아니라 **전역 커넥션 풀이 락 대기자로 가득 차 서비스 전체가 30초
> 타임아웃 루프에 빠진다.** 원인은 직렬화 단위(이미 문서별로 올바름)가
> 아니라 **대기 방식** — 줄을 서려면 전역 자원(풀 커넥션)을 쥐어야 한다.
>
> **해법**: ① 대기를 풀에서 **문서별 메모리 큐(lane)** 로 옮기고(장애 격리),
> ② 쓰기 순서를 **PG 먼저**로 바꿔 git 커밋을 평상시엔 그 자리에서,
> 폭주 시에만 뒤로 미루고(처리량), ③ 큐에 상한·병합·202 응답 정책을
> 단다(우아한 퇴화).

| 바뀌지 않는 것 | 바뀌는 것 |
|---|---|
| 평상시 사용자 경험 — 동기 응답 + 커밋 해시 | 대기 위치: 락 대기(커넥션 점유) → 메모리 큐 |
| 평상시 "쓰기 1건 = git 커밋 1개" | 쓰기 순서: git 먼저 → **PG 먼저** |
| MCP API 형태 (additive 변경만) | 본문 서빙: git → **PG** (`documents.content` 신설) |
| 문서별 직렬화 단위 | 폭주 시: 전면 장애 → 그 문서만 지연/202 |
| PG 내부 트랜잭션 원자성 | `current_commit`: "지금 커밋" → "마지막 아카이브 커밋" |

---

## 1. 문제

### 1.1 쓰기 한 건의 현재 경로

```
① PG 커넥션 획득 (풀 20개, db/postgres.py:18)
② 그 커넥션으로 (vault, path) advisory lock (document_repo.py:32)  ← 대기 지점
③ 락을 쥔 채: git 읽기 → git 커밋(~73ms 실측) → PG 쓰기(row+chunks+이벤트)
④ 응답 (커밋 해시)
```

임계 구역 ②~③은 **40~100ms** → 문서당 초당 **10~25건**이 한계.
git 커밋 구간은 vault 단위 락(git_service.py:532)이라 이 한계는 사실상
**vault당**이기도 하다.

### 1.2 핫 문서에 80 writes/s가 오면: 문제는 세 겹

- **(A) 처리량 한계** — 80/s 유입 vs ~15/s 처리. 어떤 락 방식으로도 안
  바뀌는 산수다 (한 문서의 히스토리는 직렬이므로).
- **(B) 풀 중독 — 치명** — ②에서 줄을 서려면 ①의 커넥션을 쥐고 있어야
  한다. 정체는 문서별인데 대기 비용은 전역 풀로 지불:

  ```
  t=0.0s  80건 도착 → 1건 처리, 19건이 커넥션 쥔 채 대기
  t=0.3s  풀 20개 소진 → 다른 문서·vault·읽기까지 전부 정지
  t=30s   statement_timeout으로 대기자 일괄 사망(대량 500) → 새 유입이 재충전 → 반복
  ```

- **(C) 무정책** — 초과 유입을 거절/병합/예약할 정책이 없다. 30초 폭사가
  유일한 결말.

### 1.3 코드베이스 사실 확인 (rounds 02–04에서 전수 검증)

| 항목 | 결론 | 근거 |
|---|---|---|
| PG 내부 원자성 (row+chunks+relations+이벤트) | **보장됨** | `_path_lock` 단일 tx (0.3.7 #99) |
| GET이 보여주는 "현재" | **PG가 중재** — 본문을 `current_commit`에 핀 | 0.8.6 #170, document_service.py:414 |
| git↔PG 크로스 스토어 원자성 | **없음** — git 먼저(:318/:614), PG 실패 시 git을 되돌리는 보상 없음. 크래시 윈도우에 스트레이 커밋 | round-02 검증 |
| update/edit의 merge-base 읽기 | **여전히 floating HEAD** (:576/:748) — #170은 GET만 고침 | round-04 |
| akb_sql로 documents 우회 쓰기 | **불가** — GRANT는 `vt_*`만. 모든 문서 쓰기는 API 경유 → lane이 전수 포착 | role_sync.py |
| files / tables의 git 관여 | **없음** (S3+PG / PG only) → 본 설계 범위 = documents | round-04 |
| 프로세스 모델 | 단일 uvicorn 프로세스, replicas=1, RWO PVC → 인메모리 lane 유효 | Dockerfile, deploy/k8s |

---

## 2. 설계

### 2.0 전체 그림

```
요청 → 동기 검증(권한/vault/스키마) → 문서별 lane(인메모리 큐, 상한)에 적재
lane 처리자 (lane당 1개, 직렬):
  tx1 (PG): row + 본문(content) + chunks + 이벤트 + git_commit_outbox(open)
  [평상시]  이어서 git 커밋 + tx2(outbox 닫기, current_commit=H) → 응답(커밋 해시)
  [압력 시] tx1 직후 응답(archive_pending + content_hash/revision)
            → vault별 아카이버가 뒤에서 드레인 (같은 path N건 = 1커밋)
  [한도 초과] 202 {queued, command_id, queue_depth, retry_after}
크래시 복구: outbox = git의 WAL. git이 PG 콘텐츠보다 앞서는 일은 없다.
```

세 컴포넌트가 문제 A/B/C에 1:1 대응한다.

### 2.1 [B 해결] 문서별 command lane

쓰기를 커맨드로 래핑해 `(vault_id, path)`별 인메모리 큐에 넣고, lane당
1개의 소비자가 직렬 처리한다. 호출자는 future를 await — 외부에서는 지금과
같은 동기 API.

- **대기자가 쥐는 것이 커넥션 → 메모리로 바뀐다.** 커넥션은 처리 중인
  1건만 사용. 핫 문서가 아무리 밀려도 풀은 건강하고, 장애 반경이
  "서비스 전체 → 그 문서 하나"로 준다.
- 검증·권한·vault 확인은 enqueue 시점에 동기로 끝낸다 — 비동기로 미뤄지는
  실패는 희귀한 인프라 오류뿐.
- advisory lock은 처리 중에만 유지(이중 안전장치). **대기**에 쓰지 않는
  것이 핵심.
- 이것은 actor/mailbox 패턴이지 pub/sub EDA가 아니다.

### 2.2 [A 해결] PG-first git — 평상시 inline, 압력 시에만 write-behind

> round-02에서 "상시 비동기+병합" 초안을 폐기하고 적응형으로 수정. 이유:
> 평상시까지 커밋이 뭉개지고, "데이터는 반영됐는데 히스토리에 없는" 불일치가
> 일상화되는 비용이 과지불이라서.

순서를 **PG 먼저**로 통일하고, git 커밋의 **시점**만 부하에 따라 달라진다:

- **평상시 (inline)**: lane 처리자가 tx1 직후 같은 요청 안에서 git 커밋과
  장부(tx2)까지 끝내고 응답. **지금과 동일** — 쓰기 1건 = 커밋 1개, 응답에
  커밋 해시, 히스토리 지연 0. (lane 덕에 대기자가 커넥션을 안 쥐므로 git
  73ms가 동기여도 풀은 안전하다.)
- **압력 시 (deferred)**: 백로그 임계 초과 또는 git 장애 시, tx1 직후
  바로 응답하고 vault별 아카이버가 outbox를 드레인. 아카이버는 **PG의
  현재 본문**을 읽어 커밋하므로(level-based, 델타 재생 아님) 같은 path에
  밀린 N건이 1커밋으로 병합되고, 폭주가 끝나면 path당 1커밋으로 **수 초
  내에 따라잡는다.**

**정확한 시퀀스와 크래시 윈도우** — 이 설계가 주는 것은 원자성이 아니라
**명시된 윈도우를 가진 수렴적 일관성**이다:

```
tx1 (PG): row + 본문 + chunks + 이벤트 + outbox(open)   ← 원자적
git:      커밋 H 생성 (inline 또는 아카이버)
tx2 (PG): outbox 닫기 + current_commit = H              ← 원자적, tx1과 별개
```

- **W1 (tx1↔git 크래시)**: PG만 앞섬 → outbox가 열려 있으므로 아카이버가
  수렴. ✓
- **W2 (git↔tx2 크래시)**: 커밋은 존재, 장부 미반영 → 아카이버의
  tree-동일 "skip"은 **커밋 생성만 생략하고 tx2는 반드시 수행** (안 그러면
  current_commit이 영원히 낡는다).
- 어느 윈도우에서도 git이 PG **콘텐츠**보다 앞서지 않는다 — 현행 스트레이
  커밋 윈도우(§1.3)는 사라진다. 단 `(content, current_commit)` 계약은
  #170의 "항상 일치"에서 **"current_commit은 콘텐츠와 같거나 과거"**로
  바뀐다 (GET 본문이 PG 서빙이라 사용자 영향 없음).

**수렴 보장의 전제 3가지 (구현 필수)**:

1. **아카이버는 abandon하지 않는다** — `events_publisher`의
   MAX_RETRIES→abandoned 패턴을 복사하면 "git이 반드시 따라온다"가 깨진다.
   무한 재시도(백오프 상한) + 백로그 알림.
2. **삭제도 상태다** — "PG에 행 없음" → "git 삭제 커밋"으로 수렴해야
   update→delete 레이스에 유령 파일이 안 남는다.
3. **드레인 직렬화** — inline 드레인과 아카이버의 tree비교+커밋은
   per-vault git 락 아래에서 원자적으로 (중복 커밋 방지).

**히스토리 불일치의 정직한 범위**: 평상시 0 / 폭주 시 "폭주 지속 + 수 초" /
아카이버·git 장애 시 복구까지 (→ `/health` 백로그 + 알림 필수).
deferred 구간에는 히스토리 조회에 outbox 기반 **"아카이브 대기 중" 가상
엔트리를 오버레이**해 사용자는 빠짐없는 히스토리를 즉시 본다.

**비용**: ① 낙관적 토큰을 `expected_content_hash`(기존) 또는 revision
카운터로 교체(deferred 응답엔 커밋 해시가 없으므로), ② 읽기 경로의 PG
이전(§2.4 — 최대 공사), ③ 폭주 시간대 중간 버전은 커밋으로 안 남음(의도는
audit에 기록 → 미결 #1), ④ 두 모드의 존재 자체가 복잡도(전환 조건·응답
분기를 테스트로 고정).

**Source of truth 재정의 (이 제안의 핵심 결정)**: *PG = 현재 상태의 진실,
git = 히스토리의 진실.* 평상시 git 지연이 0이어도 원칙은 같다 — "현재"를
결정하는 쪽은 PG다. (#170이 GET에 이미 도입한 방향의 일반화.)

### 2.3 [C 해결] 큐 상한 + coalescing + 202 overflow

- **Coalescing (deferred 한정)**: 같은 lane의 full-replace는 마지막 것만
  실행, 모두에게 같은 결과. `expected_*` 커맨드는 절대 병합·지연 없음
  (충돌은 보고 대상 — 동기 처리 또는 즉시 409). edit의 unchanged 단락
  (:775)도 보존.
- **평상시**: T초(3~5s) 내 완료 → 동기 응답. 에이전트는 변화를 모른다.
- **한도 초과 시**: 커맨드를 PG에 **먼저 영속화**(메모리만 믿고 "접수됨"을
  주면 크래시에 조용히 유실) 후 `{status:"queued", command_id, queue_depth,
  retry_after_ms}` 반환. 상태 조회는 command_id로 PG에서 — **어느 파드든
  응답, 파드 라우팅 불필요.** MCP description에 계약 명시.
- 기본 fire-and-forget이 아닌 이유: tx1 동기 구간이 10~20ms라 비동기화로
  아낄 게 없고, read-your-writes가 깨지며, 폴링 비용이 에이전트
  컨텍스트(LLM 턴)로 전가된다. → **overflow 전용.**

### 2.4 코드베이스가 부과하는 구현 제약 (round-04 전수 검토)

1. **적용 범위 = documents만.** files는 S3+PG, tables는 PG only로 git과
   무관함을 확인.
2. **미러 vault(external_git) 제외.** 방향이 반대(상류 git → PG)다.
   아카이버는 `source=external_git` vault를 건너뛴다. ⚠ 미러 vault에 대한
   사용자 쓰기 거부 가드는 이번 검토에서 발견 못 함 — Phase 2 전 확인/보강.
3. **`documents.content` 컬럼 신설 필수.** 본문은 chunks에서 재조립 불가
   (모든 chunk에 메타 헤더가 베이크됨). 아카이버의 풀 마크다운 재구성은
   기존 `_compose_markdown`(row 메타 + body) 재사용.
4. **git 읽기 이전 대상 전수** (#170은 GET만 고쳤다):
   update/edit merge-base(:576/:748), browse 해시 백필(:1065),
   **publication 렌더(:794)**, activity 피드(activity.py:34 — git이 원천인
   기능이므로 이전이 아니라 pending 오버레이로 보완).
   부수 발견: publication 렌더와 activity 라우트는 **동기 git 호출로 event
   loop을 블록**하는 잠복 이슈 — Phase 2의 자연 수혜 + 단기 수선 후보.
5. **bulk 연산**: collection 삭제가 `delete_paths_bulk`(:401)로 다중 path를
   1커밋 처리 — per-path 톰스톤을 outbox에 일괄 enqueue하고 아카이버가
   멀티패스 배치 커밋(선례 유지).
6. **빈 repo 첫 커밋**: vault 생성이 가이드 문서를 시드(:1296) — 아카이버에
   parentless 커밋 경로 필요 (현 `_commit_via_clone` 대체, 클론 제거).

### 2.5 확장 경로 (기록만, 지금 안 만듦)

멀티 파드가 필요해지면 lane을 공유 기질에서 claim한다 (PG `FOR UPDATE
SKIP LOCKED` — 기존 관용구). write-behind 자체가 확장의 열쇠: 읽기가 PG로
가면 **git PVC가 필요한 것은 아카이버뿐** — API 파드는 자유롭게 수평 확장.

---

## 3. 기각한 대안

| 대안 | 기각 사유 |
|---|---|
| API 경로를 순수 낙관적 동시성으로 | 고경합에서 재시도 폭풍 — 처리량 그대로, 낭비 5~6배. 낙관적 CC는 내부 백그라운드 에이전트(gardener류)의 자리 |
| worktree-per-write + git merge | 충돌할 거리가 없다 (같은 문서는 lane이 막고, 다른 문서 머지는 항상 trivial). 존재하지 않는 충돌을 푸는 복잡도 |
| 전역 단일 lane (LMAX식) | 처리 40~100ms → 시스템 전체가 10~25 req/s로 캡. 반드시 (vault,path) 파티셔닝 |
| 기본 fire-and-forget | read-your-writes 깨짐, 폴링 비용이 에이전트 토큰으로 전가, 실패 미관측. overflow 전용으로만 채택 |
| 파드별 service_id 라우팅 + orchestrator | 파드 토폴로지에 클라이언트 결합(즉시 stale), 분산 큐 재발명. git PVC가 RWO라 어차피 단일 writer — 잘못된 층의 확장 |
| full event sourcing (커맨드 로그 = 원장) | git이 이미 콘텐츠의 event store. 세 번째 원장은 정합성 부담만. 커맨드 로그는 audit |
| pub/sub EDA 코어 | MCP 호출은 동기 명령. 이벤트는 엣지(관측 피드 + 내부 에이전트 wake-up)에만 |

---

## 4. 단계별 롤아웃

| Phase | 내용 | 비고 |
|---|---|---|
| **0** | try-lock + 429(Retry-After) — 풀 중독만 즉시 차단 | ~1일, 스키마 변경 없음, 단독 출하 |
| **1** | 인메모리 lane (락 대기 → 큐 대기) | lane 수명주기·graceful shutdown. 단일 프로세스 확인됨 |
| **2** | PG-first git: `documents.content` + outbox + 아카이버(inline/deferred), 읽기 이전 전수(§2.4-4), 토큰 마이그레이션, 미러 제외, bulk 톰스톤, parentless 커밋, pending 오버레이 | **최대 공사**. source-of-truth 재정의 승인 필요 |
| **3** | 큐 상한 + coalescing + 202 + 상태 조회 툴 + MCP description | additive 계약 변경 |

**검증**: 핫 문서 burst E2E(타 문서·읽기 무영향 + 풀 비소진 + 폭주 후
PG↔git 수렴), 아카이버 kill→재시작 수렴, W2 크래시 시뮬레이션(장부 수복),
prod Linux 벤치 재측정.

---

## 5. PM 결정 필요

1. **히스토리 충실도** — 평상시 1:1은 유지됨. **폭주 시간대만** 커밋
   배치(N→1, 의도는 audit)를 허용할 수 있는가 + pending 오버레이 방식 동의.
2. **source-of-truth 재정의 승인** — PG=현재, git=히스토리 (Phase 2 게이트).
3. **202 overflow 계약** — 에이전트 UX로 수용 가능한가 (Phase 3 게이트).
4. **80 writes/s/문서가 진짜 제품 케이스인가** — 이벤트 스트림 워크로드라면
   테이블/append+컴팩션으로 재모델링이 정답이고 Phase 0+1로 오래 버틴다.
   **가장 레버리지 큰 질문.**
5. **미러 vault 쓰기 가드** — 현재 코드에서 명시적 거부를 확인 못 함
   (round-04-9). 기존 동작 확인 후 Phase 2 전 보강 여부 결정.

---

## 부록 A — 측정 근거

`commit_file`과 동일한 git 시퀀스(reset→add→write-tree→commit-tree→
update-ref)를 bare+worktree에 50회 반복 (dev Mac):

```
50 commits in 3653 ms → 73.1 ms/commit  (git CLI만; GitPython·PG 제외)
```

prod Linux는 30~50ms 예상 + GitPython 오버헤드 + PG 5~15ms → 본문의
임계 구역 40~100ms 추정의 앵커.

## 부록 B — 코드 참조 맵

- 잠금·파이프라인: `document_service.py:243`(`_path_lock`), put :306→:343,
  update :576/:614/:622, edit :748/:795, browse 백필 :1065
- GET의 current_commit 핀(#170): `document_service.py:414`
- advisory lock: `document_repo.py:15` / 풀 설정: `db/postgres.py:15`
- vault 락 + worktree 커밋: `git_service.py:39,532` / bulk:
  `collection_service.py:401` / 시드: `document_service.py:1296`
- 동기 git 호출(잠복 이슈): `publication_service.py:794`, `activity.py:34`
- outbox 선례: `events_publisher.py`, `vector_delete_outbox`,
  `metadata_worker.py` / SQL 격리: `role_sync.py` (GRANT는 `vt_*`만)
- 스키마: `init.sql:147-150` (current_commit, content_hash,
  content_hash_commit — revision 없음)
