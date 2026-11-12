import { embedTexts } from './embeddings.mjs';
import { teamsPid, captureProcess } from './desktop.mjs';
import { synthesize } from './synthesis.mjs';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { WebSocket, WebSocketServer } from 'ws';
import { extract } from './documents.mjs';

let port = Number(process.env.PORT || 4317);
const host = '127.0.0.1';
let origin = 'http://' + host + ':' + port;
const files = new Map(
  Object.entries({
    '/': 'index.html',
    '/app.js': 'app.js',
    '/engine.mjs': 'engine.mjs',
    '/style.css': 'style.css',
    '/question.wav': 'question.wav',
    '/decision.wav': 'decision.wav',
    '/pcm.js': 'pcm.js',
  }),
);
const types = {
  html: 'text/html; charset=utf-8',
  js: 'text/javascript',
  mjs: 'text/javascript',
  css: 'text/css',
  wav: 'audio/wav',
};
const server = http.createServer(async (req, res) => {
  try {
    if (req.headers.host !== host + ':' + port) return res.writeHead(403).end();
    if (
      req.url === '/import' &&
      req.method === 'POST' &&
      req.headers.origin === origin
    ) {
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 20 * 1024 * 1024)
          return res.writeHead(413).end('Limite de 20 Mo.');
        chunks.push(chunk);
      }
      const pages = await extract(
        Buffer.concat(chunks),
        req.headers['x-format'],
      );
      return res
        .writeHead(200, { 'Content-Type': 'application/json' })
        .end(JSON.stringify(pages));
    }
    if (
      ['/synthesis', '/embed'].includes(req.url) &&
      req.method === 'POST' &&
      req.headers.origin === origin
    ) {
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 64000) return res.writeHead(413).end();
        chunks.push(chunk);
      }
      const controller = new AbortController();
      res.on('close', () => controller.abort());
      try {
        const payload = JSON.parse(Buffer.concat(chunks));
        const signal = AbortSignal.any([
          controller.signal,
          AbortSignal.timeout(120000),
        ]);
        const answer =
          req.url === '/embed'
            ? await embedTexts(payload.texts, signal)
            : await synthesize(payload, signal);
        return res
          .writeHead(200, { 'Content-Type': 'application/json' })
          .end(JSON.stringify(answer));
      } catch {
        return res
          .writeHead(503)
          .end('Synthèse locale indisponible ou sans référence vérifiable.');
      }
    }
    if (req.url === '/status')
      return res.end(
        JSON.stringify({ configured: Boolean(process.env.DEEPGRAM_API_KEY) }),
      );
    const file = files.get(req.url);
    if (!file) return res.writeHead(404).end();
    res.setHeader('Content-Type', types[file.split('.').pop()]);
    res.end(await readFile(new URL('./public/' + file, import.meta.url)));
  } catch {
    res.writeHead(400).end('Document illisible ou format incompatible.');
  }
});
const proxy = new WebSocketServer({ noServer: true, maxPayload: 512 * 1024 });
server.on('upgrade', (req, socket, head) => {
  const address = new URL(req.url, origin);
  const desktop = address.pathname === '/teams';
  if (
    (!desktop && address.pathname !== '/listen') ||
    req.headers.origin !== origin ||
    req.headers.host !== host + ':' + port ||
    !process.env.DEEPGRAM_API_KEY
  )
    return socket.destroy();
  proxy.handleUpgrade(req, socket, head, async (client) => {
    let remote, stop;
    client.on('error', () => client.terminate());
    client.on('close', () => {
      stop?.();
      remote?.terminate();
    });
    try {
      const pid = desktop ? await teamsPid() : null;
      if (client.readyState !== 1) return;
      const params = new URLSearchParams({
        model: 'nova-3',
        language: 'fr',
        interim_results: 'true',
        endpointing: '120',
        punctuate: 'true',
        smart_format: 'true',
      });
      for (const [k, v] of Object.entries({
        encoding: 'linear16',
        sample_rate: '48000',
        channels: desktop ? '2' : '1',
      }))
        params.set(k, v);
      const name = address.searchParams.get('name')?.trim().slice(0, 80);
      if (name) params.set('keyterm', name);
      remote = new WebSocket('wss://api.deepgram.com/v1/listen?' + params, {
        headers: { Authorization: 'Token ' + process.env.DEEPGRAM_API_KEY },
        handshakeTimeout: 8000,
      });
      remote.on('open', async () => {
        try {
          if (desktop) {
            let lastLevel = 0;
            stop = await captureProcess(pid, (chunk) => {
              if (client.readyState === 1 && Date.now() - lastLevel > 100) {
                let energy = 0,
                  samples = 0;
                for (let i = 0; i + 1 < chunk.length; i += 16) {
                  energy += (chunk.readInt16LE(i) / 32768) ** 2;
                  samples++;
                }
                const value = Math.min(
                  1,
                  2 * Math.pow(energy / Math.max(1, samples), 0.25),
                );
                client.send(JSON.stringify({ type: 'level', value }));
                lastLevel = Date.now();
              }
              if (remote.readyState === 1) remote.send(chunk);
            });
            if (client.readyState !== 1) {
              stop();
              stop = null;
              return;
            }
          }
          if (client.readyState === 1)
            client.send(JSON.stringify({ type: 'ready' }));
        } catch {
          client.close(1011, 'Capture Teams indisponible sur ce poste.');
        }
      });
      remote.on('message', (data) => {
        if (client.readyState === 1) client.send(data.toString());
      });
      client.on('message', (data, binary) => {
        if (!desktop && binary && remote.readyState === 1) remote.send(data);
      });
      remote.on('error', () =>
        client.close(1011, 'Connexion Deepgram impossible.'),
      );
      remote.on('close', () => client.close());
    } catch (err) {
      client.close(1011, String(err.message).slice(0, 90));
    }
  });
});
server.listen(port, host, () => {
  port = server.address().port;
  origin = 'http://' + host + ':' + port;
  console.log(origin);
});

// The native launcher owns the write end. Windows also closes it if the launcher crashes.
if (process.env.MEETING_DESKTOP_CHILD === '1') {
  process.stdin.on('end', () => process.exit(0));
  process.stdin.resume();
}
