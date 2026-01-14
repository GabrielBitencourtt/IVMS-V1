"""
WebSocket Relay - Baixa latência (~1-2s)

Arquitetura:
1. App local captura RTSP e extrai pacotes H.264
2. Envia via WebSocket para este servidor
3. Servidor faz broadcast para todos os browsers conectados
4. Browsers decodificam H.264 via MSE (Media Source Extensions)

Protocolo:
- Producer (app local) envia: {"type": "data", "stream_key": "xxx", "data": base64_h264}
- Consumer (browser) recebe: dados binários H.264 puros
"""

import asyncio
import json
import base64
import time
from typing import Dict, Set
from dataclasses import dataclass, field
from fastapi import WebSocket, WebSocketDisconnect


@dataclass
class StreamRoom:
    """Sala de streaming - um producer, múltiplos consumers"""
    stream_key: str
    producer: WebSocket | None = None
    consumers: Set[WebSocket] = field(default_factory=set)
    last_data_time: float = 0
    bytes_sent: int = 0
    init_segment: bytes | None = None  # Segmento de inicialização (SPS/PPS)


class WebSocketRelay:
    """
    Gerencia salas de streaming WebSocket.
    
    - Cada stream_key tem uma sala
    - Um producer (app local) por sala
    - Múltiplos consumers (browsers) por sala
    """
    
    def __init__(self):
        self.rooms: Dict[str, StreamRoom] = {}
        self._lock = asyncio.Lock()
    
    async def register_producer(self, websocket: WebSocket, stream_key: str) -> bool:
        """Registra um producer (app local) para uma sala"""
        async with self._lock:
            if stream_key not in self.rooms:
                self.rooms[stream_key] = StreamRoom(stream_key=stream_key)
            
            room = self.rooms[stream_key]
            
            # Se já tem um producer, substituir
            if room.producer:
                try:
                    await room.producer.close()
                except:
                    pass
            
            room.producer = websocket
            room.last_data_time = time.time()
            print(f"📡 Producer connected: {stream_key}")
            return True
    
    async def register_consumer(self, websocket: WebSocket, stream_key: str) -> bool:
        """Registra um consumer (browser) para uma sala"""
        async with self._lock:
            if stream_key not in self.rooms:
                self.rooms[stream_key] = StreamRoom(stream_key=stream_key)
            
            room = self.rooms[stream_key]
            room.consumers.add(websocket)
            print(f"👁️ Consumer connected: {stream_key} (total: {len(room.consumers)})")
            
            # Se temos init segment, enviar primeiro
            if room.init_segment:
                try:
                    await websocket.send_bytes(room.init_segment)
                except:
                    pass
            
            return True
    
    async def unregister_producer(self, stream_key: str):
        """Remove producer de uma sala"""
        async with self._lock:
            if stream_key in self.rooms:
                self.rooms[stream_key].producer = None
                print(f"📡 Producer disconnected: {stream_key}")
    
    async def unregister_consumer(self, websocket: WebSocket, stream_key: str):
        """Remove consumer de uma sala"""
        async with self._lock:
            if stream_key in self.rooms:
                self.rooms[stream_key].consumers.discard(websocket)
                print(f"👁️ Consumer disconnected: {stream_key} (remaining: {len(self.rooms[stream_key].consumers)})")
    
    async def broadcast(self, stream_key: str, data: bytes, is_init: bool = False):
        """Envia dados para todos os consumers de uma sala"""
        if stream_key not in self.rooms:
            return
        
        room = self.rooms[stream_key]
        room.last_data_time = time.time()
        room.bytes_sent += len(data)
        
        # Salvar init segment
        if is_init:
            room.init_segment = data
        
        # Broadcast para todos os consumers
        dead_consumers = set()
        
        for consumer in room.consumers:
            try:
                await consumer.send_bytes(data)
            except Exception:
                dead_consumers.add(consumer)
        
        # Remover consumers mortos
        for dead in dead_consumers:
            room.consumers.discard(dead)
    
    def get_room_status(self, stream_key: str) -> dict | None:
        """Retorna status de uma sala"""
        if stream_key not in self.rooms:
            return None
        
        room = self.rooms[stream_key]
        return {
            "stream_key": stream_key,
            "has_producer": room.producer is not None,
            "consumer_count": len(room.consumers),
            "last_data_seconds_ago": time.time() - room.last_data_time if room.last_data_time else None,
            "bytes_sent": room.bytes_sent,
            "has_init_segment": room.init_segment is not None,
        }
    
    def get_all_rooms_status(self) -> list:
        """Retorna status de todas as salas"""
        return [self.get_room_status(key) for key in self.rooms]


# Instância global
ws_relay = WebSocketRelay()


async def handle_producer(websocket: WebSocket, stream_key: str):
    """
    Handler para conexão de producer (app local).
    
    Protocolo de mensagens:
    - {"type": "init", "data": base64} - Segmento de inicialização (SPS/PPS)
    - {"type": "data", "data": base64} - Dados H.264
    - {"type": "ping"} - Keepalive
    """
    await websocket.accept()
    await ws_relay.register_producer(websocket, stream_key)
    
    try:
        while True:
            # Receber mensagem (pode ser JSON ou binário)
            message = await websocket.receive()
            
            if "text" in message:
                # Mensagem JSON
                data = json.loads(message["text"])
                msg_type = data.get("type", "data")
                
                if msg_type == "ping":
                    await websocket.send_text('{"type": "pong"}')
                    continue
                
                if msg_type in ("init", "data"):
                    # Decodificar base64
                    raw_data = base64.b64decode(data.get("data", ""))
                    is_init = msg_type == "init"
                    await ws_relay.broadcast(stream_key, raw_data, is_init=is_init)
            
            elif "bytes" in message:
                # Dados binários diretos (mais eficiente)
                await ws_relay.broadcast(stream_key, message["bytes"])
    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"❌ Producer error ({stream_key}): {e}")
    finally:
        await ws_relay.unregister_producer(stream_key)


async def handle_consumer(websocket: WebSocket, stream_key: str):
    """
    Handler para conexão de consumer (browser).
    
    Consumer apenas recebe dados binários H.264.
    """
    await websocket.accept()
    await ws_relay.register_consumer(websocket, stream_key)
    
    try:
        # Consumer só recebe, mas precisamos manter a conexão
        # e responder a pings do browser
        while True:
            message = await websocket.receive()
            
            if "text" in message:
                data = json.loads(message["text"])
                if data.get("type") == "ping":
                    await websocket.send_text('{"type": "pong"}')
    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"❌ Consumer error ({stream_key}): {e}")
    finally:
        await ws_relay.unregister_consumer(websocket, stream_key)
