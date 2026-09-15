# -*- coding: utf-8 -*-
"""CPA -> sub2api 迁移工具

用法：双击同目录的『一键导入.cmd』，全自动跑完；或 `python tool.py` 进菜单。
数据源：同目录的 config.yaml（自动读取）。
目标  ：sub2api，地址和管理密钥写在 设置.json。

依赖：Python 3 + PyYAML（cmd 会自动装）。其余全用标准库。
"""
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

# 并发执行器。**必须在这里全局导入**，不能只在某个函数内部 import ——
# 这个文件里有三处用到线程池：
#   · 批量建号（import_batch）
#   · 探活计划创建（ensure_test_plans）
#   · 探活结果读取（revive_proven_inactive）
# 原先只有第一处在函数内部 import，后两处直接用了这两个名字，
# 一旦走到并发分支就会 NameError。这个错误 py_compile 查不出来
# （语法合法），只在运行且并发分支被命中时炸——由 CI 的 flake8 F821 抓到。
try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    CONCURRENT_AVAILABLE = True
except ImportError:
    # 理论上 CPython 3.2+ 都有；保留降级路径是为了不让"缺了这个模块"
    # 直接变成启动失败，而是退化成串行（各调用点都判这个标志）。
    CONCURRENT_AVAILABLE = False

# 导入智能优先级模块
try:
    from new_remap_priority import remap_priority_smart
    SMART_PRIORITY_AVAILABLE = True
except ImportError:
    SMART_PRIORITY_AVAILABLE = False
    print("警告: new_remap_priority.py 未找到，使用传统优先级映射")

# 导入模型选择模块（就高原则）
#
# 必须先把自己所在目录放进 sys.path。以前这里只靠"隐含的 cwd/sys.path[0]"
# （正常入口 run.py 会 os.chdir(HERE)，所以本机跑没问题），但换个工作目录就
# ——比如容器里换了 WORKDIR、被别的脚本 import、计划任务拉起——会静默
# ImportError，MODEL_SELECTION_AVAILABLE 变 False，**就高原则整体失效**，
# 只打印一行"跳过模型就高原则过滤"就继续跑。那是最难发现的一类失效：
# 工具看起来正常，白名单里却全是没过滤的模型。
_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERE_DIR not in sys.path:
    sys.path.insert(0, _HERE_DIR)

try:
    from model_selection import select_highest_models
    MODEL_SELECTION_AVAILABLE = True
except ImportError:
    MODEL_SELECTION_AVAILABLE = False
    print("警告: model_selection.py 未找到，跳过模型就高原则过滤")

TOOL_VERSION = "3.0"

# 默认 User-Agent。见 _http 的说明：Cloudflare 的浏览器完整性检查会按 UA
# 拦掉自报家门的自动化客户端（403 + `error code: 1010`，请求到不了源站）。
# 用常见浏览器 UA 是为了让合法请求过边缘，鉴权仍然走管理密钥。
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "设置.json")
CONFIG_YAML = os.path.join(HERE, "config.yaml")
OUT_DIR = os.path.join(HERE, "out")
PLAN_PATH = os.path.join(OUT_DIR, "import-plan.json")

# 本工具写进 accounts.notes 开头的固定标记。
# 用它而不是"备注里出现 CPA 字样"来认自己导入的账号——后者会把用户手工建的、
# 备注里恰好提到 CPA 的账号也算进删除范围。删除是不可逆的，认定必须精确。
NOTE_SENTINEL = "[cpa2sub2api]"

# 源码路径。可用 设置.json 的 sub2api_src / cpa_src 覆盖；
# 留空或路径不存在时先试在线抓取（fetch_upstream_src），再降级到内置常量，
# 每一级都会明确告警（不再静默降级）。
#
# 默认按"与本项目并列存放"推断，不写死任何人的家目录：产品代码里出现
# C:\Users\<某人>\... 既暴露作者环境，也让其他人拿到手必须先改代码。
# 目录名沿用两个上游仓库 GitHub 下载解压后的默认名。
_SIBLING = os.path.dirname(HERE)
SUB2API_SRC = os.environ.get(
    "SUB2API_SRC") or os.path.join(_SIBLING, "sub2api-main", "backend")
CPA_SRC = os.environ.get(
    "CPA_SRC") or os.path.join(_SIBLING, "CLIProxyAPI-main")

DEFAULTS = {
    # 两个地址留空：本工具面向任意部署，内置具体域名等于把作者的私有部署
    # 硬编码进产品代码，既泄露上游地址、也让别人拿到手要先改代码才能用。
    # 首次运行由菜单引导填写，写入 设置.json（该文件不进版本库）。
    "sub2api_base_url": "",
    "sub2api_admin_key": "",
    "cpa_base_url": "",
    "cpa_management_key": "",
    "group_prefix": "CPA",
    "default_concurrency": 3,
    "batch_size": 50,
    "respect_weight_zero": True,
    "sub2api_src": SUB2API_SRC,
    "cpa_src": CPA_SRC,

    # ---- 调度可恢复性（见 needs_inactive / reclaim_schedulable 的说明）----
    # zero_model_policy: excluded-models 把模型排干净的凭据怎么建。
    #   "inactive" = 建成停用（忠实反映 CPA 语义，默认）
    #   "active"   = 建成启用（靠 model_mapping 白名单拦住流量，不靠 status）
    "zero_model_policy": "inactive",
    # weight<=0 的凭据：CPA 在 weighted-round-robin 下确实不选它，但这是
    # **可恢复的临时状态**，不是凭据坏死。建成启用 + 低优先级，让它在
    # 上游恢复权重后能立刻参与调度，而不必人工重开。
    "weight_zero_policy": "deprioritize",
    # 建号时显式写 auto_pause_on_expired。sub2api schema 默认 true
    # （ent/schema/account.go:139 Default(true)），不显式传就会吃到这个默认，
    # 于是界面上出现"过期自动暂停调度"已勾选。CreateAccountRequest 支持该字段。
    "auto_pause_on_expired": False,
    # 导入后自动挂定时探活计划（auto_recover=true），用 sub2api 自带机制恢复。
    "auto_recover_enabled": True,
    "auto_recover_cron": "*/30 * * * *",
    # 每次运行是否把已存在账号的 model_mapping / 优先级等同步成 config.yaml 的最新值。
    # 关掉就退回"只建新号"的一次性快照行为（CPA 改了排除规则这边不会跟上）。
    "sync_existing_accounts": True,
    # 健康度智能重排：基于 sub2api 实际运行状态（可调度率、活跃率）动态计算优先级。
    # 开启后优先级不再盲目复制 config.yaml，而是按健康分数分层（S/A/B/C/D/F）。
    "health_rerank_enabled": True,
    # 池模式：中转站的一批 key 本就是可互换的池子。开启后 OpenAI 平台的
    # 健康熔断器才会生效（它只认池模式账号），四个平台也都会启用同账号重试。
    # 重试状态码默认用 POOL_MODE_RETRY_CODES（不含 401/403，理由见那里）。
    "pool_mode_enabled": True,
    "pool_mode_retry_status_codes": None,   # None = 用 POOL_MODE_RETRY_CODES
    "pool_mode_retry_count": 2,
    # 定价推送并发。对齐 new-api ratio_sync 的 maxConcurrentFetches=8；
    # 同一 group/channel 串行由 push_pricing 自己保证，这里只管实体之间的并行。
    "pricing_workers": 8,

    # ---- 上游源码：本地路径 + 在线兜底 ----
    # 本工具靠读上游 Go 源码来同步接口契约（见 sync_constants）。本地有源码
    # 就直接读；本地没有（比如你把源码目录删了）就按下面的 GitHub 地址在线取，
    # 取回的文件缓存在 out/upstream-cache/，之后离线也能用。
    # 三级降级：本地源码 > 在线抓取 > 内置兜底值（会明确告警）。
    "sub2api_repo": "https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/backend",
    "cpa_repo": "https://raw.githubusercontent.com/router-for-me/CLIProxyAPI/main",
    # 在线抓取开关。设为 false 则只用本地源码，取不到就直接降级到内置值。
    "fetch_upstream_src": True,
    # 三个数据文件（*.mhtml / pri.txt / model.txt）已随项目存放在本目录，
    # 默认无需任何外部路径。price_tool_dir 留空即可；只有当你想改用另一份
    # 更新的价格数据时才填，它是**兜底**而不是主路径（见 _price_paths）。
    "price_tool_dir": "",
    "price_mhtml": "",
    "price_pri": "",
    "price_model_txt": "",

    # 请求 UA。留空用 DEFAULT_USER_AGENT（浏览器 UA，避免被 Cloudflare
    # 的浏览器完整性检查按 UA 拦成 403 error 1010）。
    "user_agent": "",
}

# 浮点差分阈值。价格经 JSON 往返会有末位误差，用 == 比会永远认为有变化、
# 每次都重推一遍。对齐 new-api ratio_sync 的 floatEpsilon。
FLOAT_EPSILON = 1e-9


# =============================================================================
# 设置读写
# =============================================================================
def load_settings():
    s = dict(DEFAULTS)
    # 1. 读取设置.json（低优先级）
    if os.path.exists(SETTINGS_PATH):
        try:
            with io.open(SETTINGS_PATH, encoding="utf-8-sig") as f:
                s.update(json.load(f))
        except Exception:
            pass

    # 2. 环境变量覆盖（高优先级）
    env_mappings = {
        # 连接配置
        "SUB2API_BASE_URL": "sub2api_base_url",
        "SUB2API_ADMIN_KEY": "sub2api_admin_key",
        "CPA_BASE_URL": "cpa_base_url",
        "CPA_MANAGEMENT_KEY": "cpa_management_key",
        "FALLBACK_PROXY": "fallback_proxy",

        # 并发配置
        "IMPORT_WORKERS": "import_workers",
        "TEST_PLAN_WORKERS": "test_plan_workers",
        "PRICING_WORKERS": "pricing_workers",
        "DEFAULT_CONCURRENCY": "default_concurrency",
        "BATCH_SIZE": "batch_size",

        # 策略配置
        "WEIGHT_ZERO_POLICY": "weight_zero_policy",
        "ZERO_MODEL_POLICY": "zero_model_policy",
        "AUTO_RECOVER_ENABLED": "auto_recover_enabled",
        "AUTO_RECOVER_CRON": "auto_recover_cron",
        "AUTO_PAUSE_ON_EXPIRED": "auto_pause_on_expired",

        # 功能开关
        "HEALTH_RERANK_ENABLED": "health_rerank_enabled",
        "SYNC_EXISTING_ACCOUNTS": "sync_existing_accounts",
        "RESPECT_WEIGHT_ZERO": "respect_weight_zero",
        "FETCH_UPSTREAM_SRC": "fetch_upstream_src",
        "POOL_MODE_ENABLED": "pool_mode_enabled",

        # 源码路径
        "SUB2API_SRC": "sub2api_src",
        "CPA_SRC": "cpa_src",

        # 其他配置
        "GROUP_PREFIX": "group_prefix",
        "USER_AGENT": "user_agent",

        # HTTP 行为。这三个键原先只在 .env.example 里"写着"，没有接线，
        # 设了也不生效（日志级别与代理开关是最容易被误以为生效的两个）。
        "HTTP_TIMEOUT": "http_timeout",
        "HTTP_RETRIES": "http_retries",
        "DISABLE_PROXY_FALLBACK": "disable_proxy_fallback",
    }

    for env_key, setting_key in env_mappings.items():
        val = os.environ.get(env_key)
        if val:
            # 数值字段转换
            if setting_key in ("import_workers", "test_plan_workers", "pricing_workers",
                              "default_concurrency", "batch_size", "pool_mode_retry_count"):
                try:
                    s[setting_key] = int(val)
                except ValueError:
                    pass
            # 布尔字段转换
            elif setting_key in ("auto_recover_enabled", "health_rerank_enabled",
                                "sync_existing_accounts", "respect_weight_zero",
                                "fetch_upstream_src", "pool_mode_enabled", "auto_pause_on_expired"):
                s[setting_key] = val.lower() in ("true", "1", "yes", "on")
            # 字符串字段
            else:
                s[setting_key] = val

    if int(s.get("default_concurrency", 3)) <= 0:
        s["default_concurrency"] = 3
    # 顺手刷新兜底代理的缓存。_http 是热路径、不带 settings 参数，
    # 它调 _fallback_proxy() 时只能吃缓存；而不刷新的话，第一次读到的
    # 永远是进程启动时的旧值（甚至 None），环境变量改了也不生效。
    _fallback_proxy(s)
    return s


def save_settings(s):
    with io.open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def mask(v):
    if not v:
        return "未设置"
    v = str(v)
    return "%s…%s（共%d位）" % (v[:6], v[-4:], len(v)) if len(v) > 12 else "已设置"


# =============================================================================
# HTTP
# =============================================================================
class ApiError(Exception):
    def __init__(self, status, body, url):
        self.status, self.body, self.url = status, body, url
        super(ApiError, self).__init__("HTTP %s %s" % (status, url))


def _http(method, url, headers=None, body=None, timeout=None, retries=None,
          proxy=None, _allow_proxy_fallback=True):
    """发一个 HTTP 请求，对**瞬时**网络故障自动重试。

    timeout / retries 传 None 表示"用配置里的值"：读 设置.json 与环境变量
    （HTTP_TIMEOUT / HTTP_RETRIES，由 load_settings 映射）。
    以前这两个参数是写死的默认值 30 / 3，于是 .env.example 里承诺的
    HTTP_TIMEOUT / HTTP_RETRIES 设了也不生效 —— 文档与行为不一致。

    为什么必须重试：请求要穿过 Cloudflare + nginx 两层，偶发的
    `RemoteDisconnected: Remote end closed connection without response`
    是常态——对端在还没写出响应行时就关掉了连接。这类失败与请求内容无关，
    重发一次通常就好。没有重试的话，一次抖动就能让整个批量流程带着
    Traceback 崩掉（实测清空流程就是在列代理时这样断的）。

    只重试**安全**的情况：
      · 连接层错误（URLError / RemoteDisconnected / 超时等），请求多半
        没被服务端处理；
      · 5xx 和 429，服务端明确表示"稍后再来"。
    4xx 一律不重试——那是请求本身的问题，重发多少次都一样。

    写操作（POST/PUT/DELETE）由调用方带 Idempotency-Key，所以重发是安全的：
    服务端会识别出这是同一个请求，不会重复建号。

    代理兜底（Problem 6.3）：直连的重试全部用尽且失败属于**连接层**时，
    自动改走 设置.json 的 fallback_proxy 再试一轮。适用场景是本机到上游
    被墙/被 DNS 污染，而代理能通——这时直连永远失败，不换出口再试多少次
    都没意义。只对连接层失败和 5xx 兜底：4xx 是请求本身的问题，换出口
    同样会失败，白等一轮。proxy 参数显式指定时不再二次兜底（避免递归）。
    """
    headers = dict(headers or {})
    # timeout / retries 取配置值。放在这里而不是函数签名默认值里，
    # 是因为默认值在**函数定义时**就求值了，那时 settings 还没加载完。
    if timeout is None or retries is None:
        try:
            _s = load_settings()
        except Exception:
            _s = {}
        if timeout is None:
            try:
                timeout = int(_s.get("http_timeout") or 30)
            except (TypeError, ValueError):
                timeout = 30
            if timeout <= 0:
                timeout = 30
        if retries is None:
            try:
                retries = int(_s.get("http_retries") or 3)
            except (TypeError, ValueError):
                retries = 3
            if retries <= 0:
                retries = 1
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    # User-Agent 必须伪装成浏览器。
    #
    # sub2api 一般挂在 Cloudflare 后面，而 CF 的「浏览器完整性检查」会按
    # User-Agent 拦掉一切不像浏览器的客户端，返回 HTTP 403 + 文本
    # `error code: 1010`——注意这是 CF 边缘直接拒绝，请求根本没到源站，
    # 所以在 sub2api 的日志里什么都看不到。实测用
    # `cpa2sub2api/3.0` 这种自报家门的 UA，连 /health 都是 403 1010。
    #
    # 这不是"绕过防护"：管理密钥仍然照常校验，只是让合法的自动化请求不被
    # 按 UA 一刀切。可用 设置.json 的 user_agent 覆盖成运维要求的值。
    headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    headers.setdefault("Accept", "application/json, text/plain, */*")

    # 显式带代理时用带 ProxyHandler 的 opener；不带代理时**也必须**用
    # 空的 ProxyHandler 建 opener。
    #
    # 为什么不能直接 `urllib.request.urlopen`：默认 opener 会读环境变量
    # http_proxy / https_proxy。容器或 shell 里一旦设了这两个变量，
    # "直连"那一轮实际上也走了代理，于是：
    #   · 代理兜底的语义没了（本来就该直连的也走代理）；
    #   · 日志会误报"直连失败，经代理成功"，把排查带偏。
    # 显式装一个空 ProxyHandler，才是真正的"不走代理"。
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    attempts = max(1, int(retries or 1))
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            body_txt = ex.read().decode("utf-8", "replace")
            # 把 CF 的拦截翻译成人能看懂的话，否则只会看到一句 "error code: 1010"
            if ex.code == 403 and "1010" in body_txt:
                raise ApiError(ex.code,
                               "被 Cloudflare 浏览器完整性检查拦截（error 1010）。"
                               "请在 设置.json 里把 user_agent 设成浏览器 UA，"
                               "或在 CF 上为本机 IP / 该路径放行。原文：" + body_txt[:120],
                               url)
            # 5xx / 429 值得再试；其余是请求本身的问题，立刻上报
            if ex.code in (429, 500, 502, 503, 504) and i < attempts - 1:
                last = ApiError(ex.code, body_txt, url)
                time.sleep(1.5 * (i + 1))
                continue
            err = ApiError(ex.code, body_txt, url)
            if ex.code in (429, 500, 502, 503, 504):
                last = err
                break          # 交给下面的代理兜底
            raise err
        except Exception as ex:
            # URLError、RemoteDisconnected、socket.timeout 等连接层故障。
            # 用宽 except 是有意的：http.client 会抛出好几种彼此无关的异常类型，
            # 逐一枚举反而容易漏，而这里的处理方式对它们都一样。
            reason = getattr(ex, "reason", None) or ex
            last = ApiError(0, "连接失败：%s" % str(reason)[:160], url)
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
                continue
            break              # 交给下面的代理兜底

    # ---- 代理兜底 ----
    # 直连这一轮已经用尽。只在「没显式指定代理」且「配了 fallback_proxy」
    # 时再试一轮，且失败必须是连接层或 5xx——4xx 换出口也一样失败。
    if _allow_proxy_fallback and not proxy:
        fb = _fallback_proxy()
        if fb and last is not None and (last.status == 0 or last.status >= 500
                                        or last.status == 429):
            try:
                st, txt = _http(method, url, headers=headers, body=body,
                                timeout=timeout, retries=1, proxy=fb,
                                _allow_proxy_fallback=False)
                print("    [代理兜底] 直连失败，经代理成功：%s" % url)
                return st, txt
            except Exception:
                # 代理也不通：如实报直连的那个错，别拿代理的错掩盖真实原因
                pass

    if last:
        raise last
    raise ApiError(0, "请求失败：未知原因", url)


def _fallback_proxy(settings=None):
    """兜底代理地址，没配返回 None。

    **为什么必须看 settings 而不是直接读 设置.json**：
    load_settings() 会把环境变量 FALLBACK_PROXY 映射成 settings["fallback_proxy"]，
    而容器部署（docker-compose 里 `FALLBACK_PROXY=${FALLBACK_PROXY:-http://mihomo:7890}`）
    根本不写 设置.json —— 配置全靠环境变量下发。以前这个函数只读 设置.json，
    于是容器里 fallback_proxy 恒为 None，**代理兜底功能在容器部署下完全失效**：
    _http 遇到 403/5xx 不会去试代理，直连被墙的上游就一直失败。
    本机跑时因为 设置.json 里有该键，看不出这个问题。

    读一次就缓存：_http 是热路径，不该每个请求都去碰磁盘。
    传入 settings 时以它为准（调用方已经从 load_settings 拿到了合并结果）。
    """
    global _FALLBACK_PROXY_CACHE
    if settings is not None:
        # DISABLE_PROXY_FALLBACK 在这里统一生效：调试代理问题时需要能一键
        # 关掉兜底，否则"直连本来能通却被代理接管"这类现象很难复现。
        if str(settings.get("disable_proxy_fallback") or "").lower() in (
                "1", "true", "yes", "on"):
            _FALLBACK_PROXY_CACHE = None
            return None
        val = str(settings.get("fallback_proxy") or "").strip() or None
        _FALLBACK_PROXY_CACHE = val
        return val
    if _FALLBACK_PROXY_CACHE is not _UNSET:
        return _FALLBACK_PROXY_CACHE
    val = None
    try:
        s = load_settings()
        if str(s.get("disable_proxy_fallback") or "").lower() in (
                "1", "true", "yes", "on"):
            s = {}
        val = str(s.get("fallback_proxy") or "").strip() or None
    except Exception:
        val = None
    _FALLBACK_PROXY_CACHE = val
    return val


_UNSET = object()
_FALLBACK_PROXY_CACHE = _UNSET


def idempotency_key(payload):
    """按请求体内容算一个稳定的幂等键。

    sub2api 的写接口是 RequireKey=true（idempotency_helper.go:47），当前只因
    ObserveOnly 默认为真才容忍不带；运维一旦关掉 observe_only，不带这个头的
    创建请求会全部 400。内容派生还有第二个好处：批次超时后原样重发是安全的，
    服务端会识别为同一请求而不是再建一遍。
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Sub2Api(object):
    def __init__(self, s):
        self.base = s["sub2api_base_url"].rstrip("/")
        self.key = s["sub2api_admin_key"]
        self.timeout = 30
        # 批量创建一次要服务端逐条建号并触发异步探测，30 秒太紧，单独放宽
        self.batch_timeout = 120
        # 允许运维指定 UA（见 _http 关于 Cloudflare 1010 的说明）
        self.user_agent = (s.get("user_agent") or "").strip() or DEFAULT_USER_AGENT

    def _call(self, method, path, body=None, timeout=None, idem=None):
        headers = {"x-api-key": self.key, "User-Agent": self.user_agent}
        if idem:
            headers["Idempotency-Key"] = idem
        st, raw = _http(method, self.base + path, headers, body,
                        timeout or self.timeout)
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ApiError(st, "非 JSON 返回：" + raw[:300], path)

    def ping(self):
        try:
            n = len(self.list_groups())
            return True, "连接正常，当前 %d 个分组" % n
        except ApiError as ex:
            if ex.status == 401:
                return False, "密钥不对（401）。请检查 设置.json 里的 sub2api_admin_key。"
            if ex.status == 403:
                return False, "密钥权限不足（403）。请用 admin- 开头的管理密钥。"
            if ex.status == 404:
                return False, "接口 404。检查 sub2api 地址是否正确。"
            if ex.status == 0:
                return False, ex.body
            return False, "HTTP %s：%s" % (ex.status, ex.body[:160])

    def list_groups(self):
        d = self._call("GET", "/api/v1/admin/groups/all")
        items = d.get("data") or []
        if isinstance(items, dict):
            items = items.get("items") or items.get("list") or []
        return [x for x in items if isinstance(x, dict)]

    def create_group(self, p):
        return self._call("POST", "/api/v1/admin/groups", p,
                          idem=idempotency_key(p))

    def get_group(self, gid):
        """GET /admin/groups/:id。差分前要先读回当前定价。

        响应里的 model_pricing 是 []ChannelModelPricing
        （group_handler.go:195），与写入时的结构同构。
        """
        return self._call("GET", "/api/v1/admin/groups/%d" % gid)

    def update_group(self, gid, patch):
        """PUT /admin/groups/:id。只发要改的字段。

        **别顺手带 rate_multiplier**：它的校验是 `<=0 报错`，且用的是裸
        errors.New（admin_group.go:383 与 :772），Gin 会把它变成 HTTP 500
        而不是 400，报错信息里看不出真实原因。要改倍率就单独一次调用，
        并且确保传的是正数。
        """
        return self._call("PUT", "/api/v1/admin/groups/%d" % gid, patch,
                          idem=idempotency_key({"id": gid, "patch": patch}))

    def create_proxy(self, p):
        return self._call("POST", "/api/v1/admin/proxies", p,
                          idem=idempotency_key(p))

    def list_proxies(self):
        d = self._call("GET", "/api/v1/admin/proxies/all")
        items = d.get("data") or []
        if isinstance(items, dict):
            items = items.get("items") or items.get("list") or []
        return [x for x in items if isinstance(x, dict)]

    def batch_accounts(self, accounts):
        body = {"accounts": accounts}
        return self._call("POST", "/api/v1/admin/accounts/batch", body,
                          timeout=self.batch_timeout, idem=idempotency_key(body))

    def update_account(self, aid, patch):
        """PUT /accounts/:id。

        创建接口没有 status 字段（CreateAccountRequest 里就没有，服务端一律
        建成 active），要停用只能建完再单独改一次。这是 sub2api 的既定行为，
        不是可以绕过的参数问题。
        """
        return self._call("PUT", "/api/v1/admin/accounts/%d" % aid, patch,
                          idem=idempotency_key({"id": aid, "patch": patch}))

    def bulk_update_accounts(self, ids, patch):
        """POST /accounts/bulk-update。一次改多条账号。

        这是**唯一**同时接受 status 和 schedulable 两个字段的接口：
          · CreateAccountRequest 两个都没有；
          · UpdateAccountRequest（PUT /accounts/:id）只有 status，没有 schedulable；
          · BulkUpdateAccountsRequest 两个都有（还支持 filters 按条件批量选取）。
        所以要把被网关关掉的调度开关重新打开，只能走这里。

        注意官方的 skills/sub2api-admin/references/admin-cli.md 里列举
        bulk-update 可覆盖字段时**漏了** schedulable，但 Go 结构体
        BulkUpdateAccountsRequest 确实有 `Schedulable *bool`。以代码为准。
        """
        body = {"account_ids": list(ids)}
        body.update(patch)
        return self._call("POST", "/api/v1/admin/accounts/bulk-update", body,
                          idem=idempotency_key(body))

    def create_test_plan(self, body):
        """POST /scheduled-test-plans。给账号挂定时探活计划。

        请求字段照 createScheduledTestPlanRequest（scheduled_test_handler.go:22）：
        account_id 和 cron_expression 是 binding:"required"，model_id 可省
        （省了就用账号自己的默认模型），enabled / auto_recover 是 *bool。

        auto_recover=true 是关键：runner 在测试成功后会调
        tryRecoverAccount 把账号从 error 状态捞回来
        （scheduled_test_runner_service.go:133）。默认值是 false
        （migrations/070），不显式传等于白挂。
        """
        return self._call("POST", "/api/v1/admin/scheduled-test-plans", body,
                          idem=idempotency_key(body))

    def list_test_plans(self, aid):
        """GET /accounts/:id/scheduled-test-plans。查某账号已有的探活计划。"""
        return self._call("GET",
                          "/api/v1/admin/accounts/%d/scheduled-test-plans" % aid)

    def list_test_results(self, plan_id):
        """GET /scheduled-test-plans/:id/results。查某探活计划的历史结果。

        结果字段照 ScheduledTestResult（scheduled_test_port.go:24）：
        status / error_message / latency_ms / finished_at。
        用来判断「这条上游到底通不通」——停用账号的重新启用必须有这个证据，
        不能凭空把 config.yaml 里关掉的站点拉起来。
        """
        return self._call(
            "GET", "/api/v1/admin/scheduled-test-plans/%d/results" % plan_id)

    def list_accounts(self, page=1, size=100):
        # lite=true 只取调度必需字段，避免每页拖回几十个用不到的胖字段
        return self._call(
            "GET", "/api/v1/admin/accounts?page=%d&page_size=%d&lite=true" % (page, size))

    def delete_account(self, aid):
        return self._call("DELETE", "/api/v1/admin/accounts/%d" % aid)

    def delete_group(self, gid):
        return self._call("DELETE", "/api/v1/admin/groups/%d" % gid)

    def delete_proxy(self, pid):
        return self._call("DELETE", "/api/v1/admin/proxies/%d" % pid)


# =============================================================================
# CPA 配置读取（本地文件优先，否则联网）
# =============================================================================
def fetch_cpa_text(s):
    if os.path.exists(CONFIG_YAML):
        with io.open(CONFIG_YAML, encoding="utf-8", errors="replace") as f:
            return f.read(), "本地 config.yaml"
    # 联网模式：需要密钥，尝试从 settings 或（若有）本地 config 抽取
    key = s.get("cpa_management_key") or ""
    base = s.get("cpa_base_url", "").rstrip("/")
    if not base or not key:
        raise RuntimeError(
            "既没有本地 config.yaml，也没有配置 CPA 联网密钥。\n"
            "        请把 config.yaml 放到本工具目录，或在 设置.json 里填 cpa_management_key。")
    hdr = {"Authorization": "Bearer " + key, "X-Management-Key": key}
    _, raw = _http("GET", base + "/v0/management/config.yaml", hdr, timeout=30)
    return raw, base


# =============================================================================
# 转换核心
# =============================================================================
SECTION_MAP = {
    "claude-api-key":       ("anthropic", "apikey", None),
    "gemini-api-key":       ("gemini",    "apikey", None),
    "codex-api-key":        ("openai",    "apikey", "force_responses"),
    "openai-compatibility": ("openai",    "apikey", "force_chat_completions"),
    "interactions-api-key": ("gemini",    "apikey", None),
    "xai-api-key":          ("grok",      "apikey", None),
}
SECTION_GROUP = {
    "claude-api-key": "Claude", "gemini-api-key": "Gemini",
    "codex-api-key": "Codex", "openai-compatibility": "OpenAI",
    "interactions-api-key": "Gemini", "xai-api-key": "Grok",
}
GROUP_PLATFORM = {"Claude": "anthropic", "Gemini": "gemini",
                  "Codex": "openai", "OpenAI": "openai", "Grok": "grok"}
HEADER_OK_PLATFORMS = {"anthropic", "openai", "kimi", "zhipu", "deepseek", "minimax", "grok"}
# 禁止覆写的请求头，共 30 条，完全照抄 sub2api 源码
# （internal/service/account_header_override.go 的 headerOverrideBlockedNames）。
# 带了其中任何一个，整条账号创建会被 400 拒绝（INVALID_HEADER_OVERRIDE），
# 所以必须在本地提前剔除，而不是交给服务端报错。
# 启动时会尝试从源码动态提取，提取失败则用此兜底值。
HEADER_BLACKLIST = {
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
}
MAX_HEADER_NAME = 200
MAX_HEADER_VALUE = 8192
MAX_HEADER_ENTRIES = 64
# 合法的 header 名字符集（RFC 7230 token）
_HEADER_NAME_OK = re.compile(r"^[!#$%&'*+\-.^_`|~0-9a-z]+$")
# CPA 的强制冷却时长是硬编码的 transientErrorCooldown = time.Minute
# （conductor_refresh.go:37，conductor_cooldown.go:2125 直接用这个常量，
# 连全局的 transient-error-cooldown-seconds 都不影响它）。
# 也就是 1 分钟，不是 1 小时——写成 60 会把冷却拉长 60 倍。
DEFAULT_COOLDOWN_MINUTES = 1
# CPA 里只有 *-and-cooldown 这两个 action 才真的触发冷却；
# stop / continue 只影响是否换凭据、是否中止，不冷却。
COOLDOWN_ACTIONS = {"stop-and-cooldown", "continue-and-cooldown"}

# ---- 上游接口契约（由 sync_constants 从源码刷新，这里只是启动前的初值）----
# 写成模块级变量而不是每次现查，是为了让"上游改了契约"这件事有唯一的
# 落点：sync_constants 一处更新，全工具立刻一致，并在变化时出声提醒。
CREATE_HAS_STATUS = False        # 建号接口没有 status，停用只能建完再改
CREATE_HAS_SCHEDULABLE = False   # 建号接口也没有 schedulable
BULK_HAS_SCHEDULABLE = True      # 只有 bulk-update 能改调度开关
ACCOUNT_DEFAULTS = {"auto_pause_on_expired": True, "schedulable": True}
PRICING_PER_TOKEN = True         # 定价单位是每 token 的 USD，换算要除 1e6

# 模型「就高原则」的代际门槛，由 sync_constants 从上游源码里出现过的真实
# 模型名反推（extract.extract_model_series），并在每次启动时经
# build_model_mapping / _pick_probe_model 注入 model_selection。
#
# 这里的初值只是**启动前的占位**，不是权威值：写死代数会在上游发新一代时
# 静默失效（门槛停在 5 代而上游已到 6 代，新模型会被当旧代全砍）。
# 传进 model_selection 时若拿到 {} 或 None，它还会退一步用"模型列表里
# 观测到的最高代"作门槛，所以即便反推失败、这份占位也没被替换，
# 也不会把新一代模型砍掉。见 model_selection.select_highest_models。
MODEL_SERIES = {"anthropic": 5, "openai": 6}

# 按错误码分档的冷却时长（分钟）。
#
# 为什么要分档：CPA 对所有 *-and-cooldown 规则用同一个硬编码的 1 分钟
# （transientErrorCooldown），因为在 CPA 的模型里"冷却"只是"这次别用它，
# 过一会儿再试"，代价很低。但迁到 sub2api 之后代价不一样了——sub2api 的
# temp_unschedulable 期间该账号完全退出调度，而且 sub2api 还有独立的
# 自动置错逻辑（SetError 会连带关掉调度开关，且恢复路径不还 schedulable，
# 见 reclaim_schedulable）。对"限流类"错误用 1 分钟是对的，反复重试很快
# 就能恢复；但对"凭据坏死类"错误（401 鉴权失败、403 无权限），1 分钟一到
# 就又把请求打上去、再失败、再冷却，既拖慢整体成功率，也更容易触发上游的
# 风控。这类错误应该退避得更久。
#
# 分档依据（吸收 done-hub controller/common.go:76-111 的思路：区分
# "限流/暂时不可用"和"凭据/权限坏死"，前者绝不永久封禁、后者退避要长）：
#   · 429 / 503 / 529 = 限流或过载，上游自己会恢复 -> 沿用 CPA 的 1 分钟
#   · 408 / 504 = 超时，多半是网络抖动 -> 短冷却
#   · 401 / 403 = 凭据或权限问题，一分钟内不可能自愈 -> 长冷却
#   · 402 = 余额不足，需要人工充值 -> 更长冷却
# 没列出的错误码一律回落到 DEFAULT_COOLDOWN_MINUTES，保持与 CPA 一致。
#
# 注意这**不是**推翻 CPA 的语义：CPA 的 1 分钟针对的是"是否参与本次请求的
# 重试"，而这里写的是 sub2api 的"退出调度多久"，两者量纲不同。对限流类
# 保持 1 分钟就是为了不偏离 CPA 的行为；只有在 CPA 没有区分、而 sub2api
# 的代价明显更高的地方才加长。
#
# 服务端只校验 duration_minutes<=0（ratelimit_service.go:2652），没有上限，
# 所以这里的值不会被静默截断。
COOLDOWN_MINUTES_BY_CODE = {
    401: 30,   # unauthorized：key 失效/被撤销
    402: 60,   # payment required：余额耗尽，1小时后重试（可能配额已重置）
    403: 30,   # forbidden：无权限/被封
    408: 2,    # request timeout
    429: 1,    # too many requests：上游限流，很快恢复
    503: 1,    # service unavailable
    504: 2,    # gateway timeout
    529: 1,    # overloaded（Anthropic 用）
}

# 「与凭据健康无关」的关键词。命中这些的规则不该冷却账号。
#
# 起因：CPA 配置里有一批 `status: 400` 的规则，关键词混了两类完全不同的东西：
#   · 上游侧问题：Failed to resolve routing group / 无可用渠道 / 当前分组
#     —— 这是中转站自己没有可用后端，属于凭据当前不可用，冷却是对的；
#   · 客户端问题：Context window is full / 1m 上下文
#     —— 这是调用方把上下文塞爆了，换哪个凭据都一样会失败。
#     把它当成凭据故障去冷却，等于用户发错一次请求就白白废掉一个 key。
#
# 所以这里不是按状态码一刀切删掉 400，而是按关键词把第二类摘出去。
# 摘干净后如果这条规则没有关键词了，整条规则就不再生成。
CLIENT_SIDE_KEYWORDS = (
    "context window is full",
    "1m 上下文",
    "context length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
)

# 池模式下允许「在同一个账号上重试」的状态码。
#
# 刻意**不含 401/402/403**：
#   · 401/403：key 被吊销或封禁，同一个 key 再试无意义；
#   · 402：配额耗尽，需冷却等待配额重置，不是瞬时抖动。
# sub2api 的默认列表是 [401, 403, 429]（account.go:1133），这里显式覆盖掉。
#
# 留下的都是「换个时机同一个 key 可能就好了」的瞬时错误：
#   429 限流、500/502/503 上游内部错误、529 过载。
POOL_MODE_RETRY_CODES = (429, 500, 502, 503, 529)

# 需要补齐的冷却规则：CPA 里没配、但对中转站账号很关键的错误码。
#
# 401 是中转站 key 最常见的死法之一（被运营方吊销 / 额度用尽后直接拒认证），
# 而 CPA 侧的 request-scoped-errors 只配了 403 和 400，401 完全没覆盖 ——
# 于是这类 key 会一直留在调度池里反复失败。
#
# 关键词留空表示「只要是这个状态码就冷却」：sub2api 的匹配要求
# error_code 与 keywords 同时命中（ratelimit_service.go:2556），
# 所以这里必须给关键词，用各家上游 401 响应体里都会出现的通用词。
EXTRA_COOLDOWN_RULES = [
    {
        "error_code": 401,
        "keywords": ["unauthorized", "invalid api key", "invalid_api_key",
                     "authentication", "无效的令牌", "令牌验证失败"],
        "description": "本工具补充：401 鉴权失败，多为 key 被吊销或额度耗尽",
    },
    {
        "error_code": 402,
        "keywords": ["budget pool quota", "quota has been exhausted",
                     "insufficient balance", "payment required",
                     "余额不足", "配额已用完", "额度已耗尽"],
        "description": "本工具补充：402 配额耗尽，1小时后重试（配额可能已重置）",
    },
]


def cooldown_minutes_for(status):
    """给定上游错误码，返回该退避多少分钟。

    未收录的错误码回落到 DEFAULT_COOLDOWN_MINUTES（即 CPA 的原始行为），
    保证这个增强只在"明确知道该退避更久"的码上生效，不改变其余情况。
    """
    if _is_int(status):
        v = COOLDOWN_MINUTES_BY_CODE.get(int(status))
        if _is_int(v) and v > 0:
            return int(v)
    return DEFAULT_COOLDOWN_MINUTES


# 属于「整个域名/预算池」层面的错误码，不是单个 KEY 的毛病。
#
# 为什么需要单独一层：sub2api 的 temp_unschedulable_rules 是**逐账号**下发的，
# 冷却一个账号只让它自己退出调度。但 402 的响应体说的是
# `Budget pool quota has been exhausted` —— 这是**预算池**耗尽，
# 同一个中转站下的所有 KEY 共用同一个池子，它们会在同一时刻一起失效。
#
# 实测证据（logs.txt，2026-09-12 23:46-23:55，15 次请求）：
#   · 选中的账号 15/15 都是同一个 selected_account_id
#   · 上游一律返回 402 budget pool quota has been exhausted
#   · sub2api 判为 non-retryable，pool_mode_error_skipped，不换号
#   · sticky_honored=true，粘在同一个账号上
#   · 低优先级站点一次都没被尝试，客户端侧表现为 503 / 524
#
# 同一份 config.yaml 里早就记过同一个教训（第 194-202 行）：
#   "某站点有 14 个 key 共用同一个账号余额，该站余额耗尽时，
#    把配额用在同站内轮换毫无意义——一个请求连打 8 个同站凭证
#    全部失败，从未降级到当时可用的其它站。"
#
# 修法：给这类错误码**在关键词里带上强特征**，让 sub2api 在匹配到
# 这类响应时冷却该账号更久（见 DOMAIN_SCOPED_COOLDOWN_MINUTES）。
# 关键词刻意取得很"长且特异"（词组而非单词），确保只有真正报了
# 预算池/余额耗尽的上游才会命中，不会误伤同状态码的其它情况。
#
# 为什么不直接让同域名所有账号一起冷却：sub2api 没有这个接口，
# 规则只能逐账号下发。而"整桶一起冷却"的语义由**分桶选号**承担 ——
# 同域名同 priority 的 KEY 在一桶里轮循，桶内任意一个被冷却后，
# 请求会轮到同桶的其它 KEY；等它们也各自命中并冷却，整桶才真正退出，
# 请求自然降级到下一个域名。这正是文档第 5 条要的三层防护。
DOMAIN_SCOPED_COOLDOWN_CODES = {
    402: 180,   # payment required：预算池耗尽，3 小时内不重试
}


def build_domain_scoped_cooldown(rse):
    """生成「域名/预算池级」的冷却规则。

    与 build_temp_unschedulable 生成的是**同一类**规则（sub2api 的
    error_code + keywords + duration_minutes），区别只在：
      · 只针对 DOMAIN_SCOPED_COOLDOWN_CODES 里的错误码；
      · 关键词取得更特异，匹配到就说明整个池子没额度了，冷却更久；
      · 无论 CPA 的 request-scoped-errors 里配没配该错误码，都会生成
        —— 因为 CPA 侧根本不区分"单个 KEY 坏"和"整池耗尽"。

    返回规则列表（可能为空）。
    """
    out = []
    for code, minutes in DOMAIN_SCOPED_COOLDOWN_CODES.items():
        kws = DOMAIN_SCOPED_KEYWORDS.get(code)
        if not kws:
            continue
        out.append({
            "error_code": code,
            "keywords": list(kws),
            "duration_minutes": minutes,
            "description": ("本工具补充：%s 属于整个上游（预算池/账号余额）层面的"
                            "耗尽，不是单个 KEY 的问题。冷却 %d 分钟避免在同一"
                            "池子里逐个 KEY 空撞，把请求留给其它域名。"
                            % (code, minutes)),
        })
    return out


# 域名级冷却用的关键词。取得**长而特异**是刻意的：
# sub2api 的匹配是大小写不敏感的子串匹配（ratelimit_service.go:2556），
# 关键词太短（比如 "quota"）会误伤"单次请求配额"之类的无关响应。
DOMAIN_SCOPED_KEYWORDS = {
    402: ("budget pool quota", "quota has been exhausted",
          "insufficient balance", "余额不足", "配额已用完", "额度已耗尽"),
}


def host_of(url):
    if not url:
        return "unknown"
    try:
        return urlparse(url).hostname or "unknown"
    except Exception:
        return "unknown"


def _is_int(v):
    """真整数判定。

    Python 里 isinstance(True, int) 为真，YAML 写 `weight: false` 会被当成 0、
    `status: true` 会变成 error_code=1。凭据配置上这种静默曲解必须挡掉。
    """
    return isinstance(v, int) and not isinstance(v, bool)


def _match_excluded(name, patterns):
    """CPA 的 excluded-models 匹配（service_models.go:621-662）。

    大小写不敏感：CPA 在加载时把模式统一转小写（config_normalization.go:354），
    比对时又把模型名转小写（service_models.go:563）。
    支持精确、前缀 `abc*`、后缀 `*abc`、包含 `*abc*`，以及多段 `a*b*c`
    （锚定的首尾 + 按顺序出现的中间片段）。空模式永不匹配。
    """
    low = name.lower()
    for raw in patterns or []:
        p = str(raw).strip().lower()
        if not p:
            continue
        if "*" not in p:
            if p == low:
                return True
            continue
        parts = p.split("*")
        head, tail, mids = parts[0], parts[-1], [x for x in parts[1:-1] if x]
        if head and not low.startswith(head):
            continue
        if tail and not low.endswith(tail):
            continue
        # 首尾锚定后，中间片段必须按顺序出现在剩余区间里
        start = len(head)
        end = len(low) - len(tail)
        if start > end:
            continue
        seg, ok = low[start:end], True
        for m in mids:
            i = seg.find(m)
            if i < 0:
                ok = False
                break
            seg = seg[i + len(m):]
        if ok:
            return True
    return False


# 就高原则的实现曾经在这里有两份拷贝（_claude_major /
# filter_models_by_highest / _filter_models_by_highest_strict），
# 与 model_selection.py 各改各的，是典型的规则漂移点：
#   · 这里那份会读 MODEL_SERIES（由 sync_constants 从上游源码反推）；
#   · model_selection.py 那份把代数写死成 -5 / ^gpt-6。
# 但真正被调用的是 model_selection，所以 MODEL_SERIES 的反推结果
# 一直喂给死代码 —— 上游发新一代时告警会响，行为不会变。
#
# 现在只保留 model_selection.py 一份实现，并由它接收 MODEL_SERIES，
# 代际门槛真正跟随上游前移。见 build_model_mapping。


def build_model_mapping(models, excluded=None, platform=None, source_section=None):
    """CPA models[] -> sub2api credentials.model_mapping。

    关键语义：model_mapping 非空即**白名单**（account.go:841-861）。不在表里的
    模型会让整个账号被踢出调度候选集，网关直接报"无可用账号"，不是简单的改名
    失效。所以每个要放行的模型都必须出现在表里。

    两个后果：
      1. 有 alias 的模型必须同时写入 alias 和原名，否则按原名请求会被挡；
      2. CPA 的 excluded-models 可以借这个白名单表达——把命中排除规则的模型
         从表里减掉即可。sub2api 没有模型黑名单字段，这是唯一的等价实现。

    Args:
        models: list of {"name": str, "alias": str}
        excluded: list of wildcard patterns
        platform: "gemini" | "anthropic" | "openai" (应用就高原则)
        source_section: CPA 里的来源段名。openai 平台靠它区分
            codex（单族）与 openai-compatibility（多族）。两者 platform
            都是 "openai"，只传 platform 无法区分。
    """
    # Step 1: 应用就高原则过滤
    if platform and MODEL_SELECTION_AVAILABLE and models:
        # select_highest_models 期望 list[dict]，直接传入
        models = select_highest_models(models, platform,
                                       source_section=source_section,
                                       series=MODEL_SERIES)

    # Step 2: 应用 excluded-models 黑名单
    out = {}
    for m in models or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m["name"])
        if _match_excluded(name, excluded):
            continue
        alias = str(m.get("alias") or name)
        out[alias] = name
        # 补全同名映射：否则客户端按上游原名请求会被白名单挡掉
        if alias != name:
            out.setdefault(name, name)
    return out


def account_fingerprint(r):
    """账号的内容指纹，用于生成稳定名字和跨运行去重。

    以前名字是位置序号（ANT-host-01），编号取决于账号在 config.yaml 里的位置。
    一旦从 config 里删掉一条，后面所有编号左移：旧名字仍然存在但背后已经是
    另一个 api_key，而去重是纯按名字比对的，于是旧 key 被判"已存在，跳过"
    永久留在 sub2api，新 key 永远导不进去。指纹从 api_key + base_url 派生，
    与位置无关，删改 config 后仍能正确识别同一个上游凭据。
    """
    raw = "%s|%s" % (r.get("api_key") or "", r.get("base_url") or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


# 白名单被 excluded-models 清空时占位用的假模型名。
# 不能留空表：sub2api 里空的 model_mapping 等于"放行全部模型"
# （account.go:854 的 len(mapping)==0 分支直接 return true），
# 而 CPA 里"全部模型被排除"等于这条凭据什么都不提供——方向正好相反。
# 放一个永不会被请求的名字，白名单就非空，账号也就不会成为任何模型的候选。
BLOCK_ALL_SENTINEL = "__cpa2sub2api_all_models_excluded__"


def serves_nothing(r):
    """这条 CPA 凭据是否被 excluded-models 排除到一个模型都不剩。

    CPA 的 applyExcludedModels 作用在已配置的 models[] 之上
    （service_models.go:108-115），且 matchWildcard("*", 任意) 恒为真，
    所以 excluded-models: ["*"] 会让这条凭据注册零个模型、完全不参与调度。
    """
    all_m = build_model_mapping(r.get("models_raw"), platform=r.get("platform"),
                                source_section=r.get("source_section") or r.get("section"))
    if not all_m:
        return False  # 本来就没配模型，属于"不限制"，不是"全禁"
    return not build_model_mapping(r.get("models_raw"), r.get("excluded"),
                                   r.get("platform"),
                                   source_section=r.get("source_section") or r.get("section"))


def build_header_overrides(headers, platform):
    """CPA headers -> credentials.header_overrides。

    返回 (可用的头, 被丢弃的头名)。校验规则与 sub2api 的
    normalizeHeaderOverrideEntry 保持一致：名字小写、只含合法 token 字符、
    不在黑名单、长度不超限、值里不能有控制字符。任一条不符就会被 400 拒掉
    整条账号，所以这里提前剔除而不是交给服务端报错。
    """
    if not isinstance(headers, dict) or not headers:
        return {}, []
    if platform not in HEADER_OK_PLATFORMS:
        return {}, sorted(str(k).lower() for k in headers)

    keep, drop = {}, []
    for k, v in headers.items():
        lk = str(k).strip().lower()
        sv = str(v).strip()
        # CPA 里以 $ 开头的值是**动态头**：$CPA-SESSION-ID 展开成 CPA 内部的
        # 会话亲和 ID，其他 $Name 表示"从下游客户端请求里复制 Name 头的值，
        # 客户端没发就整条省略"（header_helpers.go:155-188）。
        # sub2api 的 header_overrides 只能存静态值，照搬过去会把字面量
        # "$Name" 当成真值发给上游。必须丢弃。
        if sv.startswith("$"):
            drop.append(lk + "(动态值)")
            continue
        if not lk or lk in HEADER_BLACKLIST:
            drop.append(lk or "<空名>")
            continue
        if len(lk) > MAX_HEADER_NAME or not _HEADER_NAME_OK.match(lk):
            drop.append(lk)
            continue
        if len(sv) > MAX_HEADER_VALUE:
            drop.append(lk)
            continue
        # 值里不能含控制字符（服务端用 httpguts.ValidHeaderFieldValue 校验）
        if any(ord(c) < 32 or ord(c) == 127 for c in sv):
            drop.append(lk)
            continue
        keep[lk] = sv
    if len(keep) > MAX_HEADER_ENTRIES:
        for k in sorted(keep)[MAX_HEADER_ENTRIES:]:
            keep.pop(k, None)
            drop.append(k)
    return keep, drop


def build_temp_unschedulable(rse):
    """CPA request-scoped-errors -> sub2api credentials.temp_unschedulable_rules。

    返回 (规则列表, 无法迁移的说明列表)。

    对应关系与差异：
      - CPA 的 status 必须与上游状态码精确相等，sub2api 的 error_code 同样是
        精确相等（ratelimit_service.go:2556），可以一一对应。
      - CPA 的 match 是**大小写敏感**子串，sub2api 的 keywords 是**大小写不敏感**
        子串。这会让 sub2api 比 CPA 多匹配一些情况，方向是更容易冷却，不会漏。
      - 只有 *-and-cooldown 两个 action 真的触发冷却；stop / continue 只决定
        是否换凭据、是否中止请求，在 sub2api 里没有对应概念，建规则反而会造出
        CPA 侧不存在的冷却，所以跳过并记账。
      - match-regexr 是正则，sub2api 只支持纯子串，无法表达，记账。
      - 服务端对 error_code<=0 / keywords 空 / duration<=0 的规则是**静默丢弃**
        （account.go:460-462，返回 200 不报错），所以这里先自行挡掉。
      - 冷却时长按错误码分档（COOLDOWN_MINUTES_BY_CODE）：限流类沿用 CPA 的
        1 分钟，鉴权/余额类退避更久。理由见该常量的注释——sub2api 的冷却
        代价比 CPA 高，对坏死类凭据反复重试会拖低整体成功率。
      - 关键词里属于「客户端自己的问题」的那些会被摘掉
        （CLIENT_SIDE_KEYWORDS），比如上下文超长——换哪个凭据都一样失败，
        冷却只会白白减少可用账号。
      - CPA 没配但对中转站很关键的错误码会补上（EXTRA_COOLDOWN_RULES），
        目前是 401：key 被吊销时最常见的表现，CPA 侧完全没覆盖。
    """
    rules, lost = [], []
    for r in rse or []:
        if not isinstance(r, dict):
            continue
        action = str(r.get("action") or "").strip().lower()
        status = r.get("status")
        if not _is_int(status) or status <= 0:
            lost.append("status 非法的规则(action=%s)" % (action or "-"))
            continue
        if action not in COOLDOWN_ACTIONS:
            lost.append("action=%s 不触发冷却，sub2api 无对应语义" % (action or "-"))
            continue
        kws = [str(x) for x in (r.get("match") or []) if str(x).strip()]
        if r.get("match-regexr"):
            lost.append("status=%s 的 match-regexr 正则无法迁移：%s"
                        % (status, r.get("match-regexr")))
        # 摘掉「客户端自己的问题」那类关键词：换任何凭据都会同样失败，
        # 冷却只会白白减少可用账号。见 CLIENT_SIDE_KEYWORDS 的说明。
        kept, dropped_kw = [], []
        for k in kws:
            low = k.strip().lower()
            if any(c in low for c in CLIENT_SIDE_KEYWORDS):
                dropped_kw.append(k)
            else:
                kept.append(k)
        if dropped_kw:
            lost.append("status=%s 移除了与凭据健康无关的关键词（客户端侧问题，"
                        "冷却无意义）：%s" % (status, "、".join(dropped_kw)))
        kws = kept
        if not kws:
            continue
        mins = cooldown_minutes_for(status)
        desc = "源自 CPA request-scoped-errors（action=%s）" % action
        if mins != DEFAULT_COOLDOWN_MINUTES:
            # 写清楚为什么和 CPA 的 1 分钟不一样，免得以后有人对着上游代码
            # 核对时以为这里算错了。
            desc += "；按错误码 %s 退避 %d 分钟" % (status, mins)
        rules.append({"error_code": status, "keywords": kws,
                      "duration_minutes": mins,
                      "description": desc})

    # 补齐 CPA 没配、但对中转站账号很关键的错误码（见 EXTRA_COOLDOWN_RULES）。
    # 已经有同状态码规则的就不重复加——以 CPA 的配置为准。
    have = {x["error_code"] for x in rules}
    for extra in EXTRA_COOLDOWN_RULES:
        if extra["error_code"] in have:
            continue
        rules.append({"error_code": extra["error_code"],
                      "keywords": list(extra["keywords"]),
                      "duration_minutes": cooldown_minutes_for(extra["error_code"]),
                      "description": extra["description"]})
    return rules, lost


def _norm_prefix(v):
    """复刻 CPA 的 normalizeModelPrefix（config_normalization.go:313-323）。

    去首尾空白、去首尾斜杠；**含内部斜杠的整个丢弃**（CPA 是静默清空，
    嵌套前缀不被支持）。不做大小写变换——CPA 保留原样，且剥前缀时大小写敏感。
    """
    p = str(v or "").strip().strip("/")
    return "" if "/" in p else p


def collect(cfg_dict):
    """解析 CPA 配置，产出待导入记录。

    这里刻意复刻了 CPA 加载期会做的丢弃与去重，否则我们会导入一批
    CPA 自己根本不会加载的条目，两边行为对不上：
      - codex/xai：base-url 为空的条目被删（config_normalization.go:208-210）
      - openai-compatibility：base-url 为空的 provider 被删（:167-170）
      - gemini/interactions：api-key 和 base-url 都空的被删，随后按
        (api-key, base-url, proxy-url, prefix, headers) 去重（:237-273）
      - 所有段：prefix 含内部斜杠会被清空（:313-323）
      - xai：alpha-search 强制关闭（:192-194）
    返回 (记录列表, 丢弃说明列表)。
    """
    recs, dropped_notes = [], []
    gemini_seen = set()

    for section, (platform, acc_type, resp_mode) in SECTION_MAP.items():
        arr = cfg_dict.get(section)
        if not isinstance(arr, list):
            continue
        for idx, entry in enumerate(arr):
            if not isinstance(entry, dict):
                continue

            base_url = str(entry.get("base-url") or "").strip()
            api_key = entry.get("api-key")

            # --- 复刻 CPA 的加载期丢弃 ---
            if section in ("codex-api-key", "xai-api-key") and not base_url:
                dropped_notes.append("%s[%d] base-url 为空，CPA 不会加载" % (section, idx))
                continue
            if section == "openai-compatibility" and not base_url:
                dropped_notes.append("%s[%d] base-url 为空，CPA 不会加载" % (section, idx))
                continue
            if section in ("gemini-api-key", "interactions-api-key"):
                if not api_key and not base_url:
                    dropped_notes.append("%s[%d] api-key 和 base-url 都为空，CPA 不会加载"
                                         % (section, idx))
                    continue

            headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
            prefix = _norm_prefix(entry.get("prefix"))
            if entry.get("prefix") and not prefix:
                dropped_notes.append("%s[%d] prefix 含内部斜杠，CPA 会清空：%r"
                                     % (section, idx, entry.get("prefix")))

            if section in ("gemini-api-key", "interactions-api-key"):
                # CPA 的去重键不含 priority/weight/models 等，差异只在这些字段的
                # 条目会被折叠成第一条
                key = (str(api_key or ""), base_url,
                       str(entry.get("proxy-url") or ""), prefix,
                       json.dumps(headers, sort_keys=True, ensure_ascii=False))
                if key in gemini_seen:
                    dropped_notes.append("%s[%d] 与前面的条目重复，CPA 会去重折叠" % (section, idx))
                    continue
                gemini_seen.add(key)

            alpha = bool(entry.get("alpha-search"))
            if section == "xai-api-key":
                alpha = False  # CPA 强制关闭

            base = {
                "section": section, "platform": platform, "type": acc_type,
                "responses_mode": resp_mode, "group": SECTION_GROUP[section],
                # 模型选择策略要按**来源段**分流，不能只看 platform。
                # codex-api-key 与 openai-compatibility 的 platform 都是 "openai"，
                # 但需求对两者的模型口径不同：
                #   codex  -> 该凭据可用的 gpt-6 全系（单族，"就高"）
                #   openai -> 允许混合多种类型各自的最高级（多族，"就高"）
                # 以前靠"模型列表里有没有 gpt-6"猜，会把声明了 gpt-6 的
                # openai-compatibility 误当 codex 处理，砍掉其余全部模型。
                "source_section": section,
                "base_url": base_url, "prefix": prefix,
                "priority": entry.get("priority"), "proxy_url": entry.get("proxy-url") or "",
                "headers": headers,
                "models_raw": entry.get("models") or [],
                "rse": entry.get("request-scoped-errors") or [],
                "excluded": entry.get("excluded-models") or [],
                "websockets": bool(entry.get("websockets")), "alpha_search": alpha,
                "fingerprint": entry.get("fingerprint-profile") or "",
                "cloak": entry.get("cloak"),
                "provider_name": entry.get("name") or "",
                "rebuild_mid_system": entry.get("rebuild-mid-system-message"),
                "prompt_cache_key": entry.get("support-prompt-cache-key"),
                # experimental-cch-signing 在 CPA 里是确认的死字段（零运行时消费者，
                # config_types.go:435-437），不再记账
                "disable_cooling": entry.get("disable-cooling"),
                "request_retry": entry.get("request-retry"),
                "disabled": bool(entry.get("disabled", False)),
            }
            if section == "openai-compatibility":
                for sub in entry.get("api-key-entries") or []:
                    if isinstance(sub, dict) and sub.get("api-key"):
                        r = dict(base)
                        r["api_key"] = sub["api-key"]
                        r["weight"] = sub.get("weight")
                        # openai-compatibility 顶层没有 proxy-url，只在 key 级别有
                        r["proxy_url"] = sub.get("proxy-url") or ""
                        recs.append(r)
            elif api_key:
                r = dict(base)
                r["api_key"] = api_key
                r["weight"] = entry.get("weight")
                recs.append(r)
    return recs, dropped_notes


def remap_priority(recs):
    """CPA 优先级 -> sub2api 优先级（全局保序分桶映射 / dense rank）。

    方向是反的：CPA 数值大者优先，sub2api 数值小者优先
    （gateway_scheduling.go:1588 取最小值集合，account_repo.go:1994 ORDER BY ASC）。

    但"方向相反"只是表面，真正决定映射方式的是**两边都按优先级严格分桶**：
      · CPA  ：auth/scheduler.go:413-416 先求 highestReadyPriority，
               再 pickReadyAtPriority(bestPriority)；selector.go:572 只取
               availableByPriority[bestPriority] 这一桶。
      · sub2api：gateway_scheduling.go:741 的 filterByMinPriority 只保留
               priority 最小的那批账号，桶内再按负载/LRU 选。
    也就是说优先级数值**不是权重**，而是桶号：同值的账号在一个桶里共同承载流量，
    只有整桶不可用才会降到下一桶。

    因此映射必须满足两条硬性要求：
      1. **同一上游域名的多个 KEY 必须同桶**。它们是彼此的备份：某个 KEY
         余额耗尽或被封号时，同域名的其余 KEY 要能立刻顶上。若被拆进不同
         的桶，sub2api 只调度最小的那个，其余全部闲置，等于把"多 KEY 冗余"
         退化成"单 KEY 单点"。这里按 (渠道, 域名) 归组、组内取最高 priority
         作为桶序位（见 _host_tier_map），不依赖用户在 CPA 里恰好把同域名的
         KEY 配成同一个 priority。
      2. **全局统一映射**。CPA 的 bestPriority 是跨 provider 比较的
         （scheduler.go:416 在 provider 循环外求全局最大），所以优先级数值
         在整份配置里是可比的。若按渠道各算一套排名，A 渠道的 priority=224
         和 B 渠道的 priority=38 会被映射到同一个桶号，跨渠道语义就丢了。
      3. **同渠道不同域名桶号必须不同**（2026-09-13 新增约束）。若两个不同
         域名映射到同一桶号，sub2api 会混调两个域名的 KEY，失去"整桶不可用
         才降级"的容灾特性。步长 10 不够时，相同序位的域名按域名字典序微调
         桶号（+0/+1/+2...），保证同渠道不同域名桶号唯一。

    早期版本按"渠道内排名×10"映射，等于给每条账号发一个独立桶号。实测
    226 条凭据只有 47 个不同的 priority 值（最大一桶 15 条），那样映射后
    每个渠道只有排名第一的那条能被调度，222/226 条永久闲置——这是本工具
    历史上最严重的一个缺陷，也是上游 524 超时的主因。

    做法：先算出每个 (渠道, 域名) 单元的序位，收集全配置出现过的序位去重、
    降序，第 k 大的映射为 k*10。步长 10 留出人工插桶的空间。未显式配置
    priority 的按 0 处理（CPA 侧 Go 零值也是 0，语义一致）。

    并列打破用完整 api_key 的哈希而不是前 8 个字符——实测 226 条里有 10 条
    前三个排序键完全并列，用前缀等于顺序由输入位置决定。这里仍保留稳定排序，
    但它只影响 rank 字段（对照表展示用），不再影响 new_priority。
    """
    # ---- 第一步：建立全局的 CPA priority -> sub2api priority 映射表 ----
    # 分桶序位不直接用单条记录的 priority，而是用"该(渠道,域名)组内的最高
    # priority"，见 _host_tier_of 的说明：同一个上游域名下的多个 KEY 必须落在
    # 同一个桶里才能互为备份、轮流承载流量。
    tier_of = _host_tier_map(recs)
    seen = set(tier_of.values())
    # CPA 数值大者优先 -> 降序枚举；sub2api 数值小者优先 -> 依次给 10,20,30...
    ordered = sorted(seen, reverse=True)
    bucket_of = {}
    for i, v in enumerate(ordered, 1):
        bucket_of[v] = i * 10

    # ---- 第二步：全局定序，保证桶号全局唯一 ----
    #
    # 关键：排序与去重都必须是**全局**的，不能按渠道各算一套。
    #
    # 为什么（2026-09-15 实测发现）：CPA 里多个不同域名被配成同一个
    # priority 是常态（比如都写 1000）。此时 tier_of 给它们同一个序位值，
    # `bucket_of` 就把它们映射到同一个桶号。而原先的防碰撞微调写在
    # `for group, items in by_group.items()` 循环**内部**，只能保证
    # "同渠道内不撞" —— 跨渠道照撞不误。
    # 端到端实测：claude-a.example.com / codex-c.example.com /
    # openai-d.example.com 三个不同渠道的不同域名全部拿到桶号 10，
    # 被 sub2api 当成同一桶轮循，跨域名降级容灾完全失效。
    #
    # 现在改成：把所有 (渠道, 域名) 单元按 (序位降序, 渠道名, 域名字典序)
    # 全局排序后依次编号，桶号全局唯一。排序后两项只为确定性
    # （同一份配置多次运行得到同样的桶号），不参与优先级语义。
    all_units = sorted(tier_of.keys(),
                       key=lambda k: (-tier_of[k], k[0], k[1]))
    adjusted_bucket = {}
    for i, k in enumerate(all_units, 1):
        adjusted_bucket[k] = i * 10

    # 保留基础映射表：仅用于日志/回溯"某个 CPA 序位被映射到了哪个基准桶"。
    bucket_of = {}
    for i, v in enumerate(sorted(set(tier_of.values()), reverse=True), 1):
        bucket_of[v] = i * 10

    # ---- 第三步：落到每条记录，并保留渠道内排名用于对照表 ----
    by_group_recs = defaultdict(list)
    for r in recs:
        by_group_recs[r["group"]].append(r)
    for arr in by_group_recs.values():
        arr.sort(key=lambda r: (-(r["priority"] if _is_int(r["priority"]) else 0),
                                host_of(r["base_url"]),
                                account_fingerprint(r)))
        for rank, r in enumerate(arr, 1):
            r["new_priority"] = adjusted_bucket[_host_key(r)]
            r["rank"] = rank

    # 返回基础映射表（仅用于日志，实际桶号已微调）
    return bucket_of


def _host_key(r):
    """一条记录所属的"上游单元"：(渠道, 域名)。

    带上渠道是因为同一个域名可能同时出现在多个渠道里（实测有单个域名同时
    出现在 4 个渠道的情况），而不同渠道在 sub2api 侧是不同分组、各自独立
    调度，不该被合成一个桶来排序。
    """
    return (r["group"], host_of(r["base_url"]))


def _host_tier_map(recs):
    """算出每个 (渠道, 域名) 单元的分桶序位 = 该单元内最高的 CPA priority。

    为什么不直接用每条记录自己的 priority：
      调度是严格分桶的（见 remap_priority 的说明），桶号相同的账号才会
      互为备份、共同承载流量。而"同一个上游域名下的多个 API KEY"恰恰是
      最应该互为备份的一组——其中某个 KEY 余额耗尽或被封号时，其余 KEY
      必须能立刻顶上，而不是让整个域名失效。
      如果直接按每条记录的 priority 分桶，就只能寄希望于用户在 CPA 里
      始终给同域名的 KEY 配一样的 priority。实测当前配置确实如此
      （46/46 个域名内部 priority 完全一致），但这是个隐式约定：
      一旦某个 KEY 的 priority 被单独调过，它就会被拆进独立的桶，
      于是该域名下要么只有它一个在干活、要么它永远闲置——而界面上
      这些账号全部显示"正常"，故障完全静默。

    取组内最高值而不是最低/平均：priority 表达的是"多想优先用它"，
    同组内既然要合并成一个桶，就应该按这组里最强的意图来排序，
    否则合并会意外降低整组的调度顺位。

    返回 {(渠道, 域名): 序位}。
    """
    tier = {}
    for r in recs:
        k = _host_key(r)
        p = r["priority"] if _is_int(r["priority"]) else 0
        if k not in tier or p > tier[k]:
            tier[k] = p
    return tier


def check_priority_drift(recs):
    """检查同一 (渠道, 域名) 内的 CPA priority 是否一致，不一致就报告。

    _host_tier_map 会把这种情况按组内最高值统一处理，保证同域名的 KEY
    仍然同桶轮循——但"自动兜住"不等于"应该不出声"：priority 不一致
    通常意味着配置写错了（比如本想调整整组却只改了一条），用户有权知道
    工具替他做了归一化。

    返回 [(渠道, 域名, KEY 数, [不同的 priority 值...]), ...]。
    """
    byhost = defaultdict(list)
    for r in recs:
        byhost[_host_key(r)].append(r)
    out = []
    for (g, h), arr in sorted(byhost.items()):
        ps = sorted({r["priority"] if _is_int(r["priority"]) else 0 for r in arr},
                    reverse=True)
        if len(ps) > 1:
            out.append((g, h, len(arr), ps))
    return out


def health_rerank_priority(recs, have_map, cfg):
    """按 sub2api 侧的实测健康度重排优先级，覆盖 remap_priority 的 CPA 照搬值。

    为什么要这一层（文档第 2 条⑶）：
      remap_priority 忠实映射 CPA 的 priority 数值，但那只是**用户当初的意图**，
      不是**上游此刻的实际表现**。config.yaml 里排在前面的站可能早就余额耗尽、
      被封号或超时严重，而被用户手工停用的站反而成功率最高 —— 照搬等于把
      "谁应该优先"这件事冻结在配置写下的那一刻。

      这里改用 sub2api 自己记录的运行状态重排：它是网关每一次真实请求的结果，
      比任何静态配置都准。

    健康分数（文档给的公式）：
        health = 可调度比例 × 0.6 + 活跃比例 × 0.4

      · 可调度比例 = schedulable=true 的 KEY 数 / 该域名的 KEY 总数
      · 活跃比例   = status=active   的 KEY 数 / 该域名的 KEY 总数

      两项都取"比例"而不是"绝对条数"：KEY 多的域名不该仅因为基数大就排前面。
      可调度权重更高（0.6）是因为它更严格 —— sub2api 出错时会同时关掉
      status 和 schedulable（account_repo.go:1750），而恢复路径 ClearError
      只还 status 不还 schedulable，所以 schedulable=false 往往意味着
      "这个 KEY 栽过跟头且没人管过它"，比单纯的 status 更能说明问题。

    三条约束（与 remap_priority / 文档第 5 条一致，不能破坏）：
      1. 同 (渠道, 域名) 的所有 KEY 必须同桶 —— 它们互为备份，拆桶就退化成单点。
      2. 不同域名的桶号必须互不相同 —— **全局**唯一，不限于同渠道内。
         相同就意味着两个域名混在一桶里轮循，失去了"整桶挂掉才降级到下一个
         域名"的分层容灾；跨渠道撞桶还会让两个不同类型的站互相抢占调度。
      3. 桶号步长保持 10，与 remap_priority 一致，留出人工插桶空间。

    冷启动（文档第 2 条⑶最后一段特别要求）：
      首次导入时 sub2api 侧一条账号都没有，have_map 为空，健康分数全是 0，
      重排会把所有域名打成同一个分数、顺序退化成任意。这种情况下**不重排**，
      直接沿用 remap_priority 的 CPA 值 —— 那虽然只是意图，但至少是有信息的
      排序。等第一轮导入跑完、网关积累了真实状态，第二次运行才会生效。
      函数返回 None 表示"本次未重排"，调用方据此打印提示。

    返回 (重排的域名单元数, 说明文本) 或 (0, 原因) —— 后者表示跳过。
    """
    if not cfg.get("health_rerank_enabled", True):
        return 0, "health_rerank_enabled=false，按配置跳过健康度重排"
    if not have_map:
        return 0, ("sub2api 侧还没有任何账号（首次导入），"
                   "健康度无从谈起，本次沿用 CPA 的优先级；"
                   "导入完成后再运行一次即可按实测健康度重排")

    # ---- 第一步：按 (渠道, 域名) 归集 sub2api 侧的实际状态 ----
    # 用账号名关联：本工具建号时名字是确定性的（assign_names + account_fingerprint），
    # 所以 recs 里的 name 能直接在 have_map 里查到对应的线上账号。
    stat = defaultdict(lambda: {"total": 0, "sched": 0, "active": 0, "known": 0})
    for r in recs:
        k = _host_key(r)
        st = stat[k]
        st["total"] += 1
        cur = have_map.get(r["name"])
        if not cur:
            # 这条还没导入过（新增的 KEY）。不计入分子也不计入 known ——
            # 用"已知条目"当分母，避免新增 KEY 把老域名的健康度稀释掉。
            continue
        st["known"] += 1
        if cur.get("schedulable"):
            st["sched"] += 1
        if str(cur.get("status") or "").lower() == "active":
            st["active"] += 1

    # ---- 第二步：算健康分数 ----
    health = {}
    for k, st in stat.items():
        n = st["known"]
        if not n:
            # 整个域名都是新增的，没有任何实测数据。给中位分 0.5：
            # 既不因"查无记录"被打到队尾（新站可能很好），
            # 也不凭空排到已验证的健康站前面。
            health[k] = 0.5
            continue
        health[k] = (st["sched"] / float(n)) * 0.6 + (st["active"] / float(n)) * 0.4

    # ---- 第三步：全局统一定序 ----
    # 文档第 2 条⑶④："同一个类型不同域名的优先级一定要不同，哪怕算出来相同，
    # 也要适当做点微调给出点偏差"；文档第 5 条："全局 dense rank 映射"。
    #
    # 这里**必须**是全局排名，不能按渠道各自从 1 开始编号。以前是
    # `for group: for i, k in enumerate(keys, 1): bucket = i * 10`，
    # 于是每个渠道的"第 1 名"都是 10、"第 2 名"都是 20 —— 跨渠道必然撞桶，
    # 两个不同渠道的不同域名被发到同一个 priority 上，sub2api 的
    # filterByMinPriority 会把它们当成同一桶轮循，正是文档第 5 条要防的场景
    # （remap_priority 的 docstring 也专门论证过"跨渠道必须可比"）。
    #
    # 排序键：健康分降序 -> 渠道名 -> 域名字典序。
    # 后两项只为确定性（同一份配置多次运行得到同样的桶号），不参与优先级语义。
    all_keys = sorted(health.keys(),
                      key=lambda k: (-health[k], k[0], k[1]))
    bucket_of = {}
    for i, k in enumerate(all_keys, 1):
        bucket_of[k] = i * 10          # 步长 10，留出人工插桶空间

    # 把桶号回填到每条记录。原先是嵌套在排序循环里、每条记录都要重算一次
    # _host_key，成了 O(域名数 × 记录数)；这里改成先建索引再一次赋值。
    ranked = 0
    for k in all_keys:
        b = bucket_of[k]
        for r in recs:
            if _host_key(r) == k:
                r["new_priority"] = b
                r["health_score"] = round(health[k], 4)
        ranked += 1

    by_group = defaultdict(list)
    for k in all_keys:
        by_group[k[0]].append(k)

    detail = []
    for group, keys in sorted(by_group.items()):
        parts = ["%s(%.2f)" % (k[1], health[k]) for k in keys[:4]]
        if len(keys) > 4:
            parts.append("…共 %d 个域名" % len(keys))
        detail.append("%s：%s" % (group, " > ".join(parts)))
    return ranked, "按实测健康度重排 %d 个域名单元（全局统一定序）。%s" % (
        ranked, "；".join(detail))


def apply_weight_zero_priority(recs, cfg):
    """把 CPA 里 weight<=0 的凭据降到本渠道末尾，而不是建成停用。

    背景：CPA 在 routing.strategy=weighted-round-robin 下确实不选零权重的凭据
    （selector.go:662-670），但这是**上游随时可以调回来的临时状态**，不是凭据坏死。
    如果照搬成 sub2api 的 status=inactive，就会掉进不可逆的坑：sub2api 把账号
    打到 error 时会同一次写入连带关掉调度开关（account_repo.go:1750），而恢复
    路径 ClearError 只还 status、不还 schedulable，于是需要人工逐行重开。

    改成降优先级则完全可逆：sub2api 数值小者优先，把它们顶到全局最大桶号之后，
    正常账号全部排在前面，零权重的只在前面都不可用时才会被选中——既保留了
    CPA 的调度意图，又不需要任何人工恢复动作。

    注意降级必须**整桶平移**而不是逐条递增：sub2api 的 filterByMinPriority
    只调度优先级最小的那一桶，若把 N 条零权重账号发成 N 个不同的桶号，
    就只有其中一条真正可用，其余白白闲置（同 remap_priority 的同值同桶要求）。
    这里按原 new_priority 分组，同桶的一起平移、桶间保持相对次序。

    只在 weight_zero_policy="deprioritize"（默认）时生效；设成 "inactive"
    则交回 needs_inactive 走老的停用路径。

    返回被降级的账号条数。
    """
    if cfg.get("weight_zero_policy", "deprioritize") != "deprioritize":
        return 0
    if not cfg.get("respect_weight_zero", True):
        return 0
    # 零权重只在 weighted-round-robin 下才真的被 CPA 排除，其余策略照常参与调度，
    # 这时降级反而是凭空改变调度顺序，不能做。
    if cfg.get("routing_strategy") != "weighted-round-robin":
        return 0

    zeros = [r for r in recs
             if _is_int(r.get("weight")) and r["weight"] <= 0]
    if not zeros:
        return 0

    # 基准取**全局**最大桶号：remap_priority 的映射表是全局统一的，
    # 按渠道各取一次最大值会让不同渠道的降级桶号互相穿插。
    base = max((r.get("new_priority") or 0) for r in recs)

    # 同一原桶号的零权重账号必须继续同桶，只做整体平移。
    tiers = sorted({(r.get("new_priority") or 0) for r in zeros})
    shift = {old: base + (i + 1) * 10 for i, old in enumerate(tiers)}
    for r in zeros:
        r["new_priority"] = shift[r.get("new_priority") or 0]
        r["weight_zero_deprioritized"] = True
    return len(zeros)


def assign_names(recs):
    """生成账号名。

    名字里带内容指纹而不是位置序号：去重是按名字比对的，位置序号会让
    "从 config 删掉一条" 变成 "后面所有账号张冠李戴"（详见 account_fingerprint）。
    """
    for r in recs:
        stem = "%s-%s" % (r["prefix"] or r["platform"][:3].upper(), host_of(r["base_url"]))
        r["fp"] = account_fingerprint(r)
        r["name"] = "%s-%s" % (stem, r["fp"])


def needs_inactive(r, cfg):
    """这条账号是否应该建成停用。

    CPA 里 weight<=0 **只在 routing.strategy 是 weighted-round-robin 时**才被
    排除（selector.go:662-670，scheduler.go:329 的 requirePositiveWeight）；
    在 round-robin / fill-first 下零权重照常参与调度。所以必须看全局策略，
    不能一律当成停用。openai-compatibility 段的 disabled 则是无条件的硬删除。

    注意 sub2api 的创建接口**没有** status 字段，这里只负责判定，
    实际停用由 do_import 在建完之后补一次 PUT。

    停用要尽量少判：sub2api 把账号打到 status=error 时会**同一次写入**
    连带关掉调度开关（account_repo.go:1750 SetError 里 SetStatus(StatusError)
    紧跟 SetSchedulable(false)），而恢复路径 ClearError 只还 status、
    **不还 schedulable**（全仓 SetSchedulable(true) 在生产代码里零调用点）。
    也就是说每一条被判成停用的账号，之后都可能卡在
    "status=active + schedulable=false" 这个人工才能解开的状态里。
    判定越保守，需要人工逐行重开的行就越少。
    """
    if r.get("disabled"):
        return True
    # excluded-models 把模型排干净的，在 CPA 里等于这条凭据不提供任何服务。
    # 默认仍建成停用（语义忠实），但可用 zero_model_policy="active" 改成启用：
    # 此时拦流量靠 to_account 写的 model_mapping 白名单哨兵，而不靠 status，
    # 这样 CPA 以后放开 excluded-models 时只要重跑一次同步就能自动生效。
    if serves_nothing(r):
        return cfg.get("zero_model_policy", "inactive") != "active"
    if not cfg.get("respect_weight_zero", True):
        return False
    if cfg.get("routing_strategy") != "weighted-round-robin":
        return False
    w = r.get("weight")
    if not (_is_int(w) and w <= 0):
        return False
    # 零权重是可恢复的临时状态（上游随时可能把权重调回来），默认不建成停用，
    # 改为 remap_priority 之后再降一档优先级（见 apply_weight_zero_priority）。
    return cfg.get("weight_zero_policy", "deprioritize") == "inactive"


def to_account(r, cfg, group_id_map, proxy_id_map, proxy_need_map):
    """把一条 CPA 记录转成 sub2api 的创建账号请求体。

    第二个参数是 plan["config"]（default_concurrency / respect_weight_zero 等），
    不是 设置.json 的全量 settings。

    proxy_need_map: {域名: 代理URL} —— 从 CPA 已有 proxy_url 反推出的域名级代理需求。
    """
    creds = {"api_key": r["api_key"]}

    # ---- 池模式 ----
    # 中转站账号本质就是「一批可互换的 key」，正是 sub2api 说的池模式。
    # 打开它有两个收益：
    #   1. OpenAI 平台的 API Key 健康熔断器只认池模式账号
    #      （openai_apikey_health_breaker.go:19 要求
    #       platform==openai && type==apikey && IsPoolMode()），
    #      不开的话那个熔断器配了也永远不触发；
    #   2. 四个平台都会启用「同账号重试」，瞬时抖动不必换号重来。
    #
    # 但默认重试状态码是 [401, 403, 429]（account.go:1133），对中转站是有害的：
    # 401/403 意味着这个 key 被吊销或封禁，在同一个 key 上再试 3 次
    # （account.go:1087 默认次数）纯属浪费时间，还会和我们写的
    # 「401/403 冷却 30 分钟」规则互相打架——重试期间那条规则还没生效。
    # 所以显式覆盖成只对「真正瞬时」的错误重试。
    if cfg.get("pool_mode_enabled", True):
        creds["pool_mode"] = True
        codes = cfg.get("pool_mode_retry_status_codes")
        if codes is None:
            codes = list(POOL_MODE_RETRY_CODES)
        if codes:
            creds["pool_mode_retry_status_codes"] = list(codes)
        n = cfg.get("pool_mode_retry_count", 2)
        if _is_int(n) and n >= 0:
            creds["pool_mode_retry_count"] = int(n)
    if r["base_url"]:
        creds["base_url"] = r["base_url"]
    # excluded-models 通过从白名单里减掉来实现：sub2api 没有模型黑名单字段
    mm = build_model_mapping(r["models_raw"], r.get("excluded"), r.get("platform"),
                             source_section=r.get("source_section") or r.get("section"))
    if mm:
        creds["model_mapping"] = mm
    elif serves_nothing(r):
        # 全部模型都被排除。空表在 sub2api 里等于放行全部，必须放占位项，
        # 否则这条在 CPA 已被完全禁用的凭据会在 sub2api 上接管所有流量。
        creds["model_mapping"] = {BLOCK_ALL_SENTINEL: BLOCK_ALL_SENTINEL}
    hdrs, dropped = build_header_overrides(r["headers"], r["platform"])
    if hdrs:
        creds["header_override_enabled"] = True
        creds["header_overrides"] = hdrs
    rules, cd_lost = build_temp_unschedulable(r["rse"])
    # 再叠加「域名级」冷却规则：见 DOMAIN_SCOPED_COOLDOWN_CODES 的说明。
    # 402 这类错误是**整个预算池**耗尽，不是单个 KEY 的毛病，同域名的其余
    # KEY 不一起冷却就会一个个撞上去，把 Cloudflare 的 120 秒预算耗光。
    #
    # 去重：build_temp_unschedulable 也会为 402 生成一条（60 分钟），
    # 与域名级的 180 分钟冲突。同一个 error_code 出现两条规则时，服务端
    # 取哪条是不确定的，所以这里显式以"域名级"那条为准（它退避更久、
    # 关键词更特异），把通用那条摘掉。
    domain_rules = build_domain_scoped_cooldown(r["rse"])
    if domain_rules:
        scoped_codes = {d["error_code"] for d in domain_rules}
        rules = [x for x in rules if x.get("error_code") not in scoped_codes]
        rules = rules + domain_rules
    if rules:
        creds["temp_unschedulable_enabled"] = True
        creds["temp_unschedulable_rules"] = rules
    if r["alpha_search"]:
        creds["openai_capabilities"] = {"alpha_search": True, "chat_completions": True}

    extra = {}
    if r["responses_mode"]:
        extra["openai_responses_mode"] = r["responses_mode"]
    if r["websockets"]:
        extra["openai_ws_enabled"] = True
    # TLS fingerprint 迁移（所有平台支持）
    # 2026-09-13 修复：原先只对 anthropic 启用，现改为所有平台通用
    if r["fingerprint"]:
        extra["enable_tls_fingerprint"] = True
        # CPA 的 fingerprint-profile 是字符串标识，sub2api 用数字 ID
        # 将常见 profile 映射到 sub2api 的 profile ID（需要先在 sub2api 创建）
        # 这里先启用功能，profile_id 默认 0 表示使用内置 profile
        # 用户可以在 sub2api 界面手动绑定具体 profile

    # concurrency 必须显式给：省略会被写成 0，而 0 在 sub2api 里等于
    # "并发无上限"（concurrency_service.go:344），不是 Ent 默认的 3。
    conc = cfg.get("default_concurrency", 3)
    try:
        conc = int(conc)
    except (TypeError, ValueError):
        conc = 3
    if conc <= 0:
        conc = 3

    acc = {"name": r["name"], "platform": r["platform"], "type": r["type"],
           "credentials": creds, "priority": r["new_priority"],
           "concurrency": conc, "confirm_mixed_channel_risk": True}

    # load_factor：sub2api 的负载因子（account_handler.go CreateAccountRequest
    # LoadFactor *int）。值越大该账号越容易被选中承载流量，用于在同桶内
    # 按健康度做二次分配 —— 桶号决定"哪一批账号参与调度"，load_factor 决定
    # "这批里谁多干活"。健康分高的多分担，低的少分担，比单纯轮循更贴近实际。
    #
    # 取值按健康分映射（health_score 由 health_rerank_priority 写入记录）：
    #   健康分 >= 0.9 -> 8（几乎全可调度且活跃，可以多接）
    #   健康分 >= 0.7 -> 6
    #   健康分 >= 0.5 -> 4（含新域名的中位分 0.5）
    #   健康分 <  0.5 -> 2（栽过跟头的，少接但不隔离）
    # 没有 health_score（首次导入/未重排）时不发该字段，用 sub2api 自己的默认值。
    hs = r.get("health_score")
    if hs is not None:
        try:
            hs = float(hs)
            acc["load_factor"] = 8 if hs >= 0.9 else 6 if hs >= 0.7 else 4 if hs >= 0.5 else 2
        except (TypeError, ValueError):
            pass

    # auto_pause_on_expired 必须显式写。sub2api 的 schema 默认是 true
    # （ent/schema/account.go:139 Default(true)，由 sync_constants 从源码
    # 读进 ACCOUNT_DEFAULTS），不传就吃这个默认，界面上表现为
    # "过期自动暂停调度"已勾选——而 CPA 侧没有过期概念、我们也不写
    # expires_at，勾着它只会在将来某天莫名其妙把账号暂停掉。
    # CreateAccountRequest 有该字段（*bool），所以能显式关掉。
    apoe = cfg.get("auto_pause_on_expired", False)
    acc["auto_pause_on_expired"] = bool(apoe)

    if extra:
        acc["extra"] = extra
    gid = group_id_map.get(r["group"])
    if gid:
        acc["group_ids"] = [gid]
    pid = proxy_id_map.get(r["proxy_url"]) if r["proxy_url"] else None
    if pid:
        acc["proxy_id"] = pid

    # leftover 必须在这里就建好。它原先是写在下面"注意：这里不写 status"
    # 那段之后的，但上面第 1751-1760 行的"同域名代理跟进"分支会先往里写
    # "需要TLS代理但未配置"，于是当 proxy_need_map 命中同域名、而
    # proxy_id_map 里查不到对应代理 id（建代理失败或该代理没建出来）时，
    # 会抛 UnboundLocalError 并被 do_import 的 except 吞成"批次执行异常"，
    # 整批账号写入失败。py_compile 和 import 都测不出来，只在运行时炸。
    leftover = {}

    # 2026-09-13：同域名下若已有条目配了代理，新条目自动跟进。
    # 为什么不能靠域名白名单判断："这个站要不要走代理" 取决于
    # **本机出口 IP 有没有被它拦截**，不取决于域名是谁。
    # 可靠的依据只有一个：用户已经在 CPA 里为哪些条目配了 proxy_url。
    if not pid:
        host = host_of(r.get("base_url") or "")
        if host and host in proxy_need_map:
            need_url = proxy_need_map[host]
            need_pid = proxy_id_map.get(need_url)
            if need_pid:
                acc["proxy_id"] = need_pid
                pid = need_pid
            else:
                leftover["需要TLS代理但未配置"] = r["base_url"]

    # 注意：这里**不写** status。CreateAccountRequest 没有该字段，写了会被
    # Gin 静默丢弃（服务端一律建成 active），反而让人以为已经停用。

    w = r["weight"]
    if _is_int(w):
        leftover["weight"] = w
    if r["prefix"]:
        leftover["prefix"] = r["prefix"]
    if r.get("provider_name"):
        leftover["provider_name"] = r["provider_name"]
    if r["excluded"]:
        # 已经通过白名单裁剪生效了，记一笔便于核对
        leftover["excluded_models_已生效"] = r["excluded"]
    if r["proxy_url"] and not pid:
        leftover["proxy_url_未绑定"] = r["proxy_url"]
    if r["fingerprint"]:
        leftover["fingerprint_已迁移至enable_tls_fingerprint"] = r["fingerprint"]
    if dropped:
        leftover["被丢弃的请求头"] = sorted(set(dropped))
    if cd_lost:
        leftover["冷却规则未迁移项"] = cd_lost
    if r.get("cloak"):
        # 2026-09-13：cloak 是 CPA 的请求伪装功能（改写 User-Agent/Referer 等）
        # sub2api 的 extra 字段支持自定义 JSON，这里迁移过去供后续扩展
        # 注意：当前 sub2api 网关层未实现 cloak 消费逻辑，写入后暂时不生效
        extra["cloak"] = r["cloak"]
        leftover["cloak_已写入extra但网关暂未实现"] = r["cloak"]
    for k in ("rebuild_mid_system", "prompt_cache_key", "cch_signing",
              "disable_cooling", "request_retry"):
        if r.get(k) not in (None, False, ""):
            leftover[k] = r[k]

    # notes 以固定 sentinel 开头。回滚/体检靠它精确认定"本工具导入的账号"，
    # 而不是"备注里出现 CPA 字样"——后者会把用户手工建的账号也算进删除范围。
    notes = "%s 源自 CPA %s｜原优先级=%s｜fp=%s" % (
        NOTE_SENTINEL, r["section"], r["priority"], r.get("fp", ""))
    if leftover:
        notes += "｜未导入项=" + json.dumps(leftover, ensure_ascii=False)
    acc["notes"] = notes[:2000]
    return acc


def group_payloads(recs, s):
    out = {}
    for r in recs:
        g = r["group"]
        if g not in out:
            out[g] = {"name": "%s-%s" % (s["group_prefix"], g),
                      "description": "从 CPA %s 渠道迁移的账号" % g,
                      "platform": GROUP_PLATFORM.get(g, "anthropic"),
                      "rate_multiplier": 1.0,
                      # 必须显式传 true。
                      # CreateGroupRequest 里这个字段是**裸 bool** 而不是 *bool
                      # （group_handler.go:194），所以不传就是 Go 零值 false，
                      # 会把数据库 schema 的 Default(true)（migrations/221）覆盖掉。
                      # 它控制「是否按上下文长度启用官方的长上下文阶梯价」，
                      # 关掉意味着超长上下文请求按短上下文的单价计费——对用户是少收，
                      # 对账目是错的，而且界面上看不出这个默认被动过。
                      "long_context_pricing_enabled": True}
    return out


def distinct_proxies(recs):
    """去重出需要创建的代理。

    sub2api 的 CreateProxyRequest 里 name 是必填（proxy_handler.go:30），
    协议只接受 http/https/socks5/socks5h。
    """
    ok_proto = {"http", "https", "socks5", "socks5h"}
    default_port = {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}
    out, skipped = {}, []
    for r in recs:
        u = r.get("proxy_url")
        if not u or u in out:
            continue
        # CPA 的特殊字面量：direct / none 表示显式直连，不是代理地址
        if str(u).strip().lower() in ("direct", "none"):
            continue
        try:
            p = urlparse(u)
        except Exception as ex:
            skipped.append("%s 解析失败（%s），相关账号将直连" % (u, ex))
            continue
        if not p.hostname:
            skipped.append("%s 没有主机名，相关账号将直连" % u)
            continue
        proto = (p.scheme or "http").lower()
        if proto not in ok_proto:
            skipped.append("%s 协议 %s 不被 sub2api 支持，按 http 处理" % (u, proto))
            proto = "http"
        try:
            port = p.port
        except ValueError:
            skipped.append("%s 端口非法，相关账号将直连" % u)
            continue
        if not port:
            # 补协议默认端口，而不是整条丢掉：sub2api 的 port 是必填且 1..65535
            port = default_port[proto]
        spec = {
            "name": "CPA-%s-%d" % (p.hostname, port),
            "protocol": proto,
            "host": p.hostname,
            "port": int(port),
        }
        if p.username:
            spec["username"] = p.username
        if p.password:
            spec["password"] = p.password
        out[u] = spec
    return out, skipped


def build_proxy_need_map(recs):
    """从 CPA 配置里**已有的 proxy-url** 反推每个域名是否需要代理。

    为什么不能靠域名白名单判断（2026-09-13 推翻了上一版实现）：
      "这个站要不要走代理" 取决于**本机出口 IP 有没有被它拦截**，不取决于
      域名是谁。同一个站，机房 A 直连可达、机房 B 被 Cloudflare 拦 403 ——
      这正是 mihomo 存在的理由。把 api.anthropic.com 之类写进白名单，
      既在中转站场景下永不命中（config.yaml 里全是中转站域名），
      又会在上游换地址时静默失效，属于文档明令禁止的硬编码。

    可靠的依据只有一个：**用户已经在 CPA 里为哪些条目配了 proxy-url**。
    那是实测结果 —— 配了就说明当初直连不通。同域名下只要有任意一条配了
    代理，就说明这个域名从本机出口需要代理，同域名的其余条目（可能是后加的、
    还没来得及配）也应该跟着走同一个代理。

    返回 {域名: 代理URL}。只收录真正的代理地址 —— CPA 用 direct/none
    表示"显式直连"，那是"确认不需要代理"，不能当成代理地址写进去。
    """
    need = {}
    for r in recs:
        u = (r.get("proxy_url") or "").strip()
        if not u or u.lower() in ("direct", "none"):
            continue
        host = host_of(r.get("base_url") or "")
        if host and host not in need:
            need[host] = u
    return need


def distinct_fingerprints(recs):
    """去重出所有 TLS 指纹模板名称。

    CPA 里只存名字，sub2api 需要完整参数才能建模板，
    所以这里只收集名字列表，实际创建需要人工配置。
    """
    names = set()
    for r in recs:
        fp = r.get("fingerprint")
        if fp and isinstance(fp, str):
            names.add(fp)
    return sorted(names)


# =============================================================================
# 功能：各菜单项
# =============================================================================
def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        print("\n  缺少 PyYAML。请关掉本窗口，重新双击『迁移工具.cmd』，它会自动安装。")
        return None


def act_settings(s):
    print("\n" + "=" * 60)
    print(" 设置 sub2api 连接")
    print("=" * 60)
    print(" 直接回车 = 保留当前值。\n")
    cur = s["sub2api_base_url"]
    v = input(" sub2api 地址 [%s]：" % cur).strip()
    if v:
        s["sub2api_base_url"] = v.rstrip("/")
    print(" 当前管理密钥：%s" % mask(s["sub2api_admin_key"]))
    print(" 提示：这是 admin- 开头的那个，在 VPS 上用这条取：")
    print('   docker exec -i sub2api-postgres psql -U sub2api -d sub2api \\')
    print("     -tAc \"SELECT value FROM settings WHERE key='admin_api_key';\"")
    v = input(" 粘贴 sub2api 管理密钥（回车跳过）：").strip()
    if v:
        s["sub2api_admin_key"] = v
    save_settings(s)
    print("\n 已保存到 设置.json")


def act_test(s):
    print("\n" + "=" * 60)
    print(" 测试")
    print("=" * 60)
    # CPA
    if os.path.exists(CONFIG_YAML):
        n = sum(1 for _ in io.open(CONFIG_YAML, encoding="utf-8", errors="replace"))
        print(" [CPA] 本地 config.yaml 存在，共 %d 行  [OK]" % n)
    else:
        print(" [CPA] 未发现本地 config.yaml，将走联网模式")
        if not s.get("cpa_management_key"):
            print("       但没配置 CPA 密钥，联网会失败。把 config.yaml 放进来最省事。")
    # sub2api
    if not s["sub2api_admin_key"]:
        print(" [sub2api] 还没设置管理密钥，请先选 [1]")
        return
    ok, msg = Sub2Api(s).ping()
    print(" [sub2api] %s  %s" % (msg, "[OK]" if ok else "[失败]"))
    if ok and os.path.exists(CONFIG_YAML):
        print("\n 两边都就绪，可以选 [3] 生成导入数据。")


def build_plan(s):
    """读 CPA 配置 -> 生成导入计划。菜单和一键导入共用。

    返回 (plan, 快照文本, 数据来源说明, 丢弃说明列表)。
    """
    yaml = _yaml()
    if yaml is None:
        raise RuntimeError("缺少 PyYAML")
    text, src = fetch_cpa_text(s)
    cfg_dict = yaml.safe_load(text)
    if not isinstance(cfg_dict, dict):
        raise RuntimeError("config.yaml 解析结果不是字典，检查格式")

    recs, dropped = collect(cfg_dict)
    if not recs:
        raise RuntimeError("没解析到任何账号，检查是否有 *-api-key 段")

    # routing.strategy 决定 weight<=0 是不是真的等于停用：
    # CPA 只在 weighted-round-robin 下排除零权重凭据。
    # 必须在 apply_weight_zero_priority 之前解析出来——降级要按策略决定做不做。
    routing = cfg_dict.get("routing") or {}
    strategy = str(routing.get("strategy") or "round-robin").strip().lower()

    # 启用智能优先级：优先从 sub2api 实际运行状态计算健康分数
    use_smart_priority = s.get("health_rerank_enabled", True)
    if use_smart_priority and SMART_PRIORITY_AVAILABLE and s.get("sub2api_admin_key"):
        try:
            api = Sub2Api(s)
            print("[智能优先级] 启用基于健康度的动态优先级分配...")
            bucket_of = remap_priority_smart(
                recs, api, host_of, account_fingerprint,
                _is_int, _host_tier_map, _host_key
            )
            # 应用映射到每条记录。
            # bucket_of 的键是**域名单元** (_host_key)，不是 CPA 的 priority 数值。
            # 以前这里用 `bucket_of.get(tier, 500)` 拿 _host_tier_map 的序位值
            # 去查表，等于把"按序位值建表"的旧约定又套了一遍，跨渠道碰撞被原样带进来；
            # 现在直接按域名单元取桶号。
            for r in recs:
                r["new_priority"] = bucket_of.get(_host_key(r), 500)
            print("[智能优先级] 应用完成")
        except Exception as e:
            print(f"[智能优先级] 失败，回退到传统映射: {e}")
            remap_priority(recs)
    else:
        remap_priority(recs)
    # 零权重降级必须跟在 remap_priority 之后：它以后者算出的 new_priority
    # 为基准往后平移，顺序颠倒会拿到空值。
    plan_cfg = {
        "default_concurrency": s.get("default_concurrency", 3),
        "respect_weight_zero": s.get("respect_weight_zero", True),
        "group_prefix": s.get("group_prefix", "CPA"),
        "batch_size": s.get("batch_size", 50),
        "routing_strategy": strategy,
        # 调度可恢复性相关策略，见 needs_inactive / apply_weight_zero_priority
        "zero_model_policy": s.get("zero_model_policy", "inactive"),
        "weight_zero_policy": s.get("weight_zero_policy", "deprioritize"),
        "auto_pause_on_expired": s.get("auto_pause_on_expired", False),
        "auto_recover_enabled": s.get("auto_recover_enabled", True),
        "auto_recover_cron": s.get("auto_recover_cron", "*/30 * * * *"),
        # Problem 6.3：停用账号也探活，探活证明可用的可选择重新启用
        "probe_inactive": s.get("probe_inactive", True),
        "revive_proven_inactive": s.get("revive_proven_inactive", False),
        "test_plan_workers": s.get("test_plan_workers", 8),
        "import_workers": s.get("import_workers", 4),
        "sync_existing_accounts": s.get("sync_existing_accounts", True),
        # health_rerank_enabled 必须进 plan_cfg：do_import 会拿它决定要不要
        # 调 health_rerank_priority 覆盖 new_priority。以前 plan_cfg 里没这个键，
        # do_import 里 cfg.get("health_rerank_enabled", True) 恒为 True——
        # 用户把这个开关关掉后，导入阶段照样会重排优先级。
        "health_rerank_enabled": s.get("health_rerank_enabled", True),
        "pool_mode_enabled": s.get("pool_mode_enabled", True),
        "pool_mode_retry_status_codes": s.get("pool_mode_retry_status_codes"),
        "pool_mode_retry_count": s.get("pool_mode_retry_count", 2),
    }
    n_depri = apply_weight_zero_priority(recs, plan_cfg)
    assign_names(recs)

    # 同域名内 priority 不一致会被 _host_tier_map 按最高值归一化（保证同域名
    # 的多个 KEY 仍然同桶轮循），但要出声：这通常是配置写漏了。
    drift = check_priority_drift(recs)
    for g, h, n, ps in drift:
        dropped.append(
            "%s / %s 的 %d 个 KEY 在 CPA 里 priority 不一致（%s），"
            "已统一按最高值 %s 分桶以保证同域名互为备份"
            % (g, h, n, "、".join(str(x) for x in ps), ps[0]))

    groups = group_payloads(recs, s)
    proxies, proxy_warn = distinct_proxies(recs)
    fps = distinct_fingerprints(recs)

    plan = {
        "groups": groups,
        "proxies": proxies,
        "fingerprints": fps,
        "accounts_raw": recs,
        "config": plan_cfg,
        # 新鲜度信息：菜单允许直接跑导入而不重新生成，没有这个就可能拿上周的
        # 计划去导今天的配置
        "meta": {
            "tool_version": TOOL_VERSION,
            "source": src,
            "config_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    return plan, text, src, dropped + proxy_warn


def write_plan_files(plan, text, s):
    """把计划、快照和对照表写到 out/。返回对照表路径。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    with io.open(PLAN_PATH, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    with io.open(os.path.join(OUT_DIR, "cpa-config-snapshot.yaml"), "w",
                 encoding="utf-8", newline="\n") as f:
        f.write(text)

    recs = plan["accounts_raw"]
    cfg = plan["config"]
    prefix = cfg.get("group_prefix", "CPA")
    grouped = defaultdict(list)
    for r in recs:
        grouped[r["group"]].append(r)

    md = os.path.join(OUT_DIR, "对照表.md")
    with io.open(md, "w", encoding="utf-8", newline="\n") as f:
        f.write("# CPA -> sub2api 导入对照表\n\n")
        f.write("本表列出每条账号迁移后的实际参数，**导入前请抽查**。\n")
        f.write("凭据在本表中已脱敏；但 `import-plan.json` 和 "
                "`cpa-config-snapshot.yaml` 是明文，用完请删。\n\n")
        f.write("生成时间 %s｜工具版本 %s｜CPA 调度策略 `%s`\n\n"
                % (plan["meta"]["generated_at"], plan["meta"]["tool_version"],
                   cfg.get("routing_strategy")))

        f.write("## 渠道与分组\n\n| 分组 | 平台 | 账号数 | 协议声明 |\n|---|---|---|---|\n")
        for g in sorted(grouped):
            a = grouped[g]
            f.write("| %s-%s | %s | %d | %s |\n"
                    % (prefix, g, a[0]["platform"], len(a), a[0]["responses_mode"] or "—"))
        f.write("\n**priority 方向**：CPA 数值大者优先，sub2api 数值小者优先，"
                "已按渠道内排名重映射。\n")

        for g in sorted(grouped):
            arr = sorted(grouped[g], key=lambda x: x["rank"])
            f.write("\n## %s-%s（%d 条）\n\n" % (prefix, g, len(arr)))
            f.write("| 名次 | 名称 | 原优先级 | 新优先级 | base-url | 放行模型 | "
                    "排除规则 | 被排除 | 请求头 | 冷却规则 | 代理 | 状态 |\n")
            f.write("|---|---|---|---|---|---|---|---|---|---|---|---|\n")
            for r in arr:
                h, d = build_header_overrides(r["headers"], r["platform"])
                # 这两处**必须**传 platform + source_section，否则对照表算的是
                # "未经就高过滤、未经 excluded 裁剪"的原始模型集合，而真正写进
                # sub2api 的是过滤后的白名单。用户导入前抽查对照表，
                # 会看到一个与线上实际生效不同的模型集合。
                _sec = r.get("source_section") or r.get("section")
                mm = build_model_mapping(r["models_raw"], r.get("excluded"),
                                         r.get("platform"), source_section=_sec)
                all_m = build_model_mapping(r["models_raw"], None,
                                            r.get("platform"), source_section=_sec)
                rules, _lost = build_temp_unschedulable(r["rse"])
                st = "停用" if needs_inactive(r, cfg) else "启用"
                # 把 excluded-models 的原文直接打出来。只写个数字的话，
                # 用户在表里搜不到 `*` 这种规则，根本不知道模型是被什么挡掉的。
                ex_txt = "、".join("`%s`" % x for x in (r.get("excluded") or [])) or "-"
                gone = max(0, len(all_m) - len(mm))
                if all_m and not mm:
                    gone_txt = "**全部 %d 个**" % len(all_m)
                else:
                    gone_txt = str(gone)
                f.write("| %d | %s | %s | %d | %s | %d | %s | %s | %d%s | %d | %s | %s |\n" % (
                    r["rank"], r["name"], r["priority"], r["new_priority"],
                    r["base_url"] or "-", len(mm), ex_txt, gone_txt,
                    len(h), ("(丢%d)" % len(d)) if d else "",
                    len(rules), r["proxy_url"] or "-", st))

        # 把"被排除到一个模型都不剩"的账号单独列出来，这是最容易出事的配置
        nothing = [r for r in recs if serves_nothing(r)]
        if nothing:
            f.write("\n## ⚠ 被 excluded-models 排除到没有模型的账号（%d 条）\n\n"
                    % len(nothing))
            f.write("这些账号在 CPA 里**一个模型都不提供、不参与调度**，"
                    "因为 `excluded-models` 的规则命中了它 `models` 里的全部模型。\n")
            f.write("导入 sub2api 时按同样语义处理为**停用**。\n\n")
            f.write("如果这不是你的本意，说明 `config.yaml` 写错了——"
                    "把对应条目的 `excluded-models` 整行删掉或改成真正要排除的模型名，"
                    "然后重新导入。\n\n")
            f.write("| 名称 | base-url | 声明的模型 | 排除规则 |\n|---|---|---|---|\n")
            for r in nothing:
                names = [str(m.get("name")) for m in (r["models_raw"] or [])
                         if isinstance(m, dict) and m.get("name")]
                f.write("| %s | %s | %s | %s |\n" % (
                    r["name"], r["base_url"] or "-",
                    "、".join(names) or "-",
                    "、".join("`%s`" % x for x in (r.get("excluded") or []))))

        f.write("\n## 无法迁移的能力\n\n")
        f.write("以下 CPA 行为活在 CPA 的请求管线里，不是凭据上的字段，"
                "sub2api 没有等价物，已记入每条账号的 notes 存档：\n\n")
        f.write("- `fingerprint-profile`：uTLS 伪造 ClientHello + HTTP/1.1 头序重写，"
                "属传输层行为。sub2api 的 TLS 指纹仅对 Anthropic 的 OAuth/setup-token 账号生效，"
                "我们导入的全是 apikey 账号，用不上。\n")
        f.write("- `cloak`：改写 system 提示、注入计费头、混淆敏感词、工具名别名化，"
                "全是按请求内容做的报文改写。\n")
        f.write("- `$` 开头的请求头：值来自下游客户端的同名头，逐请求动态解析，"
                "静态存储会把字面量发出去，已丢弃。\n")
        f.write("- `request-retry` / `disable-cooling`：CPA 的多轮重试与冷却调度策略。\n")
        f.write("- `rebuild-mid-system-message` / `support-prompt-cache-key`：报文改写。\n")
        f.write("- 同一 alias 的模型池轮转：需要按凭据的可变计数器与响应改写。\n")
        f.write("\n已成功迁移的：优先级方向、模型白名单（含 `excluded-models` 裁剪）、"
                "请求头覆写、冷却规则、代理绑定、协议声明、websockets、alpha-search。\n")
    return md


def act_generate(s):
    print("\n" + "=" * 60)
    print(" 生成导入数据")
    print("=" * 60)
    try:
        plan, text, src, warns = build_plan(s)
    except Exception as ex:
        print(" [失败] %s" % ex)
        return

    recs = plan["accounts_raw"]
    cfg = plan["config"]
    print(" 数据来源：%s" % src)
    print(" CPA 调度策略：%s" % cfg.get("routing_strategy"))
    for g, n in sorted(Counter(r["group"] for r in recs).items()):
        print("   %-8s %3d 条" % (g, n))
    print("   合计     %3d 条" % len(recs))

    md = write_plan_files(plan, text, s)

    nh = sum(1 for r in recs if build_header_overrides(r["headers"], r["platform"])[0])
    nm = sum(1 for r in recs if build_model_mapping(
        r["models_raw"], r.get("excluded"), r.get("platform"),
        source_section=r.get("source_section") or r.get("section")))
    nex = sum(1 for r in recs if r.get("excluded"))
    ncd = sum(1 for r in recs if build_temp_unschedulable(r["rse"])[0])
    noff = sum(1 for r in recs if needs_inactive(r, cfg))
    print("\n 参数：带模型白名单 %d，其中按 excluded-models 裁剪过 %d；"
          "带请求头 %d；带冷却规则 %d；需停用 %d"
          % (nm, nex, nh, ncd, noff))
    print(" 需建分组 %d：%s" % (len(plan["groups"]),
                              "、".join(g["name"] for g in plan["groups"].values())))
    print(" 需建代理 %d：%s" % (len(plan["proxies"]), "、".join(plan["proxies"]) or "无"))
    if plan["fingerprints"]:
        print(" ! 有 %d 个 TLS 指纹模板无法迁移：%s"
              % (len(plan["fingerprints"]), "、".join(plan["fingerprints"])))
    if warns:
        print("\n 解析期提示（%d 条）：" % len(warns))
        for w in warns[:8]:
            print("   - %s" % w)
        if len(warns) > 8:
            print("   ... 其余 %d 条" % (len(warns) - 8))
    print("\n 已生成：out\\对照表.md、out\\import-plan.json")
    print(" ! import-plan.json 与 cpa-config-snapshot.yaml 含明文密钥，用完请删除。")


def _pick_id(resp):
    """从各种可能的响应形态里抠出新建资源的 id。

    sub2api 正常返回 {"code":0,"data":{"id":N}}，但 data 为 null 时
    resp.get("data", {}) 会得到 None，直接 .get("id") 就是 AttributeError。
    """
    if not isinstance(resp, dict):
        return None
    for holder in (resp.get("data"), resp):
        if isinstance(holder, dict):
            for k in ("id", "ID", "group_id", "proxy_id"):
                v = holder.get(k)
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    return v
    return None


def ensure_groups(api, groups):
    """建齐分组，返回 (短名->id, 失败的短名集合)。

    groups 的 key 是短名（Claude），gpay["name"] 才是完整名（CPA-Claude），
    而 sub2api 返回的是完整名。必须按完整名比对，否则永远匹配不上，
    走到 409 分支拿不到 ID，账号就会不绑分组地导入。
    """
    try:
        existing = {g["name"]: g["id"] for g in api.list_groups() if g.get("name")}
    except ApiError as ex:
        raise RuntimeError("读取现有分组失败：HTTP %s %s（检查 sub2api_admin_key）"
                           % (ex.status, ex.body[:120]))

    gmap, failed = {}, set()
    for gname, gpay in groups.items():
        full = gpay["name"]
        if full in existing:
            gmap[gname] = existing[full]
            print("    [复用] %s (id=%s)" % (full, gmap[gname]))
            # 复用的分组要补齐后来才加的字段。早期版本建组时没传
            # long_context_pricing_enabled，而它在创建接口里是裸 bool
            # （group_handler.go:194），漏传就被写成 false，覆盖了 schema
            # 的 Default(true)。这里按现在的期望值纠正一次；值相同则不发请求。
            want = gpay.get("long_context_pricing_enabled")
            if want is not None:
                try:
                    cur = (api.get_group(gmap[gname]).get("data") or {})
                    if bool(cur.get("long_context_pricing_enabled")) != bool(want):
                        api.update_group(gmap[gname],
                                         {"long_context_pricing_enabled": bool(want)})
                        print("      已修正 long_context_pricing_enabled -> %s" % want)
                except ApiError as ex:
                    print("      ! 修正 long_context_pricing_enabled 失败：HTTP %s" % ex.status)
                except Exception as ex:
                    print("      ! 修正 long_context_pricing_enabled 失败：%s" % str(ex)[:60])
            continue
        try:
            gid = _pick_id(api.create_group(gpay))
            if gid:
                gmap[gname] = gid
                print("    [新建] %s (id=%s)" % (full, gid))
                continue
            print("    [失败] %s：响应里没有 id" % full)
            failed.add(gname)
        except ApiError as ex:
            # 409 = 重名。重查一次拿 ID；拿不到就必须让调用方跳过这批账号，
            # 不能像以前那样"打印一句跳过"却照样裸导入。
            if ex.status == 409 or "exist" in ex.body.lower():
                try:
                    existing = {g["name"]: g["id"] for g in api.list_groups() if g.get("name")}
                except ApiError:
                    existing = {}
                if full in existing:
                    gmap[gname] = existing[full]
                    print("    [复用] %s (id=%s)" % (full, gmap[gname]))
                    continue
            print("    [失败] %s：HTTP %s %s" % (full, ex.status, ex.body[:120]))
            failed.add(gname)
        except Exception as ex:
            print("    [失败] %s：%s" % (full, ex))
            failed.add(gname)
    return gmap, failed


def ensure_proxies(api, proxies):
    """建齐代理，返回 url->id。

    sub2api 的 proxies 表没有唯一约束（ent/schema/proxy.go 无 Unique，
    也没有唯一索引迁移），POST 同样的 host:port 会一直建出新行，
    所以去重必须在客户端做。按 id 升序取第一个，与清理时保留 id 最小的一致。
    """
    try:
        have = {}
        for p in sorted(api.list_proxies(), key=lambda x: x.get("id") or 0):
            have.setdefault(
                (str(p.get("protocol")), str(p.get("host")), p.get("port")), p.get("id"))
    except ApiError as ex:
        print("    ! 读不到现有代理（HTTP %s），可能建出重复项" % ex.status)
        have = {}

    pmap = {}
    for purl, ppay in proxies.items():
        key = (str(ppay.get("protocol")), str(ppay.get("host")), ppay.get("port"))
        if have.get(key):
            pmap[purl] = have[key]
            print("    [复用] %s (id=%s)" % (purl, pmap[purl]))
            continue
        try:
            pid = _pick_id(api.create_proxy(ppay))
            if pid:
                pmap[purl] = pid
                print("    [新建] %s (id=%s)" % (purl, pid))
            else:
                print("    [失败] %s：响应里没有 id，相关账号将直连" % purl)
        except ApiError as ex:
            # 撞重名/冲突时必须重查拿到 ID 写进 pmap，否则日志说"复用"、
            # 实际账号拿不到 proxy_id 就静默直连了。
            if ex.status == 409 or "exist" in ex.body.lower() or "duplicate" in ex.body.lower():
                try:
                    for p in sorted(api.list_proxies(), key=lambda x: x.get("id") or 0):
                        k = (str(p.get("protocol")), str(p.get("host")), p.get("port"))
                        if k == key and p.get("id"):
                            pmap[purl] = p["id"]
                            break
                except ApiError:
                    pass
                if purl in pmap:
                    print("    [复用] %s (id=%s)" % (purl, pmap[purl]))
                    continue
            print("    [失败] %s：HTTP %s，相关账号将直连" % (purl, ex.status))
        except Exception as ex:
            print("    [失败] %s：%s，相关账号将直连" % (purl, ex))
    return pmap


def do_import(api, plan, ask=None):
    """唯一的导入实现。菜单和一键导入共用，避免两份逻辑各自漂移。

    ask: 可选的确认回调 ask(提示语) -> bool。传 None 表示不确认直接导。

    返回统计 dict。
    """
    groups = plan["groups"]
    proxies = plan["proxies"]
    recs = plan["accounts_raw"]
    cfg = plan["config"]

    print("\n 将导入：分组 %d 个、代理 %d 个、账号 %d 条"
          % (len(groups), len(proxies), len(recs)))
    if ask and not ask("\n 确认写入 %s ？" % api.base):
        print(" 已取消。")
        return {"cancelled": True}

    # --- 1. 分组 ---
    print("\n [1/6] 创建分组...")
    gmap, gfailed = ensure_groups(api, groups)

    # 分组降级链：同平台内按优先级串起来，整组不可用时自动转给下一组。
    # 放在建组之后、导账号之前——这一步只改分组自身的字段，与账号无关。
    fb_chain = plan_fallback_chain(recs, groups)
    fb_ok, fb_fail = 0, []
    if fb_chain:
        fb_ok, fb_fail = apply_fallback_chain(api, fb_chain, gmap)
        print("    降级链：%s"
              % "；".join("%s→%s" % (a, b) for a, b in sorted(fb_chain.items())))
        print("    设置成功 %d 个，失败 %d 个" % (fb_ok, len(fb_fail)))
        for g, why in fb_fail[:5]:
            print("      ✗ %s：%s" % (g, why))

    # --- 2. 代理 ---
    print("\n [2/6] 创建代理...")
    pmap = ensure_proxies(api, proxies)

    # --- 3. 账号 ---
    print("\n [3/6] 导入账号...")
    success, failed, skipped = [], [], []

    # 分组没拿到 ID 的，账号必须真的跳过。以前只打印一句"将跳过"却照样导入，
    # 结果就是一批不绑分组的账号——而 sub2api 的列表接口不返回 api_key，
    # 事后没法补绑，只能删了重来。
    if gfailed:
        blocked = [r for r in recs if r["group"] in gfailed]
        for r in blocked:
            skipped.append((r["name"], "所属分组 %s 建失败，跳过以免裸导入" % r["group"]))
        recs = [r for r in recs if r["group"] not in gfailed]
        print("    ! 分组 %s 没拿到 ID，其下 %d 条账号已跳过"
              % ("、".join(sorted(gfailed)), len(blocked)))

    # 按名字去重。名字含内容指纹，跨 config 版本仍能正确识别同一凭据。
    have, derr = existing_account_names(api)
    if derr:
        print("    ! %s" % derr)
        print("    ! 为避免建出重复账号，本次中止。请排查后重试。")
        return {"aborted": "读不到现有账号，拒绝在无法去重的情况下写入"}

    # 健康度重排优先级（文档第 2 条⑶）：在去重之前先按 sub2api 侧的实际状态
    # 调整 new_priority，这样后续无论是新建还是同步都用上修正后的桶号。
    # 必须拿完整详情而不是仅名字——需要 schedulable / status 字段。
    have_map, hm_err = existing_accounts_by_name(api)
    if hm_err:
        print("    ! 读取已有账号详情失败，健康度重排跳过；仍将按 CPA 优先级导入")
        print("      原因：%s" % hm_err)
        have_map = {}  # 容错：读不到就等于首次导入，health_rerank 会跳过
    rerank_n, rerank_msg = health_rerank_priority(recs, have_map, cfg)
    if rerank_n:
        print("    ✓ %s" % rerank_msg)
    else:
        print("    · %s" % rerank_msg)

    dup = [r for r in recs if r["name"] in have]
    recs = [r for r in recs if r["name"] not in have]

    # 已存在的账号：默认不再简单跳过，而是把 CPA 里改过的字段同步过来。
    # 老行为（纯跳过）可用 设置.json 的 sync_existing_accounts=false 恢复。
    sync_ok = sync_same = 0
    sync_fail = []
    if dup and cfg.get("sync_existing_accounts", True):
        print("    已存在 %d 条，检查是否需要同步..." % len(dup))
        # have_map 前面已经拿过了，直接复用
        if hm_err:
            # 取不全就不要瞎更新：半份数据会把没读到的那些误判成无需同步。
            print("      ! 读取已有账号详情失败，本次跳过同步：%s" % hm_err)
            for r in dup:
                skipped.append((r["name"], "已存在，同步失败（%s）" % hm_err))
        else:
            sync_ok, sync_same, sync_fail = sync_existing_accounts(
                api, dup, cfg, gmap, pmap, have_map)
            print("      同步 %d 条，无变化 %d 条，失败 %d 条"
                  % (sync_ok, sync_same, len(sync_fail)))
            for nm, why in sync_fail[:5]:
                print("      ! %s：%s" % (nm, why))
            for r in dup:
                if any(nm == r["name"] for nm, _ in sync_fail):
                    continue
                skipped.append((r["name"], "已存在，已同步" if sync_ok else "已存在，无变化"))
    else:
        for r in dup:
            skipped.append((r["name"], "已存在，跳过"))
    if dup:
        print("    本次实导 %d 条" % len(recs))

    batch_size = int(cfg.get("batch_size", 50) or 50)
    total = len(recs)
    name_to_rec = {r["name"]: r for r in recs}
    created_ids = {}

    # 2026-09-13：从 CPA 已有的 proxy_url 反推每个域名是否需要代理。
    # 同域名下任意一条配了代理，说明这个域名从本机需要代理，新条目应跟进。
    proxy_need = build_proxy_need_map(recs)

    # 并发批次导入（P1优化 - 多线程加速）。
    # ThreadPoolExecutor / as_completed / CONCURRENT_AVAILABLE 都在模块顶部
    # 全局导入 —— 原先这里又 import 了一次并把 CONCURRENT_AVAILABLE 赋成
    # **函数局部变量**，于是模块顶层的那个同名标志没被更新，而本函数里
    # 后面的判断读的是局部值，行为上凑巧正确，但另外两处用了这两个名字的
    # 并发路径（ensure_test_plans / revive_proven_inactive）拿不到它们，
    # 走到并发分支就 NameError。
    workers = max(1, min(int(cfg.get("import_workers", 4) or 4), 8))

    def import_one_batch(start_idx, batch_recs):
        """导入一个批次，返回 (成功列表, 失败列表, 创建的ID映射)"""
        payloads = [to_account(r, cfg, gmap, pmap, proxy_need) for r in batch_recs]
        batch_success = []
        batch_failed = []
        batch_ids = {}

        try:
            resp = api.batch_accounts(payloads)
            data = resp.get("data")
            results = (data or {}).get("results") if isinstance(data, dict) else None
            results = results or []

            if len(results) != len(payloads):
                for p in payloads:
                    batch_failed.append((p["name"],
                        "批次返回 %d 条结果，与提交的 %d 条对不上" % (len(results), len(payloads))))
                return batch_success, batch_failed, batch_ids

            seen = set()
            for res in results:
                nm = res.get("name", "?")
                seen.add(nm)
                if res.get("success"):
                    batch_success.append(nm)
                    if res.get("id"):
                        batch_ids[nm] = res["id"]
                else:
                    batch_failed.append((nm, str(res.get("error", "未知错误"))))

            for p in payloads:
                if p["name"] not in seen:
                    batch_failed.append((p["name"], "服务端结果里没有这条记录"))

        except ApiError as ex:
            for p in payloads:
                batch_failed.append((p["name"], "HTTP %s %s" % (ex.status, ex.body[:120])))
        except Exception as ex:
            for p in payloads:
                batch_failed.append((p["name"], str(ex)[:120]))

        return batch_success, batch_failed, batch_ids

    # 构建批次列表
    batches = []
    for start in range(0, total, batch_size):
        batch = recs[start:start + batch_size]
        batches.append((start, batch))

    nbatch = len(batches)
    use_concurrent = CONCURRENT_AVAILABLE and workers > 1 and nbatch > 1

    if use_concurrent:
        print("    使用 %d 线程并发导入 %d 个批次..." % (workers, nbatch))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_batch = {
                executor.submit(import_one_batch, start, batch): (start, batch)
                for start, batch in batches
            }

            completed = 0
            for future in as_completed(future_to_batch):
                start_idx, batch_recs = future_to_batch[future]
                nth = start_idx // batch_size + 1

                try:
                    batch_success, batch_failed, batch_ids = future.result()
                    success.extend(batch_success)
                    failed.extend(batch_failed)
                    created_ids.update(batch_ids)

                    completed += 1
                    print("    批次 %d/%d 完成（成功 %d, 失败 %d）[总进度 %d/%d]"
                          % (nth, nbatch, len(batch_success), len(batch_failed), completed, nbatch))

                except Exception as ex:
                    for r in batch_recs:
                        failed.append((r["name"], "批次执行异常: %s" % str(ex)[:80]))
                    print("    批次 %d/%d 异常: %s" % (nth, nbatch, str(ex)[:80]))
    else:
        # 串行模式（workers=1 或无并发库或单批次）
        if not CONCURRENT_AVAILABLE and workers > 1:
            print("    ! 缺少 concurrent.futures，降级为串行导入")
        elif nbatch == 1:
            print("    单批次导入...")
        else:
            print("    串行导入 %d 个批次..." % nbatch)

        for start, batch in batches:
            batch_success, batch_failed, batch_ids = import_one_batch(start, batch)
            success.extend(batch_success)
            failed.extend(batch_failed)
            created_ids.update(batch_ids)

            nth = start // batch_size + 1
            print("    批次 %d/%d（%d 条）- 成功 %d, 失败 %d"
                  % (nth, nbatch, len(batch), len(batch_success), len(batch_failed)))

    # --- 4. 补停用 ---
    # sub2api 的 CreateAccountRequest 没有 status 字段，建号时一律 active
    # （admin_account.go:429 硬编码 StatusActive），发 "status":"inactive"
    # 会被 Gin 静默丢弃。要停用只能建完再改一次。
    # 这里走 bulk-update 而不是逐条 PUT：几十条账号逐条发等于几十个来回，
    # 而 bulk-update 一次就能带走一批（服务端语义相同）。
    print("\n [4/6] 处理需要停用的账号...")
    want_off = [n for n in success
                if n in name_to_rec and needs_inactive(name_to_rec[n], cfg)]
    off_ok, off_fail = 0, []
    off_ids, off_names = [], []
    for nm in want_off:
        aid = created_ids.get(nm)
        if not aid:
            off_fail.append((nm, "没拿到账号 id，无法停用"))
            continue
        off_ids.append(aid)
        off_names.append(nm)
    bs = int(cfg.get("batch_size", 50) or 50)
    for i in range(0, len(off_ids), bs):
        chunk = off_ids[i:i + bs]
        names = off_names[i:i + bs]
        try:
            api.bulk_update_accounts(chunk, {"status": "inactive"})
            off_ok += len(chunk)
        except ApiError as ex:
            for nm in names:
                off_fail.append((nm, "HTTP %s %s" % (ex.status, ex.body[:80])))
        except Exception as ex:
            for nm in names:
                off_fail.append((nm, str(ex)[:80]))
    if want_off:
        print("    需停用 %d 条，成功 %d 条" % (len(want_off), off_ok))
        for nm, why in off_fail[:5]:
            print("      ✗ %s：%s" % (nm, why))
    else:
        print("    没有需要停用的账号")

    # --- 5. 挂定时探活计划 ---
    # sub2api 自带的 auto_recover 机制，负责把出错的账号状态修回 active。
    # 注意它只还 status、不还 schedulable，所以必须配合第 6 步一起用。
    print("\n [5/6] 挂定时探活计划（auto_recover）...")
    plan_ok, plan_fail = 0, []
    if cfg.get("auto_recover_enabled", True):
        # 停用的账号**也要挂**（Problem 6.3）。
        #
        # 原先这里排除了 want_off，理由是"停用账号探活没意义、还产生费用"。
        # 但这恰好把「原配置关闭的上游」永久锁死了：它们不被探测，就永远
        # 不知道已经恢复，也就永远不会被重新启用——实测 47/231 条卡在这里。
        # 探活正是判断这些上游能不能重新用的唯一手段，所以默认纳入。
        # 想回到旧行为（省探活费用）把 probe_inactive 设成 false。
        off_set = set(want_off)
        probe_inactive = cfg.get("probe_inactive", True)
        targets = {nm: aid for nm, aid in created_ids.items()
                   if nm in success and (probe_inactive or nm not in off_set)}
        n_off_probed = sum(1 for nm in targets if nm in off_set)
        plan_ok, plan_fail = ensure_test_plans(api, targets, cfg)
        print("    挂载 %d 条，失败 %d 条（cron=%s）"
              % (plan_ok, len(plan_fail), cfg.get("auto_recover_cron")))
        if n_off_probed:
            print("    其中停用账号 %d 条也已纳入探活（恢复后由第 6 步重新启用）"
                  % n_off_probed)
        for nm, why in plan_fail[:5]:
            print("      ✗ %s：%s" % (nm, why))
    else:
        print("    已按配置关闭（auto_recover_enabled=false）")

    # --- 6. 回收被网关关掉的调度开关 ---
    # 补 sub2api 的恢复缺口：ClearError 只还 status 不还 schedulable，
    # 账号会永久卡在 active + 不可调度。详见 reclaim_schedulable。
    print("\n [6/6] 回收被关闭的调度开关...")
    rec_done, rec_total, rec_err = reclaim_schedulable(api)
    if rec_err:
        print("    ! 回收失败：%s" % rec_err)
    elif rec_total:
        print("    发现 %d 条状态正常但调度关闭，已恢复 %d 条" % (rec_total, rec_done))
    else:
        print("    没有需要回收的账号")

    # 停用账号里「探活已证明可用」的那些一并启用（Problem 6.3）。
    # sub2api 的自动恢复只认 error、不认 inactive，这一步是唯一的出路。
    # 默认关闭，需在 设置.json 里显式打开 revive_proven_inactive。
    rv_done, rv_total, rv_err = revive_proven_inactive(api, cfg)
    if rv_err:
        print("    ! 停用账号复活检查失败：%s" % rv_err)
    elif rv_total:
        print("    停用账号中探活已证明可用 %d 条，已重新启用 %d 条"
              % (rv_total, rv_done))
    elif cfg.get("revive_proven_inactive", False):
        print("    停用账号中没有探活证明可用的，保持停用")

    # --- 附加检查：僵尸账号 ---
    # config.yaml 里换过 api_key 的上游会留下旧账号（名字含 key 指纹），
    # 只报告不删除——删号不可逆，且可能只是用户临时注释了一段配置。
    stale, stale_err = find_stale_accounts(api, plan["accounts_raw"])
    if stale_err:
        print("\n    ! 僵尸账号检查失败：%s" % stale_err)
    elif stale:
        print("\n    ⚠ 发现 %d 条本工具导入、但当前 config.yaml 里已无对应凭据的账号：" % len(stale))
        for a in stale[:10]:
            print("      · %s (id=%s)" % (a.get("name"), a.get("id")))
        if len(stale) > 10:
            print("      · ...另有 %d 条" % (len(stale) - 10))
        print("      多半是轮换过 api_key 留下的旧号，确认后可用菜单的清空功能删除。")

    # --- 第二阶段：用刚产生的真实状态再重排一次 ---
    # 文档第 2 条⑶ 明确要求解决这个冷启动问题：
    #   "导入前 sub2api 没有账号 -> 智能优先级无法工作，
    #    需要先导入一次，再重新导入才能应用智能优先级"
    #
    # 第一阶段（上面的 health_rerank_priority）在首次导入时看不到任何实测
    # 状态，只能沿用 CPA 的优先级。但账号刚建出来就已经有 status/schedulable
    # 了，所以这里立刻拿新数据再排一次，并把新的 priority 推给已存在的账号。
    #
    # 只在这一种情形下做：本阶段**刚新建过账号**（success > 0）。
    # 没有新建就说明这是第二次及以后的运行，第一阶段已经拿到完整数据、
    # 上面这次重排是多余的，白跑一轮网络请求。
    stage2 = {"ran": False, "ranked": 0, "updated": 0, "msg": ""}
    if success > 0 and cfg.get("health_rerank_enabled", True):
        print("\n [第二阶段] 用新建账号的实测状态重排优先级...")
        have2, hm2_err = existing_accounts_by_name(api)
        if hm2_err:
            stage2["msg"] = "读取账号详情失败：%s" % hm2_err
            print("    ! %s" % stage2["msg"])
        else:
            ranked2, msg2 = health_rerank_priority(plan["accounts_raw"], have2, cfg)
            stage2["ran"] = True
            stage2["ranked"] = ranked2
            stage2["msg"] = msg2
            if not ranked2:
                print("    · %s" % msg2)
            else:
                print("    ✓ %s" % msg2)
                # 把新桶号推给线上账号。复用 sync_existing_accounts：
                # 它靠 diff_account_fields 只发真正变了的字段，所以这里
                # 只会更新 priority（和由健康分派生的 load_factor）。
                targets = [r for r in plan["accounts_raw"] if r["name"] in have2]
                if targets:
                    ok2, same2, fail2 = sync_existing_accounts(
                        api, targets, cfg, gmap, pmap, have2)
                    stage2["updated"] = ok2
                    print("    已按实测健康度更新 %d 条（无变化 %d，失败 %d）"
                          % (ok2, same2, len(fail2)))
                    for nm, why in fail2[:3]:
                        print("      ! %s：%s" % (nm, why))

    stat = {
        "total_planned": total,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "deactivated": off_ok,
        "deactivate_failed": off_fail,
        "synced_existing": sync_ok,
        "sync_unchanged": sync_same,
        "sync_failed": sync_fail,
        "test_plans_created": plan_ok,
        "test_plan_failed": plan_fail,
        "fallback_chain": fb_chain,
        "fallback_ok": fb_ok,
        "fallback_failed": fb_fail,
        "schedulable_reclaimed": rec_done,
        "schedulable_candidates": rec_total,
        "stage2_rerank": stage2,
        "stale_accounts": [{"id": a.get("id"), "name": a.get("name")}
                           for a in (stale or [])],
        "groups": {k: gmap[k] for k in gmap},
        "proxies": pmap,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool_version": TOOL_VERSION,
    }
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with io.open(os.path.join(OUT_DIR, "import-record.json"), "w", encoding="utf-8") as f:
            json.dump(stat, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return stat


def print_import_summary(stat):
    """统一的结果汇总。真实反映成功/失败，不在 0 成功时报"全部成功"。"""
    if stat.get("cancelled") or stat.get("aborted"):
        if stat.get("aborted"):
            print("\n 已中止：%s" % stat["aborted"])
        return 1

    succ, fail, skip = stat["success"], stat["failed"], stat["skipped"]
    total = stat["total_planned"]
    print()
    print("=" * 64)
    print(" 成功 %d / 失败 %d / 跳过 %d（本次计划导入 %d 条）"
          % (len(succ), len(fail), len(skip), total))
    print("=" * 64)

    if fail:
        print("\n 失败清单（最多列 10 条）：")
        for nm, err in fail[:10]:
            print("   ✗ %s：%s" % (nm, err[:100]))
        if len(fail) > 10:
            print("   ... 其余 %d 条见 out\\import-record.json" % (len(fail) - 10))

    if stat.get("deactivate_failed"):
        print("\n ! 有 %d 条账号应停用但没停成，它们正在以启用状态接流量："
              % len(stat["deactivate_failed"]))
        for nm, why in stat["deactivate_failed"][:5]:
            print("   ✗ %s：%s" % (nm, why))

    print()
    if fail:
        print(" ! %d 条导入失败，详见上面的错误信息。" % len(fail))
        return 1
    if len(succ) != total:
        print(" ! 计划导入 %d 条，实际成功 %d 条，对不上。请检查 out\\import-record.json。"
              % (total, len(succ)))
        return 1
    if total == 0:
        print(" 没有需要新建的账号（已存在的都跳过了）。")
        return 0
    print(" ✓ %d 条全部导入成功。" % len(succ))
    return 0


def _is_migrated(acc):
    """判断一条账号是否由本工具导入。

    只认 notes 开头的固定 sentinel。以前用的是"备注里出现 CPA 字样"，
    那会把用户手工建的、备注里恰好提到 CPA 的账号也算进删除范围
    （比如备注写"从 CPA 那边抄的 key"）。删除不可逆，认定必须精确。

    老版本导入的账号没有 sentinel，会被认成"不是我导的"而不被删——
    这是刻意选的安全方向：宁可漏删让用户手工处理，不可错删。
    """
    if not isinstance(acc, dict):
        return False
    return str(acc.get("notes") or "").startswith(NOTE_SENTINEL)


def _legacy_migrated(acc):
    """老版本（无 sentinel）导入的账号，仅用于提示，不自动纳入删除范围。"""
    if not isinstance(acc, dict):
        return False
    n = str(acc.get("notes") or "")
    return not n.startswith(NOTE_SENTINEL) and ("源自 CPA" in n or "迁移自 CPA" in n)


def health_check(api):
    """体检：找出需要清理的脏数据。返回 (报告 dict, 出错信息或 None)。

    三类问题分开统计，因为它们的处置方式完全不同：
      - 重名账号：同一个名字有多份，多出来的要删；
      - 没绑分组的账号：sub2api 的列表接口不返回 api_key，没法就地补绑，
        只能删了重导；
      - 重复代理：只需要删多余的代理行，**不该牵连账号**。
    """
    accs, err = _fetch_all_accounts(api)
    if err:
        return None, err

    mine = [a for a in accs if _is_migrated(a)]
    legacy = [a for a in accs if _legacy_migrated(a)]

    by_name = defaultdict(list)
    for a in mine:
        by_name[str(a.get("name"))].append(a)

    # 重名：每组保留 id 最小的一份，其余进待删
    dup_extra = []
    for name, arr in by_name.items():
        if len(arr) > 1:
            arr = sorted(arr, key=lambda x: x.get("id") or 0)
            dup_extra.extend(arr[1:])

    dup_ids = {id(a) for a in dup_extra}
    unbound = [a for a in mine if not a.get("group_ids") and id(a) not in dup_ids]

    try:
        proxies = api.list_proxies()
    except ApiError:
        proxies = []
    seen, dup_proxies = set(), []
    for p in sorted(proxies, key=lambda x: x.get("id") or 0):
        key = (str(p.get("protocol")), str(p.get("host")), p.get("port"))
        if key in seen:
            dup_proxies.append(p)
        else:
            seen.add(key)

    # 账号层面要删的 = 多余的重名份 + 没绑分组的
    bad_accounts = dup_extra + unbound
    return {
        "total": len(accs),
        "mine": len(mine),
        "others": len(accs) - len(mine) - len(legacy),
        "legacy": legacy,
        "unique_names": len(by_name),
        "dup_names": sum(1 for v in by_name.values() if len(v) > 1),
        "dup_extra": dup_extra,
        "unbound": unbound,
        "bad_accounts": bad_accounts,
        "dup_proxies": dup_proxies,
        "accounts_dirty": bool(bad_accounts),
        "proxies_dirty": bool(dup_proxies),
    }, None


def backup_accounts(accounts, tag="purge"):
    """删除前把账号快照落盘。

    sub2api 的列表接口不返回 credentials.api_key（已脱敏），删掉就再也拿不回来。
    快照救不回密钥，但至少保住 id / 名字 / 备注 / 分组，便于事后核对删了什么。
    """
    if not accounts:
        return None
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "%s-backup-%s.json" % (tag, time.strftime("%Y%m%d-%H%M%S")))
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    return path


def purge(api, accounts, proxies, progress_every=50):
    """删除给定的账号和代理，带退避重试。

    sub2api 在连续删几十条后偶尔会掐断连接（实测删到 ~75 条时
    "Remote end closed connection without response"），所以每条失败都退避重试，
    而不是直接放弃——否则会留下删一半的烂摊子。

    返回 (账号删除数, 代理删除数, 失败列表)。
    """
    fails = []

    def _try(fn, arg, label):
        for attempt in range(3):
            try:
                fn(arg)
                return True
            except ApiError as ex:
                if ex.status == 404:  # 已经没了，算成功
                    return True
                if attempt == 2:
                    fails.append("%s id=%s：HTTP %s" % (label, arg, ex.status))
                    return False
                time.sleep(0.5 * (attempt + 1))
            except Exception as ex:
                if attempt == 2:
                    fails.append("%s id=%s：%s" % (label, arg, str(ex)[:80]))
                    return False
                time.sleep(0.5 * (attempt + 1))
        return False

    aok, total = 0, len(accounts)
    for i, a in enumerate(accounts, 1):
        aid = a.get("id") if isinstance(a, dict) else a
        if not isinstance(aid, int):
            continue
        if _try(api.delete_account, aid, "账号"):
            aok += 1
        if progress_every and i % progress_every == 0:
            print("      已处理 %d/%d" % (i, total))

    pok = 0
    for p in proxies:
        pid = p.get("id") if isinstance(p, dict) else p
        if not isinstance(pid, int):
            continue
        # 代理被账号引用时 sub2api 返回 409 PROXY_IN_USE 且不级联删除，
        # 这是预期行为，不算错误
        if _try(api.delete_proxy, pid, "代理"):
            pok += 1

    return aok, pok, fails


def existing_account_names(api):
    """取回 sub2api 里已有的账号名集合，用于导入前去重。

    sub2api 的 accounts.name 没有唯一约束，重复跑工具会产生重名账号。
    列表接口不返回 credentials.api_key（已脱敏），所以只能按名字去重；
    本工具的名字里带 api_key 指纹，跨 config 版本仍指向同一凭据。

    返回 (名字集合, 出错信息或 None)。**取不全时一定返回错误**——
    以前出错会静默返回空集合，等于本次完全不去重，直接批量建出重名账号。
    """
    accs, err = _fetch_all_accounts(api)
    if err:
        return set(), err
    return {str(a.get("name")) for a in accs if a.get("name")}, None


def existing_accounts_by_name(api):
    """取回已有账号的 名字 -> 账号对象 映射，用于同步已存在账号。

    与 existing_account_names 的区别：那个只要名字集合（判断跳过与否就够了），
    这个还要 id 和当前字段值，才能算出"哪些字段真的变了"并只 PUT 变化项。
    两个函数共用 _fetch_all_accounts，不会多打一次接口的量级差别。

    返回 (名字->账号 dict, 出错信息或 None)。取不全时一定返回错误：
    半份数据会让同步误判成"这条不存在"，进而重复建号。
    """
    accs, err = _fetch_all_accounts(api)
    if err:
        return {}, err
    out = {}
    for a in accs:
        nm = a.get("name")
        if nm:
            out[str(nm)] = a
    return out, None


def diff_account_fields(existing, desired):
    """算出已存在账号需要更新的字段。返回 {字段: 新值}，空 dict 表示无需更新。

    只比对本工具真正负责的字段，**不碰** sub2api 侧的运行时状态
    （status / schedulable / error_message / temp_unschedulable_until 等）：
    那些由网关自己或运维手工掌管，导入工具覆盖它们等于把人家的运维操作冲掉。

    credentials 是整体替换语义（PUT 会覆盖整个 map），所以只要其中任一项
    有差异就整包重发；这也正是 model_mapping 能跟着 CPA 的 excluded-models
    变化而更新的原因——不重发就永远停留在首次导入时的快照。

    priority 必须参与比对：CPA 调整优先级后若不同步，sub2api 侧还在用旧桶号，
    而两边都是按优先级严格分桶调度的（见 remap_priority 的说明），
    桶号错了等于调度顺序整个错位。
    """
    patch = {}

    # --- 标量字段：逐个比对，只发变化的 ---
    # load_factor 参与比对：健康度变化后同桶内的分担比例要跟着变，
    # 不同步等于把负载分配冻结在首次导入那一刻（见 build_account_body 的说明）。
    for key in ("priority", "concurrency", "auto_pause_on_expired", "proxy_id",
                "load_factor"):
        if key not in desired:
            continue
        new = desired[key]
        old = existing.get(key)
        if key == "auto_pause_on_expired":
            if bool(old) != bool(new):
                patch[key] = new
            continue
        if _is_int(new) and _is_int(old):
            if int(old) != int(new):
                patch[key] = new
        elif old != new:
            patch[key] = new

    # --- credentials：任一子项有差异就整包重发 ---
    new_creds = desired.get("credentials") or {}
    old_creds = existing.get("credentials") or {}
    if not isinstance(old_creds, dict):
        old_creds = {}
    for k, v in new_creds.items():
        # 列表接口会把 api_key 脱敏成掩码，拿它跟明文比会永远判定"有变化"，
        # 于是每次运行都无谓地重发一遍全部账号。api_key 的变化会体现在
        # 账号名的指纹上（account_fingerprint），由建号/清理路径负责，
        # 这里不参与比对。
        if k == "api_key":
            continue
        if old_creds.get(k) != v:
            patch["credentials"] = new_creds
            break

    # --- extra：同样整体比对 ---
    if "extra" in desired:
        old_extra = existing.get("extra") or {}
        if not isinstance(old_extra, dict):
            old_extra = {}
        new_extra = desired["extra"] or {}
        for k, v in new_extra.items():
            if old_extra.get(k) != v:
                patch["extra"] = new_extra
                break

    # --- notes：带 sentinel，变了说明未导入项清单变了，值得同步 ---
    if desired.get("notes") and existing.get("notes") != desired["notes"]:
        patch["notes"] = desired["notes"]

    return patch


def sync_existing_accounts(api, recs, cfg, gmap, pmap, have_map, dry_run=False):
    """把已存在账号的可变字段同步成 config.yaml 的最新值。

    解决的问题：本工具原先对已存在的账号一律"跳过"（按名字去重），于是在 CPA
    里改了 priority / excluded-models / weight / base_url 之后重跑本工具，
    sub2api 侧仍是首次导入时的旧值，而且不会有任何提示。对
    excluded-models 尤其致命——model_mapping 是白名单快照，CPA 放开限制后
    这边仍然挡着,账号看起来正常却一个模型都不提供。

    只更新**内容真的变了**的字段（见 diff_account_fields），没变的一条都不发：
    226 条全量 PUT 一遍既慢又会把 updated_at 全部搅动，掩盖真实的变更记录。

    返回 (更新成功数, 无需更新数, 失败列表)。
    """
    # 2026-09-13：构建代理需求映射，供 to_account 使用
    proxy_need = build_proxy_need_map(recs)

    ok, same, fail = 0, 0, []
    for r in recs:
        cur = have_map.get(r["name"])
        if not cur:
            continue
        aid = cur.get("id")
        if not aid:
            fail.append((r["name"], "已有账号没有 id，无法更新"))
            continue
        desired = to_account(r, cfg, gmap, pmap, proxy_need)
        # 建号专用字段，PUT 时不需要也不该带
        for k in ("name", "platform", "type", "confirm_mixed_channel_risk"):
            desired.pop(k, None)
        patch = diff_account_fields(cur, desired)
        if not patch:
            same += 1
            continue
        if dry_run:
            ok += 1
            continue
        try:
            api.update_account(aid, patch)
            ok += 1
        except ApiError as ex:
            fail.append((r["name"], "HTTP %s %s" % (ex.status, ex.body[:80])))
        except Exception as ex:
            fail.append((r["name"], str(ex)[:80]))
    return ok, same, fail


def reclaim_schedulable(api, dry_run=False, batch=50):
    """把"状态正常但调度开关被关掉"的账号重新打开。

    这是本工具为 sub2api 的一个恢复缺口打的补丁，不是凭空加的功能：

      · 账号出错时，sub2api 在**同一次写入**里同时关掉状态和调度开关
        （account_repo.go:1750 SetError：SetStatus(StatusError) 紧跟
        SetSchedulable(false)）。
      · 但恢复路径只还了一半：ClearError（account_repo.go:1750 往下）
        只做 SetStatus(StatusActive) + SetErrorMessage("")，**没有**
        SetSchedulable(true)。全仓搜索 SetSchedulable(true)，生产代码里
        零调用点，只在测试文件出现。
      · 定时探活计划的 auto_recover 最终也是落到 ClearError
        （scheduled_test_runner_service.go 的 tryRecoverAccount ->
        RecoverAccountAfterSuccessfulTest -> RecoverAccountState -> ClearError），
        所以它同样只还状态、不还开关。

    结果就是账号永久卡在 status=active + schedulable=false：界面上看着是
    "正常"，但调度判定是两者取与（account.go:181 IsSchedulable 先查
    IsActive() 再查 Schedulable；SQL 侧 group_repo.go:982 同样是
    status='active' AND schedulable=true），于是它永远接不到请求，
    只能人工逐行去点开关。上游临时抽风越多，这种"僵尸账号"攒得越多。

    安全边界——只恢复本工具导入的账号：
    sub2api 自己不区分"网关自动关的"和"运维手工关的"（不像 new-api 有
    ManuallyDisabled=2 / AutoDisabled=3 两个状态），所以这个区分必须在
    客户端做，否则会把运维特意关掉的账号又打开。这里用 notes 的
    NOTE_SENTINEL 前缀来认定，跟回滚/清空用的是同一套认定标准。

    返回 (恢复条数, 候选条数, 出错信息或 None)。
    """
    if not BULK_HAS_SCHEDULABLE:
        # sync_constants 已经从源码确认 bulk-update 不再接受 schedulable。
        # 这时候硬发会被 Gin 静默丢弃——请求返回 200、开关却没打开，
        # 比直接报错更难查，所以宁可不做并说明白。
        return 0, 0, ("上游的 bulk-update 已不接受 schedulable 字段，"
                      "无法批量恢复调度开关，请人工处理或更新本工具。")
    accs, err = _fetch_all_accounts(api)
    if err:
        return 0, 0, err

    targets = []
    for a in accs:
        # 只认本工具导入的账号
        if not _is_migrated(a):
            continue
        # 状态必须已经是正常的：还在 error 的说明上游真的没恢复，
        # 这时候强行打开调度开关只会让请求继续打到坏账号上。
        # 交给 sub2api 的探活计划先把状态修好，下一轮再由这里接手。
        st = str(a.get("status") or "").strip().lower()
        if st != "active":
            continue
        # 已经是可调度的不用管
        if a.get("schedulable"):
            continue
        # 仍在冷却窗口里的不动：temp_unschedulable_until 是有到期时间的
        # 临时状态，sub2api 自己会让它失效，不需要也不该干预。
        if a.get("temp_unschedulable_until"):
            continue
        aid = a.get("id")
        if aid:
            targets.append(aid)

    if not targets or dry_run:
        return 0, len(targets), None

    done = 0
    for i in range(0, len(targets), batch):
        chunk = targets[i:i + batch]
        try:
            # status 一并带上是为了幂等：这批账号本来就是 active，
            # 重复写同值不会有副作用，但能保证即使中途被打回 error
            # 也会连状态一起修正。
            api.bulk_update_accounts(chunk, {"status": "active",
                                             "schedulable": True})
            done += len(chunk)
        except ApiError as ex:
            return done, len(targets), "HTTP %s %s" % (ex.status, ex.body[:120])
        except Exception as ex:
            return done, len(targets), str(ex)[:120]
    return done, len(targets), None


def revive_proven_inactive(api, cfg, dry_run=False, batch=50):
    """把「探活已证明可用」的停用账号重新启用（Problem 6.3）。

    要解决的断链：config.yaml 里 disabled / 被 excluded-models 排空的上游，
    本工具会建成 status=inactive。而 sub2api 的自动恢复**只认 error**
    （ratelimit_service.go:2109 `if account.Status == StatusError`），
    inactive 永远不会被它转回 active；本工具的 reclaim_schedulable 又只处理
    已经是 active 的账号。于是这些站点即使恢复了也永久躺平——实测 47/231。

    做法是**证据驱动**，不是无脑拉起：
      1. 只看本工具导入的、当前 status=inactive 的账号；
      2. 读它的探活计划最近一次结果，必须是成功；
      3. 成功才置 active + schedulable=true。
    探活由第 5 步给停用账号也挂上（probe_inactive），所以证据是有的。

    为什么不能省掉证据这一步：config.yaml 把站点关掉通常是有原因的
    （余额耗尽 / key 被吊销）。没有探活成功记录就拉起来，等于把坏号重新
    放回调度池，反而拖低整体成功率。

    默认关闭（revive_proven_inactive=false）：这是会改变调度面的写操作，
    要用户显式开启。返回 (启用条数, 候选条数, 出错信息或 None)。
    """
    if not cfg.get("revive_proven_inactive", False):
        return 0, 0, None

    accs, err = _fetch_all_accounts(api)
    if err:
        return 0, 0, err

    cands = []
    for a in accs:
        if not _is_migrated(a):
            continue
        if str(a.get("status") or "").strip().lower() != "inactive":
            continue
        aid = a.get("id")
        if aid:
            cands.append((aid, str(a.get("name") or "")))

    if not cands:
        return 0, 0, None

    def proven_ok(item):
        """这条账号最近一次探活是否成功。取不到结果一律当成没证据。"""
        aid, nm = item
        try:
            plans = _unwrap_list(api.list_test_plans(aid))
        except Exception:
            return None
        for p in plans:
            pid = p.get("id")
            if not pid:
                continue
            try:
                results = _unwrap_list(api.list_test_results(pid))
            except Exception:
                continue
            # 结果按时间倒序返回；只认最近一条，历史成功不代表现在能用
            for r in results[:1]:
                st = str(r.get("status") or "").strip().lower()
                if st in ("success", "ok", "passed"):
                    return (aid, nm)
        return None

    proven = []
    workers = max(1, min(int(cfg.get("test_plan_workers", 8) or 8), 8))
    if len(cands) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for got in pool.map(proven_ok, cands):
                if got:
                    proven.append(got)
    else:
        for c in cands:
            got = proven_ok(c)
            if got:
                proven.append(got)

    if not proven or dry_run:
        return 0, len(proven), None

    # sub2api 无批量更新 API，逐条调用 update_account
    done = 0
    for aid, nm in proven:
        try:
            api.update_account(aid, {"status": "active", "schedulable": True})
            done += 1
        except ApiError as ex:
            # 某条失败不中断整批，收集首个错误返回
            if done == 0:
                return 0, len(proven), "HTTP %s %s" % (ex.status, ex.body[:120])
        except Exception as ex:
            if done == 0:
                return 0, len(proven), str(ex)[:120]
    return done, len(proven), None


def _unwrap_list(resp):
    """从 sub2api 的响应里取出列表，兼容 {data:[...]} / {data:{items:[...]}} / [...]。"""
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    if not isinstance(resp, dict):
        return []
    d = resp.get("data")
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        for k in ("items", "list", "results", "records"):
            v = d.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    for k in ("items", "list", "results", "records"):
        v = resp.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def find_stale_accounts(api, recs):
    """找出"本工具导过、但当前 config.yaml 里已经没有对应凭据"的僵尸账号。

    成因：账号名是 `前缀-主机-指纹`（assign_names），指纹取自完整 api_key
    （account_fingerprint）。在 CPA 侧把某条上游的 key 轮换掉之后，指纹跟着变，
    本工具会按新名字建一条新账号——而**旧账号不会被动到**，因为去重和同步
    都是按名字比对的，旧名字在新一轮的 recs 里根本不存在。

    于是每轮换一次 key 就多留一条僵尸：它带着失效的凭据留在调度池里，
    还会被选中、失败、然后把自己打成 error+schedulable=false（见
    reclaim_schedulable 的说明）。用户侧的表现就是"账号越攒越多，
    而且总有一批莫名其妙是关着的"。

    这里只负责**识别并报告**，不自动删除：删号不可逆，而且"config 里没有"
    也可能是用户临时注释掉了一段配置，过一会儿还要加回来。
    交给调用方决定是列出来给人看，还是走已有的 purge/wipe 流程。

    返回 (僵尸账号列表, 出错信息或 None)。
    """
    accs, err = _fetch_all_accounts(api)
    if err:
        return [], err
    want = {r["name"] for r in recs}
    stale = []
    for a in accs:
        if not _is_migrated(a):
            continue
        nm = str(a.get("name") or "")
        if nm and nm not in want:
            stale.append(a)
    return stale, None


def _cron_with_offset(cron, offset):
    """给 `*/N` 形式的 cron 加一个分钟偏移，让不同账号错开探活时刻。

    为什么要错开：探活计划用的是同一个 cron，于是所有账号在**同一分钟**
    一起向上游发请求。这个"同时性"比 prompt 的措辞更容易被识别为机器行为
    —— 真实用户的请求时刻是分散的。

    只处理最常用的两种写法，其余原样返回（不认识就不要乱改）：
        "*/30 * * * *" + 7  ->  "7,37 * * * *"
        "*/15 * * * *" + 3  ->  "3,18,33,48 * * * *"
    偏移会被规整到 [0, N) 区间内，所以传任意整数都安全。
    """
    s = str(cron or "").strip()
    parts = s.split()
    if len(parts) != 5 or not parts[0].startswith("*/"):
        return s
    try:
        step = int(parts[0][2:])
    except ValueError:
        return s
    if step <= 0:
        return s
    off = int(offset) % step
    minutes = list(range(off, 60, step))
    if not minutes:
        return s
    parts[0] = ",".join(str(m) for m in minutes)
    return " ".join(parts)


def _pick_probe_model(model_mapping, platform):
    """从账号的 model_mapping 里挑一个探活用的模型 id，挑不到返回 None。

    规则必须与写入白名单时的就高原则一致，所以直接复用 model_selection
    的选型函数，而不是在本地重写一份正则。差异化的那一部分（"探活挑哪一个"）
    才是这里独有的：
      · claude 优先 sonnet（额度便宜、响应快），其次 opus，最后 haiku；
      · 其它平台取就高结果里的第一个。
    """
    if not model_mapping or not isinstance(model_mapping, dict):
        return None

    keys = [str(k) for k in model_mapping.keys()]
    if not MODEL_SELECTION_AVAILABLE or not platform:
        return keys[0] if keys else None

    models = [{"name": k, "alias": k} for k in keys]
    picked = select_highest_models(models, platform, series=MODEL_SERIES)
    names = [m["name"] for m in picked] or keys

    if platform == "anthropic":
        for preferred in ("sonnet", "opus", "haiku"):
            for n in names:
                if preferred in n.lower():
                    return n
    return names[0] if names else None


def ensure_test_plans(api, name_to_id, cfg, dry_run=False):
    """给账号挂上 sub2api 自带的定时探活计划（auto_recover=true）。

    用上游已有的机制，不自己造轮子：sub2api 的 scheduled_test_plans 表
    有 auto_recover 字段（migrations/070_add_scheduled_test_auto_recover.sql，
    默认 false），runner 每分钟扫一遍，测试成功且该字段为真时调用
    tryRecoverAccount -> RecoverAccountAfterSuccessfulTest，把账号从
    error / rate-limited 状态里捞回来（scheduled_test_runner_service.go:133-168）。

    但要说清楚它的**局限**：这条链路最终落到 ClearError，只还 status、
    不还 schedulable（见 reclaim_schedulable 的详细说明）。所以探活计划
    负责"把状态修回 active"，reclaim_schedulable 负责"把调度开关打开"，
    两个是互补的，缺一不可，不能只配其中一个。

    探活会真的向上游发请求、产生费用，所以默认 cron 是半小时一次，
    且整个功能可以用 auto_recover_enabled=false 关掉。

    返回 (成功数, 失败列表)。
    """
    if not cfg.get("auto_recover_enabled", True):
        return 0, []
    cron = str(cfg.get("auto_recover_cron") or "*/30 * * * *").strip()
    if not cron:
        return 0, []

    # 与其它三处并发一致地钳到 8。以前这里是裸的
    # int(cfg.get(...))，用户把 test_plan_workers 配成 100 就会真起 100 个线程
    # 同时打 /accounts/{id}，把 sub2api 和自己的连接池一起压垮。
    max_workers = max(1, min(int(cfg.get("test_plan_workers", 8) or 8), 8))
    items = [(nm, aid) for nm, aid in sorted(name_to_id.items()) if aid]

    if dry_run:
        return len(items), []

    # 防测活 prompt 池（文档第 2 条⑷）。
    #
    # 为什么不能用"test"/"你好"/"你是什么模型"：那是测活系统的教科书特征，
    # 一次就能被识别。
    #
    # 但换掉措辞只解决了一半。真正更强的机器特征是**调用模式的规律性**：
    # 同一批账号在同一个 cron 时刻、用同一条 prompt、以相同节奏反复发问，
    # 曲线平滑得像服务器监控图 —— 间隔方差为 0 本身就是异常信号。
    # 所以这里做三件事：
    #   1. prompt 池分中英双语、覆盖多种话题（单一语种的池在中文站里
    #      反而是统计上的异常样本）；
    #   2. 每次调用现场随机抽（见下面 random.choice）；
    #   3. **给每个账号错开分钟偏移**（见 _cron_with_offset），
    #      避免全部账号在同一分钟一起探活。
    #
    # 仍然保持简短：探活只需验证连通性，请求体越大越容易被限流。
    anti_detect_prompts = [
        # —— 英文 ——
        "What's the weather like today?",
        "Can you help me write an email?",
        "Explain the concept of recursion with an example.",
        "What are some healthy breakfast ideas for a busy morning?",
        "How do I center a div using flexbox in CSS?",
        "What's the difference between a list and a tuple in Python?",
        "Suggest a good sci-fi book for beginners.",
        "How can I improve my daily productivity at work?",
        "Give me a simple recipe for tomato pasta.",
        "How does HTTPS work in simple terms?",
        "What are the best practices for writing clean code?",
        "Explain what machine learning is in one paragraph.",
        "What time does the sun rise today?",
        "How do I convert a PDF to an editable document?",
        "What should I look for when buying a used laptop?",
        # —— 中文 ——
        "今天北京的天气怎么样？",
        "帮我把这句话改得更正式一些。",
        "解释一下什么是递归，举个例子。",
        "推荐几个适合上班族的简单早餐。",
        "怎么用 CSS 让一个 div 水平居中？",
        "Python 里列表和元组的区别是什么？",
        "推荐一本适合入门看的书。",
        "怎么提高工作效率？",
        "写一段自我介绍，用于邮件开头。",
        "解释一下 HTTPS 的基本原理。",
        "什么是机器学习？用一段话说明。",
        "帮我把下面这段话说得更简洁一点。",
        "如何快速整理一份会议纪要？",
        "解释一下什么是数据库索引。",
        "有什么办法能减少日常的重复工作？",
    ]

    def create_test_plan_for_account(nm, aid):
        """为单个账号创建探活计划"""
        try:
            acc = api._call("GET", f"/api/v1/admin/accounts/{aid}")
        except Exception as ex:
            return nm, False, f"获取账号失败: {str(ex)[:60]}"

        creds = acc.get("credentials", {})
        model_mapping = creds.get("model_mapping", {})
        model_id = None
        platform = acc.get("platform", "")
        # 探活挑哪个模型来打，必须和账号白名单用的是**同一套就高规则**。
        # 以前这里有第三份独立拷贝：gemini 正则、claude 的 `-5` 字面、
        # openai 的 [r'^gpt-6', r'^o3-', r'^gpt-5'] 全部手写在这里，
        # 与 model_selection.py 各改各的。后果是探活可能挑一个白名单里
        # 根本没放行的模型（比如白名单已按当代门槛裁到 gpt-7，探活还在打 gpt-6），
        # 那类失败会被误读成"账号不可用"。
        # 现在统一走 model_selection，门槛也跟随上游同步。
        model_id = _pick_probe_model(model_mapping, platform)

        if not model_id and model_mapping:
            model_id = next(iter(model_mapping.keys()))

        if not model_id:
            return nm, False, "未找到可用模型"

        import random
        test_prompt = random.choice(anti_detect_prompts)

        # 用账号名派生一个稳定的分钟偏移：同一账号每次得到同一个偏移
        # （计划是幂等的，重复运行不该把已排好的时刻改来改去），
        # 而不同账号之间会散开，避免全部账号在同一分钟一起探活。
        # 用 sha1 而不是内置 hash()：后者在 Python 3 里带进程随机盐，
        # 同一账号每次运行会得到不同偏移，正是要避免的。
        off = int(hashlib.sha1(str(nm).encode("utf-8")).hexdigest()[:4], 16)
        my_cron = _cron_with_offset(cron, off)

        body = {"account_id": aid, "cron_expression": my_cron,
                "enabled": True, "auto_recover": True,
                "model_id": model_id,
                "prompt": test_prompt}
        try:
            api.create_test_plan(body)
            return nm, True, None
        except ApiError as ex:
            if ex.status == 409 or "exist" in ex.body.lower():
                return nm, True, None
            return nm, False, "HTTP %s %s" % (ex.status, ex.body[:80])
        except Exception as ex:
            return nm, False, str(ex)[:80]

    ok, fail = 0, []

    if len(items) > 1 and max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(create_test_plan_for_account, nm, aid): (nm, aid)
                      for nm, aid in items}

            for future in as_completed(futures):
                try:
                    nm, success, error = future.result()
                    if success:
                        ok += 1
                    else:
                        fail.append((nm, error))
                except Exception as ex:
                    nm, aid = futures[future]
                    fail.append((nm, str(ex)[:80]))
    else:
        for nm, aid in items:
            nm, success, error = create_test_plan_for_account(nm, aid)
            if success:
                ok += 1
            else:
                fail.append((nm, error))
    return ok, fail


def plan_fallback_chain(recs, groups):
    """自动推导分组之间的降级链（sub2api 的 groups.fallback_group_id）。

    ⚠️ 重要更正（已核对 v 当前源码）：这个字段**不是**"整组不可用时降级"。

    网关运行时只有一处消费它——resolveGatewayGroup（gateway_scheduling.go:
    940-948）：

        if !group.ClaudeCodeOnly || IsClaudeCodeClient(ctx) {
            return group                    // 直接返回，压根没读 FallbackGroupID
        }
        if group.FallbackGroupID == nil { return ErrClaudeCodeOnly }
        currentID = *group.FallbackGroupID

    也就是说它**只在 claude_code_only=true 且请求不来自 Claude Code 客户端**
    时才被读取，用途是"非 CC 客户端改投别的分组"。源码注释
    （gateway_scheduling.go:951-954，checkClaudeCodeRestriction 上方）说得很
    明确。其余 50 余处引用全是管理端 CRUD / 缓存快照 / 环路校验，与调度无关。

    本工具建出来的分组 claude_code_only 一律为 false，所以这条链**永远不会
    触发**。保留本函数只为兼容既有配置和将来可能启用 claude_code_only 的
    场景，不要把它当作容错手段来依赖。

    真正生效的容错只有分桶那两层：
      1. 同一上游域名的多个 KEY 同桶轮循（_host_tier_map）；
      2. 整个域名不可用时自动落到下一个域名的桶（严格分桶天然具备）。
    跨到最大切换次数（gateway.max_account_switches，默认 10；gemini 走独立
    的 max_account_switches_gemini，默认 3）之后就返回错误，**分组级降级这
    一层在当前版本不存在**。

    另有一个名字相近但完全独立的字段 fallback_group_id_on_invalid_request，
    它确实走网关主流程（gateway_handler.go:615、:996），但只在
    "prompt too long" 且目标分组为 anthropic 非订阅制时才生效，用途很窄，
    本工具不设置它。

    推导规则（不需要用户额外配置，全部从 CPA 的 config.yaml 推出来）：
      · **只在同 platform 的分组之间串**。服务端的 validateFallbackGroup
        只校验环路、自指和 claude_code_only，**不校验平台**，所以跨平台
        配置能存进去却会在运行时把请求打到根本不支持该模型的分组上。
        这个约束必须由本工具自己守住。
      · 同平台内按"分组内最高 CPA priority"降序串联：优先级高的分组
        在前，挂了往优先级低的降。这正好复用用户在 config.yaml 里已经
        表达过的优先级意图，不需要另设一套配置。
      · 只有一个分组的平台不串（没有可降级的对象）。
      · 链是单向的，最后一个分组不设 fallback，天然无环。

    返回 {分组短名: 降级到的分组短名}。调用方负责把短名换成 sub2api 的
    分组 id 再 PUT。
    """
    # 每个分组的平台，以及该分组内出现过的最高 CPA priority
    g_plat, g_top = {}, {}
    for r in recs:
        g = r["group"]
        if g not in groups:
            continue
        g_plat.setdefault(g, r["platform"])
        p = r["priority"] if _is_int(r["priority"]) else 0
        if g not in g_top or p > g_top[g]:
            g_top[g] = p

    by_plat = defaultdict(list)
    for g, plat in g_plat.items():
        by_plat[plat].append(g)

    chain = {}
    for plat, gs in by_plat.items():
        if len(gs) < 2:
            continue
        # 高优先级在前；同优先级按名字定序，保证多次运行结果稳定
        gs.sort(key=lambda g: (-g_top.get(g, 0), g))
        for a, b in zip(gs, gs[1:]):
            chain[a] = b
    return chain


def apply_fallback_chain(api, chain, gmap, dry_run=False):
    """把推导出的降级链写进 sub2api。

    用 PUT /admin/groups/:id 的 fallback_group_id 字段。必须**逆序**写入
    （先写链尾、再写链头）：服务端在 validateFallbackGroup 里会沿着整条链
    往下走一遍（admin_group.go:695-710），如果链尾的分组还没建好或还没
    设置，校验会在中途报 "fallback group not found"。

    注意 rate_multiplier 不能顺带发：它的校验是 `<=0 就报错`，而且用的是
    裸 errors.New（admin_group.go:772），会变成 HTTP 500 而不是 400，
    看不出真实原因。这里只发 fallback_group_id 一个字段。

    返回 (成功数, 失败列表)。
    """
    ok, fail = 0, []
    # 逆序：把没有下游的排前面
    order = sorted(chain.keys(),
                   key=lambda g: len(_chain_tail(chain, g)))
    for g in order:
        tgt = chain[g]
        gid, tid = gmap.get(g), gmap.get(tgt)
        if not gid or not tid:
            fail.append((g, "分组 id 缺失，跳过降级设置"))
            continue
        if dry_run:
            ok += 1
            continue
        try:
            api.update_group(gid, {"fallback_group_id": tid})
            ok += 1
        except ApiError as ex:
            fail.append((g, "HTTP %s %s" % (ex.status, ex.body[:80])))
        except Exception as ex:
            fail.append((g, str(ex)[:80]))
    return ok, fail


def _chain_tail(chain, g, limit=20):
    """从 g 出发沿降级链能走到的后继列表，用于排序和防环。

    limit 是兜底：推导出来的链本来就是单向无环的，但如果将来有人手工
    改过 chain，这里不能变成死循环。
    """
    out, cur, seen = [], chain.get(g), {g}
    while cur and cur not in seen and len(out) < limit:
        out.append(cur)
        seen.add(cur)
        cur = chain.get(cur)
    return out


def _pricing_entries_for_group(prices, platform, models_wanted=None):
    """把解析出的价格表拼成 sub2api 的 model_pricing 数组。

    sub2api 的分组逐模型定价复用 ChannelModelPricing 结构
    （UpdateGroupRequest.ModelPricing 是 *[]service.ChannelModelPricing），
    一条 entry 可以绑定多个模型（models 数组），价格相同的模型合并成一条
    能显著压缩条数——780 个模型逐条发会让请求体非常大。

    models_wanted 给定时只输出这些模型（按分组实际能用的模型裁剪），
    传 None 表示全量。
    """
    import pricing as PR

    # 相同价格的模型合并到同一条 entry
    buckets = {}
    for name, rec in prices.items():
        if models_wanted is not None and name not in models_wanted:
            continue
        pay = PR.to_sub2api_pricing(rec)
        if not pay:
            continue
        key = tuple(sorted((k, repr(v)) for k, v in pay.items()))
        buckets.setdefault(key, (pay, []))[1].append(name)

    out = []
    for _, (pay, names) in buckets.items():
        entry = {"platform": platform, "models": sorted(names),
                 "billing_mode": "per_request" if "per_request_price" in pay
                                 else "token"}
        entry.update(pay)
        out.append(entry)
    # 稳定排序：同样的输入必须产出同样的数组，否则每次都判定"有变化"
    out.sort(key=lambda e: e["models"][0] if e["models"] else "")
    return out


def diff_pricing(old, new, eps=FLOAT_EPSILON):
    """比对两份 model_pricing，判断是否需要写入。

    浮点必须按 epsilon 比，不能用 ==：价格经 JSON 往返会有末位误差
    （0.0000021 存回来可能是 0.0000020999999999999998），用相等比较会
    永远认为有变化、每次运行都重推一遍 780 个模型。对齐 new-api
    ratio_sync 的 floatEpsilon=1e-9。

    返回 (是否需要更新, 变化说明列表)。
    """
    def flatten(entries):
        """展开成 {模型名: {字段: 值}}，忽略 entry 的分组方式。

        同样的价格既可以拆成多条 entry、也可以合并成一条，服务端行为一致。
        按模型名展开比较，才不会因为"合并方式变了"而误判成价格变了。
        """
        out = {}
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            for m in e.get("models") or []:
                row = {}
                for k, v in e.items():
                    if k in ("models", "platform", "id", "channel_id",
                             "created_at", "updated_at"):
                        continue
                    row[k] = v
                out[str(m)] = row
        return out

    a, b = flatten(old), flatten(new)
    changes = []
    for m in sorted(set(a) | set(b)):
        ra, rb = a.get(m), b.get(m)
        if ra is None:
            changes.append("+ %s 新增定价" % m)
            continue
        if rb is None:
            # 本工具只增不删：价格表里没有的模型可能是用户手工配的，
            # 不能因为"我这份数据里没有"就抹掉。
            continue
        for k in sorted(set(ra) | set(rb)):
            va, vb = ra.get(k), rb.get(k)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                if abs(float(va) - float(vb)) > eps:
                    changes.append("~ %s.%s %.12g -> %.12g" % (m, k, va, vb))
            elif va != vb:
                changes.append("~ %s.%s %r -> %r" % (m, k, va, vb))
    return bool(changes), changes


def push_pricing(api, prices, gmap, group_platforms, dry_run=False,
                 workers=8, on_log=None):
    """把价格表推送到各分组的逐模型定价。

    并发策略——**按分组切分，同一分组内串行**：
    渠道/分组定价在服务端是整体替换语义（channel_repo_pricing.go:306 先
    `DELETE FROM channel_model_pricing WHERE channel_id=$1` 再批量 INSERT），
    而且没有版本号、没有乐观锁。同一个实体并发写，后到的 DELETE 会把前一次
    的结果整个抹掉。所以并行度只能落在"实体之间"，每个分组一个任务、各自
    独占。并发上限对齐 new-api ratio_sync 的 maxConcurrentFetches=8，
    既能跑满又不会撞上服务端的限流中间件。

    先读后写、按 epsilon 差分，只推真的变了的分组：780 个模型的请求体不小，
    没变化还重推一遍既慢又会把 updated_at 全搅动，掩盖真实的变更记录。

    dry_run 时只计算差异、不发写请求。

    返回 (更新数, 无变化数, 失败列表, 差异明细 dict)。
    """
    try:
        import concurrent.futures as _fut
    except ImportError:
        _fut = None

    def log(msg):
        if on_log:
            on_log(msg)

    def one(gname):
        """处理一个分组。返回 (分组名, 状态, 说明, 变化列表)。"""
        gid = gmap.get(gname)
        if not gid:
            return (gname, "fail", "没有分组 id", [])
        platform = group_platforms.get(gname) or "anthropic"
        want = _pricing_entries_for_group(prices, platform)
        if not want:
            return (gname, "same", "没有可推送的价格", [])
        try:
            cur = api.get_group(gid)
        except ApiError as ex:
            return (gname, "fail", "读取分组失败 HTTP %s %s"
                    % (ex.status, ex.body[:80]), [])
        except Exception as ex:
            return (gname, "fail", "读取分组失败 %s" % str(ex)[:80], [])

        data = cur.get("data") if isinstance(cur, dict) else None
        old = (data or {}).get("model_pricing") if isinstance(data, dict) else None

        need, changes = diff_pricing(old, want)
        if not need:
            return (gname, "same", "价格无变化", [])
        if dry_run:
            return (gname, "would", "将更新 %d 处" % len(changes), changes)
        try:
            # 只发 model_pricing。**绝不顺带 rate_multiplier**：它的校验是
            # `<=0 报错` 且用裸 errors.New（admin_group.go:772），会变成
            # HTTP 500 而不是 400，排查时看不出真实原因。
            api.update_group(gid, {"model_pricing": want})
            return (gname, "ok", "已更新 %d 处" % len(changes), changes)
        except ApiError as ex:
            return (gname, "fail", "HTTP %s %s" % (ex.status, ex.body[:120]),
                    changes)
        except Exception as ex:
            return (gname, "fail", str(ex)[:120], changes)

    names = sorted(gmap.keys())
    results = []
    n = max(1, min(int(workers or 8), 8))
    if _fut and n > 1 and len(names) > 1:
        with _fut.ThreadPoolExecutor(max_workers=n) as pool:
            for r in pool.map(one, names):
                results.append(r)
                log("    %s：%s" % (r[0], r[2]))
    else:
        for gname in names:
            r = one(gname)
            results.append(r)
            log("    %s：%s" % (r[0], r[2]))

    ok = sum(1 for r in results if r[1] in ("ok", "would"))
    same = sum(1 for r in results if r[1] == "same")
    fail = [(r[0], r[2]) for r in results if r[1] == "fail"]
    detail = {r[0]: r[3] for r in results if r[3]}
    return ok, same, fail, detail


def _fetch_all_accounts(api):
    """翻页取回全部账号。返回 (账号列表, 出错信息或 None)。

    以 total 为准而不是"某页不足 page_size 就收工"：后者假设服务端严格返回
    请求的页大小，任何过滤或权限导致某页少返一条就会提前终止，账号取不全，
    去重和回滚都会漏。取不全时返回错误，不假装成功。
    """
    out, page, size, total = [], 1, 200, None
    while page <= 1000:
        try:
            d = api.list_accounts(page, size)
        except ApiError as ex:
            return out, "读账号列表失败：HTTP %s %s" % (ex.status, ex.body[:160])
        data = d.get("data")
        if isinstance(data, list):
            items, page_total = data, None
        elif isinstance(data, dict):
            items = (data.get("items") or data.get("list")
                     or data.get("accounts") or data.get("records") or [])
            page_total = data.get("total")
        else:
            items, page_total = [], None
        if total is None and isinstance(page_total, int):
            total = page_total
        out.extend(x for x in items if isinstance(x, dict))
        if not items:
            break
        if total is not None and len(out) >= total:
            break
        if total is None and len(items) < size:
            break
        page += 1

    if total is not None and len(out) != total:
        return out, "账号列表取不全：服务端报 %d 条，实际拿到 %d 条" % (total, len(out))
    return out, None


def wipe_all(api, s, ask, confirm):
    """清空本工具在 sub2api 里建的全部资源：账号、分组、代理。

    ask(提示) -> bool          用于「要不要连早期残留一起删」这类选择
    confirm(提示) -> str       用于最终的大写 DELETE 确认

    只删本工具建的东西：
      - 账号：notes 以 [cpa2sub2api] 开头的；早期版本（notes 含"源自 CPA"
        但无标记）单独问一次
      - 分组：名字以 设置.json 里的 group_prefix + "-" 开头的
      - 代理：名字以 "CPA-" 开头的
    sub2api 自带的 default 分组、以及用户自己建的东西一律不碰。

    返回 True 表示执行了删除，False 表示取消或无事可做。
    """
    accs, err = _fetch_all_accounts(api)
    if err:
        print("  ✗ %s" % err)
        print("  账号列表取不全时拒绝删除，避免只删掉一部分。")
        return False

    targets = [a for a in accs if _is_migrated(a)]
    legacy = [a for a in accs if _legacy_migrated(a)]
    others = len(accs) - len(targets) - len(legacy)

    print("  库里共 %d 条账号：" % len(accs))
    print("    本工具导入（带 %s 标记）  %d 条" % (NOTE_SENTINEL, len(targets)))
    print("    早期版本残留（无标记）       %d 条" % len(legacy))
    print("    其他（不是本工具建的）       %d 条  ← 不会动" % others)

    if legacy:
        print()
        print("  早期残留的判据是「备注里有『源自 CPA』字样但没有新标记」。")
        print("  如果你手工建过备注含这句话的账号，它也会被算进去。示例：")
        for a in legacy[:3]:
            print("     %-32s %s" % (str(a.get("name"))[:32],
                                     str(a.get("notes") or "")[:38]))
        if ask("  这 %d 条早期残留也一起删？输入 y 回车：" % len(legacy)):
            targets = targets + legacy
        else:
            print("  早期残留保留，只删带标记的。")

    gprefix = s["group_prefix"] + "-"
    gtargets = []
    try:
        for g in api.list_groups():
            if str(g.get("name") or "").startswith(gprefix):
                gtargets.append(g)
    except ApiError as ex:
        print("  ! 读不到分组列表（HTTP %s），本次不删分组。" % ex.status)

    ptargets = []
    try:
        for p in api.list_proxies():
            if str(p.get("name") or "").startswith("CPA-"):
                ptargets.append(p)
    except ApiError as ex:
        print("  ! 读不到代理列表（HTTP %s），本次不删代理。" % ex.status)

    print()
    print("  将删除：")
    print("    账号 %d 条" % len(targets))
    print("    分组 %d 个：%s" % (len(gtargets),
                                "、".join(str(g.get("name")) for g in gtargets) or "无"))
    print("    代理 %d 个：%s" % (len(ptargets),
                                "、".join(str(p.get("name")) for p in ptargets) or "无"))
    print("    其余 %d 条账号、default 分组、你自己建的资源都不动"
          % (len(accs) - len(targets)))

    if not targets and not gtargets and not ptargets:
        print("\n  没有需要删除的东西。")
        return False

    print()
    print("  删除不可撤销。sub2api 的接口不返回 api_key，删掉的密钥拿不回来。")
    print("  （会先把待删账号的清单写到 out/，但清单里没有密钥。）")
    if confirm("  确认请输入大写 DELETE 回车，其他任意键取消：") != "DELETE":
        print("  已取消，什么都没动。")
        return False

    bak = backup_accounts(targets, "wipe")
    if bak:
        print("  清单已写到 %s" % os.path.basename(bak))

    print("  删除中...")
    aok, _p, fails = purge(api, targets, [])
    print("  账号 %d/%d" % (aok, len(targets)))

    # 分组要在账号之后删：sub2api 删分组会级联解绑，先删分组没意义
    gok = 0
    for g in gtargets:
        gid = g.get("id")
        if not isinstance(gid, int):
            continue
        try:
            api.delete_group(gid)
            gok += 1
        except ApiError as ex:
            fails.append("分组 %s：HTTP %s %s" % (g.get("name"), ex.status, ex.body[:60]))
    print("  分组 %d/%d" % (gok, len(gtargets)))

    # 代理最后删：被账号引用时 sub2api 返回 409 且不级联，账号删完才能删掉
    pok = 0
    for p in ptargets:
        pid = p.get("id")
        if not isinstance(pid, int):
            continue
        try:
            api.delete_proxy(pid)
            pok += 1
        except ApiError as ex:
            if ex.status == 409:
                fails.append("代理 %s：还被其他账号引用，未删" % p.get("name"))
            else:
                fails.append("代理 %s：HTTP %s" % (p.get("name"), ex.status))
    print("  代理 %d/%d" % (pok, len(ptargets)))

    if fails:
        print("\n  ! %d 项未删掉：" % len(fails))
        for x in fails[:10]:
            print("     %s" % x)
        if len(fails) > 10:
            print("     ... 其余 %d 项" % (len(fails) - 10))
    return True


def act_pricing(s, dry_run=False):
    """菜单 [6]：一键提交价格清单到各分组的逐模型定价。

    数据源优先级：同目录的 .mhtml（权威、最新）> 价格工具目录的 pri.txt
    （预清洗、体积小）。两个都在就以 mhtml 为准、用 pri.txt 补漏。
    路径可用 设置.json 的 price_mhtml / price_pri / price_model_txt 覆盖，
    默认按常见位置探测——不写死绝对路径，换机器也能跑。
    """
    print("\n" + "=" * 60)
    print(" 提交价格清单" + ("（预演，不写入）" if dry_run else ""))
    print("=" * 60)
    if not s["sub2api_admin_key"]:
        print(" 还没设置 sub2api 管理密钥，请先在 设置.json 里填。")
        return

    try:
        import pricing as PR
    except Exception as ex:
        print(" 载入 pricing 模块失败：%s" % ex)
        return

    mh, pri, mtxt = _price_paths(s)
    print(" 数据源：")
    print("   mhtml     : %s" % (mh or "（未找到）"))
    print("   pri.txt   : %s" % (pri or "（未找到）"))
    print("   model.txt : %s" % (mtxt or "（未找到）"))
    if not mtxt:
        print("\n 缺少 model.txt，无法锚定模型名——解析会把厂商名误当模型。")
        print(" 请在 设置.json 里设置 price_model_txt 指向它。")
        return

    try:
        prices, notes = PR.load_prices(mhtml_path=mh, pri_path=pri,
                                       model_txt=mtxt)
    except Exception as ex:
        print("\n 解析价格失败：%s" % ex)
        return
    for n in notes:
        print("   %s" % n)

    # 跨平台一致性自检：把各平台口径折回 USD 再比，不一致就别推。
    bad = []
    for nm, rec in prices.items():
        ok2, why = PR.cross_platform_check(rec)
        if not ok2:
            bad.append((nm, why))
    if bad:
        print("\n ! 换算自检发现 %d 个模型跨平台不一致，已中止：" % len(bad))
        for nm, why in bad[:5]:
            print("     %s：%s" % (nm, why))
        return
    print("   换算自检：%d 个模型跨平台一致" % len(prices))

    api = Sub2Api(s)
    try:
        existing = {g["name"]: g["id"] for g in api.list_groups()
                    if g.get("name")}
    except ApiError as ex:
        print("\n 读取分组失败：HTTP %s %s" % (ex.status, ex.body[:120]))
        return

    # 只给本工具建的分组推价（名字带 group_prefix），不碰用户自建的分组
    prefix = s.get("group_prefix", "CPA")
    gmap, plats = {}, {}
    for short, plat in GROUP_PLATFORM.items():
        full = "%s-%s" % (prefix, short)
        if full in existing:
            gmap[short] = existing[full]
            plats[short] = plat
    if not gmap:
        print("\n 没找到本工具建的分组（前缀 %s-），先跑一次导入。" % prefix)
        return
    print("\n 目标分组：%s" % "、".join(sorted(gmap)))

    if not dry_run:
        if input("\n 确认写入 %s ？(y/N) " % api.base).strip().lower() != "y":
            print(" 已取消。")
            return

    print("")
    ok, same, fail, detail = push_pricing(
        api, prices, gmap, plats, dry_run=dry_run,
        workers=s.get("pricing_workers", 8),
        on_log=lambda m: print(m))

    print("\n %s：更新 %d 个分组，无变化 %d 个，失败 %d 个"
          % ("预演" if dry_run else "完成", ok, same, len(fail)))
    for g, why in fail[:5]:
        print("   ✗ %s：%s" % (g, why))

    # 差异明细落盘，便于核对——尤其是预演模式，这是它唯一的产出
    if detail:
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            p = os.path.join(OUT_DIR, "pricing-diff.json")
            with io.open(p, "w", encoding="utf-8") as f:
                json.dump(detail, f, ensure_ascii=False, indent=2)
            print("   差异明细：%s" % p)
        except Exception as ex:
            print("   差异明细写入失败：%s" % ex)


def _price_paths(s):
    """定位三个价格数据文件。返回 (mhtml, pri.txt, model.txt)，找不到的为 None。

    查找顺序：设置.json 的显式配置 > **本项目目录** > price_tool_dir。

    本项目目录排在外部目录之前是刻意的：这三个文件已经随项目一起存放，
    工具不依赖任何外部项目就能完整工作。price_tool_dir 只是给「想用另一份
    更新的价格数据」留的口子，删掉那个目录不影响本工具。
    """
    def pick(explicit, cands):
        if explicit and os.path.exists(explicit):
            return explicit
        for c in cands:
            if c and os.path.exists(c):
                return c
        return None

    # 本项目目录下任意 .mhtml（模型广场页面另存）
    local_mhtml = []
    try:
        for fn in sorted(os.listdir(HERE)):
            if fn.lower().endswith(".mhtml"):
                local_mhtml.append(os.path.join(HERE, fn))
    except OSError:
        pass

    base = s.get("price_tool_dir") or ""
    mh = pick(s.get("price_mhtml"),
              local_mhtml + ([os.path.join(base, "模型广场 _ Apilio API.mhtml")]
                             if base else []))
    pri = pick(s.get("price_pri"),
               [os.path.join(HERE, "pri.txt")] +
               ([os.path.join(base, "pri.txt")] if base else []))
    mtxt = pick(s.get("price_model_txt"),
                [os.path.join(HERE, "model.txt")] +
                ([os.path.join(base, "model.txt")] if base else []))
    return mh, pri, mtxt


def act_rollback(s):
    """菜单 [5]：回滚。与一键流程里的清空是同一套实现。"""
    print("\n" + "=" * 60)
    print(" 清空：删除本工具建的账号、分组、代理")
    print("=" * 60)
    if not s["sub2api_admin_key"]:
        print(" 还没设置 sub2api 管理密钥，请先在 设置.json 里填。")
        return
    api = Sub2Api(s)
    wipe_all(api, s,
             ask=lambda q: input(q).strip().lower() == "y",
             confirm=lambda q: input(q).strip())


def act_open_out(s):
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        os.startfile(OUT_DIR)  # Windows
        print("\n 已打开产出文件夹。")
    except Exception:
        print("\n 产出文件夹：%s" % OUT_DIR)


# =============================================================================
# 与上游源码同步常量
# =============================================================================
# 提取常量真正需要的上游文件清单。
# 只列这几个而不是整仓拉取：一来快，二来把「本工具依赖上游的哪些文件」
# 写在明处——上游哪天挪了文件，这里会直接报出是哪一个取不到。
UPSTREAM_FILES = {
    "sub2api": [
        "internal/service/account_header_override.go",
        "internal/domain/constants.go",
        "internal/handler/admin/account_handler.go",
        "internal/handler/admin/account_data.go",
        "internal/handler/admin/scheduled_test_handler.go",
        "internal/service/channel.go",
        "ent/schema/account.go",
        "migrations/081_create_channels.sql",
    ],
    "cpa": [
        "internal/config/config_types.go",
    ],
}


def fetch_upstream_src(s, verbose=True):
    """本地没有上游源码时，从 GitHub 取回提取常量所需的那几个文件。

    为什么需要：本工具的「自动跟随上游」是靠读 Go 源码实现的
    （见 sync_constants）。如果用户把本地源码目录删了，就只能退回内置
    兜底值——那正是我们想摆脱的硬编码。在线抓取让这件事不依赖本地副本。

    缓存到 out/upstream-cache/ 下，结构与原仓库一致，之后可离线复用。
    只在缓存缺失时联网；想强制刷新就删掉缓存目录。

    任何一步失败都不抛异常，返回 (缓存根目录或 None, 提示列表)——
    调用方会继续降级到内置值，并把原因打出来。
    """
    if not s.get("fetch_upstream_src", True):
        return None, None, ["已按配置关闭在线抓取上游源码"]

    root = os.path.join(OUT_DIR, "upstream-cache")
    msgs = []
    out = {}
    for kind, base_key in (("sub2api", "sub2api_repo"), ("cpa", "cpa_repo")):
        base = (s.get(base_key) or "").rstrip("/")
        if not base:
            continue
        local_root = os.path.join(root, kind)
        got, missed = 0, []
        for rel in UPSTREAM_FILES[kind]:
            dst = os.path.join(local_root, rel.replace("/", os.sep))
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                got += 1
                continue
            try:
                st, raw = _http("GET", base + "/" + rel, timeout=30)
                if st != 200 or not raw.strip():
                    missed.append(rel)
                    continue
                d = os.path.dirname(dst)
                if d:
                    os.makedirs(d, exist_ok=True)
                with io.open(dst, "w", encoding="utf-8", newline="\n") as f:
                    f.write(raw)
                got += 1
            except Exception as ex:
                missed.append("%s（%s）" % (rel, str(ex)[:60]))
        if got:
            out[kind] = local_root
            msgs.append("已从 GitHub 取回 %s 的 %d/%d 个源码文件（缓存在 %s）"
                        % (kind, got, len(UPSTREAM_FILES[kind]), local_root))
        if missed:
            msgs.append("! %s 有 %d 个文件取不到：%s"
                        % (kind, len(missed), "、".join(missed[:3])))
    return out.get("sub2api"), out.get("cpa"), msgs


def sync_constants(s, verbose=True):
    """从 sub2api / CPA 源码提取常量，覆盖硬编码兜底值。

    这件事必须在**每个**入口都跑，而不是只在菜单里跑：上游一旦往请求头黑名单
    里加一项，我们不更新就会整批 400（历史上真出过——50 条账号因为
    x-claude-code-session-id 被拒，是靠线上报错才发现的）。

    降级也必须出声。以前是 except: pass，提取成功和失败用户看到的输出一模一样，
    等于给了"会自动跟上游同步"的错觉。

    返回 (是否成功, 提示信息列表)。
    """
    global HEADER_BLACKLIST, MAX_HEADER_NAME, MAX_HEADER_VALUE, MAX_HEADER_ENTRIES
    msgs = []
    sub_src = s.get("sub2api_src") or SUB2API_SRC
    cpa_src = s.get("cpa_src") or CPA_SRC

    if not os.path.exists(sub_src):
        # 本地没有源码目录：先试在线抓取，取到了照样能同步契约，
        # 取不到才降级到内置值。三级降级的中间那一级就是这里。
        fetched_sub, fetched_cpa, fmsgs = fetch_upstream_src(s)
        msgs.extend(fmsgs or [])
        if fetched_sub:
            sub_src = fetched_sub
            if fetched_cpa:
                cpa_src = fetched_cpa
        else:
            msgs.append("找不到 sub2api 源码（%s）且在线抓取未成功，改用内置常量；"
                        "上游若改了请求头黑名单或接口字段，本工具不会知道。"
                        % sub_src)
            return False, msgs

    try:
        import extract as ext
        c = ext.get_constants(sub_src, cpa_src)
    except Exception as ex:
        msgs.append("常量提取失败（%s），改用内置常量。" % str(ex)[:120])
        return False, msgs

    added = set(c["header_blacklist"]) - set(HEADER_BLACKLIST)
    removed = set(HEADER_BLACKLIST) - set(c["header_blacklist"])
    HEADER_BLACKLIST = set(c["header_blacklist"])
    MAX_HEADER_NAME = c["max_header_name"]
    MAX_HEADER_VALUE = c["max_header_value"]
    MAX_HEADER_ENTRIES = c["max_header_entries"]

    if added:
        msgs.append("上游新增了 %d 个禁止覆写的请求头，已同步：%s"
                    % (len(added), "、".join(sorted(added))))
    if removed:
        msgs.append("上游移除了 %d 个禁止覆写的请求头：%s"
                    % (len(removed), "、".join(sorted(removed))))

    # ---- 接口契约同步 ----
    # 这几项直接决定本工具「哪一步能发哪个字段」。硬记会在上游调整时悄悄失效，
    # 所以从源码读，并且**只在与预期不符时出声**——平时不刷屏。
    global CREATE_HAS_STATUS, CREATE_HAS_SCHEDULABLE, BULK_HAS_SCHEDULABLE
    global ACCOUNT_DEFAULTS, PRICING_PER_TOKEN, MODEL_SERIES
    ca = set(c.get("create_account_fields") or [])
    bu = set(c.get("bulk_update_fields") or [])
    CREATE_HAS_STATUS = "status" in ca
    CREATE_HAS_SCHEDULABLE = "schedulable" in ca
    BULK_HAS_SCHEDULABLE = "schedulable" in bu
    ACCOUNT_DEFAULTS = dict(c.get("account_defaults") or {})
    PRICING_PER_TOKEN = bool(c.get("pricing_unit_is_per_token", True))

    # 模型代际门槛。上游发了新一代就前移，本工具的「就高原则」跟着走，
    # 不需要改代码。门槛变化必须出声：它直接决定哪些模型会被放行。
    new_series = dict(c.get("model_series") or {})
    for plat, gen in sorted(new_series.items()):
        old = MODEL_SERIES.get(plat)
        if old is not None and int(gen) != int(old):
            msgs.append("上游的 %s 模型已到第 %s 代（本工具原门槛 %s 代），"
                        "「就高原则」已跟随前移。" % (plat, gen, old))
    if new_series:
        MODEL_SERIES = new_series

    if CREATE_HAS_STATUS:
        # 上游给建号接口补了 status，就不必再「建完补一次 PUT」了
        msgs.append("上游的建号接口现在支持 status，停用可以在建号时一次完成"
                    "（本工具仍会走建完再改的稳妥路径，可后续简化）。")
    if not BULK_HAS_SCHEDULABLE:
        # 这是恢复调度开关的唯一入口，没了就得换方案
        msgs.append("! 上游的 bulk-update 不再接受 schedulable，"
                    "『回收调度开关』功能可能失效，需要人工确认。")
    if not PRICING_PER_TOKEN:
        msgs.append("! 定价单位注释不再是「每 token」，价格换算的 1e6 需要复核，"
                    "误判会导致计费相差百万倍。")
    if ACCOUNT_DEFAULTS.get("auto_pause_on_expired") is False:
        msgs.append("上游把 auto_pause_on_expired 的默认值改成了 false，"
                    "本工具的显式赋值已无必要（保留亦无害）。")

    # 映射表是否跟上了上游
    try:
        warns, infos = ext.diagnose(sub_src, cpa_src,
                                    {"SECTION_MAP": SECTION_MAP})
        msgs.extend(warns)
        if verbose:
            msgs.extend(infos)
    except Exception:
        pass

    if verbose and not msgs:
        msgs.append("常量已与上游源码核对一致（黑名单 %d 项）。" % len(HEADER_BLACKLIST))
    return True, msgs


# =============================================================================
# 菜单
# =============================================================================
def cpa_count():
    if not os.path.exists(CONFIG_YAML):
        return "无（将联网）"
    try:
        import yaml
    except ImportError:
        return "已就绪（缺 PyYAML，无法预览条数）"
    try:
        with io.open(CONFIG_YAML, encoding="utf-8", errors="replace") as f:
            d = yaml.safe_load(f)
        recs, _ = collect(d) if isinstance(d, dict) else ([], [])
        return "%d 个账号" % len(recs)
    except Exception:
        return "已就绪"


def act_import_menu(s):
    """菜单 [4]：读磁盘上的计划并导入。"""
    if not os.path.exists(PLAN_PATH):
        print("\n 没有找到导入计划。请先选 [3] 生成导入数据。")
        return
    with io.open(PLAN_PATH, encoding="utf-8") as f:
        plan = json.load(f)

    # 计划新鲜度校验：菜单允许不跑 [3] 直接跑 [4]，没有这层校验就可能拿上周的
    # 计划导今天的配置，新增的 key 一条都进不去。
    meta = plan.get("meta") or {}
    if meta.get("tool_version") != TOOL_VERSION:
        print("\n 计划是工具 %s 生成的，当前版本 %s，结构可能不兼容。"
              % (meta.get("tool_version", "旧版本"), TOOL_VERSION))
        print(" 请先重新跑 [3]。")
        return
    if os.path.exists(CONFIG_YAML):
        try:
            with io.open(CONFIG_YAML, encoding="utf-8", errors="replace") as f:
                cur = hashlib.sha256(f.read().encode("utf-8")).hexdigest()
            if meta.get("config_sha256") and cur != meta["config_sha256"]:
                print("\n config.yaml 在计划生成后被改过（%s 生成）。"
                      % meta.get("generated_at", "?"))
                print(" 请先重新跑 [3]，否则会按旧配置导入。")
                return
        except Exception:
            pass

    api = Sub2Api(s)
    stat = do_import(api, plan, ask=lambda q: input(q + " 输入 yes 继续：").strip().lower() == "yes")
    print_import_summary(stat)


def menu():
    s = load_settings()
    ok, msgs = sync_constants(s)
    for m in msgs:
        print(" [常量同步] %s" % m)

    while True:
        print("\n" + "=" * 60)
        print(" CPA  ->  sub2api  迁移工具  v%s" % TOOL_VERSION)
        print("=" * 60)
        print(" 数据源 config.yaml : %s" % cpa_count())
        print(" 目标 sub2api 地址  : %s" % s["sub2api_base_url"])
        print(" 目标 sub2api 密钥  : %s" % mask(s["sub2api_admin_key"]))
        print(" 上游常量同步       : %s" % ("已同步" if ok else "降级到内置值"))
        print("-" * 60)
        print("  [1] 设置 sub2api 地址和管理密钥")
        print("  [2] 测试连接")
        print("  [3] 生成导入数据（只写本地，可反复跑）")
        print("  [4] 执行导入（写入 sub2api）")
        print("  [5] 回滚（删除本工具导入的账号和分组）")
        print("  [6] 提交价格清单（分组逐模型定价）")
        print("  [7] 价格预演（只算差异，不写入）")
        print("  [8] 打开产出文件夹")
        print("  [0] 退出")
        print("-" * 60)
        choice = input(" 请输入数字回车：").strip()
        try:
            if choice == "1":
                act_settings(s)
            elif choice == "2":
                act_test(s)
            elif choice == "3":
                act_generate(s)
            elif choice == "4":
                act_import_menu(s)
            elif choice == "5":
                act_rollback(s)
            elif choice == "6":
                act_pricing(s)
            elif choice == "7":
                act_pricing(s, dry_run=True)
            elif choice == "8":
                act_open_out(s)
            elif choice == "0":
                print(" 再见。")
                return
            else:
                print(" 没有这个选项，请输入 0-6。")
        except KeyboardInterrupt:
            print("\n 已中断本操作，回到菜单。")
        except Exception as ex:
            print("\n [出错] %s" % ex)
        input("\n 按回车返回菜单...")


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print("\n 退出。")
