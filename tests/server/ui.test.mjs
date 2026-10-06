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
