#!/usr/bin/env python3
"""适配器冒烟测试 —— 不依赖任何真实 Agent 数据，用临时 fixtures 覆盖四个适配器。

覆盖的回归点：
  1. 幂等：连跑两次，行数与数值都不变
  2. codex 模型按 turn_id 关联成功（历史 bug：误用 thread_id 导致模型恒为空）
  3. codex input 已扣除 cache（历史 bug：与 cache 双计）
  4. 时间戳为 null / JSON 损坏不致命（历史 bug：AttributeError 中断整个数据源）
  5. 读取失败的文件不记录签名，下一轮会重试（历史 bug：先记签名导致永久跳过）
  6. workbuddy 逐请求口径：input 扣缓存、墙钟窗口 gen、模型按 sessionId 关联，
     且迁移时旧 quota 行被清空
  7. 统一库为 WAL 模式
  8. 提取逻辑版本号变化会触发历史文件全量重读

用法：python3 tests/smoke_test.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC_SRC = ROOT / "sync" / "sync.py"
NOW_MS = int(time.time() * 1000)   # 必须落在 zcode 适配器的 30 天回填窗口内
FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          ("" if cond else "   <- " + str(detail)))
    if not cond:
        FAILED.append(name)


def build_fixtures(tmp):
    """构造四个数据源的最小 fixtures。"""
    # zcode: model_usage 表
    zdb = tmp / "zcode.sqlite"
    con = sqlite3.connect(zdb)
    con.execute("CREATE TABLE model_usage(id TEXT, started_at INTEGER, model_id TEXT, status TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_input_tokens INTEGER, first_token_at INTEGER, completed_at INTEGER)")
    con.execute("INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?)",
                ("z1", NOW_MS, "glm-test", "completed", 100, 40, 900, NOW_MS, NOW_MS + 2000))
    con.commit()
    con.close()

    # codex: 一份正常会话 + 一个打不开的路径（目录，用于验证失败不记签名）
    # 时序：task_started@0s → tool_output@60s（= 第二次请求发出）→ usage@70s
    #      → 第二次响应 gen = 70-60 = 10s；第一次响应 gen = 60-0 = 60s
    cx = tmp / "codex"
    cx.mkdir()
    good = [
        {"type": "session_meta", "payload": {"id": "t1"}},
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-test"}},
        {"type": "event_msg", "timestamp": "2026-09-01T10:00:00.000Z",
         "payload": {"type": "task_started"}},
        {"type": "token_usage_record", "timestamp": "2026-09-01T10:01:00.000Z",
         "payload": {"thread_id": "t1", "turn_id": "turn-1", "response_id": "resp-1",
                     "usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                               "output_tokens": 300}}},
        {"type": "response_item", "timestamp": "2026-09-01T10:01:00.000Z",
         "payload": {"type": "custom_tool_call_output"}},
        {"type": "token_usage_record", "timestamp": "2026-09-01T10:01:10.000Z",
         "payload": {"thread_id": "t1", "turn_id": "turn-1", "response_id": "resp-2",
                     "usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                               "output_tokens": 50}}},
        # 时间戳为 null：不应中断本数据源
        {"type": "token_usage_record", "timestamp": None,
         "payload": {"thread_id": "t1", "turn_id": "turn-1", "response_id": "resp-null",
                     "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                               "output_tokens": 5}}},
    ]
    body = "\n".join(json.dumps(x) for x in good) + "\nnot-json-at-all\n"
    (cx / "rollout-a.jsonl").write_text(body)
    (cx / "broken.jsonl").mkdir()   # open() 会抛 IsADirectoryError

    # pi: assistant 消息带 usage + cost；toolResult 提供下一次请求的起点
    # 时序：toolResult@0s → assistant@20s → gen = 20s
    pi = tmp / "pi"
    pi.mkdir()
    (pi / "sess.jsonl").write_text(
        json.dumps({"type": "message", "id": "m0", "timestamp": "2026-09-01T09:59:40.000Z",
                    "message": {"role": "toolResult"}})
        + "\n" + json.dumps({
            "type": "message", "id": "m1", "timestamp": "2026-09-01T10:00:00.000Z",
            "message": {"role": "assistant", "model": "ds-test",
                        "usage": {"input": 200, "output": 30, "cacheRead": 500,
                                  "cost": {"total": 0.012}}}}))

    # workbuddy: sessions 表仍用 WAL 无副文件的库（复现只读打不开、退回读写连接），
    # 数据本体是 projects transcripts：function_call_result 提供请求起点，
    # 带 usage 的 function_call / message 各对应一次 LLM 响应（毫秒整数时间戳）
    wb = tmp / "workbuddy.db"
    con = sqlite3.connect(wb)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, cwd TEXT, user_id TEXT, model TEXT)")
    con.execute("INSERT INTO sessions(id, model) VALUES('s1','deepseek-test')")
    con.commit()
    con.close()
    for sidecar in (tmp / "workbuddy.db-wal", tmp / "workbuddy.db-shm"):
        if sidecar.exists():
            sidecar.unlink()

    wbp = tmp / "workbuddy-projects" / "proj-a"
    wbp.mkdir(parents=True)
    T0 = 1782000000000   # 任意毫秒时间戳（transcript 口径，非 ISO）
    wb_events = [
        {"type": "function_call_result", "sessionId": "s1", "timestamp": T0},
        # gen = 20s；input 1000 含 cached 400 → 入库 600
        {"type": "function_call", "sessionId": "s1", "timestamp": T0 + 20000,
         "message": {"usage": {"input_tokens": 1000, "output_tokens": 30,
                               "cache_read_input_tokens": 400, "total_tokens": 1030}}},
        {"type": "function_call_result", "sessionId": "s1", "timestamp": T0 + 30000},
        # gen = 10s
        {"type": "message", "sessionId": "s1", "timestamp": T0 + 40000,
         "message": {"usage": {"input_tokens": 1200, "output_tokens": 50,
                               "cache_read_input_tokens": 1100, "total_tokens": 1250}}},
        # 时间戳为 null：不应中断本数据源
        {"type": "message", "sessionId": "s1", "timestamp": None,
         "message": {"usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 0, "total_tokens": 15}}},
        # output=0 的记录不入库
        {"type": "message", "sessionId": "s1", "timestamp": T0 + 50000,
         "message": {"usage": {"input_tokens": 10, "output_tokens": 0,
                               "cache_read_input_tokens": 0, "total_tokens": 10}}},
    ]
    (wbp / "sess1.jsonl").write_text("\n".join(json.dumps(x) for x in wb_events))
    # kimi: wire.jsonl 的 step.end 事件自带 usage 与真实流式时长；
    # usage.record 与 step.end 一一对应，只取 step.end 防双计
    ki = tmp / "kimi-projects" / "wd_demo"
    ki.mkdir(parents=True)
    kb = [
        {"type": "metadata"},
        # 正常响应：真实时长 4.2s
        {"type": "usage.record", "time": T0, "model": "kimi-code/k3",
         "usage": {"inputOther": 100, "output": 9, "inputCacheRead": 0, "inputCacheCreation": 0}},
        {"type": "context.append_loop_event", "time": T0 + 1, "model": "kimi-code/k3",
         "event": {"type": "step.end", "turnId": "0", "usage": {"inputOther": 100, "output": 9,
                   "inputCacheRead": 200, "inputCacheCreation": 50},
                   "llmStreamDurationMs": 4200}},
        # 无 usage 的 step.end：不入库
        {"type": "context.append_loop_event", "time": T0 + 2,
         "event": {"type": "step.end", "turnId": "1"}},
        # 时长缺失（中断）：入库存但无 gen
        {"type": "usage.record", "time": T0 + 3, "model": "kimi-code/kimi-for-coding",
         "usage": {"inputOther": 30, "output": 4, "inputCacheRead": 0, "inputCacheCreation": 0}},
        {"type": "context.append_loop_event", "time": T0 + 4, "model": "kimi-code/kimi-for-coding",
         "event": {"type": "step.end", "turnId": "2", "usage": {"inputOther": 30, "output": 4,
                   "inputCacheRead": 0, "inputCacheCreation": 0}}},
    ]
    agents = ki / "session_x" / "agents" / "main"
    agents.mkdir(parents=True)
    (agents / "wire.jsonl").write_text("\n".join(json.dumps(x) for x in kb))
    (wbp / "broken.jsonl").mkdir()   # open() 会抛 IsADirectoryError
    return zdb, cx, wb, pi, wbp, ki


def load_sync(env):
    """注入环境变量后再加载被测模块（模块在导入时读取路径）。"""
    for k, v in env.items():
        os.environ[k] = str(v)
    spec = importlib.util.spec_from_file_location("zusage_sync_under_test", SYNC_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rows_of(con, agent):
    return con.execute("SELECT key, model, input_tokens, output_tokens, cache_read_tokens, unit, cost_usd, gen_ms FROM requests WHERE agent=? ORDER BY key", (agent,)).fetchall()


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        zdb, cx, wb, pi, wbp, ki = build_fixtures(tmp)
        udb = tmp / "usage.db"
        # 预置一条旧版配额行，验证 v2 迁移会清掉它
        pre = sqlite3.connect(udb)
        pre.execute("CREATE TABLE IF NOT EXISTS requests(agent TEXT NOT NULL, key TEXT NOT NULL, ts INTEGER NOT NULL, model TEXT, status TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0, gen_ms INTEGER, cost_usd REAL, unit TEXT DEFAULT 'token', PRIMARY KEY(agent, key))")
        pre.execute("CREATE TABLE IF NOT EXISTS sync_state(adapter TEXT PRIMARY KEY, state TEXT)")
        pre.execute("INSERT INTO requests VALUES('workbuddy','old-session',?,NULL,'completed',0,5000,0,NULL,NULL,'quota')", (NOW_MS,))
        pre.commit()
        pre.close()
        mod = load_sync({
            "ZUSAGE_UDB": udb,
            "ZUSAGE_ZCODE_DB": zdb,
            "ZUSAGE_CODEX_DIR": cx,
            "ZUSAGE_WORKBUDDY_DB": wb,
            "ZUSAGE_WORKBUDDY_PROJECTS": wbp,
            "ZUSAGE_KIMI_DIR": ki,
            "ZUSAGE_PI_DIR": pi,
        })

        print("[1] 首轮同步")
        mod.main()
        con = sqlite3.connect(udb)

        check("统一库为 WAL 模式",
              con.execute("PRAGMA journal_mode").fetchone()[0] == "wal")

        # codex
        cx_rows = {r[0]: r for r in rows_of(con, "codex")}
        r1 = cx_rows.get("resp-1")
        check("codex 记录已入库", r1 is not None, list(cx_rows))
        check("codex 模型按 turn_id 关联成功", r1 and r1[1] == "gpt-test", r1)
        check("codex input 已扣除 cache（不双计）", r1 and r1[2] == 600, r1)
        check("codex cache 单独记录", r1 and r1[4] == 400, r1)
        check("codex 第一次响应 gen=60s（task_started→usage）", r1 and r1[7] == 60000, r1)
        r2 = cx_rows.get("resp-2")
        check("codex 第二次响应 gen=10s（tool_output→usage）", r2 and r2[7] == 10000, r2)
        check("时间戳为 null 的记录仍入库(ts=0 兜底)",
              "resp-null" in cx_rows, list(cx_rows))

        # pi
        pi_rows = rows_of(con, "pi")
        check("pi 记录已入库", len(pi_rows) == 1, pi_rows)
        check("pi cost 已采集", pi_rows and abs((pi_rows[0][6] or 0) - 0.012) < 1e-9, pi_rows)
        check("pi gen=20s（toolResult→assistant）", pi_rows and pi_rows[0][7] == 20000, pi_rows)

        # workbuddy（v2 逐请求口径）
        wb_rows = {r[0].rsplit("sess1.jsonl:", 1)[1]: r for r in rows_of(con, "workbuddy")
                   if "sess1.jsonl:" in r[0]}
        check("workbuddy 逐请求行已入库（3 条，output=0 不计）", len(wb_rows) == 3, wb_rows)
        r_w1 = wb_rows.get("2")
        check("workbuddy 模型按 sessionId 关联 sessions 表",
              r_w1 and r_w1[1] == "deepseek-test", r_w1)
        check("workbuddy input 已扣除 cache（不双计）", r_w1 and r_w1[2] == 600, r_w1)
        check("workbuddy gen=20s（function_call_result→usage）", r_w1 and r_w1[7] == 20000, r_w1)
        r_w2 = wb_rows.get("4")
        check("workbuddy gen=10s（第二次窗口）", r_w2 and r_w2[7] == 10000, r_w2)
        check("workbuddy unit=token（逐请求口径）",
              all(r[5] == "token" for r in wb_rows.values()), wb_rows)
        check("时间戳为 null 的记录仍入库(ts=0 兜底)", "5" in wb_rows, list(wb_rows))
        legacy = con.execute("SELECT COUNT(*) FROM requests WHERE agent='workbuddy' AND unit='quota'").fetchone()[0]
        check("旧 quota 行已在 v2 迁移中清空", legacy == 0, legacy)

        # kimi（step.end 事件口径，真实流式时长）
        ki_rows = {r[0].rsplit("wire.jsonl:", 1)[1]: r for r in rows_of(con, "kimi")
                   if "wire.jsonl:" in r[0]}
        check("kimi 逐请求行已入库（2 条，无 usage 的 step.end 不计）", len(ki_rows) == 2, ki_rows)
        r_k1 = ki_rows.get("3")
        check("kimi 模型名去掉 kimi-code/ 前缀", r_k1 and r_k1[1] == "k3", r_k1)
        check("kimi input=inputOther、cache 单独记录", r_k1 and r_k1[2] == 100 and r_k1[4] == 200, r_k1)
        check("kimi gen=真实流式时长 4200ms", r_k1 and r_k1[7] == 4200, r_k1)
        r_k2 = ki_rows.get("6")
        check("kimi 时长缺失时入库但 gen 为空", r_k2 and r_k2[2] == 30 and r_k2[7] is None, r_k2)

        # zcode
        zc_rows = rows_of(con, "zcode")
        check("zcode 记录已入库（30 天窗口内）", len(zc_rows) == 1, zc_rows)
        check("zcode 输出与 gen_ms 已记录",
              zc_rows and zc_rows[0][3] == 40 and zc_rows[0][7] == 2000, zc_rows)

        # 读取失败的路径不应被记入签名
        st = json.loads(con.execute("SELECT state FROM sync_state WHERE adapter='codex'").fetchone()[0])
        check("写入成功的文件已记签名",
              any("rollout-a.jsonl" in k for k in st), list(st)[:3])
        check("读取失败的文件未记签名（下轮会重试）",
              not any("broken.jsonl" in k for k in st), list(st)[:3])

        total_1 = con.execute("SELECT COUNT(*), COUNT(DISTINCT agent || '|' || key) FROM requests").fetchone()
        check("无重复键", total_1[0] == total_1[1], total_1)

        print("[2] 第二轮同步（验证幂等）")
        mod.main()
        total_2 = con.execute("SELECT COUNT(*), COUNT(DISTINCT agent || '|' || key) FROM requests").fetchone()
        check("幂等：行数不变", total_1 == total_2, (total_1, total_2))
        cx_2 = {r[0]: r for r in rows_of(con, "codex")}
        check("幂等：数值不变", cx_2.get("resp-1") == r1, (cx_2.get("resp-1"), r1))

        print("[3] 提取逻辑版本号变化触发全量重读")
        con.execute("UPDATE requests SET model = 'WIPED' WHERE agent = 'codex'")
        con.commit()
        mod.CODEX_LOGIC_VERSION = 999
        mod.main()
        restored = {r[0]: r for r in rows_of(con, "codex")}
        check("版本号变化后历史文件被重读并修正",
              restored.get("resp-1") and restored["resp-1"][1] == "gpt-test",
              restored.get("resp-1"))

        con.close()

    print()
    if FAILED:
        print("FAILED (%d): %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
