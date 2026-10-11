// Optional browser smoke test: NODE_PATH must make playwright available.
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const {join} = require('node:path');
const fixture = JSON.parse(execFileSync(process.env.PYTHON || 'python3', [join(__dirname, 'export_ui_fixture.py')]));
(async () => {
  const browser = await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({viewport:{width:1100,height:800}});
    const errors=[];page.on('pageerror', error=>errors.push(error.message));
    // Give localStorage a test-only origin without starting any server.
    await page.route('http://monitor.test/**', route=>route.fulfill({contentType:'text/html',body:`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>body{font:16px sans-serif;padding:30px;background:#f8fafc}article{margin:25px 0;padding:20px;background:white;border:1px solid #ddd;max-width:650px}</style><body><h1>Codex Monitor · 合成数据验收</h1><div data-app-action-sidebar-thread-row data-app-action-sidebar-thread-active="true" data-app-action-sidebar-thread-id="chat">示例聊天</div><article data-turn-key="t1"><div data-content-search-assistant-turn-key="t1">A sufficiently long reply</div><div><time data-assistant-message-sent-time>12:00 PM</time></div></article></body></html>`}));
    await page.goto('http://monitor.test/');
    await page.evaluate(()=>(localStorage.setItem('codex-context-token-inspector-unit','raw'), localStorage.setItem('codex-context-token-inspector-unit-defaulted','true')));
    const apply=async(payload=fixture.payload)=>{
      await page.evaluate(({script,payload})=>eval(script)(payload),{script:fixture.script,payload});
      await page.waitForTimeout(1100);
    };
    await apply();
    const root=page.locator('#codex-context-token-inspector-root');
    const chip=page.locator('[data-context-token-chip]');
    assert.equal(await chip.count(),1);
    assert.match(await chip.innerText(),/600\/600/);
    assert.match(await chip.innerText(),/工具 1/);
    assert.match(await chip.getAttribute('title'),/read_file × 1/);
    assert.match(await root.innerText(),/10\.00.*个百分点/);
    assert.match(await root.innerText(),/6\.00%/);
    await root.locator('summary').click();
    assert.equal(await root.locator('details div').count()>=3,true);
    await apply();assert.equal(await chip.count(),1);
    assert.equal(await root.locator('details').evaluate(n=>n.open),true);
    assert.equal(await page.locator('[data-assistant-message-sent-time]').count(),1);
    if(process.env.SCREENSHOT) await page.screenshot({path:process.env.SCREENSHOT,fullPage:true});
    await page.setViewportSize({width:420,height:850});await apply();
    const box=await root.boundingBox();assert.ok(box.width<=420);
    await page.locator('[data-app-action-sidebar-thread-row]').evaluate(n=>n.setAttribute('data-app-action-sidebar-thread-id','unknown'));
    await apply();assert.equal(await chip.count(),0);assert.match(await root.innerText(),/暂无|No token/);
    await page.locator('[data-app-action-sidebar-thread-row]').evaluate(n=>n.setAttribute('data-app-action-sidebar-thread-id','chat'));
    await page.locator('article').evaluate(n=>n.removeAttribute('data-turn-key'));
    const ambiguous=JSON.parse(JSON.stringify(fixture.payload));
    ambiguous.detailsByThread.chat.assistantItems.push({...ambiguous.detailsByThread.chat.assistantItems[0],turnId:'t2'});
    await apply(ambiguous);assert.equal(await chip.count(),0);
    await page.locator('[data-content-search-assistant-turn-key]').evaluate(n=>n.textContent='Unrelated visible historical reply');
    await apply();assert.equal(await chip.count(),0);
    await page.locator('[data-app-action-sidebar-thread-row]').evaluate(n=>n.remove());
    await apply();assert.equal(await chip.count(),0);assert.match(await root.innerText(),/暂无|No token/);
    assert.deepEqual(errors,[]);
    console.log('PASS: whole-turn display, tools, compaction, reset cycles, reinjection, narrow layout, unknown thread and ambiguous/unmatched reply isolation');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
