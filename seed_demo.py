#!/usr/bin/env python3
"""Create one traceable teaching scenario through the same role APIs as the UI."""
import getpass
import json
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

import psycopg
from server import db_options, demo_mode, today

BASE = 'http://127.0.0.1:18766'
STAMP = today().isoformat()
MARKER = f'演示链路 · {STAMP}'
CUSTOMER = f'演示客户｜启衡办公采购项目｜{STAMP}'


class Client:
    def __init__(self, username, password):
        self.username = username
        self.role = {'sales_demo':'sales','finance_demo':'finance','employee_demo':'work',
                     'manager_demo':'review','executive_demo':'owner'}[username]
        self.csrf = ''
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        if not demo_mode():
            self.csrf = self.call('/api/login', {'username': username, 'password': password})['csrf_token']

    def call(self, path, body=None):
        headers = {'Content-Type': 'application/json'}
        if demo_mode(): headers['X-Demo-Role'] = self.role
        if body is not None:
            headers.update({'X-CSRF-Token': self.csrf, 'Idempotency-Key': uuid4().hex})
        request = Request(BASE + path, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None, headers=headers)
        with self.opener.open(request, timeout=12) as response:
            result = json.load(response)
        return result['data']


def existing(conn, sql, args):
    row = conn.execute(sql, args).fetchone()
    return next(iter(row.values())) if row else None


def main():
    if db_options()['dbname'] != 'office_demo_ready':
        raise SystemExit('仅允许写入 office_demo_ready 教学演示库')
    password = None if demo_mode() else getpass.getpass('本地教学账号密码：')
    sales = Client('sales_demo', password)
    finance = Client('finance_demo', password)
    worker = Client('employee_demo', password)
    manager = Client('manager_demo', password)
    executive = Client('executive_demo', password)
    before = executive.call('/api/dashboard')['current']
    with psycopg.connect(**db_options()) as conn:
        customer = existing(conn, 'SELECT customer_id FROM crm.customers WHERE customer_name=%s', (CUSTOMER,))
        if not customer:
            customer = sales.call('/api/customers', {'customer_name': CUSTOMER, 'contact_name': '演示联系人',
                'region': '深圳', 'historical_interest': '演示链路：办公设备采购'})['id']
        followup = existing(conn, 'SELECT followup_id FROM crm.followups WHERE customer_id=%s AND next_action=%s', (customer, MARKER))
        if not followup:
            followup = sales.call('/api/followups', {'customer_id': customer, 'occurred_on': STAMP,
                'contact_method': '面访', 'customer_needs': '演示采购设备与安装服务', 'next_action': MARKER})['id']
        opportunity = existing(conn, 'SELECT opportunity_id FROM sales.opportunities WHERE customer_id=%s AND notes=%s', (customer, MARKER))
        if not opportunity:
            opportunity = sales.call('/api/opportunities', {'customer_id': customer, 'stage': '方案报价',
                'opened_on': STAMP, 'expected_amount': '12000.00', 'lead_source': '演示', 'notes': MARKER})['id']
        quote = existing(conn, 'SELECT quote_id FROM sales.quotes WHERE opportunity_id=%s AND notes=%s', (opportunity, MARKER))
        if not quote:
            quote = sales.call('/api/quotes', {'opportunity_id': opportunity, 'quoted_on': STAMP, 'notes': MARKER,
                'lines': [{'sku': 'SKU008', 'quantity': '2', 'discount_rate': '0'},
                          {'sku': 'SKU021', 'quantity': '3', 'discount_rate': '0'}]})['id']
        order = existing(conn, 'SELECT order_id FROM sales.orders WHERE quote_id=%s', (quote,))
        if not order:
            order = sales.call('/api/orders', {'quote_id': quote, 'signed_on': STAMP, 'notes': MARKER})['id']
        receipt = existing(conn, 'SELECT receipt_id FROM finance.customer_receipts WHERE order_id=%s AND notes=%s', (order, MARKER))
        if not receipt:
            receipt = finance.call('/api/receipts', {'order_id': order, 'received_on': STAMP, 'amount': '4000.00',
                'payment_method': '银行转账', 'receipt_type': '预付款', 'notes': MARKER,
                'voucher_ref': 'DEMO-R-' + STAMP})['id']
        expense = existing(conn, 'SELECT expense_id FROM finance.expenses WHERE description=%s', (MARKER,))
        if not expense:
            expense = finance.call('/api/expenses', {'department_id': 'D003', 'expense_type': '演示办公费用',
                'occurred_on': STAMP, 'accrued_amount': '800.00', 'paid_on': STAMP,
                'paid_amount': '500.00', 'description': MARKER, 'voucher_ref': 'DEMO-E-' + STAMP})['id']
        report = existing(conn, '''SELECT version_id FROM app.daily_report_versions
            WHERE employee_id='E009' AND report_date=%s AND narrative LIKE '演示链路%%'
            ORDER BY version_no DESC LIMIT 1''', (STAMP,))
        if not report:
            first = worker.call('/api/reports', {'report_date': STAMP, 'narrative': MARKER + '：完成客户资料整理',
                'submit_state': '已提交'})['id']
            manager.call('/api/reviews', {'version_id': first, 'decision': '退回', 'comment': '请补充完成情况'})
            report = worker.call('/api/reports', {'report_date': STAMP,
                'narrative': MARKER + '：完成客户资料整理并补充交付说明',
                'completion_status': '已完成', 'submit_state': '已提交'})['id']
            manager.call('/api/reviews', {'version_id': report, 'decision': '通过', 'comment': '演示审核通过'})
    after = executive.call('/api/dashboard')['current']
    assert float(after['signed_amount']) - float(before['signed_amount']) in (0, 12000)
    assert float(after['cash_received']) - float(before['cash_received']) in (0, 4000)
    assert float(after['expenses_incurred']) - float(before['expenses_incurred']) in (0, 800)
    assert float(after['revenue']) == float(before['revenue'])
    print(json.dumps({'date': STAMP, 'customer': customer, 'followup': followup,
        'opportunity': opportunity, 'quote': quote, 'order': order,
        'receipt': receipt, 'expense': expense, 'report_version': report,
        'signed_delta': float(after['signed_amount']) - float(before['signed_amount']),
        'received_delta': float(after['cash_received']) - float(before['cash_received']),
        'expenses_delta': float(after['expenses_incurred']) - float(before['expenses_incurred']),
        'revenue_delta': float(after['revenue']) - float(before['revenue'])}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
