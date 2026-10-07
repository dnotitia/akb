# AKB login theme for Keycloak

The sign-in pages of AKB's bundled Keycloak, in AKB's own look: the same
tokens, typeface, light and dark schemes, and layout as the app. It is a
Keycloak login theme named `akb`, with `keycloak.v2` as its parent.

## Layout

```
keycloak-theme/
  akb/login/              the theme as Keycloak loads it
    theme.properties
    *.ftl                 templates that replace Keycloak's (see below)
    messages/             English and Korean
    resources/css/        akb-tokens.generated.css, akb-vocabulary.css, akb-login.css
    resources/js/         theme preference (sync, in <head>) and page behaviour
  scripts/
    theme-tokens.mjs      src/index.css -> akb-tokens.generated.css
    assemble.mjs          theme + Pretendard + asset hash -> dist-keycloak-theme/akb
    upstream-hashes.mjs   sha256 of every Keycloak original this theme replaces
  upstream/               the recorded hashes, per Keycloak version
  tests/                  static checks (run with the frontend's unit tests)
  harness/                a real Keycloak and Playwright
```

## Tokens: three layers

1. `akb-tokens.generated.css` is generated from the app's `src/index.css`
   (`@theme`, `@theme static` and `.dark`), every name prefixed `--akb-`.
   Never edit it; run `pnpm run theme:tokens`.
2. `akb-vocabulary.css` maps those onto the names the theme uses
   (`--color-accent`, `--color-text-primary`, `--radius-control`, ...).
3. `akb-login.css` uses only vocabulary names. It also points PatternFly 5's
   variables at them, so the pages this theme inherits from `keycloak.v2`
   match too.

The static checks hold the seams: no colour literal outside the generated
file, no `--akb-*` reference outside the vocabulary, every name resolves, and
the generated file matches `src/index.css` today.

## Templates

Replaced, with what differs from the original:

| Template | Original | Change |
| --- | --- | --- |
| `template.ftl` | `keycloak.v2` | AKB frame: brand panel, card, language and theme controls. Keycloak's scripts (session checker, password visibility, once-links, import map) are kept as they are. Its dark-mode script is replaced by `akb-theme-init.js`, which sets the same `pf-v5-theme-dark` class but follows an explicit choice as well as the system. |
| `login.ftl` | `keycloak.v2` | Identity providers first, then the realm's own accounts. A client with the attribute `akb.login.native-only=true` (AKB's administration client) gets the password form only: a provider sign-in would be refused there. |
| `login-oauth-grant.ftl` | `keycloak.v2` | Names the client in the title and lists what it may do; Allow and Deny. |
| `login-page-expired.ftl` | `base` | Resumes once per browser tab by itself, and explains from the second time (see below). |
| `error.ftl`, `logout-confirm.ftl` | `base` | Same content, AKB layout. |
| `info.ftl` | `base` | Same content, AKB layout; a message with no separate header is shown once, not as both title and body. |
| `login-config-totp.ftl` | `keycloak.v2` | Copy with one fix: both labels point at their inputs (upstream they point at an id that does not exist). |

`theme.properties` also defines three classes that pages inherited from
`base` use and `keycloak.v2` leaves undefined (`kcButtonDefaultClass`,
`kcButtonLargeClass`, `kcFormButtonsClass`); without them those buttons have
no variant.

**"Page has expired".** When a browser sends the same sign-in step twice
(a double click, an extension, a retried request), the second request finds
its code used and Keycloak shows this page. The theme takes the person back
into the sign-in once per tab (`sessionStorage`), and from the second time
explains what happened instead, so a loop is impossible. Without
`sessionStorage` it explains straight away.

**Theme preference.** A `ui_theme=light|dark|system` parameter on the
authorization request (AKB's apps pass their current choice), then the
choice made on these pages before (`localStorage` key `akb_theme`), then the
system. The parameter is stored because Keycloak drops it on form posts.

## Messages

`messages_en.properties` and `messages_ko.properties` hold the theme's own
keys (all `akb*`) and one Keycloak key, `offlineAccessScopeConsentText`.
Keycloak formats every message, so a literal apostrophe is written `''`.
A consent screen lists scopes by their consent text, because Keycloak gives
templates no scope names: write a scope's consent text as `${key}` to have
it translated from these files.

## Building

```bash
pnpm run theme:assemble    # -> dist-keycloak-theme/akb
```

The output adds Pretendard (the `pretendard` package's dynamic-subset build,
SIL Open Font License 1.1, licence at `login/resources/fonts/pretendard/OFL.txt`)
and sets `assetVersion` in `theme.properties` to a hash of everything shipped.
Keycloak serves theme resources with a long cache lifetime; the hash in each
URL makes a changed file a new URL.

## Checking

```bash
pnpm run test              # includes keycloak-theme/tests
pnpm run theme:harness     # docker: real Keycloak + Playwright
```

The harness (`harness/run.sh`) assembles the theme, checks the recorded
originals against the pinned Keycloak image, starts that image with the
theme mounted, seeds it (`harness/seed.mjs`: an AKB realm and an upstream
realm it brokers to, clients, and one user per page to reach), and walks
every page in light and dark at 390, 768, 1024 and 1440px: no sideways
scroll, no serious or critical axe finding (WCAG 2.1 AA), and a primary
action with at least 4.5:1 contrast. It also replays the duplicated broker
request, checks the theme preference across a form post, and checks that a
realm pointing at a missing theme still signs people in on Keycloak's own.

`--up` leaves Keycloak running on `http://localhost:18480` for a look by
hand; `--down` stops it. `AKB_THEME_SCREENSHOTS=<dir>` keeps a screenshot of
every page, scheme and width.

## Upgrading Keycloak

The Keycloak digest is pinned in the deployment manifests, the harness, and
`upstream/keycloak-<version>.json`; the static checks fail until all of them
agree. Then `node keycloak-theme/scripts/upstream-hashes.mjs` names every
original that changed. Diff each one against the previous version, carry the
change into the copy here, run the harness, and record with `--write`.
