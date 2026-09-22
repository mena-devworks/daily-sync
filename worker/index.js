// API worker. Deployed as dist/_worker.js on Cloudflare Pages; static UI comes from /public via ASSETS.
const ITER = 50000;               // PBKDF2 iterations (kept within Workers CPU limits)
const SESSION_DAYS = 7;
const LINK_HOURS = 48;
const enc = new TextEncoder();

const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...headers },
  });
const err = (msg, status = 400) => json({ error: msg }, status);

const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
const randomToken = (n = 32) => b64(crypto.getRandomValues(new Uint8Array(n))).replace(/[+/=]/g, (c) => ({ '+': '-', '/': '_', '=': '' }[c]));
const sha256 = async (s) => hex(await crypto.subtle.digest('SHA-256', enc.encode(s)));

async function hashPassword(password, saltB64) {
  const salt = saltB64 ? Uint8Array.from(atob(saltB64), (c) => c.charCodeAt(0)) : crypto.getRandomValues(new Uint8Array(16));
  const key = await crypto.subtle.importKey('raw', enc.encode(password), 'PBKDF2', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: ITER }, key, 256);
  return { hash: b64(bits), salt: b64(salt) };
}
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
const validEmail = (e) => typeof e === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e) && e.length < 200;
const validPassword = (p) => typeof p === 'string' && p.length >= 10 && p.length <= 200;
const isoIn = (ms) => new Date(Date.now() + ms).toISOString();

async function body(req) {
  try { return await req.json(); } catch { return {}; }
}

function sessionCookie(token, maxAge) {
  return `sid=${token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;
}
function readCookie(req, name) {
  const m = (req.headers.get('cookie') || '').match(new RegExp('(?:^|;\\s*)' + name + '=([^;]+)'));
  return m ? m[1] : null;
}

async function currentStaff(req, env) {
  const t = readCookie(req, 'sid');
  if (!t) return null;
  const row = await env.DB.prepare(
    `SELECT s.id, s.email, s.name, s.role FROM sessions x JOIN staff s ON s.id = x.user_id
     WHERE x.token_hash = ? AND x.kind = 'staff' AND x.expires_at > ? AND s.active = 1`
  ).bind(await sha256(t), new Date().toISOString()).first();
  return row || null;
}

async function tooManyFails(env, key) {
  const since = new Date(Date.now() - 15 * 60e3).toISOString().replace('T', ' ').slice(0, 19);
  const r = await env.DB.prepare('SELECT COUNT(*) n FROM login_fails WHERE key = ? AND at > ?').bind(key, since).first();
  return r.n >= 5;
}

async function newSession(env, kind, userId) {
  const token = randomToken();
  await env.DB.prepare('INSERT INTO sessions (token_hash, kind, user_id, expires_at) VALUES (?,?,?,?)')
    .bind(await sha256(token), kind, userId, isoIn(SESSION_DAYS * 864e5)).run();
  return sessionCookie(token, SESSION_DAYS * 86400);
}

async function oneTimeLink(env, origin, kind, userId, purpose) {
  const token = randomToken();
  await env.DB.prepare('DELETE FROM tokens WHERE kind = ? AND user_id = ?').bind(kind, userId).run();
  await env.DB.prepare('INSERT INTO tokens (token_hash, kind, user_id, purpose, expires_at) VALUES (?,?,?,?,?)')
    .bind(await sha256(token), kind, userId, purpose, isoIn(LINK_HOURS * 36e5)).run();
  return `${origin}/#set-password=${token}`;
}

const audit = (env, staffId, action, detail) =>
  env.DB.prepare('INSERT INTO audit (staff_id, action, detail) VALUES (?,?,?)').bind(staffId, action, detail ? JSON.stringify(detail) : null).run();

// ---------------- routes ----------------
async function api(req, env, url) {
  const p = url.pathname.replace(/\/+$/, '');
  const m = req.method;

  // CSRF guard for state-changing calls: same-origin only
  if (m !== 'GET') {
    const o = req.headers.get('origin');
    if (o && o !== url.origin) return err('bad origin', 403);
  }

  if (p === '/api/health') return json({ ok: true, time: new Date().toISOString() });

  if (p === '/api/setup' && m === 'GET') {
    const r = await env.DB.prepare("SELECT COUNT(*) n FROM staff WHERE role='owner'").first();
    return json({ needed: r.n === 0, configured: !!env.SETUP_CODE });
  }

  if (p === '/api/setup' && m === 'POST') {
    const r = await env.DB.prepare("SELECT COUNT(*) n FROM staff WHERE role='owner'").first();
    if (r.n > 0) return err('already set up', 409);
    if (!env.SETUP_CODE) return err('SETUP_CODE secret is not configured', 503);
    if (await tooManyFails(env, 'setup')) return err('too many attempts, wait 15 minutes', 429);
    const b = await body(req);
    if (!safeEqual(String(b.code || ''), env.SETUP_CODE)) {
      await env.DB.prepare('INSERT INTO login_fails (key) VALUES (?)').bind('setup').run();
      return err('wrong setup code', 403);
    }
    if (!b.name || !validEmail(b.email)) return err('name and valid email required');
    if (!validPassword(b.password)) return err('password must be at least 10 characters');
    const h = await hashPassword(b.password);
    const ins = await env.DB.prepare("INSERT INTO staff (email, name, role, pw_hash, pw_salt) VALUES (?,?,'owner',?,?)")
      .bind(b.email.trim(), String(b.name).trim(), h.hash, h.salt).run();
    await audit(env, ins.meta.last_row_id, 'owner_setup');
    return json({ ok: true }, 200, { 'set-cookie': await newSession(env, 'staff', ins.meta.last_row_id) });
  }

  if (p === '/api/login' && m === 'POST') {
    const b = await body(req);
    const email = String(b.email || '').trim().toLowerCase();
    const key = 'staff:' + email;
    if (await tooManyFails(env, key)) return err('too many attempts, wait 15 minutes', 429);
    const u = await env.DB.prepare('SELECT id, pw_hash, pw_salt FROM staff WHERE email = ? AND active = 1').bind(email).first();
    const ok = u && u.pw_hash && safeEqual((await hashPassword(String(b.password || ''), u.pw_salt)).hash, u.pw_hash);
    if (!ok) {
      await env.DB.prepare('INSERT INTO login_fails (key) VALUES (?)').bind(key).run();
      return err('wrong email or password', 401);
    }
    await env.DB.prepare('DELETE FROM login_fails WHERE key = ?').bind(key).run();
    return json({ ok: true }, 200, { 'set-cookie': await newSession(env, 'staff', u.id) });
  }

  if (p === '/api/logout' && m === 'POST') {
    const t = readCookie(req, 'sid');
    if (t) await env.DB.prepare('DELETE FROM sessions WHERE token_hash = ?').bind(await sha256(t)).run();
    return json({ ok: true }, 200, { 'set-cookie': sessionCookie('', 0) });
  }

  if (p === '/api/set-password' && m === 'POST') {
    const b = await body(req);
    if (!validPassword(b.password)) return err('password must be at least 10 characters');
    const th = await sha256(String(b.token || ''));
    const t = await env.DB.prepare('SELECT * FROM tokens WHERE token_hash = ? AND expires_at > ?').bind(th, new Date().toISOString()).first();
    if (!t) return err('link expired or invalid', 410);
    const h = await hashPassword(b.password);
    const table = t.kind === 'staff' ? 'staff' : 'subscribers';
    await env.DB.batch([
      env.DB.prepare(`UPDATE ${table} SET pw_hash = ?, pw_salt = ? WHERE id = ?`).bind(h.hash, h.salt, t.user_id),
      env.DB.prepare('DELETE FROM tokens WHERE token_hash = ?').bind(th),
      env.DB.prepare('DELETE FROM sessions WHERE kind = ? AND user_id = ?').bind(t.kind, t.user_id),
    ]);
    return json({ ok: true, kind: t.kind });
  }

  // ---- everything below needs a staff session ----
  const me = await currentStaff(req, env);
  if (!me) return err('not signed in', 401);
  const owner = me.role === 'owner';

  if (p === '/api/me') return json(me);

  if (p === '/api/overview') {
    const today = new Date().toISOString().slice(0, 10);
    const [subs, active, appsToday, autoToday, lastRun] = await Promise.all([
      env.DB.prepare('SELECT COUNT(*) n FROM subscribers').first(),
      env.DB.prepare('SELECT COUNT(*) n FROM subscribers WHERE locked = 0 AND sub_end >= ?').bind(today).first(),
      env.DB.prepare('SELECT COUNT(*) n FROM applications WHERE substr(created_at,1,10) = ?').bind(today).first(),
      env.DB.prepare("SELECT COUNT(*) n FROM applications WHERE method='email' AND substr(created_at,1,10) = ?").bind(today).first(),
      env.DB.prepare('SELECT place, started_at, finished_at, status FROM runs ORDER BY id DESC LIMIT 1').first(),
    ]);
    return json({ subscribers: subs.n, active: active.n, applicationsToday: appsToday.n, autoToday: autoToday.n, lastRun, storage: !!env.FILES });
  }

  if (p === '/api/staff' && m === 'GET') {
    if (!owner) return err('owner only', 403);
    const r = await env.DB.prepare('SELECT id, email, name, role, active, created_at, pw_hash IS NOT NULL AS has_password FROM staff ORDER BY id').all();
    return json(r.results);
  }

  if (p === '/api/staff' && m === 'POST') {
    if (!owner) return err('owner only', 403);
    const b = await body(req);
    if (!b.name || !validEmail(b.email)) return err('name and valid email required');
    const exists = await env.DB.prepare('SELECT id FROM staff WHERE email = ?').bind(b.email.trim()).first();
    if (exists) return err('this email already exists', 409);
    const ins = await env.DB.prepare("INSERT INTO staff (email, name, role) VALUES (?,?,'admin')").bind(b.email.trim(), String(b.name).trim()).run();
    const link = await oneTimeLink(env, url.origin, 'staff', ins.meta.last_row_id, 'invite');
    await audit(env, me.id, 'admin_add', { email: b.email });
    return json({ ok: true, link });
  }

  let mm = p.match(/^\/api\/staff\/(\d+)\/(toggle|reset)$/);
  if (mm && m === 'POST') {
    if (!owner) return err('owner only', 403);
    const id = Number(mm[1]);
    const u = await env.DB.prepare('SELECT id, role, active FROM staff WHERE id = ?').bind(id).first();
    if (!u) return err('not found', 404);
    if (u.role === 'owner') return err('cannot change the owner here', 400);
    if (mm[2] === 'toggle') {
      await env.DB.prepare('UPDATE staff SET active = ? WHERE id = ?').bind(u.active ? 0 : 1, id).run();
      if (u.active) await env.DB.prepare("DELETE FROM sessions WHERE kind='staff' AND user_id = ?").bind(id).run();
      await audit(env, me.id, 'admin_toggle', { id, active: !u.active });
      return json({ ok: true, active: !u.active });
    }
    const link = await oneTimeLink(env, url.origin, 'staff', id, 'reset');
    await audit(env, me.id, 'admin_reset', { id });
    return json({ ok: true, link });
  }

  if (p === '/api/settings' && m === 'GET') {
    if (!owner) return err('owner only', 403);
    const r = await env.DB.prepare('SELECT key, value FROM settings').all();
    return json(Object.fromEntries(r.results.map((x) => [x.key, x.value])));
  }

  if (p === '/api/settings' && m === 'PUT') {
    if (!owner) return err('owner only', 403);
    const b = await body(req);
    const stmts = Object.entries(b).filter(([k]) => /^[a-z0-9_.]{1,64}$/.test(k))
      .map(([k, v]) => env.DB.prepare('INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value').bind(k, String(v)));
    if (stmts.length) await env.DB.batch(stmts);
    await audit(env, me.id, 'settings_update', Object.keys(b));
    return json({ ok: true });
  }

  return err('not found', 404);
}

// Housekeeping: expired sessions / links / old login failures
async function cleanup(env) {
  const now = new Date().toISOString();
  await env.DB.batch([
    env.DB.prepare('DELETE FROM sessions WHERE expires_at < ?').bind(now),
    env.DB.prepare('DELETE FROM tokens WHERE expires_at < ?').bind(now),
    env.DB.prepare("DELETE FROM login_fails WHERE at < datetime('now','-1 day')"),
  ]);
}

// Runs as Cloudflare Pages advanced-mode worker (dist/_worker.js)
export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (url.pathname.startsWith('/api/')) {
      if (Math.random() < 0.02) ctx.waitUntil(cleanup(env).catch(() => {}));
      try {
        return await api(req, env, url);
      } catch (e) {
        console.error(e);
        return err('server error', 500);
      }
    }
    const res = await env.ASSETS.fetch(req);
    const h = new Headers(res.headers);
    h.set('x-robots-tag', 'noindex, nofollow');
    h.set('x-frame-options', 'DENY');
    h.set('referrer-policy', 'no-referrer');
    return new Response(res.body, { status: res.status, headers: h });
  },
};
