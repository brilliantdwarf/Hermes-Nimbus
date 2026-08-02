#!/usr/bin/env python3
"""Hermes Nimbus HTTP/WebSocket server with authoritative event ingestion."""

import asyncio
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Set

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
logger = logging.getLogger('HermesNimbus')


class HaloServer:
    """Hermes Nimbus 服务器（HTTP + WebSocket 同端口）"""

    def __init__(
        self,
        host: str = '127.0.0.1',
        port: int = 8765,
        config_path: str = None,
        *,
        lan_host: str = None,
        allowed_clients: Iterable[str] = (),
        allowed_origins: Iterable[str] = (),
    ):
        self.host = host
        self.lan_host = lan_host
        self.port = port
        self.listen_hosts = tuple(dict.fromkeys(
            value for value in (host, lan_host) if value
        ))
        self._allowed_client_networks = self._parse_client_networks(allowed_clients)
        if any(not self._is_loopback_host(value) for value in self.listen_hosts):
            if not self._allowed_client_networks:
                raise ValueError('non-loopback listeners require at least one allowed client')
        self._allowed_origins = self._build_allowed_origins(allowed_origins)
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.detector = HermesMultiDetector(self.config_path)
        self.clients: Set[web.WebSocketResponse] = set()
        self.running = False
        self._last_snapshots: Dict[str, str] = {}
        self._event_token = (
            os.environ.get('HERMES_NIMBUS_EVENT_TOKEN')
            or os.environ.get('HERMES_HALO_EVENT_TOKEN', '')
        )
        self._runner = None
        self._monitor_task = None
        self._sites = []
        self._app = web.Application(
            client_max_size=64 * 1024,
            middlewares=[self._access_control_middleware],
        )
        self._setup_routes()

    @staticmethod
    def _is_loopback_host(value: str) -> bool:
        host = str(value or '').split('%', 1)[0]
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host.lower() == 'localhost'

    @staticmethod
    def _parse_client_networks(values: Iterable[str]):
        networks = []
        for value in values:
            text = str(value or '').strip()
            if not text:
                continue
            try:
                networks.append(ipaddress.ip_network(text, strict=False))
            except ValueError as exc:
                raise ValueError(f'invalid allowed client: {text}') from exc
        return tuple(networks)

    @staticmethod
    def _origin_host(value: str) -> str:
        try:
            address = ipaddress.ip_address(value.split('%', 1)[0])
        except ValueError:
            return value
        return f'[{address}]' if address.version == 6 else str(address)

    def _build_allowed_origins(self, configured: Iterable[str]):
        origins = {str(value).strip().rstrip('/') for value in configured if str(value).strip()}
        for host in self.listen_hosts:
            if host in {'0.0.0.0', '::'}:
                continue
            origins.add(f'http://{self._origin_host(host)}:{self.port}')
            if self._is_loopback_host(host):
                origins.add(f'http://localhost:{self.port}')
        return frozenset(origins)

    def _client_request_allowed(self, request) -> bool:
        remote = (request.remote or '').split('%', 1)[0]
        try:
            address = ipaddress.ip_address(remote)
        except ValueError:
            return remote.lower() == 'localhost'
        if address.is_loopback:
            return True
        return any(address in network for network in self._allowed_client_networks)

    @web.middleware
    async def _access_control_middleware(self, request, handler):
        if not self._client_request_allowed(request):
            logger.warning('拒绝未授权客户端: %s', request.remote or '<unknown>')
            return web.json_response(
                {'ok': False, 'error': 'client is not allowed'},
                status=403,
            )
        return await handler(request)

    def _websocket_origin_allowed(self, request) -> bool:
        origin = request.headers.get('Origin', '').strip().rstrip('/')
        if not origin:
            return self._is_loopback_request(request)
        return origin in self._allowed_origins

    def _setup_routes(self):
        """设置路由"""
        self._app.router.add_get('/ws', self.ws_handler)
        self._app.router.add_get('/api/health', self.health_handler)
        self._app.router.add_get('/api/states', self.states_handler)
        self._app.router.add_post('/api/events', self.events_handler)
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
            'event_auth': 'bearer' if self._event_token else 'loopback-only',
        })

    async def states_handler(self, request):
        """HTTP 方式获取当前实例状态"""
        return web.json_response({
            'type': 'states_update',
            'data': self.detector.get_all_states(),
        })

    @staticmethod
    def _is_loopback_request(request) -> bool:
        remote = (request.remote or '').split('%', 1)[0]
        try:
            return ipaddress.ip_address(remote).is_loopback
        except ValueError:
            return remote.lower() == 'localhost'

    def _event_request_authorized(self, request) -> bool:
        if not self._event_token:
            return self._is_loopback_request(request)
        supplied = request.headers.get('Authorization', '')
        expected = f'Bearer {self._event_token}'
        return hmac.compare_digest(supplied, expected)

    async def events_handler(self, request):
        """Accept a normalized, privacy-safe Hermes lifecycle event."""
        if not self._event_request_authorized(request):
            return web.json_response(
                {'ok': False, 'error': 'event ingestion is not authorized'},
                status=401,
            )

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response(
                {'ok': False, 'error': 'request body must be valid JSON'},
                status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {'ok': False, 'error': 'request body must be an object'},
                status=400,
            )

        event = dict(body)
        profile_id = str(event.pop('profile_id', '') or '').strip()
        schema_version = event.pop('schema_version', None)
        if isinstance(schema_version, bool) or schema_version != 1:
            return web.json_response(
                {'ok': False, 'error': 'schema_version must be 1'},
                status=400,
            )
        if not profile_id:
            return web.json_response(
                {'ok': False, 'error': 'profile_id is required'},
                status=400,
            )
        if not str(event.get('event_id') or '').strip():
            return web.json_response(
                {'ok': False, 'error': 'event_id is required'},
                status=400,
            )

        instance = self.detector.instances.get(profile_id)
        if instance is None:
            return web.json_response(
                {'ok': False, 'error': f'unknown profile_id: {profile_id}'},
                status=404,
            )

        # The source is a property of this trusted endpoint, not caller input.
        event['source'] = 'hermes_hook'
        try:
            accepted = instance.ingest_event(event)
        except ValueError as exc:
            return web.json_response(
                {'ok': False, 'error': str(exc)},
                status=400,
            )

        states = self.detector.get_all_states()
        state = next(item for item in states if item['id'] == profile_id)
        if accepted:
            self._last_snapshots = self._snapshot_map(states)
            await self.broadcast({'type': 'states_update', 'data': states})
        return web.json_response({
            'ok': True,
            'accepted': accepted,
            'state': state,
        })

    async def serve_index(self, request):
        """首页"""
        return web.FileResponse(STATIC_DIR / 'index.html')

    async def serve_static(self, request):
        """静态文件"""
        path = request.match_info['path']
        static_root = STATIC_DIR.resolve()
        file_path = (static_root / path).resolve()

        # 安全检查
        try:
            file_path.relative_to(static_root)
        except ValueError:
            return web.Response(status=403, text='Forbidden')

        if file_path.is_file():
            content_type, _ = mimetypes.guess_type(str(file_path))
            return web.FileResponse(file_path, headers={'Content-Type': content_type or 'application/octet-stream'})
        else:
            return web.Response(status=404, text='Not Found')

    async def ws_handler(self, request):
        """WebSocket 处理"""
        if not self._websocket_origin_allowed(request):
            logger.warning(
                '拒绝 WebSocket Origin: remote=%s origin=%s',
                request.remote or '<unknown>',
                request.headers.get('Origin', '<missing>'),
            )
            return web.json_response(
                {'ok': False, 'error': 'websocket origin is not allowed'},
                status=403,
            )

        ws = web.WebSocketResponse(
            max_msg_size=64 * 1024,
            heartbeat=30.0,
            compress=False,
        )
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
        if not isinstance(data, dict):
            await ws.send_json({'type': 'error', 'message': '消息必须是 JSON 对象'})
            return
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

    @staticmethod
    def _snapshot_signature(state: Dict[str, Any]) -> str:
        """Ignore polling timestamps while retaining meaningful changes."""
        payload = {
            'state': state.get('state'),
            'availability': state.get('availability'),
            'stale': state.get('stale'),
            'source': state.get('source'),
            'confidence': state.get('confidence'),
            'observed_at': state.get('observed_at'),
            'reason': state.get('reason'),
            'detail': state.get('detail'),
            'diagnostic': state.get('diagnostic'),
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    @classmethod
    def _snapshot_map(cls, states):
        return {
            state['id']: cls._snapshot_signature(state)
            for state in states
        }

    async def state_monitor(self):
        """状态监控循环"""
        logger.info("状态监控已启动")

        while self.running:
            try:
                states = self.detector.get_all_states()
                snapshots = self._snapshot_map(states)
                changed = snapshots != self._last_snapshots
                self._last_snapshots = snapshots

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
        addresses = ', '.join(f'http://{host}:{self.port}' for host in self.listen_hosts)
        logger.info(f"🚀 Hermes Nimbus 服务器已启动: {addresses}")
        logger.info(f"📄 配置文件: {self.detector.config_path}")
        logger.info(f"📋 监控实例: {', '.join(self.detector.get_instance_ids())}")

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        for host in self.listen_hosts:
            site = web.TCPSite(self._runner, host, self.port)
            await site.start()
            self._sites.append(site)

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
    parser.add_argument('--host', default='127.0.0.1', help='本机监听地址 (默认: 127.0.0.1)')
    parser.add_argument('--lan-host', help='可选的局域网监听地址')
    parser.add_argument(
        '--allow-client',
        action='append',
        default=[],
        help='允许访问局域网入口的客户端 IP/CIDR，可重复指定',
    )
    parser.add_argument(
        '--allow-origin',
        action='append',
        default=[],
        help='额外允许的 WebSocket Origin，可重复指定',
    )
    parser.add_argument('--port', type=int, default=8765, help='监听端口 (默认: 8765)')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG_PATH), help='配置文件路径')
    args = parser.parse_args()

    try:
        server = HaloServer(
            host=args.host,
            port=args.port,
            config_path=args.config,
            lan_host=args.lan_host,
            allowed_clients=args.allow_client,
            allowed_origins=args.allow_origin,
        )
    except ValueError as exc:
        parser.error(str(exc))

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
