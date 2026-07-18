# QVeris Account Proxy

一个面向 QVeris 官方 REST API 的轻量多账号 Docker 代理，带有可视化管理页。项目是非官方工具，与 QVeris、Vibe-Trading 没有隶属关系；代码按 [MIT License](LICENSE) 发布。

## 3 步快速开始

### 准备：安装并启动 Docker Desktop

先安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，启动后确认 Docker Engine 正在运行。电脑需要 Git；没有 Git 时，也可以在 GitHub 仓库页面选择 **Code → Download ZIP**，解压后继续下面步骤。

### 1. 注册 QVeris

打开[注册链接](https://qveris.ai/?ref=afAfj_c90cnWYg)，注册账号并填写邀请码 75gxF1vtvXWj_A。注册完成后，在 QVeris 控制台创建一个 API key，稍后在启动脚本提示处粘贴。

### 2. 下载并启动轻量代理

在终端执行：

~~~bash
git clone https://github.com/HuLWe/qveris-account-proxy.git
cd qveris-account-proxy
~~~

首次启动会隐藏输入 QVeris API key，自动创建代理 API Key、启动轻量容器，并通过 60 秒有效的一次性链接打开已连接的管理页。上游 API key、代理 API Key、账号配置和 SQLite 状态都放在 Docker named volumes 中，不会写入 Git 仓库，也不需要手动创建宿主机 secret 目录。

Windows：

- 最简单的方式是双击 start.cmd。
- 命令行方式：

  ~~~powershell
  .\start.cmd
  ~~~

- 需要让同一局域网的 Vibe 主机访问时，使用 -Lan：

  ~~~powershell
  .\start.cmd -Lan
  ~~~

  也可以直接运行 PowerShell 脚本：.\start.ps1 或 .\start.ps1 -Lan。

macOS / Linux：

~~~bash
bash ./start.sh
~~~

局域网模式：

~~~bash
bash ./start.sh --lan
~~~

默认只监听本机 127.0.0.1。-Lan / --lan 会监听所有 IPv4 网卡；只在可信局域网使用，并先确认系统防火墙规则。脚本会在终端打印实际管理页地址和 API Base URL。

### 3. 打开管理页并连接 Vibe

管理页默认是 http://127.0.0.1:18081/admin/。启动脚本会签发一个 60 秒有效且只能使用一次的浏览器连接票据，打开后立即从地址栏清除并自动连接，真实代理 API Key 不进入浏览器启动参数。页面右上角可显示、隐藏或复制代理 API Key；页面刷新时会在当前标签页通过 sessionStorage 保持连接，点击“断开”会清除会话。若连接链接失效，重新运行启动脚本即可生成新链接；若脚本未能生成连接链接，终端会打印一条用于显示代理 API Key 的 Docker 命令，管理页的“手动连接”可作为回退。

在管理页的“账号配置”中先点击“测试”验证首个账号，再点击“添加账号”，逐个录入其他 QVeris API key 并保存。每个账号都显示 Key/OAuth 状态、额度和冷却信息；新账号会加入同一轮询池。账号“测试”只调用低成本检查接口，接口测试页对计费操作要求额外确认。

打开 Vibe-Trading 设置页（例如 http://127.0.0.1:8899/settings），在管理页右上角点击“复制”，只复制一个代理 API Key，填入这两个字段：

~~~text
Base URL: http://127.0.0.1:18081/api/v1
API Key:  管理页中的代理 API Key
~~~

不要把上游 QVeris API key 填进 Vibe。若 Vibe 在另一台局域网主机，使用 -Lan / --lan 启动代理，再使用脚本打印的局域网 API Base URL。若脚本显示 LAN_IP，请把它换成运行代理电脑的 IPv4 地址，或先设置 QVP_LAN_HOST。

### 数据会不会丢

启动脚本使用以下 Docker named volumes 保存数据：qveris_config、qveris_secrets、qveris_account_secrets，以及 Compose 的 qveris_state。重复运行启动脚本会复用这些卷，不会再次询问首个 API key，账号配置、代理令牌、额度和路由状态都会保留。停止容器不会删除 named volumes；不要对它们执行 docker volume prune，除非已经备份并确认要清空数据。

### 安全边界

- 代理 API Key 同时用于管理页和 API 调用，请像密码一样保存，不要发到群聊、截图或公开仓库。
- 默认仅本机访问。局域网模式只适合可信网络；HTTP 不提供传输加密，跨越不可信网络前应放在带 TLS 的 Caddy/Nginx 等反向代理后面。
- 不要把 18081 端口直接暴露到公网，也不要把代理令牌交给不受信任的用户。使用本项目产生的 QVeris 费用和账号责任由部署者自行承担。

## 轻量模式与持久化

启动脚本组合 compose.yaml、compose.lite.yaml、compose.ui.yaml 和 compose.quickstart.yaml。轻量模式不启动 Keeper 或 Chromium，默认限制为 160 MiB 内存、0.5 CPU、64 个 PID、16 个上游连接和单 key 2 并发。

Docker Desktop 的 Volumes 页面可以看到项目卷。重新启动、升级镜像或重启 Docker Desktop 都会保留 named volumes。停止服务：

~~~powershell
.\start.cmd -Stop
~~~

~~~bash
bash ./start.sh --stop
~~~

上面的命令不会删除卷。删除卷会同时删除账号配置、代理令牌和状态，只有在明确要重新开始时才执行。

## Vibe-Trading 参考

Vibe 的 Base URL 必须包含 /api/v1，并指向代理，不能指向 /admin/，也不能直接指向 QVeris 上游。局域网使用示例：

~~~text
QVP_BIND_ADDRESS=0.0.0.0
QVP_HOST_PORT=18081
QVP_ROUTING_MODE=round_robin
QVP_DEFAULT_ACCOUNT=account-a
QVP_ALLOW_API_KEY_FOR_OAUTH_ROUTES=true
Vibe Base URL=http://LAN_IP:18081/api/v1
Vibe API key=管理页复制的代理 API Key
~~~

QVP_DEFAULT_ACCOUNT 用于额度、用量等控制接口；搜索、Inspect 和执行接口在配置多个账号时继续进行加权轮询。QVP_ALLOW_API_KEY_FOR_OAUTH_ROUTES 只为 Vibe 的 usage 状态检查提供兼容回退，不会把 API key 当作所有 OAuth 路由的凭据。

## 账号与路由

普通用户直接在管理页添加账号，不需要手工编辑 JSON。每个账号可以配置多个 API key 和可选 OAuth token；保存前页面会验证草稿，账号测试只调用 /auth/credits 和 /auth/verify-token。

代理支持：

- /providers、/providers/categories、/search、/tools/by-ids、/tools/execute 的账号加权轮询；
- 使用 X-QVeris-Account 显式选择账号；
- 使用 X-QVeris-Session、session_id、search_id、execution_id 建立账号亲和；
- API key 与 OAuth access token 分池调度；
- 401、402、403、429 和网络故障的冷却与退避；
- 原始 JSON、CSV 和大响应流式转发。

API 代理令牌只用于代理认证。代理会移除客户端 Authorization，再向 QVeris 上游注入选中的账号凭据；Cookie、转发头和 hop-by-hop headers 会被过滤。

## API 清单

除公开的 /api/v1/meta 与健康检查外，API 路由都要求代理 Bearer 令牌：

| Method | Path | 上游凭据 |
| --- | --- | --- |
| GET | /api/v1/auth/credits | API key |
| GET | /api/v1/auth/usage/history/v2 | OAuth 或兼容 API key |
| POST | /api/v1/auth/verify-token | OAuth |
| GET | /api/v1/providers | API key |
| GET | /api/v1/providers/categories | API key |
| POST | /api/v1/search | API key |
| POST | /api/v1/tools/by-ids | API key |
| POST | /api/v1/tools/execute | API key |

本地管理路由包括 /admin/v1/accounts、/admin/v1/config、/admin/v1/config/validate、/admin/v1/accounts/{id}/test、/admin/v1/refresh-credits 和 /admin/v1/reload-accounts。管理接口返回脱敏状态，不回显凭据值。

## 环境参数

启动脚本已经提供普通用户所需的默认值。需要改端口或项目名时，在启动前设置：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| QVP_HOST_PORT | 18081 | 主机端口 |
| QVP_BIND_ADDRESS | 127.0.0.1 | 主机监听地址；脚本的 -Lan/--lan 会覆盖为 0.0.0.0 |
| QVP_LAN_HOST | 自动识别 | -Lan/--lan 模式下打印给其他设备使用的 IPv4 地址 |
| QVP_PROJECT_NAME | qveris-proxy | Compose 项目名和 named volume 前缀 |
| QVP_DEFAULT_ACCOUNT | account-a | 没有账号请求头时使用的账号 |
| QVP_ROUTING_MODE | round_robin | round_robin 或 explicit |
| QVP_MEMORY_LIMIT | 160m（轻量启动脚本） | 容器内存上限 |
| QVP_CPU_LIMIT | 0.5（轻量启动脚本） | 容器 CPU 上限 |

## 可选：Session Keeper

Session Keeper 是独立的 Chromium 控制面，不属于普通用户的轻量启动流程。它为每个账号维护独立浏览器 profile，并通过外部凭据文件管理网页登录状态。配置样例见 examples/keeper.example.json；启用前请先阅读 compose.keeper.yaml 和相关资源要求。

## 验证与开发

~~~powershell
.\.venv\Scripts\python -m ruff check src tests
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python scripts\check_openapi.py
~~~

测试使用占位凭据和 httpx.MockTransport，覆盖路由白名单、头过滤、流式响应、并发、账号轮询、持久状态、热重载、管理页面安全头、配置脱敏、账号连通性测试以及 Discover → Inspect → Call 流程。

再次提醒：这是非官方项目。请在部署前确认 QVeris 账号、API key、Vibe 配置和网络暴露范围符合你的使用要求；MIT License 文本见 LICENSE。
