# UI/UX 개선 검증 기준

이 문서는 재현 가능한 검증 범위와 한계를 설명한다. 개인 환경의 경로,
내부 문서 주소, 로컬 실행 로그와 임시 브랜치 이력은 공개 자료에 포함하지 않는다.

## 검증 대상

| 영역 | 검증할 계약 |
|---|---|
| 문서·Guide | 미저장/처리 중 이동 보호, 같은 계정의 재검증 중 초안 유지, metadata 명시적 지우기, canonical 경로·미리보기 복귀 |
| Vault 권한 | 멤버/public의 최고 유효 역할, managed·archived 제한, 지연 권한 응답 격리, public-link 가용성 |
| 계정·세션 | foreground proof, 계정 교체, password 폼 오류와 만료 세션의 구별, mutation receipt 소유권 |
| 파일 | idempotent confirm, pending-only 취소, 응답 손실 복구, 같은 세션의 upload job 보존, preview byte 제한과 URL 갱신 |
| Table·Overlay | 명시적 discard, 처리 중 닫기 제한, Schema/cell 원문, 리사이즈·focus 복귀·부모 스크롤 유지 |
| 피드백·조회 | inventory invalidation, 알림 읽음 실패의 보존, Clipboard/Theme 오류 복구, URL query/history |
| 화면 구성 | 모바일·데스크톱, light/dark, 메뉴 키보드/new-tab 동작, Collections 선택 보존, 발행 후 레이아웃 안정성 |

## 저장소 정적 게이트

`CONTRIBUTING.md`의 Python/Node 의존성을 먼저 준비한 뒤 저장소 루트에서 실행한다.

```sh
bash scripts/check.sh
```

Frontend 전용 디자인 토큰 검사와 프로덕션 번들은 별도로 실행한다.

```sh
cd frontend
pnpm run design:check
pnpm run build
pnpm exec vitest run --project storybook --maxWorkers=1
```

## 브라우저 회귀

자체 HTTP fixture를 사용하는 suite는 실제 앱·Chromium·Radix를 검사하지만
업무 API의 서버 동작이나 실제 SSO/스토리지 연결을 증명하지 않는다.
`fixture` 모드는 production build를 직접 띄우고 backend 없이 실행한다.
fixture가 응답하지 않은 요청은 닫힌 포트로 proxy되어 502가 된다.

```sh
cd frontend
AKB_FE_E2E_MODE=fixture \
  pnpm exec playwright test e2e/document-header-navigation.spec.ts \
  e2e/vault-focused-workspace.spec.ts e2e/resource-reading-workspace.spec.ts \
  e2e/resource-overlay-accessibility.spec.ts e2e/document-preview-search-return.spec.ts \
  e2e/interface-composition.spec.ts e2e/file-lifecycle.spec.ts \
  e2e/notifications-contract.spec.ts e2e/notifications-read-recovery.spec.ts \
  e2e/session-boundaries.spec.ts
```

아래 구현 경계가 유지되는지 검사한다.

- 문서 메뉴는 실제 목적지 링크를 사용하고, 현재 문서에서 Overview를 잘못 선택하지 않는다.
- Settings/Members/Public links/Graph는 Collections를 숨기지만 저장된 tree 설정을 변경하지 않는다.
- 문서에서는 Collections를 유지하고 중간 메뉴 줄만 제거한다. File/Table과 미리보기는 별도 계약을 유지한다.
- Publish → Public link 전환이 도구 줄 높이나 본문 좌표를 바꾸지 않는다.
- 수정 중 이동은 확인을 요구하고 upload/save 중 이탈을 차단한다. 수정키로 새 탭을 여는 링크는 기존 초안을 유지한다.

## 서버 및 PostgreSQL 계약

Backend 변경은 관련 unit 검사와 격리 PostgreSQL 검사를 각각 실행한다.
DB 미설정으로 skip된 검사를 성공으로 기록하지 않는다. 파일 confirm·동시 confirm·
pending 취소의 실제 transaction 경계는 PostgreSQL fixture가 담당한다.
실행 방법은 `scripts/ci/README.md`와 각 테스트의 환경 계약을 따른다.

## 범위 한계

- 브라우저 fixture와 Storybook은 실서비스 다계정 로그인, Keycloak, 실제 object-storage round trip의 대체가 아니다.
- 새 UI는 서버 권한 검사를 대체하지 않는다. 서버가 제공하지 않는 값은 unknown으로 남긴다.
- 새 pending-upload 취소 endpoint가 없는 서버에서는 성공한 파일을 삭제하는 fallback을 사용하지 않는다.
- 기존 Markdown extension 중복 경고, bundle 크기와 프로젝트 전체 의존성 갱신은 별도 작업이다.
- 공개 PR의 CI 상태는 해당 revision의 hosted check 결과를 기준으로 판단한다. 배포와 merge는 검증과 별도 단계다.
