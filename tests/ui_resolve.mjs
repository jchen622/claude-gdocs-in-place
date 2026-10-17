// Resolve a comment through the Google Docs UI and verify in-session that it worked.
//   node ui_resolve.mjs <fileId> "<text fragment>"
// The Drive API cannot resolve a comment on a stored .docx (its comment layer is empty), so
// the UI is the only way to observe what Docs itself writes back into the file.
import { chromium } from '/Users/chenj345/.claude/skills/playwright-fetch/scripts/node_modules/playwright/index.mjs';

const fileId = process.argv[2];
const frag = process.argv[3];

const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
const page = await browser.contexts()[0].newPage();
await page.goto(`https://docs.google.com/document/d/${fileId}/edit`, {
  waitUntil: 'domcontentloaded', timeout: 60000,
});
await page.waitForTimeout(9000);
console.log('title:', await page.title());

const cards = () => page.evaluate(() =>
  [...document.querySelectorAll('.docos-anchoreddocoview, .docos-docoview-tesla-conflict')]
    .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean));

console.log('open cards before:', (await cards()).length);

// Click the card first so its action buttons attach, then fire the resolve control
// that belongs to that card.
const ok = await page.evaluate((frag) => {
  const card = [...document.querySelectorAll('.docos-anchoreddocoview, .docos-docoview-tesla-conflict')]
    .find(e => (e.innerText || '').includes(frag));
  if (!card) return 'no card';
  for (const t of ['mousedown', 'mouseup', 'click']) {
    card.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
  }
  const btn = [...card.querySelectorAll('[role="button"]')]
    .find(e => /mark as resolved|^resolve$/i.test(e.getAttribute('aria-label') || ''));
  if (!btn) return 'card found, no resolve button inside it';
  for (const t of ['mousedown', 'mouseup', 'click']) {
    btn.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
  }
  return 'clicked';
}, frag);
console.log('resolve attempt:', ok);

await page.waitForTimeout(8000);
const after = await cards();
console.log('open cards after :', after.length);
const gone = !after.some(t => t.includes(frag));
console.log(gone ? 'the target card is gone from the open pane (resolve took effect)'
                 : 'the target card is STILL in the open pane (resolve did NOT take effect)');

await page.waitForTimeout(6000);   // let the autosave land
await page.close();
await browser.close();
process.exit(gone ? 0 : 4);
