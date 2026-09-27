from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CORRECTION_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, TERMINAL_STATES, TITLE, VIEW_ROLES,
                    can_correct, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def correct_quantity(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CORRECTION_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if not can_correct(item["status"]):
            raise ConflictError("进入调查后不能再更正读数")
        if not isinstance(payload.get("expected_version"), int) \
                or payload["expected_version"] < 1:
            raise ValueError("expected_version必须是正整数")
        new_quantity = require_number(payload.get("new_quantity"), "new_quantity")
        if new_quantity == float(item["quantity"]):
            raise ConflictError("new_quantity与当前读数一致，无需更正")
        reason = require_text(payload.get("reason"), "reason")
        result = self.repository.correct_quantity(
            item_id, new_quantity, reason, payload["expected_version"], actor)
        updated = result["item"]
        self.repository.append_audit("dose_correction", ENTITY, item_id, actor, {
            "correction_id": result["correction"]["id"],
            "previous_quantity": item["quantity"],
            "new_quantity": new_quantity,
            "reason": reason,
            "previous_ratio": item["quantity"] / item["threshold"]
            if item["threshold"] > 0 else 1.0,
            "new_ratio": new_quantity / item["threshold"]
            if item["threshold"] > 0 else 1.0,
            "escalation_required_after": escalation_required(
                updated["severity"], new_quantity, item["threshold"]),
            "prior_escalation_retained": bool(updated["escalation_flagged"]),
        })
        return {"item": self._detail(updated), "correction": result["correction"]}

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str, closure_note: Optional[str] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        if target in TERMINAL_STATES and item["escalation_flagged"]:
            closure_note = require_text(closure_note, "closure_note")
        elif closure_note is not None:
            closure_note = require_text(closure_note, "closure_note")
        else:
            closure_note = None
        updated = self.repository.transition_item(
            item_id, target, expected_version, actor, closure_note)
        detail = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "prior_escalation": bool(item["escalation_flagged"]),
        }
        if closure_note is not None:
            detail["closure_note"] = closure_note
        self.repository.append_audit("transition", ENTITY, item_id, actor, detail)
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._detail(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def list_corrections(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_corrections(item_id)

    def _detail(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = self.enrich(item)
        corrections = self.repository.list_corrections(item["id"])
        result["original_quantity"] = (corrections[0]["previous_quantity"]
                                       if corrections else item["quantity"])
        result["corrections"] = corrections
        return result

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["ratio"] = (item["quantity"] / item["threshold"]
                           if item["threshold"] > 0 else 1.0)
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["prior_escalation"] = bool(item.get("escalation_flagged", 0))
        return result
