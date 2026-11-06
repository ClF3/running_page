// Local production preview with shared bandwidth, latency and optional failures.
// Usage: node tests/frontend/serve-network.mjs [dist directory]
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { gzipSync } from 'node:zlib';

const root = resolve(process.argv[2] || 'dist');
const port = Number(process.env.PORT || 42731);
const latency = Number(process.env.LATENCY_MS || 150);
const bytesPerSecond = Number(process.env.KIB_PER_SECOND || 128) * 1024;
const failPattern = process.env.FAIL_PATTERN;
const failAlways = process.env.FAIL_ALWAYS === '1';
const failCount = Number(process.env.FAIL_COUNT || 1);
const failed = new Map();
const pending = new Set();
const types = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
};

// All simultaneous local responses share one bandwidth budget.
setInterval(() => {
  const allowance = Math.max(1, Math.floor(bytesPerSecond / 50 / pending.size));
  for (const item of pending) {
    if (item.response.destroyed) {
      pending.delete(item);
      continue;
    }
    if (item.response.writableNeedDrain) continue;
    const end = Math.min(item.offset + allowance, item.body.length);
    item.response.write(item.body.subarray(item.offset, end));
    item.offset = end;
    if (end === item.body.length) {
      item.response.end();
      pending.delete(item);
    }
  }
}, 20).unref();

createServer(async (request, response) => {
  const started = Date.now();
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const file = resolve(root, '.' + decodeURIComponent(pathname));
  if (file !== root && !file.startsWith(root + sep)) {
    response.writeHead(403).end();
    return;
  }
  let body;
  let status = 200;
  let type = types[extname(file)] || 'text/html';
  try {
    body = await readFile(extname(file) ? file : resolve(root, 'index.html'));
    if (
      failPattern &&
      pathname.includes(failPattern) &&
      (failAlways || (failed.get(pathname) || 0) < failCount)
    ) {
      failed.set(pathname, (failed.get(pathname) || 0) + 1);
      status = 503;
      type = 'text/plain';
      body = Buffer.from('Simulated temporary outage');
    }
  } catch {
    response.writeHead(404).end();
    return;
  }
  const compressed =
    /text\/|json|svg/.test(type) &&
    request.headers['accept-encoding']?.includes('gzip');
  if (compressed) body = gzipSync(body);
  response.on('finish', () => {
    console.log(
      JSON.stringify({
        started,
        ms: Date.now() - started,
        status,
        bytes: body.length,
        pathname,
      })
    );
  });
  setTimeout(() => {
    if (response.destroyed) return;
    response.writeHead(status, {
      'Content-Type': type,
      'Content-Length': body.length,
      'Cache-Control': 'no-store',
      ...(compressed ? { 'Content-Encoding': 'gzip' } : {}),
    });
    pending.add({ response, body, offset: 0 });
  }, latency);
}).listen(port, '127.0.0.1', () => {
  console.log(
    `Preview: http://127.0.0.1:${port} (${latency} ms, ${bytesPerSecond / 1024} KiB/s shared)`
  );
});
