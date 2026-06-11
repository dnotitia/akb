# Round 03 — 2026-06-10 — 원자성 서술 재검증 (PM 요청)

PM이 round-02의 "git↔PG 원자성 반영"을 재검토 요청. 적대적으로 다시 검증한
결과, **방향(PG-first + outbox WAL)은 성립하나 문서가 과대 서술 + 미명세
5건**을 포함하고 있었음. §2.2에 "정확한 시퀀스와 크래시 윈도우" 블록을
추가해 보강.

발견 사항:

1. **트랜잭션은 1개가 아니라 2개** — tx1(콘텐츠+outbox)과 tx2(장부:
   outbox 닫기 + current_commit). 다이어그램이 tx2를 숨기고 있었음.
   크래시 윈도우 W1(tx1↔git), W2(git↔tx2)를 명시.
2. **"tree 동일 시 skip"의 함정** — skip이 장부 갱신까지 생략하면 W2
   크래시 후 current_commit이 영원히 낡는다. skip = 커밋 생성만 생략,
   tx2는 반드시 수행으로 명세.
3. **"git이 반드시 따라온다"의 전제** — 아카이버가 events_publisher의
   MAX_RETRIES→abandoned 패턴을 따르면 보장이 깨짐. 무한 재시도 + 알림
   (no-abandon)을 구현 필수 전제로 명시.
4. **삭제도 level-based 상태** — "PG에 행 없음" → "git 삭제 커밋" 수렴.
   update→delete 레이스의 유령 파일 방지.
5. **표현 정정** — "원자성"이 아니라 "명시된 윈도우를 가진 수렴적
   일관성". inline에서도 커밋↔장부 사이 수 ms 지연 존재.
   (content, current_commit) 계약이 #170의 "항상 일치"에서 "같거나
   과거"로 바뀜을 명시 (GET 본문은 PG 서빙이라 사용자 영향 없음).
