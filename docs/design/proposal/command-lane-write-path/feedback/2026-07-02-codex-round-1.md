# codex 리뷰 라운드 1 — write-lane admission (round-05 구현)

**도구**: codex-cli 0.132.0, read-only 샌드박스, 워킹트리 diff 전체 + round-05 문서
**결과**: BLOCKING 1 · MAJOR 2 → **전부 수용, 수정 완료** (아래 disposition)
**원문**: 세 findings 요약 후 각 조치 병기. VERDICT: OBJECTIONS → 라운드 2로.

## Finding 1 [BLOCKING] — 취소 시 레인 조기 해제, git 스레드는 계속 실행

`run_git_write` 대기 중 호출 태스크가 취소되면(클라이언트 타임아웃/단절)
executor 스레드는 중단 불가라 커밋이 계속 도는데, 코루틴은 즉시 unwind되어
레인·커넥션이 풀린다. 다음 admitted writer가 커넥션을 쥔 채 executor 안에서
`_vault_lock`에 블록 — 레인이 막으려던 병리가 취소 폭주(에이전트 클라이언트
타임아웃 스톰) 시 그대로 재유입.

**조치**: `_dispatch_to_pool`에서 `asyncio.shield` + 취소 흡수 루프.
취소가 와도 스레드 종료까지(git `kill_after_timeout`으로 상한) 레인을 쥔 채
코루틴으로 대기 후 취소를 전파. 결과는 폐기하고 INFO 로그(스트레이 커밋
가능성 명시 — 다음 쓰기의 reset --hard가 수습, 기존 to_thread 시절과 동일
시맨틱). 테스트: `test_cancel_during_commit_absorbs_until_thread_done`.

## Finding 2 [MAJOR] — 게이트 없는 라이프사이클 작업이 커밋 executor를 선점 가능

vault 생성(.vault.yaml/템플릿 가이드)·삭제(cleanup)는 의도적으로 레인을 안
타는데, 이들이 M개 executor 스레드를 다 차지하면 admitted writer(커넥션
보유)가 executor 큐에서 대기 — "희소 자원 보유 중 대기 금지" 불변식 위반.

**조치**: ContextVar `_slot_held` 기반 슬롯 규율. `write_lane` 내부 호출자는
이미 슬롯 보유 → 직행; 그 외(라이프사이클)는 `run_git_write`가 글로벌
세마포어를 **데드라인 없이**(429 불가 요건) 코루틴 대기로 선획득. 결과:
executor에는 슬롯 보유자만 도달 → 내부 큐 구조적으로 0. 이중 획득
데드락은 ContextVar로 원천 차단. 테스트:
`test_lifecycle_write_consumes_global_slot`,
`test_lane_holder_dispatch_does_not_double_acquire`.

## Finding 3 [MAJOR] — kill_after_timeout 커버리지 구멍

`_ensure_worktree`의 `worktree add`/`prune`, clone 폴백의 `add`, 하드코딩
60s인 clone/push가 미커버 — 첫 커밋/worktree 재생성 경로에서 git이 wedge되면
vault 락+슬롯+커밋 스레드+커넥션을 무기한 점유.

**조치**: 해당 명령 전부 `_write_kt()` 적용, clone/push의 하드코딩 60s를
`git_write_timeout_secs` 설정으로 통일.

## 검증

write-lane 유닛 18개 + 전체 스위트 451 passed, ruff clean.
