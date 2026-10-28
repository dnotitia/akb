# 검증 기록

2026-10-21, 분석 기준 `f2158b5f`. Chromium headless, 1440×1000, reduced-motion. 로컬 Vite `http://127.0.0.1:4191`에서 수행했다. API 응답은 Playwright route interception으로 전부 대체했으며 사용자와 Vault는 합성 fixture이다. 운영 서비스의 실제 사용자 환경을 검증한 결과가 아니다.

| 사례 | 주입 조건 | 관찰 결과 | 관련 항목 |
|---|---|---|---|
| Overview 최초 조회 | `/info` 응답 보류 | Overview region 비표시. 본문에는 Loading vault overview…만 표시 | A01 |
| Home 재조회 실패 | 최초 집계 1/36/2/7 성공 후 inventory 이벤트, 집계 503 | 문서/테이블/파일 수치가 —로 교체. Vault 개수는 1 유지 | A04 |
| Activity 최초 실패 | 활동 API 503 | Failed to load 및 Retry가 표시되지만 main 내부 busy=true 영역 1개 잔존 | A14 |
| Vault 템플릿 대기 | 템플릿 API 응답 보류 | Vault template 버튼 활성화, None — empty vault 표시, 버튼 busy 속성 없음 | A03 |

위 4개는 런타임 재현, 나머지 개선 항목은 코드 분기 확인에 근거한다. 스크린리더 실제 낭독, 모바일, 운영 네트워크 및 모든 mutation 흐름은 이번 검증에 포함하지 않았다. fixture의 부가 API는 최소 응답이므로 화면에 나타나는 다른 안내를 별도 제품 결함으로 판정하지 않았다.

- [관찰값 JSON](evidence/browser-results.json)
- [재현 스크립트](evidence/reproduce.mjs)
- [Overview 지연 화면](evidence/overview-pending.png)
- [홈 집계 재조회 실패 화면](evidence/home-refresh-error.png)
- [템플릿 지연 화면](evidence/templates-pending.png)

분석 시점 재실행: 이 스크립트는 수정 전 현상을 검증하므로 기준 커밋 `f2158b5f`에서 실행한다. 개선 후 검증은 `frontend/e2e/loading-feedback.spec.ts`를 사용한다. 프로젝트 의존성 및 Playwright Chromium이 설치된 환경에서 frontend의 Vite를 4191 포트로 실행하고, 저장소 루트에서 `node docs/prd/wip/2026-10-21-api-loading-feedback/evidence/reproduce.mjs`를 실행한다. 결과 JSON과 PNG는 `/tmp/akb-loading-*`에 생성된다. 이 스크립트는 감사 재현용이며 일반 CI 스위트에 등록하지 않았다.

문서 검증: 상대 파일 링크의 존재, 관찰값 JSON 파싱, 24개 라우트 수, 코드 참조 파일 존재, 공백 오류를 확인했다. 제품 소스 변경은 없다.
