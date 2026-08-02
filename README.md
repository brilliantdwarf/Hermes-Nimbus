# Hermes Nimbus

> **Nimbus** — 神祇身后的灵光晕轮，实时映照 Hermes Agent 的每一次脉动。
> **Nimbus** — A real-time status halo for every Hermes Agent profile.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)

<p align="center">
  <img src="static/halo-states.svg" alt="Hermes Nimbus status halo legend" width="680">
</p>

Hermes Nimbus 是一个轻量级的多 Profile 状态仪表盘。它优先接收 Hermes 官方插件
Hook 产生的生命周期事件，并用日志、`state.db` 和进程存活信息作为兼容与恢复来源。
页面通过 HTTP 与 WebSocket 实时更新，不会读取或上传对话正文、工具参数或工具结果。

## 功能

- 同时监控默认 Hermes 实例和 `~/.hermes/profiles/` 下的多个 Profile
- 显示空闲、思考、输出、执行工具、等待输入、完成、错误和上下文压缩状态
- 事件优先的确定性状态机，支持并行工具、乱序事件和重复事件去重
- Hermes Hook 投递失败重试、30 秒活动心跳、退出前有界队列刷新
- 日志与数据库回退；不再用不可靠的 CPU 占用推断个体活动
- 默认仅监听 `127.0.0.1`，局域网监听必须配置客户端 IP/CIDR 白名单
- WebSocket 协议只读，并校验浏览器 Origin
- Canvas 动画首页、详情页和全屏展示，保持原有显示样式

## 状态说明

| 状态 | 颜色 | 含义 |
| --- | --- | --- |
| 空闲 | 灰白 `#aaaaaa` | 等待任务 |
| 思考中 | 琥珀 `#ff8830` | 正在处理请求 |
| 输出中 | 金色 `#e8b100` | 正在生成回复 |
| 执行中 | 蓝色 `#3399ff` | 正在调用工具 |
| 等待输入 | 红色 `#ee3333` | 等待澄清或审批 |
| 已完成 | 绿色 `#33cc55` | 最近一轮成功结束 |
| 错误 | 红色 `#ff4444` | 最近一轮失败 |
| 压缩中 | 紫色 `#9944ff` | 正在整理上下文 |

## 快速开始

要求 Python 3.10+，并且本机已经安装 Hermes Agent。

```bash
git clone https://github.com/brilliantdwarf/Hermes-Nimbus.git hermes-nimbus
cd hermes-nimbus

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

./scripts/start.sh
```

打开 `http://127.0.0.1:8765`。管理命令：

```bash
./scripts/status.sh
./scripts/stop.sh
```

Nimbus 启动时会自动发现默认实例及 `~/.hermes/profiles/` 下的 Profile，并将运行配置写到
`~/.hermes/hermes-nimbus/config.json`。如需固定显示名称、颜色或图标，可复制并编辑
[`config.example.json`](config.example.json)。仓库根目录的 `config.json` 已被忽略，避免误传本机配置。

## 安全的局域网访问

默认启动只监听 loopback。若 Nimbus 所在机器的局域网地址为 `192.168.1.10`，只允许
客户端 `192.168.1.20` 访问，可运行：

```bash
./scripts/start.sh 8765 127.0.0.1 192.168.1.10 192.168.1.20
```

也可以直接运行：

```bash
python3 scripts/halo_server.py \
  --host 127.0.0.1 \
  --lan-host 192.168.1.10 \
  --allow-client 192.168.1.20 \
  --port 8765
```

`--allow-client` 支持单个 IP 或 CIDR，并可重复指定。白名单使用 TCP 来源地址，不信任
`X-Forwarded-For`。远程浏览器还必须发送与 Nimbus 页面地址一致的 WebSocket Origin。
这是应用层访问控制，不替代主机防火墙或 TLS；请仅在可信局域网中直接暴露 HTTP 服务。

## 安装 Hermes Hook 插件

只使用日志和数据库时 Nimbus 仍能工作，但不能可靠获得工具开始、并行工具数量、等待审批
等完整生命周期。建议为每个需要监控的 Hermes Profile 安装插件。

默认 Profile：

```bash
mkdir -p ~/.hermes/plugins/hermes-nimbus
cp integrations/hermes-nimbus/plugin.yaml integrations/hermes-nimbus/__init__.py \
  ~/.hermes/plugins/hermes-nimbus/
hermes plugins enable hermes-nimbus
```

命名 Profile：

```bash
PROFILE=research_assistant
mkdir -p ~/.hermes/profiles/"$PROFILE"/plugins/hermes-nimbus
cp integrations/hermes-nimbus/plugin.yaml integrations/hermes-nimbus/__init__.py \
  ~/.hermes/profiles/"$PROFILE"/plugins/hermes-nimbus/
hermes -p "$PROFILE" plugins enable hermes-nimbus
```

重启正在运行的对应 gateway 后插件生效；停止的 gateway 无需启动，下次启动会自动加载。
命令行交互同样可以显示状态，只要该命令使用的 Profile 已启用插件。插件只发送 Profile、
Session、Turn、Tool 和 Request 标识及生命周期结果，不发送消息正文、历史、工具参数、结果或错误正文。

投递默认使用 `http://127.0.0.1:8765/api/events`，失败最多尝试 3 次。活动 Session 每
30 秒续租，Nimbus 在 90 秒未收到事件或心跳时释放遗留活动状态。

## Systemd 用户服务

仓库提供的服务文件默认仅允许本机访问，并使用快速开始中创建的 `.venv`：

```bash
mkdir -p ~/.config/systemd/user
cp hermes-nimbus.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-nimbus.service
systemctl --user status hermes-nimbus.service
```

服务可从可选的 `~/.config/hermes-nimbus.env` 读取 Token 等环境变量。若需要局域网监听，
请在复制后的服务文件中为 `ExecStart` 增加 `--lan-host` 与至少一个 `--allow-client`，然后
执行 `systemctl --user daemon-reload` 和 `systemctl --user restart hermes-nimbus.service`。

## 环境变量

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `HERMES_NIMBUS_CONFIG` | `~/.hermes/hermes-nimbus/config.json` | Nimbus 配置文件 |
| `HERMES_NIMBUS_HOME` | 当前登录用户 Home | 显式指定 Hermes 数据所属 Home |
| `HERMES_NIMBUS_PYTHON` | `python3` | `start.sh` 使用的解释器 |
| `HERMES_NIMBUS_EVENT_URL` | `http://127.0.0.1:8765/api/events` | 插件事件入口 |
| `HERMES_NIMBUS_EVENT_TOKEN` | 空 | 事件入口与插件共用的 Bearer Token |

旧的 `HERMES_HALO_*` 变量仍作为兼容别名，但新部署应使用 `HERMES_NIMBUS_*`。
未设置事件 Token 时，`POST /api/events` 只接受 loopback 请求。若通过反向代理暴露事件入口，
必须配置高强度 Token，并在代理层同时启用 TLS 和访问控制。

## API 与 WebSocket

| 地址 | 用途 |
| --- | --- |
| `GET /api/health` | 服务健康、实例和连接数 |
| `GET /api/states` | 所有实例状态快照 |
| `POST /api/events` | Hermes 生命周期事件入口 |
| `GET /ws` | WebSocket 状态推送 |

客户端可发送的 WebSocket 消息只有：

- `get_states`
- `get_state {instance_id}`
- `ping`

服务端会发送 `states_update`、`state_update`、`pong` 或 `error`。协议不提供远程手动状态修改。

## 状态判断顺序

1. Hermes Hook、TUI gateway 或 Runs API 的规范化生命周期事件。
2. Hermes 日志增量，用于旧版本兼容和事件生产者重启后的恢复。
3. `state.db` 最近完成快照。
4. 进程仅用于判断实例是否在线，不使用 CPU 占用猜测活动状态。

规范化事件支持 `turn.*`、`model.*`、`tool.*`、`input.*`、`compression.*` 和 `heartbeat`。
状态机按 Profile 与 Session 隔离，能够处理并行工具、重复事件及部分乱序终止事件。

## 测试

```bash
python scripts/test.py
```

测试覆盖状态机、日志/数据库检测、Hook 隐私边界、投递重试、心跳租约、HTTP/WebSocket
访问控制和静态页面 XSS 回归检查。

## 项目结构

```text
hermes-nimbus/
├── integrations/hermes-nimbus/  # Hermes 官方 Hook 适配插件
├── scripts/
│   ├── halo_server.py            # HTTP、WebSocket 与事件入口
│   ├── hermes_state.py           # 多 Profile 检测与自动发现
│   ├── state_model.py            # 确定性事件状态机
│   └── test.py                   # 回归测试入口
├── static/                       # 原生 HTML/Canvas 界面
├── tests/                        # 单元和协议测试
├── config.example.json
├── hermes-nimbus.service
└── requirements.txt
```

## English

Hermes Nimbus is a real-time status dashboard for multiple Hermes Agent profiles. It uses
official Hermes lifecycle hooks as the authoritative source and falls back to incremental logs,
`state.db`, and process availability for compatibility and recovery.

Key properties:

- deterministic per-session state tracking, including parallel tools and approval waits;
- privacy-safe event payloads with no conversation text, tool arguments, or tool results;
- retry, bounded shutdown flush, 30-second heartbeat, and a 90-second server-side lease;
- loopback-only defaults, explicit LAN client allowlists, read-only WebSocket messages, and
  browser Origin validation;
- unchanged Canvas halo visuals for dashboard, detail, and fullscreen views.

Quick start:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./scripts/start.sh
```

For LAN access, pass the server address and the only allowed client explicitly:

```bash
./scripts/start.sh 8765 127.0.0.1 192.168.1.10 192.168.1.20
```

Install `integrations/hermes-nimbus` into each Hermes Profile that should publish authoritative
events, enable it with `hermes plugins enable hermes-nimbus`, and restart only gateways that are
already running. CLI sessions are supported as well because status comes from profile hooks, not
from the gateway process alone.

Run the regression suite with `python scripts/test.py`. See the Chinese sections above for the
complete service, security, API, and plugin setup.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [Claude Halo](https://github.com/Houyusu/claude-halo), the visual inspiration for the halo animation

## License

[MIT](LICENSE)
