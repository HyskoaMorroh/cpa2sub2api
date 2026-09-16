#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型选择"就高原则"的**唯一权威实现**。

需求来源（cpa2sub2api修改要求.docx 第 2 条第⑵项）：
- gemini：只选该类型对应的最高级别模型，必须是带 pro 的最高编号
- codex ：取当代 gpt 系列**全部**模型名（单族，"就高"）
- claude：取 claude-*-5（当代）系列
- openai：允许混合多种类型各自的最高级（多族，"就高"）
- 同等级系列模型**全部**勾选（勾了 gpt-5.6 就不能漏 gpt-5.6-sol）
- 检测不到高级模型时，按该系列该类型最高级填充

三条设计约束，改这个文件前请先读：

1. **不写死代数**。代际门槛通过 `series` 参数注入，由 tool.sync_constants
   从上游源码反推（extract.extract_model_series）。没注入时用"实际列表中
   观测到的最高代"兜底，而不是硬编码数字——上游从 6 代走到 7 代时，
   写死的门槛会把 7 代模型当旧模型全部砍掉，且不报错。

2. **codex 与 openai 必须分开**。两者的 platform 都是 "openai"，但口径不同：
   codex 单族、openai 多族。判据是 CPA 的**来源段名**（source_section），
   不是"模型列表里有没有 gpt-6"这种猜测——那会把声明了 gpt-6 的
   openai-compatibility 误当 codex 处理，砍掉它其余全部模型。

3. **不饿死，但要出声**。过滤后一个模型都不剩时退回上一级，
   否则账号会被 serves_nothing() 判成"什么都不提供"从而建成停用。
   但回退**必须**通过 fallback_notes 报出来，不能像以前那样静默——
   静默回退会让"gemini 必须带 pro"这条要求在 flash-only 上游上悄悄失效。
"""

import re

# 回退说明的收集器。调用方传进来就写进去，用于对照表/告警。
# 不传则丢弃（例如 __main__ 自测）。
_FALLBACK_NOTES = []


def _note(msg):
    if _FALLBACK_NOTES is not None:
        _FALLBACK_NOTES.append(msg)


def _name(m):
    return str(m.get("name", "")) if isinstance(m, dict) else ""


# ---------------------------------------------------------------------------
# 代数提取
# ---------------------------------------------------------------------------
# 家族名后的**第一个**数字段就是代数：
#   claude-opus-5      -> 5
#   claude-opus-4-8    -> 4    （Opus 4.8，不是第 8 代）
#   claude-sonnet-4-5  -> 4    （Sonnet 4.5，不是第 5 代）
#   claude-3-5-sonnet  -> 3
# 取「最后一段数字」是错的：会把 Sonnet 4.5 当成 5 代放进来，
# 也会被日期后缀（-20251101）顶成天文数字。次版本与日期都不参与代际。
_CLAUDE_MAJOR_RE = re.compile(r'^claude-[a-z]+-(\d+)', re.IGNORECASE)
# claude-3-5-sonnet 这种"代在前"的旧命名式，代数同样是第一段数字
_CLAUDE_MAJOR_ALT_RE = re.compile(r'^claude-(\d+)', re.IGNORECASE)


def claude_major(name):
    """取 claude 模型的代数，取不到返回 None。"""
    s = str(name or "")
    m = _CLAUDE_MAJOR_RE.match(s) or _CLAUDE_MAJOR_ALT_RE.match(s)
    return int(m.group(1)) if m else None


def openai_gpt_major(name):
    """取 gpt-* 模型的主版本号，非 gpt-* 返回 None。"""
    m = re.match(r'^gpt-(\d+)', str(name or ""), re.IGNORECASE)
    return int(m.group(1)) if m else None


def openai_gpt_version(name):
    """取 gpt-* 的 (major, minor)，非 gpt-* 返回 None。

    为什么需要它而不是只用 openai_gpt_major：需求④原文写的是
    "保留实测最高的 gpt-5.6 系列"，不是 "gpt-5 系列"——
    可见"次最新"的粒度到小数位。只取主版本号的话 gpt-5 与 gpt-5.6
    同为 major=5，回退时会把更旧的 gpt-5 也一起留下，
    而需求明确说"gpt-5 和 gpt-5.6 理论上不可能保留"（指都不该留在最新档里），
    回退档位应当只保留其中**实测最高**的那个，即 gpt-5.6。

      gpt-6        -> (6, 0)
      gpt-5.6      -> (5, 6)
      gpt-5.6-sol  -> (5, 6)    同族变体，需求③要求一起勾
      gpt-5        -> (5, 0)
      gpt-4o       -> (4, 0)    字母后缀不算小数位
    """
    m = re.match(r'^gpt-(\d+)(?:\.(\d+))?', str(name or ""), re.IGNORECASE)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0))


def gemini_version(name):
    """取 gemini 的 (major, minor)，不是 gemini-* 返回 None。

    小数部分**可选**：`gemini-3-pro` 解析成 (3, 0)。
    以前的正则是 `^gemini-(\\d+)\\.(\\d+)`，强制要带小数点，于是
    `gemini-3-pro`（需求原文举的"次最新模型"例子）直接解析成 None、
    被整个跳过 —— 上游若提供 gemini-3-pro，本工具会当它不存在。
    """
    m = re.match(r'^gemini-(\d+)(?:\.(\d+))?(?:[-.]|$)',
                 str(name or ""), re.IGNORECASE)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0))


# 低档模型。需求①原文："所有带 mini、flash、fast 的模型都不勾选，
# 这种模型属于低档次模型，没有存在的价值。"——**只这三个词**。
#
# 用词边界而不是子串匹配：子串会把 `flashback`、`fastapi` 这类名字误伤。
# 分隔符限定为 - _ . 或首尾，覆盖 gpt-6-mini / gemini-3.1-flash /
# gpt_6_fast / gpt-6-mini-2026 这些实际写法。
#
# 不含 haiku：需求①没点它，而需求③要求"相同等级系列的模型全部都要勾选上"，
# claude 的 opus/sonnet/haiku 是同代不同档位，砍掉会违反③。
_LOW_TIER_RE = re.compile(r'(?:^|[-_.])(mini|flash|fast)(?:[-_.]|$)',
                          re.IGNORECASE)


def is_low_tier(name):
    """这个模型名是否带 mini / flash / fast（需求①要排除的低档模型）。"""
    return bool(_LOW_TIER_RE.search(str(name or "")))


def _drop_low_tier(models, label):
    """滤掉低档模型。全被滤光时原样返回并出声。

    为什么滤光了要还回去：需求④兜底写的是"如果检测出来没有高级模型，
    按该系列该类型模型的最高级进行填充勾选"——宁可留下低档的，
    也不能让凭据的白名单变成空集（空白名单 = 该账号一个模型都不提供，
    等于静默退出调度）。
    """
    kept = [m for m in models if not is_low_tier(_name(m))]
    if kept:
        return kept
    if models:
        _note("%s：过滤 mini/flash/fast 后一个不剩，已保留原列表（%d 个）"
              "以免白名单变空。" % (label, len(models)))
    return models


def _observed_generation(models, kind):
    """从实际模型列表里观测最高代。

    为什么不用写死的常量兜底：调用方（tool.sync_constants）没拿到上游源码时，
    "上游目前到第几代"这件事只有眼前的列表知道。拿列表观测值当代际门槛，
    等价于"这一批里最高的那一代就是当代"，正是"就高"的本意。
    """
    best = None
    for m in models:
        n = _name(m)
        major = claude_major(n) if kind == "anthropic" else openai_gpt_major(n)
        if major is None:
            continue
        best = major if best is None else max(best, major)
    return best


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------
def select_highest_models(models, platform, source_section=None, series=None,
                          probe=None):
    """按"就高原则"过滤模型列表。

    Args:
        models: list[dict] - CPA 的 models[]，每项含 name/alias
        platform: "gemini" / "anthropic" / "openai" / 其他
        source_section: CPA 里的来源段名，例如 "codex-api-key" /
            "openai-compatibility" / "claude-api-key"。openai 平台靠它
            区分单族（codex）与多族（openai-compatibility）。
            不传时按多族处理（更保守：不会误砍模型）。
        series: dict - 代际门槛，如 {"anthropic": 5, "openai": 6}。
            由 tool.sync_constants 注入。不传则用观测值。
        probe: dict | None - 该 (段, 域名) 的连通性探测结果，形如
            {"latest": bool, "prev": bool}，由 model_probe.probe_all 产出。
            **传 None 时行为与不带探测完全一致**（向后兼容）。

    Returns:
        list[dict] - 过滤后的模型列表
    """
    if not models or not isinstance(models, list):
        return []
    series = series or {}

    if platform == "gemini":
        picked = _select_gemini_highest(models)
    elif platform == "anthropic":
        picked = _select_claude_current(models, series)
    elif platform == "openai":
        if source_section == "codex-api-key":
            picked = _select_codex_current(models, series)
        else:
            picked = _select_openai_highest_per_family(models, series)
    else:
        # 其他平台（grok / kimi 等）保持原样
        return models

    # 需求④：最新代探测不通、但次最新代连通时，两代都保留。
    if probe is not None:
        picked = _apply_probe_fallback(models, picked, platform,
                                       source_section, series, probe)
    return picked


def _gen_of(name, platform, source_section):
    """按平台取模型代际，口径与各 _select_* 一致。"""
    if platform == "gemini":
        return gemini_version(name)
    if platform == "anthropic":
        g = claude_major(name)
        return (g, 0) if g is not None else None
    return openai_gpt_version(name)


def _apply_probe_fallback(all_models, picked, platform, source_section,
                          series, probe):
    """需求④：最新代不通 + 次最新代连通 -> 两代都留。

    需求原文："如果检测 gpt-6 系列明显不通（包括代理检测或其他办法也不通）
    但是次最新模型数据保持连通状态，这个时候按当前模型目录最高级别保留
    最新模型勾选即 gpt-6 所有模型，同时保留实测最高的次最新模型 gpt-5.6 系列"

    注意"最新代照样保留"这半句：不是把最新代换成次最新，而是**两个都留**。
    理由在需求里也写了 —— 探测可能有 BUG（"实际上能够正常调用使用"），
    所以不能仅凭一次探测就把最新代整个踢掉；留着它，真能用的时候就能用上。

    只有当探测明确说"最新不通、次最新通"时才动手。其余情况（最新通 /
    两代都不通 / 没有探测数据）一律不改 picked：
      · 最新通    -> 现有结果就是对的
      · 两代都不通 -> 由各 _select_* 的目录最高级兜底（需求④后半句）
    """
    if not probe or probe.get("latest") or not probe.get("prev"):
        return picked

    # picked 里已有的代际（正常就是最新代）
    picked_gens = set()
    for m in picked:
        g = _gen_of(_name(m), platform, source_section)
        if g is not None:
            picked_gens.add(g)
    if not picked_gens:
        return picked

    latest_gen = max(picked_gens)

    # 在全量列表里找"比最新代低的最高那一代" = 次最新代
    lower = set()
    for m in all_models:
        if not isinstance(m, dict):
            continue
        n = _name(m)
        if is_low_tier(n):
            continue            # 需求①：次最新代同样不要 mini/flash/fast
        g = _gen_of(n, platform, source_section)
        if g is not None and g < latest_gen:
            lower.add(g)
    if not lower:
        return picked

    prev_gen = max(lower)

    # gemini 的次最新代同样必须带 -pro（需求②）
    def _keep(n):
        if platform != "gemini":
            return True
        return bool(re.match(r'^gemini-\d+(?:\.\d+)?-pro(?:[.\-]|$)',
                             n, re.IGNORECASE))

    seen = {id(m) for m in picked}
    extra = [m for m in all_models
             if isinstance(m, dict) and id(m) not in seen
             and not is_low_tier(_name(m))
             and _gen_of(_name(m), platform, source_section) == prev_gen
             and _keep(_name(m))]
    if not extra:
        return picked

    _note("%s：最新代探测不通、次最新代连通，已在保留最新代的同时"
          "补入次最新代 %s（%d 个）。"
          % (source_section or platform, _fmt_gen(prev_gen), len(extra)))
    return picked + extra


def _fmt_gen(gen):
    """代际转成人看的字符串。"""
    if isinstance(gen, tuple):
        return "%d" % gen[0] if len(gen) < 2 or gen[1] == 0 \
            else "%d.%d" % (gen[0], gen[1])
    return str(gen)


# ---------------------------------------------------------------------------
# gemini
# ---------------------------------------------------------------------------
def _select_gemini_highest(models):
    """gemini：只选带 pro 的最高编号，保留该版本的全部 -pro 变体。

    变体（-preview / -customtools / -thinking）全部保留，因为它们同属一个
    "等级"，需求要求"同等级系列的模型全部都要勾选上"。

    flash 一律排除——需求写的是"必须带 pro 的最高编号模型"。
    若整个列表里一个 pro 都没有，退回"次优但同族"的选择：保留全部 gemini-*
    并在 fallback_notes 里出声。以前这里静默放行全部 flash，
    等于让"必须带 pro"这条要求在上游只提供 flash 时完全失效。
    """
    best_ver, picked = None, []
    for m in models:
        n = _name(m)
        ver = gemini_version(n)
        if ver is None:
            continue
        # 必须带 -pro 段。小数部分可选，保证 gemini-3-pro 也能匹配
        # （它是需求②原文举的"次最新模型"例子）。
        if not re.match(r'^gemini-\d+(?:\.\d+)?-pro(?:[.\-]|$)', n, re.IGNORECASE):
            continue
        if best_ver is None or ver > best_ver:
            best_ver, picked = ver, [m]
        elif ver == best_ver:
            picked.append(m)

    if picked:
        # -pro 里理论上不会出现 mini/flash/fast，仍走一遍保证口径统一
        return _drop_low_tier(picked, "gemini")

    fallback = [m for m in models if _name(m).lower().startswith("gemini-")]
    if fallback:
        _note("gemini：列表里没有任何 gemini-X.Y-pro，已回退为全部 gemini-* "
              "（%d 个），可能包含 flash。请确认该上游是否真的不提供 pro。" % len(fallback))
        return fallback
    return []


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------
def _select_claude_current(models, series):
    """claude：只选当代 claude-*，保留该代全部档位（opus/sonnet/haiku）。

    "当代"来自 series["anthropic"]；没注入就从列表里观测最高代。
    观测法的好处：上游发第 6 代时无须改代码，门槛自动前移。
    """
    gen = series.get("anthropic")
    if gen is None:
        gen = _observed_generation(models, "anthropic")
    picked = [m for m in models if claude_major(_name(m)) == gen] if gen is not None else []

    if picked:
        return picked

    fallback = [m for m in models
                if _name(m).lower().startswith("claude-") and claude_major(_name(m)) is not None]
    if fallback:
        # 取回退集合里的最高代，而不是全留：全留会让低代模型也进白名单，
        # 与"就高"直接冲突。
        best = max(claude_major(_name(m)) for m in fallback)
        picked = [m for m in fallback if claude_major(_name(m)) == best]
        _note("claude：没有代际门槛 %s 的模型，已回退为该列表里的最高代 %s。" % (gen, best))
        return picked
    return []


# ---------------------------------------------------------------------------
# codex（单族）
# ---------------------------------------------------------------------------
def _select_codex_current(models, series):
    """codex：只保留当代 gpt 系列**全部**模型。

    "全部"是需求的原话（"codex 当前最高为 gpt-6 系列所有模型名称"），
    所以同代内的 gpt-6 / gpt-6-astra / gpt-6-sol / gpt-6-terra 都要留。

    以前用 `^gpt-6` 这种字面前缀匹配，有两个问题：
      1. 会把 gpt-60 这类不属于第 6 代的模型误判进来；
      2. 门槛写死 6，上游发 7 代时新模型全被砍。
    现在按"提取主版本号 == 当代"判定，两个问题都没有。
    """
    gen = series.get("openai")
    if gen is None:
        gen = _observed_generation(models, "openai")
    picked = [m for m in models if openai_gpt_major(_name(m)) == gen] if gen is not None else []

    if picked:
        # 需求①：mini / flash / fast 这一类低档模型不勾选。
        return _drop_low_tier(picked, "codex")

    # 没有当代 gpt：退回 gpt 族里的最高版本（而不是把 o 系列之类全拉进来，
    # codex 段本来就不该出现非 gpt 模型）。
    #
    # 比较粒度是 (major, minor) 而不是只比 major：需求④原文说回退时
    # "保留实测最高的 gpt-5.6 系列"。只比 major 的话 gpt-5 与 gpt-5.6
    # 同为 5，两个会一起留下，而 gpt-5 是更旧的档次，不该进白名单。
    gpts = [m for m in models if openai_gpt_version(_name(m)) is not None
            and not is_low_tier(_name(m))]
    if gpts:
        best = max(openai_gpt_version(_name(m)) for m in gpts)
        shown = "gpt-%d" % best[0] if best[1] == 0 else "gpt-%d.%d" % best
        _note("codex：没有代际门槛 %s 的 gpt 模型，已回退为列表里的最高版本 %s。"
              % (gen, shown))
        return [m for m in gpts if openai_gpt_version(_name(m)) == best]

    # 连 gpt 都没有：把非空列表原样留下，避免把凭据饿死
    if models:
        _note("codex：列表里没有任何 gpt-* 模型，已保留原列表（%d 个）。" % len(models))
    return [m for m in models if isinstance(m, dict)]


# ---------------------------------------------------------------------------
# openai-compatibility（多族）
# ---------------------------------------------------------------------------
def _select_openai_highest_per_family(models, series):
    r"""openai：每一族各取最高级，允许多族并存。

    族（family）的划分：
      - gpt-<major>          ：gpt-6 / gpt-5 / gpt-4 各是一族
      - "gpt-<major>.<minor>"：gpt-4o 这类**字母后缀**模型
      - o<major>             ：o1 / o3 / o4 各是一族
      - other                ：无法识别的，整族保留

    关于 gpt-4o（你确认过：算 gpt-4 家族）：
      "gpt-4o" 的书写式没有点号，按 `^gpt-(\d+)` 会得到 major=4；
      "gpt-4.1" 得到 major=4, minor=1。两者同属 gpt-4 族，
      族内比 (major, minor)：4o 记作 (4,0)，4.1 记作 (4,1)，故 4.1 胜出、
      4o 被砍。**这是符合"就高"意图的正确行为**——同族只留最高代。
      以前的问题是"没有点号的模型"与"有点号的模型"比较口径不一致，
      现在统一成 (major, minor) 元组，可比且可解释。

    注意本函数**不再**靠"有没有 gpt-6"来猜 codex 场景：那个判断已经
    上移到 select_highest_models 里按 source_section 分流。

    需求①的低档过滤在**分族之前**做：`other` 族是原样保留的，
    如果留到分族之后再滤，`gemini-3.1-flash` 这类会经 other 族直接放行 ——
    实测就是这么漏网的。
    """
    models = _drop_low_tier([m for m in models if isinstance(m, dict)], "openai")

    families = {}
    for m in models:
        if not isinstance(m, dict):
            continue
        n = _name(m)
        low = n.lower()

        if low.startswith(("o1", "o3", "o4")) and re.match(r'^o\d', low):
            fam = re.match(r'^(o\d+)', low).group(1)
            ver = (int(fam[1:]), 0)
        elif gemini_version(n) is not None:
            # gemini 也要按族就高。以前它落进 other 被整族保留，于是
            # gemini-3.1-pro 和 gemini-2.5-pro 会同时进白名单，
            # 违反需求③"各自检测出来的最高级最新模型"。
            fam = "gemini"
            ver = gemini_version(n)
        elif claude_major(n) is not None:
            # 同理：claude-opus-5 与 claude-opus-4 不该同时留。
            # 同代的不同档位（opus/sonnet/haiku）靠 ver 相等而全部保留，
            # 满足需求③"相同等级系列的模型全部都要勾选上"。
            fam = "claude"
            ver = (claude_major(n), 0)
        else:
            mm = re.match(r'^gpt-(\d+)(?:\.(\d+))?', low)
            if mm:
                major = int(mm.group(1))
                minor = int(mm.group(2) or 0)
                fam = "gpt-%d" % major
                ver = (major, minor)
            else:
                fam = "other"
                ver = (0, 0)

        families.setdefault(fam, []).append((ver, m))

    result = []
    for fam, items in families.items():
        if fam == "other":
            result.extend([m for _, m in items])
            continue
        if fam == "gemini":
            # 需求②："gemini 必须带 pro 的最高编号模型"。这条在 gemini 段
            # 由 _select_gemini_highest 保证，openai 段也要一致，
            # 否则同一条规则在两个段里表现不同。
            pros = [(v, m) for v, m in items
                    if re.match(r'^gemini-\d+(?:\.\d+)?-pro(?:[.\-]|$)',
                                _name(m), re.IGNORECASE)]
            if pros:
                items = pros
            elif items:
                _note("openai：gemini 族里没有任何 -pro 模型，已保留该族全部"
                      "（%d 个）。" % len(items))
        top = max(v for v, _ in items)
        result.extend([m for v, m in items if v == top])

    if not result:
        # 一个都没识别出来：原样保留，别饿死
        result = [m for m in models if isinstance(m, dict)]
        if result:
            _note("openai：没有任何模型被识别进已知族，已保留原列表（%d 个）。" % len(result))
    return result


# ---------------------------------------------------------------------------
# 记录级封装
# ---------------------------------------------------------------------------
def apply_model_selection_to_records(records, enable_highest_only=True, series=None):
    """对 collect() 返回的记录列表就地应用模型选择。

    记录里必须带 source_section 才能正确分流 codex 与 openai；
    缺失时按多族处理（不会误砍）。
    """
    if not enable_highest_only:
        return records

    for r in records:
        platform = r.get("platform")
        models_raw = r.get("models_raw")
        if not models_raw or platform not in ("gemini", "anthropic", "openai"):
            continue
        r["models_raw"] = select_highest_models(
            models_raw, platform,
            source_section=r.get("source_section") or r.get("section"),
            series=series)
    return records


def collect_fallback_notes():
    """取走并清空本轮的回退说明。"""
    global _FALLBACK_NOTES
    notes, _FALLBACK_NOTES = _FALLBACK_NOTES, []
    return notes


def reset_fallback_notes():
    global _FALLBACK_NOTES
    _FALLBACK_NOTES = []


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    def show(title, models, platform, section=None, series=None):
        reset_fallback_notes()
        out = select_highest_models(models, platform, source_section=section, series=series)
        print("%-42s -> %s" % (title, [_name(m) for m in out]))
        for n in collect_fallback_notes():
            print("      [!] %s" % n)

    def D(*names):
        return [{"name": n, "alias": n} for n in names]

    print("== gemini ==")
    show("gemini 有 pro", D("gemini-2.5-pro", "gemini-2.5-flash",
                            "gemini-3.1-pro", "gemini-1.5-pro"), "gemini")
    show("gemini 保留同版本 pro 变体", D("gemini-3.1-pro", "gemini-3.1-pro-preview",
                                          "gemini-3.1-flash"), "gemini")
    show("gemini 全 flash（应出声回退）", D("gemini-3.5-flash", "gemini-3-flash"), "gemini")

    print("\n== claude ==")
    show("claude 注入门槛 5", D("claude-opus-4-8", "claude-opus-5",
                                "claude-sonnet-5", "claude-haiku-4-5"), "anthropic",
         series={"anthropic": 5})
    show("claude 未注入门槛（观测最高代）", D("claude-opus-4-8", "claude-sonnet-4-5",
                                                "claude-opus-5"), "anthropic")
    show("claude 全是 4 代（应回退最高代并出声）", D("claude-opus-4-8",
                                                      "claude-sonnet-4-5"), "anthropic",
         series={"anthropic": 5})

    print("\n== codex（单族）==")
    show("codex gpt-6 全系保留", D("gpt-6", "gpt-6-astra", "gpt-5.6",
                                    "gpt-5.6-sol"), "openai",
         section="codex-api-key", series={"openai": 6})
    show("codex 门槛未注入（观测）", D("gpt-7", "gpt-7-sol", "gpt-6"),
         "openai", section="codex-api-key")

    print("\n== openai（多族）==")
    show("openai 多族并存", D("gpt-6.0", "gpt-6.0-preview", "gpt-5.6",
                               "o3-mini", "o3-max"), "openai",
         section="openai-compatibility", series={"openai": 6})
    show("openai gpt-4o 与 gpt-4.1 同族", D("gpt-4o", "gpt-4o-mini",
                                             "gpt-4.1"), "openai",
         section="openai-compatibility")
    show("openai 同族同版本全留", D("gpt-5.6", "gpt-5.6-sol",
                                     "gpt-5.6-luna"), "openai",
         section="openai-compatibility")
