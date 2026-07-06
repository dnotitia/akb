import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, within } from "storybook/test";
import { http, HttpResponse } from "msw";
import { API, appLayoutHandlers } from "./page-story-fixtures";
import { AkbRouteTree } from "./page-route-shell";

const meta = {
  title: "Pages/Recovery",
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const AppRouteNotFound: Story = {
  name: "App route / not found",
  parameters: {
    router: { initialEntries: ["/stale/route"] },
    msw: {
      handlers: appLayoutHandlers,
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/Page not found/i)).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const PublicationNotFound: Story = {
  name: "Publication / removed or unknown",
  parameters: {
    authToken: false,
    router: { initialEntries: ["/p/missing-share"] },
    msw: {
      handlers: [
        http.get(`${API}/public/missing-share`, () =>
          HttpResponse.json({ detail: "Publication not found" }, { status: 404 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Nothing here")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};
