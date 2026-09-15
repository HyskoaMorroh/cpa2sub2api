# CPA2SUB2API 的 Mihomo 代理配置指南

## 为什么需要代理

部分上游站点按**出口 IP** 拦截（Cloudflare 人机验证、直接 403）。
本工具对这类站点的处理是**兜底代理**：

- 直连成功 → 不走代理
- 直连失败（连接层错误 / 429 / 5xx）→ 自动经代理重试一次

判据是"本机出口 IP 有没有被拦"，不取决于域名是谁 —— 所以由你在 CPA 里
为**哪些条目**配了 `proxy-url` 来决定谁需要代理。

---

## 方式 1：与 cpa2sub2api 联动部署（推荐，VPS 用这个）

本仓库已内置完整的 mihomo 实现（与 upstream-importer 同源），走
compose profile，不需要从别处复制文件。

### 1. 在部署根目录的 `.env` 里配置

```bash
MIHOMO_SECRET=<自己生成的随机串>
MIHOMO_SUBSCRIPTIONS=wog=https://机场订阅地址
FALLBACK_PROXY=http://mihomo:7890
```

### 2. 启动

```bash
cd /opt/deploy
docker compose --profile mihomo up -d
```

`mihomo-init` 会先把配置物化到命名卷，跑完即退，然后 mihomo 才启动。

### 3. 验证

```bash
# 出网是否正常
curl -fsS -m 15 -x http://127.0.0.1:7890 -o /dev/null \
  -w 'egress OK: %{http_code}\n' https://www.gstatic.com/generate_204

# AUTO 组有没有节点（为 0 就是静默失效）
curl -fsS -H "Authorization: Bearer $MIHOMO_SECRET" \
  http://127.0.0.1:9090/proxies/AUTO | jq '.all|length'
```

详细的故障判定见 [mihomo-manager/README.md](mihomo-manager/README.md)。

---

## 方式 2：本地开发（不用 Docker）

```bash
cd mihomo-manager
MIHOMO_TARGET_DIR=../mihomo-config \
MIHOMO_SECRET=devsecret \
MIHOMO_SUB_WOG="https://你的订阅" \
sh bootstrap-mihomo.sh

# 生成好配置后，用 mihomo 二进制指定目录启动
mihomo -d ../mihomo-config
```

---

## 怎么确认代理兜底真的生效了

### 关键误区

**容器部署下不要配 `设置.json`。** 容器的配置走环境变量
（`FALLBACK_PROXY`），`load_settings()` 会把它映射成 `fallback_proxy`。
把地址写成 `127.0.0.1:7890` 也是错的 —— 容器里的 `127.0.0.1`
是容器自己，不是宿主机。**容器间必须用服务名 `mihomo`。**

### 正确的配置

| 部署方式 | 配在哪 | 值 |
|---|---|---|
| Docker | `.env` 的 `FALLBACK_PROXY` | `http://mihomo:7890` |
| 本机直跑 | `设置.json` 的 `fallback_proxy` | `http://127.0.0.1:7890` |

### 验证

本工具**没有**单独的"探测某个站点"子命令（曾经文档里写过
`python tool.py detect --name ...`，那个命令不存在）。要验证兜底链路，
看批量流程的实际输出：走代理成功时日志里会出现"经代理"字样。

也可以直接测代理本身：

```bash
# 宿主机
curl -fsS -m 15 -x http://127.0.0.1:7890 -o /dev/null \
  -w '%{http_code}\n' https://www.gstatic.com/generate_204

# 从容器内部（这才是工具真实使用的路径）
docker compose exec cpa2sub2api \
  curl -fsS -m 15 -x http://mihomo:7890 -o /dev/null \
  -w '%{http_code}\n' https://www.gstatic.com/generate_204
```

---

## CPA 侧怎么指定哪些站点走代理

在 CPA 的 `config.yaml` 里给条目加：

```yaml
proxy-url: "http://mihomo:7890"
```

本工具会据此为对应账号绑定代理（`proxy_id`），并让**同域名**的其它条目
自动跟进（同一个站的其它 KEY 出口 IP 一样，拦不拦是同一个答案）。

---

## 常见问题

### Q1: sub2api 的 IP 管理页显示"链接失败"

按可能性排序：

1. **AUTO 组 0 节点**：mihomo 进程健康、端口在听，但出不去。
   `curl .../proxies/AUTO | jq '.all|length'` 为 0 即是。
   查订阅是否拉到、`filter: "×1倍率"` 是否把节点全筛掉。
   **mihomo 对这种情况不报错**，只表现为上游全部超时。
2. **`allow-lan: false`**：mihomo 只接受来自 127.0.0.1 的连接，
   容器间访问会被拒。本项目模板已是 `true`。
3. **不在同一 Docker 网络**：`http://mihomo:7890` 解析不到。
   跨 compose 项目的 `default` 网络是不同的网络。

### Q2: 代理不生效

1. `.env` 里有没有 `FALLBACK_PROXY=http://mihomo:7890`（不是 127.0.0.1）
2. `docker compose ps` 里 mihomo 是否在跑
3. 端口是否在听：`docker compose port mihomo 7890`
4. 该条上游在 CPA 里有没有 `proxy-url`

### Q3: 部分站点仍然 503

- 代理节点也被该站拦截（换节点）
- 订阅过期（更新订阅）
- 上游本身的问题，与代理无关

换节点：

```bash
curl -X PUT -H "Authorization: Bearer $MIHOMO_SECRET" \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:9090/proxies/PROXY \
  -d '{"name":"节点名"}'
```

### Q4: 想强制某站走代理（不管直连通不通）

直接给该条目配 `proxy-url`。只要配了，本工具就会为它绑定代理，
不走"先直连失败再兜底"这条路。

---

## 安全提示

1. **端口**：只绑宿主回环（`127.0.0.1:7890`），不对外暴露。
2. **API 鉴权**：务必设 `MIHOMO_SECRET`。为空时同网络内任何容器都能
   切换出口、读出全部节点地址。
3. **订阅 token**：`mihomo-subscriptions.conf` 含私密 token，
   `.gitignore` 已排除，不要提交。

---

## 参考文档

- 本仓库 mihomo 实现：[mihomo-manager/README.md](mihomo-manager/README.md)
- mihomo 官方文档：https://wiki.metacubex.one/
