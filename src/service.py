from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CORRECTION_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, TERMINAL_STATES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    @staticmethod
    def _current_quantity(item: Dict[str, Any]) -> float:
        return float(item.get("effective_quantity", item["quantity"]))

    @classmethod
    def _escalated(cls, item: Dict[str, Any]) -> bool:
        return escalation_required(
            item["severity"], cls._current_quantity(item), item["threshold"])

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
        locked = escalation_required(severity, quantity, threshold)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, locked, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "original_quantity": quantity, "current_quantity": quantity,
            "escalation_required": locked, "escalation_locked": bool(item["escalation_locked"]),
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_correction(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, CORRECTION_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("事件已关闭，不能再更正读数")
        new_quantity = require_number(
            payload.get("new_quantity", payload.get("quantity")), "new_quantity")
        reason = require_text(payload.get("reason", payload.get("note")), "reason")
        previous_quantity = self._current_quantity(item)
        candidate = dict(item)
        candidate["effective_quantity"] = new_quantity
        # 更正只用于重新计算，不能借更正撤销已经触发过的升级
        lock_escalation = self._escalated(candidate)
        correction = self.repository.add_correction(
            item_id, previous_quantity, new_quantity, reason, lock_escalation, actor)
        updated = self.repository.get_item(item_id)
        self.repository.append_audit("dose_correction", ENTITY, item_id, actor, {
            "correction_id": correction["id"],
            "original_quantity": item["quantity"],
            "previous_quantity": previous_quantity,
            "new_quantity": new_quantity,
            "current_quantity": self._current_quantity(updated),
            "reason": reason,
            "escalation_required": self._escalated(updated),
            "escalation_locked": bool(updated["escalation_locked"]),
        })
        result = self.enrich(updated)
        result["correction"] = correction
        result["corrections"] = self.repository.list_corrections(item_id)
        return result

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

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str,
                   closure_note: Optional[str] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        if target in TERMINAL_STATES:
            # 曾经升级即锁存：比值即使降回线下，也只能由健康物理师在关闭说明里处置
            if item["escalation_locked"]:
                closure_note = require_text(
                    closure_note, "closure_note", 2000)
            elif closure_note is not None:
                closure_note = require_text(closure_note, "closure_note", 2000)
        updated = self.repository.transition_item(
            item_id, target, expected_version, actor, closure_note)
        audit_detail: Dict[str, Any] = {
            "from": item["status"], "to": target,
            "current_quantity": self._current_quantity(item),
            "escalation_required": self._escalated(item),
            "escalation_locked": bool(item["escalation_locked"]),
        }
        if closure_note is not None:
            audit_detail["closure_note"] = closure_note
        self.repository.append_audit("transition", ENTITY, item_id, actor, audit_detail)
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        result = self.enrich(item)
        result["corrections"] = self.repository.list_corrections(item_id)
        return result

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def list_corrections(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_corrections(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @classmethod
    def enrich(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        current_quantity = cls._current_quantity(item)
        result["original_quantity"] = item["quantity"]
        result["current_quantity"] = current_quantity
        result["priority"] = priority_score(
            item["severity"], current_quantity, item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], current_quantity, item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], current_quantity, item["threshold"])
        result["escalation_locked"] = bool(item.get("escalation_locked", 0))
        return result
