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

// Tools (harnesses) keep a colour of their own, apart from the models.
const TOOL_SLOTS = 6;
function toolColor(harness) {
  const names = Object.keys(overview?.harnesses || {});
  const i = names.indexOf(harness);
  return i < 0 || i >= TOOL_SLOTS ? 'var(--other)' : `var(--h${i + 1})`;
}
function toolLabel(harness) {
  return overview?.labels?.[harness] || harness;
}
function toolChip(harness) {
  return `<span class="chip tool" data-tag="harness:${esc(harness)}"><span class="swatch" style="background:${toolColor(harness)}"></span>${esc(toolLabel(harness))}</span>`;
}

// Tags are "kind:value" for the ones worked out, bare for the ones people add.
function chip(tag, opts = {}) {
  const [k, ...rest] = tag.includes(':') ? tag.split(':') : ['', tag];
  const v = rest.join(':');
  const sw = k === 'model' ? `<span class="swatch" style="background:${modelColor(v)}"></span>` :
    k === 'harness' ? `<span class="swatch" style="background:${toolColor(v)}"></span>` : '';
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

// Clicking into a filled search box selects what is there, so typing replaces it.
document.addEventListener('focusin', e => {
  const el = e.target;
  if (el.matches('input[type=search], .search input, input[list]') && el.value) el.select();
});

// state shared between views

let overview = null;
async function loadOverview() {
  overview = await api('overview');
  const models = await api('models');
  setModelOrder(models.models.map(m => m.model));
  $('#foot').innerHTML = `${overview.sessions} sessions<br>${overview.prompts} prompts<br>` +
    `${overview.notes} notes` +
    (overview.waiting ? `<br><br>Summaries paused: ${esc(overview.waiting)}` : '');
  $('#draft-count').textContent = overview.drafts ? overview.drafts : '';
  const need = (overview.prs?.waiting || 0) + (overview.prs?.asked || 0);
  $('#pr-count').textContent = need ? need : '';
}

// views

const views = {};

views.home = async () => {
  const o = overview;
  const active = o.active;
  const [threads, tools] = await Promise.all([api('threads'), api('tools').catch(() => ({tools: []}))]);
  const have = tools.tools.filter(x => x.available);
  let conn = {ideas: []};
  main.innerHTML = `
    <div class="hero">
      <form class="search" id="q">
        <input type="search" name="q" placeholder="Search prompts and notes…  try  model:opus  project:name  tag:idea" autofocus>
        <button class="primary">Search</button>
      </form>
      <div class="row start">${active ? `<span class="pulse"></span> <span class="small">Now: <a href="${sessionHref(active.key)}">${esc(active.title)}</a> <span class="muted">· ${toolLabel(active.harness)} · ${esc(active.project)}</span></span>` : '<span class="small muted">No session active right now.</span>'}
        <span class="grow"></span>
        <span class="muted small">start in</span>${have.map(x => `<a href="#/compose?tool=${encodeURIComponent(x.name)}"><button>${esc(x.label)}</button></a>`).join('')}</div>
    </div>
    <div class="loom">
      <section>
        <h2>Threads <a class="small muted" href="#/threads">all</a></h2>
        <div class="list" id="threads">${threads.slice(0, 7).map(threadCard).join('') || '<p class="empty">Nothing yet. Use any AI tool and the work shows up here.</p>'}</div>
      </section>
      <section>
        <h2>Needs you</h2>
        <div class="list" id="needs"></div>
        <h2>Quick capture</h2>
        <form class="search" id="cap"><input name="text" placeholder="A thought, a link, or todo: …"><button>Save</button></form>
      </section>
      <section>
        <h2>Suggested</h2>
        <div class="list" id="ideas"><p class="muted small">Looking…</p></div>
      </section>
    </div>`;
  // Needs you: drafts waiting, pull requests, open action items.
  const needs = [];
  const drafts = await api('drafts').catch(() => []);
  for (const d of drafts.filter(d => !d.sent).slice(0, 4))
    needs.push(`<div class="item slim"><a href="#/compose?draft=${encodeURIComponent(d.id)}">${esc(d.title)}</a><div class="meta">draft · ${ago(d.updated)}</div></div>`);
  if (o.prs?.enabled) {
    const p = await api('prs').catch(() => ({prs: []}));
    for (const pr of p.prs.filter(x => (x.mine && x.waiting) || x.asked).slice(0, 4))
      needs.push(`<div class="item slim"><a href="#/prs">${esc(pr.repo.split('/')[1])}#${pr.number} ${esc(pr.title)}</a><div class="meta">${pr.asked ? 'review requested' : `${pr.waiting} new comment${pr.waiting > 1 ? 's' : ''}`}</div></div>`);
  }
  const open = threads.flatMap(t => t.open.map(a => ({a, t}))).slice(0, 5);
  for (const {a, t} of open)
    needs.push(`<div class="item slim">☐ ${esc(a)}<div class="meta">${esc(t.project)} · <a href="#/threads?open=${encodeURIComponent(t.id)}">${esc(t.name)}</a></div></div>`);
  $('#needs').innerHTML = needs.join('') || '<p class="empty">Nothing waiting on you.</p>';
  // Suggested: hand-offs and connection ideas.
  try { conn = await api('connections'); } catch { conn = {ideas: []}; }
  const ideas = [];
  if (o.recent[0] && have.length > 1) {
    const s0 = o.recent[0];
    const other = have.find(x => x.harness !== s0.harness) || have[0];
    ideas.push(`<div class="item slim"><strong>Hand off “${esc(s0.title)}”</strong><div class="small muted">Pick it up in ${esc(other.label)} with what was done, what's open and the brief.</div>
      <div class="row small" style="margin-top:4px"><a href="#/compose?handoff=${encodeURIComponent(s0.key)}&tool=${encodeURIComponent(other.name)}"><button>Continue in ${esc(other.label)}</button></a></div></div>`);
  }
  for (const i of conn.ideas.slice(0, 3))
    ideas.push(`<div class="item slim"><strong>${esc(i.title)}</strong><div class="small muted clip">${esc(i.prompt.slice(0, 180))}…</div>
      <div class="row small" style="margin-top:4px"><a href="#/compose?idea=${encodeURIComponent(i.prompt)}&context=${encodeURIComponent(i.where || '')}"><button>Open as a prompt</button></a></div></div>`);
  $('#ideas').innerHTML = ideas.join('') || '<p class="empty">Nothing to suggest yet.</p>';
  $('#q').onsubmit = e => { e.preventDefault(); location.hash = searchHref(e.target.q.value); };
  $('#cap').onsubmit = async e => {
    e.preventDefault();
    const text = e.target.text.value.trim();
    if (!text) return;
    await api('capture', {text});
    e.target.text.value = '';
    toast('Saved. It appears in the inbox on the next sync.');
  };
};

function threadCard(t) {
  const tools = Object.entries(t.tools).map(([h]) => toolChip(h)).join('');
  const extras = [t.drafts.length ? `${t.drafts.length} draft${t.drafts.length > 1 ? 's' : ''}` : '',
    t.prs.length ? `${t.prs.length} PR${t.prs.length > 1 ? 's' : ''}` : '',
    t.open.length ? `${t.open.length} open` : ''].filter(Boolean);
  return `<div class="item thread" data-thread="${esc(t.id)}">
    <div class="row"><a class="grow" href="#/threads?open=${encodeURIComponent(t.id)}"><h3>${t.pinned ? '📌 ' : ''}${esc(t.name)}</h3></a></div>
    <div class="meta"><a href="${projectHref(t.project)}">${esc(t.project)}</a> · ${t.sessions.length} session${t.sessions.length > 1 ? 's' : ''} · ${t.prompts} prompts · ${ago(t.updated)}${extras.length ? ' · ' + extras.join(', ') : ''}</div>
    <div class="chips" data-no-search>${tools}${Object.keys(t.models).slice(0, 3).map(m => chip('model:' + m)).join('')}</div>
  </div>`;
}

function sessionItem(s) {
  return `<div class="item">
    <a href="${sessionHref(s.key)}"><h3>${esc(s.title)}</h3></a>
    <div class="meta">${toolChip(s.harness)} <a href="${projectHref(s.project)}">${esc(s.project)}</a> · ${s.prompts} prompts · ${ago(s.updated)}</div>
    ${s.summary ? `<div class="text small">${esc(s.summary)}</div>` : ''}
    ${chips(s.tags.filter(t => !t.startsWith('project:') && !t.startsWith('harness:')))}
  </div>`;
}

views.search = async params => {
  const q = params.get('q') || '';
  main.innerHTML = `
    <h1>Search</h1>
    <form class="search" id="q"><input type="search" name="q" value="${esc(q)}" placeholder="Words to find, plus filters" autofocus><button class="primary">Search</button></form>
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
      // Replace a filter of the same kind rather than piling them up.
      const kept = form.q.value.trim().split(/\s+/).filter(w => w && !w.startsWith(el.dataset.filter + ':'));
      location.hash = searchHref([...kept, `${el.dataset.filter}:${v}`].join(' '));
    };
  }
  if (q) $('#filters').insertAdjacentHTML('beforeend', ' <a href="#/search" class="small">clear</a>');
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
  api('tools').then(t => {
    $('#handoff').innerHTML = t.tools.filter(x => x.available && !x.gui).map(x =>
      `<a href="#/compose?handoff=${encodeURIComponent(s.key)}&tool=${encodeURIComponent(x.name)}"><button title="Pick this work up in ${esc(x.label)}, with what was done, what is open and the brief">${esc(x.label)}</button></a>`).join(' ');
  }).catch(() => {});
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
      <span class="grow"></span><span class="muted small">continue in</span><span id="handoff"></span>
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
    <table><thead><tr><th>Project</th><th>Models</th><th>Tools</th><th class="num">Sessions</th><th class="num">Prompts</th><th>Active</th></tr></thead>
    <tbody>${projects.map(p => `<tr>
      <td><a href="${projectHref(p.name)}">${esc(p.name)}</a>${p.app ? '' : ' <span class="muted small">(folder)</span>'}</td>
      <td style="min-width:160px">${modelBar(p.models)}<div class="small muted">${Object.keys(p.models).length} model${Object.keys(p.models).length === 1 ? '' : 's'}</div></td>
      <td><div class="chips" data-no-search>${Object.keys(p.harnesses).map(toolChip).join('')}</div></td>
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
    <div class="row"><h1 class="grow">${esc(p.project)}</h1>
      <a href="#/compose?context=${encodeURIComponent(info.contexts[0] || '')}"><button class="primary">New prompt here</button></a>
      ${info.contexts[0] ? `<button id="proj-term">Terminal</button>` : ''}</div>
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
          <td><span class="swatch" style="background:${toolColor(h)}"></span> ${esc(toolLabel(h))}</td><td class="num">${n}</td></tr>`).join('')}</tbody></table>
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
    <div id="brief"></div>
    <h2>Notes</h2>
    <div class="list">${p.notes.map(n => `<div class="item"><a href="#/note/${encodeURIComponent(n.id)}">${esc(n.title)}</a></div>`).join('')}</div>
    <p><a href="#/note/new?project=${encodeURIComponent(p.project)}"><button>Add a note to ${esc(p.project)}</button></a></p>
    ${p.actions.filter(a => !a.done).length ? `<h2>Open action items</h2><div class="list small">${p.actions.filter(a => !a.done).map(a =>
      `<div>☐ ${esc(a.text)} <a class="muted" href="${sessionHref(a.key)}">${esc(a.title)}</a></div>`).join('')}</div>` : ''}
    <h2>Sessions, oldest first</h2>
    <div class="list">${p.sessions.map(sessionItem).join('')}</div>`;
  $('#chart').append(activityChart(p.days));
  const loadBrief = async () => { $('#brief').innerHTML = await briefSection(p.project); wireBrief(p.project, loadBrief); };
  loadBrief();
  if ($('#proj-term')) $('#proj-term').onclick = () => api('open-terminal', {context: info.contexts[0]}).then(() => toast('Opening a terminal…'), err => toast(err.message));
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
      <div class="chips" data-no-search>${Object.entries(x.harnesses).map(([h, n]) => `${toolChip(h)}<span class="small muted">${n}</span>`).join('')}</div>
      <p class="small" style="margin-top:8px"><a href="${searchHref('model:' + x.model)}">See its prompts</a> ·
        <a href="#/compose?model=${encodeURIComponent(x.model)}&harness=${encodeURIComponent(Object.keys(x.harnesses)[0] || '')}">New prompt with it</a></p>
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
      <div class="meta">${ago(n.updated)}${n.project ? ` · <a href="${projectHref(n.project)}">${esc(n.project)}</a>` : ''}${n.runs ? ` · run ${n.runs}×` : ''}${n.schedule?.time ? ` · ${badge(describeSchedule(n.schedule))}` : ''}</div>
      <div class="text small">${esc(n.body.slice(0, 220))}${n.body.length > 220 ? '…' : ''}</div>
      ${chips(n.tags)}</div>`).join('') || '<p class="empty">No notes yet.</p>'}</div>`;
};

const DAY_CHOICES = [['daily', 'every day'], ['weekdays', 'weekdays'], ['weekends', 'weekends'], ['0', 'Mondays'], ['1', 'Tuesdays'], ['2', 'Wednesdays'], ['3', 'Thursdays'], ['4', 'Fridays'], ['5', 'Saturdays'], ['6', 'Sundays']];
const DAY_WORDS = {'0123456': 'every day', '01234': 'weekdays', '56': 'weekends'};
function describeSchedule(s) {
  if (!s || !s.time) return '';
  const days = DAY_WORDS[s.days] || s.days.split('').map(d => ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][+d]).join(', ');
  return `${days} at ${s.time}`;
}

views.note = async (params, id) => {
  const [projects, toolsData] = await Promise.all([api('projects'), api('tools').catch(() => ({tools: []}))]);
  const runnable = toolsData.tools.filter(x => x.available && x.kind === 'terminal');
  let note = {title: '', body: '', tags: [], project: params.get('project') || '', session: params.get('session') || '', runs: 0, schedule: {}};
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
      <h2>As a prompt</h2>
      <p class="muted small">A note can be a prompt you run again and again: by hand, from the composer, or on a schedule. It opens the tool in the project's folder with the brief on top.</p>
      <div class="row small">
        <label>Tool <select name="tool">${runnable.map(x => `<option value="${esc(x.name)}"${x.name === (note.tool || 'claude') ? ' selected' : ''}>${esc(x.label)}</option>`).join('') || '<option value="">none installed</option>'}</select></label>
        <label>Model <input name="model" value="${esc(note.model || '')}" placeholder="default" style="width:150px"></label>
        <label>Folder <input name="context" value="${esc(note.context || '')}" placeholder="the project's" style="width:220px"></label>
      </div>
      <div class="row small" style="margin-top:8px">
        <label><input type="checkbox" name="scheduled"${note.schedule?.time ? ' checked' : ''}> On a schedule:</label>
        <select name="days">${DAY_CHOICES.map(([v, l]) => `<option value="${v}"${(note.schedule?.days && (DAY_WORDS[note.schedule.days] === l || note.schedule.days === v)) ? ' selected' : ''}>${l}</option>`).join('')}</select>
        at <input type="time" name="time" value="${esc(note.schedule?.time || '08:00')}">
        <span class="muted">(local time; while promptloom is running)</span>
      </div>
      <div class="row small" style="margin-top:10px">
        ${note.id ? `<button type="button" id="run"${runnable.length ? '' : ' disabled'}>Run as prompt now</button>
        <a href="#/compose?note=${encodeURIComponent(note.id)}"><button type="button">Open in the composer</button></a>` : '<span class="muted">Save the note first.</span>'}
        <span class="muted" id="runs">${note.runs ? `run ${note.runs} time${note.runs > 1 ? 's' : ''}, last ${ago(note.last_run)}${note.last_run_how ? ` (${esc(note.last_run_how)})` : ''}` : 'never run'}${note.next_run ? ` · next ${when(note.next_run)}` : ''}</span>
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
      session: note.session, tool: form.tool.value, model: form.model.value.trim(),
      context: form.context.value.trim(),
      schedule: form.scheduled.checked ? {time: form.time.value, days: form.days.value} : {},
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
  if ($('#run')) $('#run').onclick = async () => {
    try {
      const done = await api('note-run', {id: note.id});
      toast(`Opened ${done.tool}`);
      views.note(params, id);
    } catch (err) { toast(err.message); }
  };
};

// routing

// threads

views.threads = async params => {
  const threads = await api('threads');
  const tools = await api('tools').catch(() => ({tools: []}));
  const have = tools.tools.filter(x => x.available);
  const openId = params.get('open');
  main.innerHTML = `
    <h1>Threads</h1>
    <p class="muted">Lines of work, across tools and models: sessions in a project whose prompts share the same unusual words, close in time, with the drafts and pull requests for that folder. Rename, pin or close them.</p>
    <div class="list">${threads.map(t => `<details class="item thread" data-thread="${esc(t.id)}"${t.id === openId ? ' open' : ''}>
      <summary><div class="row"><span class="grow"><strong>${t.pinned ? '📌 ' : ''}${esc(t.name)}</strong> <span class="muted small">${esc(t.project)} · ${t.sessions.length} session${t.sessions.length > 1 ? 's' : ''} · ${ago(t.updated)}</span></span>
        <span class="chips" data-no-search>${Object.keys(t.tools).map(toolChip).join('')}</span></div></summary>
      <div class="body">
        ${t.words.length ? `<div class="chips">${t.words.map(w => `<span class="chip" data-tag="${esc(w)}">${esc(w)}</span>`).join('')}</div>` : ''}
        <div class="row small" style="margin:8px 0">
          <button data-rename="${esc(t.id)}">Rename</button>
          <button data-pin="${esc(t.id)}">${t.pinned ? 'Unpin' : 'Pin'}</button>
          <button data-close="${esc(t.id)}">Close</button>
          <span class="grow"></span>
          ${t.sessions[0] ? have.map(x => `<a href="#/compose?handoff=${encodeURIComponent(t.sessions[0].key)}&tool=${encodeURIComponent(x.name)}"><button>Continue in ${esc(x.label)}</button></a>`).join('') : ''}</div>
        ${t.open.length ? `<div class="small">${t.open.map(a => `☐ ${esc(a)}`).join('<br>')}</div>` : ''}
        ${t.drafts.length ? `<div class="small" style="margin-top:6px">Drafts: ${t.drafts.map(d => `<a href="#/compose?draft=${encodeURIComponent(d.id)}">${esc(d.title)}</a>`).join(' · ')}</div>` : ''}
        ${t.prs.length ? `<div class="small" style="margin-top:6px">Pull requests: ${t.prs.map(p => `<a href="${esc(p.url)}" target="_blank" rel="noopener">#${p.number} ${esc(p.title)}</a>${p.waiting ? ` ${badge(p.waiting + ' new', 'hot')}` : ''}`).join(' · ')}</div>` : ''}
        <div class="list" style="margin-top:10px">${t.sessions.map(s => `<div class="item slim"><a href="${sessionHref(s.key)}">${esc(s.title)}</a>
          <div class="meta">${toolChip(s.harness)} ${s.prompts} prompts · ${ago(s.updated)}</div>${s.summary ? `<div class="small">${esc(s.summary)}</div>` : ''}</div>`).join('')}</div>
      </div></details>`).join('') || '<p class="empty">Nothing yet.</p>'}</div>`;
  const set = async (id, body) => { await api('thread', {id, ...body}); views.threads(params); };
  main.querySelectorAll('[data-rename]').forEach(b => { b.onclick = () => {
    const name = prompt('Name this thread', b.closest('.thread').querySelector('strong').textContent.replace(/^📌 /, ''));
    if (name !== null) set(b.dataset.rename, {name});
  }; });
  main.querySelectorAll('[data-pin]').forEach(b => { b.onclick = () => set(b.dataset.pin, {pinned: b.textContent === 'Pin'}); });
  main.querySelectorAll('[data-close]').forEach(b => { b.onclick = () => { if (confirm('Close this thread? Its sessions stay; it just leaves the list.')) set(b.dataset.close, {closed: true}); }; });
  main.querySelectorAll('.chip[data-tag]').forEach(el => { el.onclick = e => { e.stopPropagation(); location.hash = searchHref(el.dataset.tag); }; });
  if (openId) main.querySelector(`[data-thread="${CSS.escape(openId)}"]`)?.scrollIntoView();
};

// the brief: what every assistant should know about a project

async function briefSection(project) {
  const b = await api('brief?project=' + encodeURIComponent(project));
  const sugg = b.auto.conventions.filter(c => c.times < 2).slice(0, 8);
  return `<h2>Brief <span class="muted small">what any AI should know first</span></h2>
    <div class="brief">
      <div>
        <div class="label small muted">Your part, on top of what promptloom works out</div>
        <textarea id="brief-own" rows="6" placeholder="Conventions, what matters, what not to touch, who this is for…">${esc(b.own)}</textarea>
        <div class="row small" style="margin-top:6px"><button class="primary" id="brief-save">Save</button><button id="brief-copy">Copy the whole brief</button>
          <span class="muted">${b.own_updated ? `saved ${ago(b.own_updated)}` : ''} · served to assistants as <code>project_brief</code></span></div>
        ${sugg.length || b.auto.pitfalls.length ? `<div class="label small muted" style="margin-top:12px">Things you said that sound like rules or warnings; add the ones that are</div>
        <div class="list">${sugg.map(c => `<div class="item slim row"><span class="grow small">${esc(c.text)} <a class="muted" href="${sessionHref(c.session)}">↗</a></span><button data-add="${esc(c.key)}">Add</button><button data-hide="${esc(c.key)}" class="link">hide</button></div>`).join('')}
        ${b.auto.pitfalls.map(c => `<div class="item slim row"><span class="grow small">⚠ ${esc(c.text)} <a class="muted" href="${sessionHref(c.session)}">↗</a></span><button data-hide="${esc(c.key)}" class="link">hide</button></div>`).join('')}</div>` : ''}
      </div>
      <details><summary class="small">What promptloom works out on its own</summary><div class="note-view small">${markdown(b.text)}</div></details>
    </div>`;
}

function wireBrief(project, reload) {
  const own = $('#brief-own');
  const save = async (extra = {}) => { await api('brief', {project, text: own.value, ...extra}); };
  $('#brief-save').onclick = async () => { await save(); toast('Brief saved'); reload(); };
  $('#brief-copy').onclick = async () => { const b = await api('brief?project=' + encodeURIComponent(project)); copy(b.text); };
  main.querySelectorAll('[data-add]').forEach(btn => { btn.onclick = async () => {
    const text = btn.closest('.item').querySelector('span').textContent.replace(/\s*↗\s*$/, '').trim();
    own.value = (own.value.trim() ? own.value.trim() + '\n' : '') + '- ' + text;
    await save({hide: btn.dataset.add});
    reload();
  }; });
  main.querySelectorAll('[data-hide]').forEach(btn => { btn.onclick = async () => { await api('brief', {project, hide: btn.dataset.hide}); reload(); }; });
}

// the command palette: search or jump from anywhere

let paletteData = null;
async function paletteIndex() {
  if (paletteData) return paletteData;
  const [projects, threads, sessions] = await Promise.all([
    api('projects').catch(() => []), api('threads').catch(() => []), api('sessions').catch(() => [])]);
  paletteData = {projects, threads, sessions: sessions.slice(0, 200)};
  return paletteData;
}

function openPalette() {
  if ($('dialog.palette')) return;
  const dlg = document.createElement('dialog');
  dlg.className = 'palette';
  dlg.innerHTML = `<input placeholder="Search prompts, or jump to a project, thread, session or page…" autofocus><div class="results"></div>`;
  document.body.append(dlg);
  const input = $('input', dlg), results = $('.results', dlg);
  const pages = [['Loom', '#/'], ['Threads', '#/threads'], ['Projects', '#/projects'], ['Drafts', '#/drafts'],
    ['Pull requests', '#/prs'], ['Tools', '#/tools'], ['Models', '#/models'], ['Graph', '#/graph'],
    ['Connections', '#/connections'], ['Notes', '#/notes'], ['Worktrees', '#/worktrees'], ['New prompt', '#/compose']];
  let items = [], selected = 0, turn = 0;
  const render = () => {
    results.innerHTML = items.map((it, i) => `<div class="result${i === selected ? ' on' : ''}" data-i="${i}"><span class="k">${esc(it.kind)}</span>${esc(it.label)}${it.sub ? `<span class="muted small"> · ${esc(it.sub)}</span>` : ''}</div>`).join('');
    results.querySelector('.on')?.scrollIntoView({block: 'nearest'});
  };
  const go = it => { dlg.close(); location.hash = it.href; };
  const update = async () => {
    const q = input.value.trim(), lq = q.toLowerCase();
    const hit = s => !lq || s.toLowerCase().includes(lq);
    const mine = ++turn;  // a slower, older lookup must not overwrite a newer one
    const d = await paletteIndex();
    if (mine !== turn) return;
    items = [];
    if (q) items.push({kind: 'search', label: `Search prompts for “${q}”`, href: searchHref(q)});
    for (const p of d.projects.filter(p => hit(p.name)).slice(0, 5)) {
      items.push({kind: 'project', label: p.name, sub: `${p.sessions} sessions`, href: projectHref(p.name)});
      if (q) items.push({kind: 'new', label: `New prompt in ${p.name}`, href: `#/compose?context=${encodeURIComponent(p.contexts[0] || '')}`});
    }
    for (const t of d.threads.filter(t => hit(t.name) || hit(t.project)).slice(0, 5))
      items.push({kind: 'thread', label: t.name, sub: t.project, href: `#/threads?open=${encodeURIComponent(t.id)}`});
    for (const s of d.sessions.filter(s => hit(s.title)).slice(0, q ? 6 : 3))
      items.push({kind: 'session', label: s.title, sub: `${s.project} · ${ago(s.updated)}`, href: sessionHref(s.key)});
    for (const [label, href] of pages.filter(([l]) => hit(l)).slice(0, q ? 4 : 12))
      items.push({kind: 'page', label, href});
    selected = 0;
    render();
  };
  input.oninput = update;
  input.onkeydown = e => {
    if (e.key === 'ArrowDown') { selected = Math.min(items.length - 1, selected + 1); render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { selected = Math.max(0, selected - 1); render(); e.preventDefault(); }
    else if (e.key === 'Enter' && items[selected]) { e.preventDefault(); go(items[selected]); }
  };
  results.onclick = e => { const el = e.target.closest('.result'); if (el) go(items[+el.dataset.i]); };
  dlg.onclick = e => { if (e.target === dlg) dlg.close(); };
  dlg.addEventListener('close', () => dlg.remove());
  dlg.showModal();
  update();
}
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
  else if (e.key === 'Escape') $('dialog.palette')?.close();
});
document.getElementById('palette-open').onclick = openPalette;

// tools (harnesses)

views.tools = async () => {
  const t = await api('tools');
  const stats = t.stats;
  const models = [...new Set(stats.flatMap(x => Object.keys(x.models)))].filter(m => m !== 'unknown');
  const max = Math.max(1, ...stats.flatMap(x => Object.values(x.models)));
  const byHarness = Object.fromEntries(t.tools.map(x => [x.harness, x]));
  main.innerHTML = `
    <h1>Tools</h1>
    <p class="muted">The AI tools you work in, apart from the models behind them: how much each has done, where, and with what.</p>
    <div class="cards">${stats.map(x => `<div class="card">
      <h3><span class="swatch" style="background:${toolColor(x.harness)}"></span> ${esc(x.label)}</h3>
      <div class="small muted">${x.sessions} sessions · ${x.prompts} prompts · ${Object.keys(x.projects).length} projects</div>
      <div class="small muted">${x.first ? `${ago(x.first)} → ${ago(x.last)}` : ''}</div>
      ${modelBar(x.models)}
      <div class="small" style="margin-top:4px">${Object.entries(x.models).slice(0, 4).map(([m, n]) => `<span class="swatch" style="background:${modelColor(m)}"></span> ${esc(m)} ${n}`).join(' · ')}</div>
      <p class="small" style="margin-top:8px"><a href="${searchHref('harness:' + x.harness)}">See its prompts</a>
        ${byHarness[x.harness]?.available ? ` · <a href="#/compose?tool=${encodeURIComponent(byHarness[x.harness].name)}">New prompt</a>` : ''}</p>
    </div>`).join('') || '<p class="empty">Nothing captured yet.</p>'}</div>
    <h2>Installed here</h2>
    <div class="chips">${t.tools.map(x => `<span class="chip ${x.available ? '' : 'off'}" title="${x.available ? esc(x.program) : 'not found on PATH'}">${esc(x.label)}${x.available ? '' : ' · not installed'}</span>`).join('')}</div>
    <p class="small muted">Only installed tools are offered when you start a prompt. Add others, or change how one is started, under <code>[tools.agents]</code> in the config.</p>
    <h2>Tools × models</h2>
    <p class="muted small">Prompts each tool sent to each model.</p>
    <div style="overflow:auto"><table class="matrix"><thead><tr><th>Tool</th>${models.map(n =>
      `<th class="small" title="${esc(n)}"><span class="swatch" style="background:${modelColor(n)}"></span> ${esc(n.length > 18 ? n.slice(0, 17) + '…' : n)}</th>`).join('')}</tr></thead>
    <tbody>${stats.map(x => `<tr><td><span class="swatch" style="background:${toolColor(x.harness)}"></span> ${esc(x.label)}</td>${models.map(m => {
      const n = x.models[m] || 0;
      const a = n ? 0.15 + 0.85 * n / max : 0;
      return `<td class="cell"><div style="background:${n ? `color-mix(in oklab, ${toolColor(x.harness)} ${Math.round(a * 100)}%, var(--surface-1))` : 'transparent'}">${n || ''}</div></td>`;
    }).join('')}</tr>`).join('')}</tbody></table></div>
    <h2>Tools × projects</h2>
    <table><thead><tr><th>Tool</th><th>Projects, most prompts first</th></tr></thead><tbody>${stats.map(x => `<tr>
      <td><span class="swatch" style="background:${toolColor(x.harness)}"></span> ${esc(x.label)}</td>
      <td class="small">${Object.entries(x.projects).map(([p, n]) => `<a href="${projectHref(p)}">${esc(p)}</a> <span class="muted">${n}</span>`).join(' · ')}</td></tr>`).join('')}</tbody></table>`;
};

// composing a prompt

let composeTimer = 0;

views.compose = async params => {
  const t = await api('tools');
  let draft = null;
  if (params.get('draft')) {
    try { draft = await api('draft?id=' + encodeURIComponent(params.get('draft'))); } catch { draft = null; }
  }
  // Picking a session up elsewhere, or starting from a suggested prompt.
  let seed = params.get('idea') || '';
  let seedContext = '';
  const noteId = params.get('note') || '';
  if (noteId) {
    const note = (await api('notes').catch(() => [])).find(n => n.id === noteId);
    if (note) {
      seed = note.body;
      seedContext = note.context || (await api('projects').catch(() => [])).find(p => p.name === note.project)?.contexts[0] || '';
    }
  }
  if (params.get('handoff')) {
    try {
      const h = await api(`handoff?key=${encodeURIComponent(params.get('handoff'))}&tool=${encodeURIComponent(params.get('tool') || '')}`);
      seed = h.prompt;
      seedContext = h.context;
    } catch (err) { toast(err.message); }
  }
  const installed = t.tools.filter(x => x.available);
  const byName = Object.fromEntries(t.tools.map(x => [x.name, x]));
  const wantHarness = params.get('harness');
  let tool = draft?.tool || params.get('tool') ||
    (wantHarness && installed.find(x => x.harness === wantHarness)?.name) ||
    localStorage.getItem('pl.tool') || installed[0]?.name || '';
  if (!byName[tool]?.available) tool = installed[0]?.name || '';
  const model = draft?.model ?? params.get('model') ?? '';
  const context = draft?.context || params.get('context') || seedContext || t.contexts[0]?.context || '';
  const weaver = t.weavers.find(w => w.default)?.id || t.weavers[0]?.id || '';
  const contextKnown = t.contexts.some(c => c.context === context);

  main.innerHTML = `
    <div class="row"><h1 class="grow">${draft ? 'Draft' : 'New prompt'}</h1>
      <span class="muted small" id="saved">${draft ? `saved ${ago(draft.updated)}` : 'not saved yet'}</span></div>
    <div class="compose">
      <div class="editor">
        <textarea id="body" placeholder="What do you want done? Write it the way you'd say it; weave it afterwards if you want a second pair of eyes on it.">${esc(draft?.body || seed)}</textarea>
        <label class="toggle" style="margin-top:8px"><input type="checkbox" id="with-brief"${seed.includes('# About ') ? '' : ' checked'}> Put the project brief on top <span class="muted" id="brief-size"></span></label>
        <details id="brief-preview" hidden><summary class="small">The brief that will go on top</summary><pre class="prompt-box" id="brief-text"></pre></details>
        <div class="hints" id="hints">
          <div class="row small muted"><span class="grow">As you type: what your history says about it${t.weavers.length ? ', and a model\'s notes' : ''}</span>
            ${t.weavers.length ? `<label class="toggle"><input type="checkbox" id="hints-on"${localStorage.getItem('pl.hints') === '1' ? ' checked' : ''}> model notes with <select id="hints-model">${t.weavers.map(w => `<option value="${esc(w.id)}"${w.id === (localStorage.getItem('pl.hintsModel') || t.weavers.find(x => /cheap|haiku|mini|flash/.test(x.label))?.id || weaver) ? ' selected' : ''}>${esc(w.label)}</option>`).join('')}</select></label>` : ''}</div>
          <div id="tidbits"></div><div id="model-hints"></div>
        </div>
        <div class="row small muted" id="status">${draft?.sent ? `Sent to ${esc(draft.sent_with)} ${ago(draft.sent)}.` : ''}</div>
        ${draft?.versions?.length ? `<details class="versions"><summary class="small">Woven ${draft.versions.length} time${draft.versions.length > 1 ? 's' : ''}</summary>
          ${draft.versions.slice().reverse().map((v, i) => `<div class="version">
            <div class="meta">${esc(v.by)} · ${ago(v.at)}${v.notes ? ` · ${esc(v.notes)}` : ''}</div>
            <div class="row small"><button data-restore="${draft.versions.length - 1 - i}">Put back what I had before this</button></div></div>`).join('')}</details>` : ''}
      </div>
      <div class="side">
        <div class="field"><div class="label">Tool</div>
          ${[['terminal', 'In a terminal, in the project'], ['app', 'Desktop apps'], ['web', 'In the browser'], ['gui', 'Editors (prompt copied)']].map(([kind, title]) => {
            const group = installed.filter(x => x.kind === kind);
            return group.length ? `<div class="small muted" style="margin:6px 0 2px">${title}</div><div class="seg vertical">${group.map(x =>
              `<label title="${esc(x.program)}"><input type="radio" name="tool" value="${esc(x.name)}"${x.name === tool ? ' checked' : ''}>
                <span class="grow">${esc(x.label)}</span>${x.clipboard ? '<span class="muted small">prompt copied</span>' : ''}</label>`).join('')}</div>` : '';
          }).join('') || '<div class="small muted">No tool found. Install Claude Code, Codex, Copilot, Hermes or pi, or add one under [tools.agents].</div>'}
          ${t.tools.some(x => !x.available) ? `<div class="small muted" style="margin-top:4px">Not installed: ${t.tools.filter(x => !x.available).map(x => esc(x.label)).join(', ')}</div>` : ''}</div>
        <div class="field"><div class="label">Model <span class="muted">(blank: the tool's default)</span></div>
          <input id="model" list="models" value="${esc(model)}" placeholder="default">
          <datalist id="models"></datalist></div>
        <div class="field"><div class="label">Project</div>
          <select id="context-pick">${t.contexts.map(c => `<option value="${esc(c.context)}"${c.context === context ? ' selected' : ''}>${esc(c.project)} — ${esc(c.context)}</option>`).join('')}
            <option value=""${contextKnown ? '' : ' selected'}>Another folder…</option></select>
          <input id="context" value="${esc(context)}" placeholder="/path/to/project"${contextKnown ? ' hidden' : ''}></div>
        <div class="field"><div class="label">Weave it into a better prompt</div>
          ${t.weavers.length ? `<div class="row"><select id="weaver" class="grow">${t.weavers.map(w => `<option value="${esc(w.id)}"${w.id === weaver ? ' selected' : ''}>${esc(w.label)}</option>`).join('')}</select>
            <button id="weave">Weave</button></div>
            <div class="small muted">Sends the draft to that model and asks for a stronger prompt back: goal, what done looks like, constraints. You keep both.</div>`
            : '<div class="small muted">No model to weave with: start Ollama, or store a key with <code>promptloom provider set</code>.</div>'}
          <div id="weave-notes" class="small notes" hidden></div></div>
        <div class="actions">
          <button class="primary" id="go"${tool ? '' : ' disabled'}>Open in ${esc(byName[tool]?.label || 'a tool')}</button>
          <button id="save">Save draft</button>
          <button id="copy">Copy</button>
          ${draft ? '<button class="danger" id="delete">Delete</button>' : ''}
        </div>
      </div>
    </div>`;

  const body = $('#body');
  // The brief: what any assistant should know about the project, on top of the prompt.
  let briefText = '';
  const loadBrief = async () => {
    const dir = $('#context').value.trim();
    const project = t.contexts.find(c => c.context === dir)?.project || dir.split('/').filter(Boolean).pop() || '';
    briefText = '';
    try { briefText = project ? (await api('brief?project=' + encodeURIComponent(project))).prompt : ''; } catch { briefText = ''; }
    $('#brief-size').textContent = briefText ? `(${briefText.split('\n').length} lines for ${project})` : '(none for this folder yet)';
    $('#with-brief').disabled = !briefText;
    $('#brief-preview').hidden = !briefText;
    $('#brief-text').textContent = briefText;
  };
  loadBrief();
  const withBrief = () => $('#with-brief').checked && briefText ? briefText.trim() + '\n\n---\n\n' + body.value : body.value;
  // Hints while typing: instant tidbits from the history, and (if switched on) a model's notes.
  const KIND = {rule: 'You said', broke: 'Broke before', open: 'Still open', session: 'Related'};
  let tidbitTimer = 0, hintTimer = 0, hintTurn = 0;
  const showTidbits = async () => {
    const text = body.value;
    if (text.trim().length < 6) { $('#tidbits').innerHTML = ''; return; }
    try {
      const d = await api(`tidbits?context=${encodeURIComponent($('#context').value.trim())}&q=${encodeURIComponent(text.slice(-1500))}`);
      $('#tidbits').innerHTML = d.items.map(i => `<div class="tidbit ${i.kind}"><span class="k">${KIND[i.kind]}</span>
        ${i.kind === 'session' ? `<a href="${sessionHref(i.session)}">${esc(i.text)}</a> <span class="muted">${esc(i.project)} · ${i.words.map(esc).join(', ')}</span>${i.summary ? `<div class="muted">${esc(i.summary)}</div>` : ''}`
          : `${esc(i.text)}${i.session ? ` <a class="muted" href="${sessionHref(i.session)}">↗</a>` : ''}`}</div>`).join('');
    } catch { $('#tidbits').innerHTML = ''; }
  };
  const showHints = async () => {
    const on = $('#hints-on');
    if (!on || !on.checked || body.value.trim().length < 12) { if ($('#model-hints')) $('#model-hints').innerHTML = ''; return; }
    const mine = ++hintTurn;
    $('#model-hints').innerHTML = '<div class="tidbit thinking"><span class="k">Model</span>thinking…</div>';
    try {
      const h = await api('hints', {weaver: $('#hints-model').value, body: body.value, tool: form.tool(), context: form.context()});
      if (mine !== hintTurn) return;
      const rows = [...h.watch.map(x => ['watch', 'Careful', x]), ...h.decide.map(x => ['decide', 'Decide', x]), ...h.add.map(x => ['add', 'Worth adding', x])];
      $('#model-hints').innerHTML = rows.map(([k, l, x]) => `<div class="tidbit ${k}"><span class="k">${l}</span>${esc(x)}</div>`).join('') || '<div class="tidbit muted"><span class="k">Model</span>nothing to add</div>';
    } catch (err) {
      if (mine === hintTurn) $('#model-hints').innerHTML = `<div class="tidbit muted"><span class="k">Model</span>${esc(err.message)}</div>`;
    }
  };
  const queueHints = () => {
    clearTimeout(tidbitTimer); clearTimeout(hintTimer);
    tidbitTimer = setTimeout(showTidbits, 350);
    hintTimer = setTimeout(showHints, 1800);
  };
  if ($('#hints-on')) {
    $('#hints-on').onchange = e => { localStorage.setItem('pl.hints', e.target.checked ? '1' : '0'); queueHints(); };
    $('#hints-model').onchange = e => { localStorage.setItem('pl.hintsModel', e.target.value); queueHints(); };
  }
  queueHints();
  const form = {
    tool: () => main.querySelector('input[name=tool]:checked')?.value || '',
    model: () => $('#model').value.trim(),
    context: () => $('#context').value.trim(),
  };
  const fillModels = () => {
    const x = byName[form.tool()];
    $('#models').innerHTML = (x?.models || []).map(m => `<option value="${esc(m)}">`).join('');
    $('#go').textContent = `Open in ${x?.label || 'a tool'}`;
    $('#go').disabled = !x;
    $('#model').disabled = x ? !x.takes_model : false;
    $('#model').placeholder = x && !x.takes_model ? 'picked inside the tool' : 'default';
  };
  fillModels();
  main.querySelectorAll('input[name=tool]').forEach(r => { r.onchange = () => { fillModels(); localStorage.setItem('pl.tool', form.tool()); queueSave(); }; });
  $('#context-pick').onchange = e => {
    const v = e.target.value;
    $('#context').hidden = Boolean(v);
    if (v) $('#context').value = v; else $('#context').focus();
    queueSave();
    loadBrief();
  };
  $('#context').onchange = loadBrief;

  let saving = false;
  const save = async () => {
    const text = body.value;
    if (!text.trim() && !draft) return null;
    saving = true;
    try {
      draft = await api('drafts', {id: draft?.id, body: text, tool: form.tool(), model: form.model(), context: form.context()});
      $('#saved').textContent = `saved ${ago(draft.updated)}`;
      if (!params.get('draft')) history.replaceState(null, '', `#/compose?draft=${encodeURIComponent(draft.id)}`);
      loadOverview().catch(() => {});
    } catch (err) {
      $('#saved').textContent = `not saved: ${err.message}`;
    } finally { saving = false; }
    return draft;
  };
  const queueSave = () => { clearTimeout(composeTimer); composeTimer = setTimeout(save, 700); $('#saved').textContent = 'typing…'; };
  body.oninput = () => { queueSave(); queueHints(); };
  $('#model').oninput = queueSave;
  $('#context').oninput = queueSave;
  $('#save').onclick = async () => { clearTimeout(composeTimer); await save(); toast('Saved'); };
  $('#copy').onclick = () => copy(withBrief());
  if ($('#delete')) $('#delete').onclick = async () => {
    if (!confirm('Delete this draft?')) return;
    await api('drafts', {delete: draft.id});
    location.hash = '#/drafts';
  };
  main.querySelectorAll('[data-restore]').forEach(b => {
    b.onclick = () => { body.value = draft.versions[+b.dataset.restore].before; queueSave(); toast('Put back'); };
  });
  if ($('#weave')) $('#weave').onclick = async () => {
    if (!body.value.trim()) { toast('Write something first'); return; }
    clearTimeout(composeTimer);
    const btn = $('#weave');
    btn.disabled = true; btn.textContent = 'Weaving…';
    try {
      await save();
      draft = await api('weave', {draft: draft?.id, body: body.value, weaver: $('#weaver').value, tool: form.tool(), model: form.model(), context: form.context()});
      body.value = draft.body;
      const last = draft.versions[draft.versions.length - 1];
      const notes = $('#weave-notes');
      notes.hidden = false;
      notes.innerHTML = `<strong>Woven by ${esc(last.by)}.</strong> ${esc(last.notes || '')} <button class="link" id="undo-weave">Put back what I had</button>`;
      $('#undo-weave').onclick = () => { body.value = last.before; queueSave(); notes.hidden = true; };
      $('#saved').textContent = `saved ${ago(draft.updated)}`;
      if (!params.get('draft')) history.replaceState(null, '', `#/compose?draft=${encodeURIComponent(draft.id)}`);
    } catch (err) {
      toast(err.message);
    } finally { btn.disabled = false; btn.textContent = 'Weave'; }
  };
  $('#go').onclick = async () => {
    if (!body.value.trim()) { toast('Write something first'); return; }
    clearTimeout(composeTimer);
    const btn = $('#go');
    btn.disabled = true;
    try {
      await save();
      if (byName[form.tool()]?.gui) await navigator.clipboard.writeText(withBrief()).catch(() => {});
      const done = await api('launch', {tool: form.tool(), prompt: withBrief(), model: form.model(), context: form.context(), draft: draft?.id, note: noteId || undefined});
      $('#status').textContent = done.how === 'gui'
        ? `Opened ${done.tool} on ${done.dir}; the prompt is on your clipboard, paste it in.`
        : done.how === 'url'
          ? (done.via_clipboard ? `Opened ${done.tool}; the prompt is on your clipboard (too long for a link), paste it in.` : `Opened ${done.tool} with the prompt filled in.`)
          : `Opened ${done.tool} in a terminal in ${done.dir}.`;
      toast(`Opened ${done.tool}`);
      loadOverview().catch(() => {});
    } catch (err) {
      toast(err.message);
    } finally { btn.disabled = false; }
  };
  if (!draft?.body) body.focus();
};

// drafts

views.drafts = async () => {
  const drafts = await api('drafts');
  const t = await api('tools');
  const labels = Object.fromEntries(t.tools.map(x => [x.name, x.label]));
  const item = d => `<div class="item">
    <div class="row"><a class="grow" href="#/compose?draft=${encodeURIComponent(d.id)}"><h3>${esc(d.title)}</h3></a>
      <a href="#/compose?draft=${encodeURIComponent(d.id)}"><button>${d.sent ? 'Open' : 'Continue'}</button></a></div>
    <div class="meta">${ago(d.updated)}${d.tool ? ` · ${esc(labels[d.tool] || d.tool)}` : ''}${d.model ? ` · ${esc(d.model)}` : ''}${d.context ? ` · ${esc(d.context.split('/').pop())}` : ''}${d.versions?.length ? ` · woven ${d.versions.length}×` : ''}${d.sent ? ` · sent to ${esc(d.sent_with)} ${ago(d.sent)}` : ''}</div>
    <div class="text small clip">${esc(d.body.slice(0, 300))}${d.body.length > 300 ? '…' : ''}</div></div>`;
  const open = drafts.filter(d => !d.sent), sent = drafts.filter(d => d.sent);
  main.innerHTML = `
    <div class="row"><h1 class="grow">Drafts</h1><a href="#/compose"><button class="primary">New prompt</button></a></div>
    <p class="muted">Prompts you are still working on, or don't want to lose before they go to a tool. They save as you type.</p>
    <div class="list">${open.map(item).join('') || '<p class="empty">Nothing in progress.</p>'}</div>
    ${sent.length ? `<h2>Sent</h2><div class="list">${sent.map(item).join('')}</div>` : ''}`;
};

// pull requests

let prFilter = 'all';
let prTimer = 0;
const PR_FILTERS = {all: 'All', mine: 'Mine', others: 'Others', attention: 'Needs you'};

function badge(text, kind = '') {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

function prBadges(p) {
  const tone = s => ['SUCCESS', 'APPROVED'].includes(s) ? 'ok' :
    ['FAILURE', 'ERROR', 'CHANGES_REQUESTED'].includes(s) ? 'bad' : '';
  return [
    p.draft ? badge('draft') : '',
    p.checks ? badge('checks ' + p.checks.toLowerCase(), tone(p.checks)) : '',
    p.decision ? badge(p.decision.toLowerCase().replace(/_/g, ' '), tone(p.decision)) : '',
    p.waiting ? badge(`${p.waiting} new`, 'hot') : '',
    p.asked ? badge('review requested', 'hot') : '',
  ].join('');
}

function feedbackList(p) {
  if (!p.feedback.length) return '';
  const where = f => f.kind === 'line' ? `${f.path}${f.line ? ':' + f.line : ''}` :
    f.kind === 'review' ? (f.state === 'CHANGES_REQUESTED' ? 'requested changes' : 'review') : 'comment';
  return `<details class="feedback"${p.waiting ? ' open' : ''}><summary class="small">${p.feedback.length} open comment${p.feedback.length > 1 ? 's' : ''}</summary>
    ${p.feedback.map(f => `<div class="fb${f.new ? ' new' : ''}"><div class="meta"><strong>${esc(f.author)}</strong> · ${esc(where(f))} · ${ago(f.at)}
      ${f.new ? badge('new', 'hot') : ''} <a href="${esc(f.url)}" target="_blank" rel="noopener">open</a></div>
      <div class="text small">${esc(f.body.slice(0, 600))}${f.body.length > 600 ? '…' : ''}</div></div>`).join('')}</details>`;
}

function prItem(p) {
  return `<div class="item pr">
    <div class="row"><a class="grow" href="${esc(p.url)}" target="_blank" rel="noopener"><h3>#${p.number} ${esc(p.title)}</h3></a>
      <button data-review="${esc(p.url)}">Review…</button>
      ${p.mine ? `<button data-fix="${esc(p.url)}"${p.waiting ? ' class="primary"' : ''}>Fix…</button>` : ''}</div>
    <div class="meta">${p.mine ? 'yours' : 'by ' + esc(p.author)} · ${esc(p.head)} → ${esc(p.base)} · +${p.additions} −${p.deletions} in ${p.files} files · updated ${ago(p.updated)}
      ${p.sessions.map(s => ` · from <a href="${sessionHref(s.key)}">${esc(s.title)}</a>`).join('')}</div>
    <div class="chips">${prBadges(p)}</div>
    ${feedbackList(p)}</div>`;
}

const RUN_STATUS = {running: 'running…', done: 'done', failed: 'failed', opened: 'opened in a terminal'};

function runItem(r, labels) {
  return `<div class="item run" data-run="${esc(r.id)}">
    <div class="row"><div class="grow"><strong>${r.kind === 'review' ? 'Review' : 'Fix'}</strong> of
      <a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.repo)}#${r.number}</a> ${esc(r.title)}</div>
      ${badge(RUN_STATUS[r.status] || r.status, r.status === 'failed' ? 'bad' : r.status === 'done' ? 'ok' : '')}
      ${r.has_output ? `<button data-read="${esc(r.id)}">Read</button>` : ''}</div>
    <div class="meta">${esc(labels[r.agent] || r.agent)} · ${ago(r.started)}${r.extra ? ` · added: “${esc(r.extra.slice(0, 80))}”` : ''}</div>
    ${r.error ? `<div class="small" style="color:var(--danger)">${esc(r.error)}</div>` : ''}
    <div class="out" hidden></div></div>`;
}

async function readRun(el, id) {
  const out = $('.out', el);
  if (!out.hidden) { out.hidden = true; return; }
  const run = await api(`pr-run?id=${encodeURIComponent(id)}`);
  out.innerHTML = `<div class="note-view">${markdown(run.text)}</div>
    <div class="row small"><button data-copy-run>Copy</button></div>`;
  $('[data-copy-run]', out).onclick = () => copy(run.text);
  out.hidden = false;
}

function prDialog(pr, kind, d) {
  const usable = d.agents.filter(a => a.available);
  const chosen = usable.some(a => a.name === d.settings.agent) ? d.settings.agent : usable[0]?.name;
  const dlg = document.createElement('dialog');
  dlg.className = 'dialog';
  dlg.innerHTML = `<form>
    <h2>${kind === 'fix' ? 'Fix' : 'Review'} ${esc(pr.repo)}#${pr.number}</h2>
    <p class="muted small">${esc(pr.title)}</p>
    <div class="field"><div class="label">Agent</div><div class="seg">${d.agents.map(a =>
      `<label class="${a.available ? '' : 'off'}"${a.available ? '' : ' title="Not installed"'}>
        <input type="radio" name="agent" value="${esc(a.name)}"${a.name === chosen ? ' checked' : ''}${a.available ? '' : ' disabled'}>
        ${esc(a.label)}${a.available ? '' : ' <span class="muted">(not installed)</span>'}</label>`).join('')}</div></div>
    ${kind === 'fix' ? (pr.checkout
      ? `<label class="toggle"><input type="checkbox" name="worktree"${d.settings.worktrees ? ' checked' : ''}> Work in a worktree of its own, not in ${esc(pr.checkout)}</label>`
      : `<p class="small muted">No local checkout: it will be cloned first.</p>`) : ''}
    <div class="field"><div class="label">Add to the prompt</div>
      <textarea name="extra" rows="4" placeholder="${kind === 'fix' ? 'e.g. keep the public API as it is; ignore the naming comments' : 'e.g. focus on the database migrations; we target Python 3.11'}"></textarea></div>
    <details><summary class="small">Show the prompt</summary><pre class="prompt-box" data-preview>…</pre></details>
    <p class="small muted">${kind === 'fix'
      ? 'A terminal opens with the agent working on it, so you can watch and step in. It commits and pushes, and asks before replying on GitHub.'
      : 'It runs in the background with read-only access; you get a notification when it is done. Nothing is posted to GitHub.'}</p>
    <div class="row" style="justify-content:flex-end"><button type="button" data-cancel>Cancel</button>
      <button class="primary" data-go${chosen ? '' : ' disabled'}>${kind === 'fix' ? 'Open a terminal and fix' : 'Start the review'}</button></div>
  </form>`;
  document.body.append(dlg);
  const form = $('form', dlg);
  const values = () => ({
    url: pr.url, agent: form.agent?.value || chosen, extra: form.extra.value,
    worktree: form.worktree ? form.worktree.checked : false,
  });
  let pending = 0;
  const preview = () => {
    clearTimeout(pending);
    pending = setTimeout(async () => {
      const v = values();
      const q = new URLSearchParams({kind, url: v.url, extra: v.extra, worktree: v.worktree ? '1' : '0'});
      $('[data-preview]', dlg).textContent = (await api('pr-prompt?' + q)).prompt;
    }, 250);
  };
  $('details', dlg).addEventListener('toggle', preview);
  form.addEventListener('input', () => { if ($('details', dlg).open) preview(); });
  $('[data-cancel]', dlg).onclick = () => dlg.close();
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      await api(kind === 'fix' ? 'pr-fix' : 'pr-review', values());
      dlg.close();
      toast(kind === 'fix' ? 'Opening a terminal…' : 'Review started');
      views.prs(new URLSearchParams());
    } catch (err) {
      toast(err.message);
    }
  };
  dlg.addEventListener('close', () => dlg.remove());
  dlg.showModal();
  form.extra.focus();
}

views.prs = async params => {
  clearTimeout(prTimer);
  const d = await api('prs');
  const s = d.settings;
  const labels = Object.fromEntries(d.agents.map(a => [a.name, a.label]));
  const head = `<div class="row"><h1 class="grow">Pull requests</h1>
    <label class="switch"><input type="checkbox" id="pr-on"${s.enabled ? ' checked' : ''}><span class="knob"></span>Watch pull requests</label></div>`;
  if (!s.enabled) {
    main.innerHTML = head + `
      <p class="muted">Your open pull requests and the ones others opened in your repositories, per project. Have Claude Code, Codex or Copilot review one, with anything you want added to the prompt, and get a notification when someone comments on yours, with a button to fix it.</p>
      <p class="muted">It uses the GitHub CLI (<code>gh</code>), signed in, and checks every ${s.poll_minutes} minutes while it is on.</p>`;
  } else {
    const shown = d.prs.filter(p => prFilter === 'all' || (prFilter === 'mine' ? p.mine :
      prFilter === 'others' ? !p.mine : (p.mine && p.waiting) || p.asked));
    const groups = {};
    for (const p of shown) (groups[p.project] ||= []).push(p);
    main.innerHTML = head + `
      <div class="row small settings">
        <label class="toggle"><input type="checkbox" id="pr-notify"${s.notify ? ' checked' : ''}> Notify me about new comments</label>
        <label class="toggle">Agent <select id="pr-agent">${d.agents.map(a =>
          `<option value="${esc(a.name)}"${a.name === s.agent ? ' selected' : ''}${a.available ? '' : ' disabled'}>${esc(a.label)}${a.available ? '' : ' (not installed)'}</option>`).join('')}</select></label>
        <label class="toggle"><input type="checkbox" id="pr-worktrees"${s.worktrees ? ' checked' : ''}> Fixes use a worktree</label>
        <span class="grow"></span>
        <span class="muted">${d.checking ? 'Checking GitHub…' : d.checked ? `Checked ${ago(d.checked)}${d.login ? ' as @' + esc(d.login) : ''}` : 'Not checked yet'}</span>
        <button id="pr-check"${d.checking ? ' disabled' : ''}>Check now</button>
      </div>
      ${d.error ? `<div class="banner">${esc(d.error)}</div>` : ''}
      <div class="seg filters">${Object.entries(PR_FILTERS).map(([k, v]) =>
        `<button data-filter="${k}" class="${k === prFilter ? 'on' : ''}">${v}</button>`).join('')}</div>
      ${Object.entries(groups).map(([project, list]) => `<h2>${esc(project)}
        <span class="muted small">${esc(list[0].repo)}${list[0].checkout ? ' · ' + esc(list[0].checkout) : ' · no local checkout'}</span></h2>
        <div class="list">${list.map(prItem).join('')}</div>`).join('') ||
        `<p class="empty">${d.checked ? 'No open pull requests here.' : 'Checking GitHub for the first time…'}</p>`}
      ${d.runs.length ? `<h2>Reviews and fixes</h2><div class="list">${d.runs.map(r => runItem(r, labels)).join('')}</div>` : ''}`;
    const save = body => api('pr-settings', body).catch(err => toast(err.message));
    $('#pr-notify').onchange = e => save({notify: e.target.checked});
    $('#pr-worktrees').onchange = e => save({worktrees: e.target.checked});
    $('#pr-agent').onchange = e => save({agent: e.target.value}).then(() => toast('Default agent saved'));
    $('#pr-check').onclick = async () => { await api('pr-check', {}); views.prs(params); };
    main.querySelectorAll('[data-filter]').forEach(b => { b.onclick = () => { prFilter = b.dataset.filter; views.prs(params); }; });
    const byUrl = Object.fromEntries(d.prs.map(p => [p.url, p]));
    main.querySelectorAll('[data-review]').forEach(b => { b.onclick = () => prDialog(byUrl[b.dataset.review], 'review', d); });
    main.querySelectorAll('[data-fix]').forEach(b => { b.onclick = () => prDialog(byUrl[b.dataset.fix], 'fix', d); });
    main.querySelectorAll('[data-read]').forEach(b => { b.onclick = () => readRun(b.closest('.run'), b.dataset.read); });
    const wanted = params.get('run');
    const el = wanted && main.querySelector(`.run[data-run="${CSS.escape(wanted)}"]`);
    if (el && el.querySelector('[data-read]')) { readRun(el, wanted); el.scrollIntoView(); }
    // Keep the page current while GitHub is being asked or a review is running.
    if (d.checking || !d.checked || d.runs.some(r => r.status === 'running'))
      prTimer = setTimeout(() => { if (location.hash.startsWith('#/prs') && !$('dialog[open]')) views.prs(new URLSearchParams()); }, 4000);
  }
  $('#pr-on').onchange = async e => {
    await api('pr-settings', {enabled: e.target.checked});
    if (e.target.checked) await api('pr-check', {});
    views.prs(params);
  };
};

// worktrees

views.worktrees = async () => {
  const d = await api('worktrees');
  const all = d.repos.flatMap(r => r.worktrees);
  const stale = all.filter(t => t.stale);
  main.innerHTML = `
    <h1>Worktrees</h1>
    <p class="muted">Extra working folders for your checkouts, each on a branch of its own, so an agent can work on one thing without disturbing another. A worktree is stale when removing it loses nothing: no uncommitted or untracked files, every commit already on a remote, and nothing running in it.</p>
    <h2>New worktree</h2>
    <form class="row" id="wt-add">
      <select name="checkout" required>${d.checkouts.map(c =>
        `<option value="${esc(c.checkout)}">${esc(c.name)}</option>`).join('')}</select>
      <input name="branch" placeholder="branch, new or existing" required>
      <input name="base" placeholder="start a new branch from (default: current)">
      <button class="primary">Create</button>
    </form>
    <p class="small muted">It goes in <code>&lt;checkout&gt;/.worktrees/&lt;branch&gt;</code>, which git is told to ignore.</p>
    ${stale.length ? `<div class="banner ok row"><span class="grow">${stale.length} stale worktree${stale.length > 1 ? 's' : ''} can go without losing anything.</span><button class="primary" id="wt-clean">Clean up…</button></div>` : ''}
    ${d.repos.map(r => `<h2>${esc(r.name)} <span class="muted small">${esc(r.checkout)} · ${esc(r.branch || 'detached')}</span></h2>
      <div class="list">${r.worktrees.map(t => `<div class="item">
        <div class="row"><div class="grow"><strong>${esc(t.branch || 'detached ' + t.head.slice(0, 8))}</strong>
          ${t.stale ? badge('stale', 'ok') : badge('keep')}${t.missing ? badge('folder gone', 'bad') : ''}</div>
          ${t.missing ? '' : `<button data-term="${esc(t.path)}">Terminal</button>`}
          <button data-copy-path="${esc(t.path)}">Copy path</button>
          ${t.stale ? `<button class="danger" data-remove-wt="${esc(t.path)}">Remove</button>` : ''}</div>
        <div class="meta"><code>${esc(t.path)}</code></div>
        <div class="small">${esc(t.reason)}${t.upstream && !t.stale ? ` · tracks ${esc(t.upstream)}` : ''}</div></div>`).join('')}</div>`).join('') ||
      '<p class="empty">No worktrees besides the checkouts themselves.</p>'}`;
  $('#wt-add').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    try {
      const done = await api('worktree-add', {checkout: f.checkout.value, branch: f.branch.value, base: f.base.value});
      toast('Created ' + done.path);
      views.worktrees();
    } catch (err) { toast(err.message); }
  };
  const remove = async (paths, deleteBranches) => {
    const done = await api('worktree-clean', {paths, delete_branches: deleteBranches});
    const failed = done.results.filter(r => !r.ok);
    toast(failed.length ? failed[0].message : `Removed ${done.results.length}`);
    views.worktrees();
  };
  if ($('#wt-clean')) $('#wt-clean').onclick = () => {
    const dlg = document.createElement('dialog');
    dlg.className = 'dialog';
    dlg.innerHTML = `<form><h2>Remove ${stale.length} stale worktree${stale.length > 1 ? 's' : ''}?</h2>
      <ul class="small">${stale.map(t => `<li><code>${esc(t.path)}</code><br><span class="muted">${esc(t.reason)}</span></li>`).join('')}</ul>
      <label class="toggle"><input type="checkbox" name="branches"> Also delete their local branches (git keeps any that are not merged)</label>
      <p class="small muted">Each one is checked again just before it goes; git refuses to remove one that changed.</p>
      <div class="row" style="justify-content:flex-end"><button type="button" data-cancel>Cancel</button><button class="primary danger-bg">Remove</button></div></form>`;
    document.body.append(dlg);
    $('[data-cancel]', dlg).onclick = () => dlg.close();
    $('form', dlg).onsubmit = async e => {
      e.preventDefault();
      dlg.close();
      await remove(stale.map(t => t.path), e.target.branches.checked);
    };
    dlg.addEventListener('close', () => dlg.remove());
    dlg.showModal();
  };
  main.querySelectorAll('[data-remove-wt]').forEach(b => {
    b.onclick = () => { if (confirm(`Remove ${b.dataset.removeWt}?`)) remove([b.dataset.removeWt], false); };
  });
  main.querySelectorAll('[data-copy-path]').forEach(b => { b.onclick = () => copy(b.dataset.copyPath); });
  main.querySelectorAll('[data-term]').forEach(b => {
    b.onclick = () => api('worktree-terminal', {path: b.dataset.term}).then(() => toast('Opening a terminal…'), err => toast(err.message));
  });
};

async function route() {
  hideTip();
  const raw = location.hash.slice(1) || '/';
  const [path, query = ''] = raw.split('#')[0].split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  const name = parts[0] || 'home';
  const params = new URLSearchParams(query);
  const current = name === 'session' ? 'search' : name === 'project' ? 'projects' : name === 'note' ? 'notes' : name;
  for (const a of document.querySelectorAll('header a[data-nav]'))
    a.classList.toggle('on', a.dataset.nav === current);
  const more = $('header .more');
  more.open = false;
  more.classList.toggle('on', Boolean(more.querySelector(`a[data-nav="${current}"]`)));
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
