#!/usr/bin/env python3
"""
WebSocket Server para comunicação com a plataforma web
Integra o Scanner de Câmeras com o Bridge de Streaming
"""

import asyncio
import json
import logging
import socket
import platform
import uuid
from typing import Set, Optional
from datetime import datetime

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("⚠ websockets não instalado. Execute: pip install websockets")

from stream_bridge import StreamBridge

logger = logging.getLogger(__name__)


class BridgeWebSocketServer:
    """
    Servidor WebSocket que permite a plataforma web controlar:
    - Descoberta de câmeras na rede
    - Streaming de câmeras locais via bridge
    - Instalação automática do FFmpeg
    """
    
    def __init__(self, 
                 host: str = "127.0.0.1", 
                 port: int = 8765,
                 rtmp_server_url: str = "rtmp://hopper.proxy.rlwy.net:46960/live"):
        self.host = host
        self.port = port
        self.rtmp_server_url = rtmp_server_url
        self.clients: Set = set()
        self.bridge: Optional[StreamBridge] = None
        self.server = None
        self._running = False
        self._ffmpeg_installing = False
        self.client_id = str(uuid.uuid4())[:8]  # Unique client ID
        self.hostname = platform.node()
        self.os_info = f"{platform.system()} {platform.release()}"
        
    def get_local_ip(self) -> str:
        """Obtém IP local da máquina"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
    
    def on_stream_status_change(self, stream_key: str, status: str, error: str):
        """Callback quando o status de um stream muda"""
        asyncio.create_task(self.broadcast({
            "type": "stream_status",
            "data": {
                "stream_key": stream_key,
                "status": status,
                "error": error
            }
        }))
    
    def on_ffmpeg_progress(self, message: str, percent: int):
        """Callback para progresso da instalação do FFmpeg"""
        asyncio.create_task(self.broadcast({
            "type": "ffmpeg_install_progress",
            "data": {
                "message": message,
                "percent": percent,
                "installing": self._ffmpeg_installing
            }
        }))
    
    async def broadcast(self, message: dict):
        """Envia mensagem para todos os clientes conectados"""
        if not self.clients:
            return
        
        message_str = json.dumps(message)
        disconnected = set()
        
        for client in self.clients:
            try:
                await client.send(message_str)
            except Exception as e:
                logger.warning(f"Erro ao enviar para cliente: {e}")
                disconnected.add(client)
        
        self.clients -= disconnected
    
    async def handle_client(self, websocket):
        """Handler para cada cliente WebSocket conectado"""
        self.clients.add(websocket)
        logger.info(f"Cliente conectado. Total: {len(self.clients)}")
        
        try:
            # Envia informações do bridge ao conectar
            ffmpeg_available = self.bridge.is_ffmpeg_available() if self.bridge else False
            
            await websocket.send(json.dumps({
                "type": "bridge_connected",
                "data": {
                    "client_id": self.client_id,
                    "hostname": self.hostname,
                    "os_info": self.os_info,
                    "local_ip": self.get_local_ip(),
                    "ffmpeg_available": ffmpeg_available,
                    "ffmpeg_installing": self._ffmpeg_installing,
                    "active_streams": len(self.bridge.active_streams) if self.bridge else 0,
                    "timestamp": datetime.now().isoformat()
                }
            }))
            
            async for message in websocket:
                await self.process_message(websocket, message)
                
        except Exception as e:
            logger.error(f"Erro no cliente: {e}")
        finally:
            self.clients.discard(websocket)
            logger.info(f"Cliente desconectado. Total: {len(self.clients)}")
    
    async def process_message(self, websocket, message: str):
        """Processa mensagem recebida do cliente"""
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")
            msg_data = data.get("data", {})
            
            if msg_type == "get_bridge_info":
                await websocket.send(json.dumps({
                    "type": "bridge_connected",
                    "data": {
                        "client_id": self.client_id,
                        "hostname": self.hostname,
                        "os_info": self.os_info,
                        "local_ip": self.get_local_ip(),
                        "ffmpeg_available": self.bridge.is_ffmpeg_available() if self.bridge else False,
                        "active_streams": len(self.bridge.active_streams) if self.bridge else 0,
                        "timestamp": datetime.now().isoformat()
                    }
                }))
            
            elif msg_type == "start_stream":
                if not self.bridge:
                    await websocket.send(json.dumps({
                        "type": "stream_started",
                        "data": {
                            "success": False,
                            "error": "Bridge não inicializado",
                            "stream_key": msg_data.get("stream_key", "")
                        }
                    }))
                    return
                
                stream_key = msg_data.get("stream_key", "")
                rtsp_url = msg_data.get("rtsp_url", "")
                camera_name = msg_data.get("camera_name", "")
                
                if not stream_key or not rtsp_url:
                    await websocket.send(json.dumps({
                        "type": "stream_started",
                        "data": {
                            "success": False,
                            "error": "stream_key e rtsp_url são obrigatórios",
                            "stream_key": stream_key
                        }
                    }))
                    return
                
                # Inicia stream em thread separada para não bloquear
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, 
                    lambda: self.bridge.start_stream(stream_key, rtsp_url, camera_name)
                )
                
                await websocket.send(json.dumps({
                    "type": "stream_started",
                    "data": {
                        **result,
                        "rtsp_url": rtsp_url,
                        "camera_name": camera_name
                    }
                }))
                
                # Broadcast para outros clientes
                if result.get("success"):
                    await self.broadcast({
                        "type": "stream_status",
                        "data": {
                            "stream_key": stream_key,
                            "status": "running",
                            "camera_name": camera_name
                        }
                    })
            
            elif msg_type == "stop_stream":
                if not self.bridge:
                    return
                
                stream_key = msg_data.get("stream_key", "")
                
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self.bridge.stop_stream(stream_key)
                )
                
                await websocket.send(json.dumps({
                    "type": "stream_stopped",
                    "data": result
                }))
                
                if result.get("success"):
                    await self.broadcast({
                        "type": "stream_status",
                        "data": {
                            "stream_key": stream_key,
                            "status": "stopped"
                        }
                    })
            
            elif msg_type == "list_streams":
                if not self.bridge:
                    await websocket.send(json.dumps({
                        "type": "streams_list",
                        "data": {"streams": []}
                    }))
                    return
                
                streams = self.bridge.get_all_streams()
                await websocket.send(json.dumps({
                    "type": "streams_list",
                    "data": {"streams": streams}
                }))
            
            elif msg_type == "start_scan":
                # Dispara evento para o scanner (será tratado pelo app principal)
                await self.broadcast({
                    "type": "scan_command",
                    "data": {"action": "start"}
                })
            
            elif msg_type == "stop_scan":
                await self.broadcast({
                    "type": "scan_command", 
                    "data": {"action": "stop"}
                })
            
            else:
                logger.warning(f"Tipo de mensagem desconhecido: {msg_type}")
                
        except json.JSONDecodeError:
            logger.error(f"Mensagem inválida: {message}")
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {e}")
    
    async def start(self):
        """Inicia o servidor WebSocket"""
        if not WEBSOCKETS_AVAILABLE:
            logger.error("Biblioteca websockets não disponível")
            return
        
        # Sinaliza que pode estar instalando FFmpeg
        self._ffmpeg_installing = True
        
        # Inicializa o bridge de streaming (isso pode instalar FFmpeg automaticamente)
        def ffmpeg_progress_sync(msg, pct):
            # Chamado da thread do instalador, precisa agendar no event loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self.broadcast({
                            "type": "ffmpeg_install_progress",
                            "data": {"message": msg, "percent": pct}
                        }),
                        loop
                    )
            except:
                pass
        
        self.bridge = StreamBridge(
            rtmp_server_url=self.rtmp_server_url,
            on_status_change=self.on_stream_status_change,
            on_ffmpeg_progress=ffmpeg_progress_sync
        )
        
        self._ffmpeg_installing = False
        self._running = True
        
        logger.info(f"Iniciando WebSocket server em ws://{self.host}:{self.port}")
        
        async with websockets.serve(self.handle_client, self.host, self.port):
            logger.info(f"✓ WebSocket server rodando em ws://{self.host}:{self.port}")
            logger.info(f"  FFmpeg disponível: {self.bridge.is_ffmpeg_available()}")
            
            while self._running:
                await asyncio.sleep(1)
    
    def stop(self):
        """Para o servidor"""
        self._running = False
        
        if self.bridge:
            self.bridge.shutdown()


def run_server(host: str = "127.0.0.1", port: int = 8765):
    """Função para rodar o servidor standalone"""
    server = BridgeWebSocketServer(host, port)
    
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Servidor encerrado pelo usuário")
        server.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    run_server()
