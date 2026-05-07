# AKB Frontend v1.0 재설계 — Design Spec

> 작성일: 2026-04-22
> 브랜치: `feature/frontend-v1-redesign`
> 상위 컨텍스트: [`docs/frontend-redesign-brief.md`](../../frontend-redesign-brief.md) (전체 시스템·API·현황 분석)
> 방향: 본 문서의 §1~§4 — 구현을 위한 확정 스펙

---

## 확정된 3대 축

| 축 | 결정 | 근거 |
|---|---|---|
| **Agent-first 방향** | **A. Read-only UI 고수** | `README.md`·`collaboration-design.md`의 설계 철학 일관. v0.5 포지션 유지, **기능 셋 동일하고 디자인 시스템만 재정립**. B/C는 별도 후속 버전. |
| **Phase 1 범위** | **전체 스윕** (11 페이지 + 전체 컴포넌트·토큰) | 디자인 일관성 완성도 우선. |
| **디렉토리 전략** | **D1. In-place 교체** | `lib/api.ts` · `hooks/` · `main.tsx` 그대로 재사용. 컴포넌트·페이지 파일만 재작성. 브랜치 diff = 변경 내역. |

---

## §1. 재작성 범위 & 파일 구조

### 🟢 보존 (변경 없음)
```
frontend/src/
├── main.tsx                 # 라우터 (동일 라우트 유지)
├── lib/api.ts               # REST 클라이언트 (SearchDoc 타입 source_type 대응 점검만)
├── lib/markdown.ts
├── lib/tree-route.ts
├── lib/utils.ts
├── hooks/use-vault-tree.ts
├── hooks/use-doc-outline.ts
└── hooks/use-measured-height.ts
```

### 🔴 재작성 (전면 교체)
```
frontend/src/
├── index.css                # 토큰·다크모드 전면 재정의
├── components/ui/*          # shadcn 프리미티브 재페인트
├── components/layout.tsx
├── components/vault-shell.tsx
├── components/vault-explorer.tsx
├── components/doc-outline.tsx
├── components/password-gate.tsx
├── components/table-viewer.tsx
├── components/file-viewer.tsx
├── components/json-tree.tsx
└── pages/*.tsx              # 11개 전부
```

### 🆕 신규 추가
```
frontend/src/
├── components/theme-toggle.tsx
├── components/status-badge.tsx
├── components/empty-state.tsx
├── components/keyboard-shortcut.tsx (kbd)
├── hooks/use-theme.ts
└── hooks/use-health.ts
```

### 의존성 추가

```json
{
  "dependencies": {
    "@fontsource/ibm-plex-sans": "...",
    "@fontsource-variable/jetbrains-mono": "...",
    "@fontsource-variable/fraunces": "...",
    "lucide-react": "...",
    "@radix-ui/react-dialog": "...",
    "@radix-ui/react-tabs": "...",
    "@radix-ui/react-tooltip": "...",
    "@radix-ui/react-dropdown-menu": "..."
  }
}
```

**근거**: Google Fonts 대신 self-host → CLS 방지 + 내부망 환경 동작. 현재 이모지/글리프 아이콘 → Lucide SVG로 통일.

---

## §2. 디자인 토큰 & 다크모드

### 3-레이어 토큰

```
Primitive (Raw)     →  Semantic (Role)          →  Component (Use)
paper #faf9f5          --color-background           bg-background
spark #ff4d12          --color-accent               button[variant=primary]
slate-900 #0F172A      --color-background(dark)
```

Tailwind 4 `@theme` 블록에 **semantic 토큰만 노출**. primitive는 CSS 변수로 내부 보관.

### 다크모드 메커니즘

```ts
// hooks/use-theme.ts
type Theme = "light" | "dark" | "system";
// localStorage["akb_theme"] 저장
// "system" → matchMedia("(prefers-color-scheme: dark)") 추적
// DOM <html class="dark"> 토글
```

```css
@custom-variant dark (&:where(.dark, .dark *));

@theme {
  /* 라이트 기본값 */
  --color-background: #faf9f5;
  --color-foreground: #0a0908;
  /* ... */
}

.dark {
  --color-background: #0F172A;
  --color-foreground: #F8FAFC;
  /* ... */
}
```

**FOUC 방지**: `index.html` head에 inline script — localStorage 읽고 `<html class="dark">` 즉시 적용 (body 렌더 전). 스크립트는 `document.documentElement.style.colorScheme = "dark"|"light"`도 함께 설정해 네이티브 폼 컨트롤(스크롤바, date picker, 시스템 select UI) 테마 반영.

**Radix 포털 고려**: Dialog/Tooltip/Dropdown은 `<Portal>`로 `document.body` 아래 렌더됨. `.dark` 클래스는 **`<html>`에 적용**하므로 포털도 자동 다크 테마 적용됨 (`:where(.dark, .dark *)` 선택자가 body 포털까지 도달). 컴포넌트별 `.dark` 재적용 불필요.

### Semantic 토큰 전체 셋

| 역할 | 라이트 | 다크 | 용도 |
|---|---|---|---|
| `background` | `#FAF9F5` paper | `#0F172A` | 페이지 배경 |
| `surface` | `#FFFFFF` | `#1B2336` | 카드, 패널, 모달 |
| `surface-muted` | `#ECEBE6` whisper | `#272F42` | 비활성·보조 |
| `foreground` | `#0A0908` ink | `#F8FAFC` | 본문 텍스트 |
| `foreground-muted` | `#75716B` smoke | `#94A3B8` | 라벨·메타 |
| `border` | `#0A0908` ink | `#334155` | 1px hairline |
| `border-strong` | `#0A0908` | `#475569` | 2px 강조 |
| `accent` | `#FF4D12` spark | `#FF4D12` | **공통** — 브랜드 DNA |
| `accent-foreground` | `#FAF9F5` | `#0F172A` | accent 위 텍스트 |
| `success` | `#16A34A` | `#22C55E` | 인덱싱 완료, OK |
| `warning` | `#CA8A04` | `#EAB308` | pending, retry |
| `destructive` | `#C63D09` ember | `#EF4444` | 삭제, 오류 |
| `ring` | `#FF4D12` | `#FF4D12` | focus ring |
| `ring-offset` | `background` 값 | `background` 값 | accent 버튼 위 ring 대비 확보 (ring-offset으로 흰/검 outer 레이어) |

**Accent-위-Ring 대비**: primary 버튼(배경 `accent=#FF4D12`)에 focus ring을 같은 spark orange로 그리면 보이지 않음. 해결: `ring-2 ring-offset-2 ring-offset-background` 패턴 강제 — accent 버튼은 `background` 색 offset 링을 먼저 그리고 그 바깥에 spark ring. Tailwind 기본 `ring-offset` 유틸 활용.

### 폰트 매핑

```css
--font-sans:    "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
--font-mono:    "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
--font-display: "Fraunces", ui-serif, Georgia, serif;  /* Publication 한정 */
```

| 적용 대상 | 폰트 |
|---|---|
| UI 전반 (버튼·라벨·본문) | `var(--font-sans)` |
| 코드·ID·URI·토큰·`.coord`·tabular-nums | `var(--font-mono)` 🆕 JetBrains |
| Publication prose (`/p/:slug`) | `var(--font-display)` Fraunces (보존) |
| 내부 문서 뷰어 prose | `var(--font-sans)` (Fraunces 제거) |

### 모션 토큰 (easing · duration)

```css
--duration-fast:   150ms;  /* 버튼 hover, 작은 state 변경 */
--duration-base:   200ms;  /* 일반 transition */
--duration-slow:   300ms;  /* 다이얼로그, 사이드바 */
--ease-out:        cubic-bezier(0.0, 0.0, 0.2, 1);  /* 진입 (fade-in, slide-in) */
--ease-in:         cubic-bezier(0.4, 0.0, 1, 1);    /* 퇴장 (fade-out, dismiss) */
--ease-in-out:     cubic-bezier(0.4, 0.0, 0.2, 1);  /* 양방향 (toggle) */
```

`prefers-reduced-motion: reduce` 시 duration `1ms`, easing `linear`로 강제 override.

### Typography scale

| 토큰 | 크기 | line-height | 용도 |
|---|---|---|---|
| `text-xs` | 12px | 1.33 | 메타, 타임스탬프, `.coord` |
| `text-sm` | 14px | 1.43 | 라벨, 보조 정보 |
| `text-base` | 16px | **1.5** | 기본 본문 (Tailwind default) |
| `text-lg` | 18px | 1.56 | 문서 뷰어 prose 본문 |
| `text-xl` | 20px | 1.4 | 카드 헤더 |
| `text-2xl` | 24px | 1.33 | 페이지 서브타이틀 |
| `text-3xl` | 30px | 1.2 | 페이지 H1 |

`.prose` 기본 line-height 1.75 유지.

### 보존 유틸 vs 제거

| 클래스 | 결정 | 이유 |
|---|---|---|
| `.coord` / `.coord-ink` / `.coord-spark` | ⭐ 보존 | 브랜드 DNA |
| `.font-display` / `.font-display-tight` | Publication 내 유지, 내부 UI 제거 | 미스매치 해소 |
| `.fade-up` / `.fade-in` / `.stagger` | 보존 | 절제된 진입 모션 |
| `.hairline*` / `.whisper-*` | semantic 토큰 기반 재정의 | |
| `.dotted` / `.grain` / `.marquee-track` | Auth/Landing hero에만 유지 | 내부 UI 제거 |
| `.prose` | 보존 + 다크모드 = **`prose dark:prose-invert` 패턴** | react-markdown 출력에 명시적 적용. `@tailwindcss/typography` 내장 `prose-invert`가 다크 본문 색 처리. |
| `--radius-*: 0` | ⭐ 보존 | 브랜드 DNA (샤프 모서리) |

---

## §3. 컴포넌트 & 페이지 재작성 맵

### UI 프리미티브 (`components/ui/`)

| 컴포넌트 | 주요 변경 |
|---|---|
| `button` | variants: `default/outline/ghost/destructive/link` · sizes: `sm/md/lg/icon` · radius 0 · focus-ring spark |
| `input` / `textarea` | border semantic, focus-ring spark |
| `card` | surface 배경, hairline border, padding 토큰화 |
| `badge` | **신규 variants**: `role` (owner/admin/writer/reader) · `status` (active/draft/archived) · `system` (pending/syncing/error) |
| `label` / `select` | Radix 래핑 유지 |
| `dialog` / `sheet` 🆕 | Radix 기반 — confirm, share 등 |
| `tabs` 🆕 | MCP 클라이언트 탭 통일 |
| `tooltip` 🆕 | external-git disabled 설명, 단축키 힌트 |
| `skeleton` 🆕 | 검색 rerank 2.5s 대응 |
| `kbd` 🆕 | `Cmd+\` 등 단축키 시각화 |

### 공통 레이아웃

| 파일 | 재작성 방향 |
|---|---|
| `layout.tsx` | 헤더 = 로고 · **visible 검색 입력** · 테마토글 · 계정. Cmd+K 팔레트는 도입 안 함 (유저 결정). |
| `vault-shell.tsx` | 좌 사이드바 토글 유지, 폭 280px, `Cmd+\` 단축키 유지 |
| `vault-explorer.tsx` | 트리 UX 유지. 아이콘 → Lucide. external-git/archived 뱃지 추가 |
| `doc-outline.tsx` | 스크롤 스파이 유지, 타이포만 mono 강조 |
| `theme-toggle.tsx` 🆕 | 3단(light/dark/system) dropdown |
| `status-badge.tsx` 🆕 | pending/syncing/archived/role 공통 렌더 |
| `empty-state.tsx` 🆕 | 빈 상태 일관 처리 |

### 페이지 11개

| # | 페이지 | v0.5 → v1.0 핵심 변화 |
|---|---|---|
| 1 | `auth.tsx` | marquee/grain 축소, 폼 단정화. Login/Register 탭. PasswordCredential 저장 유지 |
| 2 | `home.tsx` | 3단 레이아웃: (L) 볼트 리스트 · (M) 최근 활동 + 빠른 검색 · (R) PAT + MCP 설정. tabs 통일 |
| 3 | `vault-new.tsx` | 심플 폼. external-git 섹션은 **placeholder만** (백엔드 REST 확장 전) |
| 4 | `vault.tsx` | 헤더 = 볼트명 + role + public_access + archived/external-git 뱃지 · 본문 = 통계 + 최근활동 + 그래프 진입 |
| 5 | `document.tsx` | 좌: 트리(shell) · 중: markdown prose · 우: outline + frontmatter + relations + publish |
| 6 | `table.tsx` | 스키마 카드 + rows (tabular-nums, 50rows + 안내). SQL 쓰기 없음 |
| 7 | `file.tsx` | 메타데이터 + `FileViewer` 통합 (Publication에서만 쓰던 걸 인증 페이지에서도 사용). 다운로드 prominent |
| 8 | `graph.tsx` | force-graph 다크/라이트 대응. 사이드 패널로 선택 노드 정보. 관계 CRUD 없음. **테마 대응 방식**: `react-force-graph-2d`는 hex 문자열만 받으므로 `useTheme()` 변경 시 `getComputedStyle(document.documentElement).getPropertyValue('--color-foreground')` 등으로 토큰 읽어 `nodeColor`/`linkColor` prop에 주입, 테마 토글 시 re-render. |
| 9 | `search.tsx` | **타입별 섹션** (문서/테이블/파일). dense/literal 토글. 스켈레톤 |
| 10 | `settings.tsx` | 프로필 · PAT · admin user list · 테마 선택 |
| 11 | `public-publication.tsx` | ⭐ **유일하게 Fraunces 유지**. Editorial prose. password gate + FileViewer + TableViewer 재사용 |

### 아이콘 세트

**Lucide React** 도입. 트리 글리프(`▸`/`▾`/`·`/`⊟`/`⊞`) → Lucide SVG. 이모지는 Empty state 외 제거.

### MCP 클라이언트 설정 탭 (Home 내)

현재 개별 섹션 → 단일 `Tabs` (Cursor/Windsurf/Gemini/Claude Desktop/VSCode). config JSON 원클릭 복사 + 경로 힌트.

---

## §4. 실행 순서 & 품질 게이트

### 15단계 시퀀스

```
Phase 0: 기반 셋업
 1. @fontsource 패키지 설치 (ibm-plex-sans / jetbrains-mono / fraunces variable)
 2. Lucide React, Radix 프리미티브 (dialog/tabs/tooltip) 설치
 3. index.html에 FOUC 방지 theme inline script

Phase 1: 디자인 토큰
 4. index.css 전면 재작성 (primitive + semantic + .dark 매핑 + 유틸 정리)
 5. hooks/use-theme.ts + components/theme-toggle.tsx

Phase 2: UI 프리미티브
 6. components/ui/* 재페인트 (button/input/card/badge/label/select)
 7. components/ui/* 신규 (dialog/sheet/tabs/tooltip/skeleton/kbd)
 8. components/status-badge.tsx · empty-state.tsx

Phase 3: 공통 레이아웃
 9. layout.tsx + vault-shell.tsx + vault-explorer.tsx + doc-outline.tsx
10. hooks/use-health.ts

Phase 4: 페이지 (의존도 순)
11. auth.tsx → home.tsx → settings.tsx
12. vault-new.tsx → vault.tsx → document.tsx
13. search.tsx → table.tsx → file.tsx → graph.tsx
14. public-publication.tsx

Phase 5: QA
15. 다크/라이트 각 페이지 스크린샷 + 접근성 검증 + 빌드·타입체크
```

### 품질 게이트

**Phase 1~2**:
- [ ] `pnpm build` 성공
- [ ] `pnpm test` 통과
- [ ] 모든 semantic 토큰 라이트·다크 매핑 완료
- [ ] 대비비 ≥ 4.5:1 (본문), ≥ 3:1 (UI 글리프) — 라이트·다크 각각 독립 검증
- [ ] **Typography scale 토큰화** — `text-xs/sm/base/lg/xl/2xl/3xl` (12/14/16/18/20/24/30px). 본문 line-height 1.5-1.75
- [ ] **Easing 토큰**: `--ease-out: cubic-bezier(0.0, 0.0, 0.2, 1)` (진입), `--ease-in: cubic-bezier(0.4, 0.0, 1, 1)` (퇴장). linear 금지
- [ ] **Lucide 번들 영향 실측**: `rollup-plugin-visualizer` (또는 동등 도구)로 lucide 청크 크기 확인. **> 50KB gzipped**면 개별 경로 import 패턴(`lucide-react/dist/esm/icons/check`)으로 전환

**Phase 3**:
- [ ] 트리 키보드 네비 회귀 없음 (arrow/home/end/pgup/pgdn/typeahead)
- [ ] `Cmd+\` 사이드바 토글 동작
- [ ] 헤더 높이 측정(`useMeasuredHeight`) 정상

**Phase 4**:
- [ ] 각 페이지 라이트·다크 스크린샷 비교
- [ ] 기능 회귀 없음 (A 원칙)
- [ ] `react-force-graph-2d` 라우트 레벨 `lazy()` 처리
- [ ] 검색 결과 `source_type` 분기
- [ ] **모든 form** — visible `<label>` 확보, placeholder-only 금지 (Auth/Settings/VaultNew/Publish dialog)
- [ ] **Async submit 버튼** — `disabled` + spinner during pending (Login/Register/Publish/Grant/Revoke/Delete). 중복 submit 방지
- [ ] **Icon-only 버튼** — 전부 `aria-label` 부착 (theme-toggle, sidebar toggle, close, delete)
- [ ] **Clickable cursor** — 커스텀 클릭 요소(트리 노드, 카드, 태그)에 `cursor-pointer`
- [ ] **Graph edge 구분** — 관계 타입 6종을 **색상 + dash pattern** 조합으로 구분 (색맹 접근성). 예: `depends_on`=실선, `related_to`=점선, `implements`=이중선, `references`=dashed
- [ ] **이미지 CLS** — `FileViewer` 이미지 프리뷰에 `aspect-ratio` 또는 width/height 속성 예약

**Phase 5**:
- [ ] Lighthouse Performance ≥ 90 (홈 기준), Accessibility ≥ 95
- [ ] CLS < 0.1
- [ ] `prefers-reduced-motion` 존중 (전체 페이지)
- [ ] touch target ≥ 44×44px
- [ ] focus ring 제거 없음, accent 버튼 focus ring offset 검증
- [ ] **axe-core** 자동 검사 (Lighthouse 또는 `@axe-core/cli`) 0 violations — 라이트·다크 각각
- [ ] alias 제거 (`bg-paper` 등) + Vitest 스냅샷 업데이트

### 테스트 전략

- **Vitest 단위**: 유지 — api.ts/hooks 기존 테스트 통과
- **Playwright E2E**: 이번 Phase 제외 (수동 스크린샷 QA)
- **수동 QA**: 각 페이지 라이트·다크 2장 = **22장 스크린샷**
- **타입체크**: `tsc --noEmit` 100%

### 커밋 전략

단일 브랜치 `feature/frontend-v1-redesign`에서 Phase별 커밋. PR은 완성 후 단일. **예상 13 커밋** (Phase 0~5 + QA).

```
chore(deps): add fontsource, lucide-react, radix primitives
feat(design-tokens): primitive + semantic + dark mode mapping
feat(theme): theme toggle + useTheme + FOUC script
feat(ui): repaint button/input/card/badge primitives
feat(ui): add dialog/tabs/tooltip/skeleton primitives + dropdown-menu
feat(layout): redesign header + vault shell + explorer
feat(page): redesign auth
feat(page): redesign home with MCP config tabs
feat(page): redesign vault + vault-new + settings
feat(page): redesign document viewer
feat(page): redesign search + table + file
feat(page): redesign graph with theme-aware colors
feat(page): redesign public publication (Fraunces editorial)
chore(qa): light/dark screenshot QA, a11y verification, alias cleanup
```

### 리스크 & 대응

| 리스크 | 대응 |
|---|---|
| 토큰 rename으로 v0.5 페이지 일시 깨짐 | **Phase 2~3 alias 전략**: 기존 `bg-paper`/`text-ink`/`border-ink` 등을 Phase 1 `index.css`에서 semantic 토큰 alias로 유지 → Phase 4에서 페이지 리라이트 시 새 클래스로 교체 → **Phase 5 QA 직전 alias 제거 + Vitest 스냅샷 업데이트**. 기존 테스트(`toHaveClass("bg-paper")`)는 Phase 4 페이지별 리라이트에서 함께 갱신. `coord-ink`/`coord-spark`는 유틸 자체가 semantic이 아니라 primitive 참조 클래스라 유지. |
| Fraunces 로드 실패 시 Publication 깨짐 | `font-display: swap` + fallback stack |
| 다크모드 대비비 미달 | Phase 1 완료 후 chroma.js 검증 스크립트 (필요 시) |
| 스코프 블로우업 | Phase commit boundary에서 점검 |
| external-git UI placeholder 혼란 | 툴팁/라벨로 "차기 버전 예정" 명시 |

---

## 비스코프 (v1.0에서 제외)

- Cmd+K 커맨드 팔레트 (유저 결정에 따라 제외)
- 쓰기 UI 일체 (문서 에디터, 파일 업로드 인증 페이지, 테이블 DDL/DML, 관계 CRUD) — A 원칙
- external-git 볼트 생성 UI (백엔드 REST 확장 후 별도)
- i18n
- 모바일 1급 지원
- Playwright E2E
- WebSocket 실시간성
- React Query 도입 (수동 fetch 유지)

---

## 다음 단계

1. 본 spec 커밋 → spec review 루프 → 유저 최종 승인
2. `writing-plans` 스킬로 상세 구현 플랜 작성
3. Phase 0부터 순차 구현
