# AKB Web — Design System

Unified with the **akb-platform family** (akb-platform + seahorse-mcp-agent-server):
the Dnotitia palette, Pretendard typography, soft cool-gray surfaces, rounded
corners, glass + aurora atmosphere.

This system is **centrally governed**: one token source, a shared primitive
vocabulary, and a build guard that blocks drift. Everything below resolves from
`src/index.css` — when this doc and the code disagree, the code wins, and this
doc is wrong (fix it).

---

## 1. Identity & principles

AKB is a **Swiss-minimalist developer/agent knowledge tool** (desktop web). The
interface is calm, high-contrast, and sparsely decorated; type weight, spacing,
and hairlines carry hierarchy, and color is used with discipline.

- **One brand axis**: teal primary `#004059` + a single orange accent `#e55e2c`.
  Teal is *interactive/identity*; orange is *one marquee moment per screen*.
- **Accessibility is a floor, not a nice-to-have**: every foreground/background
  pair clears **WCAG AA (4.5:1 text / 3:1 UI)**; AAA where it's free.
- **Tokens only**: components never hardcode a color/radius/shadow — they read
  `var(--color-*)` / Tailwind token classes. A build guard enforces it.
- **Compose, don't re-roll**: build pages from the primitive vocabulary
  (`components/ui/*`), not bespoke inline markup.
- **Light = paper-cool, dark = slate.** The two themes are authored together;
  dark is a tonal re-map, never a naive inversion. Test both.
- **One atmosphere budget per view.** The glass/aurora/elevation/micro-viz kit
  (§10–§11) is a signature used sparingly, so chrome recedes and content leads.
  Per view: at most **one** masthead brand device (`.aurora-header` wash *or* a
  `.brand-gradient` wordmark *or* a leading `.feat-*` tile — not stacked), **one**
  hover-lift surface (`.card-hover`), **one** micro-viz (a rail sparkline /
  composition meter, suppressed when too sparse to read), and surfaces raised at
  most **one tier** above resting (`shadow-sm` → `shadow-md`, never higher on a
  content field). Additive, AA, and dark-correct in both themes.

---

## 2. Single source of truth — `src/index.css`

All design tokens live in the Tailwind v4 `@theme { … }` block (light) with a
`.dark { … }` override. Change a token here and the whole UI re-skins, because
every surface reads `var(--color-*)`. The dark block re-tones the *same* token
names, so a `text-link` or `bg-surface-hover` utility is correct in both modes.

### Governance — `scripts/design-check.mjs`

Runs in `npm run build` (`npm run design:check` standalone). Fails the build on:

1. **Hardcoded 6-digit hex** in component source — colors must be tokens.
2. The **`bg-foreground text-background`** slab — a pre-redesign idiom; use
   `bg-surface-2` (soft) or `bg-primary` (teal active).

Exempt: `src/index.css` (the token defs) and test/story files.

**Per-change gate:** `npm run design:check && npm run typecheck && npm run lint && npm run test`.

---

## 3. Color tokens — core ramps

| Token (`--color-…`) | Light | Dark | Role |
|---|---|---|---|
| `teal` / `primary` | `#004059` | `#0a6f86` | brand primary — headers, primary buttons, active fills |
| `teal-2` | `#0a6f86` | — | mid-teal (gradients) |
| `orange` / `accent` | `#e55e2c` | `#e55e2c` | bright accent — borders, tints, dots, glows, **decoration only on light** |
| `accent-strong` (+`-foreground` `#fff`) | `#c44a1e` | `#c44a1e` | accent for **white-text fills** (white-on-fill 4.83:1 AA) |
| `spark` | `#c44a1e` | `#f0744a` | the **fresh-token highlight** (dot + relative-time text). Split from `accent-strong` because the two have opposite dark needs: `accent-strong` stays dark so white text on a filled chip clears AA, while `spark` is used **as text on a surface** and is brightened in dark to clear AA there (6.18:1 on surface, 5.10:1 on hover). |
| `background` | `#f6f7f9` | `#0b0f14` | page canvas (cool gray) |
| `surface` | `#ffffff` | `#121821` | cards, list rows, inputs |
| `surface-2` / `surface-muted` | `#ebeef2` | `#1b2430` | insets, code headers (alias pair) |
| `foreground` | `#1d1d1f` | `#e7eaef` | body text |
| `foreground-muted` | `#5e6068` | `#9aa4af` | secondary text, coord labels |
| `subtle` | `#767a82` | `#828c98` | tertiary/meta text (AA-legal: 4.54 / 4.52:1) |
| `border` / `border-strong` | `#dfe3e8` / `#c8ced6` | `#26303c` / `#36424f` | hairlines / emphasis edge |
| `ring` | `#004059` | `#0a6f86` | `focus-visible` outline |

> **Aliases (one role, two names — keep in sync):** `surface-muted == surface-2`,
> `destructive == danger`, `good == success`. Prefer the canonical name
> (`surface-2`, `destructive`, `success`); the twins exist for back-compat and
> are being migrated out.

---

## 4. Semantic color families

Each state is a **quad**: `base` (text/border), `-foreground` (text on a filled
chip), `-soft` (tinted banner bg), `-soft-foreground` (text on the tint). Base
values are darkened in light so they clear AA **as text**. Semantic color always
pairs with an icon or label — never color as the only signal.

| Family | base (L/D) | foreground (L/D) | soft bg (L/D) | soft fg (L/D) |
|---|---|---|---|---|
| **success** | `#1e7d4b` / `#3fb27a` | `#04200d` / `#0a1f12` | `#e7f4ec` / `#13271c` | `#176b3f` / `#7fd4a6` |
| **warning** | `#9a5400` / `#d9912f` | `#fff` / `#0b0f14` | `#fdf1e3` / `#2a1d0a` | `#8a4b00` / `#e8b878` |
| **info** | `#1d627c` / `#4aa3c4` | `#fff` / `#04222e` | `#e6f0f4` / `#0f2730` | `#19566c` / `#9ed1e2` |
| **destructive**/danger | `#c42424` / `#e06464` | `#fff` / `#0b0f14` | `#fbeaea` / `#2a1212` | `#a81f1f` / `#f0a0a0` |

- **Filled** chip/button → `bg-{family} text-{family}-foreground`
  (`Badge` variants `destructive`, `success-solid`, `warning-solid`, `info-solid`).
  Note **dark warning uses dark text on fill** (white-on-`#d9912f` = 2.61:1, fails).
- **Soft** banner/callout → the `Alert` primitive (`bg-{family}-soft
  text-{family}-soft-foreground` + a tinted border).
- **Outline** chip → `border-{family} text-{family} bg-transparent`.

---

## 5. Interaction-state tokens

Solid tokens that replace the old `/opacity` and `color-mix` state hacks (those
mis-tint on the dark canvas). **Interactive = teal; hover = neutral lift;
selected = teal-tinted.**

| Token | Light | Dark | Use |
|---|---|---|---|
| `link` | `#0a6f86` | `#4aa3c4` | clickable **text** (links, row/breadcrumb hover) — `text-link` / `hover:text-link` |
| `link-hover` | `#004059` | `#8fd6ea` | link hover (darkens light, brightens dark) |
| `surface-hover` | `#f0f2f5` | `#1f2935` | row/list/ghost **hover** fill |
| `surface-active` | `#e3e7ec` | `#26303c` | **pressed** fill |
| `surface-selected` (+`-foreground` `#004059`/`#9fd4e6`) | `#e0eef2` | `#0f2a33` | **selected/current** row, active tab, current nav item — teal-tinted, never gray, never `bg-accent/10` |

Disabled state = a single value app-wide: `opacity-50` + `disabled:` semantics
(not the old four different `opacity-40/50/60/70`).

---

## 6. Color placement rules

1. **Interactive = teal, always.** Every link, link/row/breadcrumb hover, active
   nav/tab/toggle, and focus emphasis uses the teal family (`text-link` /
   `hover:text-link`, or `bg-primary` for active fills). Never `hover:text-accent`.
2. **Orange = one marquee moment per screen, fills only.** Exactly one filled
   primary CTA per view via `Button variant="accent"` (= accent-strong, 4.83:1).
   Orange may also appear as: the brand wordmark gradient, `coord-spark` eyebrow,
   tinted callout cards (`bg-accent/5 border-accent`), the hero glow, and `feat-*`
   tiles. Orange is **never** interactive text and **never** a second filled CTA.
3. **Bright `accent` `#e55e2c` = decoration only on light** (borders, 5% tints,
   dots, cluster/aurora glows, dark-mode `coord-spark`). Light orange **text**
   must use `accent-strong` (`#c44a1e`, 4.83:1) — bright orange text is 3.52:1 (fail).
4. **Selected vs hover vs pressed are distinct.** Selected = teal-tinted
   `surface-selected` (+ optional left border); hover = neutral `surface-hover`;
   pressed = `surface-active`. Selection is hue-coded, hover is lightness-coded.
5. **Semantic owns meaning, never the brand pair.** success/warning/info/
   destructive carry state and always pair color with an icon/label. Teal and
   orange are identity, not status — never use them to mean ok/error.
6. **Dataviz uses the `--color-cat-*` scale only** (no raw `hsl()`/hex islands).
7. **One warm accent per list row — the fresh-token spark.** A just-touched
   row (changed within ~1h, `isFresh` in `lib/utils`) may show exactly one warm
   accent: a `spark` dot + relative time. It decays as the change ages —
   never a permanent `NEW` badge — and is the only orange a list row may carry.
   Use the `spark` token (`text-spark`/`bg-spark`), **not** `accent-strong`: the
   spark renders as text on a surface, so it brightens in dark to clear AA there,
   whereas `accent-strong` must stay dark for white-on-fill. Always pair the dot
   with the timestamp text (color is never the sole signal).
   A type-tinted leading icon chip may tint by *kind*, but collapse many types to
   ~3 `cat` hues (never a rainbow) and **skip `cat-5`** so the type tint never
   competes with the spark; the glyph still carries the real distinction.
8. **Disabled = one value app-wide** (`opacity-50`).

---

## 7. Categorical / dataviz scale

A 6-step categorical scale on the **teal→orange brand arc + one neutral**,
**lightness-ramped** so categories stay separable under color-vision deficiency
(hue alone is not relied on). Used by graph clusters and `.feat-*` tiles — no
off-brand competing hue. `cat-5 == accent-strong`, `cat-6 == foreground-muted`.

| | cat-1 | cat-2 | cat-3 | cat-4 | cat-5 | cat-6 |
|---|---|---|---|---|---|---|
| **Light** | `#1f5a6e` | `#2f8f94` | `#4f9c7a` | `#b9791b` | `#c44a1e` | `#5e6068` |
| **Dark** | `#4aa6bd` | `#45c2c6` | `#63d29f` | `#e3a13f` | `#f0744a` | `#9aa4af` |

Floors: ≥3:1 ring/stroke contrast on the canvas; tile **fills** put white text on
the darker stop. Light/dark is handled by the token layer, not per-component math.

**Micro-viz reads as texture, not instrumentation.** An in-row / in-rail micro-viz
has no axis and no legend — exact numbers live in the `title` tooltip and the
adjacent counts; the bar/spark carries only shape. A composition bar shows
proportion (`cat-1`/`cat-3`/`cat-4` = doc/table/file) over a faint full-width
`surface-muted` track, drawn even when empty so the column always reserves its
width. A rail sparkline is **one per rail**, built from data already in state (no
extra fetch): teal (`primary`) bars with the most-recent active day tipped
`accent`, and **suppressed when too sparse to read as a shape** so a quiet account
never shows a row of dead bars. Micro-viz is decoration — `aria-hidden` bars plus
an `sr-only` summary, never the only signal.

---

## 8. Typography

| Family | Var | Used for |
|---|---|---|
| **Pretendard Variable** (bundled npm webfont) | `--font-sans` / `--font-display` | all UI, headings, masthead |
| **JetBrains Mono Variable** | `--font-mono` | code/secrets, `akb://` URIs, ids/hashes, tabular figures |

- **Body = `text-sm` (14px).** Headings via `font-display` (Pretendard 600/700,
  tight tracking, `text-foreground` — not pure black). Weights: **400 / 500 /
  600 / 700**.
- **Numbers in tables/lists/stats: `tabular-nums`** (prevents async jitter), in
  Pretendard — not monospace, and **not zero-padded** (`1`, not `01`).
- **Mono is for technical refs, not display names.** Code/secrets, `akb://`
  URIs, doc ids, and commit hashes read `font-mono`. A vault/collection **name**
  is a human display name — it reads sans (Pretendard) like the H1, breadcrumb,
  and tree, so it never flips between mono and sans across surfaces. (The one
  exception: a delete-to-confirm input whose target string must be typed
  *exactly* may render that string mono.)
- **Retired the legacy "§ coordinate" terminal/newspaper layer.** No `§` glyphs,
  no all-caps section eyebrows, no wide letter-tracking, no editorial `word.`
  mastheads (a lone colored period / italic colored last word). Section labels
  are **normal-case Pretendard** (Sentence case) via `.coord*` / `<Eyebrow>`;
  page heroes are a calm `PageHeader` (title + friendly subtitle). `.coord`
  (muted, 11px), `.coord-ink` (foreground, 12px), `.coord-spark` (muted — orange
  no longer rides the eyebrow). **Casing comes from the source string** — write
  `At a glance`, not `§ AT A GLANCE`. Keep **monospace strictly for real
  code/secrets** (wrap in `CodeSnippet`) — not for labels, paths, dates, or
  counts. `.toUpperCase()` on user/dynamic copy stays banned.
- *Roadmap:* a paired `--text-*` scale is being introduced to fold the ~140
  arbitrary `text-[Npx]` onto named steps; until then match the nearest existing
  size and avoid new arbitrary pixels.

---

## 9. Spacing & layout rhythm

- **Spacing = Tailwind's 4px ramp** (no custom `--spacing` token — the absence is
  deliberate). Section gaps `gap-y-10`, card padding `p-4`, list row `py-3`,
  dense row `py-1.5`.
- **Container**: `max-w-[1400px]` for app routes; the vault shell is full-bleed
  with a left slide-over tree.
- **Control heights**: `h-8` (32, dense rails) · `h-9` (36, default) · `h-10`
  (40, inputs) · `h-11` (44, hero CTA). Keep tappable controls ≥ 36px.
- **Borders are structural** — most surfaces are defined by 1px `border` hairlines
  plus a soft shadow, not heavy fills. `divide-y divide-border` for list rows.

---

## 10. Radius, elevation, z-index

- **Radius**: `--radius-sm .5rem` (chips, focus insets) · `-md .75` (buttons,
  inputs) · `-lg .875` (cards, panels, lists) · `-xl 1rem` (dialogs, hero) ·
  `-full 9999px` (pills, avatars, dots). Always `rounded-[var(--radius-*)]`.
- **Elevation** (3-tier rule): `shadow-xs` hairline lift · `shadow-sm` resting
  cards · `shadow-md` popover/raised tile · `shadow-lg` modal/menu · `shadow-xl`
  hover peak. Cool-tinted in light, deeper alpha in dark.
- **Z-index ladder** (reference via `z-[var(--z-*)]`): `base 0` · `raised 10` ·
  `sticky 20` · `header 40` · `overlay 50` · `modal 55` · `popover 60` ·
  `tooltip 70` · `toast 80`. One ladder so a menu/tooltip opened inside a modal
  sits above it and toasts sit above everything.

---

## 11. Motion & atmosphere

- **Tokens**: `--duration-fast 120ms` (hover/color) · `-base 220ms`
  (tabs/dropdowns) · `-slow 420ms` (page fade, modal). Easing `--ease-out`
  (entering) / `--ease-in` (exiting). Use `.transition-token` for color/shadow
  micro-transitions; `.fade-up` / `.fade-in` / `.stagger` for entrances.
- `prefers-reduced-motion: reduce` collapses every animation to 1ms — **never**
  re-introduce motion with inline styles.
- **Atmosphere** (family signature, used sparingly so chrome recedes and content
  leads): `.app-header` (near-solid surface + faint blur + hairline), `body::before`
  aurora (very low-alpha gradient mesh), `.aurora-header` (header-local wash for a
  masthead the off-screen global mesh leaves flat — static, `pointer-events:none`,
  behind the header at z-0, dark-retoned), `.hero-glow` (auth/landing only),
  `.brand-gradient` wordmark, `.feature-tile` + `.feat-*` capability tiles.
- **Glass on outer shells only.** Apply `.glass` (`--glass-bg`) to shell surfaces
  — header rails, summary cards — never to reading, input, or code surfaces.
  Because the text sits over a translucent fill, re-verify it clears AA over
  `--glass-bg` in **both** themes (it is not a fixed-contrast token). Pair glass
  with a masthead aurora (`.aurora-header`) so the wash tints the shell — the
  global `body::before` mesh is anchored off-screen and does not reach it.

---

## 12. Primitive catalog — `src/components/ui/`

Compose pages from these instead of re-writing patterns inline.

| Primitive | Use |
|---|---|
| `Button` | `default` (teal) · `accent` (orange CTA) · `outline` · `secondary` · `ghost` · `destructive` · `link`. Sizes `sm/md/lg/icon`. `loading` prop = spinner + disable + `aria-busy`. |
| `Panel` + `PanelHeader` | canonical rounded `surface` container (border + soft shadow); `inset` (default) clips divided lists. For a `.card-hover` lift (the kit lift-on-hover utility) on rows *inside* a Panel, set `inset={false}` so the row's transform/shadow isn't clipped, re-round the end rows (`[&>li:first-child>a]:rounded-t-[var(--radius-lg)]` / `…:last-child>a]:rounded-b-…`) to keep the divided look at rest, and stack the hovered row above its neighbours (`relative z-0 hover:z-10`). |
| `PageHeader` | masthead: `font-display` title + muted subtitle + actions slot. |
| `StatTile` | labelled metric tile (big tabular numeral). |
| `Eyebrow` | the `§ LABEL` mono coordinate label (`tone: muted/ink/spark`). |
| `CodeSnippet` | copyable code block with a soft header bar (insecure-origin safe). |
| `Alert` | tinted notice banner — `destructive/warning/info/success` on the `-soft` quads; assertive `role=alert` for destructive/warning, polite `role=status` otherwise; icon + text always. |
| `Badge` / `RoleBadge` / status badges | pill tags; outline + `*-solid` filled semantic variants; role/doc/system tones. |
| `VaultChip` | flat tinted **monogram** tile for a vault — a quiet identity anchor, **not** a glossy avatar or a `feat-*` hero. Swatch is a deterministic `--color-cat-*` picked by `hashHue(name) % 6` (the shared FNV-1a from `lib/utils`, §7), so a vault wears **one color** wherever its name appears — Recent rows and the vault directory. Fill `color-mix(in srgb, <cat> 14%, transparent)`, `rounded-[var(--radius-sm)]`; `sm` (`h-5 w-5`) rides inline in a row, `md` (`h-7 w-7`) anchors a directory row. `aria-hidden` — the readable name always leads. |
| `Input` / `Textarea` / `Select` / `Label` / `TagInput` | form primitives — pre-rounded, teal focus ring, `aria-[invalid]` hooks. |
| `Dialog` / `ConfirmDialog` | overlay primitives; `ConfirmDialog` surfaces a rejected `onConfirm` inline (`Alert`) and stays open for retry. |
| `Tabs` / `Tooltip` / `Skeleton` | segmented control / hint / loading placeholder. |
| `Logo` + `.feature-tile`/`.feat-*` | brand lockup + per-capability gradient tiles. |

Shell: `Layout` (glass `app-header` + content) and `VaultShell` (header + content,
collection tree in a left slide-over toggled by the Tree button / ⌘\).

*Roadmap primitives* (high-drift inline patterns being extracted): `IndexRow`
(numbered list row), `ToggleGroup`/`ToggleChip` (segmented selection),
`MetaList`/`MetaItem` (rail `dl`), `LoadingState` (`role=status` loading line +
skeleton), `InlineCode` (single-token mono chip). Until shipped, match the
existing inline pattern and flag for extraction.

---

## 13. Accessibility floor

| Rule | Contract |
|---|---|
| **Contrast** | text ≥ 4.5:1, large/UI ≥ 3:1, in **both** modes. Tokens are pre-verified — re-verify when you adjust one. |
| **Color not the only signal** | pair every status color with an icon or text label. |
| **Focus ring** | every interactive element keeps the `focus-visible:ring-2 ring-ring ring-offset-2` pattern (icon buttons included). Never remove it. |
| **Icon-only button** | `aria-label` required + `<Icon aria-hidden />`. |
| **Labels** | every input has a visible `<Label>` or an `sr-only` label; placeholder is not a label. |
| **Async / loading** | wrap loading + show-once secrets in `role=status aria-live=polite`; surface errors with `role=alert` (the `Alert` primitive). |
| **Destructive action** | `ConfirmDialog`, never `window.confirm()`. |
| **Reduced motion** | respected globally — don't override. |

---

## 14. Anti-patterns

- ❌ Raw hex / `hsl()` islands in components (use tokens; the guard blocks hex).
- ❌ New arbitrary `text-[Npx]` / `leading-[…]` (use the scale / nearest step).
- ❌ `/opacity` or `color-mix` as a *state* (use `surface-hover/active/selected`).
- ❌ A second marquee orange CTA, or orange as interactive **text**.
- ❌ `bg-accent/10` for "selected" (use `surface-selected`).
- ❌ Color as the only signal; `.toUpperCase()` on user-facing copy.
- ❌ Sibling lists aligned differently (one `items-baseline`, one `items-center`)
  — parallel lists share one vertical-alignment + row grammar.
- ❌ `rounded-*`/`shadow-*` bare values (use the token scale).
- ❌ `bg-foreground text-background` slab (the guard blocks it).
- ❌ `window.confirm()` / `alert()`; placeholder-only labels; removed focus rings.

---

## 15. Building a new page

1. Read this file.
2. `PageHeader` for the masthead → `Panel` / `PanelHeader` for sections.
3. Compose from the primitive catalog (§12) before writing inline markup.
4. Colors/radii/shadows from tokens only — teal for interactive, **one** orange
   CTA, semantic + icon for status, `surface-selected` for current.
5. Loading/empty/error are three distinct states; secrets + async use `role=status`.
6. Run the gate: `npm run design:check && npm run typecheck && npm run lint && npm run test`.
