/** Real browser + real Copilot smoke test. Explicitly opt-in; uses model allowance. */
import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const base = process.env.MS_DURABLE_TEST_TMP;
if (!base) throw new Error('Run via make test-browser (project-scoped temporary paths required).');
const root = resolve(base, 'runs', 'browser-' + Date.now());
await mkdir(root + '/workspace', {recursive:true});
await Bun.write(root + '/workspace/AGENTS.md', 'This is an isolated integration workspace. Do not access any other directory.\n');
const token = 'synthetic-local-browser-test-token';
const portProbe = Bun.serve({port:0, hostname:'127.0.0.1', fetch:()=>new Response('probe')});
const port = portProbe.port;
portProbe.stop(true);
const url = `http://127.0.0.1:${port}`;
const server = Bun.spawn([process.env.MS_DURABLE_TEST_PYTHON!, '-m', 'agent'], {
  env: {...process.env, MS_DURABLE_HOST:'127.0.0.1', MS_DURABLE_PORT:String(port), MS_DURABLE_DATA:root+'/data', MS_DURABLE_CWD:root+'/workspace', MS_DURABLE_TOKEN:token, MS_DURABLE_MCP_FILE:'', MS_DURABLE_SKILLS:''},
  stdout: Bun.file(root+'/server.stdout.log'), stderr:Bun.file(root+'/server.stderr.log'),
});
const checks:string[] = [];
const headers = {Authorization:'Bearer '+token, 'Content-Type':'application/json'};
async function api(path:string, body?:unknown) {
  const response = await fetch(url+path, {headers, method:body===undefined?'GET':'POST', body:body===undefined?undefined:JSON.stringify(body)});
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json() as Promise<any>;
}
async function until(fn:()=>Promise<boolean>, description:string, timeout=90000) {
  const end=Date.now()+timeout;
  while (Date.now()<end) { if (await fn()) return; await Bun.sleep(100); }
  throw new Error('Timed out: '+description);
}
let browser;
try {
  await until(async()=>{try{return (await api('/api/health')).status==='ok';}catch{return false;}},'server ready',30000);
  const config=await api('/api/config');
  const model=process.env.MS_DURABLE_MODEL || config.models.find((m:any)=>m.id==='claude-haiku-5.5')?.id || config.models.find((m:any)=>/mini|haiku/.test(m.id))?.id;
  if (!model) throw new Error('No test model is available.');
  browser=await chromium.launch({headless:true});
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage();
  page.setDefaultTimeout(90000);
  const errors:string[]=[];
  page.on('pageerror', e=>errors.push(e.message));
  await page.goto(url);
  await page.locator('#token').fill(token);
  await page.locator('#auth button').click();
  await page.locator('#model').selectOption(model);
  await page.locator('#prompt').fill('Use the shell tool to run exactly this command in the current integration workspace: printf BROWSER_APPROVAL_OK > browser-approval.txt. This writes only a disposable test file. Do not use another tool. After approval and successful execution, answer exactly BROWSER_APPROVAL_OK.');
  await page.locator('#send').click();
  await page.getByRole('button',{name:'Approve once',exact:true}).waitFor();
  const sessions=await api('/api/sessions');
  const sid=sessions[0].id;
  const snapshot=await api(`/api/sessions/${sid}`);
  if(snapshot.approvals.length!==1) throw new Error('Expected one native permission request.');
  const request=JSON.parse(snapshot.approvals[0].request);
  if(!['shell','write'].includes(request.kind) || !JSON.stringify(request).includes('browser-approval.txt')) throw new Error('Unexpected permission request; refusing approval.');
  if(await Bun.file(root+'/workspace/browser-approval.txt').exists()) throw new Error('Write occurred before approval.');
  await page.getByRole('button',{name:'Approve once',exact:true}).click();
  await until(async()=>{const s=await api(`/api/sessions/${sid}`);return s.turns[0].status==='done';},'approved native turn');
  await page.locator('.answer').filter({hasText:'BROWSER_APPROVAL_OK'}).waitFor();
  if((await Bun.file(root+'/workspace/browser-approval.txt').text()).trim()!=='BROWSER_APPROVAL_OK') throw new Error('Approved write did not occur.');
  checks.push('native write permission through browser; no write before approval');
  await context.setOffline(true);
  const turn=await api(`/api/sessions/${sid}/turns`,{prompt:'Reply exactly SSE_RECONNECT_OK without tools.',request_id:'browser-offline'});
  await until(async()=>{const s=await api(`/api/sessions/${sid}`);return s.turns.some((t:any)=>t.id===turn.id && t.status==='done');},'offline generation');
  await context.setOffline(false);
  await page.locator('.answer').filter({hasText:'SSE_RECONNECT_OK'}).waitFor();
  if(await page.locator('.turn').count()!==2) throw new Error('SSE reconnect duplicated or lost a turn.');
  checks.push('SSE reconnect reconciles missed final events without duplicates');
  await page.reload();
  await page.locator('#sessions button').first().click();
  await page.locator('.answer').filter({hasText:'SSE_RECONNECT_OK'}).waitFor();
  checks.push('reload restores authenticated transcript');
  await page.locator('#context-button').click();
  await page.locator('#compact').click();
  await until(async()=>{const s=await api(`/api/sessions/${sid}`);return s.turns.some((t:any)=>t.kind==='compact' && t.status==='done' && JSON.parse(t.answer).success);},'native compaction');
  checks.push('browser queues native compaction');
  await page.locator('#context-button').click();
  await until(async()=> (await page.locator('#context-info').textContent())!.includes('selectedModel'),'native metadata');
  checks.push('native context metadata');
  if(errors.length) throw new Error('Browser JavaScript errors: '+errors.join('; '));
  await page.screenshot({path:root+'/browser.png',fullPage:true});
  console.log(JSON.stringify({model,checks,evidence:root},null,2));
} finally {
  await browser?.close();
  server.kill('SIGTERM');
  await server.exited;
}
