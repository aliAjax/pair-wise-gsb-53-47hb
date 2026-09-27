import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
SUBMIT = ('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']})
OFFICER = Actor('officer', 'case_officer')
SUPERVISOR = Actor('boss', 'supervisor')
REP = Actor('rep', 'legal_rep')


class ClockStopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = self.service.create(Actor('creator', 'intake_officer'), 'IMM-CLOCK-1', CREATE_DATA)
        self.record = self._act(OFFICER, *SUBMIT)
        self.assertEqual(self.record['payload']['deadline_day'], 130)

    def tearDown(self):
        self.temp.cleanup()

    def _act(self, actor, action, role, data):
        self.record = self.service.act(actor, self.record['id'], self.record['version'], action, data)
        return self.record

    def _request_paused(self, request_day=115, allowed_days=20):
        return self._act(OFFICER, 'request_evidence', 'case_officer', {'evidence_request_day': request_day, 'allowed_days': allowed_days, 'evidence_request': '补充收入证明', 'pause_clock': True})

    def test_pause_freezes_remaining_and_waiting_is_not_overdue(self):
        # 补件截止日135晚于原期限130，但停表期间仍不算逾期
        record = self._request_paused()
        self.assertEqual(record['state'], 'evidence_requested')
        p = record['payload']
        self.assertTrue(p['clock_paused'])
        self.assertEqual(p['evidence_due_day'], 135)
        self.assertEqual(p['pause_start_day'], 115)
        self.assertEqual(p['deadline_day_before_pause'], 130)
        self.assertEqual(p['remaining_days_at_pause'], 15)
        self.assertEqual(p['days_remaining'], 15)
        self.assertFalse(p['overdue'])

    def test_respond_recomputes_deadline_from_remaining_days(self):
        self._request_paused()
        record = self._act(REP, 'respond', 'legal_rep', {'response_day': 134, 'documents': ['income_proof']})
        p = record['payload']
        self.assertEqual(record['state'], 'response_received')
        self.assertFalse(p['clock_paused'])
        self.assertEqual(p['deadline_day'], 149)  # 134 + 冻结的15天
        self.assertEqual(p['days_remaining'], 15)
        self.assertFalse(p['overdue'])
        stoppage = p['clock_stoppages'][0]
        self.assertEqual(stoppage['ended_by'], 'respond')
        self.assertEqual(stoppage['pause_start_day'], 115)
        self.assertEqual(stoppage['resume_day'], 134)
        self.assertEqual(stoppage['evidence_due_day'], 135)
        self.assertEqual(stoppage['deadline_before_pause'], 130)
        self.assertEqual(stoppage['deadline_after_resume'], 149)
        # 停表区间写入审计时间线
        event = self.service.timeline(Actor('creator', 'intake_officer'), record['id'])[-1]
        self.assertEqual(event['action'], 'respond')
        self.assertEqual(event['details']['clock']['deadline_before_pause'], 130)
        self.assertEqual(event['details']['clock']['deadline_after_resume'], 149)
        self.assertEqual(event['details']['clock']['evidence_due_day'], 135)

    def test_pending_request_blocks_second_request(self):
        self._request_paused()
        with self.assertRaises(Conflict):
            self._act(OFFICER, 'request_evidence', 'case_officer', {'evidence_request_day': 120, 'allowed_days': 5, 'evidence_request': '再次补件'})

    def test_withdraw_requires_supervisor_and_reason(self):
        self._request_paused()
        with self.assertRaises(PermissionDenied):
            self._act(OFFICER, 'withdraw_evidence', 'case_officer', {'withdraw_day': 120, 'withdraw_reason': '误发'})
        with self.assertRaises(ValidationError):
            self._act(SUPERVISOR, 'withdraw_evidence', 'supervisor', {'withdraw_day': 120})

    def test_supervisor_withdraw_restores_original_deadline(self):
        self._request_paused()
        record = self._act(SUPERVISOR, 'withdraw_evidence', 'supervisor', {'withdraw_day': 120, 'withdraw_reason': '请求误登记'})
        p = record['payload']
        self.assertEqual(record['state'], 'submitted')
        self.assertFalse(p['clock_paused'])
        self.assertEqual(p['deadline_day'], 130)  # 恢复原期限而非重算
        self.assertEqual(p['days_remaining'], 10)
        self.assertFalse(p['overdue'])
        self.assertNotIn('pause_start_day', p)
        self.assertNotIn('evidence_due_day', p)
        stoppage = p['clock_stoppages'][0]
        self.assertEqual(stoppage['ended_by'], 'withdraw')
        self.assertEqual(stoppage['withdraw_reason'], '请求误登记')
        self.assertEqual(stoppage['deadline_after_resume'], 130)
        event = self.service.timeline(Actor('creator', 'intake_officer'), record['id'])[-1]
        self.assertEqual(event['details']['clock']['withdraw_reason'], '请求误登记')
        self.assertEqual(event['details']['clock']['deadline_restored'], 130)
        # 撤回后可重新发起补件
        again = self._act(OFFICER, 'request_evidence', 'case_officer', {'evidence_request_day': 121, 'allowed_days': 5, 'evidence_request': '重新补件', 'pause_clock': True})
        self.assertEqual(again['state'], 'evidence_requested')
        self.assertEqual(len(again['payload']['clock_stoppages']), 1)

    def test_withdraw_after_deadline_restores_and_marks_overdue(self):
        self._request_paused()  # 补件截止135，原期限130
        record = self._act(SUPERVISOR, 'withdraw_evidence', 'supervisor', {'withdraw_day': 135, 'withdraw_reason': '申请人主张材料齐全'})
        p = record['payload']
        self.assertEqual(p['deadline_day'], 130)
        self.assertEqual(p['days_remaining'], -5)
        self.assertTrue(p['overdue'])

    def test_cannot_pause_after_original_deadline(self):
        with self.assertRaises(ValidationError):
            self._act(OFFICER, 'request_evidence', 'case_officer', {'evidence_request_day': 140, 'allowed_days': 10, 'evidence_request': '逾期补件', 'pause_clock': True})
        # 不停表的补件仍可发出
        record = self._act(OFFICER, 'request_evidence', 'case_officer', {'evidence_request_day': 140, 'allowed_days': 10, 'evidence_request': '逾期补件', 'pause_clock': False})
        self.assertEqual(record['state'], 'evidence_requested')
        self.assertNotIn('clock_paused', record['payload'])

    def test_respond_after_evidence_due_rejected_while_paused(self):
        self._request_paused()
        with self.assertRaises(ValidationError):
            self._act(REP, 'respond', 'legal_rep', {'response_day': 136, 'documents': ['income_proof']})

    def test_respond_before_request_day_rejected(self):
        self._request_paused()
        with self.assertRaises(ValidationError):
            self._act(REP, 'respond', 'legal_rep', {'response_day': 110, 'documents': ['income_proof']})

    def test_no_pause_keeps_legacy_behavior(self):
        self._act(OFFICER, 'request_evidence', 'case_officer', {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '普通补件'})
        record = self._act(REP, 'respond', 'legal_rep', {'response_day': 120, 'documents': ['income_proof']})
        p = record['payload']
        self.assertEqual(p['deadline_day'], 130)
        self.assertNotIn('clock_stoppages', p)
        event = self.service.timeline(Actor('creator', 'intake_officer'), record['id'])[-1]
        self.assertNotIn('clock', event['details'])
        record = self.service.act(OFFICER, record['id'], record['version'], 'decide', {'decision': 'granted', 'decision_reason': '材料充分'})
        self.assertEqual(record['state'], 'decided')

    def test_restarted_case_can_continue_to_decision(self):
        self._request_paused()
        self._act(REP, 'respond', 'legal_rep', {'response_day': 125, 'documents': ['income_proof']})
        record = self.service.act(OFFICER, self.record['id'], self.record['version'], 'decide', {'decision': 'granted', 'decision_reason': '材料充分'})
        self.assertEqual(record['state'], 'decided')
        self.assertEqual(record['payload']['deadline_day'], 140)  # 125 + 15


if __name__ == '__main__':
    unittest.main()
