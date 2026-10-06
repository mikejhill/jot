import { h, render, Component } from './vendor/preact.mjs';
import htm from './vendor/htm.mjs';
import {markdown} from './markdown.js';

const html = htm.bind(h);
const statuses = ['inbox','ready','planning','needs_input','awaiting_approval','executing','review','done','blocked','wont_do','archived'];
const criticalities = ['low','medium','high','critical'];
const types = ['feature','bug','chore','research','idea'];
const backends = ['claude','codex','copilot'];
const human = value => String(value ?? '').replaceAll('_', ' ');
const date = value => value ? new Date(value).toLocaleString() : '—';
const split = value => String(value || '').split(',').map(s => s.trim()).filter(Boolean);
const formValues = event => Object.fromEntries(new FormData(event.currentTarget));
const options = values => values.map(value => html`<option value=${value}>${human(value)}</option>`);
const pill = (value, extra = '') => html`<span class=${`pill ${value} ${extra}`}>${human(value)}</span>`;
const markdownKinds = new Set(['plan','result','comment','question','answer','approval']);
const latest = (events, kind) => [...events].reverse().find(e => e.kind === kind);

class Api {
  static async request(path, method = 'GET', body) {
    const response = await fetch('/api' + path, {
      method, headers: body === undefined ? {} : {'Content-Type':'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      const detail = data.detail;
      throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail)
        ? detail.map(e => `${e.loc.join('.')}: ${e.msg}`).join('; ') : JSON.stringify(data));
    }
    return data;
  }
}

class TaskEditor extends Component {
  constructor(props) { super(props); this.state = {dirty:false, fields:this.fields(props.task)}; }
  fields(t) {
    const due = t.due_at ? new Date(new Date(t.due_at).getTime() - new Date(t.due_at).getTimezoneOffset()*60000).toISOString().slice(0,16) : '';
    return {...t, labels:t.labels.join(', '), project_id:t.project_id || '', flow:t.flow || '', repo_path:t.repo_path || '', due_at:due};
  }
  componentWillReceiveProps(next) {
    if (!this.state.dirty) this.setState({fields:this.fields(next.task)});
  }
  async save(event) {
    event.preventDefault();
    const fields = formValues(event);
    fields.labels = split(fields.labels);
    fields.project_id = fields.project_id ? Number(fields.project_id) : null;
    fields.flow ||= null; fields.repo_path ||= null;
    fields.due_at = fields.due_at ? new Date(fields.due_at).toISOString() : null;
    if (await this.props.app.act(() => Api.request(`/tasks/${this.props.task.id}`, 'PATCH', fields))) this.setState({dirty:false});
  }
  render({projects}, {dirty, fields:t}) {
    return html`<form onSubmit=${e => this.save(e)} onInput=${e => {const {name,value}=e.target; this.setState(s => ({dirty:true,fields:{...s.fields,[name]:value}}));}}>
      <label>Title<input name="title" required value=${t.title}/></label>
      <label>Description<textarea name="description" rows="4" value=${t.description}/></label>
      <div class="grid two"><label>Project<select name="project_id" value=${t.project_id}><option value="">No project</option>${projects.map(p => html`<option value=${p.id}>${p.name}</option>`)}</select></label>
      <label>Labels · comma separated<input name="labels" value=${t.labels}/></label>
      <label>Criticality<select name="criticality" value=${t.criticality}>${options(criticalities)}</select></label>
      <label>Type<select name="type" value=${t.type}>${options(types)}</select></label>
      <label>Flow<select name="flow" value=${t.flow}><option value="">Use defaults</option>${options(['planned','direct'])}</select></label>
      <label>Due date<input type="datetime-local" name="due_at" value=${t.due_at}/></label></div>
      <label>Repository path<input name="repo_path" value=${t.repo_path} placeholder="C:\\Projects\\my-project"/></label>
      <button class="primary" disabled=${!dirty}>Save changes</button>${dirty && html`<small>Unsaved changes</small>`}
    </form>`;
  }
}

class ProjectEditor extends Component {
  constructor(props) { super(props); this.state = {flow:props.project.default_flow || ''}; }
  async save(event) {
    event.preventDefault();
    const body = formValues(event);
    body.aliases = split(body.aliases); body.priority = Number(body.priority);
    body.repo_path ||= null; body.default_flow ||= null;
    const p = this.props.project;
    if (await this.props.app.act(() => Api.request(p.id ? `/projects/${p.id}` : '/projects', p.id ? 'PATCH' : 'POST', body))) this.props.app.setState({projectEdit:null});
  }
  render({project:p, app}) {
    return html`<form class="panel" onSubmit=${e => this.save(e)}><h2>${p.id ? 'Edit project' : 'New project'}</h2>
      <div class="grid two"><label>Name<input name="name" required defaultValue=${p.name || ''}/></label><label>Slug<input name="slug" required pattern="[a-z0-9][a-z0-9_-]*" defaultValue=${p.slug || ''}/></label></div>
      <label>Description<textarea name="description" defaultValue=${p.description || ''}/></label>
      <label>Repository path<input name="repo_path" defaultValue=${p.repo_path || ''}/></label>
      <label>Aliases · comma separated<input name="aliases" defaultValue=${(p.aliases || []).join(', ')}/></label>
      <div class="grid two"><label>Priority multiplier<input type="number" name="priority" min="0.01" step="0.01" required defaultValue=${p.priority || 1}/></label>
      <label>Default flow<select name="default_flow" value=${this.state.flow} onChange=${e => this.setState({flow:e.target.value})}><option value="">Global default</option>${options(['planned','direct'])}</select></label></div>
      <button class="primary">Save project</button><button type="button" onClick=${() => app.setState({projectEdit:null})}>Cancel</button>
    </form>`;
  }
}

class App extends Component {
  constructor() {
    super();
    this.state = {view:'Board', tasks:[], projects:[], runs:[], labels:[], filters:{sort:'priority'}, selected:[], detail:null,
      backend:'claude', picks:App.loadPicks(), config:null, preset:'open', expanded:{}, connected:false, toasts:[], instructionNames:[], instruction:'triage.md', markdown:'', instructionDirty:false,
      proposal:null, approved:[], projectEdit:null, runLogs:{}, openLogs:{}, busy:false, loaded:false};
    this.refreshVersion = 0; this.detailVersion = 0; this.toastId = 0;
  }
  componentDidMount() {
    document.documentElement.dataset.theme = localStorage.getItem('jot-theme') || '';
    this.keydown = e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {e.preventDefault(); document.getElementById('capture').focus();}
      if (e.key === 'Escape') this.closeDetail();
    };
    document.addEventListener('keydown', this.keydown);
    this.refresh().catch(e => this.toast(e.message));
    Api.request('/config').then(config => this.setState(s => ({config, backend:config.drawdown.backend,
      picks:{plan:{model:'', ...s.picks.plan, backend:s.picks.plan?.backend || config.drawdown.backend},
             execute:{model:'', ...s.picks.execute, backend:s.picks.execute?.backend || config.drawdown.backend}}}))).catch(e => this.toast(e.message));
    this.stream = new EventSource('/api/stream');
    this.stream.onopen = () => { this.setState({connected:true}); this.scheduleRefresh(); };
    this.stream.onerror = () => this.setState({connected:false});
    ['task','run'].forEach(topic => this.stream.addEventListener(topic, () => this.scheduleRefresh()));
    this.stream.addEventListener('run_log', e => {
      const entry = JSON.parse(e.data);
      this.setState(s => s.runLogs[entry.run_id] ? {runLogs:{...s.runLogs, [entry.run_id]:[...s.runLogs[entry.run_id], entry]}} : null);
      this.scheduleRefresh();
    });
    // Recover missed bus messages after queue overflow or a disconnected tab.
    this.poll = setInterval(() => this.scheduleRefresh(), 30000);
  }
  componentWillUnmount() {
    this.stream.close(); clearInterval(this.poll); clearTimeout(this.refreshTimer);
    document.removeEventListener('keydown', this.keydown);
  }
  toast(message) {
    const id = ++this.toastId;
    this.setState(s => ({toasts:[...s.toasts, {id,message}]}));
    setTimeout(() => this.setState(s => ({toasts:s.toasts.filter(t => t.id !== id)})), 7000);
  }
  async act(action) {
    try { await action(); await this.refresh(); return true; }
    catch (error) { this.toast(error.message); return false; }
  }
  query() {
    const q = new URLSearchParams();
    Object.entries(this.state.filters).forEach(([key,value]) => {
      if (!value) return;
      if (key === 'labels') split(value).forEach(label => q.append('labels', label));
      else q.set(key,value);
    });
    return q.toString();
  }
  scheduleRefresh() {
    clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refresh().catch(e => this.toast(e.message)), 180);
  }
  async refresh() {
    const version = ++this.refreshVersion;
    const [tasks, projects, runs, labels] = await Promise.all([
      Api.request('/tasks?' + this.query()), Api.request('/projects'), Api.request('/runs'), Api.request('/labels'),
    ]);
    if (version !== this.refreshVersion) return;
    this.setState(s => ({tasks,projects,runs,labels,loaded:true,selected:s.selected.filter(id => tasks.some(t => t.id === id))}));
    if (this.state.detail) await this.openTask(this.state.detail.task.id, false);
    await this.refreshExpanded();
  }
  async refreshExpanded() {
    const ids = Object.keys(this.state.expanded);
    if (!ids.length) return;
    const details = await Promise.all(ids.map(id => Api.request(`/tasks/${id}`).catch(() => null)));
    this.setState(s => {
      const expanded = {...s.expanded};
      ids.forEach((id, i) => { if (details[i] && expanded[id]) expanded[id] = details[i]; });
      return {expanded};
    });
  }
  async openTask(id, focus = true) {
    const version = ++this.detailVersion;
    try {
      const detail = await Api.request(`/tasks/${id}`);
      if (version !== this.detailVersion) return;
      this.setState({detail}, () => {if (focus) document.querySelector('.drawer-close')?.focus();});
      detail.runs.forEach(r => this.loadLog(r.id));
    } catch (error) {this.toast(error.message);}
  }
  async loadLog(runId, force = false) {
    if (this.state.runLogs[runId] && !force) return;
    try { const lines = await Api.request(`/runs/${runId}/log`); this.setState(s => ({runLogs:{...s.runLogs, [runId]:lines}})); }
    catch (error) { this.toast(error.message); }
  }
  toggleLog(runId) {
    const open = !this.state.openLogs[runId];
    this.setState(s => ({openLogs:{...s.openLogs, [runId]:open}}));
    if (open) this.loadLog(runId);
  }
  closeDetail() {this.detailVersion++; this.setState({detail:null});}
  async capture(event) {
    event.preventDefault();
    const input = document.getElementById('capture'), text = input.value.trim();
    if (!text || this.state.busy) return;
    this.setState({busy:true});
    try {
      const task = await Api.request('/tasks','POST',{text}); input.value = '';
      this.setState(s => ({tasks:[task,...s.tasks.filter(t => t.id !== task.id)]}));
      this.toast(`Captured #${task.id} · inbox · enriching…`);
    } catch (error) {this.toast(error.message);}
    finally {this.setState({busy:false}); input.focus();}
  }
  filter(key, value) {this.setState(s => ({filters:{...s.filters,[key]:value}}), () => this.scheduleRefresh());}
  async move(id, status) {await this.act(() => Api.request(`/tasks/${id}/move`, 'POST', {status}));}
  static loadPicks() {
    try { return JSON.parse(localStorage.getItem('jot-picks')) || {plan:{}, execute:{}}; } catch { return {plan:{}, execute:{}}; }
  }
  pick(kind) {const p = this.state.picks[kind] || {}; return {backend:p.backend || this.state.backend, model:(p.model || '').trim() || null};}
  setPick(kind, change) {
    this.setState(s => {
      const picks = {...s.picks, [kind]:{...s.picks[kind], ...change}};
      try { localStorage.setItem('jot-picks', JSON.stringify(picks)); } catch { /* storage unavailable */ }
      return {picks};
    });
  }
  async run(id, flow) {
    const choice = this.pick(flow === 'direct' ? 'execute' : 'plan');
    if (await this.act(() => Api.request(`/tasks/${id}/run`, 'POST', {flow, ...choice}))) this.toast(`#${id}: ${flow === 'direct' ? 'running' : 'planning'} with ${choice.backend}/${choice.model || 'default'}`);
  }
  async approve(id, note) {
    const choice = this.pick('execute');
    if (await this.act(() => Api.request(`/tasks/${id}/approve`, 'POST', {note:note || null, ...choice}))) this.toast(`#${id}: approved, executing with ${choice.backend}/${choice.model || 'default'}`);
  }
  async answer(event, t, d) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const value = name => String(data.get(name) || '').trim();
    const answers = d.questions.map(q => ({question_id:q.id, text:value(`q${q.id}-other`) || value(`q${q.id}`)}));
    const phase = d.resume_phase === 'execute' ? 'execute' : 'plan';
    const choice = this.pick(phase);
    if (await this.act(() => Api.request(`/tasks/${t.id}/answers`, 'POST', {answers, ...choice}))) this.toast(`#${t.id}: answers sent, ${phase === 'execute' ? 'executing' : 'planning'} with ${choice.backend}/${choice.model || 'default'}`);
  }
  async sendBack(id) {
    const comment = prompt('What needs to change?');
    if (comment) await this.act(() => Api.request(`/tasks/${id}/send-back`, 'POST', {comment}));
  }
  async toggleExpand(id) {
    if (this.state.expanded[id]) { this.setState(s => { const expanded = {...s.expanded}; delete expanded[id]; return {expanded}; }); return; }
    try { const detail = await Api.request(`/tasks/${id}`); this.setState(s => ({expanded:{...s.expanded, [id]:detail}})); }
    catch (error) { this.toast(error.message); }
  }
  renderPicker(kind, label) {
    const backend = this.pick(kind).backend, model = this.state.picks[kind]?.model || '';
    const fallback = this.state.config?.model_defaults?.[backend]?.[kind];
    return html`<span class="picker"><small>${label}</small><select aria-label=${label + ' backend'} value=${backend} onChange=${e => this.setPick(kind, {backend:e.target.value, model:''})}>${options(backends)}</select><input class="model-input" aria-label=${label + ' model'} list=${'models-' + backend} value=${model} placeholder=${fallback ? 'default (' + fallback + ')' : 'provider default'} onInput=${e => this.setPick(kind, {model:e.target.value})}/></span>`;
  }
  renderDatalists() {
    const suggestions = this.state.config?.model_suggestions || {};
    return backends.map(b => html`<datalist id=${'models-' + b}>${(suggestions[b] || []).map(m => html`<option value=${m}/>`)}</datalist>`);
  }
  rowActions(t) {
    const active = this.state.runs.find(r => r.task_id === t.id && !r.ended_at);
    if (t.status === 'ready') return html`<button onClick=${() => this.run(t.id,'planned')}>Plan</button><button onClick=${() => this.run(t.id,'direct')}>Run now</button>`;
    if (t.status === 'needs_input') return html`<button class="primary" onClick=${() => this.state.expanded[t.id] ? null : this.toggleExpand(t.id)}>Answer</button><button onClick=${() => this.sendBack(t.id)}>Send back</button>`;
    if (t.status === 'awaiting_approval') return html`<button class="primary" onClick=${() => this.approve(t.id)}>Approve</button><button onClick=${() => this.sendBack(t.id)}>Send back</button>`;
    if (['planning','executing'].includes(t.status)) return active ? html`<button onClick=${() => this.act(() => Api.request(`/runs/${active.id}/cancel`,'POST'))}>Cancel</button>` : html`<span class="pill">running…</span>`;
    if (t.status === 'review') return html`<button class="primary" onClick=${() => this.move(t.id,'done')}>Done</button><button onClick=${() => this.sendBack(t.id)}>Send back</button>`;
    if (t.status === 'inbox') return html`<button onClick=${() => this.move(t.id,'ready')}>Mark ready</button>`;
    return null;
  }
  renderQuestions(t, d) {
    const questions = d.questions || [];
    if (!questions.length) return null;
    if (t.status !== 'needs_input') {
      return html`<h4>Questions</h4><ol class="questions">${questions.map(q => html`<li>${markdown(q.text)}${q.answer ? html`<p class="answer"><strong>Answer:</strong> ${q.answer}</p>` : html`<small>No answer</small>`}</li>`)}</ol>`;
    }
    const phase = d.resume_phase === 'execute' ? 'execution' : 'planning';
    return html`<form class="question-form" onSubmit=${e => this.answer(e, t, d)}>
      <h4>The agent needs your input</h4>
      <p class="muted">Answer what you can. Blank answers leave it to the agent's judgement. Your answers go back to the agent, which continues ${phase}.</p>
      ${questions.map((q, i) => html`<fieldset class="question" key=${q.id}>
        <div class="question-text"><span class="question-number">${i + 1}</span>${markdown(q.text)}</div>
        ${q.choices.length > 0 ? html`<div class="choices" role="radiogroup" aria-label=${'Choices for question ' + (i + 1)}>${q.choices.map(c => html`<label class="choice"><input type="radio" name=${'q' + q.id} value=${c} defaultChecked=${q.answer === c}/><span>${c}</span></label>`)}</div>
          <input name=${'q' + q.id + '-other'} aria-label=${'Other answer to question ' + (i + 1)} placeholder="Or write your own answer" defaultValue=${q.answer && !q.choices.includes(q.answer) ? q.answer : ''}/>`
        : html`<textarea name=${'q' + q.id} rows="2" aria-label=${'Answer to question ' + (i + 1)} placeholder="Your answer (optional)" defaultValue=${q.answer || ''}/>`}
      </fieldset>`)}
      <button class="primary">Send answers and continue</button>
    </form>`;
  }
  renderOutput(t, d) {
    const plan = latest(d.events, 'plan'), result = latest(d.events, 'result');
    return html`${plan ? html`<h4>Latest plan</h4>${markdown(plan.body.text, 'plan')}` : html`<p class="muted">No plan yet.</p>`}
      ${this.renderQuestions(t, d)}
      ${result && html`<h4>Latest result</h4>${markdown(result.body.text)}${result.body.branch && html`<small>Branch: ${result.body.branch}</small>`}`}`;
  }
  renderEventBody(event) {
    const {text, ...rest} = event.body;
    const extras = Object.entries(rest).filter(([, v]) => v !== null && v !== '' && !(Array.isArray(v) && !v.length));
    return html`${text !== undefined && (markdownKinds.has(event.kind) ? markdown(text) : html`<pre>${String(text)}</pre>`)}
      ${extras.length > 0 && html`<pre>${extras.map(([k,v]) => `${human(k)}: ${Array.isArray(v) ? v.join(' | ') : v}`).join('\n')}</pre>`}`;
  }
  renderExpanded(t, d) {
    return html`<tr class="expanded-row"><td></td><td colspan="6"><div class="expanded">
      ${t.description && html`<details><summary>Description</summary>${markdown(t.description)}</details>`}
      ${this.renderOutput(t, d)}
      ${t.status === 'awaiting_approval' && html`<form class="toolbar" onSubmit=${e => {e.preventDefault(); this.approve(t.id, formValues(e).note);}}><input name="note" placeholder="Approval note (optional)"/><button class="primary">Approve</button></form>`}
    </div></td></tr>`;
  }
  async bulk(action, value) {
    const ids = [...this.state.selected];
    for (const id of ids) {
      if (action === 'move') await this.move(id,value); else await this.run(id,value);
    }
  }
  toggleSelect(id) {this.setState(s => ({selected:s.selected.includes(id) ? s.selected.filter(x => x !== id) : [...s.selected,id]}));}
  theme() {
    const dark = document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
    const theme = dark ? 'light' : 'dark'; document.documentElement.dataset.theme = theme; localStorage.setItem('jot-theme',theme);
  }
  async chooseView(view) {
    if (this.state.instructionDirty && !confirm('Discard unsaved instruction changes?')) return;
    this.setState({view,instructionDirty:false});
    if (view === 'Instructions') {
      try {this.setState({instructionNames:await Api.request('/instructions')}); await this.loadInstruction(this.state.instruction);}
      catch (e) {this.toast(e.message);}
    }
  }
  async loadInstruction(name) {
    if (this.state.instructionDirty && !confirm('Discard unsaved instruction changes?')) return;
    try {const data = await Api.request('/instructions/' + name); this.setState({instruction:name,markdown:data.content,instructionDirty:false});}
    catch (e) {this.toast(e.message);}
  }
  renderFilters() {
    const f = this.state.filters;
    return html`<div class="filters panel">
      <label class="search">Search<input type="search" aria-label="Full-text search" placeholder="Search notes, titles, labels…" value=${f.q || ''} onInput=${e => this.filter('q',e.target.value)}/></label>
      <label>Project<select value=${f.project || ''} onChange=${e => this.filter('project',e.target.value)}><option value="">All projects</option>${this.state.projects.map(p => html`<option value=${p.slug}>${p.name}</option>`)}</select></label>
      <label>Status<select value=${f.status || ''} onChange=${e => this.filter('status',e.target.value)}><option value="">All statuses</option>${options(statuses)}</select></label>
      <label>Criticality<select value=${f.criticality || ''} onChange=${e => this.filter('criticality',e.target.value)}><option value="">All levels</option>${options(criticalities)}</select></label>
      <label>Type<select value=${f.type || ''} onChange=${e => this.filter('type',e.target.value)}><option value="">All types</option>${options(types)}</select></label>
      <label>Labels<input list="label-names" placeholder="comma separated" value=${f.labels || ''} onInput=${e => this.filter('labels',e.target.value)}/><datalist id="label-names">${this.state.labels.map(l => html`<option value=${l.name}>${l.count} tasks</option>`)}</datalist></label>
      <label>Older than days<input type="number" min="0" value=${f.older_than || ''} onInput=${e => this.filter('older_than',e.target.value)}/></label>
      <label>Sort<select value=${f.sort} onChange=${e => this.filter('sort',e.target.value)}>${options(['priority','updated','created'])}</select></label>
      <button onClick=${() => this.setState({filters:{sort:'priority'}}, () => this.scheduleRefresh())}>Reset</button>
    </div>`;
  }
  renderBoard() {
    return html`<div class="board">${statuses.map(status => {
      const tasks = this.state.tasks.filter(t => t.status === status);
      return html`<section class="column" onDragOver=${e => e.preventDefault()} onDrop=${e => {e.preventDefault(); const id = Number(e.dataTransfer.getData('text/plain')); if (id) this.move(id,status);}}>
        <h2><span class=${'dot ' + status}></span>${human(status)}<span class="count">${tasks.length}</span></h2>
        ${tasks.map(t => html`<button class="card" draggable=${!t.claimed_by} onDragStart=${e => e.dataTransfer.setData('text/plain',String(t.id))} onClick=${() => this.openTask(t.id)}>
          <small>#${t.id} · ${this.state.projects.find(p => p.id === t.project_id)?.name || 'Personal'}</small><strong>${t.title}</strong>
          ${t.needs_enrichment && t.status === "inbox" && html`<span class="enriching">inbox · enriching…</span>`}
          ${t.status === 'needs_input' && html`<span class="needs-input">Waiting for your answers</span>`}
          <div class="chips">${pill(t.criticality)}${pill(t.type)}${t.labels.map(label => pill(label))}</div>
        </button>`)}${!tasks.length && html`<p class="empty-small">Drop a task here</p>`}
      </section>`;
    })}</div>`;
  }
  renderList() {
    const {selected,preset,expanded} = this.state;
    const presets = {open:['Open', t => !['done','archived','wont_do'].includes(t.status)], attention:['Needs attention', t => ['needs_input','awaiting_approval','review'].includes(t.status)],
      input:['Needs input', t => t.status === 'needs_input'],
      ready:['Ready', t => t.status === 'ready'], progress:['In progress', t => ['planning','executing'].includes(t.status)], all:['All', () => true]};
    const tasks = this.state.tasks.filter(presets[preset][1]);
    return html`<div class="panel"><div class="toolbar">${this.renderPicker('plan','Plan with')}${this.renderPicker('execute','Execute with')}</div>
      <div class="toolbar presets">${Object.entries(presets).map(([key,[label,test]]) => html`<button class=${preset === key ? 'active' : ''} onClick=${() => this.setState({preset:key})}>${label} <small>${this.state.tasks.filter(test).length}</small></button>`)}</div>
      <div class="toolbar"><strong>${selected.length} selected</strong>
      <button disabled=${!selected.length} onClick=${() => this.bulk('run','planned')}>Plan</button><button disabled=${!selected.length} onClick=${() => this.bulk('run','direct')}>Run now</button>
      <select aria-label="Bulk move status" value="" disabled=${!selected.length} onChange=${e => {this.bulk('move',e.target.value); e.target.value='';}}><option value="">Move status…</option>${options(statuses)}</select></div>
      <div class="table-scroll"><table><thead><tr><th><input type="checkbox" aria-label="Select all tasks" checked=${tasks.length > 0 && tasks.every(t => selected.includes(t.id))} onChange=${e => this.setState({selected:e.target.checked ? tasks.map(t => t.id) : []})}/></th><th>Task</th><th>Project</th><th>Status</th><th>Criticality</th><th>Actions</th><th>Updated</th></tr></thead>
      <tbody>${tasks.map(t => html`<tr><td><input type="checkbox" aria-label=${'Select task ' + t.id} checked=${selected.includes(t.id)} onChange=${() => this.toggleSelect(t.id)}/></td><td><button class="caret" aria-label=${(expanded[t.id] ? 'Collapse' : 'Expand') + ' task ' + t.id} onClick=${() => this.toggleExpand(t.id)}>${expanded[t.id] ? '▾' : '▸'}</button><button class="text-button" onClick=${() => this.openTask(t.id)}>${t.title}</button><div class="chips">${t.labels.map(l => pill(l))}${t.needs_enrichment && t.status === 'inbox' && html`<small>enriching…</small>`}</div></td><td>${this.state.projects.find(p => p.id === t.project_id)?.name || '—'}</td><td>${pill(t.status)}</td><td>${pill(t.criticality)}</td><td><div class="row-actions">${this.rowActions(t)}</div></td><td>${date(t.updated_at)}</td></tr>${expanded[t.id] && this.renderExpanded(t, expanded[t.id])}`)}</tbody></table></div>
      ${!tasks.length && html`<p class="empty">No tasks match these filters.</p>`}</div>`;
  }
  renderRuns(runs = this.state.runs, inDrawer = false) {
    return html`<div class="runs">${runs.length ? [...runs].reverse().map(r => html`<article class="panel run"><div class="toolbar"><button class="text-button" onClick=${() => this.openTask(r.task_id)}>Run #${r.id} · task #${r.task_id}</button>${pill(r.status)}${pill(r.backend)}${r.model && html`<span class="pill">${r.model}</span>`}${pill(r.phase)}</div><small>${date(r.started_at)}</small>${r.summary ? markdown(r.summary) : html`<p class="muted">Waiting for output…</p>`}${r.branch && html`<small>Branch: ${r.branch}</small>`}
      ${!r.ended_at && html`<button onClick=${() => this.act(() => Api.request(`/runs/${r.id}/cancel`,'POST'))}>Cancel run</button>`}
      ${this.renderRunTotals(r)}
      ${inDrawer || this.state.openLogs[r.id] ? this.renderLog(this.state.runLogs[r.id]) : html`<button class="text-button" onClick=${() => this.toggleLog(r.id)}>Show output</button>`}
      ${!inDrawer && this.state.openLogs[r.id] && html`<button class="text-button" onClick=${() => this.toggleLog(r.id)}>Hide output</button>`}</article>`) : html`<p class="empty">No runs yet. Open a ready task to plan or run it.</p>`}</div>`;
  }
  renderRunTotals(r) {
    const usage = {input:r.input_tokens, output:r.output_tokens, cache_read:r.cache_read_tokens, cache_write:r.cache_write_tokens, premium_requests:r.premium_requests};
    if (Object.values(usage).every(v => v === null || v === undefined)) return null;
    return html`<div class="usage total"><strong>Total</strong>${App.usageParts(usage)}</div>`;
  }
  static usageParts(usage) {
    const n = v => Number(v).toLocaleString();
    const parts = [['in', usage.input], ['out', usage.output], ['cache read', usage.cache_read], ['cache write', usage.cache_write]]
      .filter(([, v]) => v !== null && v !== undefined).map(([label, v]) => html`<span>${label} <b>${n(v)}</b></span>`);
    if (usage.premium_requests !== null && usage.premium_requests !== undefined) parts.push(html`<span>premium requests <b>${n(usage.premium_requests)}</b></span>`);
    return parts.length ? parts : [html`<span class="muted">tokens not reported</span>`];
  }
  static logBlocks(entries) {
    // Group consecutive tool/thinking steps so they collapse as one block.
    const blocks = []; let group = null; let lastText = null;
    for (const entry of entries) {
      if (entry.kind === 'tool' || entry.kind === 'thinking') {
        if (!group) { group = {type:'activity', items:[]}; blocks.push(group); }
        group.items.push(entry); continue;
      }
      group = null;
      if (entry.kind === 'result' && entry.text && entry.text.trim() === (lastText || '').trim()) continue;
      if (entry.kind === 'text') lastText = entry.text;
      blocks.push({type:entry.kind, entry});
    }
    return blocks;
  }
  renderLog(entries) {
    if (!entries) return html`<p class="muted">Loading output…</p>`;
    if (!entries.length) return html`<p class="muted">No output yet.</p>`;
    return html`<div class="run-log">${App.logBlocks(entries).map(block => {
      if (block.type === 'activity') {
        const tools = block.items.filter(i => i.kind === 'tool').length, thoughts = block.items.length - tools;
        const label = [tools && `${tools} tool call${tools > 1 ? 's' : ''}`, thoughts && `${thoughts} thinking`].filter(Boolean).join(' · ');
        return html`<details class="activity"><summary>${label}</summary>${block.items.map(i => i.kind === 'tool'
          ? html`<pre class="log tool">${i.text}</pre>`
          : html`<div class="log thinking">${markdown(i.text)}</div>`)}</details>`;
      }
      const e = block.entry;
      if (block.type === 'usage') return html`<div class=${'usage' + (e.text === 'total' ? ' total' : '')}>${e.text === 'total' ? html`<strong>Run total</strong>` : null}${e.model ? html`<span class="pill">${e.model}</span>` : null}${App.usageParts(e.usage || {})}</div>`;
      if (block.type === 'error') return html`<div class="log error">${e.text}</div>`;
      return html`<div class=${'agent-text ' + block.type}>${markdown(e.text)}</div>`;
    })}</div>`;
  }
  renderProjects() {
    return html`<div><button class="primary" onClick=${() => this.setState({projectEdit:{}})}>＋ New project</button>
      ${this.state.projectEdit && html`<${ProjectEditor} key=${this.state.projectEdit.id || 'new'} project=${this.state.projectEdit} app=${this}/>`}
      <div class="grid project-grid">${this.state.projects.map(p => html`<article class="panel"><div class="toolbar"><h2>${p.name}</h2>${p.archived && pill('archived')}</div><small>${p.slug} · priority ${p.priority} · ${p.default_flow || 'default flow'}</small><p>${p.description}</p><code>${p.repo_path || 'No repository'}</code><div class="chips">${p.aliases.map(a => pill(a))}</div><button onClick=${() => this.setState({projectEdit:p})}>Edit</button><button onClick=${() => this.act(() => Api.request(`/projects/${p.id}`,'PATCH',{archived:!p.archived}))}>${p.archived ? 'Restore' : 'Archive'}</button><button class="danger" onClick=${() => {if (confirm(`Delete project ${p.name}?`)) this.act(() => Api.request(`/projects/${p.id}`,'DELETE'));}}>Delete</button></article>`)}</div></div>`;
  }
  renderInstructions() {
    return html`<div class="panel"><div class="toolbar"><select aria-label="Instruction file" value=${this.state.instruction} onChange=${e => this.loadInstruction(e.target.value)}>${options(this.state.instructionNames)}</select>
      <button onClick=${() => {const slug = prompt('Project slug for projects/<slug>.md'); if (slug && /^[a-z0-9][a-z0-9_-]*$/.test(slug)) {const name = `projects/${slug}.md`; if (this.state.instructionNames.includes(name)) this.loadInstruction(name); else if (!this.state.instructionDirty || confirm('Discard unsaved changes?')) this.setState(s => ({instructionNames:[...s.instructionNames,name],instruction:name,markdown:'# ' + slug + '\n',instructionDirty:true}));}}}>New project instructions</button>
      <button class="primary" disabled=${!this.state.instructionDirty} onClick=${async () => {if (await this.act(() => Api.request('/instructions/' + this.state.instruction,'PUT',{content:this.state.markdown}))) this.setState({instructionDirty:false});}}>Save Markdown</button>${this.state.instructionDirty && html`<small>Unsaved changes</small>`}</div>
      <textarea class="markdown" aria-label="Markdown instructions" value=${this.state.markdown} onInput=${e => this.setState({markdown:e.target.value,instructionDirty:true})}/></div>`;
  }
  renderCleanup() {
    const p = this.state.proposal;
    return html`<div class="panel"><h2>Review before anything changes</h2><p>Scan for stale or duplicate work. Approve only the items you want applied.</p>
      <button class="primary" onClick=${() => this.act(async () => {const scan = await Api.request('/cleanup/scan','POST',{}); this.setState({proposal:await Api.request('/cleanup/' + scan.id),approved:[]});})}>Scan tasks</button>
      ${p && html`<div><h3>Proposal #${p.id} · ${p.status}</h3>${p.items.map(item => html`<label class="cleanup-item"><input type="checkbox" checked=${this.state.approved.includes(item.index)} onChange=${e => this.setState(s => ({approved:e.target.checked ? [...s.approved,item.index] : s.approved.filter(i => i !== item.index)}))}/><span><strong>${item.title}</strong> · ${human(item.action)}<small>${item.reason}</small></span></label>`)}
      <button disabled=${!this.state.approved.length} onClick=${() => this.act(async () => {await Api.request(`/cleanup/${p.id}/apply`,'POST',{approved_indexes:this.state.approved}); this.setState({proposal:await Api.request('/cleanup/' + p.id),approved:[]});})}>Apply ${this.state.approved.length} approved</button></div>`}</div>`;
  }
  renderDrawer() {
    const d = this.state.detail; if (!d) return null;
    const t = d.task;
    return html`<div class="drawer-backdrop" onClick=${e => {if (e.target === e.currentTarget) this.closeDetail();}}><aside class="drawer" role="dialog" aria-modal="true" aria-label=${'Task ' + t.id} onKeyDown=${e => {
      if (e.key !== 'Tab') return;
      const nodes = [...e.currentTarget.querySelectorAll('button:not(:disabled),input,textarea,select')];
      const first = nodes[0], last = nodes[nodes.length-1];
      if (e.shiftKey && document.activeElement === first) {e.preventDefault(); last.focus();}
      else if (!e.shiftKey && document.activeElement === last) {e.preventDefault(); first.focus();}
    }}>
      <header class="drawer-header"><strong>Task #${t.id}</strong>${pill(t.status)}<button class="drawer-close" aria-label="Close task" onClick=${() => this.closeDetail()}>✕</button></header>
      <div class="drawer-body"><div class="chips">${pill(t.criticality)}${pill(t.type)}${t.labels.map(l => pill(l))}</div>
      <${TaskEditor} key=${t.id} task=${t} projects=${this.state.projects} app=${this}/>
      <section class="output"><h2>Agent output</h2>${this.renderOutput(t, d)}</section>
      <section class="actions"><h2>Draw down</h2><div class="toolbar">${this.renderPicker('plan','Plan with')}${this.renderPicker('execute','Execute with')}</div><div class="toolbar"><button onClick=${() => this.run(t.id,'planned')}>Plan first</button><button onClick=${() => this.run(t.id,'direct')}>Run now (direct)</button></div>
      <form onSubmit=${e => {e.preventDefault(); const {note} = formValues(e); this.approve(t.id, note);}}><label>Approval note<input name="note" placeholder="Optional approval note"/></label><button class="primary">Approve</button></form>
      <form onSubmit=${e => {e.preventDefault(); const body = formValues(e); this.act(() => Api.request(`/tasks/${t.id}/send-back`,'POST',body));}}><label>Send-back feedback<input name="comment" required placeholder="What needs to change?"/></label><button>Send back</button></form>
      <div class="toolbar"><button onClick=${() => this.act(() => Api.request(`/tasks/${t.id}/enrich`,'POST',{}))}>Re-enrich</button><select aria-label="Move task status" value="" onChange=${e => this.move(t.id,e.target.value)}><option value="">Move status…</option>${options(d.transitions)}</select><button class="danger" onClick=${async () => {if (confirm('Delete this task? Its history will be retained.')) {this.closeDetail(); await this.act(() => Api.request(`/tasks/${t.id}`,'DELETE'));}}}>Delete</button></div></section>
      <h2>Runs & live output</h2>${this.renderRuns(d.runs, true)}<h2>Timeline</h2>
      <form onSubmit=${async e => {e.preventDefault(); const form=e.currentTarget, body=formValues(e); if (await this.act(() => Api.request(`/tasks/${t.id}/comment`,'POST',body))) form.reset();}}><label>Add a comment<textarea name="comment" required rows="2" placeholder="Answer a question or add context…"/></label><button>Post comment</button></form>
      <ol class="timeline">${[...d.events].reverse().map(event => html`<li><div class="toolbar">${pill(event.kind)}<small>${event.actor} · ${date(event.ts)}</small></div>${this.renderEventBody(event)}</li>`)}</ol>
      <details><summary>Original capture</summary><pre>${t.raw_input}</pre></details></div></aside></div>`;
  }
  render() {
    const {view,connected,busy,loaded} = this.state;
    return html`<header class="topbar"><a class="brand" href="/" aria-label="Jot home"><span>j</span>jot<span class="brand-dot">.</span></a><form class="capture" onSubmit=${e => this.capture(e)}><span>＋</span><input id="capture" aria-label="Capture a task" placeholder="What's on your mind? Capture an idea…" autoComplete="off"/><kbd>Ctrl K</kbd><button class="primary" disabled=${busy}>${busy ? 'Saving…' : 'Capture'}</button></form><button class="theme" aria-label="Toggle light and dark theme" onClick=${() => this.theme()}>◐</button></header>
      ${this.renderDatalists()}<div class="layout"><nav aria-label="Main views"><small>WORKSPACE</small>${['Board','List','Runs','Projects','Instructions','Cleanup'].map((name,i) => html`<button class=${view === name ? 'active' : ''} onClick=${() => this.chooseView(name)}><span>${['▦','☷','▷','◇','≡','↺'][i]}</span>${name}</button>`)}<div class="connection"><span class=${connected ? 'online' : ''}></span>${connected ? 'Live updates' : 'Reconnecting…'}<small>Local space. Clear head.</small></div></nav>
      <main><div class="page-heading"><div><p class="eyebrow">MAKE ROOM FOR IDEAS</p><h1>${view}</h1></div><span class="muted">${this.state.tasks.length} tasks in view</span></div>
      ${['Board','List'].includes(view) && this.renderFilters()}
      ${!loaded ? html`<p class="empty">Loading your workspace…</p>` : view === 'Board' ? this.renderBoard() : view === 'List' ? this.renderList() : view === 'Runs' ? this.renderRuns() : view === 'Projects' ? this.renderProjects() : view === 'Instructions' ? this.renderInstructions() : this.renderCleanup()}
      </main></div>${this.renderDrawer()}<div class="toasts" aria-live="polite">${this.state.toasts.map(t => html`<div role="status" class="toast">${t.message}<button aria-label="Dismiss notification" onClick=${() => this.setState(s => ({toasts:s.toasts.filter(x => x.id !== t.id)}))}>✕</button></div>`)}</div>`;
  }
}

export {App, TaskEditor, ProjectEditor};
if (typeof document !== 'undefined') render(html`<${App}/>`, document.getElementById('app'));
