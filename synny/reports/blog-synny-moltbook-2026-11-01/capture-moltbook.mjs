import { chromium } from 'file:///C:/Users/Kieran/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';

const output = new URL('./moltbook-first-post.png', import.meta.url).pathname.replace(/^\/(.:)/, '$1');
const browser = await chromium.launch({
  executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
});

try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
    deviceScaleFactor: 1,
    colorScheme: 'light',
  });
  const page = await context.newPage();
  await page.goto('https://www.moltbook.com/post/92ef1e1b-7821-42b6-8902-a9c0bdb6bc62', {
    waitUntil: 'networkidle',
    timeout: 60000,
  });
  const heading = page.getByRole('heading', { name: 'My owner decided memory ought to look both ways', exact: true });
  const parentChain = await heading.evaluate((node) => {
    const chain = [];
    for (let current = node; current && chain.length < 8; current = current.parentElement) {
      chain.push({ tag: current.tagName, className: current.className });
    }
    return chain;
  });
  const postCard = heading.locator('xpath=../../..');
  await postCard.screenshot({ path: output });
  const title = await page.title();
  const dimensions = await page.evaluate(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  console.log(JSON.stringify({ title, output, dimensions, parentChain }));
  await context.close();
} finally {
  await browser.close();
}
