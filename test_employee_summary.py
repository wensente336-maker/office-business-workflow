import json
import unittest
from unittest.mock import patch

from employee_summary import call_model, rule_summary


DATA = {
    'employee': {'employee_id': 'E001', 'employee_name': '测试员工'},
    'week_start': '2026-11-09', 'week_end': '2026-11-15', 'expected': 5,
    'reports': [{'report_date': '2026-11-09', 'version_id': 'V001',
                 'narrative': '整理客户反馈', 'risk_note': '等待客户确认', 'next_plan': '跟进确认'}],
    'events': [{'event_id': 'ACT001', 'source_document_id': 'SO001',
                'source_kind': '业务单据', 'assignment_basis': '单据负责人'}],
}


class FakeResponse:
    def __init__(self, output):
        self.output = json.dumps({'choices': [{'message': {'content': json.dumps(output)}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, _):
        return self.output


class SummaryTest(unittest.TestCase):
    def test_rule_summary_uses_actual_report_and_source(self):
        result = rule_summary(DATA)
        self.assertIn('整理客户反馈', result['overview'])
        self.assertEqual(result['source_refs'], ['V001', 'ACT001'])

    def test_model_output_requires_existing_source(self):
        output = {'overview': '该员工本周整理客户反馈，并等待客户确认下一步安排。',
                  'highlights': ['整理客户反馈'], 'risks': ['等待客户确认'],
                  'next_focus': ['跟进确认'], 'limitations': '', 'source_refs': ['V001']}
        env = {'EMPLOYEE_SUMMARY_API_URL': 'http://127.0.0.1:9999/v1/chat/completions',
               'EMPLOYEE_SUMMARY_MODEL': 'local-test'}
        with patch.dict('os.environ', env), patch('urllib.request.urlopen', return_value=FakeResponse(output)):
            self.assertEqual(call_model(DATA)['source_refs'], ['V001'])
        output['source_refs'] = ['NOT_A_SOURCE']
        with patch.dict('os.environ', env), patch('urllib.request.urlopen', return_value=FakeResponse(output)):
            with self.assertRaisesRegex(ValueError, '不存在'):
                call_model(DATA)


if __name__ == '__main__':
    unittest.main()
