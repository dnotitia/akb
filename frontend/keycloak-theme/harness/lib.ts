import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { expect, type Page } from "@playwright/test";
import { APP_REDIRECT, PASSWORDS } from "./seed.mjs";

export { PASSWORDS };

const require = createRequire(import.meta.url);
const AXE = require.resolve("axe-core/axe.min.js");
const SCREENSHOTS = process.env.AKB_THEME_SCREENSHOTS;

export const SCHEMES = ["light", "dark"] as const;
export const WIDTHS = [390, 768, 1024, 1440] as const;

type AuthOptions = { scope?: string; hint?: string; locale?: string; theme?: string };

/** An authorization request as AKB's own clients send it (PKCE, code flow). */
export function authUrl(client: string, { scope = "openid", hint, locale, theme }: AuthOptions = {}) {
  const query = new URLSearchParams({
    client_id: client,
    redirect_uri: APP_REDIRECT,
    response_type: "code",
    scope,
    state: "harness",
    code_challenge: "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", // pragma: allowlist secret (RFC 7636 example)
    code_challenge_method: "S256",
  });
  if (hint) query.set("kc_idp_hint", hint);
  if (locale) query.set("ui_locales", locale);
  if (theme) query.set("ui_theme", theme);
  return `/realms/akb/protocol/openid-connect/auth?${query}`;
}

/** Fail the test on any uncaught script error on Keycloak's pages. */
export function watchErrors(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  return () => expect(errors, "uncaught page errors").toEqual([]);
}

export async function signIn(page: Page, username: string, password: string) {
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
}

/** WCAG contrast ratio of an element's text against its own background. */
export async function contrast(page: Page, selector: string) {
  return page.locator(selector).first().evaluate((element) => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d", { willReadFrequently: true })!;
    const rgb = (color: string) => {
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
    };
    const luminance = (channels: number[]) => {
      const [r, g, b] = channels.map((value) => {
        const c = value / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const style = getComputedStyle(element);
    const fore = luminance(rgb(style.color));
    const back = luminance(rgb(style.backgroundColor));
    return (Math.max(fore, back) + 0.05) / (Math.min(fore, back) + 0.05);
  });
}

/** axe WCAG 2.1 A/AA findings that are serious or critical. */
export async function axeFindings(page: Page) {
  if (!(await page.evaluate(() => "axe" in window))) await page.addScriptTag({ path: AXE });
  const violations = await page.evaluate(async () => {
    const axe = (window as unknown as { axe: { run: (context: Document, options: object) => Promise<{ violations: Array<{ id: string; impact: string; nodes: Array<{ target: string[] }> }> }> } }).axe;
    const result = await axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
    });
    return result.violations;
  });
  return violations
    .filter((violation) => violation.impact === "serious" || violation.impact === "critical")
    .map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`);
}

/** Wait out colour transitions: a scheme switch animates them for a moment. */
async function settle(page: Page) {
  await page.waitForFunction(() => document.getAnimations().every((animation) => animation.playState !== "running"));
}

/**
 * Look at the page the way a person would, in every scheme and width: the
 * AKB frame is there, nothing overflows sideways, axe has no serious finding,
 * and the primary action is readable. Screenshots go to AKB_THEME_SCREENSHOTS
 * when it is set, for a human pass.
 */
export async function inspect(page: Page, name: string) {
  await expect(page.locator(".akb-shell"), "the AKB frame").toBeVisible();
  await expect(page.locator("#kc-page-title")).not.toBeEmpty();
  // Off every control, so no hover state is measured.
  await page.mouse.move(0, 0);
  for (const scheme of SCHEMES) {
    await page.emulateMedia({ colorScheme: scheme });
    await expect(page.locator("html")).toHaveAttribute("data-theme", scheme);
    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 900 });
      await settle(page);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `${name} ${scheme} ${width}px scrolls sideways`).toBeLessThanOrEqual(0);
      if (width === 390 || width === 1440) {
        expect(await axeFindings(page), `${name} ${scheme} ${width}px axe`).toEqual([]);
      }
      const primary = page.locator(".akb-card .akb-button--primary:visible, .akb-card .pf-v5-c-button.pf-m-primary:visible");
      if (await primary.count()) {
        const ratio = await contrast(page, ".akb-card .akb-button--primary:visible, .akb-card .pf-v5-c-button.pf-m-primary:visible");
        expect(ratio, `${name} ${scheme} primary button contrast`).toBeGreaterThanOrEqual(4.5);
      }
      if (SCREENSHOTS) {
        mkdirSync(SCREENSHOTS, { recursive: true });
        await page.screenshot({ path: join(SCREENSHOTS, `${name}-${scheme}-${width}.png`), fullPage: true });
      }
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
}
