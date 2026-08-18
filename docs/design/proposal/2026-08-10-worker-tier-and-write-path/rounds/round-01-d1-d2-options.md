# Round 01 — D1/D2 선택지와 기각 근거

세 아키텍트(최소변경 / 정확성우선 / 운영성우선)가 독립 설계, 심사역 2명이 5축 채점,
적대적 비평가가 셋 다 놓친 것 사냥. 채점 결과: **운영성 45 > 최소변경 40~43 > 정확성 37.**
세 안이 D1·D2 헤드라인 답에는 수렴했고, 승부는 차별화된 품질에서 갈렸다.

## D1 — 워커 크래시 복구

| 선택지 | 판정 | 근거 |
|---|---|---|
| (a) heartbeat/프로세스 레지스트리 + reaper | **기각** | `replicas:1 + Recreate`라 살아있는 피어 없음 → 리스로 퇴화. 과거 P0가 Kiwi GIL 루프 정지라 같은 루프의 heartbeat는 사고 때 마감을 놓침. `backend/app/`에 프로세스 신원(gethostname/instance_id) 자체가 없음 |
| **(b) 시간 리스 + rescuer, attempt를 클레임 안에서 증가** | **채택 (만장일치)** | Oban/River 방식. SIGKILL/OOM이 attempt를 소비 → 포기 술어 도달 가능. 7 큐가 이미 같은 클레임 템플릿 공유 → in-place 수정 |
| (c) 세션 advisory lock | **기각** | `document_service.py:386-395`에 이 패턴의 프로덕션 풀 데드락 기록. `embed_worker` 16행 / `events_publisher` 64행 클레임 vs `pg_pool_max_size` 30 |

**rescuer 위치**: 부팅 시 일회성이 아니라(안전하지만 심사역이 재검토) **advisory lock 보호
워커 틱**. 부팅 rescuer는 N-레플리카 API 티어에서 돌면 살아있는 피어의 클레임을 훔치므로
싱글턴 role 플래그로 fail-closed 게이트 필수.

**minimal이 제안한 `terminal_policy` 파라미터(terminal/persistent)**: Judge A는 채택,
Judge B는 speculative generality로 기각. 종합은 **B를 따름** — never-abandon 아카이버는
D2(b) 산물이고 D2(b)는 두 PM 결정 뒤에 게이트됨.

## D2 — dual-write

| 선택지 | 판정 |
|---|---|
| (a) git-inside-transaction 유지 + CAS + drift 프로브 | 불충분 — CAS는 ref만 지킴(§4.2), 잔재원(§4.1)을 안 닫음 |
| (b) PG-first + 트랜잭셔널 아웃박스, git = 파생 재생 가능 아티팩트 | 최종 목적지 후보. 단 `documents`에 정본 본문 없음 + 6개 읽기 지점 이전 필요 |
| **(c) 하이브리드** | **채택 — 이음매 위치·순서에서 Codex vs 패널 갈림** |

두 판단이 갈리는 지점(순서: PG-first 먼저 vs gitd 먼저)이 곧 PM 결정. README §3·§8 참조.

## 적대적 비평 — 셋 다 놓친 것 (전부 CONFIRMED)

1. **attempt-at-claim without success-reset** — `_mark_success`가 카운터를 리셋 안 함 → 평생 클레임 카운터화. DR 재큐 경로가 위험.
2. **batch-claim burns attempts for rows never attempted** — 1:16/1:64 클레임에서 미시도 잔여분이 attempt를 소비.
3. **terminal sweep is unbounded under 30s timeout** — 새 술어에 인덱스 없으면 full-scan.
4. **seam and detector cover only the document path** — `.vault.yaml`, `_guide.md`(DB 행 없음), `collection_service`의 PG-커밋-후 git 삭제(반대 순서).
5. **CAS protects the ref not the shared worktree index** — §4.2, gitd 하드 게이트.
6. **CAS in `_commit_via_clone` targets a temp clone** — 그 ref는 아무것도 단언 안 함. 진짜 직렬화 지점은 `push origin`.
7. **orphan commit is a designed path** — §4.1.
8. **drift blast radius is the public page and it is cached** — §4.3.
9. **vector-store call holds a chunks row lock inside the user write path** — qdrant 드라이버는 비트랜잭셔널 HTTP 왕복인데 `FOR UPDATE` + 트랜잭션 안. → P5에 흡수.
