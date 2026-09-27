"""业务用例编排、席位认领、换班交接、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError, optional_text, text
from .repository import (
    HANDOVER_ACCEPTED,
    HANDOVER_DECLINED,
    HANDOVER_PENDING,
    HANDOVER_REVOKED,
    Repository,
)
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

    def _seat_actor(self, actor: Actor) -> Actor:
        """席位操作只面向调度员；admin是运维角色，不能占席或交接。"""
        actor = self._actor(actor)
        if actor.role != "dispatcher":
            raise PermissionDenied("仅调度员可执行席位认领与换班交接")
        if not actor.organization.strip():
            raise PermissionDenied("缺少所属机构(X-Org)，无法按机构认领")
        return actor

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, actor.organization.strip())

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def list_tasks(self, actor: Actor, scope: str = "open", limit: int = 100) -> List[Dict[str, Any]]:
        """席位视图：open=本机构全部未结束，unclaimed=待认领，mine=我负责的。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if scope not in ("open", "unclaimed", "mine"):
            raise ValidationError("scope只能是open/unclaimed/mine")
        return self.repository.list_tasks(
            organization=actor.organization.strip() or None, scope=scope, owner_id=actor.user_id, limit=limit
        )

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        record["pending_handover"] = self.repository.find_pending_handover(record_id)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        # 同一时刻一单只归一人：调度员的办理动作必须由当前负责人本人发起。
        if actor.role == "dispatcher":
            self._require_owner(actor, record)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    @staticmethod
    def _require_owner(actor: Actor, record: Dict[str, Any]) -> None:
        if not record.get("owner_id"):
            raise PermissionDenied("任务尚未被认领，请先认领后再办理")
        if record["owner_id"] != actor.user_id:
            raise PermissionDenied("任务当前由调度员%s负责，您只能查看" % record["owner_id"])

    # ---- 席位认领 ----

    def claim(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._seat_actor(Actor(actor.user_id, actor.role, actor.organization))
        record = self.repository.get(record_id)
        if record["state"] in self.rules.FINISHED_STATES:
            raise Conflict("任务已结束，不能认领")
        if record.get("organization") != actor.organization:
            raise PermissionDenied("不能跨机构认领：任务属于机构%s" % (record.get("organization") or "（空）"))
        if record.get("owner_id") == actor.user_id:
            raise Conflict("任务已由您认领，请勿重复认领")
        if record.get("owner_id"):
            raise Conflict("任务已由调度员%s认领，同一时刻只能由一人办理" % record["owner_id"])
        claimed = self.repository.try_claim(record_id, actor.user_id, actor.organization)
        claimed["pending_handover"] = None
        return claimed

    # ---- 换班交接 ----

    def start_handover(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._seat_actor(actor)
        record = self.repository.get(record_id)
        if record["state"] in self.rules.FINISHED_STATES:
            raise Conflict("任务已结束，不能发起交接")
        if record.get("organization") != actor.organization:
            raise PermissionDenied("不能跨机构交接：任务属于机构%s" % (record.get("organization") or "（空）"))
        if record.get("owner_id") != actor.user_id:
            raise PermissionDenied("只有当前负责人才可以发起交接")
        to_user = text(data or {}, "to_user")
        if to_user == actor.user_id:
            raise ValidationError("接班人不能是本人")
        to_org = optional_text(data or {}, "to_org", actor.organization)
        if to_org != actor.organization:
            raise ValidationError("接班人必须与当前负责人属于同一机构")
        note = optional_text(data or {}, "note")
        handover = self.repository.insert_handover(record_id, actor.user_id, actor.organization, to_user, to_org, note)
        return self._handover_view(self.repository.get(record_id), handover)

    def accept_handover(self, actor: Actor, handover_id: int) -> Dict[str, Any]:
        actor = self._seat_actor(actor)
        handover = self.repository.get_handover(handover_id)
        record = self.repository.get(handover["record_id"])
        if handover["to_user"] != actor.user_id:
            raise PermissionDenied("只有指定接班人才可以确认该交接")
        if handover["to_org"] != actor.organization or record.get("organization") != actor.organization:
            raise PermissionDenied("不能跨机构确认交接")
        if record["state"] in self.rules.FINISHED_STATES:
            raise Conflict("任务已结束，不能确认交接")
        if record.get("owner_id") != handover["from_user"]:
            raise Conflict("负责人已变更，该交接已失效，请重新发起")
        record, handover = self.repository.resolve_handover(
            handover_id,
            actor.user_id,
            HANDOVER_ACCEPTED,
            transfer=True,
            audit_action="handover_accept",
            details={"from": handover["from_user"], "to": actor.user_id, "note": handover["note"]},
        )
        return self._handover_view(record, handover)

    def decline_handover(self, actor: Actor, handover_id: int, reason: str = "") -> Dict[str, Any]:
        actor = self._seat_actor(actor)
        handover = self.repository.get_handover(handover_id)
        if handover["to_user"] != actor.user_id:
            raise PermissionDenied("只有指定接班人才可以拒绝该交接")
        if handover["to_org"] != actor.organization:
            raise PermissionDenied("不能跨机构处理交接")
        record, handover = self.repository.resolve_handover(
            handover_id,
            actor.user_id,
            HANDOVER_DECLINED,
            transfer=False,
            audit_action="handover_decline",
            details={"from": handover["from_user"], "to": actor.user_id, "reason": reason},
        )
        return self._handover_view(record, handover)

    def revoke_handover(self, actor: Actor, handover_id: int) -> Dict[str, Any]:
        actor = self._seat_actor(actor)
        handover = self.repository.get_handover(handover_id)
        if handover["from_user"] != actor.user_id:
            raise PermissionDenied("只有发起交接的负责人才可以撤销")
        if handover["from_org"] != actor.organization:
            raise PermissionDenied("不能跨机构处理交接")
        record, handover = self.repository.resolve_handover(
            handover_id,
            actor.user_id,
            HANDOVER_REVOKED,
            transfer=False,
            audit_action="handover_revoke",
            details={"from": actor.user_id, "to": handover["to_user"]},
        )
        return self._handover_view(record, handover)

    def list_handovers(self, actor: Actor, direction: str = "incoming", status: Optional[str] = None) -> List[Dict[str, Any]]:
        """incoming=待我确认/我处理的，outgoing=我发起的。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if direction not in ("incoming", "outgoing"):
            raise ValidationError("direction只能是incoming/outgoing")
        if status and status not in (HANDOVER_PENDING, HANDOVER_ACCEPTED, HANDOVER_DECLINED, HANDOVER_REVOKED):
            raise ValidationError("status不合法")
        return self.repository.list_handovers(user=actor.user_id, direction=direction, status=status)

    def get_handover(self, actor: Actor, handover_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        handover = self.repository.get_handover(handover_id)
        if handover["from_user"] != actor.user_id and handover["to_user"] != actor.user_id:
            raise PermissionDenied("交接单与您无关，无权查看")
        record = self.repository.get(handover["record_id"])
        return self._handover_view(record, handover)

    @staticmethod
    def _handover_view(record: Dict[str, Any], handover: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "handover": handover,
            "record": {
                "id": record["id"],
                "reference": record["reference"],
                "state": record["state"],
                "organization": record.get("organization", ""),
                "owner_id": record.get("owner_id"),
                "version": record["version"],
            },
        }

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
