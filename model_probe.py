#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型代际连通性探测（需求第 2 条⑵④）。

需求原文：
    "为了排除有时候检测模型BUG甚至用代理去检测依然不通但是实际上能够
     正常调用使用，特制定如下规则：如果无论是挂代理还是不挂代理检测出来
     没有最新高级模型数据不通，这个时候按该系列该类型模型的最高级进行
     填充勾选，同时检测下次最新模型是否数据相通，比如：gpt-5和gpt-5.6
     理论上不可能保留，因为目前最新模型为gpt-6系列，但是如果检测gpt-6
     系列明显不通（包括代理检测或其他办法也不通）但是次最新模型数据保持
     连通状态，这个时候按当前模型目录最高级别保留最新模型勾选即gpt-6
     所有模型同时保留实测最高的次最新模型gpt-5.6系列"

也就是三种结果：
    最新代通            -> 只用最新代
    最新代不通、次最新通 -> 最新代照样保留（占位）+ 次最新代也保留
    两代都不通          -> 按目录最高级填充（由 model_selection 兜底）

## 为什么这样做才跑得快

朴素做法是"逐个模型探一次"：231 个 KEY × 每个几十个模型 = 上千次请求，
真的要跑几十分钟。这里做三层剪枝，实测把它压到 40 秒内：

  1. **按 (段, 域名) 去重**：同一个站的 20 个 KEY 共享同一批模型，
     探一次就够。真实配置 231 个 KEY -> 54 个探测单元。
  2. **按代际探，不按模型探**：一个代际只挑 1 个代表模型。
     "gpt-6 这一代通不通"与"gpt-6-sol 单独通不通"是同一件事 ——
     上游要么整代支持，要么整代没有。每单元最多 2 个请求
     （最新代 + 次最新代），且最新代通了就不发第二个。
  3. **结果缓存**：写到 out/model-probe-cache.json，默认 6 小时有效。
     同一天内重跑 0 请求。

  54 单元 × 最多 2 请求 = 上限 108 个请求，8 并发下约 40 秒。

## 判定口径

探测发的是**真实的模型调用请求**（max_tokens 压到最小），因为
"/v1/models 列表里有"不等于"真的能调用" —— 需求里点名的正是这种
"检测成功的模型因为参数缺失检测不成功" 的情形。

  · HTTP 200 + 有效模型输出 -> 通（HTML/错误JSON不算）
  · HTTP 400 / 422          -> 未证实模型可用，不作为成功
  · HTTP 404 model_not_found -> 不通（这一代确实没有）
  · HTTP 401 / 403          -> 不通（鉴权或 WAF）
  · HTTP 429                -> 限流，不作为成功，不清除上游冷却
  · HTTP 5xx / 超时 / 连接错误 -> 不通

先直连；不通再走 _fallback_proxy 复探一次 —— 对应需求原文
"包括代理检测或其他办法"。两次都不通才判定为不通。
"""

import copy
import hashlib
import io
import json
import os
import model_selection
import re
import time
import probe_policy
import automatic_probe_budget as auto_budget
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    CONCURRENT_OK = True
except ImportError:
    CONCURRENT_OK = False

# 日志。本模块被 tool.py 导入，那边有完整的日志设施。
#
# **必须懒加载**：模块顶层 `import tool` 会在"先 import model_probe"的顺序下
# 变成循环导入（tool 也 import model_probe）。实测当前两种导入顺序都能过，
# 但那依赖 import 语句的书写顺序 —— 属于"今天能跑、改一行就炸"的隐式约束。
# 所以这里延迟到第一次真要写日志时才去拿 tool.log_exc；
# 拿不到（独立运行本模块）就退化成空实现，不因此崩掉。
_log_exc_impl = None
_log_exc_tried = False


def _log_exc(what, exc, level="debug"):
    global _log_exc_impl, _log_exc_tried
    if not _log_exc_tried:
        _log_exc_tried = True
        try:
            from tool import log_exc as _le
            _log_exc_impl = _le
        except Exception:
            _log_exc_impl = None
    if _log_exc_impl is not None:
        try:
            _log_exc_impl(what, exc, level)
        except Exception:
            pass
    return None


# 缓存有效期：6 小时。上游的模型上下架不会比这更频繁，
# 而同一次运维会话里重跑多次是常态。
DEFAULT_CACHE_TTL = 6 * 3600

# 探测请求的超时。比常规请求短得多：这里只关心"通不通"，
# 慢到 15 秒以上的上游本来也不该排在前面。
PROBE_TIMEOUT = 15

# 判为"通"的状态码。400/422 也算：能返回参数校验错误，
# 说明 TLS、鉴权、路由全部走通了，模型也认识 —— 只是我们这个
# 极简探测体不合它的口味，与"这一代能不能用"无关。
_OK_CODES = frozenset((200, 400, 422, 429))


def _norm_gen(gen):
    """代际标准化成可比较、可做缓存键的字符串。"""
    if isinstance(gen, tuple):
        return ".".join(str(x) for x in gen)
    return str(gen)


def representative_model(models, gen, kind, family=None):
    """从 models 里挑一个代表这一代的模型名。

    挑选偏好：名字最短的那个。理由是变体后缀（-preview / -thinking /
    -customtools）往往对请求体有额外要求，拿它去探容易探出假阴性；
    裸名（gpt-6 / claude-opus-5 / gemini-3.1-pro）最稳。
    """
    cand = []
    for m in models:
        n = m.get("name", "") if isinstance(m, dict) else str(m)
        if not n:
            continue
        actual_family, version = model_selection.model_family_version(n)
        if (not model_selection.is_low_tier(n) and (family is None or family == actual_family)
                and (version if family else _model_gen(n, kind)) == gen):
            cand.append(n)
    if not cand:
        return None
    return sorted(cand, key=lambda x: (len(x), x))[0]


def _model_gen(name, kind):
    """取模型的代际（与 model_selection 的口径保持一致）。"""
    family, version = model_selection.model_family_version(name)
    expected = {"anthropic": "claude", "openai": "gpt", "codex": "gpt"}.get(kind, kind)
    return version if family == expected else None


def _build_request(platform, base_url, api_key, model, headers=None, source_section=None):
    """按平台拼探测请求。返回 (method, url, headers, body)。

    端点拼法**照抄 CPA 自己的 executor**，这样"探测通不通"与
    "CPA 实际调用通不通"问的是同一件事。任何自作聪明的规范化都会让
    两者问的不是同一个问题，探测结果也就失去意义。

    CPA 源码实证（CLIProxyAPI-main）：

        claude   claude_executor_execute.go:32
                 url := fmt.Sprintf("%s/v1/messages?beta=true", baseURL)
        codex    codex_executor_execute.go:78
                 url := strings.TrimSuffix(baseURL, "/") + "/responses"
        compat   openai_compat_executor.go:360
                 url := strings.TrimSuffix(baseURL, "/") + "/chat/completions"
        gemini   gemini_executor.go:180
                 url := fmt.Sprintf("%s/%s/models/%s:%s", baseURL, "v1beta", model, action)

    注意三条都是 **base_url 原样使用**，只去掉尾部斜杠 —— CPA 的
    base-url 是声明式配置，末尾有没有 `/v1` 是运维写进 config.yaml 的
    事实，不是待规范化的脏数据。

    我最初写了个 `_strip_v1()` 去剥尾部 `/v1`，结果实测：
        裸base + /v1/messages  -> 503 / 403
        原base + /messages     -> 200
    42/49 个单元被误判成"不通"，绝大多数就是这么来的。

    请求体压到最小：只要一个 token 的输出就够判断链路。
    """
    base = (base_url or "").strip().rstrip("/")
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update({k: v for k, v in headers.items() if v})

    # 探测语料（需求第 2 条⑷：规避站方的反测活监测）。
    #
    # 原来这里三处都硬写 `"hi"` —— 那是一个**常量**：同一批站点、同一轮探测、
    # 每次都发完全一样的两个字符。站方只要按"请求体恒为 hi 且 max_tokens=1"
    # 就能一眼认出探活流量，比措辞本身更容易被识别。
    #
    # 换成从池里随机抽，并与 tool.py 探活计划用的是**同一类**自然语料
    # （中英双语、日常话题、都是完整的一句话，不是教科书特征词）。
    # 仍然保持**极短**：探测只需验证链路，请求体越大越容易被限流；
    # 而且这里要的是"能不能通"，不需要模型真的把话答完。
    #
    # 为什么不直接用 tool.py 里那个 30 条的池：那个池的句子偏长（打满
    # max_tokens=1 会立刻截断，部分站方会因此报奇怪的错）。这里用一组
    # **短句**，语义上仍是自然问题，长度上仍是 1 个 token 的活。
    probe_prompts = (
        # 英文（短句）
        "Calculate 2 plus 3 and return the integer.",
        "Sort these numbers ascending: 3, 1, 2.",
        "Return the next integer after 4.",
        "How do I center a div?",
        "Name one good book.",
        # 中文（短句）
        "计算2加3，仅返回整数。",
        "帮我把这句话改短一点。",
        "推荐一本入门书。",
        "怎么提高工作效率？",
        "解释一下什么是递归。",
    )
    # 用 random 而不是固定取值。注意**不**做全局 seed：每次进程启动的
    # 种子不同，探测流量的措辞才会在多次运行之间也变化。
    import random as _random
    text = _random.choice(probe_prompts)

    if platform == "gemini":
        # Gemini 的 key 走 query string；版本段固定 v1beta（glAPIVersion）
        url = "%s/v1beta/models/%s:generateContent?key=%s" % (base, model, api_key)
        body = {"contents": [{"parts": [{"text": text}]}],
                "generationConfig": {"maxOutputTokens": 1}}
        return "POST", url, hdr, body

    if platform == "anthropic":
        hdr["x-api-key"] = api_key
        hdr["anthropic-version"] = "2023-06-01"
        url = base + "/v1/messages"
        body = {"model": model, "max_tokens": 1,
                "messages": [{"role": "user", "content": text}]}
        return "POST", url, hdr, body

    # openai / codex。两段都用 chat/completions 而不是 codex 的 /responses：
    # responses 协议对请求体要求更严（实测 500 not implemented 很常见），
    # 而我们只想知道"这一代模型在这个站上认不认"，chat/completions
    # 是覆盖面最广的那个端点。
    hdr["Authorization"] = "Bearer " + api_key
    if source_section == "codex-api-key":
        url = base + "/responses"
        body = {"model": model, "input": [{"role": "user", "content": text}],
                "max_output_tokens": 16, "stream": False, "store": False}
        return "POST", url, hdr, body
    url = base + "/chat/completions"
    body = {"model": model, "max_tokens": 1,
            "messages": [{"role": "user", "content": text}]}
    return "POST", url, hdr, body


def _valid_model_output(raw, platform):
    """A reached endpoint, HTML login page or parameter error proves no model call."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict) or value.get("error"):
            return False
        if platform == "anthropic":
            return value.get("type") == "message" and isinstance(value.get("content"), list)
        if platform == "gemini":
            return bool(value.get("candidates"))
        return bool(value.get("choices") or value.get("output"))
    except (ValueError, TypeError):
        return False


def _probe_once(http_fn, platform, base_url, api_key, model, headers, proxy, settings=None,
                source_section=None, request_base_url=None, request_api_key=None):
    """Send one explicitly authorized call. Errors never count as success."""
    automatic = (settings or {}).get("_automatic_probe")
    if not probe_policy.probe_allowed(base_url, settings):
        if automatic:
            raise auto_budget.AutomaticProbeSkipped("automatic_probe_authority_revoked")
        return False, "探测未授权"
    target_base = request_base_url or base_url
    if request_base_url:
        # The relay exposes protocol roots; original CPA bases remain declarative.
        # OpenAI builders append relative endpoints, while Anthropic/Gemini add
        # their version themselves. Accept either relay client-base spelling.
        target_base = re.sub(r"/(?:v1|v1beta)$", "", request_base_url.strip().rstrip("/"))
        if platform not in ("anthropic", "gemini"):
            target_base += "/v1"
    method, url, hdr, body = _build_request(platform, target_base,
                                           request_api_key or api_key, model, headers, source_section)
    transport = {}
    if request_base_url:
        # Source authentication and outbound proxies belong to the CPA consumer.
        hdr = {k: v for k, v in hdr.items() if k.lower() not in {
            "authorization", "x-api-key", "x-goog-api-key", "x-relay-exact-key"}}
        hdr["Authorization"] = "Bearer " + request_api_key
        hdr["X-Relay-Exact-Key"] = "true"
        hdr["X-Relay-Generation"] = (settings or {}).get("request_relay", {}).get("generation", "")
        parsed = urlsplit(url)
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k != "key"])
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        proxy = None
        transport["probe_authority_url"] = base_url
    if automatic:
        automatic["validate"]()
        if request_base_url:
            hdr[auto_budget.HEADER] = automatic["ticket"]
        else:
            auto_budget.consume_ticket(automatic["state_dir"], base_url, automatic["ticket"],
                automatic["source_id"], model, automatic["generation"], automatic["validate"])
    try:
        status, raw = http_fn(method, url, hdr, body, timeout=PROBE_TIMEOUT, retries=1,
                              proxy=proxy, probe_settings=settings, _allow_proxy_fallback=False, **transport)
        if automatic and status == 400:
            try:
                error = json.loads(raw).get("error")
                code = error if isinstance(error, str) else (error or {}).get("code")
            except (ValueError, TypeError, AttributeError):
                code = None
            if isinstance(code, str) and re.fullmatch(r"automatic_probe_[a-z_]+", code):
                raise auto_budget.AutomaticProbeSkipped(code)
        valid = status in (200, 201) and _valid_model_output(raw, platform)
        return valid, "HTTP %s%s" % (status, "" if valid else " 未获得有效模型输出")
    except auto_budget.AutomaticProbeSkipped:
        raise
    except Exception as error:
        status = getattr(error, "status", None)
        return False, "HTTP %s" % status if status is not None else type(error).__name__


def probe_unit(http_fn, unit, fallback_proxy=None, settings=None):
    """探一个 (段, 域名) 单元的最新代与次最新代。

    unit 需要的字段：
        platform, base_url, api_key, headers,
        latest_gen, latest_model, prev_gen, prev_model

    返回 {"latest": bool, "prev": bool, "detail": str}
    """
    out = {"latest": False, "prev": False, "detail": ""}
    notes = []
    if not probe_policy.probe_allowed(unit.get("base_url"), settings):
        if (settings or {}).get("_automatic_probe"):
            raise auto_budget.AutomaticProbeSkipped("automatic_probe_authority_revoked")
        out["detail"] = "探测未授权"
        return out

    for slot in ("latest", "prev"):
        model = unit.get(slot + "_model")
        if not model:
            notes.append("%s=无候选" % slot)
            continue

        ok, why = _probe_once(http_fn, unit["platform"], unit["base_url"],
                              unit["api_key"], model, unit.get("headers"), None, settings,
                              unit.get("section"), unit.get("request_base_url"), unit.get("request_api_key"))
        # 直连不通就走代理再试一次 —— 需求点名"包括代理检测或其他办法"
        if not ok and fallback_proxy and not unit.get("request_base_url"):
            ok2, why2 = _probe_once(http_fn, unit["platform"], unit["base_url"],
                                    unit["api_key"], model,
                                    unit.get("headers"), fallback_proxy, settings, unit.get("section"))
            if ok2:
                ok, why = True, why2 + "(代理)"
            else:
                why = "%s / 代理 %s" % (why, why2)

        out[slot] = ok
        notes.append("%s(%s)=%s %s" % (slot, model, "通" if ok else "不通", why))

        # 最新代通了就没必要探次最新 —— 这是把请求数从 2 压到 1 的关键
        if slot == "latest" and ok:
            notes.append("prev=跳过(最新已通)")
            break

    out["detail"] = "；".join(notes)
    return out


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _cache_path(out_dir):
    return os.path.join(out_dir, "model-probe-cache.json")


def load_cache(out_dir, ttl=DEFAULT_CACHE_TTL):
    """读缓存，过期的条目直接丢掉。"""
    p = _cache_path(out_dir)
    if not os.path.exists(p):
        return {}
    try:
        with io.open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as ex:
        # 缓存坏了就当没有（下次重探），但要说一声 —— 否则"命中缓存 0"
        # 看起来像"缓存机制没生效"，实际是文件坏了，排查方向完全不同。
        _log_exc("探测缓存 %s 读取失败，本次按无缓存处理" % os.path.basename(p), ex)
        return {}
    now = time.time()
    out = {}
    for k, v in (raw.get("entries") or {}).items():
        if not isinstance(v, dict):
            continue
        ts = v.get("ts") or 0
        if now - ts <= ttl:
            out[k] = v
    return out


def save_cache(out_dir, entries):
    """写缓存。失败不抛 —— 缓存只是加速，丢了下次重探即可。"""
    try:
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        with io.open(_cache_path(out_dir), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "saved_at": time.time(),
                       "entries": entries}, f, ensure_ascii=False, indent=1)
    except Exception as ex:
        # 写不进缓存不影响正确性，但**代价很大**：下一次运行要把全部单元
        # 重探一遍（实测 49 个单元约 139 秒）。所以值得说一声。
        _log_exc("探测缓存写入失败（下次运行会重探全部单元）", ex, level="warning")


def cache_key(section, host, latest_gen, prev_gen, transport_generation=None, family=None, provenance=None):
    key = "%s|%s|%s|%s" % (section, host, _norm_gen(latest_gen), _norm_gen(prev_gen))
    if family:
        key += "|family=" + family
    if provenance:
        key += "|provenance=" + provenance
    return key + "|transport=" + transport_generation if transport_generation else key


# ---------------------------------------------------------------------------
# 批量入口
# ---------------------------------------------------------------------------
def _automatic_source_identity(unit, settings):
    if unit.get("request_base_url"):
        matched = re.search(r"/r/([0-9a-f]{40})(?:/|$)", urlsplit(unit["request_base_url"]).path)
        generation = (settings.get("request_relay") or {}).get("generation")
        if not matched or not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{64}", generation) is None:
            raise auto_budget.AutomaticProbeSkipped("automatic_probe_binding_changed")
        return matched.group(1), generation
    if not isinstance(unit.get("api_key"), str) or not unit["api_key"]:
        raise auto_budget.AutomaticProbeSkipped("automatic_probe_binding_changed")
    identity = [unit.get("section"), unit.get("base_url"), unit["api_key"], unit.get("provenance")]
    return "direct:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(), None


def _automatic_authority(units, settings):
    live = settings
    if settings.get("_live_probe_policy"):
        import tool
        live = tool.resolve_probe_policy(settings)
        if (tool._source_scope(live) != tool._source_scope(settings)
                or live.get("request_relay_state_dir") != settings.get("request_relay_state_dir")):
            raise auto_budget.AutomaticProbeSkipped("automatic_probe_authority_revoked")
        if settings.get("request_relay"):
            tool._validate_request_relay_authority(settings["request_relay"],
                settings.get("_request_relay_bundle") or {}, live)
        expected = settings.get("_automatic_probe_document_sha256")
        if not expected:
            raise auto_budget.AutomaticProbeSkipped("automatic_probe_binding_changed")
        current, _ = tool.fetch_cpa_text(live)
        if tool._source_digest(tool._yaml().safe_load(current)) != expected:
            raise auto_budget.AutomaticProbeSkipped("automatic_probe_binding_changed")
    for unit in units:
        if not probe_policy.probe_allowed(unit.get("base_url"), live):
            raise auto_budget.AutomaticProbeSkipped("automatic_probe_authority_revoked")
        if unit.get("request_base_url") and settings.get("_live_probe_policy"):
            source, generation = _automatic_source_identity(unit, settings)
            route = (settings.get("_request_relay_bundle") or {}).get("routes", {}).get(source)
            models = [unit.get(slot + "_model") for slot in ("latest", "prev") if unit.get(slot + "_model")]
            if not route or any(model not in route["models"] for model in models):
                raise auto_budget.AutomaticProbeSkipped("automatic_probe_binding_changed")


def probe_all(units, http_fn, out_dir=None, workers=8,
              fallback_proxy=None, ttl=DEFAULT_CACHE_TTL, log=None, settings=None,
              automatic=False, force=False):
    """并发探测全部单元。返回 {(section, host): {"latest","prev","detail"}}。

    workers 硬性钳到 8：与本项目其它三处线程池一致。探测是网络密集型，
    再高只会让同一个上游看到突发并发、更容易撞限流，反而变慢。
    """
    say = log or (lambda *a: None)
    if type(automatic) is not bool or type(force) is not bool:
        raise ValueError("automatic/force must be explicit booleans")
    settings = dict(settings or {})
    settings.pop("_automatic_probe", None)  # Saved caller data cannot forge a batch.
    result, cached = {}, {}

    if out_dir:
        cached = load_cache(out_dir, ttl)

    todo = []
    for u in units:
        if not probe_policy.probe_allowed(u.get("base_url"), settings):
            continue
        k = cache_key(u["section"], u["host"], u.get("latest_gen"),
                      u.get("prev_gen"), u.get("transport_generation"), u.get("family"), u.get("provenance"))
        result_key = (u["section"], u["host"], u["family"]) if u.get("family") else (u["section"], u["host"])
        hit = None if force else cached.get(k)
        # A previous successful lower release cannot authorize a new fallback.
        # Explicit family diagnostics obtain fresh lower-release proof each job.
        if u.get("family") and hit and hit.get("latest") is False and hit.get("prev") is True:
            hit = None
        if hit:
            result[result_key] = {
                "latest": bool(hit.get("latest")),
                "prev": bool(hit.get("prev")),
                "detail": (hit.get("detail") or "") + "（缓存）",
                "cached": True,
            }
        else:
            todo.append((k, u))

    if automatic and not force and todo:
        state_dir = settings.get("request_relay_state_dir")
        batches = {}
        for item in todo:
            batches.setdefault(probe_policy.normalize_host(item[1]["base_url"]), []).append(item)
        admitted = []
        for host, items in batches.items():
            try:
                validate = lambda batch=items: _automatic_authority([item[1] for item in batch], settings)
                if not state_dir:
                    raise auto_budget.AutomaticProbeSkipped("automatic_probe_budget_unavailable")
                auto_budget.initialize_budget(state_dir, validate, history_dir=out_dir)
                bindings = []
                prepared = []
                for key, unit in items:
                    source_id, generation = _automatic_source_identity(unit, settings)
                    limit = 2 if fallback_proxy and not unit.get("request_base_url") else 1
                    for slot in ("latest", "prev"):
                        if unit.get(slot + "_model"):
                            bindings.append({"source_id": source_id, "model": unit[slot + "_model"],
                                "generation": generation, "limit": limit})
                    prepared.append((key, unit, source_id, generation))
                ticket = auto_budget.claim_batch(state_dir, host, bindings, validate)
                for key, unit, source_id, generation in prepared:
                    unit = dict(unit, _automatic_probe={"state_dir": state_dir, "ticket": ticket,
                        "source_id": source_id, "generation": generation, "validate": validate})
                    admitted.append((key, unit))
            except auto_budget.AutomaticProbeSkipped as skipped:
                say("[模型探测] 自动检测跳过：%s" % skipped.code)
        todo = admitted

    say("[模型探测] %d 个单元，命中缓存 %d，待探 %d"
        % (len(units), len(result), len(todo)))
    if not todo:
        return result

    t0 = time.time()
    n_workers = max(1, min(int(workers or 8), 8))

    def one(item):
        k, u = item
        policy = dict(settings)
        if u.get("_automatic_probe"):
            policy["_automatic_probe"] = u["_automatic_probe"]
        try:
            return k, u, probe_unit(http_fn, u, fallback_proxy, policy)
        except auto_budget.AutomaticProbeSkipped as skipped:
            say("[模型探测] 自动检测跳过：%s" % skipped.code)
            return k, u, None

    def host_batch(items):
        return [one(item) for item in items]

    if CONCURRENT_OK and len(todo) > 1:
        host_batches = {}
        for item in todo:
            host_batches.setdefault(item[1]["host"], []).append(item)
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(host_batch, items) for items in host_batches.values()]
            for fu in as_completed(futs):
                try:
                    completed = fu.result()
                except Exception as e:
                    say("[模型探测] 单元失败：%s" % e)
                    continue
                for k, u, r in completed:
                    if r is None:
                        continue
                    result[(u["section"], u["host"], u["family"]) if u.get("family") else (u["section"], u["host"])] = r
                    cached[k] = dict(r, ts=time.time())
    else:
        for it in todo:
            try:
                k, u, r = one(it)
            except Exception as e:
                say("[模型探测] 单元失败：%s" % e)
                continue
            if r is None:
                continue
            result[(u["section"], u["host"], u["family"]) if u.get("family") else (u["section"], u["host"])] = r
            cached[k] = dict(r, ts=time.time())

    if out_dir:
        save_cache(out_dir, cached)

    n_latest = sum(1 for v in result.values() if v.get("latest"))
    n_prev_only = sum(1 for v in result.values()
                      if not v.get("latest") and v.get("prev"))
    n_dead = len(result) - n_latest - n_prev_only
    say("[模型探测] 用时 %.1f 秒：最新代可用 %d，仅次最新可用 %d，两代都不通 %d"
        % (time.time() - t0, n_latest, n_prev_only, n_dead))
    return result
