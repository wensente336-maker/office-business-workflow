// Verify visible entry forms send the expected fields without writing to the demo database.
import { createRequire } from 'node:module';
const require = createRequire('/Users/WIN11/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json');
const { chromium } = require('playwright');
const browser = await chromium.launch({ headless: true,
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
const root = 'http://127.0.0.1:18766';

async function check(page, endpoint, prepare, expected) {
  let payload;
  await page.route('**' + endpoint, route => {
    payload = JSON.parse(route.request().postData());
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ data: { id: 'TEST-INTERCEPTED' } }) });
  });
  await prepare();
  for (let i = 0; i < 30 && !payload; i++) await page.waitForTimeout(100);
  if (!payload || expected.some(key => !(key in payload)))
    throw new Error(endpoint + ' form mapping mismatch: ' + JSON.stringify(payload));
  await page.unroute('**' + endpoint);
  return { endpoint, fields: expected, passed: true };
}

try {
  const sales = await browser.newPage();
  await sales.goto(root + '/sales', { waitUntil: 'networkidle' });
  console.log(JSON.stringify(await check(sales, '/api/customers', async () => {
    await sales.locator('[name=customer_name]').fill('浏览器映射验收（未写入）');
    await sales.locator('#entry-form button[type=submit]').click();
  }, ['customer_name','contact_name','region','historical_interest'])));
  await sales.getByRole('button', { name: '客户跟进', exact: true }).click();
  await sales.getByRole('heading', { name: '新增客户跟进' }).waitFor();
  console.log(JSON.stringify(await check(sales, '/api/followups', async () => {
    await sales.locator('[name=customer_id]').selectOption({ index: 1 });
    await sales.locator('#entry-form button[type=submit]').click();
  }, ['customer_id','occurred_on','contact_method','customer_needs','next_action'])));
  await sales.getByRole('button', { name: '商机', exact: true }).click();
  await sales.getByRole('heading', { name: '新增商机' }).waitFor();
  console.log(JSON.stringify(await check(sales, '/api/opportunities', async () => {
    await sales.locator('[name=customer_id]').selectOption({ index: 1 });
    await sales.locator('#entry-form button[type=submit]').click();
  }, ['customer_id','stage','opened_on','expected_amount'])));
  await sales.getByRole('button', { name: '推进商机', exact: true }).click();
  await sales.getByRole('heading', { name: '推进或关闭商机' }).waitFor();
  console.log(JSON.stringify(await check(sales, '/api/opportunity-stage', async () => {
    await sales.locator('[name=opportunity_id]').selectOption({ index: 1 });
    await sales.locator('[name=stage]').selectOption('谈判中');
    await sales.locator('#entry-form button[type=submit]').click();
  }, ['opportunity_id','stage'])));
  await sales.getByRole('button', { name: '报价', exact: true }).click();
  await sales.getByRole('heading', { name: '新增报价' }).waitFor();
  console.log(JSON.stringify(await check(sales, '/api/quotes', async () => {
    await sales.locator('[name=opportunity_id]').selectOption({ index: 1 });
    await sales.locator('[name=sku]').first().selectOption({ index: 1 });
    await sales.locator('#entry-form button[type=submit]').click();
  }, ['opportunity_id','quoted_on','lines'])));
  await sales.getByRole('button', { name: '订单', exact: true }).click();
  await sales.getByRole('heading', { name: '从报价生成订单' }).waitFor();
  console.log(JSON.stringify(await check(sales, '/api/orders', async () => {
    await sales.locator('[name=quote_id]').selectOption({ index: 1 });
    await sales.locator('#entry-form button[type=submit]').click();
  }, ['quote_id','signed_on'])));
  await sales.close();

  const finance = await browser.newPage();
  await finance.goto(root + '/finance', { waitUntil: 'networkidle' });
  console.log(JSON.stringify(await check(finance, '/api/receipts', async () => {
    await finance.locator('[name=order_id]').selectOption({ index: 1 });
    await finance.locator('[name=amount]').fill('10.00');
    await finance.locator('[name=voucher_ref]').fill('TEST-INTERCEPTED-R');
    await finance.locator('#entry-form button[type=submit]').click();
  }, ['order_id','received_on','amount','voucher_ref'])));
  await finance.getByRole('button', { name: '费用登记' }).click();
  await finance.getByRole('heading', { name: '登记经营费用' }).waitFor();
  console.log(JSON.stringify(await check(finance, '/api/expenses', async () => {
    await finance.locator('[name=department_id]').selectOption({ index: 1 });
    await finance.locator('[name=expense_type]').fill('界面验收');
    await finance.locator('[name=accrued_amount]').fill('10.00');
    await finance.locator('[name=voucher_ref]').fill('TEST-INTERCEPTED-E');
    await finance.locator('#entry-form button[type=submit]').click();
  }, ['department_id','occurred_on','expense_type','accrued_amount','paid_amount','voucher_ref'])));
  await finance.close();

  const work = await browser.newPage();
  await work.goto(root + '/work', { waitUntil: 'networkidle' });
  await work.locator('[data-report]').filter({ hasText: '2026-11-16' }).first().click();
  console.log(JSON.stringify(await check(work, '/api/reports', async () => {
    await work.locator('[name=narrative]').fill('界面字段映射验收，未写入数据库');
    await work.locator('#entry-form button[type=submit]').click();
  }, ['report_date','narrative','submit_state'])));
  await work.close();

  const review = await browser.newPage();
  await review.route('**/api/reports?scope=department', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ data: [{
      employee_id: 'E-OTHER', employee_name: '测试员工', report_date: '2026-11-17',
      submit_status: '已提交', submit_state: '已提交', decision: null,
      version_id: 'RV-INTERCEPTED', version_no: 1, narrative: '测试日报内容' }] }) }));
  await review.goto(root + '/review', { waitUntil: 'networkidle' });
  console.log(JSON.stringify(await check(review, '/api/reviews', async () => {
    await review.locator('[data-review][data-decision="通过"]').click();
  }, ['version_id','decision','comment'])));
  await review.close();
} finally {
  await browser.close();
}
