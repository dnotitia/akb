# codex 리뷰 라운드 7 — round-6 fix 검증

**결과**: BLOCKING 1 → **수용, 수정 완료**. VERDICT: OBJECTIONS → 라운드 8로.

## Finding 1 [BLOCKING] — foreign 가드 목록 불완전 (publications 캐스케이드)

`akb_publish(table_query)`는 doc/file/table 없이도 publications row를 만들
수 있고(공개 URL이 사용자에게 반환됨), `DELETE FROM vaults`의
`ON DELETE CASCADE`(init.sql:343)가 이를 삼킨다. 가드는 docs/files/tables만
보고 있었다.

**조치 — 스키마 전수 기반 가드 확장**: init.sql의 `REFERENCES vaults` 전수
조사(10개 테이블) 후, 생성 창 안에서 "사용자 생성 가능 + 파괴 시 실질 손실"
기준으로 분류:

- **가드 추가**: `publications`(공개 URL), `todos`, `collections`(생성 소유
  경로 = 'overview' + 템플릿 경로 밖의 것 — `akb_create_collection`으로 생성
  가능; 템플릿 정보를 롤백에 전달)
- **가드 불요(퍼지 일관)**: chunks(파생), edges/resource_aliases(doc 가드
  통과 시 엔드포인트가 seed뿐), vault_access(사라지는 vault에 대한 권한),
  vault_external_git(미러 분기 전용, 표준 경로 무관), events(FK 없음, 퍼지
  대상)

FOR UPDATE 원자화(라운드 6)는 이들 전부에 동일하게 적용된다 — 모든 FK
INSERT가 vaults row에 FOR KEY SHARE를 잡기 때문.

## 검증

전체 스위트 456 passed, ruff clean, document_service mypy clean.
