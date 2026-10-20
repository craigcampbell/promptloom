// Promptloom app. Plain JS, no build step and nothing fetched from elsewhere:
// every request goes to the promptloom server on this machine.
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const main = $('#main');

// helpers

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  };
  const res = await fetch('/api/' + path, opts);
  const data = await res.json();
  if (!res.ok)
    throw new Error(data.error || res.statusText);
  return data;
}

function ago(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then))
    return '';
  const m = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (m < 1) return 'now';
  if (m < 60) return `${m}m ago`;
  if (m < 1440) return `${Math.round(m / 60)}h ago`;
  if (m < 1440 * 60) return `${Math.round(m / 1440)}d ago`;
  return new Date(then).toLocaleDateString();
}

function when(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString([], {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
}

function toast(text) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = text;
  document.body.append(el);
  setTimeout(() => el.remove(), 1800);
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied');
  } catch {
    toast('Copy failed');
  }
}

function highlight(text, terms) {
  let html = esc(text);
  for (const term of terms || []) {
    if (!term) continue;
    const re = new RegExp(esc(term).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    html = html.replace(re, m => `<mark>${m}</mark>`);
  }
  return html;
}

const sessionHref = key => `#/session/${encodeURIComponent(key)}`;
const projectHref = name => `#/project/${encodeURIComponent(name)}`;
const searchHref = q => `#/search?q=${encodeURIComponent(q)}`;

// Models keep one colour for the whole visit: slots go to the most used
// models first, the rest fold into "other".
const SLOTS = 7;
let modelSlots = {};
function setModelOrder(models) {
  modelSlots = {};
  models.filter(m => m !== 'unknown').slice(0, SLOTS)
    .forEach((m, i) => { modelSlots[m] = `var(--s${i + 1})`; });
}
function modelColor(model) {
  if (!model || model === 'unknown') return 'var(--unknown)';
  return modelSlots[model] || 'var(--other)';
}

// Tags are "kind:value" for the ones worked out, bare for the ones people add.
function chip(tag, opts = {}) {
  const [k, ...rest] = tag.includes(':') ? tag.split(':') : ['', tag];
  const v = rest.join(':');
  const sw = k === 'model' ? `<span class="swatch" style="background:${modelColor(v)}"></span>` : '';
  const x = opts.remove ? ` <span class="x" data-remove="${esc(tag)}" title="Remove">×</span>` : '';
  return `<span class="chip${k ? '' : ' user'}" data-tag="${esc(tag)}">${sw}` +
    (k ? `<span class="k">${esc(k)}</span>${esc(v)}` : `#${esc(v)}`) + x + '</span>';
}

function chips(tags, opts) {
  return `<div class="chips">${(tags || []).map(t => chip(t, opts)).join('')}</div>`;
}

// Clicking any tag searches for it.
document.addEventListener('click', e => {
  if (e.target.closest('[data-remove]'))
    return;
  const el = e.target.closest('.chip[data-tag]');
  if (el && !el.closest('[data-no-search]')) {
    const tag = el.dataset.tag;
    location.hash = searchHref(tag.startsWith('theme:') || tag.includes(':') ? tag : `tag:${tag}`);
  }
});

function legend(models) {
  return `<div class="legend">${models.map(m =>
    `<span><span class="swatch" style="background:${modelColor(m)}"></span>${esc(m)}</span>`).join('')}</div>`;
}

function modelBar(counts) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
  return `<div class="bar" title="${esc(Object.entries(counts).map(([m, n]) => `${m}: ${n}`).join('\n'))}">` +
    Object.entries(counts).map(([m, n]) =>
      `<span style="width:${(100 * n / total).toFixed(2)}%;background:${modelColor(m)}"></span>`).join('') +
    '</div>';
}

// One tooltip element, shared by every chart.
const tip = document.createElement('div');
tip.className = 'tip';
tip.hidden = true;
document.body.append(tip);
function showTip(e, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const pad = 14;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(e.clientX + pad, innerWidth - w - 4) + 'px';
  tip.style.top = Math.min(e.clientY + pad, innerHeight - h - 4) + 'px';
}
function hideTip() { tip.hidden = true; }

// Light markdown for notes: headings, lists, bold, italics, code, links.
function markdown(src) {
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|\W)\*([^*]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
    .replace(/(^|\s)#([\w:./+-]+)/g, (m, s, t) => `${s}<a href="${searchHref('tag:' + t)}">#${t}</a>`);
  const out = [];
  let list = false;
  for (const line of src.split('\n')) {
    const h = line.match(/^(#{1,3})\s+(.*)/);
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (list && !li) { out.push('</ul>'); list = false; }
    if (h) out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`);
    else if (li) {
      if (!list) { out.push('<ul>'); list = true; }
      out.push(`<li>${inline(li[1])}</li>`);
    } else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push('</ul>');
  return out.join('\n');
}

// state shared between views

let overview = null;
async function loadOverview() {
  overview = await api('overview');
  const models = await api('models');
  setModelOrder(models.models.map(m => m.model));
  $('#foot').innerHTML = `${overview.sessions} sessions<br>${overview.prompts} prompts<br>` +
    `${overview.notes} notes` +
    (overview.waiting ? `<br><br>Summaries paused: ${esc(overview.waiting)}` : '');
}

// views

const views = {};

views.home = async () => {
  const o = overview;
  const active = o.active;
  main.innerHTML = `
    <h1>Promptloom</h1>
    <p class="muted">Every prompt you've typed into ${Object.values(o.harnesses).map(esc).join(', ') || 'your AI tools'}, in one place.</p>
    <form class="search" id="q" style="margin:18px 0">
      <input name="q" placeholder="Search prompts and notes…  try  model:opus  project:name  tag:idea" autofocus>
      <button class="primary">Search</button>
    </form>
    <div class="stats">
      <div class="card stat"><div class="n">${o.sessions}</div><div class="l">sessions</div></div>
      <div class="card stat"><div class="n">${o.prompts}</div><div class="l">prompts</div></div>
      <div class="card stat"><div class="n">${Object.keys(o.harnesses).length}</div><div class="l">tools</div></div>
      <div class="card stat"><div class="n">${o.notes}</div><div class="l">notes</div></div>
    </div>
    ${active ? `<h2>Active now</h2><div class="item"><a href="${sessionHref(active.key)}"><h3>${esc(active.title)}</h3></a>
      <div class="meta">${esc(active.harness_label)} · ${esc(active.project)} · ${ago(active.updated)}${active.model ? ' · ' + esc(active.model) : ''}</div></div>` : ''}
    <h2>Quick capture</h2>
    <form class="search" id="cap">
      <input name="text" placeholder="A thought, a link, or start with todo: for an action item">
      <button>Save</button>
    </form>
    <h2>Recent sessions</h2>
    <div class="list">${o.recent.map(sessionItem).join('') || '<p class="empty">Nothing captured yet. Use Claude Code, Codex or another tool and it will show up here.</p>'}</div>
    ${o.captures.length ? `<h2>Inbox</h2><div class="list">${o.captures.slice(0, 10).map(c =>
      `<div class="item"><div>${c.todo ? (c.done ? '☑ ' : '☐ ') : ''}${esc(c.text)}</div>
       <div class="meta">${ago(c.ts)}${c.context ? ' · ' + esc(c.context) : ''}</div></div>`).join('')}</div>` : ''}
    ${o.errors && o.errors.length ? `<h2>Warnings</h2><div class="small muted">${o.errors.map(esc).join('<br>')}</div>` : ''}
  `;
  $('#q').onsubmit = e => {
    e.preventDefault();
    location.hash = searchHref(e.target.q.value);
  };
  $('#cap').onsubmit = async e => {
    e.preventDefault();
    const text = e.target.text.value.trim();
    if (!text) return;
    await api('capture', {text});
    e.target.text.value = '';
    toast('Saved. It appears in the inbox on the next sync.');
  };
};

function sessionItem(s) {
  return `<div class="item">
    <a href="${sessionHref(s.key)}"><h3>${esc(s.title)}</h3></a>
    <div class="meta">${esc(overview.labels[s.harness] || s.harness)} · <a href="${projectHref(s.project)}">${esc(s.project)}</a> · ${s.prompts} prompts · ${ago(s.updated)}</div>
    ${s.summary ? `<div class="text small">${esc(s.summary)}</div>` : ''}
    ${chips(s.tags.filter(t => !t.startsWith('project:') && !t.startsWith('harness:')))}
  </div>`;
}

views.search = async params => {
  const q = params.get('q') || '';
  main.innerHTML = `
    <h1>Search</h1>
    <form class="search" id="q"><input name="q" value="${esc(q)}" placeholder="Words to find, plus filters" autofocus><button class="primary">Search</button></form>
    <div class="filters row small muted" id="filters"></div>
    <div id="results"><p class="muted">Searching…</p></div>`;
  const form = $('#q');
  form.onsubmit = e => {
    e.preventDefault();
    location.hash = searchHref(form.q.value.trim());
  };
  const projects = await api('projects');
  const models = Object.keys(modelSlots);
  const tags = Object.keys(overview.tags).filter(t => !t.includes(':'));
  const pick = (name, values) => `<select data-filter="${name}"><option value="">any ${name}</option>${
    values.map(v => `<option>${esc(v)}</option>`).join('')}</select>`;
  $('#filters').innerHTML = 'Filter: ' +
    pick('project', projects.map(p => p.name)) + pick('model', models) +
    pick('harness', Object.keys(overview.harnesses)) + (tags.length ? pick('tag', tags) : '') +
    ` <input type="date" data-filter="after" title="after"> to <input type="date" data-filter="before" title="before">`;
  for (const el of document.querySelectorAll('[data-filter]')) {
    el.onchange = () => {
      if (!el.value) return;
      const v = /\s/.test(el.value) ? `"${el.value}"` : el.value;
      location.hash = searchHref(`${form.q.value.trim()} ${el.dataset.filter}:${v}`.trim());
    };
  }
  if (!q) {
    $('#results').innerHTML = `<div class="card small">
      <p>Every word has to match. Filters narrow it down:</p>
      <p><code>model:opus</code> <code>project:shop</code> <code>harness:codex</code> <code>tag:idea</code>
      <code>theme:auth</code> <code>branch:main</code> <code>after:2026-01-01</code> <code>before:2026-02-01</code>
      <code>"exact phrase"</code></p>
      <p class="muted">Tags you add on a session or prompt can be searched with <code>tag:</code>, and so can the ones promptloom adds: <code>tag:model:claude-opus-5-5</code>.</p></div>`;
    return;
  }
  const r = await api('search?q=' + encodeURIComponent(q));
  const shown = r.hits.length < r.total ? ` (newest ${r.hits.length} shown)` : '';
  $('#results').innerHTML = `
    <p class="muted">${r.total} prompt${r.total === 1 ? '' : 's'}${shown}${r.notes.length ? `, ${r.notes.length} note${r.notes.length === 1 ? '' : 's'}` : ''}</p>
    ${r.notes.length ? `<div class="list" style="margin-bottom:14px">${r.notes.map(n => `
      <div class="item"><a href="#/note/${encodeURIComponent(n.id)}"><h3>📝 ${highlight(n.title, r.terms)}</h3></a>
      <div class="text small">${highlight(n.text, r.terms)}</div>${chips(n.tags)}</div>`).join('')}</div>` : ''}
    <div class="list">${r.hits.map(h => `
      <div class="item">
        <div class="meta"><a href="${sessionHref(h.key)}#p${h.index}">${esc(h.title)}</a> · ${when(h.ts)}</div>
        <div class="text">${highlight(h.text, r.terms)}</div>
        ${chips(h.tags)}
      </div>`).join('') || '<p class="empty">No prompts match.</p>'}</div>`;
};

function tagEditor(tags, onChange) {
  const wrap = document.createElement('div');
  wrap.className = 'chips';
  wrap.dataset.noSearch = '';
  const draw = () => {
    wrap.innerHTML = tags.map(t => chip(t, {remove: true})).join('') +
      '<input class="addtag" placeholder="+ tag">';
    for (const x of wrap.querySelectorAll('[data-remove]'))
      x.onclick = () => { tags = tags.filter(t => t !== x.dataset.remove); onChange(tags).then(draw); };
    const input = wrap.querySelector('input');
    input.onkeydown = e => {
      if (e.key !== 'Enter' || !input.value.trim()) return;
      tags = [...tags, ...input.value.split(/[,\s]+/).filter(Boolean)];
      onChange(tags).then(saved => { tags = saved; draw(); wrap.querySelector('input').focus(); });
    };
  };
  draw();
  return wrap;
}

views.session = async (params, key) => {
  const s = await api('session?key=' + encodeURIComponent(key));
  const auto = s.tags.filter(t => t.includes(':'));
  main.innerHTML = `
    <div class="muted small"><a href="${projectHref(s.project)}">${esc(s.project)}</a> · ${esc(overview.labels[s.harness] || s.harness)}</div>
    <h1>${esc(s.title)}</h1>
    <div class="meta muted small">${when(s.started)} → ${when(s.updated)} · ${s.prompts.length} prompts · ${esc(s.context)}${s.branch ? ' · ' + esc(s.branch) : ''}</div>
    ${chips(auto)}
    <div id="usertags" style="margin-top:6px"></div>
    ${s.summary ? `<h2>Summary</h2><p>${esc(s.summary)}</p>` : ''}
    <div class="row" style="margin-top:14px">
      ${s.resume ? `<button id="resume">Copy resume command</button>` : ''}
      <button id="sid">Copy session ID</button>
      <a href="#/note/new?session=${encodeURIComponent(s.key)}&project=${encodeURIComponent(s.project)}"><button>Add note</button></a>
    </div>
    ${s.notes.length ? `<h2>Notes</h2><div class="list">${s.notes.map(n =>
      `<div class="item"><a href="#/note/${encodeURIComponent(n.id)}"><h3>${esc(n.title)}</h3></a>${chips(n.tags)}</div>`).join('')}</div>` : ''}
    ${s.actions.length ? `<h2>Action items</h2><div class="list">${s.actions.map(a =>
      `<div>${a.done ? '☑' : '☐'} ${esc(a.text)}</div>`).join('')}</div>` : ''}
    ${s.related.length ? `<h2>Related sessions</h2><div class="list">${s.related.map(r => `
      <div class="item"><a href="${sessionHref(r.key)}">${esc(r.title)}</a>
      <span class="muted small">· ${esc(r.project)}${r.hidden ? ' · <span style="color:var(--hidden-link)">no shared theme</span>' : ''}</span>
      <div class="chips">${r.words.map(w => `<span class="chip" data-tag="${esc(w)}" onclick="location.hash='${esc(searchHref(w))}';event.stopPropagation()">${esc(w)}</span>`).join('')}</div></div>`).join('')}</div>` : ''}
    <h2>Prompts</h2>
    <div class="list" id="prompts"></div>
    ${s.reply_urls.length ? `<h2>Links</h2><div class="small">${s.reply_urls.map(u =>
      `<div><a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a></div>`).join('')}</div>` : ''}
  `;
  $('#usertags').append(tagEditor(s.user_tags, async tags =>
    (await api('tags', {key: s.key, tags})).tags));
  if (s.resume) $('#resume').onclick = () => copy(s.resume);
  $('#sid').onclick = () => copy(s.session_id);
  const list = $('#prompts');
  for (const p of s.prompts) {
    const el = document.createElement('div');
    el.className = 'item prompt';
    el.id = `p${p.index}`;
    const long = p.text.length > 900;
    el.innerHTML = `<div class="num">${p.index + 1}</div><div>
      <div class="meta">${when(p.ts)} · <span class="swatch" style="background:${modelColor(p.model)}"></span> ${esc(p.model || 'model unknown')}
        <button class="link small copy">copy</button></div>
      <div class="text${long ? ' clipped' : ''}">${esc(p.text)}</div>
      ${long ? '<button class="link small more">show all</button>' : ''}
      <div class="ptags"></div></div>`;
    el.querySelector('.copy').onclick = () => copy(p.text);
    if (long) el.querySelector('.more').onclick = e => {
      el.querySelector('.text').classList.remove('clipped');
      e.target.remove();
    };
    el.querySelector('.ptags').append(tagEditor(p.user_tags, async tags =>
      (await api('tags', {key: s.key, prompt: p.index, tags})).tags));
    list.append(el);
  }
  const target = location.hash.split('#')[2];
  if (target) document.getElementById(target)?.scrollIntoView({block: 'center'});
};

views.projects = async () => {
  const projects = await api('projects');
  main.innerHTML = `
    <h1>Projects</h1>
    <p class="muted">Each working directory you've used an AI tool in. The bar shows which models did the work.</p>
    ${legend(Object.keys(modelSlots))}
    <table><thead><tr><th>Project</th><th>Models</th><th class="num">Sessions</th><th class="num">Prompts</th><th>Active</th></tr></thead>
    <tbody>${projects.map(p => `<tr>
      <td><a href="${projectHref(p.name)}">${esc(p.name)}</a>${p.app ? '' : ' <span class="muted small">(folder)</span>'}</td>
      <td style="min-width:160px">${modelBar(p.models)}<div class="small muted">${Object.keys(p.models).length} model${Object.keys(p.models).length === 1 ? '' : 's'}</div></td>
      <td class="num">${p.sessions}</td><td class="num">${p.prompts}</td>
      <td class="small muted">${ago(p.first)} → ${ago(p.last)}</td></tr>`).join('')}</tbody></table>`;
};

// Prompts per day, stacked by model.
function activityChart(days) {
  const wrap = document.createElement('div');
  wrap.className = 'chart';
  if (!days.length) return wrap;
  const W = 900, H = 200, L = 30, B = 22, T = 8;
  const first = new Date(days[0].day), last = new Date(days.at(-1).day);
  const span = Math.max(1, Math.round((last - first) / 864e5) + 1);
  const byDay = Object.fromEntries(days.map(d => [d.day, d.models]));
  const all = [];
  for (let i = 0; i < span; i++) {
    const d = new Date(first.getTime() + i * 864e5).toISOString().slice(0, 10);
    all.push({day: d, models: byDay[d] || {}});
  }
  const max = Math.max(...all.map(d => Object.values(d.models).reduce((a, b) => a + b, 0)), 1);
  const step = (W - L) / all.length;
  const bw = Math.max(2, Math.min(28, step - 2));
  const y = v => T + (H - T - B) * (1 - v / max);
  const ticks = [0, Math.ceil(max / 2), max];
  let svg = `<g class="grid">${ticks.map(t => `<line x1="${L}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/>`).join('')}</g>`;
  svg += `<g class="axis">${ticks.map(t => `<text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${t}</text>`).join('')}`;
  const every = Math.ceil(all.length / 8);
  all.forEach((d, i) => {
    if (i % every === 0)
      svg += `<text x="${L + i * step + step / 2}" y="${H - 6}" text-anchor="middle">${d.day.slice(5)}</text>`;
  });
  svg += '</g>';
  all.forEach((d, i) => {
    let base = 0;
    const x = L + i * step + (step - bw) / 2;
    const entries = Object.entries(d.models);
    entries.forEach(([m, n], j) => {
      const top = y(base + n), bottom = y(base);
      // 2px surface gap between stacked segments; round only the top end.
      const h = Math.max(1, bottom - top - (j ? 2 : 0));
      const r = j === entries.length - 1 ? Math.min(4, bw / 2, h) : 0;
      svg += `<path fill="${modelColor(m)}" d="M${x},${top + h} v${-(h - r)} q0,${-r} ${r},${-r} h${bw - 2 * r} q${r},0 ${r},${r} v${h - r} z" transform="translate(0,${bottom - (top + h)})"/>`;
      base += n;
    });
    svg += `<rect class="hit" data-i="${i}" x="${L + i * step}" y="${T}" width="${step}" height="${H - T - B}" fill="transparent"/>`;
  });
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Prompts per day by model">${svg}</svg>`;
  wrap.querySelectorAll('.hit').forEach(r => {
    r.onmousemove = e => {
      const d = all[+r.dataset.i];
      const total = Object.values(d.models).reduce((a, b) => a + b, 0);
      showTip(e, `<div class="t">${d.day} · ${total} prompt${total === 1 ? '' : 's'}</div>` +
        Object.entries(d.models).map(([m, n]) =>
          `<div class="r"><span class="swatch" style="background:${modelColor(m)}"></span>${esc(m)}: ${n}</div>`).join(''));
    };
    r.onmouseleave = hideTip;
  });
  return wrap;
}

views.project = async (params, name) => {
  const p = await api('project?name=' + encodeURIComponent(name));
  const info = p.info;
  const models = Object.keys(info.models);
  main.innerHTML = `
    <div class="muted small"><a href="#/projects">Projects</a></div>
    <h1>${esc(p.project)}</h1>
    <div class="muted small">${info.contexts.map(esc).join(', ')}</div>
    <div class="stats" style="margin-top:14px">
      <div class="card stat"><div class="n">${info.sessions}</div><div class="l">sessions</div></div>
      <div class="card stat"><div class="n">${info.prompts}</div><div class="l">prompts</div></div>
      <div class="card stat"><div class="n">${models.filter(m => m !== 'unknown').length}</div><div class="l">models</div></div>
      <div class="card stat"><div class="n">${p.handoffs.length}</div><div class="l">model changes</div></div>
    </div>
    <h2>How it came together</h2>
    <p class="muted small">Prompts per day, coloured by the model that answered them.</p>
    ${legend(models)}
    <div id="chart"></div>
    <div class="split">
      <div>
        <h2>Models</h2>
        <table><tbody>${Object.entries(info.models).map(([m, n]) => `<tr>
          <td><span class="swatch" style="background:${modelColor(m)}"></span> ${esc(m)}</td><td class="num">${n}</td></tr>`).join('')}</tbody></table>
        <h2>Tools</h2>
        <table><tbody>${Object.entries(info.harnesses).map(([h, n]) => `<tr>
          <td>${esc(overview.labels[h] || h)}</td><td class="num">${n}</td></tr>`).join('')}</tbody></table>
      </div>
      <div>
        <h2>Model changes</h2>
        ${p.handoffs.length ? `<div class="list small">${p.handoffs.slice(-14).reverse().map(h => `
          <div><span class="muted">${when(h.at)}</span><br>
          <span class="swatch" style="background:${modelColor(h.from)}"></span> ${esc(h.from)} → <span class="swatch" style="background:${modelColor(h.to)}"></span> ${esc(h.to)}
          <span class="muted">in <a href="${sessionHref(h.key)}">${esc(h.title)}</a></span></div>`).join('')}</div>`
          : '<p class="muted small">One model has done all the work here.</p>'}
      </div>
    </div>
    ${p.links.length ? `<h2>Connected projects</h2><div class="list">${p.links.map(l => `
      <div class="item"><a href="${projectHref(l.other)}">${esc(l.other)}</a> <span class="muted small">shares</span>
      <div class="chips">${l.words.map(w => `<span class="chip" data-tag="${esc(w)}">${esc(w)}</span>`).join('')}</div></div>`).join('')}</div>` : ''}
    ${p.themes.length ? `<h2>Themes</h2>${chips(p.themes.map(t => 'theme:' + t))}` : ''}
    <h2>Notes</h2>
    <div class="list">${p.notes.map(n => `<div class="item"><a href="#/note/${encodeURIComponent(n.id)}">${esc(n.title)}</a></div>`).join('')}</div>
    <p><a href="#/note/new?project=${encodeURIComponent(p.project)}"><button>Add a note to ${esc(p.project)}</button></a></p>
    ${p.actions.filter(a => !a.done).length ? `<h2>Open action items</h2><div class="list small">${p.actions.filter(a => !a.done).map(a =>
      `<div>☐ ${esc(a.text)} <a class="muted" href="${sessionHref(a.key)}">${esc(a.title)}</a></div>`).join('')}</div>` : ''}
    <h2>Sessions, oldest first</h2>
    <div class="list">${p.sessions.map(sessionItem).join('')}</div>`;
  $('#chart').append(activityChart(p.days));
  // The chips above search for the word; links between projects are by word.
  for (const el of main.querySelectorAll('.chip[data-tag]'))
    if (!el.dataset.tag.includes(':')) el.onclick = e => { e.stopPropagation(); location.hash = searchHref(el.dataset.tag); };
};

views.models = async () => {
  const m = await api('models');
  const names = m.models.map(x => x.model);
  const projects = [...new Set(m.models.flatMap(x => Object.keys(x.projects)))];
  const max = Math.max(1, ...m.models.flatMap(x => Object.values(x.projects)));
  main.innerHTML = `
    <h1>Models</h1>
    <p class="muted">Which models have worked on what, and where the work moved from one to another.</p>
    <div class="cards">${m.models.map(x => `<div class="card">
      <h3><span class="swatch" style="background:${modelColor(x.model)}"></span> ${esc(x.model)}</h3>
      <div class="small muted">${x.prompts} prompts · ${Object.keys(x.projects).length} projects</div>
      <div class="small muted">${x.first ? `${ago(x.first)} → ${ago(x.last)}` : ''}</div>
      <div class="small">${Object.entries(x.harnesses).map(([h, n]) => `${esc(overview.labels[h] || h)} ${n}`).join(' · ')}</div>
      <p class="small" style="margin-top:6px"><a href="${searchHref('model:' + x.model)}">See its prompts</a></p>
    </div>`).join('')}</div>
    <h2>Projects × models</h2>
    <p class="muted small">Prompts each model answered in each project. Darker is more.</p>
    <div style="overflow:auto"><table class="matrix"><thead><tr><th>Project</th>${names.map(n =>
      `<th class="small" title="${esc(n)}"><span class="swatch" style="background:${modelColor(n)}"></span> ${esc(n.length > 18 ? n.slice(0, 17) + '…' : n)}</th>`).join('')}</tr></thead>
    <tbody>${projects.map(p => `<tr><td><a href="${projectHref(p)}">${esc(p)}</a></td>${m.models.map(x => {
      const n = x.projects[p] || 0;
      const a = n ? 0.15 + 0.85 * n / max : 0;
      return `<td class="cell"><div data-tip="${esc(`${p} · ${x.model}: ${n} prompt${n === 1 ? '' : 's'}`)}" style="background:${n ? `color-mix(in oklab, var(--s1) ${Math.round(a * 100)}%, var(--surface-1))` : 'transparent'}">${n || ''}</div></td>`;
    }).join('')}</tr>`).join('')}</tbody></table></div>
    <h2>Projects more than one model worked on</h2>
    ${m.projects.filter(p => Object.keys(p.models).length > 1).length ? `<table><thead><tr><th>Project</th><th>Mix</th><th class="num">Changes</th><th>Most common changes</th></tr></thead><tbody>
    ${m.projects.filter(p => Object.keys(p.models).length > 1).map(p => `<tr>
      <td><a href="${projectHref(p.project)}">${esc(p.project)}</a></td>
      <td style="min-width:140px">${modelBar(p.models)}</td>
      <td class="num">${p.handoffs}</td>
      <td class="small">${Object.entries(p.pairs).map(([k, n]) => `${esc(k)} (${n})`).join('<br>')}</td></tr>`).join('')}
    </tbody></table>` : '<p class="muted">Each project has had a single model so far.</p>'}
    <h2>Models that shared projects</h2>
    ${m.shared.length ? `<div class="list small">${m.shared.slice(0, 20).map(s => `<div>
      <span class="swatch" style="background:${modelColor(s.a)}"></span> ${esc(s.a)} &amp;
      <span class="swatch" style="background:${modelColor(s.b)}"></span> ${esc(s.b)}:
      ${s.projects.map(p => `<a href="${projectHref(p)}">${esc(p)}</a>`).join(', ')}</div>`).join('')}</div>`
      : '<p class="muted">No two models have worked on the same project yet.</p>'}`;
  main.querySelectorAll('[data-tip]').forEach(el => {
    el.onmousemove = e => showTip(e, esc(el.dataset.tip));
    el.onmouseleave = hideTip;
  });
};

// graph

const KINDS = {
  project: {label: 'Projects', color: 'var(--s1)', on: true},
  model: {label: 'Models', color: 'var(--s2)', on: true},
  keyword: {label: 'Shared words', color: 'var(--s3)', on: true},
  theme: {label: 'Themes', color: 'var(--s4)', on: true},
  tag: {label: 'Your tags', color: 'var(--s5)', on: true},
  harness: {label: 'Tools', color: 'var(--s6)', on: true},
  note: {label: 'Notes', color: 'var(--s7)', on: true},
  session: {label: 'Sessions', color: 'var(--s8)', on: false},
};

function cssColor(value) {
  const probe = document.createElement('span');
  probe.style.color = value;
  document.body.append(probe);
  const c = getComputedStyle(probe).color;
  probe.remove();
  return c;
}

views.graph = async () => {
  main.innerHTML = `
    <h1>Graph</h1>
    <p class="muted small">Projects joined by the models, tools, themes, tags and unusual words they share.
    <span style="color:var(--hidden-link)">Dashed purple</span> lines join projects that share words but no theme: the connections least likely to have been noticed.
    Drag to move, scroll to zoom, click a node to see what it touches.</p>
    <div class="row small" id="kinds"></div>
    <div class="graph-wrap"><canvas id="cv"></canvas><div class="graph-side" id="side" hidden></div></div>`;
  const kinds = $('#kinds');
  for (const [k, v] of Object.entries(KINDS)) {
    const label = document.createElement('label');
    label.className = 'toggle';
    label.innerHTML = `<input type="checkbox" ${v.on ? 'checked' : ''}><span class="swatch" style="background:${v.color}"></span>${v.label}`;
    label.querySelector('input').onchange = e => {
      v.on = e.target.checked;
      if (k === 'session') load(); else sim.refilter();
    };
    kinds.append(label);
  }
  const search = document.createElement('input');
  search.placeholder = 'Find…';
  search.style.marginLeft = 'auto';
  search.oninput = () => { sim.find = search.value.toLowerCase(); sim.draw(); };
  kinds.append(search);

  const sim = new Graph($('#cv'), $('#side'));
  async function load() {
    const g = await api('graph' + (KINDS.session.on ? '?sessions=1' : ''));
    sim.set(g);
  }
  await load();
};

class Graph {
  constructor(canvas, side) {
    this.cv = canvas;
    this.side = side;
    this.ctx = canvas.getContext('2d');
    this.view = {x: 0, y: 0, k: 1};
    this.find = '';
    this.selected = null;
    this.colors = {};
    for (const [k, v] of Object.entries(KINDS)) this.colors[k] = cssColor(v.color);
    this.colors.hidden = cssColor('var(--hidden-link)');
    this.colors.edge = cssColor('var(--border)');
    this.colors.text = cssColor('var(--text-primary)');
    this.colors.muted = cssColor('var(--text-muted)');
    this.colors.surface = cssColor('var(--surface-1)');
    this.resize();
    this.bind();
    this.running = true;
    const tick = () => {
      if (!document.body.contains(this.cv)) return; // the view changed
      if (this.alpha > 0.005) { this.step(); this.draw(); }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  resize() {
    const r = this.cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.w = r.width; this.h = r.height;
    this.cv.width = r.width * dpr; this.cv.height = r.height * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  set(g) {
    const old = Object.fromEntries((this.all || []).map(n => [n.id, n]));
    this.all = g.nodes.map(n => {
      const prev = old[n.id];
      return Object.assign(n, prev ? {x: prev.x, y: prev.y} :
        {x: (Math.random() - 0.5) * this.w * 0.8, y: (Math.random() - 0.5) * this.h * 0.8}, {vx: 0, vy: 0});
    });
    this.allEdges = g.edges;
    this.refilter();
  }

  refilter() {
    this.nodes = this.all.filter(n => KINDS[n.kind]?.on);
    const by = Object.fromEntries(this.nodes.map(n => [n.id, n]));
    this.edges = this.allEdges.filter(e => by[e.source] && by[e.target])
      .map(e => Object.assign({}, e, {a: by[e.source], b: by[e.target]}));
    // Drop nodes left with no edges once their kind's partner is hidden.
    const linked = new Set(this.edges.flatMap(e => [e.source, e.target]));
    this.nodes = this.nodes.filter(n => linked.has(n.id) || n.kind === 'project');
    this.neighbors = {};
    for (const e of this.edges) {
      (this.neighbors[e.source] ||= []).push(e);
      (this.neighbors[e.target] ||= []).push(e);
    }
    for (const n of this.nodes) {
      n.r = this.radius(n);
      n.charge = n.kind === 'project' ? 4 : n.kind === 'session' ? 0.8 : 1.2;
    }
    this.alpha = 1;
  }

  radius(n) {
    const base = n.kind === 'project' ? 7 : n.kind === 'session' ? 3.5 : 4.5;
    return base + Math.min(14, Math.sqrt(n.weight) * (n.kind === 'project' ? 1.4 : 0.8));
  }

  step() {
    const nodes = this.nodes, a = this.alpha;
    for (let i = 0; i < nodes.length; i++) {
      const p = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const q = nodes[j];
        let dx = q.x - p.x, dy = q.y - p.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
        if (d2 > 360000) continue;
        const f = (900 * a * p.charge * q.charge) / d2;
        const d = Math.sqrt(d2);
        p.vx -= dx / d * f; p.vy -= dy / d * f;
        q.vx += dx / d * f; q.vy += dy / d * f;
      }
    }
    for (const e of this.edges) {
      const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      // Tools and models touch nearly every project; as strong springs they
      // would pull everything into one knot, so they hang loose.
      const loose = e.kind === 'harness' || e.kind === 'model';
      const want = e.kind === 'link' ? 230 : e.kind === 'in' ? 40 : loose ? 190 : 70;
      const f = (d - want) * a * (loose ? 0.002 : e.kind === 'link' ? 0.006 : 0.02);
      e.a.vx += dx / d * f; e.a.vy += dy / d * f;
      e.b.vx -= dx / d * f; e.b.vy -= dy / d * f;
    }
    for (const n of nodes) {
      if (n === this.dragging) { n.vx = n.vy = 0; continue; }
      n.vx -= n.x * 0.003 * a; n.vy -= n.y * 0.003 * a;
      n.x += n.vx; n.y += n.vy;
      n.vx *= 0.6; n.vy *= 0.6;
    }
    this.alpha *= 0.985;
  }

  toScreen(n) {
    return [this.w / 2 + (n.x + this.view.x) * this.view.k, this.h / 2 + (n.y + this.view.y) * this.view.k];
  }

  toWorld(sx, sy) {
    return [(sx - this.w / 2) / this.view.k - this.view.x, (sy - this.h / 2) / this.view.k - this.view.y];
  }

  hit(sx, sy) {
    const [x, y] = this.toWorld(sx, sy);
    let best = null, bd = Infinity;
    for (const n of this.nodes) {
      const d = Math.hypot(n.x - x, n.y - y);
      if (d < n.r + 4 / this.view.k && d < bd) { best = n; bd = d; }
    }
    return best;
  }

  draw() {
    const ctx = this.ctx, k = this.view.k;
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.save();
    ctx.translate(this.w / 2, this.h / 2);
    ctx.scale(k, k);
    ctx.translate(this.view.x, this.view.y);
    const focus = this.selected || this.hover;
    const near = new Set();
    if (focus) {
      near.add(focus.id);
      for (const e of this.neighbors[focus.id] || []) { near.add(e.source); near.add(e.target); }
    }
    const found = this.find ? new Set(this.nodes.filter(n => n.label.toLowerCase().includes(this.find)).map(n => n.id)) : null;
    for (const e of this.edges) {
      const lit = focus && (e.source === focus.id || e.target === focus.id);
      ctx.globalAlpha = focus && !lit ? 0.08 : e.kind === 'link' ? 0.9 : 0.5;
      ctx.strokeStyle = e.kind === 'link' && e.hidden ? this.colors.hidden :
        e.kind === 'link' ? this.colors.project : this.colors.edge;
      ctx.lineWidth = (e.kind === 'link' ? 1 + Math.min(3, e.weight * 4) : 1) / Math.sqrt(k);
      ctx.setLineDash(e.kind === 'link' && e.hidden ? [5 / k, 4 / k] : []);
      ctx.beginPath(); ctx.moveTo(e.a.x, e.a.y); ctx.lineTo(e.b.x, e.b.y); ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const n of this.nodes) {
      const dim = (focus && !near.has(n.id)) || (found && !found.has(n.id));
      ctx.globalAlpha = dim ? 0.15 : 1;
      ctx.fillStyle = this.colors[n.kind];
      ctx.strokeStyle = this.colors.surface;
      ctx.lineWidth = 2 / k;
      ctx.beginPath();
      if (n.kind === 'project') ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      else if (n.kind === 'model') ctx.rect(n.x - n.r, n.y - n.r, n.r * 2, n.r * 2);
      else if (n.kind === 'note') { ctx.moveTo(n.x, n.y - n.r); ctx.lineTo(n.x + n.r, n.y); ctx.lineTo(n.x, n.y + n.r); ctx.lineTo(n.x - n.r, n.y); ctx.closePath(); }
      else ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fill(); ctx.stroke();
    }
    ctx.restore();
    // Labels in screen space so they stay readable at any zoom.
    ctx.font = '12px system-ui, sans-serif';
    ctx.textAlign = 'center';
    for (const n of this.nodes) {
      const show = n.kind === 'project' || near.has(n.id) || (found && found.has(n.id)) ||
        (k > 1.4 && n.kind !== 'session') || (k > 2.2);
      if (!show || (focus && !near.has(n.id))) continue;
      const [sx, sy] = this.toScreen(n);
      const label = n.label.length > 28 ? n.label.slice(0, 27) + '…' : n.label;
      ctx.globalAlpha = 1;
      ctx.lineWidth = 3;
      ctx.strokeStyle = this.colors.surface;
      ctx.fillStyle = n.kind === 'project' ? this.colors.text : this.colors.muted;
      ctx.font = (n.kind === 'project' ? '600 ' : '') + '12px system-ui, sans-serif';
      ctx.strokeText(label, sx, sy - n.r * k - 5);
      ctx.fillText(label, sx, sy - n.r * k - 5);
    }
  }

  bind() {
    const cv = this.cv;
    let panning = null, moved = false;
    const pos = e => { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    cv.onpointerdown = e => {
      const [sx, sy] = pos(e);
      moved = false;
      const n = this.hit(sx, sy);
      cv.setPointerCapture(e.pointerId);
      if (n) { this.dragging = n; this.alpha = Math.max(this.alpha, 0.3); }
      else panning = {sx, sy, x: this.view.x, y: this.view.y};
    };
    cv.onpointermove = e => {
      const [sx, sy] = pos(e);
      if (this.dragging) {
        moved = true;
        [this.dragging.x, this.dragging.y] = this.toWorld(sx, sy);
        this.alpha = Math.max(this.alpha, 0.1);
        this.draw();
      } else if (panning) {
        moved = true;
        this.view.x = panning.x + (sx - panning.sx) / this.view.k;
        this.view.y = panning.y + (sy - panning.sy) / this.view.k;
        this.draw();
      } else {
        const n = this.hit(sx, sy);
        if (n !== this.hover) { this.hover = n; this.draw(); }
        cv.style.cursor = n ? 'pointer' : 'grab';
        if (n) showTip(e, `<div class="t">${esc(n.label)}</div><div class="r">${KINDS[n.kind].label.replace(/s$/, '')}</div>`);
        else hideTip();
      }
    };
    cv.onpointerup = e => {
      const [sx, sy] = pos(e);
      if (!moved) this.select(this.hit(sx, sy));
      this.dragging = null; panning = null;
    };
    cv.onpointerleave = () => { hideTip(); this.hover = null; this.draw(); };
    cv.onwheel = e => {
      e.preventDefault();
      const [sx, sy] = pos(e);
      const [wx, wy] = this.toWorld(sx, sy);
      this.view.k = Math.min(6, Math.max(0.2, this.view.k * Math.exp(-e.deltaY * 0.0015)));
      const [nx, ny] = this.toWorld(sx, sy);
      this.view.x += nx - wx; this.view.y += ny - wy;
      this.draw();
    };
    window.addEventListener('resize', () => { if (document.body.contains(cv)) { this.resize(); this.draw(); } });
  }

  select(n) {
    this.selected = n;
    this.draw();
    const side = this.side;
    if (!n) { side.hidden = true; return; }
    const kind = n.id.split(':')[0];
    const name = n.id.slice(kind.length + 1);
    const link = {
      project: projectHref(name), session: sessionHref(name), model: searchHref('model:' + name),
      harness: searchHref('harness:' + name), keyword: searchHref(name), theme: searchHref('theme:' + name),
      tag: searchHref('tag:' + name), note: `#/note/${encodeURIComponent(name)}`,
    }[kind];
    const edges = (this.neighbors[n.id] || []).slice().sort((a, b) => (b.kind === 'link') - (a.kind === 'link') || b.weight - a.weight);
    side.hidden = false;
    side.innerHTML = `<div class="small muted">${KINDS[n.kind].label.replace(/s$/, '')}</div>
      <h3>${esc(n.label)}</h3><p><a href="${link}">Open</a></p>
      <div class="list small">${edges.map(e => {
        const o = e.a === n ? e.b : e.a;
        return `<div><span class="swatch" style="background:${KINDS[o.kind].color}"></span> ${esc(o.label)}` +
          (e.words ? `<div class="muted">${e.hidden ? '<span style="color:var(--hidden-link)">hidden link</span>: ' : ''}${e.words.map(esc).join(', ')}</div>` : '') + '</div>';
      }).join('')}</div>`;
  }
}

views.connections = async () => {
  const c = await api('connections');
  main.innerHTML = `
    <h1>Connections</h1>
    <p class="muted">What your projects have in common that you may not have spotted: unusual words they share, and prompts that ask an assistant to connect them.</p>
    <h2>Prompts to try</h2>
    ${c.ideas.length ? `<div class="list">${c.ideas.map((i, n) => `<div class="item">
      <h3>${esc(i.title)}</h3>
      ${i.words.length ? `<div class="chips">${i.words.map(w => `<span class="chip" data-tag="${esc(w)}">${esc(w)}</span>`).join('')}</div>` : ''}
      <pre class="prompt-box">${esc(i.prompt)}</pre>
      <div class="row small"><button data-copy="${n}">Copy prompt</button>
      ${i.where ? `<span class="muted">run it in ${esc(i.where)}</span>` : ''}
      ${i.sessions.map(s => `<a href="${sessionHref(s.key)}">${esc(s.title)}</a>`).join(' · ')}</div></div>`).join('')}</div>`
      : '<p class="empty">Not enough overlap between projects yet.</p>'}
    <h2>Projects that share words</h2>
    <div class="list">${c.pairs.map(p => `<div class="item">
      <a href="${projectHref(p.a)}">${esc(p.a)}</a> ↔ <a href="${projectHref(p.b)}">${esc(p.b)}</a>
      ${p.hidden ? '<span class="small" style="color:var(--hidden-link)"> · no shared theme</span>' : ''}
      <div class="chips">${p.words.map(w => `<span class="chip" data-tag="${esc(w)}">${esc(w)}</span>`).join('')}</div>
      ${p.snippets.map(s => `<div class="small muted" style="margin-top:4px">“${esc(s)}”</div>`).join('')}
      <div class="small" style="margin-top:4px">${p.sessions.slice(0, 2).map(s =>
        `<a href="${sessionHref(s.key)}">${esc(s.title)}</a> ↔ <a href="${sessionHref(s.other)}">${esc(s.other_title)}</a>`).join('<br>')}</div>
    </div>`).join('') || '<p class="empty">None yet.</p>'}</div>
    ${c.themes.length ? `<h2>Overarching themes</h2><div class="list">${c.themes.map(t =>
      `<div class="item"><h3>${esc(t.name)}</h3><div class="small">${esc(t.description)}</div></div>`).join('')}</div>` : ''}`;
  main.querySelectorAll('[data-copy]').forEach(b => { b.onclick = () => copy(c.ideas[+b.dataset.copy].prompt); });
  main.querySelectorAll('.chip[data-tag]').forEach(el => {
    el.onclick = e => { e.stopPropagation(); location.hash = searchHref(el.dataset.tag); };
  });
};

views.notes = async () => {
  const notes = await api('notes');
  main.innerHTML = `
    <div class="row"><h1 class="grow">Notes</h1><a href="#/note/new"><button class="primary">New note</button></a></div>
    <p class="muted">Your own notes, kept with your prompts. Attach one to a project or session, tag it, and it shows up in search and on the graph.</p>
    <div class="list">${notes.map(n => `<div class="item">
      <a href="#/note/${encodeURIComponent(n.id)}"><h3>${esc(n.title)}</h3></a>
      <div class="meta">${ago(n.updated)}${n.project ? ` · <a href="${projectHref(n.project)}">${esc(n.project)}</a>` : ''}</div>
      <div class="text small">${esc(n.body.slice(0, 220))}${n.body.length > 220 ? '…' : ''}</div>
      ${chips(n.tags)}</div>`).join('') || '<p class="empty">No notes yet.</p>'}</div>`;
};

views.note = async (params, id) => {
  const projects = await api('projects');
  let note = {title: '', body: '', tags: [], project: params.get('project') || '', session: params.get('session') || ''};
  if (id !== 'new') {
    note = (await api('notes')).find(n => n.id === id);
    if (!note) { main.innerHTML = '<p class="empty">That note is gone.</p>'; return; }
  }
  main.innerHTML = `
    <div class="muted small"><a href="#/notes">Notes</a></div>
    <form id="nf">
      <input name="title" value="${esc(note.title)}" placeholder="Title" style="width:100%;font-size:20px;font-weight:600;margin:8px 0">
      <div class="row small">
        <label>Project <select name="project"><option value="">none</option>${projects.map(p =>
          `<option ${p.name === note.project ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label>
        <label class="grow">Tags <input name="tags" value="${esc(note.tags.join(' '))}" placeholder="space separated" style="width:60%"></label>
        ${note.session ? `<span>on <a href="${sessionHref(note.session)}">a session</a></span>` : ''}
      </div>
      <div class="split" style="margin-top:10px">
        <textarea class="body" name="body" placeholder="Markdown: # heading, - list, **bold**, \`code\`, #tag">${esc(note.body)}</textarea>
        <div class="card note-view" id="preview"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="primary">Save</button>
        ${note.id ? '<button type="button" class="danger" id="del">Delete</button>' : ''}
        <span class="muted small" id="saved"></span>
      </div>
    </form>`;
  const form = $('#nf');
  const preview = () => { $('#preview').innerHTML = markdown(form.body.value) || '<span class="muted">Preview</span>'; };
  form.body.oninput = preview;
  preview();
  form.onsubmit = async e => {
    e.preventDefault();
    const saved = await api('notes', {
      id: note.id, title: form.title.value, body: form.body.value,
      tags: form.tags.value.split(/[,\s]+/).filter(Boolean), project: form.project.value,
      session: note.session,
    });
    toast('Saved');
    if (!note.id) location.hash = `#/note/${encodeURIComponent(saved.id)}`;
    note = saved;
    $('#saved').textContent = `saved ${ago(saved.updated)}`;
  };
  if (note.id) $('#del').onclick = async () => {
    await api('notes', {delete: note.id});
    location.hash = '#/notes';
  };
};

// routing

async function route() {
  hideTip();
  const raw = location.hash.slice(1) || '/';
  const [path, query = ''] = raw.split('#')[0].split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  const name = parts[0] || 'home';
  const params = new URLSearchParams(query);
  for (const a of document.querySelectorAll('nav a'))
    a.classList.toggle('on', a.dataset.nav === (name === 'session' ? 'search' : name === 'project' ? 'projects' : name === 'note' ? 'notes' : name));
  const view = views[name];
  if (!view) { main.innerHTML = '<p class="empty">Nothing here.</p>'; return; }
  try {
    if (!overview || name === 'home') await loadOverview();
    await view(params, parts.slice(1).join('/'));
  } catch (err) {
    main.innerHTML = `<p class="empty">Could not load this: ${esc(err.message)}</p>`;
  }
  if (!location.hash.split('#')[2]) window.scrollTo(0, 0);
}

window.addEventListener('hashchange', route);
route();
