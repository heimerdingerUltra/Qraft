import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';
const browser = await chromium.launch({
  channel: 'chrome',
  args: ['--autoplay-policy=no-user-gesture-required'],
});
try {
  const page = await browser.newPage();
  page.on('pageerror', (error) => console.error(error.message));
  await page.goto('http://127.0.0.1:4317');
  const cloud = process.argv.includes('--cloud');
  for (const scenario of ['simple', 'decision']) {
    await page.selectOption('#scenario', scenario);
    await page.click(cloud ? '#cloud' : '#demo', { timeout: 6000 });
    await page.waitForFunction(
      () => document.querySelector('#status').textContent.includes('terminée'),
      null,
      { timeout: 50000 },
    );
    const answer = await page.locator('#answer').textContent();
    console.log(await page.locator('#history').textContent());
    assert.match(answer, scenario === 'simple' ? /100.000/ : /10.000/);
    console.log(scenario, answer, await page.locator('#latency').textContent());
  }
  await page.locator('summary').click();
  const download = page.waitForEvent('download');
  await page.click('#export');
  assert.equal((await download).suggestedFilename(), 'compte-rendu-reunion.md');
} finally {
  await browser.close();
}
