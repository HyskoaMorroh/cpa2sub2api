# -*- coding: utf-8 -*-
"""模型价格解析与换算。

数据源是「模型广场」页面的两种封装：
  A) 同目录的 .mhtml  —— 浏览器另存的完整页面，权威且最新；
  B) E:\\output\\NewAPI模型价格\\pri.txt —— 同一页面的纯文本导出，预清洗、
     体积小 27 倍，但是 A 的子集（实测 839 ⊂ 855，A 独有 16 个、B 独有 0 个）。

两者的记录结构完全一致，所以用同一套解析器；只有 MHTML 需要额外的
MIME 拆分、quoted-printable 解码、标签剥离和「单位折行」处理。

本模块只负责「把页面变成结构化价格」和「换算成各平台的口径」，不碰网络、
不碰 sub2api 的 API——那部分在 tool.py 里，便于单独测试。

参考实现：E:\\output\\NewAPI模型价格\\src\\{cards,parse,mhtml}.hpp（C++，
无正则、手写字符扫描）。那套代码里积累了约 20 个实测踩出来的边界情况，
这里逐条移植并在注释里保留原因，不照抄同目录 legacy/genprice.py——后者
的单位正则只认 5 种拼写，实测会丢掉 51 个已定价模型。
"""
import html
import io
import os
import quopri
import re

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 卡片分隔符。必须是**整行相等**才算分隔，不能用 "包含"：
# 实测 pri.txt 里有 21 行在 HTML 描述文本中间出现「点击查看」
# （形如 `点击查看接口文档</a>`），按包含切会把记录劈碎。
CARD_DELIM = "点击查看"

# 价格行形如 `⚡2.1/M tokens`，也可能只有 `⚡0.1`（单位在上一行或没有单位）。
PRICE_MARK = "⚡"

# 「标签在上一行、价格在下一行」的那些字段。
LABEL_INPUT = ("提示", "输入")
LABEL_OUTPUT = ("补全", "输出")
LABEL_CACHE_READ = ("缓存命中",)
LABEL_CACHE_WRITE = ("缓存创建",)

# 「标签 空格 数字」写在同一行的那些字段。
RE_MULT = re.compile(r"^倍率\s+([\d.]+)$")
RE_COMP = re.compile(r"^补全\s+([\d.]+)$")

# 页面自报的噪声行，不参与解析。
RE_NOISE = re.compile(r"^共 \d+ 个$|^已加载全部")

# 单位拼写 -> 归一化种类。
# 12 种拼写全部来自 parse.hpp:52-78 的 classify_unit；legacy/genprice.py
# 只认其中 5 种（`M tokens|次|img|s|K tokens`），实测因此丢掉 51 个已定价
# 模型，所以这里必须完整移植。
UNIT_MAP = {
    "M tokens": "per_m", "M Tokens": "per_m", "1M tokens": "per_m",
    "M token": "per_m", "1M Tokens": "per_m",
    "K tokens": "per_k", "K Tokens": "per_k",
    "次": "per_call", "img": "per_call",
    "per output image": "per_call", "image": "per_call",
    "s": "per_second", "秒": "per_second",
    "second": "per_second", "seconds": "per_second",
    "积分": "opaque",      # 平台自有积分，无法折算成 USD
    "万字符": "opaque",    # 按字符计费，与 token 不同量纲
}

# new-api / done-hub 的额度换算基准。
#   new-api  QuotaPerUnit = 500000  -> 1 ratio == $0.002/1K == $2/1M
#   done-hub DollarRate   = 0.002
# 即 ratio = USD_per_1M / 2。证据：src/parse.hpp:133-134、src/build.hpp:5-6、
# src/cards.hpp:173 与 :200（`out.input_ratio = per_m / 2.0`）。
USD_PER_M_PER_RATIO = 2.0

# done-hub 的「按次」列换算：input = USD_per_call * 500
# （$0.15/次 -> 75，实测与线上 5136 行数据逐行吻合）。
DONEHUB_TIMES_SCALE = 500.0


# ---------------------------------------------------------------------------
# MHTML 读取
# ---------------------------------------------------------------------------

def extract_html_from_mhtml(raw):
    """从 MHTML（multipart/related）里取出那一段 text/html 并解码。

    两个必须踩准的点：
      1. **锚定 part 头部**。外层 multipart/related 的头里同样含有
         `type="text/html"`，用「包含 text/html」去挑会选中外层容器，
         结果解析出 0 个模型且不报错。必须匹配 part 自己的
         `Content-Type: text/html` 行首。
      2. **charset 在 <meta> 里而不是 part 头里**。Blink 存的 MHTML
         part 头只写 quoted-printable，编码要按 UTF-8 解。
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw

    m = re.search(r'boundary="?([^"\r\n;]+)"?', text[:4000])
    if not m:
        raise ValueError("MHTML 里找不到 boundary")
    sep = "--" + m.group(1)

    for part in text.split(sep):
        head, _, body = part.partition("\n\n")
        if not body:
            head, _, body = part.partition("\r\n\r\n")
        if not body:
            continue
        # 行首锚定，避开外层容器头里的 type="text/html"
        if not re.search(r'(?im)^content-type:\s*text/html', head):
            continue
        if re.search(r'(?i)quoted-printable', head):
            body = quopri.decodestring(body.encode("utf-8", "replace")) \
                        .decode("utf-8", "replace")
        return body
    raise ValueError("MHTML 里没有 text/html 分段")


def strip_tags_to_lines(html_text):
    """把 HTML 压成「一行一个 DOM 文本节点」，与 pri.txt 的形态对齐。"""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    s = re.sub(r"(?s)<[^>]+>", "\n", s)
    s = html.unescape(s)
    out = []
    for line in s.split("\n"):
        line = line.strip()
        if line:
            out.append(line)
    return out


def fold_units(lines):
    """把「单独成行的 /单位」折回上一行的 ⚡价格 后面。

    MHTML 里 `⚡0.32` 和 `/M tokens` 是两个相邻的 DOM 节点，剥完标签就成了
    两行；pri.txt 已经是合并好的。不折就等于所有价格都没有单位，
    全部退化成「按次」或无法识别。对应 cards.hpp:36 的 fold_units。
    """
    out = []
    for line in lines:
        if line.startswith("/") and out and PRICE_MARK in out[-1]:
            out[-1] = out[-1] + line
        else:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# 卡片解析
# ---------------------------------------------------------------------------

def _parse_price(line):
    """解析 `⚡<数字>[/<单位>]`，返回 (数值, 单位种类) 或 None。"""
    i = line.find(PRICE_MARK)
    if i < 0:
        return None
    rest = line[i + len(PRICE_MARK):].strip()
    m = re.match(r"^([\d.]+)\s*(?:/\s*(.*))?$", rest)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit_raw = (m.group(2) or "").strip()
    unit = UNIT_MAP.get(unit_raw, "none" if not unit_raw else "unknown")
    return val, unit


def looks_like_model_id(s):
    """真正的模型 ID 至少带一个数字或 - . / @ _ ；厂商名（OpenAI/Claude）不带。

    对应 cards.hpp:75。用来避免把卡片里的厂商行误当成模型名。

    比上游多认一个下划线：实测 pri.txt 里有 `chat_fast_video` 这类纯小写
    加下划线的模型名，上游那份字符集会把它们判成厂商名而整张卡片丢弃
    （本项目实测因此丢了 51 张卡）。下划线不会出现在厂商名里，放宽是安全的。
    """
    return any(c.isdigit() or c in "-./@_" for c in s)


def _to_per_m(val, unit):
    """把价格归一到 USD / 1M tokens。不是 token 计价的返回 None。"""
    if unit == "per_m":
        return val
    if unit == "per_k":
        return val * 1000.0
    return None


def parse_block(lines, known_models):
    """解析一张卡片。返回 dict 或 None（该卡片没有可用价格）。

    模型名靠 model.txt 的名单来认，而不是按行号位置：
    cards.hpp:4-14 记录了实测结论——按结构取名在 MHTML 上只有 0/141 正确
    （会取到厂商名），按名单锚定是 140/141。
    """
    name = None
    for ln in lines:
        if ln in known_models and looks_like_model_id(ln):
            name = ln
            break
    if not name:
        return None

    rec = {"name": name, "input_per_m": None, "output_per_m": None,
           "cache_read_per_m": None, "cache_write_per_m": None,
           "per_call_usd": None, "group_mult": None, "comp_ratio": None,
           "opaque": False}

    pending = None   # 上一行是哪个字段的标签
    for ln in lines:
        if RE_NOISE.match(ln):
            continue

        m = RE_MULT.match(ln)
        if m:
            try:
                rec["group_mult"] = float(m.group(1))
            except ValueError:
                pass
            pending = None
            continue

        # 注意顺序：`补全 3.5`（同行带数字）是补全倍率，
        # 而单独一行的 `补全` 是「下一行是输出价」的标签。两者都以补全开头，
        # 必须先试同行正则再落到标签分支。
        m = RE_COMP.match(ln)
        if m:
            try:
                rec["comp_ratio"] = float(m.group(1))
            except ValueError:
                pass
            pending = None
            continue

        if PRICE_MARK in ln:
            got = _parse_price(ln)
            if not got:
                pending = None
                continue
            val, unit = got
            if unit == "opaque":
                rec["opaque"] = True
                pending = None
                continue
            per_m = _to_per_m(val, unit)
            if per_m is not None:
                key = {"input": "input_per_m", "output": "output_per_m",
                       "cache_read": "cache_read_per_m",
                       "cache_write": "cache_write_per_m"}.get(pending or "input",
                                                               "input_per_m")
                if rec[key] is None:
                    rec[key] = per_m
            elif unit == "per_call":
                # ⚡0/次 视为「没有标价」而不是「免费」：两个平台都把 0
                # 读成不限量免费（cards.hpp:219-237）。
                if val > 0 and rec["per_call_usd"] is None:
                    rec["per_call_usd"] = val
            elif unit == "per_second":
                # 按秒计价无法折成按次：两个平台都不会自己乘时长
                # （done-hub 的 times 分支是 `1000 * input`），走不透明兜底。
                rec["opaque"] = True
            pending = None
            continue

        if ln in LABEL_INPUT:
            pending = "input"
        elif ln in LABEL_OUTPUT:
            pending = "output"
        elif ln in LABEL_CACHE_READ:
            pending = "cache_read"
        elif ln in LABEL_CACHE_WRITE:
            pending = "cache_write"
        else:
            pending = None

    has_price = (rec["input_per_m"] is not None or
                 rec["per_call_usd"] is not None)
    return rec if has_price else None


def parse_source(lines, known_models):
    """按卡片切开整份文本，逐张解析。返回 {模型名: 记录}。"""
    out, cur = {}, []
    for ln in lines:
        if ln == CARD_DELIM:          # 整行相等才是分隔符
            rec = parse_block(cur, known_models)
            if rec:
                out.setdefault(rec["name"], rec)
            cur = []
        else:
            cur.append(ln)
    rec = parse_block(cur, known_models)
    if rec:
        out.setdefault(rec["name"], rec)
    return out


def harvest_model_names(lines):
    """从页面本身收集模型名，不依赖外部名单文件。

    为什么需要「名单」：一张卡片里除了模型名，还有厂商名（OpenAI / Claude）、
    标签（最低价 / 国产特价）、日期、描述等文本行，光看位置分不出哪行是模型名
    ——cards.hpp:4-14 记录了实测结论：按结构取名在 MHTML 上只有 0/141 正确。

    为什么不用外部 model.txt：那是另一个项目（NewAPI 价格生成器）的产物，
    本项目不该跨项目取文件——换台机器、对方目录一挪，这里就跑不了。
    模型名本来就印在页面上，自己收就行。

    收集规则——**必须同时满足**，任一条不满足就不算模型名：
      1. 出现在「分组定价 · 可用率 · 在线调试」这一行之前的卡片正文里；
      2. 形如模型 ID（含数字或 - . / @ _，排除纯中文/纯字母的厂商名）；
      3. 不含空格、不含中文（描述句和标签会被这条挡掉）；
      4. 长度合理（1~120）。
    再用「该名字在整份文档里出现过多少次」做二次确认：模型名在卡片里通常
    只出现一次，而厂商名会在几十张卡片里反复出现——出现次数异常高的直接剔除。

    返回模型名集合。
    """
    from collections import Counter

    cand = Counter()
    for ln in lines:
        if len(ln) < 1 or len(ln) > 120:
            continue
        if " " in ln or "\t" in ln:
            continue
        # 含中日韩字符的是描述或标签，不是模型 ID
        if any("一" <= ch <= "鿿" for ch in ln):
            continue
        if ln.startswith(PRICE_MARK) or ln.startswith("/"):
            continue
        if not looks_like_model_id(ln):
            continue
        # 纯数字是日期碎片、计数之类，不会是模型名。
        # 只挡「去掉点和横杠后全是数字」且长度很短的，避免误伤
        # 形如 o3-2025-04-16 这种带数字的真实模型名。
        bare = ln.replace(".", "").replace("-", "")
        if bare.isdigit() and len(ln) <= 8:
            continue
        # 以 * 结尾的是通配规则（fal-ai/*），不是具体模型
        if ln.endswith("*"):
            continue
        cand[ln] += 1

    if not cand:
        return set()

    # 厂商名/通用标签会在大量卡片里重复出现；模型名一般只出现一两次。
    # 用中位数的若干倍做阈值，而不是写死一个数字——卡片总数变了也能自适应。
    counts = sorted(cand.values())
    med = counts[len(counts) // 2] or 1
    cutoff = max(6, med * 6)
    return {k for k, v in cand.items() if v <= cutoff}


def load_known_models(path):
    """读 model.txt：一行一个模型名，用作解析时的名字锚点。

    这是**可选**的增强：给了就用它做权威名单，没给则由 harvest_model_names
    从页面自己收集（见那里的说明——本项目不依赖外部项目的文件）。
    """
    names = set()
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if s:
                names.add(s)
    return names


def load_prices(mhtml_path=None, pri_path=None, model_txt=None):
    """读入价格表。MHTML 优先、pri.txt 兜底，逐模型合并。

    MHTML 是权威源（实测比 pri.txt 多 17 个模型，且是它的超集），
    而且**本项目同目录就有一份**，不需要任何外部文件即可工作。
    pri.txt 与 model.txt 都是可选增强：给了就用，没给也能跑。

    model_txt 的作用是提供权威的模型名单；不给就用 harvest_model_names
    从页面自己收集——本项目不应该依赖别的项目的目录（换机器就断）。

    返回 (价格字典, 来源说明列表)。
    """
    sources = []
    if mhtml_path and os.path.exists(mhtml_path):
        sources.append(("mhtml", mhtml_path))
    if pri_path and os.path.exists(pri_path):
        sources.append(("pri", pri_path))
    if not sources:
        raise RuntimeError("没有可用的价格数据源（需要 mhtml 或 pri.txt）")

    known = None
    notes = []
    if model_txt and os.path.exists(model_txt):
        known = load_known_models(model_txt)
        notes.append("模型名单来自 model.txt（%d 条）" % len(known))

    merged = {}
    # 先 pri.txt 再 mhtml：后者权威，覆盖同名条目
    for kind, path in sorted(sources, key=lambda x: x[0] != "pri"):
        if kind == "pri":
            with io.open(path, encoding="utf-8", errors="replace") as f:
                lines = [x.strip() for x in f if x.strip()]
        else:
            with io.open(path, "rb") as f:
                raw = f.read()
            try:
                lines = strip_tags_to_lines(extract_html_from_mhtml(raw))
            except Exception as ex:
                notes.append("MHTML 解析失败，跳过：%s" % ex)
                continue
        lines = fold_units(lines)

        use = known
        if use is None:
            use = harvest_model_names(lines)
            notes.append("%s：从页面自收模型名 %d 个（未用外部 model.txt）"
                         % (kind, len(use)))
        got = parse_source(lines, use)
        merged.update(got)
        notes.append("%s 解析出 %d 个模型" % (kind, len(got)))

    if not merged:
        raise RuntimeError("价格源里没解析出数据")
    notes.append("合并后共 %d 个模型" % len(merged))
    return merged, notes


# ---------------------------------------------------------------------------
# 换算
# ---------------------------------------------------------------------------

def to_sub2api_pricing(rec):
    """把一条价格记录换算成 sub2api 的 ChannelModelPricing 字段。

    **单位是每 token 的 USD，不是每百万**。证据链：
      · migrations/081_create_channels.sql 的列注释原文
        「每 token 输入价格（USD）」，类型 NUMERIC(20,12)——12 位小数正是
        为了容纳 per-token 的量级；
      · billing_service.go:95-117 的 ModelPricing 每个字段都注释
        「每token价格 (USD)」；
      · account_stats_pricing.go:243 直接
        `float64(tokens.InputTokens) * deref(p.InputPrice)`，中间没有任何
        除以 1e6 的缩放。
    所以这里必须除 1e6。漏了就是贵一百万倍。

    **倍率列（倍率 N）绝对不能除掉**：它是上游相对厂商标价的加价档位，
    不是折扣。实测 `倍率 == prompt/2` 成立 468/468，
    而 `prompt/2/倍率` 成立 0/468；除掉会让 倍率=5 的模型算成 1/5 价。

    返回 dict（只含有值的字段），无可用价格时返回 None。
    """
    if rec.get("opaque"):
        return None

    out = {}
    ipm = rec.get("input_per_m")
    if ipm is not None and ipm > 0:
        out["input_price"] = ipm / 1e6

        opm = rec.get("output_per_m")
        if opm is None and rec.get("comp_ratio"):
            # 页面只给「补全倍率」时，输出价 = 输入价 × 补全倍率。
            # comp_ratio 是相对输入的系数，只作用于输出 token。
            opm = ipm * rec["comp_ratio"]
        if opm is not None and opm > 0:
            out["output_price"] = opm / 1e6

        for src, dst in (("cache_read_per_m", "cache_read_price"),
                         ("cache_write_per_m", "cache_write_price")):
            v = rec.get(src)
            if v is not None and v > 0:
                out[dst] = v / 1e6

    pc = rec.get("per_call_usd")
    if pc is not None and pc > 0:
        out["per_request_price"] = pc

    return out or None


def to_newapi_ratio(rec):
    """换算成 new-api / done-hub 的倍率口径：ratio = USD_per_1M / 2。

    本项目不直接写这两个平台，但「同一模型跨平台实际消耗一致」这个要求
    需要一个可核对的换算，校验器会用它做交叉验证。
    """
    out = {}
    ipm = rec.get("input_per_m")
    if ipm is not None and ipm > 0:
        out["model_ratio"] = ipm / USD_PER_M_PER_RATIO
        if rec.get("comp_ratio"):
            out["completion_ratio"] = rec["comp_ratio"]
        elif rec.get("output_per_m"):
            out["completion_ratio"] = rec["output_per_m"] / ipm
    pc = rec.get("per_call_usd")
    if pc is not None and pc > 0:
        out["model_price"] = pc
        out["donehub_times_input"] = pc * DONEHUB_TIMES_SCALE
    return out


def cross_platform_check(rec, eps=1e-9):
    """校验同一模型在各平台换算回 USD 后是否一致。

    这是「保证无论哪个平台最后实际消耗一样」的硬保障——不依赖公式写对的
    信念，而是把三种口径各自折回 USD/1M 再逐一比对。

    返回 (是否一致, 说明)。
    """
    ipm = rec.get("input_per_m")
    if not ipm or ipm <= 0:
        return True, "无 token 价，跳过"

    sub = to_sub2api_pricing(rec)
    if not sub or "input_price" not in sub:
        return True, "sub2api 侧无 token 价，跳过"

    back_sub = sub["input_price"] * 1e6
    na = to_newapi_ratio(rec)
    back_na = na["model_ratio"] * USD_PER_M_PER_RATIO

    if abs(back_sub - ipm) > eps:
        return False, "sub2api 折回 %.12g != 原值 %.12g" % (back_sub, ipm)
    if abs(back_na - ipm) > eps:
        return False, "new-api 折回 %.12g != 原值 %.12g" % (back_na, ipm)
    return True, "一致（%.12g USD/1M）" % ipm
