// Open the "Show all comments" history panel in Google Docs and report what it contains.
//   node ui_history.mjs <fileId>
// This is the measurement the Drive API cannot make: what a reviewer actually sees after a
// comment is resolved.
import { chromium } from '/Users/chenj345/.claude/skills/playwright-fetch/scripts/node_modules/playwright/index.mjs';

const fileId = process.argv[2];
const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
const page = await browser.contexts()[0].newPage();
await page.goto(`https://docs.google.com/document/d/${fileId}/edit`, {
  waitUntil: 'domcontentloaded', timeout: 60000,
});
await page.waitForTimeout(9000);
console.log('title:', await page.title());

const before = await page.evaluate(() =>
  [...document.querySelectorAll('[role="article"]')]
    .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean));
console.log(`open cards before: ${new Set(before).size} unique`);
[...new Set(before)].forEach(t => console.log('   -', t.slice(0, 110)));

await page.locator('#docs-docos-commentsbutton').first().click();
await page.waitForTimeout(7000);

const dump = await page.evaluate(() => {
  const seen = new Set();
  // the history panel renders as a dialog; grab every sizeable text block inside it
  const roots = [...document.querySelectorAll('[role="dialog"], .docs-docos-show-all-dialog, #docos-stream-view, .docos-stream-view')];
  const out = [];
  for (const r of roots) {
    const t = (r.innerText || '').trim();
    if (t.length > 20 && !seen.has(t)) { seen.add(t); out.push(t.slice(0, 2200)); }
  }
  return out;
});
console.log('\n===== "Show all comments" panel =====');
if (!dump.length) console.log('(nothing captured)');
dump.forEach(t => console.log(t));

await page.close();
await browser.close();
