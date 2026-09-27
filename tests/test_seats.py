import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError
from src.repository import HANDOVER_ACCEPTED, HANDOVER_DECLINED, HANDOVER_PENDING, HANDOVER_REVOKED, Repository
from src.rules import DomainRules
from src.audit import AuditRecorder
from src.service import Service


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'hospital_beds': 4, 'destination': 'City Hospital', 'location': 'East Gate'}
ASSIGN = {'vehicle_available': True, 'vehicle_id': 'AMB-07'}


def dispatcher(user, org='east'):
    return Actor(user, 'dispatcher', org)


class SeatClaimTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)
        self.record = self.service.create(dispatcher('creator', 'east'), "EMG-31001", CREATE_DATA)

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_and_duplicate_claim(self):
        claimed = self.service.claim(dispatcher('d01', 'east'), self.record["id"])
        self.assertEqual(claimed["owner_id"], "d01")
        with self.assertRaises(Conflict) as ctx:
            self.service.claim(dispatcher('d02', 'east'), self.record["id"])
        self.assertIn("d01", str(ctx.exception))
        with self.assertRaises(Conflict):
            self.service.claim(dispatcher('d01', 'east'), self.record["id"])

    def test_cross_org_claim_rejected(self):
        with self.assertRaises(PermissionDenied) as ctx:
            self.service.claim(dispatcher('d09', 'west'), self.record["id"])
        self.assertIn("跨机构", str(ctx.exception))

    def test_non_dispatcher_cannot_claim(self):
        with self.assertRaises(PermissionDenied):
            self.service.claim(Actor('p01', 'paramedic', 'east'), self.record["id"])

    def test_claim_without_org_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.claim(Actor('d01', 'dispatcher', ''), self.record["id"])

    def test_claim_finished_rejected(self):
        record = self.service.claim(dispatcher('d01', 'east'), self.record["id"])
        self.service.act(dispatcher('d01', 'east'), record["id"], record["version"], 'cancel', {'cancel_reason': '误报'})
        with self.assertRaises(Conflict):
            self.service.claim(dispatcher('d02', 'east'), self.record["id"])

    def test_unclaimed_and_mine_task_views(self):
        unclaimed = self.service.list_tasks(dispatcher('d01', 'east'), scope='unclaimed')
        self.assertEqual([item["id"] for item in unclaimed], [self.record["id"]])
        self.service.claim(dispatcher('d01', 'east'), self.record["id"])
        self.assertEqual(self.service.list_tasks(dispatcher('d02', 'east'), scope='unclaimed'), [])
        mine = self.service.list_tasks(dispatcher('d01', 'east'), scope='mine')
        self.assertEqual(mine[0]["owner_id"], "d01")
        # 其他机构看不到这条任务
        self.assertEqual(self.service.list_tasks(dispatcher('d09', 'west'), scope='open'), [])

    def test_owner_mismatch_can_only_view(self):
        self.service.claim(dispatcher('d01', 'east'), self.record["id"])
        with self.assertRaises(PermissionDenied) as ctx:
            self.service.act(dispatcher('d02', 'east'), self.record["id"], self.record["version"], 'assign', ASSIGN)
        self.assertIn("d01", str(ctx.exception))
        # 读取仍然允许
        view = self.service.get_record(dispatcher('d02', 'east'), self.record["id"])
        self.assertEqual(view["owner_id"], "d01")

    def test_concurrent_claims_only_one_wins(self):
        outcomes = []

        def claim(user):
            try:
                self.service.claim(dispatcher(user, 'east'), self.record["id"])
                outcomes.append((user, 'ok'))
            except Conflict:
                outcomes.append((user, 'conflict'))

        threads = [threading.Thread(target=claim, args=(u,)) for u in ('d01', 'd02')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [user for user, result in outcomes if result == 'ok']
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.service.get_record(dispatcher('d01', 'east'), self.record["id"])["owner_id"], winners[0])


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)
        self.record = self.service.create(dispatcher('d-old', 'east'), "EMG-32001", CREATE_DATA)
        self.record = self.service.claim(dispatcher('d-old', 'east'), self.record["id"])

    def tearDown(self):
        self.temp.cleanup()

    def _start(self, to_user='d-new', note='夜班交接'):
        return self.service.start_handover(
            dispatcher('d-old', 'east'), self.record["id"], {'to_user': to_user, 'note': note}
        )

    def test_old_owner_keeps_working_until_accepted(self):
        result = self._start()
        self.assertEqual(result["handover"]["status"], HANDOVER_PENDING)
        # 待确认交接在记录详情可见
        detail = self.service.get_record(dispatcher('d-old', 'east'), self.record["id"])
        self.assertEqual(detail["pending_handover"]["to_user"], "d-new")
        # 接班人确认前不能办理
        with self.assertRaises(PermissionDenied):
            self.service.act(dispatcher('d-new', 'east'), self.record["id"], self.record["version"], 'assign', ASSIGN)
        # 原调度员仍可办理
        record = self.service.act(dispatcher('d-old', 'east'), self.record["id"], self.record["version"], 'assign', ASSIGN)
        self.assertEqual(record["state"], "assigned")
        self.assertEqual(record["owner_id"], "d-old")

    def test_accept_transfers_owner_and_old_account_readonly(self):
        started = self._start()
        accepted = self.service.accept_handover(dispatcher('d-new', 'east'), started["handover"]["id"])
        self.assertEqual(accepted["handover"]["status"], HANDOVER_ACCEPTED)
        self.assertEqual(accepted["record"]["owner_id"], "d-new")
        detail = self.service.get_record(dispatcher('d-new', 'east'), self.record["id"])
        # 确认后旧账号无论用旧版本还是最新版本都只能查看
        with self.assertRaises(PermissionDenied) as ctx:
            self.service.act(dispatcher('d-old', 'east'), self.record["id"], self.record["version"], 'enroute', {'traffic_level': 'low'})
        self.assertIn("只能查看", str(ctx.exception))
        with self.assertRaises(PermissionDenied):
            self.service.act(dispatcher('d-old', 'east'), self.record["id"], detail["version"], 'enroute', {'traffic_level': 'low'})
        # 新负责人可继续办理
        record = self.service.act(dispatcher('d-new', 'east'), self.record["id"], detail["version"], 'assign', ASSIGN)
        self.assertEqual(record["state"], "assigned")
        self.assertEqual(record["owner_id"], "d-new")
        self.assertIsNone(self.service.get_record(dispatcher('d-new', 'east'), self.record["id"])["pending_handover"])

    def test_duplicate_handover_start_rejected(self):
        self._start()
        with self.assertRaises(Conflict):
            self._start()

    def test_non_owner_cannot_start_handover(self):
        with self.assertRaises(PermissionDenied):
            self.service.start_handover(dispatcher('d-new', 'east'), self.record["id"], {'to_user': 'd-other'})

    def test_cross_org_handover_rejected(self):
        # 接班人必须同机构
        with self.assertRaises(ValidationError):
            self.service.start_handover(
                dispatcher('d-old', 'east'), self.record["id"], {'to_user': 'd-west', 'to_org': 'west'}
            )
        # 不能对其他机构的任务发起交接
        other = self.service.create(dispatcher('creator', 'west'), "EMG-32002", CREATE_DATA)
        with self.assertRaises(PermissionDenied):
            self.service.start_handover(dispatcher('d-old', 'east'), other["id"], {'to_user': 'd-ww'})

    def test_handover_to_self_rejected(self):
        with self.assertRaises(ValidationError):
            self._start(to_user='d-old')

    def test_wrong_person_cannot_accept(self):
        started = self._start()
        with self.assertRaises(PermissionDenied):
            self.service.accept_handover(dispatcher('d-other', 'east'), started["handover"]["id"])
        with self.assertRaises(PermissionDenied):
            self.service.accept_handover(dispatcher('d-new', 'west'), started["handover"]["id"])

    def test_decline_keeps_old_owner(self):
        started = self._start()
        result = self.service.decline_handover(dispatcher('d-new', 'east'), started["handover"]["id"], "正在处理另一条")
        self.assertEqual(result["handover"]["status"], HANDOVER_DECLINED)
        self.assertEqual(result["record"]["owner_id"], "d-old")
        # 拒绝后可重新发起
        again = self._start()
        self.assertEqual(again["handover"]["status"], HANDOVER_PENDING)

    def test_double_decision_rejected(self):
        started = self._start()
        self.service.decline_handover(dispatcher('d-new', 'east'), started["handover"]["id"], "没空")
        with self.assertRaises(Conflict):
            self.service.accept_handover(dispatcher('d-new', 'east'), started["handover"]["id"])

    def test_revoke_by_old_owner(self):
        started = self._start()
        with self.assertRaises(PermissionDenied):
            self.service.revoke_handover(dispatcher('d-new', 'east'), started["handover"]["id"])
        result = self.service.revoke_handover(dispatcher('d-old', 'east'), started["handover"]["id"])
        self.assertEqual(result["handover"]["status"], HANDOVER_REVOKED)
        self.assertEqual(result["record"]["owner_id"], "d-old")

    def test_incoming_and_outgoing_lists(self):
        started = self._start()
        incoming = self.service.list_handovers(dispatcher('d-new', 'east'), direction='incoming', status=HANDOVER_PENDING)
        self.assertEqual([item["id"] for item in incoming], [started["handover"]["id"]])
        self.assertEqual(self.service.list_handovers(dispatcher('d-old', 'east'), direction='incoming'), [])
        outgoing = self.service.list_handovers(dispatcher('d-old', 'east'), direction='outgoing', status=HANDOVER_PENDING)
        self.assertEqual(len(outgoing), 1)
        with self.assertRaises(PermissionDenied):
            self.service.get_handover(Actor('outsider', 'paramedic', 'east'), started["handover"]["id"])

    def test_handover_not_found(self):
        with self.assertRaises(NotFound):
            self.service.accept_handover(dispatcher('d-new', 'east'), 9999)


class PersistenceTest(unittest.TestCase):
    def test_claim_and_pending_handover_survive_restart(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = str(Path(temp.name) / "persist.db")
        service = build_service(db)
        record = service.create(dispatcher('d-old', 'east'), "EMG-33001", CREATE_DATA)
        service.claim(dispatcher('d-old', 'east'), record["id"])
        started = service.start_handover(dispatcher('d-old', 'east'), record["id"], {'to_user': 'd-new'})

        # 重新组装服务，模拟进程重启
        rebuilt_repo = Repository(db)
        rebuilt = Service(rebuilt_repo, DomainRules(), AuditRecorder(rebuilt_repo))
        detail = rebuilt.get_record(dispatcher('d-new', 'east'), record["id"])
        self.assertEqual(detail["owner_id"], "d-old")
        self.assertEqual(detail["pending_handover"]["to_user"], "d-new")

        accepted = rebuilt.accept_handover(dispatcher('d-new', 'east'), started["handover"]["id"])
        self.assertEqual(accepted["record"]["owner_id"], "d-new")
        timeline = rebuilt.timeline(dispatcher('d-old', 'east'), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions, ["created", "claim", "handover_start", "handover_accept"])

    def test_old_db_file_gains_seat_columns(self):
        """无席位字段的旧库启动后自动加列，旧记录显示为待认领。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = str(Path(temp.name) / "legacy.db")
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT UNIQUE, state TEXT, version INTEGER, payload TEXT, created_by TEXT, updated_by TEXT, created_at TEXT, updated_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER, action TEXT, actor_id TEXT, version INTEGER, details TEXT, created_at TEXT)"
            )
            conn.execute("INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES('EMG-1','received',1,'{}','x','x','t','t')")
        service = build_service(db)
        tasks = service.list_tasks(dispatcher('d01', ''), scope='unclaimed')
        self.assertEqual(tasks[0]["reference"], "EMG-1")
        self.assertIsNone(tasks[0]["owner_id"])


if __name__ == "__main__":
    unittest.main()
