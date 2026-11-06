import { createInterface } from 'node:readline';

function local(message) {
  return ['initialize', 'server/discover', 'tools/list', 'ping'].includes(message?.method) || (message?.method === 'tools/call' && message.params?.name === 'akb_connection');
}

export class StdioDispatcher {
  constructor({ handle, afterHandshake, close, input = process.stdin, output = process.stdout, limit = 32 }) {
    Object.assign(this, { handle, afterHandshake, close, input, output, limit });
    this.active = new Map();
    this.writes = Promise.resolve();
    this.closed = false;
    this.handshaken = false;
  }
  write(message) {
    if (this.closed) return Promise.resolve();
    this.writes = this.writes.then(() => new Promise((resolve, reject) => {
      this.output.write(JSON.stringify(message) + '\n', (error) => error ? reject(error) : resolve());
    }));
    this.writes.catch(() => { for (const entry of this.active.values()) entry.controller.abort(); });
    return this.writes;
  }
  error(id, code, message) { return this.write({ jsonrpc: '2.0', id, error: { code, message } }); }
  async start() {
    const lines = createInterface({ input: this.input, crlfDelay: Infinity });
    try {
      for await (const line of lines) {
        if (!line.trim()) continue;
        let message;
        try {
          if (Buffer.byteLength(line) > 2 * 1024 * 1024) throw new Error();
          message = JSON.parse(line);
        } catch { void this.error(null, -32700, 'Invalid or oversized JSON message.'); continue; }
        if (message?.method === 'notifications/cancelled') {
          this.active.get(message.params?.requestId)?.controller.abort();
          continue;
        }
        const id = message?.id;
        if (id != null && this.active.has(id)) { void this.error(id, -32600, 'Request ID is already active.'); continue; }
        if (!local(message) && this.active.size >= this.limit) { if (id != null) void this.error(id, -32000, 'Proxy is busy. The request did not start.'); continue; }
        if (local(message) && this.active.size >= this.limit + 8) { if (id != null) void this.error(id, -32000, 'Too many active local requests.'); continue; }
        const controller = new AbortController();
        const key = id ?? Symbol();
        const task = Promise.resolve().then(() => this.handle(message, controller.signal)).then(async (response) => {
          if (response != null) {
            await this.write(response);
            if (!response.error && !this.handshaken && local(message)) { this.handshaken = true; this.afterHandshake(); }
          }
        }).catch((error) => this.write({ jsonrpc: '2.0', id: id ?? null, error: { code: Number.isInteger(error.code) ? error.code : -32603, message: error.message, ...(error.data !== undefined ? { data: error.data } : {}) } })).finally(() => this.active.delete(key));
        this.active.set(key, { controller, task });
      }
    } finally {
      for (const entry of this.active.values()) entry.controller.abort();
      await this.close();
      await Promise.allSettled([...this.active.values()].map((entry) => entry.task));
      await this.writes.catch(() => {});
      this.closed = true;
    }
  }
}
