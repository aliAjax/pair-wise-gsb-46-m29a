"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository, ENDED_STATES
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _enrich(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["pending_handover"] = self.repository.pending_handover(int(record["id"]))
        return record

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, actor.organization)
        return self._enrich(record)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        handovers = self.repository.pending_handover_map(int(record["id"]) for record in records)
        for record in records:
            record["pending_handover"] = handovers.get(int(record["id"]))
        return records

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._enrich(self.repository.get(record_id))

    def claim(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        """调度员认领本机构未结束任务；同一时刻一单只归一人。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_claim_seat(actor.role):
            raise PermissionDenied("仅调度员可以认领任务席位")
        record = self.repository.get(record_id)
        if record["state"] in ENDED_STATES:
            raise Conflict("任务已结束，无法认领")
        record_org = record.get("organization") or ""
        if record_org != actor.organization:
            raise Conflict("任务属于机构「%s」，不能跨机构认领" % (record_org or "未分配"))
        if record.get("owner_id"):
            if record["owner_id"] == actor.user_id:
                raise Conflict("任务已由你认领，请勿重复认领")
            raise Conflict("任务已由调度员「%s」认领，同一时刻一单只归一人" % record["owner_id"])
        record = self.repository.claim(record_id, actor.user_id, actor.organization)
        return self._enrich(record)

    def start_handover(self, actor: Actor, record_id: int, to_user: str) -> Dict[str, Any]:
        """负责人发起交接；接班人确认前，负责人保持不变、继续办理。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        to_user = text({"to_user": to_user or ""}, "to_user")
        if not self.rules.role_can_claim_seat(actor.role):
            raise PermissionDenied("仅调度员可以发起换班交接")
        record = self.repository.get(record_id)
        if record["state"] in ENDED_STATES:
            raise Conflict("任务已结束，无需交接")
        owner = record.get("owner_id")
        if not owner and actor.role != "admin":
            raise PermissionDenied("任务尚未认领，请先认领席位再发起交接")
        if actor.role != "admin" and owner != actor.user_id:
            raise PermissionDenied("仅当前负责人「%s」可以发起交接，负责人不符" % (owner or ""))
        record_org = record.get("organization") or ""
        if record_org != actor.organization:
            raise Conflict("任务属于机构「%s」，不能跨机构发起交接" % (record_org or "未分配"))
        if to_user == actor.user_id:
            raise Conflict("接班人不能是当前负责人自己")
        pending = self.repository.pending_handover(record_id)
        if pending is not None:
            raise Conflict("已有待确认交接：等待「%s」确认，不能重复发起" % pending["to_user"])
        self.repository.create_handover(record_id, actor.user_id, actor.organization, to_user)
        return self._enrich(self.repository.get(record_id))

    def confirm_handover(self, actor: Actor, record_id: int, handover_id: int) -> Dict[str, Any]:
        """接班人确认交接；确认后负责人切换，旧账号只能查看。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        pending = self.repository.pending_handover(record_id)
        if pending is None:
            raise Conflict("该任务当前没有待确认的交接")
        if int(pending["id"]) != int(handover_id):
            raise Conflict("交接编号与当前待确认交接不符，请刷新后重试")
        if pending["to_user"] != actor.user_id:
            raise PermissionDenied("仅接班人「%s」可以确认交接，负责人不符" % pending["to_user"])
        record_org = record.get("organization") or ""
        if record_org != actor.organization:
            raise Conflict("任务属于机构「%s」，不能跨机构确认交接" % (record_org or "未分配"))
        if record["state"] in ENDED_STATES:
            self.repository.invalidate_pending_handover(record_id)
            raise Conflict("任务已结束，交接自动失效")
        record = self.repository.confirm_handover(int(handover_id), record_id, actor.user_id, actor.organization)
        return self._enrich(record)

    def cancel_handover(self, actor: Actor, record_id: int) -> None:
        """交接确认前，原负责人可撤销交接，继续办理。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        pending = self.repository.pending_handover(record_id)
        if pending is None:
            raise Conflict("该任务当前没有待确认的交接")
        if actor.role != "admin" and pending["from_user"] != actor.user_id:
            raise PermissionDenied("仅交接发起人「%s」可以撤销，负责人不符" % pending["from_user"])
        self.repository.cancel_handover(record_id, pending["from_user"] if actor.role == "admin" else actor.user_id)

    def my_pending_handovers(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_pending_handovers(actor.user_id)

    def handovers(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_handovers(record_id)

    def _ensure_seat_owner(self, actor: Actor, record: Dict[str, Any]) -> None:
        """调度员角色的写操作必须是当前负责人；交接确认后旧账号只能查看。"""
        if actor.role == "admin":
            return
        if not self.rules.role_uses_seat(actor.role):
            return
        # 无机构信息的历史记录保持开放，兼容存量数据
        if not (record.get("organization") or ""):
            return
        owner = record.get("owner_id")
        if not owner:
            raise PermissionDenied("任务尚未认领，请先认领席位后再操作")
        if owner != actor.user_id:
            pending = self.repository.pending_handover(int(record["id"]))
            if pending is not None and pending["from_user"] == actor.user_id:
                raise PermissionDenied("交接已发起，等待「%s」确认；确认后你只能查看该任务" % pending["to_user"])
            raise PermissionDenied("当前负责人是「%s」，负责人不符，你只能查看该任务" % owner)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self._ensure_seat_owner(actor, record)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        record = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        # 任务结束后任何待确认交接都不再有意义
        if new_state in ENDED_STATES:
            self.repository.invalidate_pending_handover(record_id)
        return self._enrich(record)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
