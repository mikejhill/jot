// Jot web UI shell: capture, list, board, and a jot drawer over the HTTP API.
// Plugins extend it through named slots (see window.jot.register below).
import { h, render, Component } from './vendor/preact.mjs';
import htm from './vendor/htm.mjs';
import { markdown } from './markdown.js';

const html = htm.bind(h);
const CATEGORIES = ['open', 'active', 'done', 'cancelled'];
const human = value => String(value ?? '').replaceAll('_', ' ');
const date = value => (value ? new Date(value).toLocaleString() : '—');
const title = jot => jot.title || (jot.body || '').trim().split('\n')[0].slice(0, 80) || `Jot ${jot.id}`;

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export class Api {
  static async request(path, method = 'GET', body) {
    const headers = { 'X-Jot-Origin': 'ui' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(`/api${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (response.status === 204) return null;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(response.status, data.error || JSON.stringify(data.detail || data) || response.statusText);
    return data;
  }
  static query(params) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      for (const item of [].concat(value)) if (item !== '' && item !== false && item != null) search.append(key, String(item));
    }
    const text = search.toString();
    return text ? `?${text}` : '';
  }
}

// Extension slots. Each entry is a function (props) => vnode.
//   views            {id, label, render(props)}   extra top-level tabs
//   jot.badges       (props {jot})                 inline in list rows and cards
//   jot.actions      (props {jot, refresh})        drawer toolbar
//   jot.panels       (props {detail, refresh})     drawer sections
//   settings.pages   {id, label, render(props)}    sections on the Settings page
export const SLOTS = ['views', 'jot.badges', 'jot.actions', 'jot.panels', 'settings.pages'];
const registry = Object.fromEntries(SLOTS.map(slot => [slot, []]));
window.jot = {
  version: 1, api: Api, html, slots: SLOTS,
  register(slot, item) {
    if (!registry[slot]) throw new Error(`Unknown slot ${slot}`);
    registry[slot].push(item);
    window.dispatchEvent(new Event('jot:extensions'));
  },
};
const fill = (slot, props) => registry[slot].map((render, index) => html`<span class="slot" data-slot=${slot} key=${index}>${render(props)}</span>`);

export class Route {
  static parse(hash) {
    const parts = (hash || '').replace(/^#\/?/, '').split('/').filter(Boolean);
    if (parts[0] === 'jot' && /^\d+$/.test(parts[1] || '')) return { view: parts[2] || 'list', jot: Number(parts[1]) };
    return { view: parts[0] || 'list', jot: null };
  }
  static build(view, jot) { return jot ? `#/jot/${jot}/${view}` : `#/${view}`; }
}

class App extends Component {
  state = {
    ...Route.parse(location.hash), jots: [], profiles: [], projects: [], labels: [], detail: null, toasts: [],
    filters: { category: ['open', 'active'], profile: '', project: '', label: '', q: '', archived: false },
    capture: '', captureProfile: '', lastEvent: 0, loading: true,
  };

  componentDidMount() {
    this.onHash = () => this.route(Route.parse(location.hash));
    this.onKey = event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); document.getElementById('capture')?.focus(); }
      if (event.key === 'Escape' && this.state.jot) this.close();
    };
    this.onExtensions = () => this.forceUpdate();
    window.addEventListener('hashchange', this.onHash);
    window.addEventListener('keydown', this.onKey);
    window.addEventListener('jot:extensions', this.onExtensions);
    this.boot();
    this.timer = setInterval(() => this.poll(), 3000);
  }

  componentWillUnmount() {
    window.removeEventListener('hashchange', this.onHash);
    window.removeEventListener('keydown', this.onKey);
    window.removeEventListener('jot:extensions', this.onExtensions);
    clearInterval(this.timer);
  }

  async boot() {
    await this.act(async () => {
      const [profiles, events] = await Promise.all([Api.request('/profiles'), Api.request('/events?limit=1000')]);
      this.setState({ profiles, lastEvent: events.length ? events[events.length - 1].id : 0 });
      await this.refresh();
      if (this.state.jot) await this.loadDetail(this.state.jot);
    });
    this.setState({ loading: false });
  }

  async poll() {
    try {
      const events = await Api.request(`/events?after=${this.state.lastEvent}&limit=1000`);
      if (!events.length) return;
      this.setState({ lastEvent: events[events.length - 1].id });
      await this.refresh();
      if (this.state.jot && events.some(e => e.jot_id === this.state.jot)) await this.loadDetail(this.state.jot);
    } catch { /* offline: try again next tick */ }
  }

  route(next) {
    this.setState(next);
    if (next.jot) this.loadDetail(next.jot); else this.setState({ detail: null });
  }

  navigate(view, jot = null) { location.hash = Route.build(view, jot); }
  open(id) { this.navigate(this.state.view, id); }
  close() { this.navigate(this.state.view); }

  toast(message) {
    const id = Math.random();
    this.setState(s => ({ toasts: [...s.toasts, { id, message }] }));
    setTimeout(() => this.setState(s => ({ toasts: s.toasts.filter(t => t.id !== id) })), 6000);
  }

  async act(work) {
    try { return await work(); } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        this.toast('This jot changed elsewhere. Reloaded the latest version; re-apply your edit.');
        if (this.state.jot) await this.loadDetail(this.state.jot);
      } else this.toast(error.message);
      return undefined;
    }
  }

  async refresh() {
    const f = this.state.filters;
    const params = { category: f.category, profile: f.profile, project: f.project, label: f.label, q: f.q, archived: f.archived };
    const [jots, projects, labels] = await Promise.all([
      Api.request(`/jots${Api.query(params)}`), Api.request('/projects'), Api.request('/labels'),
    ]);
    this.setState({ jots, projects, labels });
  }

  async loadDetail(id) {
    try { this.setState({ detail: await Api.request(`/jots/${id}`) }); } catch (error) { this.toast(error.message); this.close(); }
  }

  async mutate(work) {
    const result = await this.act(work);
    await this.act(() => this.refresh());
    if (this.state.jot) await this.loadDetail(this.state.jot);
    return result;
  }

  setFilter(key, value) { this.setState(s => ({ filters: { ...s.filters, [key]: value } }), () => this.act(() => this.refresh())); }
  toggleCategory(category) {
    const current = this.state.filters.category;
    this.setFilter('category', current.includes(category) ? current.filter(c => c !== category) : [...current, category]);
  }

  async capture(event) {
    event.preventDefault();
    const text = this.state.capture.trim();
    if (!text) return;
    const [first, ...rest] = text.split('\n');
    const body = rest.length ? { title: first.trim(), body: rest.join('\n').trim() } : { title: first.trim() };
    if (this.state.captureProfile) body.profile = this.state.captureProfile;
    const jot = await this.mutate(() => Api.request('/jots', 'POST', body));
    if (jot) { this.setState({ capture: '' }); this.toast(`Captured #${jot.id}`); }
  }

  profile(id) { return this.state.profiles.find(p => p.id === id); }

  renderCapture() {
    return html`<form class="capture" onSubmit=${e => this.capture(e)}>
      <textarea id="capture" rows="1" aria-label="Capture a jot" placeholder="Jot something down… (Ctrl+K, Enter to save, Shift+Enter for a new line)"
        value=${this.state.capture} onInput=${e => this.setState({ capture: e.target.value })}
        onKeyDown=${e => { if (e.key === 'Enter' && !e.shiftKey) this.capture(e); }}></textarea>
      <select aria-label="Profile for new jot" value=${this.state.captureProfile} onChange=${e => this.setState({ captureProfile: e.target.value })}>
        <option value="">Default profile</option>${this.state.profiles.map(p => html`<option value=${p.id}>${p.label || p.id}</option>`)}
      </select>
      <button class="primary">Add</button>
    </form>`;
  }

  renderFilters() {
    const f = this.state.filters;
    return html`<div class="filters">
      <div class="chips" role="group" aria-label="Status categories">${CATEGORIES.map(c => html`<button type="button" class=${'chip ' + (f.category.includes(c) ? 'on' : '')} aria-pressed=${f.category.includes(c)} onClick=${() => this.toggleCategory(c)}>${c}</button>`)}</div>
      <input type="search" aria-label="Search" placeholder="Search" value=${f.q} onInput=${e => this.setFilter('q', e.target.value)}/>
      <select aria-label="Filter by profile" value=${f.profile} onChange=${e => this.setFilter('profile', e.target.value)}><option value="">All profiles</option>${this.state.profiles.map(p => html`<option value=${p.id}>${p.label || p.id}</option>`)}</select>
      <select aria-label="Filter by project" value=${f.project} onChange=${e => this.setFilter('project', e.target.value)}><option value="">All projects</option>${this.state.projects.map(p => html`<option value=${p.slug}>${p.name}</option>`)}</select>
      <select aria-label="Filter by label" value=${f.label} onChange=${e => this.setFilter('label', e.target.value)}><option value="">All labels</option>${this.state.labels.map(l => html`<option value=${l.name}>${l.name} (${l.count})</option>`)}</select>
      <label class="check"><input type="checkbox" checked=${f.archived} onChange=${e => this.setFilter('archived', e.target.checked)}/> Archived</label>
    </div>`;
  }

  tags(jot) {
    return html`${jot.labels.map(l => html`<span class="pill label">#${l}</span>`)}${jot.project && html`<span class="pill project">@${jot.project}</span>`}${jot.archived_at && html`<span class="pill">archived</span>`}${fill('jot.badges', { jot })}`;
  }

  renderList() {
    const { jots, loading } = this.state;
    if (!loading && !jots.length) return html`<p class="empty">No jots match. Capture one above.</p>`;
    return html`<ol class="list" aria-label="Jots">${jots.map(j => html`<li key=${j.id}>
      <button class="row" onClick=${() => this.open(j.id)}>
        <span class="id">#${j.id}</span><span class=${'status ' + j.category}>${human(j.status)}</span>
        <span class="title">${title(j)}</span><span class="tags">${this.tags(j)}</span>
        <time class="muted">${date(j.updated_at)}</time>
      </button></li>`)}</ol>`;
  }

  renderBoard() {
    const columns = this.state.filters.category.length ? this.state.filters.category : CATEGORIES;
    return html`<div class="board">${columns.map(category => {
      const jots = this.state.jots.filter(j => j.category === category);
      return html`<section class="column" aria-label=${category}><h2><span class=${'dot ' + category}></span>${category}<span class="count">${jots.length}</span></h2>
        ${jots.map(j => html`<button class="card" key=${j.id} onClick=${() => this.open(j.id)}><strong>${title(j)}</strong><small>${j.profile} · ${human(j.status)}</small><span class="tags">${this.tags(j)}</span></button>`)}</section>`;
    })}</div>`;
  }

  renderSettings() {
    return html`<section class="settings"><h2>Profiles</h2><p class="muted">Defined in <code>config.toml</code> in your Jot home.</p>
      ${this.state.profiles.map(p => html`<article class="panel"><h3>${p.label || p.id} <code>${p.id}</code></h3>
        <p>${p.statuses.map(s => html`<span class=${'status ' + s.category}>${human(s.id)}</span> `)}</p>
        ${Object.keys(p.fields).length > 0 && html`<p class="muted">Fields: ${Object.entries(p.fields).map(([n, s]) => `${n} (${s.type})`).join(', ')}</p>`}
        ${p.plugins.length > 0 && html`<p class="muted">Plugins: ${p.plugins.join(', ')}</p>`}</article>`)}
      ${registry['settings.pages'].map(page => html`<h2>${page.label}</h2>${page.render({ api: Api })}`)}</section>`;
  }

  renderDrawer() {
    const d = this.state.detail;
    if (!this.state.jot || !d) return null;
    return html`<${Drawer} key=${d.jot.id} detail=${d} profile=${this.profile(d.jot.profile)} app=${this}/>`;
  }

  render() {
    const { view, toasts } = this.state;
    const views = [['list', 'List'], ['board', 'Board'], ...registry.views.map(v => [v.id, v.label]), ['settings', 'Settings']];
    const extra = registry.views.find(v => v.id === view);
    let body = this.renderList();
    if (view === 'board') body = this.renderBoard();
    else if (view === 'settings') body = this.renderSettings();
    else if (extra) body = extra.render({ api: Api, refresh: () => this.refresh() });
    return html`<div class="shell"><header><h1>Jot</h1><nav aria-label="Views">${views.map(([id, label]) => html`<a href=${Route.build(id, this.state.jot)} aria-current=${view === id ? 'page' : null}>${label}</a>`)}</nav></header>
      <main>${view !== 'settings' && this.renderCapture()}${(view === 'list' || view === 'board') && this.renderFilters()}${body}</main>
      ${this.renderDrawer()}
      <div class="toasts" aria-live="polite">${toasts.map(t => html`<div role="status" class="toast">${t.message}<button aria-label="Dismiss" onClick=${() => this.setState(s => ({ toasts: s.toasts.filter(x => x.id !== t.id) }))}>✕</button></div>`)}</div></div>`;
  }
}

class Drawer extends Component {
  constructor(props) {
    super(props);
    const { jot } = props.detail;
    this.state = { title: jot.title, body: jot.body, fields: { ...(jot.metadata.fields || {}) }, label: '', comment: '', linkTo: '', linkRel: 'relates' };
  }

  componentDidUpdate(previous) {
    const before = previous.detail.jot;
    const after = this.props.detail.jot;
    if (before.version === after.version) return;
    // Take the new version's values for any field the user hasn't edited.
    const clean = (key, old) => (this.state[key] === old ? after[key] : this.state[key]);
    const fieldsClean = JSON.stringify(this.state.fields) === JSON.stringify(before.metadata.fields || {});
    this.setState({
      title: clean('title', before.title),
      body: clean('body', before.body),
      fields: fieldsClean ? { ...(after.metadata.fields || {}) } : this.state.fields,
    });
  }

  get jot() { return this.props.detail.jot; }
  get app() { return this.props.app; }
  call(path, method, body) { return this.app.mutate(() => Api.request(path, method, body)); }

  async save(event) {
    event.preventDefault();
    const fields = {};
    const current = this.jot.metadata.fields || {};
    for (const [name, value] of Object.entries(this.state.fields)) if (value !== current[name]) fields[name] = value === '' ? null : value;
    const saved = await this.call(`/jots/${this.jot.id}`, 'PATCH', { title: this.state.title, body: this.state.body, fields, expected_version: this.jot.version });
    if (!saved) {
      // Conflict or error: show what is stored now.
      const now = this.props.detail.jot;
      this.setState({ title: now.title, body: now.body, fields: { ...(now.metadata.fields || {}) } });
    }
  }

  addLabel(event) {
    event.preventDefault();
    const name = this.state.label.trim();
    if (name) this.call(`/jots/${this.jot.id}/labels`, 'POST', { add: [name] }).then(() => this.setState({ label: '' }));
  }

  addComment(event) {
    event.preventDefault();
    const body = this.state.comment.trim();
    if (body) this.call(`/jots/${this.jot.id}/comments`, 'POST', { body }).then(() => this.setState({ comment: '' }));
  }

  addLink(event) {
    event.preventDefault();
    const dst = Number(this.state.linkTo);
    if (dst) this.call('/links', 'POST', { src: this.jot.id, dst, rel: this.state.linkRel }).then(() => this.setState({ linkTo: '' }));
  }

  field(name, spec) {
    const value = this.state.fields[name] ?? '';
    const set = v => this.setState(s => ({ fields: { ...s.fields, [name]: v } }));
    const label = spec.label || human(name);
    if (spec.type === 'enum') return html`<label>${label}<select value=${value} onChange=${e => set(e.target.value)}><option value="">—</option>${spec.options.map(o => html`<option value=${o}>${o}</option>`)}</select></label>`;
    if (spec.type === 'boolean') return html`<label class="check"><input type="checkbox" checked=${value === true} onChange=${e => set(e.target.checked)}/> ${label}</label>`;
    const type = { number: 'number', date: 'date' }[spec.type] || 'text';
    return html`<label>${label}<input type=${type} value=${value} onInput=${e => set(type === 'number' && e.target.value !== '' ? Number(e.target.value) : e.target.value)}/></label>`;
  }

  render() {
    const { detail, profile } = this.props;
    const jot = this.jot;
    const app = this.app;
    const fields = Object.entries(profile?.fields || {});
    const statuses = profile?.statuses || [{ id: jot.status, category: jot.category }];
    return html`<aside class="drawer" role="dialog" aria-label=${'Jot ' + jot.id}>
      <header class="drawer-header"><strong>#${jot.id}</strong><span class=${'status ' + jot.category}>${human(jot.status)}</span><small class="muted">v${jot.version}</small>
        <button class="close" aria-label="Close" onClick=${() => app.close()}>✕</button></header>
      <form class="editor" onSubmit=${e => this.save(e)}>
        <input aria-label="Title" class="title-input" value=${this.state.title} placeholder="Title" onInput=${e => this.setState({ title: e.target.value })}/>
        <textarea aria-label="Body" rows="6" placeholder="Details (Markdown)" value=${this.state.body} onInput=${e => this.setState({ body: e.target.value })}></textarea>
        ${fields.length > 0 && html`<div class="fields">${fields.map(([name, spec]) => this.field(name, spec))}</div>`}
        <div class="toolbar"><button class="primary">Save</button></div>
      </form>
      ${jot.body && html`<section class="preview">${markdown(jot.body)}</section>`}
      <section class="toolbar">
        <label>Status<select aria-label="Status" value=${jot.status} onChange=${e => this.call(`/jots/${jot.id}/status`, 'POST', { status: e.target.value, expected_version: jot.version })}>${statuses.map(s => html`<option value=${s.id}>${human(s.id)}</option>`)}</select></label>
        <label>Profile<select aria-label="Profile" value=${jot.profile} onChange=${e => this.call(`/jots/${jot.id}`, 'PATCH', { profile: e.target.value, expected_version: jot.version })}>${app.state.profiles.map(p => html`<option value=${p.id}>${p.label || p.id}</option>`)}</select></label>
        <label>Project<select aria-label="Project" value=${jot.project || ''} onChange=${e => this.call(`/jots/${jot.id}`, 'PATCH', { project: e.target.value || null, expected_version: jot.version })}><option value="">None</option>${app.state.projects.map(p => html`<option value=${p.slug}>${p.name}</option>`)}</select></label>
      </section>
      <section class="toolbar">
        <button onClick=${() => this.call(`/jots/${jot.id}/rank`, 'POST', { placement: 'top' })}>Move to top</button>
        <button onClick=${() => this.call(`/jots/${jot.id}/rank`, 'POST', { placement: 'bottom' })}>Move to bottom</button>
        ${jot.archived_at ? html`<button onClick=${() => this.call(`/jots/${jot.id}/unarchive`, 'POST')}>Unarchive</button>` : html`<button onClick=${() => this.call(`/jots/${jot.id}/archive`, 'POST')}>Archive</button>`}
        ${jot.deleted_at ? html`<button onClick=${() => this.call(`/jots/${jot.id}/restore`, 'POST')}>Restore</button>` : html`<button class="danger" onClick=${() => this.call(`/jots/${jot.id}`, 'DELETE')}>Delete</button>`}
        ${fill('jot.actions', { jot, refresh: () => app.mutate(async () => null) })}
      </section>
      <section><h3>Labels</h3><div class="tags">${jot.labels.map(l => html`<span class="pill label">#${l}<button aria-label=${'Remove label ' + l} onClick=${() => this.call(`/jots/${jot.id}/labels`, 'POST', { remove: [l] })}>×</button></span>`)}</div>
        <form class="inline" onSubmit=${e => this.addLabel(e)}><input aria-label="New label" placeholder="add-label" value=${this.state.label} onInput=${e => this.setState({ label: e.target.value })}/><button>Add</button></form></section>
      <section><h3>Comments</h3>${detail.comments.map(c => html`<article class="comment"><header><strong>${c.author}</strong>${c.kind !== 'comment' && html` <span class="pill">${c.kind}</span>`} <small class="muted">${date(c.created_at)}${c.edited_at ? ' · edited' : ''}</small>
          <button class="text" aria-label=${'Delete comment ' + c.id} onClick=${() => this.call(`/comments/${c.id}`, 'DELETE')}>Delete</button></header>${markdown(c.body)}</article>`)}
        <form class="inline" onSubmit=${e => this.addComment(e)}><textarea aria-label="New comment" rows="2" placeholder="Add a comment" value=${this.state.comment} onInput=${e => this.setState({ comment: e.target.value })}></textarea><button>Comment</button></form></section>
      <section><h3>Links</h3><ul class="links">${detail.links.map(l => html`<li><button class="text" onClick=${() => app.open(l.src === jot.id ? l.dst : l.src)}>#${l.src} ${l.rel} #${l.dst}</button>
          <button class="text" aria-label="Remove link" onClick=${() => this.call('/links/delete', 'POST', l)}>×</button></li>`)}</ul>
        <form class="inline" onSubmit=${e => this.addLink(e)}><select aria-label="Link relation" value=${this.state.linkRel} onChange=${e => this.setState({ linkRel: e.target.value })}>${['relates', 'parent', 'blocks', 'duplicates'].map(r => html`<option value=${r}>${r}</option>`)}</select>
          <input aria-label="Link to jot id" type="number" min="1" placeholder="jot id" value=${this.state.linkTo} onInput=${e => this.setState({ linkTo: e.target.value })}/><button>Link</button></form></section>
      ${fill('jot.panels', { detail, refresh: () => app.mutate(async () => null) })}
      <section><h3>History</h3><ol class="history">${detail.history.map(e => html`<li><small class="muted">${date(e.ts)}</small> ${e.kind}${e.version ? ` v${e.version}` : ''} · ${e.actor} via ${e.origin}${e.reason ? ` (${e.reason})` : ''}</li>`)}</ol></section>
    </aside>`;
  }
}

if (document.getElementById('app')) render(html`<${App}/>`, document.getElementById('app'));
