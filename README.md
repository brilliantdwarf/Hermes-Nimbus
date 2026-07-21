# Hermes Halo

实时显示多个 Hermes Agent 运行状态的光环指示器仪表盘。

灵感来源于 [Claude Halo](https://github.com/Houyusu/claude-halo)，为 Hermes Agent 量身定制。

## 功能特性

- 🎨 **多实例支持** — 同时监控多个 Hermes 个体（Profile）
- 🔄 **实时更新** — 基于日志 + state.db 双源检测，500ms 刷新
- 🤖 **自动发现** — 启动时自动扫描 `~/.hermes/profiles/`，无需手动配置
- ✨ **流畅动画** — Canvas 绘制不规则圆环光环动画
- 📊 **首页概览** — 一眼查看所有个体状态
- 🔍 **实例详情** — 点击单个实例查看详细状态历史
- 📱 **响应式设计** — 支持桌面和移动设备
- 🩺 **健康检查** — HTTP API + WebSocket 实时推送

## 状态说明

| 状态 | 颜色 | 说明 |
|------|------|------|
| 空闲 | 灰白 `#aaaaaa` | 正在等待任务 |
| 思考中 | 琥珀 `#ff8830` | 正在处理请求 |
| 输出中 | 金色 `#e8b100` | 正在生成回复 |
| 执行中 | 蓝色 `#3399ff` | 正在调用工具 |
| 等待输入 | 红色 `#ee3333` | 需要用户确认 |
| 已完成 | 绿色 `#33cc55` | 任务已完成 |
| 错误 | 红色 `#ff4444` | 出现错误 |
| 压缩中 | 紫色 `#9944ff` | 正在整理上下文 |

## 快速开始

### 前置条件

- Python 3.10+
- aiohttp

```bash
pip install aiohttp
```

### 启动（使用脚本）

```bash
cd hermes-halo

# 启动（默认端口 8765）
./scripts/start.sh

# 查看状态
./scripts/status.sh

# 停止服务
./scripts/stop.sh

# 自定义端口
./scripts/start.sh 8080
```

### 启动（直接运行）

```bash
python3 scripts/halo_server.py --port 8765
```

### Systemd 用户服务（开机自启）

```bash
# 安装
mkdir -p ~/.config/systemd/user/
cp hermes-halo-ws.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-halo.service

# 查看状态
systemctl --user status hermes-halo.service

# 查看日志
journalctl --user -u hermes-halo.service -f
```

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `HERMES_HALO_CONFIG` | 配置文件路径 | `~/.hermes/hermes-halo/config.json` |
| `HERMES_HALO_PYTHON` | Python 解释器路径 | `python3` |

## 访问地址

| 地址 | 说明 |
|------|------|
| `http://localhost:8765` | 首页（多实例概览） |
| `http://localhost:8765/detail.html` | 实例详情页 |
| `http://localhost:8765/fullscreen.html` | 全屏展示 |
| `http://localhost:8765/api/health` | 健康检查 API |
| `http://localhost:8765/api/states` | 状态数据 API |
| `ws://localhost:8765/ws` | WebSocket 实时推送 |

## 实例发现机制

**从 v2 开始，Hermes Halo 支持自动发现 Profile。** 启动时会自动扫描：

```
~/.hermes/profiles/*/logs/agent.log
~/.hermes/profiles/*/state.db
```

扫描到有效 Profile 后自动写入配置文件并注册到仪表盘。用户无需手动编辑 `config.json`。

### 手动添加（可选）

如果自动发现无法满足需求，也可以编辑配置文件添加实例：

**配置文件路径:** `~/.hermes/hermes-halo/config.json`

```json
{
  "instances": [
    {
      "id": "my-agent",
      "name": "我的 Agent",
      "description": "自定义描述",
      "log_path": "~/.hermes/logs/agent.log",
      "db_path": "~/.hermes/state.db",
      "color": "#3399ff",
      "icon": "🤖"
    }
  ]
}
```

| 字段 | 说明 | 必填 |
|------|------|:----:|
| `id` | 实例唯一标识 | ✅ |
| `name` | 显示名称 | ✅ |
| `description` | 描述信息 | ❌ |
| `log_path` | agent.log 路径 | ✅ |
| `db_path` | state.db 路径 | ❌ |
| `color` | 主题颜色（十六进制） | ❌ |
| `icon` | 显示图标（emoji） | ❌ |

路径中的 `~` 会解析到真实用户 home 目录，不会被 Hermes Profile 的沙箱 HOME 影响。

## 检测原理

通过实时监控每个实例的 `agent.log` 日志文件和 `state.db` 数据库，三级检测确保准确性：

1. **日志实时监控** — 监听 `agent.log` 的新增行，识别关键词（`OpenAI client created`、`tool_executor`、`Turn ended` 等）
2. **数据库查询** — 读取 `state.db` 中最新的会话消息角色和时间戳
3. **进程 CPU 检测** — 回退方案，通过 `ps` 检测对应进程的 CPU 使用率

## API 接口

### HTTP

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 服务健康与实例列表 |
| `/api/states` | GET | 全部实例当前状态 |

### WebSocket 消息

**客户端 → 服务器：**
- `get_states` — 获取所有实例状态
- `get_state {instance_id}` — 获取指定实例状态
- `set_state {instance_id, state}` — 手动设置状态
- `ping` — 心跳检测

**服务器 → 客户端：**
- `states_update` — 所有实例状态更新（自动推送）
- `state_update` — 单个实例状态更新
- `pong` — 心跳响应
- `error` — 错误消息

## 管理命令

```bash
cd ~/hermes-halo

# 查看所有实例状态
python3 scripts/hermes_state.py --list

# 持续监控
python3 scripts/hermes_state.py --watch

# 调试模式（查看日志/DB路径详情）
python3 scripts/hermes_state.py --debug

# 启动/停止/状态
./scripts/start.sh
./scripts/stop.sh
./scripts/status.sh
```

## 项目结构

```
hermes-halo/
├── README.md                 # 本文件
├── LICENSE                   # MIT License
├── config.example.json       # 配置文件示例
├── .gitignore
├── hermes-halo-ws.service    # systemd 用户服务单元
├── scripts/
│   ├── halo_server.py        # HTTP + WebSocket 同端口服务器
│   ├── hermes_state.py       # 多实例状态检测器（含自动发现）
│   ├── hermes_api.py         # Hermes → OpenAI API 兼容包装器
│   ├── start.sh              # 启动脚本
│   ├── stop.sh               # 停止脚本
│   └── status.sh             # 状态检查脚本
└── static/
    ├── index.html            # 首页（多实例概览）
    ├── detail.html           # 实例详情页
    ├── fullscreen.html       # 全屏展示
    └── favicon.svg           # 网站图标
```

## 致谢

- [Claude Halo](https://github.com/Houyusu/claude-halo) — 项目灵感来源
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — 目标应用

## 许可证

MIT License
