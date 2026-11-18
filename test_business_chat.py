import json
import unittest
from unittest.mock import patch

from business_chat import ChatError, ask_model


EVIDENCE = {
    'start': '2026-11-09', 'end': '2026-11-15', 'scope': 'week',
    'sources': [{'ref': 'KPI:2026-11-09', 'kind': '经营周指标',
                 'label': '2026-11-09 经营周', 'date': '2026-11-09',
                 'detail': '新增商机：4；签单额：100.00'},
                {'ref': 'SO001', 'kind': '销售订单', 'label': 'SO001',
                 'date': '2026-11-10', 'detail': '签单金额：100.00'}],
    'counts': {'经营周指标': {'total': 1, 'shown': 1},
               '销售订单': {'total': 8, 'shown': 1}},
}


class FakeResponse:
    def __init__(self, answer, refs):
        self.content = json.dumps({'choices': [{'message': {'content': json.dumps({
            'answer': answer, 'source_refs': refs, 'limitations': ''})}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, _):
        return self.content


class BusinessChatTest(unittest.TestCase):
    def answer(self, text, refs):
        with patch('business_chat.api_key', return_value='test-only'), \
             patch('business_chat.urllib.request.urlopen', return_value=FakeResponse(text, refs)):
            return ask_model('本周经营情况如何？', EVIDENCE, [])

    def test_metric_source_and_partial_coverage_are_attached(self):
        result = self.answer('本周新增商机4个，签单额100.00。', [])
        self.assertEqual(result['sources'][0]['ref'], 'KPI:2026-11-09')
        self.assertIn('销售订单 1/8', result['limitations'])

    def test_mismatched_metric_is_rejected(self):
        with self.assertRaises(ChatError):
            self.answer('本周登记商机3条。', ['KPI:2026-11-09'])

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ChatError):
            self.answer('本周签单额100.00。', ['NOT_REAL'])


if __name__ == '__main__':
    unittest.main()
