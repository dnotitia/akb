# codex 리뷰 라운드 2 — round-1 fix 검증 + 신규 발굴

**결과**: BLOCKING 1 · MAJOR 3 → **전부 수용, 수정 완료**. 라운드 1의 세 fix
자체는 유효 판정(관련 신규 이슈만 위 4건). VERDICT: OBJECTIONS → 라운드 3으로.

## Finding 1 [BLOCKING] — create_vault 롤백 비대칭 (DB row 잔존)

seed `put()`이 이제 레인을 타므로 포화 시 WriteBusyError로 실패할 수 있는데,
롤백은 git 디렉토리만 지우고 vaults row·RBAC 롤은 남긴다 → "repo 없는 vault"
가 영구 잔존, 같은 이름 재생성 불가. (409-류 실패로도 재현 가능한 기존
비대칭이었으나, 레인이 실패 확률을 실질화함.)

**조치**: `_rollback_vault_rows()` 신설 — fresh-vault 부분집합 캐스케이드
(edges → chunks → documents → collections → todos → vault_access → vaults)
+ `role_sync.on_vault_delete` best-effort. `created_vault_id` 바인딩 후 실패
시에만 실행. 기존 비대칭 버그도 함께 해소.

**파생 발견 (자체)**: 취소 흡수가 fn 예외를 폐기하므로, 기존 이름으로의
create가 init 중 취소되면 FileExistsError가 사라져 롤백이 **기존 vault의
디렉토리를 삭제**할 수 있는 경로 확인. `existed_before` 사전 프로브 +
"이 요청이 만든 디렉토리만 청소" 가드로 차단. 흡수 시 폐기되는 예외는
로그에 남기도록 `_dispatch_to_pool` 보강.

## Finding 2 [MAJOR] — delete_vault cleanup이 슬롯 대기 중 취소로 유실 가능

DB 캐스케이드 커밋 후 슬롯 대기라는 취소 가능 갭이 생김(라운드 1 fix의
부작용). 클라이언트 단절 시 고아 bare/worktree가 남아 같은 이름 재생성을
막는다.

**조치**: `run_git_write(..., must_complete=True)` 모드 신설 — 슬롯 **대기
중** 취소도 흡수해 보상 작업을 반드시 완료한 뒤 취소를 전파. delete_vault
cleanup과 create_vault 롤백 cleanup에 적용. 테스트:
`test_must_complete_survives_cancel_during_slot_wait`,
`test_default_lifecycle_cancel_during_slot_wait_aborts`.

## Finding 3 [MAJOR] — init_vault가 롤백 try 밖

init 중 흡수-취소되면 스레드는 디렉토리를 만들었는데 코루틴은 git_path를
못 받아 orphan → 같은 이름 create가 영구 실패. **조치**: init을 try 안으로
+ 위의 `existed_before`/`vault_exists` 이중 가드로 "우리가 만든 것만" 청소.

## Finding 4 [MAJOR] — `_stage_and_commit`의 rev_parse 2곳 타임아웃 누락

**조치**: 두 rev_parse에 `_write_kt()` 적용.

## 검증

write-lane 유닛 20개 + 전체 스위트 453 passed, ruff clean.
