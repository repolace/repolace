// Read-only capture of the local app UI. Every non-GET request is blocked before it leaves the browser.
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const [out, task, repo] = process.argv.slice(2);
const prof = mkdtempSync(join(tmpdir(), 'capui-'));
const c = spawn('chromium', ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${prof}`, '--hide-scrollbars', '--force-color-profile=srgb', '--window-size=1440,900', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
process.on('exit', () => c.kill('SIGKILL'));
const url = await new Promise((ok) => { let b = ''; c.stderr.on('data', (d) => { b += d; const m = b.match(/DevTools listening on (ws:\/\/\S+)/); if (m) ok(m[1]); }); });
const ws = new WebSocket(url); await new Promise((r) => (ws.onopen = r));
let id = 0; const p = new Map(); let s; const blocked = [];
const send = (method, params = {}, sessionId) => new Promise((r) => { p.set(++id, r); ws.send(JSON.stringify({ id, method, params, sessionId })); });
ws.onmessage = (e) => { const m = JSON.parse(e.data);
  if (m.method === 'Fetch.requestPaused') { const q = m.params.request;
    if (q.method !== 'GET') { blocked.push(q.method + ' ' + q.url); send('Fetch.failRequest', { requestId: m.params.requestId, errorReason: 'BlockedByClient' }, s); }
    else send('Fetch.continueRequest', { requestId: m.params.requestId }, s); return; }
  const q = p.get(m.id); if (q) { p.delete(m.id); q(m.result ?? m.error); } };
const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
s = (await send('Target.attachToTarget', { targetId, flatten: true })).sessionId;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await send('Fetch.enable', { patterns: [{ urlPattern: '*' }] }, s);
await send('Page.enable', {}, s); await send('Runtime.enable', {}, s);
await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] }, s);
const ev = async (expression) => (await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, s)).result?.value;
const W = 1440;
async function shot(name) {
  const h = await ev('Math.max(document.documentElement.scrollHeight, 900)');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: h, deviceScaleFactor: 2, mobile: false }, s); await sleep(400);
  const { data } = await send('Page.captureScreenshot', { format: 'png' }, s); writeFileSync(join(out, name + '.png'), Buffer.from(data, 'base64'));
  const map = await ev(`JSON.stringify([...document.querySelectorAll('h1,h2,h3,section,table,form,button,[id],.badge,.chip')].map(e=>{const r=e.getBoundingClientRect();return [e.tagName.toLowerCase()+(e.id?'#'+e.id:'')+(e.className&&typeof e.className==='string'?'.'+e.className.trim().split(/\\s+/).join('.'):''),Math.round(r.x),Math.round(r.y+scrollY),Math.round(r.width),Math.round(r.height),(e.children.length?'':e.textContent.trim().slice(0,50))]}).filter(r=>r[3]>0&&r[4]>0))`);
  writeFileSync(join(out, name + '.json'), map); console.log(name, h);
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: 900, deviceScaleFactor: 2, mobile: false }, s);
}
const go = async (path) => { await send('Emulation.setDeviceMetricsOverride', { width: W, height: 900, deviceScaleFactor: 2, mobile: false }, s); await send('Page.navigate', { url: 'http://127.0.0.1:8000' + path }, s); await sleep(3500); };
await go(`/ui/tasks/${task}`); await shot('task');
await go(`/ui/repos/${repo}/tasks`); await shot('list');
await go(`/ui/repos/${repo}/tasks/new`); await shot('new');
// Select the issue (a GET for the confirm fragment). The Start button is never clicked, and POSTs are blocked anyway.
await ev(`(()=>{const r=document.querySelector('input[name=issue_number]'); r.click(); r.dispatchEvent(new Event('change',{bubbles:true}));})()`); await sleep(3000);
await shot('new-picked');

console.log('blocked:', blocked);
process.exit(0);
