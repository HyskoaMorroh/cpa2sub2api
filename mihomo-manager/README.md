# mihomo 订阅管理

为 cpa2sub2api 提供可切换的代理出口，用于绕过按 IP 拦截的上游站点。

本目录的实现与 `upstream-importer` 的 mihomo 部分**同源**，两边的
`bootstrap-mihomo.sh`、`update-mihomo-subscriptions.sh`、`config.template.yaml`、
`healthcheck.py` 保持一致；差异只在本仓库的 compose 服务名与卷名。

## 文件说明

| 文件 | 作用 |
|---|---|
| `bootstrap-mihomo.sh` | init 容器入口：把 `mihomo/config.template.yaml` 物化成 `config.yaml` |
| `update-mihomo-subscriptions.sh` | 宿主机运维脚本：改订阅、校验语法、重启、实测出网 |
| `mihomo-subscriptions.conf.example` | 订阅清单格式示例（真实清单不进版本库） |
| `mihomo/config.template.yaml` | 配置模板，只含 `${占位符}` |
| `mihomo/healthcheck.py` | 出口健康检查 + 失败自动降级到 DIRECT |

## 为什么需要自举

mihomo 官方镜像（`metacubex/mihomo`）基于 **scratch**：没有 shell、没有
`curl`/`wget`、没有 python。它自己**没法**把模板展开成配置。

所以由本项目的镜像充当 init 容器（有 python3 和 sh），启动时把模板物化到
**命名卷**，mihomo 容器挂同一个卷读它。顺序由 compose 保证：

```
mihomo-init（跑完即退） --service_completed_successfully--> mihomo（长驻）
```

这样部署保持"VPS 只拉镜像、宿主不预置任何文件"。

## 快速开始

### 1. 配置订阅

在**部署根目录的 `.env`** 里填订阅（推荐，容器部署用这个）：

```bash
MIHOMO_SECRET=<自己生成的随机串>
MIHOMO_SUBSCRIPTIONS=wog=https://机场订阅地址
```

或者用**每订阅一个变量**的写法：

```bash
MIHOMO_SUB_WOG=https://机场订阅地址
MIHOMO_SUB_WOGB=https://另一个订阅地址
```

两种写法可混用；`MIHOMO_SUB_<名称>` 优先。

### 2. 启动

```bash
cd /opt/deploy
docker compose --profile mihomo up -d
```

### 3. 验证（三步，缺一不可）

**① 进程与 API 是否活着**

```bash
docker compose ps mihomo
curl -fsS -H "Authorization: Bearer $MIHOMO_SECRET" \
  http://127.0.0.1:9090/version
```

**② AUTO 组里到底有没有节点** —— 这一步最容易漏，也最关键

```bash
curl -fsS -H "Authorization: Bearer $MIHOMO_SECRET" \
  http://127.0.0.1:9090/proxies/AUTO | jq '{now: .now, count: (.all|length)}'
```

`count: 0` 说明订阅没拉到、或被 AUTO 组的 `filter: "×1倍率"` 全筛掉了。
**mihomo 对这种情况不报错**（进程健康、端口在听、API 有响应），
表现就是"代理静默失效"——上游全部超时，但日志里看不出原因。

**③ 实际能不能出网**

```bash
curl -fsS -m 15 -x http://127.0.0.1:7890 -o /dev/null \
  -w 'egress OK: %{http_code}\n' https://www.gstatic.com/generate_204
```

**④ 从 sub2api 容器内部再验一次**（这才是"链接失败"的真实路径）

容器之间走 Docker 网络直连容器 IP、用服务名 `mihomo`，
**不经过宿主机端口映射**。所以宿主机 curl 通 ≠ 容器内通：

```bash
docker compose exec sub2api sh -c \
  'wget -q -T 5 -O /dev/null http://mihomo:7890 && echo "TCP OK"'
```

- 有响应 → 网络与准入没问题，问题在订阅/节点（回第 ② 步）
- 连不上 → 网络或 `allow-lan` 问题（见下）

## 常见故障

### "链接失败"（sub2api 的 IP 管理页）

按可能性排序：

1. **AUTO 组 0 节点**。进程健康但出不去。查第 ② 步，再看
   `filter: "×1倍率"` 是否与订阅里的节点倍率标记匹配。
2. **`allow-lan: false`**。mihomo 的语义是"只接受来自本机 127.0.0.1 的连接"，
   `bind-address: '*'` **不会**改变这一点（它只管 socket 绑哪个地址族）。
   容器间访问必须 `allow-lan: true`。本项目模板已设为 `true`。
3. **不在同一个 Docker 网络**。sub2api 与 mihomo 必须在同一 network 里，
   `http://mihomo:7890` 才能解析。跨 compose 项目的 `default` 网络是**不同的**网络。

### 健康检查一直 unhealthy

检查用的是 `python3 healthcheck.py`。**不要改成 `curl`**：
`metacubex/mihomo` 镜像里没有 curl，写 curl 会让检查恒定失败。

### 改完订阅不生效

```bash
bash mihomo-manager/update-mihomo-subscriptions.sh
```

脚本会重写 `proxy-providers` / 两个组的 `use` / 订阅域名的 DIRECT 规则，
跑 `mihomo -t` 校验语法（失败自动回滚），清 provider 缓存，重启容器，
然后逐订阅点名、查 AUTO 节点数、实测出网——任一项失败 `exit 1`。

## 添加 / 删除订阅

1. 改 `.env`（或 `mihomo-subscriptions.conf`）
2. `bash update-mihomo-subscriptions.sh`
3. 删除订阅时，可选清掉缓存：`rm mihomo-manager/providers/订阅名.yaml`

## 目录结构（容器内）

```
/app/mihomo-manager/                   镜像内，来自版本库
├── bootstrap-mihomo.sh
├── update-mihomo-subscriptions.sh
└── mihomo/
    ├── config.template.yaml
    └── healthcheck.py

/mihomo-config/                        命名卷 mihomo-config，运行时生成
├── config.yaml                        ← mihomo 读这个
├── healthcheck.py
└── providers/<订阅名>.yaml            ← mihomo 自己拉
```

## 与 CPA 的配合

CPA 的 `config.yaml` 里，需要走代理的上游条目写：

```yaml
proxy-url: "http://mihomo:7890"
```

**必须用服务名 `mihomo`**，不能写 `127.0.0.1` —— 容器内的 `127.0.0.1`
是容器自己，不是宿主机，也不是 mihomo。

## 安全提醒

- `mihomo-subscriptions.conf` 含订阅 token，属私密值，**不要提交**
  （`.gitignore` 已排除）。
- `MIHOMO_SECRET` 为空时 9090 API 无鉴权，同网络内任何容器都能切换出口、
  读出全部节点地址。生产环境务必设置。
- `allow-lan: true` 意味着同网络可访问代理端口。本项目只把端口绑在
  宿主回环（`127.0.0.1:7890`），外部无法直连。
