#!/usr/bin/env python3
"""Hermes Nimbus 服务器 - 支持多实例，同端口 HTTP + WebSocket"""
"""
Hermes Nimbus — 实时显示多个 Hermes Agent 运行状态的光环指示器仪表盘。
支持自动发现 Hermes Profile，基于日志 + state.db 双源实时状态检测。

仓库: https://github.com/NousResearch/hermes-nimbus
"""

import asyncio
import json
import logging
import mimetypes
import signal
import sys
from pathlib import Path
from typing import Set, Dict

try:
    from aiohttp import web
except ImportError:
    print("请安装 aiohttp: pip install aiohttp")
    sys.exit(1)

# 添加当前目录到路径
sys.path.insert(0, str(Path(__file__).parent))
from hermes_state import HermesMultiDetector, DEFAULT_CONFIG_PATH

# 静态文件目录
STATIC_DIR = Path(__file__).parent.parent / 'static'

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('HermesHalo')


class HaloServer:
    """Hermes Nimbus 服务器（HTTP + WebSocket 同端口）"""

    def __init__(self, host: str = '0.0.0.0', port: int = 8765, config_path: str = None):
        self.host = host
        self.port = port
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.detector = HermesMultiDetector(self.config_path)
        self.clients: Set[web.WebSocketResponse] = set()
        self.running = False
        self._last_states: Dict[str, str] = {}
        self._runner = None
        self._monitor_task = None
        self._app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        """设置路由"""
        self._app.router.add_get('/ws', self.ws_handler)
        self._app.router.add_get('/api/health', self.health_handler)
        self._app.router.add_get('/api/states', self.states_handler)
        self._app.router.add_get('/', self.serve_index)
        self._app.router.add_get('/{path:.*}', self.serve_static)

    async def health_handler(self, request):
        """健康检查"""
        return web.json_response({
            'ok': True,
            'service': 'hermes-nimbus',
            'port': self.port,
            'config_path': str(self.detector.config_path),
            'instances': self.detector.get_instance_ids(),
            'clients': len(self.clients),
        })

    async def states_handler(self, request):
        """HTTP 方式获取当前实例状态"""
        return web.json_response({
            'type': 'states_update',
            'data': self.detector.get_all_states(),
        })

    async def serve_index(self, request):
        """首页"""
        return web.FileResponse(STATIC_DIR / 'index.html')

    async def serve_static(self, request):
        """静态文件"""
        path = request.match_info['path']
        file_path = (STATIC_DIR / path).resolve()

        # 安全检查
        if not str(file_path).startswith(str(STATIC_DIR.resolve())):
            return web.Response(status=403, text='Forbidden')

        if file_path.is_file():
            content_type, _ = mimetypes.guess_type(str(file_path))
            return web.FileResponse(file_path, headers={'Content-Type': content_type or 'application/octet-stream'})
        else:
            return web.Response(status=404, text='Not Found')

    async def ws_handler(self, request):
        """WebSocket 处理"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.clients.add(ws)
        logger.info(f"客户端连接 (总数: {len(self.clients)})")

        # 发送当前状态
        try:
            states = self.detector.get_all_states()
            await ws.send_json({'type': 'states_update', 'data': states})
        except Exception as e:
            logger.error(f"发送初始状态失败: {e}")

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self.handle_message(ws, data)
                    except json.JSONDecodeError:
                        await ws.send_json({'type': 'error', 'message': '无效的 JSON 格式'})
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket 错误: {ws.exception()}")
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            logger.info(f"客户端断开 (总数: {len(self.clients)})")

        return ws

    async def handle_message(self, ws, data: dict):
        """处理客户端消息"""
        msg_type = data.get('type')

        if msg_type == 'get_states':
            states = self.detector.get_all_states()
            await ws.send_json({'type': 'states_update', 'data': states})

        elif msg_type == 'get_state':
            instance_id = data.get('instance_id')
            if instance_id:
                state = self.detector.get_instance_state(instance_id)
                if state:
                    await ws.send_json({'type': 'state_update', 'data': state})
                else:
                    await ws.send_json({'type': 'error', 'message': f'未找到节点: {instance_id}'})

        elif msg_type == 'set_state':
            instance_id = data.get('instance_id')
            state = data.get('state')
            if instance_id and state:
                if self.detector.set_instance_state(instance_id, state):
                    states = self.detector.get_all_states()
                    await self.broadcast({'type': 'states_update', 'data': states})
                else:
                    await ws.send_json({'type': 'error', 'message': '设置状态失败'})

        elif msg_type == 'ping':
            from datetime import datetime
            await ws.send_json({'type': 'pong', 'timestamp': datetime.now().isoformat()})

        else:
            await ws.send_json({'type': 'error', 'message': f'未知的消息类型: {msg_type}'})

    async def broadcast(self, message: dict):
        """广播消息"""
        if not self.clients:
            return

        disconnected = set()
        for client in list(self.clients):
            try:
                await client.send_json(message)
            except Exception:
                disconnected.add(client)

        for client in disconnected:
            self.clients.discard(client)

    async def state_monitor(self):
        """状态监控循环"""
        logger.info("状态监控已启动")

        while self.running:
            try:
                states = self.detector.get_all_states()

                changed = False
                for state in states:
                    instance_id = state['id']
                    if instance_id not in self._last_states or self._last_states[instance_id] != state['state']:
                        self._last_states[instance_id] = state['state']
                        changed = True

                if changed:
                    logger.info("状态变化，广播更新")
                    await self.broadcast({'type': 'states_update', 'data': states})

                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"状态监控错误: {e}")
                await asyncio.sleep(5)

    async def start(self):
        """启动服务器"""
        self.running = True
        logger.info(f"🚀 Hermes Nimbus 服务器已启动: http://{self.host}:{self.port}")
        logger.info(f"📄 配置文件: {self.detector.config_path}")
        logger.info(f"📋 监控实例: {', '.join(self.detector.get_instance_ids())}")

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

        # 启动状态监控
        self._monitor_task = asyncio.create_task(self.state_monitor())

        # 保持运行
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            if self._monitor_task:
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass
            if self._runner:
                await self._runner.cleanup()

    async def stop(self):
        """停止服务器"""
        self.running = False
        for client in list(self.clients):
            try:
                await client.close()
            except Exception:
                pass
        self.clients.clear()
        logger.info("Hermes Nimbus 服务器已停止")


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='Hermes Nimbus 服务器')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址 (默认: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8765, help='监听端口 (默认: 8765)')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG_PATH), help='配置文件路径')
    args = parser.parse_args()

    server = HaloServer(host=args.host, port=args.port, config_path=args.config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler():
        logger.info("收到停止信号...")
        loop.create_task(server.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(server.start())
    except KeyboardInterrupt:
        logger.info("服务器被用户中断")
    finally:
        loop.run_until_complete(server.stop())
        loop.close()


if __name__ == '__main__':
    main()
