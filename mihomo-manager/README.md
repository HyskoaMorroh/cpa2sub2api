# Mihomo 初始化与运行资产

本目录随 CPA2SUB2API 镜像发布。VPS只拉镜像，不需要上传本目录。已有共享栈优先继续使用 upstream-importer 的 mihomo服务，本工具通过共同Docker网络复用它。

## 目录与入口

| 路径 | 用途 |
|---|---|
| `/app/mihomo-manager/bootstrap-mihomo.sh` | init入口，读取私有环境变量并生成运行配置 |
| `/app/mihomo-manager/mihomo/config.template.yaml` | 镜像内配置模板 |
| `/app/mihomo-manager/mihomo/healthcheck.sh` | 基于shell工具的出口检查与降级策略 |
| `/app/mihomo-manager/mihomo/healthcheck.py` | Python实现，只有目标镜像具备Python时才可运行 |
| `/usr/local/bin/mihomo` | 镜像内代理内核 |
| `/root/.config/mihomo` | init与代理本体共享的命名卷，包含生成配置和providers数据 |

初始化入口：

```bash
/bin/sh /app/mihomo-manager/bootstrap-mihomo.sh
```

代理本体读取同一卷：

```bash
mihomo -d /root/.config/mihomo
```

`mihomo-init` 完成后退出；`mihomo` 常驻。它们必须使用兼容资产、相同配置卷和控制器密钥。不要依赖其他镜像必然带shell、Python或curl，按实际镜像核实。

## 配置步骤

1. 在部署根私有 `.env` 填写 `MIHOMO_SECRET` 和订阅。
2. 检查父compose的init与代理本体是否挂同一命名卷，并与sub2api/导入器共享网络。
3. 按该compose重建init，再启动代理本体。
4. 检查init日志、控制器鉴权、AUTO节点数和真实出口。

```dotenv
MIHOMO_SECRET=REPLACE_WITH_RANDOM_CONTROLLER_SECRET
MIHOMO_SUBSCRIPTIONS=primary=https://subscription.example/REPLACE_WITH_TOKEN
FALLBACK_PROXY=http://mihomo:7890
```

支持的逐订阅环境变量使用 `MIHOMO_SUB_<名称>` 格式，例如 `MIHOMO_SUB_PRIMARY`。生成配置和订阅token只留运行卷，不能提交仓库或打入镜像层。

已有共享父栈按自身服务定义执行，独立模板才使用mihomo profile：

```bash
cd /opt/deploy
docker compose --profile mihomo up -d
```

## 验证与排障

```bash
docker compose ps mihomo mihomo-init
docker compose logs --tail 100 mihomo-init mihomo
docker compose exec mihomo sh /root/.config/mihomo/healthcheck.sh
```

健康脚本会测试出口，并可能切换PROXY组到DIRECT或AUTO，手工运行具有实际副作用。API存活、AUTO有节点、公共端点出口可用、指定业务可用是四个不同结论，不能互相替代。

出现“连接失败”时依次检查：

- 控制器地址、端口和Bearer secret；init/代理本体/调用方凭据一致。
- 共同Docker网络和服务名；容器内 `127.0.0.1` 不指向其他服务。
- `allow-lan` 与绑定地址，确保需要的容器可访问代理。
- 订阅是否拉取成功、过滤条件是否排空AUTO组、节点是否实际可用。
- 当前健康检查使用的工具是否存在；命令缺失不是出口不通。

Docker不会仅因`unhealthy`自动执行restart策略。若代理进程活着但无出口，应检查健康脚本的降级结果；不能靠不断重启替代根因排查。

## 更新订阅

修改私有 `.env` 后，按部署compose重新运行init并重建代理本体；运行配置仍保留在命名卷。VPS不需要下载本目录里的宿主维护脚本。`update-mihomo-subscriptions.sh`供持有源码的本地维护场景使用，运行前阅读其参数和实际目标，避免改错共享配置。

不要删除共享卷清理问题，不要把 `.env`、订阅清单、config.yaml、providers缓存或日志纳入Git。

## 探测策略

代理出口检查不等于允许探测上游账号。本工具默认总闸关闭、站授权为空，永久禁探规则始终生效；代理重试不能绕过。开关、周频计划与持久化说明见 [主README](../README.md#管理每站线上检测)，完整步骤见 [图文教程](../tutorial.html)。
