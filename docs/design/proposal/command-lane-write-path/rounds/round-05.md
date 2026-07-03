# round-05 — executor 기아 발견, admission 게이트 확정 (Phase 0 대체)

**날짜**: 2026-07-02
**참여**: PM(사용자) + Claude (별도 세션에서 PG 풀 세팅 #255와 병행 토의)
**결론**: 아래 설계를 PM이 승인, 즉시 구현. round-01~04의 Phase 0(try-lock
즉시 429)을 **대체**하고, Phase 1의 per-document 세분화는 **연기**,
Phase 2(PG-first)·Phase 3은 **미결 유지**.

---

## 1. 새 발견: 두 번째 전염 경로 — executor 기아

round-01~04는 풀 중독(§1.2-B)만 식별했다. 이번 토의에서 코드 재검증 중
**독립적인 두 번째 전염 경로**를 확인했다:

- 문서 read도 전부 `asyncio.to_thread(git.read_file, …)`로 간다
  (document_service.py:543, :759, :1447 등). **read와 write가 같은 기본
  ThreadPoolExecutor를 공유**한다.
- write는 executor 스레드 **안에서** `_vault_lock`(threading.Lock)을
  blocking으로 기다린다 (git_service.py:566). 대기자 1명 = 스레드 1개 점유.
- 기본 풀 크기는 `min(32, cpu+4)` — round-04까지 "~32"로 적었으나 이는
  상한이고, CPU limit 없는 파드는 노드 코어 수를 따르므로 8코어 노드면
  **12개**다.
- 따라서 핫 vault에 writer 12명(8코어 기준)이 몰리면 — PG 풀(30)이 마르기
  **전에** — executor가 잠든 waiter로 가득 차고, **모든 vault의 모든 git
  read가 executor 큐에서 시작조차 못 한다.**

즉 round-04까지의 "read는 락을 안 잡으므로 write에 안 막힌다"는 git 락
레벨에서만 참이고, executor 레벨에서는 거짓이다. **풀 중독을 고쳐도(#255)
같은 사용자 가시 장애가 executor 경로로 재현된다.** 두 경로는 한 설계로
같이 막아야 한다.

## 2. 확정 설계: 2단 admission 게이트 + 전용 커밋 executor

핵심 원리(round-01의 통찰 유지): **대기를 희소 자원(스레드·커넥션) 위에서
하지 말 것.** 대기는 이벤트루프의 suspend된 코루틴으로 — 비용은 메모리
몇 KB + waiter 리스트 엔트리뿐이다.

```
요청 → ① per-vault 게이트 (vault당 동시 1, FIFO)     ← 코루틴 대기, 자원 0
      → ② 글로벌 write lane 세마포어 (동시 M)         ← 〃
      → ③ PG 커넥션 + (vault,path) advisory lock + 트랜잭션   ← 여기서 처음 자원 획득
      → ④ git 커밋: 전용 커밋 executor로 디스패치      ← 락은 이미 무경합
      → ⑤ PG 반영 → 트랜잭션 커밋 → 전부 반납
```

- **①이 ②보다 먼저다** — 순서가 바뀌면 핫 vault의 대기자들이 글로벌 슬롯
  M개를 쥔 채 자기 vault 게이트를 기다려, 다른 vault로 전염이 재발한다.
- ②는 write라는 **작업 부류 전체**의 총량 상한. 활성 vault가 M개를 넘어도
  write가 쥐는 커넥션 ≤ M, 커밋 스레드 ≤ M이 어떤 시나리오에서도 보장된다.
  read에는 항상 커넥션 (풀−M)개 + read 스레드 풀이 남는다.
- **④ 전용 executor** — 커밋이 read의 스레드 풀을 침범할 수 없게 물리
  절연. 크기 = M (게이트 통과자만 도달하므로 큐잉 없음).
- 기존 `_vault_lock`(threading.Lock)은 **제거하지 않는다**. external_git
  poller 등 API 밖 커밋 경로가 있으므로 최후의 정합성 가드로 남긴다.
  평상시엔 게이트 덕에 무경합 통과.

### 게이트 granularity: per-vault (per-document 아님)

round-01~04의 lane은 `(vault_id, path)` 단위였다. 이번 라운드에서
**vault 단위로 결정**: git 커밋이 vault 단위로 직렬화되는 이상(worktree
구조), per-document lane은 같은 vault 문서들의 PG 구간(10~20ms)만 겹치게
할 뿐 지배 비용(git ~73ms)은 어차피 직렬이다. per-document 세분화가 의미를
가지는 것은 git이 동기 경로에서 빠지는 Phase 2(PG-first) 이후다. 그때
lane을 쪼개면 된다 — admission 모듈은 키 문자열만 바꾸면 된다.

### 실패 모드: 대기 후 429 (즉시 429 아님) — PM 결정

- 게이트 대기 총 데드라인 **T = 10초** (PM: "30초는 너무 길다").
  초과 시 **HTTP 429 + Retry-After** / MCP 캐논 에러 envelope
  (`code=write_busy`, hint에 재시도 안내).
- 즉시 429를 버린 이유: 에이전트의 정상 패턴(멀티파일 인제스트의 연속
  put)을 처벌한다. 커밋 ~73ms면 대기 몇 건은 1~2초에 빠진다 — 대기가
  코루틴이라 공짜이므로 기다리게 하는 것이 안전해졌다(이 선택지는
  admission 이동 없이는 고를 수 없었다).
- 깊이 상한 K로 조기 거부하지 않는다(PM 결정). 대신 **글로벌 waiter
  백스톱**(기본 512)만 둔다 — 메모리·소켓 가드일 뿐 정책이 아니며,
  정상 운영에서 도달하지 않는 값.

### 트랜잭션 경계: 불변 — "커넥션 조기 해제" 기각

"git 커밋 전에 커넥션을 놓는" 변형을 검토 후 **기각**(PM 동의):

1. advisory lock이 커넥션/트랜잭션 수명에 묶여 있어 반납 순간 path 상호
   배제가 사라진다.
2. git 커밋 순서(vault 락 보장)와 PG 반영 순서가 역전될 수 있다 — A가
   sha1, B가 sha2를 커밋한 뒤 B→A 순으로 DB에 쓰면 `current_commit`이
   과거(sha1)로 되돌아가 B의 쓰기가 메타데이터에서 증발(lost update).
   막으려면 tx2 CAS + 보상 로직 + 고아 커밋 처리가 필요 — Phase 2의
   outbox 설계가 바로 그것이므로, 어중간한 중간형을 만들지 않는다.

병리는 "커밋 ~100ms 동안 커넥션을 쥐는 것"이 아니라 "**대기자 N명이
커넥션을 쥔 채 줄 서는 것**"이었다. 대기를 ③ 앞으로 옮기면 커넥션 점유
시간 = 실제 작업 시간이 되고, 그런 writer는 전역 ≤ M명이다. 기존
단일-트랜잭션 원자성(§1.3)과 크래시 의미론은 오늘과 동일하게 유지된다.

### 꼬리 보강: git write 타임아웃

커밋을 쥔 채 git이 wedge되면(디스크, 대형 vault의 reset --hard 스윕)
lane 슬롯 1 + 커밋 스레드 1 + 커넥션 1이 잠긴다. PG 쪽 백스톱
(`idle_in_transaction_session_timeout` 60s)에 더해 git 쓰기 경로의 각
명령에 `kill_after_timeout`(기본 30s)을 건다. 발동 시 해당 요청은 실패
(트랜잭션 롤백 — PG는 깨끗, worktree는 다음 쓰기의 reset --hard가 수습)
하고 자원은 회수된다.

## 3. 파라미터 (config)

| 설정 | 기본 | 의미 |
|---|---|---|
| `write_lane_concurrency` (M) | 8 | write 부류의 전역 동시성 = 커밋 executor 크기. 커넥션 상한도 겸함 (풀 30 중 read에 최소 22 보장) |
| `write_lane_queue_timeout_secs` (T) | 10.0 | ①+② 합산 대기 데드라인 → 초과 시 429 |
| `write_lane_max_waiters` | 512 | 글로벌 waiter 백스톱 (메모리 가드, 정책 아님) |
| `git_write_timeout_secs` | 30.0 | git 쓰기 명령별 kill 타임아웃 |

## 4. 적용 범위

| 경로 | 게이트 | 커밋 executor | 비고 |
|---|---|---|---|
| put / update / edit / delete / move | ✓ (`_path_lock`/`_move_lock` 진입 전) | ✓ | 표준 write 경로 |
| collection 삭제의 `delete_paths_bulk` | ✓ | ✓ | PG 커밋 후 실행 — lane 타임아웃 시 기존 "고아 파일 로그 후 계속" 경로로 (요청은 성공 유지) |
| create_vault 시드 / 템플릿 가이드 커밋 | ✗ | ✓ | 생성 중 vault라 경합 불가 — 타임아웃 실패 모드를 vault 생성에 도입하지 않음 |
| external_git poller / 워커 | ✗ | ✗ (기존 유지) | 저동시성, `_vault_lock`이 가드 |
| files / tables | — | — | git 무관 (round-04 확인) |

## 5. round-01~04와의 관계

- **Phase 0 (try-lock 즉시 429) → 본 설계로 대체.** try-lock은 버스트를
  처벌하고 재시도 폭풍을 유발한다. "코루틴 대기 10초 후 429"가 상위 호환.
- **Phase 1 (per-document in-memory lane) → 연기.** vault 게이트가 동일한
  풀 보호를 주고, git이 동기 경로에 있는 한 세분화의 처리량 이득이 없다.
- **Phase 2 (PG-first git) / Phase 3 (coalescing·202) → 미결 유지.**
  §5의 PM 결정 1~4는 여전히 열려 있다. 본 설계는 Phase 2를 막지 않는다 —
  admission 게이트는 PG-first에서도 그대로 앞단이 된다.
- 100명(에이전트 동반) 규모에서는 본 설계 + #255로 충분하다는 것이
  이번 토의의 용량 판단. Phase 2 게이트는 "80 writes/s/문서가 진짜 제품
  케이스인가"(§5-4) 답이 나올 때 연다.

## 5.5 리뷰 보강 (codex round 1 → 시맨틱 3건 추가)

feedback/2026-07-02-codex-round-1.md에서 수용한 설계 수정:

1. **취소 흡수** — executor 스레드는 중단 불가이므로, 커밋 중 호출자
   취소 시 레인을 쥔 채 스레드 종료까지 대기 후 취소 전파. 조기 unwind는
   "레인은 비었는데 vault 락은 잡힌" 거짓 신호로 병리를 재유입시킨다.
2. **슬롯 규율(ContextVar)** — 커밋 executor에는 글로벌 슬롯 보유자만
   도달한다. 레인 admitted writer는 보유 슬롯으로 직행, 게이트를 안 타는
   vault 라이프사이클 작업은 `run_git_write`가 슬롯을 무데드라인 코루틴
   대기로 선획득(라이프사이클은 429 불가). executor 내부 큐 = 구조적 0.
3. **타임아웃 전면화** — worktree add/prune, clone 폴백 add/clone/push,
   `_stage_and_commit`의 rev_parse까지 `git_write_timeout_secs`로 통일
   (하드코딩 60s 제거).

codex round 2~7 (feedback/2026-07-02-codex-round-{2..7}.md)에서 추가 수용:

4. **`must_complete` 보상-쓰기 모드** (r2) — 슬롯 대기 중 취소도 흡수해
   반드시 완료 후 취소 전파. point-of-no-return 이후의 보상 작업
   (delete_vault 디스크 정리, create_vault 롤백 정리)이 클라이언트 단절로
   유실되지 않게.
5. **create_vault 롤백 완결화** (r2→r7에 걸쳐 수렴) — git 디렉토리 정리에
   더해 DB 보상. 최종 형태:
   - `_CREATE_LOCKS`(이름별)로 같은-이름 create 직렬화 → 디스크 소유권
     프로브 건전화 (r3)
   - `run_compensation` — bare shield의 detach 문제를 흡수 루프로 대체,
     보상이 실제 완료될 때까지 파킹 (r4)
   - **"롤백은 외부 데이터를 절대 파괴하지 않는다"**: 퍼지 트랜잭션 안에서
     vaults row `FOR UPDATE`(모든 FK writer의 FOR KEY SHARE와 충돌 →
     TOCTOU 원천 차단) + foreign-write 가드(seed 외 문서·파일·테이블·
     publications·todos·소유 경로 밖 collections) — foreign 발견 시 파괴적
     롤백 포기, vault 존치, 에러만 전파 (r5·r6·r7)
   - 퍼지 순서 DB→디스크, 모든 실패는 "존치" 방향으로만 퇴화
6. **delete_vault 취소 관통 차단** (r3) — cleanup 완료 후 재전파된 취소가
   RBAC 롤 정리를 건너뛰지 않도록 파킹 + `run_compensation`.
7. **롤백 가드의 보호 범위 원칙** (r8, reasoned rejection으로 확정; r9에서
   codex 인정) — 가드가 지키는 것은 *사용자가 vault에 맡긴 콘텐츠와 외부
   노출 산출물*(docs·files·tables·publications·todos·collections + 타 vault
   publication의 `query_vault_names` 이름 참조, r9). *vault 자신에 대한
   접근 메타데이터*(vault_access·RBAC 롤·소유권)는 vault와 운명을 같이한다
   — delete_vault 캐스케이드와 동일 시맨틱. grant 때문에 롤백을 거부하면
   half-created vault 잔존이라는 더 나쁜 결과가 된다.

**리뷰 종결**: codex 라운드 10 — **NO OBJECTIONS** (2026-07-03,
feedback/2026-07-02-codex-round-10.md에 전체 요약). 수용 15 · 기각(사유
인정) 1.

## 6. 검증 계획

1. 유닛: per-vault 직렬화(동시 2 → 순차), 타 vault 비차단, 글로벌 M 상한,
   T 초과 → `WriteBusyError`(429), 백스톱 즉시 거부.
2. 기존 스위트 회귀 (`pytest -k 'not _e2e'`).
3. (후속, 스테이징) 핫 vault burst E2E — round-04 검증 항목의 축소판:
   단일 vault 100 PUT 폭주 중 타 vault GET p95 무영향 + 풀/스레드 비소진.
