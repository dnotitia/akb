export class PatCredentials {
  #snapshot;
  #rejected = false;

  constructor(pat) {
    this.#snapshot = Object.freeze({ kind: "pat", accessToken: pat });
  }

  credential() {
    if (this.#rejected) {
      throw Object.assign(new Error("The selected PAT was rejected. Replace --pat or AKB_PAT and restart."), {
        code: "pat_rejected",
      });
    }
    return this.#snapshot;
  }

  rejected() {
    this.#rejected = true;
  }

  status() {
    return { mode: "pat", state: "configured" };
  }
}

export async function createCredentials(authentication, installation, options) {
  if (authentication.kind === "pat") return new PatCredentials(authentication.pat);
  const { OAuthSession } = await import("./oauth-session.mjs");
  return new OAuthSession(installation, options);
}
