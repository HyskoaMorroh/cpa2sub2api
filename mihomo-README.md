# CPA2SUB2API 的共享 Mihomo 代理

已有部署栈优先复用 upstream-importer 提供的 `mihomo-init` 和 `mihomo`。本工具以 `http://mihomo:7890` 访问共享服务，不另起同端口代理。sub2api、CPA和导入器应处于能解析该服务名的共同Docker网络。

本镜像也包含 mihomo 内核、地理数据与 `/app/mihomo-manager`，可供独立部署使用。初始化脚本在镜像内，VPS不需要上传此目录；运行配置由init生成到 `/root/.config/mihomo` 命名卷。

## 探测与代理是两件事

`proxy-url` 决定账号的业务请求出口；`FALLBACK_PROXY` 是本工具允许重试时的出口。代理不解除检测授权：默认 `MODEL_PROBE_ENABLED=false`、站授权为空，永久禁探站始终不能被检测。开启检测须同时满足总闸和站授权；详见[主README](README.md#管理每站线上检测)。

不能从“直连失败、换出口成功”推断所有KEY都有相同权限，也不能保证换代理能满足客户端专属校验或规避封号。


## request-relay 的代理路径

可选 relay 与导入器使用同一应用镜像，运行时由官方 CPA consumer 消费原 `proxy-url`。需要显式 TLS 预设的站通过固定目标 adapter 发送，保留每 KEY/全局的显式代理并启用证书校验；不会让宿主环境中的 `NO_PROXY` 悄悄绕过指定代理。relay 与共享 mihomo 必须在同一可互通网络中。

`REQUEST_RELAY_WEIGHTED_BUCKET=false` 默认绑定原 KEY。显式开启权重池只在同来源 scope、类型、分组和原主机内选路；池内另一 KEY 成功不能当作原 KEY 的检测证据。诊断使用 exact-key、零额外重试并即时核对原站授权。普通业务重试保留原显式策略。

`REQUEST_RELAY_TLS_PROFILES` 需要显式主机映射，例如 `{"upstream.example.invalid":{"impersonate":"chrome136"}}`；一个布尔 TLS 开关不能替代预设。代理或 TLS 预设都不证明拥有 Claude Code 客户端身份、签名或专属分组权限。完整启用、state/scope、迁移和回滚流程见[主 README](README.md#可选-request-relay让原-cpa-运行时消费参数)与[教程](tutorial.html#s6-relay)。

## 填写私有配置

部署根 `.env` 示例：

```dotenv
MIHOMO_SECRET=REPLACE_WITH_RANDOM_CONTROLLER_SECRET
MIHOMO_SUBSCRIPTIONS=primary=https://subscription.example/REPLACE_WITH_TOKEN
MIHOMO_AUTOMATIC_TESTS=false
FALLBACK_PROXY=http://mihomo:7890
MODEL_PROBE_ENABLED=false
ONLINE_PROBE_HOSTS=
```

也可使用 `MIHOMO_SUB_<名称>`，两种格式混用时具体优先级以初始化脚本输出为准。订阅URL、控制器密钥和真实站点信息不能提交。

`MIHOMO_AUTOMATIC_TESTS=false` 关闭 provider 节点自动健康检测与 url-test，原自动检测组转为 `select`，保留节点、过滤与规则。模型站的在线探测由另一个总闸和站级授权控制，两个开关不互相授权。

共享栈保留 upstream-importer 原 init 和 kernel 镜像，在两者之间加入本项目镜像的 `mihomo-policy-init`：原 init 完成 → policy-init 无网络原子处理已有共享配置 → 原 kernel 启动。policy-only 不覆盖原中立连通性健康脚本、数据或手工 fallback。恢复自动节点检测时要明确恢复/重建原自动组并设置 true，不能只改变量就认为 select 已还原。

已有父栈只更新需要的代理镜像/配置并按其compose启动。仓库独立部署模板才使用：

```bash
cd /opt/deploy
docker compose --profile mihomo up -d
```

不要把独立模板覆盖共享父栈。父栈的实际四文件是 `.env`、`config.yaml`、`docker-compose.yml`、`nginx.conf`；代理源文件由镜像提供。

## 验证代理本体与真实网络路径

```bash
docker compose ps mihomo mihomo-init
docker compose logs --tail 100 mihomo-init mihomo
docker compose exec mihomo sh /root/.config/mihomo/healthcheck.sh
```

检查控制器API时，`MIHOMO_SECRET` 必须已在当前终端安全提供；`.env`供compose使用，并不自动导出到宿主shell。以下命令仅在控制器绑定宿主回环9090时适用：

```bash
curl -fsS -H "Authorization: Bearer $MIHOMO_SECRET" \
  http://127.0.0.1:9090/version
curl -fsS -H "Authorization: Bearer $MIHOMO_SECRET" \
  http://127.0.0.1:9090/proxies/AUTO | jq '{now: .now, count: (.all|length)}'
```

`count: 0` 说明AUTO没有节点，需要检查订阅拉取与过滤规则；有节点也不代表节点实际可用。使用公共连通性端点验证出口，不要把永久禁探上游当作代理测试目标：

```bash
# 宿主机代理端口已映射到127.0.0.1:7890时
curl -fsS -m 15 -x http://127.0.0.1:7890 -o /dev/null \
  -w '%{http_code}\n' https://www.gstatic.com/generate_204

# 本工具是一次性作业，通常已退出，因此用run而非exec
docker compose run --rm cpa2sub2api \
  curl -fsS -m 15 -x http://mihomo:7890 -o /dev/null \
  -w '%{http_code}\n' https://www.gstatic.com/generate_204
```

还需在实际sub2api管理页检查绑定代理的账号以及IP管理页。宿主curl成功不能代替sub2api容器的真实请求路径；目标镜像缺curl/wget时，应使用该镜像已有工具或同网络诊断容器，不要把“命令不存在”误判为网络失败。

## 常见问题

| 现象 | 核查 |
|---|---|
| 控制器401 | init与代理本体的控制器secret是否一致；shell变量是否为空 |
| 服务名解析失败 | 客户端与mihomo是否同网络；跨compose的default不是同一网络 |
| IP管理页连接失败 | API鉴权、代理端口、allow-lan、订阅、AUTO节点、真实出口逐层检查 |
| AUTO有节点但出口超时 | 节点可用性、订阅过期、出口策略；节点计数不是成功证据 |
| unhealthy但不自动重启 | Docker不会仅因健康状态变差触发restart策略；查看健康脚本结果与进程状态 |
| 全站502/524 | 分开检查出口失败、上游模型/分组、网关协议和总重试预算 |

健康脚本可能把出口切换到DIRECT，恢复后再切回AUTO。这是有实际副作用的出口策略；DIRECT可用不证明指定站点可用，依赖固定出口的业务需要单独评估。

## CPA配置

```yaml
proxy-url: "http://mihomo:7890"
```

容器里的 `127.0.0.1` 是容器自己。代理与目标账号的映射、旧字段修正结果应以本次导入对照表和sub2api详情为准。

## 更新与安全

更改私有订阅后，使用当前部署镜像的init脚本重新生成命名卷配置，再重建代理本体。镜像入口与目录说明见 [mihomo-manager/README.md](mihomo-manager/README.md)。本机源码目录里的宿主脚本仅供本地开发，不是VPS必需文件。

控制器应设置强密钥；宿主映射端口保持回环绑定，不对公网开放。真实 `.env`、订阅清单、生成配置、providers缓存和日志均不提交。不要删除共享命名卷来“重装代理”，这可能破坏另一个项目的运行配置。
