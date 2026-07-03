# codex 리뷰 라운드 4 — round-3 fix 검증

**결과**: BLOCKING 1 · MAJOR 1 → **전부 수용, 수정 완료**.
codex가 shield의 detach 동작을 실행 실험으로 증명함. VERDICT: OBJECTIONS →
라운드 5로.

## Finding 1 [BLOCKING] — bare `asyncio.shield`는 취소 후 완주를 안 기다림

shield는 취소 시 즉시 raise하고 내부 코루틴을 **detached task**로 계속
돌린다. 셧다운 취소가 퍼지 중에 오면 create 락이 풀리고 풀이 먼저 닫혀
퍼지가 반쯤 된 채 증발 — repo는 지워졌는데 vaults row/롤 잔존.

**조치**: `write_lane.run_compensation(coro)` 신설 — shield 재시도 루프로
내부 태스크가 **실제 완료될 때까지** 호출자를 파킹(락·자원 유지)하고,
그 후 취소 재전파. 흡수 중 내부 작업이 실패하면 취소 우선 + 에러 로그
(`_dispatch_to_pool`의 폐기 규칙과 동일). create 롤백 퍼지와 delete_vault의
role 정리 두 곳에 적용. 테스트:
`test_run_compensation_absorbs_cancel_until_done` 외 2개.

## Finding 2 [MAJOR] — `_rollback_vault_rows`가 events 미퍼지

seed put()이 `document.put` 이벤트를 커밋하는데 events엔 vault FK 캐스케이드
가 없어, 롤백 후에도 컨슈머가 지워진 vault/doc의 이벤트를 수신.

**조치**: 퍼지 캐스케이드 맨 앞에 `DELETE FROM events WHERE vault_id=$1`
추가. (delete_vault 본경로는 events를 감사 목적상 보존하지만, 롤백은
"공식적으로 존재한 적 없는" vault이므로 퍼지가 옳다.)

## 검증

write-lane 유닛 23개 + 전체 스위트 456 passed, ruff clean, 변경 파일 mypy clean.
