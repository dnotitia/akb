import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { http, HttpResponse } from "msw";
import {
  API,
  localAuthConfig,
  stagedSsoAuthConfig,
} from "./page-story-fixtures";
import { AkbRouteTree } from "./page-route-shell";

const meta = {
  title: "Pages/Auth",
  parameters: {
    layout: "fullscreen",
    authToken: false,
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const LocalLoginReady: Story = {
  name: "Local login / ready",
  parameters: {
    router: { initialEntries: ["/auth"] },
    msw: {
      handlers: [
        http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
        http.post(`${API}/auth/login`, () => HttpResponse.json({ token: "mock" })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByLabelText("Username")).toBeInTheDocument();
    await expect(canvas.getByRole("button", { name: /Sign in/i })).toBeEnabled();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const LocalLoginInvalidCredentials: Story = {
  name: "Local login / invalid credentials",
  parameters: {
    router: { initialEntries: ["/auth"] },
    msw: {
      handlers: [
        http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
        http.post(`${API}/auth/login`, () =>
          HttpResponse.json({ error: "Invalid username or password." }, { status: 401 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.type(await canvas.findByLabelText("Username"), "jylkim");
    await userEvent.type(canvas.getByLabelText("Password"), "wrong-password");
    await userEvent.click(canvas.getByRole("button", { name: /Sign in/i }));
    await expect(await canvas.findByText("Invalid username or password.")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const RegisterForm: Story = {
  name: "Register / form state",
  parameters: {
    router: { initialEntries: ["/auth"] },
    msw: {
      handlers: [
        http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
        http.post(`${API}/auth/register`, () => HttpResponse.json({ ok: true })),
        http.post(`${API}/auth/login`, () => HttpResponse.json({ token: "mock" })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(await canvas.findByRole("tab", { name: "Register" }));
    await expect(canvas.getByLabelText("Email")).toBeInTheDocument();
    await expect(canvas.getByLabelText(/Display name/i)).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const StagedSsoUnavailable: Story = {
  name: "SSO / browser session staged",
  parameters: {
    router: { initialEntries: ["/auth"] },
    msw: {
      handlers: [
        http.get(`${API}/auth/config`, () => HttpResponse.json(stagedSsoAuthConfig)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/SSO browser sign-in is not available yet/i)).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const ForgotPasswordGuidance: Story = {
  name: "Forgot password / admin reset guidance",
  parameters: {
    router: { initialEntries: ["/auth/forgot"] },
    msw: {
      handlers: [
        http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Forgot your password?" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const CallbackFailClosed: Story = {
  name: "SSO callback / fail closed",
  parameters: {
    router: { initialEntries: ["/auth/callback?code=story-code&redirect=/"] },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "SSO sign-in unavailable" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};
