import { constants, closeSync, fstatSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, realpathSync, renameSync, statfsSync, unlinkSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { oauthBusy } from './oauth-error.mjs';

const LOCAL_FILESYSTEMS = {
  darwin: new Set([17, 26]),
  linux: new Set([0xef53, 0x58465342, 0x9123683e, 0x01021994, 0x794c7630, 0x2fc12fc1]),
};

function assertPrivate(info, directory) {
  if (info.isSymbolicLink() || (directory ? !info.isDirectory() : !info.isFile()) || info.uid !== BigInt(process.getuid()) || (info.mode & 0o077n)) {
    throw new Error('OAuth storage must be owned by the current user, private, and free of symlinks.');
  }
  if (!directory && info.nlink !== 1n) throw new Error('OAuth storage must not contain hard-linked files.');
  return info;
}

function privatePath(path, directory = false) {
  return assertPrivate(lstatSync(path, { bigint: true }), directory);
}

function validateAncestry(path) {
  const parent = dirname(path);
  const protectedRoot = parent === path || validateAncestry(parent);
  const info = lstatSync(path);
  if (info.isSymbolicLink()) {
    if (info.uid !== 0 || !protectedRoot) throw new Error('OAuth storage ancestry must not contain mutable symlinks.');
    return validateAncestry(realpathSync(path));
  }
  const writable = Boolean(info.mode & 0o022);
  const stickyRoot = info.uid === 0 && Boolean(info.mode & 0o1000);
  if (!info.isDirectory() || ![0, process.getuid()].includes(info.uid) || (writable && !stickyRoot)) throw new Error('OAuth storage ancestry must use trusted directories without shared writable parents.');
  return protectedRoot && info.uid === 0 && !writable;
}

function privateDirectory(path) {
  const parent = dirname(path);
  try { validateAncestry(parent); }
  catch (error) { if (error.code !== 'ENOENT') throw error; privateDirectory(parent); }
  try { mkdirSync(path, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
  privatePath(path, true);
}

function sqliteBusy(error) {
  return error.code === 'ERR_SQLITE_ERROR' && [5, 6].includes(error.errcode & 0xff);
}

export class OAuthStore {
  constructor(resource, { directory = process.env.AKB_MCP_AUTH_DIR ?? join(homedir(), '.local', 'state', 'akb-mcp'), syncDirectory = (fd) => fsyncSync(fd) } = {}) {
    this.syncDirectory = syncDirectory;
    this.directory = resolve(directory);
    privateDirectory(this.directory);
    this.directory = realpathSync(this.directory);
    validateAncestry(this.directory);
    this.directoryIdentity = privatePath(this.directory, true);
    if (!LOCAL_FILESYSTEMS[process.platform].has(Number(statfsSync(this.directory).type) >>> 0)) throw new Error('OAuth storage requires a supported local filesystem. Network filesystems are not supported.');
    const key = createHash('sha256').update(resource).digest('hex');
    this.recordPath = join(this.directory, `${key}.json`);
    this.databasePath = join(this.directory, `${key}.sqlite`);
    try { closeSync(openSync(this.databasePath, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600)); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    this.databaseIdentity = privatePath(this.databasePath);
    try {
      this.database = new DatabaseSync(this.databasePath, { timeout: 0 });
      this.database.exec(`
        PRAGMA synchronous=FULL;
        PRAGMA fullfsync=ON;
        CREATE TABLE IF NOT EXISTS publication (id INTEGER PRIMARY KEY CHECK (id=1), publication_id TEXT NOT NULL);
      `);
    } catch (error) {
      this.database?.close();
      if (sqliteBusy(error)) throw oauthBusy();
      throw error;
    }
  }
  _checkPaths() {
    validateAncestry(this.directory);
    const directory = privatePath(this.directory, true);
    if (directory.dev !== this.directoryIdentity.dev || directory.ino !== this.directoryIdentity.ino) throw new Error('OAuth storage directory identity changed.');
    const database = privatePath(this.databasePath);
    if (database.dev !== this.databaseIdentity.dev || database.ino !== this.databaseIdentity.ino) throw new Error('OAuth storage database identity changed.');
  }
  read() {
    this._checkPaths();
    try {
      const fd = openSync(this.recordPath, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const info = assertPrivate(fstatSync(fd, { bigint: true }), false);
        if (info.size > 128n * 1024n) throw new Error('OAuth credential record exceeds the size limit.');
        const text = readFileSync(fd, 'utf8');
        const record = JSON.parse(text);
        if (record.version !== 1 || !record.binding || typeof record.binding.resource !== 'string') throw new Error('Invalid OAuth credential record.');
        return record;
      } finally { closeSync(fd); }
    } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  isPublished(publicationId) {
    this._checkPaths();
    try {
      return this.database.prepare('SELECT publication_id FROM publication WHERE id=1').get()?.publication_id === publicationId;
    } catch (error) {
      if (sqliteBusy(error)) return false;
      throw error;
    }
  }
  publish(publicationId) {
    if (!this.database.isTransaction) throw new Error('OAuth publication requires exclusive ownership.');
    this._checkPaths();
    this.database.prepare('INSERT INTO publication (id, publication_id) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET publication_id=excluded.publication_id').run(publicationId);
    this.database.exec('COMMIT');
  }
  acquire() {
    this._checkPaths();
    if (this.database.isTransaction) return false;
    try { this.database.exec('BEGIN IMMEDIATE'); return true; }
    catch (error) { if (sqliteBusy(error)) return false; throw error; }
  }
  release() {
    if (this.database.isTransaction) this.database.exec('ROLLBACK');
  }
  save(record) {
    if (!this.database.isTransaction) throw new Error('OAuth credentials require exclusive ownership.');
    this._checkPaths();
    try { privatePath(this.recordPath); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const json = JSON.stringify({ ...record, version: 1 }) + '\n';
    if (Buffer.byteLength(json) > 128 * 1024) throw new Error('OAuth credential record exceeds the size limit.');
    const temporary = `${this.recordPath}.tmp`;
    try { privatePath(temporary); unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const fd = openSync(temporary, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
    try {
      writeFileSync(fd, json);
      fsyncSync(fd);
    } catch (error) { try { unlinkSync(temporary); } catch {} throw error; }
    finally { closeSync(fd); }
    try {
      renameSync(temporary, this.recordPath);
      const directoryFd = openSync(this.directory, constants.O_RDONLY | constants.O_NOFOLLOW);
      try { this.syncDirectory(directoryFd); } finally { closeSync(directoryFd); }
    } catch (error) { try { unlinkSync(temporary); } catch {} throw error; }
  }
  close() { try { this.release(); } finally { this.database.close(); } }
}
