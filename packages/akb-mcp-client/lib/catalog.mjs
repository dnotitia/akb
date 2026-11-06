import generated from './generated-tools.json' with { type: 'json' };
import refinements from './generated-tool-refinements.json' with { type: 'json' };
import { isDeepStrictEqual } from 'node:util';
import { FILE_TOOLS, FILE_WRITE_TOOL_NAMES, FILE_CONTENT_TOOLS, VAULT_SKILL_ACK_ARGUMENT, vaultSkillAckProperty } from './local-tools.mjs';

const canonicalByName = new Map(generated.map((tool) => [tool.name, tool]));

export function currentToolContracts(name) {
  const tool = canonicalByName.get(name);
  return tool ? [tool, ...(refinements[name] ?? []).map((schema) => ({ name, ...schema }))] : [];
}

export function toolSchemasEqual(actual, expected) {
  return isDeepStrictEqual(actual.inputSchema, expected.inputSchema) && isDeepStrictEqual(actual.outputSchema, expected.outputSchema);
}

export const CONNECTION_TOOL = {
  name: 'akb_connection',
  description: 'Check the selected authentication mode and backend connectivity. OAuth actions start sign-in, cancel this process\'s attempt, or sign out of the shared local session. Calls made before readiness never execute later. Submit a new call after readiness.',
  inputSchema: { type: 'object', properties: { action: { type: 'string', enum: ['status', 'sign_in', 'cancel', 'sign_out'], default: 'status' } }, additionalProperties: false },
};

export class ToolCatalog {
  constructor(localTools, decorate) {
    this.decorate = decorate;
    this.canonical = generated;
    const names = [...generated, ...localTools, CONNECTION_TOOL].map((tool) => tool.name);
    if (new Set(names).size !== names.length) throw new Error('Generated and local tool names collide.');
    this.localTools = [...localTools, CONNECTION_TOOL];
    this.tools = this.compose(generated);
  }
  compose(tools) { return [...this.decorate(tools), ...this.localTools]; }
  initialCatalog() { return { tools: this.tools }; }
  refine(live) {
    if (!Array.isArray(live?.tools) || live.tools.length !== this.canonical.length) throw new Error('Backend tool catalog is incompatible. Install the matching proxy version.');
    const seen = new Set();
    for (const tool of live.tools) {
      const expected = canonicalByName.get(tool?.name);
      if (!expected || seen.has(tool.name) || tool.inputSchema?.type !== 'object') throw new Error('Backend tool catalog is incompatible. Install the matching proxy version.');
      seen.add(tool.name);
      if (!currentToolContracts(tool.name).some((contract) => toolSchemasEqual(tool, contract))) throw new Error('Backend tool schemas are incompatible. Install the matching proxy version.');
    }
    const liveByName = new Map(live.tools.map((tool) => [tool.name, tool]));
    const next = this.compose(this.canonical.map((tool) => liveByName.get(tool.name)));
    const changed = !isDeepStrictEqual(next, this.tools);
    this.tools = next;
    return changed;
  }
}

export function createCatalog() {
  const local = FILE_TOOLS.map((tool) => {
    if (!FILE_WRITE_TOOL_NAMES.has(tool.name)) return { ...tool };
    return {
      ...tool,
      inputSchema: {
        ...tool.inputSchema,
        properties: { ...tool.inputSchema.properties, [VAULT_SKILL_ACK_ARGUMENT]: vaultSkillAckProperty() },
      },
    };
  });
  return new ToolCatalog(local, (tools) => tools.map((tool) => {
    if (!FILE_CONTENT_TOOLS.has(tool.name)) return { ...tool };
    return {
      ...tool,
      inputSchema: {
        ...tool.inputSchema,
        properties: {
          ...tool.inputSchema.properties,
          file: {
            type: 'string',
            description: 'Local file path to read as document body (alternative to content). Provide either file or content, not both.',
          },
        },
      },
    };
  }));
}
