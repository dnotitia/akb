import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { delay, http, HttpResponse } from "msw";
import {
  API,
  defaultVaultInfoHandler,
  hybridSsoAuthConfig,
  localAuthConfig,
  recentChanges,
  vaultHealth,
  vaultShellHandlers,
  vaultSkillDoc,
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

export const HybridSsoAvailable: Story = {
  name: "SSO / hybrid available",
  parameters: {
    router: { initialEntries: ["/auth"] },
    msw: {
      handlers: [
        http.get(`${API}/auth/config`, () => HttpResponse.json(hybridSsoAuthConfig)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("button", { name: "Sign in with SSO" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const ForgotPasswordGuidance: Story = {
  name: "Forgot password / admin reset guidance",
  parameters: {
    router: { initialEntries: ["/auth/forgot"] },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Forgot your password?" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const CallbackCompleting: Story = {
  name: "SSO callback / completing",
  parameters: {
    router: { initialEntries: ["/auth/callback?code=story-code&redirect=/"] },
    msw: {
      handlers: [
        http.post(`${API}/auth/keycloak/exchange`, async () => {
          await delay("infinite");
          return HttpResponse.json({});
        }),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Completing sign-in…")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const CallbackMissingCode: Story = {
  name: "SSO callback / missing code",
  parameters: {
    router: { initialEntries: ["/auth/callback"] },
    msw: {
      handlers: [
        http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByLabelText("Username")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const CallbackExchangeError: Story = {
  name: "SSO callback / exchange error",
  parameters: {
    router: { initialEntries: ["/auth/callback?code=bad-code"] },
    msw: {
      handlers: [
        http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
        http.post(`${API}/auth/keycloak/exchange`, () =>
          HttpResponse.json({ error: "exchange failed" }, { status: 400 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByLabelText("Username")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  },
};

export const CallbackSuccessRedirect: Story = {
  name: "SSO callback / success redirect",
  parameters: {
    router: { initialEntries: ["/auth/callback?code=story-code&redirect=/vault/akb"] },
    msw: {
      handlers: [
        http.post(`${API}/auth/keycloak/exchange`, () =>
          HttpResponse.json({ token: "mock", kc_id_token: "kid" }),
        ),
        ...vaultShellHandlers,
        defaultVaultInfoHandler,
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
        http.get("/health/vault/akb", () => HttpResponse.json(vaultHealth)),
        http.get(`${API}/recent`, () => HttpResponse.json(recentChanges)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "akb" })).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
  },
};
