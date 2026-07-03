# codex 리뷰 라운드 8 — round-7 fix 검증

**결과**: MAJOR 1 → **기각 (reasoned rejection, 아래 disposition)**.
라운드 7의 가드 확장(publications/todos/collections)과 FOR UPDATE 원자화
자체는 유효 판정. VERDICT: OBJECTIONS → disposition 명문화 후 라운드 9에서
재평가.

## Finding 1 [MAJOR] — 롤백이 동시 유입된 vault_access(akb_grant)를 파괴 → 기각

지적: 생성 창 안에 owner가 `akb_grant`로 권한을 부여(ack됨)한 뒤 생성이
실패하면, 롤백 퍼지가 그 grant를 vault와 함께 삭제 — "ack된 외부 쓰기 파괴".

**기각 사유 — 콘텐츠와 접근 메타데이터의 구분 (설계 결정)**:

1. **접근권한은 vault 존재에 종속된 메타데이터다.** grant의 유일한 referent는
   vault 자신이고, "creation 실패 = vault는 존재한 적 없음"이 확정되면 그
   grant는 지시 대상을 잃는다. `delete_vault`(정상 삭제 경로)도 vault_access
   를 동일하게 캐스케이드 퍼지한다 — 롤백이 이보다 보수적일 이유가 없다.
2. **가드에 넣으면 결과가 더 나쁘다.** grant 하나가 롤백을 거부하면
   half-created vault(시드 미완, 잠재적 롤 불완전)가 잔존하고, 재생성은
   ConflictError로 막히며, 운영자 수동 정리가 필요해진다. 실피해( 재생성 후
   grant 재발급 한 번) 대비 과잉 방어다.
3. **실질 피해 경로는 이미 가드가 막는다.** grantee가 실제로 뭔가를 쓰면
   그 순간 documents/files/tables/publications/todos/collections 가드가
   롤백을 거부한다. 파괴되는 것은 "빈 vault에 대한 권한 행"뿐이다.
4. 같은 원리로 **ownership transfer**(생성 창 내 소유권 이전 + 콘텐츠 0)도
   퍼지 허용 — 콘텐츠가 있으면 어차피 가드가 거부한다.

**원칙 명문화**: 롤백 가드의 보호 대상은 *사용자가 vault에 맡긴 콘텐츠와
외부로 노출된 산출물*(문서·파일·테이블·발행물·todo·컬렉션)이다. *vault
자신에 대한 접근 메타데이터*(vault_access, RBAC 롤, 소유권)는 vault의
생사와 운명을 같이한다.

## 검증

코드 변경 없음 (disposition만). 스위트 상태는 라운드 7 시점과 동일
(456 passed, ruff/mypy clean).
