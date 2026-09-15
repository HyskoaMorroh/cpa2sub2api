# -*- coding: utf-8 -*-
"""一键导入：跳过菜单，按顺序自动完成全部步骤。

本文件只负责流程编排和提示，所有实际逻辑都在 tool.py 里。
以前这里复制了一份导入逻辑，两份代码各自漂移，结果是同一个 bug
在一边修好、另一边还在；现在统一走 tool.do_import。
"""
import io
import json
import os
import sys

# 必须在最开始就设置 UTF-8，防止后面 print 中文时炸。
# 用 reconfigure 而非新建 TextIOWrapper：新建会在被 run.py 调用时叠成两层
# 包装，任一层被回收即关掉底层 buffer，报 "I/O operation on closed file"。
for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tool import (
    TOOL_VERSION, NOTE_SENTINEL, OUT_DIR, PLAN_PATH, CONFIG_YAML,
    load_settings, save_settings, mask, Sub2Api, sync_constants,
    build_plan, write_plan_files, do_import, print_import_summary,
    health_check, purge, backup_accounts, wipe_all, act_open_out,
    act_pricing,
)

try:
    import yaml  # noqa: F401  仅确认依赖可用，实际解析在 tool.py 里
except ImportError:
    print("缺少 PyYAML，正在安装...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml"])


def ask_yes(prompt):
    """询问确认。非交互环境（管道、计划任务）读到 EOF 时一律当作"否"。

    破坏性操作在拿不到人的确认时必须默认不做，而不是崩掉或默认做。
    """
    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        print("(非交互环境，按否处理)")
        return False


def step_health(api):
    """体检并按需清理。只有真的查出脏数据才会问一次，干净时一路直通。"""
    print("[4/5] 体检...")
    rep, err = health_check(api)
    if err:
        print("  ✗ %s" % err)
        print("  账号列表取不全时不做清理，也不做去重，本次中止以免建出重复账号。")
        return False

    print("  现有账号 %d 条（本工具导入 %d，其他 %d）"
          % (rep["total"], rep["mine"], rep["others"]))

    # 老版本导入的账号：notes 里有 CPA 字样但没有本版的固定标记。
    # 它们多半是本工具早期版本建的，但"备注含 CPA"这个判据不够精确，
    # 所以单独问一次，并且明确说清楚判据是什么，让用户自己核对。
    if rep["legacy"]:
        print()
        print("  发现 %d 条**老版本**导入的账号：备注里有“源自 CPA”字样，"
              % len(rep["legacy"]))
        print("  但没有本版的 %s 标记。" % NOTE_SENTINEL)
        print("  这些多半是本工具早期版本建的（早期版本有 bug：账号没绑分组、"
              "重复跑会重复建）。")
        print("  但判据不如新标记精确，如果你手工建过备注含“源自 CPA”的账号，"
              "它也会在里面。")
        print("  示例：")
        for a in rep["legacy"][:5]:
            print("     %-34s %s" % (str(a.get("name"))[:34],
                                     str(a.get("notes") or "")[:40]))
        print()
        print("  删除不可撤销，sub2api 不返回 api_key，删掉的密钥拿不回来。")
        print("  跳过的话它们会留在库里，和本次新导入的账号并存。")
        if ask_yes("  输入 y 回车删除这 %d 条老账号，其他键保留：" % len(rep["legacy"])):
            bak = backup_accounts(rep["legacy"], "legacy")
            if bak:
                print("  快照已写到 %s" % os.path.basename(bak))
            aok, _p, fails = purge(api, rep["legacy"], [])
            print("  ✓ 删除 %d/%d 条" % (aok, len(rep["legacy"])))
            for f in fails[:5]:
                print("     ✗ %s" % f)
        else:
            print("  保留。")

    if not rep["accounts_dirty"] and not rep["proxies_dirty"]:
        print("  ✓ 数据干净，无需清理")
        return True

    # 代理重复和账号脏是两回事，分开处置。
    # 以前混在一起判断，结果"只多了一个重复代理"也会提示删掉全部账号。
    if rep["proxies_dirty"] and not rep["accounts_dirty"]:
        print("  发现 %d 个重复代理（同 host:port），账号本身是干净的。"
              % len(rep["dup_proxies"]))
        for p in rep["dup_proxies"][:5]:
            print("     id=%s %s://%s:%s" % (p.get("id"), p.get("protocol"),
                                             p.get("host"), p.get("port")))
        print("  只删多余的代理行，不动任何账号。")
        if ask_yes("  输入 y 回车删除重复代理，其他键跳过："):
            _a, pok, fails = purge(api, [], rep["dup_proxies"])
            print("  ✓ 删除代理 %d 个" % pok)
            for f in fails[:5]:
                print("     ✗ %s" % f)
        return True

    # 账号确实脏了
    n_dup = len(rep["dup_extra"])
    n_unbound = len(rep["unbound"])
    print("  发现需要清理的账号 %d 条：" % len(rep["bad_accounts"]))
    if n_dup:
        print("     重名多出来的副本 %d 条（%d 个名字有重复，每个名字保留最早那条）"
              % (n_dup, rep["dup_names"]))
    if n_unbound:
        print("     没绑分组的 %d 条（列表接口不返回 api_key，无法就地补绑，只能删了重导）"
              % n_unbound)
    if rep["dup_proxies"]:
        print("     另有 %d 个重复代理一并清理" % len(rep["dup_proxies"]))
    print()
    print("  将删除 %d 条账号和 %d 个代理；其余 %d 条账号和全部分组不动。"
          % (len(rep["bad_accounts"]), len(rep["dup_proxies"]),
             rep["total"] - len(rep["bad_accounts"])))
    print("  删除不可撤销，sub2api 不返回 api_key，删掉的密钥拿不回来。")
    print("  （删除前会把待删账号的快照写到 out/，但快照里没有密钥。）")

    if not ask_yes("  输入 y 回车开始清理，其他键跳过清理直接导入："):
        print("  跳过清理。已存在的账号会被跳过，脏数据保持原样。")
        return True

    bak = backup_accounts(rep["bad_accounts"], "purge")
    if bak:
        print("  快照已写到 %s" % os.path.basename(bak))
    print("  清理中...")
    aok, pok, fails = purge(api, rep["bad_accounts"], rep["dup_proxies"])
    print("  ✓ 删除账号 %d/%d、代理 %d/%d"
          % (aok, len(rep["bad_accounts"]), pok, len(rep["dup_proxies"])))
    if fails:
        print("  ! %d 项删除失败：" % len(fails))
        for f in fails[:5]:
            print("     %s" % f)
    return True


def ask_line(prompt):
    """读一行。非交互环境读到 EOF 返回空串，不抛异常。"""
    try:
        return input(prompt).strip()
    except EOFError:
        print("(非交互环境)")
        return ""


def act_wipe(api, s):
    """清空本工具建的账号、分组、代理。"""
    print()
    print("=" * 64)
    print(" 清空：删除本工具建的账号、分组、代理")
    print("=" * 64)
    wipe_all(api, s, ask=ask_yes, confirm=ask_line)


def act_setkey(s):
    """改 sub2api 地址和管理密钥，存回 设置.json。"""
    print()
    print("=" * 64)
    print(" 设置 sub2api 连接")
    print("=" * 64)
    print(" 直接回车 = 保留当前值")
    print()
    v = ask_line(" sub2api 地址 [%s]：" % s["sub2api_base_url"])
    if v:
        s["sub2api_base_url"] = v.rstrip("/")
    print(" 当前管理密钥：%s" % mask(s["sub2api_admin_key"]))
    print(" 提示：这是 admin- 开头的那个。在 VPS 上取：")
    print("   docker exec -i sub2api-postgres psql -U sub2api -d sub2api \\")
    print("     -tAc \"SELECT value FROM settings WHERE key='admin_api_key';\"")
    v = ask_line(" 粘贴管理密钥（回车跳过）：")
    if v:
        s["sub2api_admin_key"] = v
    save_settings(s)
    print(" 已保存到 设置.json")


def show_menu(s):
    """入口菜单。回车 = 直接导入，其余功能按数字选。"""
    print("=" * 64)
    print(" CPA -> sub2api 迁移工具  v%s" % TOOL_VERSION)
    print("=" * 64)
    print(" 数据源  : %s" % ("本地 config.yaml" if os.path.exists(CONFIG_YAML)
                             else "无 config.yaml，将走联网"))
    print(" 目标地址: %s" % s["sub2api_base_url"])
    print(" 管理密钥: %s" % mask(s["sub2api_admin_key"]))
    print("-" * 64)
    print("  [回车] 一键导入（建分组、建代理、导入账号）")
    print("    [2]  空跑预览（只生成对照表，不写 sub2api）")
    print("    [3]  全部清空（删账号 + 分组 + 代理，需二次确认）")
    print("    [4]  打开产出文件夹")
    print("    [5]  修改地址和管理密钥")
    print("    [6]  提交价格清单（给分组写逐模型定价）")
    print("    [7]  价格预演（只算差异，不写入）")
    print("    [0]  退出")
    print("-" * 64)
    return ask_line(" 直接回车开始导入，或输入数字：")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    s = load_settings()

    choice = show_menu(s)
    if choice == "0":
        print(" 再见。")
        return 0
    if choice == "4":
        act_open_out(s)
        return 0
    if choice == "5":
        act_setkey(s)
        return 0
    if choice in ("6", "7"):
        # 价格提交与导入是两件独立的事：价格写的是分组的 model_pricing，
        # 不碰账号；所以单独一个入口，不塞进一键导入的流水线里。
        act_pricing(s, dry_run=(choice == "7"))
        return 0
    dry_run = (choice == "2") or ("--dry-run" in sys.argv)
    wipe_mode = (choice == "3")
    if choice not in ("", "2", "3"):
        print(" 没有这个选项。")
        return 1

    print()

    # 常量同步：上游一旦往请求头黑名单加一项，不同步就会整批 400。
    ok, msgs = sync_constants(s, verbose=False)
    for m in msgs:
        print(" [常量同步] %s" % m)
    if msgs:
        print()

    # ======== 1：测试连接 ========
    print("[1/5] 测试 sub2api 连接...")
    api = Sub2Api(s)
    good, msg = api.ping()
    if not good:
        print("  ✗ %s" % msg)
        return 1
    print("  ✓ %s" % msg)
    print()

    # 清空模式：不需要读 config.yaml，直接干活
    if wipe_mode:
        act_wipe(api, s)
        return 0

    # ======== 2+3：读配置并转换 ========
    print("[2/5] 读取 CPA 配置...")
    try:
        plan, text, src, warns = build_plan(s)
    except Exception as ex:
        print("  ✗ %s" % ex)
        return 1
    recs = plan["accounts_raw"]
    cfg = plan["config"]
    print("  来源: %s" % src)
    print("  共 %d 行，CPA 调度策略 %s" % (len(text.splitlines()),
                                        cfg.get("routing_strategy")))
    print()

    print("[3/5] 解析并转换账号...")
    from collections import Counter
    for g, n in sorted(Counter(r["group"] for r in recs).items()):
        print("  %-10s %3d 条" % (g, n))
    print("  合计       %3d 条" % len(recs))
    print()
    md = write_plan_files(plan, text, s)
    print("  需建分组 %d 个：%s" % (len(plan["groups"]),
                                 "、".join(g["name"] for g in plan["groups"].values())))
    print("  需建代理 %d 个：%s" % (len(plan["proxies"]),
                                 "、".join(plan["proxies"]) or "无"))
    if plan["fingerprints"]:
        print("  ! TLS 指纹模板 %d 个无法迁移（sub2api 的指纹仅对 Anthropic OAuth 账号生效）："
              % len(plan["fingerprints"]))
        print("    %s" % "、".join(plan["fingerprints"]))
    if warns:
        print("  解析期提示 %d 条：" % len(warns))
        for w in warns[:5]:
            print("    - %s" % w)
        if len(warns) > 5:
            print("    ... 其余 %d 条" % (len(warns) - 5))
    print("  产出: import-plan.json、对照表.md")
    print()

    # ======== 4：体检并清理 ========
    # 空跑模式完全不碰清理：它的用途是"看看会生成什么"，不该问任何删除问题。
    if dry_run:
        print("[4/5] 体检... [空跑] 跳过")
    elif not step_health(api):
        return 1
    print()

    # ======== 5：导入 ========
    print("[5/5] 执行导入...")
    if dry_run:
        print("  [空跑] 只校验不写入。")
        from tool import to_account
        gm = {g: -1 for g in plan["groups"]}
        pm = {u: -1 for u in plan["proxies"]}
        bad = []
        for r in recs:
            a = to_account(r, cfg, gm, pm)
            if "status" in a:
                bad.append((a["name"], "含非法 status 字段"))
            if a.get("concurrency", 0) <= 0:
                bad.append((a["name"], "concurrency<=0 在 sub2api 里等于无并发上限"))
            if not a["credentials"].get("api_key"):
                bad.append((a["name"], "没有 api_key"))
        print("  校验 %d 条，问题 %d 个" % (len(recs), len(bad)))
        for n, w in bad[:10]:
            print("    ✗ %s：%s" % (n, w))
        return 1 if bad else 0

    # 写入生产前必须确认。删除要确认而写入不要，是说不过去的——
    # 一次误触就是几百条账号进生产库。
    print("  将向 %s 写入：" % s["sub2api_base_url"])
    print("    分组 %d 个、代理 %d 个、账号 %d 条"
          % (len(plan["groups"]), len(plan["proxies"]), len(recs)))
    if not ask_yes("  输入 y 回车开始导入，其他键取消："):
        print("  已取消，什么都没写。")
        return 0

    stat = do_import(api, plan, ask=None)
    rc = print_import_summary(stat)

    print()
    print("产出文件在：%s" % OUT_DIR)
    print("  - 对照表.md          逐账号核对表，建议抽查")
    print("  - import-record.json 本次导入结果")
    print("  - import-plan.json   含**明文密钥**")
    print("  - cpa-config-snapshot.yaml  含**明文密钥**")
    print()
    print("注意：后两个文件是明文凭据，用完请删除，不要上传。")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
    except Exception as ex:
        # 网络类故障单独给人话提示：Traceback 对使用者没有价值，
        # 而「重试三次仍失败」这件事本身才是需要知道的信息。
        txt = str(ex)
        netish = any(k in txt for k in (
            "连接失败", "Remote end closed", "timed out", "TimeoutError",
            "Connection", "URLError", "RemoteDisconnected"))
        if netish:
            print("\n[网络错误] %s" % txt)
            print("  已自动重试 3 次仍未成功。常见原因：")
            print("   · 服务端或中间的 Cloudflare/nginx 偶发断连——稍后重跑即可")
            print("   · 目标地址不对：当前是 %s" % load_settings()["sub2api_base_url"])
            print("   · 本机网络/代理不通")
            print("  没有任何数据被修改。")
            sys.exit(1)
        print("\n[致命错误] %s" % ex)
        import traceback
        traceback.print_exc()
        sys.exit(1)
