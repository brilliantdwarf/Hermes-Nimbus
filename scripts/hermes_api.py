#!/usr/bin/env python3
"""Hermes Agent → OpenAI API 包装器
将 Hermes agent 暴露为 OpenAI 兼容的 /v1/chat/completions 端点，
用于接入 OpenWebUI 等前端。
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
logger = logging.getLogger('HermesAPI')

HERMES_BIN = os.path.expanduser('~/.hermes/hermes-agent/venv/bin/python')
HERMES_MAIN = '-m'
HERMES_MODULE = 'hermes_cli.main'


class HermesAgentAPI:
    """将单个 Hermes profile 包装为 OpenAI 兼容 API"""

    def __init__(self, profile: str, port: int, name: str = None):
        self.profile = profile
        self.port = port
        self.name = name or profile
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get('/v1/models', self.list_models)
        self.app.router.add_post('/v1/chat/completions', self.chat_completions)
        self.app.router.add_get('/health', self.health)

    async def health(self, request):
        return web.json_response({'status': 'ok', 'profile': self.profile})

    async def list_models(self, request):
        return web.json_response({
            'object': 'list',
            'data': [{
                'id': f'hermes-{self.profile}',
                'object': 'model',
                'created': int(time.time()),
                'owned_by': 'hermes',
                'permission': [],
            }]
        })

    async def chat_completions(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        messages = body.get('messages', [])
        stream = body.get('stream', False)

        # 提取最后一条 user 消息
        user_msg = ''
        for msg in reversed(messages):
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                if isinstance(content, list):
                    # 多模态消息，只取文本
                    content = ' '.join(
                        p.get('text', '') for p in content if p.get('type') == 'text'
                    )
                user_msg = content
                break

        if not user_msg:
            return web.json_response({'error': 'No user message found'}, status=400)

        logger.info(f"[{self.profile}] 收到请求: {user_msg[:80]}...")

        if stream:
            return await self._stream_response(user_msg)
        else:
            return await self._sync_response(user_msg)

    async def _run_hermes(self, message: str) -> str:
        """调用 hermes chat 并返回响应文本"""
        cmd = [
            HERMES_BIN, HERMES_MAIN, HERMES_MODULE,
            '--profile', self.profile,
            'chat', '-q', message, '-Q'
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            output = stdout.decode('utf-8', errors='replace').strip()

            # 去掉 session_id 行
            lines = output.split('\n')
            if lines and lines[0].startswith('session_id:'):
                lines = lines[1:]
            return '\n'.join(lines).strip()
        except asyncio.TimeoutError:
            return '[Hermes 响应超时]'
        except Exception as e:
            logger.error(f"[{self.profile}] 调用失败: {e}")
            return f'[Hermes 调用错误: {e}]'

    async def _sync_response(self, message: str):
        """同步响应"""
        response_text = await self._run_hermes(message)
        model_id = f'hermes-{self.profile}'

        return web.json_response({
            'id': f'chatcmpl-{uuid.uuid4().hex[:12]}',
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': model_id,
            'choices': [{
                'index': 0,
                'message': {
                    'role': 'assistant',
                    'content': response_text,
                },
                'finish_reason': 'stop',
            }],
            'usage': {
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'total_tokens': 0,
            }
        })

    async def _stream_response(self, message: str):
        """流式响应（SSE）"""
        response_text = await self._run_hermes(message)
        model_id = f'hermes-{self.profile}'

        resp = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            },
        )
        await resp.prepare(request)

        # 按字符分块模拟流式
        chunk_size = 20
        for i in range(0, len(response_text), chunk_size):
            chunk = response_text[i:i + chunk_size]
            data = {
                'id': f'chatcmpl-{uuid.uuid4().hex[:12]}',
                'object': 'chat.completion.chunk',
                'created': int(time.time()),
                'model': model_id,
                'choices': [{
                    'index': 0,
                    'delta': {'content': chunk},
                    'finish_reason': None,
                }],
            }
            await resp.write(f'data: {json.dumps(data, ensure_ascii=False)}\n\n'.encode())

        # 结束标记
        final = {
            'id': f'chatcmpl-{uuid.uuid4().hex[:12]}',
            'object': 'chat.completion.chunk',
            'created': int(time.time()),
            'model': model_id,
            'choices': [{
                'index': 0,
                'delta': {},
                'finish_reason': 'stop',
            }],
        }
        await resp.write(f'data: {json.dumps(final)}\n\n'.encode())
        await resp.write(b'data: [DONE]\n\n')
        return resp

    def start(self):
        logger.info(f"🚀 Hermes API [{self.profile}] 启动于 http://0.0.0.0:{self.port}")
        web.run_app(self.app, host='0.0.0.0', port=self.port, print=None)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Hermes Agent OpenAI API 包装器')
    parser.add_argument('--profile', required=True, help='Hermes profile 名称')
    parser.add_argument('--port', type=int, required=True, help='监听端口')
    parser.add_argument('--name', help='显示名称（默认同 profile）')
    args = parser.parse_args()

    api = HermesAgentAPI(args.profile, args.port, args.name)
    api.start()


if __name__ == '__main__':
    main()
