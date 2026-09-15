# -*- coding: utf-8 -*-
"""从 sub2api 和 CPA 源码提取常量（自动同步用）

设计：
  - 提取失败时降级到兜底值（当前工具内置的硬编码），不中断运行
  - 诊断模式下报告"源码新增了什么、映射表没跟上"的不一致
  - 只提取数据常量，不尝试推断业务逻辑（那部分仍需人工维护）

用法：
  from extract import get_constants
  c = get_constants(sub_path, cpa_path)
  print(c["header_blacklist"])
"""
import os
import re


# =============================================================================
# 兜底值（当前工具的硬编码，提取失败时用）
# =============================================================================
FALLBACK = {
    "header_blacklist": {
        "host", "content-length", "content-type", "transfer-encoding",
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "proxy-connection", "te", "trailer", "upgrade",
        "authorization", "x-api-key", "x-goog-api-key", "cookie",
        "accept-encoding",
        "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
        "sec-websocket-protocol", "sec-websocket-accept",
        "session_id", "conversation_id", "x-codex-turn-state",
        "x-codex-turn-metadata", "chatgpt-account-id",
        "x-claude-code-session-id", "x-client-request-id", "x-grok-conv-id",
    },
    "max_header_entries": 64,
    "max_header_name": 200,
    "max_header_value": 8192,
    "platforms": ["anthropic", "openai", "gemini", "antigravity", "grok",
                  "kimi", "zhipu", "deepseek", "minimax", "composite"],
    "account_types": ["oauth", "setup-token", "apikey", "upstream", "bedrock", "service_account"],
    "api_protocols": ["chat_completions", "anthropic", "responses", "adaptive"],

    # ---- 模型「就高原则」的系列门槛 ----
    # filter_models_by_highest 用它决定「哪一代算当前代」。写死数字会在上游
    # 发新一代模型时静默过期：门槛还停在 claude-*-5，而上游已经是 6 代，
    # 于是新模型全被当成旧代砍掉。所以从源码里出现过的真实模型名反推。
    # 提取失败才用这里的兜底值（对应 2026-09 的上游状态）。
    "model_series": {
        "anthropic": 5,   # claude-*-5 系列（含 claude-sonnet-4-5 这种 x-5 写法）
        "openai": 6,      # Codex 场景只放 gpt-6
    },
    "cpa_sections": {
        "claude-api-key": ["api-key", "priority", "weight", "prefix", "base-url",
                           "proxy-url", "models", "headers", "websockets", "fingerprint-profile",
                           "cloak", "request-scoped-errors", "disabled", "disable-cooling",
                           "request-retry", "rebuild-mid-system-message",
                           "experimental-cch-signing"],
        "codex-api-key": ["api-key", "priority", "weight", "prefix", "base-url",
                          "proxy-url", "models", "headers", "websockets", "fingerprint-profile",
                          "cloak", "request-scoped-errors", "alpha-search", "disabled",
                          "disable-cooling", "request-retry", "support-prompt-cache-key"],
        "gemini-api-key": ["api-key", "priority", "weight", "prefix", "base-url",
                           "proxy-url", "models", "headers", "disabled", "disable-cooling",
                           "request-retry", "request-scoped-errors"],
        "openai-compatibility": ["name", "priority", "disabled", "prefix", "base-url",
                                 "proxy-url", "models", "headers", "api-key-entries",
                                 "disable-cooling", "request-retry", "request-scoped-errors"],
    },

    # ---- 以下几项支撑「调度可恢复性」相关判断，同样必须跟随上游 ----
    # 账号状态枚举。本工具靠它决定停用时发什么值：admin 接口接受
    # "inactive"，而 domain 层常量是 "disabled"，两者由服务端归一
    # （account_data.go:777-781）。上游要是改了字面量，这里不跟就会 400。
    "account_statuses": ["active", "error", "disabled"],
    # 建号请求体支持的字段。用来判断 auto_pause_on_expired 这类字段能不能
    # 在创建时就带上，而不是建完再补一次 PUT。
    "create_account_fields": [
        "name", "notes", "platform", "type", "credentials", "extra",
        "proxy_id", "concurrency", "priority", "rate_multiplier",
        "load_factor", "group_ids", "expires_at", "auto_pause_on_expired",
        "upstream_billing_probe_enabled", "confirm_mixed_channel_risk",
    ],
    # 批量更新请求体支持的字段。schedulable 只在这里出现——PUT /accounts/:id
    # 没有它，所以「重新打开调度开关」只能走 bulk-update。
    # 注意官方文档 skills/sub2api-admin/references/admin-cli.md:89 列举时
    # 漏了 schedulable，以 Go 结构体为准。
    "bulk_update_fields": [
        "account_ids", "filters", "name", "proxy_id", "concurrency",
        "priority", "rate_multiplier", "load_factor", "status", "schedulable",
        "group_ids", "credentials", "extra",
        "upstream_billing_probe_enabled", "confirm_mixed_channel_risk",
    ],
    # 分组的 schema 默认值。本工具据此判断哪些字段必须显式写：
    # auto_pause_on_expired 默认 true，不显式传就会被勾上。
    "account_defaults": {"auto_pause_on_expired": True, "schedulable": True},
    # 定时探活计划的请求字段（auto_recover 是把账号从 error 捞回来的开关）。
    "test_plan_fields": ["account_id", "model_id", "cron_expression",
                         "enabled", "max_results", "auto_recover"],
    # 渠道/分组逐模型定价的字段与单位。单位是**每 token 的 USD**，
    # 不是每百万——错一个 1e6 就是贵一百万倍，所以要能从源码核对。
    "pricing_fields": [
        "platform", "models", "billing_mode", "input_price", "output_price",
        "cache_write_price", "cache_write_1h_price", "cache_read_price",
        "fast_multiplier", "flex_multiplier", "max_reasoning_effort_multiplier",
        "image_input_price", "image_output_price", "per_request_price",
        "intervals", "time_pricing",
    ],
    "pricing_unit_is_per_token": True,
}


def _read(p):
    if not os.path.exists(p):
        return ""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


# =============================================================================
# sub2api 提取器
# =============================================================================
def extract_sub2api(sub_root):
    """提取 sub2api 的常量。返回 dict，提取失败的项用兜底值。"""
    out = {}
    aho = os.path.join(sub_root, "internal/service/account_header_override.go")
    s = _read(aho)

    # 1. 黑名单
    m = re.search(r"headerOverrideBlockedNames\s*=\s*map\[string\]struct\{\}\{(.*?)\n\}", s, re.S)
    if m:
        names = re.findall(r'"([^"]+)"\s*:', m.group(1))
        out["header_blacklist"] = set(names) if names else FALLBACK["header_blacklist"]
    else:
        out["header_blacklist"] = FALLBACK["header_blacklist"]

    # 2. 长度上限
    for k, c in [("max_header_entries", "maxHeaderOverrideEntries"),
                 ("max_header_name", "maxHeaderOverrideNameLength"),
                 ("max_header_value", "maxHeaderOverrideValueLength")]:
        mm = re.search(r"%s\s*=\s*(\d+)" % c, s)
        out[k] = int(mm.group(1)) if mm else FALLBACK[k]

    # 3. 平台/类型/协议枚举
    const = _read(os.path.join(sub_root, "internal/domain/constants.go"))
    out["platforms"] = re.findall(r'Platform\w+\s*=\s*"([a-z_]+)"', const) or FALLBACK["platforms"]
    out["account_types"] = re.findall(r'AccountType\w+\s*=\s*"([a-z_\-]+)"', const) or FALLBACK["account_types"]
    out["api_protocols"] = re.findall(r'APIProtocol\w+\s*=\s*"([a-z_]+)"', const) or FALLBACK["api_protocols"]

    # 4. 账号状态枚举。
    #    constants.go 的同一个 const 块里还混着 API Key / 订阅的状态
    #    （unused/used/expired/suspended...），全抓会把不属于账号的值也算进来。
    #    以 ent/schema/account.go 里 status 字段的注释为准——那里写明了
    #    账号真正允许的三个值；抓不到再退回按名字挑常量。
    sch_txt = _read(os.path.join(sub_root, "ent/schema/account.go"))
    m = re.search(r'field\.String\("status"\).*?Comment\("([^"]*)"\)',
                  sch_txt, re.S)
    st_vals = re.findall(r'"([a-z_]+)"', m.group(1)) if m else []
    if not st_vals:
        st_vals = [v for n, v in
                   re.findall(r'(Status(?:Active|Error|Disabled))\s*=\s*"([a-z_]+)"',
                              const)]
    # 注释里同一个值可能出现多次，去重但保持首次出现的顺序
    seen, uniq = set(), []
    for v in st_vals:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    out["account_statuses"] = uniq or FALLBACK["account_statuses"]

    # 5. 请求体字段清单。
    #    这几个直接决定「某个字段能不能在这一步传」：
    #      · create 没有 status/schedulable  -> 停用只能建完再改
    #      · update 有 status 没 schedulable -> 开调度开关只能走 bulk-update
    #      · bulk-update 两个都有            -> 唯一能批量恢复调度的入口
    #    上游一旦给 create 补上 status，本工具就能少发一轮请求；靠提取而不是
    #    靠记忆，才能在上游变更时自动受益。
    ah = _read(os.path.join(sub_root, "internal/handler/admin/account_handler.go"))
    ad = _read(os.path.join(sub_root, "internal/handler/admin/account_data.go"))
    blob = ah + "\n" + ad
    out["create_account_fields"] = (_struct_json_fields(blob, "CreateAccountRequest")
                                    or FALLBACK["create_account_fields"])
    out["bulk_update_fields"] = (_struct_json_fields(blob, "BulkUpdateAccountsRequest")
                                 or FALLBACK["bulk_update_fields"])

    # 6. Ent schema 默认值。auto_pause_on_expired 默认 true 是「过期自动暂停
    #    调度」那个勾默认被选中的根因；本工具据此决定必须显式写 false。
    sch = _read(os.path.join(sub_root, "ent/schema/account.go"))
    defaults = dict(FALLBACK["account_defaults"])
    for field in ("auto_pause_on_expired", "schedulable"):
        m = re.search(r'field\.Bool\("%s"\)\s*\.\s*Default\((true|false)\)'
                      % re.escape(field), sch, re.S)
        if m:
            defaults[field] = (m.group(1) == "true")
    out["account_defaults"] = defaults

    # 7. 定时探活计划字段（auto_recover 决定测试成功后是否自动恢复账号）
    st = _read(os.path.join(sub_root,
                            "internal/handler/admin/scheduled_test_handler.go"))
    out["test_plan_fields"] = (_struct_json_fields(st, "createScheduledTestPlanRequest")
                               or FALLBACK["test_plan_fields"])

    # 8. 逐模型定价字段 + 单位。
    #    单位从建表 SQL 的列注释里核对：写的是「每 token 输入价格（USD）」，
    #    所以换算要除 1e6。上游若改成每百万，这里能立刻发现。
    ch = _read(os.path.join(sub_root, "internal/service/channel.go"))
    out["pricing_fields"] = (_struct_json_fields(ch, "ChannelModelPricing")
                             or FALLBACK["pricing_fields"])
    mig = _read(os.path.join(sub_root, "migrations/081_create_channels.sql"))
    if mig:
        out["pricing_unit_is_per_token"] = ("每 token" in mig or "per token" in mig.lower())
    else:
        out["pricing_unit_is_per_token"] = FALLBACK["pricing_unit_is_per_token"]

    return out


def _struct_json_fields(src, struct_name):
    """从 Go 源码里抠出某个 struct 的全部 json tag 名。

    只取 tag 的第一段（逗号前），把 `json:"x,omitempty"` 归一成 `x`；
    `json:"-"` 跳过。找不到该 struct 就返回空列表，交给调用方走兜底。

    结束位置靠**行首的 }** 判断而不是 `.*?\\n\\}`：结构体里嵌套的匿名
    struct、map[string]struct{} 之类会带缩进的 `}`，非贪婪匹配会在那里
    提前收尾，只抓到前几个字段（实测 CreateAccountRequest 因此整个抓空，
    静默退回兜底值——而兜底值正是我们想摆脱的硬编码）。
    """
    if not src:
        return []
    m = re.search(r"type\s+%s\s+struct\s*\{" % re.escape(struct_name), src)
    if not m:
        return []
    body, depth = [], 1
    for line in src[m.end():].split("\n"):
        stripped = line.strip()
        # 行首（无缩进）的 } 才是结构体自身的闭合
        if stripped == "}" and not line[:1].isspace():
            break
        body.append(line)
        if len(body) > 400:        # 兜底，防止匹配失控读完整个文件
            break
    out = []
    for tag in re.findall(r'json:"([^"]+)"', "\n".join(body)):
        name = tag.split(",")[0].strip()
        if name and name != "-":
            out.append(name)
    return out


# =============================================================================
# CPA 提取器
# =============================================================================
def extract_cpa(cpa_root):
    """提取 CPA 配置结构体的字段清单。返回 dict[section -> list[field]]。"""
    ct = _read(os.path.join(cpa_root, "internal/config/config_types.go"))
    if not ct:
        return FALLBACK["cpa_sections"]

    out = {}
    section_struct = [
        ("claude-api-key", "ClaudeKey"),
        ("codex-api-key", "CodexKey"),
        ("gemini-api-key", "GeminiKey"),
        ("openai-compatibility", "OpenAICompatibility"),
    ]
    for sec, st in section_struct:
        m = re.search(r"type %s struct \{(.*?)\n\}" % st, ct, re.S)
        if m:
            tags = re.findall(r'yaml:"([a-z\-]+)', m.group(1))
            out[sec] = tags if tags else FALLBACK["cpa_sections"].get(sec, [])
        else:
            out[sec] = FALLBACK["cpa_sections"].get(sec, [])

    return out


def extract_model_series(sub_root, cpa_root):
    """从上游源码里出现过的真实模型名，反推每个平台「当前代」的门槛。

    为什么不写死：`filter_models_by_highest` 需要知道「claude 现在是第几代」
    才能只放行当代模型。写死 5 的话，上游发了 claude-*-6 之后本工具会把
    6 代全当旧代砍掉——而这正是「严禁硬编码」要避免的失效方式。

    做法：扫源码里所有 claude-* / gpt-* 字面量，取主版本号的最大值。
      · claude：同时认 `claude-opus-5` 和 `claude-sonnet-4-5` 两种写法，
        取每个名字里最后一段数字（4-5 的代是 5，不是 4）。
      · gpt：只认 `gpt-<major>`，小数点后的次版本不参与代际判断
        （gpt-5.6 和 gpt-5.5 同属 5 代）。

    只扫源码里确实出现过的名字，不猜未来版本：门槛只会随上游真的发新模型
    而前移。返回 {platform: major}；某平台一个名字都没扫到就不进结果，
    由调用方回落到兜底值。
    """
    names = set()
    for root, files in ((sub_root, _MODEL_SCAN_SUB), (cpa_root, _MODEL_SCAN_CPA)):
        if not root:
            continue
        for rel in files:
            txt = _read(os.path.join(root, rel))
            if txt:
                names.update(re.findall(r'"((?:claude|gpt)-[a-z0-9.\-]+)"', txt, re.I))

    out = {}
    claude_majors = [_claude_major(n) for n in names if n.lower().startswith("claude-")]
    claude_majors = [x for x in claude_majors if x is not None]
    if claude_majors:
        out["anthropic"] = max(claude_majors)

    gpt_majors = []
    for n in names:
        m = re.match(r"^gpt-(\d+)", n, re.I)
        if m:
            gpt_majors.append(int(m.group(1)))
    if gpt_majors:
        out["openai"] = max(gpt_majors)

    return out


def _claude_major(name):
    """Claude 模型名里的**代数**（主版本），取不到返回 None。

    代数是家族名后的**第一个**数字段，不是最后一个：
      claude-opus-5             -> 5
      claude-opus-4-8           -> 4   （Opus 4.8，不是 8 代）
      claude-sonnet-4-5         -> 4   （Sonnet 4.5，不是 5 代）
      claude-fable-5-1          -> 5   （Fable 5.1）
      claude-3-5-sonnet         -> 3
      claude-sonnet-4-5-2025…   -> 4   （日期后缀不参与）
    次版本与日期后缀一律不影响代际判断。

    实现复用 model_selection.claude_major：代际判定只能有一份实现，
    否则上游发新一代时两处会各自漂移（本模块负责"从源码反推门槛"、
    model_selection 负责"按门槛筛选"，判据必须完全一致）。
    """
    try:
        from model_selection import claude_major
        return claude_major(name)
    except ImportError:
        # 兜底：与 model_selection.claude_major 等价的正则
        for pat in (r'^claude-[a-z]+-(\d+)', r'^claude-(\d+)'):
            m = re.match(pat, str(name), re.I)
            if m:
                return int(m.group(1))
        return None


# 扫模型名的文件清单。挑的是「一定会写全模型名」的地方：模型注册表、
# 执行器默认值、定价表。不整仓扫是为了快，也为了少踩测试夹具里的假名字。
_MODEL_SCAN_SUB = (
    "internal/domain/constants.go",
    "internal/service/channel.go",
    "internal/service/account.go",
)
_MODEL_SCAN_CPA = (
    "internal/config/config_types.go",
    "internal/registry/models.go",
)


# =============================================================================
# 主入口
# =============================================================================
def get_constants(sub_path, cpa_path):
    """提取常量。返回合并后的字典。提取失败的项自动降级到兜底值。

    参数：
      sub_path: sub2api 的 backend 目录
      cpa_path: CPA 项目根目录
    """
    c = extract_sub2api(sub_path)
    c["cpa_sections"] = extract_cpa(cpa_path)
    # 模型代际门槛：扫到就用扫到的，扫不到的平台保留兜底值
    series = dict(FALLBACK["model_series"])
    series.update(extract_model_series(sub_path, cpa_path))
    c["model_series"] = series
    return c


def diagnose(sub_path, cpa_path, current_mappings):
    """诊断：源码新增了什么、映射表没跟上。

    返回 (warnings, infos)：
      warnings: 需要人工决策的不一致
      infos: 参考信息
    """
    c = get_constants(sub_path, cpa_path)
    warns, infos = [], []

    # 1. 检查 CPA 源码新增的配置段是否都在 SECTION_MAP 里
    mapped_sections = set(current_mappings["SECTION_MAP"].keys())
    new_sections = set(c["cpa_sections"]) - mapped_sections
    if new_sections:
        warns.append("CPA 新增配置段：%s，SECTION_MAP 未配置" % ", ".join(sorted(new_sections)))

    # 2. 检查映射表里用到的 platform/type 是否还在 sub2api 源码里（删了会导入失败）
    mapped_platforms = {v[0] for v in current_mappings["SECTION_MAP"].values()}
    mapped_types = {v[1] for v in current_mappings["SECTION_MAP"].values()}
    
    deleted_plat = mapped_platforms - set(c["platforms"])
    deleted_types = mapped_types - set(c["account_types"])
    
    if deleted_plat:
        warns.append("映射表用到的平台已从 sub2api 删除：%s" % ", ".join(sorted(deleted_plat)))
    if deleted_types:
        warns.append("映射表用到的类型已从 sub2api 删除：%s" % ", ".join(sorted(deleted_types)))

    # 3. CPA 配置段新增字段（info 级别）
    # 基线 = 本工具 collect() 确实会读取的字段。只有真正没被处理的新字段才提示，
    # 否则每次启动都会刷一堆"新增字段"噪音，真出问题时反而没人看。
    handled = {
        "api-key", "api-key-entries", "base-url", "priority", "weight", "prefix",
        "proxy-url", "models", "headers", "excluded-models", "websockets",
        "alpha-search", "fingerprint-profile", "cloak", "disabled", "name",
        "disable-cooling", "request-retry", "request-scoped-errors",
        "rebuild-mid-system-message", "support-prompt-cache-key",
        # 确认的死字段：CPA 里零运行时消费者（config_types.go:435-437）
        "experimental-cch-signing",
    }
    for sect in c["cpa_sections"]:
        new_fields = set(c["cpa_sections"][sect]) - handled
        if new_fields:
            warns.append("CPA %s 新增了本工具未处理的字段：%s"
                         % (sect, ", ".join(sorted(new_fields))))

    return warns, infos
