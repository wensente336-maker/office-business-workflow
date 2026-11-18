#!/usr/bin/env python3
"""End-to-end local teaching workflow verification against isolated DB."""
import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.cookiejar import CookieJar
from uuid import uuid4

import psycopg

from server import Handler, db_options, make_password_hash, today
os.environ.setdefault('OFFICE_DB_NAME','office_demo_operational')


def owner_options():
    options=db_options()
    options['user']=os.getenv('OFFICE_DB_OWNER_USER',os.getenv('USER','WIN11'))
    return options


def create_user(conn, username, employee_id, roles, password):
    user_id = 'UT' + uuid4().hex[:14]
    conn.execute('''INSERT INTO app.users(user_id,username,password_hash,employee_id)
        VALUES(%s,%s,%s,%s) ON CONFLICT(employee_id) DO UPDATE
        SET username=EXCLUDED.username,password_hash=EXCLUDED.password_hash,active=TRUE''',
        (user_id,username,make_password_hash(password),employee_id))
    user_id=conn.execute('SELECT user_id FROM app.users WHERE employee_id=%s',(employee_id,)).fetchone()['user_id']
    conn.execute('DELETE FROM app.user_roles WHERE user_id=%s',(user_id,))
    for role in roles:
        conn.execute('INSERT INTO app.user_roles(user_id,role) VALUES(%s,%s)',(user_id,role))
    return user_id


class Client:
    def __init__(self, base):
        self.base=base
        self.opener=build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf=''

    def call(self,path,body=None,expect=200,key=None):
        headers={'Content-Type':'application/json'}
        if body is not None:
            headers['Idempotency-Key']=key or uuid4().hex
            headers['X-CSRF-Token']=self.csrf
        request=Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with self.opener.open(request,timeout=10) as response:
                status=response.status
                payload=json.load(response)
        except HTTPError as error:
            status=error.code
            payload=json.load(error)
        assert status==expect,(path,status,payload)
        return payload.get('data') if expect==200 else payload

    def login(self,username,password):
        user=self.call('/api/login',{'username':username,'password':password})
        self.csrf=user['csrf_token']
        return user


def main():
    suffix=uuid4().hex[:6]
    password=secrets.token_urlsafe(20)
    with psycopg.connect(**owner_options()) as conn:
        worker_id='ET'+suffix.upper()
        admin_employee_id='EA'+suffix.upper()
        conn.execute('''INSERT INTO org.employees(employee_id,department_id,employee_name,position_name,manager_employee_id,job_type,employment_status,hired_on)
            SELECT %s,department_id,%s,position_name,manager_employee_id,job_type,employment_status,hired_on
            FROM org.employees WHERE employee_id='E009' ''',(worker_id,'联调员工'+suffix))
        conn.execute('''INSERT INTO org.employees(employee_id,department_id,employee_name,position_name,manager_employee_id,job_type,employment_status,hired_on)
            SELECT %s,department_id,%s,position_name,manager_employee_id,job_type,employment_status,hired_on
            FROM org.employees WHERE employee_id='E011' ''',(admin_employee_id,'联调新员工'+suffix))
        for name,employee,roles in [('sales','E003',['sales','employee']),('finance','E007',['finance','employee']),
                                    ('worker',worker_id,['employee']),('manager','E008',['manager','employee']),
                                    ('executive','E001',['executive'])]:
            create_user(conn,name+suffix,employee,roles,password)
        admin_id='UTA'+uuid4().hex[:13]
        conn.execute('''INSERT INTO app.users(user_id,username,password_hash) VALUES(%s,'testadmin',%s)
            ON CONFLICT(username) DO UPDATE SET password_hash=EXCLUDED.password_hash''',
            (admin_id,make_password_hash(password)))
        admin_id=conn.execute("SELECT user_id FROM app.users WHERE username='testadmin'").fetchone()['user_id']
        conn.execute('DELETE FROM app.user_roles WHERE user_id=%s',(admin_id,))
        for role in ('admin','executive'):
            conn.execute('INSERT INTO app.user_roles(user_id,role) VALUES(%s,%s)',(admin_id,role))
    os.environ['OFFICE_DB_USER']='office_demo_writer'
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        sales,finance,worker,manager,executive=[Client(base) for _ in range(5)]
        for client,name in zip([sales,finance,worker,manager,executive],['sales','finance','worker','manager','executive']):
            client.login(name+suffix,password)
        admin=Client(base)
        admin.login('testadmin',password)
        assert admin.call('/api/admin')['employees']
        new_user=admin.call('/api/admin/users',{'username':'new'+suffix,'employee_id':admin_employee_id,'roles':['employee']})
        new_client=Client(base)
        new_client.login('new'+suffix,new_user['initial_password'])
        assert new_client.call('/api/me')['employee_id']==admin_employee_id
        admin.call('/api/admin/calendar',{'day':today().isoformat(),'is_workday':True,'note':'联调配置'})
        week=today().strftime('%Y-%m-%d')
        before=executive.call('/api/dashboard')['current']
        assert sales.call('/api/dashboard',expect=403)['error']
        assert sales.call('/api/dashboard/analysis',expect=403)['error']
        customer=sales.call('/api/customers',{'customer_name':'测试客户'+suffix,'contact_name':'教学联系人','region':'深圳'})['id']
        future=(today()+timedelta(days=1)).isoformat()
        yesterday=(today()-timedelta(days=1)).isoformat()
        assert sales.call('/api/opportunities',{'customer_id':customer,'stage':'需求确认','opened_on':future,
                                                  'expected_amount':'5000.00'},expect=400)['error']
        followup=sales.call('/api/followups',{'customer_id':customer,'occurred_on':week,'contact_method':'电话','customer_needs':'教学样本需求'})['id']
        opportunity=sales.call('/api/opportunities',{'customer_id':customer,'stage':'需求确认','opened_on':week,'expected_amount':'5000.00'})['id']
        product=sales.call('/api/catalog')['products'][0]
        assert sales.call('/api/quotes',{'opportunity_id':opportunity,'quoted_on':yesterday,
                                        'lines':[{'sku':product['sku'],'quantity':'2','discount_rate':'0'}]},expect=400)['error']
        quote=sales.call('/api/quotes',{'opportunity_id':opportunity,'quoted_on':week,'lines':[{'sku':product['sku'],'quantity':'2','discount_rate':'0'}]})['id']
        order_key='order'+uuid4().hex
        order=sales.call('/api/orders',{'quote_id':quote,'signed_on':week},key=order_key)
        order_id=order['id']
        assert sales.call('/api/orders',{'quote_id':quote,'signed_on':week},key=order_key)['id']==order_id
        assert sales.call('/api/orders',{'quote_id':quote,'signed_on':week,'notes':'changed'},key=order_key,expect=409)['error']
        assert finance.call('/api/receipts',{'order_id':order_id,'received_on':future,'amount':'100.00',
                                             'voucher_ref':'TEST-FUTURE-'+suffix},expect=400)['error']
        receipt=finance.call('/api/receipts',{'order_id':order_id,'received_on':week,'amount':'1000.00','payment_method':'银行转账','voucher_ref':'TEST-R-'+suffix})['id']
        assert finance.call('/api/receipts',{'order_id':order_id,'received_on':week,'amount':'1000.00','voucher_ref':'TEST-R-'+suffix},expect=409)['error']
        expense=finance.call('/api/expenses',{'department_id':'D003','expense_type':'教学测试','occurred_on':week,
                                              'accrued_amount':'50.00','paid_on':week,'paid_amount':'50.00','voucher_ref':'TEST-E-'+suffix})['id']
        analysis_before=executive.call('/api/dashboard/analysis')['coverage']['usable']
        report=worker.call('/api/reports',{'report_date':week,'narrative':'完成教学联调记录','submit_state':'已提交'})['id']
        assert executive.call('/api/dashboard/analysis')['coverage']['usable']==analysis_before+1
        report_week=(today()-timedelta(days=today().weekday())).isoformat()
        summary_path=f'/api/dashboard/employees/{worker_id}/summary?week={report_week}'
        first_summary=executive.call(summary_path)
        assert first_summary['mode']=='rules' and first_summary['coverage']['submitted']==1
        assert sales.call('/api/reviews',{'version_id':report,'decision':'通过'},expect=403)['error']
        assert manager.call('/api/reports?scope=department')
        manager.call('/api/reviews',{'version_id':report,'decision':'退回','comment':'请补充完成情况'})
        assert executive.call('/api/dashboard/analysis')['coverage']['usable']==analysis_before
        assert executive.call(summary_path)['status']=='insufficient'
        revised=worker.call('/api/reports',{'report_date':week,'narrative':'完成教学联调记录，并补充说明','completion_status':'已完成','submit_state':'已提交'})['id']
        manager.call('/api/reviews',{'version_id':revised,'decision':'通过'})
        assert executive.call('/api/dashboard/analysis')['coverage']['usable']==analysis_before+1
        latest_summary=executive.call(summary_path)
        assert latest_summary['status']=='ready' and latest_summary['source_fingerprint']!=first_summary['source_fingerprint']
        after=executive.call('/api/dashboard')['current']
        assert int(after['new_orders'])==int(before['new_orders'])+1
        assert int(after['new_opportunities'])==int(before['new_opportunities'])+1
        assert int(after['new_quotes'])==int(before['new_quotes'])+1
        assert abs(float(after['cash_received'])-float(before['cash_received'])-1000)<0.001
        assert abs(float(after['expenses_incurred'])-float(before['expenses_incurred'])-50)<0.001
        assert abs(float(after['revenue'])-float(before['revenue']))<0.001
        assert int(after['daily_submitted'])==int(before['daily_submitted'])+1
        with psycopg.connect(**owner_options()) as conn:
            assert conn.execute('SELECT count(*) AS n FROM app.audit_events WHERE entity_id=%s',(order_id,)).fetchone()['n']==1
            assert conn.execute('SELECT count(*) AS n FROM sales.order_lines WHERE order_id=%s',(order_id,)).fetchone()['n']==1
            assert conn.execute('SELECT count(*) AS n FROM app.daily_report_reviews WHERE version_id IN (%s,%s)',(report,revised)).fetchone()['n']==2
        print(json.dumps({'status':'passed','customer':customer,'followup':followup,'order':order_id,'receipt':receipt,
                          'expense':expense,'report':report,'week':week},ensure_ascii=False))
    finally:
        server.shutdown()
        server.server_close()


if __name__=='__main__': main()
