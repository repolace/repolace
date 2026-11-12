#!/usr/bin/env node
// Renders scripts/promo_video/index.html to a video, frame by frame.
//
//   node scripts/promo_video/render.mjs                    full render -> out/
//   node scripts/promo_video/render.mjs --stills 1.5,5,14  PNG stills at those seconds
//   --workers N (default 6), --fps N (default 60), --width/--height (default 1920x1080), --resume, --keep-frames
//
// Needs `chromium` and `ffmpeg` on PATH (CHROMIUM=/path overrides) and Node 22+
// (global WebSocket). No npm packages: it speaks the DevTools protocol directly.
// The page is seeked, never played, so the result does not depend on machine speed.

import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const arg = (name, fallback) => { const i = process.argv.indexOf('--' + name); return i < 0 ? fallback : process.argv[i + 1]; };
const FPS = Number(arg('fps', 60));
const WORKERS = Number(arg('workers', 6));
const OUT = resolve(arg('out', join(HERE, 'out')));
const STILLS = arg('stills', null);
const W = Number(arg('width', 1920)), H = Number(arg('height', 1080));
// --page picks the film (index.html: the typographic promo; demo.html: the real-UI demo video), --name the output stem.
const PAGE = pathToFileURL(join(HERE, arg('page', 'index.html'))).href;
const NAME = arg('name', 'repolace-promo');
const POSTER_AT = Number(arg('poster-at', 41.9));

const profile = mkdtempSync(join(tmpdir(), 'promo-chromium-'));
const chromium = spawn(process.env.CHROMIUM || 'chromium', [
  '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
  '--allow-file-access-from-files', '--hide-scrollbars', '--force-device-scale-factor=1',
  '--force-color-profile=srgb', '--disable-lcd-text', `--window-size=${W},${H}`, 'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe'] });
const cleanup = () => { chromium.kill('SIGKILL'); try { rmSync(profile, { recursive: true, force: true }); } catch {} };
process.on('exit', cleanup);

const wsUrl = await new Promise((ok, fail) => {
  let buf = '';
  chromium.stderr.on('data', (d) => { buf += d; const m = buf.match(/DevTools listening on (ws:\/\/\S+)/); if (m) ok(m[1]); });
  chromium.on('exit', (c) => fail(new Error(`chromium exited (${c}):\n${buf}`)));
  setTimeout(() => fail(new Error('chromium did not start:\n' + buf)), 20000);
});

const ws = new WebSocket(wsUrl);
await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = () => fail(new Error('devtools socket failed')); });
let seq = 0; const pending = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data); const p = pending.get(m.id); if (!p) return;
  pending.delete(m.id); m.error ? p.fail(new Error(m.error.message)) : p.ok(m.result);
};
const send = (method, params = {}, sessionId) => new Promise((ok, fail) => {
  const id = ++seq; pending.set(id, { ok, fail }); ws.send(JSON.stringify({ id, method, params, sessionId }));
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function openPage() {
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  const call = (m, p) => send(m, p, sessionId);
  await call('Page.enable');
  await call('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url: PAGE });
  for (let i = 0; ; i++) {
    const r = await call('Runtime.evaluate', { expression: 'window.__ready === true' });
    if (r.result.value) break;
    if (i > 1200) throw new Error('page never became ready');  // two minutes: several workers decoding big screenshots at once is slow
    await sleep(100);
  }
  const evaluate = async (expression) => {
    const r = await call('Runtime.evaluate', { expression, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  };
  const shot = async (t, file) => {
    // A capture now and then never answers (seen about once per thousand frames). Seek again and
    // retry rather than hang the whole render; the seek also repaints, which is what unsticks it.
    for (let attempt = 1; ; attempt++) {
      await evaluate(`window.seek(${t})`);
      const shot = await Promise.race([
        call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }),
        sleep(15000).then(() => null),
      ]);
      if (shot) { writeFileSync(file, Buffer.from(shot.data, 'base64')); return; }
      if (attempt === 4) throw new Error(`no screenshot for t=${t} after ${attempt} tries`);
    }
  };
  return { evaluate, shot };
}

mkdirSync(OUT, { recursive: true });

if (STILLS) {
  const page = await openPage();
  for (const t of STILLS.split(',').map(Number)) {
    const f = join(OUT, `still_${t.toFixed(2).padStart(5, '0')}.png`);
    await page.shot(t, f); console.log(f);
  }
  process.exit(0);
}

const frames = join(OUT, `frames-${NAME}`);
// --resume keeps frames already on disk (only safe for frames index.html still renders the same).
const RESUME = process.argv.includes('--resume');
if (!RESUME) rmSync(frames, { recursive: true, force: true });
mkdirSync(frames, { recursive: true });
const pages = await Promise.all(Array.from({ length: WORKERS }, openPage));
// One throwaway capture per page, so images are decoded and painted before the first real frame.
await Promise.all(pages.map((page, n) => page.shot(0, join(frames, `warmup-${n}.png`))));
const duration = await pages[0].evaluate('window.DURATION');
const total = Math.round(duration * FPS);
let next = 0, done = 0; const started = Date.now();
await Promise.all(pages.map(async (page) => {
  for (;;) {
    const i = next++; if (i >= total) return;
    const file = join(frames, `f${String(i).padStart(5, '0')}.png`);
    if (!(RESUME && existsSync(file))) await page.shot(i / FPS, file);
    if (++done % 300 === 0) console.log(`${done}/${total} frames, ${((Date.now() - started) / 1000).toFixed(0)}s`);
  }
}));
console.log(`${total} frames in ${((Date.now() - started) / 1000).toFixed(0)}s`);
cleanup();

const ff = (args) => { const r = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', ...args], { stdio: 'inherit' }); if (r.status) process.exit(r.status); };
const input = ['-framerate', String(FPS), '-i', join(frames, 'f%05d.png')];
const colour = ['-pix_fmt', 'yuv420p', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709'];
const vf = 'scale=in_range=full:out_range=tv:out_color_matrix=bt709';
// Master (60 fps, near-lossless), web MP4 and WebM (30 fps, small), and a poster.
ff([...input, '-vf', vf, '-c:v', 'libx264', '-preset', 'slow', '-crf', '14', ...colour, '-movflags', '+faststart', join(OUT, `${NAME}-master.mp4`)]);
ff([...input, '-vf', vf, '-c:v', 'libx264', '-preset', 'slow', '-crf', '28', ...colour, '-movflags', '+faststart', join(OUT, `${NAME}.mp4`)]);
ff([...input, '-vf', vf, '-c:v', 'libvpx-vp9', '-crf', '32', '-b:v', '0', '-row-mt', '1', ...colour, join(OUT, `${NAME}.webm`)]);
ff(['-i', join(frames, `f${String(Math.round(POSTER_AT * FPS)).padStart(5, '0')}.png`), '-q:v', '3', join(OUT, `${NAME}-poster.jpg`)]);
if (!process.argv.includes('--keep-frames')) rmSync(frames, { recursive: true, force: true });
console.log('wrote', OUT);
process.exit(0);
