"""Evidence-backed weekly operating brief for the teaching dashboard."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


def short(value, limit=110):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def source(row):
    return {'employee_id': row['employee_id'], 'employee_name': row['employee_name'],
            'department': row['department_name'], 'date': row['report_date'].isoformat(),
            'version_id': row['version_id'], 'review': row['review_state']}


def analyze(conn, selected: date):
    end = selected + timedelta(days=6)
    metric = conn.execute('SELECT * FROM analytics.v_weekly_kpis WHERE week_start=%s', (selected,)).fetchone()
    if not metric:
        return None
    reports = conn.execute('''SELECT r.employee_id,r.report_date,r.submit_status,
        e.employee_name,d.department_name,v.version_id,v.submit_state,
        rv.decision,COALESCE(v.narrative,r.narrative) AS narrative,
        COALESCE(v.completion_status,r.completion_status) AS completion_status,
        COALESCE(v.risk_note,r.risk_note) AS risk_note,
        COALESCE(v.next_plan,r.next_plan) AS next_plan,
        COALESCE(v.need_collaboration,r.need_collaboration) AS need_collaboration
        FROM work.daily_reports r JOIN org.employees e USING(employee_id)
        JOIN org.departments d USING(department_id)
        LEFT JOIN LATERAL (SELECT * FROM app.daily_report_versions v0
            WHERE v0.employee_id=r.employee_id AND v0.report_date=r.report_date
            ORDER BY v0.version_no DESC LIMIT 1) v ON TRUE
        LEFT JOIN app.daily_report_reviews rv ON rv.version_id=v.version_id
        WHERE r.report_date BETWEEN %s AND %s
        ORDER BY r.report_date DESC,e.employee_id''', (selected, end)).fetchall()
    usable = []
    for row in reports:
        if row['version_id']:
            if row['submit_state'] != '已提交' or row['decision'] == '退回':
                continue
            row['review_state'] = '已通过' if row['decision'] == '通过' else '待审核'
        elif row['submit_status'] == '已提交':
            row['review_state'] = '历史教学资料'
        else:
            continue
        if short(row['narrative']):
            usable.append(row)

    events = conn.execute('''SELECT a.event_id,a.occurred_on,a.employee_id,e.employee_name,
        d.department_name,a.work_category,a.source_kind,a.source_document_id,a.order_id,
        a.activity_note,a.assignment_basis,a.verification_state,a.caveat
        FROM work.activity_events a JOIN org.employees e USING(employee_id)
        JOIN org.departments d ON d.department_id=a.department_id
        WHERE a.occurred_on BETWEEN %s AND %s
        ORDER BY a.occurred_on DESC,a.event_id''', (selected, end)).fetchall()

    expected = int(metric['daily_expected'] or 0)
    submitted = int(metric['daily_submitted'] or 0)
    ratio = submitted / expected if expected else None
    departments = sorted({r['department_name'] for r in usable})
    coverage_state = ('无应填日报' if expected == 0 else '样本不足' if ratio < .5 else
                      '初步分析' if ratio < .8 or len(departments) < 4 else '周期分析')
    groups = {}
    for row in usable:
        groups.setdefault(row['department_name'], []).append(row)
    event_groups = {}
    for row in events:
        event_groups.setdefault(row['department_name'], []).append(row)
    major = []
    departments_with_evidence = set(groups) | set(event_groups)
    for department in sorted(departments_with_evidence,
                             key=lambda name: (-len(event_groups.get(name, [])),-len(groups.get(name, [])),name))[:5]:
        rows = groups.get(department, [])
        details = event_groups.get(department, [])
        concrete = sorted(rows, key=lambda r: (bool(re.search(r'(SO|PO|EX|RC|Q)\d{3,}', r['narrative'] or '')),
                                                len(r['narrative'] or '')), reverse=True)
        report_examples = concrete[:1]
        event_examples = [r for r in details if r['source_kind'] != '模拟日常事项'][:2]
        if not event_examples:
            event_examples = details[:1]
        pieces = [short(r['narrative'], 85) for r in report_examples]
        pieces += [short(r['activity_note'], 85) for r in event_examples]
        evidence = [source(r) for r in report_examples]
        evidence += [{'event_id': r['event_id'], 'date': r['occurred_on'].isoformat(),
                      'source_kind': r['source_kind'], 'source_document_id': r['source_document_id'],
                      'order_id': r['order_id'], 'verification_state': r['verification_state'],
                      'assignment_basis': r['assignment_basis']} for r in event_examples]
        major.append({'title': department + '主要事项',
                      'summary': '；'.join(dict.fromkeys(pieces)),
                      'report_count': len(rows), 'event_count': len(details),
                      'traceable_event_count': sum(r['source_kind'] != '模拟日常事项' for r in details),
                      'sources': evidence})

    warnings = []
    risk_groups = {}
    for row in usable:
        risk = short(row['risk_note'], 180)
        if risk:
            risk_groups.setdefault(risk, []).append(row)
    for risk, rows in sorted(risk_groups.items(), key=lambda item: -len(item[1]))[:4]:
        warnings.append({'kind': '日报明确风险', 'level': '需关注', 'title': risk,
                         'basis': f'{len(rows)} 份日报明确记录；请核查当前状态。',
                         'department': rows[0]['department_name'],
                         'sources': [source(r) for r in rows[:3]]})

    current_period = selected <= datetime.now(SHANGHAI).date() <= end
    as_of = min(datetime.now(SHANGHAI).date(), end)
    pending_orders = conn.execute('''SELECT order_id,signed_amount,delivery_status
        FROM sales.orders WHERE signed_on BETWEEN %s AND %s
          AND delivery_status IN ('待交付','部分交付')
        ORDER BY signed_on DESC,order_id LIMIT 5''', (selected, end)).fetchall() if current_period else []
    partial = conn.execute('''SELECT o.order_id,o.signed_amount,
        COALESCE((SELECT sum(r.amount) FROM finance.customer_receipts r
                  WHERE r.order_id=o.order_id AND r.received_on<=%s),0) AS received
        FROM sales.orders o WHERE o.signed_on BETWEEN %s AND %s
        ORDER BY o.signed_on DESC,o.order_id LIMIT 100''', (as_of, selected, end)).fetchall()
    partial = [r for r in partial if r['received'] < r['signed_amount']]
    stalled_opportunities = []
    delayed_purchases = []
    if current_period:
        cutoff = datetime.now(SHANGHAI).date()
        stalled_opportunities = conn.execute('''SELECT opportunity_id,expected_close_on,stage
            FROM sales.opportunities WHERE closed_on IS NULL AND expected_close_on<%s
            ORDER BY expected_close_on LIMIT 5''', (cutoff,)).fetchall()
        delayed_purchases = conn.execute('''SELECT purchase_order_id,order_id,expected_arrival_on
            FROM ops.purchase_orders WHERE accepted_on IS NULL AND expected_arrival_on<%s
            ORDER BY expected_arrival_on LIMIT 5''', (cutoff,)).fetchall()
    if stalled_opportunities:
        warnings.append({'kind': '业务数据待核', 'level': '跟进', 'title': '商机预计结案日已过',
            'basis': '预计日期已过且尚未结案；需核对商机阶段和客户反馈，不能直接判断输单。',
            'department': '销售部',
            'sources': [{'opportunity_id': r['opportunity_id'], 'date': r['expected_close_on'].isoformat()}
                        for r in stalled_opportunities]})
    if delayed_purchases:
        warnings.append({'kind': '业务数据待核', 'level': '跟进', 'title': '采购预计到货日已过',
            'basis': '预计到货日期已过，验收日期为空；请核对实物到货与系统记录。',
            'department': '采购仓储部',
            'sources': [{'purchase_order_id': r['purchase_order_id'], 'order_id': r['order_id']}
                        for r in delayed_purchases]})
    if pending_orders:
        warnings.append({'kind': '业务数据待核', 'level': '跟进', 'title': '新签订单待交付确认',
            'basis': f'{len(pending_orders)} 笔本周期订单状态尚未完成；未交付不形成确认收入。',
            'department': '销售部 / 采购仓储部 / 技术服务部',
            'sources': [{'order_id': r['order_id']} for r in pending_orders]})
    if partial:
        warnings.append({'kind': '业务数据待核', 'level': '跟进', 'title': '新签订单尚未全额回款',
            'basis': '截至本周期观察日尚未全额回款；没有合同付款日，不能判定逾期。',
            'department': '财务部 / 销售部',
            'sources': [{'order_id': r['order_id'], 'signed_amount': str(r['signed_amount']),
                         'received': str(r['received'])} for r in partial[:5]]})
    if expected and ratio < .5:
        warnings.insert(0, {'kind': '数据覆盖', 'level': '样本不足', 'title': '日报覆盖不足以代表全公司',
            'basis': f'本周期应填 {expected} 份，已交 {submitted} 份；当前仅归纳已提交内容。',
            'department': '各部门负责人', 'sources': []})

    actions = []
    review_time = '本周期内复核' if current_period else '历史周期复盘时'
    next_time = '下个工作日核查' if current_period else '历史周期复盘时'
    if risk_groups:
        risk, rows = next(iter(sorted(risk_groups.items(), key=lambda item: -len(item[1]))))
        actions.append({'title': '核查日报明确风险', 'action': short(risk, 100) + '；确认影响和解决时间。',
            'department': rows[0]['department_name'], 'timeframe': review_time,
            'check': '风险状态与关联业务记录', 'sources': [source(r) for r in rows[:2]]})
    if expected and ratio < .8:
        actions.append({'title': '补齐本周期日报', 'action': '请部门负责人核对未提交位置，再形成全周期经营判断。',
            'department': '各部门负责人', 'timeframe': '本周期内',
            'check': '日报提交率与关键部门覆盖', 'sources': []})
    if stalled_opportunities:
        actions.append({'title': '复核临近商机', 'action': '向客户确认预算、决策人和新的预计结案时间，更新商机阶段。',
            'department': '销售部', 'timeframe': '下个工作日核查',
            'check': '预计结案日与商机状态',
            'sources': [{'opportunity_id': r['opportunity_id']} for r in stalled_opportunities[:3]]})
    if delayed_purchases:
        actions.append({'title': '核对采购到货与验收', 'action': '确认供应商交付情况和系统验收记录，再协调受影响订单。',
            'department': '采购仓储部', 'timeframe': '下个工作日核查',
            'check': '采购预计到货日和验收状态',
            'sources': [{'purchase_order_id': r['purchase_order_id'], 'order_id': r['order_id']}
                        for r in delayed_purchases[:3]]})
    if pending_orders:
        actions.append({'title': '跟踪新签订单交付', 'action': '核对采购到货、安装排期与交付计划，更新订单状态。',
            'department': '销售部 / 采购仓储部 / 技术服务部', 'timeframe': next_time,
            'check': '待交付订单及确认收入', 'sources': [{'order_id': r['order_id']} for r in pending_orders[:3]]})
    if partial:
        actions.append({'title': '核对后续收款安排', 'action': '确认客户付款节点与已到账记录；目前不能判断是否逾期。',
            'department': '财务部 / 销售部', 'timeframe': next_time,
            'check': '关联订单的实际回款', 'sources': [{'order_id': r['order_id']} for r in partial[:3]]})
    if metric['expenses_incurred'] and metric['expenses_incurred'] > 0:
        actions.append({'title': '核对费用发生与支付',
            'action': f"本周期发生费用 {metric['expenses_incurred']} 元、实付费用 {metric['expenses_paid']} 元；核对明细与付款安排，暂无预算依据判断超支。",
            'department': '财务部', 'timeframe': '周期复盘时',
            'check': '费用发生额、实付额及凭证', 'sources': []})
    collaboration = [r for r in usable if short(r['need_collaboration']) and
                     short(r['need_collaboration']) not in ('无', '暂无', '按计划协作')]
    if collaboration:
        actions.append({'title': '处理跨部门协作需求',
            'action': short(collaboration[0]['need_collaboration'], 100) + '；确认负责部门与进度。',
            'department': collaboration[0]['department_name'], 'timeframe': next_time,
            'check': '协作事项关闭情况', 'sources': [source(collaboration[0])]})
    if not actions:
        actions.append({'title': '复核周期经营数据', 'action': '结合已提交日报核对订单、回款和费用明细。',
            'department': '经营管理者', 'timeframe': '周期复盘时',
            'check': '经营指标与原始单据', 'sources': []})

    source_refs = [(r['employee_id'], str(r['report_date']), r['version_id'], r['review_state']) for r in usable]
    fingerprint = hashlib.sha256(json.dumps([source_refs, events, metric, pending_orders, partial,
        stalled_opportunities, delayed_purchases],
        ensure_ascii=False, default=str, sort_keys=True).encode()).hexdigest()[:16]
    return {'week_start': selected.isoformat(), 'week_end': end.isoformat(),
            'generated_at': datetime.now(SHANGHAI).isoformat(timespec='seconds'),
            'analysis_version': 'rules-v1', 'source_fingerprint': fingerprint,
            'coverage': {'expected': expected, 'submitted': submitted, 'usable': len(usable),
                         'departments': len(departments), 'ratio': ratio, 'state': coverage_state,
                         'risk_filled': len([r for r in usable if short(r['risk_note'])]),
                         'event_count': len(events),
                         'traceable_event_count': sum(r['source_kind'] != '模拟日常事项' for r in events),
                         'simulated_event_count': sum(r['source_kind'] == '模拟日常事项' for r in events)},
            'major_items': major, 'warnings': warnings[:6], 'recommendations': actions[:5],
            'method': '按最新有效已提交日报与原资料包业务事项归纳；业务事项中的模拟分派和待人工核验均保留标记，事项金额不重复计入经营指标。未配置摘要模型。'}
