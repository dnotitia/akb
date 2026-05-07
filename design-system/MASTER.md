# AKB Design System — MASTER

> Single source of truth for visual & interaction design across the AKB
> frontend (`/frontend/src`). When building a specific page, also check
> `design-system/pages/<page>.md` for page-level overrides.

## 1. Identity

**AKB = "editorial archive" for agents.** The interface looks and reads
like a typeset newspaper or research journal, not a SaaS dashboard.

| Pillar | Expression |
|--------|-----------|
| **Voice** | Quiet, archival, documentary. Lowercase prose; uppercase coord labels for chrome. |
| **Hierarchy** | Section numerals (`§ 01`, `§ 02`), hairline rules, tabular numerics. Type weight + spacing carry hierarchy — not color. |
| **Surface** | Paper-toned light mode (`#faf9f5`); deep slate dark mode (`#0f172a`). Sharp corners (radius `0` everywhere). |
| **Accent** | Single hot accent — `#ff4d12` "spark" — used sparingly: focus rings, current-route, primary CTAs, fresh-token highlights. Never decorative. |
| **Avoid** | Rounded corners, drop-shadow chrome, gradient hero blobs, emoji glyphs, multi-accent palettes, marketing-style oversized type. |

## 2. Color Tokens

All colors live as CSS custom properties in `frontend/src/index.css`
under `@theme` (light) and `.dark` (override). **Never hardcode hex in
components — always reference tokens.**

### Semantic tokens (use these in components)

| Token | Light | Dark | Use for |
|-------|-------|------|---------|
| `--color-background` | `#faf9f5` paper | `#0f172a` slate | Page bg |
| `--color-surface` | `#ffffff` | `#1b2336` | Cards, list rows, inputs |
| `--color-surface-muted` | `#ecebe6` whisper | `#272f42` | Hover, code blocks, soft chips |
| `--color-foreground` | `#0a0908` ink | `#f8fafc` | Body text |
| `--color-foreground-muted` | `#75716b` smoke | `#94a3b8` | Coord labels, secondary text |
| `--color-border` | `#0a0908` (ink) | `#334155` | Default 1px hairlines |
| `--color-border-strong` | same as ink | `#475569` | Emphasis edge |
| `--color-accent` | `#ff4d12` spark | `#ff4d12` | Focus, current, primary CTA |
| `--color-accent-foreground` | paper | slate | Text on accent fills |
| `--color-success` | `#16a34a` | `#22c55e` | Healthy state |
| `--color-warning` | `#ca8a04` | `#eab308` | Pending / needs setup |
| `--color-destructive` | `#c63d09` ember | `#ef4444` | Revoke, delete, error |
| `--color-info` | `#3b82f6` | `#60a5fa` | Neutral info chip |
| `--color-ring` | `= accent` | `= accent` | `focus-visible` outline |

### Rules

1. **Light = paper, dark = slate.** Don't invert — palettes are authored independently.
2. **Borders are a primary visual element.** Most surfaces are defined by hairlines, not fills. In light mode, default border = ink (#0a0908) — strong by design.
3. **One accent.** The spark orange is the only saturated color in the system besides semantic states. Fresh PAT highlight, focus ring, current-route are all the same accent.
4. **Status uses `info / warning / destructive / success`** — not raw hex. Dark mode lifts each by ~10% lightness.

## 3. Typography

Four families, each with a job. **Never** introduce a fifth without a brief.

| Family | Var | Used for | Notes |
|--------|-----|----------|-------|
| **IBM Plex Sans** (300/400/500/600/700) | `--font-sans` | All UI body, controls, h1–h6 | `font-feature-settings: "ss01","ss02"` for archival figures |
| **IBM Plex Serif** (400/500/600 + italics) | `--font-serif` | Vault titles, doc H1 reading-focus | Warm, Obsidian-like |
| **Fraunces Variable** | `--font-display` | Editorial display: home masthead, auth, publication body | Optical-size 144 for headings, 14 for prose |
| **JetBrains Mono Variable** | `--font-mono` | Coord labels, code blocks, vault names, tabular columns | `"zero","ss01"` for slashed zero |

### Utility classes (already defined)

```
.font-display          /* opsz 144, w400, ls -0.025, lh 0.95 — masthead */
.font-display-tight    /* opsz 144, w500, ls -0.04, lh 0.92 — auth/home tagline */
.font-display-body     /* opsz 14, w400, lh 1.75 — publication prose */
.font-serif            /* w500, ls -0.015 — vault titles */
.font-serif-italic     /* w400 italic — captions */
.font-mono             /* with feature-settings */

.coord                 /* mono 10px, w500, uppercase, ls .12em, fg-muted — chrome label */
.coord-ink             /* same but full ink */
.coord-spark           /* same but accent + w600 — momentary highlights */
```

### Type scale (use Tailwind tokens)

```
text-[10px]  coord, badges, dense rows
text-xs      list metadata
text-sm      body, controls, labels  ← default
text-base    standard prose, large CTA
text-xl      section h2 in shells
text-3xl/4xl masthead taglines (responsive)
```

### Rules

- **Body = sans 14px (`text-sm`).** Long-form reading prose = display-body 16px+.
- **Numbers in tables, lists, stats: always `tabular-nums`.** Prevents jitter on async updates.
- **All-caps is reserved for `.coord*` chrome and tab labels.** Never set body content to uppercase via JS (`.toUpperCase()` is an anti-pattern in error/help text).
- **No emojis as icons.** Use Lucide React. Filled vs outline = one set; stroke 1.5px (Lucide default).

## 4. Layout & Spacing

- **Container**: `max-w-[1400px]` for non-vault routes; vault shell is full-bleed.
- **Spacing rhythm**: Tailwind's 4px scale. Section gaps `gap-y-10`, card padding `p-4`, list row `py-3`, dense row `py-1.5`.
- **Radius**: `--radius-* = 0`. Sharp corners are part of the identity. **Don't add `rounded-*` utilities.** (Sole exception: thin scrollbar thumb `border-radius: 3px`.)
- **Borders**: Default = 1px solid `--color-border`. Use `.hairline-{t,b,l,r}` for single-edge rules. `divide-y divide-border` for list separators.
- **Z-index**: Header = `z-40`. Modals via Radix (auto). Tooltip via Radix. Avoid bespoke z-index.

## 5. Motion

| Token | Value | Use |
|-------|-------|-----|
| `--duration-fast` | 150ms | Hover, color transitions |
| `--duration-base` | 200ms | Tabs, dropdowns |
| `--duration-slow` | 300ms | Page fade-up, modal enter |
| `--ease-out` | `cubic-bezier(0,0,.2,1)` | Entering |
| `--ease-in` | `cubic-bezier(.4,0,1,1)` | Exiting |

Pre-built classes: `.fade-in`, `.fade-up`, `.stagger > *` (each child cascades 40ms). `prefers-reduced-motion: reduce` already collapses every animation to 1ms — **don't bypass this with inline styles**.

## 6. Components — Canonical Variants

### Button (`@/components/ui/button`)
| Variant | When |
|---------|------|
| `default` | Primary action when accent is too loud (dark fill on light surface) |
| `accent` | Marquee CTA: "Mint token", "Create first vault" |
| `outline` | Secondary action ("New vault", "Cancel") |
| `secondary` | Tertiary on muted surfaces |
| `ghost` | In-row trash, low-emphasis |
| `destructive` | Confirmed irreversible action |
| `link` | Inline text link (rare — prefer underlined `<Link>`) |

Sizes: `sm` (32×—) for dense rails, `default` (36×—) for forms, `lg` (44×—) for hero CTAs, `icon` (36×36).

### Input (`@/components/ui/input`)
Single height `h-10` (40px). Sharp border. Focus ring = 2px accent + 2px offset.

### Coord strip (header pattern, see `layout.tsx:76`)
Use for any page chrome bar:
```jsx
<div className="border-b border-border">
  <div className="mx-auto flex max-w-[1400px] justify-between px-6 py-1">
    <div className="coord">§ AKB · Agent Knowledgebase</div>
    <div className="coord">v1.0</div>
  </div>
</div>
```

### Section header pattern
Used on home / vault / settings. Always: `coord-ink §` + `text-xl font-semibold` + `coord [count]` + bottom hairline.

```jsx
<header className="flex items-baseline gap-3 pb-3 border-b border-border">
  <span className="coord-ink">§ 01</span>
  <h2 className="text-xl font-semibold tracking-tight">Recent activity</h2>
  <span className="coord tabular-nums">[{recent.length}]</span>
</header>
```

### List rows
Grid layout with leading numeric coord, mono identifier, title + path stack, trailing time.
- Hover: `bg-surface-muted` + title turns `text-accent`
- Trailing chevron: opacity-40 → 100 + `translate-x-0.5` on hover (already on home)

## 7. Accessibility Floor

| Rule | Enforcement |
|------|-------------|
| Touch target ≥ 36px (interface), 44px (mobile gestures) | Lift any `h-6 / h-7` button to at least `h-9` for tappable controls; or pad with `hitSlop` equivalent (extra padding) |
| Visible label per input | `<Label htmlFor>` or `<label className="sr-only">` paired |
| Icon-only button | `aria-label` required + `<Icon aria-hidden />` |
| Focus ring | Always use the `focus-visible:ring-ring` pattern from button.tsx; never remove |
| Destructive action | Custom `<Dialog>` confirm — never `window.confirm()` |
| Color is never the only signal | Pair status color with an icon or coord text |
| `prefers-reduced-motion` | Already collapses all animations — never override with inline `transition` |
| Contrast | All foreground/foreground-muted pairs verified ≥ 4.5:1 in both modes; verify when adjusting tokens |

## 8. Right-rail summary block (home/vault)

Repeating pattern: `border + bg-surface` card with a `coord-ink` strip header and a `divide-y` body. Each row links to its detail page. Use this for "AT A GLANCE", "CONNECT", "STATS" panels — never use a card with shadows.

## 9. Anti-patterns (don't do)

- ❌ `rounded-md`, `rounded-full`, `shadow-lg` — kills the editorial identity
- ❌ Multi-color status palettes (use `info / warning / success / destructive`)
- ❌ Emoji icons (`🚀`, `🎨`, `⚙️`) — use Lucide SVG
- ❌ Native `confirm()` / `alert()` — use the dialog component
- ❌ All-caps user-facing copy (`error.toUpperCase()`) — only `.coord` chrome is uppercase
- ❌ Placeholder-only labels — pair with `<Label>` or `sr-only` label
- ❌ Touch targets `h-6` / `h-7` for tappable controls
- ❌ Custom font without justification — 4 families is the budget
- ❌ Inline hex colors — use semantic tokens
- ❌ `cursor: not-allowed` without explanation tooltip

## 10. When building a new page

1. Read this file.
2. Check `design-system/pages/<your-page>.md`. If present, its rules override.
3. Mirror the closest existing page (`home.tsx` for index, `document.tsx` for reading, `settings.tsx` for forms, `vault.tsx` for shell-content).
4. Use `Button`, `Input`, `Tabs`, `Dialog` from `components/ui` — extend variants only after a brief.
5. Run through §7 checklist before opening a PR.
