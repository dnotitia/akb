# Round 04 — 2026-06-10 — 코드베이스 전수 대조 검토 (PM 요청, effort=max)

설계의 모든 주장·전제를 실제 코드와 대조. 신규 발견 8건, README 전면
재구성(한장요약 + 검증표 + 구현 제약 섹션 신설)에 반영.

## 검증으로 확정된 것

1. **akb_sql 우회 불가** — `role_sync.py`는 `vt_*` 사용자 테이블에만
   GRANT를 낸다. `documents`는 akb_user_* 롤에서 도달 불가 → 모든 문서
   쓰기는 API를 지나므로 lane이 전수 포착한다. "PG가 진실" 불변식에
   구멍 없음.
2. **files/tables는 git 무관** — file_service는 S3+PG(+s3_delete_outbox),
   table_service는 PG only. lane/outbox 적용 범위 = documents 한정으로
   확정.
3. **단일 uvicorn 프로세스** — Dockerfile CMD에 --workers 없음 +
   replicas=1. 인메모리 lane의 전제 성립.

## 신규 발견 (설계에 반영 필요)

4. **#170은 GET만 고쳤다** — update(:576)와 edit(:748)의 merge-base는
   여전히 floating HEAD를 읽는다. browse 해시 백필(:1065)도 동일.
   Phase 2 읽기 이전 대상에 전수 등재.
5. **publication 렌더(:794)는 floating HEAD를 *동기로* 읽는다** —
   async 함수 `resolve_document_publication` 안에서 to_thread 없이
   gitpython 호출 → event loop 블로킹 잠복 이슈. activity 라우트
   (`activity.py:34` vault_log)도 동일하게 동기 호출. Phase 2의 자연
   수혜 지점 + 단기 수선 후보로 기록.
6. **bulk 연산이 per-path lane을 가로지른다** — collection 삭제가
   `delete_paths_bulk`(collection_service:401)로 다중 path를 1커밋
   처리. 설계 대응: per-path 톰스톤을 outbox에 일괄 enqueue, 아카이버가
   멀티패스 배치 커밋(기존 선례 유지).
7. **본문은 chunks에서 재조립 불가** — 모든 chunk에 doc 메타데이터
   헤더가 베이크됨(put:355-368). Phase 2는 `documents.content` 컬럼
   신설이 필수. 아카이버의 풀 마크다운 재구성은 기존
   `_compose_markdown`(row 메타 + body) 재사용으로 해결.
8. **빈 repo 첫 커밋 경로** — vault 생성이 vault-skill 가이드를 시드
   (:1296-1304, 미러 제외). 아카이버에 parentless 커밋 경로 필요
   (현 `_commit_via_clone` 대체).
9. **미러 vault는 아카이버 제외 대상** — external_git vault는 git→PG
   방향(상류가 진실). 단, **사용자 쓰기를 미러 vault에서 명시적으로
   거부하는 가드는 이번 검토에서 발견하지 못함** — Phase 2 전 확인/보강
   필요 항목으로 미결에 추가.
10. **edit의 unchanged 단락(:775-786)** — 변경 없으면 커밋 없이 반환.
    lane 처리·coalescing 구현 시 이 단락 보존 명세.
