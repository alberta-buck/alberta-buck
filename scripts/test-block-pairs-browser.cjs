/* Local Chromium, disposable profile; no server or external chart libraries. */
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const puppeteer = require(process.env.PUPPETEER_MODULE || 'puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({executablePath:process.env.CHROME_PATH,
    headless:true, args:['--no-sandbox'], userDataDir:process.env.CHROME_USER_DATA_DIR});
  try {
    const page = await browser.newPage();
    await page.setViewport({width:1440,height:1100});
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'reduce'}]);
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    await page.goto(process.argv[2] || pathToFileURL(path.resolve('doc/control-lab/block-pairs/index.html')).href);
    await page.waitForSelector('details[data-loaded-key]');
    const manifest = await page.evaluate(() => window.controlLabManifest);
    assert.equal(manifest.status, 'complete');
    const stages = Object.keys(manifest.stages);
    const lastStage = stages.at(-1);
    assert.equal(Object.keys(manifest.entries).length, stages.length * manifest.axes[0].values.length);
    let checked = 0;
    for (const stage of Object.keys(manifest.stages)) for (const profile of manifest.axes[0].values) {
      await page.click(`#stage-nav a[href="#${stage}"]`);
      await page.select(`#${stage} select[name=profile]`, profile.id);
      const key = manifest.cases[stage][`${profile.id}:0:medium:medium`];
      await page.waitForFunction((s,k) => document.getElementById(s).dataset.loadedKey === k, {}, stage, key);
      for (let i=0; i<3; ++i) {
        assert.ok(await page.$$eval(`#${stage} [data-pane=pair-rate-${i}] svg path`, n => n.length) >= 5);
        assert.equal(await page.$$eval(`#${stage} [data-pane=pair-level-${i}] svg path`, n => n.length), 3);
      }
      if (stage === 'pair') {
        assert.equal(await page.$$eval(`#${stage} [data-pane=pair-intent] svg path`, n => n.length), 6);
        assert.equal(await page.$$eval(`#${stage} [data-pane=pair-impact] svg path`, n => n.length), 3);
      }
      assert.match(await page.$eval(`#${stage} .data-link`, e => e.href), new RegExp(key.slice(0,16)));
      checked++;
    }
    await page.$eval(`#${lastStage} .zoom input`, e => {e.value='30'; e.dispatchEvent(new Event('input'));});
    assert.equal(await page.$eval(`#${lastStage} .zoom output`, e=>e.textContent), '30–120 days');
    await page.$eval(`#${lastStage} [data-pane=pair-rate-0] svg`, e=>e.focus());
    await page.keyboard.press('Home');
    assert.equal(await page.$eval(`#${lastStage} [data-pane=pair-rate-2] .chart-day`,e=>e.textContent), 'Day 30');
    await page.setViewport({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.SCREENSHOT_DIR) {
      await page.$eval(`#${lastStage} [data-pane=pair-rate-0]`, e=>e.scrollIntoView({behavior:'instant'}));
      const lab = path.basename(path.dirname(new URL(page.url()).pathname));
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,`${lab}-mobile.png`)});
    }
    assert.deepEqual(errors, []);
    console.log(`Passed ${checked} saved-run selectors, pair panes, linked cursor, zoom and mobile layout.`);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode=1; });
