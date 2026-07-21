#!/usr/bin/env python3
"""Hermes 状态检测器 - 支持多实例，基于日志 + state.db 双源检测"""

import sqlite3
import json
import re
import time
import os
import pwd
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List


def real_user_home() -> Path:
    """Resolve the real login home, not a profile-scoped HOME sandbox."""
    for key in ('HERMES_HALO_HOME', 'SUDO_USER'):
        # Prefer an explicit override when present.
        if key == 'HERMES_HALO_HOME' and os.environ.get(key):
            return Path(os.environ[key]).expanduser()
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        pass
    # Fallback: if HOME looks like a Hermes profile sandbox, climb back to the real home.
    home = Path(os.environ.get('HOME', '') or Path.home())
    parts = home.parts
    if '.hermes' in parts and 'profiles' in parts:
        try:
            idx = parts.index('.hermes')
            if idx > 0:
                return Path(*parts[:idx])
        except Exception:
            pass
    return home


def expand_user_path(path: str | os.PathLike) -> Path:
    """Expand ~ using the real user home so profile agents still find host configs/logs."""
    text = str(path)
    if text == '~':
        return real_user_home()
    if text.startswith('~/') or text.startswith('~\\'):
        return real_user_home() / text[2:]
    return Path(text).expanduser()


DEFAULT_CONFIG_PATH = real_user_home() / '.hermes' / 'hermes-halo' / 'config.json'

# 自动发现时使用的默认配色方案（按索引循环）
DEFAULT_PROFILE_COLORS = [
    '#3399ff', '#ff8830', '#33cc55', '#9944ff', '#ee3333',
    '#ff66aa', '#00cccc', '#ffaa00', '#6666ff', '#cc66ff',
]
DEFAULT_PROFILE_ICONS = [
    '🤖', '👨‍💼', '🔬', '👤', '👔', '✍️', '🛠️', '📊', '🎯', '🧩',
]


class HermesInstance:
    """单个 Hermes 实例的状态检测器"""

    # 状态配置 - 与 Claude Halo 完全一致
    STATES = {
        'idle': {
            'color': '#aaaaaa', 'halo': '#cccccc', 'period': 6.0,
            'dashes': [60, 30], 'ms': 0, 'md': 0,
            'amin': 0.30, 'amax': 0.42, 'br': 0, 'rp': 0, 'rpperiod': 0,
            'label': '空闲', 'description': '正在等待任务'
        },
        'thinking': {
            'color': '#ff8830', 'halo': '#ffdbb8', 'period': 2.4,
            'dashes': [70, 35, 45, 30, 25, 20], 'ms': 0.6, 'md': 0.4,
            'amin': 0.45, 'amax': 0.90, 'br': 5.2, 'rp': 0, 'rpperiod': 0,
            'label': '思考中', 'description': '正在推理分析'
        },
        'streaming': {
            'color': '#e8b100', 'halo': '#fff0aa', 'period': 2.0,
            'dashes': [60, 25, 45, 20, 35, 15], 'ms': 0.7, 'md': 0.30,
            'amin': 0.50, 'amax': 0.85, 'br': 3.5, 'rp': 0, 'rpperiod': 0,
            'label': '输出中', 'description': '正在生成回复'
        },
        'executing': {
            'color': '#3399ff', 'halo': '#bbddff', 'period': 1.3,
            'dashes': [50, 25, 20, 20, 35, 25, 25, 22], 'ms': 1.2, 'md': 0.28,
            'amin': 0.60, 'amax': 0.90, 'br': 0, 'rp': 0, 'rpperiod': 0,
            'label': '执行中', 'description': '正在调用工具'
        },
        'input_needed': {
            'color': '#ee3333', 'halo': '#ffcccc', 'period': 2.8,
            'dashes': [80, 50, 30, 25], 'ms': 1.8, 'md': 0.5,
            'amin': 0.52, 'amax': 0.94, 'br': 2.0, 'rp': 0, 'rpperiod': 0,
            'label': '等待输入', 'description': '需要用户确认'
        },
        'completed': {
            'color': '#33cc55', 'halo': '#bbffcc', 'period': 5.0,
            'dashes': [70, 35, 45, 30, 25, 20], 'ms': 0.5, 'md': 0.3,
            'amin': 0.38, 'amax': 0.84, 'br': 6.0, 'rp': 0, 'rpperiod': 0,
            'label': '已完成', 'description': '任务已完成'
        },
        'error': {
            'color': '#ff4444', 'halo': '#ffcccc', 'period': 1.5,
            'dashes': [40, 20, 30, 15], 'ms': 2.0, 'md': 0.6,
            'amin': 0.50, 'amax': 0.95, 'br': 1.5, 'rp': 0, 'rpperiod': 0,
            'label': '错误', 'description': '出现错误'
        },
        'compacting': {
            'color': '#9944ff', 'halo': '#ddccff', 'period': 2.1,
            'dashes': [35, 20, 35, 20, 35, 20], 'ms': 0.4, 'md': 0.25,
            'amin': 0.38, 'amax': 0.80, 'br': 4.0, 'rp': 0.12, 'rpperiod': 1.6,
            'label': '压缩中', 'description': '正在整理上下文'
        }
    }

    # 时间窗口配置（秒）
    IDLE_TIMEOUT = 60           # 60秒无活动视为空闲
    COMPLETED_HOLD = 30         # 完成状态保持30秒
    ACTIVE_WINDOW = 120         # 2分钟内的活动视为活跃

    def __init__(self, config: Dict):
        self.id = config['id']
        self.name = config['name']
        self.description = config.get('description', '')
        self.icon = config.get('icon', '🤖')
        self.color = config.get('color', '#3399ff')

        self.log_path = expand_user_path(config['log_path'])
        self.db_path = expand_user_path(config['db_path']) if config.get('db_path') else None

        self.current_state = 'idle'
        self.last_check = None
        self.state_history: List[Dict] = []

        # 日志监控状态
        self._last_log_position = 0
        self._last_activity_time = 0
        self._turn_ended_time = 0
        self._api_call_active = False
        self._streaming_active = False
        self._tool_executing = False
        self._last_event = None
        self._last_db_check = 0

        # 稳定保护：记录上次日志位置无效的次数
        self._position_stall_count = 0

        # 初始化：读取最近的日志确定初始状态
        self._init_from_recent_logs()

    def _find_log_path(self):
        """智能查找日志路径，支持配置文件路径不存在时自动发现"""
        if self.log_path and self.log_path.exists():
            return self.log_path

        # 尝试根据 profile 名称自动发现
        candidates = []
        profile_id = self.id

        # 默认 profile
        if profile_id == 'default':
            candidates.extend([
                Path.home() / '.hermes' / 'logs' / 'agent.log',
            ])
        else:
            candidates.extend([
                Path.home() / '.hermes' / 'profiles' / profile_id / 'logs' / 'agent.log',
            ])

        # Gateway 日志作为备选
        if profile_id == 'default':
            candidates.append(Path.home() / '.hermes' / 'logs' / 'gateway.log')

        for p in candidates:
            if p.exists():
                self.log_path = p
                break

        return self.log_path

    def _find_db_path(self):
        """智能查找 state.db 路径"""
        if self.db_path and self.db_path.exists():
            return self.db_path

        profile_id = self.id
        if profile_id == 'default':
            candidates = [
                Path.home() / '.hermes' / 'state.db',
            ]
        else:
            candidates = [
                Path.home() / '.hermes' / 'profiles' / profile_id / 'state.db',
            ]

        for p in candidates:
            if p.exists():
                self.db_path = p
                break

        return self.db_path

    def _init_from_recent_logs(self):
        """从最近的日志初始化状态"""
        log_path = self._find_log_path()
        self._find_db_path()

        try:
            if not log_path or not log_path.exists():
                return

            # 读取最后 50 行日志
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                recent_lines = lines[-50:] if len(lines) > 50 else lines

            # 从末尾开始
            self._last_log_position = log_path.stat().st_size

            # 分析最近的日志
            last_event = None
            last_event_time = 0

            for line in reversed(recent_lines):
                line = line.strip()
                if not line:
                    continue

                # 提取时间戳
                time_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                if time_match:
                    try:
                        event_time = datetime.strptime(time_match.group(1), '%Y-%m-%d %H:%M:%S')
                        event_timestamp = event_time.timestamp()
                    except Exception:
                        continue
                else:
                    continue

                events_to_check = [
                    'Turn ended',            # turn_ended
                    'OpenAI client created',  # api_start (will be overridden if later events found)
                    'stream_request_complete', # stream_end
                    'conversation turn:',     # user_message (also api_start trigger)
                ]

                for keyword in events_to_check:
                    if keyword in line:
                        last_event_time = event_timestamp
                        if keyword == 'Turn ended':
                            last_event = 'turn_ended'
                        elif keyword == 'conversation turn:':
                            last_event = 'user_message'
                        elif keyword == 'stream_request_complete':
                            last_event = 'stream_end'
                        elif keyword == 'OpenAI client created':
                            last_event = 'api_start'
                        break

                if last_event:
                    break

            # 根据最后事件设置初始状态
            if last_event and last_event_time:
                self._last_event = last_event
                age = time.time() - last_event_time

                if age < self.ACTIVE_WINDOW:
                    self._last_activity_time = last_event_time

                    if last_event == 'turn_ended':
                        if age < self.COMPLETED_HOLD:
                            self.current_state = 'completed'
                            self._turn_ended_time = last_event_time
                        else:
                            self.current_state = 'idle'
                    elif last_event in ('api_start', 'user_message'):
                        self.current_state = 'thinking'
                        self._api_call_active = True
                    elif last_event == 'stream_end':
                        self.current_state = 'thinking'
                        self._api_call_active = True
                else:
                    self.current_state = 'idle'

        except Exception as e:
            print(f"初始化日志分析错误: {e}")

    def _read_new_log_lines(self) -> List[str]:
        """读取新的日志行，支持日志轮转"""
        log_path = self._find_log_path()
        if not log_path or not log_path.exists():
            return []

        try:
            current_size = log_path.stat().st_size

            # 日志轮转：文件变小了 → 复位到 0
            if current_size < self._last_log_position:
                self._last_log_position = 0
                return []

            if current_size <= self._last_log_position:
                return []

            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(self._last_log_position)
                new_lines = f.readlines()
                self._last_log_position = f.tell()

            return new_lines

        except Exception:
            return []

    def _parse_log_line(self, line: str) -> Optional[str]:
        """解析日志行，返回状态事件"""
        line = line.strip()
        if not line:
            return None

        # Turn ended - 一轮对话结束
        if 'Turn ended' in line:
            return 'turn_ended'

        # 用户消息到达
        if 'conversation turn:' in line:
            return 'user_message'

        # API 调用开始 (agent_init 或 chat_completion)
        if 'OpenAI client created' in line:
            return 'api_start'

        # 流式请求完成（必须在 api_end 之前）
        if 'stream_request_complete' in line:
            return 'stream_end'

        # API 调用结束（普通关闭，不含 stream_request_complete）
        if 'OpenAI client closed' in line:
            return 'api_end'

        # 工具执行完成
        if 'tool_executor' in line and 'completed' in line:
            return 'tool_completed'

        # 工具执行开始（新格式已无此日志，保留兼容）
        if 'tool_executor' in line and 'started' in line:
            return 'tool_started'

        # 工具错误
        if 'tool_executor' in line and ('error' in line.lower() or 'returned error' in line):
            return 'tool_error'

        # 压缩
        if 'compacting' in line.lower() or 'context compression' in line.lower():
            return 'compacting'

        # 错误
        if 'ERROR' in line:
            return 'error'

        return None

    def _detect_from_db(self) -> Optional[str]:
        """从 state.db 检测状态（增强版）"""
        db_path = self._find_db_path()
        now = time.time()

        # 每 10 秒最多查一次 DB
        if self._last_db_check > 0 and (now - self._last_db_check) < 10:
            return None
        self._last_db_check = now

        if not db_path or not db_path.exists():
            return None

        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 1. 查找最近活跃的 session
            cursor.execute("""
                SELECT id, started_at, message_count, api_call_count, title
                FROM sessions 
                WHERE (ended_at IS NULL OR ended_at = 0)
                  AND message_count > 0
                ORDER BY started_at DESC LIMIT 1
            """)
            active_session = cursor.fetchone()

            if not active_session:
                conn.close()
                # 所有 session 已结束，检查最后结束时间
                cursor.execute("""
                    SELECT ended_at FROM sessions 
                    WHERE ended_at IS NOT NULL AND ended_at > 0
                    ORDER BY ended_at DESC LIMIT 1
                """)
                last = cursor.fetchone()
                if last:
                    elapsed = now - last['ended_at']
                    session_id = active_session['id'] if active_session else ''
                    if elapsed < self.COMPLETED_HOLD:
                        conn.close()
                        return 'completed'
                conn.close()
                return 'idle'

            session_id = active_session['id']

            # 2. 获取该 session 的最新消息
            cursor.execute("""
                SELECT role, timestamp, content
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            latest = cursor.fetchone()

            if not latest:
                conn.close()
                return 'idle'

            msg_age = now - latest['timestamp']

            # 3. 根据最新消息角色 + 时间判断状态
            if msg_age < 5:
                # 5秒内的消息 → 正在处理中
                role = latest['role']
                if role == 'user':
                    conn.close()
                    return 'thinking'
                elif role == 'assistant':
                    conn.close()
                    return 'streaming'
                elif role == 'tool':
                    conn.close()
                    return 'executing'
            elif msg_age < 30:
                # 30秒内 → 可能是思考或执行中
                content_preview = (latest['content'] or '')[:50]
                if 'tool_call' in content_preview.lower() or 'function' in content_preview.lower():
                    conn.close()
                    return 'executing'
                conn.close()
                return 'thinking'
            elif msg_age < 120:
                # 60秒内有活动但不是最近 → 已完成
                if latest['role'] == 'assistant':
                    conn.close()
                    return 'completed'
                conn.close()
                return 'thinking'

            conn.close()
            return None

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return None

    def _detect_from_process(self) -> Optional[str]:
        """检测当前 agent 进程的 CPU 活动来判断状态"""
        log_path = self._find_log_path()
        if not log_path or not log_path.exists():
            return None

        try:
            # 查找正在运行的这个 profile 相关的 Python 进程
            profile_id = self.id
            if profile_id == 'default':
                # 默认 agent 进程是最活跃的 Python 进程
                result = subprocess.run(
                    ['ps', '-eo', 'pid,pcpu,comm', '--no-headers'],
                    capture_output=True, text=True, timeout=5
                )
                # 查找 hermes-agent 相关的 Python 进程
                for line in result.stdout.strip().split('\n'):
                    if 'python' in line.lower() and float(line.split()[1]) > 10:
                        return 'thinking'
                return None

            # 对非 default profile，检查具体进程
            result = subprocess.run(
                ['pgrep', '-f', f'profiles/{profile_id}'],
                capture_output=True, text=True, timeout=5
            )
            pids = result.stdout.strip().split()
            if not pids:
                return None  # 无进程运行

            for pid in pids:
                try:
                    cpu_info = subprocess.run(
                        ['ps', '-p', pid, '-o', 'pcpu=', '--no-headers'],
                        capture_output=True, text=True, timeout=3
                    )
                    cpu = float(cpu_info.stdout.strip() or '0')
                    if cpu > 10:
                        return 'executing'
                    elif cpu > 3:
                        return 'thinking'
                except Exception:
                    continue
            return None

        except Exception:
            return None

    def detect_state(self) -> str:
        """检测当前状态（三级检测）"""
        log_path = self._find_log_path()

        # ========= 1级：日志实时监控（最灵敏） =========
        if log_path and log_path.exists():
            state = self.detect_from_log()
            if state:
                self.current_state = state
                return state

        # ========= 2级：state.db 查询（最可靠） =========
        db_path = self._find_db_path()
        if db_path and db_path.exists():
            state = self._detect_from_db()
            if state:
                self.current_state = state
                return state

        # ========= 3级：进程 CPU 活性检测（备用） =========
        state = self._detect_from_process()
        if state:
            self.current_state = state
            return state

        # ========= 兜底：保持当前状态 =========
        now = time.time()

        # 检查是否超时回到 idle
        if self._last_activity_time > 0:
            elapsed = now - self._last_activity_time
            if elapsed > self.IDLE_TIMEOUT:
                self._api_call_active = False
                self._streaming_active = False
                self._tool_executing = False
                self.current_state = 'idle'
                return 'idle'

        # 检查 completed 保持期
        if self._turn_ended_time > 0:
            elapsed = now - self._turn_ended_time
            if elapsed < self.COMPLETED_HOLD:
                return 'completed'
            else:
                self._turn_ended_time = 0
                self.current_state = 'idle'
                return 'idle'

        return self.current_state

    def detect_from_log(self) -> Optional[str]:
        """从日志实时检测状态（仅日志源）"""
        new_lines = self._read_new_log_lines()

        now = time.time()

        # 处理新日志行
        if new_lines:
            for line in new_lines:
                event = self._parse_log_line(line)
                if event:
                    self._last_event = event
                    self._last_activity_time = now

                    # completed 保持期：忽略所有事件（除了 user_message 开启新轮次）
                    if self._turn_ended_time > 0 and (now - self._turn_ended_time) < self.COMPLETED_HOLD:
                        if event == 'user_message':
                            self._turn_ended_time = 0
                            return 'thinking'
                        continue

                    # 更新状态标志
                    if event == 'turn_ended':
                        self._turn_ended_time = now
                        self._api_call_active = False
                        self._streaming_active = False
                        self._tool_executing = False
                        return 'completed'

                    elif event == 'user_message':
                        self._turn_ended_time = 0
                        return 'thinking'

                    elif event == 'api_start':
                        self._api_call_active = True
                        self._streaming_active = False
                        self._turn_ended_time = 0
                        return 'thinking'

                    elif event == 'api_end':
                        self._api_call_active = False
                        if self._streaming_active:
                            return 'streaming'
                        if self._tool_executing:
                            return 'executing'
                        return 'thinking'

                    elif event == 'stream_end':
                        self._streaming_active = False
                        if self._tool_executing:
                            return 'executing'
                        return 'thinking'

                    elif event == 'tool_started':
                        self._tool_executing = True
                        return 'executing'

                    elif event == 'tool_completed':
                        self._tool_executing = False
                        return 'thinking'

                    elif event == 'tool_error':
                        return 'error'

                    elif event == 'compacting':
                        return 'compacting'

                    elif event == 'error':
                        return 'error'

        # 没有新日志，根据时间判断
        if self._turn_ended_time > 0:
            elapsed = now - self._turn_ended_time
            if elapsed < self.COMPLETED_HOLD:
                return 'completed'
            else:
                self._turn_ended_time = 0
                self._api_call_active = False
                self._streaming_active = False
                self._tool_executing = False
                return 'idle'

        if self._last_activity_time > 0:
            elapsed = now - self._last_activity_time
            if elapsed > self.IDLE_TIMEOUT:
                self._api_call_active = False
                self._streaming_active = False
                self._tool_executing = False
                return 'idle'
            elif elapsed > 10:
                if self._last_event == 'api_start':
                    if elapsed > 30:
                        return 'idle'
                    return 'thinking'
                elif self._last_event == 'stream_end':
                    if elapsed > 15:
                        return 'idle'
                    return 'streaming'
                elif self._last_event == 'tool_completed':
                    return 'thinking'
            elif elapsed < 5:
                if self._api_call_active:
                    return 'thinking'
                if self._streaming_active:
                    return 'streaming'
                if self._tool_executing:
                    return 'executing'

        return None

    def get_state_info(self) -> Dict[str, Any]:
        """获取完整的状态信息"""
        state = self.detect_state()
        self.last_check = datetime.now()

        # 记录状态历史
        if not self.state_history or self.state_history[-1]['state'] != state:
            self.state_history.append({
                'state': state,
                'timestamp': self.last_check.isoformat()
            })

        if len(self.state_history) > 50:
            self.state_history = self.state_history[-50:]

        state_config = self.STATES.get(state, self.STATES['idle'])

        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'icon': self.icon,
            'color': self.color,
            'state': state,
            'state_config': state_config,
            'timestamp': self.last_check.isoformat(),
            'history': self.state_history[-10:]
        }

    def set_state(self, state: str) -> bool:
        """手动设置状态"""
        if state not in self.STATES:
            return False
        try:
            state_file = self.log_path.parent.parent / 'hermes_state.txt'
            state_file.write_text(state)
            self.current_state = state
            return True
        except Exception:
            return False


class HermesMultiDetector:
    """多实例状态管理器"""

    def __init__(self, config_path: str | os.PathLike | None = None):
        self.config_path = expand_user_path(config_path or DEFAULT_CONFIG_PATH)
        self.instances: Dict[str, HermesInstance] = {}
        self.load_config()
        # 自动发现未配置的 Profile
        self._auto_discover_profiles()

    def load_config(self):
        """加载配置文件"""
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                for instance_config in config.get('instances', []):
                    instance = HermesInstance(instance_config)
                    self.instances[instance.id] = instance
                if not self.instances:
                    self._create_default_config()
                    self._save_config()
            else:
                # 自动发现：先加默认，再扫描 profiles
                self._create_default_config()
                self._auto_discover_profiles()
                self._save_config()
        except Exception as e:
            print(f"加载配置失败: {e}")
            self._create_default_config()
            self._auto_discover_profiles()
            self._save_config()

    def _save_config(self):
        """保存当前实例配置到文件"""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            instances = []
            for inst in self.instances.values():
                # 相对于 ~ 的路径更便携
                log_rel = str(inst.log_path).replace(str(real_user_home()), '~') if inst.log_path else ''
                db_rel = str(inst.db_path).replace(str(real_user_home()), '~') if inst.db_path else ''
                instances.append({
                    'id': inst.id,
                    'name': inst.name,
                    'description': inst.description,
                    'log_path': log_rel,
                    'db_path': db_rel,
                    'color': inst.color,
                    'icon': inst.icon,
                })
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump({'instances': instances}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"保存配置失败: {e}")

    def _auto_discover_profiles(self):
        """自动扫描 ~/.hermes/profiles/ 发现未配置的 Profile"""
        profiles_dir = real_user_home() / '.hermes' / 'profiles'
        if not profiles_dir.exists():
            return

        new_count = 0
        for profile_dir in sorted(profiles_dir.iterdir()):
            if not profile_dir.is_dir():
                continue
            profile_id = profile_dir.name

            # 跳过已配置的
            if profile_id in self.instances:
                continue

            # 检查是否有有效的日志或 state.db
            log_path = profile_dir / 'logs' / 'agent.log'
            db_path = profile_dir / 'state.db'
            if not log_path.exists() and not db_path.exists():
                continue

            # 自动生成配置
            idx = len(self.instances) + new_count
            color = DEFAULT_PROFILE_COLORS[idx % len(DEFAULT_PROFILE_COLORS)]
            icon = DEFAULT_PROFILE_ICONS[idx % len(DEFAULT_PROFILE_ICONS)]
            name = profile_id.capitalize()

            instance_config = {
                'id': profile_id,
                'name': name,
                'description': f'{name} 实例',
                'log_path': f'~/.hermes/profiles/{profile_id}/logs/agent.log',
                'db_path': f'~/.hermes/profiles/{profile_id}/state.db',
                'color': color,
                'icon': icon,
            }
            instance = HermesInstance(instance_config)
            self.instances[profile_id] = instance
            new_count += 1
            print(f"  [自动发现] 新增 Profile: {profile_id}")

        if new_count > 0:
            self._save_config()

    def _create_default_config(self):
        """创建默认配置（并自动发现其他 Profile）"""
        default_instance = HermesInstance({
            'id': 'default',
            'name': '默认',
            'description': '默认 Hermes 节点',
            'log_path': str(real_user_home() / '.hermes' / 'logs' / 'agent.log'),
            'db_path': str(real_user_home() / '.hermes' / 'state.db'),
            'color': '#3399ff',
            'icon': '🤖'
        })
        self.instances['default'] = default_instance
        self._auto_discover_profiles()

    def get_all_states(self) -> List[Dict[str, Any]]:
        """获取所有实例的状态"""
        states = []
        for instance in self.instances.values():
            states.append(instance.get_state_info())
        return states

    def get_instance_state(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """获取指定实例的状态"""
        instance = self.instances.get(instance_id)
        if instance:
            return instance.get_state_info()
        return None

    def set_instance_state(self, instance_id: str, state: str) -> bool:
        """设置指定实例的状态"""
        instance = self.instances.get(instance_id)
        if instance:
            return instance.set_state(state)
        return False

    def get_instance_ids(self) -> List[str]:
        """获取所有实例 ID"""
        return list(self.instances.keys())


# 命令行测试
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Hermes 多实例状态检测器')
    parser.add_argument('--list', action='store_true', help='列出所有实例状态')
    parser.add_argument('--instance', help='查看指定实例状态')
    parser.add_argument('--watch', action='store_true', help='持续监控所有实例')
    parser.add_argument('--debug', action='store_true', help='显示调试信息')
    args = parser.parse_args()

    detector = HermesMultiDetector()

    if args.debug:
        print("=== 日志监控调试 ===")
        for instance_id, instance in detector.instances.items():
            print(f"\n--- {instance.name} ({instance_id}) ---")
            print(f"日志文件: {instance.log_path}")
            print(f"日志存在: {instance.log_path.exists()}")
            print(f"DB文件: {instance.db_path}")
            print(f"DB存在: {instance.db_path.exists() if instance.db_path else '?'}")
            print(f"初始状态: {instance.current_state}")
            print(f"最后活动: {instance._last_activity_time}")
            print(f"Turn ended: {instance._turn_ended_time}")
            print(f"API 调用活跃: {instance._api_call_active}")
            print(f"流式输出活跃: {instance._streaming_active}")
            print(f"工具执行中: {instance._tool_executing}")
            print(f"最后事件: {instance._last_event}")

            if instance.log_path and instance.log_path.exists():
                import subprocess
                result = subprocess.run(['tail', '-5', str(instance.log_path)],
                                      capture_output=True, text=True)
                print(f"最新日志:")
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        print(f"  {line[:100]}...")

    elif args.list:
        states = detector.get_all_states()
        print(json.dumps(states, indent=2, ensure_ascii=False))
    elif args.instance:
        state = detector.get_instance_state(args.instance)
        if state:
            print(json.dumps(state, indent=2, ensure_ascii=False))
        else:
            print(f"未找到实例: {args.instance}")
    elif args.watch:
        print("持续监控所有实例 (按 Ctrl+C 退出)...")
        last_states = {}
        try:
            while True:
                states = detector.get_all_states()
                for state in states:
                    instance_id = state['id']
                    if instance_id not in last_states or last_states[instance_id] != state['state']:
                        print(f"[{state['timestamp']}] {state['name']}: {last_states.get(instance_id, '?')} -> {state['state']}")
                        last_states[instance_id] = state['state']
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n监控已停止")
    else:
        states = detector.get_all_states()
        print(json.dumps(states, indent=2, ensure_ascii=False))
