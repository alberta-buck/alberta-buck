/* Browser regression check. Use installed puppeteer-core and Chromium:
 * PUPPETEER_MODULE=/path/to/puppeteer-core CHROME_PATH=/path/to/chrome \
 *   node scripts/test-control-lab-browser.cjs [file-or-http-URL]
 * The full campaign must have rendered; screenshots are optional via SCREENSHOT_DIR.
 */
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
    const errors = []; page.on('pageerror', e => errors.push(String(e)));
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    const url = process.argv[2] || pathToFileURL(path.resolve('doc/control-lab/interactive/index.html')).href;
    await page.goto(url); await page.waitForSelector('#feeds[data-loaded-key]');
    assert.equal(await page.$$eval('.stage', nodes=>nodes.length), 11);
    assert.equal(await page.$eval('#feeds select[name=buyer]', e=>e.disabled), true);
    assert.equal(await page.$$eval('#feeds svg path', nodes=>nodes.length), 9);
    const manifest = await page.evaluate(()=>window.controlLabManifest);
    async function choose(stage, selection) {
      await page.click(`#stage-nav a[href="#${stage}"]`);
      for (const [axis, value] of Object.entries(selection)) await page.select(`#${stage} select[name=${axis}]`, value);
      const key = manifest.cases[stage][manifest.axes.map(a=>selection[a.id] || (a.requires ? 'medium' : a.values[0].id)).join(':')];
      await page.waitForFunction((stage,key)=>document.getElementById(stage).dataset.loadedKey===key, {}, stage, key);
      return key;
    }
    // Replay calibration is a sub-basis-point effect. Local scales must be
    // able to expose it after the constant K rail guides are hidden.
    await choose('monitor-k', {profile:'three-phase',walk:'0',buyer:'medium',seller:'medium'});
    await page.click('#monitor-k .toolbar input[type=checkbox]');
    for (const rail of ['Lower rail','Upper rail']) await page.click(`#monitor-k [aria-label="Toggle ${rail}"]`);
    const smallScale = await page.$$eval('#monitor-k [data-pane=k] svg text[x="85"]', nodes=>nodes.map(e=>Number(e.textContent.replaceAll(',',''))));
    assert.equal(smallScale.length,5);
    assert.ok(smallScale[4]-smallScale[0]<.002, 'local scale hides the calibration drift');
    const firstKey = await choose('all', {profile:'three-phase',walk:'0',buyer:'medium',seller:'medium'});
    assert.equal(await page.$$eval('#all .chart', n=>n.length), 8);
    const highKey = await choose('all', {profile:'three-phase',walk:'0',buyer:'high',seller:'low'});
    assert.notEqual(highKey, firstKey);
    assert.match(await page.$eval('#all .data-link',e=>e.href), new RegExp(highKey.slice(0,16)));
    // Change two uncached selections in the same event turn: only the last may paint.
    await page.evaluate(()=>{
      const e=document.querySelector('#all select[name=walk]');
      for(const value of ['0.5','1']) {e.value=value;e.dispatchEvent(new Event('change'));}
    });
    const key=manifest.cases.all['three-phase:1:high:low'];
    await page.waitForFunction(key=>document.querySelector('#all').dataset.loadedKey===key, {}, key);
    await page.click('#all .monitor-details>summary');
    await page.waitForSelector('#all [data-pane="ema-2"]');
    assert.equal(await page.$$eval('#all .chart', n=>n.length), 12);
    const before = await page.$eval('#all [data-pane=price] path', e=>e.getAttribute('d'));
    await page.$eval('#all .zoom input', e=>{e.value='30';e.dispatchEvent(new Event('input'));});
    const after = await page.$eval('#all [data-pane=price] path', e=>e.getAttribute('d'));
    assert.notEqual(after,before);
    assert.equal(await page.$eval('#all .zoom output', e=>e.textContent), '30–120 days');
    await page.$eval('#all [data-pane=price] svg', e=>e.focus()); await page.keyboard.press('Home');
    assert.equal(await page.$eval('#all [data-pane=price] .chart-day',e=>e.textContent),'Day 30');
    assert.equal(await page.$eval('#all [data-pane=k] .chart-day',e=>e.textContent),'Day 30');
    await page.click('#all [data-pane=price] .legend button');
    assert.equal(await page.$$eval('#all [data-pane=price] svg path', n=>n.length),8);
    await page.click('#all .toolbar button');
    assert.equal(await page.$eval('#monitor select[name=walk]',e=>e.value),'1');
    await page.waitForFunction(key=>document.querySelector('#feeds').dataset.loadedKey===key,{},manifest.cases.feeds['three-phase:1:high:low']);
    if (process.env.SCREENSHOT_DIR) {
      await page.$eval('#all [data-pane=price]',e=>e.scrollIntoView());
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'control-lab-charts.png')});
    }
    // Every stage/profile can actually load its selected saved run.
    for (const profile of (process.env.TEST_PROFILES || 'three-phase,mixed,shock').split(',')) for (const stage of Object.keys(manifest.stages)) {
      await choose(stage,{profile,walk:'1',buyer:'high',seller:'low'});
      assert.equal(await page.$eval(`#${stage} .run-body`,e=>e.hidden),false);
    }
    // Desk directions come from token swaps, and marker clicks select the
    // real receipt log, including the opposite leg of triangle arbitrage.
    await choose('desk', {profile:'mixed',walk:'0',buyer:'medium',seller:'medium'});
    for (const kind of ['desk-buy','desk-sell','arb-buy','arb-sell']) {
      assert.ok(await page.$$eval(`#desk [data-pane=price] polygon[data-kind="${kind}"]`, n=>n.length)>0);
    }
    const deskPoint = '#desk [data-pane=price] polygon[data-kind="desk-buy"]';
    await page.$eval(deskPoint,e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true})));
    await page.waitForFunction(()=>document.querySelector('#desk .execution-details').open && document.querySelector('#desk .execution-body table'));
    assert.match(await page.$eval('#desk .execution-body',e=>e.textContent), /desk · (supply|issue)/);
    assert.match(await page.$eval('#desk .execution-body',e=>e.textContent), /BUY/);
    await page.click('#desk [data-pane=price] [aria-label="Toggle Arb TOKEN buys"]');
    assert.equal(await page.$$eval('#desk [data-pane=price] polygon[data-kind="arb-buy"]',n=>n.length),0);
    await choose('basket', {profile:'mixed',walk:'0',buyer:'medium',seller:'medium'});
    for (const kind of ['basket-buy','basket-sell','basket-pair']) {
      assert.ok(await page.$$eval(`#basket [data-pane=price] polygon[data-kind="${kind}"]`, n=>n.length)>0);
    }
    await page.$eval('#basket [data-pane=price] polygon[data-kind="basket-pair"]',e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true})));
    await page.waitForFunction(()=>document.querySelector('#basket .execution-body').textContent.includes('no token swap'));
    await page.setViewport({width:390,height:844});
    await page.$eval('#basket .execution-details', e=>e.scrollIntoView());
    if (process.env.SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'control-lab-mobile.png')});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true,
      JSON.stringify(await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1 && e.clientWidth>0).slice(0,12).map(e=>[e.tagName,e.className,e.getBoundingClientRect().width]))));
    assert.deepEqual(errors,[]);
    console.log(`Browser checks passed: data loading, selectors, race handling, zoom, keyboard cursor, legends, copy-to-all, EMA details, all stages (${process.env.TEST_PROFILES || 'three-phase,mixed,shock'}) and mobile layout.`);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
