export class CredentialUnavailable extends Error {
  constructor(status) {
    super(status.mode === 'pat' ? 'The selected PAT was rejected. Replace --pat or AKB_PAT and restart.' : 'Authentication is not ready. Check akb_connection and submit a new call after sign-in.');
    this.code = status.mode === 'pat' ? 'pat_rejected' : 'authentication_pending';
    this.execution = 'not_started';
  }
}

class PatOwner {
  constructor(pat) { this.snapshot = Object.freeze({ kind: 'pat', accessToken: pat }); this.rejected = false; }
  currentCredential() { return this.rejected ? null : this.snapshot; }
  status() { return { mode: 'pat', authentication: this.rejected ? 'rejected' : 'ready' }; }
  rejectCredential(snapshot) { if (snapshot === this.snapshot) this.rejected = true; }
  start() {}
  signIn() { return { code: 'inapplicable_action', message: 'PAT mode does not use OAuth. Replace the selected PAT and restart.' }; }
  cancel() { return this.signIn(); }
  signOut() { return this.signIn(); }
  close() {}
}

export function createCredentialOwner(authentication, installation, options = {}) {
  if (authentication.kind === 'pat') return new PatOwner(authentication.pat);
  let session = null;
  let closed = false;
  let loading = null;
  let error = null;
  const load = () => loading ??= import('./oauth-session.mjs').then(({ OAuthSession }) => {
    if (closed) return null;
    session = new OAuthSession(installation, options);
    return session;
  }).catch(() => { error = 'OAuth runtime could not be loaded. Check the supported Node.js version and platform.'; return null; });
  return {
    currentCredential: () => session?.currentCredential() ?? null,
    status: () => session?.status() ?? { mode: 'oauth', authentication: error ? 'error' : 'pending', ...(error ? { message: error } : {}) },
    rejectCredential: (snapshot) => session?.rejectCredential(snapshot),
    start() { void load().then((owner) => owner?.start()); },
    signIn() { void load().then((owner) => owner?.signIn()); return { scheduled: true }; },
    cancel() { return session?.cancel() ?? { cancelled: false }; },
    signOut() { return session?.signOut() ?? { code: 'authentication_pending', message: 'OAuth is starting. Retry sign_out.' }; },
    close() { closed = true; return session?.close(); },
  };
}
