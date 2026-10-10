/* Browser regression check. Use installed puppeteer-core and Chromium:
 * PUPPETEER_MODULE=/path/to/puppeteer-core CHROME_PATH=/path/to/chrome \
 *   node scripts/test-control-lab-browser.cjs [file-or-http-URL]
 * The full campaign must have rendered; screenshots are optional via SCREENSHOT_DIR.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
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
    const order = Object.keys(manifest.stages);
    assert.equal(order[order.indexOf('linked') + 1], 'basket');
    assert.ok(order.slice(order.indexOf('basket')).every(s=>manifest.stages[s].basket));
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
    await page.click('#monitor-k input[name=shared-scales]');
    for (const rail of ['Lower rail','Upper rail']) await page.click(`#monitor-k [aria-label="Toggle ${rail}"]`);
    const smallScale = await page.$$eval('#monitor-k [data-pane=k] svg text[x="85"]', nodes=>nodes.map(e=>Number(e.textContent.replaceAll(',',''))));
    assert.equal(smallScale.length,5);
    assert.ok(smallScale[4]-smallScale[0]<.002, 'local scale hides the calibration drift');
    const firstKey = await choose('all', {profile:'three-phase',walk:'0',buyer:'medium',seller:'medium'});
    assert.equal(await page.$$eval('#all .chart', n=>n.length), 11);
    // BUCK, USD, the fixed initial investment, and the recorded book at reference prices.
    assert.equal(await page.$$eval('#all [data-pane=basket-value] svg path', n=>n.length), 4);
    assert.equal(await page.$$eval('#all [data-pane=basket-return] svg path', n=>n.length), 4);
    assert.equal(await page.$eval('#all [aria-label="Toggle Initial investment · BUCK / USD"] .value',e=>e.textContent),'1,000,000');
    const returnMetrics = await page.$$eval('#all .metric', nodes=>Object.fromEntries(nodes.map(n=>[n.querySelector('span').textContent,n.querySelector('strong').textContent])));
    const rawURL = new URL(manifest.entries[firstKey].file, url);
    const raw = rawURL.protocol === 'file:' ? JSON.parse(fs.readFileSync(rawURL, 'utf8')) : await (await fetch(rawURL)).json();
    const end = raw.frames.at(-1), initial = raw.config.market.basket_equity_usd;
    for (const [currency, rate] of [['BUCK', 1], ['USD', end.buck_usdc]]) {
      const roi = 100 * (end.basket.equity * rate / initial - 1);
      assert.equal(returnMetrics[`Marked return on initial ${currency}`], `${roi >= 0 ? '+' : ''}${roi.toFixed(3)}%`);
    }
    // At reference: wallet and position TOKEN at the external feed, net BUCK at the reference basket's cost.
    let atReference = (end.basket.account + end.basket.relief) * end.reference.target_usd;
    end.basket.idle.forEach((idle, i) => {
      const root = Math.sqrt(end.token_buck[i]), liquidity = Number(end.basket.liquidity[i]);
      atReference += (idle + liquidity / (root * 1e12)) * end.target_usd[i] + liquidity * root / 1e12 * end.reference.target_usd;
    });
    const referenceReturn = 100 * (atReference / initial - 1);
    assert.equal(returnMetrics['Marked return at reference'], `${referenceReturn >= 0 ? '+' : ''}${referenceReturn.toFixed(3)}%`);
    const allocationScale = () => page.$$eval('#all [data-pane=allocation] svg text[x="85"]',ns=>ns.map(n=>Number(n.textContent.replaceAll(',',''))));
    const allocationBounds = await allocationScale();
    assert.ok(Math.min(...allocationBounds)>20 && Math.max(...allocationBounds)<45,
      'allocation must fit the data even with shared scales enabled');
    const allocationSpan = Math.max(...allocationBounds)-Math.min(...allocationBounds);
    for (const token of ['B','C']) await page.click(`#all [data-pane=allocation] [aria-label="Toggle TOKEN ${token}"]`);
    await page.$eval('#all .zoom input',e=>{e.value='119';e.dispatchEvent(new Event('input'));});
    const zoomedAllocation = await allocationScale();
    assert.ok(Math.max(...zoomedAllocation)-Math.min(...zoomedAllocation)<allocationSpan,
      'allocation must rescale after zooming and hiding series');
    await page.click('#all .zoom button');
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
    assert.equal(await page.$$eval('#all .chart', n=>n.length), 27);
    await page.waitForSelector('#all .monitor-details [data-pane="pair-rate-2"]');
    await page.waitForSelector('#all .monitor-details [data-pane="pair-impact"]');
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
    await page.click('#all input[name=linked-settings]');
    assert.equal(await page.$eval('#monitor select[name=walk]',e=>e.value),'1');
    assert.ok(await page.$$eval('input[name=linked-settings]',ns=>ns.every(n=>n.checked)));
    await page.waitForFunction(key=>document.querySelector('#feeds').dataset.loadedKey===key,{},manifest.cases.feeds['three-phase:1:high:low']);
    // Edits from another test continue propagating, including to closed
    // tests and inactive actor selectors. The change is not a one-shot copy.
    await page.click('#stage-nav a[href="#monitor"]');
    await page.select('#monitor select[name=walk]','0.5');
    await page.waitForFunction(key=>document.querySelector('#all').dataset.loadedKey===key,{},manifest.cases.all['three-phase:0.5:high:low']);
    assert.equal(await page.$eval('#supply select[name=walk]',e=>e.value),'0.5');
    assert.equal(await page.$eval('#supply select[name=buyer]',e=>e.value),'high');
    assert.equal(await page.$eval('#supply',e=>e.open),false);
    await page.click('#stage-nav a[href="#supply"]');
    await page.waitForFunction(key=>document.querySelector('#supply').dataset.loadedKey===key,{},manifest.cases.supply['three-phase:0.5:high:low']);
    await page.click('#supply input[name=linked-settings]');
    assert.ok(await page.$$eval('input[name=linked-settings]',ns=>ns.every(n=>!n.checked)));
    await page.select('#all select[name=walk]','0');
    assert.equal(await page.$eval('#monitor select[name=walk]',e=>e.value),'0.5');
    if (process.env.SCREENSHOT_DIR) {
      await page.$eval('#all [data-pane=price]',e=>e.scrollIntoView());
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'control-lab-charts.png')});
    }
    // Every stage/profile can actually load its selected saved run.
    for (const profile of (process.env.TEST_PROFILES || 'three-phase,mixed,shock').split(',')) for (const stage of Object.keys(manifest.stages)) {
      const key = await choose(stage,{profile,walk:'1',buyer:'high',seller:'low'});
      assert.equal(await page.$eval(`#${stage} .run-body`,e=>e.hidden),false);
      // Quiet directions must have no marker, just as recorded fills must
      // have one. Market changes need not produce every kind of execution.
      const counts = manifest.entries[key].execution_counts;
      for (const [kind,counter] of [['desk-buy','desk_buys'],['desk-sell','desk_sells'],
          ['basket-buy','basket_buys'],['basket-sell','basket_sells']]) {
        assert.equal(await page.$$eval(`#${stage} [data-pane=price] polygon[data-kind="${kind}"]`,n=>n.length>0),
          counts[counter]>0,`${profile}/${stage}/${kind}`);
      }
    }
    // The commodity profile is a complete path: no random-walk variant exists,
    // so choosing it moves the walk to 0 and disables the other choices.
    await choose('all', {profile:'three-phase',walk:'1',buyer:'medium',seller:'medium'});
    await page.select('#all select[name=profile]','commodities');
    await page.waitForFunction(key=>document.querySelector('#all').dataset.loadedKey===key,{},manifest.cases.all['commodities:0:medium:medium']);
    assert.equal(await page.$eval('#all select[name=walk]',e=>e.value),'0');
    assert.deepEqual(await page.$$eval('#all select[name=walk] option',ns=>ns.map(n=>n.disabled)),[false,true,true]);
    assert.equal(await page.$$eval('#all [data-pane=price] svg path', n=>n.length), 10); // nine price lines and the inflation trend
    assert.match(await page.$eval('#all .run-body', e=>e.textContent), /Synthetic commodity prices: A = Fast, gold-like/);
    assert.ok(Object.keys(manifest.cases.all).every(k=>!k.startsWith('commodities:') || k.split(':')[1]==='0'));
    for (const stage of Object.keys(manifest.stages)) {
      await choose(stage,{profile:'commodities',walk:'0',buyer:'high',seller:'low'});
      assert.equal(await page.$eval(`#${stage} .run-body`,e=>e.hidden),false);
    }
    await page.select('#all select[name=profile]','three-phase');
    assert.deepEqual(await page.$$eval('#all select[name=walk] option',ns=>ns.map(n=>n.disabled)),[false,false,false]);
    // Desk directions come from token swaps, and marker clicks select the
    // real receipt log, including the opposite leg of triangle arbitrage.
    // Enabling the pair basket changes which cases cross the desk's bands
    // and funding gates. Select actual recorded fills instead of requiring
    // both desk directions in one formerly-active legacy case.
    async function deskFill(side) {
      const counter = side === 'buy' ? 'desk_buys' : 'desk_sells';
      for (const stage of ['desk','all']) for (const [selector,key] of Object.entries(manifest.cases[stage])) {
        if (!manifest.entries[key].execution_counts[counter]) continue;
        const selection = Object.fromEntries(selector.split(':').map((v,i)=>[manifest.axes[i].id,v]));
        await choose(stage, selection);
        for (const kind of [`desk-${side}`,'arb-buy','arb-sell']) {
          assert.ok(await page.$$eval(`#${stage} [data-pane=price] polygon[data-kind="${kind}"]`, n=>n.length)>0);
        }
        return stage;
      }
      return null; // A legitimately quiet direction is checked above.
    }
    await deskFill('sell');
    const deskStage = await deskFill('buy');
    assert.ok(deskStage, 'Need at least one recorded desk TOKEN buy to test its execution log');
    const deskPoint = `#${deskStage} [data-pane=price] polygon[data-kind="desk-buy"]`;
    await page.$eval(deskPoint,e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true})));
    await page.waitForFunction(stage=>document.querySelector(`#${stage} .execution-details`).open && document.querySelector(`#${stage} .execution-body table`),{},deskStage);
    assert.match(await page.$eval(`#${deskStage} .execution-body`,e=>e.textContent), /desk · (supply|issue)/);
    assert.match(await page.$eval(`#${deskStage} .execution-body`,e=>e.textContent), /BUY/);
    await page.click(`#${deskStage} [data-pane=price] [aria-label="Toggle Arb TOKEN buys"]`);
    assert.equal(await page.$$eval(`#${deskStage} [data-pane=price] polygon[data-kind="arb-buy"]`,n=>n.length),0);
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
    const linkedURL = new URL(url); linkedURL.search='profile=mixed&walk=0.5&buyer=high&seller=low&linked=1';linkedURL.hash='basket';
    await page.goto(linkedURL.href);await page.waitForSelector('#basket[data-loaded-key]');
    assert.ok(await page.$$eval('input[name=linked-settings]',ns=>ns.every(n=>n.checked)));
    await page.select('#basket select[name=walk]','1');
    assert.equal(await page.$eval('#all select[name=walk]',e=>e.value),'1');
    assert.deepEqual(errors,[]);
    console.log(`Browser checks passed: BUCK, USD and reference basket value/return, allocation autorange, persistent linked settings, selectors, the walk-free commodity profile, race handling, zoom, cursor, legends, sensor details, all stages (${process.env.TEST_PROFILES || 'three-phase,mixed,shock'}) and mobile layout.`);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
