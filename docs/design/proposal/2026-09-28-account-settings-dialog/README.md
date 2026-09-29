# Account settings without leaving the workspace

Status: proposal — implemented; pending review
Date: 2026-09-29

## Intent and boundary

Account Settings becomes a global modal, not a separate workspace. Vault
Settings remains a routed, Vault-scoped page. No backend contract, permission,
credential storage or authentication behavior changes.

The setting being adjusted is the foreground task; the document, search query,
scroll position and expanded navigation behind it remain mounted. Existing
`/settings?tab=…` URLs remain compatibility entry points that open the requested
section over Home. In-app entry points open the same dialog in place.

## Design

- Maximum 1040px width and 760px height, bounded by the viewport; a white/slate
  surface, small token radius and one shadow distinguish the task from its backdrop.
- A 208px navigation column groups Account, Connections and privileged Workspace.
  Each item uses one Lucide glyph and a sentence-case label; teal means selected.
- The right panel owns scrolling. A quiet fixed Settings/close row remains
  reachable; the section supplies its own heading rather than duplicate titles.
- Below the desktop-modal breakpoint, use a labelled section selector and one
  content column. No horizontally scrolling settings tabs.
- Profile uses a compact identity row and content-width-driven label/value rows.
  Password remains available, with unchanged local-versus-managed policy.
- Appearance uses actual content width, not browser width, to lay out previews.
- Agent connections keeps all three setup phases and token management. Avoid
  squeezing the workspace's three desktop columns into a modal.

The distinctive choice is a restrained, task-sized settings desk, not an entire
second application shell. Existing Pretendard, teal, white/slate surfaces and
semantic status tokens are retained. There is no new color palette or hero.

## Interaction contract

- Sidebar Settings and account menu open the modal without navigating.
- Escape, outside click and Close request dismissal. Unsaved profile/password
  edits and unsaved one-time token state require confirmation. In-flight writes
  prevent section changes/dismissal until a result is known.
- Closing restores focus to the invoking control, with the sidebar/account
  trigger as a fallback when a dropdown item has unmounted.
- Section switches share the same dirty-state guard. Secrets are never persisted.
- Nested token/security confirmations retain their current safeguards. Links to
  watched documents or ownership pages dismiss settings before navigation.
- Session/account replacement unmounts settings to avoid cross-account state.
- Auth/config failures are explicit retry states, never silently editable forms.
- Legacy URL/history navigation remounts only the requested settings section,
  never the underlying workspace. Ordinary browser navigation is not blocked;
  dirty guards apply to dialog dismissal and section changes, with the existing
  browser unload warning retained for refresh/external navigation.

## Implementation and checks

1. Add browser regression `account-settings-dialog.spec.ts`: open from Search,
   assert URL/query unchanged, confirm dirty edits, close/restore focus, verify
   existing deep links, SSO/admin policy and all three connection phases.
2. `settings-dialog-context.tsx` exposes
   `openSettings(section?: string, trigger?: HTMLElement | null): void` and
   `closeSettings(): void`. `SettingsDialogProvider` owns transient dialog state;
   `SettingsLink` retains a real compatibility href for modified/new-tab clicks.
3. Move the Settings controller and section composition to
   `pages/settings/settings-dialog.tsx`. `pages/settings/index.tsx` is only the
   compatibility route adapter. Remove secondary-rail geometry from Layout.
4. Rework Profile/Appearance and adjust connection layout to actual modal width;
   preserve all API paths, SSO restrictions, token and security confirmations.
5. Wire sidebar, user menu, notification management, Home watches and Vault
   onboarding. Test provider integration, every section's existing feature suite,
   desktop/mobile, both themes, keyboard dismissal, focus and no background shift.
6. Run design/type/lint/unit gates and build the frontend. Deployment and backend
   changes are outside this proposal's scope.

## Regression coverage

- Keep the workspace mounted through identity verification and same-document
  dismissal; browser Back reopens the correct deep-linked section.
- Exercise narrow and wide layouts, both themes, every existing section,
  sidebar/account-menu entry, focus restoration, backdrop and Escape handling.
- Guard pending or failed profile saves, one-time token acknowledgement, and
  document unwatch requests. Pending unwatch includes the subscription-list
  refresh; failure leaves an actionable error and allows retry.
- Browser tests use isolated HTTP fixtures, not live account credentials or
  production data. They complement the design, type, lint and unit gates.
