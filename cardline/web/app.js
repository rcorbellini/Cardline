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
function dur(a, b) {
  if (!a) return '';
  const s = Math.max(0, Math.round(((b ? new Date(b) : new Date()) - new Date(a)) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}min ${s % 60}s`;
}
const TRASH = '<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
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
  const paidRuns = S.runs.filter(r => r.paid_usd != null && r.value_now != null);
  const invested = paidRuns.reduce((s, r) => s + r.paid_usd, 0);
  const result = paidRuns.reduce((s, r) => s + r.value_now, 0) - invested;
  patch($('#stats'), [
    ['main', 'Valor da coleção', money(total)], ['', 'Cartas', qty], ['', 'Únicas', new Set(entries.map(e => e.card)).size],
    ['', 'Foils', entries.filter(e => e.foil).reduce((s, e) => s + e.qty, 0)],
    ['', 'Investido em boosters', paidRuns.length ? money(invested) : '—'],
    ['', 'Resultado', paidRuns.length ? `<span class="${cls(result)}">${money(result, true)}</span>` : '—'],
  ].map(([c, k, v]) => `<div class="stat ${c}"><small>${k}</small><b class="num">${v}</b></div>`).join(''));
  const brl = S.meta.rates.BRL;
  $('#updated').textContent = `Preços de mercado TCGplayer via Lorcast, atualizados em ${dt(S.meta.prices_updated_at)}` +
    (brl ? ` · US$ 1 = R$ ${nf.format(brl)} (${S.meta.rate_day.split('-').reverse().join('/')})` : '') + '.';
  const n = S.runs.filter(active).length;
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
function setupFilters() {
  const sets = [...new Set(entries.map(e => e.c.set))].sort((a, b) => (+a || 999) - (+b || 999) || a.localeCompare(b));
  const setName = Object.fromEntries(S.meta.sets.map(s => [s.code, s.name]));
  $('#set').innerHTML = '<option value="">Todos os sets</option>' + sets.map(s => `<option value="${esc(s)}">${esc(s)} · ${esc(setName[s] || s)}</option>`).join('');
  const rars = S.meta.rarities.map(r => r[0]).filter(r => entries.some(e => e.c.rarity === r));
  $('#rarity').innerHTML = '<option value="">Todas as raridades</option>' + rars.map(r => `<option value="${r}">${esc(rar(r).label)}</option>`).join('');
  $('#inks').innerHTML = S.meta.inks.map(([k, label, color]) => `<button class="chip" data-ink="${k}" aria-pressed="false" style="--c:${color}"><i></i>${label}</button>`).join('');
  document.querySelectorAll('[data-ink]').forEach(b => b.onclick = () => {
    const k = b.dataset.ink; F.inks = F.inks.includes(k) ? F.inks.filter(x => x !== k) : [...F.inks, k]; renderCollection();
  });
}
for (const k of ['q', 'set', 'rarity', 'foil', 'min', 'sort']) $('#' + k).addEventListener('input', e => { F[k] = e.target.value; renderCollection(); });
document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => { F.view = b.dataset.view; renderCollection(); });
$('#clear').onclick = () => { F = { ...defaults, view: F.view }; renderCollection(); };

function renderCollection() {
  store.set('filters', F);
  for (const k of ['q', 'set', 'rarity', 'foil', 'min', 'sort']) if ($('#' + k).value !== F[k]) $('#' + k).value = F[k];
  document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', b.dataset.view === F.view));
  document.querySelectorAll('[data-ink]').forEach(b => b.setAttribute('aria-pressed', F.inks.includes(b.dataset.ink)));
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
    const where = x.run
      ? `<a href="#/pipelines/${x.run}" onclick="document.getElementById('dlg').close()">Pipeline #${x.run}</a> · ${dt(run?.recorded_at || run?.created_at)} · booster ${x.pack}, carta ${x.slot} (${nf.format(x.t)}s)`
      : `Adicionada à mão em ${dt(x.added)}`;
    const d = x.paid != null && e.price != null ? e.price - x.paid : 0;
    return `<li>${where}<br><span class="muted">Na abertura: ${money(x.paid)} → hoje ${money(e.price)}</span>${x.paid ? ` <span class="${cls(d)}">${pctTxt(e.price, x.paid)}</span>` : ''}</li>`;
  }).join('');
  $('#dlgbody').innerHTML = `
    <button class="iconbtn close" onclick="document.getElementById('dlg').close()" aria-label="Fechar">✕</button>
    <div class="body">
      <div class="art${e.foil ? ' foil' : ''}">${img(c, true)}</div>
      <div>
        <h2>${esc(c.name)}</h2><p class="sub">${esc(c.version || '')}</p>
        <dl class="facts">
          <dt>Set</dt><dd>${esc(S.meta.sets.find(s => s.code === c.set)?.name || c.set)} · nº ${esc(c.number)}</dd>
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
  $('#dlg').showModal();
}
$('#dlg').addEventListener('click', ev => { if (ev.target.id === 'dlg') ev.target.close(); });

// ---- lista de pipelines ----
function statusChip(r) { return `<span class="status ${r.status}">${STATUS[r.status] || r.status}</span>`; }
function resultHtml(r) {
  if (r.paid_usd == null || r.value_now == null) return '';
  const d = r.value_now - r.paid_usd;
  return `<span><small>Resultado</small><b class="${cls(d)}">${money(d, true)} (${pctTxt(r.value_now, r.paid_usd)})</b></span>`;
}
function progressHtml(r) {
  if (!active(r)) return '';
  const label = r.status === 'queued' ? 'Na fila' : `${S.meta.steps.find(s => s.name === r.step)?.label || ''} · ${r.message || ''}`;
  const p = r.status === 'queued' ? 0 : Math.round((r.progress ?? 0) * 100);
  return `<div class="bar"><i style="width:${p}%"></i></div><div class="muted" style="font-size:13px">${esc(label)}</div>`;
}
function renderRuns() {
  const head = `<div class="toolbar"><h2>Pipelines</h2><a class="btn" href="#/nova">+ Nova pipeline</a></div>`;
  if (!S.runs.length) {
    patch($('#view-pipelines'), head + '<p class="empty">Nenhuma pipeline ainda. Envie o vídeo de uma abertura em <a href="#/nova">Nova pipeline</a>.</p>');
    return;
  }
  patch($('#view-pipelines'), head + '<div class="runs">' + S.runs.map(r => `
    <a class="panel runcard" href="#/pipelines/${r.id}">
      <div class="runhead"><h3>#${r.id}</h3><span class="when">${dt(r.recorded_at || r.created_at)} · ${esc(r.video_name)}</span>
        <span class="spacer"></span>${statusChip(r)}</div>
      ${progressHtml(r)}
      ${r.thumbs.length ? `<div class="thumbs">${r.thumbs.map(t => `<img loading="lazy" src="${esc(t)}" alt="">`).join('')}</div>` : ''}
      <div class="metrics">
        <span><small>Cartas</small><b>${r.n_cards || '—'}</b></span>
        <span><small>Pago</small><b>${r.paid != null ? `${sym(r.paid_currency)} ${nf.format(r.paid)}` : '—'}</b></span>
        <span><small>Valor hoje</small><b class="num">${money(r.value_now)}</b></span>
        ${resultHtml(r)}
      </div>
    </a>`).join('') + '</div>');
}

// ---- detalhe da pipeline ----
function runSkeleton() {
  patch($('#view-run'), `
    <a class="back" href="#/pipelines">← Pipelines</a>
    <div id="r-title"></div><p class="runsub" id="r-sub"></p>
    <div class="kpis"><div class="panel kpi" id="r-paid"></div><div id="r-kpis" style="display:contents"></div></div>
    <p class="error" id="r-error"></p>
    <div class="cols">
      <div><div class="sectionhead"><h3>Cartas</h3><span class="muted" id="r-cards-sub"></span></div>
        <ul class="pulls" id="r-cards"></ul><div id="r-edits"></div></div>
      <div class="side"><div class="panel"><ul class="steps" id="r-steps"></ul></div><div id="r-video"></div></div>
    </div>
    <details class="log"><summary>Log da execução</summary><pre id="r-log"></pre></details>`);
}
let editingPaid = false;
function renderRun() {
  const r = S.run;
  if (!r || r.id !== S.runId) return;
  const steps = S.meta.steps;
  const canRun = !active(r);
  const resumeLabel = steps.find(s => s.name === r.resume_from)?.label;
  patch($('#r-title'), `<div class="runtitle"><h2>Pipeline #${r.id}</h2>${statusChip(r)}<span class="spacer"></span>
    ${canRun && r.resume_from && r.status !== 'done' ? `<button class="btn" data-action="resume">${r.status === 'stale' ? 'Reprocessar com as edições' : `Continuar de “${esc(resumeLabel)}”`}</button>` : ''}
    ${canRun ? `<span class="rerun"><select id="r-from" aria-label="Passo inicial">${steps.map(s => `<option value="${s.name}">${esc(s.label)}</option>`).join('')}</select>
      <button class="btn ghost" data-action="rerun">Rodar de novo daqui</button></span>
      <button class="btn danger" data-action="delete">Excluir</button>` : ''}</div>`);
  patch($('#r-sub'), `${esc(r.video_name)} · gravado ${dt(r.recorded_at || r.created_at)}${r.sets ? ` · set ${esc(r.sets.join(', '))}` : ''}` +
    `${r.n_cards ? ` · ${r.n_cards} cartas · ${r.packs} ${r.packs === 1 ? 'booster' : 'boosters'}` : ''}`);

  if (!editingPaid) {
    patch($('#r-paid'), `<small>Valor pago</small><b class="num">${r.paid != null ? `${sym(r.paid_currency)} ${nf.format(r.paid)}` : '—'}</b>
      ${r.paid != null && r.paid_currency !== S.cur ? `<div class="muted" style="font-size:13px">≈ ${money(r.paid_usd)}</div>` : ''}
      <div><button class="linkbtn" data-action="edit-paid">${r.paid != null ? 'editar' : 'informar valor pago'}</button></div>`);
  }
  const d = r.paid_usd != null && r.value_now != null ? r.value_now - r.paid_usd : null;
  patch($('#r-kpis'), `
    <div class="panel kpi"><small>Cartas na abertura</small><b class="num">${money(r.value_open)}</b></div>
    <div class="panel kpi"><small>Valor hoje</small><b class="num">${money(r.value_now)}</b>
      ${r.value_open ? `<div class="${cls(r.value_now - r.value_open)}" style="font-size:13px">${pctTxt(r.value_now, r.value_open)} desde a abertura</div>` : ''}</div>
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
  const curControl = r.options?.overlay === false ? '' : `<div class="vidbar"><span>Moeda do vídeo com overlay</span>
    <div class="seg" role="group" aria-label="Moeda do vídeo com overlay">${['USD', 'BRL'].filter(c => c in S.meta.rates).map(c =>
      `<button data-action="video-currency" data-cur="${c}" aria-pressed="${c === videoCur}" ${active(r) ? 'disabled' : ''}>${sym(c)}</button>`).join('')}</div></div>`;
  patch($('#r-video'), curControl + (r.overlay && overlayStep.status !== 'running'
    ? `<video class="player" controls preload="metadata" src="${esc(r.overlay)}?v=${encodeURIComponent(overlayStep.finished_at || '')}"
         ${r.poster ? `poster="${esc(r.poster)}?v=${encodeURIComponent(overlayStep.finished_at || '')}"` : ''}></video>
       <p class="muted" style="font-size:13px"><a href="${esc(r.overlay)}" download="pipeline-${r.id}-overlay.mp4">Baixar vídeo com overlay</a></p>` : ''));

  const cards = r.cards || [];
  const editable = !active(r);
  const best = cards.reduce((b, x) => (x.price_now ?? 0) > (b?.price_now ?? -1) ? x : b, null);
  patch($('#r-cards-sub'), cards.length ? 'recorte do vídeo ao lado da imagem oficial' : '');
  patch($('#r-cards'), cards.length ? cards.map(x => {
    const c = r.card_info[x.card], rr = rar(c.rarity);
    const check = x.check?.status === 'divergente'
      ? `<span class="warn" title="${esc(x.check.model)} leu: ${esc(x.check.name)} · ${esc(x.check.number)}">⚠ conferir</span>` : '';
    return `<li class="pull${x === best ? ' best' : ''}" data-card="${esc(x.card)}" data-foil="${x.foil}" title="${x === best ? 'Melhor carta' : ''}">
      <div class="lead"><span class="n">#${x.n}</span>
        ${editable ? `<button class="trash" data-action="remove-card" data-uid="${esc(x.uid)}" title="Excluir carta" aria-label="Excluir a carta #${x.n}, ${esc(c.name)}">${TRASH}</button>` : ''}</div>
      ${x.crop ? `<img loading="lazy" src="${esc(x.crop)}" alt="Recorte do vídeo">` : '<span class="noimg">sem recorte</span>'}
      ${img(c)}
      <div class="info"><div><b>${esc(c.name)}</b></div><div class="ver">${esc(c.version || ' ')}</div>
        <div class="line"><span class="rar" style="--c:${rr.color}"><i></i>${esc(rr.label)}</span>${x.foil ? '<span class="foilpill">FOIL</span>' : ''}
          <span class="price num" style="color:var(--text)">${money(x.price_now ?? x.price_open)}</span>${x.manual ? '<span>· corrigida</span>' : ''}${check}</div>
        <div class="line" title="${x.inliers ? `${x.inliers} pontos casados com a imagem oficial` : 'inserida manualmente'}">${esc(c.set)}/${esc(c.number)} · ${x.t != null ? x.t.toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + 's' : '—'}</div>
      </div></li>`;
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
        : pending ? 'Edições pendentes: preços, coleção e vídeo só mudam depois de reprocessar.'
        : 'Use a lixeira para tirar uma carta identificada errada ou duplicada, depois reprocesse.'}</p>
      <button class="btn" data-action="resume" ${pending && editable ? '' : 'disabled'}>Reprocessar com as edições</button>
    </div>` : '');

  const log = $('#r-log');
  if (log.textContent !== (r.log || '')) {
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.textContent = r.log || '';
    if (atEnd) log.scrollTop = log.scrollHeight;
  }
}

$('#view-run').addEventListener('click', async ev => {
  const target = ev.target.closest('[data-action]');
  const action = target?.dataset.action;
  if (!action) {
    const pull = ev.target.closest('.pull');
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
    if (action === 'resume') await api(`/api/runs/${id}/rerun`, json({ from_step: null }));
    if (action === 'rerun') await api(`/api/runs/${id}/rerun`, json({ from_step: $('#r-from').value }));
    if (action === 'delete') {
      if (!confirm(`Excluir a pipeline #${id}? As cartas que ela registrou saem da coleção e a pasta runs/${id} é apagada.`)) return;
      await api(`/api/runs/${id}`, { method: 'DELETE' });
      location.hash = '#/pipelines';
      await refresh(true);
      return;
    }
    if (action === 'edit-paid') { editPaid(); return; }
    if (action === 'video-currency') {
      if (target.getAttribute('aria-pressed') === 'true') return;
      await api(`/api/runs/${id}`, json({ currency: target.dataset.cur }, 'PATCH'));
    }
    await refresh();
  } catch (e) { alert(e.message); }
});
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
  $('#new-currency').value = S.meta.currency;
  const v = S.meta.verify;
  $('#opt-verify').disabled = !v.available;
  $('#opt-verify').checked = v.available && v.default;
  $('#verify-label').textContent = v.available
    ? `Conferir cada carta com IA local (${v.model}, ~1 s por carta)` : `Conferir com IA local (Ollama não encontrado)`;
}
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
  const params = new URLSearchParams({ filename: S.file.name, paid_currency: $('#paid-currency').value,
    overlay: $('#opt-overlay').checked, verify: $('#opt-verify').checked, currency: $('#new-currency').value });
  if (paid != null) params.set('paid', paid);
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
  const runs = S.runs.filter(r => r.paid_usd != null && r.value_now != null && r.n_cards).sort((a, b) => runTime(a) - runTime(b));
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

// ---- atualizar preços de hoje (o valor na abertura de cada carta não muda) ----
$('#refresh-prices').onclick = async () => {
  const btn = $('#refresh-prices'), status = $('#refresh-status');
  btn.disabled = true;
  status.textContent = 'Buscando os preços de hoje…';
  try {
    const r = await api('/api/prices/refresh', { method: 'POST' });
    S.meta = await api('/api/meta');
    await refresh(true);
    status.textContent = r.sets.length
      ? 'Preços de hoje atualizados. O valor de cada carta na abertura continua o mesmo.' : 'Ainda não há cartas na coleção.';
  } catch (e) {
    status.textContent = e.message;
  }
  btn.disabled = false;
};

// ---- rotas, carga e polling ----
function currentView() {
  const h = location.hash || '#/colecao';
  if (/^#\/pipelines\/\d+/.test(h)) return 'run';
  if (h.startsWith('#/pipelines')) return 'pipelines';
  if (h.startsWith('#/nova')) return 'nova';
  return 'colecao';
}
async function route() {
  const view = currentView();
  for (const v of ['colecao', 'pipelines', 'run', 'nova']) $('#view-' + v).hidden = v !== view;
  document.querySelectorAll('nav.tabs a').forEach(a => {
    const on = a.dataset.tab === view || (a.dataset.tab === 'pipelines' && (view === 'run' || view === 'nova'));
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  const m = location.hash.match(/^#\/pipelines\/(\d+)/);
  const runId = m ? +m[1] : null;
  if (runId !== S.runId) { S.runId = runId; S.run = null; editingPaid = false; if (runId) runSkeleton(); }
  if (view === 'nova') prepareNew();
  if (runId) { try { S.run = await api(`/api/runs/${runId}`); } catch (e) { patch($('#view-run'), `<p class="empty">${esc(e.message)}</p>`); return; } }
  renderAll();
}
window.addEventListener('hashchange', route);

function renderAll() {
  buildEntries();
  renderCurrency(); renderStats(); renderChart();
  const view = currentView();
  if (view === 'colecao') renderCollection();
  if (view === 'pipelines') renderRuns();
  if (view === 'run') renderRun();
}

let timer = null, prevActive = new Set();
async function refresh(collectionToo = false) {
  clearTimeout(timer);
  try {
    S.runs = await api('/api/runs');
    const now = new Set(S.runs.filter(active).map(r => r.id));
    const finished = [...prevActive].some(id => !now.has(id));
    prevActive = now;
    if (collectionToo || finished) {
      S.col = await api('/api/collection');
      buildEntries(); setupFilters();
    }
    if (S.runId) S.run = await api(`/api/runs/${S.runId}`);
    renderAll();
  } catch (e) { console.warn(e); }
  timer = setTimeout(refresh, prevActive.size ? 1500 : 8000);
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
})();
