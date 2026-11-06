// Execution plugin UI: a drawer panel for supervised agent work, and phase badges.
export default function setup(jot) {
  const { html, register, api, Component } = jot;
  const call = (name, body) => api.request(`/plugins/execution/actions/${name}`, 'POST', body);
  const record = j => (j.metadata && j.metadata.execution) || {};
  const words = value => (value || 'idle').replaceAll('_', ' ');
  const QUIET = new Set(['idle', 'done']);

  register('jot.badges', ({ jot: j }) => {
    const phase = record(j).phase;
    return phase && !QUIET.has(phase) ? html`<span class="pill phase" data-phase=${phase}>${words(phase)}</span>` : null;
  });

  const line = e => {
    if (e.kind === 'usage') return `[usage] ${JSON.stringify(e.usage || {})}`;
    if (e.kind === 'result') return `[result${e.is_error ? ' error' : ''}] ${e.text || ''}`;
    return `[${e.kind}] ${e.text || ''}`;
  };

  class Panel extends Component {
    constructor() {
      super();
      this.state = { log: null, answers: {}, note: '', busy: false, error: '' };
    }

    async act(name, extra = {}) {
      this.setState({ busy: true, error: '' });
      try {
        await call(name, { jot: this.props.detail.jot.id, ...extra });
        this.setState({ note: '', answers: {} });
        await this.props.refresh();
      } catch (error) {
        this.setState({ error: error.message });
      } finally {
        this.setState({ busy: false });
      }
    }

    async showLog() {
      try {
        const events = await call('log', { jot: this.props.detail.jot.id, tail: 50 });
        this.setState({ log: events.map(line), error: '' });
      } catch (error) {
        this.setState({ error: error.message });
      }
    }

    answer(event, question) {
      event.preventDefault();
      const text = (this.state.answers[question] || '').trim();
      if (text) this.act('answer', { question, text });
    }

    render({ detail }, { log, answers, note, busy, error }) {
      const j = detail.jot;
      if (j.deleted_at) return null;
      const r = record(j);
      const phase = r.phase || 'idle';
      const comments = detail.comments;
      const latest = kind => [...comments].reverse().find(c => c.kind === kind);
      const answered = new Set(comments.filter(c => c.reply_to).map(c => c.reply_to));
      const asked = new Set(r.questions || []);
      const open = phase === 'needs_input'
        ? comments.filter(c => c.kind === 'execution.question' && asked.has(c.id) && !answered.has(c.id))
        : [];
      const plan = phase === 'awaiting_approval' && latest('execution.plan');
      const result = phase === 'review' && latest('execution.result');
      const note_ = latest('execution.note');
      const failed = phase === 'needs_input' && ['failed', 'capped'].includes(r.outcome);
      const decide = phase === 'awaiting_approval' || phase === 'review';
      return html`<details class="execution" open=${phase !== 'idle'}>
        <summary><h3>Execution</h3></summary>
        <p>Phase <strong data-testid="execution-phase">${words(phase)}</strong>
          ${r.claim ? ` · held by ${r.claim.agent}` : ''}
          ${r.outcome ? ` · outcome ${words(r.outcome)}` : ''}
          ${r.branch ? html` · <code>${r.branch}</code>` : ''}
          ${r.cost_usd ? ` · $${Number(r.cost_usd).toFixed(2)}` : ''}</p>
        <div class="toolbar">
          ${(phase === 'idle' || phase === 'approved') && html`
            <button disabled=${busy} onClick=${() => this.act('run')}>${phase === 'approved' ? 'Execute' : 'Plan'}</button>
            ${phase === 'idle' && html`<button disabled=${busy} onClick=${() => this.act('run', { direct: true })}>Run directly</button>`}`}
          ${(phase === 'planning' || phase === 'executing') && html`<button class="danger" disabled=${busy} onClick=${() => this.act('cancel')}>Cancel run</button>`}
          ${failed && html`<button disabled=${busy} onClick=${() => this.act('retry')}>Retry</button>`}
          ${phase === 'awaiting_approval' && html`<button class="primary" disabled=${busy} onClick=${() => this.act('approve')}>Approve plan</button>`}
          ${r.log && html`<button onClick=${() => this.showLog()}>Show log</button>`}
        </div>
        ${failed && note_ && html`<p class="error">${note_.body}</p>`}
        ${open.map(q => html`<form class="question" key=${q.id} onSubmit=${e => this.answer(e, q.id)}>
          <pre>${q.body}</pre>
          <input aria-label=${'Answer to question ' + q.id} value=${answers[q.id] || ''}
            onInput=${e => this.setState({ answers: { ...answers, [q.id]: e.target.value } })}/>
          <button disabled=${busy}>Answer</button></form>`)}
        ${plan && html`<h4>Plan</h4><pre class="plan" aria-label="Plan">${plan.body}</pre>`}
        ${result && html`<h4>Result</h4><pre class="result" aria-label="Result">${result.body}</pre>`}
        ${decide && html`<form class="inline" onSubmit=${e => { e.preventDefault(); if (note.trim()) this.act('send_back', { note }); }}>
          <textarea aria-label="Send back note" rows="2" placeholder="What should change?" value=${note}
            onInput=${e => this.setState({ note: e.target.value })}></textarea>
          <button disabled=${busy}>Send back</button></form>`}
        ${error && html`<p class="error" role="alert">${error}</p>`}
        ${log && html`<pre class="log" aria-label="Run log">${log.join('\n') || '(empty)'}</pre>`}
      </details>`;
    }
  }

  register('jot.panels', props => html`<${Panel} ...${props} key=${props.detail.jot.id}/>`);

  class NeedsYou extends Component {
    constructor() { super(); this.state = { items: null, error: '' }; }
    async componentDidMount() {
      try { this.setState({ items: await call('inbox', {}) }); } catch (error) { this.setState({ items: [], error: error.message }); }
    }
    render(_, { items, error }) {
      if (items === null) return html`<p class="muted">Loading…</p>`;
      if (error) return html`<p class="error">${error}</p>`;
      if (!items.length) return html`<p class="muted">Nothing waiting on you.</p>`;
      return html`<ul class="needs-you" aria-label="Needs you">${items.map(w => html`<li key=${w.jot_id}>
        <a href=${`#/jot/${w.jot_id}/needs-you`}>#${w.jot_id} ${w.title}</a> <span class="pill phase">${w.reason}</span>
        ${w.project && html` <span class="pill project">@${w.project}</span>`}</li>`)}</ul>`;
    }
  }
  register('views', { id: 'needs-you', label: 'Needs you', render: () => html`<${NeedsYou}/>` });
}
