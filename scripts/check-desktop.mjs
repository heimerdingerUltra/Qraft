import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { once } from 'node:events';
import { createServer } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { chromium } from 'playwright-core';

const probe = createServer().listen(0, '127.0.0.1');
await once(probe, 'listening');
const port = probe.address().port;
await new Promise((resolve) => probe.close(resolve));
const profile = await mkdtemp(join(tmpdir(), 'reunion-webview-'));
const executable = fileURLToPath(
  new URL('../src-tauri/target/release/assistant-reunion.exe', import.meta.url),
);
const app = spawn(executable, [], {
  windowsHide: true,
  env: {
    ...process.env,
    WEBVIEW2_USER_DATA_FOLDER: profile,
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:
      '--remote-debugging-port=' +
      port +
      ' --autoplay-policy=no-user-gesture-required',
  },
});
const exited = once(app, 'exit');
let browser, origin;
try {
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline && app.exitCode === null) {
    try {
      const res = await fetch('http://127.0.0.1:' + port + '/json/version');
      if (res.ok) {
        browser = await chromium.connectOverCDP(
          (await res.json()).webSocketDebuggerUrl,
        );
        break;
      }
    } catch {}
    await delay(250);
  }
  assert.ok(browser, 'WebView2 doit démarrer');
  const context = browser.contexts()[0];
  const page = context.pages()[0] || (await context.waitForEvent('page'));
  await page.locator('#demo').waitFor();
  origin = new URL(page.url()).origin;
  assert.equal(await page.evaluate(() => window.meetingDesktop), true);
  assert.equal(await page.locator('#live').isVisible(), false);
  await page.click('#demo');
  await page.waitForFunction(
    () => document.querySelector('#status').textContent.includes('terminée'),
    null,
    { timeout: 20000 },
  );
  assert.match(await page.locator('#answer').innerText(), /100.000/);
  console.log('Fenêtre Windows et démo audio vérifiées sur', origin);
} finally {
  if (app.exitCode === null) {
    const close = spawn(
      'powershell.exe',
      [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        '(Get-Process -Id ' + app.pid + ').CloseMainWindow()',
      ],
      { windowsHide: true, stdio: 'ignore' },
    );
    await once(close, 'exit');
    const timer = setTimeout(() => app.kill(), 5000);
    try {
      await exited;
    } finally {
      clearTimeout(timer);
    }
  }
  await browser?.close().catch(() => {});
  if (origin) {
    let alive = true;
    for (let i = 0; i < 25 && alive; i++) {
      await delay(100);
      alive = await fetch(origin + '/status').then(
        () => true,
        () => false,
      );
    }
    assert.equal(alive, false, 'La fermeture doit arrêter le serveur privé');
    console.log('Serveur privé arrêté avec la fenêtre.');
  }
  await rm(profile, {
    recursive: true,
    force: true,
    maxRetries: 10,
    retryDelay: 150,
  }).catch(() => {});
}
