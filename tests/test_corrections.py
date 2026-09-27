import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to(self, item, targets):
        for target in targets:
            item = self.service.transition(
                item["id"], target, item["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return item

    def test_correction_recomputes_priority_deadline_escalation(self):
        item = self.service.create_item(
            {"title": "c1", "description": "over limit", "severity": "low",
             "quantity": 12, "threshold": 10, "external_ref": "C-1"},
            "dos", "dosimetrist")
        self.assertTrue(item["escalation_required"])
        before = (item["priority"], item["deadline_hours"])
        result = self.service.correct_quantity(
            item["id"], {"new_quantity": 2, "reason": "仪器复测，原读数偏高",
                         "expected_version": item["version"]},
            "dos", "dosimetrist")
        updated = result["item"]
        self.assertEqual(updated["quantity"], 2.0)
        self.assertEqual(updated["original_quantity"], 12.0)
        self.assertFalse(updated["escalation_required"])
        # 列表同样按最新读数
        listed = self.service.list_items("viewer")[0]
        self.assertEqual(listed["quantity"], 2.0)
        self.assertFalse(listed["escalation_required"])
        self.assertLess(updated["priority"], before[0])
        self.assertGreater(updated["deadline_hours"], before[1])
        # 原值与更正按时间保留
        hist = self.service.list_corrections(item["id"], "viewer")
        self.assertEqual(len(hist), 1)
        self.assertEqual((hist[0]["previous_quantity"], hist[0]["new_quantity"]), (12.0, 2.0))

    def test_only_dosimetrist_and_reviewing_state_can_correct(self):
        item = self.service.create_item(
            {"title": "c2", "description": "d", "severity": "low",
             "quantity": 5, "threshold": 10, "external_ref": "C-2"},
            "dos", "dosimetrist")
        with self.assertRaises(PermissionDenied):
            self.service.correct_quantity(
                item["id"], {"new_quantity": 1, "reason": "x",
                             "expected_version": 1}, "ro", "radiation_officer")
        item = self._to(item, ["reviewing", "investigation"])
        with self.assertRaises(ConflictError):
            self.service.correct_quantity(
                item["id"], {"new_quantity": 1, "reason": "进入调查后想退回",
                             "expected_version": item["version"]},
                "dos", "dosimetrist")

    def test_correction_requires_reason_and_changed_value(self):
        item = self.service.create_item(
            {"title": "c3", "description": "d", "severity": "low",
             "quantity": 5, "threshold": 10, "external_ref": "C-3"},
            "dos", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.correct_quantity(
                item["id"], {"new_quantity": 6, "reason": "  ",
                             "expected_version": 1}, "dos", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.correct_quantity(
                item["id"], {"new_quantity": 5, "reason": "没变",
                             "expected_version": 1}, "dos", "dosimetrist")

    def test_sticky_escalation_requires_hp_closure_note(self):
        # 比值在线上触发升级
        item = self.service.create_item(
            {"title": "c4", "description": "d", "severity": "low",
             "quantity": 12, "threshold": 10, "external_ref": "C-4"},
            "dos", "dosimetrist")
        self.assertTrue(item["prior_escalation"])
        # 复核阶段更正到线下，粘性标记不清除
        item = self.service.correct_quantity(
            item["id"], {"new_quantity": 1, "reason": "复测纠正",
                         "expected_version": item["version"]},
            "dos", "dosimetrist")["item"]
        self.assertFalse(item["escalation_required"])
        self.assertTrue(item["prior_escalation"])
        # 走到关闭，没有关闭说明被拒
        item = self._to(item, ["reviewing", "investigation", "follow_up"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                item["id"], "closed", item["version"], "hp", "health_physicist")
        # 非健康物理师不能关闭
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                item["id"], "closed", item["version"], "ro", "radiation_officer",
                closure_note="处置")
        closed = self.service.transition(
            item["id"], "closed", item["version"], "hp", "health_physicist",
            closure_note="经复测确认比值降至线下，原升级按误报归档")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closure_note"], "经复测确认比值降至线下，原升级按误报归档")

    def test_no_prior_escalation_closes_without_note(self):
        item = self.service.create_item(
            {"title": "c5", "description": "d", "severity": "low",
             "quantity": 1, "threshold": 10, "external_ref": "C-5"},
            "dos", "dosimetrist")
        item = self._to(item, ["reviewing", "investigation", "follow_up", "closed"])
        self.assertIsNone(item["closure_note"])

    def test_multiple_corrections_chain_in_audit(self):
        item = self.service.create_item(
            {"title": "c6", "description": "d", "severity": "low",
             "quantity": 10, "threshold": 10, "external_ref": "C-6"},
            "dos", "dosimetrist")
        for value in (8, 4):
            r = self.service.correct_quantity(
                item["id"], {"new_quantity": value, "reason": f"复测{value}",
                             "expected_version": item["version"]},
                "dos", "dosimetrist")
            item = r["item"]
        detail = self.service.get_item(item["id"], "viewer")
        self.assertEqual(detail["original_quantity"], 10.0)
        self.assertEqual([c["new_quantity"] for c in detail["corrections"]], [8.0, 4.0])
        events = self.service.audit("health_physicist", item["id"])
        self.assertEqual(sum(1 for e in events if e["action"] == "dose_correction"), 2)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
