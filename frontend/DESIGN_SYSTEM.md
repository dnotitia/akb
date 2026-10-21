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
  Teal is _interactive/identity_; orange is _one marquee moment per screen_.
- **Product identity is AKB — Agent Knowledgebase.** Internal app navigation
  shows the mark and AKB wordmark without a repeated subtitle. Keep the full
  product name in the Home link's accessible label and compact-rail tooltip.
  Sign-in and public pages may retain the full product subtitle as introduction.
  Document-flow pages such as Home retain a quiet footer: `© Dnotitia` left,
  `Agent Knowledgebase` right, aligned with the body gutters in both themes and
  on mobile. Floating actions clear the footer when it enters the viewport.
  Full-height Vault and Search workspaces omit the footer to preserve their
  scrolling model. Sign-in and public surfaces retain company attribution.
  Routine chrome does not display a release version or another product's name
  as AKB's identity.
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
  Per view: at most **one** masthead brand device (`.aurora-header` wash _or_ a
  `.brand-gradient` wordmark _or_ a leading `.feat-*` tile — not stacked), **one**
  hover-lift surface (`.card-hover`), **one** micro-viz (a rail sparkline /
  composition meter, suppressed when too sparse to read), and surfaces raised at
  most **one tier** above resting (`shadow-sm` → `shadow-md`, never higher on a
  content field). Additive, AA, and dark-correct in both themes.

---

## 2. Single source of truth — `src/index.css`

All design tokens live in the Tailwind v4 `@theme { … }` block (light) with a
`.dark { … }` override. Change a token here and the whole UI re-skins, because
every surface reads `var(--color-*)`. The dark block re-tones the _same_ token
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

| Token (`--color-…`)                     | Light                 | Dark                  | Role                                                                                                                                                                                                                                                                                                                                 |
| --------------------------------------- | --------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `teal` / `primary`                      | `#004059`             | `#0a6f86`             | brand primary — headers, primary buttons, active fills                                                                                                                                                                                                                                                                               |
| `teal-2`                                | `#0a6f86`             | —                     | mid-teal (gradients)                                                                                                                                                                                                                                                                                                                 |
| `orange` / `accent`                     | `#e55e2c`             | `#e55e2c`             | bright accent — borders, tints, dots, glows, **decoration only on light**                                                                                                                                                                                                                                                            |
| `accent-strong` (+`-foreground` `#fff`) | `#c44a1e`             | `#c44a1e`             | accent for **white-text fills** (white-on-fill 4.83:1 AA)                                                                                                                                                                                                                                                                            |
| `spark`                                 | `#c44a1e`             | `#f0744a`             | the **fresh-token highlight** (dot + relative-time text). Split from `accent-strong` because the two have opposite dark needs: `accent-strong` stays dark so white text on a filled chip clears AA, while `spark` is used **as text on a surface** and is brightened in dark to clear AA there (6.18:1 on surface, 5.10:1 on hover). |
| `background`                            | `#f6f7f9`             | `#0b0f14`             | page canvas (cool gray)                                                                                                                                                                                                                                                                                                              |
| `surface`                               | `#ffffff`             | `#121821`             | cards, list rows, inputs                                                                                                                                                                                                                                                                                                             |
| `surface-2` / `surface-muted`           | `#ebeef2`             | `#1b2430`             | insets, code headers (alias pair)                                                                                                                                                                                                                                                                                                    |
| `foreground`                            | `#1d1d1f`             | `#e7eaef`             | body text                                                                                                                                                                                                                                                                                                                            |
| `foreground-muted`                      | `#5e6068`             | `#9aa4af`             | secondary text, coord labels                                                                                                                                                                                                                                                                                                         |
| `subtle`                                | `#767a82`             | `#828c98`             | tertiary/meta text (AA-legal: 4.54 / 4.52:1)                                                                                                                                                                                                                                                                                         |
| `border` / `border-strong`              | `#dfe3e8` / `#c8ced6` | `#26303c` / `#36424f` | hairlines / emphasis edge                                                                                                                                                                                                                                                                                                            |
| `ring`                                  | `#004059`             | `#0a6f86`             | `focus-visible` outline                                                                                                                                                                                                                                                                                                              |

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

| Family                 | base (L/D)            | foreground (L/D)      | soft bg (L/D)         | soft fg (L/D)         |
| ---------------------- | --------------------- | --------------------- | --------------------- | --------------------- |
| **success**            | `#1e7d4b` / `#3fb27a` | `#04200d` / `#0a1f12` | `#e7f4ec` / `#13271c` | `#176b3f` / `#7fd4a6` |
| **warning**            | `#9a5400` / `#d9912f` | `#fff` / `#0b0f14`    | `#fdf1e3` / `#2a1d0a` | `#8a4b00` / `#e8b878` |
| **info**               | `#1d627c` / `#4aa3c4` | `#fff` / `#04222e`    | `#e6f0f4` / `#0f2730` | `#19566c` / `#9ed1e2` |
| **destructive**/danger | `#c42424` / `#e06464` | `#fff` / `#0b0f14`    | `#fbeaea` / `#2a1212` | `#a81f1f` / `#f0a0a0` |

- **Filled** chip/button → `bg-{family} text-{family}-foreground`
  (`Badge` variants `destructive`, `success-solid`, `warning-solid`, `info-solid`).
  Note **dark warning uses dark text on fill** (white-on-`#d9912f` = 2.61:1, fails).
- **Soft** banner/callout → the `Alert` primitive (`bg-{family}-soft
text-{family}-soft-foreground` + a tinted border).
- **Outline** chip → `border-{family} text-{family} bg-transparent`.

---

## 5. Interaction-state tokens

Home uses the Paper + Teal surface arrangement: the complete route
canvas (including the global header and gutters) uses `surface`, not a floating white content card on
`background`. The shared full-height app sidebar uses the standard `surface`
token: white in light mode, neutral slate in dark mode. Standard text, border,
hover, focus, and teal-selected tokens apply in both expanded and collapsed states.
The common header's `paper` surface variant preserves its bottom hairline,
geometry, search and account controls; it adds no separate colored title band.
Session-verification loading uses the same route surface and 56px header height
so authentication does not flash a contrasting canvas or move that boundary.
The optional floating connection
guide uses the opaque `surface` with a restrained raised edge. Cards and updates stay on `surface`, bounded by
quiet hairlines. Teal/orange interaction semantics do not change. This
Home canvas arrangement must not recolor Settings or Vault content surfaces.

Solid tokens that replace the old `/opacity` and `color-mix` state hacks (those
mis-tint on the dark canvas). **Interactive = teal; hover = neutral lift;
selected = teal-tinted.**

| Token                                                   | Light     | Dark      | Use                                                                                                                                                   |
| ------------------------------------------------------- | --------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `link`                                                  | `#0a6f86` | `#4aa3c4` | clickable **text** (links, row/breadcrumb hover) — `text-link` / `hover:text-link`                                                                    |
| `link-hover`                                            | `#004059` | `#8fd6ea` | link hover (darkens light, brightens dark)                                                                                                            |
| `surface-hover`                                         | `#f0f2f5` | `#1f2935` | row/list/ghost **hover** fill                                                                                                                         |
| `surface-active`                                        | `#e3e7ec` | `#26303c` | **pressed** fill                                                                                                                                      |
| `surface-selected` (+`-foreground` `#004059`/`#9fd4e6`) | `#e0eef2` | `#0f2a33` | **selected/current** row, active tab, current nav item — teal-tinted, never gray, never `bg-accent/10`                                                |
| `workspace-section`                                     | `#edf5f7` | `#11272f` | legacy teal-tinted section surface; new Vault workspaces prefer neutral `surface-2` layering plus a `TonalIcon` so teal remains an interaction signal |

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
6. **Categories and dataviz use the `--color-cat-*` scale only** (no raw
   `hsl()`/hex islands). In operational UI, categorical color belongs on a
   compact glyph chip or micro-viz—not across a whole section header—and the
   adjacent icon/text remains the primary label.
7. **One warm accent per list row — the fresh-token spark.** A just-touched
   row (changed within ~1h, `isFresh` in `lib/utils`) may show exactly one warm
   accent: a `spark` dot + relative time. It decays as the change ages —
   never a permanent `NEW` badge — and is the only orange a list row may carry.
   Use the `spark` token (`text-spark`/`bg-spark`), **not** `accent-strong`: the
   spark renders as text on a surface, so it brightens in dark to clear AA there,
   whereas `accent-strong` must stay dark for white-on-fill. Always pair the dot
   with the timestamp text (color is never the sole signal).
   A type-tinted leading icon chip may tint by _kind_, but collapse many types to
   ~3 `cat` hues (never a rainbow) and **skip `cat-5`** so the type tint never
   competes with the spark; the glyph still carries the real distinction.
8. **Disabled = one value app-wide** (`opacity-50`).

---

## 7. Categorical / dataviz scale

A 6-step categorical scale on the **teal→orange brand arc + one neutral**,
**lightness-ramped** so categories stay separable under color-vision deficiency
(hue alone is not relied on). Used by graph clusters, `.feat-*` tiles, and
small `TonalIcon` category markers — no
off-brand competing hue. `cat-5 == accent-strong`, `cat-6 == foreground-muted`.

|           | cat-1     | cat-2     | cat-3     | cat-4     | cat-5     | cat-6     |
| --------- | --------- | --------- | --------- | --------- | --------- | --------- |
| **Light** | `#1f5a6e` | `#2f8f94` | `#4f9c7a` | `#b9791b` | `#c44a1e` | `#5e6068` |
| **Dark**  | `#4aa6bd` | `#45c2c6` | `#63d29f` | `#e3a13f` | `#f0744a` | `#9aa4af` |

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

| Family                                        | Var                              | Used for                                                 |
| --------------------------------------------- | -------------------------------- | -------------------------------------------------------- |
| **Pretendard Variable** (bundled npm webfont) | `--font-sans` / `--font-display` | all UI, headings, masthead                               |
| **JetBrains Mono Variable**                   | `--font-mono`                    | code/secrets, `akb://` URIs, ids/hashes, tabular figures |

- **Body = `text-sm` (14px).** Headings via `font-display` (Pretendard 600/700,
  tight tracking, `text-foreground` — not pure black). Weights: **400 / 500 /
  600 / 700**.
- **Compact workspace UI = `text-ui` (13px / 20px).** Vault Overview and its
  administration ledgers use this step for names, labels and short descriptions,
  with 14px section titles and 12px metadata. Search result copy stays 14px and
  document prose stays 16px. The `--text-ui` token is registered as a font-size
  group in `cn` so foreground colors and primitive overrides remain independent.
  Compact administration controls are 32px on wide, fine-pointer screens and
  at least 44px on narrow or coarse-pointer screens. Icon controls opt into
  their square size explicitly; a text button must retain its natural width.
  Compact controls do not imply compressed page margins: Settings, Members and
  Public links keep 16px mobile / 24px desktop route gutters, 16px between
  their title divider and panel, 20px desktop form insets and 12px row padding.
- **Numbers in tables/lists/stats: `tabular-nums`** (prevents async jitter), in
  Pretendard — not monospace, and **not zero-padded** (`1`, not `01`).
- **Mono is for technical refs, not display names.** Code/secrets, `akb://`
  URIs, doc ids, and commit hashes read `font-mono`. A vault/collection **name**
  is a human display name — it reads sans (Pretendard) like the H1, breadcrumb,
  and tree, so it never flips between mono and sans across surfaces. (The one
  exception: a delete-to-confirm input whose target string must be typed
  _exactly_ may render that string mono.)
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
- _Roadmap:_ a paired `--text-*` scale is being introduced to fold the ~140
  arbitrary `text-[Npx]` onto named steps; until then match the nearest existing
  size and avoid new arbitrary pixels.

---

## 9. Spacing & layout rhythm

- **Spacing = Tailwind's 4px ramp** (no custom `--spacing` token — the absence is
  deliberate). Section gaps `gap-y-10`, card padding `p-4`, list row `py-3`,
  dense row `py-1.5`.
- **Application shell**: on desktop the full-height, fixed app sidebar owns
  the AKB logo and wordmark. The right-side header spans only the working
  area; its right-aligned Search and account controls stay stable when the rail
  changes density. The desktop header's left side holds a compact 14px current
  page label on ordinary routes. Named Vault routes instead show the Vault /
  current-page breadcrumb; resources use the resolved Vault / Collection /
  human-title trail. Breadcrumbs remain location-only: the Vault crumb opens
  Overview and the current page or human title is passive text. On the Vault root,
  show only its name as the current-page link, without an Overview label or slash.
  Breadcrumbs contain no page-menu chevron, Navigate button or destination
  dropdown. Overview owns four
  scoped page links in its summary: Graph, Public links, Members and Settings.
  On those four task pages, the parent Vault link becomes a quiet teal
  `ArrowLeft / Vault name` return affordance, followed by the passive current
  page label. Its entire 44px-high target returns to that Vault's canonical root,
  including on direct visits; it does not replay arbitrary browser history.
  Hover/focus reveals `Back to {Vault name}`. Keep native modified clicks,
  keyboard focus and the workspace's unsaved-change guards. This replaces the
  parent link's Box glyph rather than adding another button or navigation row.
  No Overview self-link or duplicate Search is added. A user returns to Overview
  through the Vault crumb or Vault list before selecting another destination. An
  ordinary page label is preceded by a quiet 16px Lucide page icon, matching its sidebar entry;
  the adjacent text supplies its accessible name, so the glyph is decorative. It is
  location chrome, not another H1 or a card. Long names truncate without
  displacing account actions. Resource readers and focused Vault tasks add no
  section-navigation row. Content exploration still keeps Collections; focused
  tasks omit that explorer independently of page navigation. All named
  Vault pages share `VaultBreadcrumb` with resource readers: one leading Box
  (the four focused tasks use the return arrow described above),
  text-only path and current label, matching separators, spacing and emphasis.
  Ordinary Vault sections do not repeat their tab icon in the trail; only
  Document / File / Table titles have a trailing kind marker, including loading
  fallbacks. Standalone Home and Search retain their page icon.
  Mobile keeps the logo and a compact
  location row with a labelled navigation drawer toggle below the app header.
  All desktop routes use a 20px header inset after the last navigation rail,
  independent of their content gutters. Home keeps its generous body
  inset without shifting the location label. Account Settings opens above the
  current workspace, leaving its location label unchanged. Section names, icons,
  and permission fallback share one contract inside the dialog.
  The expanded rail uses a single-line AKB wordmark beside the mark without changing
  its 56px header or the logo's fixed anchor. Loading uses the same lockup.
  A collapsed rail retains the logo symbol and a Home link with the full product
  name in its accessible label and keyboard/pointer tooltip. Mobile retains its full-width header with the AKB mark/wordmark and compact
  navigation, without the long product subtitle. Between `sm` and `lg`, Search
  can shrink within the remaining header width; the account control must remain
  fully inside the viewport. Desktop Search keeps its 256px width and the
  neutral `Search knowledge…` label on every route, with `Search knowledge` as
  its accessible name when narrow layouts show only the search glyph. Show the
  current Vault and scope selector only inside the opened search modal; named
  Vault routes still default to searching that Vault. The global row and Vault
  section navigation in Vault workspaces share the opaque `surface` token
  (white in light mode, slate in dark), with a bottom hairline separating each
  navigation level. Keep the global header's bottom
  hairline visible on every Vault route, including the unselected Vault index;
  preserve the 56px header and full-height navigation rail alignment. The
  shared Vault menu replaces the section row on every named Vault route.
  Home instead uses the common header's `paper`
  variant to continue its `surface` canvas. Full Search routes also use `paper`.
  They retain the common page identity,
  global quick-search trigger and profile; their editable query belongs to the
  page command row below. Legacy Account Settings deep links use Home behind
  the dialog, with a `paper` header (including session verification). The dialog's
  title, navigation and content share `surface`, without a gray title band. Other workspace routes retain their
  existing treatment. Global tools share 36px controls and small
  token radii. The account trigger is a quiet avatar/name/chevron disclosure,
  not a raised outlined card; keep the name from `sm`, with full identity in the
  accessible name, tooltip and open menu. Do not add unrelated shortcut buttons.
  In a Vault header narrower than 44rem of actual available width, including
  zoomed layouts, keep the account avatar and hide its repeated name/chevron
  before sacrificing the resource location. Its full accessible name, title
  and opened account menu remain unchanged; ordinary widths keep the name.
  Loading chrome reserves the same rail width as the destination route.
  From `lg`, a compact passive `N indexing` badge immediately left of Search
  reports pending search chunks across accessible Vaults. It is not a button and
  disappears when no fresh positive count is known; it reserves no idle slot.
  Do not add a status panel, profile-menu entry, or shrink the profile for it.
  Sum only fresh `search_index.pending` counts: retries are already included,
  abandoned is not subtracted, and metadata, File, and revision work stay separate.
  If some indexing counts are unavailable, show the known subtotal as `N+ indexing`
  with a title and screen-reader explanation; absence is not an all-clear claim.
  Versioned paused/unknown stages and stale observations do not contribute.
  Legacy pending counts remain usable without a declared worker mode. No
  system-wide `/health` or `/stats` data enters this surface.
  The sidebar is a 13rem labelled entry rail on ordinary routes and
  can be collapsed to a persistent 3.5rem icon rail with an explicitly labelled
  toggle. The ordinary-route choice is remembered locally. Entering the Vault
  workspace from another route starts the same 3.5rem icon rail beside the
  existing Vault/Collections navigator. A manual expansion lasts throughout
  that continuous Vault visit, including section and Back/Forward navigation.
  Leaving restores the ordinary-route preference; re-entry starts compact.
  Route boundaries—not link click order—own this behavior. Logo, primary and
  support glyphs retain fixed horizontal/vertical anchors when labels collapse;
  mobile keeps the same Home/Vault entry points as compact header controls
  instead of reserving sidebar width. Standard route content uses equal
  responsive gutters after the sidebar; at the `2xl` tier both sides retain the
  9rem action gutter aligned with the trailing edge of global Search. Collapsing
  the sidebar changes available canvas width, never the content inset. Page mastheads
  span that available width; their working content is constrained by
  `<PageShell>` (`narrow` / `compact` / `wide` / `full`) according to reading
  and task density. The vault workspace remains full-bleed with its persistent
  navigation columns and its own content-width rules. The root reserves a
  stable scrollbar gutter for document-flow routes so the shared app bar does
  not shift when scrolling appears. Viewport-locked Vault routes disable that
  root gutter because their panes own scrolling; this avoids a duplicate dead
  strip beside the rightmost rail.
- **Control heights**: `h-8` (32, dense rails) · `h-9` (36, default) · `h-10`
  (40, inputs) · `h-11` (44, hero CTA). Keep tappable controls ≥ 36px.
- **Workspace personal navigation**: Home, Search, then Vaults remain fixed below the
  logo. Expanded navigation places at most five favorite Vault shortcuts beneath
  Vaults under a visible Favorites label, with explicit Show more/less and an independent section disclosure beside Favorites. The Vaults row only navigates; it does not collapse favorites.
  Only the personal lists scroll; compact mode keeps the three primary icons.
  Rows use the shared Box glyph and human name, not counts or role badges.
  The selected child owns the strong highlight; its parent must not compete.
  Overflow offers Remove from favorites and restores focus to the disclosure
  or Vaults link. Favorites are browser-local, account-keyed stable Vault IDs,
  synchronized across mounted consumers and tabs, and resolved through current
  accessible Vault data. Legacy unowned favorites remain stored but are not
  automatically assigned to an account. An explicit import confirmation lists
  accessible legacy favorites and merges them without replacing current pins.
  Recently viewed, Drafts, and Pinned follow as initially collapsed sections,
  each previewing three items with Show more/less. Empty sections disappear.
  They store browser-local account-specific metadata; recent/pinned rows expose
  removal, never resource deletion. Draft rows route through the existing
  recovery flow: new drafts open the composer, edit drafts open the editor.
  Draft recovery uses the editor's 24-hour boundary or an earlier server-provided
  attachment expiry. Neither list previews draft bodies or adopts unowned legacy drafts.
  Document Pin and Watch share the resource overflow, but Pin never implies a notification subscription.
  Collection Pin belongs in its overflow; following it reveals the collection
  tree and focuses the target, without overwriting the normal tree preference.
  Pins use canonical paths under the current API contract and can become stale
  after moves; removal only changes the personal shortcut. Notifications have
  one entry point in the header bell, whose View all action opens the existing
  full ledger; do not duplicate it with a sidebar Inbox item.
  Help, then Settings sit below the independent scroll region. AI connections
  belongs inside Settings; do not duplicate it as a permanent sidebar link.
  Settings is a bottom fixed button opening the global account-settings dialog,
  not a selected page link. Compact mode keeps a labelled icon and tooltip.
  Closing settings restores the invoking control and the unchanged workspace.
  Help explains Vaults, pinning, drafts, watching, and search, and is a
  keyboard-dismissable dialog, not an invented external documentation link.
- **Borders are structural** — most surfaces are defined by 1px `border` hairlines
  plus a soft shadow, not heavy fills. `divide-y divide-border` for list rows.
- **Home working set**: Home is a personal starting point, not an operations
  dashboard or second search page. Keep the application shell's symmetric
  gutters and full-width global header. Begin with one cardless inventory
  summary without a visible heading, then Recently viewed or Your vaults when no
  history exists. Its accessible description explains that totals cover Vaults
  the user can access. The summary
  uses a compact cardless ledger, capped at 42rem rather than stretched across
  the page. Each metric places a 16px resource icon and 14px label above a neutral
  20px tabular value. Reuse the explorer's Box / FileText / Table2 / Paperclip
  vocabulary and document / table / file categorical tones; color is decorative,
  never the only label. Quiet vertical separators group four desktop columns or
  two mobile columns. No fills, shadows, hover effects or invented count links.
  Skeletons retain this geometry; long counts and enlarged labels may wrap.
  Do not aggregate the four preview cards, count table rows, or add Collections.
  One authenticated count-only snapshot owns totals; missing values remain `—`
  with one quiet availability explanation. Older servers retain the verified
  directory's Vault count only. A verified empty workspace shows only zero Vaults.
  Foreground identity proof gates both directory and totals, which load in parallel
  even for unchanged local accounts: the server independently scopes the count-only
  snapshot. A verified directory's Vault count can appear while resource counts
  are pending. Skeletons apply per missing metric; one live loading status must
  not hide ready values from assistive technology. Directory completion must not
  restart the summary request. No private totals persist in browser storage or
  remain visible while an identity/access refresh is pending or has failed.
  Leave 24px before the first section without double section padding. Home has an
  `sr-only` H1, not a duplicate visible title or generic orientation sentence.
  View all vaults owns navigation without repeating its count; indexing remains in the global
  header and Vault context, not duplicated in the Home masthead or a stats rail.
  Recently viewed and Your vaults each span the full working width above the
  lower updates/context grid. Their independent cards use four columns only
  from `xl`, two from `sm`, and one on small screens, with 12px card gaps and
  28–32px between sections. DOM, visual, and keyboard reading order agree at
  every breakpoint. Cardless sentence-case H2 anchors, neutral hairlines, and
  a 12px gap before bounded content give all sections the same hierarchy.
  Recently viewed is explicitly browser-local, account-scoped view history—not
  saved drafts or editing progress. Show at most four currently accessible
  destinations, two-line titles, Vault/Collection context, and labelled Viewed
  times. A single destination uses a compact full-width row with trailing time
  on desktop, rather than leaving empty grid columns. Multiple destinations
  retain independent cards. Do not allocate an empty history card when no valid history exists.
  Your vaults is a strict four-card favorite-first preview; even a large favorite
  set must use View all for the directory. On unpin, restore focus to the same
  favorite control or the stable View all link if the card leaves the preview.
  Cards use content-sized height, not a fixed minimum. They prioritize the human name and actual two-line description. Do not fill
  absent descriptions with repeated generic text. Show readable Documents /
  Tables / Files labels only for provided numeric counts; absent is not zero.
  Read only and Archived communicate actionable restrictions without repeating
  the full administrative role hierarchy. Favorite controls stay visible.
  A first-time user with no Vaults gets one explanatory empty state and the
  existing Create a vault dialog, with an invitation alternative for joining
  a team. Never auto-open the dialog or present access errors as empty state.
  Recent updates and Watched documents appear as two equal independent columns
  from `xl`, stacking on smaller screens. Each owns its server scope, pagination,
  errors, and empty state; do not make users switch tabs. A watched document may
  also appear in the all-document ledger, so preview IDs include scope.
  Update-row titles open a labelled preview, while a separate always-visible
  Open in vault link opens the same document with full Vault navigation (never
  a nested link). The preview's promotion button uses the same Open in vault
  vocabulary and preserves the current reading mode. Recently viewed retains
  its direct full-page destination.
  Rows prioritize a
  two-line title, location, explicitly labelled update time, optional author,
  and a short content excerpt—not an invented summary of the change. Existing
  document previews, authorization, compatibility notices, and inbox read state
  remain unchanged.
  Connection setup has one Home-only bottom-right invitation, not a top notice,
  section-header action, or content sidebar. A verified empty PAT list, available
  setup and at least one Vault may introduce a 304px panel once per account/browser
  on large, tall viewports. Its X minimizes to a 44px labelled Connect an agent
  button. Small/short viewports and existing/dismissed/unknown states keep only
  that button. Both entries open the existing setup directly; no intermediate
  menu, automatic modal, repeating toast, orange banner or fabricated progress.
  Use opaque surface, border-strong, radius-md, shadow-md, a small plug glyph and
  teal action. Render the floating region in a body portal so animated route
  containers cannot anchor it to the page bottom. Position 24px from bottom/right
  (16px on mobile), respecting safe areas. When the app footer enters the viewport,
  lift the invitation above its top edge by the same gap, and return to the
  viewport anchor when the footer leaves. Re-measure after body/footer resizing.
  Reserve compact-only bottom clearance;
  collapse/hide if focused content would be obscured. Page-landmark focus is not
  a hidden control and must not suppress the launcher. Shared Dialog ownership
  suspends the invitation even under
  transparent modal overlays. Closing setup restores launcher focus without
  stealing it from another modal or navigation. Token
  presence suppresses the initial hint without claiming a working connection. OAuth
  users do not necessarily need a PAT. The guide explains optional AI-tool use, not mandatory account
  completion. Avoid token totals, progress fractions, or claims of a healthy
  agent based on PAT use. Lookup
  failure must not claim incomplete setup. Presentation memory is account-scoped and
  storage-failure tolerant, with focus transferred to the replacement control
  or heading. Keep a quiet route to connection settings when the guide is absent.
  Do not fill the rail with duplicate access/index statistics. Existing
  Quickstart and connection settings retain token and OAuth flows.
  Both entry points compose the same `ConnectionSetup`: choose a tool, use a
  supported authentication method, configure, and try a read-only request.
  Never interpolate token prefixes into copyable configuration. Explicitly
  entered saved secrets and new secrets live in component memory only. Token
  management is a separate divided ledger; token last-use is not live agent
  health. No auto-mint, required tutorial, fabricated connection check, or
  company-specific example Vaults. Copy failures provide manual-copy recovery.
  Loading, error, empty, and populated states stay distinct; optional metrics
  and connection failures never block document or Vault navigation.
- **Vault workspace density**: on desktop, Vaults and Collections are retained
  as independent full-height columns, beginning beside the AKB logo at viewport
  top. Their `h-14` identity headers align with the app header: Vaults shows the
  current Vault through a searchable, favorites-first switcher. Its borderless
  navigation trigger uses the shared Vault glyph, semibold name, and chevron;
  only hover/open adds a neutral background, while keyboard focus stays visible.
  Do not style this identity control as an outlined form field. Collections is
  exclusively a content explorer: its aligned identity row shows All collections
  or the current Collection with a folder glyph and collapse control. Do not
  insert Vault page links above or below its tree. Creation, refresh, and filtering belong to
  the `h-10` management row below; existing list filters follow independently.
  The full Vault list remains visible, and no new API is needed for switching.
  The app header begins after the combined
  navigation width and tracks both resize handles and collapsed states. Expanded
  desktop Vault lists retain their saved width and resize handle in both inline
  and overlay navigation, independently of Collections visibility. A captured
  drag retains its layout mode until release. Focusable separators expose bounds,
  current width and controlled pane; arrows resize, Shift accelerates, Home/End
  reach bounds and double-click restores default. Pointer cancel/lost capture,
  blur and unmount restore cursor/selection. Mobile drawers remain fixed-width. The
  resource command row remains below it; desktop reading controls remain `h-8`.
  Overview, Search, Activity, File and Table readers share the same location-only
  breadcrumb as documents and focused tasks. Overview shows only the Vault name;
  other routes append their current page or resolved resource trail. No second
  navigation row consumes working height; page links live in the Overview summary.
  The global header owns the only quick-search entry point; do not duplicate
  it as a field, button, or Search destination in the Overview links. Named Vault
  routes default to that Vault each time the dialog opens; Home, account Settings,
  the Vault index and the creation route default to all accessible Vaults.
  A shared scope control sits inside the modal and full-page query fields.
  Selected Vaults use at most two compact removable Box/name chips (12px labels
  and glyphs) and selected-surface tokens. A chevron / operable `+N` disclosure
  opens a bounded multi-select panel populated from
  `/my/vaults`, with a dedicated name filter, checkmarks and Current vault label.
  Multiple selections mean any selected Vault (OR). Empty selection means All
  accessible vaults, never zero Vaults. All selected names remain visible in full
  and removable in that panel, even if directory refresh fails. Above 30rem of
  actual input width, scope occupies at most 45% on the same row as the query.
  Narrow fields use one scope row followed by the query; never grow a chip shelf
  or squeeze the editable input. Keep 36px removal controls and accessible names.
  Changing scope never navigates away from the launching workspace. Keep
  the same multiple-Vault semantics in quick search and the full Search page.
  Fetch the directory when opening search, share that proof with recent-document
  history, and keep loading, empty, filtered-empty and retry states distinct.
  Directory failures must not discard the query or silently broaden its scope.
  It wraps above the input on narrow screens, keeping the query usable and the
  complete name accessible. Manual scope changes preserve the query and content
  filter, invalidate old results, and update recent history to match the scope.
  Never broaden a search automatically: an empty scoped result offers an explicit
  `Search all vaults instead` action. Changes to the account or route Vault reset
  transient search state. `Continue in search page` carries the selected scope,
  query and resource kind to the existing full search route. Document results keep
  the preview-and-return flow; the app-level resource navigation guard protects
  unsaved edits for both scoped and all-Vault results.
  Both the Vault list and Vault breadcrumb return to Overview, including when
  reselecting the current Vault. Links preserve browser new-tab behavior and
  existing unsaved-edit guards. Collections and its stored visibility/width
  remain available on Overview and resource readers. Previews add no separate
  page navigation; their launching workspace stays inactive until dismissal.
  Focused Vault tasks have no Collections tree, collapsed Collections strip,
  tree resize handle, or Collections in their mobile drawer. Workspace and Vault
  selection remain available. The current page label never becomes a menu.
  Route ownership, including
  direct links and trailing slashes, selects the mode synchronously; it never
  changes stored tree visibility or width. Returning to content restores the
  user's tree choice. Header offsets and temporary drawer decisions measure only
  the rails actually present. The explicit navigator shortcut toggles the Vault
  list in focused tasks, not an invisible Collections preference.
  No Collections menu duplicates Vault destinations. If the chosen rails leave too
  little space for search/indexing/account controls plus a readable trail, the complete Vault
  navigator temporarily uses its existing drawer. The header exposes its toggle;
  Escape/backdrop dismiss it. This does not write saved widths or collapse
  preferences, and expanding the workspace restores inline rails. Do not oscillate
  based on the drawer's own rendered width. Compare layout CSS pixels for the
  workspace, rails and header controls, including the fixed Vault menu, so zoom
  cannot keep an inline navigator while clipping the location trail.
  Mobile retains its existing drawer and `h-10` navigation headers.
  Content-oriented Vault routes retain the user's Collections preference. Focused
  task routes omit the explorer without overwriting that preference. The collapsed content rail leaves only
  a labelled Expand collections control; no extra icon menu is invented.
  A Collection deep link may temporarily reveal the tree without overwriting
  the saved preference. The explorer tree scrolls independently; its identity,
  management and filter rows remain aligned with Vaults. Avoid a repeated title/description
  masthead: Overview alone uses the compact Vault identity header. Operational
  routes begin directly with the control or data surface that performs the task
  (search form, roster toolbar, publication list, settings local nav) and retain
  an `sr-only` H1 for route orientation and heading hierarchy. The shared
  breadcrumb already provides visible location context; do not repeat it.
  Account Settings uses one global modal above the current route, not a page
  or extra workspace rail. Desktop width is capped at 1040px and height at
  760px with 24px viewport clearance. A 56px title row, 208px grouped navigation
  (Account / Connections / privileged Workspace), and independently scrolling
  content keep close/navigation controls available. Use `surface` throughout,
  quiet hairlines, cardless section headings and 20/28px content padding.
  Profile uses a compact avatar identity row and stacked profile/password forms;
  label/input columns and appearance previews respond to their container width.
  Profile photos reuse that identity row, not a new card or modal. Choosing a
  JPEG/PNG/still WebP shows a circular, centered-square preview; Save photo is
  explicit and independent of Save profile. Cancel returns focus to the photo
  picker without discarding text drafts. Removal uses an inline confirmation.
  Pending photo writes participate in Settings' dirty/busy guards; native-file
  picker focus changes alone never close Settings. Recoverable errors preserve
  the saved photo and selected draft. A revision conflict keeps Reload photo
  available even after dismissing the error, and never silently overwrites.
  `UserAvatar` owns the fixed 28/40/48px shapes in the header, account menu and
  Profile, with authenticated, session-scoped images and grapheme initials on
  absence/failure. Photos are self-only; Members/author images remain unchanged.
  Local and SSO users follow the photo capability, independently of SSO-managed
  text fields. Older servers or missing storage retain initials and explain why
  photo editing is unavailable; a request failure is not a feature-detection signal.
  Agent connections preserves three explicit phases, stacked in reading order:
  prepare access, configure, then try a read-only request. Never hide the final
  phase until token creation or offer a placeholder secret as usable config.
  Quickstart and Settings share the same numbered, hairline-separated flow.
  One tool choice leads into one access-method choice (supported browser sign-in,
  new token, or saved token), rather than nested tool/auth/token selects.
  Tool choices use an underline strip; access methods use a bordered segmented
  choice with a check on the selection. Both retain labelled native radio groups,
  a single selected tab stop, arrow-key selection and visible focus in both themes.
  Expiration and write restrictions remain under Advanced options, with the
  reviewed permissions summary always visible before issuance. Keep the form
  mounted across method changes; a hidden draft or uncertain result is not reset.
  Quickstart keeps its header and close controls fixed while only setup content
  scrolls. On narrow screens, step content uses the full available width.
  Focus-trap wrap reveals the focused control inside that scroll region without
  moving the modal or background workspace.
  Below `md`, use a labelled section selector; below `sm`, fill the viewport.
  The workspace route, scroll position and sidebar preference stay unchanged.
  Sidebar, account menu, watched-document management and onboarding open this
  same dialog. Legacy `/settings?tab=` links open the requested section above
  Home; closing returns Home. Settings-owned resource links dismiss the dialog,
  including links to the already-open document. Escape, backdrop, close button
  and section changes guard unsaved edits or newly generated secrets; pending
  writes block those actions. Nested confirmations close before their parent.
  Restore focus to the invoking control and never persist token secrets.
  Vault Settings uses the Members/Public links workspace family: the same
  `WorkspaceSectionHeader`, route-owned gutters and one bounded workspace Panel.
  The visible Settings heading is 14px with its quiet 28px tonal icon; an archived
  badge appears only when applicable. Vault details (`#general`) and Access
  permissions (`#access`) are full-width, purpose-led form sections. Each has a
  14px semibold H3, a meaningful 28px tonal icon and one short 12px explanation;
  they do not use the small ledger header strip. At 64rem of actual workspace
  width the intent occupies a 13rem left column and controls fill the right;
  below that width the intent sits above the controls. Sections share neutral
  hairline separators, with no nested card shells or category tabs. Preserve
  one mounted form per scope, all deep-link IDs and one route scroll surface.
  Use the same route-owned gutters as Members/Public links, with 16px mobile /
  20px desktop section padding and 20px group gaps. Keep 24px above the page
  header on desktop and 16px between its divider and the panel. Description uses a compact
  80px minimum field on desktop and keeps 16px input text on narrow screens.
  Vault details places the labelled description field and permanent copyable
  Vault address side by side at 44rem of actual controls width, otherwise
  stacked. The address has a quiet read-only field surface and a compact copy
  target (44px on touch); metadata about ownership, dates and inventory belongs to Overview.
  Access permissions starts with Who can access and a Sign-in required cue.
  The three native choices retain their explicit audience and permission
  descriptions. At 44rem of controls width they share one horizontal comparison
  group with vertical dividers; narrower groups use divided vertical rows.
  Never remove native radio semantics or keyboard arrow selection. Keep radio
  names distinct from descriptions and status. Current marks the applied policy;
  Not saved marks a different selected draft. Teal marks selection; public
  writing also shows a warning glyph and explicitly includes deletion.
  Preserve owner-only writes, save/discard feedback and public-write confirmation.
  Do not repeat Members or Public links navigation inside Settings; those tasks
  remain in the Vault Overview. Workspace-wide access always requires AKB
  sign-in and must not be described as anonymous sharing.
  Vault guide and owner-only Danger zone retain full-width ledger sections.
  Their shared SettingRow primitive puts labels in an 11rem column at 48rem
  of section width; smaller rows stack labels over controls. Danger actions
  align to the far edge. Do not add a permanent context sidebar, repeated
  page headings, filler cards or a separate Settings scroll surface.
  Each editable scope shows Save/Discard only while changed or saving, followed
  by a polite saved status; do not reserve disabled action footers. Serialize
  Description, Access and Guide writes. Saving must not overwrite another scope's
  saved snapshot or pull focus away from another field on completion.
  Guide shows a real read-only Markdown preview immediately below its compact
  update-time / actions row. Render headings, paragraphs, lists and code with
  the shared MarkdownRender and authenticated Vault/document adapters, omitting
  frontmatter. Initially show four complete top-level blocks; Show more expands
  in place and Show less collapses. Hide whole later blocks, including their
  focus targets, instead of clipping a live link with an ellipsis or fade.
  Keep one route scroll surface. Use a readable prose measure, small document
  headings below the section's H3, and unique preview anchor IDs so opening
  the full reader cannot collide with background headings. Guide fragments
  expand and focus their own preview heading; modified clicks open the canonical
  document. Revision-query links retain their query and open the reader. Other
  internal links use the containing Settings departure guard. The toolbar uses
  Open guide / owner-only Edit guide / options and stays mounted across modes.
  At 48rem of section width tools align right; narrower sections stack them
  below the update metadata. Draft/saved status belongs beside the update time.
  Expanded editor and Agent view occupy the same full-width content area.
  Open guide opens the same
  route-backed document reader as Overview and preserves this Settings entry,
  all draft scopes, their revision pins and return focus. Only an exact retained
  guide preview bypasses departure confirmation; full-page promotion or another
  resource still protects unsaved work. Edit guide opens the body-only editor
  inline without inner tabs. History, Agent view
  and Reset to template live in the labelled Guide options menu; resetting still
  requires confirmation. Preserve Guide drafts and their original OCC revision
  across reading modes and failed writes. The danger area has two compact action
  rows with short visible consequences, not a large red-filled banner.
  **Guide routing:** Open guide always starts in the shared document reader.
  The canonical document URL and legacy `/vault/:name/skill` URL stay read-first,
  including fresh tabs; no implicit jump from reading to Settings. Guide readers
  never expose the generic document editor or property editor. Verified owners
  of writable Vaults get a labelled Edit guide link to
  `/vault/:name/settings?edit=guide#skill`, which opens the sole body-only editor
  and consumes the edit request. Cancel/Save never reopen it automatically.
  Readers, writers, admins, archived and mirror Vaults retain reading without
  guide-management controls. Historical versions and diffs remain readable.
  Indexing observations open from Overview's single current-Vault status trigger,
  with a short hover/focus explanation and click/tap details. Verified idle status
  is absent; queue, paused, failed and unavailable states remain distinct. No
  separate Indexing details button or global queue badge repeats on Overview.
  They are not editable settings. Unverified/stale/missing
  status must never read as healthy. Keep one desktop sheet scroll surface, no
  forced viewport-height cards or filler information, and match loading geometry.
  All scopes share one workspace-level leave confirmation. Same-path hash history
  retains drafts without a warning; actual departure asks once. Keep editing
  retains every draft, and pending writes block departure. A confirmed departure
  never clears one draft before another guard can cancel navigation. Cancelling a
  menu-origin departure returns focus to the visible Vault menu, with Description
  as a fallback. Account Settings is a separate dialog and is not changed here.
  The expanded Vaults rail and Collections explorer share `RailIdentity`,
  `RailManagement`, `RailCollapseButton`, `RailFilterToggle`, and `RailFilterField`.
  Their grammar is identity (56px desktop / 40px mobile), management (40px),
  then a full-width 32px search field inside a 40px row. The management row says
  Vaults or Collections and owns refresh, filter disclosure, and create; only
  PanelLeftClose / PanelLeftOpen toggles belong in identity headers.
  Name search remains available in empty and filtered-empty states. Additional
  role or resource-type/document-state filters expand on demand below search;
  applied conditions retain a visible count and Reset filters even when folded.
  Changing document state preserves the search text; Archived forces documents
  without silently clearing the query. Collection rows and their overflow
  triggers have a 36px desktop baseline, and both lists use the same rail scrollbar.
  Each collection/resource row owns one continuous hover, focus and selected
  surface. Its title control fills the row; the icon-only overflow trigger
  floats at the right edge instead of consuming a separate flex column. Reserve
  text padding so long names never run under it or shift when it appears.
  The trigger stays transparent, including on hover and while its menu is open;
  icon color and a keyboard focus ring provide its individual feedback.
  Fine-pointer desktop rows reveal tools on row hover or focus-within; the
  active row and open menu keep theirs visible. Touch/coarse-pointer and narrow
  layouts keep tools visible with 44px targets and rows. Menu focus and closure
  retain the existing independent action/navigation controls and focus return;
  clicking the overlay must not expand a collection or navigate a resource.
  Root creation belongs in the management row rather
  than at the end of a scrollable tree. Its single create menu and every
  Collection overflow menu share the same document / upload / table /
  Collection vocabulary, with the target Collection preselected in each modal.
  Collection rows keep the human name and one overflow trigger only—never a
  second line of icon counts or sibling action icons that squeeze the name.
  Document rows also remain title-first. For archive recovery, the rail's compact document-state selector
  exposes Current (draft and active), Archived, and All. Archived rows pair a
  quiet archive glyph with an accessible label. The reader owns Archive/Restore
  in its overflow and a compact archived-state restore notice; archive never
  implies deletion or revoked access. Unsupported server filters show a recovery
  notice rather than a false empty inventory. When two documents in the same
  Collection have the same title, and only then, both rows add one quiet
  monospace filename discriminator; generated UUID suffixes collapse to four
  characters. This is ambiguity recovery, not permanent technical metadata.
  When a Collection mixes resource kinds, or one kind exceeds the 20-row
  preview, Documents / Tables / Files become peer disclosure rows with a quiet
  tabular count. Each open group renders 20 rows initially and reveals 50 more
  per request; a resource-type filter bypasses unrelated kinds, so a large
  document set can never bury tables or files at the end of the tree. A final
  300-row progressive guard prevents one action from mounting a multi-thousand
  node Vault. Small single-kind Collections stay flat and compact.
  Each group can be manually collapsed even while its resource is open in the
  reader. New resource navigation reveals its actual Collection and kind once;
  tree refresh does not undo a manual collapse. File/table ownership comes from
  the browse hierarchy, not an assumption that their IDs encode a path.
  The explorer's Filter resources searches names/paths inside the sidebar,
  never resource bodies or a separate modal. A compact, name-filterable
  Collection selector inside the Filters disclosure defaults to All collections; a selected path
  limits the tree to that Collection and descendants, excluding sibling prefixes.
  Its clear action preserves the text/type/state filters. Collection overflow's
  reader-accessible Search in collection selects that same inline boundary and
  opens Filters and focuses Filter resources without navigation. A collapsed
  scope remains active and contributes to the filter count; Reset filters also
  clears it while preserving the name query. Keep the selected boundary and
  Includes sub-collections explanation inside the disclosure even with no matches. Vault or
  account changes reset this temporary search context. Resource kind and archive
  filters still compose with the scope, and refreshed data never broadens it.
  The details
  action is the stable place to review the complete typed counts and Collection
  summary and, when supported by the connected backend, edit it.
  Older backends keep the current summary readable
  and surface an explicit compatibility notice instead of failing silently.
  A read-only permission notice is a flush, full-width policy bar directly above
  the settings tabs, with a bottom hairline and no card margin or rounded shell.
  In the active panel, each governance section uses a cardless section
  anchor above its settings panel: a sentence-case H2, one meaningfully toned
  icon, a concise description, and a full-width neutral hairline. The bordered
  panel below contains only controls and feedback, so headings remain easy to
  scan instead of reading as the first row inside each card. Generous
  inter-section rhythm separates scopes without implying a numbered workflow.
  Danger zone keeps its semantic in-panel warning header because that boundary
  communicates risk rather than ordinary section hierarchy.
- **Document composition workspace**: Match the reader's white surface,
  compact toolbar and structural hairlines. Keep the modal's
  existing responsive geometry, backdrop/unsaved-draft protection, and native
  file-picker safeguards. A single 56px header holds Vault context, New document,
  Create, and Close; technical URI previews and a second masthead are unnecessary.
  At 52rem of actual dialog width, place a dedicated 18rem Document details rail
  to the right of the writing canvas; use 20rem from 72rem. Collection is the
  first, visibly required field, followed by Summary, document type, domain and
  tags. These fields stay visible rather than hiding behind a Details disclosure.
  A neutral vertical hairline, not a nested card, separates the two independently
  scrolling regions. On narrow dialogs, details follow writing in one scroll
  flow; a compact fixed Collection row below the header shows the chosen path
  (or a required prompt) and focuses the actual Collection field when activated.
  Title and body share ordinary 16px/24px responsive text insets, without a
  centered title measure, outer card padding or a metadata band above the title.
  The editor toolbar remains attached to its writing surface. A compact footer reports local draft/upload state and character
  count without competing with Create. Preserve existing upload, duplicate-title,
  collection validation, and submission contracts.
- **Vault Overview workspace**: Overview is a full-height Vault workspace rather
  than a stack of dashboard cards. Its identity is a cardless page-level anchor:
  Vault icon, H1, one address-copy icon, role, applicable public/read-only/mirror
  state, current-Vault search-update status, and optional wrapping description.
  The address appears on hover/focus; click/tap copies it with confirmation and
  a selectable fallback when clipboard access is unavailable. One accent New
  menu on the right opens Document / Table / Upload file choices; dialog dismissal
  returns focus to that persistent trigger. Immediately below, passive
  content-sized totals and the Overview page links share a responsive summary
  row above the neutral hairline. Totals are passive text, without filled or
  outlined button surfaces; retain their 32px minimum height and 8px internal
  spacing. At 58rem of actual workspace width, totals sit left and page links
  right. Below that width the four links follow totals on their own row, before
  recent activity. Use teal text links without a tab underline, button fill,
  enclosing panel, menu heading or fixed positioning. Graph / Public links and
  Members / Settings have a small grouping gap. Every link is at least 44×44px;
  icons hide below 30rem of workspace width while readable labels remain.
  Native modifier clicks stay native, and accepted page departures focus the
  destination content without pulling focus away from a blocked leave dialog. Preserve the 12px mobile, 16px intermediate, and 20px
  desktop route gutter; do not add an Inventory band, equal-width stat cells, or
  another enclosing card. Graph-link health stays out of Overview.
  Overview keeps 20px between major regions, a 14px identity heading, 13px
  recent-item titles and context copy, and 12px metadata. Recent rows remain
  content-driven with a 48px minimum. Context headings use 44px rows and
  quiet 24px icon tiles. Navigation links retain their 44px targets.
  Recent activity leads directly below the summary. When the actual working
  area's content width reaches 60rem, a 20rem contextual column sits beside it
  with a 24px gap; narrower workspaces stack in DOM order. Use container queries, not
  viewport breakpoints, so expanding/resizing the left navigators cannot crush
  the document ledger. The contextual column separates its sections into thinly
  bordered, small-radius panels with a 12px sibling gap, including while loading.
  Do not add an enclosing panel, generic Vault context header, nested cards,
  shadows, or an independently scrolling/sticky area. Each section begins with a neutral
  `background` header, a small meaningful `TonalIcon`, and a sentence-case 14px
  H2: Vault guide (guide) and Access and ownership (people).
  Its body uses `surface` and a 16px reading inset. Guide status is a
  passive labelled badge associated with the separate outlined Open/Set up guide
  link. Existing guides open in the route-backed document preview over Overview,
  preserving the background, scroll and invoking focus. Only confirmed missing
  guides use Settings#skill for setup. Owner/visibility remain noninteractive;
  the visibility glyph and brief
  policy text distinguish signed-in public access from anonymous Public links.
  Members is a passive count in Access; its only Overview destination link is
  in the summary navigation. Do not tint entire sections or add hover effects
  to passive information. Contents is a passive definition list of
  authoritative document/collection/table/file totals in the page summary, never
  repeated on the right. Membership belongs only in Access alongside owner and
  visibility; role and the copyable address appear only beside the Vault name.
  Missing counts render a labelled dash, never zero; unknown visibility never
  becomes Private, and incomplete totals cannot trigger empty-Vault onboarding.
  Recent activity uses a bounded, small-radius ledger with plain resource glyphs,
  human document titles, Collection paths without repeated filenames, and Updated
  times aligned on the right. Titles and paths stay grouped on two lines and wrap,
  never surrendering their text to a commit hash. Commit hashes belong in Commit history, not Recent
  activity. Commit history remains a quiet, cardless
  secondary, collapsed-by-default disclosure whose header owns both the `Show
  commits` control and `Full commit log` route. Resource inventory belongs in
  Collections, not a table-only context section; totals stay in Contents.
  The current recent-change API returns document updates only. Do not blend
  table/file inventory into that ledger as if it were timestamped activity;
  a future mixed-resource feed needs an explicit API contract and kind-specific
  destinations. Empty and active Vaults keep the same outer
  skeleton—the activity ledger changes to first-run actions without moving
  identity or context. Empty Vault onboarding uses two explicit tiers inside one
  connected panel: content creation (document, file, table) first, followed by a
  `Set up this Vault` band whose three divided cells expose bundle import, Vault
  description/guide, and agent connection. Do not collapse those actions into a
  low-emphasis text-link footer or split them into unrelated floating cards. Do
  not repeat a separate About card below the description
  in the identity row, and do not fill tall displays with invented metrics: the
  workspace surface reaches the viewport while real content remains
  content-sized. Small screens retain an outer inset and stack every ledger and
  context section into one document flow.
- **Graph workspace**: Graph is a search-first relationship explorer, not a
  settings page or dashboard. It runs full-bleed inside `VaultShell` with no
  repeated page masthead. A bounded floating tool dock sits 12px inside the
  canvas, capped at 56rem; no full-width toolbar or legend band consumes a row
  above the scene. Use an opaque surface, hairline and soft shadow so controls
  stay readable over nodes. Graph/List view controls lead with a teal underline,
  followed by a bounded resource finder and Filters / icon-only More actions.
  On narrow workspaces the view controls sit
  above the finder/actions; DOM and keyboard order follow that same sequence.
  Secondary layout, saved-view and help tools live in More.
  A separate content-sized floating legend pairs each resource kind's canvas marker with its
  label, loaded count and visibility check, without three competing selected
  fills. Hidden kinds use a struck-through label and lose the check; they are
  still operable. These are independent pressed-state filter buttons,
  not tabs; hidden kinds retain their counts and remain recoverable. Counts
  describe the loaded map, not an assumed complete Vault inventory. A
  focused-neighborhood breadcrumb and a hop dropdown (1, 2 or 3 hops) appear
  around a focus; a "Changed within" dropdown (last day, 7 days, 30 days, any
  time) sits beside it and in the whole-Vault strip, keeping only resources
  whose `updated_at` falls in the window (the focus, the selection, revealed
  resources and resources without a reported time always stay), applied
  before the walk so it never passes through a hidden resource. Around a focus the canvas colours each resource by its hop distance —
  focus `cat-5`, then `cat-1`, `cat-2`, `cat-3` for 1, 2 and 3 hops, a
  lightness-ramped run of the categorical scale — and shape alone carries the
  kind; a compact hop legend beside the control names each colour, and the
  kind legend's markers turn neutral so they do not claim a colour the map is
  not using. Around a focus, "Strong links only" is on by default: the map
  follows explicit relations outward from the focus (and `derived_from` back
  to what was derived from it), so shared context such as a cluster ends the
  walk instead of pulling in every other resource that shares it; the focus's
  own body links, and those of every strongly reached resource short of the
  chosen depth (one step further out), are weak neighbours that are never
  followed: with more than
  five they fold into a "Show N weak links" control (with five or fewer they
  show, faded), and while folded no weak line is drawn. Around a focus the map
  is concentric: the focus at the centre and each strongly reached resource on
  its hop's ring (weak neighbours just outside the deepest ring); only the
  walk's backbone (the edge that first reached each resource) pulls in the
  layout and draws at full weight, other strong edges among the kept resources
  stay faint until a resource is selected. A note says where the strong walk
  ends. Labels are titles (the file name without path or extension when the
  server has no title) cut to 15 characters on one line (the inspector and
  tooltip carry the full title), prefixed by a muted category (document type,
  else collection) when the map holds more than one;
  around a focus they sit on the far side of their dot from the focus and are
  prioritised by distance. Turning it off (`links=all`) shows every loaded
  connection. A Vault without explicit relations around the focus shows all
  connections with a one-line notice. The whole-Vault map also defaults to
  strong links only (body links drawn only when it is turned off) and colours
  resources by collection: the four largest collections take `cat-1`..`cat-4`,
  the rest share `cat-6`, named in a compact legend; shape still carries the
  kind. A fit keeps the map below the floating tools. Keep gesture instructions in Help, not another persistent title row.
  Measure the dock's actual height after wrapping: List's scroll viewport,
  inspector, relationship details, and notices must begin below it. Focusable
  canvas labels must also stay clear. Empty space around floating controls
  passes pointer gestures through to the canvas; only the controls catch input.
  Honest loaded-resource/relationship counts live at the canvas foot.
  Relation kinds, hidden resources, and orphan decluttering live
  behind Filters rather than in a permanent left rail; resource types are also
  available there. The graph canvas owns the remaining
  workspace: a Three.js/WebGL scene shown as a flat map by default (every
  resource on one plane, seen from straight above, no rotation) with a 2D / 3D
  choice in the bottom tools that the browser remembers; 3D is the
  perspective volume with compact orbit buttons. Zoom/fit/pan tools serve both. Use Obsidian-inspired unlit, camera-facing dots and
  plain projected text, never glossy spheres, cubes, boxed labels or lighting.
  Dot radius stays bounded in screen pixels, including single-resource fit and
  zoom; loaded connections modestly increase size. Small flat circles identify
  documents (`cat-1`), squares tables (`cat-3`), and diamonds files (`cat-4`).
  The visible legend uses the same silhouettes and tokens; List, filters, label
  tooltips and the inspector retain explicit kind text. A separate teal ring
  marks selection without erasing the resource's kind color or shape. Neutral
  curved lines preserve parallel relationships;
  dashed lines mean weak connections (body links, or a relation made weak via `edge_type`), not a different relation type; strength comes from the server's `edge_type` (explicit relations strong by default) and is never inferred from the relation label. Where a strong connection joins two resources, a weak one between the same two is not drawn and the inspector lists the pair once. Selection accents
  only the resource and its immediate connections. All initial scenes use
  off-thread force layout in the chosen dimension (two for the flat map, three
  for 3D), never row/column packing or a tilted plane; switching dimension
  lays the map out afresh. Projected labels face the reader and avoid overlap; zoom,
  selection or List reveals dense titles. Selection, theme and filtering do
  not restart layout; incremental expansion fixes existing coordinates.
  On the flat map, dragging the background pans and arrow keys pan; in 3D,
  left-drag orbits and right-drag pans. Scroll/pinch zooms in both. Dragging a
  resource moves that resource (on its camera-facing plane, z = 0 on the flat
  map) with its lines attached and never selects it; it keeps that place
  through later layouts until Rearrange. There is no automatic camera motion;
  buttons and canvas keyboard shortcuts provide alternatives. Pinning is
  explicit. WebGL2 initialization/context failure offers List instead
  of silently substituting another renderer. Selecting a node opens a 22rem overlay inspector and never
  narrows the canvas. The inspector prioritizes the title (a link that opens the resource in the vault)
  and location, Focus (the primary action: make it the centre of the map),
  Preview and Load connections; Outgoing and Incoming connections start folded; summary and secondary
  copy/pin/hide tools remain subordinate. Whole-Vault search changes to a focused neighborhood while click
  selection remains highlight-only. A first-class List view is the keyboard and
  assistive-technology source of truth, progressively revealing at most 50 rows
  at a time. Drag, double-click, and right-click remain accelerators only; every
  essential action has a visible single-pointer/keyboard route. Mobile defaults
  to List and presents a bounded bottom inspector without a blocking backdrop.
  Map movement also has buttons on narrow workspaces. Empty Vault,
  filtered-empty, load failure, and resources-without-relationships are distinct
  stable states. When resources exist but no edges do, preserve the canvas and
  keep every isolated resource selectable; a compact non-blocking notice explains
  why no lines are present, while List remains an explicit alternate view. A
  desktop visit always opens the primary Graph view rather than restoring a stale
  List preference; mobile still defaults to List. The full graph continues to use
  the existing overview/neighborhood/search/relations endpoints, hides absent
  optional metadata from older backends, and never surfaces Graph health.
- **Members workspace**: Follow the Public links single-ledger structure and its
  route-owned responsive gutters. A cardless `WorkspaceSectionHeader` anchors
  Members, member count, and Invite; omit redundant introductory copy.
  Start with the member list (or its filter), not Vault-wide settings. Public
  access controls belong in Settings, without a second shortcut above the roster.
  Only a known public-access policy adds a brief note below the list explaining
  that signed-in people outside the roster can also read or write. Omit that note
  for private/unknown policy; archived/mirrored vaults must not promise writes.
  Roles and capabilities are secondary guidance behind a labelled help icon in
  the Role column, not a permanent inspector competing with the roster. Preserve the
  hierarchy (Owner → Admin → Writer → Reader), mark the viewer’s role, and restore
  focus to the launcher on dismissal.
  The ledger combines name and contact into one identity cell, followed by role
  and a separate overflow-action column. Joined is shown only when the actual
  content container has room, rather than using viewport breakpoints that ignore
  both left navigation rails. Long names and emails wrap; compact screens omit
  decorative avatars. Role selectors keep a 32px desktop / 44px touch target with neutral
  surfaces and foreground text in both themes, not competing role-colored fills.
  The compact header uses a 14px title and 28px icon; names use 13px medium,
  contact metadata stays 12px, and ledger cells use 12px vertical padding.
  Filtering appears for more than eight members and remains visible while active.
  Keep invite, role changes with Undo, revoke and ownership-transfer confirmations.
  Reader and unknown-role views do not expose management controls; row actions
  wait for the current user identity and exclude the owner and self.
  The ledger ends after its content, without a forced full-height frame or nested
  roster scrollbar. The shared route viewport scrolls long member lists.
- **Resource reading shell**: Document, File and Table routes use one resolved
  location trail (`Vault / Collection ancestry / human title (kind)`) and one
  `ResourceCommandRow`. Document metadata and reading tools share this row;
  all three readers use a flat full-width canvas. Desktop puts the
  trail in the existing 56px app header without a page menu or middle section
  navigation; a separate command row owns resource
  actions and optional metadata. Do not add another Explore menu or menu on the title.
  Collections remains dedicated to browsing resources. The Vault crumb is a direct Overview
  link. Location never inherits metadata's small-screen hiding rules; mobile
  uses the same trail beside the navigation toggle below its app header.
  Previews keep a local identity header with Open
  in vault and Close together; no Vault section menu inside the preview or in
  its inactive background header. Promotion opens this same document/mode.
  `ResourceLocationProvider` scopes labels to the current identity, route and
  access revision. Resolved resource data supplies the title and canonical
  collection; a single authorized browse snapshot resolves ancestor display
  names. Never infer a human title from a UUID or expose a previous resource's
  name during loading/denied/account-change states.
  Deep ancestry collapses behind a keyboard-operable menu, which retains all
  Collection destinations even when the parent crumb is hidden on small screens.
  Keep one leading 16px Box glyph beside the Vault name; Collection crumbs and
  their ancestry menu use text only. Quiet slash separators and regular-weight
  ancestor links lead to the stronger current title. FileText / File / Table2
  appears directly after that title as a muted, non-shrinking 16px kind marker,
  not an independently aligned header action or a button. Keep the glyphs
  decorative and the resource kind available to assistive technology without
  repeating it as a visible text suffix. Truncated Collection links reveal their
  complete name on hover/focus.
  Below 20rem of actual resource-trail width, the leading Vault link keeps its
  Box in a 36px-wide target with its full name available to assistive technology
  and in its title. Hide only its visual text so the current resource title
  receives space before ancestors. The direct Overview destination and ancestry
  disclosure remain reachable; the global Vault menu keeps its explicit label.
  The current title is passive text, never a second navigation menu or an
  expansion control. Truncated titles reveal their full value on hover/focus;
  Document info also wraps the full title for touch access.
  Preserve the parent and resource title before secondary ancestry.
- **Document workspace**: the location trail is the sole visible document
  identity. Keep one semantic `h1#doc-title` and preserve authored headings.
  One flat command row keeps quiet Last edited metadata on the left, with
  icon-only Preview / Raw selection, Copy markdown, Edit, Document details
  and overflow grouped on the right in that order. Edit and guide editing have named
  tooltips and no visible text label. Read modes use a white/slate surface,
  neutral outer stroke and sliding teal selection outline, without a gray inset
  or shadow. Keep radiogroup semantics, arrow/Home/End selection and one tab stop.
  Mode tooltips require a deliberate 600ms pointer hover. Focus and keyboard
  selection never open a popup; selecting either mode dismisses its tooltip.
  Accessible radio names and checked state always identify the active mode.
  Controls use 28px targets and 14px glyphs above 48rem of command space, with
  44px targets and 16px glyphs below. The command row uses 4px vertical padding
  and quiet 12px timestamps. Below
  40rem, timestamp/status form a quiet second line so the full toolbar fits 320px.
  Summary is ordinary muted 13px / 1.6 text directly after the first authored heading,
  or before a headingless body, with the same centered reading measure. Do not
  add a Summary label, card, icon disclosure or duplicate title. Use the shared
  viewer's presentation-only `afterLeadingHeading` slot: never split/rewrite
  Markdown, alter heading anchors, or include metadata in copied source.
  Only overflowing text moves slowly, with pauses at both ends and clearance
  beyond the right fade so the final characters become completely readable.
  Hover/focus pauses motion; an explicit Pause/Resume control remains available.
  Focus allows native horizontal scrolling from the beginning. Reduced motion
  wraps the full text and removes motion controls. Offscreen/hidden-document
  summaries pause, and resource/summary changes reset local animation state.
  Missing or invalid dates remain absent in the toolbar and say Not available
  in Properties. Last edited uses `updated_at` with exact localized time and
  timezone on hover/focus. Historical/diff views instead use Version saved
  from the matching history entry, never the current metadata timestamp.
  Copy document link, Copy markdown, Publish / Public link, Watch, Pin and
  Version history live in overflow before location/lifecycle actions. Publication
  still opens options before any public write and preserves permission reasons.
  Keep publication and clipboard fallback controllers mounted when menus close.
  Failed clipboard access offers manual copying and returns to the invoking
  control. Keep one persistent toolbar copy control across Preview / Raw so
  failed clipboard access retains its manual-copy dialog and return focus.
  Reading width has no user-facing control. Preview, Raw and loading use one
  automatic centered 52rem measure with container-responsive 16–32px side insets
  and 16px block padding. The first authored block has no extra top margin.
  Reading headings use the same Pretendard family as the app, weight 550,
  with a 22 / 18 / 16px scale (20px primary headings on mobile) and 24px / 8px
  block margins. Mode controls keep an inset focus ring so keyboard focus does
  not inflate the compact outline control. The summary row
  is 28px on wide readers, with 20px separation before the authored body.
  Body text uses 16px / 1.65. Prose, headings, tables, images and code share the
  same measure; wide code/tables scroll in their own keyboard-accessible regions.
  Target the actual `.ProseMirror` / `.tableWrapper` DOM. Images retain aspect
  and natural size and shrink within the reading measure without stretching.
  Toolbar and canvas share `surface` without outer cards or duplicate title bands.
  Document details has one labelled icon entry in the toolbar; there is no
  permanent edge rail. It contains On this page followed by native Properties,
  Version history and Related documents disclosures. Only the expanded section
  mounts its interactive content. Overflow Version history opens that disclosure
  and focuses its heading. With at least 48rem of actual reading width and
  20rem of height, the 19rem pane docks beside the article and remains open while
  reading. Otherwise it is a modal right sheet, max 24rem, with focus containment.
  Close/Escape returns focus to the toolbar trigger. Child dialogs retain their
  drafts and opener focus across resizing; defer presentation changes until the
  child dialog closes. Following an outline link keeps the pane open and only
  scrolls the document canvas, preserving route and link focus. Raw/diff explain
  why headings are unavailable. Editing hides the pane. Full timestamps, size,
  author and tags live in Properties; omit duplicate Summary and technical URI rows.
  Publication management uses its own verified writer-or-higher policy, rejects
  read-only Vaults and historical/diff views, and preserves exact server errors.
  Existing links use a Public link label, not a claim of live availability:
  expiry, passwords and view limits may apply. Its disclosure offers a selectable
  URL, honest copy feedback/manual recovery, Open link, the publication registry,
  and an explicit confirmation before removing all document public links.
  Publishing and existing-link disclosure share a nonmodal, 340px popover anchored 8px below the
  overflow button's right edge. Constrain and reposition it at viewport boundaries;
  use internal scrolling on short screens. No backdrop, body scroll lock or
  article reflow. Outside actions remain usable on the first click; Escape,
  Close and the same trigger dismiss it. Within a document preview, portal it
  inside that dialog's focus boundary. Initial publishing reviews password,
  expiry and view limits before the explicit Publish submission. On short
  screens its heading scrolls with the options and its action footer stays
  visible. Pending submission prevents dismissal/repeated writes; errors keep
  entered options and scroll only the publication surface into view. Success
  changes this same popover to the ready URL and copy actions. Destructive
  unpublishing keeps its confirmation dialog; cancelling returns to the
  popover's Unpublish action. Publication retains its explicit text label in
  overflow on full pages and previews. Edit hides publishing until the draft
  is saved or cancelled.
  Edit uses the context command row for visible draft/upload state and Cancel /
  Save changes. It never adds another `Editing document` band. Clean Cancel exits;
  unsaved Cancel retains its confirmation. Same-account access verification must
  preserve the live editor draft even if a permission-scoped query remounts it;
  switching accounts resets local editor state.
  Full-viewer Vault links ask before leaving unsaved edits and keep the existing
  local-draft recovery behavior. Keep editing is always available; navigation
  cannot abandon an active save or image upload. Modified/new-tab link gestures
  remain ordinary browser navigation and do not dismiss the current editor.
  Create and Edit use `DocumentAuthoringLayout` and `DocumentDetailsFields`.
  At 52rem of available container width, details occupy an independently
  scrolling 18rem right rail (20rem at 72rem); narrower workspaces stack them
  below the body with a Document details focus shortcut. Read-mode context
  stays floating. Existing Collection is read-only; Move remains separate.
  Edit owns title, body and details as one revision-checked document form. One
  Save changes patches only edited fields; local drafts include both original
  and edited metadata, and older text-only drafts remain recoverable. Its title field sits
  immediately above the editor and one Save changes action patches whichever
  values changed. Use a borderless document-scale title with a visible small
  label. Keep the title fluid with 16px / 24px responsive side insets, independent
  of the centered body measure. Omit technical title help; show validation and
  duplicate-title feedback only when needed. Local draft feedback below the editor
  uses the same side insets, 12px vertical padding and a quiet top divider, never
  flush against the canvas edge. Entering Edit starts at
  the title without scrolling the surrounding workspace. Formatting and the
  compact `Visual` / `Markdown` mode menu share one control row; do not add a
  separate WYSIWYG / Source band. Markdown remains available as a secondary
  editing mode, and returning to Visual must retain the same draft and assets.
  The system-created slug/path remains stable, so editing the
  display title does not move links or history. File names are not a user-editable
  field, and the header overflow contains location/lifecycle actions such as
  `Move document` and Delete—not a competing `Rename title` flow. When Move is
  unavailable, it remains visible with the exact permission, historical-version,
  mirror, or reserved-guide reason. The move dialog is a destination review
  rather than a generic metadata form: it keeps the human title primary and
  compares current and target Collections. Exact current and expected paths sit
  behind a technical review disclosure, and an optional Git commit message
  remains available. Create, title edits, and move share one **soft uniqueness**
  rule: an exact NFC-normalized title (outer whitespace ignored, case and inner
  whitespace preserved) in the same Collection is a conflict in interactive UI.
  The notice prioritizes `Open existing`, then changing the title or Collection;
  an explicit low-emphasis action can still keep both. If title and body are
  both identical, use stronger duplicate-copy language. Visibly different titles
  that normalize to the same slug proceed automatically—the backend assigns a
  collision-safe path and the UI does not expose a filename editor. Interactive
  writes request server-side rejection first and retry with allow only after the
  user chooses the exception; legacy API, MCP, agent, and import callers retain
  lossless allow-by-default behavior. Submission
  preserves entered values on failure; after success the backend-returned path
  is authoritative, the reader replaces the stale route, and a compact status
  notice names the destination Collection. Git history, old-URI aliases,
  relations, and publication rewrites remain backend responsibilities.
  Composer images use a 10 MB transfer ceiling and the asset service's 12 MP /
  8,192px decode boundary. The client automatically fits oversized still PNG,
  JPEG, and WebP images to that resolution before upload so a highly compressed
  camera photo is not rejected based on decoded size alone; animated GIFs are
  never flattened silently. Upload errors stay inside the editor with an
  announced reason and explicit recovery: Retry appears only for transient
  failures, while Choose another and Dismiss are always available.
  Tables and images are atomic authoring blocks, so the editor always maintains
  a trailing paragraph as a keyboard and pointer escape route. An editable table
  owns one compact caption toolbar with labelled Row, Column, and Continue below
  actions plus a spatially separated Delete table control; GFM tables remain
  rectangular because merged cells cannot round-trip to Markdown. Removing a
  table restores focus to the following text block and remains reversible through
  editor Undo. Image removal is an always-discoverable neutral `X` inset at the
  image's own top-right corner—not the document measure's edge—and returns focus
  to the nearest text block. All block controls are omitted in read-only mode,
  use Lucide icons, expose accessible names, and never rely on hover alone.
  A document opened from Search uses this same reader inside a route-backed
  preview dialog rather than replacing the result ledger. Every launching
  surface uses the same viewport-centered geometry: from 640px, all four outer
  insets use `clamp(1rem, 2vw, 3rem)` (16–48px); narrower screens use a full-screen
  reader. Never measure background sidebars to position or size this dialog.
  Resizing reflows it with CSS, without a stale opening-time DOM measurement.
  The launching query, filters, scroll position, browser Back behavior and
  result focus remain preserved. Clicking the backdrop closes a reading preview;
  editing ignores backdrop/focus-outside events (including native file pickers).
  Inside the dialog, Preview and Raw use the same automatic reading measure and
  container-responsive insets as the full page. The compact file
  identity line exposes the current Vault as a labelled link, deliberately
  leaving the preview for that Vault Overview when selected. Its URL is still the canonical
  document route; clearing the preview history state promotes it to the normal
  full-page reader. Preview / Raw, Edit, save and Cancel retain preview state.
  Only Open in vault explicitly promotes the reader. Save/Cancel return to
  the previous reading mode, scroll position and Edit control. X, Escape,
  breadcrumbs and indexed browser Back/Forward protect unsaved work; navigation
  waits for active uploads/saves. Escape first dismisses an open editor menu.
- **File workspace**: use the shared location trail, single command row and flat
  full-bleed viewer. The breadcrumb title is the resolved human filename, not a
  repeated URI. Format and byte size are quiet command metadata; Download, Info
  and existing permission-aware lifecycle actions remain explicit. Info discloses
  description, uploader and creation date without an extra context band. Images,
  PDF, HTML, JSON and text retain the existing preview renderer and download /
  retry states. Resource discovery and body loading have separate compact
  `LoadingState` boundaries; neither recreates a large identity card. Account or
  access-revision changes clear previously resolved file labels and metadata.
- **Table workspace**: table routes use the same resource shell, but the data
  grid—not schema prose—is the primary canvas. The shared breadcrumb owns table
  identity; one command row holds permission state, row/column totals and Schema.
  Reader access remains explicitly read-only. Writer and higher roles receive
  one orange `Add row` action plus an always-keyboard-reachable trailing action
  cell for Edit/Delete; archived and external-git Vaults stay read-only even
  when the member role would otherwise permit writes. Row mutations use the
  structured REST endpoints—not a browser-composed SQL statement—and key every
  Edit/Delete by the system UUID so an unfiltered mutation is impossible.
  Add/Edit opens a schema-derived dialog with visible labels, type hints,
  inline validation, nullable state, and server-managed identity/audit fields
  omitted. Delete uses `ConfirmDialog`, keeps failures inline, and successful
  mutations refresh both the grid and explorer counts with polite status text.
  The records frame begins with one connected server-query bar: Filters opens a
  schema-aware condition dialog, active conditions remain visible as removable
  wrapping chips, and column-header buttons expose single-column sort with
  `aria-sort`. Page, 25/50/100 row size, sort, and repeated filters round-trip
  through the route query string so refresh, Back, and shared links restore the
  same view. Multi-value filters use comma-delimited input; when a declared enum
  choice itself contains a comma, the UI keeps exact `is` matching available and
  omits the ambiguous multi-value condition. The footer reports the exact
  visible range and total and provides first/previous/direct-page/next/last
  navigation. Server pagination—not a
  client-side slice or an all-row fetch—is the large-table boundary; prior rows
  stay visible and `aria-busy` during a transition. Every order adds the system
  UUID as a deterministic tie-breaker.
  Interactive edits and deletes add the row's loaded `updated_at` to their UUID
  predicate. A zero-row mutation is a concurrency conflict, never a silent
  success: Edit preserves the draft and offers Reload current values or an
  explicit Overwrite anyway. Overwrite revalidates and serializes the current
  visible draft rather than replaying the payload that first encountered the
  conflict. Delete refreshes the row and requires a second
  reviewed confirmation. Rows without legacy audit metadata retain exact-ID
  mutation behavior without claiming conflict protection.
  DDL remains a separately governed concern rather than sharing the frequent
  row-action surface: direct Alter/Drop is admin-only, while the idempotent
  migration contract retains its existing writer policy.
  Command metadata carries the table description when space permits. A
  single flat data surface fills the remaining height and owns both horizontal
  and vertical scrolling; its header and row-index column remain sticky, while
  cell values use sans/tabular typography unless the value itself is structured
  data. Responsive layouts preserve the grid via a focusable horizontal-scroll
  region rather than converting records into unrelated cards. Schema opens as a
  right overlay inspector and never reduces data width; it returns focus on
  Close, backdrop click, or Escape. The inspector reads as a compact data
  dictionary: a three-value overview exposes column, primary-key, and required
  counts before a semantic `# | Column | Data type | Constraints` table. Every
  row keeps stored order, identifier, type, and explicit Required/Nullable text
  on stable visual axes; primary-key rows add both a key icon/label and a
  restrained teal surface so color is never the only signal. On narrow screens
  this table scrolls inside the inspector rather than compressing or stacking
  unlike values. Loading, empty, query-error, and sampled-result states all
  retain the same outer frame.

---

## 10. Radius, elevation, z-index

- **Radius**: `--radius-sm .5rem` (chips, focus insets) · `-md .75` (buttons,
  inputs) · `-lg .875` (cards, panels, lists) · `-xl 1rem` (dialogs, hero) ·
  `-full 9999px` (pills, avatars, dots). Always reference the `--radius-*`
  tokens through Tailwind arbitrary radius utilities.
- **Elevation** (3-tier rule): `shadow-xs` hairline lift · `shadow-sm` resting
  cards · `shadow-md` popover/raised tile · `shadow-lg` modal/menu · `shadow-xl`
  hover peak. Cool-tinted in light, deeper alpha in dark.
- **Z-index ladder** (reference via the `--z-*` tokens): `base 0` · `raised 10` ·
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
  leads): `.app-header` (opaque neutral background + hairline), `body::before`
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

| Primitive                                              | Use                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Button`                                               | `default` (teal) · `accent` (orange CTA) · `outline` · `secondary` · `ghost` · `destructive` · `link`. Sizes `sm/md/lg/icon`. `loading` prop = spinner + disable + `aria-busy`.                                                                                                                                                                                                                                                                                                                                                                     |
| `Panel` + `PanelHeader`                                | canonical `surface` container (border + soft shadow). Default `card` keeps the family rounded-card treatment; `variant="workspace"` uses a tighter radius, flatter shadow, and soft header bar for dense Vault/file workspaces. `inset` (default) clips divided lists. For a `.card-hover` lift on rows inside a Panel, set `inset={false}`, re-round the end rows to the panel's selected radius, and stack the hovered row above its neighbours (`relative z-0 hover:z-10`).                                                                      |
| `WorkspaceSectionHeader`                               | shared cardless section anchor for Settings, Members, and Publish; uses a sentence-case H2, concise description, neutral hairline, and meaningful `TonalIcon` tone above a form or ledger panel, with an optional responsive metadata/action slot.                                                                                                                                                                                                                                                                                                  |
| `PageHeader`                                           | masthead: `font-display` title + muted subtitle + actions slot; `compact` is the Vault tool-page density, not a global heading reset.                                                                                                                                                                                                                                                                                                                                                                                                               |
| `WorkspacePageHeader`                                  | compact Vault identity row combining H1, canonical address, state, and actions without a landing-page hero. Its default bounded tool-row surface suits operational workspaces; Overview removes that surface and uses a bottom hairline so identity remains page-level above the first data panel.                                                                                                                                                                                                                                                  |
| `TonalIcon`                                            | small bordered icon chip for category or semantic scan cues. Content mapping: document/knowledge=`cat-1`, collection/people=`cat-2`, table/data=`cat-3`, file/guide/publish=`cat-4`, neutral/commit=`cat-6`; status uses `info/success/warning/destructive`. Color is always paired with a glyph and label, and whole header bands remain neutral.                                                                                                                                                                                                  |
| `VaultContextBadge`                                    | compact Vault identity marker using the shared `Box` glyph; name mode is human-readable sans, address mode is `akb://` mono and may expose a copy action.                                                                                                                                                                                                                                                                                                                                                                                           |
| `StatTile`                                             | labelled metric tile (big tabular numeral).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `Eyebrow`                                              | the `§ LABEL` mono coordinate label (`tone: muted/ink/spark`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `CodeSnippet`                                          | copyable code block with a soft header bar (insecure-origin safe).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `Alert`                                                | tinted notice banner — `destructive/warning/info/success` on the `-soft` quads; assertive `role=alert` for destructive/warning, polite `role=status` otherwise; icon + text always.                                                                                                                                                                                                                                                                                                                                                                 |
| `Badge` / `RoleBadge` / status badges                  | pill tags; outline + `*-solid` filled semantic variants; role/doc/system tones.                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `VaultChip`                                            | flat tinted **monogram** tile for a vault — a quiet identity anchor, **not** a glossy avatar or a `feat-*` hero. Swatch is a deterministic `--color-cat-*` picked by `hashHue(name) % 6` (the shared FNV-1a from `lib/utils`, §7), so a vault wears **one color** wherever its name appears — Recent rows and the vault directory. Fill `color-mix(in srgb, <cat> 14%, transparent)`, use the small radius token; `sm` (`h-5 w-5`) rides inline in a row, `md` (`h-7 w-7`) anchors a directory row. `aria-hidden` — the readable name always leads. |
| `Input` / `Textarea` / `Select` / `Label` / `TagInput` | form primitives — pre-rounded, teal focus ring, `aria-[invalid]` hooks.                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `Dialog` / `ConfirmDialog`                             | overlay primitives; creation flows for documents, Vault files, and tables stay modal and preserve the surrounding workspace. `ConfirmDialog` surfaces a rejected `onConfirm` inline (`Alert`) and stays open for retry.                                                                                                                                                                                                                                                                                                                             |
| `Tabs` / `Tooltip` / `Skeleton`                        | segmented control / hint / visual loading placeholder. `Skeleton` is presentation-only and belongs inside `LoadingState`, not as a standalone status.                                                                                                                                                                                                                                                                                                                                                                                               |
| `LoadingState` / `InlineLoadingState`                  | accessible async feedback. `LoadingState` owns one atomic `role=status`, hides its layout-matched skeleton children from assistive technology, and is used for page/panel/ledger discovery. `InlineLoadingState` is reserved for brief transitions or non-blocking refreshes where a structural skeleton would be misleading.                                                                                                                                                                                                                         |
| `Logo` + `.feature-tile`/`.feat-*`                     | brand lockup + per-capability gradient tiles.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

Shell: `Layout` (neutral `app-header` + responsive `AppSidebar` + content) and
`VaultShell` (header + content, collection tree in a left slide-over toggled by
the Tree button / ⌘\).

Member invitation uses one compact account ledger within `Dialog`, not a
two-column role console. Each row groups human name and contact on the left
with a neutral `MemberRolePicker` on the right, shared with the member roster.
Choosing a role selects that one account; it never writes access immediately.
Only the selected row shows its chosen role and a check, and choosing another
account clears the previous row. A quiet footer restates the person and role
before the explicit orange Invite action. Keep search/header and footer fixed
while only the account ledger scrolls. Existing members remain excluded;
search errors offer Retry rather than an empty-state claim. Pending invitations
disable search, role controls, clearing, and dismissal. No permanent role
explanation, duplicate selected-person card, or owner option belongs here.

The Publish route is a full-working-width, single-column public-link registry,
not a creation landing page. It shares Activity's thin 12–20px route gutter and
uses one workspace Panel so long lists remain scannable. A cardless
`WorkspaceSectionHeader` above the panel owns the warm publish glyph, link count,
total views, and Vault return action; the panel itself begins with policy and
continues directly into filters and the registry. Do not reserve a persistent
policy rail. Explain
the public/read-only boundary once in a compact policy line directly below the
registry header (`Public · Read-only · No sign-in required`), then expose
password, expiry, and view-limit state per link. Each ledger row carries a
stable, non-zero-padded tabular index so its position remains recognizable when
filters narrow the list. Search and resource-type filters appear only after the
registry exceeds eight links (progressive disclosure). Empty state copy sends
people back to a document, table, or file because publishing begins at the
source resource.

Activity follows the same single-column registry grammar as Publish but uses a
thinner route gutter so long subjects and paths retain working width:
one workspace Panel, a compact neutral ledger header with a neutral commit glyph,
and one short context line
instead of a page hero or persistent inspector. Its Git-backed ledger is newest
first and keeps subject, actor, primary path, change kind, short hash, and time
in each dense row. Author search and frequent-author Quick filters remain visible
for every loaded log instead of disappearing below a count threshold; the chips
wrap rather than clip on narrow widths.
The full route owns up to 50 results and tells users when the visible log is
capped; empty, loading, filtered-empty, and retry states retain the same panel
boundary so the workspace does not jump between states.

Search combines a quick-search modal with a dedicated refinement workspace.
On every route, the global trigger opens the existing modal for quick
lookup and document previews; its scope/query/kind carry into the full page.
On `/search` and `/vault/:name/search`, the common global title, location icon,
quick-search trigger and profile remain intact. A dedicated command row below
owns the URL-backed query form and explicit Search action. The compact
global trigger opens an independent quick lookup; opening or dismissing it must
not change the page's query or filters. The workspace uses the same cool-gray
`background` canvas and bounded `Panel` list as Vault Overview. A single
`max-w-6xl` frame aligns the query field and results panel, with the standard
12 / 16 / 20px responsive gutters. At `lg` and wider, the global location already
names the page, so the body H1 is visually hidden. Below `lg`, retain a small
Search text heading without an icon tile because the global location is hidden.

One bordered search field contains Vault scope, query, Search method, and the
quiet teal Search action. The full-page scope picker uses its `summary` variant:
All vaults, one Vault's name, or N vaults. Long names truncate inside a bounded
trigger; the existing menu keeps every selection visible and removable even
when the directory refresh fails. Quick search retains its existing inline
Vault chips. By meaning / Exact text options explain Semantic / Literal
behavior. Enter submits the same form as the integrated trailing Search action.

At an available workspace width of 45rem or more, scope / query / method /
submit share one row of 44px on desktop fine-pointer screens. Below 45rem, scope and method share the first
row, with query and submit below; this follows available container width rather
than the browser viewport. Query text is 16px in the stacked field and 14px in
the wide row. Scope names and selected counts cannot squeeze out the input.
Query clear, method, scope, and submit controls have 36px height on desktop
fine-pointer screens, retaining 44px on narrow/coarse-pointer screens. The entire
field shares one border and focus treatment. Keep the alignment frame's overflow
visible so its outer focus ring is not clipped; the padded viewport and results
panel own clipping and scrolling. Do not add a separate title toolbar,
context footer, or individual button outlines. The results panel follows with
a 12px gap.

There is no permanent refinement rail or resource-kind tab strip. A Result type
menu in the results panel header selects All content / Documents / Tables / Files
for Semantic search. The heading reads Results, or Exact matches for Literal
search, beside an honest result count. One Filters action opens the complete
refinement dialog. Collection, document type and tag controls stay inside that
dialog. It preserves independent Collection Apply, immediate metadata/state
changes, Literal options and Reset filters. Its body scrolls while its title and
Done action remain reachable. Escape returns focus to Filters. Active filter
chips remain removable above results when the dialog is closed, and removal
retains focus on a remaining chip or Filters.

Search owns its viewport and results scroll. The results panel follows content
height until the viewport fills, then scrolls below its fixed header. Result rows
use the same 14px dark semibold titles, teal resource glyphs, quiet dividers, and
16px panel inset as the existing workspace ledgers. A two-line excerpt follows
the title at 20px line-height; location, source type and compact metadata share a quiet line below.
Rows use 10px vertical padding. Filter dialogs use 16px insets, 14px titles and
13px form labels; their touch controls remain 44px.
Preview / Open occupies the trailing edge. Top match stays beside metadata.
Initial re-entry content shows recent queries, recent documents and suggested
searches inside the same panel, without a separate hero or nested outer card.
Keep the ready-state ledgers connected: each region has a compact, softly
filled `surface-2/60` header band, small semibold H3 label, and divided list rows
immediately below. Recent searches and Recently viewed share the upper row with
a continuous vertical boundary when the available workspace reaches 40rem;
stacked regions use a horizontal boundary. Omit absent ledgers. Recent items
retain their 28px tonal icon tiles in content-driven rows at least 48px tall. Suggested searches occupy a full-width
header band followed by three joined action cells at 40rem, or stacked cells
below it. Preserve wrapping labels and a visible forward arrow. The ready state
has no outer body padding: section bands and dividers reach the panel edges.
Section headers and list/action rows own a single 16px horizontal inset. Reserve
scrollbar space only when ready-state content overflows. Active pre-query filter
chips and notices keep their own 16px inset; searched results retain the normal
padded body and stable scrollbar gutter. Do not replace the bands with large
free-standing headings or separate suggestion cards.
No illustrative sidebar, invented facet totals, unsupported sort, or query
qualifier parser is added. Server contracts and saved URL state remain authoritative.
The shared multi-Vault picker lives inside the page query field, not in a second
scope-picker row above results. On a named Vault search route, an absent `v`
defaults to the route Vault; explicit `v=` selects All, and a comma-delimited
`v` retains a specific set through reload and browser history. The request uses
the existing repeated `vault` server predicate before ranking/limits.
The results canvas owns all empty, loading,
no-result, degraded, filtered-empty, retry, and
ranked-list states inside the same stable boundary. Its empty state reuses the
global panel's Suggested searches ledger, but real re-entry context leads when it is
available: user-scoped browser search history and recently viewed documents are
shown as connected ledgers, with inaccessible Vault history removed through the
existing Vault list. The history stores query/scope/mode and document identity
only—never result bodies—and silently disappears when browser storage is not
available. Semantic rows lead with a resource icon and linked title,
one focused excerpt, then location and source type/tags. Literal rows retain exact snippets,
body-relative line numbers and match counts. Stable result IDs and accessible
ranks preserve preview return focus. Optional backend
tags progressively add compact result badges and optional tag suggestions.
Tag entry remains available independently of loaded results. Indexed chunk headers and
markdown list markers are presentation metadata and are cleaned from semantic
previews, while Literal results keep their source text intact. Raw semantic
scores are ranking inputs, not percentages, so the UI never presents them as
confidence or relevance percentages. Filters are URL-backed server predicates:
source kind, collection (including children), document types, tags and archived
inclusion all re-fetch before the result limit. Document metadata filters target
documents only. Literal search uses document-body grep with optional regex and
case sensitivity; its exact counts are distinct from Semantic's top-K candidate
counts. Degraded empty responses never appear as a genuine zero-match. Confirmed
filter changes create browser history entries; pending requests retain the
previous ledger with explicit busy feedback and discard stale responses.
Document rows open the route-backed document preview described in the Document
workspace contract in both Semantic and Literal modes; table and file rows
continue to open their native resource routes. Closing with Escape, the close
control, backdrop click, or browser Back restores the exact ledger rather than
rebuilding the search from scratch. The preview's Vault link is the explicit
exception: it exits the result context and opens that Vault Overview.
The global app-header search is a real semantic search surface rather than a
form that immediately redirects to this route. Its field-shaped trigger opens
an accessible, header-attached command panel rather than a small centered
modal. The panel may grow to 96rem while retaining an 8–16px viewport gutter;
its strongly bounded query field leads into dense Suggested searches or Top
matches ledgers. Results expose source, Vault, path, and one line of context,
but never present raw semantic ranking inputs as percentages. The labelled
combobox auto-focuses, announces loading/result states, supports arrow-key
selection and Enter, closes with Escape, and returns focus to its trigger.
Only the result ledger scrolls on short viewports, leaving the query, filters,
close control and advanced-search action reachable. Arrow-key selection keeps
the active result in that ledger's viewport without scrolling the workspace.
The query field visibly includes the current search scope on every route:
up to two removable Vault-name chips and a chevron / `+N` scope disclosure
(All vaults when no individual scope is selected). Its
name-filterable checkbox list can select any number of accessible Vaults without
discarding the query or kind. The current route's Vault is labelled
separately from the selected search scope. The list is bounded and scrollable;
keyboard users can type a filter, use arrows and Enter to toggle, or Escape back
to the scope trigger without closing search. Loading failures offer local retry,
and unavailable directory entries are never synthesized from browser history.
A query matching accessible Vault names may offer up to five explicit Add Vault
suggestions below the field. Selecting one consumes that query as a Vault chip
and returns focus to the input; ordinary typing never changes the search scope.
The picker remains available to find names beyond those suggestions. Do not
implement GitHub qualifier syntax implicitly or turn resource search text into
filters without a deliberate selection. Recent-query history matches the exact
selected Vault set regardless of selection order. Full-page searches with an
advanced Collection restriction are not recorded in the legacy history format
that cannot preserve that restriction.
A compact row below exposes All / Documents / Tables / Files so people can set
intent before entering a query. The panel then shows recent queries for the
selected scope beside recently viewed documents in that scope, followed by
the shared suggestions. Recent document history is browser-local and must be
filtered through the current accessible Vault list before rendering; either
history block disappears cleanly when no valid entries exist. The source choice
persists into the result ledger, requests a server-filtered result set and carries
into the advanced-search URL. If the selected kind has no matches, the stable empty state provides a
`Show all results` recovery action rather than silently resetting the choice.
Selecting a document opens the same route-backed preview over the launching
page; tables and files go directly to their native resource. The full Search route remains
an explicit advanced-search destination for Literal mode, Vault scope, and type
filters; opening the search panel by itself never changes browser history.
The app header is the quick-search entry; Collection overflow instead scopes
the local sidebar name filter. The Vault menu has no duplicate search control.
Header openings default to the route's Vault, or all accessible
Vaults outside a named Vault route. A manual scope change remains in effect while
the panel is open and immediately invalidates old results; empty results never
silently expand the scope. An explicit `Search all vaults instead` action does.
Clearing recent queries affects only that user's matching search scope.
Account or route Vault changes reset the panel's query and results.
`Continue in search page` carries the query and content kind to the selected
scope's existing Search route. Both search scopes respect the app-level
unsaved-resource navigation guard before opening results or the search page;
document preview dismissal resumes its originating quick-search session with
query, Vault scope, kind and selected result intact. The search focus scope is
suspended during preview/edit, never stacked behind it. Page-launched previews
return to their result ledger. Promotion/account changes invalidate the handoff.

_Roadmap primitives_ (high-drift inline patterns being extracted): `IndexRow`
(numbered list row), `ToggleGroup`/`ToggleChip` (segmented selection),
`MetaList`/`MetaItem` (rail `dl`), `InlineCode` (single-token mono chip). Until shipped, match the
existing inline pattern and flag for extraction.

### Personal notification ledger

Personal notifications share query/mutation ownership and resource navigation,
but the header's approved Compact dropdown is distinct from the full inbox.
The bell anchors a nonmodal 404px Popover at every viewport width, bounded by
an 8px viewport inset. Let its height follow content, capped at 40rem and the
available viewport height; the feed scrolls while header and footer remain
visible. Do not add a category rail, fullscreen mobile shell, transparent
overlay, scroll lock, or focus trap. Outside actions work on their first click.
Keep the same mounted controller and surface when the viewport changes.

The compact header is 56px minimum: title, plain global unread text and two
quiet icon controls (Mark all notifications read / Close). No large Bell tile,
count pill, or separate text CTA. All / Unread are horizontal underline state
tabs; compact queries include every event category. The state is a server
predicate before pagination, not a local filter of one loaded page. Category
filtering belongs on the full page. Initial focus goes to the selected state
tab, not a tooltip-bearing header action, so the first Escape closes the panel.

Compact rows are a flat list divided by quiet hairlines, not individual cards.
Use a plain 16px event glyph, a 14px/20px title and 12px/16px event, Vault and
time metadata beneath it, with 12px vertical padding. Unread uses a heavier
title and a small trailing dot; the labelled dot control marks read. Read rows
have a check control revealed on pointer hover, keyboard focus and coarse
pointers, labelled to mark unread. These controls stay focusable while busy,
with aria-disabled and guarded handlers. If marking read removes a focused row
from Unread, focus its surviving neighbor or the Unread tab. Never invent
per-category counts, actors or role details from unavailable data. Link and
description IDs remain distinct between the panel and page.

Navigable compact titles retain foreground with teal hover/focus; notice-only
rows have neutral titles, Notice only metadata, no fake navigation and no row
hover fill. Read actions remain independent of resource navigation. The compact
footer is one 48px band: View all notifications on the left, a tooltip-labelled
Watched documents settings icon on the right. View all preserves read state
with category=all. Match the approved Lucide outline glyphs at 16px and 1.7px
stroke: FilePenLine / ArrowRightLeft / UserRoundCheck / Archive / Trash2 for
updated / moved / access granted / archived / deleted; CheckCheck / X /
Settings2 for header and footer actions. Ownership changes use UserRoundKey;
KeyRound remains reserved for access changes. Desktop fine-pointer header/footer
controls are 32px and row read controls are 24px with a 1px top inset. Keep
44px targets on small screens and coarse pointers. The header bell uses the
same stroke and a 5px unread dot, not a numeric badge; keep the exact global
count in its accessible name and the panel header. Empty-state glyphs are 24px.
Keep long titles wrapping, never fixed-height or clipped.

The full page keeps its single compact header aligned to a 56rem ledger,
visible Watched documents action, All / Documents / Vault access underline
category tabs and independent Unread only checkbox. Categories are server
predicates before pagination, with unknown support shown as a recovery notice.
Its dated Today / Yesterday / Earlier groups, row cards with TonalIcons,
per-row read menus and header Mark all overflow remain available for detailed
review. Retention information belongs only at the full ledger's foot. Mark all
uses the observed global snapshot in either view. Unknown counts show loading
or unavailable feedback, never claim zero; pending actions retain focus and
prevent duplicate writes.
The app-header notification and account controls use content-sized, non-shrinking
slots, never share a fixed account-width box. Account names stay visible from
`sm` (bounded only for unusually long names); smaller screens use the avatar
with the full identity available in its labelled account menu.

### Workspace search-update status

- The header, Vault Overview badge and Settings Operations consume the same
  account-scoped observation owner. Foreground identity and directory proof
  precede private status; revoked rows disappear immediately. Refresh is a
  deduplicated GET observation, not retry, reindex, or a mutation.
- `PendingIndexingBadge` is passive, content-sized, semantic text with a small
  progress glyph and tabular count. The global header owns polite announcements;
  Overview uses the same badge scoped to its own Vault without a second live
  region. Hide zero/unknown-only totals rather than showing a persistent status
  control. `N+` means a known pending-chunk subtotal, explained in both the title
  and accessible text; it never mixes units or counts affected Vaults.
- Detailed stage observations live only in Vault Settings → Operations through
  `SearchStatusStages`. Keep stage units, pending, retry subsets, and
  stopped/final-retry distinctions separate. Unknown configuration is not an
  inferred pause, and legacy historical failures are not current-Head failures.
  No progress percentage, dense-search readiness, or save-success claim may be
  derived from queue counts.
- Home has no status rail, summary-card row, or additional processing notice.
  Its optional floating connection guide and cardless inventory summary remain
  independent of indexing observations.
  Do not introduce a global status modal or a mobile account-menu entry.

### Loading-state contract

- Initial page and panel discovery keeps the final outer shell, masthead, rails,
  and major content geometry in place. Use a layout-matched skeleton inside one
  `LoadingState`; never return `null` or replace a whole page with a centered
  loading word.
- A refresh, filter change, or background revalidation preserves the last
  successful content. Mark its owning region `aria-busy` and use
  `InlineLoadingState` near the initiating control; do not blank the ledger or
  block unrelated navigation.
- Loading, empty, error, and ready are distinct states. A failed initial request
  replaces its skeleton with a recoverable `Alert`; a failed background refresh
  keeps the prior content and adds an inline error with retry where practical.
- Skeleton dimensions approximate the final content at every breakpoint so
  async completion does not cause avoidable layout shift. Skeleton blocks are
  decorative, contain no fake readable copy, and inherit the global
  reduced-motion rule.

---

## 13. Accessibility floor

| Rule                          | Contract                                                                                                                             |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Contrast**                  | text ≥ 4.5:1, large/UI ≥ 3:1, in **both** modes. Tokens are pre-verified — re-verify when you adjust one.                            |
| **Color not the only signal** | pair every status color with an icon or text label.                                                                                  |
| **Focus ring**                | every interactive element keeps the `focus-visible:ring-2 ring-ring ring-offset-2` pattern (icon buttons included). Never remove it. |
| **Icon-only button**          | `aria-label` required + `<Icon aria-hidden />`.                                                                                      |
| **Labels**                    | every input has a visible `<Label>` or an `sr-only` label; placeholder is not a label.                                               |
| **Form validation**           | `noValidate` forms with the app's own copy, never browser bubbles. Each invalid field gets `aria-invalid` and a message under it via `aria-describedby`; focus the first invalid field on submit. A new password shows its rule up front and has a confirm field. Form-level server results use one `Alert` (info for success, destructive for failure). |
| **Async / loading**           | Use one `LoadingState` per async boundary (`role=status aria-live=polite`); preserve successful content during refresh, mark its owner `aria-busy`, and surface errors with `Alert`. |
| **Destructive action**        | `ConfirmDialog`, never `window.confirm()`.                                                                                           |
| **Reduced motion**            | respected globally — don't override.                                                                                                 |

---

## 14. Anti-patterns

- ❌ Raw hex / `hsl()` islands in components (use tokens; the guard blocks hex).
- ❌ New arbitrary `text-[Npx]` / `leading-[…]` (use the scale / nearest step).
- ❌ `/opacity` or `color-mix` as a _state_ (use `surface-hover/active/selected`).
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

### Audit refinements (2026-10-14)

- The no-selection Vault workspace is a usable directory, not an onboarding
  hero for returning users. Name filtering, favorites, ownership, direct links,
  and progressive reveal remain usable without the sidebar. A genuinely empty
  account gets one first-Vault action and an invitation alternative.
- Overview context cards flow as two columns below the activity ledger at
  medium workspace widths, and one right rail when the workspace has room.
- Guide preview uses one outer panel and a quiet divider for metadata; do not
  add another framed card around the rendered body.
- Search keeps title/excerpt/location-and-metadata hierarchy, with at most two
  excerpt lines. Preserve the full document in preview, not in the result row.
- Narrow editor toolbars keep every command mounted in a horizontally
  scrollable row and offer an explicit expand/collapse control. Keyboard
  roving focus, selection, and Visual/Markdown state must survive expansion.
- Public document attribution sits below its title. Optional classification
  uses a native disclosure, not a permanent metadata column. Authored headings
  stay intact to preserve meaning and anchors.
- Public-link lists progressively reveal 30 loaded rows; distinguish display
  limits from server pagination. Availability stays primary, type/delivery are
  subordinate, and the technical address is available on request.
- Keep the default light wordmark gradient, but use the foreground token for
  its dark-mode text: a dark teal gradient is not readable against slate.
