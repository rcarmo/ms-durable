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
const inline = '\n\n[Example](https://example.com/) and [same page](https://example.com/#details)\n\n> https://example.org/article\n\n![Inline PNG](data:image/png;base64,' + png.toString('base64') + ')\n\n' + '```svg\n' + svg + '\n```';
const turns: any[] = [
  {id: 't1', kind: 'turn', prompt: 'List the files in the workspace.', status: 'done', answer: 'There are two files: `AGENTS.md` and `notes.txt`.\n\n| File | State |\n| --- | --- |\n| notes.txt | **OK** |\n\n<b>literal HTML</b>' + inline, created_at: now - 590, finished_at: now - 585, images:[{name:'diagram.svg', ordinal:0, mime:'image/svg+xml', digest:'svg'}, {name:'dot.png', ordinal:1, mime:'image/png', digest:'png'}]},
  {id: 't2', kind: 'compact', prompt: '', status: 'done', answer: JSON.stringify({success: true, messagesRemoved: 6, tokensRemoved: 4210}), created_at: now - 500},
  {id: 't3', kind: 'turn', prompt: 'Write a short summary to summary.md. [[message:s1:t1:answer]]', sender_id:'s2', sender_handle:'build', status: 'running', answer: '', created_at: now - 30},
];
const events = [
  {seq: 1, turn_id: 't1', type: 'tool.execution_start', data: {toolCallId: 'a', toolName: 'bash', arguments: {command: 'ls -1', description:'Checking which files are present'}}},
  {seq: 2, turn_id: 't1', type: 'tool.execution_complete', data: {toolCallId: 'a', success: true, result: {content: '{"files":["AGENTS.md","notes.txt"],"ok":true}'}}},
  {seq: 3, turn_id: 't3', type: 'assistant.reasoning_delta', data: {deltaContent: '**Prepare** the file with the edit tool. [Reasoning source](https://reasoning.example/).'}},
  {seq: 4, turn_id: 't3', type: 'tool.execution_start', data: {toolCallId: 'b', toolName: 'create', arguments: {path: 'summary.md'}}},
  {seq: 6, turn_id: 't1', type: 'tool.execution_start', data: {toolCallId: 'chat-call', toolName: 'chat', arguments: {session_id:'@build',prompt:'Check the build.'}}},
  {seq: 7, turn_id: 't1', type: 'assistant.reasoning_delta', data: {deltaContent: 'Verify independently.'}},
  {seq: 8, turn_id: 't1', type: 'app.chat_sent', data: {destination:'@old',message:'An older message.'}},
  {seq: 9, turn_id: 't1', type: 'tool.execution_start', data: {toolCallId: 'later', toolName: 'read', arguments: {path:'notes.txt'}}},
  {seq:10, turn_id:'t1', type:'tool.execution_start', data:{toolCallId:'inspect',toolName:'observe_session',toolTitle:'Inspect timeline',arguments:'{"session_id":"s2","query":"  build\\n log  "}'}},
  {seq:11, turn_id:'t1', type:'tool.execution_start', data:{toolCallId:'current',toolName:'app_observe_session',arguments:{}}},
  {seq:12, turn_id:'t1', type:'tool.execution_start', data:{toolCallId:'change',toolName:'set_model',arguments:{session_id:'s1',model:'gpt-5-mini'}}},
  {seq:13, turn_id:'t1', type:'tool.execution_start', data:{toolCallId:'workers',toolName:'delegate_to_sessions',arguments:{tasks:[{session_id:'s1',prompt:'Omit from summary'},{session_id:'@build'}]}}},
];
const pendingApproval = {id: 'p1', status: 'pending', request: JSON.stringify({kind: 'write', fileName: 'summary.md', intention: 'Create the summary file'})};
let approvals = [pendingApproval];
let linkPreviews = {enabled:false,any_domain:false};
const plan = {revision:1, explanation:'Release checklist', items:[{step:'Inspect',status:'in_progress'}]};
let emitPlan = () => {}, emitChat = () => {}, emitChatUpdate = (_: unknown) => {};
let timelineListener: ((emit: (event: unknown) => void) => void) | null = null;
const calls: string[] = [];
const server = Bun.serve({port: 0, hostname: '127.0.0.1', idleTimeout: 0, async fetch(request) {
  const url = new URL(request.url), path = url.pathname;
  if (path === '/vendor/markdown-it.min.js') return new Response(Bun.file(resolve(import.meta.dir, '../static/vendor/markdown-it.min.js')), {headers:{'Content-Type':'text/javascript'}});
  if (path === '/') return new Response(html, {headers: {'Content-Type': 'text/html'}});
  const reportPath = '/media/shared/s1/test-report/file';
  if (path === reportPath && request.method === 'GET' && request.headers.get('cookie')?.includes('fixture_download=ok') && !request.headers.has('authorization')) {
    return new Response('Report', {headers:{'Content-Type':'application/octet-stream', 'Content-Disposition':'attachment; filename="report.txt"'}});
  }
  if (request.headers.get('authorization') !== 'Bearer ' + token) return new Response('{}', {status: 401});
  calls.push(request.method + ' ' + path);
  const json = (value: unknown) => Response.json(value);
  if (path === '/api/config') return json({models, link_previews:linkPreviews, summary_model:'gpt-5.6-luna', skills: ['memento'], mcp_servers: ['memento'], working_directory: '/srv/workspace', auth_source: 'Copilot CLI login'});
  if (path === '/api/settings/link-previews') { linkPreviews = await request.json() as typeof linkPreviews; return json(linkPreviews); }
  if (path === '/api/link-preview') {
    const source = url.searchParams.get('url');
    if (source === 'https://example.com/') return json({title:'Example preview', description:'Cached description', image:'/media/preview/abcdef'});
    if (source === 'https://example.org/article') return json({title:'Article preview', image:'/media/preview/123456'});
    return json({title:'Unexpected preview'});
  }
  if (['/media/preview/abcdef','/media/preview/123456'].includes(path)) return new Response(png, {headers:{'Content-Type':'image/png'}});
  if (path === '/api/sessions' && request.method === 'GET') return json(sessions);
  if (path === '/api/sessions/s1/summary') return json({text:'Reviewed the release files.',model:'gpt-5.6-luna'});
  if (path === '/api/sessions/s1/title' && request.method === 'POST') { const value = await request.json() as {title:string,handle:string,delegation_enabled:boolean,description:string}; Object.assign(sessions[0],value); return json(sessions[0]); }
  if (path === '/api/sessions/s1') return json({session: sessions[0], turns, events, approvals, plan});
  if (path === '/media/s1/t1/0') return new Response(svg, {headers:{'Content-Type':'image/svg+xml'}});
  if (path === '/media/s1/t1/1') return new Response(png, {headers:{'Content-Type':'image/png'}});
  if (path === '/media/shared/s1/test-gif/file') return new Response(Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64'), {headers:{'Content-Type':'image/gif'}});
  if (path === reportPath && request.method === 'POST') return new Response(null, {status:204, headers:{'Set-Cookie':'fixture_download=ok; HttpOnly; SameSite=Strict; Path=' + reportPath}});
  if (path === '/api/sessions/s1/timeline') return json(url.searchParams.get('after') === '0' ? {events:events.slice(0,2),next_after:2} : {events:events.slice(2),next_after:null});
  if (path === '/api/sessions/s1/turns' && request.method === 'POST') {
    const body = await request.json() as any;
    if (body.prompt !== '[[message:s1:t1:answer]]') return new Response('{}', {status:422});
    if (body.images?.[0]?.data !== Buffer.from(svg).toString('base64')) return new Response('{}', {status:422});
    const turn = {id:'upload', prompt:body.prompt, status:'queued', created_at:now, images:[turns[0].images[0]]};
    turns.push(turn); return json(turn);
  }
  if (path === '/media/s1/upload/0') return new Response(svg,{headers:{'Content-Type':'image/svg+xml'}});
  if (path === '/api/sessions/s2') return json({session: sessions[1], turns: [], events: [], approvals: []});
  if (path.endsWith('/events')) return new Response(new ReadableStream({start(c) {
    const encode = (s:string) => new TextEncoder().encode(s); c.enqueue(encode(': open\n\n'));
    if (path.includes('/s1/')) {
      emitChatUpdate = event => c.enqueue(encode('data: ' + JSON.stringify(event) + '\n\n'));
      timelineListener?.(emitChatUpdate);
    }
    if (path.includes('/s1/')) emitChat = () => c.enqueue(encode('data: ' + JSON.stringify({seq:9001, turn_id:'t1', type:'app.chat_sent',data:{tool_call_id:'chat-call',destination:'@build',message:'Check the build.'}}) + '\n\n'));
    if (path.includes('/s1/')) emitPlan = () => {
      plan.revision++; plan.items[0].status = 'completed';
      c.enqueue(encode('data: ' + JSON.stringify({seq:5,type:'app.plan_updated',data:plan}) + '\n\n'));
    };
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
  if (process.env.MS_DURABLE_TIMELINE_PROFILE) {
    const fixture = Array.from({length: 400}, (_, i) => ({
      id: 'bulk-' + i, status: 'done', prompt: 'Question ' + i, created_at: now,
      answer: ('A paragraph with **formatting** and enough text to wrap when the window narrows. '.repeat(5) + '\n\n').repeat(4),
    }));
    await run('long timeline resize and return', {width:1280,height:860}, 'light', async page => {
      let unblock!: () => void, delay = false;
      const blocked = new Promise<void>(resolve => { unblock = resolve; });
      await page.route('**/api/sessions/s1', async route => {
        if (delay) await blocked;
        await route.fulfill({json:{session:sessions[0], turns:fixture, events:[], approvals:[]}});
      });
      await page.goto(url + '#token=' + token);
      await page.locator('#message-bulk-399').waitFor({state:'attached'});
      const cdp = await page.context().newCDPSession(page); await cdp.send('Performance.enable');
      const metric = async (name: string) => (await cdp.send('Performance.getMetrics')).metrics.find(m => m.name === name)!.value;
      await page.locator('#connection[data-state=live]').waitFor();
      const scriptBefore = await metric('ScriptDuration');
      for (let frame = 0; frame < 24; frame++) {
        emitChatUpdate({seq:1000+frame,turn_id:'bulk-399',type:'assistant.reasoning_delta',data:{deltaContent:`Frame ${frame}. `}});
        await page.locator('#message-bulk-399 .reasoning-content').getByText(`Frame ${frame}.`,{exact:false}).waitFor({state:'attached'});
      }
      const streamScriptMs = (await metric('ScriptDuration') - scriptBefore) * 1000;
      const layout = async () => (await cdp.send('Performance.getMetrics')).metrics.find(m => m.name === 'LayoutDuration')!.value;
      const before = await layout();
      for (let i = 0; i < 12; i++) {
        await page.setViewportSize({width:i % 2 ? 1280 : 850,height:860});
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      }
      const layoutMs = (await layout() - before) * 1000;
      await page.locator('#sessions .session-select').getByText('Check the backup job').click();
      await page.waitForFunction(() => document.querySelectorAll('.turn').length === 0);
      delay = true;
      await page.locator('#sessions .session-select').getByText('Summarise the release notes', {exact:false}).click();
      try {
        const cachedBeforeResponse = await page.locator('#message-bulk-399').count() === 1;
        console.log(JSON.stringify({turns:fixture.length, resizes:12, liveFrames:24, streamScriptMs, layoutMs, cachedBeforeResponse}));
        if (process.env.MS_DURABLE_TIMELINE_PROFILE === 'verify' && !cachedBeforeResponse) throw new Error('Returning to a session waited on the network');
      } finally { unblock(); }
    });
  } else {
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
    await page.locator('.activity-title').getByText('Checking which files are present',{exact:true}).waitFor();
    await page.locator('.activity-title').getByText('Creating a file · summary.md',{exact:true}).waitFor();
    if (await page.locator('.activity summary').filter({hasText:'ls -1'}).count()) throw new Error('Raw command leaked into collapsed summary');
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
    await page.locator('.activity details[open] pre').filter({hasText: 'notes.txt'}).waitFor();
    const output = page.locator('.activity details[open]').first();
    const pre = output.locator('pre');
    await pre.locator('.key').filter({hasText:'command'}).waitFor();
    await pre.locator('.key').filter({hasText:'files'}).waitFor();
    if (!(await pre.textContent())?.includes('\n  "files": [')) throw new Error('Result JSON was not pretty-printed');
    await output.getByRole('button',{name:'Raw',exact:true}).click();
    const raw = JSON.parse((await pre.textContent())!);
    if (raw.tool !== 'bash' || raw.arguments.command !== 'ls -1' || raw.result.content !== events[1].data.result?.content) throw new Error('Raw payload changed');
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
    await page.locator('#sessions .session-select').nth(1).click();
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
    await page.locator('#connection[data-state=live]').waitFor();
    emitChat();
    const activity = page.locator('#message-t1 .activity > details');
    await activity.nth(1).locator('summary').getByText(/Sent to @build\s+Check the build\./).waitFor();
    const labels = await activity.locator('.activity-title > span:first-child').allTextContents();
    const expected = ['Checking which files are present','Sending a message · @build','Thinking through the request','Sending a message · @old','Reading a file · notes.txt',
      'Searching the session timeline · Check the backup job · “build log”', 'Inspecting the session timeline · Current session',
      'Changing the model · @review · gpt-5-mini', 'Waiting for delegates · @review, @build'];
    if (JSON.stringify(labels) !== JSON.stringify(expected)) throw new Error('Unexpected activity summaries: ' + JSON.stringify(labels));
    await activity.nth(3).locator('summary').getByText(/Sent to @old\s+An older message\./).waitFor();
    const first = page.locator('#message-t1');
    await first.locator('.answer table strong').getByText('OK').waitFor();
    await page.waitForFunction(() => { const images = [...document.querySelectorAll<HTMLImageElement>('.answer img.inline-svg')]; return images.length === 2 && images.every(img => img.naturalWidth > 0); });
    await page.locator('#message-t3 summary').filter({hasText:'Thinking through'}).click();
    await page.locator('.reasoning-content strong').getByText('Prepare').waitFor();
    await page.locator('#prompt').fill('/'); await page.getByRole('button',{name:'/plan',exact:true}).waitFor();
    await page.locator('#prompt').fill('/plan'); await page.locator('#prompt').press('Enter');
    await page.locator('#plan-panel').waitFor(); await page.keyboard.press('Escape');
    if (await first.locator('.answer b').count()) throw new Error('Raw HTML was rendered');
    const slots = await first.locator('.message-meta').evaluateAll(rows => rows.map(row => [row.firstElementChild?.textContent,row.children[1]?.querySelector('time')?.dateTime]));
    if (slots[0][0] !== 'You' || slots[1][0] !== '@review' || slots.some(row => !row[1])) throw new Error('Name must precede timestamp');
    await page.getByRole('button',{name:'Rename session'}).click(); await page.locator('#rename-name').fill('Review notes'); await page.locator('#rename-handle').fill('notes');
    await page.getByText('Reviewed the release files.').waitFor();
    await page.getByLabel('Description',{exact:true}).fill('Reviews release notes');
    await page.getByLabel('Allow other agents to delegate tasks to this session').check();
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.locator('#title').filter({hasText:'@notes · Review notes'}).waitFor();
    await page.locator('#sessions .session-select').filter({hasText:'@notes · Review notes'}).waitFor();
    await page.getByRole('button',{name:'Rename session'}).click();
    if (!(await page.locator('#delegate-enabled').isChecked())) throw new Error('Delegation opt-in did not persist');
    if (await page.getByLabel('Description',{exact:true}).inputValue() !== 'Reviews release notes') throw new Error('Description did not persist');
    if (!(await page.locator('#sessions .session-select').filter({hasText:'@notes · Review notes'}).getAttribute('aria-description'))?.includes('Reviews release notes')) throw new Error('Description not exposed in sidebar');
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
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
    await page.locator('#title').filter({hasText:'@notes · Review notes'}).waitFor();
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
    await page.locator('#sessions .session-select').nth(1).click(); await page.locator('#plan-summary').filter({hasText:'No plan yet.'}).waitFor();
    await page.locator('#menu').click(); await page.locator('#sessions .session-select').first().click();
    await page.locator('#plan-summary').filter({hasText:'1 of 1 completed'}).waitFor();
    await page.screenshot({path: root + '/plan-mobile.png'});
    await page.setViewportSize({width:1280,height:860});
    const dock = (await page.locator('#plan-panel').boundingBox())!, chat = (await page.locator('#chat-column').boundingBox())!, input = (await page.locator('#composer').boundingBox())!;
    if (dock.x < chat.x + chat.width || dock.x < input.x + input.width || dock.width !== 320) throw new Error('Plan should dock beside chat and composer');
    await page.screenshot({path: root + '/plan-desktop.png'});
  });
  await run('tablet sidebar: resize, collapse, agent activity and delete', {width:1024,height:768}, 'light', async page => {
    const child = {id:'child', title:'Agent-created chat', model:models[0].id, activity:'running'};
    let listed = false, deleted = false;
    await page.route('**/api/sessions', route => route.fulfill({json:listed && !deleted ? [child, ...sessions] : sessions}));
    await page.route('**/api/sessions/child', route => {
      if (route.request().method() !== 'DELETE') return route.fulfill({json:{session:child, turns:[]}});
      deleted = true; return route.fulfill({status:204});
    });
    await page.goto(url + '#token=' + token);
    await page.locator('#connection[data-state=live]').waitFor();
    const handle = page.getByRole('separator', {name:'Resize conversations sidebar'});
    const edge = (await handle.boundingBox())!;
    await page.mouse.move(edge.x + edge.width / 2, edge.height / 2);
    await page.mouse.down(); await page.mouse.move(edge.x + edge.width / 2 + 60, edge.height / 2, {steps:4}); await page.mouse.up();
    await handle.press('ArrowRight');
    if (await handle.getAttribute('aria-valuenow') !== '336') throw new Error('Sidebar did not resize by pointer and keyboard');
    await page.getByRole('button', {name:'Collapse conversations'}).click();
    await page.locator('#sidebar').waitFor({state:'hidden'});
    await page.reload(); await page.locator('#sidebar').waitFor({state:'hidden'});
    await page.locator('#menu').click();
    if (await handle.getAttribute('aria-valuenow') !== '336') throw new Error('Sidebar width was not retained');
    listed = true;
    const row = page.locator('.session-row').filter({hasText:child.title});
    await row.locator('.session-dot[data-state=running]').waitFor();
    const remove = row.getByRole('button', {name:'Delete ' + child.title});
    if (await remove.count() || await row.locator('.session-delete:disabled').count() !== 1) throw new Error('Running session can be deleted');
    child.activity = 'idle'; await row.locator('.session-dot[data-state=idle]').waitFor();
    await row.locator('.session-select').click();
    await page.locator('#connection[data-state=live]').waitFor();
    await page.mouse.move(900, 600); await page.locator('#prompt').focus();
    if (await remove.evaluate(el => getComputedStyle(el).opacity) !== '0') throw new Error('Delete icon should be quiet until hover or focus');
    await remove.focus();
    if (await remove.evaluate(el => getComputedStyle(el).opacity) !== '1') throw new Error('Keyboard focus did not reveal delete');
    const entry = (await row.locator('.session-select').boundingBox())!, cross = (await remove.boundingBox())!;
    if (Math.abs(cross.y - entry.y - 2) > 1 || Math.abs(entry.x + entry.width - cross.x - cross.width - 2) > 1) {
      throw new Error('Delete control is not in the top-right corner of the entry');
    }
    if (await remove.locator('use[href="#i-close"]').count() !== 1) throw new Error('Delete control should use the plain close icon');
    await remove.press('Enter');
    const confirm = page.getByRole('dialog', {name:'Delete conversation?'});
    await confirm.getByText(child.title, {exact:true}).waitFor();
    await confirm.getByRole('button', {name:'Cancel'}).press('Enter');
    await confirm.waitFor({state:'hidden'});
    if (deleted) throw new Error('Cancelled deletion still ran');
    await remove.click(); await confirm.getByRole('button', {name:'Delete', exact:true}).click();
    await row.waitFor({state:'detached'});
    await page.locator('#title').filter({hasText:sessions[0].title}).waitFor();
  });
  await run('agent attachments: GIF preview and authenticated download', {width:1024,height:768}, 'light', async page => {
    const aspectFixture = '```svg\n<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="2400"><rect width="1200" height="2400"/></svg>\n```';
    await page.route('**/api/sessions/s1', route => route.fulfill({json:{session:sessions[0], turns:[{...turns[0], answer:aspectFixture, files:[
      {id:'test-gif', name:'demo.gif', mime:'image/gif', size:42}, {id:'test-report', name:'report.txt', mime:'application/octet-stream', size:6},
      {id:'large-gif', name:'large.gif', mime:'image/gif', size:2*1024*1024},
    ]}], events:[], approvals:[]}}));
    await page.goto(url + '#token=' + token);
    await page.waitForFunction(() => document.querySelector<HTMLImageElement>('.shared-file img[alt="demo.gif"]')?.naturalWidth === 1);
    if (await page.locator('.shared-file img[alt="large.gif"]').count()) throw new Error('Large shared image rendered inline');
    await page.setViewportSize({width:390,height:360});
    await page.waitForFunction(() => {
      const img = document.querySelector<HTMLImageElement>('.answer .inline-svg');
      if (!img?.naturalWidth) return false;
      const {width,height} = img.getBoundingClientRect();
      return width > 0 && width <= img.parentElement!.clientWidth && height <= innerHeight * .6 + 1 && Math.abs(height / width - 2) < .02;
    });
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button',{name:'Download report.txt'}).click()]);
    if (download.suggestedFilename() !== 'report.txt' || await download.failure()) throw new Error('Shared file download failed');
  });
  await run('timeline follows latest without interrupting scrollback', {width:1024,height:660}, 'light', async page => {
    const history: any[] = Array.from({length:35}, (_, i) => ({id:'scroll-' + i, status:'done', prompt:'Question ' + i, answer:'An earlier answer.'}));
    const live = (id: string, seq: number) => ({seq,type:'turn.updated',data:{id,status:'done',prompt:'New message',answer:'New answer'}});
    let emit!: (event: unknown) => void;
    await page.route('**/api/sessions/s1', route => route.fulfill({json:{session:sessions[0],turns:history,events:[],approvals:[]}}));
    // Use the fixture stream to exercise the same live render path as production.
    timelineListener = listener => { emit = listener; };
    try {
      await page.goto(url + '#token=' + token);
      await page.locator('#connection[data-state=live]').waitFor();
      const atEnd = () => page.waitForFunction(() => {
        const el = document.getElementById('transcript')!;
        return el.scrollTop > 0 && el.scrollHeight - el.scrollTop - el.clientHeight <= 4;
      });
      await atEnd();
      emit(live('live-1', 100));
      await page.locator('#message-live-1').waitFor({state:'attached'}); await atEnd();
      const transcript = page.locator('#transcript');
      await transcript.evaluate(el => { el.scrollTop = 500; });
      await page.locator('#jump').waitFor();
      history.push({id:'live-1',status:'done',prompt:'New message',answer:'New answer'});
      history.push({id:'live-2',status:'done',prompt:'Build passed',answer:'',kind:'chat_reply',sender_id:'s2',sender_handle:'build'});
      emit({seq:101, type:'app.chat_reply', turn_id:'live-2', data:{}});
      await page.locator('#message-live-2').getByText('Reply from @build', {exact:true}).waitFor({state:'attached'});
      if (await transcript.evaluate(el => Math.abs(el.scrollTop - 500) > 2)) throw new Error('Live update interrupted scrollback');
      await page.locator('#jump').click(); await atEnd();
      await page.setViewportSize({width:1024,height:580}); await atEnd();
      await transcript.evaluate(el => { el.scrollTop = 0; });
      await page.locator('#jump').waitFor();
      await page.locator('#sessions .session-select').nth(1).click();
      await page.locator('#connection[data-state=live]').waitFor();
      await page.locator('#sessions .session-select').first().click();
      await page.locator('#message-scroll-34').waitFor({state:'attached'}); await atEnd();
    } finally { timelineListener = null; }
  });
  await run('late submission response cannot roll back a settled turn', {width:1024,height:660}, 'light', async page => {
    const queued = {id:'fast-turn',status:'queued',prompt:'Test the response race'};
    const settled = {...queued,status:'done',answer:'Already finished'};
    let release!: () => void, submitted = false;
    const held = new Promise<void>(resolve => { release = resolve; });
    await page.route('**/api/sessions/s1/turns', async route => {
      submitted = true;
      emitChatUpdate({seq:9001, turn_id:queued.id, type:'app.turn_settled', data:{}});
      await held;
      await route.fulfill({json:queued});
    });
    await page.route('**/api/sessions/s1', route => route.fulfill({json:{session:sessions[0],turns:submitted ? [settled] : [],events:[],approvals:[]}}));
    await page.goto(url + '#token=' + token);
    await page.locator('#connection[data-state=live]').waitFor();
    try {
      await page.locator('#prompt').fill(queued.prompt);
      await page.locator('#send').click();
      await page.locator('#message-fast-turn .answer').getByText(settled.answer).waitFor();
    } finally { release(); }
    await page.waitForFunction(() => (document.getElementById('prompt') as HTMLTextAreaElement).value === '' && !document.querySelector('.session-select[disabled]'));
    if ((await page.locator('#message-fast-turn .answer').textContent())?.trim() !== settled.answer) throw new Error('Late response overwrote settled answer');
  });
  await run('link previews: opt-in settings and cached thumbnail', {width:1280,height:860}, 'light', async page => {
    await page.goto(url + '#token=' + token);
    const newTab = '[target="_blank"][rel="noopener noreferrer"]';
    await page.locator(`.answer a[href="https://example.com/"]${newTab}`).waitFor();
    await page.locator(`.answer blockquote a[href="https://example.org/article"]${newTab}`).waitFor();
    await page.locator('.message-ref:not([target])').first().waitFor();
    if (calls.includes('GET /api/link-preview')) throw new Error('Disabled previews requested metadata');
    await page.locator('#open-settings').click();
    await page.locator('#preview-enabled').check();
    await page.keyboard.press('Escape');
    await page.locator('#message-t3 summary').filter({hasText:'Thinking through'}).click();
    await page.locator(`.reasoning-content a[href="https://reasoning.example/"]${newTab}`).waitFor();
    await page.locator(`.link-preview a[href="https://example.com/"]${newTab}`).getByText('Example preview').waitFor();
    await page.locator(`.link-preview a[href="https://example.org/article"]${newTab}`).getByText('Article preview').waitFor();
    await page.waitForFunction(() => {
      const images = [...document.querySelectorAll<HTMLImageElement>('.answer .link-preview img')];
      return images.length === 2 && images.every(img => img.naturalWidth > 0);
    });
    if (await page.locator('.answer > .link-preview:last-child').count() !== 1 ||
        await page.locator('.link-preview').count() !== 2 || calls.filter(call => call === 'GET /api/link-preview').length !== 2) {
      throw new Error('Expected only the two distinct timeline links to have appended previews');
    }
    await page.locator('#open-settings').click(); await page.locator('#preview-enabled').uncheck();
    await page.keyboard.press('Escape'); await page.waitForFunction(() => !document.querySelector('.link-preview'));
  });
  }
  console.log(JSON.stringify({checks, evidence: root}, null, 2));
} finally {
  await browser.close(); server.stop(true);
}
