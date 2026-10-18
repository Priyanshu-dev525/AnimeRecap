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
    const fresh = !data.providers;
    data.apiKeys ??= [newRouterKey()];
    data.providers ??= {};
    data.combos ??= { ...DEFAULT_COMBOS };
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

  // All keys for a provider: saved ones first, then env ones.
  providerKeys(id) {
    const saved = this.data.providers[id]?.keys || [];
    return [...new Set([...saved, ...this.envKeys(id)])];
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
    const providers = Object.keys(this.data.providers).map((id) => {
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
