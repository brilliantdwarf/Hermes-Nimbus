# Agent Halo

实时显示多个 AI Agent 运行状态的光环指示器。

灵感来源于 [Claude Halo](https://github.com/Houyusu/claude-halo)，为 Hermes Agent 量身定制。

![Agent Halo Preview](https://via.placeholder.com/800x400?text=Agent+Halo+Preview)

## ✨ 功能特性

- 🎨 **多实例支持**: 同时监控多个 Agent 实例
- 📊 **首页概览**: 一眼查看所有实例状态
- 🔄 **实时更新**: 基于日志监控，500ms 刷新
- ✨ **流畅动画**: Canvas 绘制不规则圆环
- 📱 **响应式设计**: 支持桌面和移动设备
- 🖥️ **全屏模式**: 支持全屏展示

## 📊 状态说明

| 状态 | 颜色 | 说明 |
|------|------|------|
| 空闲 | 灰白 `#aaaaaa` | 正在等待任务 |
| 思考中 | 琥珀 `#ff8830` | 正在处理请求 |
| 输出中 | 黄色 `#e8b100` | 正在生成回复 |
| 执行中 | 蓝色 `#3399ff` | 正在调用工具 |
| 等待输入 | 红色 `#ee3333` | 需要用户确认 |
| 已完成 | 绿色 `#33cc55` | 任务已完成 |
| 错误 | 红色 `#ff4444` | 出现错误 |
| 压缩中 | 紫色 `#9944ff` | 正在整理上下文 |

## 🚀 快速开始

### 前置要求

- Python 3.8+
- pip

### 安装

```bash
# 克隆仓库
git clone https://github.com/yourusername/agent-halo.git
cd agent-halo

# 安装依赖
pip install -r requirements.txt

# 复制示例配置
cp config.example.json config.json
```

### 配置

编辑 `config.json`，添加你的 Agent 实例：

```json
{
  "instances": [
    {
      "id": "default",
      "name": "Default Agent",
      "description": "默认 Hermes 代理节点",
      "log_path": "~/.hermes/logs/agent.log",
      "db_path": "~/.hermes/state.db",
      "color": "#3399ff",
      "icon": "🤖"
    },
    {
      "id": "servant",
      "name": "Servant Agent",
      "description": "仆人代理节点",
      "log_path": "~/.hermes/profiles/servant/logs/agent.log",
      "db_path": "~/.hermes/profiles/servant/state.db",
      "color": "#ff8830",
      "icon": "👨‍💼"
    }
  ]
}
```

### 配置字段说明

| 字段 | 说明 | 必填 |
|------|------|------|
| id | 实例唯一标识 | ✅ |
| name | 显示名称 | ✅ |
| description | 描述信息 | ❌ |
| log_path | agent.log 路径 | ✅ |
| db_path | state.db 路径 | ❌ |
| color | 主题颜色 | ❌ |
| icon | 显示图标（emoji） | ❌ |

### 启动服务

```bash
# 使用启动脚本
./scripts/start.sh

# 或手动启动
cd scripts
python3 halo_server.py --port 8765
```

### 访问地址

- **首页（多实例概览）**: http://localhost:8765
- **实例详情**: 点击任意实例卡片进入
- **全屏模式**: http://localhost:8765/fullscreen.html

## 📁 项目结构

```
agent-halo/
├── config.example.json      # 示例配置
├── requirements.txt         # Python 依赖
├── LICENSE                  # MIT 许可证
├── README.md               # 项目说明
├── .gitignore
├── scripts/
│   ├── hermes_state.py      # 多实例状态检测器
│   ├── halo_server.py       # HTTP + WebSocket 服务器
│   ├── start.sh             # 启动脚本
│   ├── stop.sh              # 停止脚本
│   └── status.sh            # 状态检查
└── static/
    ├── index.html           # 首页（多实例概览）
    ├── detail.html          # 实例详情页
    ├── fullscreen.html      # 全屏模式
    └── favicon.svg          # 图标
```

## 🔍 检测原理

通过实时监控每个实例的 `agent.log` 日志文件：

- `Turn ended` → 已完成
- `conversation turn:` → 思考中
- `OpenAI client created` → 思考中
- `stream_request_complete` → 思考中
- `tool_executor: tool xxx started` → 执行中
- `tool_executor: tool xxx completed` → 思考中
- 60秒无活动 → 空闲

## 📡 API 接口

### WebSocket 消息

#### 客户端 → 服务器

- `get_states`: 获取所有实例状态
- `get_state`: 获取指定实例 `{type: "get_state", instance_id: "xxx"}`
- `set_state`: 设置状态 `{type: "set_state", instance_id: "xxx", state: "thinking"}`
- `ping`: 心跳检测

#### 服务器 → 客户端

- `states_update`: 所有实例状态更新
- `state_update`: 单个实例状态更新
- `pong`: 心跳响应
- `error`: 错误消息

## 🛠️ 管理命令

```bash
# 查看状态
./scripts/status.sh

# 停止服务
./scripts/stop.sh

# 启动服务
./scripts/start.sh

# 测试状态检测
python3 scripts/hermes_state.py --list
```

## 🔧 命令行参数

```bash
python3 scripts/halo_server.py [OPTIONS]

Options:
  --host TEXT    监听地址 (默认: 0.0.0.0)
  --port INTEGER 监听端口 (默认: 8765)
  --config TEXT  配置文件路径
  --help         显示帮助信息
```

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建你的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交你的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开一个 Pull Request

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

## 🙏 致谢

- [Claude Halo](https://github.com/Houyusu/claude-halo) - 项目灵感来源
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) - 目标应用
- [aiohttp](https://docs.aiohttp.org/) - 异步 HTTP 框架
