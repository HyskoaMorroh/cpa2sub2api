#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能优先级分配模块 - 基于 sub2api 实际运行状态

核心改进：
1. 不再盲目复制 config.yaml 的优先级
2. 从 sub2api 读取实际运行状态（status, schedulable）
3. 计算健康分数：可调度比例*60% + 活跃比例*40%
4. 按健康分数降序分配优先级：健康度高 → priority 小
5. 同域名所有 KEY 保持相同 priority

健康分数分层：
- S (>=90): priority 10-50   (优秀)
- A (>=70): priority 60-100  (良好)
- B (>=50): priority 110-150 (中等)
- C (>=30): priority 160-200 (较差)
- D (>0):   priority 210-250 (很差)
- F (=0):   priority 260+    (完全不可用)
"""

from collections import defaultdict
from urllib.parse import urlparse


def _fetch_runtime_accounts(sub2api_client):
    """翻页取回全部账号，用于统计运行状态。

    为什么要自己翻页：账号列表接口是**分页**的，且响应体形如
    {"data": {"items": [...], "total": N}}。以前这里是
    `sub2api_client._call('GET', '/api/v1/admin/accounts')` —— 既没带
    page/page_size，取字段又是 `resp['items']`（真实位置是 resp['data']['items']），
    两条都错。结果是**静默**拿到空列表：异常没抛，下面按空集算健康度，
    所有域名的 score 都是 0，全被分到同一档，智能优先级完全失效却不报错。
    这正是"智能优先级看起来在跑、实际没生效"的根源。

    以 total 为准而不是"某页不足就收工"：后者假设服务端严格按请求页大小返回，
    任何过滤或权限导致某页少返一条就会提前终止，统计口径随之失真。
    """
    out, page, size, total = [], 1, 200, None
    while page <= 1000:
        try:
            d = sub2api_client.list_accounts(page, size)
        except Exception as e:
            raise RuntimeError("读账号列表失败（第 %d 页）：%s" % (page, e))
        data = d.get("data")
        if isinstance(data, list):
            items, page_total = data, None
        elif isinstance(data, dict):
            items = data.get("items") or []
            page_total = data.get("total")
        else:
            items, page_total = [], None
        if not isinstance(items, list):
            items = []
        out.extend(items)
        if total is None and page_total is not None:
            try:
                total = int(page_total)
            except (TypeError, ValueError):
                total = None
        if total is not None and len(out) >= total:
            break
        if len(items) < size:
            break
        page += 1
    return out


def remap_priority_smart(recs, sub2api_client, host_of_func, account_fingerprint_func, is_int_func, host_tier_map_func, host_key_func):
    """基于 sub2api 实际运行状态的智能优先级分配。

    Args:
        recs: 导入记录列表
        sub2api_client: Sub2Api 客户端实例
        host_of_func: host_of() 函数引用
        account_fingerprint_func: account_fingerprint() 函数引用
        is_int_func: _is_int() 函数引用
        host_tier_map_func: _host_tier_map() 函数引用
        host_key_func: _host_key() 函数引用

    Returns:
        bucket_of: 优先级映射字典
    """

    # 第一步：从 sub2api 获取实际运行状态（翻页取全量）
    try:
        accounts = _fetch_runtime_accounts(sub2api_client)
        print(f"[智能优先级] 获取了 {len(accounts)} 个账号的运行状态")
        if not accounts:
            print("警告：sub2api 侧暂无账号（首次导入），改用传统映射")
            return remap_priority_legacy(recs, host_of_func, account_fingerprint_func, is_int_func, host_tier_map_func, host_key_func)
    except Exception as e:
        print(f"警告：无法获取 sub2api 运行状态，回退到原始逻辑: {e}")
        return remap_priority_legacy(recs, host_of_func, account_fingerprint_func, is_int_func, host_tier_map_func, host_key_func)

    # 第二步：按 (platform, domain) 聚合统计
    runtime_stats = {}
    for acc in accounts:
        platform = acc.get('platform', '')
        creds = acc.get('credentials', {})
        base_url = creds.get('base_url', '') if isinstance(creds, dict) else ''

        if not base_url:
            continue

        # 提取域名
        try:
            parsed = urlparse(base_url)
            domain = parsed.netloc if parsed.netloc else base_url.replace('https://', '').replace('http://', '').split('/')[0]
        except:
            domain = base_url.replace('https://', '').replace('http://', '').split('/')[0]

        key = f'{platform}:{domain}'
        if key not in runtime_stats:
            runtime_stats[key] = {'total': 0, 'active': 0, 'error': 0, 'schedulable': 0, 'not_schedulable': 0}

        status = acc.get('status', '')
        schedulable = acc.get('schedulable', False)

        runtime_stats[key]['total'] += 1
        if status == 'active':
            runtime_stats[key]['active'] += 1
        elif status == 'error':
            runtime_stats[key]['error'] += 1

        if schedulable:
            runtime_stats[key]['schedulable'] += 1
        else:
            runtime_stats[key]['not_schedulable'] += 1

    # 第三步：计算健康分数
    health_scores = {}
    for key, stats in runtime_stats.items():
        if stats['total'] > 0:
            schedulable_ratio = stats['schedulable'] / stats['total']
            active_ratio = stats['active'] / stats['total']
            health_scores[key] = schedulable_ratio * 60 + active_ratio * 40
        else:
            health_scores[key] = 0

    print(f"[智能优先级] 计算了 {len(health_scores)} 个域名的健康分数")

    # 第四步：按健康分数分层并分配优先级
    # 对 recs 按域名分组，取每组的健康分数
    from_records = {}  # (platform, domain) -> list of records
    for r in recs:
        h = host_of_func(r)
        k = host_key_func(r)
        if k not in from_records:
            from_records[k] = []
        from_records[k].append(r)

    # 给每个域名分配优先级
    domain_priorities = {}
    for k in from_records.keys():
        score = health_scores.get(k, 0)  # 默认 0 分（未找到运行状态）

        # 分层映射
        if score >= 90:
            base = 10
            tier_range = 40
        elif score >= 70:
            base = 60
            tier_range = 40
        elif score >= 50:
            base = 110
            tier_range = 40
        elif score >= 30:
            base = 160
            tier_range = 40
        elif score > 0:
            base = 210
            tier_range = 40
        else:  # score == 0
            base = 260
            tier_range = 100

        # 在层级内按分数细分
        if tier_range > 0 and score > 0:
            offset = int((1.0 - (score % (100 / 6)) / (100 / 6)) * tier_range)
            priority = base + offset
        else:
            priority = base

        domain_priorities[k] = priority

    print(f"[智能优先级] 分配了 {len(domain_priorities)} 个域名的优先级")

    # 第五步：构建最终映射
    #
    # 这里返回的是 **域名单元 -> 桶号** 的映射，而不是旧版的
    # "原始 CPA priority 值 -> 桶号"。
    #
    # 为什么要改：旧版 `bucket_of[int(orig_priority)] = domain_priorities[k]`
    # 是按**序位值**建表。两个不同渠道的不同域名，只要在 CPA 里被配成同一个
    # priority（很常见，比如都写 1000），就会算出同一个 tier，
    # 后写入的覆盖先写入的 —— 两个域名拿到同一个桶号，被 sub2api 当成同一桶
    # 轮循，跨域名降级容灾失效。文档第 5 条要求"全局 dense rank 映射"、
    # "同一类型不同域名的优先级一定要不同"，所以必须按域名单元建表。
    #
    # 同时这里做全局 dense rank：按健康分降序编号（步长 10），
    # 用渠道名 + 域名打破并列，保证同一份配置多次运行得到相同的桶号。
    all_units = sorted(domain_priorities.keys(),
                       key=lambda k: (-health_scores.get(k, 0), str(k[0]), str(k[1])))
    bucket_of = {}
    for i, k in enumerate(all_units, 1):
        bucket_of[k] = i * 10

    return bucket_of


def remap_priority_legacy(recs, host_of_func, account_fingerprint_func, is_int_func, host_tier_map_func, host_key_func):
    """原始优先级映射逻辑（盲目复制 config.yaml）。

    仅作为 API 查询失败或首次导入时的回退方案。
    同样按**全局 dense rank** 编号：把各域名单元的 CPA 序位去重后降序编号，
    而不是按序位值本身建表（那会跨渠道撞桶）。
    """
    tier_of = host_tier_map_func(recs)
    # 每个域名单元一个桶号，按该单元的 CPA 序位降序；同序位用域名打破并列
    units = sorted(tier_of.keys(),
                   key=lambda k: (-tier_of.get(k, 0), str(k[0]), str(k[1])))
    bucket_of = {}
    for i, k in enumerate(units, 1):
        bucket_of[k] = i * 10

    print(f"[回退逻辑] 使用原始优先级映射，共 {len(bucket_of)} 个域名单元")
    return bucket_of

