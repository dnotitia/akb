# Storybook contract

AKB Storybook is curated scenario documentation for the frontend source at the
current repository revision. It is not the source of truth for application
runtime behavior, and it does not claim parity with whichever frontend build is
currently deployed.

## What Storybook is for

- Reviewing representative visual and interaction states in isolation.
- Exercising deterministic component and page scenarios with browser-rendered
  Storybook tests.
- Documenting the design system and important empty, loading, error, permission,
  and success states.

Storybook passing means those curated scenarios rendered under the Storybook
adapter. Application correctness still requires the unit checks and real-app
Playwright coverage appropriate to the change.

## Intentional runtime differences

| Concern | Application | Storybook |
|---|---|---|
| Navigation | `BrowserRouter` and browser history | `MemoryRouter` with a story-defined entry |
| Network | Configured backend | MSW scenario handlers; unmatched requests are bypassed |
| Authentication | Persisted login/session state and redirects | A story-controlled mock token and auth responses |
| Query behavior | 30-second stale time and one query retry | No stale time and no query or mutation retries |
| State lifetime | Normal navigation and browser storage lifetime | Isolated story render with scenario reset behavior |

Because of those differences, Storybook alone must not be used to approve auth
and redirect behavior, history/navigation state, retry and cache behavior,
unhandled network behavior, or production integration. Focus management,
permissions, asynchronous failure/recovery, resize and collapse behavior, and
canvas interactions also need direct interaction coverage at the appropriate
test layer rather than incidental page rendering.

## Route ownership

The application and Storybook both render `src/app-routes.tsx`. The canonical
path, component, and boundary map lives in `src/app-route-contract.ts`; the
contract distinguishes public/auth routes, `Layout` routes, and `VaultShell`
routes. `src/__tests__/app-route-contract.test.ts` is an intentional review gate:
route changes must update its expected contract.

This shared tree prevents Storybook from retaining an obsolete route, page
component, redirect, or shell owner after the application changes. It does not
make the surrounding Storybook runtime equivalent to the application runtime.

## Review checklist

Run the frontend gate for every Storybook change:

```sh
pnpm design:check
pnpm typecheck
pnpm lint
pnpm test
pnpm build-storybook
pnpm test:storybook
```

Add a direct story when a stateful surface benefits from isolated visual review,
and add a real-app test when the behavior depends on browser routing, persisted
auth, live integration, caching/retries, resizing, or canvas input.
