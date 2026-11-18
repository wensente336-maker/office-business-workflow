#!/usr/bin/env python3
"""Create an isolated operational copy and apply the workflow migration."""
import argparse
import getpass
import os
import re
from pathlib import Path

import psycopg
from psycopg import sql

from server import db_options, make_password_hash

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE_DIR = Path('/Users/FDE 商业模式/AI企业经营模拟实验室_学习备忘录与完整数据资料包_2026-11-17/统一经营数据库_V1.0')
SOURCE_SCHEMA = SOURCE_DIR / '01_schema_postgresql.sql'


def dynamic_views():
    source = SOURCE_SCHEMA.read_text(encoding='utf-8')
    section = source[source.index('CREATE OR REPLACE VIEW finance.v_cash_ledger AS'):]
    section = section.replace('generate_series(c.demo_start,c.demo_end,',
                              'generate_series(c.demo_start,GREATEST(c.demo_end,CURRENT_DATE),')
    return section


def import_source():
    """Load the published teaching CSV set into a fresh isolated database."""
    with psycopg.connect(**db_options()) as conn:
        conn.execute(SOURCE_SCHEMA.read_text(encoding='utf-8'))
        conn.execute('SET CONSTRAINTS ALL DEFERRED')
        for line in (SOURCE_DIR / '03_load_psql.sql').read_text(encoding='utf-8').splitlines():
            match = re.match(r"\\copy\s+(.+?)\s+FROM\s+'(data/[^']+)'\s+WITH\s+\(FORMAT csv,HEADER true,NULL '\\N'\)", line)
            if not match:
                continue
            target, file_name = match.groups()
            with conn.cursor().copy(f"COPY {target} FROM STDIN WITH (FORMAT csv, HEADER true, NULL '\\N')") as copy:
                copy.write((SOURCE_DIR / file_name).read_bytes())
        print('Imported original teaching CSV data into isolated database')


def provision_writer(conn):
    conn.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='office_demo_writer') THEN CREATE ROLE office_demo_writer LOGIN; END IF; END $$")
    database = db_options()['dbname']
    conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO office_demo_writer').format(sql.Identifier(database)))
    conn.execute('GRANT USAGE ON SCHEMA org,catalog,crm,sales,ops,finance,work,analytics,app TO office_demo_writer')
    conn.execute('''GRANT SELECT ON org.companies,org.departments,org.employees,catalog.categories,catalog.products,catalog.suppliers,
        crm.customers,crm.followups,crm.ai_assessments,sales.opportunities,sales.quotes,sales.quote_lines,
        sales.orders,sales.order_lines,ops.purchase_orders,ops.purchase_lines,ops.deliveries,
        finance.customer_receipts,finance.supplier_payments,finance.expenses,
        work.daily_reports,work.activity_events,analytics.v_weekly_kpis TO office_demo_writer''')
    conn.execute('''GRANT INSERT ON crm.customers,crm.followups,sales.opportunities,sales.quotes,
        sales.quote_lines,sales.orders,sales.order_lines,finance.customer_receipts,
        finance.expenses,work.daily_reports TO office_demo_writer''')
    conn.execute('GRANT UPDATE ON sales.opportunities,sales.quotes,work.daily_reports TO office_demo_writer')
    conn.execute('GRANT DELETE ON work.daily_reports TO office_demo_writer')
    conn.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA app TO office_demo_writer')
    conn.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA app TO office_demo_writer')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--create-db', action='store_true', help='Clone the accepted teaching database once')
    parser.add_argument('--admin', action='store_true', help='Create/reset local admin login interactively')
    parser.add_argument('--demo-accounts', action='store_true', help='Create local teaching role logins interactively')
    parser.add_argument('--user', help='Create/reset login for an existing employee ID')
    parser.add_argument('--username', help='Username for --user')
    parser.add_argument('--roles', help='Comma-separated roles for --user')
    args = parser.parse_args()
    new_empty = False
    if args.create_db:
        target_database = db_options()['dbname']
        with psycopg.connect(**db_options('postgres'), autocommit=True) as conn:
            exists = conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', (target_database,)).fetchone()
            if not exists:
                accepted = conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', ('office_demo_acceptance',)).fetchone()
                if accepted:
                    conn.execute(sql.SQL('CREATE DATABASE {} TEMPLATE {}').format(
                        sql.Identifier(target_database), sql.Identifier('office_demo_acceptance')))
                    print('Cloned accepted teaching database')
                else:
                    conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target_database)))
                    new_empty = True
                    print('Created isolated empty operational database')
    if new_empty:
        import_source()
    with psycopg.connect(**db_options()) as conn:
        conn.execute((HERE / 'migrate.sql').read_text(encoding='utf-8'))
        conn.execute(dynamic_views())
        provision_writer(conn)
        if args.admin or args.user or args.demo_accounts:
            password = getpass.getpass('New password (minimum 12 characters): ')
            if len(password) < 12:
                raise SystemExit('Password must have at least 12 characters')
            if args.demo_accounts:
                accounts = [('admin',None,['admin','executive']),
                            ('sales_demo','E003',['sales','employee']),
                            ('finance_demo','E007',['finance','employee']),
                            ('employee_demo','E009',['employee']),
                            ('manager_demo','E008',['manager','employee']),
                            ('executive_demo','E001',['executive'])]
            elif args.admin:
                accounts = [('admin',None,['admin','executive'])]
            else:
                if not args.username or not args.roles:
                    raise SystemExit('--user needs --username and --roles')
                roles = args.roles.split(',')
                if not set(roles) <= {'sales','finance','employee','manager','executive'}:
                    raise SystemExit('Invalid role')
                employee_id = args.user
                if not conn.execute('SELECT 1 FROM org.employees WHERE employee_id=%s', (employee_id,)).fetchone():
                    raise SystemExit('Employee does not exist')
                accounts = [(args.username,employee_id,roles)]
            from uuid import uuid4
            for username,employee_id,roles in accounts:
                user_id = 'U' + uuid4().hex[:16]
                if employee_id:
                    conn.execute('''INSERT INTO app.users(user_id,username,password_hash,employee_id)
                        VALUES(%s,%s,%s,%s) ON CONFLICT(employee_id) DO UPDATE
                        SET username=EXCLUDED.username,password_hash=EXCLUDED.password_hash,active=TRUE''',
                        (user_id, username, make_password_hash(password), employee_id))
                else:
                    conn.execute('''INSERT INTO app.users(user_id,username,password_hash,employee_id)
                        VALUES(%s,%s,%s,%s) ON CONFLICT(username) DO UPDATE
                        SET password_hash=EXCLUDED.password_hash,employee_id=EXCLUDED.employee_id,active=TRUE''',
                        (user_id, username, make_password_hash(password), employee_id))
                actual_id = conn.execute('SELECT user_id FROM app.users WHERE username=%s', (username,)).fetchone()['user_id']
                conn.execute('DELETE FROM app.user_roles WHERE user_id=%s', (actual_id,))
                for role in roles:
                    conn.execute('INSERT INTO app.user_roles(user_id,role) VALUES(%s,%s)', (actual_id, role))
                conn.execute('DELETE FROM app.sessions WHERE user_id=%s', (actual_id,))
                print(f'Configured {username}: {", ".join(roles)}')
    print('Workflow schema and dynamic weekly views ready')


if __name__ == '__main__':
    main()
