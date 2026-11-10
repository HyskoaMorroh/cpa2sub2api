# CPA2SUB2API

把 CPA / CPAMP 生成的 `config.yaml` 转为 sub2api 账号，批量创建、校正已有参数、整理模型和优先级，并清理本工具受管的陈旧账号。

部署流程：本机修改与验证 → GitHub Actions 构建 Docker Hub 镜像 → VPS 拉镜像。VPS 不需要项目源码或 `mihomo-manager` 宿主目录。

- [图文教程](tutorial.html)：部署图、逐字段合同、全量/单站导入、Web 开关、request-relay、代理、迁移回滚与排障；代码块带复制按钮。
- [环境变量模板](.env.example) / [CPA 输入示例](config.yaml.example)
- [代理说明](mihomo-README.md) / [代理脚本说明](mihomo-manager/README.md)

## 先了解默认行为

**默认只做离线配置分析和 sub2api 管理操作，不向上游站点发送探测请求。** 导入会访问你配置的 sub2api 管理 API；启用远程 CPA 配置或模型目录刷新时，还会访问相应配置来源。这些管理/目录请求与上游模型探活是不同操作。

管理 API 返回 429 时，按 `Retry-After` 的秒数或 HTTP 日期等待；缺失或无效时等待 60 秒，最多额外重试两次。任一次要求等待超过 120 秒就明确失败，不缩短服务器冷却时间；其他 4xx 不重试。

线上探测需要同时满足：

1. 保存的总开关开启；未保存时才使用 `MODEL_PROBE_ENABLED`，默认false。
2. 对应站点已单独授权。
3. 站点不在内置永久禁探名单中。

站点授权默认全部关闭。已保存的总闸true/false均优先于 `MODEL_PROBE_ENABLED` 默认值，环境true不能复活保存的false。独立 `MODEL_PROBE_HARD_DISABLED=true` 始终禁止provider检测，拒绝开启新授权，仍允许关闭撤权。批量开启也不能解除永久禁探限制。普通导入、代理失败重试、模型探测和恢复计划均应遵守同一策略。**改变提示词、客户端请求头或使用代理不能保证规避站方限制或封号。**

已授权站点的恢复计划默认为每周一次：`AUTO_RECOVER_CRON=0 3 * * 0`，并可按账号错开分钟。执行时间按目标调度器时区解释；请在 sub2api 中核对计划的下次执行时间。旧半小时计划也需要协调停用或迁移，不能只修改环境变量。

自动检测另有按规范化主机共享的 **604800 秒周预算**：默认导入入口的自动批次与 sub2api 原生周计划共用同一账本，更换 KEY、模型或运行代际不会重新放行。预算在网络请求前持久化，失败或中断也计入该周；未到期返回受控跳过、上游请求数为零，不把健康账号改成错误。`MODEL_PROBE_CACHE_TTL=604800` 管理结果缓存，缓存过期或最高模型上次失败都不能绕过周预算。显式 `diagnose` / `recover` 使用独立手工诊断路径，不占自动周预算，仍逐次核对总闸、站授权和永久禁探规则。

## 功能与边界

| 功能 | 行为 |
|---|---|
| 完整参数同步 | 新增与已有账号统一处理 credentials、模型、优先级、并发、代理、请求头、冷却规则及目标版本支持的字段；检查陈旧值 |
| 来源优先级 | 保留每条凭据的显式 CPA priority 顺序与并列关系，以全局反向 dense rank 转成 sub2api 的小值优先；不按域名或健康分覆盖已确认的来源意图 |
| 健康辅助 | 使用已有账号的可调度和活跃状态计算辅助负载参数；健康评分不改写来源 priority，也不证明某个 KEY 已恢复 |
| 模型与别名 | 显式 models 保留完整列表与 name/alias 关系，按客户端 alias 应用 excluded-models；仅缺少显式列表时采用按来源家族就高的回退规则 |
| 授权模型检测 | 缺省家族回退可在最高系列失败后补入实测可用的次新系列；检测不截断显式配置列表，未检测不能标记为“实测成功” |
| 差异与去重 | 同一来源/分组内按归一化URL/KEY识别；Codex与OpenAI兼容来源即使URL/KEY相同、平台都为openai，也保留各自协议/分组身份；名称带稳定来源区分 |
| 陈旧清理 | 仅清理有本工具严格管理标记、已不在完整输入中的账号；提供单独预览入口。手工账号不按名称前缀推断所有权 |
| 状态恢复 | 普通同步保留运行健康状态；本工具因来源停用而设置的干净状态可按严格标记恢复，其他错误或手工停用仍需明确恢复授权与即时证据 |
| TLS对照诊断 | 显式授权站比较最多两个真实源传输参数；有效模型输出及完整状态校验通过才保存绑定参数，失败回滚 |
| 并发处理 | 导入、定价和显式授权的检测使用有界并发；失败明确报告 |
| 代理 | 优先复用部署栈已有的 upstream-importer mihomo；本镜像也包含代理内核、数据和管理脚本 |
| 日志与产物 | 记录阶段、差异与失败；本地计划、快照、对照表仍含敏感数据，应妥善保管 |

已经显式配置的模型列表是权威输入：完整保留名称、后缀和 alias，不因型号看起来较旧或包含某个词就再次截断。客户端请求使用 alias，relay 将该请求交给 CPA 按原 name/alias 配对消费；不额外暴露未配置的原名。`excluded-models` 按对外 alias 匹配，不能误删仅原名相似的其他别名。

以下规则仅用于没有显式模型列表的回退路径：GPT按整个家族的数值版本就高；Codex来源也只保留最高数值版本及其全部有效后缀变体。OpenAI兼容来源中的GPT、Grok、Claude、Gemini、Kimi K和DeepSeek等已识别谱系分别比较，`mini`/`flash`/`fast`仍排除。Claude短次版本与日期后缀区分；Kimi K与Moonshot V、DeepSeek V与R各自独立，日期和上下文长度不当成更高版本。未标版本的符号别名和未知名称保持原配置语义，不猜测其当前指向。次新补充只消费本轮同KEY、同来源目录和对应家族的成功证据，保留最高版本；另一个KEY或家族成功不会被借用。

客户端身份头只补齐缺失项，保留显式配置。`503 ... only allows Claude Code clients` 还可能涉及协议、账号授权或上游专属校验；补一个 User-Agent 不保证恢复可用。`400 invalid codex request` 也应核对 Responses/Chat 协议、模型和请求体，不能一概归咎于模型名。

导入 OpenAI 平台账号时，Codex 来源写入 `force_responses`，OpenAI 兼容来源写入 `force_chat_completions`。这些合法固定模式避免目标版本在创建账号后进入 `auto` 能力探测；协议选定仍需与上游和客户端实际要求匹配。

目标 sub2api 未消费的字段不会凭写入变得有效。需要原 CPA 运行语义的记录使用下文可选 request-relay；默认逐 KEY 绑定，权重池必须显式启用。TLS 使用有边界的显式预设，客户端权限仍需按实际上游核实。本工具不修改第三方源码或网关调度实现。

## VPS 部署

### 使用哪个文件

VPS `/opt/deploy` 使用部署根目录的四个实际文件：`.env`、`config.yaml`、`docker-compose.yml`、`nginx.conf`。项目里的同名文件和 `*.example` 是模板，不能直接覆盖已有共享栈。

已有部署栈通常同时包含 sub2api、CPA、upstream-importer、数据库及共享 mihomo。只更新本工具服务；不要重复创建同端口代理、替换数据库或删除共享卷。代理脚本已经在镜像内，无需把源码目录上传 VPS。

### 填写连接与策略

从 `.env.example` 对照填写自己的部署根 `.env`。不要把下面占位符当作真实凭据：

```dotenv
DOCKERHUB_USERNAME=REPLACE_WITH_REGISTRY_OWNER
DOCKERHUB_IMAGE=cpa2sub2api
CPA2SUB2API_IMAGE_TAG=latest
SUB2API_BASE_URL=http://sub2api:8080
SUB2API_ADMIN_KEY=REPLACE_WITH_ADMIN_KEY
FALLBACK_PROXY=http://mihomo:7890
SETTINGS_PATH=/app/state/设置.json
MODEL_PROBE_ENABLED=false
MODEL_PROBE_HARD_DISABLED=false
ONLINE_PROBE_HOSTS=
AUTO_RECOVER_ENABLED=true
AUTO_RECOVER_CRON=0 3 * * 0
RUN_INTERVAL_SECONDS=0
LOG_LEVEL=INFO
```

- `SUB2API_BASE_URL` 必须是导入容器能访问的地址。服务名和端口以你的实际 compose 为准。
- `SUB2API_ADMIN_KEY` 是 sub2api 管理 API 密钥，不是 CPA 管理密钥、面板密码或下游调用 KEY。
- `config.yaml` 是 CPA/CPAMP 生成的输入，不是本工具设置文件。真实文件挂载到容器 `/app/config.yaml:ro`。不要让缺失的文件路径被 Docker 自动创建为目录。
- CentOS启用SELinux且多个服务共享同一config文件时，使用共享标签 `:ro,z`；不要给每个容器重复打私有 `:Z` 标签。
- `SETTINGS_PATH` 位于 `/app/state`，由 `cpa2sub2api-state` 卷持久化；`/app/out` 使用独立产物卷。升级和回滚均保留这些卷。
- 容器内的 `127.0.0.1` 指向当前容器。共享代理通常写 `http://mihomo:7890`，不要写宿主代理的回环地址。
- `ONLINE_PROBE_HOSTS` 非空时以逗号分隔主机名，提供并限制可开启范围；持久化站点 `false` 仍可撤权。为空则保留站授权设置。以 `probes list` 的实际有效状态为准。

已有共享栈拉取并运行：

```bash
cd /opt/deploy
docker compose config --quiet
docker compose pull cpa2sub2api
docker compose up -d --no-deps --force-recreate cpa2sub2api
docker compose logs --tail 200 cpa2sub2api
docker compose ps -a cpa2sub2api
```

本工具是一次性导入作业。`RUN_INTERVAL_SECONDS=0` 时完成后退出，退出码 0 是正常完成；退出码非 0 必须排查，不能仅因容器不常驻认定故障。日志显示成功也应继续在 sub2api 管理页抽查模型、代理、状态和调度开关。

独立部署时使用仓库内 `docker-compose.yml`，将输入 `config.yaml` 放在模板约定的位置，并填写 `.env`。需要独立代理才启动 mihomo profile：

```bash
docker compose --profile mihomo up -d
```

已有共享 mihomo 的父栈不要再次运行这条独立栈命令。叠加模板 `docker-compose.cpa2sub2api.yml` 仅定义本工具，依赖既有栈的服务与网络；使用前核对实际服务名和网络。

### 先只读预览，再按范围导入

新增或修改账号，先更新权威 CPA `config.yaml`，再运行同一导入流程。新 CLI 的 `--preview` 只读取本地输入、检查字段与模型，既不调用管理 API 或上游 HTTP，也不导出 relay 私有状态。它与旧入口的 `--dry-run` 含义不同。

```bash
cd /opt/deploy
# 全量只读预览
# 以下 python 位于服务命令中，保留镜像 entrypoint 对挂载权限的初始化
docker compose run --rm cpa2sub2api python tool.py import --preview
# 确认全量输入后，新增/同步当前来源账号
docker compose run --rm cpa2sub2api python tool.py import --yes
# 仅添加/同步指定站；可重复 --host，不修改或清理其他站
docker compose run --rm cpa2sub2api python tool.py import --host upstream.example.invalid --preview
docker compose run --rm cpa2sub2api python tool.py import --host upstream.example.invalid --yes
# 交互菜单仍可使用
docker compose run --rm cpa2sub2api python run.py
```

主机名应是当前配置中的精确规范化域名，不带协议、路径或端口。只导入一站仍使用完整配置计算优先级和 relay 运行代际；`--host` 只缩小账号变更范围，不能作为跨站清理的授权。未加 `--host` 才是全量导入。普通导入不隐式执行诊断或模型检测。

旧的 `python 一键导入.py --dry-run` 会生成本地产物，并可能读取配置来源和管理端；已授权检测也受原策略约束。首次核查优先使用新的 `import --preview`，并保留探测总闸关闭。

## 管理每站线上检测

本工具提供 CLI、交互菜单 `[9]` 和可选独立 Web 面板。三者使用相同状态。sub2api 自带管理页面不会凭更新本镜像增加本工具按钮。CLI 中的每站 `[开/关/永久禁止]` 与批量命令如下。

```bash
# 查看每站状态、永久禁探与总闸
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py probes list
# 只授权指定站，主机名使用自己的真实站点
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py probes enable upstream.example
# 关闭指定站
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py probes disable upstream.example
# 批量授权/关闭；永久禁探名单仍生效
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py probes enable-all
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py probes disable-all
# 协调已有恢复计划：未授权站停用，授权站按周频处理
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py probes reconcile
```

**总闸与站授权是两层设置。** `MODEL_PROBE_ENABLED=false` 只为未保存总闸提供默认关闭；保存的master true/false优先。确需检测时，明确授权站点并保存总闸开启，再核对 `probes list` 的有效状态。非空 `ONLINE_PROBE_HOSTS` 继续限制允许的站点。独立硬禁 `MODEL_PROBE_HARD_DISABLED=true` 不可被任何保存值或代理绕过；三个服务必须传入一致的此参数，默认false。

可用 `python tool.py probes master on` / `off` 管理持久化总闸；保存的true/false优先于环境默认值。独立硬禁开启时不能新增开启授权，仍可 `off` 撤权。CLI 授权修改只保存；关闭后运行 `probes reconcile`，确认旧在线计划停止。此命令仅调用管理 API，不执行上游模型检测。Web 关闭则会在保存后尽力自动校准，仍须检查应用结果。

### 可选 Web 授权面板

CLI、菜单和本工具独立的 Web 面板共用持久化设置与文件锁。面板提供每站开关、选择全部可操作站、批量开/关和计划校准；永久禁探行锁定。它管理检测授权，不提供隐式模型/TLS测试按钮，也不替换 sub2api 管理页。

在私有 `.env` 填入强随机 `PROBE_UI_TOKEN`（至少32字符、至少12种不同字符、无空白），不要使用示例口令。令牌只在登录表单中提交，不能放进 URL、截图或命令历史。

```dotenv
PROBE_UI_PORT=8775
PROBE_UI_ORIGIN=http://127.0.0.1:8775
# PROBE_UI_TOKEN 只在私有 .env 填入强随机值；公开示例不提供可用令牌
```

```bash
docker compose --profile probe-ui up -d probe-ui
# 在运行 Docker 的本机浏览器打开 http://127.0.0.1:8775
# 远程 VPS：先从自己的电脑建立 SSH 隧道，替换用户名和占位域名
ssh -L 8775:127.0.0.1:8775 user@server.example.invalid
```

面板默认只映射宿主 `127.0.0.1:8775`。改变宿主端口时，必须把 `PROBE_UI_ORIGIN` 改为浏览器实际看到的精确 `http(s)://主机:端口` 并重建 `probe-ui`；不含路径，不自动信任 `X-Forwarded-*`，不支持随意挂到反代子路径。HTTPS 独立域名反代还须保留精确 Host/Origin 并自行配置访问控制。

Nginx 也在容器内时，与后端加入同一 Docker 网络，配置 `resolver 127.0.0.11 valid=5s ipv6=off;`，通过变量地址转发到 `sub2api:8080` / `probe-ui:8775`，保留 `$http_host` 并省略代理地址后的路径，以保留原始 URI。这样后端重建换 IP 后能重新解析；使用旧的固定解析配置时，应在后端重建后运行 `nginx -t && nginx -s reload`。完整代码见教程的反向代理示例。

- **开启**在独立硬禁关闭时只保存授权，产生零次上游检测，不等于模型可用。独立硬禁开启时返回 `409 PROBE_HARD_DISABLED`，允许关闭既有授权。
- **关闭**先持久保存撤权，再尽力通过管理 API 停用相关旧计划。`saved` 是保存成功，`applied` 是计划应用成功；`saved_not_applied` 需检查管理连接并重试“校准计划”。

关闭后的计划校准会逐账号核实。账号较多或管理端返回429时，页面可能需要等待一个或多个限流窗口；等待期间显示旧列表不代表撤权没有保存。网页请求超过120秒会提示超时，此时先刷新确认保存值和应用结果，避免重复点击；只有核实`applied`，才可认为相关旧计划已完成停用校准。
- 页面显示保存值、本次有效值及独立硬禁原因。`MODEL_PROBE_ENABLED`只提供未保存时的默认值；`MODEL_PROBE_HARD_DISABLED=true` 强制所有provider检测关闭，既有保存值true仍会显示有效false。永久禁探站保持独立永久锁。
- 启动和刷新页面不会发起上游模型检测。只保存开启后，仍需由明确的诊断、恢复命令或获授权的计划发起请求。

### 显式TLS诊断与账号恢复

以下命令会向选中的上游发起实际请求，必须先完成总闸及站授权，永久禁探站始终拒绝。原生周计划、账号恢复与TLS诊断需要已同步、已就绪的安全relay；已有普通Direct账号先按下面步骤迁移：

```bash
# 部署根.env先设置REQUEST_RELAY_ENABLED=true、REQUEST_RELAY_FORCE_ALL_ACCOUNTS=true
# 并启用COMPOSE_PROFILES=request-relay；预览不访问管理端或模型上游
docker compose up -d request-relay
docker compose run --rm cpa2sub2api python tool.py import --preview --host upstream.example
docker compose run --rm cpa2sub2api python tool.py import --yes --host upstream.example
# 只诊断指定已授权站；不会因此激活手工停用账号
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py probes diagnose upstream.example
# 明确选择需要恢复的受管账号ID；示例ID必须替换成你核实过的账号
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py probes recover 7 8
```

`diagnose`只处理受管、active且schedulable、没有429或临时冷却的账号。它按当前真实来源、KEY和实际模型比较传输参数：先测原参数，失败后才试一个明确TLS预设，每账号最多两个模型请求。有效输出、实时授权、账号/来源快照及完整state校验都通过后，才保存绑定来源、KEY、模型和consumer二进制摘要的参数；来源或版本变化后旧结果失效。它不试写sub2api账号的TLS布尔字段。失败、取消、撤权或并发修改均保留主设置、CURRENT、secrets及其他路由；中断事务由journal恢复。源级成功不代表sub2api原生客户端或所有模型兼容。

`recover`只读加载已有`CURRENT`代际，不重新导出配置或轮换令牌；`diagnose`仅在上述成功事务中保存完整新代际，保留其他路由与现有令牌。发送请求、保存TLS参数及恢复写入前都会重新核对授权、来源、relay地址、状态目录、权重/账号选择模式和当前代际；中途变化时旧成功结果不能继续使用。遇到不匹配，先同步当前来源并核对服务就绪。

普通导入只使用已学习的TLS参数，不隐式发起该诊断，也不会复活来源不明的手工inactive或`schedulable=false`账号。显式来源停用或排除全部模型时，账号会原子写为 `inactive + schedulable=false`，最终模型映射清空，不把内部占位项当作可用模型显示。`extra.cpa2sub2api_source_disabled` 只记录本工具在强来源绑定下设置的停用；已有手工停用、错误或冷却不会被猜测认领。来源重新放开后，只有该标记、来源绑定、当前停用状态及无错误/冷却/模型限额等条件全部通过，才原子恢复并清除标记；其他状态保持原样。`recover`是本次命令明确选择账号ID的恢复授权；自动错误的`error + schedulable=false`也可在当前源仍启用、密钥归属和模型映射一致时检测，源映射已变化应先同步。即时检测失败不会清错；`error`但调度已经开启的异常组合会停止并提示核对。

自动错误恢复先清错，再恢复调度，每步及管理429重试前都重新核对授权、来源、代际和状态。未来有效冷却、无法解释的时间或运行状态仍阻断；已明确过期的残留可在即时成功后清理。只有最终读回`active`、`schedulable=true`且错误已清才算成功；清错成功但恢复调度失败属于部分完成。目标管理API没有原子比较并交换条件，前后校验不能消除两个请求之间的全部并发竞态；不要同时手工修改同一账号。

本工具对导入账号显式关闭sub2api原生的“自动费率探测”和“自动费率同步”（`upstream_billing_probe_enabled` / `upstream_billing_rate_sync_enabled`），避免第三方原生任务绕过这里的检测开关。**在sub2api页面手工创建第三方上游账号时，也要检查并关闭这两个开关**；本工具的站授权不能管理未归属本工具的手工账号。

不要同时设置多个重复宿主 cron、容器循环和 sub2api 探测计划。配置同步与模型探活是不同任务；默认无需周期运行同步作业。

## 修正已有参数和删除陈旧账号

正常导入会对已有受管账号做差异同步。请在计划和结果中分别查看新增、更新、重复、陈旧、失败数量。

```bash
# 先查看哪些严格受管账号将因不在当前config而清理
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py prune preview
# 确认当前config是完整输入并已备份目标数据后再应用
docker compose run --rm --no-deps --entrypoint python cpa2sub2api tool.py prune apply
```

正常导入成功后默认自动清理，可用 `PRUNE_STALE_ACCOUNTS=false` 关闭。只在完整读取配置与目标账号、所有当前目标均已存在且同步成功、所有权标记匹配时才允许清理；逐候选重新读取并核实删除结果。损坏配置、分页读取失败、部分导入失败不能解释为“站点已删除”。仅有历史短指纹的 `legacy_claim` 账号保留，不自动删除；来源未知的旧账号先按下文核对并显式认领。名称带 `ANT-` 或备注提到 CPA 都不能单独证明归属；单站导入不清理其他站。

去重不能跨来源协议折叠：相同URL和KEY同时出现在Codex与OpenAI兼容段时，可能分别使用Responses/Chat协议和不同分组。工具使用稳定的来源/分组后缀区分名称，并迁移可确认归属的旧重名账号；不能仅因目标平台都叫`openai`就删掉其中一条。

## 可选 request-relay：让原 CPA 运行时消费参数

sub2api API key 账号本身没有等价消费者的 CPA 行为，通过可选 `request-relay` 保留：原配置的 cloak、中途 system 重建、请求头、prompt-cache、连接/重试配置及 Responses WebSocket 等由**同一应用镜像内从固定官方 CPA 源码构建的 consumer**实际消费。本项目在诊断出站边界加入禁止重定向的安全补丁，原第三方工作区不修改。镜像附带其 MIT 许可。需要这些行为的记录在 relay 未启用或未就绪时明确拒绝，不能降为“仅写备注也算生效”。

consumer 的来源记录位于 `/app/third_party/CLIProxyAPI.PROVENANCE.json`，包括固定上游 commit、构建镜像、二进制 SHA256、构建输入摘要、CGO、Go版本与架构。诊断功能标识为 `cpa2sub2api-diagnostic-no-redirect-v1`；它区分本项目派生 consumer 与原版上游镜像。relay 还要求实际可执行文件和来源记录同时证明 `automatic_output_cap: 16` 与 `automatic_output_header: X-CPA2SUB2API-Auto-Output-Cap`；旧二进制只有原诊断标识时不能就绪。自动请求在 CPA 的全部 payload 规则处理后、最后出站前限制输出为最多16；无法证明小输出的压缩、签名、thinking/reasoning 等配置受控跳过，不降低选定的真实最高模型。诊断入口选择精确 KEY、关闭重试并拒绝跟随重定向；relay 将诊断30x转换为受控非重定向错误，去掉 Location，避免下游客户端再次跟随。普通业务保留原传输行为；手工诊断不使用自动16输出额度，仍遵守诊断分支的禁止重定向和授权检查。不能把重试次数为0当作重定向已经关闭。

原生周计划使用工具自动生成的 `aRID40-hash58` 自动诊断model标记；显式手工诊断使用 `dRID40-hash58`。两类新标记都由当前route与实际model摘要组成，固定100个合法ASCII字符，符合当前原生计划DTO的model_id长度上限；旧106字符标记仅为已有state兼容读取保留，不用于新建计划，并各自写入精确self-map，完整保留普通及通配模型映射。不要人工拼接 `dRID/model`，Gemini URL模型片段不允许斜线。relay在认证、当前route/model、双闸授权及自动周预算验证后才选内部诊断prefix；自动票据还绑定当前主机、KEY来源、模型和代际。不要手工改标记或把它当真实上游模型名。

公开模板默认 `REQUEST_RELAY_ENABLED=false`，保留可直接迁移输入的原行为。若预览提示该输入需要 relay，必须同时启用开关和 profile：

```dotenv
REQUEST_RELAY_ENABLED=true
# 默认false仅选择需要CPA行为的记录；true也选择普通Direct受管账号以支持安全诊断
REQUEST_RELAY_FORCE_ALL_ACCOUNTS=false
COMPOSE_PROFILES=request-relay
REQUEST_RELAY_BASE_URL=http://request-relay:8380
REQUEST_RELAY_STATE_DIR=/app/state/relay
REQUEST_RELAY_SOURCE_SCOPE=default
REQUEST_RELAY_WEIGHTED_BUCKET=false
REQUEST_RELAY_TIMEOUT=600
REQUEST_RELAY_MAX_RESPONSE_BYTES=67108864
REQUEST_RELAY_TLS_PROFILES={}
```

```bash
cd /opt/deploy
# 首次没有导出状态时，服务暂时 unready 是正常状态；不要设置成导入前必须 healthy
# profile 由上面的 COMPOSE_PROFILES 启用，也可显式添加 --profile request-relay
docker compose up -d request-relay
docker compose run --rm cpa2sub2api python tool.py import --preview
# 此步先原子导出私有代际，等待实际 consumer 就绪且代际一致，再创建/同步账号
docker compose run --rm cpa2sub2api python tool.py import --yes
docker compose ps request-relay
docker compose logs --tail 100 request-relay
```

relay 不映射宿主端口，sub2api 与 relay 需处于可互通的 Docker 网络。私有运行状态只写入命名 state 卷，不从 Git 或宿主源码目录加载派生配置；其中包含真实 KEY、路由令牌和控制令牌，不能发布、下载作教程附件或打进镜像。服务启动、重新加载和就绪检测不合成用户模型请求；新代际先关闭接入并有界排空旧流，默认600秒后取消超时流再切换。

每次官方consumer启动都使用独立的空OAuth目录；旧代际配置和私有令牌保留。正常重启后应重新核实服务健康，无需删除state卷；删除该卷会丢失现有路由和控制令牌。

`REQUEST_RELAY_FORCE_ALL_ACCOUNTS`默认false，保留普通Direct业务；显式true且relay已启用时，`import --preview --host` / `import --yes --host`可迁移指定站的普通受管账号。预览不联网，同步保持原身份、优先级和普通模型映射。默认Direct业务及显式单次源探测仍可使用；完整原生诊断须迁到安全relay。

**默认业务请求绑定原 KEY。** `REQUEST_RELAY_WEIGHTED_BUCKET=true` 才显式使用同 scope、来源类型、分组和原主机内的权重池；原请求的业务重试仍按源配置策略。池内其他 KEY 成功不能证明最初 KEY 可用。诊断固定 exact-key、零额外重试，并即时复核原站授权；不能利用池替代指定账号的成功证据。无合格放行模型的记录保持阻断，不允许空白名单变成全部放行。

### 显式站级 TLS 与代理

`fingerprint-profile`、`enable_tls_fingerprint=true` 和“真实客户端身份”是不同能力。通用站点需要 TLS 预设时，显式配置精确 host → 对象映射；不能从一个布尔开关猜测预设：

```dotenv
REQUEST_RELAY_TLS_PROFILES={"upstream.example.invalid":{"impersonate":"chrome136"}}
```

允许 `chrome`（当前固定到 `chrome136`）、`chrome136`、`firefox135`、`safari184`。也可在受管配置条目上写工具扩展 `request-relay-tls: {impersonate: chrome136}`，不能放在 `api-key-entries` 子项内。可选 `ca-bundle` 必须是容器内绝对、可读的证书文件路径，并另行只读挂载；证书和主机名校验仍启用。参见 [安全输入骨架](config.yaml.example)。

固定目标 TLS adapter 保留指定目标、SNI/Host、每 KEY 的认证与显式代理，拒绝重定向。SSE 与文本/二进制 WebSocket 走真实流式转发；gzip/deflate 按边界解码，Brotli 取决于安装的依赖，不支持时明确拒绝。解码响应默认限制64MiB，可配置1–512MiB，并有压缩膨胀限制。它不能证明拥有合法 Claude Code 身份、签名、账号权限或保证永不封号。

### 上游版本信息与字段合同

`import --preview` 和显式离线检查不请求 GitHub；预览也不访问 sub2api 管理端或模型上游。公开模板设 `FETCH_UPSTREAM_SRC=true`，正常线上执行按版本证据TTL自动查询官方最新稳定 tag/commit及候选源码，默认每7天；缓存新鲜时不重复HTTP。设为 false 时正常执行只用本地证据。官方 CPA consumer 仍由镜像固定版本及摘要；发现有新 release 不会改变正在运行的 consumer 或目标 sub2api。

```bash
# 只读当前缓存和版本证据：零网络、零缓存写入
docker compose run --rm cpa2sub2api python tool.py upstream inspect
# 显式刷新当前固定版本的源码证据
docker compose run --rm cpa2sub2api python tool.py upstream refresh
# 只用已有完整缓存；此命令不向GitHub发请求
docker compose run --rm cpa2sub2api python tool.py upstream refresh --offline
# 显式查询官方最新稳定版，并取得候选源码；不会自动切版本
docker compose run --rm cpa2sub2api python tool.py upstream refresh --latest
```

`--kind cpa` 或 `--kind sub2api` 可限制检查对象，默认 `all`。`--latest` 与 `--offline` 不能同时使用。官方版本查询与模型上游探测是两个独立功能，站级探测默认关闭。

源码缓存保存到 `out/upstream-cache/<kind>/<commit>/<generation>/manifest.json`；每个完整代际逐文件核验 SHA，再更新该 commit 下的 `CURRENT.json`。版本发现报告位于 `out/upstream-cache/release-evidence.json`，固定源码刷新报告为同目录的 `source-cache-evidence.json`。`UPSTREAM_CACHE_TTL_SECONDS=604800`（7天）对应设置键 `upstream_cache_ttl_seconds`；过期但完整、摘要匹配的固定版本缓存可在离线模式复用，会明确标记 stale，不能据此声称最新。尚未激活的新版本候选不会阻止仍匹配当前固定合同的导入；实际使用的源码发生未审查漂移、配置含未知受管字段或旧计划固定身份不匹配时，导入及清理才会拒绝继续。首次空 out 卷仍可使用镜像内已验证的固定字段合同，不要求自动下载Go源码。

仅已人工审查并登记、Go AST、完整 token 与编译指令均一致的精确注释/排版源码表示，允许继续采用原字段合同。`verified_reviewed_equivalent` 描述单个源文件；例如已登记的 CPA v8.0.61 配置结构文件仍采用 v8.0.60 consumer 合同，不会自动升级运行消费者。原始文件 SHA 仍保留在计划与消费前校验中，更换源码后必须重新生成计划；未登记的字节变化、未知字段或语义变化仍需受控复核。

当前受管字段按已核准的官方 CPA v8.0.60 合同校验字段名和递归类型；不回显配置值。未知受管字段、嵌套类型改变或来源合同漂移都要处理。升级流程是：取得候选报告 → 审查新字段和消费语义 → 适配并固定新 consumer 摘要及基线源码SHA → 构建隔离容器并验证真实配置、目标管理页和调用链 → 发布已验证镜像。随后再按[升级与回滚](#升级与回滚)替换部署。没有一条命令能跳过合同适配而自动激活任意未来版本；删除候选报告也不能让新源码变成兼容源码。

## 多来源隔离与旧账号认领

每个独立 CPA 输入使用稳定的 `REQUEST_RELAY_SOURCE_SCOPE`（默认 `default`），即使关闭 relay 也用于账号来源所有权。多来源必须各自设置不同 scope、不同 relay `state_dir`/卷和独立配置；一个 state 目录一旦绑定 scope，不得换名覆写另一来源。不要把两个来源轮流作为同一完整输入执行清理。

新增账号保存严格来源元数据，更新、诊断和删除前重新校验归属；A 来源不能清理 B 来源。普通导入先查找唯一受管来源，再决定新增或修复，因此可确认的历史旧名称不会被当成新的账号重复创建。

### 已有名称、类型和平台如何修复

| 情形 | 行为与证据要求 |
|---|---|
| 唯一旧名称，来源 sentinel、段、URL、协议和指纹一致 | 使用原账号 ID 修正名称及受管参数，保留原状态、调度开关和非受管附加信息 |
| 合法账号类型变化，例如受管 OAuth 改为 apikey | 取得完整来源身份后使用原 ID；显式清除旧类型敏感凭据并读回。存在影子账号依赖或目标 API 不允许改型时停止 |
| 平台不同，目标 API 不支持原 ID 改平台 | 仅允许强来源身份且无影子/计划等依赖的受控替换；新账号使用新 ID |
| 只有历史短指纹，完整凭据导出不可用 | 允许同平台、同类型的旧名称和非身份参数修复，记录为 `legacy_claim`；不改密钥、类型或平台，不自动清理 |
| 无 sentinel、手工账号、其它 scope、来源冲突或候选不唯一 | 保留；无 sentinel 的历史账号只能走下面的明确 ID 强证据认领 |

平台替换先把候选放进随机独占隔离组并限制全部模型，读取候选的精确 ID 原文凭据，再明确停用候选并读回。随后写入最终参数、原有及配置要求的分组、原状态与调度意图，再核实原文凭据和全部受管字段；删除旧 ID 前还会重新检查旧账号依赖和候选快照。旧 ID 删除是最后一步。失败时保留旧账号，只清理可确认由本次操作创建的候选与隔离组；删除响应丢失需读回核实，结果未知时保留已验证候选并报告失败。平台替换会改变 ID，已有计划等关联不能被静默搬移。

### 显式认领没有完整来源标记的历史账号

先备份目标数据，在管理页核对具体 ID 和当前完整 `config.yaml`。认领要求单个候选的实际完整密钥、URL、协议及配置记录唯一对应；没有 sentinel 时还限定历史 `ANT-域名-数字` 名称形式，但这个名称形式本身不构成授权。不会批量猜测 `ANT-` 前缀，也不会认领普通手工名称。

```bash
# 示例ID必须换成已核实且能与当前完整config唯一绑定的历史账号
docker compose run --rm cpa2sub2api python tool.py prune claim-preview 101 102
# 重新生成并核对预演后，在提示中输入大写 CLAIM
docker compose run --rm cpa2sub2api python tool.py prune claim-apply 101 102
# 认领、完整同步均确认后，再检查清理范围
docker compose run --rm cpa2sub2api python tool.py prune preview
```

认领预演会访问管理 API，并通过精确 ID 的管理员原文导出核实敏感凭据；原文只在内存中比较，不打印或写入公开产物。应用前重新读取配置、账号快照与完整凭据；任一变化都会取消认领。认领只添加来源归属并修正配置名称/备注，不改变状态或调度开关。已有其它来源归属的账号仍留在其原 scope。

如果原文导出返回 `403` 或要求管理员增强验证，保持该门禁。按目标管理页的登录/验证流程手工导出所选单个账号，不能关闭增强验证、绕过权限或把隐藏的密钥当成已核实。将原文导出保存在 VPS 的私有位置，只授予容器实际 `APP_UID` 必要读取权限；不要放进仓库、镜像、日志、截图或公开教程。下面的宿主与容器路径是示例，文件必须是管理员原文导出的 `accounts` 单元素结构，明确 ID 必须与刚核对的账号一致：

```bash
# 已在管理页验证并导出单个账号101；私有文件只读挂入一次性容器
docker compose run --rm \
  --volume /opt/deploy/private/account-101-export.json:/private/account-101-export.json:ro \
  cpa2sub2api python tool.py prune claim-preview 101 \
  --evidence /private/account-101-export.json
# 核对预演，再按提示输入 CLAIM；--evidence 每次只接受一个明确ID
docker compose run --rm \
  --volume /opt/deploy/private/account-101-export.json:/private/account-101-export.json:ro \
  cpa2sub2api python tool.py prune claim-apply 101 \
  --evidence /private/account-101-export.json
```

显式私有导出也要匹配当前账号快照、URL和配置完整密钥。导出来源不明、含多个账号、出现冲突或预演后配置变化时停止；认领不能替代备份和最终同步读回。

## 代理复用与验证

父栈共享 `mihomo` / `mihomo-init` 通常使用 upstream-importer 镜像。本项目镜像也包含 `mihomo-manager`、mihomo 内核和数据，用作独立部署或替代镜像；两者是否同版本应按实际镜像摘要核对，不保证历史 md5 永远一致。

公开模板的 `MIHOMO_AUTOMATIC_TESTS=false` 关闭代理节点的自动 provider health-check 与 url-test；模型账号探测由 `MODEL_PROBE_ENABLED` 和站级授权另行控制。共享栈按 `mihomo-init` → 本项目镜像的 `mihomo-policy-init` → 原共享 `mihomo` 内核运行。叠加模板的 `MIHOMO_SHARED_VOLUME` 要填父 compose 已声明的配置卷 key，默认 `mihomo-data`；独立模板用自身的 `mihomo-config` 卷。policy-init 无网络，只对原初始化已生成的共享配置原子应用策略，不复制本项目模板或覆盖原 importer 的中立连通性健康脚本、手工 fallback。自动检测组改为手工 `select`，节点、过滤条件、规则保留。

若要恢复代理节点自动检测，明确设置 `MIHOMO_AUTOMATIC_TESTS=true` 并恢复或重建原本的自动检测组配置，再按上述次序重建；仅把环境变量改成 true 不会把已转成 select 的组自动还原。它也不会授权任何模型站探测。配置缺失、不是普通文件或含符号链接时，policy-init 明确失败，内核应等待成功后再启动。

镜像内初始化入口为 `/bin/sh /app/mihomo-manager/bootstrap-mihomo.sh`；运行配置使用 `/root/.config/mihomo` 命名卷。订阅地址和控制器密钥只在私有 `.env` 配置。已有栈的 init、代理本体和客户端需共享正确网络、配置卷及控制器凭据。

```bash
docker compose ps mihomo mihomo-init
docker compose logs --tail 100 mihomo-init mihomo
docker compose exec mihomo sh /root/.config/mihomo/healthcheck.sh
```

进程存活不代表有可用节点。还要检查订阅是否成功、AUTO 组节点数量、从本工具容器到代理的连通性，以及目标请求是否确实使用了该代理。健康脚本降级 DIRECT 是出口策略，不能代替对目标站的可用性证明。

## 升级与回滚

升级前记录正在使用的镜像标签/摘要，备份私有四个部署文件、state/out 卷及目标 sub2api 数据。修改 `.env` 中 `CPA2SUB2API_IMAGE_TAG` 后执行拉取与重建。使用发布提供的固定 SHA 标签比始终跟随 `latest` 更容易复现。

```bash
cd /opt/deploy
# .env中填入已发布的目标标签后
docker compose pull cpa2sub2api
docker compose up -d --no-deps --force-recreate cpa2sub2api
docker compose logs --tail 200 cpa2sub2api
```

启用了 Web/relay 时须让三个服务使用相同镜像标签，并分别拉取重建相应 profile 服务。保留 relay state 代际及稳定 scope；旧格式运行状态需从权威配置重新导出，再验证实际 consumer 就绪。迁移 relay 先预览、启服务、实际导入通过代际检查，最后抽查管理页；不要手工把账号 URL/KEY 改成派生路由。

回滚时把该标签改回升级前记录的值，再执行同样命令。回滚程序镜像不会自动撤销已经写入 sub2api 的账号变更；需要撤销数据变更时使用升级前备份。如要从 relay 退回直连，先确认原输入所有字段都能由直连消费，再设 `REQUEST_RELAY_ENABLED=false`、只读预览并重新导入原来源；不能仅停 relay，否则已导入路由会失效。需要 CPA 独有行为的输入应回滚到已验证镜像和对应状态/数据备份。不要运行 `docker compose down -v`，它会删除共享栈数据卷。

## 故障排查

| 现象 | 核查与处理 |
|---|---|
| `503 No available channel ... under group default` | 检查上游分组授权、模型存在性和协议；临时错误恢复还需检查 status 与 schedulable，不能无限强制启用真实失效密钥 |
| `503 ... only allows Claude Code clients` | 对比直连和中转的端点、请求体、客户端身份头；站方专属校验可能无法由通用API代理复制 |
| `400 invalid codex request` | 核对 Responses/Chat 模式、必填字段、模型、originator 和上游实际支持能力 |
| `401` / `402` / `400` / `403` 冷却不符 | 检查规则是否分别为30/15/1/1分钟；确认旧账号规则也被本次导入更新 |
| 账号 active 但不可调度 | 检查 `schedulable`、临时冷却、手工停用以及恢复证据；只改status不足 |
| 旧半小时计划仍执行 | 保持总闸关闭，执行 `probes reconcile`，在sub2api查看计划启用状态和下次执行时间 |
| 新config删站后旧账号还在 | 看 `prune preview`、管理标记、同步失败与分页读取结果；未标记的手工/旧账号不会被猜测删除 |
| 旧名未修复、改型/平台替换被拒绝 | 核对唯一来源绑定、原文导出权限、影子/计划依赖和完整失败项；不要改名伪造归属或手工按ANT前缀删除 |
| 原文导出 `403` / 增强验证要求 | 在管理页完成要求；显式认领可使用私有单账号原文导出，不能降低权限；平台替换仍须在线读回候选原文凭据 |
| `524 origin_response_timeout` | 比较整个CPA/网关/反代重试预算和实际超时；不能靠无限增大nginx超时解决Cloudflare固定限制 |
| mihomo“连接失败” | 检查控制器鉴权、服务名/端口、同网络、初始化卷和AUTO节点数；与模型路由错误分别排查 |
| `423 Locked` | 目标sub2api可能有管理员合规确认门禁，在实际管理页确认；不要反复轮换管理密钥 |
| `403 error code: 1010` | 请求可能在边缘防护层被拦，比较实际访问路径和目标策略 |
| `/app/config.yaml` 是目录 | 核查宿主真实文件存在性，修正错误挂载后重建服务 |
| `/app/out` 或 `/app/state` Permission denied | 核对当前镜像UID/GID与卷属主；不要在未经核实的共享卷上递归改权限 |
| 本机通过，VPS失败 | 对比镜像摘要、四部署文件、网络、环境变量、卷权限、目标API版本与真实前端字段 |

目标版本可能在管理API返回中隐藏`api_key`，或把默认关闭的费率字段省略；不能只因响应没有该字段就判断未导入。应结合目标版本、实际编辑页状态和受控存储读回核实，避免把真实KEY回显到日志、截图或公开报告。

详细管理员密钥区分、轮换和关联项目排障见教程第八章。不要把面板登录密钥、CPA管理密钥、sub2api管理密钥和下游调用KEY混用。

## 本地开发与验证

本地直连 CLI 和配置编译支持 Python 3.9+；request relay 需要 Python 3.10+，推荐使用镜像内 Python 3.11。

导入后的计划、恢复、旧账号和健康重排校准会逐项输出进度；等待管理接口时每 5 秒提示一次。每次管理网络尝试（含写入前校验）最多等待 30 秒，每个阶段最多 10 分钟，整个校准最多 15 分钟；配置中的较短 HTTP 超时仍然生效。收到 429 时，顶层操作按原 Retry-After 完整暂停，在不变的阶段/总预算内最多顺序重试两次；每次重试重新校验，单次网络截止重新计算。冷却放不进剩余预算、重试耗尽、触及总截止、连续三项真实网络失败或收到 401/403 时，剩余项标为未校准并返回失败，不缩短冷却或继续向后续账号突发请求。请查看错误摘要并修正管理接口连接或授权后重试。

校准只读取或修正 Sub 管理数据，关闭线上检测的站点仍会核查并停用危险的旧探活计划。该步骤不会因此新增模型探活请求，也不会缩短管理接口给出的限流冷却时间。

```bash
pip install -r requirements.txt
python run.py
python 一键导入.py --dry-run
python -m unittest discover -s tests -p "test_*.py"
```

测试通过只证明相应测试条件。发布前还需用当前源码构建镜像、隔离数据库和网络，按VPS挂载真实config、跑实际sub2api前端，检查导入字段、旧数据修正、删除边界、计划迁移、代理和控制台错误。永久禁探站不能为验收例外开启。

产物位于 `out/`：导入计划、对照表、配置快照、执行记录和日志。这些文件可能包含真实上游地址、KEY或账号资料，不能上传到Git或作为公开教程附件。

主要模块：`probe_web.py` / `probe_web_static/`（授权面板）、`request_relay.py` / `relay/`（官方 CPA consumer 与固定目标传输）、`tool.py`（导入与管理）、`model_selection.py`（模型选择）、`model_probe.py`（授权模型检测）、`probe_policy.py`（探测策略）、`new_remap_priority.py`（健康排序）、`extract.py`（上游目录）、`pricing.py`（定价）、`mihomo-manager/`（代理脚本）、`tests/`（回归测试）。

## 发布与隐私

GitHub Actions配置见 [docker-publish.yml](.github/workflows/docker-publish.yml)。仓库变量决定镜像命名，发布凭据由GitHub Secrets保存。

不要提交或复制进镜像：relay 派生状态/令牌、真实 `.env`、`设置.json`、`config.yaml`、订阅配置、日志、`out/`、真实测试截图，以及包含明文KEY的需求DOCX/PDF/MHTML。公开示例只使用 `example` 保留域和明显占位符。`.gitignore`不能清除已经跟踪的秘密；提交前还需扫描暂存内容和镜像构建上下文。

## 许可

MIT，见 [LICENSE](LICENSE)。
