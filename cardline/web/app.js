'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const norm = s => String(s ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem('cardline:' + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem('cardline:' + k, JSON.stringify(v)); } catch {} },
};
// atualiza o HTML de um container só quando mudou (não reinicia vídeo, foco, <details>...)
const patch = (el, html) => { if (el._html === html) return false; el.innerHTML = html; el._html = html; return true; };

const S = { meta: null, col: { cards: {}, owned: [] }, runs: [], run: null, runId: null, cur: 'USD', file: null, xhr: null };

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => null);
  if (!r.ok) {
    const d = body?.detail;
    const err = new Error(typeof d === 'string' ? d : d?.message || r.statusText);
    err.detail = d;
    throw err;
  }
  return body;
}

// ---- formatação ----
const nf = new Intl.NumberFormat('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const sym = cur => cur === 'BRL' ? 'R$' : 'US$';
function money(usd, sign = false) {
  if (usd == null) return '—';
  const v = usd * (S.meta.rates[S.cur] ?? 1);
  const s = `${sym(S.cur)} ${nf.format(Math.abs(v))}`;
  return sign ? `${v >= 0 ? '+' : '−'}${s}` : `${v < 0 ? '−' : ''}${s}`;
}
const pctTxt = (now, base) => base ? `${now >= base ? '+' : '−'}${Math.abs((now - base) / base * 100).toFixed(0)}%` : '';
const cls = v => v > 0.004 ? 'up' : v < -0.004 ? 'down' : '';
const dt = s => s ? new Date(s).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : '—';
const dtShort = s => new Date(s).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const localInput = d => new Date(d - d.getTimezoneOffset() * 60e3).toISOString().slice(0, 16);  // valor de <input type=datetime-local>
function dur(a, b) {
  if (!a) return '';
  const s = Math.max(0, Math.round(((b ? new Date(b) : new Date()) - new Date(a)) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}min ${s % 60}s`;
}
const TRASH = '<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
const PENCIL = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
const STATUS = { queued: 'Na fila', running: 'Rodando', done: 'Concluída', failed: 'Falhou', interrupted: 'Interrompida', stale: 'Desatualizada' };
const STEP_ICON = { pending: '', running: '•', done: '✓', skipped: '–', failed: '!', stale: '↻' };
const active = r => r.status === 'queued' || r.status === 'running';
let RAR = {}, INK = {};  // preenchidos quando /api/meta chega
const rar = r => RAR[r] || { label: r || '?', color: '#999', rank: -1 };
const inkLabel = ink => (ink || '—').split('/').map(i => INK[i]?.label || i).join(' / ');
function img(c, large) {
  const src = large ? (c.img_large || c.img) : c.img;
  const fb = c.local ? ` data-fallback="${esc(c.local)}" onerror="if(this.dataset.fallback){this.src=this.dataset.fallback;delete this.dataset.fallback}"` : '';
  return `<img loading="lazy" src="${esc(src)}" alt="${esc(c.name)}${c.version ? ' - ' + esc(c.version) : ''}"${fb}>`;
}

// ---- tema e moeda ----
const applyTheme = t => { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; };
applyTheme(store.get('theme', null));
$('#theme').onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = cur === 'dark' ? 'light' : 'dark';
  applyTheme(next); store.set('theme', next);
};
function renderCurrency() {
  const curs = Object.keys(S.meta.rates);
  $('#currency').hidden = curs.length < 2;
  $('#currency').innerHTML = curs.map(c => `<button data-cur="${c}" aria-pressed="${c === S.cur}">${sym(c)}</button>`).join('');
}
$('#currency').onclick = e => {
  const b = e.target.closest('[data-cur]'); if (!b) return;
  S.cur = b.dataset.cur; store.set('currency', S.cur); renderAll();
};

// ---- dados da coleção ----
let entries = [];
function buildEntries() {
  entries = S.col.owned.map(o => {
    const c = S.col.cards[o.card];
    return { ...o, c, value: (o.price ?? 0) * o.qty, last: o.copies.reduce((m, x) => x.added > m ? x.added : m, ''),
             text: norm(`${c.name} ${c.version || ''} ${c.set}/${c.number} ${c.set}-${c.number}`) };
  });
}

function renderStats() {
  const total = entries.reduce((s, e) => s + e.value, 0);
  const qty = entries.reduce((s, e) => s + e.qty, 0);
  const paidRuns = S.runs.filter(r => r.kind === 'abertura' && r.paid_usd != null && r.value_now != null);
  const invested = paidRuns.reduce((s, r) => s + r.paid_usd, 0);
  const result = paidRuns.reduce((s, r) => s + r.value_now, 0) - invested;
  // de onde vem o valor da coleção: aberturas, cadastros ou cartas avulsas
  const kindOf = Object.fromEntries(S.runs.map(r => [r.id, r.kind]));
  const origin = { abertura: 0, cadastro: 0, avulsa: 0 };
  for (const e of entries) for (const x of e.copies) origin[x.run ? kindOf[x.run] || 'abertura' : 'avulsa'] += e.price ?? 0;
  const parts = [['abertura', 'de aberturas'], ['cadastro', 'de cadastros'], ['avulsa', 'avulsas']].filter(([k]) => origin[k] > 0);
  const breakdown = parts.length > 1 ? parts.map(([k, label]) => `${money(origin[k])} ${label}`).join(' · ') : '';
  patch($('#stats'), [
    ['main', 'Valor da coleção', money(total), breakdown], ['', 'Cartas', qty], ['', 'Únicas', new Set(entries.map(e => e.card)).size],
    ['', 'Foils', entries.filter(e => e.foil).reduce((s, e) => s + e.qty, 0)],
    ['', 'Investido em boosters', paidRuns.length ? money(invested) : '—'],
    ['', 'Resultado das aberturas', paidRuns.length ? `<span class="${cls(result)}">${money(result, true)}</span>` : '—'],
  ].map(([c, k, v, sub]) => `<div class="stat ${c}"><small>${k}</small><b class="num">${v}</b>${sub ? `<span class="breakdown">${sub}</span>` : ''}</div>`).join(''));
  const brl = S.meta.rates.BRL;
  $('#updated').textContent = `Preços de mercado TCGplayer via Lorcast, atualizados em ${dt(S.meta.prices_updated_at)}` +
    (brl ? ` · US$ 1 = R$ ${nf.format(brl)} (${S.meta.rate_day.split('-').reverse().join('/')})` : '') + '.';
  const n = S.runs.filter(active).length + S.jobs.filter(j => j.status === 'running').length;
  $('#active-badge').hidden = !n;
  $('#active-badge').textContent = n ? `${n} rodando` : '';
}

// ---- coleção ----
const defaults = { q: '', set: '', rarity: '', foil: 'all', min: '', sort: 'value', view: 'grid', inks: [] };
let F = { ...defaults, ...store.get('filters', {}) };
const sorters = {
  'value': (a, b) => (b.price ?? -1) - (a.price ?? -1),
  'value-asc': (a, b) => (a.price ?? 1e9) - (b.price ?? 1e9),
  'total': (a, b) => b.value - a.value,
  'name': (a, b) => a.c.name.localeCompare(b.c.name) || (a.c.version || '').localeCompare(b.c.version || ''),
  'number': (a, b) => ((+a.c.set || 999) - (+b.c.set || 999)) || ((a.c.sort ?? 0) - (b.c.sort ?? 0)) || (a.foil - b.foil),
  'recent': (a, b) => b.last.localeCompare(a.last),
  'qty': (a, b) => b.qty - a.qty,
};
// ícones das tintas: desenhos próprios a partir dos símbolos oficiais (escudo, vórtice, onda, fogo, olho, fortaleza)
const SPIRAL = Array.from({ length: 64 }, (_, i) => { const t = i / 63 * 4 * Math.PI, r = 0.44 * t; return `${(12 + r * Math.cos(t)).toFixed(2)},${(12 + r * Math.sin(t)).toFixed(2)}`; }).join(' ');
const INK_GLYPH = {
  Amber: '<path d="M12 6.2 17 8v3.6c0 3-2.1 5.1-5 6.2-2.9-1.1-5-3.2-5-6.2V8z"/><circle cx="12" cy="11.9" r="1.7"/>',
  Amethyst: `<polyline points="${SPIRAL}"/>`,
  Emerald: '<path d="M5.6 10.2c1.6-1.7 3.1-1.7 4.6 0s3.1 1.7 4.6 0 2.9-1.4 3.6-.5"/><path d="M5.6 14.3c1.6-1.7 3.1-1.7 4.6 0s3.1 1.7 4.6 0 2.9-1.4 3.6-.5"/>',
  Ruby: '<path d="M12.3 5.6c.5 2.6 4.3 4.3 4.3 8.1a4.6 4.6 0 0 1-9.2 0c0-2 1-3.2 2.1-4.1.2 1.5.9 2.4 1.9 2.7-.6-2.4-.2-4.6.9-6.7z"/>',
  Sapphire: '<path d="M4.8 12c2-3.4 4.4-5 7.2-5s5.2 1.6 7.2 5c-2 3.4-4.4 5-7.2 5s-5.2-1.6-7.2-5z"/><path d="M12 9.6l2.4 2.4-2.4 2.4-2.4-2.4z"/>',
  Steel: '<path d="M7 17.6V9.4h1.9V11h1.6V9.4h3V11h1.6V9.4H17v8.2z"/><path d="M11 17.6v-2.5a1 1 0 0 1 2 0v2.5"/>',
};
function inkIcon(k, color) {
  const ink = k === 'Amber' || k === 'Steel' ? '#1d1a14' : '#fff';  // desenho com contraste sobre a cor da tinta
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="12,1.4 21.7,7 21.7,17 12,22.6 2.3,17 2.3,7" fill="${color}"/>
    <g fill="none" stroke="${ink}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${INK_GLYPH[k] || ''}</g></svg>`;
}
let filtersOpen = store.get('filtersOpen', false);
function setupFilters() {
  const sets = [...new Set(entries.map(e => e.c.set))].sort((a, b) => (+a || 999) - (+b || 999) || a.localeCompare(b));
  const setName = Object.fromEntries(S.meta.sets.map(s => [s.code, s.name]));
  $('#set').innerHTML = '<option value="">Todos os sets</option>' + sets.map(s => `<option value="${esc(s)}">${esc(s)} · ${esc(setName[s] || s)}</option>`).join('');
  const rars = S.meta.rarities.map(r => r[0]).filter(r => entries.some(e => e.c.rarity === r));
  $('#rarity').innerHTML = '<option value="">Todas as raridades</option>' + rars.map(r => `<option value="${r}">${esc(rar(r).label)}</option>`).join('');
  $('#inks').innerHTML = S.meta.inks.map(([k, label, color]) => `<button class="inkchip" data-ink="${k}" aria-pressed="false" aria-label="${esc(label)}" title="${esc(label)}">${inkIcon(k, color)}</button>`).join('');
  document.querySelectorAll('[data-ink]').forEach(b => b.onclick = () => {
    const k = b.dataset.ink; F.inks = F.inks.includes(k) ? F.inks.filter(x => x !== k) : [...F.inks, k]; renderCollection();
  });
}
for (const k of ['q', 'set', 'rarity', 'foil', 'min', 'sort']) $('#' + k).addEventListener('input', e => { F[k] = e.target.value; renderCollection(); });
document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => { F.view = b.dataset.view; renderCollection(); });
$('#clear').onclick = () => { F = { ...defaults, view: F.view }; renderCollection(); };
$('#filter-toggle').onclick = () => { filtersOpen = !filtersOpen; store.set('filtersOpen', filtersOpen); renderCollection(); };

function renderCollection() {
  store.set('filters', F);
  for (const k of ['q', 'set', 'rarity', 'foil', 'min', 'sort']) if ($('#' + k).value !== F[k]) $('#' + k).value = F[k];
  document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', b.dataset.view === F.view));
  document.querySelectorAll('[data-ink]').forEach(b => b.setAttribute('aria-pressed', F.inks.includes(b.dataset.ink)));
  // os filtros escondidos que estão valendo aparecem no funil, para não sumirem cartas sem explicação
  const hidden = [F.set, F.rarity, F.foil !== 'all', F.min !== '', F.inks.length].filter(Boolean).length;
  $('#morefilters').hidden = !filtersOpen;
  $('#filter-toggle').setAttribute('aria-expanded', filtersOpen);
  $('#filter-toggle').title = `${filtersOpen ? 'Esconder' : 'Mostrar'} os filtros${hidden ? ` (${hidden} valendo)` : ''}`;
  $('#filter-count').hidden = !hidden;
  $('#filter-count').textContent = hidden;
  const q = norm(F.q.trim()), min = parseFloat(F.min), rate = S.meta.rates[S.cur] ?? 1;
  const list = entries.filter(e =>
    (!q || e.text.includes(q)) && (!F.set || e.c.set === F.set) && (!F.rarity || e.c.rarity === F.rarity) &&
    (F.foil === 'all' || (F.foil === 'foil') === e.foil) &&
    (!F.inks.length || (e.c.ink || '').split('/').some(i => F.inks.includes(i))) &&
    (isNaN(min) || (e.price ?? 0) * rate >= min));
  list.sort((a, b) => sorters[F.sort](a, b) || a.c.name.localeCompare(b.c.name));
  const qty = list.reduce((s, e) => s + e.qty, 0), val = list.reduce((s, e) => s + e.value, 0);
  $('#count').innerHTML = `<b>${qty}</b> ${qty === 1 ? 'carta' : 'cartas'} · <b>${new Set(list.map(e => e.card)).size}</b> únicas · <b class="num">${money(val)}</b>`;
  const out = $('#results');
  if (!entries.length) { patch(out, '<p class="empty">Nenhuma carta ainda. Comece por <a href="#/nova">uma nova pipeline</a>.</p>'); return; }
  if (!list.length) { patch(out, '<p class="empty">Nenhuma carta com esses filtros.</p>'); return; }
  if (F.view === 'grid') {
    patch(out, '<div class="grid">' + list.map(e => `
      <button class="tile${e.foil ? ' foil' : ''}" data-entry="${entries.indexOf(e)}">
        <div class="art">${img(e.c)}</div>
        ${e.qty > 1 ? `<span class="qty">×${e.qty}</span>` : ''}${e.foil ? '<span class="foiltag">FOIL</span>' : ''}
        <div class="meta">
          <div class="name">${esc(e.c.name)}</div>${e.c.version ? `<div class="ver">${esc(e.c.version)}</div>` : ''}
          <div class="row"><span class="rar" style="--c:${rar(e.c.rarity).color}"><i></i>${esc(rar(e.c.rarity).label)}</span>
          <span class="price num">${money(e.price)}</span></div>
        </div>
      </button>`).join('') + '</div>');
  } else {
    patch(out, `<div class="tablewrap"><table class="list"><thead><tr><th>Carta</th><th>Set/Nº</th><th>Raridade</th><th>Tinta</th>
      <th class="r">Qtd</th><th class="r">Preço</th><th class="r">Total</th></tr></thead><tbody>` + list.map(e => `
      <tr data-entry="${entries.indexOf(e)}"><td><b>${esc(e.c.name)}</b>${e.c.version ? ` <span class="muted">${esc(e.c.version)}</span>` : ''}
        ${e.foil ? ' <span class="foilpill">FOIL</span>' : ''}</td>
      <td class="num">${esc(e.c.set)}/${esc(e.c.number)}</td>
      <td><span class="rar" style="--c:${rar(e.c.rarity).color}"><i></i>${esc(rar(e.c.rarity).label)}</span></td>
      <td>${esc(inkLabel(e.c.ink))}</td><td class="r num">${e.qty}</td><td class="r price">${money(e.price)}</td><td class="r num">${money(e.value)}</td></tr>`).join('') +
      '</tbody></table></div>');
  }
}
$('#results').addEventListener('click', ev => { const el = ev.target.closest('[data-entry]'); if (el) openCard(entries[+el.dataset.entry]); });

// ---- detalhe da carta ----
function openCard(e) {
  const c = e.c, r = rar(c.rarity);
  const copies = e.copies.map(x => {
    const run = S.runs.find(r => r.id === x.run);
    const cadastro = run?.kind === 'cadastro';
    const link = `<a href="#/pipelines/${x.run}" onclick="document.getElementById('dlg').close()">${cadastro ? 'Cadastro' : 'Abertura'} #${x.run}</a>`;
    const where = x.run
      ? `${link} · ${dt(run?.recorded_at || run?.created_at)} · ${x.pack ? `booster ${x.pack}, ` : ''}carta ${x.slot} (${nf.format(x.t)}s)`
      : `Adicionada à mão em ${dt(x.added)}`;
    const d = x.paid != null && e.price != null ? e.price - x.paid : 0;
    return `<li>${where}<br><span class="muted">${cadastro ? 'No cadastro' : x.run ? 'Na abertura' : 'Quando adicionada'}: ${money(x.paid)} → hoje ${money(e.price)}</span>${x.paid ? ` <span class="${cls(d)}">${pctTxt(e.price, x.paid)}</span>` : ''}</li>`;
  }).join('');
  $('#dlgbody').innerHTML = `
    <button class="iconbtn close" onclick="document.getElementById('dlg').close()" aria-label="Fechar">✕</button>
    <div class="body">
      <div class="art${e.foil ? ' foil' : ''}">${img(c, true)}</div>
      <div>
        <h2>${esc(c.name)}</h2><p class="sub">${esc(c.version || '')}</p>
        <dl class="facts">
          <dt>Set</dt><dd><span class="setref">${setIcon(c.set, 'seticon small')}${esc(setTitle(c.set))}</span> · nº ${esc(c.number)}</dd>
          <dt>Raridade</dt><dd><span class="rar" style="--c:${r.color};font-size:14px;color:var(--text)"><i></i>${esc(r.label)}</span>${e.foil ? ' <span class="foilpill">FOIL</span>' : ''}</dd>
          <dt>Tinta</dt><dd>${esc(inkLabel(c.ink))}</dd>
          <dt>Tipo</dt><dd>${esc(c.type || '—')}${c.cost != null ? ` · custo ${c.cost}` : ''}</dd>
          <dt>Preço normal</dt><dd class="num">${money(c.usd)}</dd>
          <dt>Preço foil</dt><dd class="num">${money(c.usd_foil)}</dd>
          ${e.qty ? `<dt>Na coleção</dt><dd class="num">${e.qty} · total ${money(e.value)}</dd>` : ''}
        </dl>
        ${copies ? `<b>Cópias</b><ul class="copies">${copies}</ul>` : ''}
        ${c.tcg ? `<a class="btn" href="${esc(c.tcg)}" target="_blank" rel="noopener">Ver no TCGplayer ↗</a>` : ''}
      </div>
    </div>`;
  $('#dlg').className = 'dlg';
  $('#dlg').showModal();
}
$('#dlg').addEventListener('click', ev => {
  if (ev.target.id === 'dlg' || ev.target.closest('[data-close]')) $('#dlg').close();
});

// ---- lista de pipelines ----
function statusChip(r) { return `<span class="status ${r.status}">${STATUS[r.status] || r.status}</span>`; }
// selo hexagonal de quem não tem ícone: o texto encolhe com o tamanho do código (P1, D23, Coconut...)
const setHex = (code, cls = '') => `<span class="sethex ${cls}" style="--n:${Math.max(2, String(code).length)}" aria-hidden="true">${esc(code)}</span>`;
function setIcon(code, cls = 'seticon') {
  const set = S.meta.sets.find(x => x.code === code);
  return set?.icon ? `<img class="${cls}" src="${esc(set.icon)}" alt="" title="${esc(set.name)}" loading="lazy">` : setHex(code, cls);
}
const setTitle = code => S.meta.sets.find(x => x.code === code)?.name || `set ${code}`;
const KIND_LABEL = { abertura: 'Abertura de booster', cadastro: 'Cadastro de coleção', lacrados: 'Registro de lacrados' };
function kindChip(r) { return `<span class="kindchip ${r.kind}">${KIND_LABEL[r.kind] || r.kind}</span>`; }
function dupChip(r) {  // as mesmas cartas de outra pipeline (ordem, foil e repetidas não contam)
  return r.duplicates?.length ? `<span class="dupchip" title="Mesmas cartas da pipeline ${r.duplicates.map(i => `#${i}`).join(', ')}">⚠ Repetida</span>` : '';
}
let runKind = store.get('runkind', 'todas');
function resultHtml(r) {
  if (r.paid_usd == null || r.value_now == null) return '';
  const d = r.value_now - r.paid_usd;
  return `<span><small>Resultado</small><b class="${cls(d)}">${money(d, true)} (${pctTxt(r.value_now, r.paid_usd)})</b></span>`;
}
function queueLabel(r) {  // o servidor roda uma pipeline por vez, na ordem em que entraram na fila
  const ahead = S.runs.filter(x => x.id !== r.id && (x.status === 'running'
    || (x.status === 'queued' && (x.created_at < r.created_at || (x.created_at === r.created_at && x.id < r.id))))).length;
  return ahead ? `Na fila · ${ahead} na frente` : 'Na fila · começa em seguida';
}
// ícones das redes onde a pipeline já tem post vinculado
const NET_ICONS = {
  youtube: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="5" width="20" height="14" rx="4" fill="#FF0033"/><path d="M10 9l5.2 3-5.2 3z" fill="#fff"/></svg>',
  instagram: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="5.5" fill="none" stroke="#E1306C" stroke-width="2"/><circle cx="12" cy="12" r="4.2" fill="none" stroke="#E1306C" stroke-width="2"/><circle cx="17.4" cy="6.6" r="1.2" fill="#E1306C"/></svg>',
  tiktok: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3h3c.2 1.9 1.6 3.4 3.6 3.6v3a6.6 6.6 0 0 1-3.6-1.1v6.4a5.3 5.3 0 1 1-5.3-5.3l.8.1v3.1a2.3 2.3 0 1 0 1.5 2.1z" fill="currentColor"/></svg>',
};
const upcoming = at => at && new Date(at) > Date.now() ? at : null;
function netBadges(r, links = false) {  // postado; apagado = programado (ainda não saiu)
  return ['youtube', 'instagram', 'tiktok'].filter(n => r.posts?.[n] || r.scheduled?.[n]?.status === 'waiting').map(n => {
    const p = r.posts?.[n], label = NETS.find(x => x.key === n).label;
    const at = p ? upcoming(p.scheduled_at) : r.scheduled[n].publish_at;
    const title = at ? `Programado no ${label} para ${dt(at)}` : `Postado no ${label}${p.views != null ? ` · ${fmtInt(p.views)} visualizações` : ''}`;
    const cls = `netbadge${at ? ' pending' : ''}`;
    return links && p ? `<a class="${cls}" href="${esc(p.url)}" target="_blank" rel="noopener" title="${esc(title)}" aria-label="${esc(title)}">${NET_ICONS[n]}</a>`
      : `<span class="${cls}" title="${esc(title)}" role="img" aria-label="${esc(title)}">${NET_ICONS[n]}</span>`;
  }).join('');
}
function progressHtml(r) {
  if (!active(r)) return '';
  const label = r.status === 'queued' ? queueLabel(r) : `${S.meta.steps.find(s => s.name === r.step)?.label || ''} · ${r.message || ''}`;
  const p = r.status === 'queued' ? 0 : Math.round((r.progress ?? 0) * 100);
  return `<div class="bar"><i style="width:${p}%"></i></div><div class="muted" style="font-size:13px">${esc(label)}</div>`;
}
// ---- atualizações (preços, números das redes, sincronização): linhas na aba Pipelines ----
S.jobs = [];
const pctDelta = (a, b) => a ? ` (${b >= a ? '+' : '−'}${Math.abs((b - a) / a * 100).toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%)` : '';
function jobSummary(j) {  // o que a atualização mudou, numa linha
  const r = j.result;
  if (!r) return '';
  if (j.kind === 'precos') {
    const parts = [`Cartas ${money(r.cards_before)} → ${money(r.cards_after)}${pctDelta(r.cards_before, r.cards_after)}`];
    if (r.sealed) parts.push(`lacrados ${money(r.sealed_before)} → ${money(r.sealed_after)}`);
    parts.push(`${r.sets.length} ${r.sets.length === 1 ? 'set' : 'sets'}`);
    return parts.join(' · ');
  }
  if (j.kind === 'redes') return `Visualizações ${fmtInt(r.views_before)} → ${fmtInt(r.views_after)} (${r.views_after >= r.views_before ? '+' : '−'}${fmtInt(Math.abs(r.views_after - r.views_before))}) · ${r.videos} ${r.videos === 1 ? 'vídeo' : 'vídeos'}`;
  if (j.kind === 'sync' && r.sets) return `${r.sets} sets no catálogo`;
  return '';
}
function jobCard(j) {
  const running = j.status === 'running', p = Math.round((j.progress ?? 0) * 100);
  return `<a class="panel runcard jobcard" href="#/atualizacoes/${j.id}">
    <div class="runhead"><h3>Atualização #${j.id}</h3><span class="kindchip job">${esc(j.label)}</span>
      <span class="when">${dt(j.started_at)}${j.finished_at ? ` · ${dur(j.started_at, j.finished_at)}` : ''}</span>
      <span class="spacer"></span>${statusChip(j)}</div>
    ${running ? `<div class="bar"><i style="width:${j.kind === 'sync' ? 100 : p}%"${j.kind === 'sync' ? ' class="indeterminate"' : ''}></i></div>` : ''}
    <p class="jobmsg${j.status === 'failed' ? ' error' : ''}">${esc(j.message || '')}</p>
    ${jobSummary(j) ? `<p class="jobsum">${jobSummary(j)}</p>` : ''}
  </a>`;
}
function renderJob() {
  const j = S.job;
  if (!j || j.id !== S.jobId) return;
  const r = j.result || {};
  const rows = [['Início', dt(j.started_at)], ['Fim', j.finished_at ? dt(j.finished_at) : '—'], ['Duração', j.started_at ? dur(j.started_at, j.finished_at) : '—']];
  if (j.kind === 'precos' && j.result) rows.push(['Sets', (r.sets || []).map(esc).join(', ') || '—'], ['Cartas', `${money(r.cards_before)} → ${money(r.cards_after)}${pctDelta(r.cards_before, r.cards_after)}`],
    ...(r.sealed ? [['Lacrados', `${money(r.sealed_before)} → ${money(r.sealed_after)} (${r.sealed} ${r.sealed === 1 ? 'item' : 'itens'})`]] : []));
  if (j.kind === 'redes' && j.result) rows.push(['Vídeos lidos', r.videos], ['Visualizações', `${fmtInt(r.views_before)} → ${fmtInt(r.views_after)}`]);
  patch($('#view-job'), `<a class="back" href="#/pipelines">← Pipelines</a>
    <div class="runtitle"><h2>Atualização #${j.id}</h2><span class="kindchip job">${esc(j.label)}</span>${statusChip(j)}</div>
    ${j.status === 'running' && j.kind !== 'sync' ? `<div class="bar"><i style="width:${Math.round((j.progress ?? 0) * 100)}%"></i></div>` : ''}
    <p class="${j.status === 'failed' ? 'error' : 'runsub'}">${esc(j.message || '')}</p>
    <div class="panel jobinfo"><dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl></div>
    ${j.log ? `<details class="log" open><summary>Log</summary><pre>${esc(j.log)}</pre></details>` : ''}`);
}
function renderRuns() {
  const filters = [['todas', 'Todas'], ['abertura', 'Aberturas'], ['cadastro', 'Cadastros'], ['lacrados', 'Lacrados'], ['atualizacao', 'Atualizações']];
  const head = `<div class="toolbar"><h2>Pipelines</h2>
    <div class="seg" role="group" aria-label="Tipo de pipeline">${filters.map(([k, label]) =>
      `<button data-runkind="${k}" aria-pressed="${runKind === k}">${label}</button>`).join('')}</div>
    <span class="spacer"></span><a class="btn" href="#/nova">+ Nova pipeline</a></div>`;
  const runs = runKind === 'atualizacao' ? [] : S.runs.filter(r => runKind === 'todas' || r.kind === runKind);
  const jobs = ['todas', 'atualizacao'].includes(runKind) ? S.jobs : [];
  const list = [...runs.map(r => ({ r, t: new Date(r.created_at) })), ...jobs.map(j => ({ j, t: new Date(j.started_at) }))]
    .sort((a, b) => b.t - a.t);  // pipelines e atualizações juntas, da mais nova
  if (!list.length) {
    patch($('#view-pipelines'), head + `<p class="empty">${runKind === 'atualizacao' ? 'Nenhuma atualização ainda: use ↻ Atualizar preços no Resumo ou Sincronizar em Sets.'
      : S.runs.length ? 'Nenhuma pipeline deste tipo.' : 'Nenhuma pipeline ainda. Envie um vídeo em <a href="#/nova">Nova pipeline</a>.'}</p>`);
    return;
  }
  patch($('#view-pipelines'), head + '<div class="runs">' + list.map(({ r, j }) => j ? jobCard(j) : `
    <a class="panel runcard" href="#/pipelines/${r.id}">
      <div class="runhead"><h3>#${r.id}</h3>${kindChip(r)}${dupChip(r)}${(r.sets || []).map(c => setIcon(c, 'seticon small')).join('')}${netBadges(r)}<span class="when">${dt(r.recorded_at || r.created_at)} · ${esc(r.video_name)}</span>
        <span class="spacer"></span>${statusChip(r)}</div>
      ${progressHtml(r)}
      ${r.thumbs.length ? `<div class="thumbs">${r.thumbs.map(t => `<img loading="lazy" src="${esc(t)}" alt="">`).join('')}</div>` : ''}
      <div class="metrics">
        ${r.kind === 'lacrados' ? `<span><small>Boosters</small><b>${r.packs || '—'}</b></span>` : `<span><small>Cartas</small><b>${r.n_cards || '—'}</b></span>`}
        ${r.kind === 'cadastro'
          ? `<span><small>Valor no cadastro</small><b class="num">${money(r.value_open)}</b></span>`
          : `<span><small>Pago</small><b>${r.paid != null ? `${sym(r.paid_currency)} ${nf.format(r.paid)}` : '—'}</b></span>`}
        <span><small>Valor hoje</small><b class="num">${money(r.value_now)}</b></span>
        ${r.kind !== 'cadastro' ? resultHtml(r) : ''}
      </div>
    </a>`).join('') + '</div>');
}
$('#view-pipelines').addEventListener('click', ev => {
  const b = ev.target.closest('[data-runkind]');
  if (!b) return;
  runKind = b.dataset.runkind;
  store.set('runkind', runKind);
  renderRuns();
});

// ---- detalhe da pipeline ----
function runSkeleton() {
  patch($('#view-run'), `
    <a class="back" href="#/pipelines">← Pipelines</a>
    <div id="r-title"></div><p class="runsub" id="r-sub"></p><p class="warnbox" id="r-dup" hidden></p>
    <div class="kpis"><div class="panel kpi" id="r-paid"></div><div id="r-kpis" style="display:contents"></div></div>
    <p class="error" id="r-error"></p>
    <div class="cols">
      <div><div class="sectionhead"><h3 id="r-cards-title">Cartas</h3><span class="secside"><span class="muted" id="r-cards-sub"></span><span id="r-sanitize"></span></span></div>
        <ul class="pulls" id="r-cards"></ul><div id="r-edits"></div></div>
      <div class="side"><div class="panel"><ul class="steps" id="r-steps"></ul></div><div id="r-video"></div><div id="r-narr"></div><div id="r-yt"></div></div>
    </div>
    <details class="log"><summary>Log da execução</summary><pre id="r-log"></pre></details>`);
}
let editingPaid = false;
function renderRun() {
  const r = S.run;
  if (!r || r.id !== S.runId) return;
  const steps = r.steps;  // os passos deste tipo de pipeline
  const cadastro = r.kind === 'cadastro';
  const canRun = !active(r);
  const resumeLabel = steps.find(s => s.name === r.resume_from)?.label;
  patch($('#r-title'), `<div class="runtitle"><h2>Pipeline #${r.id}</h2>${kindChip(r)}${dupChip(r)}${statusChip(r)}${netBadges(r, true)}<span class="spacer"></span>
    ${canRun && r.resume_from && r.status !== 'done' ? `<button class="btn" data-action="resume">${r.status === 'stale' ? 'Reprocessar com as edições' : `Continuar de “${esc(resumeLabel)}”`}</button>` : ''}
    ${canRun ? `<span class="rerun"><select id="r-from" aria-label="Passo inicial">${steps.map(s => `<option value="${s.name}">${esc(s.label)}</option>`).join('')}</select>
      <button class="btn ghost" data-action="rerun">Rodar de novo daqui</button></span>
      <button class="btn danger" data-action="delete">Excluir</button>` : ''}</div>`);
  patch($('#r-sub'), `${esc(r.video_name)} · gravado ${dt(r.recorded_at || r.created_at)}${r.sets ? ` · ${r.sets.map(c => `<span class="setref">${setIcon(c, 'seticon small')}${esc(setTitle(c))}</span>`).join(', ')}` : ''}` +
    (r.kind === 'lacrados' ? (r.packs ? ` · ${r.packs} ${r.packs === 1 ? 'booster lacrado' : 'boosters lacrados'}` : '')
      : `${r.n_cards ? ` · ${r.n_cards} cartas${cadastro ? '' : ` · ${r.packs} ${r.packs === 1 ? 'booster' : 'boosters'}`}` : ''}`) +
    (r.options?.sealed ? ` · ${r.options.sealed.qty > 1 ? `${r.options.sealed.qty} boosters` : 'booster'} dos lacrados (${esc(setTitle(r.options.sealed.set))})` : ''));

  const twins = (r.duplicates || []).map(id => S.runs.find(x => x.id === id) || { id });
  $('#r-dup').hidden = !twins.length;
  patch($('#r-dup'), twins.length ? `<b>Pipeline repetida:</b> as cartas identificadas são as mesmas da ${twins.map(o =>
    `<a href="#/pipelines/${o.id}">#${o.id}</a>${o.video_name ? ` (${esc(o.video_name)}, ${dt(o.recorded_at || o.created_at)})` : ''}`).join(' e da ')}.
    Ordem, foil e cartas repetidas não contam. Se for o mesmo booster, exclua uma delas para a coleção não contar as cartas em dobro.` : '');
  $('#r-paid').hidden = cadastro;
  if (!editingPaid && !cadastro) {
    patch($('#r-paid'), `<small>Valor pago</small><b class="num">${r.paid != null ? `${sym(r.paid_currency)} ${nf.format(r.paid)}` : '—'}</b>
      ${r.paid != null && r.paid_currency !== S.cur ? `<div class="muted" style="font-size:13px">≈ ${money(r.paid_usd)}</div>` : ''}
      <div><button class="linkbtn" data-action="edit-paid">${r.paid != null ? 'editar' : 'informar valor pago'}</button></div>`);
  }
  const d = r.paid_usd != null && r.value_now != null ? r.value_now - r.paid_usd : null;
  if (cadastro) patch($('#r-kpis'), `
    <div class="panel kpi"><small>Cartas</small><b class="num">${r.n_cards || '—'}</b></div>
    <div class="panel kpi"><small>Valor no cadastro</small><b class="num">${money(r.value_open)}</b></div>
    <div class="panel kpi"><small>Valor hoje</small><b class="num">${money(r.value_now)}</b>
      ${r.value_open ? `<div class="${cls(r.value_now - r.value_open)}" style="font-size:13px">${pctTxt(r.value_now, r.value_open)} desde o cadastro</div>` : ''}</div>`);
  else patch($('#r-kpis'), `
    <div class="panel kpi"><small>${r.kind === 'lacrados' ? 'Boosters no registro' : 'Cartas na abertura'}</small><b class="num">${money(r.value_open)}</b></div>
    <div class="panel kpi"><small>Valor hoje</small><b class="num">${money(r.value_now)}</b>
      ${r.value_open ? `<div class="${cls(r.value_now - r.value_open)}" style="font-size:13px">${pctTxt(r.value_now, r.value_open)} desde ${r.kind === 'lacrados' ? 'o registro' : 'a abertura'}</div>` : ''}</div>
    <div class="panel kpi"><small>Resultado</small><b class="num ${d == null ? '' : cls(d)}">${d == null ? '—' : money(d, true)}</b>
      ${d != null ? `<div class="${cls(d)}" style="font-size:13px">${pctTxt(r.value_now, r.paid_usd)} sobre o valor pago</div>` : ''}</div>`);
  patch($('#r-error'), r.status === 'failed' || r.status === 'interrupted' ? esc(r.error?.[0] || 'A execução foi interrompida.') : '');

  patch($('#r-steps'), r.steps.map(s => {
    const running = s.status === 'running';
    const p = running ? Math.round((r.progress ?? 0) * 100) : 0;
    const msg = running ? r.message : s.message;
    return `<li class="${s.status}"><span class="ico">${STEP_ICON[s.status] ?? ''}</span>
      <div><div class="label">${esc(s.label)}</div>${msg ? `<div class="msg">${esc(msg)}</div>` : ''}
        ${s.status === 'stale' ? '<div class="msg">desatualizado: reprocesse para aplicar as edições</div>' : ''}
        ${running ? `<div class="bar"><i style="width:${p}%"></i></div>` : ''}</div>
      <span class="dur">${s.started_at && s.status !== 'skipped' ? dur(s.started_at, s.finished_at) : ''}</span></li>`;
  }).join(''));

  const overlayStep = r.steps.find(s => s.name === 'overlay');
  const videoCur = r.options?.currency || 'USD';
  const curControl = cadastro || r.options?.overlay === false ? '' : `<div class="vidbar"><span>Moeda do vídeo com overlay</span>
    <div class="seg" role="group" aria-label="Moeda do vídeo com overlay">${['USD', 'BRL'].filter(c => c in S.meta.rates).map(c =>
      `<button data-action="video-currency" data-cur="${c}" aria-pressed="${c === videoCur}" ${active(r) ? 'disabled' : ''}>${sym(c)}</button>`).join('')}</div></div>`;
  const logoOn = !!r.options?.logo, canLogo = logoOn || !!S.meta.logo?.default;  // ligar usa o logo padrão
  const logoControl = cadastro || r.options?.overlay === false ? '' : `<div class="vidbar"><span>Logo no vídeo</span>
    <div class="seg" role="group" aria-label="Logo no vídeo">${[[true, 'Com'], [false, 'Sem']].map(([on, label]) =>
      `<button data-action="video-logo" data-on="${on}" aria-pressed="${on === logoOn}" ${active(r) || (on && !canLogo) ? 'disabled' : ''}>${label}</button>`).join('')}</div></div>`;
  const narrStep = r.steps.find(s => s.name === 'narrate');
  const narrated = r.narrated && narrStep?.status !== 'running';
  const mode = narrated && videoMode === 'narrated' ? 'narrated' : 'plain';
  const [vsrc, vstep] = mode === 'narrated' ? [r.narrated, narrStep] : [r.overlay, overlayStep];
  const modeControl = narrated ? `<div class="vidbar"><span>Narração no vídeo</span>
    <div class="seg" role="group" aria-label="Versão do vídeo">${[['narrated', 'Com'], ['plain', 'Sem']].map(([m, label]) =>
      `<button data-action="video-mode" data-mode="${m}" aria-pressed="${m === mode}">${label}</button>`).join('')}</div></div>` : '';
  patch($('#r-video'), curControl + logoControl + modeControl + (vsrc && overlayStep && overlayStep.status !== 'running'
    ? `<video class="player" controls preload="metadata" src="${esc(vsrc)}?v=${encodeURIComponent(vstep?.finished_at || '')}"
         ${r.poster ? `poster="${esc(r.poster)}?v=${encodeURIComponent(overlayStep.finished_at || '')}"` : ''}></video>
       <p class="muted" style="font-size:13px"><a href="${esc(vsrc)}" download="pipeline-${r.id}-${mode === 'narrated' ? 'narrado' : 'overlay'}.mp4">Baixar ${mode === 'narrated' ? 'vídeo narrado' : 'vídeo com overlay'}</a></p>` : ''));
  renderNarration(r);
  renderSocial(r);

  const cards = r.cards || [];
  const editable = !active(r);
  $('#r-cards-title').textContent = r.kind === 'lacrados' ? 'Boosters' : 'Cartas';
  if (r.kind === 'lacrados') renderPacks(r, editable);
  else {
  const best = cards.reduce((b, x) => (x.price_now ?? 0) > (b?.price_now ?? -1) ? x : b, null);
  patch($('#r-cards-sub'), !cards.length ? '' : editable ? 'deslize uma carta para editar ou remover' : 'recorte do vídeo ao lado da imagem oficial');
  const nrep = r.repeated || 0;
  patch($('#r-sanitize'), cards.length ? `<button class="btn ghost small" data-action="sanitize" ${nrep && editable ? '' : 'disabled'}
    title="${nrep ? `Tira ${nrep === 1 ? 'a carta repetida' : `as ${nrep} cartas repetidas`}, deixando a primeira aparição de cada uma` : 'Nenhuma carta repetida'}">Sanitizar${nrep ? ` (${nrep})` : ''}</button>` : '');
  patch($('#r-cards'), cards.length ? cards.map(x => {
    const c = r.card_info[x.card], rr = rar(c.rarity);
    const check = x.check?.status === 'divergente'
      ? `<span class="warn" title="${esc(x.check.model)} leu: ${esc(x.check.name)} · ${esc(x.check.number)}">⚠ conferir</span>` : '';
    const name = `#${x.n}, ${esc(c.name)}`;
    return `<li class="swipe${x === best ? ' best' : ''}" data-uid="${esc(x.uid)}">
      ${editable ? `<div class="swipe-act left"><button data-action="edit-card" data-uid="${esc(x.uid)}" aria-label="Editar a carta ${name}">${PENCIL}<span>Editar</span></button></div>
      <div class="swipe-act right"><button data-action="remove-card" data-uid="${esc(x.uid)}" aria-label="Remover a carta ${name}">${TRASH}<span>Remover</span></button></div>` : ''}
      <div class="pull${editable ? ' draggable' : ''}" data-card="${esc(x.card)}" data-foil="${x.foil}" title="${x === best ? 'Melhor carta' : ''}">
      <span class="n">#${x.n}</span>
      ${x.crop ? `<img loading="lazy" src="${esc(x.crop)}" alt="Recorte do vídeo" draggable="false">` : '<span class="noimg">sem recorte</span>'}
      ${img(c)}
      <div class="info"><div><b>${esc(c.name)}</b></div><div class="ver">${esc(c.version || ' ')}</div>
        <div class="line"><span class="rar" style="--c:${rr.color}"><i></i>${esc(rr.label)}</span>${x.foil ? '<span class="foilpill">FOIL</span>' : ''}
          <span class="price num" style="color:var(--text)">${money(x.price_now ?? x.price_open)}</span>${x.manual ? '<span>· corrigida</span>' : ''}${check}
          ${x.repeat_of ? `<span class="warn" title="A mesma carta já apareceu antes no vídeo; Sanitizar tira esta e deixa a #${x.repeat_of}">↺ repete a #${x.repeat_of}</span>` : ''}</div>
        <div class="line" title="${x.inliers ? `${x.inliers} pontos casados com a imagem oficial` : 'inserida manualmente'}">${esc(c.set)}/${esc(c.number)} · ${x.t != null ? x.t.toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + 's' : '—'}</div>
      </div></div></li>`;
  }).join('') : `<p class="muted">${active(r) ? 'As cartas aparecem aqui quando a identificação terminar.' : 'Nenhuma carta identificada.'}</p>`);

  const removed = r.removed || [];
  const pending = r.status === 'stale';
  patch($('#r-edits'), (cards.length || removed.length) ? `
    ${removed.length ? `<div class="removed"><b>Removidas (${removed.length})</b><ul>${removed.map(x => {
      const c = r.card_info[x.card];
      return `<li>${x.crop ? `<img loading="lazy" src="${esc(x.crop)}" alt="">` : '<span class="noimg"></span>'}
        <span>${esc(c.name)}${c.version ? ` <span class="muted">${esc(c.version)}</span>` : ''}${x.foil ? ' <span class="foilpill">FOIL</span>' : ''}</span>
        ${editable ? `<button class="btn ghost small" data-action="restore-card" data-uid="${esc(x.uid)}">Restaurar</button>` : ''}</li>`;
    }).join('')}</ul></div>` : ''}
    <div class="reprocess${pending ? ' pending' : ''}">
      <p>${active(r) ? 'A pipeline está rodando; as cartas ficam editáveis quando ela terminar.'
        : pending ? `Edições pendentes: preços${cadastro ? ' e coleção' : ', coleção e vídeo'} só mudam depois de reprocessar.`
        : cadastro ? 'Deslize uma carta para a direita para marcar se é foil ou para a esquerda para remover, depois reprocesse.'
        : 'Deslize uma carta para a direita para editar ou para a esquerda para remover, depois reprocesse.'}</p>
      <button class="btn" data-action="resume" ${pending && editable ? '' : 'disabled'}>Reprocessar com as edições</button>
    </div>` : '');
  }

  const log = $('#r-log');
  if (log.textContent !== (r.log || '')) {
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.textContent = r.log || '';
    if (atEnd) log.scrollTop = log.scrollHeight;
  }
}

// ---- registro de lacrados: os boosters da pilha, com correção de set, remoção e inclusão ----
const boosterSets = () => S.meta.sets.filter(x => x.booster).slice().reverse();
function renderPacks(r, editable) {
  const packs = r.pack_items || [], removed = r.removed || [], pending = r.status === 'stale';
  patch($('#r-cards-sub'), packs.length ? (editable ? 'troque o set ou remova o que foi identificado errado' : 'recorte do vídeo') : '');
  patch($('#r-sanitize'), '');
  const options = sel => boosterSets().map(x => `<option value="${esc(x.code)}" ${x.code === sel ? 'selected' : ''}>${esc(x.name)}</option>`).join('');
  patch($('#r-cards'), packs.length ? packs.map(p => `<li class="packrow">
      <span class="n">#${p.n}</span>
      ${p.crop ? `<img loading="lazy" src="${esc(p.crop)}" alt="Recorte do vídeo">` : `<span class="packicon">${setIcon(p.set)}</span>`}
      <div class="info">
        <div class="pname">${setIcon(p.set, 'seticon small')}<b>${esc(p.set_name || setTitle(p.set))}</b></div>
        <div class="line"><span class="price num">${money(p.price_now ?? p.price_open)}</span>
          <span class="muted">${p.t.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}s${p.manual ? ' · corrigido' : ''}</span></div>
        ${editable ? `<div class="packedit"><select data-pack-set="${esc(p.uid)}" aria-label="Set do booster #${p.n}">${options(p.set)}</select>
          <button class="linkbtn" data-action="remove-pack" data-uid="${esc(p.uid)}">Remover</button></div>` : ''}
      </div></li>`).join('')
    : `<p class="muted">${active(r) ? 'Os boosters aparecem aqui quando a identificação terminar.' : 'Nenhum booster identificado.'}</p>`);
  patch($('#r-edits'), `
    ${removed.length ? `<div class="removed"><b>Removidos (${removed.length})</b><ul>${removed.map(p => `<li>
      ${p.crop ? `<img loading="lazy" src="${esc(p.crop)}" alt="">` : '<span class="noimg"></span>'}<span>${esc(p.set_name || setTitle(p.set))}</span>
      ${editable ? `<button class="btn ghost small" data-action="restore-pack" data-uid="${esc(p.uid)}">Restaurar</button>` : ''}</li>`).join('')}</ul></div>` : ''}
    ${editable ? `<div class="packadd"><b>Faltou um booster?</b>
      <select id="pack-add-set" aria-label="Set do booster que faltou">${options(packs.at(-1)?.set)}</select>
      <input type="number" id="pack-add-t" min="0" step="0.1" placeholder="aos … s" aria-label="Instante do vídeo, em segundos">
      <button class="btn ghost small" data-action="add-pack">Incluir</button></div>` : ''}
    <div class="reprocess${pending ? ' pending' : ''}">
      <p>${active(r) ? 'A pipeline está rodando; os boosters ficam editáveis quando ela terminar.'
        : pending ? 'Edições pendentes: preços, lacrados e vídeo só mudam depois de reprocessar.'
        : 'Corrija o set, remova ou inclua boosters e depois reprocesse.'}</p>
      <button class="btn" data-action="resume" ${pending && editable ? '' : 'disabled'}>Reprocessar com as edições</button>
    </div>`);
}
$('#view-run').addEventListener('change', async ev => {
  const sel = ev.target.closest('[data-pack-set]');
  if (!sel) return;
  try { await api(`/api/runs/${S.runId}/packs/${encodeURIComponent(sel.dataset.packSet)}`, json({ set: sel.value }, 'PATCH')); }
  catch (e) { alert(e.message); }
  await refresh();
});

// ---- narração: liga/desliga e roteiro editável ----
let videoMode = 'narrated';  // quando as duas versões existem: com ou sem narração
let narr = { runId: null, base: '', draft: [], dirty: false, voice: null };
function narrSync(r) {  // o rascunho acompanha o servidor enquanto não há edição por salvar
  const lines = r.narration?.lines || [];
  const base = JSON.stringify([r.narration?.voz, lines.map(l => [l.t, l.texto])]);
  if (narr.runId === r.id && (narr.dirty || narr.base === base)) return false;
  narr = { runId: r.id, base, draft: lines.map(l => ({ t: l.t, texto: l.texto })), dirty: false, voice: r.narration?.voz };
  return true;
}
const pitchGroup = v => !v.f0 ? 'Outras vozes' : v.f0 < 140 ? 'Graves' : v.f0 < 190 ? 'Médias' : 'Agudas';
function voiceOptions(selected) {  // agrupadas pelo tom medido na amostra
  const groups = {};
  for (const v of S.meta.narration.voices || []) (groups[pitchGroup(v)] ||= []).push(v);
  return ['Graves', 'Médias', 'Agudas', 'Outras vozes'].filter(g => groups[g]).map(g => `<optgroup label="${g}">${groups[g]
    .sort((a, b) => (a.f0 || 0) - (b.f0 || 0)).map(v => `<option value="${esc(v.name)}" ${v.name === selected ? 'selected' : ''}>${esc(v.name)}</option>`).join('')}</optgroup>`).join('');
}
let voicePlayer = null;
function playVoice(name, btn) {  // ▶ toca a amostra da voz escolhida; tocar de novo para
  const v = (S.meta.narration.voices || []).find(x => x.name === name);
  if (voicePlayer) { voicePlayer.pause(); voicePlayer = null; btn.textContent = '▶'; return; }
  if (!v?.sample) { alert('Esta voz ainda não tem amostra (rode: cardline voz --amostras).'); return; }
  voicePlayer = new Audio(v.sample);
  btn.textContent = '■';
  voicePlayer.onended = () => { voicePlayer = null; btn.textContent = '▶'; };
  voicePlayer.play().catch(() => { voicePlayer = null; btn.textContent = '▶'; });
}
const fmtT = t => (Math.round(t * 100) / 100).toLocaleString('pt-BR', { maximumFractionDigits: 2 });
function scriptRows() {
  $('#r-script').innerHTML = narr.draft.map((l, i) => `<li>
    <div class="lhead"><button type="button" class="seek" data-action="script-seek" data-i="${i}" title="Ver este momento no vídeo" aria-label="Ver o momento da fala ${i + 1} no vídeo">▶</button>
      <input class="t" type="text" inputmode="decimal" value="${fmtT(l.t)}" data-i="${i}" data-field="t" aria-label="Instante da fala ${i + 1}, em segundos"><span class="muted">s</span>
      <span class="spacer"></span><button type="button" class="del" data-action="script-del" data-i="${i}" title="Apagar a fala" aria-label="Apagar a fala ${i + 1}">✕</button></div>
    <textarea class="txt" rows="2" maxlength="160" data-i="${i}" data-field="texto" aria-label="Fala ${i + 1}">${esc(l.texto)}</textarea></li>`).join('');
}
function renderNarration(r) {
  const box = $('#r-narr'), nm = S.meta.narration || {};
  if (r.kind === 'cadastro' || r.options?.overlay === false) { patch(box, ''); return; }
  const n = r.narration || { enabled: false, lines: [] };
  const busy = active(r);
  if (!n.enabled) {
    narr.runId = null;
    patch(box, `<div class="panel narr"><h3>Narração</h3>
      <p class="muted">Um narrador de trailer comenta a abertura: a aposta, a falsa esperança, as raras e o desfecho, sem spoiler. O roteiro fica editável aqui.</p>
      <div><button class="btn" data-action="narration-on" ${nm.unavailable || busy ? 'disabled' : ''}>Narrar este vídeo</button></div>
      ${nm.unavailable ? `<p class="muted small">Para narrar, ${esc(nm.unavailable)}.</p>` : ''}</div>`);
    return;
  }
  if (!box.querySelector('#r-script')) {  // o editor é montado uma vez; depois só muda o que não é digitado
    patch(box, `<div class="panel narr">
      <div class="narrhead"><h3>Narração</h3><span class="muted narrsrc"></span><span class="spacer"></span>
        <button class="linkbtn" data-action="narration-off">Desligar</button></div>
      <fieldset class="scriptset"><div class="voicepick"><label for="r-voice">Voz</label><select id="r-voice"></select>
          <button type="button" class="btn ghost small" data-action="voice-play" title="Ouvir esta voz" aria-label="Ouvir a amostra da voz">▶</button></div>
        <ol class="script" id="r-script"></ol><p class="muted small narrempty"></p>
        <div class="narractions"><button type="button" class="linkbtn" data-action="script-add">+ Fala</button><span class="spacer"></span>
          <button type="button" class="linkbtn" data-action="script-new">Escrever outro roteiro</button></div>
        <button type="button" class="btn" data-action="script-save">Salvar e narrar de novo</button></fieldset>
      <p class="muted small">Edite o texto ou o instante (em segundos) de cada fala. Ao narrar de novo, só as falas que mudaram são gravadas.</p></div>`);
    narr.runId = null;
  }
  if (narrSync(r)) { scriptRows(); patch($('#r-voice'), voiceOptions(narr.voice)); }
  const empty = !narr.draft.length;
  box.querySelector('.narrsrc').textContent = n.source === 'editado' ? 'roteiro editado por você'
    : n.writer ? `piadas do ${n.writer}` : n.lines.length ? 'roteiro automático' : '';
  box.querySelector('.narrempty').hidden = !empty;
  box.querySelector('.narrempty').textContent = busy ? 'O roteiro aparece aqui quando a narração começar.' : 'Sem falas. Use “Outro roteiro” para escrever um.';
  box.querySelector('.scriptset').disabled = busy;
  box.querySelector('[data-action="script-save"]').disabled = !narr.dirty || empty;
}
$('#view-run').addEventListener('change', ev => {  // trocar a voz: vale ao narrar de novo
  if (ev.target.id !== 'r-voice') return;
  narr.voice = ev.target.value;
  narr.dirty = true;
  if (voicePlayer) { voicePlayer.pause(); voicePlayer = null; }
  $('#r-narr [data-action="voice-play"]').textContent = '▶';
  $('#r-narr [data-action="script-save"]').disabled = false;
});
$('#view-run').addEventListener('input', ev => {
  const el = ev.target.closest('#r-script [data-field]');
  if (!el) return;
  const line = narr.draft[+el.dataset.i];
  if (el.dataset.field === 't') line.t = Math.max(0, parseFloat(el.value.replace(',', '.')) || 0); else line.texto = el.value;
  narr.dirty = true;
  $('#r-narr [data-action="script-save"]').disabled = false;
});

$('#view-run').addEventListener('click', async ev => {
  const target = ev.target.closest('[data-action]');
  const action = target?.dataset.action;
  if (!action) {
    const pull = ev.target.closest('.pull');
    if (Date.now() - swipe.endedAt < 400) return;  // o clique que vem junto com o fim do arraste
    if (pull && swipe.open === pull.closest('.swipe')) { closeSwipe(); return; }
    closeSwipe();
    if (pull) {
      const e = entries.find(x => x.card === pull.dataset.card && String(x.foil) === pull.dataset.foil);
      const c = S.run.card_info[pull.dataset.card];
      openCard(e || { c, foil: pull.dataset.foil === 'true', qty: 0, copies: [], value: 0, price: null });
    }
    return;
  }
  const id = S.runId;
  try {
    if (action === 'remove-card' || action === 'restore-card') {
      target.disabled = true;
      const uid = encodeURIComponent(target.dataset.uid);
      await api(action === 'remove-card' ? `/api/runs/${id}/cards/${uid}` : `/api/runs/${id}/cards/${uid}/restore`,
                { method: action === 'remove-card' ? 'DELETE' : 'POST' });
    }
    if (action === 'remove-pack' || action === 'restore-pack') {
      target.disabled = true;
      const uid = encodeURIComponent(target.dataset.uid);
      await api(action === 'remove-pack' ? `/api/runs/${id}/packs/${uid}` : `/api/runs/${id}/packs/${uid}/restore`,
                { method: action === 'remove-pack' ? 'DELETE' : 'POST' });
    }
    if (action === 'add-pack') {
      const t = parseFloat(String($('#pack-add-t').value).replace(',', '.'));
      if (isNaN(t)) { alert('Informe em que segundo do vídeo o booster aparece.'); return; }
      target.disabled = true;
      await api(`/api/runs/${id}/packs`, json({ set: $('#pack-add-set').value, t }));
    }
    if (action === 'sanitize') {
      const n = S.run?.repeated || 0;
      if (!confirm(`Tirar ${n === 1 ? 'a carta repetida' : `as ${n} cartas repetidas`}? Fica a primeira aparição de cada carta; as tiradas vão para "Removidas" (dá para restaurar) e valem depois de reprocessar.`)) return;
      target.disabled = true;
      await api(`/api/runs/${id}/sanitize`, { method: 'POST' });
    }
    if (action === 'resume') await api(`/api/runs/${id}/rerun`, json({ from_step: null }));
    if (action === 'rerun') await api(`/api/runs/${id}/rerun`, json({ from_step: $('#r-from').value }));
    if (action === 'delete') {
      if (!confirm(`Excluir a pipeline #${id}? As cartas que ela registrou saem da coleção e a pasta runs/${id} é apagada.`)) return;
      await api(`/api/runs/${id}`, { method: 'DELETE' });
      location.hash = '#/pipelines';
      await refresh(true);
      return;
    }
    if (action === 'video-mode') { videoMode = target.dataset.mode; renderRun(); return; }
    if (action === 'narration-on') {
      target.disabled = true;
      await api(`/api/runs/${id}`, json({ narration: true }, 'PATCH'));
      await api(`/api/runs/${id}/rerun`, json({ from_step: null }));
    }
    if (action === 'narration-off') {
      if (!confirm('Desligar a narração? O vídeo narrado é apagado; as falas gravadas ficam guardadas para quando você ligar de novo.')) return;
      await api(`/api/runs/${id}`, json({ narration: false }, 'PATCH'));
    }
    if (action === 'script-add' || action === 'script-del') {
      if (action === 'script-del') narr.draft.splice(+target.dataset.i, 1);
      else {
        const last = narr.draft[narr.draft.length - 1];
        narr.draft.push({ t: last ? Math.round((last.t + 2) * 10) / 10 : 0.2, texto: '' });
      }
      narr.dirty = true;
      scriptRows();
      renderNarration(S.run);
      if (action === 'script-add') $('#r-script li:last-child .txt')?.focus();
      return;
    }
    if (action === 'voice-play') { playVoice($('#r-voice').value, target); return; }
    if (action === 'script-seek') {
      const video = $('#r-video video'), line = narr.draft[+target.dataset.i];
      if (video && line) { video.currentTime = Math.max(0, line.t - 0.5); video.play(); }
      return;
    }
    if (action === 'script-save') {
      target.disabled = true;
      await api(`/api/runs/${id}/narration`, json({ lines: narr.draft.filter(l => l.texto.trim()), voz: narr.voice }, 'PUT'));
      narr.dirty = false; narr.base = '';  // a próxima atualização traz o roteiro como o servidor guardou
      await api(`/api/runs/${id}/rerun`, json({ from_step: null }));
    }
    if (action === 'script-new') {
      if (!confirm(narr.dirty ? 'Descartar suas edições e escrever outro roteiro?' : 'Escrever outro roteiro no lugar deste?')) return;
      await api(`/api/runs/${id}/narration/new`, { method: 'POST' });
      narr.dirty = false; narr.base = '';
      await api(`/api/runs/${id}/rerun`, json({ from_step: null }));
    }
    if (action === 'edit-paid') { editPaid(); return; }
    if (action === 'edit-card') { editCard(target.dataset.uid); return; }
    if (action === 'video-currency') {
      if (target.getAttribute('aria-pressed') === 'true') return;
      await api(`/api/runs/${id}`, json({ currency: target.dataset.cur }, 'PATCH'));
    }
    if (action === 'video-logo') {  // vale ao reprocessar (o vídeo é refeito a partir do overlay)
      if (target.getAttribute('aria-pressed') === 'true') return;
      await api(`/api/runs/${id}`, json({ logo: target.dataset.on === 'true' ? S.meta.logo?.default : null }, 'PATCH'));
    }
    await refresh();
  } catch (e) { alert(e.message); }
});
// ---- deslizar a carta: direita revela Editar, esquerda revela Remover ----
const SWIPE_W = 96;
const swipe = { open: null, drag: null, endedAt: 0 };
function setSwipe(li, x, animate = true) {
  const pull = li.querySelector('.pull');
  pull.style.transition = animate ? '' : 'none';
  pull.style.transform = x ? `translateX(${x}px)` : '';
  li.dataset.open = x > 0 ? 'left' : x < 0 ? 'right' : '';
}
function closeSwipe() {
  if (swipe.open?.isConnected) setSwipe(swipe.open, 0);
  swipe.open = null;
}
const cardsEl = $('#view-run');
cardsEl.addEventListener('pointerdown', ev => {
  const pull = ev.target.closest('.pull.draggable');
  if (!pull || ev.button > 0) return;
  const li = pull.closest('.swipe');
  const base = li.dataset.open === 'left' ? SWIPE_W : li.dataset.open === 'right' ? -SWIPE_W : 0;
  swipe.drag = { li, pull, id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, base, x: base, moving: false };
});
cardsEl.addEventListener('pointermove', ev => {
  const d = swipe.drag;
  if (!d || ev.pointerId !== d.id) return;
  const dx = ev.clientX - d.x0, dy = ev.clientY - d.y0;
  if (!d.moving) {
    if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
    if (Math.abs(dy) > Math.abs(dx)) { swipe.drag = null; return; }  // é rolagem da página
    d.moving = true;
    d.pull.setPointerCapture(ev.pointerId);
    if (swipe.open && swipe.open !== d.li) closeSwipe();
  }
  const limit = SWIPE_W * 1.3;
  d.x = Math.max(-limit, Math.min(limit, d.base + dx));
  setSwipe(d.li, d.x, false);
});
const endSwipe = ev => {
  const d = swipe.drag;
  if (!d || ev.pointerId !== d.id) return;
  swipe.drag = null;
  if (!d.moving) return;
  const to = d.x > SWIPE_W * 0.45 ? SWIPE_W : d.x < -SWIPE_W * 0.45 ? -SWIPE_W : 0;
  setSwipe(d.li, to);
  swipe.open = to ? d.li : null;
  swipe.endedAt = Date.now();
};
cardsEl.addEventListener('pointerup', endSwipe);
cardsEl.addEventListener('pointercancel', endSwipe);
// teclado: o botão de ação ganha foco pelo Tab e a carta desliza para mostrá-lo
cardsEl.addEventListener('focusin', ev => {
  const act = ev.target.closest('.swipe-act');
  if (!act) return;
  const li = act.closest('.swipe');
  if (swipe.open && swipe.open !== li) closeSwipe();
  setSwipe(li, act.classList.contains('left') ? SWIPE_W : -SWIPE_W);
  swipe.open = li;
});
document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && !$('#dlg').open) closeSwipe(); });
document.addEventListener('pointerdown', ev => { if (swipe.open && !swipe.open.contains(ev.target)) closeSwipe(); });

// ---- editar carta (por enquanto, só se é foil) ----
const FOIL_ONLY = ['Enchanted', 'Epic', 'Iconic'];
function editCard(uid) {
  const r = S.run, x = r.cards.find(card => card.uid === uid);
  if (!x) return;
  const c = r.card_info[x.card], fixed = FOIL_ONLY.includes(c.rarity);
  $('#dlgbody').innerHTML = `
    <form class="editcard" id="edit-form">
      <button class="iconbtn close" type="button" data-close aria-label="Fechar">✕</button>
      <h2>Editar carta #${x.n}</h2>
      <div class="editpreview">
        ${x.crop ? `<img src="${esc(x.crop)}" alt="Recorte do vídeo">` : ''}${img(c)}
        <div><b>${esc(c.name)}</b><div class="muted">${esc(c.version || '')}</div>
          <div class="muted">${esc(rar(c.rarity).label)} · ${esc(c.set)}/${esc(c.number)}</div></div>
      </div>
      <label class="toggle"><input type="checkbox" id="edit-foil" ${x.foil ? 'checked' : ''} ${fixed ? 'disabled' : ''}><span>Foil</span></label>
      <p class="muted">${fixed ? `${esc(rar(c.rarity).label)} é sempre foil.`
        : r.kind === 'cadastro' ? 'Marque se esta cópia é foil. O preço do cadastro é recalculado para o acabamento escolhido quando você reprocessar.'
        : 'Um booster tem uma foil: marcar esta carta tira a marcação automática de outra do mesmo booster. O preço da abertura é recalculado para o acabamento escolhido quando você reprocessar.'}</p>
      <div class="actions"><button class="btn" id="edit-save" ${fixed ? 'disabled' : ''}>Salvar</button>
        <button class="btn ghost" type="button" data-close>Cancelar</button></div>
      <p class="error" id="edit-error" hidden></p>
    </form>`;
  $('#dlg').className = 'dlg narrow';
  $('#dlg').showModal();
  $('#edit-form').onsubmit = async ev => {
    ev.preventDefault();
    $('#edit-save').disabled = true;
    try {
      await api(`/api/runs/${r.id}/cards/${encodeURIComponent(uid)}`, json({ foil: $('#edit-foil').checked }, 'PATCH'));
      $('#dlg').close();
      closeSwipe();
      await refresh();
    } catch (e) {
      $('#edit-error').textContent = e.message;
      $('#edit-error').hidden = false;
      $('#edit-save').disabled = false;
    }
  };
}

const json = (body, method = 'POST') => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const parseMoney = s => { const v = parseFloat(String(s).trim().replace(/\./g, '').replace(',', '.')); return isNaN(v) ? null : v; };
function editPaid() {
  const r = S.run;
  editingPaid = true;
  const box = $('#r-paid');
  box._html = null;
  box.innerHTML = `<small>Valor pago</small><form><input type="text" inputmode="decimal" value="${r.paid != null ? nf.format(r.paid) : ''}" placeholder="34,90" aria-label="Valor pago">
    <select aria-label="Moeda"><option value="BRL">R$</option><option value="USD">US$</option></select>
    <button class="btn">Salvar</button></form>`;
  box.querySelector('select').value = r.paid_currency || 'BRL';
  box.querySelector('input').focus();
  box.querySelector('form').onsubmit = async ev => {
    ev.preventDefault();
    try {
      await api(`/api/runs/${r.id}`, json({ paid: parseMoney(box.querySelector('input').value), paid_currency: box.querySelector('select').value }, 'PATCH'));
    } catch (e) { alert(e.message); }
    editingPaid = false;
    await refresh(true);
  };
}

// ---- nova pipeline ----
function prepareNew() {
  const sets = S.meta.sets.filter(s => s.booster).reverse();
  $('#new-set').innerHTML = '<option value="">Detectar automaticamente</option>' + sets.map(s => `<option value="${esc(s.code)}">${esc(s.code)} · ${esc(s.name)}</option>`).join('');
  setNewCurrency(S.meta.currency);
  const v = S.meta.verify;
  $('#opt-verify').disabled = !v.available;
  $('#opt-verify').checked = v.available && v.default;
  $('#verify-label').textContent = v.available ? `${v.model} lê o nome e o número de cada recorte (~1 s por carta)` : 'Precisa do Ollama rodando (não encontrado)';
  const nm = S.meta.narration;
  $('#opt-narration').checked = false;
  $('#narration-label').textContent = nm.unavailable ? `Para isso, ${nm.unavailable}`
    : 'Um narrador comenta a abertura sem dar spoiler (voz em português, ~2 min a mais)';
  newLogo = null;
  $('#opt-logo').checked = !!S.meta.logo?.default;
  loadSealedBoosters();
  syncNarrationOption();
  applyKind();
}
function syncNarrationOption() {  // a narração e o logo são do vídeo com overlay
  $('#opt-narration').disabled = !!S.meta.narration.unavailable || !$('#opt-overlay').checked;
  if ($('#opt-narration').disabled) $('#opt-narration').checked = false;
  syncLogoOption();
}
$('#opt-overlay').addEventListener('change', syncNarrationOption);
// ---- booster dos lacrados: o valor de registro e o set vêm dele, e ele sai do estoque ----
let sealedBoosters = [];
async function loadSealedBoosters() {
  try { sealedBoosters = await api('/api/sealed/boosters'); } catch { sealedBoosters = []; }
  $('#from-sealed-box').hidden = !sealedBoosters.length || newKind() !== 'abertura';
  $('#from-sealed').innerHTML = '<option value="">Nenhum: não veio dos lacrados</option>' + sealedBoosters.map(x =>
    `<option value="${x.id}">${esc(setTitle(x.set_code))} · ${esc(x.name)} (${x.qty} ${x.qty === 1 ? 'fechado' : 'fechados'})</option>`).join('');
  pickSealedBooster();
}
function pickSealedBooster() {
  const x = sealedBoosters.find(b => String(b.id) === $('#from-sealed').value), qty = $('#from-sealed-qty');
  qty.hidden = !x || x.qty < 2;
  if (!x) { $('#from-sealed-note').textContent = 'Escolha um booster fechado da coleção: o valor pago e o set vêm dele, e ele sai do estoque.'; return; }
  qty.max = x.qty;
  const n = Math.min(x.qty, Math.max(1, parseInt(qty.value, 10) || 1));
  qty.value = n;
  if (x.paid != null) {  // o valor que você informou ao registrar; sem ele, o preço de mercado do registro
    setNewCurrency(x.paid_currency);
    $('#paid').value = nf.format(x.paid * n);
  } else if (x.registered_usd != null) {
    $('#paid').value = nf.format(x.registered_usd * n * (S.meta.rates[newCurrency] ?? 1));
  }
  if ([...$('#new-set').options].some(o => o.value === x.set_code)) $('#new-set').value = x.set_code;
  $('#from-sealed-note').textContent = `${x.paid != null ? `Registrado por ${sym(x.paid_currency)} ${nf.format(x.paid)} cada` : `Valia ${money(x.registered_usd)} cada quando foi registrado`}; sai do estoque ao enviar (volta se a pipeline for excluída).`;
}
$('#from-sealed').onchange = pickSealedBooster;
$('#from-sealed-qty').oninput = pickSealedBooster;
// moeda escolhida uma vez: a do valor pago é a do vídeo
let newCurrency = 'BRL';
function setNewCurrency(cur) {
  newCurrency = cur === 'USD' ? 'USD' : 'BRL';
  document.querySelectorAll('[data-newcur]').forEach(b => b.setAttribute('aria-pressed', b.dataset.newcur === newCurrency));
  $('#paid-cur').textContent = sym(newCurrency);
}
document.querySelectorAll('[data-newcur]').forEach(b => b.onclick = () => setNewCurrency(b.dataset.newcur));
// ---- logo do vídeo: o padrão (data/logos/padrao.png) ou um enviado agora ----
let newLogo = null;  // { logo, url } enviado nesta criação; null = o padrão
function syncLogoOption() {
  const def = S.meta.logo?.default, src = newLogo ? newLogo.url : def ? `/logos/${def}` : '';
  $('#opt-logo').disabled = !$('#opt-overlay').checked || !src;
  if ($('#opt-logo').disabled) $('#opt-logo').checked = false;
  $('#logo-preview').hidden = !src;
  if (src && $('#logo-preview').getAttribute('src') !== src) $('#logo-preview').src = src;
  $('#logo-change').textContent = src ? 'Trocar logo' : 'Escolher um logo';
  $('#logo-change').disabled = !$('#opt-overlay').checked;
  $('#logo-default').hidden = !newLogo || !def;
}
$('#opt-logo').addEventListener('change', syncLogoOption);
$('#logo-change').onclick = () => { $('#logo-file').value = ''; $('#logo-file').click(); };
$('#logo-default').onclick = () => { newLogo = null; $('#opt-logo').checked = true; syncLogoOption(); };
$('#logo-file').onchange = async ev => {
  const f = ev.target.files[0];
  if (!f) return;
  try {
    newLogo = await api('/api/logos', { method: 'POST', body: f, headers: { 'Content-Type': 'application/octet-stream' } });
    $('#opt-logo').checked = true;
  } catch (e) { newError(esc(e.message)); }
  syncLogoOption();
};
document.addEventListener('input', ev => { if (ev.target.id === 'sp-when' && S.run) renderSocial(S.run); });
const newKind = () => document.querySelector('input[name="kind"]:checked').value;
function applyKind() {  // cada campo diz em que tipos de pipeline ele vale (data-kinds)
  const kind = newKind();
  document.querySelectorAll('#new-form [data-kinds]').forEach(el => { el.hidden = !el.dataset.kinds.split(' ').includes(kind); });
  if (!sealedBoosters.length) $('#from-sealed-box').hidden = true;
  const lacrados = kind === 'lacrados';
  $('#paid-label').textContent = lacrados ? 'Valor pago pelos boosters (total)' : 'Valor pago pelo(s) booster(s)';
  $('#overlay-label').textContent = lacrados ? 'A etiqueta de cada booster, o total da pilha e o resumo por set'
    : 'As etiquetas de cada carta, o total do booster e o resumo';
  if (!S.meta.narration.unavailable) $('#narration-label').textContent = lacrados
    ? 'Um narrador comenta a pilha de lacrados (voz em português, ~2 min a mais)'
    : 'Um narrador comenta a abertura sem dar spoiler (voz em português, ~2 min a mais)';
  $('#send').textContent = kind === 'cadastro' ? 'Enviar e cadastrar' : lacrados ? 'Enviar e registrar' : 'Enviar e processar';
}
document.querySelectorAll('input[name="kind"]').forEach(r => r.addEventListener('change', applyKind));
function pickFile(f) {
  if (!f) return;
  S.file = f;
  $('#drop').classList.add('ready');
  $('#drop-title').textContent = f.name;
  $('#drop-sub').textContent = `${(f.size / 1048576).toFixed(0)} MB · clique para trocar`;
  $('#new-error').hidden = true;
}
$('#file').onchange = e => pickFile(e.target.files[0]);
const drop = $('#drop');
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); pickFile(e.dataTransfer.files[0]); });
function newError(html) { $('#new-error').innerHTML = html; $('#new-error').hidden = false; }
$('#new-form').onsubmit = ev => {
  ev.preventDefault();
  if (!S.file) { newError('Escolha o vídeo da abertura.'); return; }
  const paidRaw = $('#paid').value.trim();
  const paid = paidRaw ? parseMoney(paidRaw) : null;
  if (paidRaw && paid == null) { newError('Valor pago inválido.'); return; }
  const kind = newKind();
  const params = new URLSearchParams({ filename: S.file.name, kind, verify: $('#opt-verify').checked });
  if (kind === 'abertura' || kind === 'lacrados') {
    params.set('paid_currency', newCurrency);
    params.set('overlay', $('#opt-overlay').checked);
    params.set('narration', $('#opt-narration').checked);
    params.set('currency', newCurrency);
    if (paid != null) params.set('paid', paid);
    params.set('logo', $('#opt-logo').checked ? newLogo?.logo || S.meta.logo?.default || '' : '');
    if (kind === 'abertura' && $('#from-sealed').value) { params.set('sealed_id', $('#from-sealed').value); params.set('sealed_qty', $('#from-sealed-qty').value || 1); }
  }
  if ($('#new-set').value) params.set('set_hint', $('#new-set').value);
  const xhr = S.xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/runs?' + params);
  xhr.setRequestHeader('Content-Type', 'application/octet-stream');
  $('#upload').hidden = false; $('#send').disabled = true; $('#new-error').hidden = true;
  xhr.upload.onprogress = e => {
    const p = e.total ? e.loaded / e.total : 0;
    $('#upload-bar').style.width = `${(p * 100).toFixed(1)}%`;
    $('#upload-text').textContent = p < 1 ? `Enviando ${(p * 100).toFixed(0)}% (${(e.loaded / 1048576).toFixed(0)} de ${(e.total / 1048576).toFixed(0)} MB)` : 'Conferindo o vídeo…';
  };
  xhr.onload = async () => {
    S.xhr = null; $('#send').disabled = false; $('#upload').hidden = true;
    let body = null; try { body = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 201) {
      S.file = null; $('#new-form').reset(); $('#drop').classList.remove('ready');
      $('#drop-title').textContent = 'Arraste o vídeo aqui ou clique para escolher';
      $('#drop-sub').textContent = 'MP4 ou MOV, como sai da câmera do celular';
      location.hash = `#/pipelines/${body.id}`;
      await refresh();
    } else if (xhr.status === 409 && body?.detail?.run_id) {
      newError(`${esc(body.detail.message)} <a href="#/pipelines/${body.detail.run_id}">Abrir pipeline #${body.detail.run_id}</a>`);
    } else {
      newError(esc(typeof body?.detail === 'string' ? body.detail : `Falha no envio (${xhr.status}).`));
    }
  };
  xhr.onerror = () => { S.xhr = null; $('#send').disabled = false; $('#upload').hidden = true; newError('Falha de conexão durante o envio.'); };
  xhr.send(S.file);
};
window.addEventListener('beforeunload', e => { if (S.xhr) e.preventDefault(); });

// ---- gráfico: gasto vs valor das cartas ----
const SERIES = [  // cor segue a série (slots validados), nunca a posição
  { key: 'spent', label: 'Gasto', color: 'var(--s-spent)' },
  { key: 'open', label: 'Valor na abertura', color: 'var(--s-open)' },
  { key: 'now', label: 'Valor hoje', color: 'var(--s-now)' },
];
const runTime = r => new Date(r.recorded_at || r.created_at).getTime();
function chartPoints() {
  const runs = S.runs.filter(r => r.kind === 'abertura' && r.paid_usd != null && r.value_now != null && r.n_cards)
    .sort((a, b) => runTime(a) - runTime(b));
  const pts = [{ label: 'início', spent: 0, open: 0, now: 0 }];
  let spent = 0, open = 0, now = 0;
  for (const r of runs) {
    spent += r.paid_usd; open += r.value_open ?? 0; now += r.value_now ?? 0;
    pts.push({ label: `#${r.id}`, run: r, spent, open, now });
  }
  return pts;
}
function niceStep(max, n = 4) {
  const raw = max / n, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
}
function renderChart() {
  const wrap = $('#chart');
  patch($('#chart-legend'), SERIES.map(s => `<li><i style="--c:${s.color}"></i>${s.label}</li>`).join(''));
  const pts = chartPoints(), rate = S.meta.rates[S.cur] ?? 1;
  if (pts.length < 2) {
    patch(wrap, '<p class="muted chartempty">Informe o valor pago nas pipelines para comparar o gasto com o valor das cartas.</p>');
    patch($('#chart-table'), '');
    return;
  }
  if (!chartWidth) return;  // ainda sem layout: o ResizeObserver chama de novo
  const W = Math.max(280, chartWidth), H = 210, m = { l: 66, r: 96, t: 12, b: 28 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const top = (() => { const max = Math.max(...pts.flatMap(p => SERIES.map(s => p[s.key]))) * rate || 1; const st = niceStep(max); return { st, v: Math.ceil(max / st) * st }; })();
  const x = i => m.l + i * pw / (pts.length - 1);
  const y = usd => m.t + ph - usd * rate / top.v * ph;
  const digits = top.st < 1 ? 2 : Number.isInteger(top.st) ? 0 : 1;  // passo 2,5 → 7,5 e não "8"
  const tickFmt = v => `${sym(S.cur)} ${v.toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
  let svg = '';
  for (let v = 0; v <= top.v + 1e-9; v += top.st) {
    const yy = (m.t + ph - v / top.v * ph).toFixed(1);
    svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}"/><text class="tick" x="${m.l - 8}" y="${yy}" text-anchor="end" dominant-baseline="middle">${tickFmt(v)}</text>`;
  }
  const every = Math.ceil(pts.length / Math.max(2, Math.floor(pw / 56)));
  pts.forEach((p, i) => { if (i % every === 0 || i === pts.length - 1) svg += `<text class="tick" x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(p.label)}</text>`; });
  for (const s of SERIES) svg += `<polyline points="${pts.map((p, i) => `${x(i).toFixed(1)},${y(p[s.key]).toFixed(1)}`).join(' ')}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const last = pts.at(-1), lx = x(pts.length - 1);
  for (const s of SERIES) svg += `<circle cx="${lx.toFixed(1)}" cy="${y(last[s.key]).toFixed(1)}" r="4" fill="${s.color}" stroke="var(--panel)" stroke-width="2"/>`;
  const placed = [];  // rótulos no fim só onde não colidem; o resto fica na legenda, tooltip e tabela
  for (const s of [SERIES[0], SERIES[2], SERIES[1]]) {
    const yy = y(last[s.key]);
    if (placed.every(l => Math.abs(l - yy) >= 15)) { placed.push(yy); svg += `<text class="endlabel" x="${(lx + 10).toFixed(1)}" y="${yy.toFixed(1)}" dominant-baseline="middle">${money(last[s.key])}</text>`; }
  }
  const summary = `Acumulado: gasto ${money(last.spent)}, cartas valiam ${money(last.open)} na abertura e valem ${money(last.now)} hoje.`;
  const changed = patch(wrap, `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" tabindex="0" aria-label="${esc(summary)} Use as setas para ver cada abertura.">${svg}
    <line class="xhair" id="xhair" y1="${m.t}" y2="${m.t + ph}" visibility="hidden"/><rect x="${m.l - 12}" y="0" width="${pw + 24}" height="${H}" fill="transparent"/></svg>
    <div class="tip" id="chart-tip" hidden></div>`);
  renderChartTable(pts);
  if (!changed) return;
  const el = wrap.querySelector('svg'), tip = $('#chart-tip'), hair = $('#xhair');
  let idx = pts.length - 1;
  const show = i => {
    idx = Math.max(0, Math.min(pts.length - 1, i));
    const p = pts[idx], xx = x(idx);
    hair.setAttribute('x1', xx); hair.setAttribute('x2', xx); hair.setAttribute('visibility', 'visible');
    tip.replaceChildren();
    const head = document.createElement('div'); head.className = 't-head';
    head.textContent = p.run ? `Até a pipeline #${p.run.id} · ${dt(p.run.recorded_at || p.run.created_at)}` : 'Início';
    tip.append(head);
    for (const s of SERIES) {
      const row = document.createElement('div'); row.className = 't-row';
      const key = document.createElement('i'); key.style.setProperty('--c', s.color);
      const val = document.createElement('b'); val.textContent = money(p[s.key]);
      const name = document.createElement('span'); name.textContent = s.label;
      row.append(key, val, name); tip.append(row);
    }
    if (p.run) {
      const foot = document.createElement('div'); foot.className = 't-row t-foot';
      const val = document.createElement('b'); val.textContent = money(p.now - p.spent, true); val.className = cls(p.now - p.spent);
      const name = document.createElement('span'); name.textContent = 'resultado acumulado';
      foot.append(val, name); tip.append(foot);
    }
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${xx + 12 + tw > W ? xx - 12 - tw : xx + 12}px`;
  };
  const hide = () => { tip.hidden = true; hair.setAttribute('visibility', 'hidden'); };
  const nearest = ev => { const r = el.getBoundingClientRect(); return Math.round((ev.clientX - r.left - m.l) / (pw / (pts.length - 1))); };
  el.addEventListener('pointermove', ev => show(nearest(ev)));
  el.addEventListener('pointerdown', ev => show(nearest(ev)));
  el.addEventListener('pointerleave', hide);
  el.addEventListener('focus', () => show(idx));
  el.addEventListener('blur', hide);
  el.addEventListener('keydown', ev => {
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') { ev.preventDefault(); show(idx + (ev.key === 'ArrowRight' ? 1 : -1)); }
    if (ev.key === 'Escape') hide();
  });
}
function renderChartTable(pts) {
  const rows = pts.filter(p => p.run).map(p => p.run);
  const tot = rows.reduce((a, r) => ({ paid: a.paid + r.paid_usd, open: a.open + (r.value_open ?? 0), now: a.now + r.value_now }), { paid: 0, open: 0, now: 0 });
  const line = (label, date, paid, open, now) => `<td>${label}</td><td>${date}</td><td class="r">${money(paid)}</td><td class="r">${money(open)}</td><td class="r">${money(now)}</td><td class="r ${cls(now - paid)}">${money(now - paid, true)}</td>`;
  patch($('#chart-table'), `<table><thead><tr><th>Abertura</th><th>Data</th><th class="r">Pago</th><th class="r">Na abertura</th><th class="r">Hoje</th><th class="r">Resultado</th></tr></thead>
    <tbody>${rows.map(r => `<tr>${line(`<a href="#/pipelines/${r.id}">#${r.id}</a>`, dt(r.recorded_at || r.created_at), r.paid_usd, r.value_open, r.value_now)}</tr>`).join('')}</tbody>
    <tfoot><tr>${line('Total', '', tot.paid, tot.open, tot.now)}</tr></tfoot></table>`);
}
// redesenha quando a largura real do painel muda (layout inicial, rotação do celular, janela)
let chartWidth = 0;
new ResizeObserver(([entry]) => {
  const w = Math.round(entry.contentRect.width);
  if (Math.abs(w - chartWidth) > 2) { chartWidth = w; if (S.meta) renderChart(); }
}).observe($('#chart'));

// ---- gráficos por data (valor da coleção, visualizações): um ponto por dia, eixo de tempo proporcional ----
const chartWidths = new Map();
function watchWidth(el, render) {  // redesenha quando a largura real do painel muda
  new ResizeObserver(([entry]) => {
    const w = Math.round(entry.contentRect.width);
    if (Math.abs(w - (chartWidths.get(el) || 0)) > 2) { chartWidths.set(el, w); if (S.meta) render(); }
  }).observe(el);
}
const dayLabel = d => d.split('-').reverse().slice(0, 2).join('/');  // "2026-10-08" → "08/10"
const dayTime = d => new Date(`${d}T12:00:00`).getTime();
// rows: [{ day: 'AAAA-MM-DD', v: { chave: valor } }]; display: valor → unidade do eixo; fmt: valor → texto
function timeChart({ wrap, rows, series, display, fmt, axis, what, minStep = 0 }) {
  const width = chartWidths.get(wrap);
  if (!width) return false;
  const W = Math.max(280, width), H = 210, m = { l: 66, r: 92, t: 12, b: 28 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const t0 = dayTime(rows[0].day), span = dayTime(rows.at(-1).day) - t0 || 1;
  const x = i => rows.length === 1 ? m.l + pw / 2 : m.l + (dayTime(rows[i].day) - t0) / span * pw;
  const max = Math.max(...rows.flatMap(r => series.map(s => display(r.v[s.key] ?? 0)))) || 1;
  const st = Math.max(minStep, niceStep(max)), top = Math.ceil(max / st) * st;
  const y = v => m.t + ph - display(v) / top * ph;
  let svg = '';
  for (let v = 0; v <= top + 1e-9; v += st) {
    const yy = (m.t + ph - v / top * ph).toFixed(1);
    svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}"/><text class="tick" x="${m.l - 8}" y="${yy}" text-anchor="end" dominant-baseline="middle">${axis(v, st)}</text>`;
  }
  let lastX = -1e9;  // datas no eixo só onde cabem (a última sempre)
  rows.forEach((r, i) => {
    const xx = x(i);
    if (xx - lastX >= 48 && (i === rows.length - 1 || x(rows.length - 1) - xx >= 48)) { svg += `<text class="tick" x="${xx.toFixed(1)}" y="${H - 8}" text-anchor="middle">${dayLabel(r.day)}</text>`; lastX = xx; }
    else if (i === rows.length - 1) svg += `<text class="tick" x="${xx.toFixed(1)}" y="${H - 8}" text-anchor="middle">${dayLabel(r.day)}</text>`;
  });
  const dots = pw / rows.length >= 14;  // com espaço, um marcador por dia; apertado (celular, muitos dias), só o último
  for (const s of series) {
    const idx = rows.map((r, i) => r.v[s.key] != null ? i : -1).filter(i => i >= 0);
    if (idx.length > 1) svg += `<polyline points="${idx.map(i => `${x(i).toFixed(1)},${y(rows[i].v[s.key]).toFixed(1)}`).join(' ')}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    for (const i of dots ? idx : idx.slice(-1)) svg += `<circle cx="${x(i).toFixed(1)}" cy="${y(rows[i].v[s.key]).toFixed(1)}" r="4" fill="${s.color}" stroke="var(--panel)" stroke-width="2"/>`;
  }
  const placed = [];  // rótulo no fim de cada linha só onde não colide; o resto fica na legenda, tooltip e tabela
  for (const s of series) {
    const i = rows.map(r => r.v[s.key]).findLastIndex(v => v != null);
    if (i < 0) continue;
    const yy = y(rows[i].v[s.key]);
    if (placed.every(l => Math.abs(l - yy) >= 15)) { placed.push(yy); svg += `<text class="endlabel" x="${(x(i) + 10).toFixed(1)}" y="${yy.toFixed(1)}" dominant-baseline="middle">${fmt(rows[i].v[s.key])}</text>`; }
  }
  const last = rows.at(-1);
  const summary = `${what}: ${series.map(s => `${s.label} ${fmt(last.v[s.key])}`).join(', ')} em ${dayLabel(last.day)}.`;
  const changed = patch(wrap, `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" tabindex="0" aria-label="${esc(summary)} Use as setas para ver cada dia.">${svg}
    <line class="xhair" y1="${m.t}" y2="${m.t + ph}" visibility="hidden"/><rect x="${m.l - 12}" y="0" width="${pw + 24}" height="${H}" fill="transparent"/></svg>
    <div class="tip" hidden></div>`);
  if (!changed) return true;
  const el = wrap.querySelector('svg'), tip = wrap.querySelector('.tip'), hair = wrap.querySelector('.xhair');
  let idx = rows.length - 1;
  const show = i => {
    idx = Math.max(0, Math.min(rows.length - 1, i));
    const r = rows[idx], xx = x(idx);
    hair.setAttribute('x1', xx); hair.setAttribute('x2', xx); hair.setAttribute('visibility', 'visible');
    tip.replaceChildren();
    const head = document.createElement('div'); head.className = 't-head';
    head.textContent = new Date(`${r.day}T12:00:00`).toLocaleDateString('pt-BR');
    tip.append(head);
    for (const s of series) {
      const row = document.createElement('div'); row.className = 't-row';
      const key = document.createElement('i'); key.style.setProperty('--c', s.color);
      const val = document.createElement('b'); val.textContent = fmt(r.v[s.key]);
      const name = document.createElement('span'); name.textContent = s.label;
      row.append(key, val, name); tip.append(row);
    }
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${xx + 12 + tw > W ? xx - 12 - tw : xx + 12}px`;
  };
  const hide = () => { tip.hidden = true; hair.setAttribute('visibility', 'hidden'); };
  const nearest = ev => {  // o dia mais perto do ponteiro (os dias não são igualmente espaçados)
    const px = ev.clientX - el.getBoundingClientRect().left;
    return rows.reduce((b, _, i) => Math.abs(x(i) - px) < Math.abs(x(b) - px) ? i : b, 0);
  };
  el.addEventListener('pointermove', ev => show(nearest(ev)));
  el.addEventListener('pointerdown', ev => show(nearest(ev)));
  el.addEventListener('pointerleave', hide);
  el.addEventListener('focus', () => show(idx));
  el.addEventListener('blur', hide);
  el.addEventListener('keydown', ev => {
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') { ev.preventDefault(); show(idx + (ev.key === 'ArrowRight' ? 1 : -1)); }
    if (ev.key === 'Escape') hide();
  });
  return true;
}
const moneyAxis = (v, st) => {  // passo 2,5 → 7,5 e não "8"
  const digits = st < 1 ? 2 : Number.isInteger(st) ? 0 : 1;
  return `${sym(S.cur)} ${v.toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
};
function renderValueChart() {
  const wrap = $('#value-chart');
  const rows = (S.history?.value || []).map(p => ({ day: p.day, cards: p.cards, v: { cards: p.cards_usd, sealed: p.sealed_usd } }));
  const series = [{ key: 'cards', label: 'Cartas', color: 'var(--s-now)' },
    ...(rows.some(r => r.v.sealed) ? [{ key: 'sealed', label: 'Lacrados', color: 'var(--s-open)' }] : [])];
  patch($('#value-legend'), series.length > 1 ? series.map(s => `<li><i style="--c:${s.color}"></i>${s.label}</li>`).join('') : '');
  $('#value-tabledetails').hidden = !rows.length;
  if (!rows.length) {
    patch(wrap, '<p class="muted chartempty">O primeiro ponto aparece quando os preços forem atualizados ou uma pipeline registrar cartas.</p>');
    patch($('#value-table'), '');
    return;
  }
  const rate = () => S.meta.rates[S.cur] ?? 1;
  timeChart({ wrap, rows, series, display: v => v * rate(), fmt: v => money(v), axis: moneyAxis, what: 'Valor da coleção' });
  patch($('#value-table'), `<table><thead><tr><th>Data</th>${series.map(s => `<th class="r">${s.label}</th>`).join('')}<th class="r">Cartas na coleção</th></tr></thead>
    <tbody>${rows.slice().reverse().map(r => `<tr><td>${new Date(`${r.day}T12:00:00`).toLocaleDateString('pt-BR')}</td>${series.map(s => `<td class="r">${money(r.v[s.key])}</td>`).join('')}<td class="r">${fmtInt(r.cards)}</td></tr>`).join('')}</tbody></table>`);
}
watchWidth($('#value-chart'), renderValueChart);
function renderViewsChart() {
  const wrap = $('#views-chart'), data = S.history?.views || [];
  const series = NETS.filter(n => data.some(d => d.views[n.key] != null));  // a rede nomeia a linha: legenda mesmo com uma
  patch($('#views-legend'), series.map(s => `<li><i style="--c:${s.color}"></i>${s.label}</li>`).join(''));
  $('#views-tabledetails').hidden = !data.length;
  if (!data.length) {
    patch(wrap, '<p class="muted chartempty">Os pontos aparecem quando os números dos vídeos forem lidos (↻ Atualizar números, no gráfico acima).</p>');
    patch($('#views-table'), '');
    return;
  }
  const rows = data.map(d => ({ day: d.day, v: d.views }));
  timeChart({ wrap, rows, series, display: v => v, fmt: fmtInt, axis: v => v.toLocaleString('pt-BR'), what: 'Visualizações', minStep: 1 });
  const total = r => series.reduce((t, s) => t + (r.v[s.key] ?? 0), 0);
  patch($('#views-table'), `<table><thead><tr><th>Data</th>${series.map(s => `<th class="r">${s.label}</th>`).join('')}${series.length > 1 ? '<th class="r">Total</th>' : ''}</tr></thead>
    <tbody>${rows.slice().reverse().map(r => `<tr><td>${new Date(`${r.day}T12:00:00`).toLocaleDateString('pt-BR')}</td>${series.map(s => `<td class="r">${fmtInt(r.v[s.key])}</td>`).join('')}${series.length > 1 ? `<td class="r">${fmtInt(total(r))}</td>` : ''}</tr>`).join('')}</tbody></table>`);
}
watchWidth($('#views-chart'), renderViewsChart);
async function loadHistory() {
  try { S.history = await api('/api/history'); } catch (e) { console.warn(e); return; }
  renderValueChart(); renderViewsChart();
}

// ---- tarefas de fundo do Resumo: preços e números das redes (rodam no servidor; a página só acompanha) ----
S.tasks = {};
const taskSeen = {};  // tarefas que esta página viu rodando: só delas mostra a mensagem do fim
let taskTimer = null;
const TASK_UI = {
  prices: { btn: '#refresh-prices', status: '#refresh-status', idle: '↻ Atualizar preços', busy: '↻ Atualizando preços…' },
  social: { btn: '#yt-refresh', status: '#yt-refresh-status', idle: '↻ Atualizar números', busy: '↻ Atualizando números…' },
};
function renderTasks() {
  for (const [name, ui] of Object.entries(TASK_UI)) {
    const t = S.tasks[name] || {}, btn = $(ui.btn), status = $(ui.status);
    btn.disabled = !!t.running;
    btn.textContent = t.running ? ui.busy : ui.idle;
    const text = t.running ? `${t.message || 'Começando'}…` : taskSeen[name] ? t.message || '' : '';
    patch(status, esc(text) + (text && t.job ? ` <a href="#/atualizacoes/${t.job}">ver na aba Pipelines</a>` : ''));
    status.classList.toggle('taskerr', t.ok === false);
  }
}
async function pollTasks() {
  clearTimeout(taskTimer);
  let st;
  try { st = await api('/api/tasks'); } catch { return; }
  const finished = Object.keys(st).filter(k => S.tasks[k]?.running && !st[k].running);
  for (const k of Object.keys(st)) if (st[k].running) taskSeen[k] = true;
  S.tasks = st;
  renderTasks();
  if (finished.includes('prices')) { S.meta = await api('/api/meta'); await refresh(true); }
  else if (finished.length) await refresh();
  if (Object.values(st).some(t => t.running)) taskTimer = setTimeout(pollTasks, 1500);
}
async function startTask(name, query = '') {
  try {
    const st = await api(`/api/tasks/${name}${query}`, { method: 'POST' });
    if (st.running) { S.tasks[name] = st; taskSeen[name] = true; renderTasks(); }
  } catch (e) {
    S.tasks[name] = { ...(S.tasks[name] || {}), running: false, ok: false, message: e.message };
    taskSeen[name] = true;
    renderTasks();
    return;
  }
  pollTasks();
}
$('#refresh-prices').onclick = () => startTask('prices');
$('#yt-refresh').onclick = () => startTask('social');

// ---- lacrados: boosters, caixas e decks fechados, cotados pelo TCGplayer ----
S.sealed = null;
let colTab = store.get('coltab', 'cartas');
function renderColTabs() {
  document.querySelectorAll('[data-coltab]').forEach(b => b.setAttribute('aria-pressed', b.dataset.coltab === colTab));
  $('#col-cartas').hidden = colTab !== 'cartas';
  $('#col-lacrados').hidden = colTab !== 'lacrados';
  $('#sealed-count').textContent = S.sealed?.qty ? `(${S.sealed.qty})` : '';
}
document.querySelectorAll('[data-coltab]').forEach(b => b.onclick = () => {
  colTab = b.dataset.coltab; store.set('coltab', colTab); renderColTabs();
  if (colTab === 'lacrados') loadSealed();
});
async function loadSealed() {
  try { S.sealed = await api('/api/sealed'); } catch (e) { console.warn(e); return; }
  renderSealed(); renderSealedResumo(); renderColTabs();
}
const paidEach = x => x.paid == null ? null : `${sym(x.paid_currency)} ${nf.format(x.paid)}`;
function sealedResult(value, paid) {  // valor de hoje sobre o pago
  if (paid == null || value == null) return '';
  return `<span class="${cls(value - paid)}">${money(value - paid, true)} (${pctTxt(value, paid)})</span>`;
}
function renderSealed() {
  const d = S.sealed;
  if (!d) return;
  patch($('#sealed-sum'), d.items.length ? `<b>${d.qty}</b> ${d.qty === 1 ? 'item' : 'itens'} · <b class="num">${money(d.value_usd)}</b> hoje` +
    (d.paid_usd != null ? ` · pago ${money(d.paid_usd)} · ${sealedResult(d.value_of_paid_usd, d.paid_usd)}` : '') : '');
  patch($('#sealed-list'), d.items.length ? d.items.map(x => `
    <article class="panel sealeditem">
      ${x.image ? `<img src="${esc(x.image)}" alt="" loading="lazy">` : '<span class="noimg"></span>'}
      <div class="sinfo"><b>${esc(x.name)}</b><span class="muted">${esc(setTitle(x.set_code))}</span>
        <span class="muted">${money(x.usd)} cada${paidEach(x) ? ` · pago ${paidEach(x)} cada` : ''}</span></div>
      <div class="sqty" role="group" aria-label="Quantidade de ${esc(x.name)}">
        <button class="iconbtn" data-sealed-qty="${x.id}" data-delta="-1" ${x.qty <= 1 ? 'disabled' : ''} aria-label="Um a menos">−</button>
        <b>${x.qty}</b><button class="iconbtn" data-sealed-qty="${x.id}" data-delta="1" aria-label="Um a mais">+</button></div>
      <div class="sval"><b class="num">${money(x.value_usd)}</b>${sealedResult(x.value_usd, x.paid_total_usd)}</div>
      <div class="sact"><button class="linkbtn" data-sealed-paid="${x.id}">${x.paid == null ? 'Informar o pago' : 'Editar o pago'}</button>
        <button class="linkbtn" data-sealed-del="${x.id}">Remover</button></div>
    </article>`).join('') : '<p class="empty">Nenhum lacrado ainda. Use “+ Adicionar lacrado”.</p>');
}
function renderSealedResumo() {
  const d = S.sealed;
  if (!d) return;
  if (!d.items.length) { patch($('#sealed-resumo'), '<p class="muted chartempty">Nenhum lacrado na coleção. Adicione em Coleção → Lacrados.</p>'); return; }
  const all = d.items.every(x => x.paid != null);  // o resultado compara só o que tem valor pago
  const tiles = [['Valor hoje', money(d.value_usd)], ['Itens', d.qty],
    ...(d.paid_usd != null ? [[all ? 'Pago' : 'Pago (dos informados)', money(d.paid_usd)], ['Resultado', sealedResult(d.value_of_paid_usd, d.paid_usd)]] : [])];
  patch($('#sealed-resumo'), `<div class="sealedtiles">${tiles.map(([k, v]) => `<div><small>${k}</small><b class="num">${v}</b></div>`).join('')}</div>
    <ul class="sealedtop">${d.items.slice(0, 5).map(x => `<li>${x.image ? `<img src="${esc(x.image)}" alt="" loading="lazy">` : '<span class="noimg"></span>'}
      <span><b>${esc(x.name)}</b><small class="muted">${esc(setTitle(x.set_code))} · ${x.qty}×</small></span><b class="num">${money(x.value_usd)}</b></li>`).join('')}</ul>
    ${d.items.length > 5 ? `<p class="muted small">e mais ${d.items.length - 5} na Coleção → Lacrados.</p>` : ''}`);
}
$('#sealed-goto').onclick = () => { colTab = 'lacrados'; store.set('coltab', colTab); };
let sealedProducts = [];
async function loadSealedProducts() {
  const sel = $('#sealed-product');
  sel.disabled = true; sel.innerHTML = '<option>Buscando os produtos…</option>';
  try {
    sealedProducts = await api(`/api/sealed/products?set=${encodeURIComponent($('#sealed-set').value)}`);
    sel.innerHTML = sealedProducts.length ? sealedProducts.map(p => `<option value="${p.product_id}">${esc(p.name)}${p.usd != null ? ` · ${money(p.usd)}` : ''}</option>`).join('')
      : '<option>Nenhum lacrado deste set no TCGplayer</option>';
    sel.disabled = !sealedProducts.length;
  } catch (e) { sealedProducts = []; sel.innerHTML = `<option>${esc(e.message)}</option>`; }
  pickSealedProduct();
}
function pickSealedProduct() {
  const p = sealedProducts.find(x => String(x.product_id) === $('#sealed-product').value);
  $('#sealed-preview').hidden = !p?.image;
  if (p?.image) $('#sealed-preview').src = p.image;
  $('#sealed-price').textContent = p ? (p.usd != null ? `Preço de mercado hoje: ${money(p.usd)} cada` : 'Sem preço de mercado no TCGplayer agora') : '';
  $('#sealed-save').disabled = !p;
}
$('#sealed-new').onclick = () => {
  if (!$('#sealed-set').options.length) {  // o set escolhido da última vez continua
    const sets = [...S.meta.sets].reverse().sort((a, b) => (b.booster ? 1 : 0) - (a.booster ? 1 : 0));
    $('#sealed-set').innerHTML = sets.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join('');
  }
  $('#sealed-form').hidden = false; $('#sealed-error').hidden = true;
  loadSealedProducts();
};
$('#sealed-set').onchange = loadSealedProducts;
$('#sealed-product').onchange = pickSealedProduct;
$('#sealed-cancel').onclick = () => { $('#sealed-form').hidden = true; };
$('#sealed-form').onsubmit = async ev => {
  ev.preventDefault();
  const raw = $('#sealed-paid').value.trim(), paid = raw ? parseMoney(raw) : null;
  if (raw && paid == null) { $('#sealed-error').textContent = 'Valor pago inválido.'; $('#sealed-error').hidden = false; return; }
  try {
    await api('/api/sealed', json({ set_code: $('#sealed-set').value, product_id: +$('#sealed-product').value,
      qty: Math.max(1, parseInt($('#sealed-qty').value, 10) || 1), paid, paid_currency: $('#sealed-cur').value }));
    $('#sealed-form').hidden = true; $('#sealed-qty').value = 1; $('#sealed-paid').value = '';
    await loadSealed();
  } catch (e) { $('#sealed-error').textContent = e.message; $('#sealed-error').hidden = false; }
};
$('#sealed-list').addEventListener('click', async ev => {
  const b = ev.target.closest('[data-sealed-qty], [data-sealed-paid], [data-sealed-del]');
  if (!b) return;
  const id = b.dataset.sealedQty || b.dataset.sealedPaid || b.dataset.sealedDel, x = S.sealed.items.find(i => String(i.id) === id);
  try {
    if (b.dataset.sealedQty) await api(`/api/sealed/${id}`, json({ qty: x.qty + +b.dataset.delta }, 'PATCH'));
    if (b.dataset.sealedPaid) {
      const cur = x.paid_currency || 'BRL';
      const raw = prompt(`Quanto pagou por unidade de “${x.name}” (${sym(cur)})? Deixe vazio para apagar.`, x.paid != null ? nf.format(x.paid) : '');
      if (raw === null) return;
      const paid = raw.trim() ? parseMoney(raw) : null;
      if (raw.trim() && paid == null) { alert('Valor inválido.'); return; }
      await api(`/api/sealed/${id}`, json({ paid, paid_currency: cur }, 'PATCH'));
    }
    if (b.dataset.sealedDel) {
      if (!confirm(`Remover “${x.name}” (${x.qty}×) dos lacrados?`)) return;
      await api(`/api/sealed/${id}`, { method: 'DELETE' });
    }
    await loadSealed();
  } catch (e) { alert(e.message); }
});

// ---- sets: base de coleções, ícones e sincronização ----
S.sets = [];
let syncTimer = null;
async function loadSets() {
  try { S.sets = await api('/api/sets'); } catch (e) { console.warn(e); }
  renderSets();
  pollSync();
}
function renderSets() {
  const fmtDay = d => d ? d.split('-').reverse().join('/') : '—';
  const card = x => `
    <article class="panel setcard${x.booster ? '' : ' minor'}">
      <div class="seticonbox">${x.icon ? `<img src="${esc(x.icon)}" alt="Booster de ${esc(x.name)}" loading="lazy">` : setHex(x.code, 'big')}</div>
      <div class="setinfo">
        <h3>${esc(x.name)}</h3>
        <p class="muted">Set ${esc(x.code)} · ${fmtDay(x.released_at)}${x.booster ? '' : ' · promo/outros'}</p>
        <p class="setstats"><span>${x.cards} cartas no catálogo</span>
          <span>${x.owned ? `${x.owned} na coleção (${x.owned_unique} únicas)` : 'nenhuma na coleção'}</span></p>
        <p class="${x.recognized ? 'ok' : 'muted'}">${x.recognized ? '✓ reconhecido em vídeo' : x.booster ? 'ainda não reconhecido em vídeo: sincronize' : 'não reconhecido em vídeo (sem booster)'}</p>
        <div class="seticonactions">
          <button class="linkbtn" data-icon-upload="${esc(x.code)}">Trocar ícone</button>
          ${x.icon_source === 'manual' ? `<button class="linkbtn" data-icon-reset="${esc(x.code)}">${x.booster ? 'Usar a foto do booster' : 'Voltar ao selo'}</button>` : ''}
          <span class="muted">${x.icon_source === 'manual' ? 'ícone enviado por você' : x.icon ? 'foto do booster (TCGplayer)' : 'selo com o número do set'}</span>
        </div>
      </div>
    </article>`;
  const boosters = S.sets.filter(x => x.booster), others = S.sets.filter(x => !x.booster);
  patch($('#sets-grid'), S.sets.length ? `<h3 class="setsgroup">Sets de booster</h3><div class="setsgrid-inner">${boosters.map(card).join('')}</div>
    <h3 class="setsgroup">Promos e outros</h3><div class="setsgrid-inner">${others.map(card).join('')}</div>` : '<p class="empty">Carregando…</p>');
}
async function pollSync() {
  clearTimeout(syncTimer);
  let st;
  try { st = await api('/api/sets/sync'); } catch { return; }
  const box = $('#sync-status'), btn = $('#sync-sets');
  btn.disabled = st.running;
  btn.textContent = st.running ? 'Sincronizando…' : 'Sincronizar';
  if (st.running || st.finished_at) {
    const last = ((st.log || '').trim().split('\n').filter(Boolean).slice(-1)[0] || '').replace(/\s+/g, ' ').trim();
    box.hidden = false;
    box.className = `syncstatus ${st.running ? 'running' : st.ok ? 'ok' : 'failed'}`;
    box.textContent = st.running ? `Sincronizando: ${last}` : st.ok ? `Sincronizado em ${dt(st.finished_at)}.` : `A sincronização falhou: ${last}`;
  }
  if (st.running) { S.syncSeen = true; syncTimer = setTimeout(pollSync, 1500); return; }
  if (S.syncSeen) {  // acabou de terminar: sets, cartas, preços e ícones podem ter mudado
    S.syncSeen = false;
    S.meta = await api('/api/meta');
    S.sets = await api('/api/sets');
    renderSets();
    await refresh(true);
  }
}
$('#sync-sets').onclick = async () => {
  try { await api('/api/sets/sync', { method: 'POST' }); } catch (e) { alert(e.message); }
  pollSync();
};
let iconTarget = null;
$('#sets-grid').addEventListener('click', async ev => {
  const up = ev.target.closest('[data-icon-upload]'), reset = ev.target.closest('[data-icon-reset]');
  if (up) { iconTarget = up.dataset.iconUpload; $('#icon-file').value = ''; $('#icon-file').click(); }
  if (reset) {
    try { await api(`/api/sets/${encodeURIComponent(reset.dataset.iconReset)}/icon`, { method: 'DELETE' }); } catch (e) { alert(e.message); }
    await reloadIcons();
  }
});
$('#icon-file').onchange = async ev => {
  const f = ev.target.files[0];
  if (!f || !iconTarget) return;
  try {
    await api(`/api/sets/${encodeURIComponent(iconTarget)}/icon`, { method: 'POST', body: f, headers: { 'Content-Type': 'application/octet-stream' } });
  } catch (e) { alert(e.message); }
  await reloadIcons();
};
async function reloadIcons() {
  S.meta = await api('/api/meta');
  S.sets = await api('/api/sets');
  renderAll();
}

// ---- gráfico: valor médio do booster por coleção (pelas cartas, não pelo preço de compra) ----
const SET_SERIES = [  // mesmas cores do gráfico de gasto: a cor segue a medida
  { key: 'open', label: 'Na abertura', color: 'var(--s-open)' },
  { key: 'now', label: 'Hoje', color: 'var(--s-now)' },
];
let setWidth = 0;
function setAverages() {  // cada pacote é um booster; a coleção dele vem da maioria das cartas
  const by = {};
  for (const r of S.runs) for (const p of r.pack_values || []) {
    const g = by[p.set] ||= { set: p.set, packs: [], runs: new Set() };
    g.packs.push(p); g.runs.add(r.id);
  }
  const order = S.meta.sets.map(s => s.code);  // ordem de lançamento
  const avg = (ps, k) => ps.reduce((a, p) => a + p[k], 0) / ps.length;
  return Object.values(by).map(g => ({ ...g, n: g.packs.length, open: avg(g.packs, 'value_open'), now: avg(g.packs, 'value_now'),
    best: Math.max(...g.packs.map(p => p.value_now)) })).sort((a, b) => order.indexOf(a.set) - order.indexOf(b.set));
}
function renderSetChart() {
  const groups = setAverages(), rate = S.meta.rates[S.cur] ?? 1;
  patch($('#set-legend'), groups.length ? SET_SERIES.map(s => `<li><i style="--c:${s.color}"></i>${s.label}</li>`).join('') : '');
  $('#set-tabledetails').hidden = !groups.length;
  if (!groups.length) {
    patch($('#set-chart'), '<p class="muted chartempty">Abra boosters (pipeline de abertura) para ver o valor médio por coleção.</p>');
    patch($('#set-table'), '');
    return;
  }
  if (!setWidth) return;  // ainda sem layout: o ResizeObserver chama de novo
  const W = setWidth, H = 230, m = { l: 66, r: 8, t: 22, b: 46 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
  const max = Math.max(...groups.flatMap(g => [g.open, g.now])) * rate || 1;
  const st = niceStep(max), top = Math.ceil(max / st) * st;
  const digits = st < 1 ? 2 : Number.isInteger(st) ? 0 : 1;
  const tickFmt = v => `${sym(S.cur)} ${v.toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
  const y = v => m.t + ph - v / top * ph;
  const slot = pw / groups.length, bw = Math.min(48, (slot * 0.7 - 2) / 2);
  let svg = '';
  for (let v = 0; v <= top + 1e-9; v += st) {
    svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text class="tick" x="${m.l - 8}" y="${y(v).toFixed(1)}" text-anchor="end" dominant-baseline="middle">${tickFmt(v)}</text>`;
  }
  groups.forEach((g, i) => {
    const cx = m.l + slot * (i + 0.5);
    SET_SERIES.forEach((s, k) => {
      const v = g[s.key] * rate, x0 = cx - (2 * bw + 2) / 2 + k * (bw + 2), y0 = y(v), h = m.t + ph - y0, rr = Math.min(4, h, bw / 2);
      if (h > 0) svg += `<path d="M${x0.toFixed(1)},${(m.t + ph).toFixed(1)}V${(y0 + rr).toFixed(1)}Q${x0.toFixed(1)},${y0.toFixed(1)} ${(x0 + rr).toFixed(1)},${y0.toFixed(1)}H${(x0 + bw - rr).toFixed(1)}Q${(x0 + bw).toFixed(1)},${y0.toFixed(1)} ${(x0 + bw).toFixed(1)},${(y0 + rr).toFixed(1)}V${(m.t + ph).toFixed(1)}Z" fill="${s.color}"/>`;
      if (bw >= 34) svg += `<text class="barlabel" x="${(x0 + bw / 2).toFixed(1)}" y="${(y0 - 6).toFixed(1)}" text-anchor="middle">${nf.format(v)}</text>`;
    });
    const name = setTitle(g.set), room = Math.max(4, Math.floor(slot / 7));
    svg += `<text class="tick" x="${cx.toFixed(1)}" y="${H - 26}" text-anchor="middle">${esc(name.length > room ? name.slice(0, room - 1) + '…' : name)}</text>
      <text class="tick" x="${cx.toFixed(1)}" y="${H - 10}" text-anchor="middle">${g.n} ${g.n === 1 ? 'booster' : 'boosters'}</text>
      <rect class="hit" data-i="${i}" x="${(cx - slot / 2).toFixed(1)}" y="0" width="${slot.toFixed(1)}" height="${H}" fill="transparent"/>`;
  });
  const summary = groups.map(g => `${setTitle(g.set)}: ${money(g.open)} na abertura, ${money(g.now)} hoje (${g.n} ${g.n === 1 ? 'booster' : 'boosters'})`).join('; ');
  const changed = patch($('#set-chart'), `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Valor médio do booster por coleção. ${esc(summary)}">${svg}</svg>
    <div class="tip" id="set-tip" hidden></div>`);
  patch($('#set-table'), `<table><thead><tr><th>Coleção</th><th class="r">Boosters</th><th class="r">Média na abertura</th><th class="r">Média hoje</th><th class="r">Melhor booster (hoje)</th></tr></thead>
    <tbody>${groups.map(g => `<tr><td><span class="setref">${setIcon(g.set, 'seticon small')}${esc(setTitle(g.set))}</span></td><td class="r">${g.n}</td>
      <td class="r">${money(g.open)}</td><td class="r">${money(g.now)} <small class="${cls(g.now - g.open)}">${pctTxt(g.now, g.open)}</small></td><td class="r">${money(g.best)}</td></tr>`).join('')}</tbody></table>`);
  if (!changed) return;
  const tip = $('#set-tip'), wrap = $('#set-chart');
  wrap.querySelectorAll('.hit').forEach(hit => {
    const show = () => {
      const g = groups[+hit.dataset.i];
      tip.replaceChildren();
      const head = document.createElement('div'); head.className = 't-head';
      head.textContent = `${setTitle(g.set)} · ${g.n} ${g.n === 1 ? 'booster' : 'boosters'} em ${g.runs.size} ${g.runs.size === 1 ? 'pipeline' : 'pipelines'}`;
      tip.append(head);
      for (const s of SET_SERIES) {
        const row = document.createElement('div'); row.className = 't-row';
        const key = document.createElement('i'); key.style.setProperty('--c', s.color);
        const val = document.createElement('b'); val.textContent = money(g[s.key]);
        const name = document.createElement('span'); name.textContent = `média ${s.label.toLowerCase()}`;
        row.append(key, val, name); tip.append(row);
      }
      const foot = document.createElement('div'); foot.className = 't-row t-foot';
      const val = document.createElement('b'); val.textContent = money(g.best);
      const name = document.createElement('span'); name.textContent = 'melhor booster, hoje';
      foot.append(val, name); tip.append(foot);
      tip.hidden = false;
      const box = hit.getBoundingClientRect(), host = wrap.getBoundingClientRect();
      const x = box.left - host.left + box.width / 2, tw = tip.offsetWidth;
      tip.style.left = `${Math.max(0, Math.min(host.width - tw, x - tw / 2))}px`;
      tip.style.top = '8px';
    };
    hit.addEventListener('pointerenter', show);
    hit.addEventListener('pointerdown', show);
    hit.addEventListener('pointerleave', () => { tip.hidden = true; });
  });
}
new ResizeObserver(([entry]) => {
  const w = Math.round(entry.contentRect.width);
  if (Math.abs(w - setWidth) > 2) { setWidth = w; if (S.meta) renderSetChart(); }
}).observe($('#set-chart'));

// ---- redes: postar (YouTube, Instagram, TikTok), vincular e acompanhar os números ----
const PRIVACY = { public: 'Público', unlisted: 'Não listado', private: 'Privado' };
const NETS = [  // cor segue a rede (slots validados da paleta); uma escala por gráfico
  { key: 'youtube', label: 'YouTube', color: 'var(--net-youtube)' },
  { key: 'instagram', label: 'Instagram', color: 'var(--net-instagram)' },
  { key: 'tiktok', label: 'TikTok', color: 'var(--net-tiktok)' },
];
const fmtInt = n => n == null ? '—' : n.toLocaleString('pt-BR');
let ytLogin = null, ytLoginTimer = null;
const sp = { key: null, recent: null, recentLoading: false, share: null, editing: {} };  // estado local do painel

function ytConnectBox() {  // configuração e conexão do canal do YouTube (no painel e no Resumo)
  const m = S.meta.youtube || {};
  if (m.connected) return '';
  if (!m.configured) return `<div class="ytbox"><p>Para postar e acompanhar os números, cole o cliente OAuth do Google
      (tipo "TVs e dispositivos de entrada limitada"; o passo a passo está no README).</p>
    <div class="ytclient"><input id="yt-client-id" placeholder="ID do cliente (…apps.googleusercontent.com)" autocomplete="off">
      <input id="yt-client-secret" placeholder="Chave secreta" type="password" autocomplete="off">
      <button class="btn small" data-yt="client">Salvar</button></div></div>`;
  const login = ytLogin && ytLogin.status === 'waiting' ? ytLogin : null;
  if (!login) return `<div class="ytbox"><p>Conecte o seu canal para postar e acompanhar visualizações e reações.</p>
    ${ytLogin?.status === 'error' ? `<p class="error">${esc(ytLogin.error)}</p>` : ''}
    <button class="btn small" data-yt="connect">Conectar o canal do YouTube</button></div>`;
  const until = new Date(login.expires_at * 1000).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  return `<div class="ytbox ytcode"><p>Abra <a href="${esc(login.verification_url)}" target="_blank" rel="noopener">${esc(login.verification_url.replace(/^https?:\/\/(www\.)?/, ''))}</a>
      (no celular ou no computador), entre na conta do canal e digite o código:</p>
    <div class="coderow"><b class="code">${esc(login.user_code)}</b><button class="btn ghost small" data-yt="copy-code">Copiar</button></div>
    <p class="muted small">Esperando a autorização… o código vale até ${until}.</p></div>`;
}
async function ytPollLogin() {
  clearTimeout(ytLoginTimer);
  try {
    const st = await api('/api/youtube');
    ytLogin = st.login;
    S.meta.youtube = { configured: st.configured, connected: st.connected, channel: st.channel };
    if (st.connected) { ytLogin = null; renderAll(); refreshSocialStats(0); return; }
  } catch (e) { console.warn(e); }
  renderAll();
  if (ytLogin?.status === 'waiting') ytLoginTimer = setTimeout(ytPollLogin, 3000);
}
async function ytSync() {  // a situação das redes muda fora da página (token salvo, servidor reiniciado)
  try {
    const [st, ig] = await Promise.all([api('/api/youtube'), api('/api/instagram')]);
    const now = { configured: st.configured, connected: st.connected, channel: st.channel };
    if (JSON.stringify([now, ig]) !== JSON.stringify([S.meta.youtube, S.meta.instagram])) {
      S.meta.youtube = now; S.meta.instagram = ig; renderAll();
    }
    if (st.login?.status === 'waiting' && !ytLoginTimer) { ytLogin = st.login; ytPollLogin(); }
  } catch (e) { console.warn(e); }
}
async function refreshSocialStats(maxAge) {  // em segundo plano: o Resumo mostra que está lendo
  if (!S.runs.some(r => Object.keys(r.posts || {}).length)) return;
  if (!S.meta?.youtube?.connected && !S.meta?.instagram?.connected) return;
  await startTask('social', maxAge ? `?max_age=${maxAge}` : '');
}

function numbersLine(p) {
  const parts = [[p.views, 'visualizações'], [p.likes, 'curtidas'], [p.comments, 'comentários'], [p.shares, 'compartilhamentos'], [p.saves, 'salvamentos']]
    .filter(([v], i) => v != null || i < 3).map(([v, label]) => `<span><b>${fmtInt(v)}</b> ${label}</span>`);
  const when = p.fetched_at ? `${p.manual ? 'informado' : 'lido'} em ${dt(p.fetched_at)}` : 'sem números ainda';
  return `<div class="netnums">${parts.join('')}</div><p class="muted small">${when}</p>`;
}
function netRowHtml(r, n) {
  const post = r.posts?.[n.key], job = r.post_jobs?.[n.key], sched = r.scheduled?.[n.key];
  const verb = $('#sp-when')?.value ? 'Programar' : 'Postar';
  const ytm = S.meta.youtube || {}, ig = S.meta.instagram || {};
  const head = (extra = '') => `<div class="nethead"><i class="netdot" style="--c:${n.color}"></i><b>${n.label}</b>${extra}`;
  const editing = sp.editing[n.key];
  if (job?.status === 'sending') {
    const pct = Math.round((job.progress || 0) * 100);
    return `${head(`<span class="muted small">${esc(job.message || 'Publicando')}… ${pct}%</span></div>`)}<div class="bar"><i style="width:${pct}%"></i></div>`;
  }
  if (editing === 'numbers') return `${head('</div>')}<div class="netform">
      ${['views:Visualizações', 'likes:Curtidas', 'comments:Comentários', 'shares:Compartilhamentos'].map(x => { const [k, label] = x.split(':');
        return `<label>${label}<input type="number" min="0" inputmode="numeric" data-num="${k}" value="${post?.[k] ?? ''}"></label>`; }).join('')}
      <div class="ytactions"><button class="btn small" data-sp="save-numbers" data-net="${n.key}">Salvar números</button>
        <button class="linkbtn" data-sp="cancel" data-net="${n.key}">Cancelar</button></div></div>`;
  if (editing === 'token') return `${head('</div>')}<div class="netform">
      <p class="muted small">Gere o token no painel do app na Meta (Instagram → Configuração da API com login do Instagram),
        com as permissões instagram_business_basic, instagram_business_content_publish e instagram_business_manage_insights.</p>
      <textarea id="ig-token" rows="3" placeholder="Token do Instagram (IG…)" autocomplete="off"></textarea>
      <div class="ytactions"><button class="btn small" data-sp="save-token">Salvar token</button>
        <button class="linkbtn" data-sp="cancel" data-net="instagram">Cancelar</button></div></div>`;
  if (post) {
    const soon = upcoming(post.scheduled_at);  // programado no YouTube: privado até a data
    const locked = n.key === 'youtube' && post.via === 'api' && post.privacy === 'private' && !soon;
    const auto = (n.key === 'youtube' && ytm.connected) || (n.key === 'instagram' && ig.connected && post.post_id);
    const chip = soon ? `<span class="chip" title="${esc(dt(soon))}">Programado · ${dtShort(soon)}</span>`
      : n.key === 'youtube' && post.privacy ? `<span class="chip">${esc(PRIVACY[post.privacy] || post.privacy)}</span>` : '';
    return `${head(`${chip}
        <span class="spacer"></span><a href="${esc(post.url)}" target="_blank" rel="noopener">Abrir ↗</a></div>`)}
      ${soon ? '<p class="muted small">Fica privado até lá; o próprio YouTube publica na data, mesmo com o PC desligado.</p>' : numbersLine(post)}
      ${locked ? '<p class="warnbox">O YouTube travou este vídeo como privado: ele foi enviado por um projeto da API que ainda não passou pela auditoria do Google. Para publicar, poste pelo app e vincule o novo link (desvinculando este).</p>' : ''}
      <div class="ytactions">${auto ? `<button class="linkbtn" data-sp="stats" data-net="${n.key}">Atualizar números</button>`
        : `<button class="linkbtn" data-sp="numbers" data-net="${n.key}">Informar números</button>`}
        <span class="spacer"></span><button class="linkbtn" data-sp="unlink" data-net="${n.key}">Desvincular</button></div>`;
  }
  if (sched && sched.status !== 'failed') {  // Instagram: a API não programa, o cardline publica na hora
    return `${head(`<span class="chip" title="${esc(dt(sched.publish_at))}">Programado · ${dtShort(sched.publish_at)}</span><span class="spacer"></span></div>`)}
      <p class="muted small">O cardline publica nessa hora: deixe o PC ligado e o túnel (ngrok) aberto.</p>
      ${sched.error ? `<p class="warnbox">${esc(sched.error)} Tento de novo a cada 30 s, até 1 h depois da hora marcada.</p>` : ''}
      <div class="ytactions"><span class="spacer"></span><button class="linkbtn" data-sp="unschedule" data-net="${n.key}">Cancelar a programação</button></div>`;
  }
  const failed = sched?.status === 'failed'
    ? `<p class="error">A publicação programada para ${dt(sched.publish_at)} não saiu: ${esc(sched.error)} <button class="linkbtn" data-sp="unschedule" data-net="${n.key}">Descartar</button></p>`
    : job?.status === 'failed' ? `<p class="error">${esc(job.error)}</p>` : '';
  if (n.key === 'youtube') return `${head(`<span class="spacer"></span>${ytm.connected ? `<button class="btn small" data-sp="post" data-net="youtube">${verb} no YouTube</button>` : ''}</div>`)}
    ${failed}${ytm.connected ? `<p class="muted small">${esc(ytm.channel?.title || 'canal conectado')} · pela API, o YouTube deixa o vídeo privado até o seu projeto passar pela auditoria do Google · <button class="linkbtn" data-yt="disconnect">desconectar</button></p>` : ytConnectBox()}`;
  if (n.key === 'instagram') return `${head(`<span class="spacer"></span>${ig.connected ? `<button class="btn small" data-sp="post" data-net="instagram">${verb} no Instagram</button>` : '<button class="btn ghost small" data-sp="token">Conectar o Instagram</button>'}</div>`)}
    ${failed}${ig.connected ? `<p class="muted small">@${esc(ig.username || '')} · o Reel sai público; o Instagram baixa o vídeo pelo túnel · <button class="linkbtn" data-sp="ig-disconnect">desconectar</button></p>` : ''}`;
  return `${head('<span class="spacer"></span></div>')}<p class="muted small">Sem API (o TikTok exige aprovar o app): compartilhe o vídeo, poste pelo app e vincule o link; os números você informa aqui.${verb === 'Programar' ? ' Para programar, use o agendamento do próprio TikTok.' : ''}</p>`;
}
function renderSocial(r) {
  const box = $('#r-yt');
  if (r.kind === 'cadastro' || !r.overlay) { patch(box, ''); sp.key = null; return; }
  if (sp.key !== r.id) {  // o formulário é montado uma vez por pipeline: as atualizações não apagam o que foi digitado
    sp.key = r.id; sp.recent = null; sp.share = null; sp.editing = {};
    const sug = r.post_suggestion || {};
    box.innerHTML = `<div class="panel ytpanel"><h3>Postar nas redes</h3>
      <label class="ytfield">Título (YouTube)<input id="sp-title" maxlength="100" value="${esc(sug.title || '')}"></label>
      <label class="ytfield">Legenda<textarea id="sp-caption" rows="4" maxlength="2200">${esc(sug.caption || '')}</textarea></label>
      <label class="ytfield">Tags (YouTube, separadas por vírgula)<input id="sp-tags" value="${esc((sug.tags || []).join(', '))}"></label>
      <div class="ytrow"><label class="ytfield" id="sp-variant-box">Versão<select id="sp-variant"><option value="narrado">com narração</option><option value="overlay">sem narração</option></select></label>
        <label class="ytfield">Visibilidade no YouTube<select id="sp-privacy">${Object.entries(PRIVACY).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></label></div>
      <div class="ytrow ytwhen"><label class="ytfield">Programar a publicação (opcional)<input type="datetime-local" id="sp-when"></label>
        <button class="linkbtn" data-sp="when-clear" id="sp-when-clear" hidden>Publicar agora</button></div>
      <p class="muted small" id="sp-when-note" hidden>YouTube: o vídeo sobe agora, fica privado e o próprio YouTube publica na data, mesmo com o PC desligado.
        Instagram: a API não programa, então o cardline publica na hora marcada (deixe o PC ligado e o túnel aberto).</p>
      <div class="netrows">${NETS.map(n => `<div class="netrow" id="net-${n.key}"></div>`).join('')}</div>
      <div class="ytactions"><button class="btn ghost" data-sp="share" id="sp-share">Compartilhar o vídeo</button></div>
      <p class="muted small">No celular, abre o compartilhamento com o vídeo (escolha YouTube, Instagram ou TikTok) e copia a legenda;
        no computador, baixa o vídeo. Depois de postar pelo app, vincule o link aqui.</p>
      <div class="ytlink"><b>Já postou?</b> Cole o link do vídeo no YouTube, do Reel ou do TikTok.
        <div class="ytrow"><input id="sp-url" placeholder="https://…"><button class="btn ghost small" data-sp="link">Vincular</button></div>
        <div id="sp-recent"></div></div></div>`;
    box._html = null;
  }
  $('#sp-variant-box').hidden = !r.narrated;
  const when = $('#sp-when');
  when.min = localInput(new Date(Date.now() + 5 * 60e3));
  $('#sp-when-clear').hidden = $('#sp-when-note').hidden = !when.value;
  $('#sp-privacy').disabled = !!when.value;  // programado: privado até a data, público depois
  for (const n of NETS) {
    const el = $(`#net-${n.key}`);
    if (el && !(sp.editing[n.key] && el.querySelector('.netform'))) patch(el, netRowHtml(r, n));
  }
  const share = $('#sp-share');
  if (share) share.textContent = sp.share?.file ? 'Abrir o compartilhamento' : sp.share?.loading ? 'Preparando o vídeo…' : 'Compartilhar o vídeo';
  if (S.meta.youtube?.connected && !r.posts?.youtube && !sp.recent && !sp.recentLoading) loadRecent();
  renderRecent(r);
}
async function loadRecent() {
  sp.recentLoading = true;
  try { sp.recent = await api('/api/youtube/recent'); } catch (e) { sp.recent = { error: e.message }; }
  sp.recentLoading = false;
  if (S.run) renderRecent(S.run);
}
function renderRecent(r) {
  const el = $('#sp-recent');
  if (!el) return;
  if (!S.meta.youtube?.connected || r.posts?.youtube || !sp.recent) { patch(el, ''); return; }
  if (sp.recent.error) { patch(el, `<p class="muted small">${esc(sp.recent.error)}</p>`); return; }
  patch(el, sp.recent.length ? `<p class="muted small">Últimos vídeos do seu canal:</p><ul class="ytrecent">${sp.recent.slice(0, 5).map(v => `<li>
      ${v.thumbnail ? `<img src="${esc(v.thumbnail)}" alt="" loading="lazy">` : '<span class="noimg"></span>'}
      <span><b>${esc(v.title)}</b><small class="muted">${dt(v.published_at)}</small></span>
      <button class="btn ghost small" data-sp="link-id" data-vid="${esc(v.id)}">Vincular</button></li>`).join('')}</ul>` : '');
}
function captionFor(network) {
  const caption = $('#sp-caption').value.trim(), tags = network ? S.run.post_suggestion?.hashtags?.[network] : '#shorts #reels #fyp';
  return tags ? `${caption}\n\n${tags}` : caption;
}
async function spShare(r) {  // pelo app: YouTube, Instagram e TikTok publicam normalmente (sem a trava das APIs)
  const variant = $('#sp-variant').value;
  const src = variant === 'narrado' && r.narrated ? r.narrated : r.overlay;
  const text = captionFor(null);
  try { await navigator.clipboard.writeText(text); } catch { /* sem permissão: o usuário copia à mão */ }
  if (sp.share?.file) {  // segundo toque: o arquivo já está pronto e o compartilhamento abre na hora
    try { await navigator.share({ files: [sp.share.file], title: $('#sp-title').value, text }); }
    catch (e) { if (e.name !== 'AbortError') alert(`Não consegui abrir o compartilhamento: ${e.message}`); }
    return;
  }
  const probe = new File([''], 'video.mp4', { type: 'video/mp4' });
  if (navigator.canShare?.({ files: [probe] })) {  // celular: baixa primeiro (o compartilhamento precisa do toque)
    sp.share = { loading: true };
    renderSocial(r);
    try {
      const blob = await (await fetch(src)).blob();
      sp.share = { file: new File([blob], `cardline-${r.id}.mp4`, { type: 'video/mp4' }) };
    } catch (e) { sp.share = null; alert(`Não consegui baixar o vídeo: ${e.message}`); }
    renderSocial(r);
    return;
  }
  const a = document.createElement('a');  // computador: baixa o vídeo
  a.href = src; a.download = `cardline-${r.id}.mp4`; document.body.append(a); a.click(); a.remove();
  alert('O vídeo está sendo baixado e a legenda foi copiada. Poste pelo YouTube Studio, Instagram ou TikTok e depois vincule o link aqui.');
}
document.addEventListener('click', async ev => {
  const b = ev.target.closest('[data-yt], [data-sp]');
  if (!b) return;
  const action = b.dataset.yt || b.dataset.sp, net = b.dataset.net, r = S.run, id = S.runId;
  try {
    if (b.dataset.yt) {  // conexão do YouTube (painel e Resumo)
      if (action === 'client') {
        S.meta.youtube = await api('/api/youtube/client', json({ client_id: $('#yt-client-id').value, client_secret: $('#yt-client-secret').value }));
        renderAll(); return;
      }
      if (action === 'connect') { b.disabled = true; ytLogin = await api('/api/youtube/connect', { method: 'POST' }); renderAll(); ytPollLogin(); return; }
      if (action === 'copy-code') { try { await navigator.clipboard.writeText(ytLogin.user_code); b.textContent = 'Copiado'; } catch {} return; }
      if (action === 'disconnect') {
        if (!confirm('Desconectar o canal do YouTube? Os vídeos vinculados continuam, mas os números param de atualizar.')) return;
        S.meta.youtube = await api('/api/youtube/disconnect', { method: 'POST' }); sp.recent = null; renderAll(); return;
      }
      return;
    }
    if (action === 'share') { await spShare(r); return; }
    if (action === 'when-clear') { $('#sp-when').value = ''; renderSocial(r); return; }
    if (action === 'token') { sp.editing.instagram = 'token'; renderSocial(r); $('#ig-token')?.focus(); return; }
    if (action === 'numbers') { sp.editing[net] = 'numbers'; renderSocial(r); return; }
    if (action === 'cancel') { delete sp.editing[net]; renderSocial(r); return; }
    if (action === 'save-token') {
      b.disabled = true;
      S.meta.instagram = await api('/api/instagram/token', json({ token: $('#ig-token').value }));
      delete sp.editing.instagram; renderAll(); return;
    }
    if (action === 'ig-disconnect') {
      if (!confirm('Desconectar o Instagram? Os Reels vinculados continuam, mas os números param de atualizar.')) return;
      S.meta.instagram = await api('/api/instagram/disconnect', { method: 'POST' }); renderAll(); return;
    }
    if (action === 'save-numbers') {
      const body = {};
      document.querySelectorAll(`#net-${net} [data-num]`).forEach(i => { if (i.value !== '') body[i.dataset.num] = Number(i.value); });
      b.disabled = true;
      await api(`/api/runs/${id}/posts/${net}`, json(body, 'PATCH'));
      delete sp.editing[net];
    }
    if (action === 'stats') { b.disabled = true; await api(`/api/social/stats?run=${id}`, { method: 'POST' }); }
    if (action === 'post') {
      b.disabled = true;
      const when = $('#sp-when').value;  // horário do aparelho; vai com o fuso
      await api(`/api/runs/${id}/posts/${net}`, json({ title: $('#sp-title').value, caption: $('#sp-caption').value,
        privacy: $('#sp-privacy').value, variant: $('#sp-variant').value,
        tags: $('#sp-tags').value.split(',').map(t => t.trim()).filter(Boolean),
        publish_at: when ? new Date(when).toISOString() : null }));
    }
    if (action === 'unschedule') {
      const s = r.scheduled?.[net], label = NETS.find(n => n.key === net).label;
      if (s?.status !== 'failed' && !confirm(`Cancelar a publicação programada no ${label} para ${dt(s?.publish_at)}?`)) return;
      b.disabled = true;
      await api(`/api/runs/${id}/scheduled/${net}`, { method: 'DELETE' });
    }
    if (action === 'link' || action === 'link-id') {
      const url = action === 'link' ? $('#sp-url').value : `https://youtu.be/${b.dataset.vid}`;
      if (!url.trim()) return;
      b.disabled = true;
      await api(`/api/runs/${id}/posts`, json({ url }, 'PUT'));
      if (action === 'link') $('#sp-url').value = '';
    }
    if (action === 'unlink') {
      if (!confirm(`Desvincular o post do ${NETS.find(n => n.key === net).label} desta abertura? Ele continua na rede.`)) return;
      await api(`/api/runs/${id}/posts/${net}`, { method: 'DELETE' });
      sp.recent = null;
    }
    await refresh();
  } catch (e) { alert(e.message); b.disabled = false; }
});

// ---- gráfico: abertura vs visualizações e reações nas redes ----
let ytWidth = 0;
function renderSocialChart() {
  const card = $('#yt-card');
  if (!card) return;
  const runs = S.runs.filter(r => Object.keys(r.posts || {}).length).sort((a, b) => runTime(a) - runTime(b));
  const nets = NETS.filter(n => runs.some(r => r.posts[n.key]));
  patch($('#yt-connect-resumo'), S.meta.youtube?.connected || !S.meta.youtube?.configured ? '' : ytConnectBox());
  patch($('#yt-legend'), nets.length ? nets.map(n => `<li><i style="--c:${n.color}"></i>${n.label}</li>`).join('') : '');
  $('#yt-tabledetails').hidden = !runs.length;
  // "Atualizar números": lê de novo todos os vídeos que a API alcança (rede conectada e post com ID na rede)
  const canRead = S.meta.youtube?.connected || S.meta.instagram?.connected;
  $('#yt-read').hidden = !runs.length || !canRead;
  if (runs.length && canRead) {
    const posts = runs.flatMap(r => Object.entries(r.posts));
    const reads = posts.filter(([n, p]) => p.post_id && S.meta[n]?.connected).map(([, p]) => p.fetched_at);
    const oldest = reads.every(Boolean) && reads.length ? reads.reduce((a, b) => new Date(a) < new Date(b) ? a : b) : null;
    patch($('#yt-read-when'), (oldest ? `Números lidos em ${dt(oldest)}.` : reads.length ? 'Há vídeos sem números lidos.' : '') +
      (posts.some(([n]) => n === 'tiktok') ? ' Os do TikTok são informados à mão, em cada pipeline.' : ''));
  }
  if (!runs.length) {
    patch($('#yt-charts'), '<p class="muted chartempty">Poste ou vincule o vídeo de uma abertura (na página da pipeline) para acompanhar as visualizações e reações aqui.</p>');
    patch($('#yt-table'), '');
    return;
  }
  if (!ytWidth) return;
  const react = p => p ? (p.likes ?? 0) + (p.comments ?? 0) : null;
  const multiples = [{ title: 'Visualizações', value: p => p?.views }, { title: 'Reações (curtidas + comentários)', value: react }];
  const wide = ytWidth >= 640, W = wide ? Math.floor((ytWidth - 24) / 2) : ytWidth, H = 200;
  const m = { l: 44, r: 8, t: 10, b: 44 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
  const result = r => r.paid_usd ? (r.value_now - r.paid_usd) / r.paid_usd : null;
  const html = multiples.map(mp => {
    const max = Math.max(1, ...runs.flatMap(r => nets.map(n => mp.value(r.posts[n.key]) ?? 0)));
    const st = Math.max(1, Math.ceil(niceStep(max))), top = Math.ceil(max / st) * st;
    const slot = pw / runs.length, bw = Math.min(40, (slot * 0.75 - 2 * (nets.length - 1)) / nets.length);
    const y = v => m.t + ph - v / top * ph;
    let svg = '';
    for (let v = 0; v <= top; v += st) svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text class="tick" x="${m.l - 6}" y="${y(v).toFixed(1)}" text-anchor="end" dominant-baseline="middle">${fmtInt(v)}</text>`;
    runs.forEach((r, i) => {
      const cx = m.l + slot * (i + 0.5), group = bw * nets.length + 2 * (nets.length - 1);
      nets.forEach((n, k) => {
        const v = mp.value(r.posts[n.key]) ?? 0, x0 = cx - group / 2 + k * (bw + 2), y0 = y(v), h = m.t + ph - y0, rr = Math.min(4, h, bw / 2);
        if (h > 0) svg += `<path d="M${x0.toFixed(1)},${(m.t + ph).toFixed(1)}V${(y0 + rr).toFixed(1)}Q${x0.toFixed(1)},${y0.toFixed(1)} ${(x0 + rr).toFixed(1)},${y0.toFixed(1)}H${(x0 + bw - rr).toFixed(1)}Q${(x0 + bw).toFixed(1)},${y0.toFixed(1)} ${(x0 + bw).toFixed(1)},${(y0 + rr).toFixed(1)}V${(m.t + ph).toFixed(1)}Z" fill="${n.color}"/>`;
      });
      const res = result(r);
      svg += `<text class="tick" x="${cx.toFixed(1)}" y="${H - 26}" text-anchor="middle">#${r.id}</text>`;
      if (res != null) svg += `<text class="tick ${cls(res)}" x="${cx.toFixed(1)}" y="${H - 10}" text-anchor="middle">${res >= 0 ? '+' : '−'}${Math.abs(res * 100).toFixed(0)}%</text>`;
      svg += `<rect class="hit" data-i="${i}" x="${(cx - slot / 2).toFixed(1)}" y="0" width="${slot.toFixed(1)}" height="${H}" fill="transparent"/>`;
    });
    const label = `${mp.title} por abertura: ${runs.map(r => `#${r.id}: ${nets.map(n => `${n.label} ${fmtInt(mp.value(r.posts[n.key]))}`).join(', ')}`).join('; ')}`;
    return `<figure class="ytmultiple"><figcaption>${mp.title}</figcaption><svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">${svg}</svg></figure>`;
  }).join('');
  const changed = patch($('#yt-charts'), html + '<div class="tip" id="yt-tip" hidden></div>');
  patch($('#yt-table'), `<table><thead><tr><th>Abertura</th><th>Rede</th><th class="r">Resultado</th><th class="r">Visualizações</th><th class="r">Curtidas</th><th class="r">Comentários</th><th class="r">Compart.</th></tr></thead>
    <tbody>${runs.flatMap(r => NETS.filter(n => r.posts[n.key]).map(n => { const p = r.posts[n.key], res = result(r); return `<tr>
      <td><a href="#/pipelines/${r.id}">#${r.id}</a></td><td><a href="${esc(p.url)}" target="_blank" rel="noopener">${n.label}</a>${p.manual ? ' <small class="muted">(informado)</small>' : ''}</td>
      <td class="r ${res == null ? '' : cls(res)}">${res == null ? '—' : `${res >= 0 ? '+' : '−'}${Math.abs(res * 100).toFixed(0)}%`}</td>
      <td class="r">${fmtInt(p.views)}</td><td class="r">${fmtInt(p.likes)}</td><td class="r">${fmtInt(p.comments)}</td><td class="r">${fmtInt(p.shares)}</td></tr>`; })).join('')}</tbody></table>`);
  if (!changed) return;
  const tip = $('#yt-tip'), wrap = $('#yt-charts');
  wrap.querySelectorAll('.hit').forEach(hit => {
    const show = () => {
      const r = runs[+hit.dataset.i], res = result(r);
      tip.replaceChildren();
      const head = document.createElement('div'); head.className = 't-head';
      head.textContent = `Pipeline #${r.id} · ${dt(r.recorded_at || r.created_at)}`;
      tip.append(head);
      for (const n of nets) {
        const p = r.posts[n.key];
        const row = document.createElement('div'); row.className = 't-row';
        const key = document.createElement('i'); key.style.setProperty('--c', n.color);
        const val = document.createElement('b'); val.textContent = p ? fmtInt(p.views) : '—';
        const name = document.createElement('span');
        name.textContent = p ? `${n.label}: visualizações · ${fmtInt(p.likes)} curtidas · ${fmtInt(p.comments)} coment.` : `${n.label}: sem post`;
        row.append(key, val, name); tip.append(row);
      }
      if (res != null) {
        const foot = document.createElement('div'); foot.className = 't-row t-foot';
        const val = document.createElement('b'); val.className = cls(res);
        val.textContent = `${money(r.value_now - r.paid_usd, true)} (${res >= 0 ? '+' : '−'}${Math.abs(res * 100).toFixed(0)}%)`;
        const name = document.createElement('span'); name.textContent = 'resultado do booster';
        foot.append(val, name); tip.append(foot);
      }
      tip.hidden = false;
      const box = hit.getBoundingClientRect(), host = wrap.getBoundingClientRect();
      const x = box.left - host.left + box.width / 2, tw = tip.offsetWidth;
      tip.style.left = `${Math.max(0, Math.min(host.width - tw, x - tw / 2))}px`;
      tip.style.top = `${box.top - host.top + 8}px`;
    };
    hit.addEventListener('pointerenter', show);
    hit.addEventListener('pointerdown', show);
    hit.addEventListener('pointerleave', () => { tip.hidden = true; });
  });
}
new ResizeObserver(([entry]) => {
  const w = Math.round(entry.contentRect.width);
  if (Math.abs(w - ytWidth) > 2) { ytWidth = w; if (S.meta) renderSocialChart(); }
}).observe($('#yt-charts'));

// ---- rotas, carga e polling ----
function currentView() {
  const h = location.hash || '#/resumo';  // a página abre no Resumo
  if (/^#\/pipelines\/\d+/.test(h)) return 'run';
  if (/^#\/atualizacoes\/\d+/.test(h)) return 'job';
  if (h.startsWith('#/pipelines')) return 'pipelines';
  if (h.startsWith('#/nova')) return 'nova';
  if (h.startsWith('#/colecao')) return 'colecao';
  if (h.startsWith('#/sets')) return 'sets';
  return 'resumo';
}
async function route() {
  const view = currentView();
  for (const v of ['resumo', 'colecao', 'pipelines', 'run', 'job', 'nova', 'sets']) $('#view-' + v).hidden = v !== view;
  if (view === 'sets') loadSets();
  ytSync();
  if (view === 'resumo') { refreshSocialStats(1800); pollTasks(); loadHistory(); }
  if (view === 'resumo' || view === 'colecao') loadSealed();
  document.querySelectorAll('nav.tabs a').forEach(a => {
    const on = a.dataset.tab === view || (a.dataset.tab === 'pipelines' && ['run', 'job', 'nova'].includes(view));
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  const m = location.hash.match(/^#\/pipelines\/(\d+)/);
  const runId = m ? +m[1] : null;
  if (runId !== S.runId) { S.runId = runId; S.run = null; editingPaid = false; if (runId) runSkeleton(); }
  const jm = location.hash.match(/^#\/atualizacoes\/(\d+)/);
  S.jobId = jm ? +jm[1] : null;
  if (S.jobId) { try { S.job = await api(`/api/jobs/${S.jobId}`); } catch (e) { patch($('#view-job'), `<p class="empty">${esc(e.message)}</p>`); return; } }
  if (view === 'nova') prepareNew();
  if (runId) { try { S.run = await api(`/api/runs/${runId}`); } catch (e) { patch($('#view-run'), `<p class="empty">${esc(e.message)}</p>`); return; } }
  renderAll();
}
window.addEventListener('hashchange', route);

function renderAll() {
  buildEntries();
  renderCurrency(); renderStats(); renderChart(); renderValueChart(); renderSetChart(); renderSocialChart(); renderViewsChart();
  renderSealed(); renderSealedResumo(); renderColTabs();
  const view = currentView();
  if (view === 'colecao') renderCollection();
  if (view === 'pipelines') renderRuns();
  if (view === 'run') renderRun();
  if (view === 'job') renderJob();
  if (view === 'sets') renderSets();
}

let timer = null, prevActive = new Set();
async function refresh(collectionToo = false) {
  clearTimeout(timer);
  try {
    S.runs = await api('/api/runs');
    S.jobs = await api('/api/jobs');
    if (S.jobId) S.job = await api(`/api/jobs/${S.jobId}`);
    const now = new Set(S.runs.filter(active).map(r => r.id));
    const finished = [...prevActive].some(id => !now.has(id));
    prevActive = now;
    if (collectionToo || finished) {
      S.col = await api('/api/collection');
      buildEntries(); setupFilters();
    }
    if (S.runId) S.run = await api(`/api/runs/${S.runId}`);
    if (currentView() === 'resumo') S.history = await api('/api/history');
    if (['resumo', 'colecao'].includes(currentView())) S.sealed = await api('/api/sealed');
    renderAll();
  } catch (e) { console.warn(e); }
  timer = setTimeout(refresh, prevActive.size || S.jobs.some(j => j.status === 'running') ? 1500 : 8000);
}

(async () => {
  S.meta = await api('/api/meta');
  RAR = Object.fromEntries(S.meta.rarities.map(([k, label, color], rank) => [k, { label, color, rank }]));
  INK = Object.fromEntries(S.meta.inks.map(([k, label, color]) => [k, { label, color }]));
  S.cur = store.get('currency', S.meta.currency);
  if (!(S.cur in S.meta.rates)) S.cur = 'USD';
  S.col = await api('/api/collection');
  buildEntries(); setupFilters();
  await route();
  await refresh();
  if (currentView() === 'resumo') refreshSocialStats(1800);  // a lista de pipelines só chega agora
})();
