import 'dotenv/config';
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import os from 'os';

import WebSocket from 'ws';
import { finalizeEvent, getPublicKey, nip04 } from 'nostr-tools';

// --- Config ---

const BASE = (process.env.ORACLE_BASE_URL || 'https://oracle.satsgate.org').replace(/\/$/, '');
const NWC_URL = (process.env.NWC_URL || '').trim();

// Stored by this script after the first successful top-up.
let API_KEY = (process.env.ORACLE_API_KEY || '').trim();

// If not set, we use trial for first run.
const DEFAULT_PLAN = (process.env.ORACLE_DEFAULT_PLAN || 'trial').trim();

// Autopilot controls
const SNAPSHOT_INTERVAL_SECONDS = Number(process.env.SNAPSHOT_INTERVAL_SECONDS || '2');
const REASONING_INTERVAL_SECONDS = Number(process.env.REASONING_INTERVAL_SECONDS || '300');

// Guardrails (safe defaults; operator can change)
const AUTO_TOPUP_ENV = (process.env.AUTO_TOPUP || '').trim();
const AUTO_TOPUP = AUTO_TOPUP_ENV
  ? !['0', 'false', 'off', 'no'].includes(AUTO_TOPUP_ENV.toLowerCase())
  : Boolean(NWC_URL); // default: ON when NWC_URL exists

const MAX_SINGLE_TOPUP_SATS = Number(process.env.MAX_SINGLE_TOPUP_SATS || '1000000');
const MAX_TOPUP_SATS_PER_DAY = Number(process.env.MAX_TOPUP_SATS_PER_DAY || '10000000');
const TOPUP_COOLDOWN_SECONDS = Number(process.env.TOPUP_COOLDOWN_SECONDS || '300');

// Recommendation settings
const LOOKBACK_HOURS = Number(process.env.LOOKBACK_HOURS || '24');
const TARGET_DAYS = Number(process.env.TARGET_DAYS || '3');
const BUFFER_HOURS = Number(process.env.BUFFER_HOURS || '12');

// Paths
const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname);
const ENV_PATH = process.env.ENV_PATH ? String(process.env.ENV_PATH) : path.join(SCRIPT_DIR, '.env');
const STATE_PATH = process.env.STATE_PATH
  ? String(process.env.STATE_PATH)
  : path.join(SCRIPT_DIR, '.oracle_autopilot_state.json');

// Modes
const args = new Set(process.argv.slice(2));
const ONCE = args.has('--once');
const NO_SNAPSHOT = args.has('--no-snapshot');
const NO_REASONING = args.has('--no-reasoning');

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function nowUtcDay() {
  return new Date().toISOString().slice(0, 10);
}

function loadState() {
  try {
    const raw = fs.readFileSync(STATE_PATH, 'utf8');
    const s = JSON.parse(raw);
    return {
      day_utc: s.day_utc || nowUtcDay(),
      topup_sats_today: Number(s.topup_sats_today || 0),
      last_topup_ts: Number(s.last_topup_ts || 0),
    };
  } catch {
    return { day_utc: nowUtcDay(), topup_sats_today: 0, last_topup_ts: 0 };
  }
}

function saveState(s) {
  try {
    fs.writeFileSync(STATE_PATH, JSON.stringify(s, null, 2));
  } catch {
    // best-effort
  }
}

function normalizeDay(state) {
  const d = nowUtcDay();
  if (state.day_utc !== d) {
    state.day_utc = d;
    state.topup_sats_today = 0;
  }
}

function canTopup({ state, sats }) {
  normalizeDay(state);
  const now = Math.floor(Date.now() / 1000);

  if (!AUTO_TOPUP) return { ok: false, reason: 'AUTO_TOPUP is disabled' };
  if (!NWC_URL) return { ok: false, reason: 'NWC_URL is missing' };

  if (Number.isFinite(TOPUP_COOLDOWN_SECONDS) && TOPUP_COOLDOWN_SECONDS > 0) {
    if (state.last_topup_ts && now - state.last_topup_ts < TOPUP_COOLDOWN_SECONDS) {
      return { ok: false, reason: `cooldown (${TOPUP_COOLDOWN_SECONDS}s) active` };
    }
  }

  if (Number.isFinite(MAX_SINGLE_TOPUP_SATS) && sats > MAX_SINGLE_TOPUP_SATS) {
    return { ok: false, reason: `plan costs ${sats} sats > MAX_SINGLE_TOPUP_SATS=${MAX_SINGLE_TOPUP_SATS}` };
  }

  if (Number.isFinite(MAX_TOPUP_SATS_PER_DAY) && state.topup_sats_today + sats > MAX_TOPUP_SATS_PER_DAY) {
    return {
      ok: false,
      reason: `daily cap: ${state.topup_sats_today + sats} > MAX_TOPUP_SATS_PER_DAY=${MAX_TOPUP_SATS_PER_DAY}`,
    };
  }

  return { ok: true };
}

function redact(s) {
  if (!s) return s;
  return String(s).slice(0, 12) + '…';
}

function updateEnvVar(filePath, key, value) {
  const line = `${key}=${value}`;
  let text = '';
  try {
    text = fs.readFileSync(filePath, 'utf8');
  } catch {
    text = '';
  }

  const lines = text.split(/\r?\n/);
  let found = false;
  const out = lines.map((l) => {
    if (l.startsWith(`${key}=`)) {
      found = true;
      return line;
    }
    return l;
  });
  if (!found) out.push(line);

  fs.writeFileSync(filePath, out.join(os.EOL));
}

// --- NWC (NIP-47) ---

function hexToBytes(hex) {
  const clean = hex.startsWith('0x') ? hex.slice(2) : hex;
  if (clean.length % 2 !== 0) throw new Error('invalid hex length');
  const out = new Uint8Array(clean.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function parseNWCUrl(nwcUrl) {
  const u = new URL(nwcUrl);
  const walletPubkey = (u.hostname || u.pathname.replace(/^\/+/, '')).trim();
  const relays = u.searchParams.getAll('relay');
  const secret = (u.searchParams.get('secret') || '').trim();
  if (!walletPubkey) throw new Error('NWC_URL missing wallet pubkey');
  if (!relays.length) throw new Error('NWC_URL missing relay');
  if (!secret) throw new Error('NWC_URL missing secret');
  return { walletPubkey, relay: relays[0], secret };
}

async function callNip47(parsed, method, params, timeoutMs = 12000) {
  const { relay: relayUrl, walletPubkey, secret } = parsed;
  const skBytes = hexToBytes(secret);
  // validate secret shape
  getPublicKey(skBytes);

  const ws = new WebSocket(relayUrl);
  const created_at = Math.floor(Date.now() / 1000);

  const plaintext = JSON.stringify({ method, params });
  const content = await nip04.encrypt(secret, walletPubkey, plaintext);

  const reqEvent = finalizeEvent(
    {
      kind: 23194,
      created_at,
      tags: [['p', walletPubkey]],
      content,
    },
    skBytes
  );

  const subId = 'sub-' + reqEvent.id.slice(0, 8);
  const filter = {
    kinds: [23195],
    authors: [walletPubkey],
    '#e': [reqEvent.id],
    limit: 1,
  };

  return await new Promise((resolve, reject) => {
    const t = setTimeout(() => {
      try { ws.close(); } catch {}
      reject(new Error(`timeout waiting for NIP-47 response to ${method}`));
    }, timeoutMs);

    ws.on('open', () => {
      ws.send(JSON.stringify(['REQ', subId, filter]));
      ws.send(JSON.stringify(['EVENT', reqEvent]));
    });

    ws.on('error', () => {
      clearTimeout(t);
      reject(new Error('websocket error'));
    });

    ws.on('message', async (data) => {
      try {
        const msg = JSON.parse(String(data));
        const [type, sid, payload] = msg;
        if (sid !== subId) return;

        if (type === 'EVENT') {
          clearTimeout(t);
          try { ws.close(); } catch {}
          const decrypted = await nip04.decrypt(secret, walletPubkey, payload.content);
          resolve(JSON.parse(decrypted));
        }
      } catch (e) {
        clearTimeout(t);
        try { ws.close(); } catch {}
        reject(e);
      }
    });
  });
}

function extractPreimage(res) {
  return res?.result?.preimage || res?.preimage || null;
}

// --- Oracle calls ---

async function getJSON(pathname, { apiKey } = {}) {
  const headers = {};
  if (apiKey) headers['X-Api-Key'] = apiKey;
  headers['X-Request-Id'] = `autopilot-${crypto.randomUUID()}`;

  const r = await fetch(`${BASE}${pathname}`, { headers });
  const j = await r.json().catch(() => ({ raw: 'non-json response' }));
  return { status: r.status, ok: r.ok, json: j };
}

async function topupPlan(planId, { apiKey }) {
  if (!NWC_URL) throw new Error('Missing NWC_URL');
  const parsed = parseNWCUrl(NWC_URL);

  // sanity check (fast)
  const info = await callNip47(parsed, 'get_info', {});
  if (info?.error) throw new Error(`NWC get_info error: ${JSON.stringify(info.error)}`);

  // 1) challenge
  const r1 = await fetch(`${BASE}/v1/topup/${planId}`, {
    headers: apiKey ? { 'X-Api-Key': apiKey } : undefined,
  });
  const j1 = await r1.json();
  if (r1.status !== 402) throw new Error(`Expected 402 from /v1/topup/${planId}, got ${r1.status}: ${JSON.stringify(j1)}`);

  const { invoice, macaroon, plan } = j1;

  // 2) pay
  const pay = await callNip47(parsed, 'pay_invoice', { invoice });
  if (pay?.error) throw new Error(`NWC pay_invoice error: ${JSON.stringify(pay.error)}`);

  const preimage = extractPreimage(pay);
  if (!preimage) throw new Error(`No preimage in pay_invoice result: ${JSON.stringify(pay)}`);

  // 3) finalize
  const auth = `L402 ${macaroon}:${preimage}`;
  const r2 = await fetch(`${BASE}/v1/topup/${planId}`, {
    headers: {
      Authorization: auth,
      ...(apiKey ? { 'X-Api-Key': apiKey } : {}),
    },
  });
  const j2 = await r2.json();
  if (!r2.ok) throw new Error(`Finalize failed ${r2.status}: ${JSON.stringify(j2)}`);

  return { plan, result: j2 };
}

async function ensureApiKey() {
  if (API_KEY) return API_KEY;

  if (!NWC_URL) {
    throw new Error('Missing ORACLE_API_KEY and NWC_URL. Set NWC_URL to auto-provision an API key.');
  }

  console.log('No API key found. Running first top-up to provision one…');
  const { result } = await topupPlan(DEFAULT_PLAN, { apiKey: null });
  if (!result.api_key) {
    throw new Error('Top-up succeeded but no api_key returned (unexpected).');
  }

  API_KEY = result.api_key;

  // Persist locally
  try {
    updateEnvVar(ENV_PATH, 'ORACLE_API_KEY', API_KEY);
    console.log('Saved ORACLE_API_KEY into', ENV_PATH);
  } catch {
    // best-effort
  }

  return API_KEY;
}

async function doSnapshot(apiKey) {
  const r = await getJSON('/v1/snapshot/btc', { apiKey });
  if (r.status === 402 && r.json?.error === 'insufficient_balance') {
    return { insufficient: true, response: r };
  }
  if (!r.ok) throw new Error(`snapshot failed ${r.status}: ${JSON.stringify(r.json)}`);

  const s = r.json?.snapshot || {};
  const price = s.price?.price;
  const funding = s.perps?.funding_8h;
  const oi = s.perps?.open_interest;

  console.log(
    JSON.stringify(
      {
        t: new Date().toISOString(),
        kind: 'snapshot',
        price,
        quote: r.json.quote,
        funding_8h: funding,
        open_interest: oi,
        verifications_spent: r.json.verifications_spent,
        verification_balance: r.json.verification_balance,
        staleness_ms: {
          binance: s.price?.staleness_ms,
          deribit: s.perps?.staleness_ms,
        },
      },
      null,
      0
    )
  );

  return { insufficient: false, response: r };
}

async function doReasoning(apiKey) {
  const bal = await getJSON('/v1/balance', { apiKey });
  const fc = await getJSON(`/v1/usage/forecast?lookback_hours=${LOOKBACK_HOURS}`, { apiKey });
  const rec = await getJSON(
    `/v1/recommendation/topup?lookback_hours=${LOOKBACK_HOURS}&target_days=${TARGET_DAYS}&buffer_hours=${BUFFER_HOURS}`,
    { apiKey }
  );

  const by = await getJSON('/v1/usage/by-endpoint?since_hours=24', { apiKey });

  console.log(
    JSON.stringify(
      {
        t: new Date().toISOString(),
        kind: 'reasoning',
        balance: bal.json,
        forecast: fc.json?.forecast,
        recommendation: rec.json?.recommendation,
        by_endpoint_top: (by.json?.endpoints || []).slice(0, 5),
      },
      null,
      2
    )
  );

  return { bal, fc, rec, by };
}

async function maybeAutoTopup(apiKey, state, recommendation) {
  if (!recommendation) return;

  const planId = recommendation.plan_id;
  const qty = Number(recommendation.quantity || 1);
  const plan = recommendation.plan || {};
  const sats = Number(recommendation.sats_total || plan.price_sats || 0);
  const priceEach = Number(plan.price_sats || 0);

  // We enforce caps per purchase, not per recommendation.
  if (!priceEach) {
    console.log('autopilot: missing plan price in recommendation; skipping');
    return;
  }

  console.log(`autopilot: recommended ${planId} x${qty} (~${sats} sats total)`);

  for (let i = 0; i < qty; i++) {
    const ok = canTopup({ state, sats: priceEach });
    if (!ok.ok) {
      console.log('autopilot: topup blocked:', ok.reason);
      return;
    }

    console.log(`autopilot: buying ${planId} (${priceEach} sats)…`);

    const res = await topupPlan(planId, { apiKey });

    normalizeDay(state);
    state.topup_sats_today += priceEach;
    state.last_topup_ts = Math.floor(Date.now() / 1000);
    saveState(state);

    // update key if returned (shouldn't, but safe)
    if (res.result?.api_key && !API_KEY) {
      API_KEY = res.result.api_key;
      try { updateEnvVar(ENV_PATH, 'ORACLE_API_KEY', API_KEY); } catch {}
    }

    console.log(
      'autopilot: topup ok. verifications_added=',
      res.result?.verifications_added,
      'balance=',
      res.result?.verification_balance,
      'spent_today_sats=',
      state.topup_sats_today
    );

    // small delay to avoid hammering
    await sleep(1500);
  }
}

async function main() {
  console.log('Oracle Autopilot starting…');
  console.log('BASE=', BASE);
  console.log('AUTO_TOPUP=', AUTO_TOPUP, 'cooldown_s=', TOPUP_COOLDOWN_SECONDS);
  console.log('caps: max_single=', MAX_SINGLE_TOPUP_SATS, 'max_per_day=', MAX_TOPUP_SATS_PER_DAY);
  console.log('intervals: snapshot_s=', SNAPSHOT_INTERVAL_SECONDS, 'reasoning_s=', REASONING_INTERVAL_SECONDS);

  const state = loadState();
  normalizeDay(state);

  const apiKey = await ensureApiKey();

  const tickSnapshot = async () => {
    if (NO_SNAPSHOT) return;
    const out = await doSnapshot(apiKey);

    if (out.insufficient) {
      console.log('snapshot: insufficient balance');

      if (!AUTO_TOPUP) return;

      // Try a reasoning cycle + top-up, then retry snapshot once.
      const rr = await doReasoning(apiKey);
      await maybeAutoTopup(apiKey, state, rr.rec?.json?.recommendation);
      await doSnapshot(apiKey);
    }
  };

  const tickReasoning = async () => {
    if (NO_REASONING) return;
    const rr = await doReasoning(apiKey);
    await maybeAutoTopup(apiKey, state, rr.rec?.json?.recommendation);
  };

  // First run
  if (!NO_SNAPSHOT) await tickSnapshot();
  if (!NO_REASONING) await tickReasoning();

  if (ONCE) return;

  // Loops
  if (!NO_SNAPSHOT) {
    setInterval(() => {
      tickSnapshot().catch((e) => console.error('snapshot loop error:', e?.message || e));
    }, Math.max(0.5, SNAPSHOT_INTERVAL_SECONDS) * 1000);
  }

  if (!NO_REASONING) {
    setInterval(() => {
      tickReasoning().catch((e) => console.error('reasoning loop error:', e?.message || e));
    }, Math.max(5, REASONING_INTERVAL_SECONDS) * 1000);
  }

  // keep alive
  // eslint-disable-next-line no-constant-condition
  while (true) {
    await sleep(60_000);
  }
}

main().catch((e) => {
  console.error('ERROR:', e?.message || e);
  process.exit(1);
});
