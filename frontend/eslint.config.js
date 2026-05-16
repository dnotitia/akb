// ESLint 9 flat config. Mirrors the Vite/React/TS template that
// `pnpm create vite@latest -- --template react-ts` ships, plus the
// react-hooks + react-refresh plugins that catch the two highest-
// payoff classes of React bugs (rules-of-hooks violations, stale
// component exports that break HMR).
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  // Generated bundles never live in src — never lint them.
  { ignores: ["dist", "node_modules", "coverage"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2024,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // Classic two-rule recommended set — same as `eslint-plugin-react-hooks` <=6.
      // v7 added React-Compiler-shaped rules (set-state-in-effect, impure-
      // call-during-render, access-before-declared, …) which require a
      // wider refactor pass to honor on this codebase. Adopt those as a
      // follow-up — flag here so the gap is documented, not hidden.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      // We rely on the call site to interrogate `unknown` from API errors
      // — explicit `any` is sometimes the pragmatic call. Warn rather
      // than block, so the lint stays passable on day one.
      "@typescript-eslint/no-explicit-any": "warn",
      // Test files build small fixtures and unused imports are noisy;
      // mirror the same rule for ts source so production code is clean.
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
);
