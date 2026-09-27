import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import json
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.http_api import create_server


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'hospital_beds': 4, 'destination': 'City Hospital', 'location': 'East Gate'}

ALICE = Actor("alice", "dispatcher", "station-a")
BOB = Actor("bob", "dispatcher", "station-a")
CAROL = Actor("carol", "dispatcher", "station-b")
ASSIGN = {'vehicle_available': True, 'vehicle_id': 'AMB-07'}


class SeatServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)
        self.record = self.service.create(Actor("creator", "dispatcher", "station-a"), "EMG-31001", CREATE_DATA)

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_and_duplicate_claim(self):
        claimed = self.service.claim(ALICE, self.record["id"])
        self.assertEqual(claimed["owner_id"], "alice")
        self.assertEqual(claimed["owner_org"], "station-a")
        with self.assertRaises(Conflict) as ctx:
            self.service.claim(BOB, self.record["id"])
        self.assertIn("已由调度员「alice」认领", str(ctx.exception))
        with self.assertRaises(Conflict) as ctx2:
            self.service.claim(ALICE, self.record["id"])
        self.assertIn("请勿重复认领", str(ctx2.exception))

    def test_cross_org_claim_rejected(self):
        with self.assertRaises(Conflict) as ctx:
            self.service.claim(CAROL, self.record["id"])
        self.assertIn("跨机构", str(ctx.exception))

    def test_non_owner_dispatcher_cannot_act(self):
        self.service.claim(ALICE, self.record["id"])
        with self.assertRaises(PermissionDenied) as ctx:
            self.service.act(BOB, self.record["id"], self.record["version"], "assign", ASSIGN)
        self.assertIn("当前负责人是「alice」", str(ctx.exception))
        # 负责人可以正常办理
        record = self.service.act(ALICE, self.record["id"], self.record["version"], "assign", ASSIGN)
        self.assertEqual(record["state"], "assigned")

    def test_unclaimed_org_record_requires_claim_first(self):
        other = self.service.create(Actor("creator", "dispatcher", "station-a"), "EMG-31002", CREATE_DATA)
        with self.assertRaises(PermissionDenied) as ctx:
            self.service.act(BOB, other["id"], other["version"], "assign", ASSIGN)
        self.assertIn("尚未认领", str(ctx.exception))

    def test_handover_pending_owner_keeps_working(self):
        self.service.claim(ALICE, self.record["id"])
        pending = self.service.start_handover(ALICE, self.record["id"], "bob")
        self.assertEqual(pending["pending_handover"]["from_user"], "alice")
        self.assertEqual(pending["pending_handover"]["to_user"], "bob")
        self.assertEqual(pending["owner_id"], "alice")
        # 确认前原负责人仍能办理
        record = self.service.act(ALICE, self.record["id"], self.record["version"], "assign", ASSIGN)
        self.assertEqual(record["state"], "assigned")
        # 重复发起被拒绝
        with self.assertRaises(Conflict) as ctx:
            self.service.start_handover(ALICE, self.record["id"], "bob")
        self.assertIn("已有待确认交接", str(ctx.exception))
        # 其他人不能确认
        with self.assertRaises(PermissionDenied):
            self.service.confirm_handover(ALICE, self.record["id"], pending["pending_handover"]["id"])

    def test_handover_confirm_swaps_owner_old_account_readonly(self):
        self.service.claim(ALICE, self.record["id"])
        handover = self.service.start_handover(ALICE, self.record["id"], "bob")["pending_handover"]
        # 跨机构确认被拒
        with self.assertRaises(Conflict) as ctx:
            self.service.confirm_handover(Actor("bob", "dispatcher", "station-b"), self.record["id"], handover["id"])
        self.assertIn("跨机构", str(ctx.exception))
        confirmed = self.service.confirm_handover(BOB, self.record["id"], handover["id"])
        self.assertEqual(confirmed["owner_id"], "bob")
        self.assertIsNone(confirmed["pending_handover"])
        # 旧账号写操作被拒，但仍可查看
        record = self.service.act(BOB, self.record["id"], self.record["version"], "assign", ASSIGN)
        self.assertEqual(record["state"], "assigned")
        with self.assertRaises(PermissionDenied) as ctx:
            self.service.act(ALICE, self.record["id"], record["version"], "enroute", {'traffic_level': 'low'})
        self.assertIn("只能查看", str(ctx.exception))
        view = self.service.get_record(ALICE, self.record["id"])
        self.assertEqual(view["owner_id"], "bob")

    def test_cancel_handover(self):
        self.service.claim(ALICE, self.record["id"])
        self.service.start_handover(ALICE, self.record["id"], "bob")
        with self.assertRaises(PermissionDenied):
            self.service.cancel_handover(BOB, self.record["id"])
        self.service.cancel_handover(ALICE, self.record["id"])
        self.assertIsNone(self.service.get_record(ALICE, self.record["id"])["pending_handover"])

    def test_ended_task_cannot_claim(self):
        ended = self.service.create(Actor("creator", "dispatcher", "station-a"), "EMG-31003", CREATE_DATA)
        self.service.claim(ALICE, ended["id"])
        ended = self.service.act(ALICE, ended["id"], ended["version"], "cancel", {"cancel_reason": "duplicate call"})
        self.assertEqual(ended["state"], "cancelled")
        with self.assertRaises(Conflict) as ctx:
            self.service.claim(BOB, ended["id"])
        self.assertIn("已结束", str(ctx.exception))

    def test_start_handover_requires_owner(self):
        self.service.claim(ALICE, self.record["id"])
        with self.assertRaises(PermissionDenied) as ctx:
            self.service.start_handover(BOB, self.record["id"], "carol")
        self.assertIn("负责人不符", str(ctx.exception))

    def test_timeline_records_seat_events(self):
        self.service.claim(ALICE, self.record["id"])
        handover = self.service.start_handover(ALICE, self.record["id"], "bob")["pending_handover"]
        self.service.confirm_handover(BOB, self.record["id"], handover["id"])
        actions = [event["action"] for event in self.service.timeline(ALICE, self.record["id"])]
        self.assertEqual(actions, ["created", "claimed", "handover_started", "handover_confirmed"])


class SeatConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)
        self.record = self.service.create(Actor("creator", "dispatcher", "station-a"), "EMG-32001", CREATE_DATA)

    def tearDown(self):
        self.temp.cleanup()

    def test_parallel_claims_exactly_one_wins(self):
        barrier = threading.Barrier(2)
        results = {}

        def worker(name, actor):
            barrier.wait()
            try:
                results[name] = self.service.claim(actor, self.record["id"])
            except Conflict as exc:
                results[name] = exc

        t1 = threading.Thread(target=worker, args=("a", ALICE))
        t2 = threading.Thread(target=worker, args=("b", BOB))
        t1.start(); t2.start(); t1.join(); t2.join()

        winners = [name for name, value in results.items() if not isinstance(value, Exception)]
        self.assertEqual(len(winners), 1)
        loser = results["b" if winners[0] == "a" else "a"]
        self.assertIn("同一时刻一单只归一人", str(loser))


class SeatRestartTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_owner_and_pending_handover_survive_restart(self):
        service = build_service(self.db)
        record = service.create(Actor("creator", "dispatcher", "station-a"), "EMG-33001", CREATE_DATA)
        service.claim(ALICE, record["id"])
        handover = service.start_handover(ALICE, record["id"], "bob")["pending_handover"]

        restarted = build_service(self.db)
        view = restarted.get_record(BOB, record["id"])
        self.assertEqual(view["owner_id"], "alice")
        self.assertIsNotNone(view["pending_handover"])
        self.assertEqual(view["pending_handover"]["id"], handover["id"])
        confirmed = restarted.confirm_handover(BOB, record["id"], handover["id"])
        self.assertEqual(confirmed["owner_id"], "bob")


class SeatHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)
        self.static = Path(__file__).resolve().parent.parent / "static"
        self.server = create_server("127.0.0.1", 0, self.service, self.static)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _request(self, method, path, body=None, actor=ALICE):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request("http://127.0.0.1:%s%s" % (self.port, path), data=data, method=method)
        request.add_header("X-User-Id", actor.user_id)
        request.add_header("X-Role", actor.role)
        request.add_header("X-Org", actor.organization)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_handover_over_http(self):
        status, record = self._request("POST", "/api/records", {"reference": "EMG-41001", "data": CREATE_DATA})
        self.assertEqual(status, 201)
        record_id = record["id"]

        status, claimed = self._request("POST", "/api/records/%s/claim" % record_id, {}, ALICE)
        self.assertEqual(status, 200)
        self.assertEqual(claimed["owner_id"], "alice")

        status, payload = self._request("POST", "/api/records/%s/claim" % record_id, {}, BOB)
        self.assertEqual(status, 409)
        self.assertIn("已由调度员「alice」认领", payload["message"])

        status, payload = self._request("POST", "/api/records/%s/claim" % record_id, {}, CAROL)
        self.assertEqual(status, 409)
        self.assertIn("跨机构", payload["message"])

        status, payload = self._request("POST", "/api/records/%s/actions/assign" % record_id,
                                        {"expected_version": 1, "data": ASSIGN}, BOB)
        self.assertEqual(status, 403)
        self.assertIn("负责人不符", payload["message"])

        status, pending = self._request("POST", "/api/handovers", {"record_id": record_id, "to_user": "bob"}, ALICE)
        self.assertEqual(status, 201)
        handover_id = pending["pending_handover"]["id"]

        status, payload = self._request("POST", "/api/records/%s/handovers/%s/confirm" % (record_id, handover_id), {}, ALICE)
        self.assertEqual(status, 403)

        status, confirmed = self._request("POST", "/api/records/%s/handovers/%s/confirm" % (record_id, handover_id), {}, BOB)
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["owner_id"], "bob")

        status, payload = self._request("GET", "/api/records/%s/handovers" % record_id, None, ALICE)
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][-1]["status"], "confirmed")
