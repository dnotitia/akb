# AKB Web — Design System

Unified with the **akb-platform family** (akb-platform + seahorse-mcp-agent-server):
the Dnotitia palette, Pretendard typography, soft cool-gray surfaces, 12px radius,
glass + aurora atmosphere.

This system is **centrally governed**: one token source, a shared primitive
vocabulary, and a build guard that blocks drift.

## 1. Single source of truth — `src/index.css`

All design tokens live in the Tailwind v4 `@theme { … }` block (+ a `.dark`
override). **Nothing else in the app hardcodes a color.** Change a token here and
the whole UI re-skins, because every surface reads `var(--color-*)`.

| Token | Light | Role |
|---|---|---|
| `--color-teal` / `--color-primary` | `#004059` | brand primary — headers, links, primary buttons |
| `--color-teal-2` | `#0a6f86` | mid-teal (gradients, dark primary) |
| `--color-orange` / `--color-accent` | `#E55E2C` | accent — CTAs (create/publish), toggles, highlights |
| `--color-background` | `#f6f7f9` | page canvas (cool gray) |
| `--color-surface` | `#ffffff` | cards |
| `--color-surface-2` / `--color-surface-muted` | `#ebeef2` | insets, hover, code headers |
| `--color-foreground` / `-muted` / `--color-subtle` | `#1d1d1f` / `#5e6068` / `#94989e` | text ramp |
| `--color-border` / `-strong` | `#dfe3e8` / `#c8ced6` | hairlines |
| `--color-success` / `--color-warning` / `--color-danger` | | semantic |
| `--radius` / `-sm` / `-md` / `-lg` | `.75rem` / `.5` / `.75` / `.75` | corner rounding |

Fonts: `--font-sans` = **Pretendard** (bundled npm webfont), `--font-mono` = JetBrains Mono.
`--font-display` / `--font-serif` also map to Pretendard (family unification).

Signature utilities (also in index.css): `.glass`, `.app-header`, `body::before`
aurora, `.brand-gradient`, `.card-hover`, `.feature-tile` + `.feat-*`, `.coord*`
mono eyebrows, `.animate-in` / `.fade-up`.

## 2. Primitive vocabulary — `src/components/ui/`

Compose pages from these instead of re-writing patterns inline:

| Primitive | Use |
|---|---|
| `Button` | `default` (teal) · `accent` (orange CTA) · `outline` · `ghost` · `destructive` · `link` |
| `Panel` + `PanelHeader` | the canonical rounded `surface` container (border, soft shadow) |
| `PageHeader` | canonical masthead: `font-display` title + muted subtitle + actions slot |
| `StatTile` | labelled metric tile (big tabular numeral) |
| `Eyebrow` | the `§ LABEL` mono coordinate label (`tone: muted/ink/spark`) |
| `CodeSnippet` | copyable code block with a soft header bar |
| `Badge` / `RoleBadge` / status badges | pill tags, role/status tones |
| `Input` / `Textarea` / `Select` / `Dialog` / `Tabs` | form + overlay primitives (pre-rounded, teal focus ring) |
| `Logo` + `.feature-tile`/`.feat-*` (CSS) | brand lockup + per-capability gradient tiles |

Shell: `Layout` (glass `app-header` + content) and `VaultShell` (header + content,
collection tree in a left **slide-over** toggled by the Tree button / ⌘\).

## 3. Governance — `scripts/design-check.mjs`

Runs in `pnpm build` (`pnpm design:check` to run standalone). Fails the build on:

1. **Hardcoded 6-digit hex** in component source — colors must be tokens.
2. The **`bg-foreground text-background`** slab — a pre-redesign idiom; use
   `bg-surface-2` (soft) or `bg-primary` (teal active).

Exempt: `src/index.css` (the token defs) and test/story files.

## 4. Rules of thumb

- Container/card → `Panel` (or `rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm`); divided lists add `overflow-hidden`.
- Page top → `PageHeader`. Section label → `Eyebrow` / `PanelHeader`.
- Primary action = teal `Button`; create/publish = `accent`. Never a raw dark slab.
- Color/radius come from tokens only — the guard enforces it.
