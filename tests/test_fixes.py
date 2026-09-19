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
# o3-max 留；o3-mini 被需求①砍掉（"所有带 mini、flash、fast 的模型都不勾选"）
out = model_selection.select_highest_models(
    D("gpt-6.0", "gpt-6.0-preview", "gpt-5.6", "o3-mini", "o3-max"),
    "openai", source_section="openai-compatibility", series={"openai": 6})
check("D10 openai 多族并存、同族同版本全留（mini 除外）",
      set(names(out)) == {"gpt-6.0", "gpt-6.0-preview", "gpt-5.6", "o3-max"},
      "%r" % names(out))
# 需求①：低档词过滤在分族之前做，所以 o3-mini 不会经 o3 族漏出来
check("D10c o3-mini 被需求①砍掉", "o3-mini" not in names(out), "%r" % names(out))
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
# o3 族用 o3-max 而不是 o3-mini —— 后者会被需求①的低档过滤砍掉，
# 那样 o3 族整个消失，就测不出"多族并存"这件事了。
mixed = D("gpt-6", "gpt-6-astra", "o3-max", "gpt-5.6")
cx = model_selection.select_highest_models(mixed, "openai",
                                           source_section="codex-api-key",
                                           series={"openai": 6})
oa = model_selection.select_highest_models(mixed, "openai",
                                           source_section="openai-compatibility",
                                           series={"openai": 6})
check("D13 codex 与 openai 策略确实分流", names(cx) != names(oa),
      "codex=%r openai=%r" % (names(cx), names(oa)))
check("D14 codex 结果不含 o3（单族）", "o3-max" not in names(cx), "%r" % names(cx))
check("D15 openai 结果保留 o3（多族）", "o3-max" in names(oa), "%r" % names(oa))

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

# o3 族用 o3-max：o3-mini 会被需求①的低档过滤砍掉，测不出分流差异
models = D("gpt-6", "gpt-6-astra", "o3-max", "gpt-5.6")
m_cx = tool.build_model_mapping(models, None, "openai", source_section="codex-api-key")
m_oa = tool.build_model_mapping(models, None, "openai", source_section="openai-compatibility")
check("F1 codex 映射不含 o3", "o3-max" not in m_cx, "%r" % sorted(m_cx))
check("F2 openai 映射含 o3", "o3-max" in m_oa, "%r" % sorted(m_oa))
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
print("M. 跨文件调用签名一致性（防漏改）")
print("=" * 70)

import ast as _ast  # noqa: E402

_tool_src = open(os.path.join(ROOT, "tool.py"), encoding="utf-8").read()
_sigs = {}
for _n in _ast.walk(_ast.parse(_tool_src)):
    if isinstance(_n, _ast.FunctionDef):
        _args = _n.args.args
        _req = len(_args) - len(_n.args.defaults)
        _sigs[_n.name] = {
            "params": [a.arg for a in _args],
            "req": _req,
            "kwonly": [a.arg for a in _n.args.kwonlyargs],
            "line": _n.lineno,
        }

# 这些文件 import 了 tool.py 的函数，改动签名时最容易漏改它们
_callers = ["一键导入.py", "run.py"]
_problems = []
_checked = 0
for _f in _callers:
    _fp = os.path.join(ROOT, _f)
    if not os.path.exists(_fp):
        continue
    for _n in _ast.walk(_ast.parse(open(_fp, encoding="utf-8").read())):
        if not (isinstance(_n, _ast.Call) and isinstance(_n.func, _ast.Name)):
            continue
        _name = _n.func.id
        if _name not in _sigs:
            continue
        _s = _sigs[_name]
        _checked += 1
        _npos = len(_n.args)
        _given = {k.arg for k in _n.keywords if k.arg}
        _covered = set(_s["params"][:_npos]) | _given
        _missing = [p for p in _s["params"][:_s["req"]] if p not in _covered]
        _unknown = _given - set(_s["params"]) - set(_s["kwonly"])
        _extra = _npos - len(_s["params"])
        if _missing or _unknown or _extra > 0:
            _problems.append(
                "%s:%d %s() 缺=%r 未知kw=%r 多余位置=%d"
                % (_f, _n.lineno, _name, _missing, sorted(_unknown), max(0, _extra)))

check("M1 跨文件调用签名全部匹配（共检查 %d 处）" % _checked,
      not _problems, "; ".join(_problems[:4]))

# 已经有对应的静态检查了，再显式盯住 to_account 这个踩过坑的函数
_to_acc_params = _sigs.get("to_account", {}).get("params") or []
check("M2 to_account 仍是 5 参数（改签名时别忘了 一键导入.py）",
      len(_to_acc_params) == 5, "%r" % _to_acc_params)


print()
print("=" * 70)
print("N. 客户端形态请求头（需求 2⑴：站方只认特定客户端）")
print("=" * 70)

# 以前 build_header_overrides 只做**被动搬运**：CPA 里写了就搬，没写就什么都不带。
# 于是靠 CPA 默认伪装跑通的站点，导入 sub2api 后退化成裸请求 →
# 上游回 `503 No available accounts / this group only allows ...`。

_h, _d = tool.build_header_overrides({}, "anthropic")
check("N1 anthropic 空输入也会补客户端头（以前返回空）",
      len(_h) > 0, "%d 个" % len(_h))
check("N2 补出的 UA 是 claude-cli 形态",
      str(_h.get("user-agent", "")).startswith("claude-cli/"),
      repr(_h.get("user-agent")))
check("N3 补出 x-app: cli", _h.get("x-app") == "cli", repr(_h.get("x-app")))

_h2, _ = tool.build_header_overrides({"user-agent": "my-custom/1.0"}, "anthropic")
check("N4 用户显式配的 UA 不被覆盖（只补缺）",
      _h2.get("user-agent") == "my-custom/1.0", repr(_h2.get("user-agent")))

_h3, _ = tool.build_header_overrides({}, "gemini")
check("N5 gemini 段不再被整段丢弃（以前 HEADER_OK_PLATFORMS 不含 gemini）",
      len(_h3) > 0, "%d 个" % len(_h3))

_h4, _ = tool.build_header_overrides({"authorization": "x"}, "anthropic")
check("N6 黑名单仍然拦住（带了会被服务端 400 拒整条账号）",
      "authorization" not in _h4, "")
check("N7 不补 accept-encoding（在黑名单里）",
      "accept-encoding" not in _h4, "")

_many = {"h%d" % i: "v" for i in range(200)}
_h5, _ = tool.build_header_overrides(_many, "anthropic")
check("N8 超过 64 条上限时被裁剪", len(_h5) <= tool.MAX_HEADER_ENTRIES,
      "%d 条" % len(_h5))
check("N9 裁剪时优先保留客户端形态头",
      "user-agent" in _h5 and "x-app" in _h5, "")
check("N10 不支持的平台仍然全丢（不硬塞头）",
      tool.build_header_overrides({"a": "b"}, "unknownplat")[0] == {}, "")

print()
print("=" * 70)
print("O. 单对象接口信封（/accounts/{id} 带 {code,data,message}）")
print("=" * 70)

# 实测 2026-09-19：ensure_test_plans 直接读顶层 credentials，拿到 None，
# 于是每条账号都"未找到可用模型"，231 条探活计划全部挂载失败。
# 探活是停用账号恢复的唯一证据来源，挂了等于自动恢复链路整体断掉。
_uw = tool._unwrap_one({"code": 0, "message": "ok",
                        "data": {"id": 7, "platform": "anthropic",
                                 "credentials": {"model_mapping": {"claude-opus-5": "x"}}}})
check("O1 单对象信封能取出 data", _uw.get("id") == 7, repr(_uw)[:120])
check("O2 取出的对象带 credentials",
      isinstance(_uw.get("credentials"), dict) and "model_mapping" in _uw["credentials"], "")
_uw2 = tool._unwrap_one({"id": 9, "platform": "openai"})
check("O3 无信封时原样返回（不丢数据）", _uw2.get("id") == 9, repr(_uw2)[:80])
check("O4 非 dict 输入返回空 dict", tool._unwrap_one(None) == {} and tool._unwrap_one([]) == {}, "")
_uw3 = tool._unwrap_one({"data": {"items": [{"id": 3}]}})
check("O5 data.items 也能取第一条", _uw3.get("id") == 3, repr(_uw3)[:80])

print()
print("=" * 70)
print("P. 探活选型口径与写入白名单一致（source_section 贯通）")
print("=" * 70)

# codex 段与 openai-compatibility 段都是 platform=openai，但选型分流不同：
# codex 走单族、openai 走多族。探活若不传 source_section 会落到多族分支，
# 挑出一个**不在白名单里**的模型，那样探活结果不能代表账号真实可用性。
_mm = {"gpt-6-astra": "gpt-6-astra", "gpt-5.6": "gpt-5.6", "claude-opus-5": "claude-opus-5"}
_a = tool._pick_probe_model(_mm, "openai", source_section="codex-api-key")
_b = tool._pick_probe_model(_mm, "openai", source_section="openai-compatibility")
check("P1 _pick_probe_model 接受 source_section 参数", _a is not None, repr(_a))
check("P2 不传 source_section 时仍能工作（向后兼容）",
      tool._pick_probe_model(_mm, "openai") is not None, "")
check("P3 anthropic 优先挑 sonnet",
      "sonnet" in (tool._pick_probe_model(
          {"claude-sonnet-5": "x", "claude-opus-5": "x", "claude-haiku-4-5": "x"},
          "anthropic") or ""),
      "")

print()
print("=" * 70)
print("Q. 部署链路（本机实测暴露过的坑）")
print("=" * 70)

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)


def _read(rel):
    try:
        with open(os.path.join(_root, rel), encoding="utf-8") as _f:
            return _f.read()
    except Exception as _ex:
        # 读不到就打印出来，不要静默返回空串——否则下面所有"文件里应当包含 X"
        # 的断言都会因为读到空串而误报失败，看起来像代码有问题。
        print("  ! 读不到 %s：%s" % (rel, _ex))
        return ""


_dc = _read("docker-compose.yml")
# 实测 2026-09-19：metacubex/mihomo:latest 里 HAVE wget/nc/sed/awk/grep，
# MISS curl/python/python3。所以 mihomo 的 healthcheck 不能是 python3 或 curl。
check("Q1 mihomo 的 healthcheck 指向 healthcheck.sh（不是 python3）",
      '"sh", "/root/.config/mihomo/healthcheck.sh"' in _dc, "")
_hc_lines = [l for l in _dc.splitlines() if l.strip().startswith("test:")]
check("Q1b mihomo 的 test 里不出现 python3 / curl",
      not any("python3" in l or "curl" in l for l in _hc_lines),
      repr(_hc_lines[:2]))

check("Q2 新增了 mihomo 容器用的 healthcheck.sh",
      os.path.exists(os.path.join(_root, "mihomo-manager", "mihomo", "healthcheck.sh")), "")
_hcsh = _read("mihomo-manager/mihomo/healthcheck.sh")
# 去掉注释行后再判，避免"注释里提到 curl"被误判成依赖 curl
_hc_code = "\n".join(l for l in _hcsh.splitlines() if not l.strip().startswith("#"))
check("Q3 healthcheck.sh 代码里不依赖 curl / python",
      "curl" not in _hc_code and "python" not in _hc_code, "")
check("Q4 healthcheck.sh 只用 busybox 可用命令（wget + nc）",
      "wget" in _hc_code and "nc " in _hc_code, "")

# mihomo-init 缺 env_file 时，.env 里的 MIHOMO_SUBSCRIPTIONS 传不进容器
# （值里含 = 和 ;，compose 的 ${VAR} 插值处理不了），init 会因占位符未展开
# 退出 1，mihomo 因 service_completed_successfully 永不启动。
_init_block = (_dc.split("mihomo-init:")[1].split("\n  mihomo:")[0]
               if "mihomo-init:" in _dc else "")
check("Q5 本机 compose 里 mihomo-init 接了 env_file", "env_file" in _init_block, "")
check("Q6 docker-compose 的 bind mount 带 :Z（CentOS SELinux）",
      ":ro,Z" in _dc and ":/app/out:Z" in _dc, "")
check("Q7 子网可通过变量覆盖（VPS 网段冲突）",
      "CPA2SUB2API_SUBNET" in _dc, "")
check("Q8 有 pull_policy（否则 up -d 不会拉新镜像）",
      "pull_policy: always" in _dc, "")

_di = _read(".dockerignore")
check("Q9 .dockerignore 排除 设置.json 的所有变体（含带时间戳的备份）",
      "*设置*.json" in _di and "设置.json.*" in _di, "")
check("Q10 .dockerignore 排除 docx_temp（含明文 key 的截图）", "docx_temp/" in _di, "")
check("Q11 .dockerignore 排除 graphify-out", "graphify-out/" in _di, "")
check("Q12 .dockerignore 排除 .upstream_cache", ".upstream_cache/" in _di, "")
check("Q13 .dockerignore 排除子目录 __pycache__", "**/__pycache__/" in _di, "")

_dex = _read(".env.example")
check("Q14 .env.example 里有 REVIVE_PROVEN_INACTIVE（此前从未接线）",
      "REVIVE_PROVEN_INACTIVE" in _dex, "")
check("Q15 .env.example 里有 PROBE_INACTIVE", "PROBE_INACTIVE" in _dex, "")

print()
print("=" * 70)
print("R. 空 key 凭据不再无声丢弃（需求第 8 条：日志可排查）")
print("=" * 70)

# collect 以前对 api-key 为空的子条目直接跳过且不记账，用户永远不知道漏填了。
_cfg_min = {
    "openai-compatibility": [
        {"name": "p1", "base-url": "https://a.example.com", "priority": 1,
         "api-key-entries": [{"api-key": "sk-a"},
                             {"weight": 1}],          # 无 api-key
         "models": [{"name": "gpt-6", "alias": "gpt-6"}]},
    ],
    "claude-api-key": [
        {"base-url": "https://b.example.com", "priority": 2},   # 无 api-key
    ],
}
_recs, _dropped = tool.collect(_cfg_min)
_silent = [s for s in _dropped if "没有 api-key" in s or "api-key-entries" in s]
check("R1 空 api-key 子条目被记入 dropped_notes（以前无声丢弃）",
      len(_silent) >= 2, "命中 %d 条：%r" % (len(_silent), _silent[:3]))
check("R2 有 api-key 的条目照常导入", len(_recs) == 1, "%d 条" % len(_recs))

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
