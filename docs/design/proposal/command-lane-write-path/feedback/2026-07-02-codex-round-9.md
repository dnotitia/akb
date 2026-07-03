# codex 리뷰 라운드 9 — round-8 disposition 판정 + 최종 패스

**결과**: round-8 disposition(접근 메타데이터 vs 콘텐츠 구분)은 **인정** —
vault_access 건 재제기 없음. 신규 MAJOR 1 → **수용, 수정 완료**.
VERDICT: OBJECTIONS → 라운드 10으로.

## Finding 1 [MAJOR] — 타 vault 소유 publication의 이름 참조(query_vault_names) 미가드

`akb_publish(table_query, vault="stable", query_vault_names=[..., "new-vault"])`
— publication row는 **다른 vault 소유**로 저장되지만 TEXT[] 컬럼에 이 vault
를 **이름으로** 참조한다. 롤백 가드는 `publications.vault_id = 이 vault`만
보므로 이를 놓치고, 퍼지 후 그 publication의 공개 URL이 해석 시점에
NotFoundError로 깨진다. 외부 노출 산출물 파괴 → round-8 원칙상 보호 대상.

**조치**: 가드에 `$name = ANY(query_vault_names)` 교차-vault 검사
(`xvault_pubs`) 추가 — 존재 시 롤백 포기(vault 존치).

**수용된 잔여 리스크 (문서화)**: 이 참조는 FK가 아니라서 vaults row
FOR UPDATE 직렬화가 커버하지 못함 — 검사→퍼지 사이 마이크로초 창에 커밋되는
publish는 여전히 dangling 가능. 단 **동일한 dangling 위험이 delete_vault
정상 경로에 이미 존재**(PRE-EXISTING — vault 삭제는 타 vault publication의
query_vault_names 참조를 검사하지 않음)하므로, 이 창을 닫는 것은 본 diff
범위 밖의 별도 과제로 남긴다. → 후속 과제: delete_vault에도 동일 검사(경고
또는 거부) 추가 검토.

## 검증

전체 스위트 456 passed, ruff clean, document_service mypy clean.
