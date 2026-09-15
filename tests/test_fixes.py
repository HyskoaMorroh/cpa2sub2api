#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cpa2sub2api 核心逻辑回归测试。

只测纯函数，不触碰网络、不写 out/。
运行：python tests/test_fixes.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import model_selection  # noqa: E402
import new_remap_priority  # noqa: E402
import tool  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  <- " + detail) if (detail and not cond) else ""))


def D(*names):
    return [{"name": n, "alias": n} for n in names]


def names(ms):
    return [m["name"] for m in ms]


def rec(name, group, base_url, priority, api_key="sk-a", section="claude-api-key",
        platform="anthropic", weight=1):
    return {
        "name": name, "group": group, "base_url": base_url, "priority": priority,
        "api_key": api_key, "section": section, "source_section": section,
        "platform": platform, "type": "apikey", "responses_mode": None,
        "new_priority": None, "weight": weight, "prefix": "", "proxy_url": "",
        "headers": {}, "models_raw": D("claude-opus-5"), "rse": [],
        "excluded": [], "websockets": False, "alpha_search": False,
        "fingerprint": "", "cloak": None, "provider_name": "", "disabled": False,
        "rebuild_mid_system": None, "prompt_cache_key": None,
        "disable_cooling": None, "request_retry": None,
    }


print("=" * 70)
print("A. to_account —— P0 UnboundLocalError 回归")
print("=" * 70)

# A1: 同域名代理命中但 proxy_id_map 里没有该 id（原先必崩）
r = rec("t-1", "Claude", "https://a.example.com", 100)
try:
    acc = tool.to_account(r, {"default_concurrency": 3}, {"Claude": 1}, {},
                          {"a.example.com": "http://mihomo:7890"})
    check("A1 建代理失败时不再抛 UnboundLocalError", True)
    check("A1 leftover 记录了未配置代理",
          "需要TLS代理但未配置" in acc.get("notes", ""),
          "notes=%r" % acc.get("notes", "")[:120])
except Exception as e:
    check("A1 建代理失败时不再抛 UnboundLocalError", False, "%s: %s" % (type(e).__name__, e))

# A2: 有代理 id 时正常绑定
try:
    acc = tool.to_account(r, {"default_concurrency": 3}, {"Claude": 1},
                          {"http://mihomo:7890": 42},
                          {"a.example.com": "http://mihomo:7890"})
    check("A2 代理 id 可用时正确绑定", acc.get("proxy_id") == 42,
          "proxy_id=%r" % acc.get("proxy_id"))
except Exception as e:
    check("A2 代理 id 可用时正确绑定", False, "%s: %s" % (type(e).__name__, e))

# A3: proxy_url 直接命中
r3 = rec("t-3", "Claude", "https://b.example.com", 100)
r3["proxy_url"] = "http://mihomo:7890"
try:
    acc = tool.to_account(r3, {"default_concurrency": 3}, {"Claude": 1},
                          {"http://mihomo:7890": 7}, {})
    check("A3 proxy_url 直接绑定", acc.get("proxy_id") == 7,
          "proxy_id=%r" % acc.get("proxy_id"))
except Exception as e:
    check("A3 proxy_url 直接绑定", False, "%s: %s" % (type(e).__name__, e))

print()
print("=" * 70)
print("B. health_rerank_priority —— 全局 dense rank（跨渠道不撞桶）")
print("=" * 70)

# 构造：两个渠道，各有 2 个域名，CPA priority 完全相同（最容易撞桶的情形）
recs = [
    rec("c1", "Claude", "https://same.example.com", 1000, api_key="k1"),
    rec("c2", "Claude", "https://other.example.com", 1000, api_key="k2"),
    rec("g1", "Gemini", "https://same.example.com", 1000, api_key="k3",
        section="gemini-api-key", platform="gemini"),
    rec("g2", "Gemini", "https://third.example.com", 1000, api_key="k4",
        section="gemini-api-key", platform="gemini"),
]
have_map = {r["name"]: {"schedulable": True, "status": "active"} for r in recs}

for fn in (tool.remap_priority,):
    fn(recs)

ranked, detail = tool.health_rerank_priority(recs, have_map, {"health_rerank_enabled": True})
check("B1 健康度重排已执行", ranked == 4, "ranked=%r" % ranked)

buckets = {}
for r in recs:
    buckets.setdefault(r["new_priority"], []).append(r["name"])

check("B2 四个域名单元拿到四个互不相同的桶号", len(buckets) == 4,
      "桶号分布=%r" % {k: v for k, v in sorted(buckets.items())})
check("B3 桶号全局唯一（跨渠道不撞）", all(len(v) == 1 for v in buckets.values()),
      "同桶成员=%r" % {k: v for k, v in buckets.items() if len(v) > 1})

# 同域名同渠道的多 KEY 必须同桶
recs2 = [
    rec("m1", "Claude", "https://multi.example.com", 1000, api_key="ka"),
    rec("m2", "Claude", "https://multi.example.com", 900, api_key="kb"),
    rec("m3", "Claude", "https://multi.example.com", 800, api_key="kc"),
    rec("n1", "Claude", "https://solo.example.com", 700, api_key="kd"),
]
have2 = {r["name"]: {"schedulable": True, "status": "active"} for r in recs2}
tool.remap_priority(recs2)
tool.health_rerank_priority(recs2, have2, {})
b_multi = {r["new_priority"] for r in recs2 if "multi" in r["base_url"]}
check("B4 同域名多 KEY 同桶（互为备份）", len(b_multi) == 1, "桶号=%r" % b_multi)
check("B5 不同域名桶号不同",
      {r["new_priority"] for r in recs2 if "multi" in r["base_url"]} !=
      {r["new_priority"] for r in recs2 if "solo" in r["base_url"]})

# 冷启动：have_map 为空 -> 不重排
recs3 = [rec("z1", "Claude", "https://z.example.com", 100)]
tool.remap_priority(recs3)
n3, why3 = tool.health_rerank_priority(recs3, {}, {})
check("B6 冷启动（无账号）不重排并给出原因", n3 == 0 and "首次导入" in why3, why3[:80])

# 开关关闭 -> 不重排
n4, why4 = tool.health_rerank_priority(recs3, have_map, {"health_rerank_enabled": False})
check("B7 health_rerank_enabled=false 时跳过", n4 == 0 and "false" in why4, why4[:80])

print()
print("=" * 70)
print("C. remap_priority_smart —— 按域名单元建表 + 分页取数")
print("=" * 70)


class FakeApi:
    """模拟 sub2api 的分页响应：data.items + data.total。"""

    def __init__(self, accounts):
        self._all = accounts
        self.calls = []

    def list_accounts(self, page, size):
        self.calls.append((page, size))
        start = (page - 1) * size
        return {"data": {"items": self._all[start:start + size],
                         "total": len(self._all)}}


# 造 250 个账号，强制翻两页（size=200）
fake_accounts = []
for i in range(250):
    fake_accounts.append({
        "name": "acc-%d" % i, "platform": "anthropic",
        "credentials": {"base_url": "https://d%d.example.com" % (i % 5)},
        "status": "active" if i % 2 == 0 else "error",
        "schedulable": i % 3 != 0,
    })
api = FakeApi(fake_accounts)

recs4 = [
    # s1a/s1b 同渠道同域名 -> 必须同桶；s2 另一域名 -> 必须不同桶
    rec("s1a", "Claude", "https://d0.example.com", 1000, api_key="k1"),
    rec("s1b", "Claude", "https://d0.example.com", 1000, api_key="k1b"),
    rec("s2", "Claude", "https://d1.example.com", 1000, api_key="k2"),
    rec("s3", "Gemini", "https://d2.example.com", 1000, api_key="k3",
        section="gemini-api-key", platform="gemini"),
]
b_of = new_remap_priority.remap_priority_smart(
    recs4, api, tool.host_of, tool.account_fingerprint,
    tool._is_int, tool._host_tier_map, tool._host_key)

check("C1 分页取全量（250 条走两页）", len(api.calls) >= 2, "calls=%r" % api.calls)
check("C2 按域名单元建表", set(b_of.keys()) == {tool._host_key(r) for r in recs4},
      "keys=%r" % sorted(str(k) for k in b_of.keys()))
check("C3 桶号全局唯一", len(set(b_of.values())) == len(b_of),
      "values=%r" % sorted(b_of.values()))
check("C4 同渠道同域名同桶（多 KEY 互为备份）",
      b_of[tool._host_key(recs4[0])] == b_of[tool._host_key(recs4[1])],
      "%r vs %r" % (b_of[tool._host_key(recs4[0])], b_of[tool._host_key(recs4[1])]))
check("C5 不同域名不同桶",
      b_of[tool._host_key(recs4[0])] != b_of[tool._host_key(recs4[2])])

print()
print("=" * 70)
print("D. model_selection —— 就高原则")
print("=" * 70)

# gemini
out = model_selection.select_highest_models(
    D("gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-pro", "gemini-1.5-pro"), "gemini")
check("D1 gemini 只留最高 pro", names(out) == ["gemini-3.1-pro"], "%r" % names(out))

out = model_selection.select_highest_models(
    D("gemini-3.1-pro", "gemini-3.1-pro-preview", "gemini-3.1-flash"), "gemini")
check("D2 gemini 保留同版本全部 pro 变体",
      set(names(out)) == {"gemini-3.1-pro", "gemini-3.1-pro-preview"}, "%r" % names(out))

model_selection.reset_fallback_notes()
out = model_selection.select_highest_models(D("gemini-3.5-flash"), "gemini")
notes = model_selection.collect_fallback_notes()
check("D3 gemini 全 flash 时回退并出声", names(out) == ["gemini-3.5-flash"] and notes,
      "notes=%r" % notes)

# claude
out = model_selection.select_highest_models(
    D("claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
    "anthropic", series={"anthropic": 5})
check("D4 claude 门槛 5 只留 5 代", set(names(out)) == {"claude-opus-5", "claude-sonnet-5"},
      "%r" % names(out))

out = model_selection.select_highest_models(
    D("claude-opus-4-8", "claude-sonnet-4-5", "claude-opus-5"), "anthropic")
check("D5 claude 未注入门槛时观测最高代", names(out) == ["claude-opus-5"], "%r" % names(out))

model_selection.reset_fallback_notes()
out = model_selection.select_highest_models(D("claude-sonnet-4-5"), "anthropic",
                                            series={"anthropic": 5})
notes = model_selection.collect_fallback_notes()
check("D6 claude 无当代时回退最高代并出声",
      names(out) == ["claude-sonnet-4-5"] and notes, "notes=%r" % notes)

# codex（单族）
out = model_selection.select_highest_models(
    D("gpt-6", "gpt-6-astra", "gpt-6-sol", "gpt-5.6", "gpt-5.6-sol"),
    "openai", source_section="codex-api-key", series={"openai": 6})
check("D7 codex 只留当代 gpt 全系",
      set(names(out)) == {"gpt-6", "gpt-6-astra", "gpt-6-sol"}, "%r" % names(out))

out = model_selection.select_highest_models(
    D("gpt-7", "gpt-7-sol", "gpt-6"), "openai", source_section="codex-api-key")
check("D8 codex 门槛未注入时跟随观测到的最高代",
      set(names(out)) == {"gpt-7", "gpt-7-sol"}, "%r" % names(out))

# codex 不再误吃 gpt-60
out = model_selection.select_highest_models(
    D("gpt-6", "gpt-60"), "openai", source_section="codex-api-key", series={"openai": 6})
check("D9 gpt-60 不被当成第 6 代", "gpt-60" not in names(out), "%r" % names(out))

# openai（多族）
# gpt-6.0 与 gpt-6.0-preview 同族同版本 -> 都要留（"同等级系列全部勾选"）
# gpt-5.6 属 gpt-5 族 -> 该族最高，留
# o3-mini / o3-max 属 o3 族同版本 -> 都留
out = model_selection.select_highest_models(
    D("gpt-6.0", "gpt-6.0-preview", "gpt-5.6", "o3-mini", "o3-max"),
    "openai", source_section="openai-compatibility", series={"openai": 6})
check("D10 openai 多族并存、同族同版本全留",
      set(names(out)) == {"gpt-6.0", "gpt-6.0-preview", "gpt-5.6", "o3-mini", "o3-max"},
      "%r" % names(out))
# gpt-5 族内低版本必须被砍（gpt-5.5 < gpt-5.6）
out = model_selection.select_highest_models(
    D("gpt-6.0", "gpt-5.6", "gpt-5.5", "gpt-5.5-mini"), "openai",
    source_section="openai-compatibility", series={"openai": 6})
check("D10b 同族低版本被砍",
      set(names(out)) == {"gpt-6.0", "gpt-5.6"}, "%r" % names(out))

out = model_selection.select_highest_models(
    D("gpt-4o", "gpt-4o-mini", "gpt-4.1"), "openai",
    source_section="openai-compatibility")
check("D11 gpt-4o 与 gpt-4.1 同族，留最高", names(out) == ["gpt-4.1"], "%r" % names(out))

out = model_selection.select_highest_models(
    D("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-luna"), "openai",
    source_section="openai-compatibility")
check("D12 同族同版本全留（不漏 gpt-5.6-sol）",
      len(names(out)) == 3, "%r" % names(out))

# codex 与 openai 分流：同一份模型列表，两种策略结果必须不同
mixed = D("gpt-6", "gpt-6-astra", "o3-mini", "gpt-5.6")
cx = model_selection.select_highest_models(mixed, "openai",
                                           source_section="codex-api-key",
                                           series={"openai": 6})
oa = model_selection.select_highest_models(mixed, "openai",
                                           source_section="openai-compatibility",
                                           series={"openai": 6})
check("D13 codex 与 openai 策略确实分流", names(cx) != names(oa),
      "codex=%r openai=%r" % (names(cx), names(oa)))
check("D14 codex 结果不含 o3（单族）", "o3-mini" not in names(cx), "%r" % names(cx))
check("D15 openai 结果保留 o3（多族）", "o3-mini" in names(oa), "%r" % names(oa))

# 其他平台原样
out = model_selection.select_highest_models(D("grok-4", "grok-3"), "grok")
check("D16 grok 原样返回", names(out) == ["grok-4", "grok-3"], "%r" % names(out))

print()
print("=" * 70)
print("E. collect —— source_section 字段")
print("=" * 70)

cfg = {
    "codex-api-key": [{"base-url": "https://cx.example.com", "api-key": "sk-cx",
                       "models": [{"name": "gpt-6", "alias": "gpt-6"}]}],
    "openai-compatibility": [{"base-url": "https://oa.example.com", "name": "oa",
                              "api-key-entries": [{"api-key": "sk-oa", "weight": 5}],
                              "models": [{"name": "gpt-6", "alias": "gpt-6"}]}],
}
recs5, _ = tool.collect(cfg)
by_sec = {r["section"]: r for r in recs5}
check("E1 codex 记录带 source_section",
      by_sec.get("codex-api-key", {}).get("source_section") == "codex-api-key")
check("E2 openai 记录带 source_section",
      by_sec.get("openai-compatibility", {}).get("source_section") == "openai-compatibility")
check("E3 两者 platform 同为 openai（所以在 platform 上无法区分）",
      by_sec["codex-api-key"]["platform"] == by_sec["openai-compatibility"]["platform"] == "openai")

print()
print("=" * 70)
print("F. build_model_mapping —— 按来源段分流")
print("=" * 70)

models = D("gpt-6", "gpt-6-astra", "o3-mini", "gpt-5.6")
m_cx = tool.build_model_mapping(models, None, "openai", source_section="codex-api-key")
m_oa = tool.build_model_mapping(models, None, "openai", source_section="openai-compatibility")
check("F1 codex 映射不含 o3", "o3-mini" not in m_cx, "%r" % sorted(m_cx))
check("F2 openai 映射含 o3", "o3-mini" in m_oa, "%r" % sorted(m_oa))
check("F3 分流后结果确实不同", sorted(m_cx) != sorted(m_oa))

print()
print("=" * 70)
print("H. 域名级冷却（402 预算池耗尽）")
print("=" * 70)

rules, _ = tool.build_temp_unschedulable([{"status-code": 403,
                                           "action": "continue-and-cooldown"}])
d = tool.build_domain_scoped_cooldown([{"status-code": 403,
                                        "action": "continue-and-cooldown"}])
codes = [x["error_code"] for x in rules + d]
check("H1 402 域名级规则已生成", 402 in [x["error_code"] for x in d],
      "%r" % [x["error_code"] for x in d])
check("H2 402 冷却时长 180 分钟",
      [x["duration_minutes"] for x in d if x["error_code"] == 402] == [180])
check("H3 关键词是短语而非裸单词（不会误伤）",
      all((" " in k) or len(k) >= 4 for x in d for k in x["keywords"]),
      "%r" % [k for x in d for k in x["keywords"]])
# 去重后不应出现两条同 error_code 的规则
r2 = rec("h1", "Claude", "https://h.example.com", 100)
r2["rse"] = [{"status-code": 403, "action": "continue-and-cooldown"}]
acc2 = tool.to_account(r2, {"default_concurrency": 3}, {"Claude": 1}, {}, {})
issued = [x["error_code"] for x in
          acc2["credentials"].get("temp_unschedulable_rules") or []]
check("H4 下发的规则里 error_code 不重复", len(issued) == len(set(issued)),
      "%r" % issued)
check("H5 402 以域名级那条为准（180 而非 60）",
      [x["duration_minutes"] for x in
       acc2["credentials"]["temp_unschedulable_rules"]
       if x["error_code"] == 402] == [180])

print()
print("=" * 70)
print("I. 反测活：探活时刻错开")
print("=" * 70)

import hashlib  # noqa: E402

check("I1 */30 加偏移",
      tool._cron_with_offset("*/30 * * * *", 7) == "7,37 * * * *",
      tool._cron_with_offset("*/30 * * * *", 7))
check("I2 */15 加偏移",
      tool._cron_with_offset("*/15 * * * *", 3) == "3,18,33,48 * * * *",
      tool._cron_with_offset("*/15 * * * *", 3))
check("I3 偏移超出步长会回绕",
      tool._cron_with_offset("*/30 * * * *", 37) == "7,37 * * * *",
      tool._cron_with_offset("*/30 * * * *", 37))
check("I4 非 */N 写法原样返回",
      tool._cron_with_offset("0 * * * *", 5) == "0 * * * *",
      tool._cron_with_offset("0 * * * *", 5))
check("I5 表达式非法时原样返回",
      tool._cron_with_offset("bad", 1) == "bad")

# 不同账号必须散开（打破"同一分钟一起探活"）
seen = set()
for i in range(60):
    off = int(hashlib.sha1(("acct-%d" % i).encode()).hexdigest()[:4], 16)
    seen.add(tool._cron_with_offset("*/30 * * * *", off))
check("I6 60 个账号散成多种探活时刻（>10 种）", len(seen) > 10,
      "只有 %d 种" % len(seen))

# 同一账号必须稳定（计划幂等，重跑不该改时刻）
off1 = int(hashlib.sha1(b"same-acct").hexdigest()[:4], 16)
off2 = int(hashlib.sha1(b"same-acct").hexdigest()[:4], 16)
check("I7 同一账号偏移稳定",
      tool._cron_with_offset("*/30 * * * *", off1) ==
      tool._cron_with_offset("*/30 * * * *", off2))

print()
print("=" * 70)
print("J. remap_priority —— 跨渠道桶号全局唯一")
print("=" * 70)

# 多个不同渠道的不同域名，在 CPA 里被配成**同一个 priority**。
# 这是最容易撞桶的情形，原先的防碰撞只在同渠道内做，跨渠道必撞。
cfg_j = {
    "routing": {"strategy": "round-robin"},
    "claude-api-key": [
        {"api-key": "sk-a", "base-url": "https://a.example.com", "priority": 1000,
         "models": [{"name": "claude-opus-5", "alias": "claude-opus-5"}]},
        {"api-key": "sk-a2", "base-url": "https://a.example.com", "priority": 1000,
         "models": [{"name": "claude-opus-5", "alias": "claude-opus-5"}]},
        {"api-key": "sk-b", "base-url": "https://b.example.com", "priority": 900,
         "models": [{"name": "claude-sonnet-5", "alias": "claude-sonnet-5"}]},
    ],
    "codex-api-key": [
        {"api-key": "sk-c", "base-url": "https://c.example.com", "priority": 1000,
         "models": [{"name": "gpt-6", "alias": "gpt-6"}]},
    ],
    "openai-compatibility": [
        {"name": "oa", "base-url": "https://d.example.com", "priority": 1000,
         "api-key-entries": [{"api-key": "sk-d", "weight": 2}],
         "models": [{"name": "o3-mini", "alias": "o3-mini"}]},
    ],
}
recs_j, _ = tool.collect(cfg_j)
tool.remap_priority(recs_j)
tool.assign_names(recs_j)

by_bucket = {}
for r in recs_j:
    by_bucket.setdefault(r["new_priority"], []).append(r)

units = {(r["group"], tool.host_of(r["base_url"])) for r in recs_j}
check("J1 桶数 == 域名单元数（全局唯一）", len(by_bucket) == len(units),
      "桶=%d 单元=%d" % (len(by_bucket), len(units)))

bad = []
for b, rs in by_bucket.items():
    hosts = {tool.host_of(r["base_url"]) for r in rs}
    if len(hosts) > 1:
        bad.append((b, hosts))
check("J2 没有跨域名撞桶", not bad, "%r" % bad)

# 同渠道同域名的两条必须同桶
same = [r for r in recs_j if r["base_url"].endswith("a.example.com")]
check("J3 同渠道同域名同桶（互为备份）",
      len({r["new_priority"] for r in same}) == 1,
      "%r" % [r["new_priority"] for r in same])

# 不同渠道即使 CPA priority 相同，也必须不同桶（原先的缺陷）
cx = [r for r in recs_j if r["section"] == "codex-api-key"][0]
oa = [r for r in recs_j if r["section"] == "openai-compatibility"][0]
check("J4 同 CPA priority 的跨渠道域名不同桶（回归修复）",
      cx["new_priority"] != oa["new_priority"],
      "codex=%s openai=%s" % (cx["new_priority"], oa["new_priority"]))

# 确定性：同一份配置重跑得到同样的桶号（不含随机/时间因素）
snapshot = {r["name"]: r["new_priority"] for r in recs_j}
recs_j2, _ = tool.collect(cfg_j)
tool.remap_priority(recs_j2)
tool.assign_names(recs_j2)
snapshot2 = {r["name"]: r["new_priority"] for r in recs_j2}
check("J5 重跑得到完全相同的 (名称 -> 桶号) 映射", snapshot == snapshot2,
      "差异=%r" % {k: (snapshot.get(k), snapshot2.get(k))
                  for k in set(snapshot) | set(snapshot2)
                  if snapshot.get(k) != snapshot2.get(k)})

print()
print("=" * 70)
print("K. 无人值守开关 ASSUME_YES")
print("=" * 70)

ONE_CLICK = os.path.join(ROOT, "一键导入.py")

src = open(ONE_CLICK, encoding="utf-8").read()
check("K1 一键导入.py 里存在 _assume_yes_env 判定", "_assume_yes_env" in src)
check("K2 只在导入那一处应用 ASSUME_YES（不放进 ask_yes）",
      src.count("_assume_yes_env()") == 2,
      "出现 %d 次" % src.count("_assume_yes_env()"))
check("K3 删除类操作未被 ASSUME_YES 放宽",
      "def ask_yes(prompt):" in src and
      "ASSUME_YES 不在这里生效" in src)

# entrypoint.sh 必须设 ASSUME_YES，否则容器里导入会被静默取消
ep = open(os.path.join(ROOT, "entrypoint.sh"), encoding="utf-8").read()
check("K4 entrypoint.sh 设置了 ASSUME_YES", "ASSUME_YES=1" in ep)
check("K5 entrypoint.sh 默认跑一次（RUN_INTERVAL_SECONDS 缺省为 0）",
      'INTERVAL="${RUN_INTERVAL_SECONDS:-0}"' in ep)
check("K6 entrypoint.sh 会校验必填环境变量",
      "SUB2API_BASE_URL" in ep and "SUB2API_ADMIN_KEY" in ep)
check("K7 entrypoint.sh 按脚本位置推导目录（不硬编码 /app）",
      'dirname "$0"' in ep)

# Dockerfile 的默认命令必须是非交互入口
dk = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
check("K8 Dockerfile 的 CMD 指向 entrypoint.sh（不是交互菜单）",
      'CMD ["/app/entrypoint.sh"]' in dk,
      "实际: %r" % [l for l in dk.split("\n") if l.startswith("CMD")])
check("K9 Dockerfile 给 entrypoint.sh 加了执行位",
      "chmod +x /app/entrypoint.sh" in dk)

print()
print("=" * 70)
print("L. 连接信息的前置校验（地址为空不再甩 traceback）")
print("=" * 70)


def _expect_raise(fn, exc=RuntimeError):
    try:
        fn()
        return None
    except exc as e:
        return str(e)
    except Exception as e:
        return "__WRONG__%s: %s" % (type(e).__name__, e)


# 地址为空：以前会一路走到 urllib 抛 "unknown url type: '/api/v1/...'"
msg = _expect_raise(lambda: tool.Sub2Api({"sub2api_base_url": "",
                                          "sub2api_admin_key": "k"}))
check("L1 地址为空时立刻报错（不是 urllib 的 unknown url type）",
      msg is not None and "unknown url type" not in msg and "地址为空" in msg,
      "%r" % msg)

# 缺协议头
msg = _expect_raise(lambda: tool.Sub2Api({"sub2api_base_url": "sub2api:8080",
                                          "sub2api_admin_key": "k"}))
check("L2 地址缺协议头时给出明确提示",
      msg is not None and "协议头" in msg, "%r" % msg)

# 报错信息里要包含"怎么填"，不能只说错
msg = _expect_raise(lambda: tool.Sub2Api({"sub2api_base_url": "",
                                          "sub2api_admin_key": "k"})) or ""
check("L3 报错里给了三种填法",
      "设置.json" in msg and "SUB2API_BASE_URL" in msg, "%r" % msg[:120])
check("L4 报错里提醒容器不要用 127.0.0.1",
      "127.0.0.1" in msg, "%r" % msg[:160])

# 正常地址仍然工作，且尾部斜杠被去掉
api = tool.Sub2Api({"sub2api_base_url": "http://sub2api:8080/",
                    "sub2api_admin_key": "k"})
check("L5 正常地址可构造且去尾斜杠", api.base == "http://sub2api:8080", api.base)
api = tool.Sub2Api({"sub2api_base_url": "https://a.example.com",
                    "sub2api_admin_key": "k"})
check("L6 https 地址正常", api.base == "https://a.example.com", api.base)

# 一键导入.py 的入口校验必须在 sync_constants 之前 ——
# 否则地址没配也会先发网络请求去拉 GitHub 源码
_oneclick = open(os.path.join(ROOT, "一键导入.py"), encoding="utf-8").read()
_i_guard = _oneclick.find("还没配置 sub2api 连接信息")
_i_sync = _oneclick.find("sync_constants(s, verbose=False)")
check("L7 入口校验位于常量同步之前（不会先发网络请求）",
      _i_guard != -1 and _i_sync != -1 and _i_guard < _i_sync,
      "guard@%d sync@%d" % (_i_guard, _i_sync))

# 清空模式（菜单 3）不应被 needs-config 类检查挡住
check("L8 清空模式在取 config 之前就返回",
      _oneclick.find("if wipe_mode:") < _oneclick.find("build_plan(s)"),
      "wipe@%d build_plan@%d" % (_oneclick.find("if wipe_mode:"),
                                 _oneclick.find("build_plan(s)")))

print()
print("=" * 70)
print("G. 汇总")
print("=" * 70)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED: %s" % f)
    sys.exit(1)
print("全部通过")
