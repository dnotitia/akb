---
status: proposal
stage: proposal
created: 2026-07-28
updated: 2026-07-28
head: b3abdf0
method: production measurement (PG + Redis, read-only) + PM decision on purpose/retention + independent Codex adversarial review (verdict REDESIGN; wave A applied, wave B deferred)
---

# MCP 툴 사용량 추적 — 디스패치 초크포인트 → PG 싱크

## 0. 한 장 요약

> AKB는 **어떤 MCP 툴이 실제로 쓰이는지 답할 수 없다.** 관측 지점은 이미 있지만
> (`record_tool`, 디스패치 초크포인트), 그 싱크가 조회 불가능한 해시체인 파일이고
> 프로덕션에서 꺼져 있다. 나머지 두 스트림(`events`, `audit`)은 목적이 달라
> 대체할 수 없다. **같은 초크포인트에 PG 싱크를 하나 추가한다.**

`akb_todo`가 1년 넘게 유령으로 남아 있었던 것도, 지금 어떤 툴이 죽었는지 물었을 때
답할 데이터가 없는 것도 같은 공백에서 나온다.

## 1. 문제

### 1-1. 지금 답할 수 없는 질문

- 어떤 툴이 실제로 호출되는가 (어떤 툴이 죽었는가)
- 어떤 툴이 자주 실패하는가, 어떤 에러 코드로
- 누가 언제 무엇을 호출했는가 (장애 조사)
- 에이전트가 툴을 어떤 순서로 쓰는가 (행동 패턴)

### 1-2. 기존 두 스트림이 답하지 못하는 이유

| | `events` | `audit` |
|---|---|---|
| 기록 대상 | 도메인 동사 `document.put` | API 표면 호출 `akb_put` |
| 시점 | **성공만** | 성공 + 실패 |
| 읽기 포함 | **아니오** | 예 (`log_reads` 켜면) |
| 저장소 | PG 테이블 | 해시체인 JSONL → S3 |
| 목적 | Redis 팬아웃 = **배달** | 변조 탐지 = **증거** |
| 수명 | **배달되면 삭제** (7일치) | 보존 (WORM) |
| 프로덕션 상태 | 켜짐 | **꺼짐** (`enabled=False` 기본값) |

**`events`가 부적합한 이유 (실측):**

`emit_event`는 초크포인트가 아니라 서비스마다 손으로 부르는 **38개 호출**이고,
kind 30종에 다음이 아예 없다:

| 없는 kind | 표현 못 하는 툴 |
|---|---|
| `publication.*` | `akb_publish` `akb_unpublish` `akb_publications` `akb_publication_snapshot` |
| `edge.*` / `link.*` | `akb_link` `akb_unlink` |
| `vault.create/delete/archive` | `akb_create_vault` `akb_delete_vault` `akb_archive_vault` |
| export / import | `akb_export` `akb_import` |
| sql | `akb_sql` |

읽기·조회·메타 17종을 더하면 **43개 툴 중 최소 29개가 `events`에 나타날 수 없다.**
게다가 배달되면 삭제되므로 추세 분석이 원리적으로 불가능하다.

**`audit`이 부적합한 이유:**

관측 지점은 정확하지만 싱크가 원장(append-only + 해시 체인)이라 `GROUP BY`가 안 된다.
그리고 `log_reads=false`로 끄면 읽기 툴이 통째로 사라진다 — `akb_grep(replace=)`가
감사에서 누락됐던 것과 **동일한 함정**이다 (PR #313에서 수정한 그 결함).

`record_tool` 독스트링이 이미 `events`↔`audit`을 "different altitudes … do not try to
unify the two"로 분리해놨다. 이 설계는 그 선을 한 칸 더 적용하는 것이다.

## 2. 결정

**PM 결정 (2026-07-28):**

| 항목 | 결정 |
|---|---|
| 용도 | 제품 분석 + 운영 디버깅 + 에이전트 행동 분석 (쿼터·과금 제외) |
| 저장 단위 | **호출 1건 = 1행** (행동 분석에 순서가 필요하므로 집계 버킷 탈락) |
| 보존 | **C안** — 원본 30일 + 일일 롤업 영구 |
| 기본 활성화 | **off** (설정 플래그) |

`B`(원본만)로 만들면 장기 추세를 잃고, `A`(집계만)로 만들면 순서·행위자를 영영
못 만든다. `C`는 원본에서 롤업이 파생되므로 상위집합이고 저장은 유한하다.

## 3. 설계

### 3-1. 관측 지점 — 기존 초크포인트 공유

```
call_tool(name, arguments)                      server.py:1424
  ├─ user = await _get_user()                   :1428
  ├─ t0 = perf_counter()
  ├─ result = await _dispatch(...)              :1430
  ├─ [성공] audit_log.record_tool(...)          :1435   (기존, 무변경)
  │         tool_usage.record(...)              (신규)
  └─ [예외] audit_log.record_tool(...)          :1464   (기존, 무변경)
            tool_usage.record(...)              (신규)
```

**`audit_log`는 손대지 않는다.** 두 싱크는 독립적으로 켜고 끈다.

### 3-2. 비블로킹 제약 (강제 사항)

`server.py:1431` 주석이 계약을 명시한다:

> "No disk I/O or lock on the event loop, and it never touches the shared to_thread
> pool — a stalled audit disk can't freeze the loop or starve bcrypt / document reads."

이 코드베이스는 이벤트루프 블로킹이 고질적 장애 원인(503)이므로 **동기 PG INSERT는
금지**다. 따라서:

- `record()`는 `collections.deque(maxlen=N)`에 **append만** 한다 — O(1), 락 없음
- `BackfillRunner`가 배치로 드레인해 한 번의 `executemany`로 INSERT
- 큐가 가득 차면 **가장 오래된 것부터 버리고 드롭 수를 로그**한다
  (조용한 유실 금지 — 이 코드베이스의 반복 결함)

### 3-3. 스키마

```sql
-- 원본: 호출 1건 = 1행, 30일 보존
CREATE TABLE IF NOT EXISTS tool_calls (
    id           BIGSERIAL PRIMARY KEY,       -- 삽입 순서 = 행동 분석의 시퀀스
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tool         TEXT        NOT NULL,        -- akb_put, akb_search, …
    actor_id     TEXT,                        -- events와 동일하게 TEXT (익명 호출 대비)
    actor        TEXT,                        -- username 스냅샷
    session_id   TEXT,                        -- MCP 세션 — 호출 순서·패턴용
    vault        TEXT,                        -- args에서 (이름 그대로, 조회 없음)
    outcome      TEXT        NOT NULL,        -- 'ok' | 'error'
    code         TEXT,                        -- 실패 시 에러 코드
    duration_ms  INTEGER,                     -- 디스패치 소요
    is_write     BOOLEAN     NOT NULL DEFAULT FALSE,
    rolled_at    TIMESTAMPTZ                  -- NULL = 아직 집계 안 됨 (클레임 표시)
);

-- append-only 시계열 → BRIN이 btree보다 수백 배 작고 purge/범위질의에 충분
CREATE INDEX IF NOT EXISTS idx_tool_calls_occurred_brin
    ON tool_calls USING BRIN (occurred_at);
-- 행동 분석: 한 세션의 호출을 순서대로
CREATE INDEX IF NOT EXISTS idx_tool_calls_session
    ON tool_calls (session_id, id) WHERE session_id IS NOT NULL;
-- 클레임 스캔은 미집계 행만 보므로 부분 인덱스면 백로그 크기에 비례한다
CREATE INDEX IF NOT EXISTS idx_tool_calls_unrolled
    ON tool_calls (id) WHERE rolled_at IS NULL;

-- 롤업: 영구 보존
CREATE TABLE IF NOT EXISTS tool_usage_daily (
    day               DATE   NOT NULL,
    tool              TEXT   NOT NULL,
    outcome           TEXT   NOT NULL,
    calls             BIGINT NOT NULL,
    total_duration_ms BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, tool, outcome)
);
```

**저장하지 않는 것:** 원시 `args`. 문서 본문·검색어·SQL이 그대로 들어가므로
프라이버시 위험이 크다. `audit_log`가 정규 URI 대신 "honest, lossy `target`"을
택한 것과 같은 판단이다. `vault`만 뽑는다.

**행 수 추정:** `events`(쓰기 성공만)가 일 ~9,082건. 읽기 포함이면 일 수만 건 규모.
30일 보존이면 원본 최대 수백만 행 — BRIN + 시간범위 DELETE로 관리 가능.
롤업은 43툴 × 2 outcome ≈ 일 86행 = **연 3만 행**.

### 3-4. 워커

`BackfillRunner` 2개 (기존 패턴, 이미 10곳에서 사용 중):

| 이름 | 주기 | 하는 일 |
|---|---|---|
| `tool_usage_flusher` | 짧게 (~5s) | deque 드레인 → 배치 INSERT |
| `tool_usage_rollup` | 길게 (~1h) | 어제치 집계 UPSERT → 보존기간 지난 원본 DELETE |

롤업은 **행 단위 클레임**이다. 상태 테이블은 없고, 클레임 표시가 행 자신에 있다
(`tool_calls.rolled_at`). 집계하는 그 문장이 같은 행을 stamp한다:

```sql
WITH claimed AS (
    UPDATE tool_calls SET rolled_at = NOW()
     WHERE id IN (SELECT id FROM tool_calls WHERE rolled_at IS NULL
                   ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED)
    RETURNING (occurred_at AT TIME ZONE 'UTC')::date AS day, tool, outcome, duration_ms
), agg AS (SELECT day, tool, outcome, COUNT(*), SUM(duration_ms) FROM claimed GROUP BY 1,2,3),
   ins AS (INSERT INTO tool_usage_daily ... SELECT ... FROM agg
           ON CONFLICT DO UPDATE SET calls = tool_usage_daily.calls + EXCLUDED.calls ...)
SELECT COUNT(*) FROM claimed
```

**한 문장·원자적이라 인서터가 몇 개든 정확히 1회**다. 집계와 클레임이 함께
커밋되거나 함께 롤백된다.

> **왜 `MAX(id)` 워터마크를 버렸나** — 초안은 단일 행 `last_rolled_id`를 썼다.
> PostgreSQL은 시퀀스를 **커밋 전에** 할당하므로, 아직 열려 있는 낮은 id를 워터마크가
> 지나칠 수 있다. 그 행은 영원히 워터마크 아래에 남아 **집계되지 않고**, purge 조건
> (`id <= watermark`)은 그것을 삭제한다 — 조용한 영구 과소집계. 인서터가 1개인
> 현 토폴로지에서는 안 터지지만 **그 토폴로지를 강제하는 장치가 없다.** 우연히
> 맞는 설계를 문서로는 안전하다고 적는 것이 이 저장소가 반복해온 실패다.

`SKIP LOCKED`로 두 번째 러너는 블록되지 않고 다른 슬라이스를 가져간다. `LIMIT`이
한 문장이 옮기는 양을 묶어, 장애 후 밀린 백로그를 statement timeout 크기의 한 방이
아니라 여러 청크로 따라잡는다(비어 있지 않은 롤업은 러너를 즉시 재실행시킨다).

purge는 나이(`occurred_at < 자정 경계`)와 **클레임 표시**(`rolled_at IS NOT NULL`)
양쪽에 걸린다 — 오래됐지만 아직 집계 안 된 행은 살아남는다.

> **`events`의 함정을 반복하지 않는다**: `events`는 purge가 publisher 안에 있어
> `redis_url`을 비우면 발행과 purge가 **동시에** 멈추고 테이블이 무한 증가한다.
> 여기서는 롤업/purge를 flusher와 분리된 러너에 두고, `enabled=false`여도
> 기존 데이터의 purge는 계속 돌게 한다.

#### 이 설계에 도달하기까지 고친 결함 6건

**단위 테스트는 그때마다 전부 통과 중이었다.** 1·2는 실제 Postgres가, 3~6은 Codex
적대적 리뷰(1차 REDESIGN → 2차 STILL REDESIGN)가 잡았다.

| # | 결함 | 결과 | 수정 |
|---|---|---|---|
| 1 | 롤업 창이 purge 기준과 같은 폭 | 창 밖 행은 **집계도 삭제도 안 됨** → 영구 누적 | 시간 창 제거 |
| 2 | purge 기준이 임의 시각 | 하루가 **부분 삭제**되고 재계산이 조각으로 덮어씀 | 기준을 **UTC 자정으로 절단** |
| 3 | 1의 수정이 **매시간 보존창 전체 집계** | 90만~300만 행 시간당 스캔 → I/O 포화 → probe 초과 → **503** | 증분 롤업 |
| 4 | `MAX(id)` 워터마크가 **커밋 순서를 보장 못 함** | 열려 있는 낮은 id를 지나쳐 **집계 없이 purge** → 조용한 영구 과소집계 | **행 단위 클레임** (`rolled_at`) |
| 5 | 성공 기록 후 `json.dumps` 실패 → **1콜 2행** | 카운트 부풀림, 모순되는 감사 기록 | `recorded` 가드로 두 번째 기록 억제 |
| 6 | `stop()`이 러너 정지(최대 120초) **후에** 드레인 | k8s 유예 30초·all-in-one 15초 → **드레인 전 SIGKILL** | `stop(timeout=…)` 신설 + `shutdown_deadline_secs`로 단계별 상한 |

추가로 4차 Codex 리뷰와 xhigh 코드리뷰 워크플로(53 에이전트)가 6건을 더 잡았다.
**두 리뷰는 서로 다른 각도를 봤다** — 코덱스는 "수정이 새 결함을 만들었나", 워크플로는
"적대적 입력이 오면" 이었고, 겹친 것은 종료 경쟁 2건뿐이다.

| # | 결함 | 결과 | 수정 |
|---|---|---|---|
| 7 | `tool`·`vault`에 길이 상한 없음 | `tool`이 `tool_usage_daily` btree PK에 들어가 ~2704B 초과 시 롤업이 **영구 정지** → purge도 멈춰 무한 증가 | `_clip()`으로 256자 절단 (형제 `audit_log._TARGET_MAX` 선례) |
| 8 | NUL 미제거 + 실패 배치를 무한히 head로 재삽입 | `{"vault":"\x00"}` 한 번이면 flush가 **영원히 같은 배치에서 실패** | NUL 제거 + 연속 실패 3회 후 배치 폐기·카운트 |
| 9 | `CancelledError`가 `except Exception` 우회 | 마감시한이 INSERT 중 발동하면 드레인된 배치가 **아무데도 없이 소멸**, 카운트도 안 됨 | 명시적 `except asyncio.CancelledError` → requeue 후 재전파 |
| 10 | `asyncio.shield`가 `stop(timeout)`을 무력화 | 취소된 건 래퍼뿐이라 shield된 flush가 **detached로 계속 실행** → "유일 청구자" 전제 거짓 | `BackfillRunner`가 in-flight를 붙잡고 `stop()`이 그것까지 대기 |
| 11 | 예산 만료 시 `_maintainer.stop()` 통째로 건너뜀 | shield된 롤업이 `close_pool()` 이후까지 생존 | 단계별 개별 상한 — 세 단계 모두 **반드시 도달** |
| 12 | `maintenance_once` 실패 결합 | purge 실패가 롤업 성공 카운트를 삼켜 러너가 **1시간 잠듦** → 처리량 시간당 1배치 | 양 다리 독립 실행·독립 집계 |

그 밖에: `vault_of`가 `parse_uri`에 위임(prefix 정규식은 `akb://eng/not-a-resource`를
받아들여 정규 문법과 어긋났다), `_vault_of` 우선순위를 핸들러와 일치시킴(URI 우선),
`akb_sql`의 `vaults[]` 배열 인식, `/health`에 큐 깊이·유실 노출, `record()` 내부 실패도
카운트.

5차 리뷰는 **4차의 수정이 만든 결함**을 잡았다. 이 패턴이 다섯 번째다.

| # | 결함 | 결과 | 수정 |
|---|---|---|---|
| 13 | 독약 배치를 **연속 실패 3회**로 판정 | 5초 주기라 **정상 PG 재시작 10~15초**가 기준을 넘겨 배치를 폐기 — "일시 장애는 지연시키지 유실시키지 않는다"는 계약 정면 위반. 게다가 카운터가 전역이라 무관한 배치가 남의 실패를 물려받아 첫 오류에 폐기됨 | 횟수가 아니라 **종류로 판정** — `DataError`(class 22)만 폐기, 나머지는 무한 재큐 |
| 14 | lone surrogate 미처리 | Python str에는 살지만 UTF-8 인코딩 불가 → NUL과 똑같이 배치를 막음 | `_clip()`이 UTF-8 왕복으로 치환 |
| 15 | 절단이 값을 병합 | `tool`이 PK라 접두사가 같은 두 값이 **한 집계 행으로 합쳐져** 무관한 트래픽을 합산 | 절단 시 sha256 8자 접미 |
| 16 | URI 파싱이 클립 **이전**에 실행 | 1MB 인자 4개 = **이벤트루프 45ms** — 이 서비스의 장애 계열 그 자체 | 길이 초과 인자는 파싱 없이 기각 |
| 17 | 볼트 귀속이 단일 규칙 | `akb_sql`은 `vaults[]`가 우선(내 코드는 반대), `akb_publish(table_query)`는 `uri`를 무시(내 코드는 URI 우선) | **도구별 규칙**. 다중 볼트 SQL은 첫 원소가 아니라 NULL |
| 18 | `stop(timeout)`이 단계마다 전액 소비, in-flight 회수가 타임아웃 경로에만 | 15초 유예에 맞춘 호출자가 그 이상 차단. 외부에서 취소된 래퍼는 정상 경로를 타서 **shield된 작업이 살아있는 채로 반환** | **절대 마감시한** 하나 + 모든 경로에서 회수 |

그 밖에 라이브 실행이 하나 더 잡았다 — `stop()`이 첫 줄에서 raise하면
`stop_workers()` 전체가 중단되어 **뒤따르는 워커 9종이 정지되지 않았다**. 어떤
리뷰도 잡지 못했고 실제 종료 경로를 태워서 발견했다.

**게이트 자체도 고쳤다.** e2e가 명시적 `AKB_TEST_DSN`이 불통일 때 skip하면 8건 skip +
잡 통과가 되어 게이트가 아니게 된다 → 이제 **실패**한다. 그리고 동시성 테스트가
직렬 실행으로도 통과했다(200행 < 배치) → 첫 슬라이스를 명시 트랜잭션으로 **잠근 채**
다른 연결의 롤업이 다음 슬라이스를 가져오는지로 교체. `SKIP LOCKED`를 빼면 5초
타임아웃으로 실패함을 확인했다.

3·4가 특히 교훈적이다 — 1을 고치려고 창을 없앤 것이 3을 만들었고, 3을 고치려고 쓴
워터마크가 4를 만들었다. 매번 실제 DB 검증도 통과했다(인서터가 1개라 4가 안 터짐).
**클레임 설계는 순서 가정 자체가 없어 이 계열이 끝난다.**

5는 처음에 "직렬화를 먼저" 로 고쳤다가 되돌렸다 — 그러면 **커밋에 성공한 변경이
직렬화 실패 시 `error`로만 기록되어**, 이중기록을 오귀속으로 바꾸는 것이었다.
지금은 dispatch 결과를 기록하고 두 번째 기록만 막는다.

#### 종료 시 드레인

큐는 메모리에만 있고 `terminationGracePeriodSeconds`가 미설정(k8s 기본 30초)이라,
`stop()`이 러너 정지 후 남은 큐를 마저 flush하지 않으면 **롤아웃마다 꼬리가
유실**된다. flush 실패 시에는 배치를 큐 앞으로 되돌려 일시적 DB 장애가 데이터
삭제가 되지 않게 한다.

### 3-5. 설정

```python
class ToolUsageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False               # PM 결정
    raw_retention_days: int = 30
    queue_max: int = 10_000             # 초과 시 오래된 것부터 드롭 + 로그
    flush_interval_secs: int = 5
    flush_batch: int = 500
```

`Settings`에 `tool_usage: ToolUsageSettings`로 중첩 (`audit`와 동일 패턴).

### 3-6. 세션 ID 확보

`_get_user()`가 이미 `server.request_context.request`(Starlette Request)를 잡으므로
같은 자리에서 `request.headers.get("mcp-session-id")`를 읽는다. 첫 `initialize`
호출에는 헤더가 없으므로 NULL 허용.

> **알려진 한계**: `uvicorn --workers > 1`이 되면 `_transports`가 프로세스 로컬이라
> 세션 상관관계가 무너진다. 그리고 실제 동작은 "세션이 여러 개로 갈린다"보다 나쁘다 —
> POST 미스 시 `http_app.py`가 새 transport id를 만들고, SDK가 들어온 옛 id와 비교해
> **404를 반환**한다. sticky 라우팅이 없으면 세션이 반복적으로 실패·재연결한다.
> `session_id` 차원은 그 환경에서 신뢰할 수 없다.
>
> **단 적재 자체는 안전하다** — 롤업이 행 단위 클레임이라 인서터/러너 개수에 대한
> 가정이 없다. (초안의 `MAX(id)` 워터마크는 이 주장이 **거짓**이었다. 위 결함 #4 참조.)

## 4. 대안과 기각 사유

| 안 | 기각 사유 |
|---|---|
| `events`에 툴 호출 추가 | 초크포인트 아님(29/43 표현 불가), 배달되면 삭제, 손으로 29곳 추가는 유령툴과 같은 실패 유발 |
| `audit`에 PG 싱크 추가 | 목적 충돌 — `log_reads=false`가 사용량을 조용히 삭제 (PR #313에서 고친 그 함정) |
| 인메모리 카운터만 | 행동 분석(순서) 불가. 워커 2개에서 flush가 last-write-wins로 덮어씀 |
| 요청 경로에서 동기 INSERT | 이벤트루프 블로킹 — `server.py:1431`이 명시적으로 금지 |
| Prometheus 카운터 | 카디널리티 폭발(actor×tool×vault), 그리고 "누가 언제"를 못 답함 |

## 5. 테스트 계획 (TDD)

| # | RED | 검증 대상 |
|---|---|---|
| 1 | 초크포인트가 성공·실패 양 경로에서 `tool_usage.record`를 부른다 (AST 가드) | 커버리지 회귀 방지 |
| 2 | `record()`는 이벤트루프에서 I/O를 하지 않는다 (큐 길이만 증가) | 비블로킹 계약 |
| 3 | 큐 초과 시 오래된 것부터 드롭 + 드롭 수 카운트 노출 | **조용한 유실 금지** |
| 4 | flusher가 배치를 INSERT하고 큐를 비운다 | 기본 동작 |
| 5 | `enabled=false`면 큐에 아무것도 안 쌓인다 | 플래그 |
| 6 | 실패 호출이 `outcome='error'` + `code`로 기록된다 | 에러 경로 |
| 7 | 롤업이 멱등 — 두 번 돌려도 `calls`가 두 배 안 됨 | 재실행 안전성 |
| 8 | purge가 롤업 워터마크를 넘지 않는다 | 데이터 유실 방지 |
| 9 | `record()`는 절대 raise하지 않는다 | 추적이 서비스를 죽이지 않음 |

## 6. 측정 경계 — 무엇이 안 잡히는가 (확인됨)

이 싱크의 지표는 **"백엔드 MCP 툴 실행"** 이지 "제품 사용량" 전체가 아니다. Codex
리뷰로 확인된 사각지대를 숨기지 않고 명시한다. 전부 wave B 대상이다.

| 사각지대 | 근거 | 영향 |
|---|---|---|
| **인자 스키마 검증 실패** | MCP SDK가 `jsonschema.validate` 후 `_make_error_result`로 반환 — `call_tool`에 **도달하지 않음** | 잘못된 인자로 실패한 호출이 0으로 보임. "이 툴 실패율" 과소 추정 |
| **프록시 전용 파일 툴 3종** | `akb_put_file`/`akb_get_file`/`akb_delete_file`은 `proxy.mjs`가 REST/S3로 직접 처리 | 에이전트가 보는 표면은 43이 아니라 **46**. 이 3개는 "죽었다"고 오판될 수 있음 |
| **`_get_user()` 예외** | 초크포인트의 `try` **밖**에 있음 | 인증 해석 중 DB 오류면 두 싱크 모두 우회 |
| **HTTP 인증 거부** | `http_app.py:94-123`에서 MCP 전송 계층 도달 전 반려 | 거부된 호출은 이 지표에 없음 |
| **`CancelledError`** | `except Exception`이 안 잡음 | 클라이언트 연결 끊김/세션 취소가 두 싱크 모두 우회 |
| **전송 실패** | 기록이 응답 전달 **전**에 일어남 | 클라이언트가 못 받은 응답도 `ok`로 남을 수 있음 |
| **직접 Python stdio 진입점** | `tool_usage` 워커를 기동하지 않음 (`server.py` stdio 경로) | 그 경로는 큐만 쌓이고 적재 안 됨. 지원 경로인 Node 프록시→HTTP는 정상 |
| **큐 오버플로 / SIGKILL** | 유실은 카운트되지만 표본에서 빠짐 | 모집단이 달라지므로 해석 시 감안 |

REST 표면도 잡히지 않는다 — 지표 정의상 의도된 것이며, 위 표와 같은 이유로
"AKB 제품 사용량"으로 읽으면 안 된다.

## 7. 범위

**wave A (이번):** 마이그레이션 046(`tool_calls` + `tool_usage_daily` 2 테이블),
`app/services/tool_usage.py`, 초크포인트 배선, 설정 섹션(하한 포함), 워커 2개,
`BackfillRunner.log_progress` / `stop(timeout=…)`, `uri_service.vault_of()`,
유닛 29개 + **실제 PG e2e 8개(CI `pgvector-e2e` 잡에 등록)**.

**wave B (다음, Codex 재설계 항목):** 위 사각지대 3건 계측, 안정적 `event_id` +
호출 진입 시점 시퀀스 + `started_at`/`completed_at`, 롤업에 `code`/actor/vault
차원 추가, `is_write` 개명(실제로는 "쓰기 스코프 필요"), UNIQUE 제약으로 재시도
멱등화, `/health`에 큐 깊이·드롭·마지막 flush/rollup 노출, 실제 PG 통합 테스트
(늦은 행·백필·커밋 모호성·날짜 경계·재시작), 종료 유예 정책.

**미포함:** 조회 API/UI, 대시보드, 쿼터 강제.

## 7. 열린 질문

1. 롤업에 `vault` 차원을 넣을 것인가? (행 수 × 볼트 수 = 40배) — 현재는 제외
2. `duration_ms`를 롤업에 p95로 넣을 것인가? — 현재는 `total_duration_ms`만 (평균 도출 가능)
3. 프로덕션 활성화 시점 — 기본 off이므로 `internal/backend-config-patch.yaml`에
   명시적으로 켜야 한다. 켜지 않으면 이 작업은 코드만 있고 데이터는 0이 된다.
