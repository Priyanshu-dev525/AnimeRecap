// Core routing: resolve a requested model name to an ordered list of
// provider targets, then try them (rotating keys) until one succeeds.

const KEY_ERRORS = new Set([401, 403, 429]); // try the next key of the same provider

export class Usage {
  constructor(limit = 300) {
    this.limit = limit;
    this.logs = [];
    this.totals = {}; // "provider/model" -> { requests, errors, promptTokens, completionTokens }
  }

  record(entry) {
    this.logs.unshift({ time: new Date().toISOString(), ...entry });
    if (this.logs.length > this.limit) this.logs.length = this.limit;
    const k = entry.target || entry.model;
    const t = (this.totals[k] ??= { requests: 0, errors: 0, promptTokens: 0, completionTokens: 0 });
    t.requests++;
    if (!entry.ok) t.errors++;
    t.promptTokens += entry.usage?.prompt_tokens || 0;
    t.completionTokens += entry.usage?.completion_tokens || 0;
  }
}

export class Router {
  constructor(store, usage = new Usage()) {
    this.store = store;
    this.usage = usage;
    this.cooldowns = new Map(); // "provider:key" -> until (ms)
    this.cursor = new Map(); // provider -> round-robin index
  }

  // "combo" | "provider/model" | bare model name found in a provider's list.
  resolve(model) {
    if (!model) return [];
    const { providers, combos } = this.store.data;
    if (combos[model]) return combos[model].flatMap((m) => this.resolveDirect(m));
    const direct = this.resolveDirect(model);
    if (direct.length) return direct;
    return Object.keys(providers)
      .filter((id) => providers[id].models?.includes(model))
      .map((id) => ({ provider: id, model }));
  }

  resolveDirect(name) {
    const i = name.indexOf('/');
    if (i > 0 && this.store.data.providers[name.slice(0, i)]) {
      return [{ provider: name.slice(0, i), model: name.slice(i + 1) }];
    }
    return [];
  }

  listModels() {
    const out = [];
    const { providers, combos } = this.store.data;
    for (const name of Object.keys(combos)) out.push({ id: name, object: 'model', owned_by: 'combo' });
    for (const id of Object.keys(providers)) {
      if (!this.store.isReady(id)) continue;
      for (const m of providers[id].models || []) out.push({ id: `${id}/${m}`, object: 'model', owned_by: id });
    }
    return { object: 'list', data: out };
  }

  // Keys not in cooldown, rotated round-robin so load spreads across keys.
  availableKeys(providerId) {
    const p = this.store.provider(providerId);
    if (p.noKey) return [null];
    const keys = this.store.providerKeys(providerId);
    const now = Date.now();
    const usable = keys.filter((k) => (this.cooldowns.get(`${providerId}:${k}`) || 0) <= now);
    if (!usable.length) return [];
    const start = (this.cursor.get(providerId) || 0) % usable.length;
    this.cursor.set(providerId, start + 1);
    return [...usable.slice(start), ...usable.slice(0, start)];
  }

  cooldown(providerId, key, status, retryAfter) {
    if (!key) return;
    let ms = status === 429 ? 30_000 : 10 * 60_000;
    const sec = Number(retryAfter);
    if (status === 429 && sec > 0) ms = Math.min(sec * 1000, 3600_000);
    this.cooldowns.set(`${providerId}:${key}`, Date.now() + ms);
  }

  keyStatus() {
    const now = Date.now();
    const out = {};
    for (const [k, until] of this.cooldowns) {
      if (until > now) {
        const [provider] = k.split(':');
        out[provider] = (out[provider] || 0) + 1;
      }
    }
    return out;
  }

  // Handles an OpenAI /chat/completions request end-to-end.
  async chat(body, res) {
    const started = Date.now();
    const targets = this.resolve(body.model);
    if (!targets.length) {
      return sendJson(res, 404, errBody(`Unknown model "${body.model}". Use a combo name or "provider/model". See GET /v1/models.`, 'model_not_found'));
    }

    const attempts = [];
    let last = null;
    for (const t of targets) {
      const label = `${t.provider}/${t.model}`;
      if (!this.store.isReady(t.provider)) {
        attempts.push({ target: label, skipped: 'provider disabled or no API key' });
        continue;
      }
      const keys = this.availableKeys(t.provider);
      if (!keys.length) {
        attempts.push({ target: label, skipped: 'all keys cooling down' });
        continue;
      }
      for (const key of keys) {
        const r = await this.attempt(t, key, body, res);
        if (r.ok) {
          this.usage.record({ ok: true, model: body.model, target: label, status: 200, ms: Date.now() - started, stream: !!body.stream, usage: r.usage, attempts: attempts.length + 1 });
          return;
        }
        if (r.aborted) {
          this.usage.record({ ok: false, model: body.model, target: label, status: 499, ms: Date.now() - started, error: 'client disconnected' });
          return;
        }
        last = r;
        attempts.push({ target: label, status: r.status, error: r.error?.slice(0, 300) });
        if (KEY_ERRORS.has(r.status)) {
          this.cooldown(t.provider, key, r.status, r.retryAfter);
          continue;
        }
        break; // model/provider-level failure: move on to the next target
      }
    }

    const status = last?.status && last.status !== 0 ? last.status : 502;
    this.usage.record({ ok: false, model: body.model, status, ms: Date.now() - started, error: 'all targets failed', attempts: attempts.length, detail: attempts });
    sendJson(res, status, {
      error: {
        message: `All ${targets.length} target(s) for "${body.model}" failed. Last error: ${last?.error?.slice(0, 500) || attempts.at(-1)?.skipped || 'none'}`,
        type: 'router_error',
        attempts,
      },
    });
  }

  async attempt(target, key, body, res) {
    const p = this.store.provider(target.provider);
    const url = p.baseUrl.replace(/\/+$/, '') + '/chat/completions';
    const ctrl = new AbortController();
    let clientGone = false;
    const onClose = () => {
      if (!res.writableFinished) {
        clientGone = true;
        ctrl.abort();
      }
    };
    res.on('close', onClose);
    const timeoutMs = this.store.data.settings.timeoutMs || 120000;
    let timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const headers = { 'content-type': 'application/json', ...p.headers };
    if (key) headers.authorization = `Bearer ${key}`;

    try {
      const up = await fetch(url, { method: 'POST', headers, body: JSON.stringify({ ...body, model: target.model }), signal: ctrl.signal });
      if (!up.ok) {
        const text = await up.text().catch(() => '');
        return { ok: false, status: up.status, error: text || up.statusText, retryAfter: up.headers.get('retry-after') };
      }
      const routed = { 'x-router-provider': target.provider, 'x-router-model': target.model };

      if (body.stream) {
        clearTimeout(timer); // streams may legitimately run long
        timer = null;
        res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'keep-alive', ...routed });
        const usage = await pipeSse(up.body, res);
        res.end();
        return { ok: true, usage };
      }

      const json = await up.json();
      sendJson(res, 200, json, routed);
      return { ok: true, usage: json.usage };
    } catch (err) {
      if (clientGone) return { ok: false, aborted: true };
      if (res.headersSent) {
        // Failed mid-stream; we cannot fall back any more.
        res.end();
        return { ok: true, usage: null };
      }
      return { ok: false, status: 0, error: err.name === 'AbortError' ? `timeout after ${timeoutMs}ms` : err.message };
    } finally {
      if (timer) clearTimeout(timer);
      res.off('close', onClose);
    }
  }
}

// Forward an SSE body unchanged and pick out a usage block if one is present.
async function pipeSse(stream, res) {
  const decoder = new TextDecoder();
  let buf = '';
  let usage = null;
  for await (const chunk of stream) {
    res.write(chunk);
    buf += decoder.decode(chunk, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line.startsWith('data:') && line.includes('"usage"')) {
        try {
          usage = JSON.parse(line.slice(5)).usage || usage;
        } catch {}
      }
    }
  }
  return usage;
}

export function errBody(message, code = 'error') {
  return { error: { message, type: code, code } };
}

export function sendJson(res, status, obj, extraHeaders = {}) {
  if (res.headersSent) return res.end();
  const data = JSON.stringify(obj);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data), ...extraHeaders });
  res.end(data);
}
