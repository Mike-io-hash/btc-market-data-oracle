import 'dotenv/config';
import WebSocket from 'ws';
import { finalizeEvent, getPublicKey, nip04 } from 'nostr-tools';

const BASE = process.env.ORACLE_BASE_URL || 'https://oracle.satsgate.org';
const NWC_URL = process.env.NWC_URL;
const API_KEY_ENV = process.env.ORACLE_API_KEY || process.env.API_KEY || '';

const args = new Set(process.argv.slice(2));
const DO_TOPUP = args.has('--topup') || (!args.has('--snapshot') && !args.has('--no-topup'));
const DO_SNAPSHOT = args.has('--snapshot') || (!args.has('--topup') && !args.has('--no-snapshot'));

const PLAN = (() => {
  const i = process.argv.indexOf('--plan');
  if (i !== -1 && process.argv[i + 1]) return String(process.argv[i + 1]).trim();
  return 'trial';
})();

function hexToBytes(hex) {
  if (!hex || typeof hex !== 'string') return new Uint8Array();
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
  // Derived pubkey is unused, but validates the secret shape.
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
          const res = JSON.parse(decrypted);
          resolve(res);
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

async function topupTrial() {
  if (!NWC_URL) throw new Error('Missing NWC_URL (see clients/node/.env.example)');

  const parsed = parseNWCUrl(NWC_URL);

  // sanity check before spending sats
  const info = await callNip47(parsed, 'get_info', {});
  if (info?.error) throw new Error(`NWC get_info error: ${JSON.stringify(info.error)}`);

  // 1) Request topup challenge
  const r1 = await fetch(`${BASE}/v1/topup/${PLAN}`, API_KEY_ENV ? { headers: { 'X-Api-Key': API_KEY_ENV } } : undefined);
  const j1 = await r1.json();

  if (r1.status !== 402) {
    throw new Error(`Expected 402 from /v1/topup/${PLAN}, got ${r1.status}: ${JSON.stringify(j1)}`);
  }

  const { invoice, macaroon } = j1;
  console.log('invoice:', String(invoice).slice(0, 16) + '…');

  // 2) Pay invoice via NWC
  const pay = await callNip47(parsed, 'pay_invoice', { invoice });
  if (pay?.error) throw new Error(`NWC pay_invoice error: ${JSON.stringify(pay.error)}`);

  const preimage = extractPreimage(pay);
  if (!preimage) throw new Error(`No preimage in pay_invoice result: ${JSON.stringify(pay)}`);

  console.log('paid. got preimage (redacted).');

  // 3) Finalize topup
  const auth = `L402 ${macaroon}:${preimage}`;
  const r2 = await fetch(`${BASE}/v1/topup/${PLAN}`, {
    headers: {
      Authorization: auth,
      ...(API_KEY_ENV ? { 'X-Api-Key': API_KEY_ENV } : {}),
    },
  });
  const j2 = await r2.json();

  if (!r2.ok) throw new Error(`Finalize failed ${r2.status}: ${JSON.stringify(j2)}`);

  return j2;
}

async function callJSON(path, apiKey) {
  const r = await fetch(`${BASE}${path}`, {
    headers: {
      'X-Api-Key': apiKey,
      'X-Request-Id': `demo-${Date.now()}`,
    },
  });
  const j = await r.json();
  if (!r.ok) throw new Error(`GET ${path} failed ${r.status}: ${JSON.stringify(j)}`);
  return j;
}

async function main() {
  let apiKey = API_KEY_ENV;

  if (DO_TOPUP) {
    const res = await topupTrial();
    console.log(JSON.stringify(res, null, 2));
    if (res.api_key) {
      apiKey = res.api_key;
      console.log('\nAPI_KEY:', apiKey);
      console.log('(Save it somewhere safe; it will not be shown again.)');
    }
  }

  if (!apiKey && DO_SNAPSHOT) {
    throw new Error('Missing ORACLE_API_KEY. Run with --topup first or set ORACLE_API_KEY in .env.');
  }

  if (DO_SNAPSHOT) {
    const snap = await callJSON('/v1/snapshot/btc', apiKey);
    console.log('\nSNAPSHOT OK. verifications_spent=', snap.verifications_spent, 'balance=', snap.verification_balance);

    const bal = await callJSON('/v1/balance', apiKey);
    console.log('BALANCE:', bal);

    const by = await callJSON('/v1/usage/by-endpoint?since_hours=24', apiKey);
    console.log('BY_ENDPOINT:', by.endpoints?.slice(0, 10));

    const rec = await callJSON('/v1/recommendation/topup?target_days=3&buffer_hours=12', apiKey);
    console.log('RECOMMENDATION:', rec.recommendation);
  }
}

main().catch((e) => {
  console.error('ERROR:', e?.message || e);
  process.exit(1);
});
