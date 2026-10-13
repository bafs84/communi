import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import ts from 'typescript';

let counter = 0;
async function loadAccess(fetcher, registered = false) {
  const storage = new Map([['communi-ckw-guest-csrf', 'stale'], ['communi-ckw-guest-expiry', '{}']]);
  globalThis.sessionStorage = { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  globalThis.window = new EventTarget();
  window.location = { origin: 'https://ckw.communi.ch', pathname: '/', hostname: 'ckw.communi.ch', search: '' };
  window.__COMMUNI_CONFIG__ = registered ? { auth0Domain: 'auth.invalid', auth0ClientId: 'test', auth0Audience: 'test' } : {};
  globalThis.fetch = fetcher;
  globalThis.__reported = [];
  const tokenOptions = [];
  window.auth0 = { createAuth0Client: async () => ({
    isAuthenticated: async () => true,
    getTokenSilently: async options => { tokenOptions.push(options); return `token-${tokenOptions.length}`; },
    getUser: async () => ({}),
  }) };
  if (registered) storage.clear();
  let source = await readFile(new URL('../src/access.ts', import.meta.url), 'utf8');
  source = source.replace('import { reportError, reportHttpFailure } from "./observability";', 'const reportError = (...args) => globalThis.__reported.push(args); const reportHttpFailure = reportError;');
  source = source.replace('import.meta.env.VITE_API_BASE', 'undefined');
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}#${counter++}`);
  return { ...module, tokenOptions };
}

function guest(resumed = false) {
  return Response.json({ csrfToken: 'current', idleExpiresAt: '2099-01-01', hardExpiresAt: '2099-01-02', resumed });
}

test('validates cached guest state and retries billing once after server restart without losing files', async () => {
  const calls = [];
  let billing = 0;
  const access = await loadAccess(async (url, init) => {
    calls.push({ url, init });
    return url.endsWith('/guest/session') ? guest() : new Response('', { status: ++billing === 1 ? 401 : 200 });
  });
  const body = new FormData(); body.append('csv_file', new Blob(['synthetic CSV']), 'synthetic.csv');
  assert.equal((await access.apiFetch('/api/vzev/invoices', { method: 'POST', body })).status, 200);
  assert.equal(calls.length, 4);
  assert.ok(calls[0].url.endsWith('/guest/session'));
  assert.equal(calls[1].init.body, body);
  assert.equal(calls[3].init.body, body);
  assert.equal(calls[3].init.headers.get('X-CSRF-Token'), 'current');
});

test('never replays billing on 5xx or network failure', async () => {
  for (const failure of [503, 'network']) {
    let uploads = 0;
    const access = await loadAccess(async url => {
      if (url.endsWith('/guest/session')) return guest();
      uploads++;
      if (failure === 'network') throw new TypeError('Network failure');
      return new Response('', { status: failure });
    });
    if (failure === 'network') await assert.rejects(access.apiFetch('/api/vzev/invoices', { method: 'POST' }));
    else assert.equal((await access.apiFetch('/api/vzev/invoices', { method: 'POST' })).status, 503);
    assert.equal(uploads, 1);
  }
});

test('does not replay a stateful mutation after the guest project disappeared', async () => {
  let writes = 0;
  const access = await loadAccess(async url => {
    if (url.endsWith('/guest/session')) return guest();
    writes++; return new Response('', { status: 401 });
  });
  const response = await access.apiFetch('/api/projects/old/files', { method: 'POST' });
  assert.equal(response.status, 401); assert.equal(writes, 1);
  assert.match((await response.json()).detail, /Gastsitzung/);
});

test('refreshes registered tokens per request and forces refresh on one 401', async () => {
  let requests = 0;
  const access = await loadAccess(async () => new Response('', { status: ++requests === 1 ? 401 : 200 }), true);
  await access.apiFetch('/api/projects');
  await access.apiFetch('/api/projects');
  assert.equal(access.tokenOptions.length, 4);
  assert.equal(access.tokenOptions[2].cacheMode, 'off');
});

test('does not send credentials to an external download URL', async () => {
  const access = await loadAccess(async () => guest());
  await assert.rejects(access.apiFetch('https://external.invalid/private'), /API-Adresse/);
});
