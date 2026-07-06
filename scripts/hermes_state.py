#!/usr/bin/env python3
"""Hermes 状态检测器 - 支持多实例，基于日志实时监控"""

import sqlite3
import json
import re
import time
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

class HermesInstance:
    """单个 Hermes 实例的状态检测器"""
    
    # 状态配置 - 与 Claude Halo 完全一致
    STATES = {
        'idle': {
            'color': '#aaaaaa',
            'halo': '#cccccc',
            'period': 6.0,
            'dashes': [60, 30],
            'ms': 0,
            'md': 0,
            'amin': 0.30,
            'amax': 0.42,
            'br': 0,
            'rp': 0,
            'rpperiod': 0,
            'label': '空闲',
            'description': '正在等待任务'
        },
        'thinking': {
            'color': '#ff8830',
            'halo': '#ffdbb8',
            'period': 2.4,
            'dashes': [70, 35, 45, 30, 25, 20],
            'ms': 0.6,
            'md': 0.4,
            'amin': 0.45,
            'amax': 0.90,
            'br': 5.2,
            'rp': 0,
            'rpperiod': 0,
            'label': '思考中',
            'description': '正在推理分析'
        },
        'streaming': {
            'color': '#e8b100',
            'halo': '#fff0aa',
            'period': 2.0,
            'dashes': [60, 25, 45, 20, 35, 15],
            'ms': 0.7,
            'md': 0.30,
            'amin': 0.50,
            'amax': 0.85,
            'br': 3.5,
            'rp': 0,
            'rpperiod': 0,
            'label': '输出中',
            'description': '正在生成回复'
        },
        'executing': {
            'color': '#3399ff',
            'halo': '#bbddff',
            'period': 1.3,
            'dashes': [50, 25, 20, 20, 35, 25, 25, 22],
            'ms': 1.2,
            'md': 0.28,
            'amin': 0.60,
            'amax': 0.90,
            'br': 0,
            'rp': 0,
            'rpperiod': 0,
            'label': '执行中',
            'description': '正在调用工具'
        },
        'input_needed': {
            'color': '#ee3333',
            'halo': '#ffcccc',
            'period': 2.8,
            'dashes': [80, 50, 30, 25],
            'ms': 1.8,
            'md': 0.5,
            'amin': 0.52,
            'amax': 0.94,
            'br': 2.0,
            'rp': 0,
            'rpperiod': 0,
            'label': '等待输入',
            'description': '需要用户确认'
        },
        'completed': {
            'color': '#33cc55',
            'halo': '#bbffcc',
            'period': 5.0,
            'dashes': [70, 35, 45, 30, 25, 20],
            'ms': 0.5,
            'md': 0.3,
            'amin': 0.38,
            'amax': 0.84,
            'br': 6.0,
            'rp': 0,
            'rpperiod': 0,
            'label': '已完成',
            'description': '任务已完成'
        },
        'error': {
            'color': '#ff4444',
            'halo': '#ffcccc',
            'period': 1.5,
            'dashes': [40, 20, 30, 15],
            'ms': 2.0,
            'md': 0.6,
            'amin': 0.50,
            'amax': 0.95,
            'br': 1.5,
            'rp': 0,
            'rpperiod': 0,
            'label': '错误',
            'description': '出现错误'
        },
        'compacting': {
            'color': '#9944ff',
            'halo': '#ddccff',
            'period': 2.1,
            'dashes': [35, 20, 35, 20, 35, 20],
            'ms': 0.4,
            'md': 0.25,
            'amin': 0.38,
            'amax': 0.80,
            'br': 4.0,
            'rp': 0.12,
            'rpperiod': 1.6,
            'label': '压缩中',
            'description': '正在整理上下文'
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
        
        self.log_path = Path(config['log_path']).expanduser()
        self.db_path = Path(config.get('db_path', '')).expanduser() if config.get('db_path') else None
        
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
        
        # 初始化：读取最近的日志确定初始状态
        self._init_from_recent_logs()
    
    def _init_from_recent_logs(self):
        """从最近的日志初始化状态"""
        try:
            if not self.log_path.exists():
                return
            
            # 读取最后 50 行日志
            with open(self.log_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                recent_lines = lines[-50:] if len(lines) > 50 else lines
            
            # 从末尾开始
            self._last_log_position = self.log_path.stat().st_size
            
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
                    except:
                        continue
                else:
                    continue
                
                # 检查事件类型
                if 'Turn ended' in line:
                    last_event = 'turn_ended'
                    last_event_time = event_timestamp
                    break
                elif 'OpenAI client created' in line:
                    last_event = 'api_start'
                    last_event_time = event_timestamp
                    break
                elif 'stream_request_complete' in line:
                    last_event = 'stream_end'
                    last_event_time = event_timestamp
                    break
                elif 'tool_executor' in line and 'completed' in line:
                    last_event = 'tool_completed'
                    last_event_time = event_timestamp
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
                    elif last_event == 'api_start':
                        self.current_state = 'thinking'
                        self._api_call_active = True
                    elif last_event == 'stream_end':
                        # stream_request_complete 表示流式输出已完成，等待下一步
                        self.current_state = 'thinking'
                        self._api_call_active = True
                    elif last_event == 'tool_completed':
                        self.current_state = 'executing'
                        self._tool_executing = True
                else:
                    self.current_state = 'idle'
            
        except Exception as e:
            print(f"初始化日志分析错误: {e}")
    
    def _read_new_log_lines(self) -> List[str]:
        """读取新的日志行"""
        if not self.log_path.exists():
            return []
        
        try:
            current_size = self.log_path.stat().st_size
            
            if current_size < self._last_log_position:
                self._last_log_position = 0
            
            if current_size <= self._last_log_position:
                return []
            
            with open(self.log_path, 'r', encoding='utf-8', errors='ignore') as f:
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
        
        # API 调用开始
        if 'OpenAI client created' in line:
            return 'api_start'
        
        # 流式请求完成（必须在 api_end 之前，因为 'OpenAI client closed (stream_request_complete)' 同时匹配两者）
        if 'stream_request_complete' in line:
            return 'stream_end'
        
        # API 调用结束（不含 stream_request_complete 的普通关闭）
        if 'OpenAI client closed' in line:
            return 'api_end'
        
        # 工具执行完成
        if 'tool_executor' in line and 'completed' in line:
            return 'tool_completed'
        
        # 工具执行开始
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
    
    def detect_from_log(self) -> Optional[str]:
        """从日志实时检测状态"""
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
                        # 其他事件不改变状态
                        continue
                    
                    # 更新状态标志
                    if event == 'turn_ended':
                        self._turn_ended_time = now
                        self._api_call_active = False
                        self._streaming_active = False
                        self._tool_executing = False
                        return 'completed'
                    
                    elif event == 'user_message':
                        # 用户消息到达，准备思考
                        self._turn_ended_time = 0
                        return 'thinking'
                    
                    elif event == 'api_start':
                        self._api_call_active = True
                        self._streaming_active = False
                        self._turn_ended_time = 0
                        return 'thinking'
                    
                    elif event == 'api_end':
                        self._api_call_active = False
                        # API 结束，但可能还在流式输出
                        if self._streaming_active:
                            return 'streaming'
                        if self._tool_executing:
                            return 'executing'
                        return 'thinking'
                    
                    elif event == 'stream_end':
                        self._streaming_active = False
                        # 流式输出完成
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
                # 超时，变为空闲
                self._api_call_active = False
                self._streaming_active = False
                self._tool_executing = False
                return 'idle'
            elif elapsed > 10:
                # 超过10秒没有新日志
                if self._last_event == 'api_start':
                    # API 调用开始后没有结束
                    if elapsed > 30:
                        return 'idle'
                    return 'thinking'
                elif self._last_event == 'stream_end':
                    # 流式输出结束
                    if elapsed > 15:
                        return 'idle'
                    return 'streaming'
                elif self._last_event == 'tool_completed':
                    # 工具完成，等待下一次 API 调用
                    return 'thinking'
            elif elapsed < 5:
                # 刚刚活跃，保持当前状态
                if self._api_call_active:
                    return 'thinking'
                if self._streaming_active:
                    return 'streaming'
                if self._tool_executing:
                    return 'executing'
        
        return None
    
    def detect_from_database(self) -> Optional[str]:
        """从数据库检测状态（备用）"""
        if not self.db_path or not self.db_path.exists():
            return None
        
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id, ended_at FROM sessions 
                WHERE ended_at IS NULL OR ended_at = 0
                ORDER BY started_at DESC LIMIT 1
            """)
            session = cursor.fetchone()
            
            if not session:
                conn.close()
                return 'idle'
            
            cursor.execute("""
                SELECT role, timestamp FROM messages 
                WHERE session_id = ? ORDER BY timestamp DESC LIMIT 1
            """, (session['id'],))
            latest = cursor.fetchone()
            conn.close()
            
            if latest:
                msg_age = time.time() - latest['timestamp']
                if msg_age < 10:
                    if latest['role'] == 'user':
                        return 'thinking'
                    elif latest['role'] == 'assistant':
                        return 'completed'
                    elif latest['role'] == 'tool':
                        return 'executing'
            
            return None
        except Exception:
            return None
    
    def detect_state(self) -> str:
        """检测当前状态"""
        # 1. 日志监控
        state = self.detect_from_log()
        if state:
            self.current_state = state
            return state
        
        # 2. 数据库备用
        state = self.detect_from_database()
        if state:
            self.current_state = state
            return state
        
        # 3. 保持当前状态
        return self.current_state
    
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
    
    def __init__(self, config_path: str = '~/.hermes/hermes-halo/config.json'):
        self.config_path = Path(config_path).expanduser()
        self.instances: Dict[str, HermesInstance] = {}
        self.load_config()
    
    def load_config(self):
        """加载配置文件"""
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                
                for instance_config in config.get('instances', []):
                    instance = HermesInstance(instance_config)
                    self.instances[instance.id] = instance
            else:
                self._create_default_config()
        except Exception as e:
            print(f"加载配置失败: {e}")
            self._create_default_config()
    
    def _create_default_config(self):
        """创建默认配置"""
        default_instance = HermesInstance({
            'id': 'default',
            'name': '默认',
            'description': '默认 Hermes 节点',
            'log_path': '~/.hermes/logs/agent.log',
            'db_path': '~/.hermes/state.db',
            'color': '#3399ff',
            'icon': '🤖'
        })
        self.instances['default'] = default_instance
    
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
            print(f"初始状态: {instance.current_state}")
            print(f"最后活动: {instance._last_activity_time}")
            print(f"Turn ended: {instance._turn_ended_time}")
            print(f"API 调用活跃: {instance._api_call_active}")
            print(f"流式输出活跃: {instance._streaming_active}")
            print(f"工具执行中: {instance._tool_executing}")
            print(f"最后事件: {instance._last_event}")
            
            if instance.log_path.exists():
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
