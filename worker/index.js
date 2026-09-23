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

function sessionCookie(token, maxAge, name = 'sid') {
  return `${name}=${token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;
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
  return sessionCookie(token, SESSION_DAYS * 86400, kind === 'sub' ? 'ssid' : 'sid');
}

async function oneTimeLink(env, origin, kind, userId, purpose, path = '/') {
  const token = randomToken();
  await env.DB.prepare('DELETE FROM tokens WHERE kind = ? AND user_id = ?').bind(kind, userId).run();
  await env.DB.prepare('INSERT INTO tokens (token_hash, kind, user_id, purpose, expires_at) VALUES (?,?,?,?,?)')
    .bind(await sha256(token), kind, userId, purpose, isoIn(LINK_HOURS * 36e5)).run();
  return `${origin}${path}#set-password=${token}`;
}

const audit = (env, staffId, action, detail) =>
  env.DB.prepare('INSERT INTO audit (staff_id, action, detail) VALUES (?,?,?)').bind(staffId, action, detail ? JSON.stringify(detail) : null).run();

// ---------------- phase 2: catalog, crypto, subscribers ----------------
const CITIES = {
  AE: ['Abu Dhabi', 'Dubai', 'Sharjah', 'Ajman', 'Umm Al Quwain', 'Ras Al Khaimah', 'Fujairah'],
  EG: ['Cairo', 'Giza', 'Alexandria', 'Qalyubia', 'Sharqia', 'Dakahlia', 'Gharbia', 'Monufia', 'Beheira', 'Kafr El Sheikh',
    'Damietta', 'Port Said', 'Ismailia', 'Suez', 'North Sinai', 'South Sinai', 'Red Sea', 'Matrouh', 'New Valley', 'Faiyum',
    'Beni Suef', 'Minya', 'Asyut', 'Sohag', 'Qena', 'Luxor', 'Aswan'],
  SA: ['Riyadh', 'Jeddah', 'Mecca', 'Medina', 'Dammam', 'Khobar', 'Dhahran', 'Jubail', 'Al Ahsa', 'Taif', 'Tabuk', 'Abha',
    'Khamis Mushait', 'Buraidah', 'Hail', 'Yanbu', 'Jazan', 'Najran'],
};
const FIELDS = ['Accounting & Finance', 'Banking', 'Sales', 'Marketing', 'Customer Service', 'Administration & Secretarial',
  'Reception & Front Office', 'Human Resources', 'IT & Software', 'Data & Analytics', 'Civil Engineering', 'Mechanical Engineering',
  'Electrical Engineering', 'Construction & Site Management', 'Project Management', 'Healthcare & Nursing', 'Pharmacy',
  'Education & Teaching', 'Hospitality & Hotels', 'Tourism & Travel', 'Food & Beverage', 'Retail', 'Logistics & Supply Chain',
  'Procurement', 'Real Estate', 'Graphic Design & Creative', 'Legal', 'Security', 'Driving & Delivery'];
const SETTING_DEFAULTS = {
  daily_apply_limit: '20', central_daily_limit: '5', min_match_score: '60', email_cooldown_days: '14',
  run_time_cloud: '07:00', default_sub_days: '30', auto_fields_count: '5', engine_live: '0',
};
const CV_TYPES = { pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', txt: 'text/plain' };
const validDate = (d) => typeof d === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(d) && !isNaN(Date.parse(d));
const addDays = (d, n) => new Date(Date.parse(d) + n * 864e5).toISOString().slice(0, 10);
const todayStr = () => new Date().toISOString().slice(0, 10);

async function getSetting(env, key) {
  const r = await env.DB.prepare('SELECT value FROM settings WHERE key = ?').bind(key).first();
  return r ? r.value : SETTING_DEFAULTS[key];
}

// App passwords: AES-GCM, key derived (HKDF) from SETUP_CODE. Format "v1:<iv b64>:<ciphertext b64>".
// The engine (phase 3) decrypts with the same derivation. Changing SETUP_CODE means re-entering app passwords.
async function appKey(env) {
  const base = await crypto.subtle.importKey('raw', enc.encode(env.SETUP_CODE), 'HKDF', false, ['deriveKey']);
  return crypto.subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt: enc.encode('job-hunter/app-password'), info: enc.encode('v1') },
    base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
}
async function encryptSecret(env, text) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, await appKey(env), enc.encode(text));
  return `v1:${b64(iv)}:${b64(ct)}`;
}

// Validate + normalise a subscriber payload (shared by create and update)
function cleanSub(b) {
  const out = {};
  if (!b.name || String(b.name).trim().length > 120) return { error: 'name required' };
  if (!validEmail(String(b.email || '').trim())) return { error: 'valid email required' };
  out.name = String(b.name).trim();
  out.email = String(b.email).trim().toLowerCase();
  out.current_country = ['AE', 'EG', 'SA', 'OTHER'].includes(b.current_country) ? b.current_country : null;
  out.current_city = b.current_city ? String(b.current_city).trim().slice(0, 80) : null;
  if (b.sub_end !== undefined && !validDate(b.sub_end)) return { error: 'invalid subscription end date' };
  out.sub_end = b.sub_end;
  out.cities = [];
  for (const c of Array.isArray(b.cities) ? b.cities : []) {
    if (!CITIES[c.country] || !CITIES[c.country].includes(c.city)) return { error: 'unknown city: ' + c.city };
    out.cities.push([c.country, c.city, c.enabled ? 1 : 0]);
  }
  out.fields = [];
  for (const f of Array.isArray(b.fields) ? b.fields : []) {
    if (!FIELDS.includes(f.field)) return { error: 'unknown field: ' + f.field };
    out.fields.push([f.field, f.enabled ? 1 : 0]);
  }
  if (b.app_password) {
    const ap = String(b.app_password).replace(/\s+/g, '');
    if (ap.length < 8 || ap.length > 64) return { error: 'app password looks wrong' };
    out.app_password = ap;
  }
  out.clear_app_password = !!b.clear_app_password;
  return out;
}

async function saveSubLists(env, id, s) {
  const st = [
    env.DB.prepare('DELETE FROM subscriber_cities WHERE subscriber_id = ?').bind(id),
    env.DB.prepare('DELETE FROM subscriber_fields WHERE subscriber_id = ?').bind(id),
    ...s.cities.map(([co, ci, en]) => env.DB.prepare('INSERT INTO subscriber_cities (subscriber_id, country, city, enabled) VALUES (?,?,?,?)').bind(id, co, ci, en)),
    ...s.fields.map(([f, en]) => env.DB.prepare('INSERT INTO subscriber_fields (subscriber_id, field, enabled) VALUES (?,?,?)').bind(id, f, en)),
  ];
  await env.DB.batch(st);
}

async function subRoutes(req, env, url, p, m, me) {
  if (p === '/api/catalog') return json({ cities: CITIES, fields: FIELDS, settingDefaults: SETTING_DEFAULTS, storage: !!env.FILES });

  if (p === '/api/subscribers' && m === 'GET') {
    const r = await env.DB.prepare(
      `SELECT s.id, s.name, s.email, s.sub_start, s.sub_end, s.locked, s.send_mode,
        (SELECT COUNT(*) FROM subscriber_cities c WHERE c.subscriber_id = s.id AND c.enabled = 1) cities,
        (SELECT COUNT(*) FROM subscriber_fields f WHERE f.subscriber_id = s.id AND f.enabled = 1) fields,
        (SELECT COUNT(*) FROM applications a WHERE a.subscriber_id = s.id AND substr(a.created_at,1,10) = ?1) apps_today,
        (SELECT COUNT(*) FROM applications a WHERE a.subscriber_id = s.id AND a.method = 'email' AND substr(a.created_at,1,10) = ?1) auto_today,
        EXISTS(SELECT 1 FROM cv_files v WHERE v.subscriber_id = s.id) has_cv,
        EXISTS(SELECT 1 FROM reset_requests r WHERE r.subscriber_id = s.id) reset_req,
        s.pw_hash IS NOT NULL has_password
       FROM subscribers s ORDER BY s.id DESC`
    ).bind(todayStr()).all();
    return json(r.results);
  }

  if (p === '/api/subscribers' && m === 'POST') {
    const s = cleanSub(await body(req));
    if (s.error) return err(s.error);
    if (await env.DB.prepare('SELECT 1 x FROM subscribers WHERE email = ?').bind(s.email).first()) return err('a subscriber with this email already exists', 409);
    const start = todayStr();
    const end = s.sub_end || addDays(start, Number(await getSetting(env, 'default_sub_days')) || 30);
    const ap = s.app_password ? await encryptSecret(env, s.app_password) : null;
    const ins = await env.DB.prepare(
      `INSERT INTO subscribers (slug, name, email, current_country, current_city, send_mode, app_password_enc, sub_start, sub_end, lang, created_by)
       VALUES (?,?,?,?,?,?,?,?,?,?,?)`
    ).bind(randomToken(9), s.name, s.email, s.current_country, s.current_city, ap ? 'app_password' : 'central', ap, start, end, 'en', me.id).run();
    const id = ins.meta.last_row_id;
    await saveSubLists(env, id, s);
    await audit(env, me.id, 'sub_add', { id, email: s.email });
    return json({ ok: true, id });
  }

  const mm = p.match(/^\/api\/subscribers\/(\d+)(?:\/(lock|unlock|renew|cv|link))?$/);
  if (!mm) return null;
  const id = Number(mm[1]);
  const sub = await env.DB.prepare('SELECT * FROM subscribers WHERE id = ?').bind(id).first();
  if (!sub) return err('not found', 404);
  const act = mm[2];

  if (!act && m === 'GET') {
    const [c, f, cv] = await Promise.all([
      env.DB.prepare('SELECT country, city, enabled FROM subscriber_cities WHERE subscriber_id = ?').bind(id).all(),
      env.DB.prepare('SELECT field, enabled, auto_picked FROM subscriber_fields WHERE subscriber_id = ?').bind(id).all(),
      env.DB.prepare('SELECT name, size, store, uploaded_at FROM cv_files WHERE subscriber_id = ?').bind(id).first(),
    ]);
    const { app_password_enc, pw_hash, pw_salt, cv_text, ...safe } = sub;
    const rr = await env.DB.prepare('SELECT at FROM reset_requests WHERE subscriber_id = ?').bind(id).first();
    return json({ ...safe, has_password: !!pw_hash, reset_requested: rr ? rr.at : null, has_app_password: !!app_password_enc, has_cv_text: !!cv_text, cities: c.results, fields: f.results, cv });
  }

  if (!act && m === 'PUT') {
    const s = cleanSub(await body(req));
    if (s.error) return err(s.error);
    if (await env.DB.prepare('SELECT 1 x FROM subscribers WHERE email = ? AND id <> ?').bind(s.email, id).first()) return err('a subscriber with this email already exists', 409);
    // Optional App Password: staff can add/replace/remove it here; the subscriber can also add it from their dashboard (phase 4)
    let ap = s.clear_app_password ? null : sub.app_password_enc;
    if (s.app_password) ap = await encryptSecret(env, s.app_password);
    const mode = ap ? 'app_password' : 'central';
    await env.DB.prepare(
      `UPDATE subscribers SET name=?, email=?, current_country=?, current_city=?, send_mode=?, app_password_enc=?, sub_end=?, lang=?, updated_at=datetime('now') WHERE id=?`
    ).bind(s.name, s.email, s.current_country, s.current_city, mode, ap, s.sub_end || sub.sub_end, sub.lang, id).run();
    await saveSubLists(env, id, s);
    await audit(env, me.id, 'sub_edit', { id });
    return json({ ok: true });
  }

  if (!act && m === 'DELETE') {
    if (me.role !== 'owner') return err('owner only', 403);
    if (env.FILES) await env.FILES.delete(`cv/${id}`).catch(() => {});
    await env.DB.batch([
      env.DB.prepare('DELETE FROM subscriber_cities WHERE subscriber_id = ?').bind(id),
      env.DB.prepare('DELETE FROM subscriber_fields WHERE subscriber_id = ?').bind(id),
      env.DB.prepare('DELETE FROM cv_files WHERE subscriber_id = ?').bind(id),
      env.DB.prepare('DELETE FROM applications WHERE subscriber_id = ?').bind(id),
      env.DB.prepare('DELETE FROM tailored_cvs WHERE subscriber_id = ?').bind(id),
      env.DB.prepare('DELETE FROM tailored_pdfs WHERE subscriber_id = ?').bind(id),
      env.DB.prepare('DELETE FROM reset_requests WHERE subscriber_id = ?').bind(id),
      env.DB.prepare("DELETE FROM tokens WHERE kind='sub' AND user_id = ?").bind(id),
      env.DB.prepare("DELETE FROM sessions WHERE kind='sub' AND user_id = ?").bind(id),
      env.DB.prepare('DELETE FROM subscribers WHERE id = ?').bind(id),
    ]);
    await audit(env, me.id, 'sub_delete', { id, email: sub.email });
    return json({ ok: true });
  }

  if ((act === 'lock' || act === 'unlock') && m === 'POST') {
    await env.DB.prepare("UPDATE subscribers SET locked = ?, updated_at = datetime('now') WHERE id = ?").bind(act === 'lock' ? 1 : 0, id).run();
    await audit(env, me.id, 'sub_' + act, { id });
    return json({ ok: true });
  }

  if (act === 'renew' && m === 'POST') {
    const from = sub.sub_end > todayStr() ? sub.sub_end : todayStr();
    const end = addDays(from, Number(await getSetting(env, 'default_sub_days')) || 30);
    await env.DB.prepare("UPDATE subscribers SET sub_end = ?, updated_at = datetime('now') WHERE id = ?").bind(end, id).run();
    await audit(env, me.id, 'sub_renew', { id, sub_end: end });
    return json({ ok: true, sub_end: end });
  }

  // Phase 4: one-time "set your password" link for the subscriber (also used for resets)
  if (act === 'link' && m === 'POST') {
    const link = await oneTimeLink(env, url.origin, 'sub', id, sub.pw_hash ? 'reset' : 'invite', '/me');
    await env.DB.prepare('DELETE FROM reset_requests WHERE subscriber_id = ?').bind(id).run();
    await audit(env, me.id, 'sub_link', { id });
    return json({ ok: true, link, dashboard: `${url.origin}/me` });
  }

  if (act === 'cv' && m === 'POST') {
    let form;
    try { form = await req.formData(); } catch { return err('upload a file'); }
    const file = form.get('file');
    if (!file || typeof file === 'string') return err('upload a file');
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    if (!CV_TYPES[ext]) return err('CV must be PDF, DOCX or TXT');
    const max = env.FILES ? 5e6 : 7e5;
    if (file.size > max) return err(`file too large (max ${Math.round(max / 1e3)} KB)`);
    const buf = await file.arrayBuffer();
    const hash = hex(await crypto.subtle.digest('SHA-256', buf));
    const text = String(form.get('text') || '').slice(0, 200000);
    let store = 'd1', data = null;
    if (env.FILES) { await env.FILES.put(`cv/${id}`, buf, { httpMetadata: { contentType: CV_TYPES[ext] } }); store = 'r2'; }
    else { let s = ''; const u = new Uint8Array(buf); for (let i = 0; i < u.length; i += 8192) s += String.fromCharCode(...u.subarray(i, i + 8192)); data = btoa(s); }
    await env.DB.batch([
      env.DB.prepare(`INSERT INTO cv_files (subscriber_id, name, mime, size, store, data) VALUES (?,?,?,?,?,?)
        ON CONFLICT(subscriber_id) DO UPDATE SET name=excluded.name, mime=excluded.mime, size=excluded.size, store=excluded.store, data=excluded.data, uploaded_at=datetime('now')`)
        .bind(id, file.name.slice(0, 150), CV_TYPES[ext], file.size, store, data),
      env.DB.prepare("UPDATE subscribers SET cv_key = ?, cv_text = ?, cv_hash = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(`${store}:cv/${id}`, text || null, hash, id),
    ]);
    await audit(env, me.id, 'sub_cv', { id, size: file.size, store });
    return json({ ok: true, store, textChars: text.length });
  }

  if (act === 'cv' && m === 'GET') {
    const f = await env.DB.prepare('SELECT * FROM cv_files WHERE subscriber_id = ?').bind(id).first();
    if (!f) return err('no CV', 404);
    let bytes;
    if (f.store === 'r2') { const o = env.FILES && await env.FILES.get(`cv/${id}`); if (!o) return err('file missing', 404); bytes = o.body; }
    else bytes = Uint8Array.from(atob(f.data), (c) => c.charCodeAt(0));
    return new Response(bytes, { headers: { 'content-type': f.mime || 'application/octet-stream', 'cache-control': 'no-store',
      'content-disposition': `attachment; filename*=UTF-8''${encodeURIComponent(f.name)}` } });
  }

  return err('not found', 404);
}

// ---------------- phase 4: subscriber dashboard (/api/me/...) ----------------
// Separate cookie (ssid) so a staff member and a subscriber can be signed in on the same browser.
async function currentSub(req, env) {
  const t = readCookie(req, 'ssid');
  if (!t) return null;
  return await env.DB.prepare(
    `SELECT s.* FROM sessions x JOIN subscribers s ON s.id = x.user_id
     WHERE x.token_hash = ? AND x.kind = 'sub' AND x.expires_at > ?`
  ).bind(await sha256(t), new Date().toISOString()).first() || null;
}
const subReadOnly = (s) => !!s.locked || s.sub_end < todayStr();

async function meRoutes(req, env, url, p, m) {
  if (p === '/api/me/login' && m === 'POST') {
    const b = await body(req);
    const email = String(b.email || '').trim().toLowerCase(), slug = String(b.slug || '').trim();
    if (!email && !slug) return err('email required');
    const key = 'sub:' + (email || slug);
    if (await tooManyFails(env, key)) return err('too many attempts, wait 15 minutes', 429);
    const u = await env.DB.prepare(`SELECT id, pw_hash, pw_salt FROM subscribers WHERE ${email ? 'email' : 'slug'} = ? ORDER BY id DESC LIMIT 1`).bind(email || slug).first();
    const ok = u && u.pw_hash && safeEqual((await hashPassword(String(b.password || ''), u.pw_salt)).hash, u.pw_hash);
    if (!ok) {
      await env.DB.prepare('INSERT INTO login_fails (key) VALUES (?)').bind(key).run();
      return err('wrong email or password', 401);
    }
    await env.DB.prepare('DELETE FROM login_fails WHERE key = ?').bind(key).run();
    return json({ ok: true }, 200, { 'set-cookie': await newSession(env, 'sub', u.id) });
  }
  if (p === '/api/me/logout' && m === 'POST') {
    const t = readCookie(req, 'ssid');
    if (t) await env.DB.prepare('DELETE FROM sessions WHERE token_hash = ?').bind(await sha256(t)).run();
    return json({ ok: true }, 200, { 'set-cookie': sessionCookie('', 0, 'ssid') });
  }
  // Forgot password: recorded for the team (they send a new link). Same answer whether or not the slug exists.
  if (p === '/api/me/forgot' && m === 'POST') {
    const email = String((await body(req)).email || '').trim().toLowerCase();
    if (!validEmail(email)) return err('enter your email first');
    if (await tooManyFails(env, 'forgot')) return json({ ok: true });
    await env.DB.prepare('INSERT INTO login_fails (key) VALUES (?)').bind('forgot').run();
    const u = await env.DB.prepare('SELECT id FROM subscribers WHERE email = ? ORDER BY id DESC LIMIT 1').bind(email).first();
    if (u) await env.DB.prepare('INSERT INTO reset_requests (subscriber_id) VALUES (?) ON CONFLICT(subscriber_id) DO UPDATE SET at = datetime(\'now\')').bind(u.id).run();
    return json({ ok: true });
  }

  const s = await currentSub(req, env);
  if (!s) return err('not signed in', 401);
  const ro = subReadOnly(s);

  if (p === '/api/me/info' && m === 'GET') {
    const [c, f, live] = await Promise.all([
      env.DB.prepare('SELECT country, city FROM subscriber_cities WHERE subscriber_id = ? AND enabled = 1').bind(s.id).all(),
      env.DB.prepare('SELECT field, auto_picked FROM subscriber_fields WHERE subscriber_id = ? AND enabled = 1').bind(s.id).all(),
      getSetting(env, 'engine_live'),
    ]);
    return json({ name: s.name, email: s.email, slug: s.slug, sub_start: s.sub_start, sub_end: s.sub_end, locked: !!s.locked, readOnly: ro,
      lang: s.lang, send_mode: s.send_mode, has_app_password: !!s.app_password_enc, cities: c.results, fields: f.results,
      live: live === '1', runTime: await getSetting(env, 'run_time_cloud') });
  }

  if (p === '/api/me/overview' && m === 'GET') {
    const today = todayStr(), d7 = addDays(today, -6), d14 = addDays(today, -13);
    const [tod, week, match, daily, byField] = await Promise.all([
      env.DB.prepare("SELECT SUM(status='sent') e, SUM(status='manual') m FROM applications WHERE subscriber_id = ? AND substr(created_at,1,10) = ?").bind(s.id, today).first(),
      env.DB.prepare("SELECT SUM(status='sent') e, SUM(status='manual') m FROM applications WHERE subscriber_id = ? AND substr(created_at,1,10) >= ?").bind(s.id, d7).first(),
      env.DB.prepare("SELECT COUNT(*) n FROM applications WHERE subscriber_id = ? AND status IN ('sent','manual','failed')").bind(s.id).first(),
      env.DB.prepare("SELECT substr(created_at,1,10) d, SUM(status='sent') e, SUM(status='manual') m FROM applications WHERE subscriber_id = ? AND substr(created_at,1,10) >= ? GROUP BY d").bind(s.id, d14).all(),
      env.DB.prepare("SELECT field, COUNT(*) n FROM applications WHERE subscriber_id = ? AND status IN ('sent','manual') GROUP BY field ORDER BY n DESC LIMIT 8").bind(s.id).all(),
    ]);
    const n = (x) => Number(x || 0);
    return json({ todaySent: n(tod.e), todayManual: n(tod.m), weekSent: n(week.e), weekManual: n(week.m), matched: n(match.n),
      daily: daily.results, fields: byField.results });
  }

  if (p === '/api/me/jobs' && m === 'GET') {
    const days = Math.min(90, Math.max(1, Number(url.searchParams.get('days')) || 30));
    const r = await env.DB.prepare(
      `SELECT a.id, a.score, a.field, a.method, a.status, a.created_at, a.sent_at, j.title, j.company, j.city, j.country, j.url, j.source,
         EXISTS(SELECT 1 FROM tailored_pdfs t WHERE t.subscriber_id = a.subscriber_id AND t.field = a.field) has_pdf
       FROM applications a JOIN jobs j ON j.id = a.job_id
       WHERE a.subscriber_id = ? AND a.status IN ('sent','manual','failed') AND substr(a.created_at,1,10) >= ?
       ORDER BY a.created_at DESC, a.score DESC LIMIT 500`
    ).bind(s.id, addDays(todayStr(), -(days - 1))).all();
    return json(r.results);
  }

  if (p === '/api/me/cvs' && m === 'GET') {
    const r = await env.DB.prepare('SELECT field, size, updated_at FROM tailored_pdfs WHERE subscriber_id = ? ORDER BY field').bind(s.id).all();
    return json(r.results);
  }

  const cm = p.match(/^\/api\/me\/cv\/(.+)$/);
  if (cm && m === 'GET') {
    const field = decodeURIComponent(cm[1]);
    const f = await env.DB.prepare('SELECT data FROM tailored_pdfs WHERE subscriber_id = ? AND field = ?').bind(s.id, field).first();
    if (!f) return err('not found', 404);
    const name = `CV - ${s.name} - ${field}.pdf`.replace(/[\\/:*?"<>|]/g, '-');
    return new Response(Uint8Array.from(atob(f.data), (c) => c.charCodeAt(0)), { headers: { 'content-type': 'application/pdf', 'cache-control': 'no-store',
      'content-disposition': `attachment; filename*=UTF-8''${encodeURIComponent(name)}` } });
  }

  if (p === '/api/me/app-password' && m === 'PUT') {
    if (ro) return err('your subscription has ended — read only', 403);
    const b = await body(req);
    let ap = null;
    if (!b.clear) {
      const v = String(b.app_password || '').replace(/\s+/g, '');
      if (!/^[a-zA-Z]{16}$/.test(v)) return err('an App Password is 16 letters (Google shows it in 4 groups of 4)');
      ap = await encryptSecret(env, v);
    }
    await env.DB.prepare("UPDATE subscribers SET app_password_enc = ?, send_mode = ?, updated_at = datetime('now') WHERE id = ?")
      .bind(ap, ap ? 'app_password' : 'central', s.id).run();
    await audit(env, null, 'me_app_password', { id: s.id, set: !!ap });
    return json({ ok: true, has_app_password: !!ap });
  }

  if (p === '/api/me/lang' && m === 'PUT') {
    const l = (await body(req)).lang === 'ar' ? 'ar' : 'en';
    await env.DB.prepare('UPDATE subscribers SET lang = ? WHERE id = ?').bind(l, s.id).run();
    return json({ ok: true });
  }

  return err('not found', 404);
}

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

  // Owner/admin password recovery with the setup code (no email sending yet)
  if (p === '/api/recover' && m === 'POST') {
    if (!env.SETUP_CODE) return err('SETUP_CODE secret is not configured', 503);
    if (await tooManyFails(env, 'recover')) return err('too many attempts, wait 15 minutes', 429);
    const b = await body(req);
    if (!safeEqual(String(b.code || ''), env.SETUP_CODE)) {
      await env.DB.prepare('INSERT INTO login_fails (key) VALUES (?)').bind('recover').run();
      return err('wrong setup code', 403);
    }
    if (!validPassword(b.password)) return err('password must be at least 10 characters');
    const email = String(b.email || '').trim().toLowerCase();
    const u = await env.DB.prepare('SELECT id FROM staff WHERE email = ?').bind(email).first();
    if (!u) return err('no account with this email', 404);
    const h = await hashPassword(b.password);
    await env.DB.batch([
      env.DB.prepare('UPDATE staff SET pw_hash = ?, pw_salt = ?, active = 1 WHERE id = ?').bind(h.hash, h.salt, u.id),
      env.DB.prepare("DELETE FROM sessions WHERE kind='staff' AND user_id = ?").bind(u.id),
      env.DB.prepare('DELETE FROM login_fails WHERE key = ?').bind('staff:' + email),
      env.DB.prepare('DELETE FROM login_fails WHERE key = ?').bind('recover'),
    ]);
    await audit(env, u.id, 'password_recover', { email });
    return json({ ok: true });
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
      env.DB.prepare('DELETE FROM reset_requests WHERE subscriber_id = ? AND ? = \'sub\'').bind(t.user_id, t.kind),
    ]);
    const slug = t.kind === 'sub' ? (await env.DB.prepare('SELECT slug FROM subscribers WHERE id = ?').bind(t.user_id).first())?.slug : undefined;
    return json({ ok: true, kind: t.kind, slug });
  }

  if (p.startsWith('/api/me/')) return meRoutes(req, env, url, p, m);

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
    const d14 = new Date(Date.now() - 13 * 864e5).toISOString().slice(0, 10), d7 = new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10);
    const [daily, fields, jobs7, cities, st] = await Promise.all([
      env.DB.prepare("SELECT substr(created_at,1,10) d, SUM(method='email' AND status='sent') e, SUM(status='manual') m FROM applications WHERE substr(created_at,1,10) >= ? GROUP BY d").bind(d14).all(),
      env.DB.prepare('SELECT field, COUNT(*) n FROM jobs WHERE substr(fetched_at,1,10) >= ? GROUP BY field ORDER BY n DESC LIMIT 8').bind(d7).all(),
      env.DB.prepare('SELECT COUNT(*) n, SUM(apply_email IS NOT NULL) e FROM jobs WHERE substr(fetched_at,1,10) >= ?').bind(d7).first(),
      env.DB.prepare('SELECT COUNT(DISTINCT c.country || c.city) n, COUNT(DISTINCT f.field) f FROM subscribers s LEFT JOIN subscriber_cities c ON c.subscriber_id = s.id AND c.enabled = 1 LEFT JOIN subscriber_fields f ON f.subscriber_id = s.id AND f.enabled = 1 WHERE s.locked = 0 AND s.sub_end >= ?').bind(today).first(),
      env.DB.prepare("SELECT key, value FROM settings WHERE key IN ('run_time_cloud','engine_live')").all(),
    ]);
    const S = Object.fromEntries(st.results.map((x) => [x.key, x.value]));
    return json({ subscribers: subs.n, active: active.n, applicationsToday: appsToday.n, autoToday: autoToday.n, lastRun, storage: !!env.FILES,
      daily: daily.results, fields: fields.results, jobs7: jobs7.n || 0, jobs7Email: jobs7.e || 0, cities: cities.n || 0, fieldsActive: cities.f || 0,
      runTime: S.run_time_cloud || SETTING_DEFAULTS.run_time_cloud, live: (S.engine_live || '0') === '1' });
  }

  // phase 3: engine runs. The GitHub workflow checks every 15 min and starts when run_requested is newer than the last run.
  if (p === '/api/run-now' && m === 'POST') {
    const busy = await env.DB.prepare("SELECT id FROM runs WHERE status = 'running' AND started_at > datetime('now','-3 hours') LIMIT 1").first();
    if (busy) return err('a run is already in progress', 409);
    const now = new Date().toISOString().replace('T', ' ').slice(0, 19);
    await env.DB.prepare("INSERT INTO settings (key, value) VALUES ('run_requested', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value").bind(now).run();
    await audit(env, me.id, 'run_now', now);
    return json({ ok: true, requested_at: now });
  }
  if (p === '/api/runs' && m === 'GET') {
    const r = await env.DB.prepare('SELECT id, place, started_at, finished_at, status, stats_json FROM runs ORDER BY id DESC LIMIT 10').all();
    const req = await env.DB.prepare("SELECT value FROM settings WHERE key = 'run_requested'").first();
    return json({ requested: req ? req.value : null, runs: r.results.map((x) => {
      let st = {}; try { st = JSON.parse(x.stats_json || '{}'); } catch {}
      if (!owner) delete st.preview;
      return { ...x, stats_json: undefined, stats: st };
    }) });
  }

  const sr = await subRoutes(req, env, url, p, m, me);
  if (sr) return sr;

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
    return json({ ...SETTING_DEFAULTS, ...Object.fromEntries(r.results.map((x) => [x.key, x.value])) });
  }

  if (p === '/api/settings' && m === 'PUT') {
    if (!owner) return err('owner only', 403);
    const b = await body(req);
    const stmts = Object.entries(b).filter(([k, v]) => k in SETTING_DEFAULTS && String(v).length <= 200)
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
