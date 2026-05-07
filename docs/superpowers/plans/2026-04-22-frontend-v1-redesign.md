# AKB Frontend v1.0 재설계 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AKB 프론트엔드를 v0.5 "Schematic Editorial" 프로토타입에서 v1.0 IDE-Native 디자인 시스템으로 전면 재설계 (내부 UI는 Developer Mono, Publication만 Fraunces editorial 보존).

**Architecture:** In-place 교체 (`lib/api.ts`·`hooks/`·`main.tsx` 보존, `index.css` + `components/*` + `pages/*` 재작성). 3-레이어 토큰(primitive → semantic → component) + `@custom-variant dark` + localStorage 기반 테마 토글 + FOUC 방지 inline script. Agent-first 원칙 유지 → 쓰기 UI 없음.

**Tech Stack:** React 19, Tailwind 4, Vite 8, shadcn 베이스, Radix UI, Lucide React, @fontsource (IBM Plex Sans / JetBrains Mono / Fraunces), react-force-graph-2d.

**Spec:** `docs/superpowers/specs/2026-04-22-frontend-v1-redesign-design.md`
**Branch:** `feature/frontend-v1-redesign`

---

## 사전 확인

- [ ] `git status`가 clean (이전 커밋 `3f197a9`까지 완료)
- [ ] `frontend/` 에서 `pnpm install` 성공
- [ ] `pnpm dev` 기동 후 `http://localhost:5173/` 접근 가능
- [ ] `pnpm test` 기존 테스트 통과

---

## Phase 0 — 기반 셋업

### Task 0.1: 폰트 패키지 설치

**Files:**
- Modify: `frontend/package.json`

- [ ] **Step 1: 폰트 패키지 3종 설치**

```bash
cd frontend
pnpm add @fontsource/ibm-plex-sans @fontsource-variable/jetbrains-mono @fontsource-variable/fraunces
```

- [ ] **Step 2: 설치 확인**

```bash
ls node_modules/@fontsource/ibm-plex-sans
ls node_modules/@fontsource-variable/jetbrains-mono
ls node_modules/@fontsource-variable/fraunces
```
Expected: 각 디렉토리 존재

- [ ] **Step 3: 커밋**

```bash
git add package.json pnpm-lock.yaml
git commit -m "chore(deps): add fontsource packages for self-hosted fonts"
```

---

### Task 0.2: FOUC 방지 inline script

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: index.html `<head>`에 theme 감지 스크립트 추가** (body 렌더 전 실행)

```html
<!-- frontend/index.html <head> 안에 -->
<script>
  (function() {
    try {
      var saved = localStorage.getItem('akb_theme');
      var system = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      var theme = saved === 'dark' || saved === 'light' ? saved : system;
      if (theme === 'dark') {
        document.documentElement.classList.add('dark');
      }
      document.documentElement.style.colorScheme = theme;
    } catch (e) {}
  })();
</script>
```

- [ ] **Step 2: 빌드 확인**

```bash
pnpm build
```
Expected: 에러 없이 `dist/` 생성

- [ ] **Step 3: 커밋**

```bash
git add index.html
git commit -m "feat(theme): add FOUC-preventing inline theme script"
```

---

## Phase 1 — 디자인 토큰

### Task 1.1: `index.css` 전면 재작성

**Files:**
- Rewrite: `frontend/src/index.css`

- [ ] **Step 1: 기존 파일 백업 이름으로 리네임 (참조용)**

```bash
mv frontend/src/index.css frontend/src/index.css.v05-backup
```

- [ ] **Step 2: 새 `index.css` 작성** — primitive + semantic + `.dark` 매핑 + 모션 토큰 + typography scale + 유틸

전체 코드:
```css
@import "tailwindcss";
@import "@tailwindcss/typography";

@import "@fontsource/ibm-plex-sans/300.css";
@import "@fontsource/ibm-plex-sans/400.css";
@import "@fontsource/ibm-plex-sans/500.css";
@import "@fontsource/ibm-plex-sans/600.css";
@import "@fontsource/ibm-plex-sans/700.css";
@import "@fontsource-variable/jetbrains-mono";
@import "@fontsource-variable/fraunces";

@custom-variant dark (&:where(.dark, .dark *));

@theme {
  /* ── Primitives (내부 참조용, UI에서 직접 쓰지 말 것) ────── */
  --color-paper: #faf9f5;
  --color-ink: #0a0908;
  --color-smoke: #75716b;
  --color-whisper: #ecebe6;
  --color-spark: #ff4d12;
  --color-ember: #c63d09;

  /* ── Semantic tokens (라이트 기본) ─────────────────────── */
  --color-background: #faf9f5;
  --color-surface: #ffffff;
  --color-surface-muted: #ecebe6;
  --color-foreground: #0a0908;
  --color-foreground-muted: #75716b;
  --color-border: #0a0908;
  --color-border-strong: #0a0908;
  --color-accent: #ff4d12;
  --color-accent-foreground: #faf9f5;
  --color-success: #16a34a;
  --color-warning: #ca8a04;
  --color-destructive: #c63d09;
  --color-destructive-foreground: #faf9f5;
  --color-ring: #ff4d12;

  /* shadcn alias (기존 컴포넌트 호환 — Phase 5에서 정리) */
  --color-primary: #0a0908;
  --color-primary-foreground: #faf9f5;
  --color-secondary: #ecebe6;
  --color-secondary-foreground: #0a0908;
  --color-muted: #ecebe6;
  --color-muted-foreground: #75716b;
  --color-card: #ffffff;
  --color-card-foreground: #0a0908;
  --color-input: #0a0908;

  /* Typography */
  --font-sans: "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  --font-mono: "JetBrains Mono Variable", "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  --font-display: "Fraunces Variable", "Fraunces", ui-serif, Georgia, serif;

  /* Motion tokens */
  --duration-fast: 150ms;
  --duration-base: 200ms;
  --duration-slow: 300ms;
  --ease-out: cubic-bezier(0, 0, 0.2, 1);
  --ease-in: cubic-bezier(0.4, 0, 1, 1);
  --ease-in-out: cubic-bezier(0.4, 0, 0.2, 1);

  /* Sharp radius by default */
  --radius-sm: 0;
  --radius-md: 0;
  --radius-lg: 0;
}

/* ── Dark mode overrides ──────────────────────────────────── */
.dark {
  --color-background: #0f172a;
  --color-surface: #1b2336;
  --color-surface-muted: #272f42;
  --color-foreground: #f8fafc;
  --color-foreground-muted: #94a3b8;
  --color-border: #334155;
  --color-border-strong: #475569;
  --color-accent: #ff4d12;
  --color-accent-foreground: #0f172a;
  --color-success: #22c55e;
  --color-warning: #eab308;
  --color-destructive: #ef4444;
  --color-destructive-foreground: #f8fafc;
  --color-ring: #ff4d12;

  --color-primary: #f8fafc;
  --color-primary-foreground: #0f172a;
  --color-secondary: #272f42;
  --color-secondary-foreground: #f8fafc;
  --color-muted: #272f42;
  --color-muted-foreground: #94a3b8;
  --color-card: #1b2336;
  --color-card-foreground: #f8fafc;
  --color-input: #334155;
}

/* ── Global ──────────────────────────────────────────────── */
html, body {
  font-family: var(--font-sans);
  background: var(--color-background);
  color: var(--color-foreground);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  font-feature-settings: "ss01", "ss02";
}

*, *::before, *::after {
  border-color: var(--color-border);
}

::selection {
  background: var(--color-accent);
  color: var(--color-accent-foreground);
}

/* ── Typography utilities ────────────────────────────────── */
.font-display {
  font-family: var(--font-display);
  font-optical-sizing: auto;
  font-variation-settings: "opsz" 144, "SOFT" 30;
  font-weight: 400;
  letter-spacing: -0.025em;
  line-height: 0.95;
}

.font-display-tight {
  font-family: var(--font-display);
  font-optical-sizing: auto;
  font-variation-settings: "opsz" 144, "SOFT" 30;
  font-weight: 500;
  letter-spacing: -0.04em;
  line-height: 0.92;
}

.font-mono {
  font-family: var(--font-mono);
  font-feature-settings: "zero", "ss01";
}

.coord {
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--color-foreground-muted);
}

.coord-ink {
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--color-foreground);
}

.coord-spark {
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--color-accent);
}

h1, h2, h3, h4, h5, h6 {
  font-family: var(--font-sans);
  font-weight: 600;
  letter-spacing: -0.012em;
}

/* tabular figures for numeric columns */
.tabular-nums {
  font-variant-numeric: tabular-nums;
}

/* ── Motion ──────────────────────────────────────────────── */
@keyframes fade-up {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

@keyframes fade-in {
  from { opacity: 0; }
  to   { opacity: 1; }
}

.fade-up {
  animation: fade-up var(--duration-slow) var(--ease-out) both;
}

.fade-in {
  animation: fade-in var(--duration-base) var(--ease-out) both;
}

.stagger > * {
  animation: fade-up var(--duration-slow) var(--ease-out) both;
}
.stagger > *:nth-child(1) { animation-delay: 40ms; }
.stagger > *:nth-child(2) { animation-delay: 80ms; }
.stagger > *:nth-child(3) { animation-delay: 120ms; }
.stagger > *:nth-child(4) { animation-delay: 160ms; }
.stagger > *:nth-child(5) { animation-delay: 200ms; }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 1ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 1ms !important;
    scroll-behavior: auto !important;
  }
}

/* ── Focus ring (accent-safe) ────────────────────────────── */
.focus-ring {
  outline: none;
}
.focus-ring:focus-visible {
  outline: 2px solid var(--color-ring);
  outline-offset: 2px;
}

/* ── Prose (markdown) ────────────────────────────────────── */
.prose {
  color: var(--color-foreground);
  max-width: 72ch;
  line-height: 1.75;
}

.dark .prose {
  color: var(--color-foreground);
}

.prose a { color: var(--color-accent); }
.prose code {
  font-family: var(--font-mono);
  background: var(--color-surface-muted);
  padding: 0.1em 0.3em;
  font-size: 0.9em;
}
.prose pre {
  font-family: var(--font-mono);
  background: var(--color-surface-muted);
  color: var(--color-foreground);
  padding: 1em;
  overflow-x: auto;
}

/* ── Hairline utilities (preserved from v0.5) ────────────── */
.hairline-b { border-bottom: 1px solid var(--color-border); }
.hairline-t { border-top: 1px solid var(--color-border); }
.hairline-l { border-left: 1px solid var(--color-border); }
.hairline-r { border-right: 1px solid var(--color-border); }
```

- [ ] **Step 3: 빌드 + 타입체크**

```bash
pnpm build
```
Expected: 성공. dist/ 생성됨.

- [ ] **Step 4: dev 서버 시각 확인**

```bash
pnpm dev
```
Expected: 기존 페이지들이 라이트 모드로 정상 렌더. 색 깨짐 없음 (alias 덕).

- [ ] **Step 5: 다크모드 수동 테스트**

브라우저 DevTools Console에서:
```js
document.documentElement.classList.add('dark')
```
Expected: 전 페이지 배경 slate-900 전환, 텍스트 F8FAFC, accent은 spark orange 유지.

- [ ] **Step 6: 백업 파일 제거**

```bash
rm frontend/src/index.css.v05-backup
```

- [ ] **Step 7: 커밋**

```bash
git add frontend/src/index.css
git commit -m "feat(design-tokens): rewrite index.css with semantic tokens + dark mode"
```

---

### Task 1.2: `useTheme` 훅 + `ThemeToggle` 컴포넌트

**Files:**
- Create: `frontend/src/hooks/use-theme.ts`
- Create: `frontend/src/components/theme-toggle.tsx`

- [ ] **Step 1: `hooks/use-theme.ts` 작성**

```ts
import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";
const STORAGE_KEY = "akb_theme";

function getSystemTheme(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function resolveTheme(theme: Theme): "light" | "dark" {
  return theme === "system" ? getSystemTheme() : theme;
}

function applyTheme(theme: Theme) {
  const resolved = resolveTheme(theme);
  document.documentElement.classList.toggle("dark", resolved === "dark");
  document.documentElement.style.colorScheme = resolved;
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    return saved === "dark" || saved === "light" ? saved : "system";
  });

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    if (next === "system") {
      localStorage.removeItem(STORAGE_KEY);
    } else {
      localStorage.setItem(STORAGE_KEY, next);
    }
    applyTheme(next);
  }, []);

  useEffect(() => {
    if (theme !== "system") return;
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => applyTheme("system");
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [theme]);

  return { theme, setTheme, resolved: resolveTheme(theme) };
}
```

- [ ] **Step 2: `components/theme-toggle.tsx` 작성** (Radix DropdownMenu 기반, 3단 선택)

```tsx
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme, type Theme } from "@/hooks/use-theme";

const ICONS: Record<Theme, React.ComponentType<{ className?: string }>> = {
  light: Sun,
  dark: Moon,
  system: Monitor,
};

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const Icon = ICONS[theme];
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        aria-label={`Theme (current: ${theme})`}
        className="inline-flex h-9 w-9 items-center justify-center border border-border bg-surface text-foreground hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <Icon className="h-4 w-4" aria-hidden />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          className="min-w-[140px] border border-border bg-surface p-1 shadow-none"
        >
          {(["light", "dark", "system"] as const).map((opt) => {
            const OptIcon = ICONS[opt];
            return (
              <DropdownMenu.Item
                key={opt}
                onSelect={() => setTheme(opt)}
                className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-muted"
              >
                <OptIcon className="h-4 w-4" aria-hidden />
                <span className="capitalize">{opt}</span>
                {theme === opt && <span className="coord-spark ml-auto">ON</span>}
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
```

- [ ] **Step 3: 임시로 `layout.tsx`에 ThemeToggle 추가하여 수동 테스트**

(이후 Task 3.1에서 정식 배치. 지금은 동작만 확인)

```tsx
// layout.tsx 헤더 어디든 임시로:
import { ThemeToggle } from "@/components/theme-toggle";
// ... <ThemeToggle /> 배치
```

- [ ] **Step 4: dev에서 토글 3번 (light/dark/system) 확인**

```bash
pnpm dev
```
- light 선택 → paper 배경
- dark 선택 → slate-900 배경
- system 선택 → OS 설정 따름
- 페이지 새로고침 → 선택 유지

- [ ] **Step 5: 임시 ThemeToggle 배치는 롤백** (Task 3.1에서 정식 배치)

- [ ] **Step 6: 커밋**

```bash
git add frontend/src/hooks/use-theme.ts frontend/src/components/theme-toggle.tsx
git commit -m "feat(theme): add useTheme hook and 3-way ThemeToggle dropdown"
```

---

## Phase 2 — UI 프리미티브

### Task 2.1: Button 컴포넌트 재페인트

**Files:**
- Rewrite: `frontend/src/components/ui/button.tsx`

- [ ] **Step 1: `button.tsx` 재작성** (CVA 기반, accent-safe focus ring)

```tsx
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 cursor-pointer",
  {
    variants: {
      variant: {
        default:
          "bg-foreground text-background hover:bg-foreground/90 border border-foreground",
        accent:
          "bg-accent text-accent-foreground hover:bg-accent/90 border border-accent",
        outline:
          "bg-surface text-foreground border border-border hover:bg-surface-muted",
        ghost:
          "bg-transparent text-foreground hover:bg-surface-muted",
        destructive:
          "bg-destructive text-destructive-foreground hover:bg-destructive/90 border border-destructive",
        link:
          "bg-transparent text-accent underline-offset-4 hover:underline h-auto px-0",
      },
      size: {
        sm: "h-8 px-3 text-sm",
        md: "h-10 px-4 text-sm",
        default: "h-10 px-4 text-sm", /* alias for backward compat */
        lg: "h-11 px-5 text-base",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size, className }))}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { buttonVariants };
```

- [ ] **Step 2: 기존 사용처 타입 체크**

```bash
cd frontend && pnpm tsc --noEmit
```
Expected: 에러 없음 (variant/size 이름 호환성).

- [ ] **Step 3: 라이트·다크 모두에서 모든 variant 수동 확인**

홈 또는 auth 페이지에서 버튼 나오는 곳에서 focus (Tab)·hover·disabled 상태 모두 확인.

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/components/ui/button.tsx
git commit -m "feat(ui): repaint Button with accent variant and safe focus ring"
```

---

**Note**: Button spec allows `accent` variant (neue) + existing `default/outline/ghost/destructive/link`. The `accent` variant handles primary brand-colored CTAs where the base `default` (foreground on background) would be too subtle. Usage sites may pass `variant="accent"` for publish, primary submit, etc.

---

### Task 2.2: Input / Textarea / Label

**Files:**
- Rewrite: `frontend/src/components/ui/input.tsx`
- Create: `frontend/src/components/ui/textarea.tsx`
- Create: `frontend/src/components/ui/label.tsx`

- [ ] **Step 1: `input.tsx` 재작성**

```tsx
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type = "text", ...props }, ref) => (
    <input
      ref={ref}
      type={type}
      className={cn(
        "flex h-10 w-full border border-border bg-surface px-3 py-2 text-sm text-foreground placeholder:text-foreground-muted",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        "disabled:opacity-50 disabled:cursor-not-allowed",
        "file:border-0 file:bg-transparent file:text-sm file:font-medium",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";
```

- [ ] **Step 2: `textarea.tsx` 작성** (동일 패턴)

```tsx
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

export const Textarea = forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        "flex min-h-[80px] w-full border border-border bg-surface px-3 py-2 text-sm text-foreground placeholder:text-foreground-muted",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        "disabled:opacity-50",
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = "Textarea";
```

- [ ] **Step 3: `label.tsx` 작성** (Radix Label 래핑)

```tsx
import * as LabelPrimitive from "@radix-ui/react-label";
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

export const Label = forwardRef<
  React.ElementRef<typeof LabelPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof LabelPrimitive.Root>
>(({ className, ...props }, ref) => (
  <LabelPrimitive.Root
    ref={ref}
    className={cn(
      "text-sm font-medium text-foreground leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70",
      className,
    )}
    {...props}
  />
));
Label.displayName = "Label";
```

- [ ] **Step 4: 빌드 + 타입체크**

```bash
pnpm tsc --noEmit && pnpm build
```

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/components/ui/input.tsx frontend/src/components/ui/textarea.tsx frontend/src/components/ui/label.tsx
git commit -m "feat(ui): repaint Input + add Textarea and Label primitives"
```

---

### Task 2.3: Card + Badge 재페인트 (role/status/system variants)

**Files:**
- Rewrite: `frontend/src/components/ui/card.tsx`
- Rewrite: `frontend/src/components/ui/badge.tsx`

- [ ] **Step 1: `card.tsx` 재작성**

```tsx
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

export const Card = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn("border border-border bg-surface text-foreground", className)}
      {...props}
    />
  ),
);
Card.displayName = "Card";

export const CardHeader = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex flex-col gap-1.5 p-6", className)} {...props} />
  ),
);
CardHeader.displayName = "CardHeader";

export const CardTitle = forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => (
    <h3 ref={ref} className={cn("text-xl font-semibold leading-tight tracking-tight", className)} {...props} />
  ),
);
CardTitle.displayName = "CardTitle";

export const CardDescription = forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLParagraphElement>>(
  ({ className, ...props }, ref) => (
    <p ref={ref} className={cn("text-sm text-foreground-muted", className)} {...props} />
  ),
);
CardDescription.displayName = "CardDescription";

export const CardContent = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("p-6 pt-0", className)} {...props} />
  ),
);
CardContent.displayName = "CardContent";

export const CardFooter = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex items-center p-6 pt-0", className)} {...props} />
  ),
);
CardFooter.displayName = "CardFooter";
```

- [ ] **Step 2: `badge.tsx` 재작성** (role / status / system variants 추가)

```tsx
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium uppercase tracking-wider font-mono border",
  {
    variants: {
      variant: {
        default: "border-border text-foreground bg-surface",
        outline: "border-border text-foreground-muted bg-transparent",
        /* role badges */
        owner: "border-accent text-accent bg-transparent",
        admin: "border-foreground text-foreground bg-transparent",
        writer: "border-foreground-muted text-foreground-muted bg-transparent",
        reader: "border-foreground-muted text-foreground-muted bg-transparent opacity-70",
        /* status badges */
        active: "border-success text-success bg-transparent",
        draft: "border-foreground-muted text-foreground-muted bg-transparent",
        archived: "border-warning text-warning bg-transparent",
        superseded: "border-foreground-muted text-foreground-muted bg-transparent line-through",
        /* system badges */
        pending: "border-warning text-warning bg-transparent",
        syncing: "border-accent text-accent bg-transparent",
        error: "border-destructive text-destructive bg-transparent",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant, className }))} {...props} />;
}
```

- [ ] **Step 3: 빌드 + 타입체크**

```bash
pnpm tsc --noEmit
```

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/components/ui/card.tsx frontend/src/components/ui/badge.tsx
git commit -m "feat(ui): repaint Card and extend Badge with role/status/system variants"
```

---

### Task 2.3b: Select 프리미티브

**Files:**
- Create: `frontend/src/components/ui/select.tsx`

**Decision**: 현재 코드는 native `<select>`를 그대로 씀. v1.0도 **native `<select>` 유지** + 토큰 클래스 스타일만 정의 (shadcn Select는 도입 안 함 — YAGNI). `label.tsx`와 함께 써서 접근성 확보.

- [ ] **Step 1: `select.tsx` 작성** (native + 토큰 클래스)

```tsx
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

export const Select = forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, children, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        "flex h-10 w-full border border-border bg-surface px-3 py-2 text-sm text-foreground appearance-none",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        "disabled:opacity-50 disabled:cursor-not-allowed",
        "bg-[right_0.75rem_center] bg-no-repeat pr-9",
        className,
      )}
      style={{
        backgroundImage: `url("data:image/svg+xml;charset=UTF-8,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'%3e%3cpath d='m6 9 6 6 6-6'/%3e%3c/svg%3e")`,
        backgroundSize: "1rem",
      }}
      {...props}
    >
      {children}
    </select>
  ),
);
Select.displayName = "Select";
```

- [ ] **Step 2: 빌드**

```bash
pnpm tsc --noEmit
```

- [ ] **Step 3: 커밋**

```bash
git add frontend/src/components/ui/select.tsx
git commit -m "feat(ui): add styled native Select primitive"
```

---

### Task 2.4: Dialog / Sheet / Tooltip 프리미티브 추가

**Files:**
- Create: `frontend/src/components/ui/dialog.tsx`
- Create: `frontend/src/components/ui/tooltip.tsx`

- [ ] **Step 1: `dialog.tsx` 작성** (Radix Dialog 래핑)

```tsx
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogPortal = DialogPrimitive.Portal;

export const DialogOverlay = forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-50 bg-foreground/60 data-[state=open]:animate-in data-[state=closed]:animate-out",
      className,
    )}
    {...props}
  />
));
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName;

export const DialogContent = forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>
>(({ className, children, ...props }, ref) => (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed left-1/2 top-1/2 z-50 grid w-full max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4 border border-border bg-surface p-6 text-foreground shadow-none",
        className,
      )}
      {...props}
    >
      {children}
      <DialogPrimitive.Close
        aria-label="Close"
        className="absolute right-3 top-3 h-8 w-8 inline-flex items-center justify-center text-foreground-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <X className="h-4 w-4" />
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPortal>
));
DialogContent.displayName = DialogPrimitive.Content.displayName;

export const DialogHeader = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("flex flex-col gap-1.5", className)} {...props} />
);

export const DialogFooter = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("flex flex-col-reverse sm:flex-row sm:justify-end sm:gap-2", className)} {...props} />
);

export const DialogTitle = forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title ref={ref} className={cn("text-lg font-semibold", className)} {...props} />
));
DialogTitle.displayName = DialogPrimitive.Title.displayName;

export const DialogDescription = forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description ref={ref} className={cn("text-sm text-foreground-muted", className)} {...props} />
));
DialogDescription.displayName = DialogPrimitive.Description.displayName;
```

- [ ] **Step 2: `tooltip.tsx` 작성** (Radix Tooltip 래핑)

```tsx
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

export const TooltipProvider = TooltipPrimitive.Provider;
export const Tooltip = TooltipPrimitive.Root;
export const TooltipTrigger = TooltipPrimitive.Trigger;

export const TooltipContent = forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <TooltipPrimitive.Content
    ref={ref}
    sideOffset={sideOffset}
    className={cn(
      "z-50 border border-border bg-foreground px-2 py-1 text-xs text-background",
      className,
    )}
    {...props}
  />
));
TooltipContent.displayName = TooltipPrimitive.Content.displayName;
```

- [ ] **Step 3: 빌드 + 타입체크**

```bash
pnpm tsc --noEmit
```

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/components/ui/dialog.tsx frontend/src/components/ui/tooltip.tsx
git commit -m "feat(ui): add Dialog and Tooltip primitives"
```

---

### Task 2.5: Tabs / Skeleton / Kbd 프리미티브

**Files:**
- Create: `frontend/src/components/ui/tabs.tsx`
- Create: `frontend/src/components/ui/skeleton.tsx`
- Create: `frontend/src/components/ui/kbd.tsx`

- [ ] **Step 1: `tabs.tsx` 작성**

```tsx
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { forwardRef } from "react";
import { cn } from "@/lib/utils";

export const Tabs = TabsPrimitive.Root;

export const TabsList = forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.List
    ref={ref}
    className={cn("inline-flex items-center gap-1 border-b border-border", className)}
    {...props}
  />
));
TabsList.displayName = TabsPrimitive.List.displayName;

export const TabsTrigger = forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={cn(
      "inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-foreground-muted hover:text-foreground border-b-2 border-transparent -mb-px",
      "data-[state=active]:text-foreground data-[state=active]:border-accent",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
      "cursor-pointer",
      className,
    )}
    {...props}
  />
));
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName;

export const TabsContent = forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content ref={ref} className={cn("pt-4 focus:outline-none", className)} {...props} />
));
TabsContent.displayName = TabsPrimitive.Content.displayName;
```

- [ ] **Step 2: `skeleton.tsx` 작성**

```tsx
import { cn } from "@/lib/utils";

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse bg-surface-muted", className)}
      aria-hidden
      {...props}
    />
  );
}
```

- [ ] **Step 3: `kbd.tsx` 작성**

```tsx
import { cn } from "@/lib/utils";

export function Kbd({ className, ...props }: React.HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        "inline-flex items-center px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider",
        "border border-border bg-surface-muted text-foreground-muted",
        className,
      )}
      {...props}
    />
  );
}
```

- [ ] **Step 4: 빌드 + 타입체크**

```bash
pnpm tsc --noEmit
```

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/components/ui/tabs.tsx frontend/src/components/ui/skeleton.tsx frontend/src/components/ui/kbd.tsx
git commit -m "feat(ui): add Tabs, Skeleton, Kbd primitives"
```

---

### Task 2.6: StatusBadge + EmptyState 공통 컴포넌트

**Files:**
- Create: `frontend/src/components/status-badge.tsx`
- Create: `frontend/src/components/empty-state.tsx`

- [ ] **Step 1: `status-badge.tsx` 작성**

```tsx
import { Archive, CircleDashed, GitBranch, Lock, Shield, ShieldCheck, User } from "lucide-react";
import { Badge } from "@/components/ui/badge";

type RoleBadgeProps = { role: "owner" | "admin" | "writer" | "reader" };
export function RoleBadge({ role }: RoleBadgeProps) {
  const Icon = role === "owner" ? ShieldCheck : role === "admin" ? Shield : User;
  return (
    <Badge variant={role as any}>
      <Icon className="h-3 w-3" aria-hidden /> {role}
    </Badge>
  );
}

type DocStatusBadgeProps = { status: "draft" | "active" | "archived" | "superseded" };
export function DocStatusBadge({ status }: DocStatusBadgeProps) {
  return <Badge variant={status as any}>{status}</Badge>;
}

type VaultStateBadgeProps = {
  archived?: boolean;
  externalGit?: boolean;
  publicAccess?: "none" | "reader" | "writer";
};
export function VaultStateBadge({ archived, externalGit, publicAccess }: VaultStateBadgeProps) {
  return (
    <div className="flex gap-1">
      {archived && (
        <Badge variant="archived"><Archive className="h-3 w-3" aria-hidden /> archived</Badge>
      )}
      {externalGit && (
        <Badge variant="syncing"><GitBranch className="h-3 w-3" aria-hidden /> external</Badge>
      )}
      {publicAccess && publicAccess !== "none" && (
        <Badge variant="outline"><Lock className="h-3 w-3" aria-hidden /> public:{publicAccess}</Badge>
      )}
    </div>
  );
}

type IndexingBadgeProps = { pending: number };
export function IndexingBadge({ pending }: IndexingBadgeProps) {
  if (pending === 0) return null;
  return (
    <Badge variant="pending" title={`${pending} items pending`}>
      <CircleDashed className="h-3 w-3 animate-spin" aria-hidden />
      indexing {pending.toLocaleString()}
    </Badge>
  );
}
```

- [ ] **Step 2: `empty-state.tsx` 작성**

```tsx
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  title: string;
  description?: string;
  action?: React.ReactNode;
  icon?: React.ReactNode;
  className?: string;
}

export function EmptyState({ title, description, action, icon, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center py-12 px-6 text-center border border-dashed border-border",
        className,
      )}
    >
      {icon && <div className="mb-4 text-foreground-muted">{icon}</div>}
      <p className="text-base font-medium text-foreground">{title}</p>
      {description && <p className="mt-1 text-sm text-foreground-muted max-w-md">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
```

- [ ] **Step 3: 빌드 + 타입체크**

```bash
pnpm tsc --noEmit
```

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/components/status-badge.tsx frontend/src/components/empty-state.tsx
git commit -m "feat(ui): add StatusBadge variants and EmptyState shared component"
```

---

## Phase 3 — 공통 레이아웃

### Task 3.1: `layout.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/components/layout.tsx`

- [ ] **Step 1: 기존 `layout.tsx` 읽어 라우트 조건(auth/publication 제외 등) 파악**

```bash
cat frontend/src/components/layout.tsx | head -80
```

- [ ] **Step 2: 재작성** — 헤더 = 로고 + **visible 검색 input** + ThemeToggle + 계정 메뉴

핵심 패턴:
```tsx
import { Outlet, Link, useLocation, useNavigate } from "react-router-dom";
import { useState } from "react";
import { Search as SearchIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/theme-toggle";
import { getToken, setToken } from "@/lib/api";

export function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const [q, setQ] = useState("");

  const hideChrome = location.pathname === "/auth" || location.pathname.startsWith("/p/");
  if (hideChrome) return <Outlet />;

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-40 hairline-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-4 px-4">
          <Link to="/" className="font-mono text-sm font-semibold tracking-tight">
            AKB
          </Link>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (q.trim()) navigate(`/search?q=${encodeURIComponent(q.trim())}`);
            }}
            className="relative flex-1 max-w-md"
          >
            <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-foreground-muted pointer-events-none" aria-hidden />
            <Input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search vaults, documents, tables…"
              aria-label="Search"
              className="pl-9 h-9"
            />
          </form>
          <nav className="ml-auto flex items-center gap-2">
            <Link to="/settings">
              <Button variant="ghost" size="sm">Settings</Button>
            </Link>
            <ThemeToggle />
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setToken(null);
                navigate("/auth");
              }}
            >
              Sign out
            </Button>
          </nav>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
```

- [ ] **Step 3: 빌드 + dev 시각 확인** (light/dark 모두)

```bash
pnpm dev
```

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/components/layout.tsx
git commit -m "feat(layout): redesign header with visible search + theme toggle"
```

---

### Task 3.2: `vault-shell.tsx` + `vault-explorer.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/components/vault-shell.tsx`
- Rewrite: `frontend/src/components/vault-explorer.tsx`

- [ ] **Step 1: 기존 shell/explorer 읽고 키보드 네비 로직 보존**

```bash
cat frontend/src/components/vault-shell.tsx
cat frontend/src/components/vault-explorer.tsx
```

- [ ] **Step 2: `vault-shell.tsx` 재작성** — 280px 좌 사이드바, `Cmd+\` 토글 유지, 토큰 색상 적용

`useEffect`로 `keydown` 리스너 등록 (기존 로직 그대로). `localStorage["akb-explorer-visible"]` 유지.

- [ ] **Step 3: `vault-explorer.tsx` 재작성**

- 트리 글리프(`▸`/`▾`/`·`/`⊟`/`⊞`) → Lucide로 교체:
  - 컬렉션 접힘: `ChevronRight`
  - 컬렉션 펼침: `ChevronDown`
  - 문서: `FileText`
  - 테이블: `Table`
  - 파일: `File`
- 키보드 네비(arrow/home/end/pgup/pgdn/typeahead) **보존** — 기존 로직을 그대로 옮김
- 각 노드 active 상태는 `data-active` 속성 + CSS로 `bg-surface-muted`
- external-git/archived 볼트에는 헤더 옆 `VaultStateBadge`

- [ ] **Step 3b: 기존 Vitest 테스트 사전 점검**

```bash
cd frontend
grep -l "▸\|▾\|·\|⊟\|⊞" src/**/*.test.{ts,tsx} 2>/dev/null
grep -l "bg-paper\|text-ink" src/**/*.test.{ts,tsx} 2>/dev/null
```
Expected: 해당 글리프/클래스 하드코딩된 assertion이 발견되면 **Lucide SVG + semantic 토큰 대응으로 테스트 먼저 수정**. 스냅샷은 pnpm test -u로 갱신. 이 작업을 Task 3.2 커밋 전에 완료.

ARIA 보강 확인:
- 트리 컨테이너 `role="tree"`
- 노드 `role="treeitem"` + `aria-expanded` (컬렉션만) + `aria-level`
- 이미 구현된 키보드 네비 재검증

- [ ] **Step 4: 키보드 테스트**

dev 서버에서 볼트 페이지 들어가 트리에 포커스:
- ↓/↑ 네비
- →/← 확장/접기
- Home/End
- PgUp/PgDn
- 타이핑 → 해당 이름으로 점프

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/components/vault-shell.tsx frontend/src/components/vault-explorer.tsx
git commit -m "feat(layout): redesign vault shell + explorer with Lucide icons"
```

---

### Task 3.3: `doc-outline.tsx` + `use-health.ts`

**Files:**
- Rewrite: `frontend/src/components/doc-outline.tsx`
- Create: `frontend/src/hooks/use-health.ts`

- [ ] **Step 1: `doc-outline.tsx` 재작성** — 스크롤 스파이 로직 보존, 타이포만 mono 강조

핵심 변경:
- 헤딩 번호/레벨 배지를 `.coord` 스타일로
- 활성 헤딩은 `text-accent`
- 비활성은 `text-foreground-muted`

- [ ] **Step 2: `use-health.ts` 작성** — `/health` 주기 폴링 훅

```ts
import { useEffect, useState } from "react";

export interface HealthSnapshot {
  embed_backfill?: { pending: number };
  external_git?: { total: number; due: number };
  metadata_backfill?: { pending: number };
  qdrant?: {
    reachable: boolean;
    backfill?: { upsert?: { pending: number } };
  };
}

const ENDPOINT = "/health";
const DEFAULT_INTERVAL = 15000;

/**
 * /health is a PUBLIC endpoint (no auth) per backend `main.py`.
 * No token header needed. On failure returns { data: null, error }.
 * Consumers should render fallback UI silently — /health failure must
 * not break the page (it's purely an informational badge).
 */
export function useHealth(enabled: boolean, intervalMs = DEFAULT_INTERVAL) {
  const [data, setData] = useState<HealthSnapshot | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const r = await fetch(ENDPOINT);
        if (!r.ok) throw new Error(`${r.status}`);
        const json = await r.json();
        if (!cancelled) {
          setData(json);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e as Error);
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled, intervalMs]);

  return { data, error };
}
```

**Consumer rule**: `useHealth`의 `error`가 null 아니어도 페이지는 정상 작동해야 함. `IndexingBadge`는 `data?.embed_backfill?.pending > 0`일 때만 렌더 (`data`가 null이면 아무것도 안 보임 — degraded gracefully).

- [ ] **Step 3: 빌드 + 타입체크**

```bash
pnpm tsc --noEmit
```

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/components/doc-outline.tsx frontend/src/hooks/use-health.ts
git commit -m "feat(layout): redesign doc outline + add useHealth polling hook"
```

---

## Phase 4 — 페이지 재작성

> 각 페이지 Task는 동일한 패턴: (1) 기존 파일 읽고 state/effect 파악 → (2) JSX 재작성 (토큰 클래스명 사용, aria-label 부착, visible label, async button disabled, Lucide 아이콘) → (3) `pnpm tsc --noEmit` → (4) dev에서 라이트·다크 시각 확인 → (5) 커밋.

### Task 4.1: `auth.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/pages/auth.tsx`

- [ ] **Step 1: 기존 auth.tsx 읽기** — Login/Register 폼, PasswordCredential 저장 로직, marquee 장식

- [ ] **Step 2: 재작성**
  - marquee/grain 제거, 폼 중앙 정렬 단정한 카드
  - Login/Register를 `Tabs`로 분리
  - `<Label>` 필수, `<Input>` 토큰 적용
  - submit 버튼 `disabled={pending}` + spinner
  - PasswordCredential API 호출 로직 **보존**
  - 에러는 필드 아래 inline + `role="alert"` + color `text-destructive`

- [ ] **Step 3: 수동 QA**
  - 로그인 성공/실패, 회원가입 성공/실패, Tab 네비, 다크모드

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/pages/auth.tsx
git commit -m "feat(page): redesign auth with tabbed login/register"
```

---

### Task 4.2: `home.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/pages/home.tsx`

- [ ] **Step 1: 기존 home.tsx 읽기** — listVaults / getRecent / listPATs / MCP 클라이언트 탭

- [ ] **Step 2: 재작성** (3단 레이아웃)

```
┌─────────────────────────────────────────────────────────────┐
│ Header                                                      │
├──────────────┬─────────────────────────────┬────────────────┤
│ My Vaults    │ Recent activity             │ PAT management │
│ (list Card)  │ (compact list, tabular-nums)│ (card)         │
│              │                             │                │
│              │                             │ ──────────     │
│              │                             │ MCP Config     │
│              │                             │ (Tabs)         │
└──────────────┴─────────────────────────────┴────────────────┘
```

- 볼트 목록: `VaultStateBadge` 사용
- 최근 활동: timestamp는 `.coord` + `tabular-nums`
- MCP 설정: `Tabs` 컴포넌트로 Cursor/Windsurf/Gemini/Claude Desktop/VSCode. 각 탭에 JSON 코드블록 + "Copy" 버튼
- PAT 발급 버튼 → `Dialog` 사용

- [ ] **Step 3: 수동 QA** — PAT 발급/복사, MCP 탭 전환, 볼트 링크

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/pages/home.tsx
git commit -m "feat(page): redesign home with 3-column layout + MCP tabs"
```

---

### Task 4.3: `settings.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/pages/settings.tsx`

- [ ] **Step 1: 기존 읽기** — 프로필 · PAT · admin users list

- [ ] **Step 2: 재작성** — 섹션별 카드 (프로필 / PAT / 테마 / admin users if is_admin)
  - 테마 섹션: `ThemeToggle` + 설명 텍스트
  - admin users: 삭제 confirm은 `Dialog`

- [ ] **Step 3: 수동 QA**

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/pages/settings.tsx
git commit -m "feat(page): redesign settings with theme section + admin delete dialog"
```

---

### Task 4.4: `vault-new.tsx` + `vault.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/pages/vault-new.tsx`
- Rewrite: `frontend/src/pages/vault.tsx`

- [ ] **Step 1: 기존 읽기**

- [ ] **Step 2: `vault-new.tsx`** — 심플 Card 폼. 템플릿 선택은 select. external-git 섹션은 `<Tooltip>` 감싸 disabled ("차기 버전")

- [ ] **Step 3: `vault.tsx`** — 헤더 = 볼트명 + `VaultStateBadge` + role · 본문 = 통계 + 최근활동(useHealth로 pending 표시) + 그래프 진입 버튼

- [ ] **Step 4: 수동 QA**

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/pages/vault-new.tsx frontend/src/pages/vault.tsx
git commit -m "feat(page): redesign vault overview + vault-new form"
```

---

### Task 4.5: `document.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/pages/document.tsx`

- [ ] **Step 1: 기존 읽기** — outline + relations + publish toggle

- [ ] **Step 2: 재작성**

3단 레이아웃:
- 좌: VaultShell 제공
- 중: `<article className="prose dark:prose-invert">` + react-markdown (IBM Plex Sans, **Fraunces 제거**)
- 우: Sticky rail = Outline + frontmatter 뷰 + Relations + Publish 컨트롤

Publish 컨트롤:
- 버튼 `Publish` → `Dialog` 열어 옵션(password/expires/embed) 선택
- 기존 slug 있으면 `Copy URL` + `Unpublish`

- [ ] **Step 3: 수동 QA** — markdown 렌더, outline 스크롤 스파이, publish dialog, 다크 prose

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/pages/document.tsx
git commit -m "feat(page): redesign document viewer with publish dialog"
```

---

### Task 4.6: `search.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/pages/search.tsx`

- [ ] **Step 1: 기존 읽기** — `searchDocs` / `grepDocs`, URL query 파싱

- [ ] **Step 2: 재작성**

- 상단: 쿼리 표시 + `Tabs` (Semantic / Literal)
- 결과는 **source_type별 섹션** (문서 / 테이블 / 파일 아이콘 분리)
- 로딩 시 `Skeleton` (rerank 2.5s 대응)
- 빈 결과 `EmptyState`
- 각 결과 카드: title + vault/collection · matched_section · score는 `tabular-nums` `.coord`

- [ ] **Step 3: API 타입 점검** — `SearchDoc` 타입이 `source_type` + `source_id` 가지는지 `lib/api.ts` 확인. 필요 시 수정.

```bash
grep -n "SearchDoc\|source_type\|source_id" frontend/src/lib/api.ts
```

- [ ] **Step 4: 수동 QA** — 검색어 입력, semantic/literal 전환, 타입별 섹션

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/pages/search.tsx frontend/src/lib/api.ts
git commit -m "feat(page): redesign search with source_type sections + skeleton"
```

---

### Task 4.7: `table.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/pages/table.tsx`

- [ ] **Step 1: 기존 읽기**

- [ ] **Step 2: 재작성**
  - 상단: 스키마 카드 (컬럼 · 타입 · PK · NN 플래그)
  - 본문: rows 테이블 (최대 50) · `tabular-nums` · mono 컬럼 헤더
  - 하단: "50rows만 미리보기 · `akb_sql`로 전체 접근" 안내
  - 빈 테이블 `EmptyState`

- [ ] **Step 3: 수동 QA**

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/pages/table.tsx
git commit -m "feat(page): redesign table viewer with schema card + tabular preview"
```

---

### Task 4.8: `file.tsx` 재작성

**Files:**
- Rewrite: `frontend/src/pages/file.tsx`

- [ ] **Step 1: 기존 읽기** — 메타데이터 + 다운로드

- [ ] **Step 2: 재작성**
  - 헤더: 파일명 (mono), mime_type Badge, size (tabular)
  - `FileViewer` 컴포넌트 통합 사용 (이미지/PDF/JSON/HTML/text 프리뷰)
  - 이미지 프리뷰는 `aspect-ratio` 예약 (CLS 방지)
  - 다운로드 버튼 `variant="accent"` prominent

- [ ] **Step 3: FileViewer 컴포넌트 자체는 다음 태스크에서 재페인트**

- [ ] **Step 4: 수동 QA**

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/pages/file.tsx
git commit -m "feat(page): redesign file page with viewer integration"
```

---

### Task 4.9: `file-viewer.tsx` + `json-tree.tsx` + `password-gate.tsx` + `table-viewer.tsx` 재페인트

**Files:**
- Rewrite: `frontend/src/components/file-viewer.tsx`
- Rewrite: `frontend/src/components/json-tree.tsx`
- Rewrite: `frontend/src/components/password-gate.tsx`
- Rewrite: `frontend/src/components/table-viewer.tsx`

- [ ] **Step 1: 각 파일 읽고 로직 파악**

- [ ] **Step 2: 재페인트** — 토큰 클래스, mono 폰트, 다크모드 대응, aspect-ratio 예약

- `json-tree.tsx`: 색상 하드코드(purple/orange/green) → 토큰 기반 (예: null=`text-foreground-muted`, bool=`text-warning`, number=`text-success`, string=`text-foreground`, key=`text-accent`)
- `password-gate.tsx`: Dialog 재사용
- `table-viewer.tsx` / `file-viewer.tsx`: 토큰 색만 교체

- [ ] **Step 3: 빌드 + 타입체크**

```bash
pnpm tsc --noEmit
```

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/components/file-viewer.tsx frontend/src/components/json-tree.tsx frontend/src/components/password-gate.tsx frontend/src/components/table-viewer.tsx
git commit -m "feat(ui): repaint file/json/table viewers with token-based colors"
```

---

### Task 4.10: `graph.tsx` 재작성 (테마 대응 color)

**Files:**
- Rewrite: `frontend/src/pages/graph.tsx`

- [ ] **Step 1: 기존 읽기**

- [ ] **Step 2: 재작성** — force-graph 토큰 색 동적 주입

핵심 패턴:
```tsx
import { useTheme } from "@/hooks/use-theme";
import { lazy, Suspense, useMemo } from "react";

const ForceGraph2D = lazy(() => import("react-force-graph-2d"));

function useGraphColors() {
  const { resolved } = useTheme();
  return useMemo(() => {
    const root = getComputedStyle(document.documentElement);
    return {
      background: root.getPropertyValue("--color-background").trim(),
      foreground: root.getPropertyValue("--color-foreground").trim(),
      mutedFg: root.getPropertyValue("--color-foreground-muted").trim(),
      accent: root.getPropertyValue("--color-accent").trim(),
      success: root.getPropertyValue("--color-success").trim(),
      warning: root.getPropertyValue("--color-warning").trim(),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolved]);
}
```

관계 타입별 linkColor + linkLineDash:
- `depends_on` → foreground, solid
- `related_to` → mutedFg, [2,2]
- `implements` → accent, solid
- `references` → mutedFg, [4,4]
- `attached_to` → success, solid
- `derived_from` → warning, [6,2]

노드 타입별 nodeColor:
- document → foreground
- table → success
- file → warning

Suspense fallback = `Skeleton`.

- [ ] **Step 3: 수동 QA** — 노드 클릭 → 사이드 패널 정보, 다크 토글 시 색 전환

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/pages/graph.tsx
git commit -m "feat(page): redesign graph with theme-aware colors + dash patterns"
```

---

### Task 4.11: `public-publication.tsx` 재작성 (Fraunces editorial 유지)

**Files:**
- Rewrite: `frontend/src/pages/public-publication.tsx`

- [ ] **Step 1: 기존 읽기** — PasswordGate + 문서/테이블/파일 dispatch + expiry/view-limit 에러

- [ ] **Step 2: 재작성** — 내부 UI와 **구별되는 editorial tone**
  - **Hero title**: `.font-display-tight text-4xl` (Fraunces)
  - **본문 prose**: `<article className="prose dark:prose-invert">` + Fraunces body로 지정:
    - 해당 article 컨테이너에 inline 스타일 `style={{ fontFamily: "var(--font-display)" }}` 적용 또는 유틸 클래스 `.font-display-body` 신규 정의 (`.font-display`를 제외한 body 전용 — 400 weight, tracking normal, leading normal)
  - `.font-display-body` 유틸을 `index.css` prose 섹션 하단에 추가 (Task 1.1 편집이 아닌 별도 commit):

    ```css
    /* Publication prose body (외부 공유 전용) */
    .font-display-body {
      font-family: var(--font-display);
      font-variation-settings: "opsz" 14, "SOFT" 30;
      font-weight: 400;
      letter-spacing: 0;
      line-height: 1.75;
    }
    ```
  - 좌우 여백 넉넉, line-length 제한 (`max-w-prose` 또는 `max-w-[65ch]`)
  - PasswordGate Dialog 재사용
  - FileViewer / TableViewer 재사용
  - Footer = 발행일 + view_count · `.coord`

**주의**: `public-publication.tsx`는 `useTheme`도 동작해야 함 (다크 토글 가능). 단, `hideChrome` 로직 때문에 Layout 헤더는 없으므로 페이지 안에 작은 ThemeToggle 배치.

- [ ] **Step 3: 수동 QA** — 패스워드 gate, 문서/파일/테이블 각각 렌더, 다크모드, mobile width

- [ ] **Step 4: 커밋**

```bash
git add frontend/src/pages/public-publication.tsx
git commit -m "feat(page): redesign public publication with Fraunces editorial tone"
```

---

## Phase 5 — QA

### Task 5.1: 빌드 + 타입체크 + 기존 테스트

- [ ] **Step 1: 전체 빌드**

```bash
cd frontend
pnpm build
```
Expected: 성공, dist 생성

- [ ] **Step 2: 타입체크**

```bash
pnpm tsc --noEmit
```
Expected: 0 errors

- [ ] **Step 3: 단위 테스트**

```bash
pnpm test
```
Expected: 전부 통과 (스냅샷 갱신 필요할 수 있음 — 클래스명 변경 반영)

- [ ] **Step 4: 필요 시 스냅샷 업데이트**

```bash
pnpm test -u
```

- [ ] **Step 5: 커밋 (스냅샷 있을 경우)**

```bash
git add frontend/src/**/*.snap
git commit -m "test: update snapshots for v1.0 redesign"
```

---

### Task 5.2: Lucide 번들 크기 실측 + (필요 시) per-icon import 전환

- [ ] **Step 1: rollup-plugin-visualizer 임시 설치**

```bash
cd frontend
pnpm add -D rollup-plugin-visualizer
```

- [ ] **Step 2: `vite.config.ts`에 visualizer 플러그인 추가** (임시)

```ts
import { visualizer } from "rollup-plugin-visualizer";
// ...
plugins: [react(), tailwindcss(), visualizer({ open: true, gzipSize: true })]
```

- [ ] **Step 3: 빌드 → 번들 분석 리포트 확인**

```bash
pnpm build
```
브라우저에서 `dist/stats.html` 자동 오픈. `lucide-react` 청크 크기 확인.

- [ ] **Step 4: 판정 분기**

- lucide-react gzipped < 50KB → **유지** (Vite가 tree-shake 잘 함)
- ≥ 50KB → per-icon import로 전환 (예: `import Check from "lucide-react/dist/esm/icons/check"`)

- [ ] **Step 5: visualizer 제거**

```bash
pnpm remove rollup-plugin-visualizer
# vite.config.ts에서 visualizer import/plugin 라인 제거
```

- [ ] **Step 6: 커밋**

```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/vite.config.ts
git commit -m "chore(perf): measure lucide bundle size (<target, keep barrel imports)"
```

(전환 필요한 경우엔 그 변경도 함께 커밋)

---

### Task 5.3: 접근성 자동 검사 (axe-core)

- [ ] **Step 1: `@axe-core/cli` 설치 + Puppeteer 기반 검사 스크립트 작성**

```bash
pnpm add -D @axe-core/cli
```

- [ ] **Step 2: dev 서버 기동 상태에서 라이트 모드 검사**

```bash
pnpm dev &
sleep 3
npx @axe-core/cli http://localhost:5173 --exit
```
Expected: 0 violations (또는 경미한 것만)

- [ ] **Step 3: 다크 모드 검사** (localStorage + reload 주입)

구체 커맨드:
```bash
npx @axe-core/cli http://localhost:5173 \
  --load-delay 1500 \
  --script "localStorage.setItem('akb_theme','dark'); location.reload();" \
  --exit
```
(또는 Puppeteer 스크립트 작성해 여러 경로 순회: `/`, `/vault/<sample>`, `/search?q=test`, `/settings`, `/p/<slug>` — 각각 라이트·다크)

- [ ] **Step 4: 발견된 이슈 수정**

- [ ] **Step 5: 결과 커밋**

```bash
git commit -m "chore(a11y): fix axe-core findings (light+dark)"
```

---

### Task 5.4: 스크린샷 QA (22장)

각 페이지를 라이트·다크로 1장씩 = 11 × 2 = 22장.

- [ ] **Step 1: dev 기동, 테스트 계정 로그인**

- [ ] **Step 2: 페이지별 스크린샷** (브라우저 devtools 또는 Playwright headed)
  - `/auth` (login / register 2장)
  - `/` (home)
  - `/settings`
  - `/vault/new`
  - `/vault/:name` (내용 있는 샘플)
  - `/vault/:name/doc/:id`
  - `/search?q=test`
  - `/vault/:name/table/:name`
  - `/vault/:name/file/:id`
  - `/vault/:name/graph`
  - `/p/:slug` (샘플 퍼블리케이션)

라이트·다크 각각.

- [ ] **Step 3: 이슈 발견 시 페이지별 패치 + 재스크린샷**

- [ ] **Step 4: 스크린샷은 저장소에 커밋하지 않음** — `.gitignore` 업데이트 (혹시 빠져있으면)

```bash
# .gitignore에 *.png 관련 패턴 유지
```

---

### Task 5.5: alias 제거 + 최종 커밋

- [ ] **Step 1: `index.css`의 shadcn alias 블록 점검** — 더 이상 참조 없으면 제거 가능

```bash
cd frontend/src
grep -rn "bg-paper\|text-ink\|border-ink\|bg-whisper\|text-smoke\|text-spark\|text-ember" --include="*.tsx" --include="*.ts"
```

- [ ] **Step 2: 발견된 참조 전부 semantic 토큰으로 교체**

- [ ] **Step 3: `index.css`의 primitive `paper/ink/smoke/whisper/spark/ember` CSS 변수는 유지** (`.coord-ink`/`.coord-spark` 참조 + 과거 호환)

- [ ] **Step 4: shadcn alias 블록 (`--color-primary` 등)은 shadcn 컴포넌트가 참조하므로 유지** 

- [ ] **Step 5: 최종 빌드**

```bash
pnpm tsc --noEmit && pnpm build && pnpm test
```

- [ ] **Step 6: 최종 커밋**

```bash
git commit -m "chore(qa): remove bg-paper/text-ink aliases, complete v1.0 redesign"
```

---

### Task 5.6: PR 생성

- [ ] **Step 1: 브랜치 푸시**

```bash
git push -u origin feature/frontend-v1-redesign
```

- [ ] **Step 2: PR 생성** — 제목·요약 준비

PR 제목 후보: `feat(frontend): v1.0 redesign — IDE-native design system + dark mode`

PR 본문에 포함:
- Summary (design system 교체, dark mode, 11 pages)
- Breaking changes 없음 (A 원칙 — 기능 셋 동일)
- Screenshots (light/dark 각 주요 페이지)
- 스펙 링크 (`docs/superpowers/specs/2026-04-22-frontend-v1-redesign-design.md`)
- Test plan 체크리스트

---

## 완료 기준

모두 체크되어야 완료:

- [ ] 모든 Phase 0~5 Task 완료 + 커밋
- [ ] `pnpm build` 성공
- [ ] `pnpm tsc --noEmit` 0 errors
- [ ] `pnpm test` 전부 통과
- [ ] axe-core 0 violations (light + dark)
- [ ] 22장 스크린샷 QA 완료
- [ ] Lighthouse Performance ≥ 90, Accessibility ≥ 95 (홈 기준)
- [ ] focus ring 보이고, accent 버튼에도 offset으로 분리됨
- [ ] 트리 키보드 네비 회귀 없음
- [ ] 기능 회귀 없음 (A 원칙 — v0.5와 동일)
- [ ] PR 생성 및 리뷰 요청

---

## 참고

**스펙**: `docs/superpowers/specs/2026-04-22-frontend-v1-redesign-design.md`
**브리프**: `docs/frontend-redesign-brief.md`
**브랜치**: `feature/frontend-v1-redesign`
**기반 커밋**: `3f197a9`
