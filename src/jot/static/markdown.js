import { h } from './vendor/preact.mjs';

// Safe Markdown → Preact vnodes. Text is only ever passed as vnode children,
// which Preact escapes, so raw HTML in agent output renders as plain text.
// Never use innerHTML / dangerouslySetInnerHTML here.

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^ {0,3}> ?/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const INLINE = new RegExp([
  '`([^`]+)`',                                        // 1 code
  '\\[([^\\]]+)\\]\\(\\s*([^)\\s]+)(?:\\s+"[^"]*")?\\s*\\)', // 2 text, 3 url
  '<((?:https?:\\/\\/|mailto:)[^\\s>]+)>',             // 4 autolink
  '\\*\\*(.+?)\\*\\*', '__(.+?)__',                     // 5, 6 strong
  '~~(.+?)~~',                                        // 7 strike
  '\\*(?=\\S)(.+?)\\*', '(?<![\\w])_(?=\\S)(.+?)_(?![\\w])', // 8, 9 em
  '(https?:\\/\\/[^\\s<]*[^\\s<.,;:!?)\\]\'"])',        // 10 bare url
].join('|'));

const safeUrl = url => {
  const clean = String(url).replace(/[\u0000-\u001f\u007f\s]/g, '');
  return /^(https?:\/\/|mailto:)/i.test(clean) ? clean : null;
};

const link = (url, children) => {
  const href = safeUrl(url);
  return href ? h('a', {href, target:'_blank', rel:'noopener noreferrer'}, children) : children;
};

function inline(text) {
  const out = [];
  let rest = String(text);
  while (rest) {
    const m = INLINE.exec(rest);
    if (!m) { out.push(rest); break; }
    if (m.index) out.push(rest.slice(0, m.index));
    if (m[1] !== undefined) out.push(h('code', null, m[1]));
    else if (m[2] !== undefined) out.push(...[link(m[3], inline(m[2]))].flat());
    else if (m[4] !== undefined) out.push(link(m[4], m[4]));
    else if (m[5] !== undefined || m[6] !== undefined) out.push(h('strong', null, inline(m[5] ?? m[6])));
    else if (m[7] !== undefined) out.push(h('del', null, inline(m[7])));
    else if (m[8] !== undefined || m[9] !== undefined) out.push(h('em', null, inline(m[8] ?? m[9])));
    else out.push(link(m[10], m[10]));
    rest = rest.slice(m.index + m[0].length);
  }
  return out;
}

const indent = line => line.match(/^\s*/)[0].replace(/\t/g, '    ').length;
const blank = line => !line.trim();
const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
const starts = (lines, i) => FENCE.test(lines[i]) || HEADING.test(lines[i]) || RULE.test(lines[i])
  || QUOTE.test(lines[i]) || ITEM.test(lines[i]) || isTable(lines, i);
const isTable = (lines, i) => lines[i].includes('|') && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]) && lines[i + 1].includes('-');

function list(lines, i) {
  const base = indent(lines[i]);
  const ordered = /\d/.test(ITEM.exec(lines[i])[2]);
  const start = ordered ? parseInt(ITEM.exec(lines[i])[2], 10) : undefined;
  const items = [];
  while (i < lines.length) {
    // A blank line between sibling items keeps the list going.
    if (blank(lines[i]) && i + 1 < lines.length && ITEM.test(lines[i + 1]) && indent(lines[i + 1]) === base) i++;
    const m = ITEM.exec(lines[i]);
    if (!m || indent(lines[i]) !== base || /\d/.test(m[2]) !== ordered) break;
    const body = [m[3]];
    i++;
    // Continuation lines and nested blocks belong to the item while indented.
    while (i < lines.length && (blank(lines[i]) ? i + 1 < lines.length && indent(lines[i + 1]) > base : indent(lines[i]) > base)) {
      body.push(lines[i].slice(Math.min(indent(lines[i]), base + 2)));
      i++;
    }
    const [first, ...nested] = body;
    const task = /^\[([ xX])\]\s+(.*)$/.exec(first);
    const head = task
      ? [h('input', {type:'checkbox', checked:task[1] !== ' ', disabled:true}), ' ', ...inline(task[2])]
      : inline(first);
    items.push(h('li', null, head, nested.some(l => !blank(l)) ? blocks(nested) : null));
  }
  return [h(ordered ? 'ol' : 'ul', ordered && start !== 1 ? {start} : null, items), i];
}

function blocks(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (blank(line)) { i++; continue; }
    const fence = FENCE.exec(line);
    if (fence) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) code.push(lines[i++]);
      i++;
      out.push(h('pre', null, h('code', fence[2] ? {class:'language-' + fence[2]} : null, code.join('\n'))));
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) { out.push(h('h' + heading[1].length, null, inline(heading[2]))); i++; continue; }
    if (RULE.test(line)) { out.push(h('hr', null)); i++; continue; }
    if (QUOTE.test(line)) {
      const quoted = [];
      while (i < lines.length && QUOTE.test(lines[i])) quoted.push(lines[i++].replace(QUOTE, ''));
      out.push(h('blockquote', null, blocks(quoted)));
      continue;
    }
    if (ITEM.test(line)) { const [node, next] = list(lines, i); out.push(node); i = next; continue; }
    if (isTable(lines, i)) {
      const head = cells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && !blank(lines[i])) rows.push(cells(lines[i++]));
      out.push(h('div', {class:'md-table'}, h('table', null,
        h('thead', null, h('tr', null, head.map(c => h('th', null, inline(c))))),
        h('tbody', null, rows.map(r => h('tr', null, head.map((_, k) => h('td', null, inline(r[k] ?? '')))))))));
      continue;
    }
    const para = [line.trim()];
    i++;
    while (i < lines.length && !blank(lines[i]) && !starts(lines, i)) para.push(lines[i++].trim());
    out.push(h('p', null, inline(para.join(' '))));
  }
  return out;
}

/** Render Markdown text as a `.md` block of Preact vnodes. */
export function markdown(text, extraClass = '') {
  return h('div', {class:('md ' + extraClass).trim()}, blocks(String(text ?? '').replace(/\r\n?/g, '\n').split('\n')));
}

export {safeUrl};
