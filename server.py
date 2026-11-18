#!/usr/bin/env python3
"""Local role-aware workflow app backed by the isolated PostgreSQL copy."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from analysis import analyze
from employee_summary import get_summary
from business_chat import chat as business_chat, ChatError

HERE = Path(__file__).resolve().parent
ROLES = {'admin', 'sales', 'finance', 'employee', 'manager', 'executive'}
MONEY = Decimal('0.01')
SHANGHAI = ZoneInfo('Asia/Shanghai')
DEFAULT_DEMO_WEEK = date(2026, 11, 9)
def today(): return datetime.now(SHANGHAI).date()


def db_options(dbname=None):
    return dict(host=os.getenv('OFFICE_DB_SOCKET', '/private/tmp/office-biz-workflow-pg'),
                port=int(os.getenv('OFFICE_DB_PORT', '55433')),
                dbname=dbname or os.getenv('OFFICE_DB_NAME', 'office_demo_ready'),
                user=os.getenv('OFFICE_DB_USER', os.getenv('USER', 'WIN11')),
                row_factory=dict_row, connect_timeout=3)


def demo_mode():
    """Passwordless local teaching mode; explicit opt-out for authenticated use."""
    return os.getenv('OFFICE_DEMO_MODE', '1' if db_options()['dbname'] == 'office_demo_ready' else '0') == '1'


def make_password_hash(password):
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 300_000)
    return f'pbkdf2_sha256$300000${salt.hex()}${key.hex()}'


def check_password(password, stored):
    try:
        name, rounds, salt, expected = stored.split('$')
        if name != 'pbkdf2_sha256': return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def as_json(value):
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if isinstance(value, (date, datetime)) else str(value)
    raise TypeError(type(value).__name__)


class AppError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def required(data, key, maxlen=500):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maxlen:
        raise AppError(400, f'{key} 必填，且不得超过 {maxlen} 字')
    return value.strip()


def optional(data, key, maxlen=2000):
    value = data.get(key)
    if value is None or value == '': return None
    if not isinstance(value, str) or len(value) > maxlen: raise AppError(400, f'{key} 格式错误')
    return value.strip()


def day(data, key, mandatory=True):
    raw = required(data, key, 10) if mandatory else optional(data, key, 10)
    if not raw: return None
    try: return date.fromisoformat(raw)
    except ValueError as exc: raise AppError(400, f'{key} 日期错误') from exc


def fact_day(conn, data, key, mandatory=True):
    value = day(data, key, mandatory)
    if value is None:
        return None
    first = one(conn, 'SELECT min(demo_start) AS first FROM org.companies')['first']
    if value > today() or (first and value < first):
        raise AppError(400, f'{key} 须在演示经营周期内，且不能晚于今天')
    return value


def money(data, key, positive=False, default=None):
    raw = data.get(key, default)
    try:
        value = Decimal(str(raw))
        if not value.is_finite() or value != value.quantize(MONEY): raise InvalidOperation()
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AppError(400, f'{key} 须为两位小数金额') from exc
    if value < 0 or (positive and value == 0) or value > 999999999999:
        raise AppError(400, f'{key} 金额范围错误')
    return value


def identifier(prefix): return prefix + uuid4().hex[:14].upper()


def audit(conn, user, action, kind, entity_id, after, request_id=None):
    conn.execute('''INSERT INTO app.audit_events(actor_user_id,action,entity_type,entity_id,after_json,request_id)
                    VALUES(%s,%s,%s,%s,%s,%s)''',
                 (user['user_id'], action, kind, entity_id, json.dumps(after, default=as_json, ensure_ascii=False), request_id))


def has_role(user, *roles): return bool(set(user['roles']) & set(roles))


def authorize(user, *roles):
    if not has_role(user, *roles): raise AppError(403, '没有此操作权限')


def one(conn, sql, values=()): return conn.execute(sql, values).fetchone()
def many(conn, sql, values=()): return conn.execute(sql, values).fetchall()


def ensure_slots(conn, target_day):
    monday = target_day - timedelta(days=target_day.weekday())
    for offset in range(7):
        current = monday + timedelta(days=offset)
        if current > target_day: continue
        configured = one(conn, 'SELECT is_workday FROM app.calendar_days WHERE day=%s', (current,))
        if (configured and not configured['is_workday']) or (not configured and current.weekday() >= 5): continue
        code = f'{current.isocalendar().year}-W{current.isocalendar().week:02d}'
        conn.execute('''INSERT INTO work.daily_reports(employee_id,report_date,week_code,submit_status)
            SELECT employee_id,%s,%s,'未提交' FROM org.employees WHERE employment_status='在职'
            ON CONFLICT(employee_id,report_date) DO NOTHING''', (current, code))


def active_session(conn, headers):
    cookie = SimpleCookie()
    try: cookie.load(headers.get('Cookie', ''))
    except Exception: return None
    token = cookie.get('office_session')
    if not token: return None
    digest = hashlib.sha256(token.value.encode()).hexdigest()
    row = one(conn, '''SELECT u.user_id,u.username,u.employee_id,s.csrf_token,
        e.employee_name,e.department_id
        FROM app.sessions s JOIN app.users u USING(user_id)
        LEFT JOIN org.employees e USING(employee_id)
        WHERE s.token_hash=%s AND s.expires_at>now() AND u.active''', (digest,))
    if not row: return None
    row['roles'] = [r['role'] for r in many(conn, 'SELECT role FROM app.user_roles WHERE user_id=%s', (row['user_id'],))]
    row['token_hash'] = digest
    return row


def demo_user(conn, headers):
    route = headers.get('X-Demo-Role', '')
    accounts = {'owner': 'executive_demo', 'sales': 'sales_demo',
                'finance': 'finance_demo', 'work': 'employee_demo',
                'review': 'manager_demo'}
    username = accounts.get(route)
    if not username: return None
    row = one(conn, '''SELECT u.user_id,u.username,u.employee_id,e.employee_name,e.department_id
        FROM app.users u LEFT JOIN org.employees e USING(employee_id)
        WHERE u.username=%s AND u.active''', (username,))
    if not row: return None
    row['roles'] = [r['role'] for r in many(conn,
        'SELECT role FROM app.user_roles WHERE user_id=%s', (row['user_id'],))]
    row['csrf_token'] = ''
    return row


def public_user(user):
    return {key: user.get(key) for key in ('user_id','username','employee_id','employee_name','department_id','roles','csrf_token')}


def catalog(conn, user):
    result = {'departments': many(conn, 'SELECT department_id,department_name FROM org.departments ORDER BY department_name'),
              'products': many(conn, "SELECT sku,product_name,standard_price FROM catalog.products WHERE status IS DISTINCT FROM '停用' ORDER BY product_name")}
    if has_role(user, 'sales'):
        result['customers'] = many(conn, '''SELECT customer_id,customer_name FROM crm.customers
            WHERE owner_employee_id=%s ORDER BY customer_name''', (user['employee_id'],))
        result['opportunities'] = many(conn, '''SELECT opportunity_id,customer_id,stage,expected_amount FROM sales.opportunities
            WHERE salesperson_id=%s ORDER BY opened_on DESC LIMIT 200''', (user['employee_id'],))
        result['quotes'] = many(conn, '''SELECT quote_id,opportunity_id,customer_id,declared_amount,quote_status
            FROM sales.quotes WHERE salesperson_id=%s ORDER BY quoted_on DESC LIMIT 200''', (user['employee_id'],))
    if has_role(user, 'finance'):
        result['orders'] = many(conn, '''SELECT o.order_id,c.customer_name,o.signed_amount FROM sales.orders o
            JOIN crm.customers c USING(customer_id) ORDER BY o.signed_on DESC LIMIT 300''')
    return result


def lists(conn, user, kind):
    if kind == 'customers':
        authorize(user, 'sales')
        return many(conn, 'SELECT customer_id,customer_name,contact_name,region FROM crm.customers WHERE owner_employee_id=%s ORDER BY customer_id DESC LIMIT 100', (user['employee_id'],))
    if kind == 'followups':
        authorize(user, 'sales')
        return many(conn, '''SELECT f.followup_id,f.customer_id,c.customer_name,f.occurred_on,f.next_action
            FROM crm.followups f JOIN crm.customers c USING(customer_id)
            WHERE c.owner_employee_id=%s ORDER BY f.occurred_on DESC NULLS LAST LIMIT 100''', (user['employee_id'],))
    if kind == 'opportunities':
        authorize(user, 'sales')
        return many(conn, 'SELECT opportunity_id,customer_id,stage,expected_amount,opened_on FROM sales.opportunities WHERE salesperson_id=%s ORDER BY opened_on DESC LIMIT 100', (user['employee_id'],))
    if kind == 'quotes':
        authorize(user, 'sales')
        return many(conn, 'SELECT quote_id,opportunity_id,declared_amount,quoted_on FROM sales.quotes WHERE salesperson_id=%s ORDER BY quoted_on DESC LIMIT 100', (user['employee_id'],))
    if kind == 'orders':
        if has_role(user, 'finance'):
            return many(conn, 'SELECT order_id,customer_id,signed_amount,signed_on FROM sales.orders ORDER BY signed_on DESC LIMIT 100')
        authorize(user, 'sales')
        return many(conn, 'SELECT order_id,customer_id,signed_amount,signed_on FROM sales.orders WHERE salesperson_id=%s ORDER BY signed_on DESC LIMIT 100', (user['employee_id'],))
    if kind == 'receipts':
        authorize(user, 'finance')
        return many(conn, 'SELECT receipt_id,order_id,amount,received_on FROM finance.customer_receipts ORDER BY received_on DESC LIMIT 100')
    if kind == 'expenses':
        authorize(user, 'finance')
        return many(conn, 'SELECT expense_id,department_id,accrued_amount,paid_amount,occurred_on FROM finance.expenses ORDER BY occurred_on DESC LIMIT 100')
    raise AppError(404, '未知列表')


def dashboard(conn, user, params):
    authorize(user, 'executive', 'admin')
    ensure_slots(conn, today())
    raw = params.get('week', [None])[0]
    if raw:
        try: selected = date.fromisoformat(raw)
        except ValueError as exc: raise AppError(400, '经营周格式错误') from exc
        if selected.weekday(): raise AppError(400, '经营周必须为周一')
    else:
        selected = DEFAULT_DEMO_WEEK if demo_mode() else today() - timedelta(days=today().weekday())
    weekly = many(conn, 'SELECT * FROM analytics.v_weekly_kpis ORDER BY week_start DESC LIMIT 20')
    weekly.reverse()
    current = one(conn, 'SELECT * FROM analytics.v_weekly_kpis WHERE week_start=%s', (selected,))
    if not current: raise AppError(404, '经营周不存在')
    orders = many(conn, '''SELECT o.order_id,o.quote_id,o.opportunity_id,o.signed_on,o.signed_amount,o.delivery_status,
        o.notes,c.customer_name,c.source_marker
        FROM sales.orders o JOIN crm.customers c USING(customer_id)
        WHERE o.signed_on BETWEEN %s AND %s ORDER BY o.signed_on DESC LIMIT 100''', (selected, selected + timedelta(days=6)))
    receipts = many(conn, '''SELECT r.receipt_id,r.received_on,r.amount,r.order_id,c.customer_name,r.notes
        FROM finance.customer_receipts r JOIN crm.customers c USING(customer_id)
        WHERE r.received_on BETWEEN %s AND %s ORDER BY r.received_on DESC LIMIT 100''', (selected, selected + timedelta(days=6)))
    expenses = many(conn, '''SELECT x.expense_id,x.occurred_on,x.accrued_amount,x.paid_amount,
        x.expense_type,x.description,d.department_name
        FROM finance.expenses x JOIN org.departments d USING(department_id)
        WHERE x.occurred_on BETWEEN %s AND %s ORDER BY x.occurred_on DESC LIMIT 100''', (selected, selected + timedelta(days=6)))
    employees = many(conn, '''SELECT e.employee_id,e.employee_name,d.department_name,
        count(dr.*) AS expected_count,count(*) FILTER(WHERE dr.submit_status='已提交') AS submitted_count
        FROM org.employees e JOIN org.departments d USING(department_id)
        LEFT JOIN work.daily_reports dr ON dr.employee_id=e.employee_id AND dr.report_date BETWEEN %s AND %s
        GROUP BY e.employee_id,d.department_name ORDER BY d.department_name,e.employee_name''', (selected,selected+timedelta(days=6)))
    demo_reports = many(conn, '''SELECT e.employee_name,v.report_date,v.version_no,v.submit_state,
        rv.decision,rv.comment,v.version_id
        FROM app.daily_report_versions v JOIN org.employees e USING(employee_id)
        LEFT JOIN app.daily_report_reviews rv USING(version_id)
        WHERE v.report_date BETWEEN %s AND %s AND v.narrative LIKE '演示链路%%'
        ORDER BY v.report_date DESC,v.version_no DESC LIMIT 20''', (selected,selected+timedelta(days=6)))
    return dict(current=current,weekly=weekly,orders=orders,receipts=receipts,expenses=expenses,
                employees=employees,demo_reports=demo_reports)


def business_analysis(conn, user, params):
    authorize(user, 'executive', 'admin')
    ensure_slots(conn, today())
    raw = params.get('week', [None])[0]
    try:
        selected = date.fromisoformat(raw) if raw else (DEFAULT_DEMO_WEEK if demo_mode() else today() - timedelta(days=today().weekday()))
    except ValueError as exc:
        raise AppError(400, '经营周格式错误') from exc
    if selected.weekday(): raise AppError(400, '经营周必须为周一')
    result = analyze(conn, selected)
    if result is None: raise AppError(404, '经营周不存在')
    return result


def reports(conn, user, scope):
    ensure_slots(conn, today())
    if scope == 'mine':
        authorize(user, 'employee', 'manager', 'sales', 'finance')
        if not user['employee_id']: raise AppError(403, '账号未关联员工')
        where, args = 'r.employee_id=%s', (user['employee_id'],)
    elif scope == 'department':
        authorize(user, 'manager')
        where, args = 'e.department_id=%s', (user['department_id'],)
    else: raise AppError(400, 'scope 错误')
    return many(conn, f'''SELECT r.employee_id,e.employee_name,e.department_id,r.report_date,r.submit_status,
        COALESCE(v.narrative,r.narrative) AS narrative,
        COALESCE(v.completion_status,r.completion_status) AS completion_status,
        COALESCE(v.risk_note,r.risk_note) AS risk_note,
        COALESCE(v.next_plan,r.next_plan) AS next_plan,
        COALESCE(v.need_collaboration,r.need_collaboration) AS need_collaboration,
        v.version_id,v.version_no,v.submit_state,rv.decision,rv.comment
        FROM work.daily_reports r JOIN org.employees e USING(employee_id)
        LEFT JOIN LATERAL (SELECT * FROM app.daily_report_versions v WHERE v.employee_id=r.employee_id
            AND v.report_date=r.report_date ORDER BY v.version_no DESC LIMIT 1) v ON TRUE
        LEFT JOIN app.daily_report_reviews rv ON rv.version_id=v.version_id
        WHERE {where} AND r.report_date BETWEEN CURRENT_DATE-INTERVAL '21 days' AND CURRENT_DATE+INTERVAL '7 days'
        ORDER BY r.report_date DESC,e.employee_name LIMIT 300''', args)


def admin_data(conn, user):
    authorize(user, 'admin')
    return {
        'users': many(conn, '''SELECT u.user_id,u.username,u.employee_id,u.active,
            COALESCE(e.employee_name,'系统管理员') AS employee_name,
            COALESCE(array_agg(r.role) FILTER(WHERE r.role IS NOT NULL),'{}') AS roles
            FROM app.users u LEFT JOIN org.employees e USING(employee_id)
            LEFT JOIN app.user_roles r USING(user_id)
            GROUP BY u.user_id,e.employee_name ORDER BY u.username'''),
        'employees': many(conn, '''SELECT e.employee_id,e.employee_name,d.department_name
            FROM org.employees e JOIN org.departments d USING(department_id)
            WHERE e.employment_status='在职' ORDER BY d.department_name,e.employee_name'''),
        'calendar': many(conn, '''SELECT day,is_workday,note FROM app.calendar_days
            WHERE day BETWEEN CURRENT_DATE-INTERVAL '30 days' AND CURRENT_DATE+INTERVAL '60 days'
            ORDER BY day''')
    }


def write_resource(conn, user, path, data, request_id):
    if path == '/api/admin/users':
        authorize(user,'admin')
        username=required(data,'username',100)
        employee_id=required(data,'employee_id',40)
        roles=data.get('roles')
        if not isinstance(roles,list) or not roles or not set(roles)<={'sales','finance','employee','manager','executive'}:
            raise AppError(400,'岗位角色错误')
        if not one(conn,"SELECT 1 FROM org.employees WHERE employee_id=%s AND employment_status='在职'",(employee_id,)):
            raise AppError(400,'员工不存在或未在职')
        if 'manager' in roles and not one(conn,'SELECT 1 FROM org.departments WHERE leader_employee_id=%s',(employee_id,)):
            raise AppError(400,'只有部门档案中的负责人可授予审核角色')
        existing=one(conn,'SELECT user_id,username FROM app.users WHERE employee_id=%s',(employee_id,))
        if existing and existing['username']!=username: raise AppError(409,'该员工已有账号')
        if one(conn,'SELECT 1 FROM app.users WHERE username=%s',(username,)):
            raise AppError(409,'用户名已存在')
        password=secrets.token_urlsafe(18)
        entity=identifier('U')
        conn.execute('INSERT INTO app.users(user_id,username,password_hash,employee_id) VALUES(%s,%s,%s,%s)',
                     (entity,username,make_password_hash(password),employee_id))
        for role in sorted(set(roles)):
            conn.execute('INSERT INTO app.user_roles(user_id,role) VALUES(%s,%s)',(entity,role))
        row={'user_id':entity,'username':username,'employee_id':employee_id,'roles':roles}
        audit(conn,user,'create','app.users',entity,row,request_id)
        return {'id':entity,'record':row,'initial_password':password}
    if path == '/api/admin/calendar':
        authorize(user,'admin')
        calendar_day=day(data,'day')
        workday=data.get('is_workday')
        if not isinstance(workday,bool): raise AppError(400,'工作日状态错误')
        note=optional(data,'note',200)
        conn.execute('''INSERT INTO app.calendar_days(day,is_workday,note,configured_by)
            VALUES(%s,%s,%s,%s) ON CONFLICT(day) DO UPDATE SET is_workday=EXCLUDED.is_workday,
            note=EXCLUDED.note,configured_by=EXCLUDED.configured_by,configured_at=now()''',
            (calendar_day,workday,note,user['user_id']))
        if not workday:
            conn.execute('''DELETE FROM work.daily_reports dr WHERE dr.report_date=%s AND dr.submit_status='未提交'
                AND NOT EXISTS (SELECT 1 FROM app.daily_report_versions v WHERE v.employee_id=dr.employee_id AND v.report_date=dr.report_date)''',
                (calendar_day,))
        else:
            code=f'{calendar_day.isocalendar().year}-W{calendar_day.isocalendar().week:02d}'
            conn.execute('''INSERT INTO work.daily_reports(employee_id,report_date,week_code,submit_status)
                SELECT employee_id,%s,%s,'未提交' FROM org.employees WHERE employment_status='在职'
                ON CONFLICT(employee_id,report_date) DO NOTHING''',(calendar_day,code))
        entity=calendar_day.isoformat()
        row={'day':entity,'is_workday':workday,'note':note}
        audit(conn,user,'configure','app.calendar_days',entity,row,request_id)
        return {'id':entity,'record':row}
    if path == '/api/followups':
        authorize(user,'sales')
        customer_id=required(data,'customer_id',40)
        if not one(conn,'SELECT 1 FROM crm.customers WHERE customer_id=%s AND owner_employee_id=%s',(customer_id,user['employee_id'])):
            raise AppError(403,'客户不属于当前销售')
        entity=identifier('FU')
        row=dict(followup_id=entity,customer_id=customer_id,occurred_on=fact_day(conn,data,'occurred_on'),
                 contact_method=required(data,'contact_method',100),customer_needs=optional(data,'customer_needs',1000),
                 obstacle=optional(data,'obstacle',1000),next_action=optional(data,'next_action',1000),
                 review_state='待复核',source_locator='web_form')
        conn.execute('''INSERT INTO crm.followups(followup_id,customer_id,occurred_on,contact_method,customer_needs,obstacle,next_action,review_state,source_locator)
            VALUES(%(followup_id)s,%(customer_id)s,%(occurred_on)s,%(contact_method)s,%(customer_needs)s,%(obstacle)s,%(next_action)s,%(review_state)s,%(source_locator)s)''',row)
    elif path == '/api/customers':
        authorize(user, 'sales')
        entity = identifier('C')
        row = dict(customer_id=entity, customer_name=required(data,'customer_name',200),
                   contact_name=optional(data,'contact_name',100), region=optional(data,'region',100),
                   historical_interest=optional(data,'historical_interest',1000),
                   owner_employee_id=user['employee_id'], source_marker='web_form')
        conn.execute('''INSERT INTO crm.customers(customer_id,customer_name,contact_name,region,historical_interest,owner_employee_id,source_marker)
            VALUES(%(customer_id)s,%(customer_name)s,%(contact_name)s,%(region)s,%(historical_interest)s,%(owner_employee_id)s,%(source_marker)s)''', row)
    elif path == '/api/opportunity-stage':
        authorize(user,'sales')
        opportunity_id=required(data,'opportunity_id',40)
        current=one(conn,'SELECT opportunity_id,closed_on FROM sales.opportunities WHERE opportunity_id=%s AND salesperson_id=%s',
                    (opportunity_id,user['employee_id']))
        if not current: raise AppError(403,'商机不属于当前销售')
        if current['closed_on']: raise AppError(409,'已结案商机不能修改阶段')
        stage=required(data,'stage',50)
        if stage not in ('初步沟通','需求确认','方案报价','谈判中','关闭输单'):
            raise AppError(400,'商机阶段错误')
        if stage=='关闭输单':
            conn.execute("UPDATE sales.opportunities SET stage=%s,closed_on=%s,close_result='输单' WHERE opportunity_id=%s",
                         (stage,today(),opportunity_id))
        else:
            conn.execute('UPDATE sales.opportunities SET stage=%s WHERE opportunity_id=%s',(stage,opportunity_id))
        entity=opportunity_id
        row={'opportunity_id':entity,'stage':stage,'closed_on':today().isoformat() if stage=='关闭输单' else None}
    elif path == '/api/opportunities':
        authorize(user, 'sales')
        customer_id = required(data,'customer_id',40)
        if not one(conn,'SELECT 1 FROM crm.customers WHERE customer_id=%s AND owner_employee_id=%s', (customer_id,user['employee_id'])):
            raise AppError(403,'客户不属于当前销售')
        entity = identifier('OP')
        row = dict(opportunity_id=entity,customer_id=customer_id,salesperson_id=user['employee_id'],
                   opened_on=fact_day(conn,data,'opened_on'),expected_close_on=day(data,'expected_close_on',False),
                   stage=required(data,'stage',50),expected_amount=money(data,'expected_amount',default='0'),
                   lead_source=optional(data,'lead_source',100),notes=optional(data,'notes',1000))
        if row['expected_close_on'] and row['expected_close_on'] < row['opened_on']:
            raise AppError(400,'预计成交日不能早于商机开启日')
        conn.execute('''INSERT INTO sales.opportunities(opportunity_id,customer_id,salesperson_id,opened_on,expected_close_on,stage,expected_amount,lead_source,notes)
            VALUES(%(opportunity_id)s,%(customer_id)s,%(salesperson_id)s,%(opened_on)s,%(expected_close_on)s,%(stage)s,%(expected_amount)s,%(lead_source)s,%(notes)s)''', row)
    elif path == '/api/quotes':
        authorize(user, 'sales')
        opportunity_id=required(data,'opportunity_id',40)
        opportunity=one(conn,'SELECT * FROM sales.opportunities WHERE opportunity_id=%s AND salesperson_id=%s', (opportunity_id,user['employee_id']))
        if not opportunity: raise AppError(403,'商机不属于当前销售')
        lines=data.get('lines')
        if not isinstance(lines,list) or not 1<=len(lines)<=30: raise AppError(400,'报价需要 1 至 30 条商品明细')
        entity=identifier('Q')
        total=Decimal('0')
        normalized=[]
        for item in lines:
            if not isinstance(item,dict): raise AppError(400,'商品明细错误')
            sku=required(item,'sku',60)
            product=one(conn,'SELECT standard_price FROM catalog.products WHERE sku=%s', (sku,))
            if not product: raise AppError(400,'商品不存在')
            try:
                quantity=Decimal(str(item.get('quantity')))
                discount=Decimal(str(item.get('discount_rate','0')))
            except InvalidOperation as exc: raise AppError(400,'数量或折扣错误') from exc
            if not quantity.is_finite() or quantity<=0 or quantity>100000 or quantity.as_tuple().exponent < -3 or not discount.is_finite() or not 0<=discount<=1:
                raise AppError(400,'数量或折扣范围错误')
            price=Decimal(product['standard_price'])
            unit=(price*(1-discount)).quantize(MONEY,ROUND_HALF_UP)
            amount=(unit*quantity).quantize(MONEY,ROUND_HALF_UP)
            total+=amount
            normalized.append((identifier('QL'),entity,sku,quantity,price,discount,unit,amount))
        row=dict(quote_id=entity,opportunity_id=opportunity_id,customer_id=opportunity['customer_id'],
                 salesperson_id=user['employee_id'],quoted_on=fact_day(conn,data,'quoted_on'),
                 valid_until=day(data,'valid_until',False),quote_status='已报价',declared_amount=total,
                 notes=optional(data,'notes',1000))
        if row['quoted_on'] < opportunity['opened_on']:
            raise AppError(400,'报价日不能早于商机开启日')
        if row['valid_until'] and row['valid_until'] < row['quoted_on']:
            raise AppError(400,'报价有效期不能早于报价日')
        conn.execute('''INSERT INTO sales.quotes(quote_id,opportunity_id,customer_id,salesperson_id,quoted_on,valid_until,quote_status,declared_amount,notes)
            VALUES(%(quote_id)s,%(opportunity_id)s,%(customer_id)s,%(salesperson_id)s,%(quoted_on)s,%(valid_until)s,%(quote_status)s,%(declared_amount)s,%(notes)s)''',row)
        conn.cursor().executemany('''INSERT INTO sales.quote_lines(quote_line_id,quote_id,sku,quantity,standard_price,discount_rate,unit_price,line_amount)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',normalized)
    elif path == '/api/orders':
        authorize(user, 'sales')
        quote_id=required(data,'quote_id',40)
        quote=one(conn,'SELECT * FROM sales.quotes WHERE quote_id=%s AND salesperson_id=%s', (quote_id,user['employee_id']))
        if not quote: raise AppError(403,'报价不属于当前销售')
        if one(conn,'SELECT 1 FROM sales.orders WHERE quote_id=%s',(quote_id,)):
            raise AppError(409,'该报价已有订单')
        lines=many(conn,'SELECT * FROM sales.quote_lines WHERE quote_id=%s ORDER BY quote_line_id',(quote_id,))
        if not lines: raise AppError(400,'报价没有商品明细')
        entity=identifier('SO')
        signed_on=fact_day(conn,data,'signed_on')
        if signed_on < quote['quoted_on']:
            raise AppError(400,'订单签订日不能早于报价日')
        if quote['valid_until'] and signed_on>quote['valid_until']:
            raise AppError(400,'报价已过有效期')
        row=dict(order_id=entity,opportunity_id=quote['opportunity_id'],quote_id=quote_id,
                 customer_id=quote['customer_id'],salesperson_id=user['employee_id'],
                 signed_on=signed_on,delivery_status='待交付',
                 signed_amount=sum((line['line_amount'] for line in lines),Decimal('0')),
                 notes=optional(data,'notes',1000))
        conn.execute('''INSERT INTO sales.orders(order_id,opportunity_id,quote_id,customer_id,salesperson_id,signed_on,delivery_status,signed_amount,notes)
            VALUES(%(order_id)s,%(opportunity_id)s,%(quote_id)s,%(customer_id)s,%(salesperson_id)s,%(signed_on)s,%(delivery_status)s,%(signed_amount)s,%(notes)s)''',row)
        conn.cursor().executemany('''INSERT INTO sales.order_lines(order_line_id,order_id,sku,quantity,standard_price,discount_rate,sales_amount)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',[(identifier('OL'),entity,l['sku'],l['quantity'],l['standard_price'],l['discount_rate'],l['line_amount']) for l in lines])
        conn.execute("UPDATE sales.quotes SET quote_status='已成交' WHERE quote_id=%s",(quote_id,))
        conn.execute("UPDATE sales.opportunities SET stage='已成交',closed_on=%s,close_result='赢单' WHERE opportunity_id=%s AND closed_on IS NULL",
                     (signed_on,quote['opportunity_id']))
    elif path == '/api/receipts':
        authorize(user, 'finance')
        order_id=required(data,'order_id',40)
        order=one(conn,'SELECT customer_id,salesperson_id FROM sales.orders WHERE order_id=%s',(order_id,))
        if not order: raise AppError(400,'订单不存在')
        entity=identifier('RC')
        row=dict(receipt_id=entity,order_id=order_id,customer_id=order['customer_id'],
                 salesperson_id=order['salesperson_id'],received_on=fact_day(conn,data,'received_on'),
                 amount=money(data,'amount',positive=True),payment_method=optional(data,'payment_method',100),
                 receipt_type=optional(data,'receipt_type',100),notes=optional(data,'notes',1000))
        conn.execute('''INSERT INTO finance.customer_receipts(receipt_id,order_id,customer_id,salesperson_id,received_on,amount,payment_method,receipt_type,notes)
            VALUES(%(receipt_id)s,%(order_id)s,%(customer_id)s,%(salesperson_id)s,%(received_on)s,%(amount)s,%(payment_method)s,%(receipt_type)s,%(notes)s)''',row)
        voucher=required(data,'voucher_ref',100)
        conn.execute("INSERT INTO app.finance_evidence(entity_kind,entity_id,voucher_ref,created_by) VALUES('receipt',%s,%s,%s)",
                     (entity,voucher,user['user_id']))
        row['voucher_ref']=voucher
    elif path == '/api/expenses':
        authorize(user, 'finance')
        department_id=required(data,'department_id',40)
        if not one(conn,'SELECT 1 FROM org.departments WHERE department_id=%s',(department_id,)):
            raise AppError(400,'部门不存在')
        paid=money(data,'paid_amount',default='0')
        paid_on=fact_day(conn,data,'paid_on',False)
        if paid>0 and not paid_on: raise AppError(400,'已付款须填写付款日')
        entity=identifier('EX')
        accrued=money(data,'accrued_amount',positive=True)
        if paid>accrued: raise AppError(400,'实付金额不能超过发生金额')
        row=dict(expense_id=entity,department_id=department_id,
                 handled_by_employee_id=user['employee_id'],occurred_on=fact_day(conn,data,'occurred_on'),
                 expense_type=required(data,'expense_type',100),description=optional(data,'description',1000),
                 accrued_amount=accrued,paid_on=paid_on,
                 paid_amount=paid,payment_status='未支付' if paid==0 else '已支付' if paid==accrued else '部分支付')
        conn.execute('''INSERT INTO finance.expenses(expense_id,department_id,handled_by_employee_id,occurred_on,expense_type,description,accrued_amount,paid_on,paid_amount,payment_status)
            VALUES(%(expense_id)s,%(department_id)s,%(handled_by_employee_id)s,%(occurred_on)s,%(expense_type)s,%(description)s,%(accrued_amount)s,%(paid_on)s,%(paid_amount)s,%(payment_status)s)''',row)
        voucher=required(data,'voucher_ref',100)
        conn.execute("INSERT INTO app.finance_evidence(entity_kind,entity_id,voucher_ref,created_by) VALUES('expense',%s,%s,%s)",
                     (entity,voucher,user['user_id']))
        row['voucher_ref']=voucher
    elif path == '/api/reports':
        authorize(user, 'employee','sales','finance','manager')
        if not user['employee_id']: raise AppError(403,'账号未关联员工')
        report_date=day(data,'report_date')
        if report_date>today() or (today()-report_date).days>21:
            raise AppError(400,'仅可填写今日及此前 21 天内日报')
        configured=one(conn,'SELECT is_workday FROM app.calendar_days WHERE day=%s',(report_date,))
        if report_date.weekday()>4 and not (configured and configured['is_workday']):
            raise AppError(400,'非工作日需管理员先配置')
        if configured and not configured['is_workday']: raise AppError(400,'该日期配置为休息日')
        current=one(conn,'''SELECT v.version_id,v.submit_state,rv.decision FROM app.daily_report_versions v
            LEFT JOIN app.daily_report_reviews rv ON rv.version_id=v.version_id
            WHERE v.employee_id=%s AND v.report_date=%s ORDER BY v.version_no DESC LIMIT 1''',(user['employee_id'],report_date))
        if current and current['decision']=='通过': raise AppError(409,'已审核日报不能覆盖')
        if current and current['submit_state']=='已提交' and current['decision'] is None:
            raise AppError(409,'日报正在等待审核，暂不能修改')
        state=required(data,'submit_state',10)
        if state not in ('草稿','已提交'): raise AppError(400,'提交状态错误')
        narrative=optional(data,'narrative',4000)
        if state=='已提交' and not narrative: raise AppError(400,'提交日报须填写工作内容')
        week_code=f'{report_date.isocalendar().year}-W{report_date.isocalendar().week:02d}'
        conn.execute('''INSERT INTO work.daily_reports(employee_id,report_date,week_code,submit_status)
            VALUES(%s,%s,%s,'未提交') ON CONFLICT(employee_id,report_date) DO NOTHING''',(user['employee_id'],report_date,week_code))
        conn.execute('''UPDATE work.daily_reports SET submit_status=%s,narrative=%s,completion_status=%s,
            risk_note=%s,next_plan=%s,need_collaboration=%s WHERE employee_id=%s AND report_date=%s''',
            ('已提交' if state=='已提交' else '未提交', narrative if state=='已提交' else None,
             optional(data,'completion_status',500),optional(data,'risk_note',1000),
             optional(data,'next_plan',1000),optional(data,'need_collaboration',1000),user['employee_id'],report_date))
        version=one(conn,'SELECT COALESCE(MAX(version_no),0)+1 AS n FROM app.daily_report_versions WHERE employee_id=%s AND report_date=%s',(user['employee_id'],report_date))['n']
        entity=identifier('RV')
        row=dict(version_id=entity,employee_id=user['employee_id'],report_date=report_date,
                 version_no=version,narrative=narrative,completion_status=optional(data,'completion_status',500),
                 risk_note=optional(data,'risk_note',1000),next_plan=optional(data,'next_plan',1000),
                 need_collaboration=optional(data,'need_collaboration',1000),submit_state=state,
                 created_by=user['user_id'])
        conn.execute('''INSERT INTO app.daily_report_versions(version_id,employee_id,report_date,version_no,narrative,completion_status,risk_note,next_plan,need_collaboration,submit_state,created_by)
            VALUES(%(version_id)s,%(employee_id)s,%(report_date)s,%(version_no)s,%(narrative)s,%(completion_status)s,%(risk_note)s,%(next_plan)s,%(need_collaboration)s,%(submit_state)s,%(created_by)s)''',row)
    elif path == '/api/reviews':
        authorize(user,'manager')
        version_id=required(data,'version_id',40)
        decision=required(data,'decision',10)
        if decision not in ('通过','退回'): raise AppError(400,'审核结果错误')
        report=one(conn,'''SELECT v.*,e.department_id FROM app.daily_report_versions v
            JOIN org.employees e ON e.employee_id=v.employee_id WHERE v.version_id=%s''',(version_id,))
        if not report or report['department_id']!=user['department_id']:
            raise AppError(403,'不可审核其他部门日报')
        if report['employee_id']==user['employee_id']: raise AppError(403,'不可审核本人日报')
        if report['submit_state']!='已提交': raise AppError(400,'仅已提交日报可审核')
        latest=one(conn,'SELECT version_id FROM app.daily_report_versions WHERE employee_id=%s AND report_date=%s ORDER BY version_no DESC LIMIT 1',(report['employee_id'],report['report_date']))
        if latest['version_id']!=version_id: raise AppError(409,'日报已有新版本')
        if one(conn,'SELECT 1 FROM app.daily_report_reviews WHERE version_id=%s',(version_id,)):
            raise AppError(409,'该版本已审核')
        comment=optional(data,'comment',1000)
        if decision=='退回' and not comment: raise AppError(400,'退回须填写原因')
        entity=identifier('RR')
        row=dict(review_id=entity,version_id=version_id,reviewer_user_id=user['user_id'],decision=decision,comment=comment)
        conn.execute('''INSERT INTO app.daily_report_reviews(review_id,version_id,reviewer_user_id,decision,comment)
            VALUES(%(review_id)s,%(version_id)s,%(reviewer_user_id)s,%(decision)s,%(comment)s)''',row)
    else:
        raise AppError(404,'未知写入接口')
    action='review' if path=='/api/reviews' else 'update' if path=='/api/opportunity-stage' else 'create'
    audit(conn,user,action,path,entity,row,request_id)
    return {'id':entity,'record':row}


class Handler(BaseHTTPRequestHandler):
    server_version='OfficeWorkflow/1.0'

    def respond(self,status,payload,cookie=None):
        body=json.dumps(payload,ensure_ascii=False,default=as_json).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'")
        if cookie: self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self,path):
        files={'/':'index.html','/login':'index.html','/owner':'index.html','/sales':'index.html',
               '/finance':'index.html','/work':'index.html','/review':'index.html','/admin':'index.html',
               '/index.html':'index.html','/app.js':'app.js','/style.css':'style.css'}
        name=files.get(path)
        if not name:
            self.send_error(404); return
        content=(HERE/'public'/name).read_bytes()
        mime='text/html; charset=utf-8' if name.endswith('html') else 'text/javascript; charset=utf-8' if name.endswith('js') else 'text/css; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type',mime)
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'")
        self.send_header('Content-Length',str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def body(self):
        try: size=int(self.headers.get('Content-Length','0'))
        except ValueError as exc: raise AppError(400,'Content-Length 格式错误') from exc
        if size<=0 or size>1_000_000: raise AppError(413,'请求体大小错误')
        try: value=json.loads(self.rfile.read(size))
        except (ValueError,UnicodeDecodeError) as exc: raise AppError(400,'JSON 格式错误') from exc
        if not isinstance(value,dict): raise AppError(400,'请求体须为对象')
        return value

    def handle_api(self,method):
        path=urlsplit(self.path).path
        params=parse_qs(urlsplit(self.path).query)
        try:
            with psycopg.connect(**db_options()) as conn:
                if path=='/api/login' and method=='POST' and not demo_mode():
                    data=self.body()
                    username=required(data,'username',100)
                    password=required(data,'password',200)
                    row=one(conn,'SELECT * FROM app.users WHERE username=%s AND active',(username,))
                    if not row or not check_password(password,row['password_hash']):
                        raise AppError(401,'用户名或密码错误')
                    token=secrets.token_urlsafe(32)
                    csrf=secrets.token_urlsafe(24)
                    conn.execute('INSERT INTO app.sessions(token_hash,user_id,csrf_token,expires_at) VALUES(%s,%s,%s,now()+interval \'12 hours\')',
                                 (hashlib.sha256(token.encode()).hexdigest(),row['user_id'],csrf))
                    user=active_session(conn,{'Cookie':f'office_session={token}'})
                    conn.commit()
                    self.respond(200,{'data':public_user(user)},f'office_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')
                    return
                user=demo_user(conn,self.headers) if demo_mode() else active_session(conn,self.headers)
                if not user: raise AppError(401,'演示入口不可用' if demo_mode() else '请先登录')
                if method=='POST':
                    origin=self.headers.get('Origin')
                    expected=f'http://127.0.0.1:{self.server.server_port}'
                    if origin and origin!=expected: raise AppError(403,'请求来源不匹配')
                    if not demo_mode() and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),user['csrf_token']):
                        raise AppError(403,'CSRF 校验失败')
                if path=='/api/logout' and method=='POST' and not demo_mode():
                    conn.execute('DELETE FROM app.sessions WHERE token_hash=%s',(user['token_hash'],))
                    conn.commit()
                    self.respond(200,{'data':True},'office_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                    return
                if path=='/api/assistant/chat' and method=='POST':
                    authorize(user,'executive','admin')
                    data=self.body()
                    raw=data.get('week')
                    try: selected=date.fromisoformat(raw) if raw else (DEFAULT_DEMO_WEEK if demo_mode() else today()-timedelta(days=today().weekday()))
                    except (ValueError,TypeError) as exc: raise AppError(400,'数据周期格式错误') from exc
                    try: result=business_chat(conn,data.get('question'),selected,
                                              data.get('scope','week'),data.get('history',[]))
                    except ChatError as exc: raise AppError(exc.status,exc.message) from exc
                    self.respond(200,{'data':result})
                    return
                if method=='GET':
                    if path=='/api/me': result=public_user(user)
                    elif path=='/api/catalog': result=catalog(conn,user)
                    elif path=='/api/list': result=lists(conn,user,params.get('kind',[''])[0])
                    elif path=='/api/reports': result=reports(conn,user,params.get('scope',['mine'])[0])
                    elif path=='/api/dashboard': result=dashboard(conn,user,params)
                    elif path=='/api/dashboard/analysis': result=business_analysis(conn,user,params)
                    elif path.startswith('/api/dashboard/employees/') and path.endswith('/summary'):
                        authorize(user, 'executive', 'admin')
                        employee_id=path[len('/api/dashboard/employees/'):-len('/summary')].strip('/')
                        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',employee_id): raise AppError(400,'员工编号错误')
                        raw=params.get('week',[None])[0]
                        try: selected=date.fromisoformat(raw) if raw else (DEFAULT_DEMO_WEEK if demo_mode() else today()-timedelta(days=today().weekday()))
                        except ValueError as exc: raise AppError(400,'数据周期格式错误') from exc
                        if selected.weekday() or selected>today(): raise AppError(400,'数据周期必须为已开始的周一')
                        result=get_summary(conn, db_options, employee_id, selected)
                        if result is None: raise AppError(404,'员工不存在')
                    elif path=='/api/admin': result=admin_data(conn,user)
                    else: raise AppError(404,'接口不存在')
                else:
                    data=self.body()
                    request_id=self.headers.get('Idempotency-Key','')
                    if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',request_id):
                        raise AppError(400,'缺少有效的幂等请求键')
                    request_hash=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                    prior=one(conn,'''SELECT result_json,request_hash FROM app.idempotency_keys WHERE user_id=%s AND operation=%s AND request_key=%s''',
                              (user['user_id'],path,request_id))
                    if prior:
                        if prior['request_hash'] and prior['request_hash']!=request_hash:
                            raise AppError(409,'幂等键已用于不同内容')
                        self.respond(200,{'data':prior['result_json'],'replayed':True});return
                    result=write_resource(conn,user,path,data,request_id)
                    if path!='/api/admin/users':
                        conn.execute('''INSERT INTO app.idempotency_keys(user_id,operation,request_key,request_hash,result_json)
                            VALUES(%s,%s,%s,%s,%s)''',(user['user_id'],path,request_id,request_hash,json.dumps(result,default=as_json,ensure_ascii=False)))
                conn.commit()
                self.respond(200,{'data':result})
        except AppError as exc: self.respond(exc.status,{'error':exc.message})
        except psycopg.errors.UniqueViolation: self.respond(409,{'error':'记录已存在，请刷新后重试'})
        except psycopg.errors.ForeignKeyViolation: self.respond(400,{'error':'关联记录不存在'})
        except psycopg.errors.CheckViolation: self.respond(400,{'error':'字段违反数据库约束'})
        except psycopg.DataError: self.respond(400,{'error':'字段类型或范围错误'})
        except psycopg.Error: self.respond(503,{'error':'数据库暂不可用'})

    def do_GET(self):
        path=urlsplit(self.path).path
        if path.startswith('/api/'):
            self.handle_api('GET')
        else: self.serve_file(path)

    def do_POST(self): self.handle_api('POST')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18766)
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'Workflow ready at http://127.0.0.1:{args.port}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=='__main__': main()
