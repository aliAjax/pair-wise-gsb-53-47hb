import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
PAUSE_REQUEST = {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补充收入证明', 'pause_clock': True}


class ClockPauseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _submitted_record(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-31001", CREATE_DATA)
        return self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "submit", {'documents': ['passport', 'sponsor_letter']})

    def _request_pause(self, record, data=None):
        return self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "request_evidence", data or PAUSE_REQUEST)

    def test_pause_freezes_deadline_and_response_recalculates(self):
        record = self._request_pause(self._submitted_record())
        payload = record["payload"]
        self.assertEqual(record["state"], "evidence_requested")
        self.assertTrue(payload["evidence_pending"])
        self.assertTrue(payload["pause_clock"])
        self.assertEqual(payload["paused_remaining_days"], 15)
        self.assertEqual(payload["days_remaining"], 15)
        self.assertFalse(payload["overdue"])
        self.assertEqual(payload["evidence_due_day"], 125)
        record = self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "respond", {'response_day': 120, 'documents': ['income_proof']})
        payload = record["payload"]
        self.assertEqual(record["state"], "response_received")
        self.assertEqual(payload["deadline_day"], 135)
        self.assertEqual(payload["days_remaining"], 15)
        self.assertFalse(payload["overdue"])
        self.assertFalse(payload["pause_clock"])
        self.assertFalse(payload["evidence_pending"])
        self.assertEqual(payload["clock_pauses"], [{'start_day': 115, 'end_day': 120, 'saved_days': 15}])
        record = self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "decide", {'decision': 'granted', 'decision_reason': '材料充分'})
        self.assertEqual(record["state"], "decided")

    def test_late_request_pause_not_overdue_while_waiting(self):
        record = self._request_pause(self._submitted_record(), {'evidence_request_day': 132, 'allowed_days': 10, 'evidence_request': '补充收入证明', 'pause_clock': True})
        payload = record["payload"]
        self.assertEqual(payload["paused_remaining_days"], -2)
        self.assertEqual(payload["days_remaining"], -2)
        self.assertFalse(payload["overdue"])
        record = self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "respond", {'response_day': 135, 'documents': ['income_proof']})
        payload = record["payload"]
        self.assertEqual(payload["deadline_day"], 133)
        self.assertEqual(payload["days_remaining"], -2)
        self.assertTrue(payload["overdue"])

    def test_duplicate_request_rejected_while_pending(self):
        record = self._request_pause(self._submitted_record())
        with self.assertRaises(Conflict):
            self._request_pause(record)

    def test_withdraw_requires_supervisor_and_reason(self):
        record = self._request_pause(self._submitted_record())
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "withdraw_evidence", {'withdraw_day': 118, 'withdraw_reason': '补件要求发出有误'})
        with self.assertRaises(ValidationError):
            self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "withdraw_evidence", {'withdraw_day': 118})
        record = self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "withdraw_evidence", {'withdraw_day': 118, 'withdraw_reason': '补件要求发出有误'})
        payload = record["payload"]
        self.assertEqual(record["state"], "submitted")
        self.assertEqual(payload["deadline_day"], 130)
        self.assertEqual(payload["days_remaining"], 12)
        self.assertFalse(payload["overdue"])
        self.assertFalse(payload["evidence_pending"])
        self.assertFalse(payload["pause_clock"])
        self.assertIsNone(payload["paused_remaining_days"])
        self.assertEqual(payload["withdraw_reason"], '补件要求发出有误')
        record = self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "decide", {'decision': 'granted', 'decision_reason': '材料充分'})
        self.assertEqual(record["state"], "decided")

    def test_audit_timeline_shows_pause_details(self):
        record = self._request_pause(self._submitted_record())
        record = self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "respond", {'response_day': 120, 'documents': ['income_proof']})
        record = self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "decide", {'decision': 'granted', 'decision_reason': '材料充分'})
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        events = {event["action"]: event["details"] for event in timeline}
        request_details = events["request_evidence"]
        self.assertEqual(request_details["changes"]["paused_remaining_days"], 15)
        self.assertEqual(request_details["changes"]["evidence_due_day"], 125)
        self.assertEqual(request_details["previous"]["days_remaining"], 20)
        respond_details = events["respond"]
        self.assertEqual(respond_details["changes"]["clock_pauses"], [{'start_day': 115, 'end_day': 120, 'saved_days': 15}])
        self.assertEqual(respond_details["changes"]["deadline_day"], 135)
        self.assertEqual(respond_details["previous"]["deadline_day"], 130)

    def test_audit_timeline_shows_withdraw_reason(self):
        record = self._request_pause(self._submitted_record())
        record = self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "withdraw_evidence", {'withdraw_day': 118, 'withdraw_reason': '补件要求发出有误'})
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        events = {event["action"]: event["details"] for event in timeline}
        withdraw_details = events["withdraw_evidence"]
        self.assertEqual(withdraw_details["changes"]["withdraw_reason"], '补件要求发出有误')
        self.assertEqual(withdraw_details["changes"]["days_remaining"], 12)
        self.assertEqual(withdraw_details["previous"]["paused_remaining_days"], 15)
