// Playwright CLI fixture; serves only the built dashboard and synthetic GET state.
async (page) => {
  const origin = 'http://127.0.0.1:4186';
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const errors = [], writes = [];
  let reads = 0, expired = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.install({ time: new Date('2026-10-09T04:00:00Z') });
  await page.clock.pauseAt(new Date('2026-10-09T04:00:01Z'));
  await page.addInitScript(() => {
    window.hiddenFixture = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.hiddenFixture });
  });
  await page.route('**/api/**', route => {
    const request = route.request();
    if (request.method() !== 'GET') writes.push(request.url());
    if (new URL(request.url()).pathname !== '/api/state') return route.fulfill({ status: 404, json: {} });
    reads++;
    return route.fulfill({ status: expired ? 401 : 200, json: expired ? {} : { demo: reads % 2 === 0, ready: true,
      accounts: [], pairs: [], markets: {}, events: [], updated_at: Date.now() / 1000, notification: { configured: false, pending: 0 } } });
  });
  const settle = async () => new Promise(resolve => setTimeout(resolve, 30));
  const expectRead = async action => {
    const previous = reads;
    const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/state');
    await action(); await (await response).finished(); await settle();
    assert(reads === previous + 1, 'one read per refresh');
    if (!expired) assert(await page.locator('.connection').innerText() === (reads % 2 === 0 ? '模拟环境' : '服务已连接'), 'latest sample renders');
  };
  await page.goto(origin); await page.getByText('服务已连接', { exact: true }).waitFor();
  await expectRead(() => page.clock.runFor(3000));
  await expectRead(() => page.evaluate(() => { window.hiddenFixture = true; document.dispatchEvent(new Event('visibilitychange')); }));
  for (let round = 0; round < 3; round++) {
    const before = reads;
    await page.clock.runFor(29_999); await settle(); assert(reads === before, 'no foreground-frequency hidden reads');
    await expectRead(() => page.clock.runFor(1));
  }
  await expectRead(() => page.evaluate(() => window.dispatchEvent(new Event('focus'))));
  await expectRead(() => page.evaluate(() => { window.hiddenFixture = false; document.dispatchEvent(new Event('visibilitychange')); }));
  await expectRead(() => page.clock.runFor(3000));
  expired = true; await expectRead(() => page.clock.runFor(3000));
  const loggedOutReads = reads;
  await page.clock.runFor(60_000); await settle(); assert(reads === loggedOutReads, '401 remains paused');
  assert(writes.length === 0, 'automatic updates only GET');
  assert(errors.length === 0, errors.join('; '));
  await page.clock.resume();
  return { passed: true, reads, hiddenRounds: 3, writes: writes.length, browserErrors: errors.length };
}
