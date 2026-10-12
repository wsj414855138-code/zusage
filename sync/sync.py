#!/usr/bin/python3
"""agent-usage 同步器：把本机各 AI Agent 的 token 用量采集进统一库 usage.db。

适配器：
  zcode     ~/.zcode/cli/db/db.sqlite (model_usage 表，含首字/完成时间戳，可算速度)
  codex     ~/.codex/sessions/**/*.jsonl (turn_context 提供模型，token_usage_record 提供用量)
  workbuddy ~/.workbuddy/projects/**/*.jsonl (transcripts，逐请求 usage；
             模型按 sessionId 关联 workbuddy.db sessions.model)
  pi        ~/.pi/agent/sessions/**/*.jsonl (assistant 消息带 usage + cost)
  kimi      ~/.kimi-code/sessions/**/agents/*/wire.jsonl (step.end 事件自带真实
             流式时长 llmStreamDurationMs，无需估算)

统一口径：input_tokens 一律表示「不含缓存」的输入，缓存单独进 cache_read_tokens。
  zcode / pi 的来源本身就不含缓存；codex 与 workbuddy 的 input_tokens 含缓存
  （total = input + output），入库前必须相减，否则 input + cache 求和会双计。

数据目录（usage.db）固定在 ~/.zcode/cli/agent-usage/，与代码目录分离。
所有路径可用环境变量覆盖（便于测试）：
  ZUSAGE_UDB / ZUSAGE_ZCODE_DB / ZUSAGE_CODEX_DIR /
  ZUSAGE_WORKBUDDY_DB / ZUSAGE_WORKBUDDY_PROJECTS /
  ZUSAGE_KIMI_DIR / ZUSAGE_PI_DIR

幂等：所有行按 (agent, key) INSERT OR REPLACE，重复跑不会重复计数；
文件型数据源按 mtime:size 签名增量跳过（读取成功后才会记录签名）。
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


def _path(env_key, default):
    raw = os.environ.get(env_key)
    return Path(raw).expanduser() if raw else default


DATA_DIR = Path.home() / ".zcode" / "cli" / "agent-usage"
UDB = _path("ZUSAGE_UDB", DATA_DIR / "usage.db")
ZCODE_DB = _path("ZUSAGE_ZCODE_DB", Path.home() / ".zcode" / "cli" / "db" / "db.sqlite")
CODEX_SESSIONS = _path("ZUSAGE_CODEX_DIR", Path.home() / ".codex" / "sessions")
WORKBUDDY_DB = _path("ZUSAGE_WORKBUDDY_DB", Path.home() / ".workbuddy" / "workbuddy.db")
WORKBUDDY_PROJECTS = _path("ZUSAGE_WORKBUDDY_PROJECTS", Path.home() / ".workbuddy" / "projects")
KIMI_SESSIONS = _path("ZUSAGE_KIMI_DIR", Path.home() / ".kimi-code" / "sessions")
PI_SESSIONS = _path("ZUSAGE_PI_DIR", Path.home() / ".pi" / "agent" / "sessions")

BACKFILL_MS = int((time.time() - 30 * 86400) * 1000)   # 首次全量回填窗口（30 天）
TAIL_MS = 3 * 3600 * 1000                              # 增量重扫尾部 3 小时（兼容 running→completed）

# 提取逻辑版本：改动某源的解析方式时 +1，会让该源的历史文件全量重读一次，
# 否则旧文件因 mtime:size 签名未变而被跳过、修正无法回溯。
# v2: codex 模型改按 turn_id 关联；v3: codex 增加逐请求耗时（gen_ms）
CODEX_LOGIC_VERSION = 3
# v1: session_usage 会话配额口径；v2: 改用 projects transcripts 逐请求口径（含 gen_ms），
#     并在迁移时清空旧 quota 行
WORKBUDDY_LOGIC_VERSION = 2
# v2: step.end 行不带 model，改为从同文件 usage.record/llm.request 行捎带模型名
KIMI_LOGIC_VERSION = 2
# v2: pi 增加逐请求耗时（gen_ms）
PI_LOGIC_VERSION = 2

MIN_GEN_MS = 300        # 生成时长下限：低于此视为噪声（请求排队/瞬发）
MAX_GEN_MS = 600_000    # 生成时长上限：超出视为异常窗口（应用挂起等）

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  agent TEXT NOT NULL, key TEXT NOT NULL, ts INTEGER NOT NULL,
  model TEXT, status TEXT,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, gen_ms INTEGER, cost_usd REAL,
  unit TEXT DEFAULT 'token',
  PRIMARY KEY(agent, key));
CREATE TABLE IF NOT EXISTS sync_state(
  adapter TEXT PRIMARY KEY, state TEXT);
"""


def udb():
    """打开统一库：WAL + busy_timeout，避免与读端（菜单栏/CLI）互锁。"""
    UDB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(UDB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)
    cols = {row[1] for row in con.execute("PRAGMA table_info(requests)").fetchall()}
    if "cost_usd" not in cols:
        con.execute("ALTER TABLE requests ADD COLUMN cost_usd REAL")
    if "unit" not in cols:
        con.execute("ALTER TABLE requests ADD COLUMN unit TEXT")
        con.execute("UPDATE requests SET unit = 'token' WHERE unit IS NULL")
    return con


def get_state(con, adapter):
    r = con.execute("SELECT state FROM sync_state WHERE adapter=?", (adapter,)).fetchone()
    return json.loads(r[0]) if r else {}


def set_state(con, adapter, state):
    blob = json.dumps(state)
    con.execute("INSERT OR REPLACE INTO sync_state(adapter, state) VALUES(?,?)",
                (adapter, blob))


def iso_to_ms(s):
    """ISO8601 → 毫秒时间戳；null / 空串 / 超长小数 / 畸形格式一律返回 None。"""
    if not s or not isinstance(s, str):
        return None
    txt = s.replace("Z", "+00:00")
    m = re.match(r"^(.*\.\d{6})\d+", txt)   # 兼容 9 位以上小数（3.9/3.10 只认 3 或 6 位）
    if m:
        txt = m.group(1) + txt[m.end():]
    try:
        return int(datetime.fromisoformat(txt).timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return None


def open_db(path):
    """打开数据源库：优先只读，失败则退回读写连接（只执行 SELECT，不写任何数据）。

    为什么不能只用 mode=ro：WAL 模式的库在缺少 -wal/-shm 副文件时（对方应用干净退出、
    checkpoint 后删除了副文件），只读连接无法创建共享内存索引，会直接报
    "unable to open database file"（实测：连 /tmp 里的副本也一样，与权限/占用无关）。
    退回读写连接后 SQLite 可以自行建立副文件。这也是 sqlite3 命令行的默认行为。

    另外注意：绝不能加 immutable=1 —— WorkBuddy 用 WAL 写库，immutable 会跳过 WAL 丢新数据。
    """
    uri = path.as_uri() + "?mode=ro"
    try:
        ro = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        ro = None
    if ro is not None:
        try:
            # 只读连接是惰性打开的：真正打不开时错误在首次使用才抛出，必须探测一次
            ro.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return ro
        except sqlite3.Error:
            ro.close()
    try:
        return sqlite3.connect(path, timeout=3)
    except sqlite3.Error as e:
        print("库打开失败:", path, e, file=sys.stderr)
        return None


def write_token_row(con, agent, key, ts, model, status, i, o, cr, gen, cost, unit):
    """唯一的 token 行写入口（所有值先算成裸变量，这里只做参数化绑定）。"""
    con.execute("INSERT OR REPLACE INTO requests(agent, key, ts, model, status, input_tokens, output_tokens, cache_read_tokens, gen_ms, cost_usd, unit) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (agent, key, ts, model, status, i, o, cr, gen, cost, unit))


def sync_zcode(con):
    """ZCode 的 model_usage 表 → 逐请求行，含生成耗时。首轮回填 30 天，之后增量。"""
    if not ZCODE_DB.exists():
        return
    state = get_state(con, "zcode")
    since = BACKFILL_MS
    prev = state.get("since")
    if prev:
        since = max(BACKFILL_MS, int(prev) - TAIL_MS)
    src = open_db(ZCODE_DB)
    if src is None:
        print("zcode 库打开失败，跳过本轮", file=sys.stderr)
        return
    try:
        rows = src.execute("SELECT id, started_at, model_id, status, input_tokens, output_tokens, cache_read_input_tokens, first_token_at, completed_at FROM model_usage WHERE started_at >= ? ORDER BY started_at", (since,)).fetchall()
    except sqlite3.Error as e:
        print("zcode 读取失败，跳过本轮:", e, file=sys.stderr)
        return
    finally:
        src.close()
    max_ts = since
    for row in rows:
        rid = row[0]
        ts = row[1]
        model = row[2]
        status = row[3]
        i = row[4]
        o = row[5]
        cr = row[6]
        ft = row[7]
        ct = row[8]
        gen = None
        if ft and ct and ct > ft:
            gen = ct - ft
        write_token_row(con, "zcode", rid, ts, model, status, i, o, cr, gen, None, "token")
        if ts > max_ts:
            max_ts = ts
    set_state(con, "zcode", {"since": max_ts})


def sync_codex(con):
    """Codex rollout：turn_context 按 turn_id 提供模型，token_usage_record 提供用量。

    速度还原（v3）：每次 API 响应完成时写一条 token_usage_record；下一次请求由工具
    输出（response_item/custom_tool_call_output）触发，其 timestamp ≈ 请求发出时刻。
    因此单次响应生成时长 ≈ ts(usage_record) − max(上一个 tool_output, task_started)。
    该窗口含首字延迟与 agent 调度间隙，系统性偏慢，但与 ZCode 口径同数量级、可横向比较。

    注意 codex 的 input_tokens 已包含 cached_input_tokens，入库前相减归一化；
    签名在文件读取成功后才记录，读取失败的文件下轮自动重试。
    """
    if not CODEX_SESSIONS.exists():
        return
    state = get_state(con, "codex")
    force = state.get("v") != CODEX_LOGIC_VERSION
    if force:
        print("codex 提取逻辑已更新，全量重读会话文件", file=sys.stderr)
    new_state = {"v": CODEX_LOGIC_VERSION}
    for f in sorted(CODEX_SESSIONS.rglob("*.jsonl")):
        path_key = str(f)
        try:
            st = f.stat()
            sig = str(int(st.st_mtime)) + ":" + str(st.st_size)
        except OSError:
            continue
        if not force and state.get(path_key) == sig:
            new_state[path_key] = sig
            continue
        recs = []
        models = {}
        last_start = None   # 当前请求的发出时刻（ms）
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if ('"usage"' not in line and '"model"' not in line
                            and '"custom_tool_call_output"' not in line
                            and '"task_started"' not in line):
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    p = j.get("payload") or {}
                    kind = j.get("type")
                    sub = p.get("type")
                    if kind == "turn_context":
                        tid = p.get("turn_id")
                        mdl = p.get("model")
                        if tid and mdl:
                            models[tid] = mdl
                    elif kind == "event_msg" and sub == "task_started":
                        last_start = iso_to_ms(j.get("timestamp"))
                    elif kind == "response_item" and sub == "custom_tool_call_output":
                        last_start = iso_to_ms(j.get("timestamp"))
                    elif kind == "token_usage_record":
                        u = p.get("usage") or {}
                        if not u:
                            continue
                        ts = iso_to_ms(j.get("timestamp"))
                        if ts is None:
                            ts = 0
                        key = p.get("response_id")
                        if not key:
                            key = str(p.get("thread_id")) + ":" + str(j.get("ordinal"))
                        model = models.get(p.get("turn_id"))
                        cached = u.get("cached_input_tokens", 0) or 0
                        raw_in = u.get("input_tokens", 0) or 0
                        i = max(0, raw_in - cached)
                        o = u.get("output_tokens", 0) or 0
                        gen = None
                        if last_start and ts and o > 0:
                            g = ts - last_start
                            if MIN_GEN_MS < g < MAX_GEN_MS:
                                gen = g
                        recs.append((key, ts, model, i, o, cached, gen))
        except OSError as e:
            print("codex 文件读取失败，下轮重试:", path_key, e, file=sys.stderr)
            continue   # 不记签名 → 下一轮重试
        for rec in recs:
            write_token_row(con, "codex", rec[0], rec[1], rec[2], "completed",
                            rec[3], rec[4], rec[5], rec[6], None, "token")
        new_state[path_key] = sig
    set_state(con, "codex", new_state)   # 顺带清理已删除会话文件的条目


def _wb_ms(v):
    """transcript 的 timestamp 通常是毫秒整数；兼容秒级整数与 ISO 字符串。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n if n > 10**12 else n * 1000
    return iso_to_ms(v) if isinstance(v, str) else None


def wb_session_models():
    """sessionId → model（workbuddy.db sessions 表）。transcript 记录本身不带模型，
    只能按会话关联；库打不开或表缺失时返回空表（模型列退化为 NULL，不影响用量）。"""
    if not WORKBUDDY_DB.exists():
        return {}
    src = open_db(WORKBUDDY_DB)
    if src is None:
        return {}
    try:
        return dict(src.execute("SELECT id, model FROM sessions WHERE model IS NOT NULL").fetchall())
    except sqlite3.Error as e:
        print("workbuddy sessions 读取失败，本轮模型列为空:", e, file=sys.stderr)
        return {}
    finally:
        src.close()


def sync_workbuddy(con):
    """WorkBuddy projects transcripts → 逐请求行（v2，替代 session_usage 配额口径）。

    ~/.workbuddy/projects/<项目>/<sessionId>.jsonl 每行一条事件；带 usage 的
    message / function_call 记录各对应一次独立的 LLM 响应（实测相邻记录 usage
    零重复，无双计）。message.usage 口径与 codex 相同：input_tokens 含缓存、
    total = input + output，入库前相减归一化。

    速度还原（v2）：function_call_result 的 timestamp ≈ 下一次模型请求的发出时刻
    （与 pi 的 toolResult、codex 的 custom_tool_call_output 同理），其后第一条
    带 usage 记录的 timestamp ≈ 响应完成，两者之差为墙钟生成窗口，过滤
    MIN/MAX_GEN_MS 后入库 gen_ms。含首字延迟与调度间隙，系统性偏慢但跨源可比。
    """
    if not WORKBUDDY_PROJECTS.exists():
        return
    state = get_state(con, "workbuddy")
    force = state.get("v") != WORKBUDDY_LOGIC_VERSION
    if force:
        print("workbuddy 提取逻辑已更新，全量重读 transcripts 并清理旧配额行", file=sys.stderr)
        con.execute("DELETE FROM requests WHERE agent='workbuddy' AND unit='quota'")
    new_state = {"v": WORKBUDDY_LOGIC_VERSION}
    smap = wb_session_models()
    for f in sorted(WORKBUDDY_PROJECTS.rglob("*.jsonl")):
        path_key = str(f)
        try:
            st = f.stat()
            sig = str(int(st.st_mtime)) + ":" + str(st.st_size)
        except OSError:
            continue
        if not force and state.get(path_key) == sig:
            new_state[path_key] = sig
            continue
        recs = []
        last_done = None   # 最近一条 function_call_result 的完成时刻（ms）
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for ln, line in enumerate(fh, 1):
                    if '"usage"' not in line and '"function_call_result"' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = j.get("type")
                    if kind == "function_call_result":
                        last_done = _wb_ms(j.get("timestamp"))
                        continue
                    u = (j.get("message") or {}).get("usage") or {}
                    o = u.get("output_tokens", 0) or 0
                    if not o:
                        continue
                    cr = u.get("cache_read_input_tokens", 0) or 0
                    raw_in = u.get("input_tokens", 0) or 0
                    i = max(0, raw_in - cr)
                    ts = _wb_ms(j.get("timestamp"))
                    gen = None
                    if last_done and ts and o > 0:
                        g = ts - last_done
                        if MIN_GEN_MS < g < MAX_GEN_MS:
                            gen = g
                    model = smap.get(j.get("sessionId"))
                    recs.append((path_key + ":" + str(ln), ts, model, i, o, cr, gen))
        except OSError as e:
            print("workbuddy 文件读取失败，下轮重试:", path_key, e, file=sys.stderr)
            continue   # 不记签名 → 下一轮重试
        for rec in recs:
            write_token_row(con, "workbuddy", rec[0], rec[1] or 0, rec[2], "completed",
                            rec[3], rec[4], rec[5], rec[6], None, "token")
        new_state[path_key] = sig
    set_state(con, "workbuddy", new_state)   # 顺带清理已删除会话文件的条目


def sync_kimi(con):
    """Kimi Code CLI 的 wire.jsonl → 逐请求行（自带真实生成时长，无需估算）。

    ~/.kimi-code/sessions/wd_*/session_*/agents/*/wire.jsonl 每行一条事件；
    type=context.append_loop_event 且 event.type=step.end 的记录对应一次 LLM
    响应（与 usage.record 一一对应，覆盖率 ~99.7%；少数无 step.end 的为中途
    打断，不入库）。event.usage 口径：inputOther=不含缓存的输入、output=输出、
    inputCacheRead=缓存读；inputCacheCreation（新建缓存）在统一库无对应列，不入库。
    event.llmStreamDurationMs 是真实流式生成时长（毫秒），直接入库 gen_ms。
    step.end 行不带 model，从最近一条 usage.record / llm.request 行捎带模型名。
    """
    if not KIMI_SESSIONS.exists():
        return
    state = get_state(con, "kimi")
    force = state.get("v") != KIMI_LOGIC_VERSION
    if force:
        print("kimi 提取逻辑已更新，全量重读会话文件", file=sys.stderr)
    new_state = {"v": KIMI_LOGIC_VERSION}
    for f in sorted(KIMI_SESSIONS.rglob("wire.jsonl")):
        path_key = str(f)
        try:
            st = f.stat()
            sig = str(int(st.st_mtime)) + ":" + str(st.st_size)
        except OSError:
            continue
        if not force and state.get(path_key) == sig:
            new_state[path_key] = sig
            continue
        recs = []
        last_model = "?"   # step.end 行不带 model，从最近的 usage.record/llm.request 行捎带
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for ln, line in enumerate(fh, 1):
                    if '"step.end"' not in line and '"model"' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = j.get("type")
                    if t == "usage.record" or t == "llm.request":
                        m = j.get("model")
                        if m:
                            last_model = str(m)
                        continue
                    if t != "context.append_loop_event":
                        continue
                    ev = j.get("event") or {}
                    if ev.get("type") != "step.end":
                        continue
                    u = ev.get("usage") or {}
                    o = u.get("output", 0) or 0
                    i = u.get("inputOther", 0) or 0
                    if not o and not i:
                        continue
                    ts = j.get("time") or ev.get("time") or 0
                    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                        ts = 0
                    model = str(ev.get("model") or j.get("model") or last_model)
                    if model.startswith("kimi-code/"):
                        model = model[len("kimi-code/"):]
                    gen = ev.get("llmStreamDurationMs")
                    if not isinstance(gen, (int, float)) or gen <= 0:
                        gen = None
                    recs.append((path_key + ":" + str(ln), int(ts), model,
                                 i, o, u.get("inputCacheRead", 0) or 0, int(gen) if gen else None))
        except OSError as e:
            print("kimi 文件读取失败，下轮重试:", path_key, e, file=sys.stderr)
            continue   # 不记签名 → 下一轮重试
        for rec in recs:
            write_token_row(con, "kimi", rec[0], rec[1], rec[2], "completed",
                            rec[3], rec[4], rec[5], rec[6], None, "token")
        new_state[path_key] = sig
    set_state(con, "kimi", new_state)   # 顺带清理已删除会话文件的条目


def sync_pi(con):
    """Pi CLI 会话 JSONL（assistant 消息带 usage + cost）→ 逐消息行。

    速度还原（v2）：toolResult 消息的 timestamp ≈ 下一次模型请求的发出时刻，
    其后第一条 assistant 消息的 timestamp ≈ 响应完成。两者之差为该响应的
    墙钟生成窗口（含首字延迟与调度间隙，系统性偏慢），过滤异常窗口后入库 gen_ms。
    """
    if not PI_SESSIONS.exists():
        return
    state = get_state(con, "pi")
    force = state.get("v") != PI_LOGIC_VERSION
    if force:
        print("pi 提取逻辑已更新，全量重读会话文件", file=sys.stderr)
    new_state = {"v": PI_LOGIC_VERSION}
    for f in sorted(PI_SESSIONS.rglob("*.jsonl")):
        path_key = str(f)
        try:
            st = f.stat()
            sig = str(int(st.st_mtime)) + ":" + str(st.st_size)
        except OSError:
            continue
        if not force and state.get(path_key) == sig:
            new_state[path_key] = sig
            continue
        recs = []
        last_tool_done = None   # 最近一条 toolResult 的完成时刻（ms）
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line and '"toolResult"' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if j.get("type") != "message":
                        continue
                    msg = j.get("message") or {}
                    role = msg.get("role")
                    ts = iso_to_ms(j.get("timestamp"))
                    if role == "toolResult":
                        last_tool_done = ts
                        continue
                    if role != "assistant":
                        continue
                    u = msg.get("usage") or {}
                    i = u.get("input", 0) or 0
                    o = u.get("output", 0) or 0
                    if not i and not o:
                        continue
                    if ts is None:
                        ts = 0
                    key = path_key + ":" + str(j.get("id"))
                    model = msg.get("model")
                    cr = u.get("cacheRead", 0) or 0
                    cost = (u.get("cost") or {}).get("total")
                    gen = None
                    if last_tool_done and ts and o > 0:
                        g = ts - last_tool_done
                        if MIN_GEN_MS < g < MAX_GEN_MS:
                            gen = g
                    last_tool_done = None
                    recs.append((key, ts, model, i, o, cr, cost, gen))
        except OSError as e:
            print("pi 文件读取失败，下轮重试:", path_key, e, file=sys.stderr)
            continue   # 不记签名 → 下一轮重试
        for rec in recs:
            write_token_row(con, "pi", rec[0], rec[1], rec[2], "completed",
                            rec[3], rec[4], rec[5], rec[7], rec[6], "token")
        new_state[path_key] = sig
    set_state(con, "pi", new_state)   # 顺带清理已删除会话文件的条目


def main():
    con = udb()
    try:
        for name, fn in (("zcode", sync_zcode), ("codex", sync_codex),
                         ("workbuddy", sync_workbuddy), ("pi", sync_pi),
                         ("kimi", sync_kimi)):
            try:
                fn(con)
                con.commit()   # 逐个提交：单个数据源失败不影响已采集的数据
            except Exception as e:  # noqa: BLE001 — 采集器必须吞掉一切异常保住整轮
                print(name, "同步失败:", e, file=sys.stderr)
        set_state(con, "_meta", {"last_sync": int(time.time() * 1000)})
        con.commit()
        n = con.execute("SELECT agent, COUNT(*) FROM requests GROUP BY agent").fetchall()
        print("同步完成:", {a: c for a, c in n})
    finally:
        con.close()


if __name__ == "__main__":
    main()
