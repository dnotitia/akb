import { useEffect, useMemo, type ReactNode } from "react";
import type { Decorator, Preview } from "@storybook/react-vite";
import { themes } from "storybook/theming";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { initialize, mswLoader } from "msw-storybook-addon";
import { setToken } from "@/lib/api";
import "../src/index.css";

initialize({
  onUnhandledRequest: "bypass",
});

type AkbTheme = "light" | "dark";

function ThemeBridge({ theme, children }: { theme: AkbTheme; children: ReactNode }) {
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.style.colorScheme = theme;
    document.body.classList.toggle("dark", theme === "dark");
    return () => {
      document.documentElement.classList.remove("dark");
      document.documentElement.style.colorScheme = "";
      document.body.classList.remove("dark");
    };
  }, [theme]);

  return (
    <div className="min-h-screen bg-background text-foreground antialiased">
      {children}
    </div>
  );
}

const withAkbProviders: Decorator = (Story, context) => {
  const queryClient = useMemo(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
          mutations: { retry: false },
        },
      }),
    [context.id],
  );

  const theme = (context.globals.theme === "dark" ? "dark" : "light") satisfies AkbTheme;
  const router = context.parameters.router as
    | { initialEntries?: string[]; initialIndex?: number }
    | undefined;
  const authToken = context.parameters.authToken === false
    ? null
    : typeof context.parameters.authToken === "string"
      ? context.parameters.authToken
      : "mock";

  setToken(authToken);

  useEffect(() => {
    setToken(authToken);
    return () => setToken(null);
  }, [authToken]);

  return (
    <ThemeBridge theme={theme}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={router?.initialEntries || ["/"]} initialIndex={router?.initialIndex}>
          <Story />
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeBridge>
  );
};

const preview: Preview = {
  decorators: [withAkbProviders],
  loaders: [mswLoader],
  initialGlobals: {
    theme: "light",
  },
  globalTypes: {
    theme: {
      name: "Theme",
      description: "AKB color theme",
      toolbar: {
        icon: "circlehollow",
        dynamicTitle: true,
        items: [
          { value: "light", title: "Light" },
          { value: "dark", title: "Dark" },
        ],
      },
    },
  },
  parameters: {
    layout: "centered",
    docs: {
      theme: themes.light,
    },
    a11y: {
      test: "todo",
    },
    msw: {
      handlers: [],
    },
    options: {
      storySort: {
        order: ["Foundation", "UI", "Components", "Pages", "Scenarios"],
      },
    },
  },
};

export default preview;
