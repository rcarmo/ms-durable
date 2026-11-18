/** Deterministic UI test against a fixture API. No model calls; run via make test-ui. */
import { chromium, type Page } from 'playwright';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const base = process.env.MS_DURABLE_TEST_TMP;
if (!base) throw new Error('Run via make test-ui (project-scoped temporary paths required).');
const root = resolve(base, 'runs', 'ui-' + Date.now());
await mkdir(root, {recursive: true});
const html = await Bun.file(resolve(import.meta.dir, '../static/index.html')).text();
const token = 'synthetic-ui-token';
const now = Math.floor(Date.now() / 1000);
const models = [{id: 'gpt-5-mini', name: 'GPT-5 mini'}, {id: 'claude-haiku-4.5', name: 'Claude Haiku 4.5'}];
const sessions = [
  {id: 's1', title: 'Summarise the release notes', handle:'review', model: 'gpt-5-mini', created_at: now - 600},
  {id: 's2', title: 'Check the backup job', model: 'claude-haiku-4.5', created_at: now - 86400 * 3},
];
const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><script>window.svgRan=true</script><rect width="12" height="12"/></svg>';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');
const turns: any[] = [
  {id: 't1', kind: 'turn', prompt: 'List the files in the workspace.', status: 'done', answer: 'There are two files: `AGENTS.md` and `notes.txt`.\n\n| File | State |\n| --- | --- |\n| notes.txt | **OK** |\n\n<b>literal HTML</b>', created_at: now - 590, finished_at: now - 585, images:[{name:'diagram.svg', ordinal:0, mime:'image/svg+xml', digest:'svg'}, {name:'dot.png', ordinal:1, mime:'image/png', digest:'png'}]},
  {id: 't2', kind: 'compact', prompt: '', status: 'done', answer: JSON.stringify({success: true, messagesRemoved: 6, tokensRemoved: 4210}), created_at: now - 500},
  {id: 't3', kind: 'turn', prompt: 'Write a short summary to summary.md. [[message:s1:t1:answer]]', sender_id:'s2', sender_handle:'build', status: 'running', answer: '', created_at: now - 30},
];
const events = [
  {seq: 1, turn_id: 't1', type: 'tool.execution_start', data: {toolCallId: 'a', toolName: 'bash', arguments: {command: 'ls -1'}}},
  {seq: 2, turn_id: 't1', type: 'tool.execution_complete', data: {toolCallId: 'a', success: true, result: {content: '{"files":["AGENTS.md","notes.txt"],"ok":true}'}}},
  {seq: 3, turn_id: 't3', type: 'assistant.reasoning_delta', data: {deltaContent: 'I should write the file with the edit tool.'}},
  {seq: 4, turn_id: 't3', type: 'tool.execution_start', data: {toolCallId: 'b', toolName: 'create', arguments: {path: 'summary.md'}}},
];
const pendingApproval = {id: 'p1', status: 'pending', request: JSON.stringify({kind: 'write', fileName: 'summary.md', intention: 'Create the summary file'})};
let approvals = [pendingApproval];
const plan = {revision:1, explanation:'Release checklist', items:[{step:'Inspect',status:'in_progress'}]};
let emitPlan = () => {};
const calls: string[] = [];
const server = Bun.serve({port: 0, hostname: '127.0.0.1', idleTimeout: 0, async fetch(request) {
  const url = new URL(request.url), path = url.pathname;
  if (path === '/vendor/markdown-it.min.js') return new Response(Bun.file(resolve(import.meta.dir, '../static/vendor/markdown-it.min.js')), {headers:{'Content-Type':'text/javascript'}});
  if (path === '/') return new Response(html, {headers: {'Content-Type': 'text/html'}});
  if (request.headers.get('authorization') !== 'Bearer ' + token) return new Response('{}', {status: 401});
  calls.push(request.method + ' ' + path);
  const json = (value: unknown) => Response.json(value);
  if (path === '/api/config') return json({models, skills: ['memento'], mcp_servers: ['memento'], working_directory: '/srv/workspace', auth_source: 'Copilot CLI login'});
  if (path === '/api/sessions' && request.method === 'GET') return json(sessions);
  if (path === '/api/sessions/s1/title' && request.method === 'POST') { const value = await request.json() as {title:string}; sessions[0].title = value.title; return json(sessions[0]); }
  if (path === '/api/sessions/s1') return json({session: sessions[0], turns, events, approvals, plan});
  if (path === '/api/sessions/s1/turns/t1/images/0') return new Response(svg, {headers:{'Content-Type':'image/svg+xml'}});
  if (path === '/api/sessions/s1/turns/t1/images/1') return new Response(png, {headers:{'Content-Type':'image/png'}});
  if (path === '/api/sessions/s1/timeline') return json(url.searchParams.get('after') === '0' ? {events:events.slice(0,2),next_after:2} : {events:events.slice(2),next_after:null});
  if (path === '/api/sessions/s1/turns' && request.method === 'POST') {
    const body = await request.json() as any;
    if (body.prompt !== '[[message:s1:t1:answer]]') return new Response('{}', {status:422});
    if (body.images?.[0]?.data !== Buffer.from(svg).toString('base64')) return new Response('{}', {status:422});
    const turn = {id:'upload', prompt:body.prompt, status:'queued', created_at:now, images:[turns[0].images[0]]};
    turns.push(turn); return json(turn);
  }
  if (path === '/api/sessions/s1/turns/upload/images/0') return new Response(svg,{headers:{'Content-Type':'image/svg+xml'}});
  if (path === '/api/sessions/s2') return json({session: sessions[1], turns: [], events: [], approvals: []});
  if (path.endsWith('/events')) return new Response(new ReadableStream({start(c) {
    const encode = (s:string) => new TextEncoder().encode(s); c.enqueue(encode(': open\n\n'));
    if (path.includes('/s1/')) emitPlan = () => c.enqueue(encode('data: ' + JSON.stringify({seq:5,type:'app.plan_updated',data:{revision:2,explanation:plan.explanation,items:[{step:'Inspect',status:'completed'}]}}) + '\n\n'));
  }}), {headers: {'Content-Type': 'text/event-stream'}});
  if (path.endsWith('/context')) return json({context: {contextInfo: {limit: 128000, totalTokens: 41200, systemTokens: 9800, toolDefinitionsTokens: 14100, conversationTokens: 17300, compactionThreshold: 102400, modelName: 'gpt-5-mini'}}, attribution: {contextAttribution: {compactions: {count: 1}}}});
  if (path.startsWith('/api/approvals/')) { approvals = []; return json({status: 'approved'}); }
  if (path.endsWith('/model') || path.endsWith('/abort') || path.endsWith('/compact')) return json({status: 'ok'});
  return new Response('{}', {status: 404});
}});
const url = `http://127.0.0.1:${server.port}`;
const checks: string[] = [];
const browser = await chromium.launch({headless: true});
async function run(name: string, viewport: {width: number, height: number}, scheme: 'dark' | 'light', fn: (page: Page) => Promise<void>) {
  const context = await browser.newContext({viewport, colorScheme: scheme, hasTouch: viewport.width < 600, isMobile: viewport.width < 600});
  approvals = [pendingApproval];
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  try { await fn(page); } finally { await context.close(); }
  if (errors.length) throw new Error(name + ': ' + errors.join('; '));
  checks.push(name);
}
try {
  await run('desktop dark: transcript, activity, approval, context', {width: 1280, height: 860}, 'dark', async page => {
    await page.goto(url);
    await page.screenshot({path: root + '/auth.png'});
    await page.locator('#token').fill(token); await page.keyboard.press('Enter');
    await page.getByRole('button', {name: 'Approve once'}).waitFor();
    if (await page.locator('.turn').count() !== 3) throw new Error('expected three turns');
    await page.locator('.compact-note').filter({hasText: '6 messages'}).waitFor();
    await page.locator('#stop').waitFor({state: 'visible'});
    await page.locator('.answer code').filter({hasText: 'AGENTS.md'}).waitFor();
    if ((await page.locator('.answer').first().textContent())!.includes('`')) throw new Error('inline code delimiters rendered');
    if (await page.locator('.answer.working').count()) throw new Error('redundant Working indicator beside activity');
    if (await page.locator('.activity .spin').count() !== 0 || await page.locator('.activity .wait').count() !== 1) throw new Error('expected one approval-pending indicator and no spinners');
    for (const selector of ['.activity','#notice','.approval','.approval code','.prompt']) {
      const rail = await page.locator(selector).first().evaluate(el => getComputedStyle(el).borderLeftWidth);
      if (rail !== '0px') throw new Error(selector + ': decorative edge ' + rail);
    }
    const draft = page.locator('#prompt'), send = page.locator('#send');
    for (const value of ['   ', 'Follow up', '']) {
      await draft.fill(value);
      if (await send.isEnabled() !== Boolean(value.trim())) throw new Error('Send state for draft: ' + value);
    }
    if (!await page.locator('#stop').isEnabled()) throw new Error('Stop must not require a draft');
    await page.keyboard.press('Tab');
    if (!await page.locator('#model').evaluate(el => el.matches(':focus-visible') && getComputedStyle(el).outlineStyle !== 'none')) throw new Error('Missing keyboard focus');
    await draft.blur(); await page.locator('#model').blur();
    await page.screenshot({path: root + '/desktop-dark.png'});
    await page.emulateMedia({colorScheme:'light'});
    await page.screenshot({path: root + '/desktop-light.png'});
    await page.emulateMedia({forcedColors:'active'});
    if (!await page.locator('#sessions [aria-current=true]').evaluate(el => getComputedStyle(el).borderTopStyle === 'solid' && getComputedStyle(el).borderColor !== 'transparent')) throw new Error('Missing selected-session boundary in forced colours');
    await page.emulateMedia({forcedColors:'none',colorScheme:'dark'});
    await page.locator('.activity summary').first().click();
    await page.locator('.activity pre').filter({hasText: 'notes.txt'}).waitFor();
    const output = page.locator('.activity details[open]').first();
    const pre = output.locator('pre');
    await pre.locator('.key').filter({hasText:'command'}).waitFor();
    await pre.locator('.key').filter({hasText:'files'}).waitFor();
    if (!(await pre.textContent())?.includes('\n  "files": [')) throw new Error('Result JSON was not pretty-printed');
    await output.getByRole('button',{name:'Raw',exact:true}).click();
    const raw = JSON.parse((await pre.textContent())!);
    if (raw.arguments.command !== 'ls -1' || raw.result.content !== events[1].data.result?.content) throw new Error('Raw payload changed');
    await output.getByRole('button',{name:'Readable',exact:true}).click();
    await pre.locator('.key').filter({hasText:'files'}).waitFor();
    await page.locator('#context-button').click();
    await page.waitForFunction(() => document.querySelector('#context-summary')?.textContent?.includes('41k of 128k')).catch(async e => { throw new Error('context: ' + await page.evaluate(() => JSON.stringify({open: (document.querySelector('#context-dialog') as any).open, s: document.querySelector('#context-summary')!.textContent}))); });
    await page.screenshot({path: root + '/context.png'});
    await page.locator('#compact').click();
    await page.locator('#context-dialog').waitFor({state: 'hidden'});
    await page.getByRole('button', {name: 'Approve once'}).click();
    await page.locator('.approval').waitFor({state: 'detached'});
    await page.locator('#open-settings').click();
    await page.locator('#config').filter({hasText: '/srv/workspace'}).waitFor();
    await page.screenshot({path: root + '/settings.png'});
    await page.keyboard.press('Escape');
    await page.locator('#sessions button').nth(1).click();
    await page.locator('#empty').waitFor({state: 'visible'});
    await page.locator('#new').click();
    await page.locator('#title').filter({hasText: 'New conversation'}).waitFor();
  });
  await run('mobile light: drawer and composer', {width: 390, height: 780}, 'light', async page => {
    await page.goto(url + '#token=' + token);
    await page.locator('.turn').first().waitFor();
    await page.getByRole('button', {name: 'Approve once'}).waitFor();
    if (!await page.getByRole('button', {name: 'Approve once'}).isVisible()) throw new Error('approval not visible on mobile');
    if (await page.locator('#sidebar').isVisible()) throw new Error('closed drawer is visible');
    for (const id of ['#menu', '#send']) { const box = (await page.locator(id).boundingBox())!; if (box.width < 44 || box.height < 44) throw new Error(id + ' touch target too small'); }
    for (const el of [page.getByRole('button',{name:'Deny'}),page.getByRole('button',{name:'Approve once'}),page.locator('#composer')]) {
      const b=await el.boundingBox(); if (!b || b.x<0 || b.y<0 || b.y+b.height>780 || b.x+b.width>390) throw new Error('Mobile control clipped');
    }
    await page.screenshot({path: root + '/mobile-light.png'});
    await page.locator('#menu').click();
    await page.locator('#sidebar.open').waitFor();
    await page.waitForTimeout(300);
    if ((await page.locator('#sidebar').boundingBox())!.x !== 0) throw new Error('drawer not in view');
    await page.screenshot({path: root + '/mobile-drawer.png'});
    await page.keyboard.press('Escape');
    if (await page.locator('#sidebar.open').count()) throw new Error('drawer did not close');
  });
  for (const expected of ['POST /api/approvals/p1', 'POST /api/sessions/s1/compact']) if (!calls.includes(expected)) throw new Error('missing call ' + expected);
  await run('chat features: images, references, timestamps, theme and paginated timeline', {width:1280,height:860}, 'light', async page => {
    await page.context().grantPermissions(['clipboard-read','clipboard-write']);
    await page.goto(url + '#token=' + token);
    await page.waitForFunction(() => [...document.querySelectorAll<HTMLImageElement>('.message-images img')].length === 2 && [...document.querySelectorAll<HTMLImageElement>('.message-images img')].every(i => i.naturalWidth > 0));
    if (await page.evaluate(() => (window as any).svgRan)) throw new Error('SVG script ran');
    await page.getByText('@build',{exact:true}).waitFor();
    const first = page.locator('#message-t1');
    await first.locator('.answer table strong').getByText('OK').waitFor();
    if (await first.locator('.answer b').count()) throw new Error('Raw HTML was rendered');
    const slots = await first.locator('.message-meta').evaluateAll(rows => rows.map(row => [row.firstElementChild?.textContent,row.children[1]?.querySelector('time')?.dateTime]));
    if (slots[0][0] !== 'You' || slots[1][0] !== '@review' || slots.some(row => !row[1])) throw new Error('Name must precede timestamp');
    await page.getByRole('button',{name:'Rename session'}).click(); await page.locator('#rename-name').fill('Review notes');
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.locator('#title').filter({hasText:'@review · Review notes'}).waitFor();
    await page.locator('#sessions button').filter({hasText:'@review · Review notes'}).waitFor();
    for (const t of await first.locator('time').all()) if (!await t.getAttribute('datetime')) throw new Error('Missing timestamp');
    await first.getByRole('button',{name:'Copy answer'}).focus(); await page.keyboard.press('Enter');
    if (!(await page.evaluate(() => navigator.clipboard.readText())).includes('AGENTS.md')) throw new Error('Copy failed');
    await first.getByRole('button',{name:'Reference answer'}).focus(); await page.keyboard.press('Enter');
    if (!(await page.locator('#prompt').inputValue()).includes('#t1a')) throw new Error('Missing reference');
    for (const name of ['Copy answer','Reference answer']) {
      await first.getByRole('button',{name}).locator('svg').waitFor();
    }
    await page.locator('#prompt').fill(''); await first.locator('.message-time').last().click();
    if (!(await page.locator('#prompt').inputValue()).includes('#t1a')) throw new Error('Timestamp did not reference answer');
    await page.getByRole('link',{name:'View referenced answer'}).focus(); await page.keyboard.press('Enter');
    if (!await first.locator('.answer').evaluate(el => el === document.activeElement)) throw new Error('Reference did not focus target');
    if (await page.locator('.topbar #timeline-button').count()) throw new Error('Event log is a diagnostic');
    await page.locator('#open-settings').click(); await page.getByRole('button',{name:'Session event log'}).click(); await page.getByRole('button',{name:'Load more events'}).click();
    await page.getByText('#4 · tool.execution_start').waitFor(); await page.keyboard.press('Escape');
    await page.locator('#open-settings').click(); await page.locator('#theme').selectOption('dark'); await page.keyboard.press('Escape');
    await page.locator('#prompt').fill('#t1a');
    await page.locator('#image-input').setInputFiles({name:'diagram.svg',mimeType:'image/svg+xml',buffer:Buffer.from(svg)});
    await page.getByRole('button',{name:'Remove diagram.svg'}).waitFor(); await page.locator('#send').click();
    await page.locator('#message-upload img').waitFor();
    await page.reload(); await page.locator('#message-upload img').waitFor();
    await page.locator('#title').filter({hasText:'@review · Review notes'}).waitFor();
    if (await page.locator('html').getAttribute('data-theme') !== 'dark') throw new Error('Theme not preserved');
  });
  await run('Plan panel: live updates, focus, session separation and narrow layout', {width:390,height:780}, 'light', async page => {
    await page.goto(url + '#token=' + token);
    await page.locator('#connection[data-state=live]').waitFor({state:'attached'});
    await page.locator('#plan-toggle').focus(); await page.keyboard.press('Enter');
    await page.locator('#plan-items').getByText('In progress').waitFor();
    if (!await page.locator('#plan-close').evaluate(el => el === document.activeElement)) throw new Error('Plan should move focus on open');
    emitPlan(); await page.locator('#plan-summary').filter({hasText:'1 of 1 completed'}).waitFor();
    const panel = (await page.locator('#plan-panel').boundingBox())!, composer = (await page.locator('#composer').boundingBox())!;
    if (panel.x < 0 || panel.x + panel.width > 390 || panel.y + panel.height > composer.y) throw new Error('Plan panel overlaps composer or viewport');
    await page.locator('#prompt').fill('Can still type');
    await page.keyboard.press('Escape');
    if (await page.locator('#plan-panel').isVisible() || !await page.locator('#plan-toggle').evaluate(el => el === document.activeElement)) throw new Error('Escape did not restore focus');
    await page.locator('#plan-toggle').click(); await page.locator('#menu').click();
    await page.locator('#sessions button').nth(1).click(); await page.locator('#plan-summary').filter({hasText:'No plan yet.'}).waitFor();
    await page.locator('#menu').click(); await page.locator('#sessions button').first().click();
    await page.locator('#plan-items').getByText('In progress').waitFor();
    await page.screenshot({path: root + '/plan-mobile.png'});
    await page.setViewportSize({width:1280,height:860});
    const dock = (await page.locator('#plan-panel').boundingBox())!, chat = (await page.locator('#chat-column').boundingBox())!, input = (await page.locator('#composer').boundingBox())!;
    if (dock.x < chat.x + chat.width || dock.x < input.x + input.width || dock.width !== 320) throw new Error('Plan should dock beside chat and composer');
    await page.screenshot({path: root + '/plan-desktop.png'});
  });
  console.log(JSON.stringify({checks, evidence: root}, null, 2));
} finally {
  await browser.close(); server.stop(true);
}
