# codex 리뷰 라운드 3 — round-2 fix 검증

**결과**: BLOCKING 2 → **전부 수용, 수정 완료**. 두 건 모두 라운드 2에서
내가 넣은 롤백 코드의 결함. VERDICT: OBJECTIONS → 라운드 4로.

## Finding 1 [BLOCKING] — `vault_exists` 소유권 프로브가 동시 같은-이름 create에 불건전

A·B가 같은 이름으로 동시 create → A의 init이 승리, B의 init은
FileExistsError(git_path=None) → B의 롤백이 "디렉토리가 내 시도 중 생겼다"
고 판단해 **A의 repo를 삭제**. 데이터 손실 레이스.

**조치**: `_CREATE_LOCKS` — 이름별 asyncio.Lock으로 create 표준 경로 전체
(롤백 포함)를 직렬화(`_create_vault_standard`로 추출). 락 안에서는 "전에
없었고 이 구간에서 생긴 디렉토리 = 내 것"이 참이 되어 프로브가 건전해진다.
write-lane vault 게이트와 의도적으로 별도 락 — seed put()이 게이트를 타므로
같은 락이면 자기-데드락.

## Finding 2 [BLOCKING] — must_complete의 사후 CancelledError가 DB 롤백을 관통

롤백 중 cleanup(must_complete)이 완료 후 재전파한 CancelledError는
`except Exception`에 안 잡혀 `_rollback_vault_rows`를 건너뜀 → repo는
지워졌는데 vaults row·롤 잔존.

**조치**: 롤백 핸들러에 `pending_cancel` 파킹 패턴 — cleanup의 취소를
붙잡고, DB 퍼지는 `asyncio.shield`로 중도 취소에도 완주시킨 뒤, 모든 보상
완료 후 취소를 재전파. **동류 버그 선제 수정**: delete_vault에서도 cleanup
사후 취소가 `role_sync.on_vault_delete`를 건너뛰는 같은 관통 경로를 동일
패턴(파킹 + shield)으로 차단.

## 검증

전체 스위트 453 passed, ruff clean, 변경 4개 파일 mypy clean.
