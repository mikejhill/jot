import {test} from 'node:test';
import assert from 'node:assert/strict';
import {App, TaskEditor, ProjectEditor} from '../../src/jot/static/app.js';
import {markdown, safeUrl} from '../../src/jot/static/markdown.js';

// Render templates as virtual nodes; no browser, server, DOM, or network.
function walk(node) {
  if (!node || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(walk);
  return [node, ...walk(node.props?.children)];
}
const task = {id:1,title:'Health checks',description:'Dependency probes',labels:['ops'],
  project_id:2,status:'ready',criticality:'high',type:'bug',flow:'direct',repo_path:'C:/repo',
  due_at:'2026-12-01T00:00:00Z',updated_at:'2026-10-05T00:00:00Z',needs_enrichment:false};

test('all views and drawer produce valid Preact trees', () => {
  const app = new App();
  Object.assign(app.state, {loaded:true,tasks:[task],projects:[{id:2,name:'Orbit',slug:'orbit',aliases:['orb']}],
    detail:{task,events:[{kind:'plan',actor:'agent',ts:task.updated_at,body:{text:'Inspect → test'}}],runs:[],transitions:['executing']}});
  for (const view of ['Board','List','Runs','Projects','Instructions','Cleanup']) {
    app.state.view = view;
    const nodes = walk(app.render());
    assert(nodes.some(n => n.props?.id === 'capture'), view);
    assert(nodes.some(n => n.props?.role === 'dialog'), view);
  }
});

test('task editor preserves actual project, type, flow and criticality values', () => {
  const props = {task, projects:[{id:2,name:'Orbit'}], app:{}};
  const editor = new TaskEditor(props);
  const nodes = walk(editor.render(props, editor.state));
  const field = name => nodes.find(n => n.props?.name === name).props;
  assert.equal(field('project_id').value, 2);
  assert.equal(field('criticality').value, 'high');
  assert.equal(field('type').value, 'bug');
  assert.equal(field('flow').value, 'direct');
  assert.equal(field('labels').value, 'ops');
  assert.equal(field('repo_path').value, 'C:/repo');
});

test('live task updates do not overwrite unsaved edits', () => {
  const props = {task,projects:[],app:{}};
  const editor = new TaskEditor(props);
  editor.setState = patch => Object.assign(editor.state, patch);
  editor.componentWillReceiveProps({task:{...task,title:'Enriched'}});
  assert.equal(editor.state.fields.title, 'Enriched');
  editor.state.dirty = true;
  editor.state.fields.title = 'Human draft';
  editor.componentWillReceiveProps({task:{...task,title:'Another update'}});
  assert.equal(editor.state.fields.title, 'Human draft');
});

test('project editor selects the saved flow', () => {
  const props = {project:{id:2,name:'Orbit',default_flow:'direct'},app:{}};
  const editor = new ProjectEditor(props);
  const field = walk(editor.render(props)).find(n => n.props?.name === 'default_flow');
  assert.equal(field.props.value, 'direct');
});

test('list rows offer one-click actions per status and expand inline', () => {
  const app = new App();
  const labels = status => walk(app.rowActions({...task, status}))
    .filter(n => n.type === 'button').map(n => [n.props.children].flat().join(''));
  Object.assign(app.state, {runs:[{id:9,task_id:1,ended_at:null}]});
  assert.deepEqual(labels('ready'), ['Plan', 'Run now']);
  assert.deepEqual(labels('needs_input'), ['Answer', 'Send back']);
  assert.deepEqual(labels('awaiting_approval'), ['Approve', 'Send back']);
  assert.deepEqual(labels('executing'), ['Cancel']);
  assert.deepEqual(labels('review'), ['Done', 'Send back']);
  assert.deepEqual(labels('done'), []);
  const detail = {events:[{kind:'plan',body:{text:'Step 1'}},{kind:'question',body:{text:'Which DB?'}}],
    questions:[{id:2,text:'Which DB?',choices:[],answer:'Postgres'}]};
  const texts = strings(app.renderExpanded({...task, status:'awaiting_approval'}, detail));
  assert(texts.includes('Step 1') && texts.includes('Which DB?') && texts.includes('Postgres'));
});

const strings = tree => walk(tree).flatMap(n => [n.props?.children].flat()).filter(c => typeof c === 'string');
const noRawHtml = tree => walk(tree).every(n => !n.props || !('dangerouslySetInnerHTML' in n.props));

test('markdown renders structure as elements', () => {
  const tree = markdown('# Plan\n\n1. First **bold**\n2. Second\n   - nested `code`\n\n```py\nprint("<b>")\n```\n\n> quoted\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n---\ntext with _em_ and ~~old~~');
  const types = walk(tree).map(n => n.type);
  for (const tag of ['h1','ol','li','strong','ul','code','pre','blockquote','table','th','td','hr','em','del','p']) assert(types.includes(tag), tag);
  const pre = walk(tree).find(n => n.type === 'pre');
  assert.equal(walk(pre).find(n => n.type === 'code').props.class, 'language-py');
  assert(strings(tree).includes('print("<b>")'));
  const ol = walk(markdown('3. three\n\n4. four')).filter(n => n.type === 'ol');
  assert.equal(ol.length, 1);
  assert.equal(ol[0].props.start, 3);
});

test('markdown never injects raw HTML or unsafe links', () => {
  const payload = '<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[click](javascript:alert(1)) [data](data:text/html,x) [ok](https://example.com) <mailto:me@example.com>';
  const tree = markdown(payload);
  assert(noRawHtml(tree));
  const types = new Set(walk(tree).map(n => n.type));
  assert(!types.has('script') && !types.has('img'));
  assert(strings(tree).some(s => s.includes('<script>alert(1)</script>')));
  const hrefs = walk(tree).filter(n => n.type === 'a').map(n => n.props.href);
  assert.deepEqual(hrefs, ['https://example.com', 'mailto:me@example.com']);
  assert(walk(tree).filter(n => n.type === 'a').every(n => n.props.rel === 'noopener noreferrer'));
  assert(strings(tree).includes('click'));
  assert.equal(safeUrl('java\nscript:alert(1)'), null);
  assert.equal(safeUrl(' HTTPS://x.dev '), 'HTTPS://x.dev');
});

test('needs_input shows one input per question and posts answers', async () => {
  const app = new App();
  const detail = {events:[{kind:'plan',body:{text:'## Steps\n1. Pick'}}], resume_phase:'execute',
    questions:[{id:5,text:'Which DB?',choices:['pg','lite'],answer:null},{id:6,text:'Deadline?',choices:[],answer:'Friday'}]};
  const t = {...task, status:'needs_input'};
  const tree = app.renderQuestions(t, detail);
  const nodes = walk(tree);
  assert.equal(nodes.filter(n => n.type === 'fieldset').length, 2);
  assert.deepEqual(nodes.filter(n => n.props?.type === 'radio').map(n => n.props.value), ['pg','lite']);
  assert.equal(nodes.find(n => n.type === 'textarea').props.defaultValue, 'Friday');
  assert(strings(tree).join('').includes('continues execution'));
  assert(noRawHtml(app.renderOutput(t, detail)));
  assert(walk(app.renderOutput(t, detail)).some(n => n.type === 'h2'));

  const calls = [];
  const fetchBefore = globalThis.fetch, formBefore = globalThis.FormData;
  globalThis.fetch = async (url, init) => { calls.push([url, JSON.parse(init.body)]); return {ok:true, json:async () => ({})}; };
  globalThis.FormData = class { get(name) { return {q5:'pg', 'q5-other':'', q6:' Monday '}[name]; } };
  app.act = async action => { await action(); return true; };
  app.toast = () => {};
  Object.assign(app.state, {backend:'claude', picks:{plan:{}, execute:{backend:'codex', model:'astra'}}});
  try { await app.answer({preventDefault() {}, currentTarget:{}}, t, detail); }
  finally { globalThis.fetch = fetchBefore; globalThis.FormData = formBefore; }
  assert.deepEqual(calls, [['/api/tasks/1/answers', {answers:[{question_id:5,text:'pg'},{question_id:6,text:'Monday'}], backend:'codex', model:'astra'}]]);
});

test('timeline renders prose as markdown and system events as one readable line', () => {
  const app = new App();
  const md = walk(app.renderEventBody({kind:'plan', body:{text:'# Title', run_id:3}}));
  assert(md.some(n => n.type === 'h1'));
  assert(!md.some(n => n.type === 'pre'), 'no <pre> for prose');
  const line = text => strings(app.renderEventBody(text));
  assert(line({kind:'status', body:{from:'ready', to:'planning', action:'claim'}}).includes('ready → planning (claim)'));
  assert(line({kind:'run_log', body:{run_id:1, status:'running', action:'created'}}).includes('Run #1 running · created'));
  assert(line({kind:'enriched', body:{action:'label_add', label:'idea'}}).includes('Label added: idea'));
  assert(line({kind:'enriched', body:{backend:'claude', model:'opus', confidence:0.6}}).includes('Enriched by claude/opus · confidence 0.6'));
  assert(line({kind:'created', body:{source:'ui'}}).includes('Captured from ui'));
  const listed = strings(app.renderEventBody({kind:'question', body:{text:'Q', choices:['a','b']}}));
  assert(listed.includes('a | b'));
});

test('structured plan JSON renders as summary, plan and questions', () => {
  const json = JSON.stringify({summary:'**Short** take', plan:'1. Step one', questions:['Which tone?', {text:'Who wins?'}]});
  const nodes = walk(App.renderAgentText(json));
  assert(nodes.some(n => n.type === 'strong'));
  assert(nodes.some(n => n.type === 'ol'));
  const texts = strings(App.renderAgentText(json));
  assert(texts.includes('Who wins?') && !texts.includes('"summary"'));
  assert.equal(App.structured('not json'), null);
  assert.equal(App.structured('{"other": 1}'), null);
  assert(App.structured('```json\n{"plan": "x"}\n```'));
});

test('model pickers resolve per-action choices with config defaults', () => {
  const app = new App();
  Object.assign(app.state, {backend:'claude', config:{model_defaults:{claude:{plan:'opus'}}, model_suggestions:{claude:['opus','sonnet']}},
    picks:{plan:{backend:'claude', model:''}, execute:{backend:'codex', model:'gpt-6-astra'}}});
  assert.deepEqual(app.pick('plan'), {backend:'claude', model:null});
  assert.deepEqual(app.pick('execute'), {backend:'codex', model:'gpt-6-astra'});
  const input = walk(app.renderPicker('plan', 'Plan with')).find(n => n.type === 'input');
  assert.equal(input.props.placeholder, 'default (opus)');
  assert.equal(walk(app.renderDatalists()).filter(n => n.type === 'option' && n.props.value !== 'auto').length, 2);
});

test('run logs render Markdown, collapse tool/thinking runs, and show per-turn usage', () => {
  const entries = [
    {kind:'thinking', text:'consider'}, {kind:'tool', text:'Read a.py'}, {kind:'tool', text:'Grep x'},
    {kind:'text', text:'**Done**'}, {kind:'usage', text:'', model:'opus', usage:{input:10, output:5, cache_read:100, cache_write:null}},
    {kind:'result', text:'**Done**'}, {kind:'error', text:'warn'},
  ];
  const blocks = App.logBlocks(entries);
  assert.deepEqual(blocks.map(b => b.type), ['activity', 'text', 'usage', 'error']);
  assert.equal(blocks[0].items.length, 3);
  const nodes = walk(new App().renderLog(entries));
  const details = nodes.find(n => n.type === 'details');
  assert(details && !details.props.open, 'activity collapsed by default');
  assert(nodes.some(n => n.type === 'strong'), 'markdown rendered');
  const texts = nodes.flatMap(n => [n.props?.children].flat()).filter(c => typeof c === 'string');
  assert(texts.includes('opus') && texts.includes('cache read'));
  assert(!texts.includes('cache write'), 'unreported fields omitted');
  const none = walk(App.usageParts({}));
  assert(none.some(n => [n.props?.children].flat().includes('tokens not reported')));
});

test('shared picker shows Auto and action pins and filters model policy', () => {
  const app = new App();
  app.state.settings = {actions:{plan:'quick'}, harnesses:{quick:{kind:'fake',label:'Quick',enabled:true,models:{allowed:['small','blocked'],disallowed:['blocked'],default:{plan:'small'}}},gone:{enabled:false,models:{}}}, pins:[{label:'Fast plan',harness:'quick',model:'small',actions:['plan']},{label:'Capture only',harness:'quick',model:'small',actions:['triage']}],model_suggestions:{quick:['small','blocked','other']}};
  assert.deepEqual(app.modelsFor('quick'), ['small']);
  assert.equal(app.pick('plan').backend, 'quick');
  const nodes = walk(app.renderPicker('plan','Plan with'));
  assert(nodes.some(n => n.type === 'option' && n.props.value === 'auto'));
  assert(!nodes.some(n => n.type === 'option' && n.props.value === 'gone'));
  assert(strings(app.renderPicker('plan','Plan with')).includes('Fast plan'));
  assert(!strings(app.renderPicker('plan','Plan with')).includes('Capture only'));
  app.state.picks.plan = {backend:'auto'};
  assert(walk(app.renderPicker('plan','Plan with')).find(n => n.type === 'input').props.disabled);
});

test('settings renders editable harnesses, action defaults, discovery and pins', () => {
  const app = new App();
  app.state.settings = {actions:Object.fromEntries(App.actions.map(a => [a,'test'])),harnesses:{test:{kind:'fake',label:'Test',enabled:true,models:{allowed:[],disallowed:[],default:{}},instructions:{},loadout:{}}},pins:[{label:'Pinned',harness:'test',model:'small'}]};
  app.state.discovered = {test:{skills:['jot'],mcp:['docs'],plugins:['local-plugin']}};
  const nodes = walk(app.renderSettings()), text = strings(app.renderSettings());
  assert(text.includes('Save settings') && text.includes('Discover') && nodes.some(n => n.props?.value === 'Pinned'));
  assert(nodes.some(n => n.props?.['aria-label'] === 'test plan instructions'));
  assert(nodes.some(n => n.type === 'fieldset' && n.props.disabled));
  assert(text.includes('jot') && text.includes('docs') && text.includes('local-plugin'));
  assert.equal(App.route('#/settings').view, 'Settings');
  assert(App.eventSummary({kind:'routing',body:{action:'plan',harness:'test',model:'small',reason:'Routine'}}).includes('Routine'));
  assert(App.loadoutSummary('{"skills":[],"mcp":[],"plugins":[],"project_instructions":true}').includes('repo instructions: on'));
});

test('capture picker is available without an open task drawer', () => {
  const app = new App();
  app.state.loaded = true;
  const nodes = walk(app.render());
  assert(nodes.some(n => n.props?.class === 'capture-chip'));
  assert(!nodes.some(n => n.props?.role === 'dialog'));
  assert.equal(nodes.filter(n => n.props?.id === 'capture').length, 1);
});
