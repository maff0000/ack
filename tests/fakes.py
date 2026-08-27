import fnmatch
import time


class FakeRedis:
    def __init__(self):
        self.hashes = {}; self.values = {}; self.expiry = {}; self.streams = {}

    def _expire(self, key):
        if key in self.expiry and self.expiry[key] <= time.time():
            self.values.pop(key, None); self.expiry.pop(key, None)

    def set(self, key, value, nx=False, ex=None):
        self._expire(key)
        if nx and key in self.values: return False
        self.values[key] = value
        if ex: self.expiry[key] = time.time() + ex
        return True

    def get(self, key): self._expire(key); return self.values.get(key)
    def expire(self, key, seconds):
        self._expire(key)
        if key not in self.values: return False
        self.expiry[key] = time.time() + seconds; return True
    def delete(self, key): self.values.pop(key, None); self.expiry.pop(key, None); return 1
    def hset(self, key, mapping): self.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()}); return len(mapping)
    def hgetall(self, key): return dict(self.hashes.get(key, {}))
    def xadd(self, key, fields):
        ident = f"{len(self.streams.get(key, [])) + 1}-0"
        self.streams.setdefault(key, []).append((ident, dict(fields))); return ident
    def scan_iter(self, match):
        for key in self.hashes:
            if fnmatch.fnmatch(key, match): yield key
    def eval(self, script, numkeys, *values):
        keys=list(values[:numkeys]); args=list(values[numkeys:])
        if "ACK_START" in script:
            if self.get(keys[0]) is not None: return 0
            self.set(keys[0],args[0],ex=int(args[1]))
            self.hset(keys[1],{"project":args[2],"task":args[3],"agent_instance":args[4],"role":args[5],"model":args[6],"status":"starting","phase":"startup","started_at_utc":args[7],"heartbeat_at_utc":args[7],"progress_at_utc":args[7],"current_action":"loading task context","base_commit":args[8],"worktree":args[9],"result":"","commit":"","error":""})
            self.hset(keys[2],{"status":"starting","agent_instance":args[4],"lease_owner":args[4],"lease_until_utc":args[10]})
            self.xadd(keys[3],{"project":args[2],"task":args[3],"agent_instance":args[4],"event":"task_started","utc":args[7]}); return 1
        owner=self.get(keys[0])
        if owner != args[0]: return 0
        if "ACK_RENEW" in script:
            self.expire(keys[0],int(args[1])); self.hset(keys[1],{"lease_owner":args[3],"lease_until_utc":args[2]}); return 1
        if "ACK_PROGRESS" in script:
            self.hset(keys[1],{"status":"working","phase":args[1],"current_action":args[2],"progress_at_utc":args[3]})
            self.hset(keys[2],{"status":"working"}); self.xadd(keys[3],{"project":args[4],"task":args[5],"agent_instance":args[6],"event":"phase_changed","utc":args[3],"summary":args[7]}); return 1
        if "ACK_GUARD" in script:
            self.hset(keys[1],{"health":args[1],"stop_reason":args[2],"current_action":args[3],"usage_tokens":args[4],"usage_cost_usd":args[5]})
            self.hset(keys[2],{"health":args[1],"stop_reason":args[2],"usage_tokens":args[4],"usage_cost_usd":args[5]})
            self.xadd(keys[3],{"project":args[6],"task":args[7],"agent_instance":args[8],"event":"worker_guard","utc":args[9],"summary":args[10]}); return 1
        if "ACK_FINISH" in script:
            self.hset(keys[1],{"status":args[1],"phase":args[2],"heartbeat_at_utc":args[3],"progress_at_utc":args[3],"current_action":args[4],"result":args[5],"commit":args[6],"error":args[7]})
            self.hset(keys[2],{"status":args[1],"result":args[5],"commit":args[6],"error":args[7]})
            self.xadd(keys[3],{"project":args[8],"task":args[9],"agent_instance":args[10],"event":args[4],"utc":args[3],"result":args[5],"commit":args[6],"summary":args[7]}); self.delete(keys[0]); return 1
        if "ACK_SLOT" in script:
            if args[1] == "release": self.delete(keys[0])
            else: self.expire(keys[0],int(args[1]))
            return 1
        raise AssertionError("unknown script")
