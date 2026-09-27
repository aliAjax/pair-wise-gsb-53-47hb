"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'withdraw_evidence': {'supervisor'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'withdraw_evidence': {'evidence_requested': 'submitted'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}

# 停表结束后需要清理的临时性字段
PAUSE_FIELDS = ("pause_start_day", "remaining_days_at_pause", "deadline_day_before_pause")
EVIDENCE_FIELDS = ("evidence_request_day", "evidence_due_day", "allowed_days", "evidence_request")


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Dict[str, Any]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        clock: Dict[str, Any] = {}
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            pause_clock = boolean(data, "pause_clock", False)
            due_day = request_day + allowed_days
            if p.get("clock_paused"):
                raise Conflict("已有待回应补件，不能重复发起")
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = due_day
            changes["allowed_days"] = allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            if pause_clock:
                deadline_day = int(p["deadline_day"])
                if request_day > deadline_day:
                    raise ValidationError("原期限已届满，无法停表")
                frozen_remaining = deadline_day - request_day
                changes["clock_paused"] = True
                changes["pause_start_day"] = request_day
                changes["remaining_days_at_pause"] = frozen_remaining
                changes["deadline_day_before_pause"] = deadline_day
                # 停表期间保留剩余天数，不随等待流逝，也不算逾期
                changes["days_remaining"] = frozen_remaining
                changes["overdue"] = False
                clock = {
                    "paused": True,
                    "pause_start_day": request_day,
                    "evidence_due_day": due_day,
                    "allowed_days": allowed_days,
                    "deadline_before_pause": deadline_day,
                    "remaining_days_frozen": frozen_remaining,
                }
                summary = "补件要求已发出，案件期限停表"
            else:
                summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            request_day = int(p["evidence_request_day"])
            due_day = int(p["evidence_due_day"])
            response_day = int(data.get("response_day", p["response_day"]))
            if response_day < request_day:
                raise ValidationError("回应日期不能早于补件请求日")
            if response_day > due_day:
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = response_day
            changes["evidence_documents"] = docs
            if p.get("clock_paused"):
                frozen_remaining = int(p["remaining_days_at_pause"])
                deadline_before = int(p["deadline_day_before_pause"])
                # 回应后按剩余天数重算新期限，停表区间写入详情
                deadline_after = response_day + frozen_remaining
                stoppage = {
                    "request_day": request_day,
                    "pause_start_day": int(p["pause_start_day"]),
                    "allowed_days": int(p.get("allowed_days", due_day - request_day)),
                    "evidence_due_day": due_day,
                    "resume_day": response_day,
                    "ended_by": "respond",
                    "deadline_before_pause": deadline_before,
                    "deadline_after_resume": deadline_after,
                    "remaining_days_at_pause": frozen_remaining,
                }
                changes["deadline_day"] = deadline_after
                changes["days_remaining"] = frozen_remaining
                changes["overdue"] = False
                changes["clock_paused"] = False
                changes["clock_stoppages"] = p.get("clock_stoppages", []) + [stoppage]
                clock = {
                    "paused": True,
                    "resumed": True,
                    "pause_start_day": request_day,
                    "resume_day": response_day,
                    "evidence_due_day": due_day,
                    "deadline_before_pause": deadline_before,
                    "deadline_after_resume": deadline_after,
                    "remaining_days_restored": frozen_remaining,
                }
                summary = "补件已回应，案件期限按剩余天数重启"
            else:
                summary = "补件已回应"
        elif action == "withdraw_evidence":
            withdraw_day = integer(data, "withdraw_day", 0)
            reason = text(data, "withdraw_reason")
            request_day = int(p["evidence_request_day"])
            if withdraw_day < request_day:
                raise ValidationError("撤回日期不能早于补件请求日")
            changes["response_day"] = withdraw_day
            if p.get("clock_paused"):
                # 主管撤回：恢复原期限，停表期间不再扣除
                deadline_before = int(p["deadline_day_before_pause"])
                remaining = deadline_before - withdraw_day
                stoppage = {
                    "request_day": request_day,
                    "pause_start_day": int(p["pause_start_day"]),
                    "allowed_days": int(p.get("allowed_days", int(p["evidence_due_day"]) - request_day)),
                    "evidence_due_day": int(p["evidence_due_day"]),
                    "resume_day": withdraw_day,
                    "ended_by": "withdraw",
                    "withdraw_reason": reason,
                    "deadline_before_pause": deadline_before,
                    "deadline_after_resume": deadline_before,
                    "remaining_days_at_pause": int(p["remaining_days_at_pause"]),
                }
                changes["deadline_day"] = deadline_before
                changes["days_remaining"] = remaining
                changes["overdue"] = remaining < 0
                changes["clock_paused"] = False
                changes["clock_stoppages"] = p.get("clock_stoppages", []) + [stoppage]
                clock = {
                    "paused": True,
                    "withdrawn": True,
                    "pause_start_day": request_day,
                    "withdraw_day": withdraw_day,
                    "evidence_due_day": int(p["evidence_due_day"]),
                    "deadline_before_pause": deadline_before,
                    "deadline_restored": deadline_before,
                    "days_remaining": remaining,
                    "overdue": remaining < 0,
                    "withdraw_reason": reason,
                }
            else:
                remaining = int(p["deadline_day"]) - withdraw_day
                changes["days_remaining"] = remaining
                changes["overdue"] = remaining < 0
                clock = {
                    "paused": False,
                    "withdrawn": True,
                    "withdraw_day": withdraw_day,
                    "deadline_day": int(p["deadline_day"]),
                    "withdraw_reason": reason,
                }
            summary = "主管撤回补件要求，已恢复原期限"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        # 停表重启后清理活动字段，历史区间保留在 clock_stoppages
        if action in ("respond", "withdraw_evidence") and not p.get("clock_paused"):
            for field in PAUSE_FIELDS:
                p.pop(field, None)
        if action == "withdraw_evidence":
            for field in EVIDENCE_FIELDS:
                p.pop(field, None)
        return new_state, p, summary or ("已执行%s" % action), clock
