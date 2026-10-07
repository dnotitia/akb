// Every page a person can meet on AKB's paths, reached the way they reach it,
// then inspected in both schemes at four widths. Grade A pages are the ones
// the theme rewrites; grade B pages inherit keycloak.v2 inside the AKB frame.
import { expect, test } from "@playwright/test";
import { authUrl, inspect, PASSWORDS, signIn, watchErrors } from "./lib";

test.describe("grade A", () => {
  test("sign-in offers the identity provider, then local accounts", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web"));
    await expect(page.locator("#kc-page-title")).toHaveText("Sign in to AKB");
    await expect(page.locator("#social-entra")).toHaveText("Sign in with teams");
    await expect(page.locator(".akb-divider")).toBeVisible();
    await expect(page.locator("#kc-form-login")).toBeVisible();
    await inspect(page, "login");
    settled();
  });

  test("the product administration sign-in has no identity provider", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web-admin"));
    await expect(page.locator("#kc-page-title")).toHaveText("Sign in to AKB administration");
    await expect(page.locator(".akb-card__lead")).toHaveText("Use this installation's administrator account.");
    await expect(page.locator("[id^=social-]")).toHaveCount(0);
    await expect(page.locator("#kc-social-providers, .akb-divider")).toHaveCount(0);
    await expect(page.locator("#username")).toBeFocused();
    await inspect(page, "login-admin");
    settled();
  });

  test("a wrong password is reported on the form", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web-admin"));
    await signIn(page, "akb-recovery", "not-the-password");
    await expect(page.locator("#input-error-username")).toHaveText("Invalid username or password.");
    await inspect(page, "login-error");
    settled();
  });

  test("Korean sign-in", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web", { locale: "ko" }));
    await expect(page.locator("html")).toHaveAttribute("lang", "ko");
    await expect(page.locator("#kc-page-title")).toHaveText("AKB에 로그인");
    await expect(page.locator("#social-entra")).toHaveText("teams(으)로 로그인");
    await inspect(page, "login-ko");
    settled();
  });

  test("consent names the agent and each permission", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("mcp-agent", { scope: "openid offline_access akb:vault:read akb:vault:write" }));
    await signIn(page, "ada", PASSWORDS.ada);
    await expect(page.locator("#kc-page-title")).toHaveText("Codex wants to use AKB");
    const scopes = page.locator(".akb-consent__scopes");
    await expect(scopes).toContainText("Stay connected until you revoke this access");
    await expect(scopes).toContainText("Read your AKB vaults (documents, tables, files, search)");
    await expect(scopes).toContainText("Create, edit, and delete AKB content");
    await expect(page.locator("#kc-login")).toHaveText("Allow");
    await expect(page.locator("#kc-cancel")).toHaveText("Deny");
    await inspect(page, "consent");
    settled();
  });

  test("an unknown client gets the error page", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("no-such-client"));
    await expect(page.locator(".akb-alert--error")).toBeVisible();
    await inspect(page, "error");
    settled();
  });

  test("sign-out asks first, then says it is done", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web"));
    await signIn(page, "ada", PASSWORDS.ada);
    // Nothing listens on the app's callback, so the browser lands on its own
    // error page; the Keycloak session exists by then.
    await page.waitForURL((url) => url.protocol === "chrome-error:");
    await page.goto("/realms/akb/protocol/openid-connect/logout");
    await expect(page.locator("#kc-logout")).toBeVisible();
    await inspect(page, "logout-confirm");
    await page.locator("#kc-logout").click();
    await expect(page.locator("#kc-page-title")).toHaveText("You are logged out");
    // Said once, as the title; upstream repeats it as the body.
    await expect(page.locator("#kc-info-message")).not.toContainText("You are logged out");
    await inspect(page, "info-logged-out");
    settled();
  });
});

test.describe("grade B", () => {
  test("the recovery administrator replaces a temporary password", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web-admin"));
    await signIn(page, "akb-recovery", PASSWORDS.recovery);
    await expect(page.locator("#password-new")).toBeVisible();
    await inspect(page, "update-password");
    settled();
  });

  test("an authenticator is required", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web"));
    await signIn(page, "casey", PASSWORDS.local);
    await expect(page.locator("#kc-totp-settings, #totp").first()).toBeVisible();
    await inspect(page, "config-totp");
    settled();
  });

  test("a profile update is required", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web"));
    await signIn(page, "dana", PASSWORDS.local);
    await expect(page.locator("#kc-update-profile-form")).toBeVisible();
    await inspect(page, "update-profile");
    settled();
  });

  test("terms must be accepted", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web"));
    await signIn(page, "erin", PASSWORDS.local);
    await expect(page.locator("#kc-accept")).toBeVisible();
    await inspect(page, "terms");
    settled();
  });

  test("a provider account whose address is taken asks before linking", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web"));
    await page.locator("#social-entra").click();
    await page.waitForURL(/realms\/workforce/);
    await signIn(page, "bob", PASSWORDS.upstream);
    await expect(page.locator("#linkAccount, #updateProfile").first()).toBeVisible();
    await inspect(page, "idp-link-confirm");
    settled();
  });

  test("a provider account with a missing name is completed first", async ({ page }) => {
    const settled = watchErrors(page);
    await page.goto(authUrl("akb-web"));
    await page.locator("#social-entra").click();
    await page.waitForURL(/realms\/workforce/);
    await signIn(page, "carol", PASSWORDS.upstream);
    await expect(page.locator("#lastName")).toBeVisible();
    // The field draws its own border; keycloak.v2 nests a second, opaque
    // form-control inside the wrapper that used to paint over it.
    const inner = await page.locator("#email").evaluate((element) => getComputedStyle(element).backgroundColor);
    expect(inner).toBe("rgba(0, 0, 0, 0)");
    await inspect(page, "idp-review-profile");
    settled();
  });
});
