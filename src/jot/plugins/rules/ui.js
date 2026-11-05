// Rules plugin UI: a settings page that summarizes the configured rules.
export default function setup(jot) {
  const { html, register, api } = jot;
  const describe = rule => {
    const w = rule.when || {}, t = rule.then || {};
    const when = [
      `on ${(w.kinds || ['jot.created']).join(', ')}`,
      w.profiles?.length && `profile ${w.profiles.join('/')}`,
      w.categories?.length && `category ${w.categories.join('/')}`,
      w.labels?.length && `has #${w.labels.join(' #')}`,
      w.text && `contains "${w.text}"`,
      w.pattern && `matches /${w.pattern}/`,
    ].filter(Boolean).join(', ');
    const then = [
      t.add_labels?.length && `add #${t.add_labels.join(' #')}`,
      t.remove_labels?.length && `remove #${t.remove_labels.join(' #')}`,
      t.status && `status ${t.status}`,
      t.project !== undefined && t.project !== null && (t.project ? `project @${t.project}` : 'clear project'),
      t.comment && 'comment',
    ].filter(Boolean).join(', ');
    return `${when} → ${then || 'nothing'}`;
  };
  class RulesPage extends jot.Component {
    constructor() { super(); this.state = { rules: null }; }
    async componentDidMount() {
      const plugins = await api.request('/plugins');
      const rules = plugins.find(p => p.id === 'rules');
      this.setState({ rules: rules ? rules.settings.rules || [] : [] });
    }
    render(_, { rules }) {
      if (rules === null) return html`<p class="muted">Loading rules…</p>`;
      if (!rules.length) return html`<p class="muted">No rules yet. Add them in the plugin's settings JSON.</p>`;
      return html`<ol class="rules-list">${rules.map(r => html`<li key=${r.name}><strong>${r.name}</strong>${r.enabled === false ? ' (off)' : ''}: <span class="muted">${describe(r)}</span></li>`)}</ol>`;
    }
  }
  register('settings.pages', { id: 'rules', label: 'Rules', render: () => html`<${RulesPage} />` });
}
