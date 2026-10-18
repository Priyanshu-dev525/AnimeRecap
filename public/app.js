const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
let state = null;
let adminPassword = sessionStorageGet('adminPassword') || '';

function sessionStorageGet(k) {
  try { return sessionStorage.getItem(k); } catch { return null; }
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove('show'), 2200);
}

async function api(path, opts = {}) {
  const r = await fetch('/api/' + path, {
    ...opts,
    headers: { 'content-type': 'application/json', 'x-admin-password': adminPassword },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401) {
    adminPassword = prompt('Admin password') || '';
    try { sessionStorage.setItem('adminPassword', adminPassword); } catch {}
    return api(path, opts);
  }
  const json = await r.json();
  if (!r.ok) throw new Error(json.error?.message || r.statusText);
  return json;
}

async function refresh(view) {
  state = view ? { ...state, ...view } : await api('state');
  renderConnect();
  renderProviders();
  renderCombos();
  renderPlaygroundModels();
}

// ---------- Connect ----------
function renderConnect() {
  const base = location.origin + '/v1';
  const key = state.apiKeys[0] || 'YOUR_ROUTER_KEY';
  $('#baseUrl').textContent = base;
  document.querySelectorAll('.baseUrlText').forEach((el) => (el.textContent = base));
  $('#requireKey').checked = state.settings.requireApiKey !== false;
  $('#apiKeys').innerHTML =
    state.apiKeys.map((k) => `<div class="keyline"><code class="copy">${esc(k)}</code><button class="danger small" data-delkey="${esc(k)}">Delete</button></div>`).join('') +
    (state.envRouterKey ? '<div class="muted small">+ ROUTER_API_KEY from environment</div>' : '') +
    (!state.apiKeys.length && !state.envRouterKey ? '<div class="muted">No keys. Create one.</div>' : '');

  const ready = state.providers.filter((p) => p.ready);
  $('#status').innerHTML = ready.length
    ? ready.map((p) => `<span class="pill ok">${esc(p.name)}</span>`).join('')
    : '<span class="pill warn">No providers ready yet. Add a free key under Providers.</span>';

  const models = Object.fromEntries(Object.keys(state.combos).map((c) => [c, { name: `${c} (combo)` }]));
  $('#opencodeCfg').textContent = JSON.stringify({
    $schema: 'https://opencode.ai/config.json',
    provider: {
      animerouter: {
        npm: '@ai-sdk/openai-compatible',
        name: 'AnimeRecap Router',
        options: { baseURL: base, apiKey: key },
        models,
      },
    },
    model: 'animerouter/free-coder',
  }, null, 2);
  $('#curlCfg').textContent = `curl ${base}/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"anime-recap","messages":[{"role":"user","content":"Recap episode 1 of a shonen anime in 3 lines"}]}'`;
}

$('#newKey').onclick = async () => refresh(await api('keys', { method: 'POST' }));
$('#requireKey').onchange = async (e) => refresh(await api('settings', { method: 'PUT', body: { requireApiKey: e.target.checked } }));
document.addEventListener('click', async (e) => {
  const t = e.target;
  if (t.dataset.delkey && confirm('Delete this router key? Clients using it will stop working.')) {
    refresh(await api('keys/' + encodeURIComponent(t.dataset.delkey), { method: 'DELETE' }));
  } else if (t.classList.contains('copy')) {
    navigator.clipboard?.writeText(t.textContent).then(() => toast('Copied'));
  }
});

// ---------- Providers ----------
function renderProviders() {
  $('#providerList').innerHTML = state.providers.map((p) => {
    const cooling = state.cooldowns?.[p.id];
    const status = !p.enabled ? '<span class="pill">disabled</span>'
      : p.ready ? `<span class="pill ok">${p.anonymous && !p.keys.length && !p.envKeys.length && !p.localKeys.length ? 'ready · free, no key' : 'ready'}</span>${cooling ? `<span class="pill warn">${cooling} key(s) cooling down</span>` : ''}`
      : '<span class="pill warn">needs key</span>';
    const keys = p.keys.map((k, i) => `<div class="keyline"><code>${esc(k)}</code><button class="danger small" data-act="delkey" data-i="${i}">Remove</button></div>`).join('');
    const envKeys = p.envKeys.map((k) => `<div class="keyline"><code>${esc(k)}</code><span class="muted small">from ${esc(p.env)}</span></div>`).join('') +
      p.localKeys.map((k) => `<div class="keyline"><code>${esc(k)}</code><span class="muted small">from your OpenCode login</span></div>`).join('') +
      (p.anonymous && !p.keys.length && !p.envKeys.length && !p.localKeys.length ? '<div class="muted small">No key set: using free anonymous access.</div>' : '');
    return `<div class="card" data-pid="${esc(p.id)}">
      <div class="prov-head"><h2>${esc(p.name)} <span class="muted small">${esc(p.id)}</span></h2><div>${status}</div></div>
      ${p.note ? `<p class="muted small">${esc(p.note)} ${p.signup ? `<a href="${esc(p.signup)}" target="_blank" rel="noopener">Get key ↗</a>` : ''}</p>` : ''}
      <div class="stack">
        <input data-f="baseUrl" value="${esc(p.baseUrl)}" />
        ${p.noKey ? '' : `${keys}${envKeys}<div class="keyline"><input data-f="newKey" type="password" placeholder="Paste API key (comma-separate several)" /><button class="small" data-act="addkey">Add</button></div>`}
        <label class="muted small">Models (one per line, used as <code>${esc(p.id)}/&lt;model&gt;</code>)</label>
        <textarea data-f="models" rows="4">${esc(p.models.join('\n'))}</textarea>
      </div>
      <div class="actions">
        <button class="small" data-act="save">Save</button>
        <button class="ghost small" data-act="fetch">Fetch models</button>
        <button class="ghost small" data-act="test">Test</button>
        <button class="ghost small" data-act="toggle">${p.enabled ? 'Disable' : 'Enable'}</button>
        <button class="danger small" data-act="delete">${p.custom ? 'Delete' : 'Reset'}</button>
      </div>
      <div class="result"></div>
    </div>`;
  }).join('');
}

$('#providerList').addEventListener('click', async (e) => {
  const act = e.target.dataset.act;
  if (!act) return;
  const card = e.target.closest('[data-pid]');
  const id = card.dataset.pid;
  const out = $('.result', card);
  const field = (f) => $(`[data-f="${f}"]`, card);
  const path = 'providers/' + encodeURIComponent(id);
  e.target.disabled = true;
  try {
    if (act === 'addkey') {
      const keys = field('newKey').value.split(',').map((s) => s.trim()).filter(Boolean);
      if (keys.length) refresh(await api(path, { method: 'PUT', body: { addKeys: keys } }));
      toast('Key added');
    } else if (act === 'delkey') {
      refresh(await api(path, { method: 'PUT', body: { removeKeys: [Number(e.target.dataset.i)] } }));
    } else if (act === 'save') {
      refresh(await api(path, { method: 'PUT', body: { baseUrl: field('baseUrl').value, models: field('models').value.split('\n') } }));
      toast('Saved');
    } else if (act === 'toggle') {
      const p = state.providers.find((x) => x.id === id);
      refresh(await api(path, { method: 'PUT', body: { enabled: !p.enabled } }));
    } else if (act === 'delete') {
      if (confirm('Delete/reset this provider? Its saved keys will be removed.')) refresh(await api(path, { method: 'DELETE' }));
    } else if (act === 'test') {
      out.textContent = 'Testing…';
      const r = await api(path + '/test', { method: 'POST' });
      out.innerHTML = r.ok
        ? `<span class="pill ok">OK</span> ${esc(r.model)} replied "${esc(r.reply)}" in ${r.ms} ms`
        : `<span class="pill bad">Failed</span> ${esc(r.model || '')} ${esc(r.error)}`;
    } else if (act === 'fetch') {
      out.textContent = 'Fetching…';
      const r = await api(path + '/fetch-models', { method: 'POST' });
      if (!r.ok) { out.innerHTML = `<span class="pill bad">Failed</span> ${esc(r.error)}`; return; }
      const current = new Set(field('models').value.split('\n').map((s) => s.trim()));
      const hasFree = r.models.some((m) => m.free !== undefined);
      out.innerHTML = `<div class="muted small">${r.models.length} models. Tick the ones to use, then Save.${hasFree ? ' <label class="inline"><input type="checkbox" data-freeonly checked/> free only</label>' : ''}</div>
        <div class="fetched">${r.models.map((m) => `<label data-free="${m.free === true}"><input type="checkbox" value="${esc(m.id)}" ${current.has(m.id) ? 'checked' : ''}/> ${esc(m.id)}${m.free ? ' <span class="pill ok">free</span>' : ''}</label>`).join('')}</div>`;
      const apply = () => {
        const freeOnly = $('[data-freeonly]', out)?.checked;
        out.querySelectorAll('.fetched label').forEach((l) => (l.style.display = freeOnly && l.dataset.free !== 'true' ? 'none' : ''));
      };
      out.onchange = (ev) => {
        if (ev.target.matches('[data-freeonly]')) return apply();
        const picked = [...out.querySelectorAll('.fetched input:checked')].map((i) => i.value);
        const manual = field('models').value.split('\n').map((s) => s.trim()).filter((s) => s && !r.models.some((m) => m.id === s));
        field('models').value = [...manual, ...picked].join('\n');
      };
      apply();
    }
  } catch (err) {
    out.innerHTML = `<span class="pill bad">Error</span> ${esc(err.message)}`;
  } finally {
    e.target.disabled = false;
  }
});

$('#customForm').onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    refresh(await api('providers', { method: 'POST', body: { id: f.get('id'), name: f.get('name'), baseUrl: f.get('baseUrl'), noKey: !!f.get('noKey') } }));
    e.target.reset();
    toast('Provider added');
  } catch (err) { toast(err.message); }
};

// ---------- Combos ----------
function allModelIds() {
  const ids = [];
  for (const p of state.providers) for (const m of p.models) ids.push(`${p.id}/${m}`);
  return ids;
}

function renderCombos() {
  const ready = new Set(state.providers.filter((p) => p.ready).map((p) => p.id));
  $('#modelOptions').innerHTML = allModelIds().map((m) => `<option value="${esc(m)}">`).join('');
  $('#comboList').innerHTML = Object.entries(state.combos).map(([name, targets]) => {
    const live = targets.filter((t) => ready.has(t.split('/')[0])).length;
    return `<div class="card" data-combo="${esc(name)}">
      <div class="prov-head"><h2><code>${esc(name)}</code></h2><span class="pill ${live ? 'ok' : 'warn'}">${live}/${targets.length} usable</span></div>
      <textarea rows="${Math.max(3, targets.length)}">${esc(targets.join('\n'))}</textarea>
      <div class="actions"><button class="small" data-cact="save">Save</button><button class="danger small" data-cact="delete">Delete</button></div>
    </div>`;
  }).join('');
}

$('#comboList').addEventListener('click', async (e) => {
  const act = e.target.dataset.cact;
  if (!act) return;
  const card = e.target.closest('[data-combo]');
  const name = card.dataset.combo;
  try {
    if (act === 'save') {
      refresh(await api('combos/' + encodeURIComponent(name), { method: 'PUT', body: { targets: $('textarea', card).value.split('\n') } }));
      toast('Combo saved');
    } else if (confirm(`Delete combo "${name}"?`)) {
      refresh(await api('combos/' + encodeURIComponent(name), { method: 'DELETE' }));
    }
  } catch (err) { toast(err.message); }
});

$('#comboForm').onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    refresh(await api('combos/' + encodeURIComponent(f.get('name').trim()), { method: 'PUT', body: { targets: f.get('targets').split('\n') } }));
    e.target.reset();
    toast('Combo created');
  } catch (err) { toast(err.message); }
};

// ---------- Playground ----------
function renderPlaygroundModels() {
  const sel = $('#pgModel');
  const prev = sel.value;
  const ready = new Set(state.providers.filter((p) => p.ready).map((p) => p.id));
  const opts = [
    ...Object.keys(state.combos).map((c) => `<option value="${esc(c)}">${esc(c)} (combo)</option>`),
    ...allModelIds().filter((m) => ready.has(m.split('/')[0])).map((m) => `<option>${esc(m)}</option>`),
  ];
  sel.innerHTML = opts.join('');
  if (prev) sel.value = prev;
}

$('#pgSend').onclick = async () => {
  const btn = $('#pgSend');
  const out = $('#pgOut');
  const meta = $('#pgMeta');
  const stream = $('#pgStream').checked;
  const messages = [];
  if ($('#pgSystem').value.trim()) messages.push({ role: 'system', content: $('#pgSystem').value });
  messages.push({ role: 'user', content: $('#pgPrompt').value });
  out.textContent = '';
  meta.textContent = 'Waiting…';
  btn.disabled = true;
  const t0 = performance.now();
  try {
    const r = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer ' + (state.apiKeys[0] || '') },
      body: JSON.stringify({ model: $('#pgModel').value, messages, stream }),
    });
    const via = () => `via ${r.headers.get('x-router-provider') || '?'}/${r.headers.get('x-router-model') || '?'}`;
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      meta.textContent = `HTTP ${r.status}`;
      out.textContent = j.error?.message || r.statusText;
      if (j.error?.attempts) out.textContent += '\n\nAttempts:\n' + j.error.attempts.map((a) => `- ${a.target}: ${a.skipped || `${a.status ? 'HTTP ' + a.status : 'network error:'} ${a.error || ''}`}`).join('\n');
      return;
    }
    meta.textContent = via();
    if (!stream) {
      const j = await r.json();
      out.textContent = j.choices?.[0]?.message?.content ?? JSON.stringify(j, null, 2);
    } else {
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data:') || line.includes('[DONE]')) continue;
          try {
            const d = JSON.parse(line.slice(5)).choices?.[0]?.delta;
            out.textContent += (d?.content || '');
          } catch {}
        }
      }
    }
    meta.textContent = `${via()} · ${Math.round(performance.now() - t0)} ms`;
  } catch (err) {
    meta.textContent = 'Error';
    out.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};

// ---------- Logs ----------
async function renderLogs() {
  const { logs, totals } = await api('usage');
  const rows = Object.entries(totals);
  $('#totals').innerHTML = rows.length
    ? '<tr><th>Target</th><th>Requests</th><th>Errors</th><th>Prompt tok</th><th>Completion tok</th></tr>' +
      rows.map(([k, t]) => `<tr><td>${esc(k)}</td><td>${t.requests}</td><td>${t.errors}</td><td>${t.promptTokens}</td><td>${t.completionTokens}</td></tr>`).join('')
    : '<tr><td class="muted">No requests yet.</td></tr>';
  $('#logTable').innerHTML = logs.length
    ? '<tr><th>Time</th><th>Model</th><th>Served by</th><th>Status</th><th>ms</th><th>Tries</th><th>Error</th></tr>' +
      logs.map((l) => `<tr><td>${esc(new Date(l.time).toLocaleTimeString())}</td><td>${esc(l.model)}</td><td>${esc(l.target || '-')}</td>
        <td><span class="pill ${l.ok ? 'ok' : 'bad'}">${l.status}</span></td><td>${l.ms}</td><td>${l.attempts ?? 1}</td>
        <td class="err">${esc(l.error || '')}${l.detail ? '<br>' + l.detail.map((a) => esc(`${a.target}: ${a.skipped || a.status}`)).join('<br>') : ''}</td></tr>`).join('')
    : '<tr><td class="muted">No requests yet.</td></tr>';
}
$('#refreshLogs').onclick = renderLogs;

// ---------- Tabs ----------
$('#tabs').addEventListener('click', (e) => {
  const tab = e.target.dataset.tab;
  if (!tab) return;
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b === e.target));
  document.querySelectorAll('.tab').forEach((s) => s.classList.toggle('active', s.id === tab));
  if (tab === 'logs') renderLogs();
  if (tab === 'providers') refresh();
});

refresh().catch((err) => toast(err.message));
