# -*- coding: utf-8 -*-
"""CPA -> sub2api 迁移工具

用法：双击同目录的『一键导入.cmd』，全自动跑完；或 `python tool.py` 进菜单。
数据源：同目录的 config.yaml（自动读取）。
目标  ：sub2api，地址和管理密钥写在 设置.json。

依赖：Python 3 + PyYAML（cmd 会自动装）。其余全用标准库。
"""
import hashlib
import copy
import errno
import io
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import upstream_contract
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar

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
    from new_remap_priority import (remap_priority_smart,
                                    assign_group_local_buckets)
    SMART_PRIORITY_AVAILABLE = True
except ImportError:
    SMART_PRIORITY_AVAILABLE = False
    print("警告: new_remap_priority.py 未找到，使用传统优先级映射")

    # 兜底实现：new_remap_priority 缺失时本模块仍要能用（它被 remap_priority
    # 与 health_rerank_priority 两处调用）。这里给一份等价的最小实现，
    # 而不是让上面那个 ImportError 顺带把这两个函数也废掉 ——
    # "模块缺失"应该只降级智能优先级，不该让导入整体不可用。
    def assign_group_local_buckets(units, sort_key):
        from collections import defaultdict as _dd
        _by = _dd(list)
        for _u in units:
            if isinstance(_u, (tuple, list)) and len(_u) >= 2:
                _by[str(_u[0])].append(_u)
            else:
                _by["_"].append(_u)
        _out = {}
        for _g, _keys in _by.items():
            for _i, _k in enumerate(sorted(_keys, key=sort_key), 1):
                _out[_k] = _i * 10
        return _out

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
    import model_selection
    from model_selection import select_highest_models
    MODEL_SELECTION_AVAILABLE = True
except ImportError:
    MODEL_SELECTION_AVAILABLE = False
    print("警告: model_selection.py 未找到，跳过模型就高原则过滤")

# 模型代际连通性探测（需求第 2 条⑵④）。缺这个文件时整条链路静默降级为
# "不探测"，就高逻辑照常工作，只是拿不到"最新代其实不通"这个信息。
try:
    import model_probe
    MODEL_PROBE_AVAILABLE = True
except ImportError:
    MODEL_PROBE_AVAILABLE = False

import probe_policy


def _truthy(v):
    """把配置里的真值写法统一成 bool。

    环境变量下发的一律是字符串，"false" 在 Python 里是真值 ——
    直接 bool(v) 会让所有开关永远为开。
    """
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")

TOOL_VERSION = "3.0"

# 默认 User-Agent。见 _http 的说明：Cloudflare 的浏览器完整性检查会按 UA
# 拦掉自报家门的自动化客户端（403 + `error code: 1010`，请求到不了源站）。
# 用常见浏览器 UA 是为了让合法请求过边缘，鉴权仍然走管理密钥。
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.environ.get("SETTINGS_PATH") or os.path.join(HERE, "设置.json")
CONFIG_YAML = os.path.join(HERE, "config.yaml")
OUT_DIR = os.path.join(HERE, "out")
PLAN_PATH = os.path.join(OUT_DIR, "import-plan.json")
LOG_DIR = os.path.join(OUT_DIR, "logs")


# =============================================================================
# 日志（需求第 8 条：支持详细的日志输出，出错时能及时排查）
# =============================================================================
#
# 以前全仓库没有一处 `import logging`，而 .env.example 里承诺了
# `LOG_LEVEL=INFO`（第 226 行）—— **设了完全不生效**，属于"文档与行为
# 不一致"那一类（同 HTTP_TIMEOUT / HTTP_RETRIES 的历史问题）。
#
# 这里补一套最小的日志设施，只做三件事：
#   1. 级别可控（LOG_LEVEL 环境变量 / 设置.json 的 log_level）
#   2. 可选落盘（LOG_TO_FILE=1 → out/logs/run-<时间戳>.log）
#   3. 静默失败点有地方出声（见 `log_exc`）
#
# 为什么同时保留 print：本工具的主要交互是"给用户看的进度与结论"，
# 那些是**产品输出**，不该被日志级别过滤掉；日志只承载**排查信息**。
# 所以两者分工明确，不互相替代：
#   print     -> stdout，给用户看的阶段进度、统计、结论
#   log.*     -> 给排查看的细节（重试、降级、被丢弃的字段、异常原因）
#   log_exc   -> 原本静默的 except，现在带上异常类型与位置
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30,
               "ERROR": 40, "CRITICAL": 50}
_logger = None
_log_ready = False


def _init_logging():
    """懒初始化日志。第一次需要写日志时才建 handler。

    为什么懒初始化：load_settings() 本身要读 设置.json 与环境变量，而
    log_level / log_to_file 是**它读出来的**。如果在模块导入期就初始化，
    那时 settings 还没加载，只能拿环境变量的默认值 —— 用户写在
    设置.json 里的 log_level 会被永远忽略。
    """
    global _logger, _log_ready
    if _log_ready:
        return _logger
    _log_ready = True

    lg = logging.getLogger("cpa2sub2api")
    lg.setLevel(logging.DEBUG)          # handler 上再按级别过滤
    lg.propagate = False
    if lg.handlers:
        _logger = lg
        return lg

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    # 级别与落盘都从 settings 读；读不到就用环境变量兜底，再兜底 INFO。
    # 全程 try：日志设施本身**绝不能**把主流程搞崩 —— 它只是排查辅助。
    level_name = "INFO"
    to_file = False
    try:
        s = load_settings()
        level_name = str(s.get("log_level") or "").strip().upper() or "INFO"
        to_file = str(s.get("log_to_file") or "").lower() in ("1", "true", "yes", "on")
    except Exception:
        env_lv = str(os.environ.get("LOG_LEVEL") or "").strip().upper()
        if env_lv:
            level_name = env_lv
        to_file = str(os.environ.get("LOG_TO_FILE") or "").lower() in (
            "1", "true", "yes", "on")

    # 认不出的级别名要**明确回退到 INFO 并说出来**，不能默默按 DEBUG 处理 ——
    # 那是把"排查信息全开"当成默认，日志会一下子膨胀几十倍；
    # 反过来若默默按 ERROR 处理，用户会以为没日志。两种误判都很难查。
    if level_name not in _LOG_LEVELS:
        print("警告: LOG_LEVEL=%r 不是有效级别（可选 %s），已按 INFO 处理"
              % (level_name, "/".join(sorted(_LOG_LEVELS))), file=sys.stderr)
        level_name = "INFO"
    level = _LOG_LEVELS[level_name]
    # logger 本身**恒为 DEBUG**，级别过滤交给各 handler 自己做。
    #
    # 不能 here 设成 `level`：那样 DEBUG 记录在到达 handler 之前就被
    # logger 级别挡掉了，文件 handler 虽然写着 DEBUG 也永远收不到 ——
    # 实测踩到（LOG_TO_FILE=1 且 LOG_LEVEL=INFO 时，文件里没有 DEBUG 行）。
    lg.setLevel(logging.DEBUG)

    # stdout handler：与 print 混在一起，容器里 `docker logs` 能直接看
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    lg.addHandler(sh)

    if to_file:
        try:
            if not os.path.isdir(LOG_DIR):
                os.makedirs(LOG_DIR)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            fh = logging.FileHandler(
                os.path.join(LOG_DIR, "run-%s.log" % stamp), encoding="utf-8")
            # 文件里一律记 DEBUG 及以上：落盘的目的是事后排查，
            # 用户把 LOG_LEVEL 调成 INFO 时也不该丢掉 DEBUG 细节。
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            lg.addHandler(fh)
        except Exception as ex:
            lg.warning("日志落盘失败（%s），只在 stdout 输出", ex)

    _logger = lg
    return lg


def log():
    """取 logger。用法 `log().debug(...)`，不在导入期建 handler。"""
    return _init_logging() or logging.getLogger("cpa2sub2api")


def log_exc(what, exc, level="debug"):
    """把**原本会被静默吞掉**的异常记下来。

    需求第 8 条要求"出错时能及时排查"。而这类 `except Exception: pass`
    是排查的最大障碍：出问题时**没有任何痕迹**，只能靠复现。

    用法：`except Exception as ex: log_exc("读 设置.json 失败", ex)`

    默认 DEBUG 级别 —— 这些位置大多是"有兜底、业务能继续"的降级路径，
    不该在正常运行时刷屏；但设 LOG_LEVEL=DEBUG 或 LOG_TO_FILE=1 时必须
    全部可见。真的影响业务时调用方应自己用 warning/error。
    """
    msg = "%s: %s: %s" % (what, type(exc).__name__, exc)
    # 只有**真的处于 except 块里**时才带 traceback。否则 exc_info=True 会打出
    # 一行 "NoneType: None"（当前没有活跃异常时的占位）—— 纯噪音，还会让人
    # 以为有问题。sys.exc_info()[0] 非空就说明有活跃异常。
    in_handler = sys.exc_info()[0] is not None
    getattr(log(), level, log().debug)(msg, exc_info=in_handler)



# 本工具写进 accounts.notes 开头的固定标记。
# 用它而不是"备注里出现 CPA 字样"来认自己导入的账号——后者会把用户手工建的、
# 备注里恰好提到 CPA 的账号也算进删除范围。删除是不可逆的，认定必须精确。
NOTE_SENTINEL = "[cpa2sub2api]"

# 源码路径。可用 设置.json 的 sub2api_src / cpa_src 覆盖；
# 留空或路径不存在时先试在线抓取（fetch_upstream_src），再降级到内置常量，
# 每一级都会明确告警（不再静默降级）。
#
# 默认按"与本项目并列存放"推断，不写死任何人的家目录：产品代码里出现
# 个人绝对路径会暴露环境；源码仅按项目相对位置查找。
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
    # ---- 日志（需求第 8 条）----
    # 级别：DEBUG / INFO / WARNING / ERROR / CRITICAL。默认 INFO。
    #
    # 为什么要"详细日志"这件事单独占一个开关：本工具的主输出（阶段进度、
    # 统计、结论）走 print，是给用户看的；日志承载的是**排查信息** ——
    # 重试、降级、被丢弃的字段、原本静默的异常。两者职责不同，不能用
    # 日志级别去过滤产品输出，也不能让产品输出淹没排查信息。
    "log_level": "INFO",
    # 是否把日志落盘到 out/logs/run-<时间戳>.log。
    #
    # 默认关：本工具的常见用法是"跑一次就退"，落盘会在 out/ 里攒文件。
    # 但**排查时请打开** —— 事后追查"当时到底发生了什么"只有它能给答案
    # （容器里 stdout 经常只有最后几十行）。落盘时一律记 DEBUG 及以上，
    # 所以把 LOG_LEVEL 调成 INFO 也不会丢掉 DEBUG 细节。
    "log_to_file": False,
    # 导入后自动挂定时探活计划（auto_recover=true），用 sub2api 自带机制恢复。
    "auto_recover_enabled": True,
    "auto_recover_cron": "0 3 * * 0",
    # 停用账号也要挂探活计划。需求第 2 条⑴要求"原先 config.yaml 里已关闭的
    # 上游，检测可用也要重新打开"——不探测就永远不知道它恢复没有。
    "probe_inactive": True,
    # 探活证明可用后，把本工具建成的 inactive 账号**自动重新启用**。
    #
    # 需求第 2 条⑴ 的核心诉求，但这个键此前**从来没有被接到设置里**：
    # revive_proven_inactive() 读 cfg.get("revive_proven_inactive", False)，
    # 而 DEFAULTS、设置.json、.env.example 三处都没有这个键 → 恒为 False →
    # 探活即使证明某站已恢复，账号也永远躺在 inactive，只能人工点开。
    # 这正是"原配置关闭的上游永远起不来"的另一半成因。
    #
    # 只动本工具导入的账号（notes 带 NOTE_SENTINEL），且必须有
    # 本轮针对精确账号 ID 的即时 SSE 测试成功，且配置快照未变才启用。
    # 历史计划结果不作为恢复依据。导入标记无法区分后续手工停用；
    # 若需保留这些账号的停用状态，应关闭 revive_proven_inactive。
    "revive_proven_inactive": True,
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
    "sub2api_repo": "https://raw.githubusercontent.com/Wei-Shaw/sub2api/v0.2.20/backend",
    "cpa_repo": "https://raw.githubusercontent.com/router-for-me/CLIProxyAPI/v8.0.60",
    "upstream_cache_ttl_seconds": upstream_contract.CACHE_TTL,
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

    # ---- 模型代际连通性探测（需求第 2 条⑵④）----
    # 探"最新代 / 次最新代能不能真的调用"，用于实现
    # "最新代不通但次最新代连通时，两代都保留"。
    # 按 (段, 域名) 去重后每单元最多 2 个请求，8 并发，结果缓存 6 小时。
    # 关掉则完全按静态的就高逻辑走（与加这个功能之前一致）。
    "model_probe_enabled": False,
    "model_probe_hard_disabled": False,
    "online_probe_hosts": {},
    "detected_tls_fingerprint_hosts": [],
    "prune_stale_accounts": True,
    "model_probe_workers": 8,
    "model_probe_cache_ttl": 21600,
    "request_relay_enabled": False,
    "request_relay_base_url": "http://request-relay:8380",
    "request_relay_state_dir": os.path.join(HERE, "state", "relay"),
    "request_relay_source_scope": "default",
    "request_relay_weighted_bucket": False,
    "request_relay_force_all_accounts": False,
    "request_relay_tls_profiles": {},
    "request_relay_learned_tls_profiles": {},
}

# 浮点差分阈值。价格经 JSON 往返会有末位误差，用 == 比会永远认为有变化、
# 每次都重推一遍。对齐 new-api ratio_sync 的 floatEpsilon。
FLOAT_EPSILON = 1e-9


# =============================================================================
# 设置读写
# =============================================================================
class SettingsError(RuntimeError):
    """Settings I/O or lock failure; callers must not overwrite as recovery."""


class SettingsFormatError(SettingsError):
    """The saved file is not a valid JSON object."""


class SettingsLockTimeout(SettingsError, TimeoutError):
    """Another process held the settings transaction beyond its deadline."""


def _read_saved_settings(path):
    def reject_constant(value):
        raise ValueError("non-finite JSON number")
    try:
        with io.open(path, encoding="utf-8-sig") as stream:
            saved = json.load(stream, parse_constant=reject_constant)
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError) as ex:
        raise SettingsFormatError("设置文件必须是有效的 JSON 对象；原文件未更改") from ex
    except OSError as ex:
        raise SettingsError("无法读取设置文件；原文件未更改") from ex
    if not isinstance(saved, dict):
        raise SettingsFormatError("设置文件必须是 JSON 对象；原文件未更改")
    return saved


def _settings_os_lock(fd, unlock=False):
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _settings_lock(path, timeout):
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("settings lock timeout must be finite and nonnegative")
    fd = None
    acquired = False
    try:
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SettingsError("设置锁不是普通文件；未更改设置")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    _settings_os_lock(fd)
                    acquired = True
                    break
                except OSError as ex:
                    if ex.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SettingsLockTimeout("等待设置锁超时；未更改设置") from ex
                    time.sleep(min(0.025, remaining))
            os.lseek(fd, 0, os.SEEK_SET)
            marker = os.read(fd, 2)
            if not marker:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
                os.fsync(fd)
            elif marker != b"\0":
                raise SettingsError("设置锁文件已损坏；未更改设置或锁文件")
        except SettingsError:
            raise
        except OSError as ex:
            raise SettingsError("无法打开或使用设置锁；未更改设置") from ex
        yield
    finally:
        if fd is not None:
            try:
                if acquired:
                    _settings_os_lock(fd, unlock=True)
            finally:
                os.close(fd)


def _write_settings_atomic(path, saved):
    # Serialize before creating a temporary file, so caller mistakes leave no debris.
    payload = json.dumps(saved, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp",
                                         dir=os.path.dirname(path))
        with io.open(fd, "w", encoding="utf-8", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as ex:
        raise SettingsError("无法原子保存设置；原文件未更改") from ex
    finally:
        if temporary is not None:
            os.unlink(temporary)


def update_saved_settings(mutator, *, timeout=5.0):
    """Atomically mutate raw saved JSON under SETTINGS_PATH + '.lock'.

    Missing files start as {}. The mutator receives a deep copy and must return
    a dict; its exceptions propagate without writing. Defaults and environment
    values are never merged here. Invalid saved JSON and unusable/corrupt locks
    fail explicitly. Callers must not call save_settings while holding this lock.
    """
    path = os.path.abspath(SETTINGS_PATH)
    with _settings_lock(path, timeout):
        saved = mutator(copy.deepcopy(_read_saved_settings(path)))
        if not isinstance(saved, dict):
            raise TypeError("settings mutator must return a dict")
        saved = copy.deepcopy(saved)
        _write_settings_atomic(path, saved)
        return copy.deepcopy(saved)


def load_settings():
    s = dict(DEFAULTS)
    saved = {}
    saved_invalid = False
    # 1. 读取设置.json（低优先级）
    if os.path.exists(SETTINGS_PATH):
        try:
            saved = _read_saved_settings(SETTINGS_PATH)
            s.update(saved)
        except Exception as ex:
            saved_invalid = True
            # 这条**必须出声**（需求第 8 条）：设置.json 是用户唯一手写的配置，
            # 它坏了却静默退回默认值的话，表现是"我明明配了 / 改了，为什么不生效"——
            # 而且改的是密钥、地址这类最要紧的字段时特别难查。
            #
            # 注意：这里不能用 log_exc()。load_settings 是日志初始化**自己**要
            # 调的（读 log_level），在初始化过程中打日志会无限递归。所以直接
            # 写 stderr —— 不经过 logger，没有递归风险。
            print("警告: 读取 %s 失败（%s: %s），已改用默认值 + 环境变量。"
                  "该文件里的配置本次**不会生效**。"
                  % (os.path.basename(SETTINGS_PATH), type(ex).__name__, ex),
                  file=sys.stderr)

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
        "PROBE_INACTIVE": "probe_inactive",
        "REVIVE_PROVEN_INACTIVE": "revive_proven_inactive",

        # 功能开关
        "HEALTH_RERANK_ENABLED": "health_rerank_enabled",
        "SYNC_EXISTING_ACCOUNTS": "sync_existing_accounts",
        "RESPECT_WEIGHT_ZERO": "respect_weight_zero",
        "FETCH_UPSTREAM_SRC": "fetch_upstream_src",
        "UPSTREAM_CACHE_TTL_SECONDS": "upstream_cache_ttl_seconds",
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

        # 日志（需求第 8 条）。这两个键以前**只在 .env.example 里写着**
        # （LOG_LEVEL 在第 226 行），env_mappings 里没有接线 —— 设了完全不生效。
        # 与 HTTP_TIMEOUT / HTTP_RETRIES 是同一类"文档与行为不一致"的问题。
        "LOG_LEVEL": "log_level",
        "LOG_TO_FILE": "log_to_file",

        # 模型代际连通性探测
        "MODEL_PROBE_ENABLED": "model_probe_enabled",
        "MODEL_PROBE_HARD_DISABLED": "model_probe_hard_disabled",
        "PRUNE_STALE_ACCOUNTS": "prune_stale_accounts",
        "MODEL_PROBE_WORKERS": "model_probe_workers",
        "MODEL_PROBE_CACHE_TTL": "model_probe_cache_ttl",
        "REQUEST_RELAY_ENABLED": "request_relay_enabled",
        "REQUEST_RELAY_BASE_URL": "request_relay_base_url",
        "REQUEST_RELAY_STATE_DIR": "request_relay_state_dir",
        "REQUEST_RELAY_SOURCE_SCOPE": "request_relay_source_scope",
        "REQUEST_RELAY_WEIGHTED_BUCKET": "request_relay_weighted_bucket",
        "REQUEST_RELAY_FORCE_ALL_ACCOUNTS": "request_relay_force_all_accounts",
        "REQUEST_RELAY_TLS_PROFILES": "request_relay_tls_profiles",
    }

    for env_key, setting_key in env_mappings.items():
        val = os.environ.get(env_key)
        if val:
            if setting_key == "model_probe_enabled" and setting_key in saved:
                # The environment supplies an initial default, never replaces
                # an explicit saved grant or revocation.
                continue
            if setting_key == "request_relay_tls_profiles":
                try:
                    profiles = json.loads(val)
                    if not isinstance(profiles, dict):
                        raise ValueError("not a mapping")
                except (ValueError, TypeError) as ex:
                    raise SettingsFormatError("REQUEST_RELAY_TLS_PROFILES 必须是显式域名到 TLS preset 配置的 JSON 对象") from ex
                s[setting_key] = profiles
                continue
            # 数值字段转换
            if setting_key in ("import_workers", "test_plan_workers", "pricing_workers",
                              "default_concurrency", "batch_size", "pool_mode_retry_count",
                              "model_probe_workers", "model_probe_cache_ttl",
                              # HTTP_TIMEOUT / HTTP_RETRIES 以前不在这里，值是字符串。
                              # 用它的地方有 `int(...)` 兜着，所以**行为是对的**，
                              # 但 settings 里存字符串意味着任何新调用点漏了 int()
                              # 就会 TypeError —— 而且只在设了这两个变量时才出现。
                              # 统一在这里转成整数，消掉这个埋着的坑。
                              "http_timeout", "http_retries", "upstream_cache_ttl_seconds"):
                try:
                    s[setting_key] = int(val)
                except ValueError:
                    # 环境变量写了个非数字。静默忽略会让"我设了 IMPORT_WORKERS=8
                    # 怎么还是慢"这种问题查不出来 —— 说清是哪个键、值是什么、
                    # 退回了什么。同样用 stderr 避开 logger 递归。
                    print("警告: 环境变量 %s=%r 不是整数，已忽略（沿用默认值 %r）"
                          % (env_key, val, s.get(setting_key)),
                          file=sys.stderr)
            # 布尔字段转换
            elif setting_key in ("auto_recover_enabled", "health_rerank_enabled",
                                "sync_existing_accounts", "respect_weight_zero",
                                "fetch_upstream_src", "pool_mode_enabled", "auto_pause_on_expired",
                                "model_probe_enabled", "model_probe_hard_disabled", "probe_inactive", "prune_stale_accounts",
                                "revive_proven_inactive", "log_to_file",
                                "request_relay_enabled", "request_relay_weighted_bucket",
                                "request_relay_force_all_accounts"):
                s[setting_key] = val.lower() in ("true", "1", "yes", "on")
            # 字符串字段
            else:
                s[setting_key] = val

    if int(s.get("default_concurrency", 3)) <= 0:
        s["default_concurrency"] = 3
    # 顺手刷新兜底代理的缓存。_http 是热路径、不带 settings 参数，
    # 它调 _fallback_proxy() 时只能吃缓存；而不刷新的话，第一次读到的
    # 永远是进程启动时的旧值（甚至 None），环境变量改了也不生效。
    # Saved master decisions are authoritative. The separately named operator
    # veto remains effective across UI, stale plans and explicit caller config.
    if "model_probe_enabled" in saved:
        s["model_probe_enabled"] = _truthy(saved["model_probe_enabled"])
    s["model_probe_hard_disabled"] = probe_policy.hard_disabled(saved)
    if s["model_probe_hard_disabled"]:
        s["model_probe_enabled"] = False
    if os.environ.get("ONLINE_PROBE_HOSTS", "").strip():
        environment_hosts = probe_policy.site_switches({
            "online_probe_hosts": os.environ["ONLINE_PROBE_HOSTS"]})
        saved_hosts = probe_policy.site_switches(saved)
        s["online_probe_hosts"] = {
            host: bool(environment_hosts.get(host, False) and saved_hosts.get(host, True))
            for host in environment_hosts.keys() | saved_hosts.keys()}
    if saved_invalid:
        s["model_probe_enabled"] = False
    s["auto_recover_cron"] = probe_policy.weekly_cron(s.get("auto_recover_cron"))
    s["_live_probe_policy"] = True
    _fallback_proxy(s)
    return s


def save_settings(s):
    # Preserve the historical full-replacement API. Product settings edits use
    # update_saved_settings so a stale loaded dict cannot restore old consent.
    if not isinstance(s, dict):
        raise TypeError("settings must be a dict")
    path = os.path.abspath(SETTINGS_PATH)
    with _settings_lock(path, 5.0):
        _write_settings_atomic(path, copy.deepcopy(s))


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


class ProbeAuthorityRevoked(RuntimeError):
    """A sensitive write lost consent, ownership, or its proven account snapshot."""


_ADMIN_WRITE_GUARD = ContextVar("admin_write_guard", default=None)


def _check_admin_write_guard(method):
    guard = _ADMIN_WRITE_GUARD.get()
    if guard is not None and method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        guard()


@contextmanager
def _guarded_admin_write(guard):
    token = _ADMIN_WRITE_GUARD.set(guard)
    try:
        guard()  # Fake/in-process APIs use the same final boundary.
        yield
    finally:
        _ADMIN_WRITE_GUARD.reset(token)


def _account_probe_write_guard(api, account, cfg=None, require_auto_recover=False,
                               plan_id=None, require_consent=True, record=None, require_clear_runtime=False):
    expected = _recovery_snapshot(account)
    expected_scope = _policy_source_scope(cfg) if cfg is not None else None
    def guard():
        if plan_id is not None:
            plans = _strict_test_plans(api.list_test_plans(account["id"]))
            if not any(p.get("id") == plan_id for p in plans):
                raise ProbeAuthorityRevoked("plan ownership changed before sensitive write")
        # The GET can itself block or retry. Resolve policy after it finishes.
        current = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % account["id"]))
        if (current.get("id") != account["id"] or not _is_migrated(current)
                or _recovery_snapshot(current) != expected):
            raise ProbeAuthorityRevoked("account changed before sensitive write")
        if "_cpa2sub2api_raw_credentials" in account:
            if (not hasattr(api, "reveal_account_credentials")
                    or api.reveal_account_credentials(current) != account["_cpa2sub2api_raw_credentials"]):
                raise ProbeAuthorityRevoked("credential identity changed before sensitive write")
        if require_clear_runtime and _recovery_runtime_block(current):
            raise ProbeAuthorityRevoked("account has an active or unknown runtime block")
        owned_scope = account_source_scope(current)
        if record is not None and not account_matches_source(current, record):
            raise ProbeAuthorityRevoked("record source ownership changed before write")
        if cfg is None:
            return
        reference = cfg.get("request_relay") or {}
        if _truthy(reference.get("enabled")) and "cpa2sub2api_original_source" in (current.get("extra") or {}):
            import request_relay
            bundle = request_relay.load_bundle(reference["state_dir"])
            if bundle["generation"] != reference["generation"] or bundle["source_scope"] != reference["source_scope"]:
                raise ProbeAuthorityRevoked("relay generation changed after diagnostic")
        live = resolve_probe_policy(cfg)
        if _truthy(reference.get("enabled")) and "cpa2sub2api_original_source" in (current.get("extra") or {}):
            try:
                _validate_request_relay_authority(reference, bundle, live)
            except (ValueError, KeyError, OSError):
                raise ProbeAuthorityRevoked("relay runtime authority changed after diagnostic") from None
        live_scope = live.get("_current_source_scope", _policy_source_scope(live))
        if (expected_scope is not None and (live_scope != expected_scope
                                           or owned_scope != expected_scope and record is None)
                or require_consent and not probe_policy.probe_allowed(account_probe_base_url(current), live)
                or require_auto_recover and not _truthy(live.get("auto_recover_enabled", True))):
            raise ProbeAuthorityRevoked("probe consent or source authority revoked")
    return guard


def _validate_request_relay_authority(reference, bundle, settings):
    """The same live relay contract gates both model I/O and consuming its proof."""
    if not settings.get("_live_probe_policy"):
        return
    import request_relay
    if not _truthy(settings.get("request_relay_enabled", False)):
        raise ValueError("relay disabled")
    expected = _request_relay_reference(settings, bundle)
    if any(reference.get(key) != expected.get(key) for key in (
            "source_scope", "generation", "state_dir", "base_url", "weighted_bucket")):
        raise ValueError("relay runtime authority changed")
    if bool(reference.get("force_all_accounts", False)) != bool(expected.get("force_all_accounts", False)):
        raise ValueError("relay account selection authority changed")
    current = request_relay.load_bundle(expected["state_dir"])
    if current["generation"] != bundle.get("generation"):
        raise ValueError("relay generation changed")


def _bound_probe_authority(url, headers, settings, authority_url):
    """An authority override is valid only for a token-bound immutable relay route."""
    if authority_url is None:
        return url
    reference = settings.get("request_relay") or {}
    bundle = settings.get("_request_relay_bundle") or {}
    if settings.get("_live_probe_policy"):
        try:
            _validate_request_relay_authority(reference, bundle, settings)
        except (ValueError, KeyError, OSError):
            raise ApiError(0, "relay probe runtime authority revoked", "<redacted>") from None
    if (not _truthy(reference.get("enabled")) or not reference.get("generation")
            or bundle.get("generation") != reference["generation"]
            or bundle.get("source_scope") != reference.get("source_scope")
            or {k.lower(): v for k, v in headers.items()}.get("x-relay-generation") != bundle.get("generation")):
        raise ApiError(0, "unbound relay probe authority", "<redacted>")
    import request_relay
    for route in bundle.get("routes", {}).values():
        prefix = reference["base_url"].rstrip("/") + "/r/" + route["id"] + "/"
        if (url.startswith(prefix) and route["original_source"]["base_url"] == authority_url
                and request_relay.authorized(headers, route["token"])):
            return authority_url
    raise ApiError(0, "relay probe authority mismatch", "<redacted>")


def _http(method, url, headers=None, body=None, timeout=None, retries=None,
          proxy=None, _allow_proxy_fallback=True, probe_settings=None, _admin_request=False,
          probe_authority_url=None, _admin_deadline=None, _admin_origin=None,
          _admin_progress=None):
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
    if probe_authority_url is not None:
        if probe_settings is None or proxy:
            raise ApiError(0, "relay probe requires bound consent and direct internal transport", "<redacted>")
        _allow_proxy_fallback = False
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
    if _admin_deadline is not None:
        if not _admin_request or probe_settings is not None or probe_authority_url is not None:
            raise ApiError(0, "管理传输边界不能承载上游探测", "<redacted>")
        headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
        headers.setdefault("Accept", "application/json, text/plain, */*")
        return _bounded_admin_http(method, url, headers, body, timeout, retries,
                                   _admin_deadline, _admin_origin, _admin_progress)
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

    if probe_settings is not None:
        probe_settings = resolve_probe_policy(probe_settings)
        authority = _bound_probe_authority(url, headers, probe_settings, probe_authority_url)
        if not probe_policy.probe_allowed(authority, probe_settings):
            raise ApiError(0, "upstream probe not authorized", "<redacted>")
        class ConsentRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                # A diagnostic endpoint is declarative. Never forward its key,
                # even to a same-host redirect on another port or scheme.
                raise ApiError(0, "probe redirect rejected", "<redacted>")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
            ConsentRedirect())

    attempts = max(1, int(retries or 1))
    last = None
    for i in range(attempts):
        if probe_settings is not None:
            probe_settings = resolve_probe_policy(probe_settings)
            authority = _bound_probe_authority(url, headers, probe_settings, probe_authority_url)
            if not probe_policy.probe_allowed(authority, probe_settings):
                raise ApiError(0, "upstream probe authority revoked", "<redacted>")
        _check_admin_write_guard(method)
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            body_txt = ex.read().decode("utf-8", "replace")
            if ex.code == 429 and _admin_request:
                error = ApiError(ex.code, "管理接口限流", "<redacted>")
                error.retry_after = ex.headers.get("Retry-After") if ex.headers else None
                raise error
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
                                _allow_proxy_fallback=False, probe_settings=probe_settings,
                                probe_authority_url=probe_authority_url, _admin_request=_admin_request)
                print("    [代理兜底] 直连失败，经代理成功：%s" % url)
                return st, txt
            except Exception as ex:
                # 代理也不通：如实报直连的那个错，别拿代理的错掩盖真实原因。
                # 但**要留痕** —— "代理兜底到底试没试过、为什么没成功"是排查
                # 上游 5xx/超时时最常问的问题，静默会让日志看起来像"根本没兜底"。
                log_exc("代理兜底失败（%s，最终按直连的错误上报）" % fb, ex)

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
    """One opaque identity per logical write, retained for that write's retries.

    Identical payloads after a delete or an operator edit are new write intents.
    A permanent payload hash would replay a cached deleted ID or stale success.
    Callers create this key once; _request and _http reuse its headers unchanged.
    Explicit _call(idem=...) may resume an intentionally retained logical intent.
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(os.urandom(32) + b"\x00" + raw).hexdigest()


def admin_retry_wait(value, now=None):
    """Honor server seconds/HTTP-date; missing metadata uses a full minute."""
    from email.utils import parsedate_to_datetime
    if value is None or not str(value).strip():
        return 60.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            instant = parsedate_to_datetime(str(value)).timestamp()
            return max(0.0, instant - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return 60.0


def _admin_failure(error):
    reasons = {
        "deadline": "管理网络操作截止，结果未验证",
        "network": "管理网络连接失败，结果未验证",
        "worker": "管理传输进程失败，结果未验证",
        "response_too_large": "管理响应超过大小上限，结果未验证",
        "boundary": "管理请求越界，未发送",
        "redirect": "管理接口重定向已拒绝，未转发",
        "calibration_stopped": "管理校准已停止，剩余操作未执行",
    }
    reason = ("管理接口 HTTP %s，结果未验证" % error.status
              if error.kind == "http" else reasons.get(error.kind, "管理请求失败，结果未验证"))
    failure = ApiError(error.status, reason, "<redacted>")
    failure.admin_kind = error.kind
    failure.retry_after = error.retry_after
    return failure


def _start_admin_calibration(api, cfg, label, total=0):
    import admin_io
    calibration = getattr(api, "_admin_calibration", None)
    if calibration is None:
        try:
            seconds = float(cfg.get("http_timeout") or 30)
        except (TypeError, ValueError):
            seconds = 30
        calibration = admin_io.Calibration(getattr(api, "base", ""), seconds)
        api._admin_calibration = calibration
    calibration.phase(label, total)
    return calibration


@contextmanager
def _admin_calibration_scope(api, cfg, label, total):
    owned = getattr(api, "_admin_calibration", None) is None
    calibration = _start_admin_calibration(api, cfg, label, total)
    try:
        yield calibration
    finally:
        if owned:
            del api._admin_calibration


def _calibration_failure_reason(error, fallback):
    status = getattr(error, "status", None)
    if status:
        return "管理接口 HTTP %s，结果未验证" % status
    if status == 0:
        return "管理网络请求失败或截止，结果未验证"
    return fallback


def _bounded_admin_http(method, url, headers, body, timeout, retries,
                        deadline, origin, progress):
    import admin_io
    attempts = min(2, max(1, int(retries or 1)))
    for attempt in range(attempts):
        # Never outsource ownership/consent checks to the transport child.
        _check_admin_write_guard(method)
        try:
            return admin_io.request(method, url, headers, body, timeout,
                                    deadline, origin, progress)
        except admin_io.Failure as error:
            transient = error.kind in ("network", "worker") or error.status in (500, 502, 503, 504)
            wait = 1.5 * (attempt + 1)
            if transient and attempt < attempts - 1 and time.monotonic() + wait < deadline:
                if progress:
                    progress("管理请求暂时失败，按同一幂等身份顺序重试")
                time.sleep(wait)
                continue
            raise _admin_failure(error) from None


def _with_import_calibration_cleanup(function):
    from functools import wraps

    @wraps(function)
    def run(api, *args, **kwargs):
        previous = getattr(api, "_admin_calibration", None)
        try:
            return function(api, *args, **kwargs)
        finally:
            current = getattr(api, "_admin_calibration", None)
            if current is not previous:
                if previous is None:
                    if current is not None:
                        del api._admin_calibration
                else:
                    api._admin_calibration = previous
    return run


class Sub2Api(object):
    def __init__(self, s):
        # 地址为空时立刻报错，不要等到发请求才炸。
        #
        # 以前这里是 `s["sub2api_base_url"].rstrip("/")`，空字符串能通过，
        # 然后 self.base + path 得到 "/api/v1/admin/groups/all"，
        # 最后在 urllib 里抛：
        #     ValueError: unknown url type: '/api/v1/admin/groups/all'
        # 那句报错完全没提"是你的地址没配"，排查要往回翻好几层。
        # 这里兜住，任何入口（菜单、一键导入、别的脚本 import）都绕不过去。
        base = str(s.get("sub2api_base_url") or "").strip()
        if not base:
            raise RuntimeError(
                "sub2api 地址为空（sub2api_base_url）。\n"
                "        本机：菜单选 [5] 填写，或编辑同目录的 设置.json。\n"
                "        容器：在 .env 里设 SUB2API_BASE_URL=http://sub2api:8080\n"
                "        （容器里不要写 127.0.0.1，那指向容器自己）")
        if not base.lower().startswith(("http://", "https://")):
            raise RuntimeError(
                "sub2api 地址缺少协议头：%r\n"
                "        要写成 http://主机:端口 或 https://主机，例如\n"
                "            http://sub2api:8080\n"
                "            https://your-sub2api.example.com" % base)
        self.base = base.rstrip("/")
        self.key = str(s.get("sub2api_admin_key") or "").strip()
        self.timeout = 30
        # 批量创建一次要服务端逐条建号并触发异步探测，30 秒太紧，单独放宽
        self.batch_timeout = 120
        # 允许运维指定 UA（见 _http 关于 Cloudflare 1010 的说明）
        self.user_agent = (s.get("user_agent") or "").strip() or DEFAULT_USER_AGENT

    def _call(self, method, path, body=None, timeout=None, idem=None):
        if not idem and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            idem = idempotency_key({"method": method.upper(), "path": path, "body": body})
        headers = {"x-api-key": self.key, "User-Agent": self.user_agent}
        if idem:
            headers["Idempotency-Key"] = idem
        st, raw = self._request(method, path, headers, body, timeout)
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ApiError(st, "非 JSON 返回：" + raw[:300], path)

    def _request(self, method, path, headers, body=None, timeout=None):
        import admin_io
        calibration = getattr(self, "_admin_calibration", None)
        deadline = None
        deadline_token = None
        try:
            deadline = calibration.request_deadline() if calibration is not None else None
            if deadline is not None:
                deadline_token = admin_io.REQUEST_DEADLINE.set(deadline)
            for attempt in range(3):
                if calibration is not None:
                    calibration.check()
                _check_admin_write_guard(method)
                try:
                    bounded = ({"_admin_deadline": deadline, "_admin_origin": self.base,
                                "_admin_progress": calibration.progress}
                               if calibration is not None else {})
                    result = _http(method, self.base + path, headers, body,
                                   timeout=timeout or self.timeout, _allow_proxy_fallback=False,
                                   _admin_request=True, **bounded)
                    if calibration is not None:
                        if time.monotonic() >= deadline:
                            raise _admin_failure(admin_io.Failure("deadline"))
                        calibration.check()
                        # A guard/detail GET is only part of a calibration item.
                        # Reset consecutive failures after its final readback,
                        # not before a later plan/read/write can still fail.
                    return result
                except ApiError as error:
                    if error.status != 429 or attempt == 2:
                        raise
                    wait = admin_retry_wait(getattr(error, "retry_after", None))
                    if wait > 120 or deadline is not None and time.monotonic() + wait >= deadline:
                        raise  # Never shorten a server cooldown to fit a local budget.
                    print("    管理API限流，等待 %.1f 秒后重试（%d/2）。" % (wait, attempt + 1), flush=True)
                    remaining = wait
                    while remaining > 0:
                        interval = min(5.0, remaining) if calibration is not None else remaining
                        time.sleep(interval)
                        remaining -= interval
                        if calibration is not None:
                            calibration.check()
                            if remaining > 0:
                                calibration.progress("正在遵守管理接口冷却时间")
        except admin_io.Failure as failure:
            error = _admin_failure(failure)
            if calibration is not None:
                calibration.record_error(error)
            raise error from None
        except ApiError as error:
            if calibration is not None:
                calibration.record_error(error)
            raise
        finally:
            if deadline_token is not None:
                admin_io.REQUEST_DEADLINE.reset(deadline_token)


    def ping(self):
        try:
            n = len(self.list_groups())
            return True, "连接正常，当前 %d 个分组" % n
        except ApiError as ex:
            if ex.status == 401:
                return False, ("密钥不对（401）。请检查 设置.json 里的 sub2api_admin_key：\n"
                               "        必须是 x-api-key 那一个（admin- 开头的管理密钥），\n"
                               "        不是面板登录密码、也不是用户的 API Key。")
            if ex.status == 403:
                return False, ("管理接口请求被拒绝（403），原因尚未确认。"
                               "请核对部署端的管理接口权限与访问策略；"
                               "仅凭状态码无法判断是否为密钥问题。")
            if ex.status == 404:
                return False, "接口 404。检查 sub2api 地址是否正确。"
            if ex.status == 423:
                # sub2api 新版本给管理员加了**合规确认门禁**
                # （server/middleware/admin_compliance.go）：管理员没在面板上
                # 确认合规承诺之前，所有 /api/v1/admin/* 都返回 423 Locked。
                # 密钥明明是对的、地址也是对的，症状却像"认证失败"，
                # 是本工具最容易被误判成"密钥填错"的一种情况。
                return False, ("被 sub2api 的合规确认门禁拦住（423 Locked）。\n"
                               "        这不是密钥问题：请先用管理员账号登录 sub2api 面板，\n"
                               "        按提示确认管理员合规承诺（每个管理员各确认一次），\n"
                               "        确认后本工具即可正常写入。\n"
                               "        原文：" + ex.body[:120])
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
        wire = dict(patch)
        if "proxy_id" in wire and wire["proxy_id"] is None:
            # Native *int64 treats JSON null as omitted; explicit zero clears it.
            # Keep desired/readback None and normalize only the outgoing DTO.
            wire["proxy_id"] = 0
        return self._call("PUT", "/api/v1/admin/accounts/%d" % aid, wire,
                          idem=idempotency_key({"id": aid, "patch": wire}))

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
        if "proxy_id" in body and body["proxy_id"] is None:
            body["proxy_id"] = 0
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
        path = "/api/v1/admin/accounts/%d/scheduled-test-plans" % aid
        status, raw = self._request("GET", path,
                                   {"x-api-key": self.key, "User-Agent": self.user_agent})
        if status != 200:
            raise ApiError(status, "探活计划列表请求失败", path)
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            raise ApiError(status, "探活计划列表返回非JSON数据", path)
        # v0.2.20 uses a literal JSON null for an empty native list. Only
        # this successful transport response is normalized; errors stay errors.
        return [] if parsed is None and raw.strip() == "null" else parsed

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
        # Synchronization and deletion guards require full credentials/extra.
        return self._call(
            "GET", "/api/v1/admin/accounts?page=%d&page_size=%d" % (page, size))

    def reveal_account_credentials(self, account):
        """Explicit one-ID admin export; never persist or print credential data."""
        aid = account["id"]
        if not _is_int(aid) or aid <= 0 or account.get("parent_account_id") is not None:
            raise ValueError("ordinary exact account id required")
        payload = _unwrap_one(self._call(
            "GET", "/api/v1/admin/accounts/data?ids=%d&include_proxies=false" % aid))
        items = payload.get("accounts") if isinstance(payload, dict) else None
        if (not isinstance(items, list) or len(items) != 1 or payload.get("skipped_shadows", 0)
                or not isinstance(items[0], dict)):
            raise ValueError("exact credential export unavailable")
        exported = items[0]
        if any(exported.get(k) != account.get(k) for k in ("name", "platform", "type", "notes")):
            raise ValueError("exported identity differs from requested account")
        credentials = exported.get("credentials")
        if not isinstance(credentials, dict):
            raise ValueError("credential export unavailable")
        if any(v != credentials.get(k) for k, v in (account.get("credentials") or {}).items()
               if k not in SENSITIVE_CREDENTIAL_FIELDS):
            raise ValueError("exported credential parameters changed")
        fresh = _unwrap_one(self._call("GET", "/api/v1/admin/accounts/%d" % aid))
        if fresh.get("id") != aid or _recovery_snapshot(fresh) != _recovery_snapshot(account):
            raise ValueError("account changed while reading its credentials")
        return copy.deepcopy(credentials)

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
HEADER_OK_PLATFORMS = {"anthropic", "openai", "kimi", "zhipu", "deepseek", "minimax", "grok",
                       "gemini"}
# 需求第 2 条⑴：站方只认特定客户端时，请求头没有伪装到位就会回
# `503 No available accounts / this group only allows ...`。CPA 那边的做法是
# 在凭据的 headers 里显式写一套客户端头（实测 config.yaml 里 75/77 条
# claude-api-key 都手写了 user-agent + anthropic-beta + x-app + x-stainless-*）。
# 本工具原先只做**被动搬运**：CPA 里写了就搬，没写就什么都不带——于是那些
# 靠 CPA 默认伪装跑通的站点，导入 sub2api 后就退化成裸请求。
#
# 这里补一套**与 sub2api 自身内置伪装一致的客户端头**作为兜底：只在 CPA
# 没给同名头时补，不覆盖用户显式配置。来源是 sub2api 自己的常量表
#（backend/internal/pkg/claude/constants.go 的 DefaultHeaders + beta 族），
# 所以补出来的形态与 sub2api 原生 Claude Code 伪装完全相同，不会自相矛盾。
#
# 注意：不补 anthropic-version——CPA 给 75/77 条都写了，缺的那些可能是有意的；
# 也不补 accept-encoding（在 sub2api 的黑名单里，带了会被 400 拒整条账号）。
CLIENT_PROFILE_HEADERS = {
    "anthropic": {
        "user-agent": "claude-cli/2.1.258 (external, cli)",
        "x-app": "cli",
        "anthropic-dangerous-direct-browser-access": "true",
        "x-stainless-lang": "js",
        "x-stainless-package-version": "0.94.0",
        "x-stainless-os": "Linux",
        "x-stainless-arch": "arm64",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v24.3.0",
        "x-stainless-retry-count": "0",
        "x-stainless-timeout": "600",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,"
                          "interleaved-thinking-2025-05-14",
    },
    "openai": {
        "user-agent": "codex_cli_rs/0.20.0 (Mac OS 15.0; arm64) vscode",
        "originator": "codex_cli_rs",
    },
    "gemini": {
        "user-agent": "GeminiCLI/0.1.5 (darwin; arm64)",
        "x-goog-api-client": "gl-go/1.24.0 gccl/0.1.5",
        "accept": "*/*",
    },
}
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

# 按修改要求P8分档：401=30分钟，402=15分钟，400/403=1分钟。
# 余额充值可能很快完成，域名/预算池规则也必须遵守同一15分钟期限。
# 这里只配置本工具下发的临时不可调度规则，不清除或缩短上游真实429、
# Retry-After或运维手工设置的运行时冷却。未列错误码沿用1分钟默认。
COOLDOWN_MINUTES_BY_CODE = {
    401: 30,   # unauthorized：key 失效/被撤销
    402: 15,   # payment required：按修改要求，充值后最多等待15分钟
    403: 1,    # forbidden：按修改要求，暂时权限错误1分钟重试
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
        "description": "本工具补充：402 配额耗尽，15分钟后重试",
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
    402: 15,   # payment required：预算池规则同样遵守15分钟上限
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


def build_model_mapping(models, excluded=None, platform=None, source_section=None,
                        probe=None, preserve_declared=False):
    """CPA models[] -> sub2api credentials.model_mapping。

    关键语义：model_mapping 非空即**白名单**（account.go:841-861）。不在表里的
    模型会让整个账号被踢出调度候选集，网关直接报"无可用账号"，不是简单的改名
    失效。所以每个要放行的模型都必须出现在表里。

    显式配置 preserve_declared 时，仅注册源声明的客户端 alias，不把未暴露的
    原名添加为另一条公开入口，也不重新按代际裁剪用户已验证的列表。
    excluded-models 与 CPA 一致，匹配注册后的 alias。非显式列表保留既有
    代际回退选择。sub2api 没有模型黑名单字段，通过白名单减法表达排除。

    Args:
        models: list of {"name": str, "alias": str}
        excluded: list of wildcard patterns
        platform: "gemini" | "anthropic" | "openai" (应用就高原则)
        source_section: CPA 里的来源段名。openai 平台靠它区分
            codex（单族）与 openai-compatibility（多族）。两者 platform
            都是 "openai"，只传 platform 无法区分。
        probe: 该 (段, 域名) 的连通性探测结果 {"latest":bool,"prev":bool}，
            由 model_probe 产出。需求④：最新代探测不通、次最新代连通时，
            两代都保留。**传 None 时行为与不带探测完全一致**。
        preserve_declared: 已在源配置中明确声明的 models[] 是权威列表。
    """
    # Step 1: 应用就高原则过滤
    if platform and MODEL_SELECTION_AVAILABLE and models and not preserve_declared:
        # select_highest_models 期望 list[dict]，直接传入
        models = select_highest_models(models, platform,
                                       source_section=source_section,
                                       series=MODEL_SERIES,
                                       probe=probe)

    # Step 2: 应用 excluded-models 黑名单
    out = {}
    for m in models or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m["name"]).strip()
        alias = str(m.get("alias") or name).strip()
        # CPA registers ModelInfo.ID=alias, then applies exclusions to that ID.
        exposed = alias if preserve_declared else name
        if _match_excluded(exposed, excluded):
            continue
        out[alias] = name
        # 补全同名映射：否则客户端按上游原名请求会被白名单挡掉
        if alias != name and not preserve_declared:
            out.setdefault(name, name)
    return out


def record_model_mapping(record, excluded=True):
    """Configured models are authoritative; generated defaults retain selection."""
    return build_model_mapping(record.get("models_raw"),
        record.get("excluded") if excluded else None, record.get("platform"),
        source_section=record.get("source_section") or record.get("section"),
        probe=record.get("_probe"), preserve_declared=record.get("_models_explicit", False))


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


# Fixed official consumer v8.0.60, config_types.go SHA256 below. New fields
# require an adapter review; reading a newer model directory is not support.
CPA_CAPABILITY_SNAPSHOT = {
    "consumer_version": "v8.0.60",
    "consumer_commit": upstream_contract.PINNED["cpa"]["commit"],
    "verified_at": upstream_contract.VERIFIED_AT,
    "config_types_sha256": "a234c8850ae30b8124f17613abdbe56f8bd0e3dd5e6cbc9ebc0e809c4f3ceb53",
}
_CPA_COMMON_FIELDS = frozenset({"api-key", "priority", "weight", "prefix", "base-url", "proxy-url",
    "connection-pool", "models", "headers", "excluded-models", "disable-cooling", "request-retry", "request-scoped-errors"})
CPA_MANAGED_FIELDS = {
    "claude-api-key": _CPA_COMMON_FIELDS | {"rebuild-mid-system-message", "native-tool-search", "cloak", "fingerprint-profile", "experimental-cch-signing"},
    "codex-api-key": _CPA_COMMON_FIELDS | {"websockets", "alpha-search", "disable-codex-cloaking"},
    "gemini-api-key": _CPA_COMMON_FIELDS,
    "interactions-api-key": _CPA_COMMON_FIELDS,
    "xai-api-key": _CPA_COMMON_FIELDS | {"websockets", "alpha-search", "disable-codex-cloaking"},
    "openai-compatibility": frozenset({"name", "priority", "disabled", "prefix", "base-url", "api-key-entries",
        "connection-pool", "models", "headers", "support-prompt-cache-key", "disable-cooling", "request-retry", "request-scoped-errors"}),
}
_CPA_NESTED_FIELDS = {
    "connection-pool": {"enabled", "idle-conn-timeout", "max-idle-conns-per-host"},
    "request-scoped-errors": {"status", "match", "match-regexr", "action"},
    "cloak": {"mode", "strict-mode", "sensitive-words", "cache-user-id"},
    "thinking": {"min", "max", "zero-allowed", "dynamic-allowed", "disable-type", "levels"},
    "api-key-entries": {"api-key", "weight", "proxy-url", "connection-pool"},
    "request-relay-tls": {"impersonate", "ca-bundle"},
}


def cpa_capability_issues(document, settings):
    errors = []
    def validate(value, known, path):
        if isinstance(value, list):
            for index, item in enumerate(value):
                validate(item, known, "%s[%d]" % (path, index))
            return
        if not isinstance(value, dict):
            return  # Existing shape checks handle scalar/list mistakes separately.
        for key, item in value.items():
            if key not in known:
                errors.append(path + "." + str(key))
            elif key in _CPA_NESTED_FIELDS:
                validate(item, _CPA_NESTED_FIELDS[key], path + "." + key)
    for section, known in CPA_MANAGED_FIELDS.items():
        for index, entry in enumerate(document.get(section) or []):
            if not isinstance(entry, dict):
                continue
            path = "%s[%d]" % (section, index)
            validate(entry, known | {"disabled", "request-relay-tls"}, path)
            models = {"name", "alias", "display-name", "max-context-length", "force-mapping", "is-compat", "thinking"}
            if section in ("codex-api-key", "xai-api-key"):
                models |= {"support-configuration-update"}
            if section == "openai-compatibility":
                models |= {"image", "input-modalities", "output-modalities", "use-max-completion-tokens"}
            validate(entry.get("models"), models, path + ".models")
    drift = []
    source_root = settings.get("_runtime_cpa_src") or settings.get("cpa_src") or CPA_SRC
    source = os.path.join(source_root, "internal", "config", "config_types.go") if source_root else ""
    if source and os.path.isfile(source):
        with open(source, "rb") as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        if (digest != CPA_CAPABILITY_SNAPSHOT["config_types_sha256"]
                and upstream_contract.reviewed_source_equivalence(
                    "cpa", "internal/config/config_types.go", digest) is None):
            drift.append("CPA 配置结构源码与已核对的 v8.0.60 能力快照不同；须复核递归字段及消费者语义，禁止导入写入和自动删除")
    elif source_root and os.path.isdir(source_root):
        drift.append("CPA 源码目录缺少配置结构合同；禁止导入写入和自动删除，先修复来源或明确使用内置固定合同")
    drift.extend(settings.get("_upstream_contract_errors") or [])
    return sorted(set(errors)), drift


def _upstream_source_contract(settings):
    configured = {"sub2api": settings.get("sub2api_src") or SUB2API_SRC,
                  "cpa": settings.get("cpa_src") or CPA_SRC}
    runtime = {"sub2api": settings.get("_runtime_sub2api_src") or configured["sub2api"],
               "cpa": settings.get("_runtime_cpa_src") or configured["cpa"]}
    def canonical(roots):
        return {kind: os.path.normcase(os.path.realpath(os.path.abspath(path))) for kind, path in roots.items()}
    configured, runtime = canonical(configured), canonical(runtime)
    binding = {"configured_roots": configured, "runtime_roots": runtime}
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
    return dict(binding, binding_sha256=digest,
                evidence={kind: upstream_contract.source_evidence(path, kind) for kind, path in runtime.items()})


def _validate_upstream_source_contract(plan, settings):
    """Rehash both actual source roots before consuming a saved import plan."""
    contract = plan.get("source_contract")
    if contract is None:
        if plan.get("capability_snapshot"):
            raise ValueError("source contract provenance missing; regenerate the import plan")
        return  # Internal legacy fixtures without a saved capability snapshot.
    if not isinstance(contract, dict):
        raise ValueError("invalid upstream source contract")
    binding = {key: contract.get(key) for key in ("configured_roots", "runtime_roots")}
    for roots in binding.values():
        if (not isinstance(roots, dict) or set(roots) != {"sub2api", "cpa"}
                or any(not isinstance(path, str) or not os.path.isabs(path) for path in roots.values())):
            raise ValueError("invalid upstream source root binding")
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
    if digest != contract.get("binding_sha256"):
        raise ValueError("upstream runtime source roots changed; regenerate the import plan")
    current = _upstream_source_contract(settings)
    if current["configured_roots"] != contract["configured_roots"]:
        raise ValueError("upstream configured source roots changed; regenerate the import plan")
    for kind in ("cpa", "sub2api"):
        expected = (contract.get("evidence") or {}).get(kind)
        observed = upstream_contract.source_evidence(contract["runtime_roots"][kind], kind)
        if (not isinstance(expected, dict) or observed["status"] == "review_required"
                or observed != expected):
            raise ValueError("current %s upstream contract drift; regenerate and review the import plan" % kind)
        # A configured root may appear or change while a pinned cache was used.
        configured = upstream_contract.source_evidence(current["configured_roots"][kind], kind)
        if configured["status"] == "review_required":
            raise ValueError("current configured %s upstream contract drift" % kind)
        marker = "_runtime_sub2api_src" if kind == "sub2api" else "_runtime_cpa_src"
        if settings.get(marker) and current["runtime_roots"][kind] != contract["runtime_roots"][kind]:
            raise ValueError("current runtime %s source root changed" % kind)


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
    all_m = record_model_mapping(r, excluded=False)
    if not all_m:
        # An explicit list filtered to nothing must never turn into allow-all.
        return bool(r.get("models_raw"))
    return not record_model_mapping(r)


def _apply_client_profile(headers, platform, source=""):
    """把 CLIENT_PROFILE_HEADERS 里该平台缺失的客户端头补齐。

    只在 CPA 没给同名头时补，绝不覆盖用户显式配置——用户写的值优先，
    这与 CPA 自己的 ensureHeader/setHeader 分流是同一个原则。
    返回补入的头名字典（供日志说明"补了什么"）。
    """
    added = {}
    for k, v in CLIENT_PROFILE_HEADERS.get(platform, {}).items():
        if k in headers:
            continue
        headers[k] = v
        added[k] = source or platform
    return added


def build_header_overrides(headers, platform):
    """CPA headers -> credentials.header_overrides。

    返回 (可用的头, 被丢弃的头名)。校验规则与 sub2api 的
    normalizeHeaderOverrideEntry 保持一致：名字小写、只含合法 token 字符、
    不在黑名单、长度不超限、值里不能有控制字符。任一条不符就会被 400 拒掉
    整条账号，所以这里提前剔除而不是交给服务端报错。
    """
    if not isinstance(headers, dict):
        headers = {}
    if platform not in HEADER_OK_PLATFORMS:
        return {}, sorted(str(k).lower() for k in headers)

    keep, drop = {}, []
    for k, v in (headers or {}).items():
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
    # 需求 2⑴：补齐客户端形态的头（只补缺，不覆盖用户显式配置）。
    added = _apply_client_profile(keep, platform)

    # 截断放在补头**之后**：CLIENT_PROFILE_HEADERS 是伪装的最低要求，
    # 若先按 64 条截断再补，补完又超限；若补完不截断，服务端 400 拒整条账号。
    # 超出时优先保留客户端形态头，被砍掉的记进 drop 让用户看得见。
    if len(keep) > MAX_HEADER_ENTRIES:
        removable = [k for k in sorted(keep) if k not in added]
        for k in removable[:len(keep) - MAX_HEADER_ENTRIES]:
            keep.pop(k, None)
            drop.append(k + "(超出 %d 条上限)" % MAX_HEADER_ENTRIES)

    for k in added:
        drop.append(k + "(补客户端形态)")
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
                "_models_explicit": isinstance(entry.get("models"), list) and bool(entry["models"]),
                "_source_fidelity": True,
                "rse": entry.get("request-scoped-errors") or [],
                "excluded": entry.get("excluded-models") or [],
                "websockets": bool(entry.get("websockets")), "alpha_search": alpha,
                "fingerprint": entry.get("fingerprint-profile") or "",
                "request_relay_tls": copy.deepcopy(entry.get("request-relay-tls")),
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
                entries = entry.get("api-key-entries") or []
                for idx, sub in enumerate(entries):
                    if isinstance(sub, dict) and sub.get("api-key"):
                        r = dict(base)
                        r["api_key"] = sub["api-key"]
                        r["weight"] = sub.get("weight")
                        # openai-compatibility 顶层没有 proxy-url，只在 key 级别有
                        r["proxy_url"] = sub.get("proxy-url") or ""
                        recs.append(r)
                    else:
                        # 需求第 8 条：以前这条是**无声丢弃**——api-key 为空或
                        # 不是 dict 的子条目直接消失，用户永远不知道自己漏填了。
                        # 现在记进 dropped_notes，跑完能看见丢了几条、丢在哪。
                        dropped_notes.append(
                            "%s[%s] 第 %d 个 api-key-entries 没有 api-key，已跳过"
                            % (section, entry.get("name") or base.get("fp") or "?",
                               idx + 1))
            elif api_key:
                r = dict(base)
                r["api_key"] = api_key
                r["weight"] = entry.get("weight")
                recs.append(r)
            else:
                # 同上：非 openai-compatibility 段的空 api-key 凭据以前也是
                # 无声丢弃（collect 顶部只在 base-url 也为空时才记账）。
                dropped_notes.append(
                    "%s 有一条凭据没有 api-key（base-url=%s），已跳过"
                    % (section, base.get("base_url") or "空"))
    unique, seen_credentials = [], {}
    for record in recs:
        identity = (record.get("platform"), record.get("section"),
                    str(record.get("base_url") or "").rstrip("/"), record.get("api_key"))
        if identity in seen_credentials:
            dropped_notes.append("重复 URL 与凭据已折叠，保留首条配置")
            continue
        seen_credentials[identity] = record
        unique.append(record)
    return unique, dropped_notes


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
    if recs and all(r.get("_source_fidelity") for r in recs):
        # A tested CPA config is authoritative, including ties across domains.
        values = sorted({r["priority"] if _is_int(r.get("priority")) else 0
                         for r in recs}, reverse=True)
        buckets = {value: (index + 1) * 10 for index, value in enumerate(values)}
        for record in recs:
            value = record["priority"] if _is_int(record.get("priority")) else 0
            record["new_priority"] = buckets[value]
        for rank, record in enumerate(sorted(recs, key=lambda r: (
                r["new_priority"], str(r.get("group")), str(r.get("base_url")),
                account_fingerprint(r))), 1):
            record["rank"] = rank
        return

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

    # ---- 第二步：组内定序，保证同组内桶号唯一 ----
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
    # 现在改成：按**分组各自**编号 —— 同组内把所有 (渠道, 域名) 单元按
    # (序位降序, 域名字典序) 排序后依次编号，组内桶号唯一、各组都从 10 起。
    #
    # 为什么从"全局唯一"再改成"组内唯一"（2026-09-19）：
    #   分桶的作用域本来就是分组。sub2api 选号按 group_id 取候选
    #   （SelectAccountWithGroup → account_groups 中间表），实测 231 条
    #   账号在 account_groups 里是 231 条映射、**跨组账号 0 条** —— 每条
    #   账号只属于一个分组，所以 CPA-Claude 的桶号 10 与 CPA-OpenAI 的
    #   320 永远不会被放在一起比较。
    #
    #   全局编号的代价：某个组的最大桶号 = 全配置域名单元总数 × 10，
    #   加一个域名到 A 组会让 B 组的上限跟着涨，站点多的配置会逼近
    #   sub2api 的 priority 上限（不受我们控制）；而且界面上 Codex 显示
    #   120、Claude 显示 10 会让人误判谁更优先。
    #
    #   组内编号把"跨渠道撞桶"从根上消除（不同分组根本不在一个命名空间
    #   比较），同时保住真正要保的两条：同组同域名同桶、同组不同域名桶号
    #   互不相同。见 new_remap_priority.assign_group_local_buckets 的长注释。
    adjusted_bucket = assign_group_local_buckets(
        list(tier_of.keys()),
        key=lambda k: (-tier_of[k], k[1]))

    # 保留基础映射表：仅用于日志/回溯"某个 CPA 序位被映射到了哪个基准桶"。
    # 这张表只进日志，不参与实际赋值，所以保持全局去重编号即可。
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
    # Source-fidelity records retain their declared scheduling order below;
    # health_score still controls their load_factor within the selected tier.
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

    # ---- 第三步：按分组各自 dense rank ----
    # 文档第 2 条⑶④："同一个类型不同域名的优先级一定要不同，哪怕算出来相同，
    # 也要适当做点微调给出点偏差"。
    #
    # 编号范围是**分组内**，不是全局。为什么（2026-09-19 改，附实测证据）：
    #   · 选号按 group_id 取候选（SelectAccountWithGroup → account_groups），
    #     实测 231 条账号 231 条映射、**跨组账号 0 条**，所以两个分组的桶号
    #     之间不存在可比性；
    #   · 全局编号会让一个组的桶号上限被**别的组**的域名数推高（加域名到 A 组、
    #     B 组上限跟着涨），有撞上 sub2api priority 上限的隐患；
    #   · 界面上会误读（Codex 显示 120、Claude 显示 10，看着像低一等）。
    #
    # 以前是 `for group: for i, k in enumerate(keys, 1): bucket = i * 10`，
    # 那个写法的问题是**每组都从 1 重新开始**却没有分组语义 —— 当时确实会
    # 跨渠道撞桶；现在的 assign_group_local_buckets 明确了"同一分组内唯一"，
    # 不同分组各编各的，跨撞在定义上不可能发生。
    #
    # 排序键：健康分降序 -> 域名字典序。
    # 第二项只为确定性（同一份配置多次运行得到同样的桶号），不参与优先级语义。
    bucket_of = assign_group_local_buckets(
        list(health.keys()),
        key=lambda k: (-health[k], str(k[1])))
    # 回填时按 (分组, 桶号) 排序，让日志与对照表的顺序稳定可读
    all_keys = sorted(health.keys(), key=lambda k: (str(k[0]), bucket_of[k]))

    # 把桶号回填到每条记录。原先是嵌套在排序循环里、每条记录都要重算一次
    # _host_key，成了 O(域名数 × 记录数)；这里改成先建索引再一次赋值。
    ranked = 0
    for k in all_keys:
        b = bucket_of[k]
        for r in recs:
            if _host_key(r) == k:
                if not r.get("_source_fidelity"):
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
    return ranked, "按实测健康度重排 %d 个域名单元（各分组内独立定序）。%s" % (
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
    # Codex and compatibility can share URL/key/platform while requiring
    # different endpoints, model policies and groups. Preserve legacy names
    # unless they actually collide; disambiguation is independent of ordering.
    identities = defaultdict(set)
    for record in recs:
        identities[record["name"]].add((record.get("source_section") or record.get("section"),
                                         record.get("group")))
    for record in recs:
        if len(identities[record["name"]]) > 1:
            identity = "%s|%s" % (record.get("source_section") or record.get("section"),
                                   record.get("group"))
            suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
            record["name"] += "-" + suffix
        scope = record.get("_source_scope", "default")
        if scope != "default":
            record["name"] += "-s" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]


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
        if r.get("_source_fidelity"):
            return True
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
    # probe：需求 2⑵④ 的连通性探测结果，由 build_plan 写进记录。
    # 没有它时 build_model_mapping 退回纯静态的就高逻辑。
    mm = record_model_mapping(r)
    if mm:
        creds["model_mapping"] = mm
    elif serves_nothing(r):
        # 全部模型都被排除。空表在 sub2api 里等于放行全部，必须放占位项，
        # 否则这条在 CPA 已被完全禁用的凭据会在 sub2api 上接管所有流量。
        creds["model_mapping"] = ({} if r.get("_source_fidelity") else
                                  {BLOCK_ALL_SENTINEL: BLOCK_ALL_SENTINEL})
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

    extra = {"upstream_billing_probe_enabled": False,
             "upstream_billing_rate_sync_enabled": False,
             "cpa2sub2api_source": _record_source_metadata(r, cfg)}
    if r.get("_source_fidelity"):
        extra.update(openai_passthrough=False, anthropic_passthrough=False,
                     cpa2sub2api_source_disabled=needs_inactive(r, cfg))
    if r["responses_mode"]:
        extra["openai_responses_mode"] = r["responses_mode"]
    if r["websockets"]:
        extra["openai_ws_enabled"] = True
    # TLS fingerprint 迁移（所有平台支持）
    # 2026-09-13 修复：原先只对 anthropic 启用，现改为所有平台通用
    learned_tls = {probe_policy.normalize_host(h)
                   for h in cfg.get("detected_tls_fingerprint_hosts", [])}
    if r["fingerprint"] or probe_policy.normalize_host(r.get("base_url")) in learned_tls:
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
    # Native billing probes have their own frequency and bypass our site policy.
    # Always disable them; authorized diagnostics run only through weekly plans.
    acc["upstream_billing_probe_enabled"] = False
    acc["upstream_billing_rate_sync_enabled"] = False
    if r.get("_source_fidelity") and needs_inactive(r, cfg):
        # Desired persisted state. Native creation is protected separately because
        # CreateAccountRequest ignores status and always starts accounts active.
        acc.update(status="inactive", schedulable=False)

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
        except (TypeError, ValueError) as ex:
            # 健康分算不出来时不发 load_factor，sub2api 用自己的默认值。
            # 这是有意的降级，但要说清楚 —— load_factor 直接影响桶内负载
            # 分配，静默缺失会让人以为"重排生效了但调度没变"。
            log_exc("health_score=%r 无法转成浮点，本条不发 load_factor" % (hs,), ex)

    # auto_pause_on_expired 必须显式写。sub2api 的 schema 默认是 true
    # （ent/schema/account.go:139 Default(true)，由 sync_constants 从源码
    # 读进 ACCOUNT_DEFAULTS），不传就吃这个默认，界面上表现为
    # "过期自动暂停调度"已勾选——而 CPA 侧没有过期概念、我们也不写
    # expires_at，勾着它只会在将来某天莫名其妙把账号暂停掉。
    # CreateAccountRequest 有该字段（*bool），所以能显式关掉。
    apoe = cfg.get("auto_pause_on_expired", False)
    acc["auto_pause_on_expired"] = bool(apoe)

    acc["extra"] = extra
    gid = group_id_map.get(r["group"])
    if gid:
        acc["group_ids"] = [gid]
    pid = proxy_id_map.get(r["proxy_url"]) if r["proxy_url"] else None
    acc["proxy_id"] = pid or None

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
    relay = cfg.get("request_relay") or {}
    if _truthy(relay.get("enabled")):
        import request_relay
        bundle = cfg.get("_request_relay_bundle") or _load_request_relay(relay)
        rid = request_relay.route_id(r, relay["source_scope"])
        if rid in bundle["routes"]:
            acc = request_relay.adapt_account(acc, r, bundle, relay["base_url"])
            acc["notes"] = (acc["notes"].replace("｜未导入项=", "｜原CPA参数=")
                            .replace("cloak_已写入extra但网关暂未实现", "cloak_由请求中继消费"))
        elif r.get("_request_relay_required"):
            raise request_relay.RelayError("required_route_missing_from_generation")
    return acc


def account_probe_base_url(account):
    """Use the validated original authority for relay accounts; never the relay host."""
    if "cpa2sub2api_source" in (account.get("extra") or {}):
        account_source_scope(account)
    if "cpa2sub2api_original_source" in (account.get("extra") or {}):
        import request_relay
        return request_relay.account_original_source(account)["base_url"]
    return (account.get("credentials") or {}).get("base_url") or ""


def _source_scope(config):
    value = config.get("source_scope", (config.get("request_relay") or {}).get(
        "source_scope", config.get("request_relay_source_scope", "default")))
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or value != value.strip():
        raise ValueError("source scope must be a nonempty stable name of at most 128 characters")
    return value


def _source_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _record_source_metadata(record, cfg):
    value = {"version": 1, "scope": record.get("_source_scope", _source_scope(cfg)),
             "source_section": record.get("source_section") or record.get("section"),
             "group": record.get("group"), "base_url": record.get("base_url") or "",
             "key_sha256": hashlib.sha256(str(record.get("api_key") or "").encode("utf-8")).hexdigest(),
             "key_fingerprint": record.get("fp") or account_fingerprint(record),
             "account_name": record.get("name")}
    value["identity_sha256"] = _source_digest(value)
    return value


def account_source_scope(account):
    """Validate explicit source ownership; invalid metadata is never legacy."""
    extra = account.get("extra") or {}
    if "cpa2sub2api_source" not in extra:
        if "cpa2sub2api_original_source" in extra:
            import request_relay
            try:
                return request_relay.account_original_source(account)["scope"]
            except request_relay.RelayError as ex:
                raise ValueError("invalid relay source ownership") from ex
        return None
    value = extra["cpa2sub2api_source"]
    if not isinstance(value, dict):
        raise ValueError("invalid source ownership")
    unsigned = {k: v for k, v in value.items() if k != "identity_sha256"}
    if (value.get("version") != 1 or _source_scope({"source_scope": value.get("scope")}) != value["scope"]
            or value.get("account_name") != account.get("name")
            or value.get("identity_sha256") != _source_digest(unsigned)
            or value.get("source_section") not in SECTION_MAP
            or not isinstance(value.get("base_url"), str)
            or not re.fullmatch("[0-9a-f]{8}", str(value.get("key_fingerprint") or ""))):
        raise ValueError("invalid source ownership")
    if value.get("kind") == "legacy_claim":
        if value.get("claimed_account_id") != account.get("id") or "key_sha256" in value:
            raise ValueError("invalid legacy source claim")
    elif not re.fullmatch("[0-9a-f]{64}", str(value.get("key_sha256") or "")):
        raise ValueError("invalid source key identity")
    if "cpa2sub2api_original_source" in extra:
        import request_relay
        original = request_relay.account_original_source(account)
        if (any(original.get(k) != value.get(k) for k in ("scope", "source_section", "group", "key_sha256"))
                or original.get("base_url", "").rstrip("/") != value["base_url"].rstrip("/")):
            raise ValueError("relay/source ownership mismatch")
    elif str((account.get("credentials") or {}).get("base_url") or "").rstrip("/") != value["base_url"].rstrip("/"):
        raise ValueError("source URL changed outside managed identity")
    return value["scope"]


def _visible_source_key(account):
    key = (account.get("_cpa2sub2api_raw_credentials", account.get("credentials")) or {}).get("api_key")
    if (not isinstance(key, str) or not key or any(mark in key for mark in ("*", "…"))
            or key.strip().lower() in ("redacted", "masked", "[redacted]")):
        return None
    return key


def _with_source_credentials(api, account, evidence=None):
    result = copy.deepcopy(account)
    if hasattr(api, "reveal_account_credentials"):
        try:
            result["_cpa2sub2api_raw_credentials"] = api.reveal_account_credentials(account)
        except Exception:
            result["_cpa2sub2api_credentials_unavailable"] = True
    if "_cpa2sub2api_raw_credentials" not in result and evidence is not None:
        if (not isinstance(evidence, dict) or any(evidence.get(k) != account.get(k)
                for k in ("name", "platform", "type", "notes"))
                or not isinstance(evidence.get("credentials"), dict)
                or any(v != evidence["credentials"].get(k) for k, v in (account.get("credentials") or {}).items()
                       if k not in SENSITIVE_CREDENTIAL_FIELDS)):
            raise ValueError("trusted export does not bind this exact account snapshot")
        result["_cpa2sub2api_raw_credentials"] = copy.deepcopy(evidence["credentials"])
        result["_cpa2sub2api_evidence_credentials"] = True
    return result


def _legacy_claim_metadata(account, record, scope):
    value = {"version": 1, "kind": "legacy_claim", "scope": scope,
             "claimed_account_id": account["id"], "source_section": record.get("source_section") or record["section"],
             "group": record.get("group"), "base_url": record.get("base_url") or "",
             "key_fingerprint": record.get("fp") or account_fingerprint(record), "account_name": record["name"]}
    value["identity_sha256"] = _source_digest(value)
    return value


def _strong_source_binding(account, record):
    value = (account.get("extra") or {}).get("cpa2sub2api_source") or {}
    if value and value.get("kind") != "legacy_claim":
        return _account_source_identity_matches(account, record, allow_platform_change=True)
    if "cpa2sub2api_original_source" in (account.get("extra") or {}):
        return _account_source_identity_matches(account, record, allow_platform_change=True)
    return _visible_source_key(account) == str(record.get("api_key") or "")


def _account_source_identity_matches(account, record, allow_platform_change=False):
    if not _is_migrated(account):
        return False
    scope = record.get("_source_scope", "default")
    current = account_source_scope(account)
    expected = _record_source_metadata(record, {"source_scope": scope})
    relayed = "cpa2sub2api_original_source" in (account.get("extra") or {})
    visible_key = _visible_source_key(account)
    if current is not None:
        if current != scope:
            return False
        value = (account.get("extra") or {}).get("cpa2sub2api_source")
        if value is None:
            import request_relay
            value = request_relay.account_original_source(account)
        if value.get("kind") == "legacy_claim":
            if not all(value.get(k) == expected.get(k) for k in (
                    "source_section", "group", "base_url", "key_fingerprint")):
                return False
            if visible_key is not None and visible_key != str(record.get("api_key") or ""):
                return False
        elif not all(value.get(k) == expected.get(k) for k in (
                "source_section", "group", "base_url", "key_sha256")):
            return False
        # A relay credential is a route token, not the original provider key.
        # A validated modern full binding owns credential drift. Synchronization
        # corrects a visible wrong key instead of abandoning the owned ID.
        return (account.get("platform") == record.get("platform")
                or allow_platform_change and value.get("kind") != "legacy_claim")
    if scope != "default" or account.get("platform") != record.get("platform"):
        return False
    notes = str(account.get("notes") or "")
    section = record.get("source_section") or record.get("section")
    return ("源自 CPA %s｜" % section in notes
            and re.search(r"(?:^|｜)fp=%s(?:｜|$)" % re.escape(record.get("fp") or account_fingerprint(record)), notes)
            is not None and (visible_key is None and "api_key" not in (
                account.get("_cpa2sub2api_raw_credentials", account.get("credentials")) or {})
                             or visible_key == str(record.get("api_key") or ""))
            and account_probe_base_url(account).rstrip("/") == str(record.get("base_url") or "").rstrip("/"))


def account_matches_source(account, record):
    return _account_source_identity_matches(account, record)


def resolve_source_accounts(accounts, records):
    """Resolve unique owned source signatures before import chooses new creates."""
    found, failures, used = {}, [], {}
    for record in records:
        name, candidates, invalid = record["name"], [], False
        for account in accounts:
            try:
                if _account_source_identity_matches(account, record, allow_platform_change=True):
                    candidates.append(account)
                elif account.get("name") == name:
                    invalid = True  # A same-name foreign or unverifiable row blocks creates.
            except (ValueError, RuntimeError):
                if account.get("name") == name:
                    invalid = True
        if invalid or len(candidates) > 1:
            failures.append((name, "来源身份冲突、损坏或候选不唯一，未创建或迁移"))
            continue
        if not candidates:
            continue
        account = candidates[0]
        aid = account.get("id")
        if not _is_int(aid) or aid <= 0 or aid in used:
            failures.append((name, "同一旧账号匹配多个配置条目，未创建或迁移"))
            if aid in used:
                previous = used[aid]
                found.pop(previous, None)
                failures.append((previous, "同一旧账号匹配多个配置条目，未创建或迁移"))
            continue
        used[aid] = name
        found[name] = account
    return found, failures


def _policy_source_scope(cfg):
    return _source_scope(cfg) if "source_scope" in cfg or "request_relay_source_scope" in cfg else None


def _unclaimed_record_matches(account, record):
    if account_source_scope(account) is not None or account.get("platform") != record.get("platform"):
        return False
    key = _visible_source_key(account)
    if (key is None or key != str(record.get("api_key") or "")
            or str((account.get("credentials") or {}).get("base_url") or "").rstrip("/")
            != str(record.get("base_url") or "").rstrip("/")):
        return False
    if _is_migrated(account):
        candidate = dict(record, _source_scope="default")
        return account_matches_source(account, candidate)
    # Explicit IDs plus a full visible binding authorize only the reserved old
    # position-name namespace; ordinary/manual names remain outside this path.
    return re.fullmatch(r"ANT-%s-[0-9]+" % re.escape(host_of(record.get("base_url") or "")),
                        str(account.get("name") or "")) is not None


def _require_unique_claim_candidate(api, account, record):
    rows, error = _fetch_all_accounts(api)
    if error or not any(r.get("id") == account["id"] for r in rows):
        raise ValueError("complete legacy claim snapshot unavailable")
    for other in rows:
        if other.get("id") == account["id"]:
            continue
        potential = (account_source_scope(other) is None and other.get("platform") == record.get("platform")
                     and str((other.get("credentials") or {}).get("base_url") or "").rstrip("/")
                     == str(record.get("base_url") or "").rstrip("/")
                     and (_is_migrated(other) or re.fullmatch(r"ANT-%s-[0-9]+" % re.escape(
                         host_of(record.get("base_url") or "")), str(other.get("name") or ""))))
        if potential:
            other = _with_source_credentials(api, other)
            if _visible_source_key(other) is None:
                raise ValueError("another legacy candidate cannot be verified")
        if other.get("name") == record["name"] or _unclaimed_record_matches(other, record):
            raise ValueError("legacy claim candidate is ambiguous")
        if _account_source_identity_matches(other, record, allow_platform_change=True):
            raise ValueError("source already has an owned account")


def claim_source_accounts(api, ids, scope, dry_run=False, expected=None, records=None, evidence=None):
    """Explicit ID-only legacy claim. A preview hash is required before applying."""
    scope = _source_scope({"source_scope": scope})
    approved = {item["id"]: item for item in (expected or [])}
    claimed, failures = [], []
    for aid in sorted(set(ids)):
        try:
            if not _is_int(aid) or aid <= 0:
                raise ValueError("invalid account id")
            account = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            if account.get("id") != aid or account_source_scope(account) is not None:
                raise ValueError("not unclaimed managed legacy")
            account = _with_source_credentials(api, account, (evidence or {}).get(aid))
            record = None
            if records is not None:
                candidates = [r for r in records if _unclaimed_record_matches(account, r)]
                if len(candidates) != 1:
                    raise ValueError("unique visible source binding required")
                record = candidates[0]
                _require_unique_claim_candidate(api, account, record)
            notes = str(account.get("notes") or "")
            section = re.search(r"源自 CPA ([^｜]+)｜", notes)
            fingerprint = re.search(r"(?:^|｜)fp=([0-9a-f]{8})(?:｜|$)", notes)
            if record is None:
                key = _visible_source_key(account)
                if (not _is_migrated(account) or section is None or section.group(1) not in SECTION_MAP
                        or fingerprint is None or key is None
                        or account.get("platform") != SECTION_MAP[section.group(1)][0]
                        or account_fingerprint({"api_key": key, "base_url": account_probe_base_url(account)})
                        != fingerprint.group(1)):
                    raise ValueError("legacy provenance unavailable")
            snapshot = hashlib.sha256(_recovery_snapshot(account).encode("utf-8")).hexdigest()
            item = {"id": aid, "name": account.get("name"), "scope": scope, "snapshot_sha256": snapshot}
            if record is not None:
                metadata = _record_source_metadata(dict(record, _source_scope=scope), {"source_scope": scope})
                item["source_identity_sha256"] = metadata["identity_sha256"]
                item["new_name"] = record["name"]
            if "_cpa2sub2api_raw_credentials" in account:
                item["credential_export_sha256"] = _source_digest(account["_cpa2sub2api_raw_credentials"])
            if dry_run:
                claimed.append(item)
                continue
            if approved.get(aid) != item:
                raise ValueError("fresh preview required")
            if record is not None:
                value = metadata
            else:
                value = {"version": 1, "kind": "legacy_claim", "scope": scope, "claimed_account_id": aid,
                         "source_section": section.group(1), "group": SECTION_GROUP.get(section.group(1)),
                         "base_url": account_probe_base_url(account), "key_fingerprint": fingerprint.group(1),
                         "account_name": account.get("name")}
                value["identity_sha256"] = _source_digest(value)
            extra = dict(account.get("extra") or {}, cpa2sub2api_source=value)
            change = {"extra": extra}
            if record is not None:
                change["name"] = record["name"]
                change["notes"] = "%s 源自 CPA %s｜原优先级=%s｜fp=%s" % (
                    NOTE_SENTINEL, record.get("source_section") or record["section"],
                    record.get("priority"), record.get("fp") or account_fingerprint(record))
            def guard():
                fresh = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
                if fresh.get("id") != aid or _recovery_snapshot(fresh) != _recovery_snapshot(account):
                    raise ProbeAuthorityRevoked("legacy claim snapshot changed")
                fresh = _with_source_credentials(api, fresh, (evidence or {}).get(aid))
                if "_cpa2sub2api_raw_credentials" in account:
                    if fresh.get("_cpa2sub2api_raw_credentials") != account["_cpa2sub2api_raw_credentials"]:
                        raise ProbeAuthorityRevoked("legacy full credential evidence changed")
                if record is not None and not _unclaimed_record_matches(fresh, record):
                    raise ProbeAuthorityRevoked("legacy claim source changed")
                if record is not None:
                    _require_unique_claim_candidate(api, fresh, record)
            with _guarded_admin_write(guard):
                api.update_account(aid, change)
            actual = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            if account_source_scope(actual) != scope or actual.get("extra") != extra:
                raise ValueError("claim not persisted")
            expected_account = dict(account, **change)
            if _recovery_snapshot(dict(actual, updated_at=account.get("updated_at"))) != _recovery_snapshot(expected_account):
                raise ValueError("claim changed other state")
            claimed.append(item)
        except Exception:
            failures.append((aid, "旧受管账号认领失败、已归属其它来源或预演后发生变化；未计成功"))
    return claimed, failures


def _load_request_relay(reference):
    import request_relay
    bundle = request_relay.load_bundle(reference["state_dir"])
    if (bundle["generation"] != reference["generation"]
            or bundle["source_scope"] != reference["source_scope"]
            or bool(bundle.get("weighted_bucket", False)) != bool(reference.get("weighted_bucket", False))):
        raise request_relay.RelayError("import_relay_generation_mismatch")
    # Export wakes the optional supervisor asynchronously. Wait only on local
    # authenticated readiness; this barrier performs no model inference.
    deadline = time.monotonic() + 30.0
    announced = False
    while True:
        try:
            request_relay.verify_runtime(reference["base_url"], bundle, timeout=1.0)
            break
        except request_relay.RelayError as ex:
            if str(ex) not in ("relay_readiness_unavailable", "relay_generation_not_ready") or time.monotonic() >= deadline:
                raise
            if not announced:
                print("    等待请求中继加载当前配置版本（仅内部就绪检查，不调用上游）...")
                announced = True
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return bundle


def _request_relay_reference(settings, bundle):
    """Use one config-bound runtime reference for imports and explicit diagnostics."""
    import request_relay
    scope = settings.get("_current_source_scope", _source_scope(settings))
    weighted = _truthy(settings.get("request_relay_weighted_bucket", False))
    if bundle["source_scope"] != scope or bool(bundle.get("weighted_bucket", False)) != weighted:
        raise request_relay.RelayError("relay_runtime_source_mismatch")
    return {"enabled": True, "source_scope": scope, "generation": bundle["generation"],
            "state_dir": settings.get("request_relay_state_dir") or DEFAULTS["request_relay_state_dir"],
            "base_url": settings.get("request_relay_base_url") or DEFAULTS["request_relay_base_url"],
            "weighted_bucket": weighted,
            "force_all_accounts": _truthy(settings.get("request_relay_force_all_accounts", False))}


def _relay_account_diagnostic_context(account, cfg):
    """Validate the actual persisted account and the current guarded runtime."""
    import request_relay
    reference = cfg.get("request_relay") or {}
    if not _truthy(reference.get("enabled")) and _truthy(cfg.get("request_relay_enabled", False)):
        current = request_relay.load_bundle(cfg.get("request_relay_state_dir") or DEFAULTS["request_relay_state_dir"])
        reference = _request_relay_reference(cfg, current)
    if not _truthy(reference.get("enabled")):
        raise request_relay.RelayError("relay_diagnostic_requires_enabled_relay_and_sync")
    original = request_relay.account_original_source(account)
    if not original or original["scope"] != _source_scope(cfg):
        raise request_relay.RelayError("relay_diagnostic_source_mismatch")
    bundle = _load_request_relay(reference)
    route = bundle["routes"].get(original["route_id"])
    if (not route or any(route["original_source"].get(k) != original.get(k) for k in (
            "scope", "source_section", "group", "base_url", "key_sha256"))
            or str((account.get("credentials") or {}).get("base_url") or "").rstrip("/")
            != reference["base_url"].rstrip("/") + "/r/" + route["id"]
            or (account.get("credentials") or {}).get("api_key") != route.get("token")):
        raise request_relay.RelayError("relay_diagnostic_account_generation_mismatch")
    return reference, bundle, route, original


def _native_scheduled_diagnostic_model(account, model, cfg, automatic=True):
    from relay.config import diagnostic_model_marker
    reference, bundle, route, original = _relay_account_diagnostic_context(account, cfg)
    if model not in route.get("models", []):
        model = (account.get("credentials") or {}).get("model_mapping", {}).get(model)
    if automatic and model in route.get("models", []):
        # A duplicate-alias business pool cannot choose a smaller model for an
        # automatic diagnostic. Select a declared highest actual provider name.
        matching = {entry["name"] for provider in bundle["consumer_config"].get(route["source_section"], [])
            if provider.get("prefix") == route["diagnostic_prefix"] for entry in provider.get("models", [])
            if entry.get("alias") == model and entry.get("name")}
        if len(matching) > 1:
            model = _pick_probe_model({name: name for name in sorted(matching)},
                account.get("platform"), route["source_section"])
    marker = diagnostic_model_marker(route, model, automatic=automatic)
    if (account.get("credentials") or {}).get("model_mapping", {}).get(marker) != marker:
        raise ValueError("native_diagnostic_model_self_mapping_missing_sync_source_first")
    return marker


def _test_relay_account_exact(account, model, cfg):
    """A pool request never proves a key; diagnostics bind to its exact route."""
    reference, bundle, route, original = _relay_account_diagnostic_context(account, cfg)
    if model not in route.get("models", []):
        model = (account.get("credentials") or {}).get("model_mapping", {}).get(model)
    if model not in route.get("models", []):
        raise ValueError("relay_diagnostic_model_not_in_current_route")
    policy = dict(cfg, _request_relay_bundle=bundle)
    return model_probe._probe_once(
        _http, account.get("platform"), original["base_url"], "", model, {}, None,
        settings=policy, source_section=original["source_section"],
        request_base_url=reference["base_url"].rstrip("/") + "/r/" + route["id"],
        request_api_key=route["token"])[0]


def _prepare_request_relay(cfg, records):
    """Validate the immutable runtime before any admin mutation, including pruning."""
    reference = cfg.get("request_relay") or {}
    if cfg.get("_live_probe_policy"):
        live = load_settings()
        if not _truthy(live.get("request_relay_enabled", False)):
            cfg["request_relay"] = {"enabled": False}
            cfg.pop("_request_relay_bundle", None)
            return
        if "request_relay" not in cfg:
            import request_relay
            raise request_relay.RelayError("regenerate_plan_for_enabled_request_relay")
        if (not _truthy(reference.get("enabled")) and not reference.get("desired_enabled")
                or _truthy(live.get("request_relay_weighted_bucket", False))
                != bool(reference.get("weighted_bucket", False))
                or _truthy(live.get("request_relay_force_all_accounts", False))
                != bool(reference.get("force_all_accounts", False))):
            import request_relay
            raise request_relay.RelayError("regenerate_plan_for_changed_request_relay_mode")
        if _truthy(reference.get("enabled")):
            import request_relay
            if live.get("request_relay_source_scope", "default") != reference.get("source_scope"):
                raise request_relay.RelayError("import_relay_source_scope_mismatch")
            reference = dict(reference, state_dir=live.get("request_relay_state_dir", reference["state_dir"]),
                             base_url=live.get("request_relay_base_url", reference["base_url"]))
            cfg["request_relay"] = reference
    if not _truthy(reference.get("enabled")):
        return
    import request_relay
    bundle = _load_request_relay(reference)
    for record in records:
        if (record.get("_request_relay_required")
                and request_relay.route_id(record, reference["source_scope"]) not in bundle["routes"]):
            raise request_relay.RelayError("required_route_missing_from_generation")
    cfg["_request_relay_bundle"] = bundle


def _export_request_relay(records, source_config, settings):
    if not _truthy(settings.get("request_relay_enabled", False)):
        return {"enabled": False, "desired_enabled": False}
    import request_relay
    weighted = _truthy(settings.get("request_relay_weighted_bucket", False))
    force_all = _truthy(settings.get("request_relay_force_all_accounts", False))
    selected = [r for r in records if force_all or weighted or r.get("_relay_tls") is not None
                or request_relay.requires_relay(r, source_config)]
    if not selected:
        return {"enabled": False, "desired_enabled": True, "weighted_bucket": weighted,
                "force_all_accounts": force_all}
    for record in selected:
        record["_request_relay_required"] = True
        mapping = record_model_mapping(record)
        mapping = request_relay.relay_model_mapping(record, mapping)
        record["_relay_models"] = sorted(set(mapping.values()))
        if (_truthy(settings.get("model_probe_enabled", False))
                and probe_policy.probe_allowed(record.get("base_url"), settings)):
            models = record.get("models_raw") or []
            families = model_selection.probe_family_models(models, record.get("platform"),
                           record.get("source_section") or record.get("section"))
            for family, candidates in families.items():
                generations = {model_selection.model_family_version(m.get("name", ""))[1] for m in candidates}
                for generation in sorted(generations, reverse=True)[:2]:
                    candidate = model_probe.representative_model(candidates, generation, family, family=family)
                    if candidate:
                        record["_relay_models"] = sorted(set(record["_relay_models"]) | {candidate})
    scope = settings.get("request_relay_source_scope") or "default"
    state_dir = settings.get("request_relay_state_dir") or DEFAULTS["request_relay_state_dir"]
    if settings.get("_request_relay_preview"):
        request_relay.compile_bundle(selected, source_config, scope, weighted_bucket=weighted)
        return {"enabled": True, "source_scope": scope, "generation": "preview-not-exported",
                "state_dir": state_dir, "base_url": settings.get("request_relay_base_url")
                or DEFAULTS["request_relay_base_url"], "weighted_bucket": weighted,
                "force_all_accounts": force_all}
    import automatic_probe_budget
    def validate_budget_storage():
        live = resolve_probe_policy(settings)
        if (_source_scope(live) != _source_scope(settings)
                or live.get("request_relay_state_dir", DEFAULTS["request_relay_state_dir"]) != state_dir):
            raise ProbeAuthorityRevoked("automatic budget storage authority changed")
    try:
        automatic_probe_budget.initialize_budget(state_dir, validate_budget_storage, history_dir=OUT_DIR)
    except automatic_probe_budget.AutomaticProbeSkipped as skipped:
        print("    自动周检测预算尚不可用：%s；未授权任何模型请求。" % skipped.code)
    bundle = request_relay.export_bundle(selected, source_config, scope, state_dir, weighted_bucket=weighted)
    return _request_relay_reference(settings, bundle)


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
      这正是代理参数存在的理由。把任何实际部署域名写进白名单，
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
    changed = {}
    cur = s["sub2api_base_url"]
    v = input(" sub2api 地址 [%s]：" % cur).strip()
    if v:
        s["sub2api_base_url"] = v.rstrip("/")
        changed["sub2api_base_url"] = s["sub2api_base_url"]
    print(" 当前管理密钥：%s" % mask(s["sub2api_admin_key"]))
    print(" 提示：这是 admin- 开头的那个，在 VPS 上用这条取：")
    print('   docker exec -i sub2api-postgres psql -U sub2api -d sub2api \\')
    print("     -tAc \"SELECT value FROM settings WHERE key='admin_api_key';\"")
    v = input(" 粘贴 sub2api 管理密钥（回车跳过）：").strip()
    if v:
        s["sub2api_admin_key"] = v
        changed["sub2api_admin_key"] = v
    update_saved_settings(lambda saved: dict(saved, **changed))
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


def _probe_record_provenance(record):
    """Opaque exact-key/catalog identity; never persist raw probe credentials."""
    value = {key: record.get(key) for key in ("base_url", "api_key", "headers", "platform", "models_raw",
                                             "excluded", "source_section", "section", "_source_scope")}
    return _source_digest(value)


def run_model_probe(recs, s, force=False):
    """需求第 2 条⑵④：探测各上游的最新代/次最新代模型能否真的调用。

    返回 ({(段, 域名): {"latest":bool,"prev":bool,"detail":str}}, 告警列表)。
    探测关闭或不可用时返回 ({}, [])，调用方据此走原有行为。

    ## 为什么按 (段, 域名) 而不是按 KEY

    同一个站的 N 个 KEY 卖的是同一批模型。真实配置里 231 个 KEY
    去重后只剩 54 个 (段, 域名) 单元 —— 探测量直接降到 1/4。
    每个单元最多 2 个请求（最新代 + 次最新代），且最新代通了就不发第二个，
    8 并发下整体 40 秒内跑完，命中缓存时 0 请求。

    ## 为什么挑"名字最短"的模型当代表

    变体后缀（-preview / -thinking / -customtools）常有额外的请求体要求，
    拿它去探容易探出假阴性 —— 明明这一代能用，却因为我们的极简探测体
    不合它口味而被判死。裸名最稳。
    """
    if s.get("_request_relay_preview") or s.get("_probe_execution_disabled"):
        return {}, []
    s = resolve_probe_policy(s)
    if not MODEL_PROBE_AVAILABLE:
        return {}, []
    if not _truthy(s.get("model_probe_enabled", False)):
        return {}, []

    # ---- 按 (段, 域名) 归集，每个单元挑一个 KEY 当代表 ----
    units_by_key = {}
    for r in recs:
        if r.get("disabled"):
            continue
        base = r.get("base_url") or ""
        if not probe_policy.probe_allowed(base, s):
            continue
        api_key = r.get("api_key") or ""
        if not base or not api_key:
            continue
        sec = r.get("source_section") or r.get("section")
        key = (sec, host_of(base))
        if key in units_by_key:
            continue
        units_by_key[key] = r

    if not units_by_key:
        return {}, []

    kind_of = {"gemini": "gemini", "anthropic": "anthropic"}
    units = []
    for (sec, host), r in units_by_key.items():
        models = r.get("models_raw") or []
        if not models:
            continue
        platform = r.get("platform") or "openai"
        families = model_selection.probe_family_models(models, platform, sec)
        for family, candidates in families.items():
            ordered = sorted({model_selection.model_family_version(m.get("name", ""))[1]
                              for m in candidates}, reverse=True)
            latest, prev = ordered[0], ordered[1] if len(ordered) > 1 else None
            units.append({
            "section": sec, "host": host, "platform": platform,
            "family": family, "provenance": _probe_record_provenance(r),
            "base_url": r.get("base_url"), "api_key": r.get("api_key"),
            "headers": r.get("headers") or {},
            "latest_gen": latest, "prev_gen": prev,
            "latest_model": model_probe.representative_model(candidates, latest, family, family=family),
            "prev_model": (model_probe.representative_model(candidates, prev, family, family=family)
                           if prev is not None else None),
            })

    if not units:
        return {}, []

    reference = s.get("request_relay") or {}
    if _truthy(reference.get("enabled")):
        import request_relay
        bundle = _load_request_relay(reference)
        s = dict(s, _request_relay_bundle=bundle)
        for unit in units:
            record = units_by_key[(unit["section"], unit["host"])]
            rid = request_relay.route_id(record, reference["source_scope"])
            route = bundle["routes"].get(rid)
            if route:
                unit.update(base_url=route["original_source"]["base_url"],
                            request_base_url=reference["base_url"].rstrip("/") + "/r/" + rid,
                            request_api_key=route["token"],
                            transport_generation="relay-exact-v1:" + bundle["generation"] + ":" + rid)

    try:
        res = model_probe.probe_all(
            units, _http, out_dir=OUT_DIR,
            workers=s.get("model_probe_workers", 8),
            fallback_proxy=_fallback_proxy(s),
            ttl=int(s.get("model_probe_cache_ttl")
                    or model_probe.DEFAULT_CACHE_TTL),
            log=print, settings=s, automatic=True, force=force)
    except Exception as ex:
        return {}, ["模型连通性探测失败（不影响导入，按原有就高逻辑处理）：%s"
                    % str(ex)[:120]]

    warn, grouped = [], {}
    units_by_family = {(unit["section"], unit["host"], unit["family"]): unit for unit in units}
    for key, v in sorted(res.items()):
        sec, host, family = key
        unit = units_by_family[key]
        entry = grouped.setdefault((sec, host), {"provenance": unit["provenance"], "families": {}})
        entry["families"][family] = dict(v, latest_gen=unit["latest_gen"], prev_gen=unit["prev_gen"])
        if not v.get("latest") and v.get("prev"):
            warn.append("%s / %s：最新代探测不通、次最新代连通，已同时保留两代"
                        "（需求 2⑵④）。%s" % (sec, host, v.get("detail", "")[:90]))
        elif not v.get("latest") and not v.get("prev"):
            warn.append("%s / %s：最新代与次最新代均探测不通，按目录最高级填充。%s"
                        % (sec, host, v.get("detail", "")[:90]))
    return grouped, warn


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
    unsupported_fields, capability_drift = cpa_capability_issues(cfg_dict, s)
    source_contract = _upstream_source_contract(s)
    capability_drift.extend("%s 源码合同未核对；禁止写入和自动删除" % kind
        for kind, evidence in source_contract["evidence"].items() if evidence["status"] == "review_required")
    dropped.extend("未适配的受管配置字段：%s；未执行目标写入或自动删除" % field for field in unsupported_fields)
    dropped.extend(capability_drift)
    source_scope = _source_scope(s)
    profiles = s.get("request_relay_tls_profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("request_relay_tls_profiles must map explicit hosts to TLS presets")
    for record in recs:
        record["_source_scope"] = source_scope
        host = probe_policy.normalize_host(record.get("base_url"))
        if record.get("request_relay_tls") is not None:
            record["_relay_tls"] = copy.deepcopy(record["request_relay_tls"])
        if host in profiles:
            record["_relay_tls"] = copy.deepcopy(profiles[host])
    _apply_learned_source_tls(recs, cfg_dict, s)
    incomplete_config = bool(unsupported_fields or capability_drift) or any(
        section in cfg_dict and (not isinstance(cfg_dict[section], list)
                                or any(not isinstance(e, dict) for e in cfg_dict[section]))
        for section in SECTION_MAP)
    # Falsy malformed values must not masquerade as an intentionally empty
    # credential list and thereby authorize deletion of the previous provider.
    for section in SECTION_MAP:
        entries = cfg_dict.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or "api-key-entries" not in entry:
                continue
            nested = entry["api-key-entries"]
            if (not isinstance(nested, list)
                    or any(not isinstance(item, dict)
                           or not isinstance(item.get("api-key"), str)
                           or not item["api-key"].strip() for item in nested)):
                incomplete_config = True
    incomplete_config = incomplete_config or any(
        "为空" in str(message) or "无有效" in str(message)
        or "api-key" in str(message) and "重复" not in str(message)
        for message in dropped)
    if not recs:
        raise RuntimeError("没解析到任何账号，检查是否有 *-api-key 段")

    # routing.strategy 决定 weight<=0 是不是真的等于停用：
    # CPA 只在 weighted-round-robin 下排除零权重凭据。
    # 必须在 apply_weight_zero_priority 之前解析出来——降级要按策略决定做不做。
    routing = cfg_dict.get("routing") or {}
    strategy = str(routing.get("strategy") or "round-robin").strip().lower()

    # 启用智能优先级：优先从 sub2api 实际运行状态计算健康分数
    use_smart_priority = (s.get("health_rerank_enabled", True)
                          and not all(r.get("_source_fidelity") for r in recs))
    if use_smart_priority and SMART_PRIORITY_AVAILABLE and s.get("sub2api_admin_key"):
        try:
            api = Sub2Api(s)
            print("[智能优先级] 启用基于健康度的动态优先级分配...")
            # 账号名 -> (渠道, 域名) 对照表。智能优先级内部按 (渠道, 域名)
            # 统计健康分，而 sub2api 账号列表里的 group 字段是名称还是 id
            # 不确定，所以用账号名反查（账号名由 assign_names 生成，一一对应）。
            name_to_hostkey = {}
            assign_names(recs)
            for _r in recs:
                if not _r.get("name"):
                    # assign_names 在本函数后半段才跑；智能优先级需要账号名
                    # 反查域名单元，所以这里先按同一套规则物化一次。
                    _stem = "%s-%s" % (_r["prefix"] or _r["platform"][:3].upper(),
                                       host_of(_r["base_url"]))
                    _r["fp"] = account_fingerprint(_r)
                    _r["name"] = "%s-%s" % (_stem, _r["fp"])
                name_to_hostkey[_r["name"]] = _host_key(_r)
            bucket_of = remap_priority_smart(
                recs, api, host_of, account_fingerprint,
                _is_int, _host_tier_map, _host_key,
                name_to_hostkey=name_to_hostkey
            )
            # 应用映射到每条记录。
            # bucket_of 的键是**域名单元** (_host_key)，不是 CPA 的 priority 数值。
            # 以前这里用 `bucket_of.get(tier, 500)` 拿 _host_tier_map 的序位值
            # 去查表，等于把"按序位值建表"的旧约定又套了一遍，跨渠道碰撞被原样带进来；
            # 现在直接按域名单元取桶号。
            for r in recs:
                r["new_priority"] = bucket_of.get(_host_key(r), 500)
            # rank 必须一起补上：对照表按它排序（write_plan_files 里
            # `sorted(arr, key=lambda x: x["rank"])`）。这条智能路径不走
            # remap_priority，而 rank 只在那边被赋值，漏了就 KeyError 'rank'，
            # 而且是在**写对照表**的时候才炸 —— 前面的优先级计算全部白做。
            # 语义：同一渠道内按新桶号（小者优先）排名，与对照表的呈现一致。
            _by_group = defaultdict(list)
            for r in recs:
                _by_group[r["group"]].append(r)
            for _arr in _by_group.values():
                _arr.sort(key=lambda r: (r.get("new_priority") or 0,
                                         host_of(r["base_url"]),
                                         account_fingerprint(r)))
                for _rank, r in enumerate(_arr, 1):
                    r["rank"] = _rank
            print("[智能优先级] 应用完成")
        except Exception as e:
            print(f"[智能优先级] 失败，回退到传统映射: {e}")
            remap_priority(recs)
    else:
        remap_priority(recs)
    # 零权重降级必须跟在 remap_priority 之后：它以后者算出的 new_priority
    # 为基准往后平移，顺序颠倒会拿到空值。
    plan_cfg = {
        "source_scope": source_scope,
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
        "auto_recover_cron": probe_policy.weekly_cron(s.get("auto_recover_cron")),
        "model_probe_enabled": s.get("model_probe_enabled", False),
        "model_probe_hard_disabled": s.get("model_probe_hard_disabled", False),
        "online_probe_hosts": dict(probe_policy.site_switches(s)),
        "detected_tls_fingerprint_hosts": s.get("detected_tls_fingerprint_hosts", []),
        "prune_stale_accounts": s.get("prune_stale_accounts", True),
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
    plan_cfg["request_relay"] = _export_request_relay(recs, cfg_dict, s)

    # 需求第 2 条⑵④：探测每个 (段, 域名) 的最新代/次最新代是否真的能调用。
    # 结果写进每条记录的 _probe 字段，供 build_model_mapping 消费。
    probe_settings = dict(s, request_relay=plan_cfg["request_relay"],
        _automatic_probe_document_sha256=_source_digest(cfg_dict))
    if unsupported_fields or capability_drift:
        probe_settings["_probe_execution_disabled"] = True
    probe_map, probe_warn = run_model_probe(recs, probe_settings)
    if probe_map:
        for r in recs:
            key = (r.get("source_section") or r.get("section"),
                   host_of(r.get("base_url") or ""))
            pr = probe_map.get(key)
            if pr and pr.get("provenance") == _probe_record_provenance(r):
                r["_probe"] = {"families": copy.deepcopy(pr["families"])}
        plan_cfg["request_relay"] = _export_request_relay(recs, cfg_dict, s)

    plan = {
        "groups": groups,
        "proxies": proxies,
        "fingerprints": fps,
        "accounts_raw": recs,
        "config_incomplete": incomplete_config,
        "unsupported_config_fields": unsupported_fields,
        "capability_snapshot": dict(CPA_CAPABILITY_SNAPSHOT, source_evidence=
            upstream_contract.source_evidence(s.get("_runtime_cpa_src") or s.get("cpa_src") or CPA_SRC, "cpa")),
        "capability_drift": capability_drift,
        "source_contract": source_contract,
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
    return plan, text, src, dropped + proxy_warn + probe_warn


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
                mm = record_model_mapping(r)
                all_m = record_model_mapping(r, excluded=False)
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

        relay = cfg.get("request_relay") or {}
        if _truthy(relay.get("enabled")):
            f.write("\n## 原 CPA 请求管线\n\n")
            f.write("已为需要原请求管线的账号生成独立请求中继配置。以下行为由原 CPA consumer 消费；"
                    "导入前必须核实中继运行版本与计划一致。原始账号身份、优先级和模型白名单保持不变。\n\n")
            f.write("中继版本 `%s`；路由模式 `%s`。\n\n" % (
                relay["generation"], "weighted_bucket" if relay.get("weighted_bucket") else "exact_key"))
            if relay.get("weighted_bucket"):
                f.write("加权共享桶的业务结果属于逻辑池，不证明某条原始 KEY 可用；"
                        "不会据此生成逐 KEY 恢复证据。\n\n")
        else:
            f.write("\n## 无法直接迁移的能力\n\n")
            f.write("以下 CPA 行为活在 CPA 的请求管线里，不是凭据上的字段；"
                    "当前未启用请求中继，sub2api 没有等价物，已记入每条账号的 notes 存档：\n\n")
        f.write("- `fingerprint-profile`：uTLS 伪造 ClientHello + HTTP/1.1 头序重写，"
                "属传输层行为。sub2api 的 TLS 指纹仅对 Anthropic 的 OAuth/setup-token 账号生效，"
                "我们导入的全是 apikey 账号，用不上。\n")
        f.write("- `cloak`：改写 system 提示、注入计费头、混淆敏感词、工具名别名化，"
                "全是按请求内容做的报文改写。\n")
        f.write("- `$` 开头的请求头：值来自下游客户端的同名头，逐请求动态解析，"
                "不能按静态字面量保存到 sub2api。\n")
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
    nm = sum(1 for r in recs if record_model_mapping(r))
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
                except ApiError as ex2:
                    existing = {}
                    # 重查失败 = 拿不到已有分组的 ID = 这批账号没有 group_id。
                    # 以前静默，表现为"日志说重名冲突，然后账号不知去哪了"。
                    log_exc("建分组撞重名后重查分组列表也失败（HTTP %s %s），"
                            "无法复用已有分组" % (ex2.status, ex2.body[:80]), ex2,
                            level="warning")
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
                except ApiError as ex2:
                    # 建代理撞重名后重查失败 → 这批账号拿不到 proxy_id → 会
                    # **静默直连**（而不是走代理）。这正好是需求第 4⑷ 抱怨的
                    # "前端 mihomo 链接失败"的一种成因，必须留痕。
                    log_exc("建代理撞重名后重查代理列表也失败（HTTP %s %s），"
                            "相关账号将直连而非走代理" % (ex2.status, ex2.body[:80]),
                            ex2, level="warning")
                if purl in pmap:
                    print("    [复用] %s (id=%s)" % (purl, pmap[purl]))
                    continue
            print("    [失败] %s：HTTP %s，相关账号将直连" % (purl, ex.status))
        except Exception as ex:
            print("    [失败] %s：%s，相关账号将直连" % (purl, ex))
    return pmap


def current_probe_policy(config):
    """A generated plan cannot persist or restore revoked network authority."""
    effective = dict(config)
    live = load_settings()
    for key in ("model_probe_enabled", "model_probe_hard_disabled", "online_probe_hosts", "auto_recover_enabled",
                "auto_recover_cron", "detected_tls_fingerprint_hosts"):
        effective[key] = live.get(key, DEFAULTS.get(key))
    if ("request_relay" in config or _truthy(config.get("request_relay_enabled", False))
            or _truthy(live.get("request_relay_enabled", False))):
        for key in ("request_relay_enabled", "request_relay_source_scope", "request_relay_state_dir",
                    "request_relay_base_url", "request_relay_weighted_bucket", "request_relay_force_all_accounts"):
            effective[key] = live.get(key, DEFAULTS.get(key))
    effective["_live_probe_policy"] = True
    effective["_current_source_scope"] = _source_scope(live)
    return effective


def resolve_probe_policy(config):
    """Production calls recheck persisted consent; test/in-process policies stay explicit."""
    return current_probe_policy(config) if config.get("_live_probe_policy") else config


def _source_create_payload(desired):
    """Shield native active-by-default creation until stopped state is verified."""
    payload = copy.deepcopy(desired)
    if desired.get("status") == "inactive" and desired.get("schedulable") is False:
        payload["credentials"]["model_mapping"] = {BLOCK_ALL_SENTINEL: BLOCK_ALL_SENTINEL}
        payload.setdefault("extra", {}).update(openai_passthrough=False, anthropic_passthrough=False)
    payload.pop("status", None)
    payload.pop("schedulable", None)
    return payload


def _finalize_source_stopped_account(api, aid, desired, record=None, cfg=None):
    """Remove the transient fake model only in the same write that stops routing."""
    before = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
    if (before.get("id") != aid or before.get("name") != desired.get("name")
            or not _is_migrated(before)
            or (before.get("extra") or {}).get("cpa2sub2api_source")
            != (desired.get("extra") or {}).get("cpa2sub2api_source")):
        raise ValueError("created stopped source identity mismatch")
    patch = {"status": "inactive", "schedulable": False,
             "credentials": copy.deepcopy(desired["credentials"]),
             "extra": copy.deepcopy(desired.get("extra") or {})}
    before = _with_source_credentials(api, before)
    guard = _account_probe_write_guard(api, before, cfg, require_consent=False, record=record)
    with _guarded_admin_write(guard):
        api.bulk_update_accounts([aid], patch)
    actual = _with_source_credentials(api, _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid)))
    if (actual.get("id") != aid or actual.get("status") != "inactive"
            or actual.get("schedulable") is not False
            or actual.get("name") != desired.get("name") or not _is_migrated(actual)
            or (actual.get("extra") or {}).get("cpa2sub2api_source")
            != (desired.get("extra") or {}).get("cpa2sub2api_source")
            or _visible_source_key(actual) != desired["credentials"].get("api_key")
            or diff_account_fields(actual, desired)):
        raise ValueError("stopped source account readback mismatch")


@_with_import_calibration_cleanup
def do_import(api, plan, ask=None):
    """唯一的导入实现。菜单和一键导入共用，避免两份逻辑各自漂移。

    ask: 可选的确认回调 ask(提示语) -> bool。传 None 表示不确认直接导。

    返回统计 dict。
    """
    if plan.get("unsupported_config_fields"):
        raise ValueError("unadapted managed configuration fields; refresh and review capability support before importing")
    if plan.get("capability_drift"):
        raise ValueError("upstream contract drift; review capability support before importing or deleting")
    snapshot = plan.get("capability_snapshot")
    if snapshot and any(snapshot.get(key) != value for key, value in CPA_CAPABILITY_SNAPSHOT.items()):
        raise ValueError("stale capability contract snapshot; regenerate and review the import plan")
    _, current_drift = cpa_capability_issues({}, load_settings())
    if current_drift:
        raise ValueError("current upstream contract drift; regenerate and review the import plan")
    _validate_upstream_source_contract(plan, load_settings())
    groups = plan["groups"]
    proxies = plan["proxies"]
    recs = plan["accounts_raw"]
    all_recs = recs
    selected_hosts = None
    if "selected_hosts" in plan:
        selected_hosts = set(plan["selected_hosts"])
        available = {probe_policy.normalize_host(r.get("base_url")) for r in recs}
        if (not selected_hosts or any(not isinstance(h, str) or h != probe_policy.normalize_host(h)
                                      or h not in available for h in selected_hosts)):
            raise ValueError("partial import requires exact configured source hosts")
        recs = [r for r in recs if probe_policy.normalize_host(r.get("base_url")) in selected_hosts]
        groups = {k: v for k, v in groups.items() if k in {r["group"] for r in recs}}
        proxies, _ = distinct_proxies(recs)
    source_recs = recs
    cfg = current_probe_policy(plan["config"])
    live_scope = _source_scope(load_settings())
    if _source_scope(cfg) != live_scope:
        raise ValueError("source scope changed; regenerate the import plan")
    cfg["source_scope"] = live_scope
    for record in recs:
        if record.get("_source_scope", "default") != live_scope:
            raise ValueError("record source scope does not match current source")
        record["_source_scope"] = live_scope

    print("\n 将导入：分组 %d 个、代理 %d 个、账号 %d 条"
          % (len(groups), len(proxies), len(recs)))
    if ask and not ask("\n 确认写入 %s ？" % api.base):
        print(" 已取消。")
        return {"cancelled": True}
    _validate_upstream_source_contract(plan, load_settings())

    _prepare_request_relay(cfg, recs)

    # --- 1. 分组 ---
    print("\n [1/6] 创建分组...")
    gmap, gfailed = ensure_groups(api, groups)

    # 分组降级链：同平台内按优先级串起来，整组不可用时自动转给下一组。
    # 放在建组之后、导账号之前——这一步只改分组自身的字段，与账号无关。
    fb_chain = {} if selected_hosts is not None else plan_fallback_chain(recs, groups)
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
        print("    ! 读取已有账号详情失败，无法确认旧来源身份；本次中止")
        print("      原因：%s" % hm_err)
        return {"aborted": "读不到完整已有账号身份，拒绝创建或迁移"}
    source_matches, identity_fail = resolve_source_accounts(list(have_map.values()), recs)
    unresolved_names = set(source_matches)
    source_matches = {name: _with_source_credentials(api, account) for name, account in source_matches.items()}
    # A public DTO cannot reveal secret drift; resolve again after the exact
    # exports so a wrong visible legacy key never becomes a weak migration.
    source_matches, revealed_fail = resolve_source_accounts(list(source_matches.values()), recs)
    identity_fail.extend(revealed_fail)
    identity_fail.extend((name, "旧来源完整凭据不匹配，未创建或迁移") for name in unresolved_names - set(source_matches))
    blocked_names = {name for name, _ in identity_fail}
    recs = [r for r in recs if r["name"] not in blocked_names]
    have_map.update(source_matches)
    have.update(source_matches)
    rerank_n, rerank_msg = health_rerank_priority(all_recs, have_map, cfg)
    if rerank_n:
        print("    ✓ %s" % rerank_msg)
    else:
        print("    · %s" % rerank_msg)

    dup = [r for r in recs if r["name"] in have]
    recs = [r for r in recs if r["name"] not in have]

    # 已存在的账号：默认不再简单跳过，而是把 CPA 里改过的字段同步过来。
    # 老行为（纯跳过）可用 设置.json 的 sync_existing_accounts=false 恢复。
    sync_ok = sync_same = 0
    sync_fail = list(identity_fail)
    if dup and cfg.get("sync_existing_accounts", True):
        print("    已存在 %d 条，检查是否需要同步..." % len(dup))
        # have_map 前面已经拿过了，直接复用
        if hm_err:
            # 取不全就不要瞎更新：半份数据会把没读到的那些误判成无需同步。
            print("      ! 读取已有账号详情失败，本次跳过同步：%s" % hm_err)
            for r in dup:
                skipped.append((r["name"], "已存在，同步失败（%s）" % hm_err))
        else:
            sync_ok, sync_same, update_fail = sync_existing_accounts(
                api, dup, cfg, gmap, pmap, have_map)
            sync_fail.extend(update_fail)
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
    # 注意：recs 到这里已经是"仅新账号"（上面已把已存在的过滤进 dup）。
    # 所以全量对照表必须用 dup + recs 一起建 —— 只按 recs 建的话，
    # 已存在账号在表里查不到，下面补挂探活计划时会一条都匹配不上。
    name_to_rec = {r["name"]: r for r in list(dup) + list(recs)}
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
            resp = api.batch_accounts([_source_create_payload(p) for p in payloads])
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
                    desired = next((p for p in payloads if p["name"] == nm), None)
                    if desired is not None and desired.get("status") == "inactive":
                        try:
                            aid = res.get("id")
                            if not _is_int(aid) or aid <= 0:
                                raise ValueError("missing created stopped source id")
                            record = next(r for r in batch_recs if r["name"] == nm)
                            _finalize_source_stopped_account(api, aid, desired, record, cfg)
                        except Exception:
                            batch_failed.append((nm, "源配置停用状态或模型清理未通过读回，未计成功"))
                            continue
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
                if n in name_to_rec and needs_inactive(name_to_rec[n], cfg)
                and not name_to_rec[n].get("_source_fidelity")]
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

    # --- 5. Reconcile plans, including revoked/disabled sites ---
    print("\n [5/6] 校准每周探活计划及关闭站点...")
    admin_calibration = _start_admin_calibration(api, cfg, "计划快照")
    recovery_evidence = {}
    target_rows, target_error = _fetch_all_accounts(api)
    if selected_hosts is not None and not target_error:
        try:
            target_rows = [row for row in target_rows
                           if probe_policy.normalize_host(account_probe_base_url(row)) in selected_hosts]
        except (ValueError, RuntimeError):
            target_error = "账号来源归属损坏，未执行单站计划校准"
    targets = managed_plan_targets(target_rows)
    section_by_name = {r["name"]: r.get("source_section") or r.get("section")
                       for r in plan["accounts_raw"]}
    if target_error:
        plan_ok, plan_fail = 0, [("snapshot", "读取不完整，探活计划未校准")]
    else:
        plan_ok, plan_fail = ensure_test_plans(api, targets, cfg,
                                              section_by_name=section_by_name,
                                              account_snapshot=target_rows)
        if not admin_calibration.stopped and _truthy(cfg.get("auto_recover_enabled", True)):
            recovery_evidence, recovery_failures = probe_import_recovery(
                api, targets, cfg, section_by_name=section_by_name)
            print("    本轮授权恢复测试成功 %d 条，跳过或失败 %d 条"
                  % (len(recovery_evidence), len(recovery_failures)))
    print("    已校准 %d 条，失败 %d 条（周频；默认站级关闭）"
          % (plan_ok, len(plan_fail)))

    # --- 6. 回收被网关关掉的调度开关 ---
    # 补 sub2api 的恢复缺口：ClearError 只还 status 不还 schedulable，
    # 账号会永久卡在 active + 不可调度。详见 reclaim_schedulable。
    print("\n [6/6] 回收被关闭的调度开关...")
    _start_admin_calibration(api, cfg, "恢复校准")
    rec_done, rec_total, rec_err = reclaim_schedulable(api, evidence=recovery_evidence)
    if rec_err:
        print("    ! 回收失败：%s" % rec_err)
    elif rec_total:
        print("    发现 %d 条状态正常但调度关闭，已恢复 %d 条" % (rec_total, rec_done))
    else:
        print("    没有需要回收的账号")

    # 停用账号里「探活已证明可用」的那些一并启用（Problem 6.3）。
    # sub2api 的自动恢复只认 error、不认 inactive，这一步是唯一的出路。
    # 默认设置开启；设置 revive_proven_inactive=false 可保留停用状态。
    rv_done, rv_total, rv_err = revive_proven_inactive(api, cfg, evidence=recovery_evidence)
    if rv_err:
        print("    ! 停用账号复活检查失败：%s" % rv_err)
    elif rv_total:
        print("    停用账号中探活已证明可用 %d 条，已重新启用 %d 条"
              % (rv_total, rv_done))
    elif cfg.get("revive_proven_inactive", False):
        print("    停用账号中没有探活证明可用的，保持停用")

    # Only a fully successful import authorizes removal from this managed set.
    unclaimed = []
    if not target_error:
        for row in target_rows:
            try:
                if _is_migrated(row) and account_source_scope(row) is None:
                    unclaimed.append({"id": row.get("id"), "name": row.get("name")})
            except (ValueError, RuntimeError):
                pass  # The ownership error below blocks pruning separately.
        if unclaimed:
            print("\n    保留 %d 条未认领来源的旧受管账号。核对 id 后可执行 prune claim-preview / claim-apply。"
                  % len(unclaimed))
            for row in unclaimed[:10]:
                print("      id=%s %s" % (row["id"], row["name"]))
    prune_options = {"hosts": selected_hosts} if selected_hosts is not None else {}
    _start_admin_calibration(api, cfg, "旧账号校准")
    stale, stale_err = find_stale_accounts(api, source_recs, **prune_options)
    pruned, prune_fail = [], []
    prune_safe = (not failed and not sync_fail and not off_fail and not gfailed and not plan_fail
                  and not hm_err and not stale_err and cfg.get("sync_existing_accounts", True)
                  and not plan.get("config_incomplete", False))
    if cfg.get("prune_stale_accounts", True):
        if prune_safe:
            pruned, prune_fail = prune_stale_accounts(api, source_recs, **prune_options)
            print("\n    已按当前配置清理旧受管账号 %d 条，失败 %d 条"
                  % (len(pruned), len(prune_fail)))
        else:
            prune_fail = [("safety", "本轮配置或同步不完整，保留全部旧账号")]
            print("\n    配置/导入/同步存在失败，已阻止旧账号删除。")

    # --- 第二阶段：用刚产生的真实状态再重排一次 ---
    # 文档第 2 条⑶ 明确要求解决这个冷启动问题：
    #   "导入前 sub2api 没有账号 -> 智能优先级无法工作，
    #    需要先导入一次，再重新导入才能应用智能优先级"
    #
    # 第一阶段（上面的 health_rerank_priority）在首次导入时看不到任何实测
    # 状态，只能沿用 CPA 的优先级。但账号刚建出来就已经有 status/schedulable
    # 了，所以这里立刻拿新数据再排一次，并把新的 priority 推给已存在的账号。
    #
    # 只在这一种情形下做：本阶段**刚新建过账号**。
    # 注意 success 是**名字列表**不是计数（见 do_import 开头的初始化），
    # 所以判空用 `if success:`，跟 0 比大小会 TypeError。
    # 没有新建就说明这是第二次及以后的运行，第一阶段已经拿到完整数据、
    # 上面这次重排是多余的，白跑一轮网络请求。
    stage2 = {"ran": False, "ranked": 0, "updated": 0, "msg": ""}
    if success and cfg.get("health_rerank_enabled", True):
        print("\n [第二阶段] 用新建账号的实测状态重排优先级...")
        _start_admin_calibration(api, cfg, "健康快照")
        have2, hm2_err = existing_accounts_by_name(api)
        if hm2_err:
            stage2["msg"] = "读取账号详情失败：%s" % hm2_err
            print("    ! %s" % stage2["msg"])
        else:
            ranked2, msg2 = health_rerank_priority(all_recs, have2, cfg)
            stage2["ran"] = True
            stage2["ranked"] = ranked2
            stage2["msg"] = msg2
            if not ranked2:
                print("    · %s" % msg2)
            else:
                print("    ✓ %s" % msg2)
                # 第二阶段只更新调度权重，不能再次同步凭据、代理等配置。
                # 写后按账号 ID 读回核对，HTTP 成功本身不计为更新成功。
                targets = [r for r in source_recs if r["name"] in have2]
                if targets:
                    ok2, same2, fail2 = sync_health_ranking(api, targets, have2)
                    stage2["updated"] = ok2
                    stage2["failed"] = len(fail2)
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
        "calibration_failed": [(admin_calibration.stop_phase, admin_calibration.stop_reason)] if admin_calibration.stopped else [],
        "calibration_unverified": dict(admin_calibration.unverified),
        "test_plans_created": plan_ok,
        "test_plan_failed": plan_fail,
        "fallback_chain": fb_chain,
        "fallback_ok": fb_ok,
        "fallback_failed": fb_fail,
        "schedulable_reclaimed": rec_done,
        "schedulable_candidates": rec_total,
        "stage2_rerank": stage2,
        "pruned_accounts": pruned,
        "prune_failed": prune_fail,
        "selected_hosts": sorted(selected_hosts) if selected_hosts is not None else None,
        "unclaimed_source_accounts": unclaimed,
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
    except Exception as ex:
        # import-record.json 是"这次到底导了哪些账号"的**唯一存档**。
        # 写不出来不影响本次导入的正确性，但事后追查（"某条为什么是停用"）
        # 就没有依据了 —— 而且屏幕上的汇总一滚就没了。所以必须出声。
        log_exc("写 out/import-record.json 失败（本次导入的明细无法事后追查）",
                ex, level="warning")
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

    required_failures = [key for key in ("sync_failed", "deactivate_failed", "test_plan_failed", "prune_failed", "calibration_failed")
                         if stat.get(key)]
    if (stat.get("stage2_rerank") or {}).get("failed"):
        required_failures.append("stage2_rerank")
    if required_failures:
        print(" ! 必要同步/计划/删除校准存在失败：%s；本次未完成。" % ", ".join(required_failures))
        return 1

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
            if str(nm) in out:
                return {}, "账号重名，无法取得唯一已有身份快照"
            out[str(nm)] = a
    return out, None


MANAGED_CREDENTIAL_FIELDS = frozenset({
    "api_key", "base_url", "pool_mode", "pool_mode_retry_status_codes", "pool_mode_retry_count",
    "model_mapping", "header_override_enabled", "header_overrides", "custom_headers", "temp_unschedulable_enabled",
    "temp_unschedulable_rules", "openai_capabilities",
})
# Pinned Sub MergePreservingSensitiveCreds retains omitted auth fields. Type
# conversion must explicitly clear them, including fields hidden by its DTO.
SENSITIVE_CREDENTIAL_FIELDS = frozenset({
    "access_token", "refresh_token", "id_token", "agent_private_key", "api_key", "session_key", "cookie",
    "password", "sso_token", "sso", "sso-rw", "clearTextPassword", "aws_secret_access_key",
    "aws_session_token", "service_account_json", "service_account", "private_key",
})
MANAGED_EXTRA_FIELDS = frozenset({
    "openai_responses_mode", "openai_ws_enabled", "enable_tls_fingerprint", "cloak",
    "openai_passthrough", "openai_oauth_passthrough", "anthropic_passthrough",
    "upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled",
    "cpa2sub2api_original_source",
    "cpa2sub2api_source",
    "cpa2sub2api_source_disabled",
})


def _billing_extra_comparison(extra):
    """Native typed billing switches may omit disabled false entries.

    These two readers default absence to false. Other managed fields and true
    values remain exact; this is not a blanket missing-field normalization.
    """
    result = dict(extra or {})
    for key in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
        if result.get(key) is False:
            result.pop(key)
    return result


def diff_account_fields(existing, desired):
    """算出已存在账号需要更新的字段。返回 {字段: 新值}，空 dict 表示无需更新。

    通常只比对本工具真正负责的字段，**不碰** sub2api 侧的运行时状态
    （status / schedulable / error_message / temp_unschedulable_until 等）：
    那些由网关自己或运维手工掌管。唯一例外是明确源停用/排除全部的原子
    关闭，以及本工具来源标记、精确停用状态和无运行时错误共同证明的解除。

    credentials 是整体替换语义（PUT 会覆盖整个 map），所以只要其中任一项
    有差异就整包重发；这也正是 model_mapping 能跟着 CPA 的 excluded-models
    变化而更新的原因——不重发就永远停留在首次导入时的快照。

    priority 必须参与比对：CPA 调整优先级后若不同步，sub2api 侧还在用旧桶号，
    而两边都是按优先级严格分桶调度的（见 remap_priority 的说明），
    桶号错了等于调度顺序整个错位。
    """
    patch = {}
    for field in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
        if field in desired:
            value = (existing.get(field, False)
                     or (existing.get("extra") or {}).get(field, False))
            if _truthy(value) != _truthy(desired[field]):
                patch[field] = bool(desired[field])

    # --- 标量字段：逐个比对，只发变化的 ---
    # load_factor 参与比对：健康度变化后同桶内的分担比例要跟着变，
    # 不同步等于把负载分配冻结在首次导入那一刻（见 build_account_body 的说明）。
    for key in ("name", "type", "priority", "concurrency", "auto_pause_on_expired", "proxy_id",
                "load_factor", "group_ids", "rate_multiplier", "status", "schedulable"):
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
    old_creds = existing.get("_cpa2sub2api_raw_credentials", old_creds)
    if not isinstance(old_creds, dict):
        old_creds = {}
    # Credentials/extra are replacement maps in the upstream API. Comparing
    # only desired keys misses obsolete options removed from the new config.
    new_creds = dict({k: v for k, v in old_creds.items() if k not in MANAGED_CREDENTIAL_FIELDS},
                     **new_creds)
    if desired.get("type") and existing.get("type") != desired["type"]:
        # A protocol/type conversion must not carry OAuth or other old-type
        # credentials into the reviewed apikey payload.
        new_creds = dict({key: None for key in SENSITIVE_CREDENTIAL_FIELDS},
                         **(desired.get("credentials") or {}))
    old_compare = {k: v for k, v in old_creds.items() if k != "api_key"}
    new_compare = {k: v for k, v in new_creds.items() if k != "api_key"}
    visible_key = _visible_source_key(existing)
    key_changed = (visible_key is not None and "api_key" in (desired.get("credentials") or {})
                   and visible_key != desired["credentials"]["api_key"])
    if old_compare != new_compare or "type" in patch or key_changed:
        patch["credentials"] = new_creds

    if "extra" in desired:
        old_extra = existing.get("extra") or {}
        if not isinstance(old_extra, dict):
            old_extra = {}
        new_extra = {k: v for k, v in old_extra.items() if k not in MANAGED_EXTRA_FIELDS}
        new_extra.update(desired.get("extra") or {})
        if _billing_extra_comparison(old_extra) != _billing_extra_comparison(new_extra):
            patch["extra"] = new_extra

    if desired.get("notes") and existing.get("notes") != desired["notes"]:
        patch["notes"] = desired["notes"]

    return patch


def sync_health_ranking(api, recs, have_map, dry_run=False):
    """仅同步健康重排的 priority/load_factor，批量读回后才计成功。

    dry_run 返回拟更新数，不发请求。读回缺字段、缺账号或不一致都算失败；
    错误正文可能含凭据，因此这里只报告固定的失败原因。
    """
    with _admin_calibration_scope(api, {}, '健康重排校准', len(recs)) as calibration:
        import math

        ok, same, fail = 0, 0, []
        planned, seen = [], set()
        records_by_name = {r["name"]: r for r in recs}
        for r in recs:
            name = r["name"]
            cur = have_map.get(name)
            if not cur:
                continue
            if "_source_scope" in r:
                try:
                    if not account_matches_source(cur, r):
                        raise ValueError("source mismatch")
                except (ValueError, RuntimeError):
                    fail.append((name, "来源归属无法确认，未重排"))
                    continue
            aid = cur.get("id")
            if not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0:
                fail.append((name, "已有账号没有有效 id，无法重排"))
                continue
            if aid in seen:
                # 在开始写入前拒绝全部计划，避免同一个 ID 被多个名字覆盖。
                return 0, 0, [(name, "重排账号 id 重复，未执行写入")]
            seen.add(aid)
            priority = r.get("new_priority")
            if not isinstance(priority, int) or isinstance(priority, bool):
                fail.append((name, "重排优先级无效，未更新"))
                continue
            desired = {"priority": priority}
            if r.get("health_score") is not None:
                try:
                    hs = float(r["health_score"])
                    if not math.isfinite(hs) or not 0 <= hs <= 1:
                        raise ValueError("invalid health score")
                    desired["load_factor"] = 8 if hs >= .9 else 6 if hs >= .7 else 4 if hs >= .5 else 2
                except (TypeError, ValueError, OverflowError):
                    fail.append((name, "健康分无效，未更新"))
                    continue
            patch = {key: value for key, value in desired.items() if cur.get(key) != value}
            if not patch:
                same += 1
            else:
                planned.append((name, aid, patch))
        if dry_run:
            return len(planned), same, fail
        pending = []
        for index, (name, aid, patch) in enumerate(planned, 1):
            calibration.item(index, len(planned), aid)
            try:
                calibration.check()
                if "_source_scope" in records_by_name[name]:
                    fresh = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
                    if fresh.get("id") != aid or not account_matches_source(fresh, records_by_name[name]):
                        raise ValueError("source changed")
                    scope = records_by_name[name]["_source_scope"]
                    policy = {"_live_probe_policy": True, "source_scope": scope}
                    with _guarded_admin_write(_account_probe_write_guard(
                            api, fresh, policy, require_consent=False, record=records_by_name[name])):
                        api.update_account(aid, patch)
                else:
                    api.update_account(aid, patch)
                pending.append((name, aid, patch))
                calibration.progress("写入已发送，等待批量读回核对")
            except Exception as error:
                calibration.record_error(error)
                reason = _calibration_failure_reason(error, "重排写入请求失败，结果未验证")
                fail.append((name, reason))
                calibration.add_unverified(1)
                calibration.progress("失败：" + reason)
                if calibration.stopped:
                    remaining = planned[index:]
                    fail.extend((name, calibration.stop_reason + "；未校准") for name, _, _ in remaining)
                    calibration.add_unverified(len(remaining))
                    calibration.progress("已停止，剩余 %d 项未校准" % len(remaining))
                    break
        if calibration.stopped:
            fail.extend((name, "已发送的重排尚未读回，结果未验证") for name, _, _ in pending)
            calibration.add_unverified(len(pending))
            return ok, same, fail
        if not pending:
            return ok, same, fail
        try:
            calibration.progress("批量读取重排结果并核对字段")
            calibration.check()
            rows, error = _fetch_all_accounts(api)
            if error:
                raise ValueError("incomplete readback")
            by_id = {}
            for row in rows:
                aid = row.get("id")
                if aid in by_id:
                    raise ValueError("duplicate readback id")
                by_id[aid] = row
        except Exception as error:
            calibration.record_error(error)
            reason = _calibration_failure_reason(error, "重排写后读取失败，结果未验证")
            fail.extend((name, reason) for name, _, _ in pending)
            calibration.add_unverified(len(pending))
            calibration.progress("失败：" + reason)
            return ok, same, fail
        for name, aid, patch in pending:
            actual = by_id.get(aid, {})
            if all(key in actual and actual[key] == value for key, value in patch.items()):
                ok += 1
                calibration.record_success()
            else:
                fail.append((name, "重排写后字段不一致或缺失，结果未验证"))
        return ok, same, fail


def _migration_write_guard(api, account, record, cfg, replacement=False):
    snapshot = _recovery_snapshot(account)
    def guard():
        fresh = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % account["id"]))
        if (fresh.get("id") != account["id"] or _recovery_snapshot(fresh) != snapshot
                or not _account_source_identity_matches(fresh, record, allow_platform_change=replacement)):
            raise ProbeAuthorityRevoked("migration identity or snapshot changed")
        if "_cpa2sub2api_raw_credentials" in account:
            if api.reveal_account_credentials(fresh) != account["_cpa2sub2api_raw_credentials"]:
                raise ProbeAuthorityRevoked("migration credential snapshot changed")
        if replacement:
            _require_migration_no_dependencies(api, fresh)
    return guard


def _require_no_shadow_dependencies(api, account):
    # DeleteAccount cascades shadow children. Export omits shadows, so only the
    # complete ordinary account listing is suitable for this guard.
    rows, error = _fetch_all_accounts(api, require_total=True)
    if error or not any(r.get("id") == account["id"] for r in rows):
        raise ValueError("complete dependency snapshot unavailable")
    ids = {r.get("id") for r in rows}
    if any(not _is_int(aid) or aid <= 0 for aid in ids):
        raise ValueError("dependency account ids are invalid")
    for row in rows:
        parent = row.get("parent_account_id")
        dimension = row.get("quota_dimension")
        if dimension not in (None, "", "global", "spark"):
            raise ValueError("unknown shadow dimension")
        if parent is None:
            if dimension == "spark":
                raise ValueError("shadow dimension has no verified parent")
        elif (not _is_int(parent) or parent <= 0 or parent == row["id"] or parent not in ids
              or next(r for r in rows if r["id"] == parent).get("parent_account_id") is not None):
            raise ValueError("unknown shadow parent relation")
    if (account.get("parent_account_id") is not None
            or any(r.get("parent_account_id") == account["id"] for r in rows)):
        raise ValueError("shadow dependency prevents migration")


def _require_migration_no_dependencies(api, account):
    _require_no_shadow_dependencies(api, account)
    if _strict_test_plans(api.list_test_plans(account["id"])):
        raise ValueError("scheduled plan or history dependency prevents replacement")
    if (account.get("status") not in ("active", "inactive")
            or not isinstance(account.get("schedulable"), bool)
            or _recovery_runtime_block(account)):
        raise ValueError("unknown runtime intent prevents replacement")


def _verified_candidate_readback(api, aid, desired):
    actual = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
    actual = _with_source_credentials(api, actual)
    if (actual.get("id") != aid or _visible_source_key(actual) is None
            or actual.get("platform") != desired["platform"] or diff_account_fields(actual, desired)):
        raise ValueError("replacement credential or parameter readback unavailable/mismatched")
    return actual


def _replace_owned_account(api, current, record, desired, cfg):
    """Create/read back safely, then delete precisely the dependency-free old ID."""
    _require_migration_no_dependencies(api, current)
    guard = _migration_write_guard(api, current, record, cfg, replacement=True)
    marker = os.urandom(16).hex()
    quarantine_name = "cpa2sub2api-migration-" + marker
    quarantine_id = None
    quarantine_deleted = False
    payload = copy.deepcopy(desired)
    payload["name"] = desired["name"] + "-m" + marker[:12]
    # Empty group_ids auto-bind the native default group. A private exclusive
    # group plus a deny-all model mapping isolates the active-by-default factory.
    payload["credentials"]["model_mapping"] = {BLOCK_ALL_SENTINEL: BLOCK_ALL_SENTINEL}
    payload["extra"] = dict({k: v for k, v in (current.get("extra") or {}).items()
                              if k not in MANAGED_EXTRA_FIELDS}, **payload.get("extra", {}))
    # Native OpenAI passthrough bypasses model_mapping. Keep the temporary
    # quarantine shield effective even when the final account uses a relay.
    payload["extra"].update(openai_passthrough=False, anthropic_passthrough=False)
    # Native buildAccountForCreate removes these two system-managed defaults.
    # Keep top-level billing off; final Update/readback still verifies its full
    # desired extra map. No other missing field is accepted at factory readback.
    for field in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
        if payload["extra"].get(field) is False:
            payload["extra"].pop(field)
    payload["extra"]["cpa2sub2api_migration"] = marker
    value = payload["extra"]["cpa2sub2api_source"]
    value["account_name"] = payload["name"]
    value["identity_sha256"] = _source_digest({k: v for k, v in value.items() if k != "identity_sha256"})
    for field in ("expires_at", "auto_pause_on_expired"):
        if field in current:
            payload[field] = current[field]
    created_id = None
    deleted_old = False
    delete_attempted = False
    old_present = None
    try:
        with _guarded_admin_write(guard):
            group = _unwrap_one(api.create_group({"name": quarantine_name, "platform": desired["platform"],
                "rate_multiplier": 1, "is_exclusive": True, "subscription_type": "standard"}))
        if (not isinstance(group, dict) or not _is_int(group.get("id")) or group["id"] <= 0
                or group.get("name") != quarantine_name or group.get("platform") != desired["platform"]
                or group.get("is_exclusive") is not True):
            raise ValueError("quarantine group not verified")
        quarantine_id = group["id"]
        payload["group_ids"] = [quarantine_id]
        with _guarded_admin_write(guard):
            response = api.batch_accounts([payload])
        results = (response.get("data") or {}).get("results") or []
        if (len(results) != 1 or not results[0].get("success")
                or not _is_int(results[0].get("id")) or results[0]["id"] <= 0
                or results[0].get("name") != payload["name"]):
            raise ValueError("replacement create result incomplete")
        created_id = results[0]["id"]
        if created_id == current["id"]:
            raise ValueError("replacement reused the old id")
        actual = _verified_candidate_readback(api, created_id, payload)
        with _guarded_admin_write(guard):
            api.bulk_update_accounts([created_id], {"status": "inactive", "schedulable": False})
        inactive = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % created_id))
        if (inactive.get("status") != "inactive" or inactive.get("schedulable") is not False
                or inactive.get("group_ids") != [quarantine_id]):
            raise ValueError("quarantine stop state not persisted")
        final = copy.deepcopy(desired)
        final["extra"] = dict(payload["extra"], **desired.get("extra", {}))
        final["extra"]["cpa2sub2api_source"] = copy.deepcopy(desired["extra"]["cpa2sub2api_source"])
        # Preserve existing memberships; source-configured memberships are added.
        final["group_ids"] = sorted(set(current.get("group_ids") or []) | set(desired.get("group_ids") or []))
        for field in ("expires_at", "auto_pause_on_expired"):
            if field in current:
                final[field] = current[field]
        with _guarded_admin_write(guard):
            api.update_account(created_id, {k: v for k, v in final.items()
                                           if k not in ("platform", "confirm_mixed_channel_risk")})
        intent = {"status": "inactive" if needs_inactive(record, cfg) else current["status"],
                  "schedulable": False if needs_inactive(record, cfg) else current["schedulable"]}
        with _guarded_admin_write(guard):
            api.bulk_update_accounts([created_id], intent)
        actual = _verified_candidate_readback(api, created_id, final)
        if any(actual.get(k) != v for k, v in intent.items()):
            raise ValueError("replacement parameters or intent not persisted")
        if quarantine_id in (actual.get("group_ids") or []):
            raise ValueError("candidate still bound to quarantine")
        with _guarded_admin_write(guard):
            api.delete_group(quarantine_id)
        quarantine_deleted = True
        delete_attempted = True
        try:
            def deletion_guard():
                guard()
                fresh_candidate = _verified_candidate_readback(api, created_id, final)
                if (_recovery_snapshot(fresh_candidate) != _recovery_snapshot(actual)
                        or fresh_candidate.get("_cpa2sub2api_raw_credentials")
                        != actual.get("_cpa2sub2api_raw_credentials")):
                    raise ProbeAuthorityRevoked("replacement candidate changed before old deletion")
            with _guarded_admin_write(deletion_guard):
                api.delete_account(current["id"])
        except Exception:
            pass  # A lost DELETE response must be resolved by readback.
        try:
            _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % current["id"]))
            old_present = True
        except ApiError as ex:
            if ex.status != 404:
                raise
            deleted_old = True
        if not deleted_old:
            raise ValueError("old account deletion not verified")
        # Deleting the old-owned ID is the final mutation. The nonsensitive
        # marker remains as migration provenance and a recovery identifier.
        return actual
    except Exception:
        if not deleted_old and (not delete_attempted or old_present is True):
            if created_id is None:
                rows, error = _fetch_all_accounts(api)
                candidates = [] if error else [r for r in rows if r.get("name") == payload["name"]
                    and (r.get("extra") or {}).get("cpa2sub2api_migration") == marker]
                if len(candidates) == 1:
                    created_id = candidates[0]["id"]
            if created_id is not None and created_id != current["id"]:
                candidate = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % created_id))
                if ((candidate.get("extra") or {}).get("cpa2sub2api_migration") == marker
                        and account_source_scope(candidate) == record.get("_source_scope", "default")):
                    _require_migration_no_dependencies(api, candidate)
                    api.delete_account(created_id)
            if not quarantine_deleted:
                if quarantine_id is None:
                    groups = [g for g in api.list_groups() if g.get("name") == quarantine_name
                              and g.get("platform") == desired["platform"] and g.get("is_exclusive") is True]
                    if len(groups) == 1:
                        quarantine_id = groups[0]["id"]
                if quarantine_id is not None:
                    rows, error = _fetch_all_accounts(api)
                    if error or any(quarantine_id in (r.get("group_ids") or []) for r in rows):
                        raise ValueError("quarantine cleanup has unknown bindings")
                    api.delete_group(quarantine_id)
        raise


def _source_stop_healthy(account):
    """Only the exact tool stop may be undone; runtime/manual holds remain."""
    return (not account.get("error_message") and not account.get("temp_unschedulable_reason")
            and all(account.get(key) in (None, "") for key in (
                "temp_unschedulable_until", "rate_limit_reset_at", "rate_limited_at", "overload_until"))
            and not (account.get("extra") or {}).get("model_rate_limits")
            and not (account.get("extra") or {}).get("antigravity_quota_scopes"))


def _source_stop_transition(current, desired, record):
    """Bind reversible source stops to verified source metadata and exact state."""
    result = copy.deepcopy(desired)
    if not record.get("_source_fidelity"):
        return result
    try:
        bound = _strong_source_binding(current, record)
    except (ValueError, RuntimeError, TypeError):
        bound = False
    marker = bound and (current.get("extra") or {}).get("cpa2sub2api_source_disabled") is True
    clean = _source_stop_healthy(current)
    stopped = current.get("status") == "inactive" and current.get("schedulable") is False
    if result.get("status") == "inactive":
        was_running = current.get("status") == "active" and current.get("schedulable") is True
        result["extra"]["cpa2sub2api_source_disabled"] = bound and clean and (was_running or marker and stopped)
    elif marker:
        if stopped and clean:
            result.update(status="active", schedulable=True)
        result["extra"]["cpa2sub2api_source_disabled"] = False
    return result


SOURCE_BULK_FIELDS = frozenset({
    "name", "proxy_id", "concurrency", "priority", "rate_multiplier", "load_factor",
    "status", "schedulable", "group_ids", "credentials", "extra",
    "upstream_billing_probe_enabled", "confirm_mixed_channel_risk",
})


def _write_source_state_patch(api, current, change, cfg, record):
    """Use the real single-ID bulk DTO for schedulable; unsupported fields stay PUT.

    If the complete transition cannot fit bulk, freeze routing first, update the
    other parameters while stopped, then publish only the exact desired state.
    Every boundary rereads ownership and the previous verified snapshot.
    """
    aid = current["id"]

    def write(snapshot, patch, bulk):
        guard = _account_probe_write_guard(api, snapshot, cfg, require_consent=False, record=record)
        def checked():
            guard()
            if "type" in patch:
                _require_no_shadow_dependencies(api, snapshot)
        with _guarded_admin_write(checked):
            if bulk:
                api.bulk_update_accounts([aid], patch)
            else:
                api.update_account(aid, patch)

    removes_map_fields = any(
        field in change and set((current.get("_cpa2sub2api_raw_credentials", current.get(field))
                                 if field == "credentials" else current.get(field)) or {})
        - set(change[field] or {}) for field in ("credentials", "extra"))
    source_state_fields = {"status", "schedulable", "credentials", "extra"}
    if not set(change) - source_state_fields and not removes_map_fields:
        write(current, change, True)
        return
    write(current, {"status": "inactive", "schedulable": False}, True)
    stopped = _with_source_credentials(api, _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid)))
    expected = copy.deepcopy(current)
    expected.update(status="inactive", schedulable=False)
    before_snapshot = json.loads(_recovery_snapshot(expected))
    stopped_snapshot = json.loads(_recovery_snapshot(stopped))
    before_snapshot.pop("updated_at", None)
    stopped_snapshot.pop("updated_at", None)
    if (before_snapshot != stopped_snapshot
            or current.get("_cpa2sub2api_raw_credentials") != stopped.get("_cpa2sub2api_raw_credentials")):
        raise ValueError("source freeze readback changed outside stopped state")
    ordinary = {key: value for key, value in change.items() if key not in ("status", "schedulable")}
    if ordinary:
        write(stopped, ordinary, False)
    updated = _with_source_credentials(api, _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid)))
    expected_updated = copy.deepcopy(stopped)
    expected_updated.update(copy.deepcopy(ordinary))
    if "credentials" in ordinary and "_cpa2sub2api_raw_credentials" in expected_updated:
        expected_updated["_cpa2sub2api_raw_credentials"] = copy.deepcopy(ordinary["credentials"])
    expected_snapshot = json.loads(_recovery_snapshot(expected_updated))
    actual_snapshot = json.loads(_recovery_snapshot(updated))
    expected_snapshot["extra"] = _billing_extra_comparison(expected_snapshot.get("extra"))
    actual_snapshot["extra"] = _billing_extra_comparison(actual_snapshot.get("extra"))
    if "_cpa2sub2api_raw_credentials" in expected_updated:
        expected_snapshot["credentials"] = expected_updated["_cpa2sub2api_raw_credentials"]
    if "_cpa2sub2api_raw_credentials" in updated:
        actual_snapshot["credentials"] = updated["_cpa2sub2api_raw_credentials"]
    expected_snapshot.pop("updated_at", None)
    actual_snapshot.pop("updated_at", None)
    if (updated.get("status") != "inactive" or updated.get("schedulable") is not False
            or not account_matches_source(updated, record)
            or expected_snapshot != actual_snapshot
            or expected_updated.get("_cpa2sub2api_raw_credentials") != updated.get("_cpa2sub2api_raw_credentials")
            or diff_account_fields(updated, expected_updated)):
        raise ValueError("source stopped parameter readback mismatch")
    write(updated, {"status": change.get("status", current.get("status")),
                    "schedulable": change.get("schedulable", current.get("schedulable"))}, True)


def sync_existing_accounts(api, recs, cfg, gmap, pmap, have_map, dry_run=False):
    """Update managed configuration, retaining unknown upstream runtime metadata."""
    if _truthy((cfg.get("request_relay") or {}).get("enabled")) and not cfg.get("_request_relay_bundle"):
        cfg = dict(cfg)
        _prepare_request_relay(cfg, recs)
    proxy_need = build_proxy_need_map(recs)
    ok, same, failures = 0, 0, []
    for record in recs:
        current = have_map.get(record["name"])
        if not current:
            continue
        if "_cpa2sub2api_raw_credentials" not in current:
            current = _with_source_credentials(api, current)
        if "_source_scope" in record:
            try:
                if not _account_source_identity_matches(current, record, allow_platform_change=True):
                    raise ValueError("different or unverified source")
            except (ValueError, RuntimeError):
                failures.append((record["name"], "来源归属不同、损坏或旧账号身份无法确认，未同步"))
                continue
        aid = current.get("id")
        if not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0:
            failures.append((record["name"], "已有账号 id 无效"))
            continue
        desired = copy.deepcopy(to_account(record, cfg, gmap, pmap, proxy_need))
        strong_identity = "_source_scope" not in record or _strong_source_binding(current, record)
        if not strong_identity:
            if current.get("type") != desired["type"] or current.get("platform") != desired["platform"]:
                failures.append((record["name"], "旧身份仅有历史短指纹；类型/平台变更需要完整凭据或显式可信证据"))
                continue
            desired["credentials"].pop("api_key", None)
            desired["extra"]["cpa2sub2api_source"] = _legacy_claim_metadata(
                current, record, record.get("_source_scope", _source_scope(cfg)))
        replace = current.get("platform") != desired["platform"]
        if replace:
            if dry_run:
                try:
                    _require_migration_no_dependencies(api, current)
                    ok += 1
                except Exception:
                    failures.append((record["name"], "平台替换存在依赖或身份无法核实，未迁移"))
                continue
            try:
                actual = _replace_owned_account(api, current, record, desired, cfg)
                have_map[record["name"]] = actual
                ok += 1
            except Exception:
                failures.append((record["name"], "平台替换失败或清理/读回不完整，未计成功"))
            continue
        for key in ("platform", "confirm_mixed_channel_risk"):
            desired.pop(key, None)
        desired = _source_stop_transition(current, desired, record)
        change = diff_account_fields(current, desired)
        if not change:
            same += 1
            continue
        if dry_run:
            ok += 1
            continue
        try:
            fresh = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            if fresh.get("id") != aid or not _is_migrated(fresh):
                raise ValueError("ownership mismatch")
            if "_source_scope" in record and not account_matches_source(fresh, record):
                raise ValueError("source ownership changed")
            fresh = _with_source_credentials(api, fresh)
            if "_source_scope" in record and not account_matches_source(fresh, record):
                raise ValueError("revealed source identity changed")
            # A manual/error hold can appear after the initial list read.
            source_desired = to_account(record, cfg, gmap, pmap, proxy_need)
            desired = _source_stop_transition(fresh, source_desired, record)
            for key in ("platform", "confirm_mixed_channel_risk"):
                desired.pop(key, None)
            if not strong_identity:
                desired["credentials"].pop("api_key", None)
                desired["extra"]["cpa2sub2api_source"] = _legacy_claim_metadata(
                    fresh, record, record.get("_source_scope", _source_scope(cfg)))
            change = diff_account_fields(fresh, desired)
            if not change:
                same += 1
                continue
            if "type" in change:
                _require_no_shadow_dependencies(api, fresh)
            if "credentials" in change and fresh.get("platform") == "openai":
                mode = (desired.get("extra") or {}).get("openai_responses_mode")
                if mode not in ("force_responses", "force_chat_completions"):
                    raise ValueError("unsafe OpenAI migration mode")
                change["extra"] = dict({k: v for k, v in (fresh.get("extra") or {}).items()
                                        if k not in MANAGED_EXTRA_FIELDS}, **desired["extra"])
            if "schedulable" in change:
                _write_source_state_patch(api, fresh, change, cfg, record)
            elif "_source_scope" in record:
                base_guard = _account_probe_write_guard(api, fresh, cfg, require_consent=False, record=record)
                def guard():
                    base_guard()
                    if "type" in change:
                        _require_no_shadow_dependencies(api, fresh)
                with _guarded_admin_write(guard):
                    api.update_account(aid, change)
            else:
                api.update_account(aid, change)
            actual = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            if "_cpa2sub2api_raw_credentials" in fresh:
                actual = _with_source_credentials(api, actual)
                if "_cpa2sub2api_raw_credentials" not in actual:
                    raise ValueError("credential readback unavailable")
            verify = dict(desired)
            # Admin API may mask keys; never use a masked key as evidence.
            if diff_account_fields(actual, verify):
                raise ValueError("readback mismatch")
            have_map[record["name"]] = actual
            ok += 1
        except Exception:
            failures.append((record["name"], "受管参数同步失败或读回不一致，未计成功"))
    return ok, same, failures


def _restore_schedulable_verified(api, targets, activate=False, batch=50):
    """使用支持调度字段的接口；HTTP 成功后逐账号核对持久状态。"""
    if not BULK_HAS_SCHEDULABLE:
        return 0, len(targets), "上游 bulk-update 不支持 schedulable，未执行恢复。"
    done, failed = 0, 0
    batch = max(1, int(batch))
    for i in range(0, len(targets), batch):
        chunk = targets[i:i + batch]
        patch = {"schedulable": True}
        if activate:
            patch["status"] = "active"
        try:
            api.bulk_update_accounts(chunk, patch)
        except Exception:
            # 不回显服务端正文：其中可能含上游地址或凭据。
            failed += len(chunk)
            continue
        for aid in chunk:
            try:
                account = _unwrap_one(api._call("GET", f"/api/v1/admin/accounts/{aid}"))
                verified = (account.get("id") == aid
                            and str(account.get("status") or "").strip().lower() == "active"
                            and account.get("schedulable") is True)
            except Exception:
                verified = False
            if verified:
                done += 1
            else:
                failed += 1
    err = "有 %d 条恢复未能核实 active 与 schedulable=true，请检查实际状态。" % failed if failed else None
    return done, len(targets), err


def _recovery_snapshot(account):
    """Conservative observable configuration guard; GET/PUT is not atomic CAS."""
    fields = ("id", "name", "status", "schedulable", "credentials", "proxy_id",
              "notes", "extra", "platform", "type", "group_ids",
              "temp_unschedulable_until", "temp_unschedulable_reason", "rate_limited_at", "rate_limit_reset_at",
              "overload_until", "updated_at", "priority", "rate_multiplier",
              "concurrency", "load_factor", "expires_at", "auto_pause_on_expired", "error_message")
    return json.dumps({k: account.get(k) for k in fields}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def _recovery_runtime_block(account, now=None):
    """Expired known deadlines are harmless; ambiguous runtime state is not."""
    from datetime import datetime
    now = time.time() if now is None else now
    for field in ("temp_unschedulable_until", "rate_limit_reset_at", "overload_until"):
        value = account.get(field)
        if value in (None, ""):
            continue
        try:
            instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.timestamp() > now:
                return True
        except (TypeError, ValueError, OverflowError):
            return True
    if account.get("rate_limited_at") and not account.get("rate_limit_reset_at"):
        return True
    if account.get("rate_limited_at"):
        try:
            started = datetime.fromisoformat(str(account["rate_limited_at"]).replace("Z", "+00:00"))
            if started.tzinfo is None or started.timestamp() > now:
                return True
        except (TypeError, ValueError, OverflowError):
            return True
    if account.get("temp_unschedulable_reason") and not account.get("temp_unschedulable_until"):
        return True
    extra = account.get("extra") or {}
    return bool(extra.get("model_rate_limits") or extra.get("antigravity_quota_scopes"))


def _current_error_recovery_record(account, cfg):
    """Error recovery needs the still-declared source key, not only old metadata."""
    with io.open(CONFIG_YAML, encoding="utf-8-sig") as source:
        document = _yaml().safe_load(source)
    if not isinstance(document, dict):
        raise ValueError("invalid current source")
    records, dropped = collect(document)
    for record in records:
        record["_source_scope"] = _source_scope(cfg)
    assign_names(records)
    matched = [record for record in records if record.get("name") == account.get("name")
               and account_matches_source(account, record)]
    if len(matched) != 1 or matched[0].get("disabled") or serves_nothing(matched[0]):
        raise ValueError("error account is not an enabled current source record")
    record = matched[0]
    weight = record.get("weight")
    if (str((document.get("routing") or {}).get("strategy") or "").lower() == "weighted-round-robin"
            and _is_int(weight) and weight <= 0):
        raise ValueError("current source excludes zero-weight credentials")
    mapping = record_model_mapping(record)
    if "cpa2sub2api_original_source" in (account.get("extra") or {}):
        import request_relay
        mapping = request_relay.relay_model_mapping(record, mapping)
        _, _, route, _ = _relay_account_diagnostic_context(account, cfg)
        mapping = request_relay.diagnostic_model_mapping(route, mapping)
    if not mapping or (account.get("credentials") or {}).get("model_mapping") != mapping:
        raise ValueError("error account model policy is stale; sync source first")
    return record


def _successful_test_stream(raw):
    """Accept only an explicit successful terminal SSE event, never HTML/cron."""
    if not isinstance(raw, str):
        return False
    raw = raw.replace("\r\n", "\n")
    if not raw.endswith("\n\n"):
        return False
    events = []
    try:
        for block in raw.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines()
                             if line.startswith("data:"))
            if data:
                event = json.loads(data)
                if not isinstance(event, dict):
                    return False
                events.append(event)
    except (ValueError, TypeError):
        return False
    return bool(events and events[-1].get("type") == "test_complete"
                and events[-1].get("success") is True
                and not any(e.get("type") == "error" or e.get("success") is False
                            for e in events))


def probe_import_recovery(api, name_to_id, cfg, section_by_name=None):
    """Fresh per-invocation evidence for exact imported IDs, with bounded workers.

    The upstream test route may clear error/rate-limit state itself. Only active
    or inactive accounts without cooldown are candidates; any observed state or
    configuration change causes a conservative skip before our explicit write.
    A protocol test success is not proof of all model business outputs.
    """
    section_by_name = section_by_name or {}
    authorized_ids = {value for value in cfg.get("explicit_recovery_account_ids", [])
                      if isinstance(value, int) and not isinstance(value, bool) and value > 0}
    def probe(item):
        name, aid = item
        if aid not in authorized_ids:
            return aid, None, None
        try:
            account = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%s" % aid))
            if not probe_policy.probe_allowed(account_probe_base_url(account), cfg):
                return aid, None, None
            scope = _policy_source_scope(cfg)
            if scope is not None and account_source_scope(account) != scope:
                return aid, None, "账号不属于当前来源，未探测或恢复"
            if account.get("id") != aid or not _is_migrated(account):
                return aid, None, "账号归属或 ID 不匹配"
            if (account.get("extra") or {}).get("synthetic_ui_test") is True:
                return aid, None, "合成 UI 测试账号不可作为真实探活证据"
            status = str(account.get("status") or "").strip().lower()
            if status not in ("active", "inactive", "error"):
                return aid, None, None
            if status == "error" and not cfg.get("_explicit_error_recovery"):
                return aid, None, None
            if status == "error" and account.get("schedulable") is not False:
                return aid, None, "error账号调度状态不是明确关闭，需先检查，未自动清错"
            if status == "active" and account.get("schedulable") is not False:
                return aid, None, None
            if status == "inactive" and not cfg.get("revive_proven_inactive", True):
                return aid, None, None
            if _recovery_runtime_block(account):
                return aid, None, "账号仍有冷却状态，保留原状"
            if "cpa2sub2api_original_source" not in (account.get("extra") or {}):
                return aid, None, "原生账号检测需开启 REQUEST_RELAY_ENABLED 和 REQUEST_RELAY_FORCE_ALL_ACCOUNTS 后同步此站；普通 Direct 业务保持可用"
            error_record = _current_error_recovery_record(account, cfg) if status == "error" else None
            model = _pick_probe_model((account.get("credentials") or {}).get("model_mapping"),
                                      account.get("platform"), section_by_name.get(name))
            if not model:
                return aid, None, "持久化账号缺少可测试模型"
            snapshot = _recovery_snapshot(account)
            live = resolve_probe_policy(cfg)
            if not probe_policy.probe_allowed(account_probe_base_url(account), live):
                return aid, None, "站点授权已撤销"
            success = _test_relay_account_exact(account, model, live)
            if not success:
                return aid, None, "本轮即时探活未获得完整成功事件"
            current = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%s" % aid))
            if _recovery_snapshot(current) != snapshot:
                return aid, None, "探活期间状态或配置发生变化，跳过显式恢复"
            return aid, {"snapshot": snapshot, "status": status, "success": True,
                         "error_source_provenance": _probe_record_provenance(error_record) if error_record else None,
                         "recovery_authorized": True,
                         "live_policy": cfg.get("_live_probe_policy", False),
                         "policy": {key: copy.deepcopy(cfg[key]) for key in (
                             "_live_probe_policy", "source_scope", "request_relay_source_scope", "request_relay",
                             "model_probe_enabled", "online_probe_hosts", "auto_recover_enabled") if key in cfg},
                         "source_scope": _policy_source_scope(cfg),
                         "host": probe_policy.normalize_host(account_probe_base_url(account))}, None
        except Exception:
            return aid, None, "本轮即时探活或状态读取失败"
    evidence, failures = {}, []
    workers = max(1, min(int(cfg.get("test_plan_workers", 8) or 8), 8))
    # Stable IDs are tested once even when names alias the same account.
    unique = dict((aid, name) for name, aid in name_to_id.items() if aid)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for aid, result, reason in pool.map(probe, ((name, aid) for aid, name in unique.items())):
            if result:
                evidence[aid] = result
            if reason:
                failures.append((aid, reason))
    return evidence, failures


def _restore_from_job(api, evidence, status, dry_run, batch):
    candidates = [(aid, e) for aid, e in (evidence or {}).items()
                  if e.get("success") is True and e.get("status") == status
                  and e.get("recovery_authorized") is True]
    if dry_run:
        return 0, len(candidates), None
    done, failed = 0, 0
    for aid, entry in candidates:
        try:
            if entry.get("live_policy"):
                live = load_settings()
                if (not probe_policy.probe_allowed(entry.get("host"), live)
                        or entry.get("source_scope") is not None and _source_scope(live) != entry["source_scope"]):
                    failed += 1
                    continue
            current = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%s" % aid))
            if current.get("id") != aid or _recovery_snapshot(current) != entry.get("snapshot"):
                failed += 1
                continue
            policy = entry.get("policy")
            if policy is None and entry.get("live_policy"):
                policy = {"_live_probe_policy": True, "model_probe_enabled": True,
                          "online_probe_hosts": {entry.get("host"): True}}
                if entry.get("source_scope") is not None:
                    policy["source_scope"] = entry["source_scope"]
            guard = _account_probe_write_guard(api, current, policy)
            with _guarded_admin_write(guard):
                count, _, error = _restore_schedulable_verified(api, [aid],
                                            activate=status == "inactive", batch=batch)
            done += count
            failed += bool(error)
        except Exception:
            failed += 1
    return done, len(candidates), ("有 %d 条状态变化或恢复未核实，已保留失败记录。" % failed if failed else None)


def _restore_error_from_job(api, evidence):
    """Clear only explicitly tested errors, then separately restore scheduling."""
    done, failed = 0, 0
    for aid, entry in (evidence or {}).items():
        if entry.get("status") != "error" or entry.get("success") is not True or not entry.get("recovery_authorized"):
            continue
        try:
            policy = entry["policy"]
            current = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            if _recovery_snapshot(current) != entry["snapshot"] or current.get("status") != "error":
                raise ProbeAuthorityRevoked("error changed after probe")
            source_digest = entry["error_source_provenance"]
            def guard_for(account):
                base_guard = _account_probe_write_guard(api, account, policy, require_clear_runtime=True)
                def guard():
                    base_guard()
                    record = _current_error_recovery_record(account, resolve_probe_policy(policy))
                    if _probe_record_provenance(record) != source_digest:
                        raise ProbeAuthorityRevoked("error source changed after probe")
                return guard
            with _guarded_admin_write(guard_for(current)):
                api._call("POST", "/api/v1/admin/accounts/%d/clear-error" % aid)
            cleared = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            expected = dict(current, status="active", error_message="", updated_at=cleared.get("updated_at"))
            # The official endpoint also discards known expired runtime metadata.
            for field in ("temp_unschedulable_until", "temp_unschedulable_reason", "rate_limited_at",
                          "rate_limit_reset_at", "overload_until"):
                if cleared.get(field) in (None, ""):
                    expected[field] = cleared.get(field)
            if (_recovery_runtime_block(cleared) or cleared.get("error_message") != ""
                    or _recovery_snapshot(cleared) != _recovery_snapshot(expected)):
                raise ProbeAuthorityRevoked("clear error changed unrelated state")
            with _guarded_admin_write(guard_for(cleared)):
                count, _, error = _restore_schedulable_verified(api, [aid])
            final = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            expected_final = dict(cleared, schedulable=True, updated_at=final.get("updated_at"))
            if (count != 1 or error or final.get("error_message") != "" or _recovery_runtime_block(final)
                    or _recovery_snapshot(final) != _recovery_snapshot(expected_final)):
                raise ValueError("error recovery was not fully verified")
            done += 1
        except Exception:
            # Clearing may have succeeded while scheduling remains closed.
            failed += 1
    return done, failed


def reclaim_schedulable(api, dry_run=False, batch=50, evidence=None):
    """Only exact current-job IDs with fresh successful probes may be restored."""
    return _restore_from_job(api, evidence, "active", dry_run, batch)


def revive_proven_inactive(api, cfg, dry_run=False, batch=50, evidence=None):
    """Default enabled; historical scheduled-test results never authorize writes."""
    if not cfg.get("revive_proven_inactive", True):
        return 0, 0, None
    return _restore_from_job(api, evidence, "inactive", dry_run, batch)


def _unwrap_one(resp):
    """从 sub2api 的**单对象**响应里取出对象。

    为什么必须有这个函数：sub2api 的两类接口信封不一样。
      · 列表接口（/accounts?page=..）      -> {"data": {"items": [...], "total": N}}
      · 单对象接口（/accounts/{id}）        -> {"code": 0, "message": "...", "data": {...}}
    直接读顶层键（resp["credentials"]、resp["platform"]）在单对象接口上
    **恒为 None** —— 不抛异常、不报错，只是悄悄拿到空值。
    实测后果（2026-09-19，本机 231 条导入）：ensure_test_plans 逐账号去读
    credentials.model_mapping 拿模型，拿到的全是 None，于是每条都
    "未找到可用模型" → **231 条探活计划全部挂载失败**。
    而探活是停用账号恢复的唯一手段（见 revive_proven_inactive），
    挂了等于整条自动恢复链路断掉。
    """
    if not isinstance(resp, dict):
        return {}
    d = resp.get("data")
    if isinstance(d, dict):
        # 有的接口 data 里还套一层 items
        for k in ("items", "list", "results", "records"):
            v = d.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v[0]
        return d
    if not any(k in resp for k in ("code", "message", "success")):
        # 没有任何信封特征：就当它本身就是那个对象
        return resp
    # 有信封但没有可用的 data，退回原响应（至少不丢信息）
    return resp


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


def find_stale_accounts(api, recs, hosts=None):
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
    scopes = {r["_source_scope"] for r in recs if "_source_scope" in r}
    if len(scopes) > 1:
        return [], "配置混合多个来源，拒绝判断旧账号"
    stale = []
    for a in accs:
        if not _is_migrated(a):
            continue
        if ((a.get("extra") or {}).get("cpa2sub2api_source") or {}).get("kind") == "legacy_claim":
            continue  # A short historical fingerprint cannot authorize deletion.
        if scopes:
            try:
                if account_source_scope(a) not in scopes:
                    continue
            except (ValueError, RuntimeError):
                return [], "账号来源归属损坏，拒绝判断旧账号"
        if hosts is not None and probe_policy.normalize_host(account_probe_base_url(a)) not in hosts:
            continue
        nm = str(a.get("name") or "")
        if nm and nm not in want:
            stale.append(a)
    return stale, None


def prune_stale_accounts(api, recs, dry_run=False, hosts=None):
    """Remove only exact sentinel-owned stale/duplicate accounts after full reads.

    Empty plans are never deletion authority. Each candidate is reread before
    deletion so a concurrent edit or ownership change cancels that deletion.
    """
    if not recs or any(not r.get("name") or not r.get("api_key") for r in recs):
        return [], [("plan", "配置为空或存在不完整条目，拒绝删除")]
    rows, error = _fetch_all_accounts(api)
    if error:
        return [], [("snapshot", "账号列表读取不完整，拒绝删除")]
    scopes = {r["_source_scope"] for r in recs if "_source_scope" in r}
    if len(scopes) > 1 or scopes and any("_source_scope" not in r for r in recs):
        return [], [("plan", "来源配置不一致，拒绝删除")]
    if scopes:
        try:
            rows = [row for row in rows if _is_migrated(row) and account_source_scope(row) in scopes]
        except (ValueError, RuntimeError):
            return [], [("ownership", "账号来源归属损坏，拒绝删除")]
    if hosts is not None:
        try:
            rows = [row for row in rows if probe_policy.normalize_host(account_probe_base_url(row)) in hosts]
        except (ValueError, RuntimeError):
            return [], [("ownership", "单站账号来源归属损坏，拒绝删除")]
    desired = {r["name"] for r in recs}
    keep_ids = {}
    for row in sorted(rows, key=lambda a: a.get("id") or 0):
        if _is_migrated(row) and row.get("name") in desired:
            keep_ids.setdefault(row["name"], row.get("id"))
    # Do not remove old credentials until every new configuration entry exists.
    if set(keep_ids) != desired:
        return [], [("snapshot", "新配置账号未全部持久化，拒绝删除")]
    candidates = [row for row in rows if _is_migrated(row)
                  and ((row.get("extra") or {}).get("cpa2sub2api_source") or {}).get("kind") != "legacy_claim"
                  and (row.get("name") not in desired
                       or row.get("id") != keep_ids.get(row.get("name")))]
    if dry_run:
        return [{"id": row.get("id"), "name": row.get("name")} for row in candidates], []
    removed, failures = [], []
    for row in candidates:
        aid = row.get("id")
        if not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0:
            failures.append(("account", "删除候选 id 无效"))
            continue
        try:
            fresh = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            if (not _is_migrated(fresh) or fresh.get("id") != aid
                    or _recovery_snapshot(fresh) != _recovery_snapshot(row)):
                raise ValueError("candidate changed")
            if scopes and account_source_scope(fresh) not in scopes:
                raise ValueError("source ownership changed")
            def guard():
                latest = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
                if (latest.get("id") != aid or not _is_migrated(latest)
                        or _recovery_snapshot(latest) != _recovery_snapshot(row)
                        or scopes and account_source_scope(latest) not in scopes):
                    raise ProbeAuthorityRevoked("prune candidate changed")
                _require_no_shadow_dependencies(api, latest)
            try:
                with _guarded_admin_write(guard):
                    api.delete_account(aid)
            except Exception:
                # DELETE may have applied despite a lost response; the final
                # readback decides success without retrying a broad cleanup.
                pass
            try:
                result = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            except ApiError as missing:
                if missing.status != 404:
                    raise
                result = {}
            if result.get("id") == aid:
                raise ValueError("deletion not verified")
            removed.append({"id": aid, "name": row.get("name")})
        except Exception:
            failures.append(("account-%s" % aid, "删除失败、候选已变化或删除未能读回确认"))
    return removed, failures


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


def _pick_probe_model(model_mapping, platform, source_section=None):
    """从账号的 model_mapping 里挑一个探活用的模型 id，挑不到返回 None。

    规则必须与写入白名单时的就高原则一致，所以直接复用 model_selection
    的选型函数，而不是在本地重写一份正则。差异化的那一部分（"探活挑哪一个"）
    才是这里独有的：
      · claude 优先 sonnet（额度便宜、响应快），其次 opus，最后 haiku；
      · 其它平台取就高结果里的第一个。

    source_section 必须一路传下来：写入白名单时 select_highest_models 是按
    它分流的（codex-api-key 走单族、openai-compatibility 走多族），探活若不传
    就会落到多族分支，**挑出一个不在白名单里的模型**——探活结果无论成败都
    不能代表这个账号真实可用性，还会在 sub2api 里留下无意义的失败记录。
    """
    if not model_mapping or not isinstance(model_mapping, dict):
        return None

    from relay.config import is_diagnostic_model_marker
    keys = [str(k) for k in model_mapping.keys() if not is_diagnostic_model_marker(str(k))]
    if not keys:
        return None
    if not MODEL_SELECTION_AVAILABLE or not platform:
        return keys[0] if keys else None

    models = [{"name": k, "alias": k} for k in keys]
    picked = select_highest_models(models, platform, series=MODEL_SERIES,
                                  source_section=source_section)
    names = [m["name"] for m in picked] or keys

    if platform == "anthropic":
        for preferred in ("sonnet", "opus", "haiku"):
            for n in names:
                if preferred in n.lower():
                    return n
    return names[0] if names else None


def _strict_test_plans(response):
    """Reject API error envelopes; an error cannot prove absence of live plans."""
    if isinstance(response, dict):
        if response.get("code") not in (None, 0, 200):
            raise ValueError("plan list error envelope")
        if "data" not in response:
            raise ValueError("plan list missing data")
        response = response["data"]
        if isinstance(response, dict):
            for key in ("items", "list", "plans"):
                if key in response:
                    response = response[key]
                    break
    if not isinstance(response, list) or any(not isinstance(plan, dict) or not plan.get("id")
                                             for plan in response):
        raise ValueError("invalid plan list")
    return response


def _ordered_plan_targets(name_to_id, cfg, account_snapshot):
    # Snapshot data changes order only; every account and sensitive write stays fresh.
    rows = {row.get("id"): row for row in (account_snapshot or [])
            if isinstance(row, dict) and row.get("id")}
    try:
        live = resolve_probe_policy(cfg)
    except Exception:
        live = None  # Unknown policy cannot authorize anything through ordering.

    def key(item):
        name, aid = item
        rank = 2
        row = rows.get(aid)
        if row is not None:
            try:
                base = account_probe_base_url(row)
                if probe_policy.normalize_host(base):
                    if probe_policy.permanently_denied(base):
                        rank = 0
                    elif live is not None:
                        rank = (3 if _truthy(live.get("auto_recover_enabled", True))
                                and probe_policy.probe_allowed(base, live) else 1)
            except (ValueError, RuntimeError):
                pass
        return rank, str(name), str(aid)

    return sorted(((name, aid) for name, aid in name_to_id.items() if aid), key=key)


def ensure_test_plans(api, name_to_id, cfg, dry_run=False, section_by_name=None,
                      account_snapshot=None):
    """Reconcile managed plans offline: explicit consent, weekly schedule, readback.

    Admin API reads/writes do not call the upstream. Revoked sites have existing
    plans disabled even when the global switch or auto recovery is off.
    """
    with _admin_calibration_scope(api, cfg, '计划校准', len(name_to_id)) as calibration:
        failures, reconciled = [], 0
        work = _ordered_plan_targets(name_to_id, cfg, account_snapshot)
        for index, (name, aid) in enumerate(work, 1):
            calibration.item(index, len(work), aid)
            try:
                calibration.check()
                account = _unwrap_one(api._call("GET", f"/api/v1/admin/accounts/{aid}"))
                if account.get("id") != aid or not _is_migrated(account):
                    raise ValueError("unmanaged account")
                credentials = account.get("credentials") or {}
                live = resolve_probe_policy(cfg)
                scope = _source_scope(live)
                if (scope is not None and account_source_scope(account) != scope
                        and _truthy(live.get("model_probe_enabled", False))):
                    continue
                allowed = (_truthy(live.get("auto_recover_enabled", True))
                           and probe_policy.probe_allowed(account_probe_base_url(account), live))
                model = _pick_probe_model(credentials.get("model_mapping"),
                                          account.get("platform"),
                                          (section_by_name or {}).get(name)) if allowed else None
                allowed = bool(allowed and model)
                diagnostic_problem = None
                if allowed:
                    try:
                        model = _native_scheduled_diagnostic_model(account, model, live)
                    except (ValueError, OSError) as error:
                        allowed = False
                        model = None
                        diagnostic_problem = str(error)
                offset = int(hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:4], 16)
                desired = {"enabled": allowed, "auto_recover": allowed,
                           "cron_expression": probe_policy.weekly_cron(
                               cfg.get("auto_recover_cron"), offset)}
                if model:
                    desired["model_id"] = model
                plans = _strict_test_plans(api.list_test_plans(aid))
                if not isinstance(plans, list) or any(not isinstance(p, dict) or not p.get("id")
                                                      for p in plans):
                    raise ValueError("invalid plans")
                if diagnostic_problem:
                    failures.append((name, "原生周检测需已启用并同步的安全 relay：" + diagnostic_problem))
                if dry_run:
                    reconciled += int(bool(plans or allowed))
                    continue
                if not plans and allowed:
                    try:
                        with _guarded_admin_write(_account_probe_write_guard(api, account, cfg, require_auto_recover=True)):
                            api.create_test_plan(dict(desired, account_id=aid))
                    except ProbeAuthorityRevoked:
                        continue
                    except ApiError as ex:
                        if ex.status != 409:
                            raise
                    # Creation conflict is not success: discover then reconcile.
                    plans = _strict_test_plans(api.list_test_plans(aid))
                    if not plans:
                        raise ValueError("creation not verified")
                for plan in plans:
                    if not plan.get("id"):
                        raise ValueError("plan id missing")
                    live = resolve_probe_policy(cfg)
                    if not (_truthy(live.get("auto_recover_enabled", True))
                            and probe_policy.probe_allowed(account_probe_base_url(account), live)
                            and (scope is None or (live.get("_current_source_scope", _policy_source_scope(live)) == scope
                                                   and account_source_scope(account) == scope))):
                        desired.update(enabled=False, auto_recover=False)
                    patch = {key: val for key, val in desired.items() if plan.get(key) != val}
                    if patch:
                        path = "/api/v1/admin/scheduled-test-plans/%d" % plan["id"]
                        if desired["enabled"] or desired["auto_recover"]:
                            try:
                                with _guarded_admin_write(_account_probe_write_guard(
                                        api, account, cfg, require_auto_recover=True, plan_id=plan["id"])):
                                    api._call("PUT", path, patch)
                            except ProbeAuthorityRevoked:
                                # A delayed enable may have been accepted despite a
                                # retryable response. Reconcile it to the safe state.
                                desired.update(enabled=False, auto_recover=False)
                                with _guarded_admin_write(_account_probe_write_guard(api, account, plan_id=plan["id"])):
                                    api._call("PUT", path, {"enabled": False, "auto_recover": False})
                        else:
                            with _guarded_admin_write(_account_probe_write_guard(api, account, plan_id=plan["id"])):
                                api._call("PUT", path, patch)
                if plans:
                    checked = _strict_test_plans(api.list_test_plans(aid))
                    by_id = {p.get("id"): p for p in checked if isinstance(p, dict)}
                    if any(any(by_id.get(p["id"], {}).get(k) != v for k, v in desired.items())
                           for p in plans):
                        raise ValueError("plan readback mismatch")
                    reconciled += 1
                calibration.record_success()
                calibration.progress("本项核查完成")
            except Exception as error:
                calibration.record_error(error)
                reason = _calibration_failure_reason(error, "探活计划校准失败，未能核实停用或周频状态")
                failures.append((name, reason))
                calibration.add_unverified(1)
                calibration.progress("失败：" + reason)
                if calibration.stopped:
                    remaining = work[index:]
                    failures.extend((name, calibration.stop_reason + "；未校准") for name, _ in remaining)
                    calibration.add_unverified(len(remaining))
                    calibration.progress("已停止，剩余 %d 项未校准" % len(remaining))
                    break
        return reconciled, failures


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
        if not _is_int(gid) or gid <= 0 or not _is_int(tid) or tid <= 0:
            fail.append((g, "分组 id 缺失，跳过降级设置"))
            continue
        try:
            current = _unwrap_one(api.get_group(gid))
            if current.get("id") != gid:
                raise ValueError("fallback group identity mismatch")
            value = current.get("fallback_group_id")
            if _is_int(value) and value == tid:
                continue
            if dry_run:
                ok += 1
                continue
            api.update_group(gid, {"fallback_group_id": tid})
            actual = _unwrap_one(api.get_group(gid))
            if actual.get("id") != gid or not _is_int(actual.get("fallback_group_id")) or actual["fallback_group_id"] != tid:
                raise ValueError("fallback field readback mismatch")
            ok += 1
        except ApiError as ex:
            fail.append((g, "降级分组同步失败：HTTP %s" % ex.status))
        except Exception:
            fail.append((g, "降级分组读取、更新或读回校验失败，未计成功"))
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


def _fetch_all_accounts(api, require_total=False):
    """翻页取回全部账号。返回 (账号列表, 出错信息或 None)。

    以 total 为准而不是"某页不足 page_size 就收工"：后者假设服务端严格返回
    请求的页大小，任何过滤或权限导致某页少返一条就会提前终止，账号取不全，
    去重和回滚都会漏。取不全时返回错误，不假装成功。
    """
    out, page, size, total = [], 1, 200, None
    while page <= 1000:
        try:
            d = api.list_accounts(page, size)
        except Exception:
            return out, "读账号列表失败，未取得完整快照"
        if not isinstance(d, dict) or d.get("code") not in (None, 0, 200) or "data" not in d:
            return out, "账号列表返回错误或缺少数据"
        data = d.get("data")
        if isinstance(data, list):
            items, page_total = data, None
        elif isinstance(data, dict):
            items = next((data[key] for key in ("items", "list", "accounts", "records")
                          if key in data), None)
            page_total = data.get("total")
        else:
            return out, "账号列表数据结构无效"
        if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
            return out, "账号列表包含无效项"
        if require_total and (not _is_int(page_total) or page_total < 0
                              or total is not None and page_total != total):
            return out, "破坏性操作需要完整且稳定的账号总数"
        if total is None and isinstance(page_total, int):
            total = page_total
        out.extend(items)
        if not items:
            break
        if total is not None and len(out) >= total:
            break
        if total is None and len(items) < size:
            break
        page += 1

    if total is not None and len(out) != total:
        return out, "账号列表取不全：服务端报 %d 条，实际拿到 %d 条" % (total, len(out))
    ids = [row.get("id") for row in out]
    if len(ids) != len(set(ids)):
        return out, "账号列表重复id，快照不完整"
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
    except OSError as ex:
        # 列不出本目录。价格工具会因此少几个候选来源 —— 不是致命错，
        # 但如果用户明明放了 .mhtml 却"没被识别"，这条是唯一的线索。
        log_exc("列举 %s 下的 .mhtml 失败" % HERE, ex)

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
    """Refresh reviewed, commit-pinned source evidence; never auto-upgrade."""
    root = os.path.join(OUT_DIR, "upstream-cache")
    msgs = []
    out = {}
    for kind, base_key in (("sub2api", "sub2api_repo"), ("cpa", "cpa_repo")):
        spec = upstream_contract.PINNED[kind]
        configured = (s.get(base_key) or DEFAULTS[base_key]).rstrip("/")
        legacy = "https://raw.githubusercontent.com/%s/main%s" % (
            spec["repo"], "/" + spec["prefix"] if spec["prefix"] else "")
        if configured not in (DEFAULTS[base_key], legacy):
            msgs.append("%s 非已核对来源；请先核对固定版本合同，不抓取可变源码" % kind)
            continue
        snapshot, warnings = upstream_contract.cached_or_refresh(
            root, kind, spec, _http, ttl=s.get("upstream_cache_ttl_seconds", upstream_contract.CACHE_TTL),
            offline=not _truthy(s.get("fetch_upstream_src", True)) or bool(s.get("_request_relay_preview")))
        msgs.extend("%s: %s" % (kind, warning) for warning in warnings)
        if snapshot:
            out[kind] = snapshot["root"]
            msgs.append("%s 固定合同 %s commit=%s extracted_at=%s；完整源码摘要已核对" % (
                kind, snapshot["tag"], snapshot["commit"], snapshot["extracted_at"]))
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

    # A source check explicitly authorized online is the automatic discovery
    # mode. Preview/import construction never calls this network path.
    if _truthy(s.get("fetch_upstream_src", True)) and not s.get("_request_relay_preview"):
        version_report, _ = upstream_contract.check_releases(
            os.path.join(OUT_DIR, "upstream-cache"), _http,
            ttl=s.get("upstream_cache_ttl_seconds", upstream_contract.CACHE_TTL))
        for kind, item in version_report["upstreams"].items():
            release = item.get("available_release") or item.get("last_available_release")
            if release:
                msgs.append("%s 最新可用版本证据 %s commit=%s%s；执行仍用固定合同 %s" % (
                    kind, release["tag"], release["commit"],
                    "（已过期/本次未确认）" if item.get("latest_evidence_stale") else "",
                    upstream_contract.PINNED[kind]["tag"]))
            msgs.extend("%s: %s" % (kind, warning) for warning in item.get("warnings", []))

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

    proofs = {"sub2api": upstream_contract.source_evidence(sub_src, "sub2api"),
              "cpa": upstream_contract.source_evidence(cpa_src, "cpa")}
    errors = ["%s 未核对源码合同：%s；禁止写入和删除" % (
        kind, "、".join(proof["unverified_files"])) for kind, proof in proofs.items()
        if proof["status"] == "review_required"]
    s["_upstream_contract_errors"] = errors
    s["_upstream_contract_evidence"] = proofs
    s["_runtime_cpa_src"] = cpa_src
    s["_runtime_sub2api_src"] = sub_src
    if errors:
        msgs.extend(errors)
        return False, msgs

    try:
        import extract as ext
        c = ext.get_constants(sub_src, cpa_src)
    except Exception as ex:
        msgs.append("常量提取失败（%s），改用内置常量。" % str(ex)[:120])
        return False, msgs

    _, capability_drift = cpa_capability_issues({}, dict(s, cpa_src=cpa_src))
    msgs.extend(capability_drift)
    for section, fields in (c.get("cpa_sections") or {}).items():
        added_fields = set(fields) - set(CPA_MANAGED_FIELDS.get(section, ()))
        if added_fields:
            msgs.append("CPA %s 新增字段尚未适配：%s；不能将源码字段清单当作运行支持" % (
                section, "、".join(sorted(added_fields))))

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
    except Exception as ex:
        # 漂移诊断失败 = "上游常量有没有变"这件事没被检查。
        # 以前静默，界面表现为"漂移检查跑了，无异常" —— 而实际是没跑。
        # 这类"把失败当成功"的静默是最误导人的一种。
        log_exc("上游漂移诊断失败（extract.diagnose），本次**未能**检查"
                "SECTION_MAP 等常量是否与上游一致", ex)

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
        except Exception as ex:
            # 这里失败意味着**没能校验 config.yaml 是否被改过**。
            # 计划可能已经过期，但校验器自己哑了 —— 以前静默通过，
            # 于是"按旧配置导入"这件事毫无提示。宁可出声。
            log_exc("校验 config.yaml 是否被改过时失败，本次未做该检查"
                    "（导入仍会继续，但计划可能已过期）", ex, level="warning")

    api = Sub2Api(s)
    stat = do_import(api, plan, ask=lambda q: input(q + " 输入 yes 继续：").strip().lower() == "yes")
    print_import_summary(stat)


def _tls_consumer_provenance(settings):
    from relay.server import verify_consumer_guard
    binary = settings.get("request_relay_consumer_bin") or os.environ.get(
        "REQUEST_RELAY_CONSUMER_BIN", "/usr/local/bin/cpa-consumer")
    return verify_consumer_guard(binary)


def _apply_learned_source_tls(records, document, settings, bundle=None, provenance=None):
    """Automatic parameters apply only to their still-current source contract."""
    if not settings.get("request_relay_learned_tls_profiles"):
        return
    import tls_diagnostic_transaction as tls
    provenance = provenance or _tls_consumer_provenance(settings)
    for record in records:
        rid = tls.route_id(record, record.get("_source_scope") or _source_scope(settings))
        if bundle is not None:
            route = bundle["routes"].get(rid)
            if route is None:
                continue
            models = route["models"]
        else:
            mapping = record_model_mapping(record)
            models = sorted(set(mapping.values()))
        profile = tls.learned_profile(record, document, settings, models, provenance)
        if profile is not None:
            record["_relay_tls"] = profile


def _tls_source_records(account, settings, bundle, provenance):
    import request_relay
    source, _ = fetch_cpa_text(settings)
    document = _yaml().safe_load(source)
    if not isinstance(document, dict):
        raise ValueError("TLS诊断当前来源配置无效")
    records, _ = collect(document)
    for record in records:
        record["_source_scope"] = _source_scope(settings)
    assign_names(records)
    selected, target = [], None
    profiles = settings.get("request_relay_tls_profiles") or {}
    for record in records:
        rid = request_relay.route_id(record, _source_scope(settings))
        route = bundle["routes"].get(rid)
        if route is None:
            continue
        if record.get("disabled") or serves_nothing(record):
            raise ValueError("TLS诊断来源路由已停用，先同步配置")
        record["_relay_models"] = list(route["models"])
        record["new_priority"] = record.get("priority", 0)
        if record.get("request_relay_tls") is not None:
            record["_relay_tls"] = copy.deepcopy(record["request_relay_tls"])
        host = probe_policy.normalize_host(record.get("base_url"))
        if host in profiles:
            record["_relay_tls"] = copy.deepcopy(profiles[host])
        selected.append(record)
        if record.get("name") == account.get("name") and account_matches_source(account, record):
            if target is not None:
                raise ValueError("TLS诊断来源身份不唯一")
            target = record
    if (target is None or len(selected) != len(bundle["routes"])):
        raise ValueError("TLS诊断完整来源路由合同发生变化，先同步配置")
    _apply_learned_source_tls(selected, document, settings, bundle, provenance)
    rebuilt = request_relay.compile_bundle(selected, document, bundle["source_scope"], bundle["state"],
        consumer_port=bundle["consumer_port"], weighted_bucket=bundle.get("weighted_bucket", False),
        transport_port=bundle["transport_port"])
    if rebuilt["generation"] != bundle["generation"]:
        raise ValueError("TLS诊断来源参数或模型发生变化，先同步配置")
    return document, selected, target


def diagnose_tls_fingerprint(api, targets, settings):
    """Compare two real source transports; never write sub2api account TLS flags."""
    import tls_diagnostic_transaction as tls
    learned, failures = set(), []
    for aid in sorted(set(targets)):
        try:
            live = resolve_probe_policy(settings)
            account = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
            base = account_probe_base_url(account)
            scope = _policy_source_scope(live)
            if scope is not None and account_source_scope(account) != scope:
                continue
            if not probe_policy.probe_allowed(base, live):
                continue
            if (account.get("id") != aid or not _is_migrated(account)
                    or account.get("status") != "active" or account.get("schedulable") is not True
                    or _recovery_runtime_block(account)):
                continue
            if "cpa2sub2api_original_source" not in (account.get("extra") or {}):
                raise ValueError("完整原生诊断需开启 REQUEST_RELAY_ENABLED 和 REQUEST_RELAY_FORCE_ALL_ACCOUNTS 后同步此站")
            reference, bundle, route, original = _relay_account_diagnostic_context(account, live)
            live = dict(live, request_relay=reference)
            provenance = _tls_consumer_provenance(live)
            document, records, record = _tls_source_records(account, live, bundle, provenance)
            model = _pick_probe_model((account.get("credentials") or {}).get("model_mapping"), account.get("platform"))
            if model not in route["models"]:
                model = (account.get("credentials") or {}).get("model_mapping", {}).get(model)
            _native_scheduled_diagnostic_model(account, model, live, automatic=False)
            expected_account = _recovery_snapshot(account)
            expected_source = tls.digest(document)
            def validate():
                fresh = _unwrap_one(api._call("GET", "/api/v1/admin/accounts/%d" % aid))
                current = resolve_probe_policy(settings)
                text, _ = fetch_cpa_text(current)
                current_document = _yaml().safe_load(text)
                if (_recovery_snapshot(fresh) != expected_account or account_source_scope(fresh) != scope
                        or _recovery_runtime_block(fresh) or not probe_policy.probe_allowed(base, current)
                        or _source_scope(current) != bundle["source_scope"]
                        or not _truthy(current.get("request_relay_enabled", False))
                        or _truthy(current.get("request_relay_weighted_bucket", False)) != bundle.get("weighted_bucket", False)
                        or _truthy(current.get("request_relay_force_all_accounts", False)) != bool(reference.get("force_all_accounts", False))
                        or current.get("request_relay_state_dir", DEFAULTS["request_relay_state_dir"]) != reference["state_dir"]
                        or current.get("request_relay_base_url", DEFAULTS["request_relay_base_url"]) != reference["base_url"]
                        or tls.digest(current_document) != expected_source):
                    raise ProbeAuthorityRevoked("TLS诊断授权、账号或当前来源发生变化")
            result = tls.compare_and_publish(records, document, record, model, live, bundle,
                SETTINGS_PATH, provenance, validate)
            if result["saved"]:
                learned.add(probe_policy.normalize_host(base))
            elif result["status"] == "comparison_failed":
                failures.append((aid, "两种真实源级TLS参数均未得到有效模型输出；主状态未更改"))
        except Exception:
            failures.append((aid, "源级TLS对照未获得可安全保留的证据；请核对当前来源与事务报告"))
    return learned, failures


def configured_probe_sites(settings):
    """Read only local configuration; listing/authorizing sites never sends HTTP."""
    sites = set(probe_policy.site_switches(settings))
    if os.path.isfile(CONFIG_YAML):
        with io.open(CONFIG_YAML, encoding="utf-8-sig") as stream:
            document = _yaml().safe_load(stream)
        if not isinstance(document, dict):
            raise ValueError("config.yaml 必须是映射")
        records, _ = collect(document)
        sites.update(probe_policy.normalize_host(row.get("base_url")) for row in records)
    return sorted(site for site in sites if site)


def managed_plan_targets(rows):
    """Include every managed ID, including duplicate names and stale accounts."""
    targets = {}
    for row in rows:
        if _is_migrated(row) and row.get("name") and row.get("id"):
            targets["%s#%s" % (row["name"], row["id"])] = row["id"]
    return targets


def probe_cli(arguments):
    """Offline per-site switches, plus explicit admin-only plan reconciliation."""
    import argparse
    parser = argparse.ArgumentParser(prog="tool.py probes")
    parser.add_argument("action", choices=("list", "enable", "disable", "enable-all", "disable-all",
                                           "master", "reconcile", "diagnose", "recover"), default="list", nargs="?")
    parser.add_argument("hosts", nargs="*")
    args = parser.parse_args(arguments)
    settings = current_probe_policy(load_settings())
    sites = configured_probe_sites(settings)
    if args.action in ("recover", "diagnose") and _truthy(settings.get("request_relay_enabled", False)):
        state_dir = settings.get("request_relay_state_dir") or DEFAULTS["request_relay_state_dir"]
        if os.path.isfile(os.path.join(state_dir, "CURRENT")):
            import request_relay
            settings["request_relay"] = _request_relay_reference(settings, request_relay.load_bundle(state_dir))
    if args.action == "recover":
        if not args.hosts or any(not value.isdigit() or int(value) <= 0 for value in args.hosts):
            parser.error("recover需要明确授权恢复的正整数账号id")
        ids = sorted({int(value) for value in args.hosts})
        settings["explicit_recovery_account_ids"] = ids
        settings["_explicit_error_recovery"] = True
        settings["revive_proven_inactive"] = True
        api = Sub2Api(settings)
        evidence, failures = probe_import_recovery(api, {str(aid): aid for aid in ids}, settings)
        active, _, active_error = reclaim_schedulable(api, evidence=evidence)
        inactive, _, inactive_error = revive_proven_inactive(api, settings, evidence=evidence)
        recovered_error, error_failures = _restore_error_from_job(api, evidence)
        done = active + inactive + recovered_error
        if error_failures:
            print("error恢复未完整核实 %d 个；可能已清错但调度仍关闭，请检查精确账号状态。" % error_failures)
        print("明确授权恢复 %d 个账号，已验证恢复 %d，跳过或失败 %d" % (len(ids), done, len(ids) - done))
        return int(done != len(ids) or bool(failures or active_error or inactive_error or error_failures))
    if args.action == "diagnose":
        selected = {probe_policy.normalize_host(h) for h in args.hosts}
        if not selected or any(not probe_policy.probe_allowed(h, settings) for h in selected):
            parser.error("diagnose需要一个或多个已开启总闸、站级授权且非永久禁探的域名")
        api = Sub2Api(settings)
        rows, error = _fetch_all_accounts(api)
        if error:
            print("账号列表不完整，未执行诊断")
            return 1
        targets = [row["id"] for row in rows if _is_migrated(row)
                   and probe_policy.normalize_host(account_probe_base_url(row))
                   in selected]
        learned, failures = diagnose_tls_fingerprint(api, targets, settings)
        print("源级TLS对照诊断自动保留参数 %d 个站点，失败 %d；sub2api原生账号客户端需独立验证。"
              % (len(learned), len(failures)))
        return int(bool(failures))
    if args.action == "reconcile":
        api = Sub2Api(settings)
        rows, error = _fetch_all_accounts(api)
        if error:
            print("账号列表不完整，未校准计划")
            return 1
        targets = managed_plan_targets(rows)
        count, failures = ensure_test_plans(api, targets, settings, account_snapshot=rows)
        print("已校准 %d 个受管账号计划，失败 %d" % (count, len(failures)))
        return int(bool(failures))
    if args.action == "master":
        if args.hosts not in (["on"], ["off"]):
            parser.error("master 需要 on 或 off")
        update_saved_settings(lambda saved: dict(saved, model_probe_enabled=args.hosts == ["on"]))
    elif args.action != "list":
        selected = sites if args.action.endswith("-all") else args.hosts
        if not selected:
            parser.error("需要一个或多个站点域名")
        enable = args.action.startswith("enable")
        refused = 0
        def update_switches(saved):
            nonlocal refused
            switches = probe_policy.site_switches(saved)
            targets = set(selected) | set(switches) if args.action.endswith("-all") else selected
            for value in targets:
                host = probe_policy.normalize_host(value)
                if not host or enable and probe_policy.permanently_denied(host):
                    refused += 1
                    continue
                switches[host] = enable
            saved["online_probe_hosts"] = switches
            return saved
        update_saved_settings(update_switches)
        print("更新站点授权，拒绝无效或永久禁探项 %d" % refused)
    if args.action != "list":
        settings = load_settings()
    master = _truthy(settings.get("model_probe_enabled", False))
    print("线上检测总闸：%s；环境初始默认：%s；独立硬禁用：%s" % (
        "开" if master else "关", os.environ.get("MODEL_PROBE_ENABLED", "未设置"),
        "开" if probe_policy.hard_disabled(settings) else "关"))
    for host in configured_probe_sites(settings):
        status = "永久禁止" if probe_policy.permanently_denied(host) else (
            "开" if probe_policy.site_switches(settings).get(host) else "关")
        print("[%s] %s（本次有效：%s）" % (
            status, host, "开" if probe_policy.probe_allowed(host, settings) else "关"))
    if args.action != "list":
        print("已保存。已有线上计划请执行 probes reconcile 立即停用/校准；该命令仅调用管理API。")
    return 0


def probe_menu():
    probe_cli(["list"])
    print("操作：enable 域名 / disable 域名 / enable-all / disable-all / master on / master off / reconcile")
    import shlex
    command = input("输入操作（空行返回）：").strip()
    if command:
        probe_cli(shlex.split(command))


def select_import_hosts(plan, hosts):
    """Keep full source context for priority and generation, narrow only mutations."""
    selected = set(hosts)
    available = {probe_policy.normalize_host(r.get("base_url")) for r in plan["accounts_raw"]}
    if (not selected or any(not isinstance(h, str) or h != probe_policy.normalize_host(h)
                            or h not in available for h in selected)):
        raise ValueError("--host requires exact domain names from the current source configuration")
    result = copy.deepcopy(plan)
    result["selected_hosts"] = sorted(selected)
    return result


def import_cli(arguments):
    import argparse
    parser = argparse.ArgumentParser(prog="tool.py import", description="从当前 CPA 配置添加或同步账号；单站与全量共用参数迁移")
    parser.add_argument("--host", action="append", default=[], help="只导入该域名；可重复；其余站点不会更新或删除")
    parser.add_argument("--preview", action="store_true", help="仅显示配置预演，不访问管理端、不执行检测")
    parser.add_argument("--yes", action="store_true", help="确认执行已列出的导入范围")
    args = parser.parse_args(arguments)
    settings = load_settings()
    # Import/preview is not an implicit diagnostic action. Explicit probes use
    # the existing per-host authorization commands and the same runtime payload.
    build_settings = dict(settings, model_probe_enabled=False, health_rerank_enabled=False,
                          _probe_execution_disabled=True, _request_relay_preview=args.preview)
    plan, _, _, warnings = build_plan(build_settings)
    if args.host:
        plan = select_import_hosts(plan, args.host)
    selected = set(plan.get("selected_hosts") or [])
    count = sum(not selected or probe_policy.normalize_host(r.get("base_url")) in selected
                for r in plan["accounts_raw"])
    print("导入预演：来源 %s，范围 %s，账号 %d 条。" % (
        plan["config"]["source_scope"], "、".join(sorted(selected)) if selected else "全量", count))
    print("新账号先添加到当前 CPA 配置，再执行同一导入；已有账号同步受管参数。")
    for warning in warnings:
        print("  %s" % warning)
    if args.preview:
        return 0
    # Offline plan construction must not erase the operator's execution policy.
    plan["config"]["health_rerank_enabled"] = settings.get("health_rerank_enabled", True)
    api = Sub2Api(settings)
    ask = None if args.yes else lambda prompt: input(prompt + " 输入 yes 继续：").strip().lower() == "yes"
    stat = do_import(api, plan, ask=ask)
    if stat.get("cancelled") or stat.get("aborted"):
        return 1
    return print_import_summary(stat)


def prune_cli(arguments):
    import argparse
    parser = argparse.ArgumentParser(prog="tool.py prune")
    parser.add_argument("action", choices=("preview", "apply", "claim-preview", "claim-apply"))
    parser.add_argument("ids", nargs="*", type=int)
    parser.add_argument("--yes", action="store_true", help="明确确认所列旧账号的来源认领")
    parser.add_argument("--evidence", help="可选：私有管理员单账号原文导出文件；只用于明确列出的一个 id")
    args = parser.parse_args(arguments)
    settings = load_settings()
    if args.action.startswith("claim-"):
        if not args.ids or any(aid <= 0 for aid in args.ids):
            parser.error("来源认领需要明确列出的正整数账号 id")
        evidence = None
        if args.evidence:
            if len(args.ids) != 1:
                parser.error("单账号可信导出证据只接受一个明确 id")
            try:
                with io.open(args.evidence, encoding="utf-8") as stream:
                    exported = _unwrap_one(json.load(stream))
                items = exported.get("accounts") if isinstance(exported, dict) else None
                if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                    raise ValueError("exact export required")
                evidence = {args.ids[0]: items[0]}
            except Exception:
                print("可信证据必须是私有管理员单账号原文导出；读取或结构校验失败。")
                return 1
        api, scope = Sub2Api(settings), _source_scope(settings)
        offline = dict(settings, model_probe_enabled=False, health_rerank_enabled=False)
        plan, _, _, _ = build_plan(offline)
        if plan.get("config_incomplete"):
            print("配置不完整，无法安全绑定旧账号；未认领。")
            return 1
        records = plan["accounts_raw"]
        preview, failures = claim_source_accounts(api, args.ids, scope, dry_run=True, records=records, evidence=evidence)
        print("旧受管账号来源认领预演：scope=%s，%d 条，失败 %d" % (scope, len(preview), len(failures)))
        for item in preview:
            print("  id=%s %s" % (item["id"], item["name"]))
        if args.action == "claim-preview" or failures:
            return int(bool(failures))
        if not args.yes and input("确认仅将这些旧受管账号认领到当前来源？输入 CLAIM：").strip() != "CLAIM":
            print("已取消。")
            return 1
        # Reload the source at the final write boundary; the preview includes
        # its identity digest and cannot consume a changed key/URL/protocol.
        refreshed, _, _, _ = build_plan(offline)
        if refreshed.get("config_incomplete"):
            print("配置发生变化或不完整，未认领。")
            return 1
        claimed, failures = claim_source_accounts(api, args.ids, scope, expected=preview,
                                                  records=refreshed["accounts_raw"], evidence=evidence)
        print("已核实认领 %d 条，失败 %d；未改变状态或调度开关。" % (len(claimed), len(failures)))
        return int(bool(failures))
    if args.ids or args.yes or args.evidence:
        parser.error("preview/apply 不接受账号 id、--yes 或 --evidence")
    # Pruning never authorizes diagnostics or upstream probes.
    settings["model_probe_enabled"] = False
    settings["health_rerank_enabled"] = False
    plan, _, _, _ = build_plan(settings)
    if plan.get("config_incomplete"):
        print("配置存在无效/丢弃条目，拒绝删除")
        return 1
    removed, failures = prune_stale_accounts(
        Sub2Api(settings), plan["accounts_raw"], dry_run=args.action == "preview")
    print("%s旧受管账号 %d 条，失败 %d 条" % (
        "拟删除" if args.action == "preview" else "已删除", len(removed), len(failures)))
    for item in removed:
        print("  id=%s %s" % (item["id"], item["name"]))
    return int(bool(failures))


def upstream_cli(arguments):
    """Read-only evidence inspection, or explicitly refresh upgrade candidates."""
    import argparse
    parser = argparse.ArgumentParser(prog="tool.py upstream")
    parser.add_argument("action", choices=("inspect", "refresh"))
    parser.add_argument("--kind", choices=("all", "cpa", "sub2api"), default="all")
    parser.add_argument("--latest", action="store_true", help="发现官方最新稳定版；只生成候选，绝不升级消费者")
    parser.add_argument("--offline", action="store_true", help="只读固定缓存，不联网")
    args = parser.parse_args(arguments)
    if args.latest and (args.action != "refresh" or args.offline):
        parser.error("--latest requires explicit online refresh")
    settings = load_settings()
    cache_root = os.path.join(OUT_DIR, "upstream-cache")
    report_path = os.path.join(cache_root, "release-evidence.json")
    ttl = settings.get("upstream_cache_ttl_seconds", upstream_contract.CACHE_TTL)
    kinds = ("cpa", "sub2api") if args.kind == "all" else (args.kind,)
    if args.latest:
        report, exit_code = upstream_contract.check_releases(
            cache_root, _http, kinds=kinds, ttl=ttl, force=True)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return exit_code
    report = {"automatic_upgrade": False, "checked_at": time.time(), "upstreams": {}}
    exit_code = 0
    for kind in kinds:
        spec = upstream_contract.PINNED[kind]
        if args.action == "inspect":
            snapshot = upstream_contract.load_snapshot(cache_root, kind, spec)
            warnings = []
        else:
            snapshot, warnings = upstream_contract.cached_or_refresh(cache_root, kind, spec, _http,
                ttl=ttl, offline=args.offline, force=not args.offline and not args.latest)
        item = {"pinned_tag": spec["tag"], "pinned_commit": spec["commit"],
                "source_cache": "available" if snapshot else "unavailable", "warnings": warnings}
        if snapshot:
            item.update(extracted_at=snapshot["extracted_at"], files=snapshot["files"],
                        source_cache_stale=time.time() - snapshot["extracted_at"] > ttl)
        elif args.action == "refresh":
            exit_code = 1
        report["upstreams"][kind] = item
    if args.action == "inspect":
        try:
            with open(report_path, encoding="utf-8") as stream:
                previous = json.load(stream)
            report["latest_available_evidence"] = previous
            report["latest_evidence_stale"] = time.time() - previous["checked_at"] > ttl
        except (OSError, ValueError, KeyError, TypeError):
            report["latest_available_evidence"] = None
    elif not args.offline:
        os.makedirs(cache_root, exist_ok=True)
        fd, pending = tempfile.mkstemp(prefix=".release-evidence-", dir=cache_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
            os.replace(pending, os.path.join(cache_root, "source-cache-evidence.json"))
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


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
        print("  [9] 每站线上检测开关 / 批量授权 / 周频计划校准")
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
            elif choice == "9":
                probe_menu()
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
        if len(sys.argv) > 1 and sys.argv[1] == "upstream":
            sys.exit(upstream_cli(sys.argv[2:]))
        elif len(sys.argv) > 1 and sys.argv[1] == "probes":
            sys.exit(probe_cli(sys.argv[2:]))
        elif len(sys.argv) > 1 and sys.argv[1] == "prune":
            sys.exit(prune_cli(sys.argv[2:]))
        elif len(sys.argv) > 1 and sys.argv[1] == "import":
            sys.exit(import_cli(sys.argv[2:]))
        else:
            menu()
    except KeyboardInterrupt:
        print("\n 退出。")
