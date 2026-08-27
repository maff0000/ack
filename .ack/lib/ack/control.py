from datetime import timedelta
import re
from typing import Any

from .errors import AckError
from .time import parse_utc, utc_now, utc_text


STATUS_FIELDS = {
    "project", "task", "agent_instance", "role", "model", "status", "phase",
    "started_at_utc", "heartbeat_at_utc", "progress_at_utc", "current_action",
    "base_commit", "worktree", "result", "commit", "error", "health",
    "stop_reason", "usage_tokens", "usage_cost_usd",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

RENEW_SCRIPT = """-- ACK_RENEW
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[2])
redis.call('HSET', KEYS[2], 'lease_owner', ARGV[4], 'lease_until_utc', ARGV[3])
return 1
"""
START_SCRIPT = """-- ACK_START
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('HSET', KEYS[2], 'project',ARGV[3],'task',ARGV[4],'agent_instance',ARGV[5],'role',ARGV[6],'model',ARGV[7],'status','starting','phase','startup','started_at_utc',ARGV[8],'heartbeat_at_utc',ARGV[8],'progress_at_utc',ARGV[8],'current_action','loading task context','base_commit',ARGV[9],'worktree',ARGV[10],'result','','commit','','error','','health','progressing','stop_reason','','usage_tokens','0','usage_cost_usd','')
redis.call('HSET', KEYS[3], 'status','starting','agent_instance',ARGV[5],'lease_owner',ARGV[5],'lease_until_utc',ARGV[11])
redis.call('XADD', KEYS[4], '*', 'project',ARGV[3], 'task',ARGV[4], 'agent_instance',ARGV[5], 'event','task_started', 'utc',ARGV[8])
return 1
"""
PROGRESS_SCRIPT = """-- ACK_PROGRESS
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[2], 'status','working','phase',ARGV[2],'current_action',ARGV[3],'progress_at_utc',ARGV[4])
redis.call('HSET', KEYS[3], 'status','working')
redis.call('XADD', KEYS[4], '*', 'project',ARGV[5], 'task',ARGV[6], 'agent_instance',ARGV[7], 'event','phase_changed', 'utc',ARGV[4], 'summary',ARGV[8])
return 1
"""
FINISH_SCRIPT = """-- ACK_FINISH
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[2], 'status',ARGV[2],'phase',ARGV[3],'heartbeat_at_utc',ARGV[4],'progress_at_utc',ARGV[4],'current_action',ARGV[5],'result',ARGV[6],'commit',ARGV[7],'error',ARGV[8])
redis.call('HSET', KEYS[3], 'status',ARGV[2],'result',ARGV[6],'commit',ARGV[7],'error',ARGV[8])
redis.call('XADD', KEYS[4], '*', 'project',ARGV[9], 'task',ARGV[10], 'agent_instance',ARGV[11], 'event',ARGV[5], 'utc',ARGV[4], 'result',ARGV[6], 'commit',ARGV[7], 'summary',ARGV[8])
redis.call('DEL', KEYS[1])
return 1
"""
SLOT_SCRIPT = """-- ACK_SLOT
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if ARGV[2] == 'release' then redis.call('DEL', KEYS[1]) else redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return 1
"""
GUARD_SCRIPT = """-- ACK_GUARD
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[2], 'health', ARGV[2], 'stop_reason', ARGV[3], 'current_action', ARGV[4], 'usage_tokens', ARGV[5], 'usage_cost_usd', ARGV[6])
redis.call('HSET', KEYS[3], 'health', ARGV[2], 'stop_reason', ARGV[3], 'usage_tokens', ARGV[5], 'usage_cost_usd', ARGV[6])
redis.call('XADD', KEYS[4], '*', 'project', ARGV[7], 'task', ARGV[8], 'agent_instance', ARGV[9], 'event', 'worker_guard', 'utc', ARGV[10], 'summary', ARGV[11])
return 1
"""


class ControlPlane:
    def __init__(self, redis_client: Any, project: str):
        self._validate_id(project, "project")
        self.redis = redis_client
        self.project = project
        self.prefix = f"ack:{project}"

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise AckError(f"{label} must use 1-64 letters, digits, hyphens, or underscores")

    @staticmethod
    def _concise(value: Any, limit: int = 240) -> str:
        text = str(value or "")
        if len(text) > limit or any(ord(c) < 32 and c not in "\t" for c in text):
            raise AckError("Redis operational text must be concise and single-line")
        return text.replace("\t", " ")

    def agent_key(self, agent: str) -> str: self._validate_id(agent,"agent_instance"); return f"{self.prefix}:agent:{agent}"
    def task_key(self, task: str) -> str: self._validate_id(task,"task id"); return f"{self.prefix}:task:{task}"
    def lease_key(self, task: str) -> str: return f"{self.task_key(task)}:lease"
    @property
    def events_key(self) -> str: return f"{self.prefix}:events"
    @property
    def pl_key(self) -> str: return f"{self.prefix}:pl"
    def slot_key(self, number: int) -> str: return f"{self.prefix}:slot:{number}"

    @staticmethod
    def _clean(fields: dict[str, Any]) -> dict[str, str]:
        unknown = set(fields) - STATUS_FIELDS
        if unknown:
            raise AckError(f"unsafe/unrecognised Redis status fields: {', '.join(sorted(unknown))}")
        return {k: ControlPlane._concise(v, 500 if k in {"error"} else 240) for k, v in fields.items()}

    def start(self, task: dict[str, Any], agent: str, token: str, lease_seconds: int) -> None:
        self._validate_id(agent, "agent_instance")
        now = utc_text()
        until = utc_text(utc_now() + timedelta(seconds=lease_seconds))
        ok = self.redis.eval(START_SCRIPT, 4, self.lease_key(task["id"]), self.agent_key(agent), self.task_key(task["id"]), self.events_key, token, lease_seconds, self.project, task["id"], agent, task["role"], task["model"], now, task.get("base_commit", ""), task.get("worktree", ""), until)
        if not ok: raise AckError(f"task lease already owned: {task['id']}")

    def acquire_lease(self, task: str, token: str, seconds: int) -> bool:
        return bool(self.redis.set(self.lease_key(task), token, nx=True, ex=seconds))

    def acquire_slot(self, agent: str, token: str, maximum: int, seconds: int) -> int | None:
        self._validate_id(agent, "agent_instance")
        for number in range(1, maximum + 1):
            if self.redis.set(self.slot_key(number), token, nx=True, ex=seconds): return number
        return None

    def renew_slot(self, token: str, number: int, seconds: int) -> None:
        if not self.redis.eval(SLOT_SCRIPT, 1, self.slot_key(number), token, seconds):
            raise AckError("worker slot expired or was replaced")

    def release_slot(self, token: str, number: int) -> None:
        if not self.redis.eval(SLOT_SCRIPT, 1, self.slot_key(number), token, "release"):
            raise AckError("worker cannot release a slot it does not own")

    def renew(self, task: str, agent: str, token: str, seconds: int) -> None:
        until = utc_text(utc_now() + timedelta(seconds=seconds))
        ok = self.redis.eval(RENEW_SCRIPT, 2, self.lease_key(task), self.task_key(task), token, seconds, until, agent)
        if not ok:
            raise AckError(f"cannot renew lease for {task}")

    def heartbeat(self, task: str, agent: str, token: str, lease_seconds: int) -> None:
        self.renew(task, agent, token, lease_seconds)
        self.redis.hset(self.agent_key(agent), mapping={"heartbeat_at_utc": utc_text()})

    def progress(self, task: str, agent: str, token: str, phase: str, action: str) -> None:
        now = utc_text()
        phase = self._concise(phase, 80); action = self._concise(action, 240)
        ok = self.redis.eval(PROGRESS_SCRIPT, 4, self.lease_key(task), self.agent_key(agent), self.task_key(task), self.events_key, token, phase, action, now, self.project, task, agent, f"{phase}: {action}")
        if not ok: raise AckError("expired/zombie worker cannot publish progress")

    def finish(self, task: str, agent: str, token: str, status: str, *, result: str = "", commit: str = "", error: str = "") -> None:
        if status not in {"completed", "blocked", "failed"}:
            raise AckError("worker cannot set acceptance state")
        event = {"completed": "task_completed", "blocked": "task_blocked", "failed": "task_failed"}[status]
        now = utc_text()
        clean = self._clean({"result": result, "commit": commit, "error": error})
        ok = self.redis.eval(FINISH_SCRIPT, 4, self.lease_key(task), self.agent_key(agent), self.task_key(task), self.events_key, token, status, "done" if status == "completed" else status, now, event, clean["result"], clean["commit"], clean["error"], self.project, task, agent)
        if not ok: raise AckError("expired/zombie worker cannot publish authoritative result")

    def guard(self, task: str, agent: str, token: str, classification: str, reason: str, *, usage_tokens: int = 0, usage_cost_usd: float | None = None) -> None:
        self._validate_id(task, "task id"); self._validate_id(agent, "agent_instance")
        classification = self._concise(classification, 80)
        reason = self._concise(reason, 240)
        tokens = str(max(0, int(usage_tokens)))
        cost = "" if usage_cost_usd is None else f"{usage_cost_usd:.6f}"
        summary = self._concise(f"{classification}: {reason}", 240)
        ok = self.redis.eval(GUARD_SCRIPT, 4, self.lease_key(task), self.task_key(task), self.agent_key(agent), self.events_key, token, classification, reason, summary, tokens, cost, self.project, task, agent, utc_text(), summary)
        if not ok: raise AckError("expired/zombie worker cannot publish guard state")

    def event(self, task: str, agent: str, event: str, **extra: Any) -> str:
        self._validate_id(task,"task id"); self._validate_id(agent,"agent_instance")
        fields = {"project": self.project, "task": task, "agent_instance": agent, "event": event, "utc": utc_text()}
        fields.update({k: self._concise(v, 500) for k, v in extra.items() if v})
        return self.redis.xadd(self.events_key, fields)

    def agents(self) -> list[dict[str, str]]:
        records = []
        for key in self.redis.scan_iter(match=f"{self.prefix}:agent:*"):
            raw = self.redis.hgetall(key)
            records.append({(k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v)) for k, v in raw.items()})
        return records

    @staticmethod
    def health(record: dict[str, str], degraded_seconds: int, stale_seconds: int) -> str:
        if record.get("status") in {"completed", "blocked", "failed"}:
            return "FINISHED"
        value = record.get("heartbeat_at_utc")
        if not value: return "STALE"
        age = (utc_now() - parse_utc(value)).total_seconds()
        if age >= stale_seconds: return "STALE"
        if age >= degraded_seconds: return "DEGRADED"
        return "HEALTHY"

    @staticmethod
    def progress_health(record: dict[str, str], stale_seconds: int) -> str:
        if record.get("status") != "working": return "-"
        value = record.get("progress_at_utc")
        if not value or (utc_now() - parse_utc(value)).total_seconds() >= stale_seconds:
            return "ALIVE_BUT_STALLED"
        return "PROGRESSING"

    def pl_heartbeat(self, instance: str, objective: str, scheduler_state: str) -> None:
        self.redis.hset(self.pl_key, mapping={"project": self.project, "axiom_instance": instance, "heartbeat_at_utc": utc_text(), "current_objective": objective, "scheduler_state": scheduler_state})
