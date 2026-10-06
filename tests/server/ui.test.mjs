import {test} from 'node:test';
import assert from 'node:assert/strict';
import {App, TaskEditor, ProjectEditor} from '../../src/jot/static/app.js';

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
  assert.deepEqual(labels('awaiting_approval'), ['Approve', 'Send back']);
  assert.deepEqual(labels('executing'), ['Cancel']);
  assert.deepEqual(labels('review'), ['Done', 'Send back']);
  assert.deepEqual(labels('done'), []);
  const detail = {events:[{kind:'plan',body:{text:'Step 1'}},{kind:'question',body:{text:'Which DB?'}}]};
  const texts = walk(app.renderExpanded({...task, status:'awaiting_approval'}, detail))
    .flatMap(n => [n.props?.children].flat()).filter(c => typeof c === 'string');
  assert(texts.includes('Step 1') && texts.includes('Which DB?'));
});

test('model pickers resolve per-action choices with config defaults', () => {
  const app = new App();
  Object.assign(app.state, {backend:'claude', config:{model_defaults:{claude:{plan:'opus'}}, model_suggestions:{claude:['opus','sonnet']}},
    picks:{plan:{backend:'claude', model:''}, execute:{backend:'codex', model:'gpt-6-astra'}}});
  assert.deepEqual(app.pick('plan'), {backend:'claude', model:null});
  assert.deepEqual(app.pick('execute'), {backend:'codex', model:'gpt-6-astra'});
  const input = walk(app.renderPicker('plan', 'Plan with')).find(n => n.type === 'input');
  assert.equal(input.props.placeholder, 'default (opus)');
  assert.equal(walk(app.renderDatalists()).filter(n => n.type === 'option').length, 2);
});
