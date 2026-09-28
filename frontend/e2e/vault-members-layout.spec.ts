import { expect, test, type Page } from "@playwright/test";

test.skip(
  process.env.AKB_FE_E2E_MODE === "mock",
  "Owns isolated HTTP fixtures.",
);

async function fixture(page: Page, dark = false, count = 4) {
  await page.addInitScript((dark) => {
    localStorage.setItem("akb_token", "members-layout-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, dark);
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let json: object = {
      items: [],
      vaults: [],
      relations: [],
      unread_count: 0,
    };
    if (path.endsWith("/auth/config"))
      json = {
        schema_version: 2,
        auth_mode: "local",
        local_auth: { enabled: true },
        keycloak: { enabled: false, browser_session_ready: false },
        providers: [],
        mcp_oauth: { enabled: false },
      };
    else if (path.endsWith("/auth/me"))
      json = {
        user_id: "member-fixture",
        username: "owner",
        display_name: "Review owner",
        email: "owner@example.invalid",
        auth_method: "local",
        is_admin: false,
      };
    else if (path.endsWith("/vaults"))
      json = {
        vaults: [{ id: "member-vault", name: "fixture", role: "owner" }],
      };
    else if (path.endsWith("/info"))
      json = {
        name: "fixture",
        role: "owner",
        public_access: "none",
        member_count: count,
      };
    else if (path.endsWith("/members"))
      json = {
        members: Array.from({ length: count }, (_, i) => ({
          username: i ? `member-${i}` : "owner",
          display_name:
            [
              "Review owner",
              "Mina Park",
              "Dana Lee",
              "A long member name that must remain readable",
            ][i] || `Member ${i}`,
          email:
            i === 3
              ? "long.member.address@example.invalid"
              : `member${i}@example.invalid`,
          role: ["owner", "admin", "writer", "reader"][i % 4],
          since: "2026-09-15T00:00:00Z",
        })),
      };
    else if (path.includes("/browse/")) json = { items: [] };
    else if (path.includes("/publications/")) json = { publications: [] };
    return route.fulfill({ json });
  });
}

for (const dark of [false, true])
  for (const width of [375, 1440, 1920]) {
    test(`Member ledger uses its working width at ${width}px in ${dark ? "dark" : "light"}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 });
      await fixture(page, dark);
      await page.goto("/vault/fixture/members");
      const roster = page.getByRole("table", { name: "Vault members" });
      await expect(roster).toBeVisible();
      await expect(
        page.getByRole("heading", { level: 2, name: "Members", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("link", { name: "Change in Settings", exact: true }),
      ).toHaveCount(0);
      await expect(page.getByText(/People not listed here/)).toHaveCount(0);
      await page.screenshot({
        path: testInfo.outputPath("members.png"),
        animations: "disabled",
      });
      await expect(
        page.getByRole("complementary", { name: "Member access context" }),
      ).toHaveCount(0);
      const region = page.getByRole("region", { name: "Members", exact: true });
      const regionBox = (await region.boundingBox())!;
      const tableBox = (await roster.boundingBox())!;
      expect(Math.abs(tableBox.width - regionBox.width)).toBeLessThanOrEqual(2);
      expect(regionBox.height).toBeLessThan(700);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      for (const row of await roster.getByRole("row").all()) {
        expect(
          await row.evaluate((el) => el.scrollWidth <= el.clientWidth + 1),
        ).toBe(true);
      }
      const guide = roster.getByRole("button", { name: "Role permissions" });
      for (const control of await roster
        .getByRole("button", { name: /Change role for/ })
        .all()) {
        const contrast = await control.evaluate((element) => {
          const canvas = document.createElement("canvas");
          canvas.width = canvas.height = 1;
          const context = canvas.getContext("2d")!;
          const luminance = (color: string) => {
            context.clearRect(0, 0, 1, 1);
            context.fillStyle = color;
            context.fillRect(0, 0, 1, 1);
            const channels = Array.from(context.getImageData(0, 0, 1, 1).data)
              .slice(0, 3)
              .map((value) => value / 255)
              .map((value) =>
                value <= 0.04045
                  ? value / 12.92
                  : ((value + 0.055) / 1.055) ** 2.4,
              );
            return (
              channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
            );
          };
          const style = getComputedStyle(element);
          const front = luminance(style.color),
            back = luminance(style.backgroundColor);
          return (
            (Math.max(front, back) + 0.05) / (Math.min(front, back) + 0.05)
          );
        });
        expect(contrast).toBeGreaterThanOrEqual(4.5);
      }
      await guide.click();
      await expect(
        page.getByRole("dialog", { name: "Role permissions" }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(guide).toBeFocused();
      await page
        .getByRole("button", { name: "Invite member", exact: true })
        .click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.keyboard.press("Escape");
      // The two registry pages use identical route gutters, not separate inset frames.
      const headerBox = (await page
        .getByTestId("member-roster-header")
        .boundingBox())!;
      await page.goto("/vault/fixture/publications");
      const publicationHeader = page.getByTestId("publication-ledger-header");
      await expect(publicationHeader).toBeVisible();
      const publicationBox = (await publicationHeader.boundingBox())!;
      expect(headerBox.x).toBe(publicationBox.x);
      expect(headerBox.width).toBe(publicationBox.width);
    });
  }

test("A long roster scrolls in the route and still exposes the last member", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixture(page, false, 60);
  await page.goto("/vault/fixture/members");
  const viewport = page.locator('[data-slot="vault-route-viewport"]');
  const last = page.getByText("Member 59", { exact: true });
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  expect(await viewport.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  const search = page.getByRole("searchbox", { name: "Filter members" });
  await search.fill("member59@");
  await expect(
    page.getByRole("table", { name: "Vault members" }).getByRole("row"),
  ).toHaveCount(2);
});

for (const publicAccess of ["reader", "writer"] as const) {
  test(`Public ${publicAccess} access stays an explanatory footer, not roster settings`, async ({
    page,
  }) => {
    await fixture(page);
    await page.route("**/api/v1/vaults/fixture/info", (route) =>
      route.fulfill({
        json: {
          name: "fixture",
          role: "owner",
          public_access: publicAccess,
          member_count: 4,
        },
      }),
    );
    await page.goto("/vault/fixture/members");
    const roster = page.getByRole("table", { name: "Vault members" });
    await expect(roster).toBeVisible();
    const note = page.getByText(
      publicAccess === "reader"
        ? "People not listed here can also read this vault when signed in."
        : "People not listed here can also read and change content when signed in.",
      { exact: true },
    );
    await expect(note).toBeVisible();
    const bounds = (await roster.boundingBox())!;
    expect((await note.boundingBox())!.y).toBeGreaterThanOrEqual(
      bounds.y + bounds.height,
    );
    await expect(
      page.getByRole("link", { name: "Change in Settings", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Invite member" }),
    ).toBeEnabled();
  });
}
