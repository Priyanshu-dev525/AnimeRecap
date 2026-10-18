#!/usr/bin/env node
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { Store, newRouterKey } from './store.js';
import { Router, Usage, sendJson, errBody } from './router.js';

const PUBLIC_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'public');
const MAX_BODY = 50 * 1024 * 1024; // transcripts/subtitles can be large
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };

export function createServer({ dataDir, env = process.env } = {}) {
  const store = new Store({ dataDir, env });
  const usage = new Usage();
  const router = new Router(store, usage);
  const adminPassword = env.ADMIN_PASSWORD || '';

  const server = http.createServer(async (req, res) => {
    res.setHeader('access-control-allow-origin', '*');
    res.setHeader('access-control-allow-headers', 'authorization, content-type, x-api-key, x-admin-password');
    res.setHeader('access-control-allow-methods', 'GET, POST, PUT, DELETE, OPTIONS');
    if (req.method === 'OPTIONS') return res.writeHead(204).end();

    const url = new URL(req.url, 'http://localhost');
    const p = url.pathname.replace(/\/+$/, '') || '/';
    try {
      if (p === '/health') return sendJson(res, 200, { ok: true });

      // ---- OpenAI-compatible API (also without the /v1 prefix) ----
      const api = p.startsWith('/v1/') ? p.slice(3) : p;
      if (api === '/models' || api === '/chat/completions') {
        if (!checkRouterKey(req, store)) return sendJson(res, 401, errBody('Invalid or missing router API key', 'invalid_api_key'));
        if (api === '/models' && req.method === 'GET') return sendJson(res, 200, router.listModels());
        if (api === '/chat/completions' && req.method === 'POST') {
          const body = await readJson(req);
          if (!body || !Array.isArray(body.messages)) return sendJson(res, 400, errBody('Body must be JSON with a "messages" array', 'invalid_request'));
          return await router.chat(body, res);
        }
        return sendJson(res, 405, errBody('Method not allowed'));
      }

      // ---- Admin API for the dashboard ----
      if (p.startsWith('/api/')) {
        if (adminPassword && !safeEqual(req.headers['x-admin-password'] || '', adminPassword)) {
          return sendJson(res, 401, errBody('Admin password required', 'admin_auth'));
        }
        return await handleAdmin(req, res, p, { store, router, usage, needsPassword: !!adminPassword });
      }

      if (req.method === 'GET') return serveStatic(p, res);
      sendJson(res, 404, errBody('Not found'));
    } catch (err) {
      sendJson(res, err.status || 500, errBody(err.message, 'server_error'));
    }
  });
  return { server, store, router, usage };
}

async function handleAdmin(req, res, p, { store, router, usage, needsPassword }) {
  const m = req.method;
  const parts = p.split('/').slice(2).map(decodeURIComponent); // after /api/
  const [section, id, action] = parts;
  const data = store.data;

  if (section === 'state' && m === 'GET') {
    return sendJson(res, 200, { ...store.publicView(), cooldowns: router.keyStatus(), needsPassword });
  }
  if (section === 'usage' && m === 'GET') {
    return sendJson(res, 200, { logs: usage.logs, totals: usage.totals });
  }

  if (section === 'keys') {
    if (m === 'POST') {
      data.apiKeys.push(newRouterKey());
    } else if (m === 'DELETE') {
      data.apiKeys = data.apiKeys.filter((k) => k !== id);
    } else return sendJson(res, 405, errBody('Method not allowed'));
    store.save();
    return sendJson(res, 200, store.publicView());
  }

  if (section === 'settings' && m === 'PUT') {
    const b = await readJson(req);
    if (typeof b.requireApiKey === 'boolean') data.settings.requireApiKey = b.requireApiKey;
    if (Number(b.timeoutMs) >= 5000) data.settings.timeoutMs = Number(b.timeoutMs);
    store.save();
    return sendJson(res, 200, store.publicView());
  }

  if (section === 'providers') {
    if (m === 'POST' && !id) {
      const b = await readJson(req);
      const pid = String(b.id || '').toLowerCase().replace(/[^a-z0-9_-]/g, '');
      if (!pid || data.providers[pid]) return sendJson(res, 400, errBody('Provider id missing or already exists'));
      if (!/^https?:\/\//.test(b.baseUrl || '')) return sendJson(res, 400, errBody('baseUrl must start with http(s)://'));
      data.providers[pid] = { name: b.name || pid, baseUrl: b.baseUrl, keys: [], models: [], enabled: true, noKey: !!b.noKey };
      store.save();
      return sendJson(res, 200, store.publicView());
    }
    const prov = data.providers[id];
    if (!prov) return sendJson(res, 404, errBody(`No provider "${id}"`));

    if (m === 'PUT' && !action) {
      const b = await readJson(req);
      if (typeof b.enabled === 'boolean') prov.enabled = b.enabled;
      if (typeof b.baseUrl === 'string' && /^https?:\/\//.test(b.baseUrl)) prov.baseUrl = b.baseUrl.trim();
      if (typeof b.name === 'string' && b.name.trim()) prov.name = b.name.trim();
      if (Array.isArray(b.models)) prov.models = [...new Set(b.models.map((x) => String(x).trim()).filter(Boolean))];
      if (Array.isArray(b.addKeys)) {
        for (const k of b.addKeys.map((x) => String(x).trim()).filter(Boolean)) if (!prov.keys.includes(k)) prov.keys.push(k);
      }
      if (Array.isArray(b.removeKeys)) {
        const drop = new Set(b.removeKeys.map(Number));
        prov.keys = prov.keys.filter((_, i) => !drop.has(i));
      }
      store.save();
      return sendJson(res, 200, store.publicView());
    }
    if (m === 'DELETE' && !action) {
      delete data.providers[id];
      store.save();
      store.load(); // built-in presets come back reset; custom ones are gone
      store.save();
      return sendJson(res, 200, store.publicView());
    }
    if (action === 'fetch-models' && m === 'POST') {
      return sendJson(res, 200, await fetchUpstreamModels(store, id));
    }
    if (action === 'test' && m === 'POST') {
      return sendJson(res, 200, await testProvider(store, id));
    }
  }

  if (section === 'combos') {
    if (m === 'PUT' && id) {
      const b = await readJson(req);
      const name = id.trim();
      if (!/^[\w.:-]+$/.test(name)) return sendJson(res, 400, errBody('Combo name may only contain letters, digits, - _ . :'));
      if (data.providers[name]) return sendJson(res, 400, errBody('Combo name clashes with a provider id'));
      const targets = (Array.isArray(b.targets) ? b.targets : []).map((x) => String(x).trim()).filter(Boolean);
      if (!targets.length) return sendJson(res, 400, errBody('A combo needs at least one target'));
      data.combos[name] = targets;
      store.save();
      return sendJson(res, 200, store.publicView());
    }
    if (m === 'DELETE' && id) {
      delete data.combos[id];
      store.save();
      return sendJson(res, 200, store.publicView());
    }
  }

  sendJson(res, 404, errBody('Unknown admin route'));
}

async function fetchUpstreamModels(store, id) {
  const p = store.provider(id);
  const key = store.providerKeys(id)[0];
  if (!p.noKey && !key) return { ok: false, error: 'Add an API key first' };
  const headers = { ...p.headers };
  if (key) headers.authorization = `Bearer ${key}`;
  try {
    const r = await fetch(p.baseUrl.replace(/\/+$/, '') + '/models', { headers, signal: AbortSignal.timeout(20000) });
    const text = await r.text();
    if (!r.ok) return { ok: false, error: `HTTP ${r.status}: ${text.slice(0, 300)}` };
    const json = JSON.parse(text);
    const list = json.data || json.models || [];
    const models = list.map((x) => {
      const mid = String(x.id || x.name || '').replace(/^models\//, '');
      const free = x.pricing ? Number(x.pricing.prompt) === 0 && Number(x.pricing.completion) === 0 : undefined;
      return { id: mid, free };
    }).filter((x) => x.id);
    return { ok: true, models };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

async function testProvider(store, id) {
  const p = store.provider(id);
  const model = p.models?.[0];
  const key = store.providerKeys(id)[0];
  if (!model) return { ok: false, error: 'No models configured' };
  if (!p.noKey && !key) return { ok: false, error: 'Add an API key first' };
  const headers = { 'content-type': 'application/json', ...p.headers };
  if (key) headers.authorization = `Bearer ${key}`;
  const t = Date.now();
  try {
    const r = await fetch(p.baseUrl.replace(/\/+$/, '') + '/chat/completions', {
      method: 'POST',
      headers,
      body: JSON.stringify({ model, messages: [{ role: 'user', content: 'Reply with just: pong' }], max_tokens: 10 }),
      signal: AbortSignal.timeout(30000),
    });
    const text = await r.text();
    if (!r.ok) return { ok: false, model, error: `HTTP ${r.status}: ${text.slice(0, 300)}` };
    const reply = JSON.parse(text).choices?.[0]?.message?.content ?? '';
    return { ok: true, model, ms: Date.now() - t, reply: String(reply).slice(0, 100) };
  } catch (err) {
    return { ok: false, model, error: err.message };
  }
}

function checkRouterKey(req, store) {
  if (store.data.settings.requireApiKey === false) return true;
  const auth = req.headers.authorization || '';
  const given = auth.replace(/^Bearer\s+/i, '') || req.headers['x-api-key'] || '';
  return !!given && store.routerKeys().some((k) => safeEqual(given, k));
}

function safeEqual(a, b) {
  const x = Buffer.from(String(a));
  const y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(Object.assign(new Error('Request body too large'), { status: 413 }));
        req.destroy();
      } else chunks.push(c);
    });
    req.on('end', () => {
      if (!size) return resolve({});
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch {
        reject(Object.assign(new Error('Invalid JSON body'), { status: 400 }));
      }
    });
    req.on('error', reject);
  });
}

function serveStatic(p, res) {
  const rel = p === '/' ? 'index.html' : p.slice(1);
  const file = path.normalize(path.join(PUBLIC_DIR, rel));
  if (!file.startsWith(PUBLIC_DIR) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    return sendJson(res, 404, errBody('Not found'));
  }
  res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
}

function loadDotEnv() {
  try {
    for (const line of fs.readFileSync('.env', 'utf8').split('\n')) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m && m[2] && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  } catch {}
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  loadDotEnv();
  const port = Number(process.env.PORT) || 20128;
  const host = process.env.HOST || '127.0.0.1';
  const { server, store } = createServer({ dataDir: process.env.DATA_DIR });
  server.listen(port, host, () => {
    const base = `http://${host === '0.0.0.0' ? 'localhost' : host}:${port}`;
    console.log(`\n  AnimeRecap Router running`);
    console.log(`  Dashboard : ${base}/`);
    console.log(`  API base  : ${base}/v1`);
    console.log(`  API key   : ${store.data.apiKeys[0] || '(set ROUTER_API_KEY)'}`);
    if (host !== '127.0.0.1' && host !== 'localhost' && !process.env.ADMIN_PASSWORD) {
      console.warn('\n  WARNING: listening on a public interface without ADMIN_PASSWORD. Anyone who can reach this port can edit your keys.');
    }
    console.log('');
  });
}
