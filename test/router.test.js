import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createServer } from '../src/server.js';

// Mock upstream: behaviour is selected by the API key sent.
const seen = [];
const upstream = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    const key = (req.headers.authorization || '').replace('Bearer ', '');
    seen.push({ url: req.url, key, body: body && JSON.parse(body) });
    if (req.url.endsWith('/models')) {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ data: [{ id: 'm1', pricing: { prompt: '0', completion: '0' } }, { id: 'm2' }] }));
    }
    if (key === 'limited') {
      res.writeHead(429, { 'retry-after': '120' });
      return res.end('rate limited');
    }
    if (key === 'broken') {
      res.writeHead(500);
      return res.end('boom');
    }
    const parsed = JSON.parse(body);
    if (parsed.stream) {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n');
      res.write('data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n');
      return res.end('data: [DONE]\n\n');
    }
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ choices: [{ message: { role: 'assistant', content: `ok from ${key} ${parsed.model}` } }], usage: { prompt_tokens: 5, completion_tokens: 4 } }));
  });
});

let base, ctx, dataDir, upBase;
before(async () => {
  await new Promise((r) => upstream.listen(0, '127.0.0.1', r));
  upBase = `http://127.0.0.1:${upstream.address().port}/v1`;
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'router-test-'));
  ctx = createServer({ dataDir, env: {} });
  const d = ctx.store.data;
  d.apiKeys = ['test-key'];
  d.providers.alpha = { name: 'Alpha', baseUrl: upBase, keys: ['limited', 'good-a'], models: ['m1'], enabled: true };
  d.providers.beta = { name: 'Beta', baseUrl: upBase, keys: ['broken'], models: ['m2'], enabled: true };
  d.providers.gamma = { name: 'Gamma', baseUrl: upBase, keys: ['good-g'], models: ['m3'], enabled: true };
  d.combos = { recap: ['beta/m2', 'gemini/gemini-2.5-flash', 'gamma/m3'] };
  ctx.store.save();
  await new Promise((r) => ctx.server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${ctx.server.address().port}`;
});
after(() => {
  ctx.server.close();
  upstream.close();
  fs.rmSync(dataDir, { recursive: true, force: true });
});

const chat = (body, key = 'test-key') =>
  fetch(`${base}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${key}` },
    body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }], ...body }),
  });

test('rejects requests without a valid router key', async () => {
  assert.equal((await chat({ model: 'recap' }, 'nope')).status, 401);
  assert.equal((await fetch(`${base}/v1/models`)).status, 401);
});

test('lists combos and models of ready providers', async () => {
  const r = await fetch(`${base}/v1/models`, { headers: { authorization: 'Bearer test-key' } });
  const ids = (await r.json()).data.map((m) => m.id);
  assert.ok(ids.includes('recap'));
  assert.ok(ids.includes('alpha/m1'));
  assert.ok(!ids.includes('gemini/gemini-2.5-flash'), 'providers without keys are hidden');
});

test('rotates past a rate-limited key and cools it down', async () => {
  seen.length = 0;
  const r = await chat({ model: 'alpha/m1' });
  assert.equal(r.status, 200);
  assert.match((await r.json()).choices[0].message.content, /good-a m1/);
  assert.ok(seen.some((s) => s.key === 'limited'));
  assert.ok(ctx.router.cooldowns.get('alpha:limited') > Date.now() + 100_000, 'retry-after honoured');

  seen.length = 0;
  await chat({ model: 'alpha/m1' });
  assert.deepEqual(seen.map((s) => s.key), ['good-a'], 'cooled-down key is skipped');
});

test('combo falls back across providers and skips unconfigured ones', async () => {
  seen.length = 0;
  const r = await chat({ model: 'recap' });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('x-router-provider'), 'gamma');
  assert.match((await r.json()).choices[0].message.content, /good-g m3/);
  assert.deepEqual(seen.map((s) => s.body.model), ['m2', 'm3'], 'upstream receives bare model names');
});

test('bare model names resolve to the provider that lists them', async () => {
  const r = await chat({ model: 'm3' });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('x-router-provider'), 'gamma');
});

test('streams SSE through unchanged and records usage', async () => {
  const r = await chat({ model: 'gamma/m3', stream: true });
  assert.equal(r.status, 200);
  assert.match(r.headers.get('content-type'), /event-stream/);
  const text = await r.text();
  assert.match(text, /Hel/);
  assert.match(text, /\[DONE\]/);
  assert.equal(ctx.usage.logs[0].usage.completion_tokens, 2);
});

test('returns an aggregated error when every target fails', async () => {
  const r = await chat({ model: 'beta/m2' });
  assert.equal(r.status, 500);
  const j = await r.json();
  assert.equal(j.error.type, 'router_error');
  assert.equal(j.error.attempts.length, 1);
});

test('unknown model is a 404', async () => {
  assert.equal((await chat({ model: 'does-not-exist' })).status, 404);
});

test('admin API masks provider keys and can fetch upstream models', async () => {
  const s = await (await fetch(`${base}/api/state`)).json();
  const alpha = s.providers.find((p) => p.id === 'alpha');
  assert.ok(alpha.keys.every((k) => !k.includes('good-a') || k.length < 6));
  const f = await (await fetch(`${base}/api/providers/gamma/fetch-models`, { method: 'POST' })).json();
  assert.deepEqual(f.models.map((m) => m.id), ['m1', 'm2']);
  assert.equal(f.models[0].free, true);
});

test('admin API edits combos and keys persist to disk', async () => {
  let r = await fetch(`${base}/api/combos/mine`, { method: 'PUT', body: JSON.stringify({ targets: ['gamma/m3', ''] }) });
  assert.equal(r.status, 200);
  r = await fetch(`${base}/api/providers/gamma`, { method: 'PUT', body: JSON.stringify({ addKeys: ['k2'] }) });
  assert.equal(r.status, 200);
  const disk = JSON.parse(fs.readFileSync(path.join(dataDir, 'config.json'), 'utf8'));
  assert.deepEqual(disk.combos.mine, ['gamma/m3']);
  assert.deepEqual(disk.providers.gamma.keys, ['good-g', 'k2']);
});

test('admin password is enforced when set', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'router-test-'));
  const c = createServer({ dataDir: dir, env: { ADMIN_PASSWORD: 'pw' } });
  await new Promise((r) => c.server.listen(0, '127.0.0.1', r));
  const b = `http://127.0.0.1:${c.server.address().port}`;
  assert.equal((await fetch(`${b}/api/state`)).status, 401);
  assert.equal((await fetch(`${b}/api/state`, { headers: { 'x-admin-password': 'pw' } })).status, 200);
  c.server.close();
  fs.rmSync(dir, { recursive: true, force: true });
});
