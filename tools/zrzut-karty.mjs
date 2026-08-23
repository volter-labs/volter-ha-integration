import fs from 'node:fs';
const BAZA = 'http://127.0.0.1:9333';
const HA = 'http://100.88.64.93:8123';
const dane = fs.readFileSync('C:/tmp/dane-karty.json', 'utf8');
const skrypt = fs.readFileSync('C:/tmp/render.js', 'utf8');

async function cel() {
  for (let i = 0; i < 40; i += 1) {
    try { const l = await (await fetch(`${BAZA}/json/list`)).json();
      const t = l.find((x) => x.type === 'page'); if (t) return t; } catch {}
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error('brak celu');
}
const t = await cel();
const ws = new WebSocket(t.webSocketDebuggerUrl);
let id = 0; const oczek = new Map(); const bledy = [];
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && oczek.has(m.id)) { oczek.get(m.id)(m); oczek.delete(m.id); return; }
  if (m.method === 'Runtime.exceptionThrown') bledy.push('WYJATEK ' +
    (m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text));
});
const w = (method, params = {}) => new Promise((res) => {
  const nr = ++id; oczek.set(nr, res); ws.send(JSON.stringify({ id: nr, method, params }));
});
await new Promise((r) => ws.addEventListener('open', r));
await w('Runtime.enable'); await w('Page.enable');
await w('Emulation.setDeviceMetricsOverride',
  { width: 1280, height: 1400, deviceScaleFactor: 2, mobile: false });
await w('Page.navigate', { url: HA + '/' });
await new Promise((r) => setTimeout(r, 3500));
await w('Runtime.evaluate', { expression: 'window.__dane = ' + dane + '; "ok"', returnByValue: true });
const r = await w('Runtime.evaluate', { expression: skrypt, awaitPromise: true, returnByValue: true });
console.log('render:', r.result?.result?.value ?? JSON.stringify(r.result?.exceptionDetails ?? r));
await new Promise((rr) => setTimeout(rr, 900));
const wym = await w('Runtime.evaluate', {
  expression: 'JSON.stringify({h: document.body.scrollHeight})', returnByValue: true });
const h = JSON.parse(wym.result.result.value).h;
await w('Emulation.setDeviceMetricsOverride',
  { width: 1280, height: h + 20, deviceScaleFactor: 2, mobile: false });
await new Promise((rr) => setTimeout(rr, 400));
const z = await w('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync('C:/tmp/karta.png', Buffer.from(z.result.data, 'base64'));
console.log('zrzut zapisany, wysokosc', h);
if (bledy.length) bledy.forEach((l) => console.log(' ', l));
ws.close(); process.exit(0);
