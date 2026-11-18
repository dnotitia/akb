export class OAuthAuthError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'OAuthAuthError';
    this.code = code;
  }
}

export function checkCancelled(signal) {
  if (signal.aborted) {
    throw signal.reason instanceof OAuthAuthError
      ? signal.reason
      : new OAuthAuthError('oauth_cancelled', 'OAuth operation was cancelled.');
  }
}

export function oauthBusy() {
  return new OAuthAuthError('oauth_busy', 'Another process owns OAuth login or refresh for this installation. Retry when it finishes.');
}
