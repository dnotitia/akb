// Checks on the login theme's source that need no Keycloak. The browser
// checks live in ../harness and run against a real one.
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { buildTokens, OUTPUT as TOKENS } from "../scripts/theme-tokens.mjs";
import { overriddenTemplates, RECORD } from "../scripts/upstream-hashes.mjs";

const root = resolve(import.meta.dirname, "..");
const login = join(root, "akb/login");
const css = join(login, "resources/css");
const read = (path: string) => readFileSync(path, "utf8");
const repo = resolve(root, "../..");

const GENERATED = "akb-tokens.generated.css";
const VOCABULARY = "akb-vocabulary.css";

/** Every file a person's browser gets from source, except the generated tokens. */
function authoredFiles() {
  const files = [
    ...readdirSync(css).filter((name) => name !== GENERATED).map((name) => join(css, name)),
    ...readdirSync(join(login, "resources/js")).map((name) => join(login, "resources/js", name)),
    ...readdirSync(login).filter((name) => name.endsWith(".ftl")).map((name) => join(login, name)),
  ];
  return files.map((path) => ({ path, name: path.slice(root.length + 1), text: stripComments(read(path)) }));
}

function stripComments(text: string) {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/<#--[\s\S]*?-->/g, "");
}

function properties(lang: string) {
  const entries = new Map<string, string>();
  for (const line of read(join(login, `messages/messages_${lang}.properties`)).split("\n")) {
    if (!line.trim() || line.startsWith("#")) continue;
    const at = line.search(/(?<!\\)=/);
    entries.set(line.slice(0, at).replace(/\\:/g, ":"), line.slice(at + 1));
  }
  return entries;
}

describe("token seam", () => {
  it("the generated tokens are what the app's index.css says today", () => {
    expect(read(TOKENS), "run `pnpm run theme:tokens`").toBe(buildTokens());
  });

  it("no colour is written anywhere but the generated file", () => {
    const literal =
      /#[0-9a-fA-F]{3,8}\b(?![-\w])|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(|:\s*(?:white|black|red|green|blue|gray|grey|orange|teal|navy|silver)\b/;
    const offenders = authoredFiles().flatMap((file) =>
        file.text
          .split("\n")
          .map((line, index) => ({ line, index }))
          .filter(({ line }) => literal.test(line.replace(/url\([^)]*\)/g, "")))
          .map(({ line, index }) => `${file.name}:${index + 1}: ${line.trim()}`),
    );
    expect(offenders).toEqual([]);
  });

  it("only the vocabulary reads the generated --akb-* tokens", () => {
    const offenders = authoredFiles()
      .filter((file) => !file.name.endsWith(VOCABULARY))
      .filter((file) => /--akb-/.test(file.text))
      .map((file) => file.name);
    expect(offenders).toEqual([]);
  });

  it("every vocabulary name points at a token that exists", () => {
    const generated = new Set(read(TOKENS).match(/--akb-[\w-]+(?=\s*:)/g));
    const used = read(join(css, VOCABULARY)).match(/var\(--akb-[\w-]+/g) ?? [];
    const missing = [...new Set(used.map((use) => use.slice(4)))].filter((name) => !generated.has(name));
    expect(missing).toEqual([]);
  });

  it("every custom property the theme reads is defined by the theme or PatternFly", () => {
    const sheet = stripComments(read(join(css, "akb-login.css")));
    const defined = new Set([
      ...(read(join(css, VOCABULARY)).match(/--[\w-]+(?=\s*:)/g) ?? []),
      ...(sheet.match(/--[\w-]+(?=\s*:)/g) ?? []),
    ]);
    const used = new Set((sheet.match(/var\(--[\w-]+/g) ?? []).map((use) => use.slice(4)));
    const undefinedNames = [...used].filter((name) => !defined.has(name) && !name.startsWith("--pf-v5-"));
    expect(undefinedNames).toEqual([]);
  });
});

describe("messages", () => {
  const en = properties("en");
  const ko = properties("ko");

  it("English and Korean define the same keys", () => {
    expect([...ko.keys()].sort()).toEqual([...en.keys()].sort());
  });

  it("both languages keep the same placeholders", () => {
    const placeholders = (text: string) => (text.match(/\{\d+\}/g) ?? []).sort().join(",");
    const differ = [...en.keys()].filter((key) => placeholders(en.get(key)!) !== placeholders(ko.get(key) ?? ""));
    expect(differ).toEqual([]);
  });

  it("apostrophes are doubled, since Keycloak formats every message", () => {
    // MessageFormat drops a lone ' (it starts a quoted section).
    const lone = [...en, ...ko].filter(([, text]) => /(?<!')'(?!')/.test(text)).map(([key]) => key);
    expect(lone).toEqual([]);
  });

  it("every message the templates name exists, and every akb message is used", () => {
    const templates = readdirSync(login)
      .filter((name) => name.endsWith(".ftl"))
      .map((name) => read(join(login, name)))
      .join("\n");
    const named = new Set([...templates.matchAll(/msg\(["'](akb[\w.]+)["']/g)].map((match) => match[1]));
    const defined = [...en.keys()].filter((key) => key.startsWith("akb"));
    expect([...named].filter((key) => !en.has(key))).toEqual([]);
    expect(defined.filter((key) => !named.has(key))).toEqual([]);
  });
});

describe("upstream originals", () => {
  const record = JSON.parse(read(RECORD));

  it("every replaced template has its original's hash recorded", () => {
    expect(Object.keys(record.overrides).sort()).toEqual(overriddenTemplates());
  });

  it("the record is for the Keycloak AKB ships and the harness runs", () => {
    const digest = record.image.split("@")[1];
    const pins = [
      "deploy/k8s/standalone-sso/keycloak.yaml",
      "deploy/helm/akb/values.yaml",
      "frontend/keycloak-theme/harness/compose.yaml",
    ].map((path) => ({ path, digests: [...read(join(repo, path)).matchAll(/quay\.io\/keycloak\/keycloak[^\s"]*@(sha256:[0-9a-f]{64})/g)].map((match) => match[1]) }));
    for (const pin of pins) {
      expect(pin.digests.length, pin.path).toBeGreaterThan(0);
      expect(new Set(pin.digests), pin.path).toEqual(new Set([digest]));
    }
  });
});
