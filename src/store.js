import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { PRESETS, DEFAULT_COMBOS, presetFor } from './providers.js';

export function newRouterKey() {
  return 'sk-anime-' + crypto.randomBytes(18).toString('base64url');
}

export function maskKey(key) {
  if (!key) return '';
  return key.length <= 10 ? '••••' : `${key.slice(0, 6)}…${key.slice(-4)}`;
}

const CONFIG_VERSION = 2;

// v2 added OpenCode Zen and Cline: add their targets to existing default combos.
function mergeDefaultTargets(combos) {
  for (const [name, targets] of Object.entries(DEFAULT_COMBOS)) {
    const have = combos[name];
    if (!have) continue;
    const merged = [];
    for (const t of targets) if (have.includes(t) || /^(opencode|cline)\//.test(t)) merged.push(t);
    for (const t of have) if (!merged.includes(t)) merged.push(t);
    combos[name] = merged;
  }
}

// Key saved by `opencode auth login` (~/.local/share/opencode/auth.json).
function readOpenCodeLogin(env) {
  const home = env.HOME || env.USERPROFILE;
  const dirs = [env.XDG_DATA_HOME, home && path.join(home, '.local', 'share'), env.LOCALAPPDATA].filter(Boolean);
  for (const dir of dirs) {
    try {
      const auth = JSON.parse(fs.readFileSync(path.join(dir, 'opencode', 'auth.json'), 'utf8'));
      const key = auth.opencode?.key;
      if (typeof key === 'string' && key) return [key];
    } catch {}
  }
  return [];
}

// Persistent config in <dataDir>/config.json. Keys from environment
// variables are merged in at runtime and never written to disk.
export class Store {
  constructor({ dataDir, env = process.env } = {}) {
    this.dataDir = dataDir || path.resolve('data');
    this.file = path.join(this.dataDir, 'config.json');
    this.env = env;
    this.data = this.load();
  }

  load() {
    let data = {};
    try {
      data = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    } catch (err) {
      if (err.code !== 'ENOENT') throw new Error(`Cannot read ${this.file}: ${err.message}`);
    }
    const fresh = !data.providers || (data.version || 1) < CONFIG_VERSION;
    data.apiKeys ??= [newRouterKey()];
    data.providers ??= {};
    if (!data.combos) data.combos = structuredClone(DEFAULT_COMBOS);
    else if ((data.version || 1) < 2) mergeDefaultTargets(data.combos);
    data.version = CONFIG_VERSION;
    data.settings ??= { requireApiKey: true, timeoutMs: 120000 };
    for (const p of PRESETS) {
      data.providers[p.id] ??= {
        name: p.name,
        baseUrl: p.baseUrl,
        keys: [],
        models: [...p.models],
        enabled: true,
      };
    }
    this.data = data;
    if (fresh) this.save();
    return data;
  }

  save() {
    fs.mkdirSync(this.dataDir, { recursive: true });
    const tmp = this.file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2), { mode: 0o600 });
    fs.renameSync(tmp, this.file);
  }

  provider(id) {
    const p = this.data.providers[id];
    if (!p) return null;
    const preset = presetFor(id);
    return { id, ...p, noKey: p.noKey ?? preset?.noKey ?? false, headers: { ...preset?.headers, ...p.headers } };
  }

  envKeys(id) {
    const preset = presetFor(id);
    const raw = preset?.env && this.env[preset.env];
    return raw ? raw.split(',').map((k) => k.trim()).filter(Boolean) : [];
  }

  localKeys(id) {
    if (!presetFor(id)?.importOpenCodeLogin) return [];
    const now = Date.now();
    if (!this.loginCache || now - this.loginCache.at > 30_000) this.loginCache = { at: now, keys: readOpenCodeLogin(this.env) };
    return this.loginCache.keys;
  }

  // All keys for a provider: saved, then env, then a local app login.
  // Providers with an anonymous key fall back to it when nothing is set.
  providerKeys(id) {
    const saved = this.data.providers[id]?.keys || [];
    const keys = [...new Set([...saved, ...this.envKeys(id), ...this.localKeys(id)])];
    const anon = presetFor(id)?.anonymousKey;
    return keys.length || !anon ? keys : [anon];
  }

  isReady(id) {
    const p = this.provider(id);
    return !!p && p.enabled !== false && (p.noKey || this.providerKeys(id).length > 0);
  }

  routerKeys() {
    const extra = this.env.ROUTER_API_KEY ? [this.env.ROUTER_API_KEY] : [];
    return [...this.data.apiKeys, ...extra];
  }

  // Safe view for the dashboard: provider keys are masked.
  publicView() {
    const order = (id) => {
      const i = PRESETS.findIndex((p) => p.id === id);
      return i < 0 ? PRESETS.length : i;
    };
    const ids = Object.keys(this.data.providers).sort((a, b) => order(a) - order(b));
    const providers = ids.map((id) => {
      const p = this.provider(id);
      const preset = presetFor(id);
      return {
        id,
        name: p.name,
        baseUrl: p.baseUrl,
        enabled: p.enabled !== false,
        noKey: p.noKey,
        custom: !preset,
        signup: preset?.signup,
        note: preset?.note,
        env: preset?.env,
        models: p.models,
        keys: (p.keys || []).map(maskKey),
        envKeys: this.envKeys(id).map(maskKey),
        localKeys: this.localKeys(id).map(maskKey),
        anonymous: !!preset?.anonymousKey,
        ready: this.isReady(id),
      };
    });
    return {
      apiKeys: this.data.apiKeys,
      envRouterKey: !!this.env.ROUTER_API_KEY,
      providers,
      combos: this.data.combos,
      settings: this.data.settings,
    };
  }
}
