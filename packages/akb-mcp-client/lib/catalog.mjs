import generated from './generated-tools.json' with { type: 'json' };

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
    if (!Array.isArray(live.tools)) throw new Error('Backend tool catalog is incompatible. Install the matching proxy version.');
    const canonical = new Map(this.canonical.map((tool) => [tool.name, tool]));
    const seen = new Set();
    for (const tool of live.tools) {
      const expected = canonical.get(tool.name);
      if (!expected || seen.has(tool.name) || tool.inputSchema?.type !== 'object') throw new Error('Backend tool catalog is incompatible. Install the matching proxy version.');
      seen.add(tool.name);
      const before = expected.inputSchema;
      const after = tool.inputSchema;
      if (JSON.stringify(before.required ?? []) !== JSON.stringify(after.required ?? []) || Object.keys(before.properties ?? {}).some((key) => JSON.stringify(before.properties[key]) !== JSON.stringify(after.properties?.[key])) || Object.keys(after.properties ?? {}).some((key) => !Object.hasOwn(before.properties ?? {}, key))) throw new Error('Backend tool schemas are incompatible. Install the matching proxy version.');
    }
    const liveByName = new Map(live.tools.map((tool) => [tool.name, tool]));
    const next = this.compose(this.canonical.map((tool) => liveByName.get(tool.name) ?? tool));
    const changed = JSON.stringify(next) !== JSON.stringify(this.tools);
    this.tools = next;
    return changed;
  }
}
