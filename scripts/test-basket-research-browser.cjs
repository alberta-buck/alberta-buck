/* Exercise all cached research arms without a web server or remote libraries. */
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
    const errors=[];
    page.on('pageerror', e=>errors.push(String(e)));
    page.on('console', m=>{if(m.type()==='error')errors.push(m.text());});
    await page.goto(process.argv[2] || pathToFileURL(path.resolve('doc/control-lab/basket-research/current/index.html')).href);
    await page.waitForSelector('details[data-loaded-key]');
    const m=await page.evaluate(()=>window.controlLabManifest);
    assert.equal(m.kind,'basket-research'); assert.equal(m.status,'complete');
    assert.equal(Object.keys(m.stages).length,4);
    let checked=0;
    for(const stage of Object.keys(m.stages))for(const profile of m.axes[0].values){
      await page.click(`#stage-nav a[href="#${stage}"]`);
      await page.select(`#${stage} select[name=profile]`,profile.id);
      const key=m.cases[stage][`${profile.id}:0:medium:medium`];
      await page.waitForFunction((s,k)=>document.getElementById(s).dataset.loadedKey===k,{},stage,key);
      await page.$eval(`#${stage} .monitor-details`,e=>{e.open=true;});
      await page.waitForSelector(`#${stage} [data-pane=pair-noise-2] svg`);
      for(let i=0;i<3;i++){
        assert.equal(await page.$$eval(`#${stage} [data-pane=pair-noise-${i}] svg path`,n=>n.length),4);
        assert.ok(await page.$$eval(`#${stage} [data-pane=pair-rate-${i}] svg path`,n=>n.length)>=8);
      }
      assert.equal(await page.$$eval(`#${stage} [data-pane=pair-innovation] svg path`,n=>n.length),3);
      const owned=stage.endsWith('-owned');
      for(const currency of ['buck','usd']){
        assert.equal(await page.$$eval(`#${stage} [data-pane=basket-income-${currency}] svg path`,n=>n.length),owned?4:0);
        assert.equal(await page.$$eval(`#${stage} [data-pane=basket-effects-${currency}] svg path`,n=>n.length),stage==='rebalance-owned'?5:0);
      }
      if(owned)assert.match(await page.$eval(`#${stage} .metrics`,e=>e.textContent),/Arb income received · BUCK/);
      if(profile.id==='historical-bridge')assert.match(await page.$eval(`#${stage} .run-body`,e=>e.textContent),/A = Gold.*B = Food.*C = Energy/);
      assert.ok(await page.$$eval(`#${stage} svg path`,ns=>ns.every(n=>!/(NaN|Infinity)/.test(n.getAttribute('d')))));
      checked++;
    }
    const stage='rebalance-owned';
    await page.click(`#${stage} input[name=linked-settings]`);
    await page.select(`#${stage} select[name=profile]`,'three-phase');
    assert.ok(await page.$$eval('select[name=profile]',ns=>ns.every(n=>n.value==='three-phase')));
    const key=m.cases[stage]['three-phase:0:medium:medium'];
    await page.waitForFunction(k=>document.getElementById('rebalance-owned').dataset.loadedKey===k,{},key);
    await page.$eval(`#${stage} .zoom input`,e=>{e.value='30';e.dispatchEvent(new Event('input'));});
    assert.equal(await page.$eval(`#${stage} .zoom output`,e=>e.textContent),'30–120 days');
    await page.$eval(`#${stage} [data-pane=basket-income-buck] svg`,e=>e.focus());
    await page.keyboard.press('Home');
    assert.equal(await page.$eval(`#${stage} [data-pane=basket-income-usd] .chart-day`,e=>e.textContent),'Day 30');
    await page.setViewport({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    if(process.env.SCREENSHOT_DIR){
      await page.$eval(`#${stage} [data-pane=basket-income-buck]`,e=>e.scrollIntoView({behavior:'instant'}));
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'basket-research-mobile.png')});
    }
    assert.deepEqual(errors,[]);
    console.log(`Passed ${checked} research selectors, income and estimator panes, linked settings, zoom and mobile layout.`);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
