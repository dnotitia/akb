# codex 리뷰 라운드 6 — round-5 fix 검증

**결과**: BLOCKING 1 → **수용, 수정 완료**. VERDICT: OBJECTIONS → 라운드 7로.

## Finding 1 [BLOCKING] — 롤백 가드의 TOCTOU (레인을 안 타는 writer)

write-lane 게이트는 문서 writer만 배제한다. `akb_create_table`
(table_service, 레인 미경유)이나 파일 업로드(vault_files)가 foreign 검사
**직후**에 커밋되면, `DELETE FROM vaults`의 `ON DELETE CASCADE`가 방금
ack된 vault_tables row를 삼키고 물리 `vt_*` 테이블은 고아가 된다.

**조치 — DB 레벨 원자화**: 검사를 퍼지 트랜잭션 **안**으로 이동하고,
트랜잭션 첫 문장으로 `SELECT id FROM vaults WHERE id=$1 FOR UPDATE`.
근거: 모든 FK writer(documents·vault_tables·vault_files INSERT)는 부모
vaults row에 `FOR KEY SHARE`를 트랜잭션 종료까지 쥐는데, 이는 `FOR UPDATE`
와 충돌한다. 따라서 —

- in-flight foreign 쓰기는 우리 락 승인 **전에** 커밋 → 트랜잭션 내 재검사가
  이를 보고 퍼지를 abort (vault 존치)
- 이후 foreign 쓰기는 우리 트랜잭션 종료까지 블록 → row 삭제 후 FK 위반으로
  **깨끗하게 실패** (PG의 트랜잭셔널 DDL로 물리 테이블 생성도 함께 롤백)
- 캐스케이드가 ack된 foreign row를 삼키는 시나리오는 구조적으로 불가능

`_rollback_vault_rows`는 bool(purged)을 반환하고, 디스크 정리는 퍼지가
실제 수행된 경우에만 뒤따른다. 퍼지 실패 시에도 존치 방향(rows+repo 모두
유지 = 기능하는 vault)으로만 퇴화. write-lane 게이트는 rmtree 전 in-flight
문서 커밋 드레인 용도로 유지.

## 검증

전체 스위트 456 passed, ruff clean, 변경 파일 mypy clean.
