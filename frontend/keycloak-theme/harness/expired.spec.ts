// The 2026-10-06 incident: the browser sent the broker step twice, the second
// request found its code already used, and the person was left on "Page has
// expired". The theme resumes once per tab on its own and explains from the
// second time, so a stray duplicate costs nothing and a loop is impossible.
import { expect, test, type Page } from "@playwright/test";
import { authUrl, inspect, watchErrors } from "./lib";

/** Start sign-in the way AKB's apps do with a provider hint; return the broker step's URL. */
async function brokerStep(page: Page, locale?: string) {
  let broker = "";
  page.on("request", (request) => {
    if (!broker && /\/realms\/akb\/broker\/entra\/login\?/.test(request.url())) broker = request.url();
  });
  await page.goto(authUrl("akb-web", { hint: "entra", locale }));
  await page.waitForURL(/\/realms\/workforce\//);
  expect(broker, "the broker step was requested").not.toBe("");
  return broker;
}

test("a broker step opened twice resumes once, then explains", async ({ page }) => {
  const settled = watchErrors(page);
  const broker = await brokerStep(page);
  const tab = new URL(broker).searchParams.get("tab_id");

  // The duplicate request. Keycloak answers "Page has expired"; the theme
  // takes the person straight back into the sign-in.
  await page.goto(broker);
  await page.waitForURL((url) => !url.pathname.includes("/broker/"));
  await expect(page.locator("#akb-expired")).toHaveCount(0);
  await expect(page.locator("#username")).toBeVisible();
  expect(await page.evaluate((key) => sessionStorage.getItem(key), `akb.kc.expired.${tab}`)).toBe("1");

  // The same step again in the same tab: no second automatic move.
  await page.goto(broker);
  await expect(page.locator("#akb-expired")).toBeVisible();
  await expect(page.locator("html")).toHaveClass(/akb-expired-explain/);
  await expect(page.locator("#kc-page-title")).toHaveText("This sign-in page is no longer valid", { useInnerText: true });
  await expect(page.locator("#kc-page-title")).toBeFocused();
  await expect(page.locator("#loginRestartLink")).toBeVisible();
  await inspect(page, "expired-explain");
  settled();
});

test("the explanation is in Korean when sign-in is", async ({ page }) => {
  const settled = watchErrors(page);
  const broker = await brokerStep(page, "ko");
  await page.goto(broker);
  await page.waitForURL((url) => !url.pathname.includes("/broker/"));
  await page.goto(broker);
  await expect(page.locator("#kc-page-title")).toHaveText("이 로그인 페이지는 더 이상 유효하지 않습니다", { useInnerText: true });
  await expect(page.locator("#loginRestartLink")).toHaveText("로그인 다시 시작");
  await inspect(page, "expired-explain-ko");
  settled();
});

test("without session storage the page explains instead of moving", async ({ page }) => {
  const settled = watchErrors(page);
  const broker = await brokerStep(page);
  await page.addInitScript(() => {
    Object.defineProperty(window, "sessionStorage", {
      get() {
        throw new Error("blocked");
      },
    });
  });
  await page.goto(broker);
  await expect(page.locator("#akb-expired")).toBeVisible();
  await expect(page.locator("html")).toHaveClass(/akb-expired-explain/);
  const shown = page.url();
  await page.waitForTimeout(1000);
  expect(page.url(), "no automatic move").toBe(shown);
  settled();
});
