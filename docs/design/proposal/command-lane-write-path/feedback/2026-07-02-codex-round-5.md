# codex 리뷰 라운드 5 — round-4 fix 검증

**결과**: BLOCKING 1 → **수용, 수정 완료**. VERDICT: OBJECTIONS → 라운드 6으로.

## Finding 1 [BLOCKING] — 롤백 퍼지가 동시 유입된 외부 쓰기를 파괴

vaults row는 커밋 즉시 외부에 보인다(owner 단축 경로) → 생성이 seed 단계에
있는 동안 owner의 동시 put이 가능. 그 경합으로 seed put이 10초 데드라인을
넘겨 WriteBusyError가 나면, 롤백이 vault_id 전체를 퍼지하면서 **타 요청의
이미 성공(ack)된 문서·chunks·이벤트까지 삭제**. create-후-즉시-ingest는
에이전트의 현실적인 사용 패턴이라 발생 가능성도 실질적.

**조치 — 안전 규칙 "롤백은 외부 데이터를 절대 파괴하지 않는다"**:
`_rollback_vault_create`로 재구조화.

1. 롤백이 해당 vault의 **write-lane 게이트를 획득** — in-flight 외부
   writer는 게이트를 쥐고 있으므로 먼저 드레인되고, 신규 writer는 검사·퍼지
   동안 배제된다(TOCTOU 차단).
2. **foreign-write 가드**: seed 경로(`overview/vault-skill.md`) 외 문서,
   또는 임의의 file/table row가 존재하면 파괴적 롤백을 **통째로 포기** —
   vault는 (부분 시드 상태로) 존치, 생성 에러는 그대로 전파, ERROR 로그로
   수동 정리 안내. 게이트 획득이 WriteBusyError로 실패해도 같은 방향
   (존치·스킵)으로 퇴화 — 안전한 쪽으로만 실패한다.
3. 퍼지 순서 재정렬: **DB 퍼지 → 디스크** ("repo 없는 살아있는 row"보다
   "재생성 막는 고아 디렉토리(로그+수동 rm)"가 덜 해롭다).
4. 롤백 전체를 `run_compensation`으로 감싸 반복 취소에도 완주.

**잔여 코너(문서화, 수용)**: 생성 윈도우 안에 외부 writer가 정확히
`overview/vault-skill.md` 경로로 쓴 경우 seed와 구별 불가. 요청 신원
추적 없이는 판별 불가능하며 발생 확률이 무시 가능한 수준.

## 검증

전체 스위트 456 passed, ruff clean, 변경 파일 mypy clean.
