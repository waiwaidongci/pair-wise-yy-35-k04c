import tempfile, unittest
from pathlib import Path
from src.domain import PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES, escalation_required, priority_score, response_deadline_hours


class DoseCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to_closed(self, item, note="健康物理师关闭说明"):
        current = item
        for target in STATES[1:]:
            kwargs = {"closure_note": note} if target == STATES[-1] else {}
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0], **kwargs)
        return current

    def test_latest_correction_drives_list_priority_deadline_and_escalation(self):
        item = self.service.create_item(
            {"title": "dose item", "description": "instrument re-read",
             "severity": "elevated", "quantity": 2, "threshold": 10},
            "dos", "dosimetrist")
        self.assertFalse(item["escalation_required"])
        before = (item["priority"], item["deadline_hours"])
        corrected = self.service.add_correction(
            item["id"], {"new_quantity": 20, "reason": "仪器复测，初次读数漏乘系数"},
            "dos", "dosimetrist")
        self.assertEqual(corrected["original_quantity"], 2)
        self.assertEqual(corrected["current_quantity"], 20)
        self.assertEqual(corrected["version"], item["version"] + 1)
        self.assertTrue(corrected["escalation_required"])
        self.assertEqual(priority_score("elevated", 20, 10), corrected["priority"])
        self.assertEqual(response_deadline_hours("elevated", 20, 10),
                         corrected["deadline_hours"])
        self.assertGreater(corrected["priority"], before[0])
        self.assertLess(corrected["deadline_hours"], before[1])
        listing = self.service.list_items("viewer")
        self.assertEqual(listing[0]["current_quantity"], 20)
        self.assertEqual(listing[0]["priority"], corrected["priority"])
        history = self.service.list_corrections(item["id"], "viewer")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["previous_quantity"], 2)
        self.assertEqual(history[0]["new_quantity"], 20)
        self.assertEqual(history[0]["created_by"], "dos")
        detail = self.service.get_item(item["id"], "viewer")
        self.assertEqual(len(detail["corrections"]), 1)
        self.assertEqual(detail["current_quantity"], 20)
        # 再次更正，历史追加、计算按最近一次
        again = self.service.add_correction(
            item["id"], {"new_quantity": 12, "reason": "录入纠错，复测值为12"},
            "dos2", "dosimetrist")
        self.assertEqual(again["current_quantity"], 12)
        history = self.service.list_corrections(item["id"], "viewer")
        self.assertEqual([c["new_quantity"] for c in history], [20, 12])
        self.assertEqual(history[-1]["previous_quantity"], 20)
        self.assertTrue(self.repo.verify_audit_chain())
        events = self.service.audit("viewer", item["id"])
        actions = [e["action"] for e in events]
        self.assertEqual(actions.count("dose_correction"), 2)
        last = [e for e in events if e["action"] == "dose_correction"][-1]
        self.assertEqual(last["detail"]["previous_quantity"], 20)
        self.assertEqual(last["detail"]["new_quantity"], 12)
        self.assertEqual(last["detail"]["original_quantity"], 2)

    def test_ratio_below_threshold_after_correction_keeps_escalation_locked(self):
        item = self.service.create_item(
            {"title": "over limit", "description": "started critical",
             "severity": "low", "quantity": 15, "threshold": 10},
            "dos", "dosimetrist")
        self.assertTrue(item["escalation_required"])
        self.assertTrue(item["escalation_locked"])
        self.service.add_record(item["id"], {"kind": "evidence", "detail": "ready",
                                             "status": "closed"}, "ro", "radiation_officer")
        corrected = self.service.add_correction(
            item["id"], {"new_quantity": 3, "reason": "复测发现探头未校准"},
            "dos", "dosimetrist")
        # 比值降回线下，当前判断随之下降，但曾经升级的事实仍然锁存
        self.assertFalse(corrected["escalation_required"])
        self.assertFalse(escalation_required("low", 3, 10))
        self.assertTrue(corrected["escalation_locked"])
        current = self.service.get_item(item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        # 健康物理师不写关闭说明不能关闭锁存事件
        with self.assertRaises(ValidationError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "hp", "health_physicist")
        closed = self.service.transition(
            current["id"], STATES[-1], current["version"], "hp",
            "health_physicist", "复测确认误报，原升级在关闭说明中处置")
        self.assertTrue(closed["escalation_locked"])
        events = self.service.audit("viewer", item["id"])
        close_event = [e for e in events if e["detail"].get("to") == STATES[-1]][0]
        self.assertTrue(close_event["detail"]["escalation_locked"])
        self.assertIn("处置", close_event["detail"]["closure_note"])
        # 关闭后不能再更正读数
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.add_correction(
                item["id"], {"new_quantity": 1, "reason": "已关闭事件"},
                "dos", "dosimetrist")

    def test_correction_allowed_during_investigation_but_never_rolls_back(self):
        item = self.service.create_item(
            {"title": "investigating", "description": "recheck while open",
             "severity": "high", "quantity": 12, "threshold": 10},
            "dos", "dosimetrist")
        current = item
        for target in ("reviewing", "investigation"):
            current = self.service.transition(
                current["id"], target, current["version"], "ro",
                TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "investigation")
        corrected = self.service.add_correction(
            item["id"], {"new_quantity": 4, "reason": "调查中仪器复测"},
            "dos", "dosimetrist")
        # 状态不退回，升级历史不擦除
        self.assertEqual(corrected["status"], "investigation")
        self.assertTrue(corrected["escalation_locked"])
        self.assertFalse(corrected["escalation_required"])
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.transition(item["id"], "reviewing", corrected["version"],
                                    "ro", "radiation_officer")

    def test_only_dosimetrist_can_correct_and_reason_required(self):
        item = self.service.create_item(
            {"title": "perm", "description": "permission checks",
             "severity": "low", "quantity": 1, "threshold": 10},
            "dos", "dosimetrist")
        with self.assertRaises(PermissionDenied):
            self.service.add_correction(
                item["id"], {"new_quantity": 2, "reason": "x"}, "ro", "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.add_correction(
                item["id"], {"new_quantity": 2, "reason": "x"}, "hp", "health_physicist")
        with self.assertRaises(ValidationError):
            self.service.add_correction(
                item["id"], {"new_quantity": 2}, "dos", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.add_correction(
                item["id"], {"new_quantity": -1, "reason": "bad"}, "dos", "dosimetrist")
        self.assertEqual(
            self.service.list_corrections(item["id"], "viewer"), [])


if __name__ == "__main__":
    unittest.main()
