"""账号池与任务存储（JSON 落盘，原子写）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid

from proxy_support import proxy_host

_LOCK = threading.Lock()

# 决定账号生死的核心 cookie（与 engine.ESSENTIAL_COOKIES 保持一致）
# 注：hatch_gw 为网关路由缓存（约1年），服务端续签与 VM 唤醒主要依赖 hatch_sess 和 hatch_vml，缺 gw 亦可正常分配路由
ESSENTIAL_COOKIES = ("hatch_sess", "hatch_vml",
                     "hatch_native_auth_device")


def _is_pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


# hatch_vml 固定约 2 天，且不因使用而延长 —— 它就是决定账号寿命的那条 cookie。
# 实测对照：生成前后 synced_at 变了、也捞到新 cookie，但 4 条核心 cookie 的
# expires 一动不动。所以拿不到 hatch_vml 的 expires 时，按「导入时间 + 2 天」估算，
# 而不是退回到 hatch_native_auth_device 的 30 天（那个不决定生死，会误导用户）。
VML_TTL = 2 * 86400


def min_expiry(cookies_exp: dict | None) -> int | None:
    """核心 cookie 里最早过期的那个（通用兜底，不区分哪条决定寿命）。"""
    if not cookies_exp:
        return None
    vals = []
    for name in ESSENTIAL_COOKIES:
        v = cookies_exp.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if not vals:
        for v in cookies_exp.values():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
    return min(vals) if vals else None


def account_expiry(cookies_exp: dict | None,
                   base_ts: int | None = None) -> int | None:
    """账号实际失效时间。

    优先级：
      1) hatch_vml 的真实 expires（最准）；
      2) 拿不到就用 base_ts(导入/同步时刻) + VML_TTL 估算 —— 因为寿命就是 2 天；
      3) 都没有再退回核心 cookie 最早过期。
    """
    ce = cookies_exp or {}
    vml = ce.get("hatch_vml")
    if _is_pos(vml):
        return int(vml)
    if base_ts:
        return int(base_ts) + VML_TTL
    return min_expiry(ce)


ROTATE_MIN, ROTATE_MAX, ROTATE_DEFAULT = 1, 50, 5
LOAD_SLOTS_MAX, LOAD_SLOTS_DEFAULT = 8, 3  # 同时加载页面的标签数
THREADS_MIN, THREADS_MAX = 1, 5  # 每账号并发任务数
ACCOUNTS_AT_ONCE_MAX = 10 # 同时工作的账号数上限


def _read(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write(path: str, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# accounts.json 加密后的文件头；其后是 Fernet 密文
_ENC_PREFIX = "MUSE2API-ENC1:"


def _fernet(key: str):
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def _read_accounts(path: str, key: str) -> list:
    """读取账号文件：设置了 MUSE2API_DATA_KEY 时解密；密文但缺密钥/密钥错误时直接报错，
    绝不当成「没有账号」继续运行（那样下一次保存会覆盖掉原文件）。"""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    if raw.startswith(_ENC_PREFIX):
        fernet = _fernet(key)
        if fernet is None:
            raise RuntimeError(f"{path} 已加密，请设置 MUSE2API_DATA_KEY 后再启动")
        from cryptography.fernet import InvalidToken
        try:
            return json.loads(fernet.decrypt(raw[len(_ENC_PREFIX):].encode()).decode("utf-8"))
        except InvalidToken:
            raise RuntimeError(f"{path} 解密失败：MUSE2API_DATA_KEY 不正确") from None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return []


def _write_accounts(path: str, accounts: list, key: str):
    fernet = _fernet(key)
    if fernet is None:
        _write(path, accounts)
        return
    token = fernet.encrypt(json.dumps(accounts, ensure_ascii=False).encode("utf-8")).decode()
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_ENC_PREFIX + token)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.accounts: list[dict] = _read_accounts(cfg.accounts_file, self._data_key)
        if self._data_key and self.accounts and not self._accounts_encrypted():
            # 设置密钥后首次启动：把明文账号文件改写为密文
            self._save_accounts()
        for a in self.accounts:
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                a["expires_at"] = int(ce["hatch_vml"])
        self.tasks: dict[str, dict] = _read(cfg.tasks_file, {})
        self.rotate_every = self._load_rotate_every()
        self._load_pool_limits()

    def _load_rotate_every(self) -> int:
        saved = _read(self.cfg.rotation_file, {}).get("every")
        if isinstance(saved, int) and not isinstance(saved, bool):
            return min(max(saved, ROTATE_MIN), ROTATE_MAX)
        try:
            return min(max(int(os.environ.get("MUSE2API_ROTATE_EVERY", "")), ROTATE_MIN), ROTATE_MAX)
        except ValueError:
            return ROTATE_DEFAULT

    def _load_pool_limits(self):
        saved = _read(self.cfg.rotation_file, {})

        def pick(key: str, lo: int, hi: int, default: int) -> int:
            v = saved.get(key)
            return min(max(v, lo), hi) if isinstance(v, int) and not isinstance(v, bool) else default

        env_default = min(max(getattr(self.cfg, "account_concurrency", 2), THREADS_MIN), THREADS_MAX)
        self.per_account = pick("perAccount", THREADS_MIN, THREADS_MAX, env_default)
        self.max_accounts = pick("maxAccounts", 1, ACCOUNTS_AT_ONCE_MAX, 5)
        self.load_slots = pick("loadSlots", 1, LOAD_SLOTS_MAX, LOAD_SLOTS_DEFAULT)

    def _save_pool_settings(self):
        _write(self.cfg.rotation_file, {"every": self.rotate_every, "perAccount": self.per_account, "maxAccounts": self.max_accounts, "loadSlots": self.load_slots})

    def set_rotate_every(self, n: int) -> int:
        """每个账号连续处理多少次请求后换号；落盘，重启后保留。"""
        n = min(max(int(n), ROTATE_MIN), ROTATE_MAX)
        with _LOCK:
            self.rotate_every = n
            self._save_pool_settings()
        return n

    def set_pool_limits(self, per_account: int | None = None, max_accounts: int | None = None, load_slots: int | None = None):
        """每个账号同时跑几个任务（1-5）、最多几个账号同时工作（1-5）；运行中立即生效，落盘。"""
        with _LOCK:
            if per_account is not None:
                self.per_account = min(max(int(per_account), THREADS_MIN), THREADS_MAX)
            if max_accounts is not None:
                self.max_accounts = min(max(int(max_accounts), 1), ACCOUNTS_AT_ONCE_MAX)
            if load_slots is not None:
                self.load_slots = min(max(int(load_slots), 1), LOAD_SLOTS_MAX)
            self._save_pool_settings()

    def usable_accounts(self) -> int:
        return sum(1 for a in self.accounts if a.get("enabled", True) and a.get("cookies"))

    def concurrency_limit(self) -> int:
        """同时运行的任务总数 = 同时工作的账号数 × 每账号任务数。"""
        return max(1, min(self.usable_accounts(), self.max_accounts) * self.per_account)

    @property
    def _data_key(self) -> str:
        return getattr(self.cfg, "data_key", "") or ""

    def _accounts_encrypted(self) -> bool:
        try:
            with open(self.cfg.accounts_file, encoding="utf-8") as f:
                return f.read(len(_ENC_PREFIX)) == _ENC_PREFIX
        except OSError:
            return False

    def _save_accounts(self):
        _write_accounts(self.cfg.accounts_file, self.accounts, self._data_key)

    # ---------- 账号 ----------
    def add_account(self, cookies: dict, label: str = "",
                    cookies_exp: dict | None = None) -> dict:
        with _LOCK:
            aid = uuid.uuid4().hex[:12]
            exp = {k: int(v) for k, v in (cookies_exp or {}).items()
                   if _is_pos(v)}
            now = int(time.time())
            acc = {"id": aid, "label": label or aid, "cookies": cookies,
                   "cookies_exp": exp, "expires_at": account_expiry(exp, now),
                   "expiry_anchor": None,
                   "enabled": True, "created_at": now,
                   "last_used": None, "last_keepalive": None, "use_count": 0,
                   "ok": None, "note": "", "synced_at": None}
            self.accounts.append(acc)
            self._save_accounts()
            return acc

    @staticmethod
    def public_account(a: dict) -> dict:
        """The account without secrets: no cookies and no proxy URL (it may hold a password)."""
        item = {k: v for k, v in a.items() if k not in ("cookies", "proxy_url")}
        item["has_proxy"] = bool(a.get("proxy_url"))
        item["proxy_host"] = proxy_host(a.get("proxy_url"))
        return item

    def list_accounts(self) -> list[dict]:
        out = []
        for a in self.accounts:
            item = self.public_account(a)
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                item["expires_at"] = int(ce["hatch_vml"])
            item["cookie_count"] = len(a.get("cookies", {}))
            item["essential_ok"] = all(
                a.get("cookies", {}).get(n) for n in ESSENTIAL_COOKIES)
            # 有效期是 hatch_vml 的实测 expires，还是按「2 天寿命」推算的
            item["expires_estimated"] = not _is_pos(ce.get("hatch_vml"))
            out.append(item)
        return out

    def delete_account(self, aid: str) -> bool:
        with _LOCK:
            n = len(self.accounts)
            self.accounts = [a for a in self.accounts if a["id"] != aid]
            self._save_accounts()
            return len(self.accounts) != n

    def pick_account(self, preferred_id: str | None = None, rotate: bool = True,
                     force_rotate: bool = False, exclude_id: str | None = None,
                     exclude_ids: set[str] | list[str] | None = None,
                     record_use: bool = True) -> dict | None:
        """选择可用账号：优先复用当前已预热的健康账号（减少切号重连耗时），每满 15 次或遇错自动轮转。
        exclude_ids: 需要排除的账号 ID 集合（例如正在被其他并发线程占用的账号）。"""
        with _LOCK:
            live = [a for a in self.accounts if a.get("enabled", True) and a.get("cookies")]
            if not live:
                return None
            # An account whose session failed is only used when no healthy one exists at all: when the healthy ones
            # are merely busy, the caller waits for them instead of being handed a broken account.
            live = [a for a in live if a.get("ok") is not False] or live
            blocked = set(exclude_ids or [])
            if exclude_id:
                blocked.add(exclude_id)
            if blocked:
                available = [a for a in live if a["id"] not in blocked]
                if available:
                    live = available
                elif exclude_ids is not None:
                    # 并发模式下要求避开所有繁忙账号，但当前无剩余空闲账号
                    return None
                elif len(live) > 1:
                    live = [a for a in live if a["id"] != exclude_id] or live
            if preferred_id and not force_rotate:
                for a in live:
                    if a["id"] == preferred_id and a.get("ok") is not False:
                        sticky_n = getattr(self, "_sticky_count", 0) if getattr(self, "_sticky_id", None) == preferred_id else 0
                        if not rotate or sticky_n < self.rotate_every:
                            self._sticky_id = preferred_id
                            self._sticky_count = sticky_n + 1
                            if record_use:
                                a["last_used"] = time.time()
                                a["use_count"] = a.get("use_count", 0) + 1
                                self._save_accounts()
                            return a
            healthy = [a for a in live if a.get("ok") is not False]
            candidates = healthy if healthy else live
            candidates.sort(key=lambda a: (a.get("last_used") or 0.0, a.get("use_count") or 0))
            acc = candidates[0]
            self._sticky_id = acc["id"]
            self._sticky_count = 1
            if record_use:
                acc["last_used"] = time.time()
                acc["use_count"] = acc.get("use_count", 0) + 1
                self._save_accounts()
            return acc

    def touch_keepalive(self, aid: str, ok: bool | None = True, note: str = ""):
        """ok=None 仅记录未确认的检测；保留上次账号状态、成功保活时间和有效期。"""
        with _LOCK:
            now_ts = int(time.time())
            for a in self.accounts:
                if a["id"] == aid:
                    if ok is not None:
                        a["ok"] = ok
                    if ok is True:
                        a["last_keepalive"] = now_ts
                        # ponytail: 只信实际 Cookie 到期时间或原导入锚点，不凭检测成功虚推 48h。
                        a["expires_at"] = account_expiry(
                            a.get("cookies_exp"), a.get("expiry_anchor") or a.get("created_at"))
                    if note:
                        a["note"] = note[:300]
                    a["checked_at"] = now_ts
            self._save_accounts()

    def mark(self, aid: str, ok: bool, note: str = ""):
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    a["ok"] = ok
                    a["note"] = note[:300]
                    a["checked_at"] = int(time.time())
                    # Muse locked the account: turn it off so rotation and keepalive leave it alone.
                    if not ok and "Muse is disconnected" in note:
                        a["enabled"] = False
            self._save_accounts()

    def get_account(self, aid: str) -> dict | None:
        for a in self.accounts:
            if a["id"] == aid:
                return a
        return None

    def update_account(self, aid: str, **kw) -> dict | None:
        """更新标签 / 启用状态 / cookies / 有效期等；值为 None 的字段跳过。

        传了 cookies_exp 会自动重算 expires_at，不用调用方自己算。
        估算锚点 expiry_anchor 的维护规则（避免有效期被反复虚推）：
          - 读到了 hatch_vml 的真实 expires  -> 清空锚点，直接用真值；
          - 读不到且锚点为空                -> 把锚点钉在当下（只钉一次）；
          - 补入全新会话 cookie（重登）      -> 调用方显式传 expiry_anchor=now 重置。
        """
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    for k, v in kw.items():
                        if v is not None:
                            a[k] = v
                    if "cookies_exp" in kw and kw["cookies_exp"] is not None:
                        ce = a["cookies_exp"]
                        if _is_pos(ce.get("hatch_vml")):
                            a["expiry_anchor"] = None
                        elif not a.get("expiry_anchor"):
                            a["expiry_anchor"] = int(time.time())
                        a["expires_at"] = account_expiry(
                            ce, a.get("expiry_anchor") or a.get("created_at"))
                    self._save_accounts()
                    return a
        return None

    def stats(self) -> dict:
        now = int(time.time())
        total = len(self.accounts)
        enabled = sum(1 for a in self.accounts if a.get("enabled", True))
        bad = sum(1 for a in self.accounts
                  if a.get("enabled", True) and a.get("ok") is False)
        healthy = sum(1 for a in self.accounts
                      if a.get("enabled", True) and a.get("ok") is True)
        expiring = sum(1 for a in self.accounts
                       if a.get("enabled", True)
                       and a.get("expires_at")
                       and a["expires_at"] - now < 3 * 86400)
        expired = sum(1 for a in self.accounts
                      if a.get("enabled", True)
                      and a.get("expires_at") and a["expires_at"] <= now)
        return {"total": total, "enabled": enabled, "disabled": total - enabled,
                "healthy": healthy, "error": bad,
                "expiring": expiring, "expired": expired}

    # ---------- 任务 ----------
    def create_task(self, kind: str, prompt: str) -> dict:
        with _LOCK:
            tid = "task_" + uuid.uuid4().hex[:20]
            t = {"id": tid, "object": "task", "kind": kind, "prompt": prompt,
                 "status": "queued", "created_at": int(time.time()),
                 "updated_at": int(time.time()), "result": None, "error": None}
            self.tasks[tid] = t
            _write(self.cfg.tasks_file, self.tasks)
            return t

    def update_task(self, tid: str, **kw):
        with _LOCK:
            t = self.tasks.get(tid)
            if not t:
                return None
            t.update(kw)
            t["updated_at"] = int(time.time())
            _write(self.cfg.tasks_file, self.tasks)
            return t

    def get_task(self, tid: str) -> dict | None:
        return self.tasks.get(tid)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        items = sorted(self.tasks.values(),
                       key=lambda t: t.get("created_at") or 0, reverse=True)
        return items[:limit]

    def delete_task(self, tid: str) -> bool:
        with _LOCK:
            if tid in self.tasks:
                del self.tasks[tid]
                _write(self.cfg.tasks_file, self.tasks)
                return True
            return False

    def clear_tasks(self, keep: int = 0) -> int:
        with _LOCK:
            items = sorted(self.tasks.values(),
                           key=lambda t: t.get("created_at") or 0, reverse=True)
            keep_ids = {t["id"] for t in items[:keep]}
            removed = len(self.tasks) - len(keep_ids)
            self.tasks = {k: v for k, v in self.tasks.items() if k in keep_ids}
            _write(self.cfg.tasks_file, self.tasks)
            return max(0, removed)
