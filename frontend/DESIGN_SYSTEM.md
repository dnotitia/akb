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
  human-title trail. Vault section menus never occupy this global header. An
  ordinary page label is preceded by a quiet 16px Lucide page icon, matching its sidebar entry;
  the adjacent text supplies its accessible name, so the glyph is decorative. It is
  location chrome, not another H1 or a card. Long names truncate without
  displacing account actions. Working pages have one dedicated section-navigation
  row below the header; full resource readers retain this same row. All named
  Vault pages share `VaultBreadcrumb` with resource readers: one leading Box,
  text-only path and current label, matching separators, spacing and emphasis.
  Ordinary Vault sections do not repeat their tab icon in the trail; only
  Document / File / Table titles have a trailing kind marker, including loading
  fallbacks. Standalone Home, Search and account Settings retain their page icon.
  Mobile keeps the logo and a compact
  location row with a labelled navigation drawer toggle below the app header.
  All desktop routes use a 20px header inset after the last navigation rail,
  independent of their content gutters. Home keeps its generous body/footer
  inset without shifting the location label. Account Settings shows the selected
  section (Profile, Agent connections, etc.) rather than repeating the rail's
  Settings heading. Section names, icons, and permission fallback share one contract.
  A collapsed rail retains the logo symbol and accessible Home
  link. Mobile retains its full-width header with the AKB mark/wordmark and compact
  navigation, without the long product subtitle. Between `sm` and `lg`, Search
  can shrink within the remaining header width; the account control must remain
  fully inside the viewport. Desktop Search keeps its 256px width and the
  explicit `Search all vaults…` label; its accessible name retains that scope
  when narrow layouts show only the search glyph. The global row and Vault
  section navigation in Vault workspaces share an opaque `background` surface, separated from the
  `surface` reading area by one bottom hairline. On desktop, omit the visual
  divider between those two rows without changing their 56px/40px geometry or
  the full-height navigation rails. Home instead uses the common header's `paper`
  variant to continue its `surface` canvas; other workspace routes retain their
  existing treatment. Global tools share 36px controls and small
  token radii. The account trigger is a quiet avatar/name/chevron disclosure,
  not a raised outlined card; keep the name from `sm`, with full identity in the
  accessible name, tooltip and open menu. Do not add unrelated shortcut buttons.
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
  toggle. The user's choice is remembered locally. Vault routes always use the same 3.5rem icon
  rail beside the existing Vault/Collections navigator;
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
  Settings is the bottom fixed entry into the secondary account-settings rail;
  it stays selected across all `/settings?tab=` sections, including compact mode
  with a labelled icon and tooltip. Re-selecting it preserves the current section.
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
  uses neutral 16px tabular values and 14px labels for accessible Vaults, documents,
  tables and confirmed files; mobile reflows into two text columns, not cards.
  Do not aggregate the four preview cards, count table rows, or add Collections.
  One authenticated count-only snapshot owns totals; missing values remain `—`
  with one quiet availability explanation. Older servers retain the verified
  directory's Vault count only. A verified empty workspace shows only zero Vaults.
  Foreground identity proof gates the summary, then refreshes directory and totals
  even for unchanged local accounts. No private totals persist in browser storage.
  Leave 20px before the first section without double section padding. Home has an
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
  (16px on mobile), respecting safe areas. Reserve compact-only bottom clearance;
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
  navigation width and tracks both resize handles and collapsed states. The
  resource command row remains below it; desktop reading controls remain `h-8`.
  Overview, Search, Graph, Public links, Members, Settings, Activity and full resource readers share one
  flat section-navigation row above their working content and outside its scroll
  region. Its real links begin with Overview / Search / Graph / Public links.
  Members / Settings immediately follow the content destinations, with a short
  neutral divider before Members rather than a large gap across the workspace.
  The Search destination visibly says `Search`; its enclosing Vault navigation
  supplies the scope, while the global field explicitly says `Search all vaults…`.
  All links and More follow the same left-to-right flow.
  The divider's padding participates in overflow measurements, not unmeasured margins.
  When links no longer fit, measure the actual label/icon widths and move the
  trailing destinations into a labelled More disclosure. Keep Overview and the
  actual current destination visible where both fit; at very narrow widths the
  current destination takes priority. Preserve the original link order.
  This is purpose-based grouping, not measured usage frequency or a permission gate.
  Use 14px icon-and-label links, neutral hover, and a
  teal underline plus stronger weight for the current destination. Desktop links
  are 40px high; mobile links are 44px. The row never wraps or creates horizontal
  page scroll. The nonmodal More menu uses real destination links, supports
  keyboard navigation and Escape, and does not lock the page scrollbar.
  Resizing must restore focus if a destination moves into overflow.
  Overview owns a selected
  state on the exact Vault root; Activity does not select it. Both the Vault list
  and Vault breadcrumb also return there, including when reselecting the current
  Vault. Document, File and Table retain the same navigation without falsely
  selecting Overview; their breadcrumb and tree identify the current resource.
  Creation and redirect routes omit the row. Previews do not mount it inside
  their dialog; the launching workspace remains inactive until dismissal.
  No additional global or Collections menu duplicates these destinations. If the chosen rails leave too
  little space for search/indexing/account controls plus a readable trail, the complete Vault
  navigator temporarily uses its existing drawer. The header exposes its toggle;
  Escape/backdrop dismiss it. This does not write saved widths or collapse
  preferences, and expanding the workspace restores inline rails. Do not oscillate
  based on the drawer's own rendered width.
  Mobile retains its existing drawer and `h-10` navigation headers.
  Named Vault routes retain the user's Collections preference rather than
  automatically collapsing it by page type. The collapsed rail leaves only
  a labelled Expand collections control; no extra icon menu is invented.
  A Collection deep link may temporarily reveal the tree without overwriting
  the saved preference. The explorer tree scrolls independently; its identity,
  management and filter rows remain aligned with Vaults. Avoid a repeated title/description
  masthead: Overview alone uses the compact Vault identity header. Operational
  routes begin directly with the control or data surface that performs the task
  (search form, roster toolbar, publication list, settings local nav) and retain
  an `sr-only` H1 for route orientation and heading hierarchy. The active Vault
  tab and breadcrumb already provide visible location context; do not repeat it.
  Account Settings (`/settings`) uses a full-height 220px secondary navigation
  rail beside Workspace. Its 56px identity row aligns with the app header;
  the header begins after both rails. Single-line section links replace the
  old profile/menu card and page masthead. The content owns scrolling with
  16/24/32px responsive padding, cardless section headings and hairlines,
  an avatar/identity block, profile and password columns on wide screens,
  compact appearance previews, and wide data ledgers. Agent connections keeps
  three explicit setup phases visible from entry: prepare access, configure,
  and try a read-only request. Wide screens place them alongside one another;
  narrow screens stack them in order. Never hide the final phase until token
  creation or offer a placeholder secret as a usable configuration.
  Mobile replaces the secondary rail with a labelled section selector.
  Preserve `?tab=` links and guard section changes with unsaved edits or a
  newly generated token; never persist token secrets to retain them.
  On wide screens, Vault Settings is a full-height three-pane workspace that runs
  edge-to-edge inside `VaultShell`. Unlike Members and Publish, it does not add a
  second route inset or rounded outer frame: the persistent Vault rail, local
  settings navigation, and context rail already provide enough structure, while
  the form column keeps its own 20–28px reading inset. The panes are separated by
  structural hairlines rather than wrapped in another card. The local navigation
  stays pinned, the form column owns the primary scroll, and the Vault context
  rail remains available with its own overflow only when its content exceeds the
  viewport. The context rail is an edge-to-edge inspector:
  its compact Current vault, About, and Operations headers use neutral
  `surface-2` layering; category/state color stays inside the leading icon or
  labelled status, while addresses, badges, and metrics stay on the neutral
  surface. Its groups use horizontal hairlines rather than inset
  floating cards. Both internal scroll axes use the thin, transparent-track
  `rail-scroll` treatment rather than platform-default gray scrollbar slabs.
  Narrow layouts return to one document flow and restore card spacing for
  separation.
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
  triggers have a 36px baseline, and both lists use the same rail scrollbar.
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
  node Vault. Small single-kind Collections stay flat and compact. The details
  action is the stable place to review the complete typed counts and Collection
  summary and, when supported by the connected backend, edit it.
  Older backends keep the current summary readable
  and surface an explicit compatibility notice instead of failing silently.
  A read-only permission notice is a flush, full-width policy bar directly above
  the three panes, with a bottom hairline and no card margin or rounded shell.
  In the primary form column, each governance section uses a cardless section
  anchor above its settings panel: a sentence-case H2, one meaningfully toned
  icon, a concise description, and a full-width neutral hairline. The bordered
  panel below contains only controls and feedback, so headings remain easy to
  scan instead of reading as the first row inside each card. Generous
  inter-section rhythm separates scopes without implying a numbered workflow.
  Danger zone keeps its semantic in-panel warning header because that boundary
  communicates risk rather than ordinary section hierarchy.
- **Vault Overview workspace**: Overview is a full-height Vault workspace rather
  than a stack of dashboard cards. Its identity is a cardless page-level anchor:
  Vault icon, H1, copyable address, role, applicable public/read-only/mirror state,
  pending-indexing badge, and optional wrapping description. Immediately below,
  passive content-sized totals sit on the left and existing creation buttons on
  the right in one wrapping row above a neutral hairline. Give totals the same
  compact 32px minimum height and 8px spacing as the buttons, without implying
  they are interactive. Preserve the 12px mobile, 16px intermediate, and 24px
  desktop route gutter; do not add an Inventory band, equal-width stat cells, or
  another enclosing card. Graph-link health stays out of Overview.
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
  link. Owner/visibility remain noninteractive; the visibility glyph and brief
  policy text distinguish signed-in public access from anonymous Public links.
  The Members shortcut uses a full-width hit area, a visible chevron, neutral
  hover and inset keyboard focus without moving their bounds. Do not tint entire
  sections or add hover effects to passive information. Contents is a passive definition list of
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
  repeated page masthead. A connected two-row command bar owns the Vault-scoped
  resource search, Graph/List view switch, on-demand Filters, saved views,
  focused-neighborhood breadcrumb, hop control, and honest resource/relation
  counts. Types, relation kinds, hidden resources, and orphan decluttering live
  behind Filters rather than in a permanent left rail; hop depth appears only
  after a resource becomes the focus. The graph canvas owns the remaining
  workspace, with only a compact bottom zoom/fit group and collapsed visual key.
  Selecting a node opens a 23rem overlay inspector and never resizes or reheats
  the canvas. The inspector prioritizes direct relationships and the explicit
  Open / Focus actions; summary, preview, and metadata progressively disclose
  below them. Whole-Vault search changes to a focused neighborhood while click
  selection remains highlight-only. A first-class List view is the keyboard and
  assistive-technology source of truth, progressively revealing at most 50 rows
  at a time. Drag, double-click, and right-click remain accelerators only; every
  essential action has a visible single-pointer/keyboard route. Mobile defaults
  to List and presents the inspector over a dismissible backdrop. Empty Vault,
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
  decorative avatars. Role selectors keep a usable 36px target with neutral
  surfaces and foreground text in both themes, not competing role-colored fills.
  Filtering appears for more than eight members and remains visible while active.
  Keep invite, role changes with Undo, revoke and ownership-transfer confirmations.
  Reader and unknown-role views do not expose management controls; row actions
  wait for the current user identity and exclude the owner and self.
  The ledger ends after its content, without a forced full-height frame or nested
  roster scrollbar. The shared route viewport scrolls long member lists.
- **Resource reading shell**: Document, File and Table routes use one resolved
  location trail (`Vault / Collection ancestry / human title (kind)`) and one
  `ResourceCommandRow`. Documents separate their reading tools inside a shallow
  inset viewer frame; File and Table retain their flat full-width canvas. Desktop puts the
  trail in the existing 56px app header. The shared Vault section-navigation row
  stays below that header; a separate command row owns only resource actions
  and optional metadata. Do not add a resource-only Explore menu or global menu list.
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
  The current title is passive text, never a second navigation menu or an
  expansion control. Truncated titles reveal their full value on hover/focus;
  Document info also wraps the full title for touch access.
  Preserve the parent and resource title before secondary ancestry.
- **Document workspace**: the location trail is the sole visible document
  identity. Keep one semantic `h1#doc-title` without a second title card, and
  preserve authored body headings and their anchors. A compact cardless context
  row owns Draft, Last edited, an optional labelled `Summary: …`, and trailing
  Publish / overflow. A separate toolbar belongs to the viewer frame below it:
  Preview / Raw and line/byte statistics lead, while Copy / Edit trail. Read modes are
  compact segmented tabs on a neutral inset with a token-selected surface, not
  a second underline navigation row or solid primary button slabs. Copy/Edit
  share the same labelled ghost-icon treatment and keyboard/hover tooltips.
  Separate selection and action groups spatially rather than adding another
  title, commit band, or context menu. Publish keeps a
  visible text label and opens options before any public write. All commands share 14px medium type, small token
  radius and the same height: 32px above 48rem of available command space, 44px
  below. At less than 32rem, statistics yield their space; the optional summary
  appears only above 48rem of reader width. Each row keeps its essential actions
  on one line. Open overflow uses selected-state tokens.
  Preview promotion sits beside navigation, not among read-mode commands.
  Last edited, statistics and the optional one-line summary yield before
  primary actions; all remain available in Info. Last edited uses the API's
  `updated_at`, with an exact localized date and timezone on hover/focus. Missing
  or invalid dates remain absent in the toolbar and say Not available in Info.
  Historical/diff views instead show Version saved from the matching history
  entry; the live metadata timestamp must never be presented as the version date.
  The summary opens a full-text
  dialog, and failed clipboard access offers manual copying rather than silence.
  Watch, Pin and Standard / Wide reading-width choices share a named
  group in overflow above the existing location/lifecycle actions. Keep exact
  permission reasons and archived/historical/diff notices visible.
  The working area has an 8px mobile / 12px desktop inset, without a grey moat.
  Only toolbar, article and context rail share one thin border, small token
  radius and no card shadow. The toolbar uses `background` and the article
  `surface` in both themes. Do not frame the title or add a duplicate context
  band or forced short-body height. Loading reserves both rows and the frame.
  Start the body with 16–24px horizontal inset and a 20px top inset; its first
  authored block has no additional top margin. Rendered body text uses 16px /
  1.65. Headings, paragraphs, lists, quotes and ordinary tables share a centered
  64rem measure. Wide removes that measure. Target the actual
  `.ProseMirror` / `.tableWrapper` DOM, not legacy direct-child selectors.
  Code and wide tables scroll inside their own labelled, keyboard-focusable
  regions. Small images retain natural dimensions and aspect ratio.
  Context has one entry per view in a 3rem right-edge icon rail: Document info,
  Table of contents, Relations and Version history. Do not duplicate these entries
  in overflow or add another header Info button. Each control has a tooltip,
  accessible name, 44px target and expanded state; pressing the selected control
  closes its panel. With at least 48rem of actual reading width and 20rem of
  height, context floats immediately left of the rail in a 24rem, content-sized
  panel with a bounded independently scrolling body. Opening it never changes
  article width, line wrapping or scroll position. Use opaque surface, a quiet
  border, small radius and soft shadow, without a backdrop or scroll lock.
  Outside click and keyboard focus leaving for the article close it without
  swallowing the outside action or restoring focus away from its target.
  Otherwise it opens as a modal right overlay (max 24rem) with backdrop, focus
  containment and its own context controls while the background rail is
  inaccessible. Each view owns the available panel height, without nested tabs.
  Close/Escape restore the selected edge control. Child editing dialogs and
  their portalled menus must not dismiss the inspector or lose drafts when the
  viewport changes. Following an outline link dismisses either mode and focuses
  the authored heading, scrolling only the document canvas (never the app shell
  or sidebars, including when a short article needs no scrolling). Raw and diff modes explain why heading navigation is
  unavailable instead of exposing inactive links. Edit hides the rail. Author,
  full timestamps, tags and technical identifiers live in Info, not a new band.
  Publication management uses its own verified writer-or-higher policy, rejects
  read-only Vaults and historical/diff views, and preserves exact server errors.
  Existing links use a Public link label, not a claim of live availability:
  expiry, passwords and view limits may apply. Its disclosure offers a selectable
  URL, honest copy feedback/manual recovery, Open link, the publication registry,
  and an explicit confirmation before removing all document public links.
  Publishing and existing-link disclosure share a nonmodal, 340px popover anchored 8px below the
  toolbar button's right edge. Constrain and reposition it at viewport boundaries;
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
  popover's Unpublish action. On narrow command rows, omit the decorative
  globe before sacrificing the label or wrapping when publication completes.
  Edit uses the context command row for visible draft/upload state and Cancel /
  Save changes. It never adds another `Editing document` band. Clean Cancel exits;
  unsaved Cancel retains its confirmation. Same-account access verification must
  preserve the live editor draft even if a permission-scoped query remounts it;
  switching accounts resets local editor state.
  Full-viewer Vault links ask before leaving unsaved edits and keep the existing
  local-draft recovery behavior. Keep editing is always available; navigation
  cannot abandon an active save or image upload. Modified/new-tab link gestures
  remain ordinary browser navigation and do not dismiss the current editor.
  Edit owns the human title and body as one document form: its title field sits
  immediately above the editor and one Save changes action patches whichever
  values changed. The system-created slug/path remains stable, so editing the
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
  preview dialog rather than replacing the result ledger. The dialog leaves the
  persistent Vault navigation visible on wide screens, becomes full-screen on
  narrow screens, and preserves the launching query, filters, scroll position,
  browser Back behavior, and result focus. On wide screens it retains a clear
  dismissible backdrop gutter (at least 32px where no persistent rail occupies
  that edge); clicking that backdrop closes the preview. The compact file
  identity line exposes the current Vault as a labelled link, deliberately
  leaving the preview for that Vault Overview when selected. Its URL is still the canonical
  document route; clearing the preview history state promotes it to the normal
  full-page reader. Preview / Raw and version changes retain preview state,
  while Edit deliberately promotes to the full page so unsaved work never lives
  in a dismissible reading overlay.
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

Member invitation is a two-stage permission console within `Dialog`: searchable
AKB accounts form the left ledger; the selected identity and descriptive role
cards form the right review pane. A single footer sentence restates the person
and role before the orange Invite action. On narrow screens the same order
stacks into one flow; existing members remain excluded from results.

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

Search is an advanced, single-ledger workbench rather than a hero or a permanent
two-pane inspector. It uses the Graph explorer's full-bleed, viewport-locked
workspace grammar instead of sitting inside a second route card or page inset.
A connected two-row command bar leads the page: the first row owns the strongly
bounded query field, Semantic / Literal mode, on-demand Filters, and Search
action; the thinner second row owns Vault scope plus honest loading/result
status. The remaining height belongs to one independently scrolling,
full-working-width results ledger. Source-kind and document-type filters stay
behind the labelled Filters control and remain available before searching and
after zero results; active filters remain visible through the control's count badge. This
keeps refinement close to the query without allowing a fixed rail to tax every
result row or compete with the independent Collections rail. On narrow screens,
the first row wraps controls below the query without separating mode from its
label; Vault scope and status stack within the second row rather than truncating
between adjacent controls. Before a query runs, the results canvas uses a quiet
`surface-2` field and centers recent/suggested re-entry content in one bounded
ledger, preserving clear side gutters and separation from the command bar. Once
results exist, that inner cap disappears and rows use the full working width.
The results canvas owns all empty, loading,
no-result, degraded, filtered-empty, retry, and
ranked-list states inside the same stable boundary. Its empty state reuses the
global panel's Suggested searches ledger, but real re-entry context leads when it is
available: user-scoped browser search history and recently viewed documents are
shown as connected ledgers, with inaccessible Vault history removed through the
existing Vault list. The history stores query/scope/mode and document identity
only—never result bodies—and silently disappears when browser storage is not
available. Each semantic or literal row uses a stable,
non-zero-padded rank followed by source identity, compact location, one focused
match context, and a calibrated label or literal match count. Optional backend
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
Before typing, a compact `Search in` row exposes All / Documents / Tables /
Files so people can set intent before entering a query. The panel then shows
user-scoped recent global queries beside recently viewed documents, followed by
the shared suggestions. Recent document history is browser-local and must be
filtered through the current accessible Vault list before rendering; either
history block disappears cleanly when no valid entries exist. The source choice
persists into the result ledger, requests a server-filtered result set and carries
into the advanced-search URL. If the selected kind has no matches, the stable empty state provides a
`Show all results` recovery action rather than silently resetting the choice.
Selecting a document opens the same route-backed preview over the launching
page; tables and files go directly to their native resource. The full Search route remains
an explicit advanced-search destination for Literal mode, Vault scope, and type
filters; opening the global panel by itself never changes browser history.

_Roadmap primitives_ (high-drift inline patterns being extracted): `IndexRow`
(numbered list row), `ToggleGroup`/`ToggleChip` (segmented selection),
`MetaList`/`MetaItem` (rail `dl`), `InlineCode` (single-token mono chip). Until shipped, match the
existing inline pattern and flag for extraction.

### Personal notification ledger

Personal notifications use one shared ledger in the header panel and full inbox.
Use two header bands: title and overflow actions, then text-only category
underline tabs (All / Documents / Access) with an independent Unread only
checkbox. Allow the filter band to wrap on narrow screens. Do not mix read state
and event categories in one tab list.
Category filters are server predicates applied before pagination, not a filter
over the first loaded page. The full inbox URL and the panel's View all action
retain both filters. The header overflow groups Mark all notifications read
(explicitly global) and Notification settings. Unknown server category support
shows a recovery notice, never a misleading filtered-empty result. Rows stay
title-first with a short event label and non-repeated Vault context; unread
weight and a narrow indicator complement accessible state text. Times and
always-accessible read actions sit side by side at the trailing edge. Group rows
under Today / Yesterday / Earlier using local calendar boundaries, preserving
server order. Avoid repeated row dividers and icon boxes. Category color stays
on compact glyphs; do not tint entire rows or add invented counters.
Navigable titles use link color and an always-visible arrow, with destination
purpose in the accessible description. Notice-only rows retain neutral titles,
say Notice only beside the event label, and have no row hover treatment or fake
disabled navigation button. Read/unread controls remain available independently.
Use 8px vertical row padding, 14px/20px titles and 12px/16px metadata separated
by 2px: a normal two-line notification is approximately 54px tall. Keep long
titles readable rather than fixing the row height. Header bands use 48px and
36px minimum heights; read controls remain 36px.
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
