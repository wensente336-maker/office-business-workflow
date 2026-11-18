// Local visual acceptance for passwordless teaching pages.
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const require = createRequire('/Users/WIN11/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json');
const { chromium } = require('playwright');
const ROOT = 'http://127.0.0.1:18766/';
const OUT = fileURLToPath(new URL('./acceptance/', import.meta.url));
const NOTICE = '此为示例版已去掉登录账号，真实场景会设置账号权限';
await mkdir(OUT, { recursive: true });

const browser = await chromium.launch({ headless: true,
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });

async function inspect(target, viewport, filename) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(ROOT + target, { waitUntil: 'networkidle' });
  await page.locator('.page-heading h1').waitFor();
  if (await page.locator('input[name=username],input[name=password],#logout').count())
    throw new Error(`${target}: 仍显示登录控件`);
  if (target === 'owner') {
    if (await page.locator('.demo-disclaimer, .owner-toolbar, #refresh-dashboard').count())
      throw new Error('老板页仍显示已移除的说明或读取时间行');
    if (await page.locator('.topbar .brandmark').innerText() !== '启衡办公｜老板 AI 经营驾驶舱' ||
        await page.locator('.page-heading h1').innerText() !== '老板 AI 经营驾驶舱')
      throw new Error('老板页标题未更新');
  } else if (await page.locator('.demo-disclaimer').innerText() !== NOTICE)
    throw new Error(`${target}: 示例说明缺失`);
  if (await page.locator('#sidebar-toggle').getAttribute('aria-expanded') !== 'false' || await page.locator('#sidebar').isVisible())
    throw new Error(`${target}: 侧边栏未默认关闭`);
  const metrics = await page.evaluate(() => ({ width: innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    heading: document.querySelector('.page-heading h1')?.textContent,
    oversized: [...document.querySelectorAll('body *')].filter(el => el.getBoundingClientRect().right > innerWidth + 2 && getComputedStyle(el).position !== 'fixed').slice(0, 8).map(el => ({ tag: el.tagName, className: String(el.className).slice(0, 60), right: Math.round(el.getBoundingClientRect().right) })) }));
  if (metrics.scrollWidth > metrics.width) throw new Error(`${target}: horizontal overflow ${JSON.stringify(metrics)}`);
  if (errors.length) throw new Error(`${target}: browser errors ${errors.join(' | ')}`);
  await page.screenshot({ path: OUT + filename, fullPage: false });
  if (target === 'owner') {
    if (!await page.getByRole('combobox', { name: '选择数据周期' }).isVisible() ||
        !await page.locator('label[for="week-select"]').isVisible())
      throw new Error('数据周期下拉框缺少可见名称');
    await page.locator('#business-analysis').waitFor();
    if (await page.locator('#week-select').inputValue() !== '2026-11-09' ||
        !await page.locator('#business-analysis').getByText('关联业务事项 132 条', { exact: false }).count())
      throw new Error('默认周期未设为资料较完整的 11 月 9 日经营周');
    await page.locator('#business-analysis').scrollIntoViewIfNeeded();
    await page.screenshot({ path: OUT + (viewport.width > 900 ? 'desktop-analysis.png' : 'mobile-analysis.png'), fullPage: false });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth))
      throw new Error('经营分析板块横向溢出');
    await page.evaluate(() => scrollTo(0, 0));
    await page.locator('#sidebar-toggle').click();
    if (await page.locator('#sidebar-toggle').getAttribute('aria-expanded') !== 'true' || !await page.locator('#sidebar').isVisible())
      throw new Error('经营看板侧边栏无法打开');
    await page.waitForTimeout(250);
    await page.screenshot({ path: OUT + (viewport.width > 900 ? 'desktop-owner-sidebar-open.png' : 'mobile-owner-sidebar-open.png'), fullPage: false });
    await page.keyboard.press('Escape');
    if (await page.locator('#sidebar').isVisible()) throw new Error('Esc 未关闭侧边栏');
    await page.locator('#sidebar-toggle').click();
    await page.locator('#sidebar-backdrop').click({ position: { x: viewport.width - 10, y: 100 } });
    if (await page.locator('#sidebar').isVisible()) throw new Error('遮罩未关闭侧边栏');
    if (viewport.width > 900) {
      await page.locator('#assistant-open').click();
      if (!await page.locator('#assistant-panel').isVisible()) throw new Error('电脑 AI 窗口未打开');
      await page.screenshot({ path: OUT + 'desktop-assistant-window.png', fullPage: false });
      await page.keyboard.press('Escape');
      if (await page.locator('#assistant-panel').isVisible()) throw new Error('电脑 AI 窗口无法用 Esc 关闭');
    }
  }
  await context.close();
  return { target, viewport, metrics, passed: true };
}

try {
  for (const [target, viewport, filename] of [
    ['owner', { width: 1440, height: 900 }, 'desktop-owner.png'],
    ['owner', { width: 320, height: 693 }, 'compact-owner.png'],
    ['owner', { width: 390, height: 693 }, 'mobile-owner.png'],
    ['sales', { width: 390, height: 693 }, 'mobile-sales.png'],
    ['finance', { width: 360, height: 640 }, 'mobile-finance.png'],
    ['work', { width: 390, height: 693 }, 'mobile-work.png'],
    ['review', { width: 1440, height: 900 }, 'desktop-review.png'],
  ]) console.log(JSON.stringify(await inspect(target, viewport, filename)));
  const context = await browser.newContext({ viewport: { width: 390, height: 693 } });
  const page = await context.newPage();
  await page.goto(ROOT + 'owner', { waitUntil: 'networkidle' });
  await page.getByRole('heading', { name: '老板 AI 经营驾驶舱' }).waitFor();
  await page.locator('.mobile-nav a[href="/sales"]').click();
  await page.getByRole('heading', { name: '新增客户' }).waitFor();
  if (page.url() !== ROOT + 'sales') throw new Error('老板入口未进入销售独立页面');
  console.log(JSON.stringify({ check: 'owner -> sales without login', passed: true }));
  await context.close();
  const historyContext = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const historyPage = await historyContext.newPage();
  await historyPage.goto(ROOT + 'owner', { waitUntil: 'networkidle' });
  await historyPage.locator('#week-select').selectOption('2026-11-09');
  await historyPage.locator('#business-analysis').getByText('关联业务事项 132 条', { exact: false }).waitFor();
  await historyPage.locator('.analysis-card').first().locator('details summary').first().click();
  if (!await historyPage.locator('.analysis-card').first().getByText('事项 ACT', { exact: false }).count())
    throw new Error('原资料包详细业务事项未显示来源编号');
  await historyPage.locator('#business-analysis').scrollIntoViewIfNeeded();
  await historyPage.screenshot({ path: OUT + 'desktop-analysis-original-week.png', fullPage: false });
  console.log(JSON.stringify({ check: 'original week 132 activity records and evidence', passed: true }));
  await historyPage.getByRole('button', { name: '员工与部门' }).click();
  const firstEmployee = historyPage.locator('.employee-item').first();
  await firstEmployee.locator('.employee-expand').click();
  await firstEmployee.getByText('规则摘要').waitFor();
  if (await firstEmployee.locator('.employee-expand').getAttribute('aria-expanded') !== 'true' ||
      !await firstEmployee.getByText('有效日报 5/5 份').count())
    throw new Error('员工周摘要没有正确展开或未按所选周期取数');
  await firstEmployee.locator('.employee-expand').click();
  if (await firstEmployee.locator('.employee-expand').getAttribute('aria-expanded') !== 'false')
    throw new Error('员工周摘要无法收起');
  console.log(JSON.stringify({ check: 'desktop employee weekly summary', passed: true }));
  await historyContext.close();
  const mobileContext = await browser.newContext({ viewport: { width: 390, height: 693 } });
  await mobileContext.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: 'http://127.0.0.1:18766' });
  const mobilePage = await mobileContext.newPage();
  await mobilePage.goto(ROOT + 'owner', { waitUntil: 'networkidle' });
  await mobilePage.locator('#week-select').selectOption('2026-11-09');
  await mobilePage.getByRole('button', { name: '员工与部门' }).click();
  await mobilePage.locator('.employee-item').first().locator('.employee-expand').click();
  await mobilePage.locator('.employee-item').first().getByText('规则摘要').waitFor();
  if (await mobilePage.evaluate(() => document.documentElement.scrollWidth > innerWidth))
    throw new Error('手机员工周摘要横向溢出');
  await mobilePage.locator('.employee-item').first().scrollIntoViewIfNeeded();
  await mobilePage.screenshot({ path: OUT + 'mobile-employee-summary.png', fullPage: false });
  console.log(JSON.stringify({ check: 'mobile employee weekly summary', passed: true }));
  await mobilePage.locator('#assistant-open').click();
  const assistantSize = await mobilePage.locator('#assistant-panel').evaluate(el => ({
    width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height,
    viewportWidth: innerWidth, viewportHeight: innerHeight,
    messagesOverflow: getComputedStyle(el.querySelector('.assistant-messages')).overflowY }));
  if (assistantSize.width > assistantSize.viewportWidth ||
      assistantSize.height >= assistantSize.viewportHeight * .8 ||
      assistantSize.messagesOverflow !== 'auto')
    throw new Error('手机 AI 窗口尺寸或内部滚动不符合要求: ' + JSON.stringify(assistantSize));
  if (await mobilePage.locator('.assistant-example').count() !== 3)
    throw new Error('AI 窗口缺少三个示例问题');
  await mobilePage.screenshot({ path: OUT + 'mobile-assistant-window.png', fullPage: false });
  const assistantRequests = [];
  await mobilePage.route('**/api/assistant/chat', route => { assistantRequests.push(JSON.parse(route.request().postData())); return route.fulfill({ status: 200,
    contentType: 'application/json', body: JSON.stringify({ data: {
      answer: '这是测试回答。'.repeat(20), limitations: '仅用于界面验收。',
      sources: [{ kind: '经营周指标', label: '测试周期', date: '2026-11-09',
        detail: '签单额：测试值', ref: 'KPI:test' }] } }) }); });
  await mobilePage.locator('.assistant-example').first().click();
  await mobilePage.locator('.assistant-reply').getByText('这是测试回答。', { exact: false }).waitFor();
  await mobilePage.locator('.assistant-reply .assistant-copy').first().click();
  if (await mobilePage.evaluate(() => navigator.clipboard.readText()) !== '这是测试回答。'.repeat(20) ||
      !await mobilePage.locator('.assistant-reply .assistant-copy').first().getByText('已复制').count())
    throw new Error('AI 回答复制按钮没有复制当前消息');
  if (assistantRequests[0].week !== '2026-11-09' || assistantRequests[0].scope !== 'week')
    throw new Error('AI 助手的当前周期与看板选择不一致');
  await mobilePage.locator('.assistant-sources summary').click();
  if (!await mobilePage.getByText('签单额：测试值').isVisible())
    throw new Error('AI 回答来源无法展开');
  for (let i = 0; i < 3; i++) {
    await mobilePage.locator('#assistant-question').fill('继续追问第 ' + (i + 1) + ' 个问题');
    await mobilePage.locator('#assistant-form button').click();
    await mobilePage.locator('.assistant-reply').nth(i + 1).getByText('这是测试回答。', { exact: false }).waitFor();
  }
  if (await mobilePage.locator('.assistant-reply .assistant-copy').count() !== 4)
    throw new Error('不是每条 AI 回答都提供复制按钮');
  const scrollable = await mobilePage.locator('#assistant-messages').evaluate(el =>
    el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY === 'auto');
  if (!scrollable) throw new Error('多轮对话没有在窗口内部滚动');
  await mobilePage.screenshot({ path: OUT + 'mobile-assistant-conversation.png', fullPage: false });
  await mobilePage.locator('#assistant-close').click();
  if (await mobilePage.locator('#assistant-panel').isVisible())
    throw new Error('手机 AI 窗口无法关闭');
  await mobilePage.locator('#week-select').selectOption('2026-11-16');
  await mobilePage.locator('#assistant-open').click();
  if (await mobilePage.locator('#assistant-period').innerText() !== '2026-11-16')
    throw new Error('切换看板周期后 AI 助手没有同步更新');
  await mobilePage.locator('#assistant-close').click();
  console.log(JSON.stringify({ check: 'mobile assistant size, scroll, examples, close', passed: true, assistantSize }));
  await mobileContext.close();
  const raceContext = await browser.newContext({ viewport: { width: 390, height: 693 } });
  const racePage = await raceContext.newPage();
  await racePage.route('**/api/assistant/chat', async route => {
    await new Promise(resolve => setTimeout(resolve, 900));
    await route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ data: { answer: '旧周期测试回答', sources: [] } }) });
  });
  await racePage.goto(ROOT + 'owner', { waitUntil: 'networkidle' });
  await racePage.locator('#assistant-open').click();
  await racePage.locator('.assistant-example').first().click();
  await racePage.locator('#assistant-close').click();
  await racePage.locator('#week-select').selectOption('2026-11-16');
  await racePage.locator('#assistant-open').click();
  await racePage.waitForTimeout(1200);
  if (await racePage.locator('#assistant-messages').getByText('旧周期测试回答').count())
    throw new Error('旧周期的延迟回答混入新周期对话');
  console.log(JSON.stringify({ check: 'late answer discarded after period change', passed: true }));
  await raceContext.close();
} finally { await browser.close(); }
