/* Binance TH portfolio dashboard — no build step, no dependencies. */
'use strict';

const state = {
  ccy: localStorage.getItem('ccy') || 'thb',
  portfolio: null,
  trades: null,
  transfers: null,
  realised: null,
  history: null,
  histDays: 90,
  tab: 'overview',
  hideDust: false,
};

/* ── formatting ──────────────────────────────────────────────────── */

const CCY_LABEL = { thb: 'THB', usdt: 'USDT' };
const CCY_SIGN = { thb: '฿', usdt: '$' };

function fmt(value, opts = {}) {
  const { digits = 2, sign = false, compact = false } = opts;
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const abs = Math.abs(value);
  let text;
  if (compact && abs >= 1e6) text = (value / 1e6).toFixed(2) + 'M';
  else if (compact && abs >= 1e4) text = (value / 1e3).toFixed(1) + 'k';
  else text = value.toLocaleString('en-US', {
    minimumFractionDigits: digits, maximumFractionDigits: digits });
  if (sign && value > 0) text = '+' + text;
  return text;
}

function fmtMoney(value, opts = {}) {
  const digits = opts.digits ?? (state.ccy === 'thb' ? 2 : 2);
  return CCY_SIGN[state.ccy] + fmt(value, { ...opts, digits });
}

/** Quantities need more precision than money, but not trailing noise. */
function fmtQty(qty) {
  if (qty === 0) return '0';
  const abs = Math.abs(qty);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.001 ? 6 : 8;
  return qty.toLocaleString('en-US', { maximumFractionDigits: digits });
}

function fmtPrice(value) {
  if (!value) return '—';
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 6 : 8;
  return CCY_SIGN[state.ccy] + fmt(value, { digits });
}

function fmtPct(value, opts = {}) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return fmt(value, { digits: 2, sign: true, ...opts }) + '%';
}

function fmtTime(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit' })
       + ' ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

function fmtDate(ms) {
  if (!ms) return '—';
  return new Date(ms).toLocaleDateString('en-GB',
    { day: '2-digit', month: 'short', year: 'numeric' });
}

function pick(pair) { return pair ? (pair[state.ccy] ?? 0) : 0; }

function signClass(value) {
  if (!value) return 'neutral';
  return value > 0 ? 'up' : 'down';
}

/* Stable per-asset colour so a coin keeps its identity across every chart.
   The majors get the shades people already associate with them; everything
   else is dealt from a palette picked to stay distinguishable side by side. */
const ASSET_COLORS = {
  BTC: '#f7931a', ETH: '#8a92f5', USDT: '#26a17b', USDC: '#2775ca',
  BNB: '#f0b90b', SOL: '#14f195', XRP: '#7dd3fc', ADA: '#3468d1',
  DOGE: '#c9a227', THB: '#4f9cf9', TRX: '#e8443a', MATIC: '#8247e5',
  DOT: '#e6007a', AVAX: '#e84142', LINK: '#2a5ada', NEAR: '#9aa0a6',
};
const PALETTE = [
  '#5b8ff9', '#61ddaa', '#f6bd16', '#e8684a', '#9270ca', '#6dc8ec',
  '#ff9d4d', '#269a99', '#ff99c3', '#78d3f8', '#b6a2de', '#7ec2f3',
];

function assetColor(asset) {
  if (ASSET_COLORS[asset]) return ASSET_COLORS[asset];
  let hash = 0;
  for (let i = 0; i < asset.length; i++) hash = (hash * 31 + asset.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(hash) % PALETTE.length];
}

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ── data loading ────────────────────────────────────────────────── */

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).error || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

async function loadPortfolio(force = false) {
  state.portfolio = await api('/api/portfolio' + (force ? '?force=true' : ''));
  renderKpis();
  renderHoldings();
  renderAllocation();
  renderFunding();
  renderWarnings();
  renderFooter();
}

async function loadTrades() {
  state.trades = await api('/api/trades?limit=2000');
  renderTrades();
  renderRecent();
}

async function loadTransfers() {
  state.transfers = await api('/api/transfers');
  renderTransfers();
}

async function loadRealised() {
  state.realised = await api('/api/realised?limit=2000');
  renderRealised();
}

async function loadHistory(refresh = false) {
  state.history = await api('/api/history' + (refresh ? '?refresh=true' : ''));
  renderEquityChart();
  renderPnlChart();
  renderHistoryTable();
}

/* ── KPIs ────────────────────────────────────────────────────────── */

function renderKpis() {
  const t = state.portfolio?.totals;
  if (!t) return;
  const other = state.ccy === 'thb' ? 'usdt' : 'thb';

  $('#kpi-equity').textContent = fmtMoney(pick(t.equity));
  $('#kpi-equity-alt').textContent =
    `${CCY_SIGN[other]}${fmt(t.equity[other])} ${CCY_LABEL[other]}`;

  const unrealised = pick(t.unrealised);
  const un = $('#kpi-unrealised');
  un.textContent = fmtMoney(unrealised, { sign: true });
  un.className = 'value ' + signClass(unrealised);
  $('#kpi-unrealised-pct').textContent = fmtPct(t.unrealised_pct[state.ccy])
    + ' on cost';

  const realised = pick(t.realised);
  const re = $('#kpi-realised');
  re.textContent = fmtMoney(realised, { sign: true });
  re.className = 'value ' + signClass(realised);

  const total = pick(t.total_pnl);
  const tot = $('#kpi-total');
  tot.textContent = fmtMoney(total, { sign: true });
  tot.className = 'value ' + signClass(total);
  $('#kpi-total-hint').textContent =
    `${fmtPct(t.total_pnl_pct[state.ccy])} on ${fmtMoney(pick(t.net_invested))} net in`;

  $('#kpi-cost').textContent = fmtMoney(pick(t.cost));
  const excluded = pick(t.excluded);
  $('#kpi-cost-hint').textContent = excluded
    ? `excludes ${fmtMoney(excluded)} with no basis `
      + `(${(t.unknown_assets || []).join(', ')})`
    : 'what you paid for what you hold';

  const fees = pick(t.fees);
  $('#kpi-fees').textContent = fmtMoney(fees);
  const equity = pick(t.equity);
  $('#kpi-fees-hint').textContent = equity
    ? `${fmtPct((fees / equity) * 100, { sign: false })} of portfolio value`
    : 'commission on every fill';

  $('#fx-chip').textContent = t.fx_rate
    ? `1 USDT = ฿${fmt(t.fx_rate, { digits: 4 })}` : 'USDT rate unavailable';

  $('#sum-deposits').textContent = fmtMoney(pick(t.deposits));
  $('#sum-withdrawals').textContent = fmtMoney(pick(t.withdrawals));
}

function renderWarnings() {
  const banner = $('#banner');
  const warnings = state.portfolio?.warnings || [];
  const connError = state.portfolio?.meta?.connection_error;
  if (!warnings.length && !connError) { banner.classList.add('hidden'); return; }

  banner.className = 'banner' + (connError ? ' error' : '');
  banner.innerHTML = '';
  if (connError) {
    banner.appendChild(el('strong', null, 'Not connected to Binance TH'));
    banner.appendChild(el('div', null, connError));
  }
  if (warnings.length) {
    banner.appendChild(el('strong', null,
      warnings.length + ' thing' + (warnings.length > 1 ? 's' : '') + ' worth knowing'));
    const list = el('ul');
    warnings.slice(0, 8).forEach((w) => list.appendChild(el('li', null, w.message)));
    banner.appendChild(list);
  }
  banner.classList.remove('hidden');
}

function renderFooter() {
  const meta = state.portfolio?.meta;
  if (!meta) return;
  const c = meta.counts || {};
  $('#foot-meta').textContent =
    `${c.trades || 0} fills · ${c.deposits || 0} deposits · ${c.withdrawals || 0} withdrawals · `
    + `${meta.cost_basis_method.toUpperCase()} cost basis · FX ${meta.fx_mode} · `
    + `last sync ${meta.last_sync ? fmtTime(meta.last_sync) : 'never'}`;

  const line = $('#conn-line');
  if (meta.connection_error) {
    line.className = 'sub err';
    line.innerHTML = '<span class="dot"></span>offline — showing stored data';
  } else {
    line.className = 'sub';
    line.innerHTML = `<span class="dot"></span>${meta.base_url.replace('https://', '')}`
      + ` · live prices`;
  }
}

/* ── holdings ────────────────────────────────────────────────────── */

function renderHoldings() {
  const body = $('#tbl-holdings tbody');
  body.innerHTML = '';
  let positions = state.portfolio?.positions || [];
  if (state.hideDust) {
    positions = positions.filter((p) => Math.abs(pick(p.value)) >= 1);
  }
  if (!positions.length) {
    body.appendChild(emptyRow(10, 'Nothing here yet — hit Sync to pull your history.'));
    return;
  }

  for (const p of positions) {
    const row = el('tr');

    const nameCell = el('td');
    const wrap = el('div', 'asset-cell');
    const dot = el('span', 'dot');
    dot.style.background = assetColor(p.asset);
    wrap.append(dot, el('span', null, p.asset));
    if (p.is_cash) wrap.appendChild(el('span', 'pill muted', 'cash'));
    if (p.cost_assumed) {
      const tag = el('span', 'pill warn', 'est. cost');
      tag.title = 'Part of this holding has no purchase on record; '
        + 'its cost was estimated from the market price.';
      wrap.appendChild(tag);
    }
    nameCell.appendChild(wrap);
    row.appendChild(nameCell);

    const qty = el('td', 'r num');
    qty.appendChild(document.createTextNode(fmtQty(p.qty)));
    if (p.locked > 0) qty.appendChild(el('span', 'sub-cell', `${fmtQty(p.locked)} locked`));
    row.appendChild(qty);

    row.appendChild(cell(p.is_cash ? '—' : fmtPrice(pick(p.avg_cost))));
    row.appendChild(cell(fmtPrice(pick(p.price))));
    row.appendChild(cell(p.is_cash ? '—' : fmtMoney(pick(p.cost))));
    row.appendChild(cell(fmtMoney(pick(p.value))));

    const unrealised = pick(p.unrealised);
    // A holding with no costed part has no knowable profit — say so rather
    // than printing a zero that looks measured.
    const uncosted = p.basis_unknown && !pick(p.cost);
    row.appendChild(cell(
      p.is_cash || uncosted ? (uncosted ? 'no basis' : '—')
                            : fmtMoney(unrealised, { sign: true }),
      p.is_cash || uncosted ? 'muted' : signClass(unrealised)));
    row.appendChild(cell(p.is_cash ? '—' : fmtPct(p.roi[state.ccy]),
                         p.is_cash ? '' : signClass(p.roi[state.ccy])));

    const realised = pick(p.realised);
    row.appendChild(cell(realised ? fmtMoney(realised, { sign: true }) : '—',
                         realised ? signClass(realised) : ''));
    row.appendChild(cell(fmt(p.weight, { digits: 1 }) + '%'));
    body.appendChild(row);
  }
}

function cell(text, cls) {
  const td = el('td', 'r num' + (cls ? ' ' + cls : ''), text);
  return td;
}

function emptyRow(span, message) {
  const row = el('tr');
  const td = el('td', 'empty', message);
  td.colSpan = span;
  row.appendChild(td);
  return row;
}

/* ── trades ──────────────────────────────────────────────────────── */

function filteredTrades() {
  let rows = state.trades?.rows || [];
  const asset = $('#f-trade-asset').value.trim().toUpperCase();
  const side = $('#f-trade-side').value;
  const quote = $('#f-trade-quote').value;
  if (asset) rows = rows.filter((r) => r.base === asset || r.quote === asset);
  if (side) rows = rows.filter((r) => r.side === side);
  if (quote) rows = rows.filter((r) => r.quote === quote);
  return rows;
}

function renderTrades() {
  const body = $('#tbl-trades tbody');
  body.innerHTML = '';
  const rows = filteredTrades();
  if (!rows.length) {
    body.appendChild(emptyRow(8, 'No fills match this filter.'));
    $('#trades-foot').textContent = '';
    return;
  }

  for (const t of rows.slice(0, 800)) {
    const row = el('tr');
    row.appendChild(el('td', null, fmtTime(t.time)));

    const pair = el('td');
    pair.appendChild(el('span', null, t.base + '/' + t.quote));
    pair.appendChild(el('span', t.quote === 'THB' ? 'pill thb' : 'pill usdt',
                        t.quote === 'THB' ? 'baht' : t.quote.toLowerCase()));
    row.appendChild(pair);

    const side = el('td');
    side.appendChild(el('span', 'pill ' + t.side.toLowerCase(), t.side));
    row.appendChild(side);

    row.appendChild(cell(fmt(t.price, { digits: t.price >= 1000 ? 2 : 6 })));
    row.appendChild(cell(fmtQty(t.qty)));

    const traded = el('td', 'r num');
    traded.appendChild(document.createTextNode(fmt(t.quote_qty, { digits: 2 })));
    traded.appendChild(el('span', 'sub-cell', t.quote));
    row.appendChild(traded);

    row.appendChild(cell(fmtMoney(pick(t.value))));
    const fee = el('td', 'r num');
    fee.appendChild(document.createTextNode(t.fee ? fmtQty(t.fee) : '—'));
    if (t.fee) fee.appendChild(el('span', 'sub-cell', t.fee_asset));
    row.appendChild(fee);

    body.appendChild(row);
  }
  const shown = Math.min(rows.length, 800);
  $('#trades-foot').textContent =
    `showing ${shown} of ${rows.length} fills` +
    (rows.length > 800 ? ' — narrow the filter to see the rest' : '');
}

function renderRecent() {
  const body = $('#tbl-recent tbody');
  body.innerHTML = '';
  const trades = (state.trades?.rows || []).slice(0, 12);
  if (!trades.length) {
    body.appendChild(emptyRow(4, 'No activity yet.'));
    return;
  }
  for (const t of trades) {
    const row = el('tr');
    row.appendChild(el('td', null, fmtDate(t.time)));
    const what = el('td');
    what.appendChild(el('span', 'pill ' + t.side.toLowerCase(), t.side));
    what.appendChild(document.createTextNode(' ' + t.base));
    row.appendChild(what);
    row.appendChild(cell(fmtQty(t.qty)));
    row.appendChild(cell(fmtMoney(pick(t.value))));
    body.appendChild(row);
  }
}

/* ── transfers ───────────────────────────────────────────────────── */

function renderTransfers() {
  const body = $('#tbl-transfers tbody');
  body.innerHTML = '';
  let rows = state.transfers?.rows || [];
  const kind = $('#f-transfer-kind').value;
  const asset = $('#f-transfer-asset').value.trim().toUpperCase();
  if (kind) rows = rows.filter((r) => r.kind === kind);
  if (asset) rows = rows.filter((r) => r.asset === asset);

  if (!rows.length) {
    body.appendChild(emptyRow(8, 'No deposits or withdrawals on record.'));
    return;
  }

  for (const t of rows) {
    const row = el('tr');
    row.appendChild(el('td', null, fmtTime(t.time)));

    const kindCell = el('td');
    kindCell.appendChild(el('span', 'pill ' + (t.kind === 'DEPOSIT' ? 'dep' : 'wd'),
                            t.kind === 'DEPOSIT' ? 'Deposit' : 'Withdrawal'));
    row.appendChild(kindCell);

    const assetCell = el('td');
    const wrap = el('div', 'asset-cell');
    const dot = el('span', 'dot');
    dot.style.background = assetColor(t.asset);
    wrap.append(dot, el('span', null, t.asset));
    if (t.is_fiat) wrap.appendChild(el('span', 'pill thb', 'fiat'));
    assetCell.appendChild(wrap);
    row.appendChild(assetCell);

    row.appendChild(cell(fmtQty(t.amount)));
    row.appendChild(cell(t.fee ? fmtQty(t.fee) : '—'));
    row.appendChild(cell(fmtMoney(pick(t.value_now))));

    const status = el('td');
    status.appendChild(el('span',
      'pill ' + (t.status === 'COMPLETED' ? 'muted' : 'warn'), t.status));
    row.appendChild(status);

    const ref = el('td');
    ref.appendChild(el('span', null, t.network || (t.is_fiat ? 'bank' : '—')));
    if (t.tx_id) {
      ref.appendChild(el('span', 'sub-cell',
        t.tx_id.length > 22 ? t.tx_id.slice(0, 10) + '…' + t.tx_id.slice(-8) : t.tx_id));
    }
    row.appendChild(ref);
    body.appendChild(row);
  }
}

/* ── realised P&L ────────────────────────────────────────────────── */

function renderRealised() {
  const body = $('#tbl-realised tbody');
  body.innerHTML = '';
  const rows = state.realised?.rows || [];
  const counted = rows.filter((r) => r.counts);
  const total = counted.reduce((sum, r) => sum + pick(r.pnl), 0);
  const wins = counted.filter((r) => pick(r.pnl) > 0).length;

  $('#realised-note').textContent = counted.length
    ? `${counted.length} closed lots · ${fmtMoney(total, { sign: true })} total · `
      + `${Math.round(wins / counted.length * 100)}% profitable`
    : 'Nothing closed yet.';

  if (!rows.length) {
    body.appendChild(emptyRow(9, 'No closed positions yet.'));
    return;
  }

  for (const r of rows) {
    const row = el('tr');
    row.appendChild(el('td', null, fmtTime(r.time)));

    const assetCell = el('td');
    const wrap = el('div', 'asset-cell');
    const dot = el('span', 'dot');
    dot.style.background = assetColor(r.asset);
    wrap.append(dot, el('span', null, r.asset));
    assetCell.appendChild(wrap);
    row.appendChild(assetCell);

    row.appendChild(cell(fmtQty(r.qty)));
    row.appendChild(cell(fmtMoney(pick(r.cost))));
    row.appendChild(cell(fmtMoney(pick(r.proceeds))));

    if (r.counts) {
      row.appendChild(cell(fmtMoney(pick(r.pnl), { sign: true }), signClass(pick(r.pnl))));
      row.appendChild(cell(fmtPct(r.roi[state.ccy]), signClass(r.roi[state.ccy])));
    } else {
      row.appendChild(cell('—'));
      row.appendChild(cell('—'));
    }

    row.appendChild(cell(r.holding_days >= 1
      ? Math.round(r.holding_days) + 'd'
      : Math.round(r.holding_days * 24) + 'h'));

    const type = el('td');
    const label = { sell: 'Sold', 'transfer-out': 'Withdrawn', fee: 'Fee',
                    funding: 'Funded a buy' }[r.reason] || r.reason;
    type.appendChild(el('span', 'pill ' + (r.counts ? 'muted' : 'warn'), label));
    if (r.assumed) {
      const tag = el('span', 'pill warn', 'est.');
      tag.title = 'Cost basis estimated — no matching purchase in the synced history.';
      type.appendChild(tag);
    }
    row.appendChild(type);
    body.appendChild(row);
  }
}

/* ── history table ───────────────────────────────────────────────── */

function renderHistoryTable() {
  const body = $('#tbl-history tbody');
  body.innerHTML = '';
  const rows = (state.history?.rows || []).slice().reverse();
  if (!rows.length) {
    body.appendChild(emptyRow(6, 'No history yet — sync first, then rebuild.'));
    return;
  }
  for (const r of rows.slice(0, 400)) {
    const row = el('tr');
    row.appendChild(el('td', null, r.day));
    row.appendChild(cell(fmtMoney(pick(r.equity))));
    row.appendChild(cell(fmtMoney(pick(r.cost))));
    row.appendChild(cell(fmtMoney(pick(r.unrealised), { sign: true }),
                         signClass(pick(r.unrealised))));
    row.appendChild(cell(fmtMoney(pick(r.realised), { sign: true }),
                         signClass(pick(r.realised))));
    row.appendChild(cell(fmtMoney(pick(r.net_deposit))));
    body.appendChild(row);
  }
}

/* ── charts (hand-rolled canvas, so the app stays dependency-free) ── */

function prepCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  // The intended CSS height is captured once: setting canvas.height for the
  // device-pixel backing store overwrites the attribute, so reading it back on
  // a later redraw would double the size every single time.
  if (!canvas.dataset.baseHeight) {
    canvas.dataset.baseHeight = canvas.getAttribute('height') || '240';
  }
  const height = parseInt(canvas.dataset.baseHeight, 10) || 240;
  const width = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  canvas.width = Math.max(1, Math.round(width * ratio));
  canvas.height = Math.max(1, Math.round(height * ratio));
  canvas.style.height = height + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function historySlice() {
  const rows = state.history?.rows || [];
  if (!state.histDays) return rows;
  return rows.slice(-state.histDays);
}

function drawLineChart(canvas, series, opts = {}) {
  const { ctx, width, height } = prepCanvas(canvas);
  const padL = 62, padR = 12, padT = 12, padB = 26;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  const all = series.flatMap((s) => s.values).filter(Number.isFinite);
  if (!all.length || plotW <= 0) {
    ctx.fillStyle = cssVar('--text-faint');
    ctx.font = '12px ' + cssVar('--sans');
    ctx.textAlign = 'center';
    ctx.fillText('No data for this range yet', width / 2, height / 2);
    return;
  }

  let min = Math.min(...all, opts.baseline ?? Infinity);
  let max = Math.max(...all, opts.baseline ?? -Infinity);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08;
  max += span * 0.08;

  const n = Math.max(...series.map((s) => s.values.length));
  const x = (i) => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (v) => padT + plotH - ((v - min) / (max - min)) * plotH;

  // grid + y labels
  ctx.strokeStyle = cssVar('--border-soft');
  ctx.fillStyle = cssVar('--text-faint');
  ctx.font = '10px ' + cssVar('--mono');
  ctx.lineWidth = 1;
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const value = min + ((max - min) * i) / 4;
    const yy = Math.round(y(value)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(padL, yy);
    ctx.lineTo(width - padR, yy);
    ctx.stroke();
    ctx.fillText(fmt(value, { digits: 0, compact: true }), padL - 8, yy + 3);
  }

  if (opts.baseline !== undefined && Number.isFinite(opts.baseline)) {
    ctx.strokeStyle = cssVar('--text-faint');
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(padL, y(opts.baseline));
    ctx.lineTo(width - padR, y(opts.baseline));
    ctx.stroke();
    ctx.setLineDash([]);
  }

  for (const s of series) {
    if (!s.values.length) continue;
    if (s.fill) {
      const gradient = ctx.createLinearGradient(0, padT, 0, padT + plotH);
      gradient.addColorStop(0, s.fill);
      gradient.addColorStop(1, 'transparent');
      ctx.beginPath();
      ctx.moveTo(x(0), y(s.values[0]));
      s.values.forEach((v, i) => ctx.lineTo(x(i), y(v)));
      ctx.lineTo(x(s.values.length - 1), padT + plotH);
      ctx.lineTo(x(0), padT + plotH);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();
    }
    ctx.beginPath();
    s.values.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width || 2;
    if (s.dash) ctx.setLineDash(s.dash);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // x labels
  const labels = opts.labels || [];
  if (labels.length) {
    ctx.fillStyle = cssVar('--text-faint');
    ctx.font = '10px ' + cssVar('--mono');
    ctx.textAlign = 'center';
    const step = Math.max(1, Math.floor(labels.length / 6));
    for (let i = 0; i < labels.length; i += step) {
      ctx.fillText(labels[i].slice(5), x(i), height - 8);
    }
  }
}

function renderEquityChart() {
  const rows = historySlice();
  drawLineChart($('#chart-equity'), [
    { values: rows.map((r) => pick(r.net_deposit)), color: cssVar('--text-faint'),
      width: 1.5, dash: [4, 4] },
    { values: rows.map((r) => pick(r.cost)), color: cssVar('--neutral'), width: 1.5 },
    { values: rows.map((r) => pick(r.equity)), color: cssVar('--accent'), width: 2.2,
      fill: 'rgba(240,185,11,0.14)' },
  ], { labels: rows.map((r) => r.day) });
}

function renderPnlChart() {
  const rows = historySlice();
  drawLineChart($('#chart-pnl'), [
    { values: rows.map((r) => pick(r.unrealised)), color: cssVar('--accent'), width: 2 },
    { values: rows.map((r) => pick(r.realised)), color: cssVar('--up'), width: 2 },
  ], { labels: rows.map((r) => r.day), baseline: 0 });
}

function renderAllocation() {
  const canvas = $('#chart-alloc');
  const list = $('#alloc-list');
  list.innerHTML = '';

  const positions = (state.portfolio?.positions || [])
    .filter((p) => pick(p.value) > 0);
  const total = positions.reduce((sum, p) => sum + pick(p.value), 0);

  const { ctx, width, height } = prepCanvas(canvas);
  if (!total) {
    ctx.fillStyle = cssVar('--text-faint');
    ctx.font = '12px ' + cssVar('--sans');
    ctx.textAlign = 'center';
    ctx.fillText('Nothing to allocate', width / 2, height / 2);
    return;
  }

  const cx = width / 2, cy = height / 2;
  const outer = Math.min(cx, cy) - 4, inner = outer * 0.62;
  let angle = -Math.PI / 2;

  for (const p of positions) {
    const share = pick(p.value) / total;
    const sweep = share * Math.PI * 2;
    ctx.beginPath();
    ctx.arc(cx, cy, outer, angle, angle + sweep);
    ctx.arc(cx, cy, inner, angle + sweep, angle, true);
    ctx.closePath();
    ctx.fillStyle = assetColor(p.asset);
    ctx.fill();
    ctx.strokeStyle = cssVar('--bg-raised');
    ctx.lineWidth = 2;
    ctx.stroke();
    angle += sweep;

    const item = el('li');
    const dot = el('span', 'dot');
    dot.style.background = assetColor(p.asset);
    item.append(dot, el('span', 'name', p.asset),
                el('span', 'pct', fmt(share * 100, { digits: 1 }) + '%'));
    list.appendChild(item);
  }

  ctx.fillStyle = cssVar('--text');
  ctx.textAlign = 'center';
  ctx.font = '600 15px ' + cssVar('--mono');
  ctx.fillText(fmt(total, { digits: 0, compact: true }), cx, cy + 2);
  ctx.fillStyle = cssVar('--text-faint');
  ctx.font = '10px ' + cssVar('--sans');
  ctx.fillText(CCY_LABEL[state.ccy], cx, cy + 16);
}

/**
 * Funding split: for each coin, how much of its cost basis came in through
 * baht pairs versus tether pairs. This is the question the whole tracker is
 * built around, so it gets its own chart.
 */
function renderFunding() {
  const canvas = $('#chart-funding');
  const { ctx, width, height } = prepCanvas(canvas);
  const trades = state.trades?.rows || [];

  const byAsset = new Map();
  for (const t of trades) {
    if (t.side !== 'BUY') continue;
    const entry = byAsset.get(t.base) || { thb: 0, usdt: 0 };
    const value = pick(t.value);
    if (t.quote === 'THB') entry.thb += value;
    else entry.usdt += value;
    byAsset.set(t.base, entry);
  }

  const rows = [...byAsset.entries()]
    .map(([asset, v]) => ({ asset, ...v, total: v.thb + v.usdt }))
    .filter((r) => r.total > 0)
    .sort((a, b) => b.total - a.total)
    .slice(0, 8);

  if (!rows.length) {
    ctx.fillStyle = cssVar('--text-faint');
    ctx.font = '12px ' + cssVar('--sans');
    ctx.textAlign = 'center';
    ctx.fillText('Sync your trades to see the baht / tether split',
                 width / 2, height / 2);
    return;
  }

  const padL = 58, padR = 12, padT = 8, padB = 22;
  const plotW = width - padL - padR;
  const barH = Math.min(22, (height - padT - padB) / rows.length - 6);
  const max = Math.max(...rows.map((r) => r.total));

  rows.forEach((r, i) => {
    const y = padT + i * ((height - padT - padB) / rows.length);
    const thbW = (r.thb / max) * plotW;
    const usdtW = (r.usdt / max) * plotW;

    ctx.fillStyle = cssVar('--text-dim');
    ctx.font = '11px ' + cssVar('--mono');
    ctx.textAlign = 'right';
    ctx.fillText(r.asset, padL - 8, y + barH * 0.72);

    ctx.fillStyle = cssVar('--thb');
    ctx.fillRect(padL, y, thbW, barH);
    ctx.fillStyle = cssVar('--usdt');
    ctx.fillRect(padL + thbW, y, usdtW, barH);
  });

  ctx.textAlign = 'left';
  ctx.font = '10px ' + cssVar('--sans');
  ctx.fillStyle = cssVar('--thb');
  ctx.fillRect(padL, height - 13, 9, 3);
  ctx.fillText('bought with THB', padL + 14, height - 8);
  ctx.fillStyle = cssVar('--usdt');
  ctx.fillRect(padL + 120, height - 13, 9, 3);
  ctx.fillText('bought with USDT', padL + 134, height - 8);
}

function redrawCharts() {
  renderAllocation();
  renderFunding();
  renderEquityChart();
  renderPnlChart();
}

/* ── live updates ────────────────────────────────────────────────── */

function connectStream() {
  const source = new EventSource('/api/events');

  source.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (_) { return; }

    if (payload.type === 'snapshot') {
      state.portfolio = payload.portfolio;
      renderKpis(); renderHoldings(); renderAllocation();
      renderWarnings(); renderFooter();
    } else if (payload.type === 'tick' && state.portfolio) {
      // Patch prices in place so the table does not flicker on every tick.
      state.portfolio.totals = payload.totals;
      for (const p of state.portfolio.positions) {
        const live = payload.prices[p.asset];
        if (!live) continue;
        p.price = { thb: live.thb, usdt: live.usdt };
        p.value = { thb: live.value_thb, usdt: live.value_usdt };
        p.unrealised = { thb: live.unrealised_thb, usdt: live.unrealised_usdt };
      }
      renderKpis(); renderHoldings(); renderAllocation();
    }
  };

  source.onerror = () => {
    // EventSource retries on its own; just surface the state.
    const line = $('#conn-line');
    line.className = 'sub err';
    line.innerHTML = '<span class="dot"></span>live feed interrupted — retrying';
  };
}

/* ── sync ────────────────────────────────────────────────────────── */

let syncPoll = null;

async function startSync(full = false) {
  $('#btn-sync').disabled = true;
  $('#btn-sync-full').disabled = true;
  $('#sync-bar').classList.remove('hidden');
  try {
    await fetch(`/api/sync?full=${full}&deep=${full}`, { method: 'POST' });
  } catch (err) {
    $('#sync-text').textContent = 'could not start sync: ' + err.message;
  }
  if (syncPoll) clearInterval(syncPoll);
  syncPoll = setInterval(pollSync, 900);
}

async function pollSync() {
  let data;
  try { data = await api('/api/sync/status'); } catch (_) { return; }
  const p = data.progress;
  $('#sync-fill').style.width = (p.percent || 0) + '%';
  $('#sync-text').textContent = p.error
    ? 'sync failed: ' + p.error
    : `${p.stage}${p.detail ? ' · ' + p.detail : ''}`
      + (p.total ? `  (${p.done}/${p.total})` : '');

  if (!p.running) {
    clearInterval(syncPoll);
    syncPoll = null;
    $('#btn-sync').disabled = false;
    $('#btn-sync-full').disabled = false;
    setTimeout(() => $('#sync-bar').classList.add('hidden'), p.error ? 8000 : 2500);
    await refreshAll(true);
  }
}

async function refreshAll(force = false) {
  await Promise.allSettled([
    loadPortfolio(force), loadTrades(), loadTransfers(),
    loadRealised(), loadHistory(force),
  ]);
  redrawCharts();
}

/* ── wiring ──────────────────────────────────────────────────────── */

function setCurrency(ccy) {
  state.ccy = ccy;
  localStorage.setItem('ccy', ccy);
  $$('.ccy-toggle button').forEach((b) =>
    b.classList.toggle('active', b.dataset.ccy === ccy));
  renderKpis(); renderHoldings(); renderTrades(); renderTransfers();
  renderRealised(); renderHistoryTable(); renderRecent();
  redrawCharts();
}

function setTab(tab) {
  state.tab = tab;
  $$('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.tab-panel').forEach((p) =>
    p.classList.toggle('active', p.id === 'tab-' + tab));
  redrawCharts();
}

function init() {
  $$('.ccy-toggle button').forEach((b) =>
    b.addEventListener('click', () => setCurrency(b.dataset.ccy)));
  $$('.tabs button').forEach((b) =>
    b.addEventListener('click', () => setTab(b.dataset.tab)));

  $('#btn-sync').addEventListener('click', () => startSync(false));
  $('#btn-sync-full').addEventListener('click', () => {
    if (confirm('Re-fetch every pair and every day of history? '
              + 'This can take several minutes.')) startSync(true);
  });
  $('#btn-rebuild-history').addEventListener('click', () => loadHistory(true));

  $('#hide-dust').addEventListener('change', (e) => {
    state.hideDust = e.target.checked;
    renderHoldings();
  });

  ['#f-trade-asset', '#f-trade-side', '#f-trade-quote'].forEach((sel) =>
    $(sel).addEventListener('input', renderTrades));
  ['#f-transfer-kind', '#f-transfer-asset'].forEach((sel) =>
    $(sel).addEventListener('input', renderTransfers));

  $$('#hist-range button').forEach((b) =>
    b.addEventListener('click', () => {
      state.histDays = Number(b.dataset.days);
      $$('#hist-range button').forEach((o) => o.classList.toggle('active', o === b));
      renderEquityChart(); renderPnlChart();
    }));

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redrawCharts, 150);
  });

  setCurrency(state.ccy);
  refreshAll().then(connectStream);

  // If the database is empty, offer to fill it straight away.
  api('/api/status').then((s) => {
    if (s.has_credentials && !(s.counts?.trades)) startSync(false);
  }).catch(() => {});
}

document.addEventListener('DOMContentLoaded', init);
