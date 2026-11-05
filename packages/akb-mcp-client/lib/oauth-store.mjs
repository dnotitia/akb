import { constants, closeSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, statfsSync, unlinkSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createHash, randomBytes } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

const LOCAL_FILESYSTEMS = {
  darwin: new Set([17, 26]),
  linux: new Set([0xef53, 0x58465342, 0x9123683e, 0x01021994, 0x794c7630, 0x2fc12fc1]),
};

function privatePath(path, directory = false) {
  const info = lstatSync(path);
  if (info.isSymbolicLink() || (directory ? !info.isDirectory() : !info.isFile()) || info.uid !== process.getuid() || (info.mode & 0o077)) {
    throw new Error('OAuth storage must be owned by the current user, private, and free of symlinks.');
  }
  if (!directory && info.nlink !== 1) throw new Error('OAuth storage must not contain hard-linked files.');
  return info;
}

function privateDirectory(path) {
  const parent = dirname(path);
  if (parent !== path) {
    try {
      const info = lstatSync(parent);
      if (info.isSymbolicLink() || !info.isDirectory()) throw new Error('OAuth storage parent must be a real directory.');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      privateDirectory(parent);
    }
  }
  try { mkdirSync(path, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
  privatePath(path, true);
}

export function sameBinding(left, right) {
  return left?.resource === right?.resource && left?.issuer === right?.issuer && left?.clientId === right?.clientId;
}

export class OAuthStore {
  constructor(resource, { directory = process.env.AKB_MCP_AUTH_DIR ?? join(homedir(), '.local', 'state', 'akb-mcp'), syncDirectory = (fd) => fsyncSync(fd) } = {}) {
    if (!['darwin', 'linux'].includes(process.platform)) throw new Error('OAuth storage supports macOS and Linux local filesystems. Use PAT on other platforms.');
    this.syncDirectory = syncDirectory;
    this.directory = resolve(directory);
    privateDirectory(this.directory);
    if (!LOCAL_FILESYSTEMS[process.platform].has(Number(statfsSync(this.directory).type) >>> 0)) throw new Error('OAuth storage requires a supported local filesystem. Network filesystems are not supported.');
    const key = createHash('sha256').update(resource).digest('hex');
    this.recordPath = join(this.directory, `${key}.json`);
    const databasePath = join(this.directory, `${key}.sqlite`);
    try { closeSync(openSync(databasePath, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600)); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    privatePath(databasePath);
    this.database = new DatabaseSync(databasePath);
    try { this.database.exec('PRAGMA busy_timeout=0; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS publication (id INTEGER PRIMARY KEY CHECK (id=1), publication_id TEXT NOT NULL)'); }
    catch (error) { this.database.close(); throw error; }
    this.owned = false;
  }
  read() {
    privatePath(this.directory, true);
    try {
      privatePath(this.recordPath);
      const fd = openSync(this.recordPath, constants.O_RDONLY | constants.O_NOFOLLOW);
      try {
        const text = readFileSync(fd, 'utf8');
        if (text.length > 128 * 1024) throw new Error('OAuth credential record exceeds the size limit.');
        const record = JSON.parse(text);
        if (record.version !== 1 || !record.binding || typeof record.binding.resource !== 'string') throw new Error('Invalid OAuth credential record.');
        return record;
      } finally { closeSync(fd); }
    } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  isPublished(record) {
    if (!record?.publicationId) return false;
    return this.database.prepare('SELECT publication_id FROM publication WHERE id=1').get()?.publication_id === record.publicationId;
  }
  publish(publicationId) {
    if (!this.owned) throw new Error('OAuth publication requires exclusive ownership.');
    this.database.prepare('INSERT INTO publication (id, publication_id) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET publication_id=excluded.publication_id').run(publicationId);
    this.database.exec('COMMIT');
    this.owned = false;
  }
  acquire() {
    if (this.owned) return false;
    try { this.database.exec('BEGIN IMMEDIATE'); this.owned = true; return true; }
    catch (error) { if (error.code === 'ERR_SQLITE_ERROR' && /busy|locked/i.test(error.message)) return false; throw error; }
  }
  release() {
    if (!this.owned) return;
    try { this.database.exec('ROLLBACK'); } finally { this.owned = false; }
  }
  save(record) {
    if (!this.owned) throw new Error('OAuth credentials require exclusive ownership.');
    privatePath(this.directory, true);
    try { privatePath(this.recordPath); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const temporary = `${this.recordPath}.${randomBytes(12).toString('hex')}.tmp`;
    const fd = openSync(temporary, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
    try {
      writeFileSync(fd, JSON.stringify({ ...record, version: 1 }) + '\n');
      fsyncSync(fd);
    } catch (error) { try { unlinkSync(temporary); } catch {} throw error; }
    finally { closeSync(fd); }
    try {
      renameSync(temporary, this.recordPath);
      const directoryFd = openSync(this.directory, constants.O_RDONLY | constants.O_NOFOLLOW);
      try { this.syncDirectory(directoryFd); } finally { closeSync(directoryFd); }
    } catch (error) { try { unlinkSync(temporary); } catch {} throw error; }
  }
  close() { this.release(); this.database.close(); }
}
