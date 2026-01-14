#!/usr/bin/env python3
"""
WebSocket Producer - Envia stream H.264 para servidor via WebSocket

Captura RTSP local e envia pacotes H.264 para o servidor Railway,
que faz broadcast para browsers conectados.

Latência esperada: ~1-2 segundos (vs 3-5s do HLS)
"""

import subprocess
import threading
import logging
import base64
import json
import time
import os
from typing import Optional, Callable
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class WebSocketStream:
    """Representa um stream ativo via WebSocket"""
    stream_key: str
    rtsp_url: str
    ws_url: str
    ffmpeg_process: subprocess.Popen
    ws_thread: threading.Thread
    started_at: datetime
    camera_name: str = ""
    status: str = "running"
    error_message: str = ""
    bytes_sent: int = 0


class WebSocketProducer:
    """
    Captura RTSP e envia H.264 via WebSocket para servidor relay.
    
    Fluxo:
    1. FFmpeg captura RTSP e converte para H.264 raw (stdout)
    2. Thread lê stdout do FFmpeg e envia via WebSocket
    3. Servidor Railway faz broadcast para browsers
    """
    
    def __init__(self, 
                 server_url: str = "wss://hopper.proxy.rlwy.net:443",
                 on_status_change: Optional[Callable] = None):
        self.server_url = server_url.rstrip('/')
        self.on_status_change = on_status_change
        self.active_streams: dict[str, WebSocketStream] = {}
        self._running = True
        self._ffmpeg_path: Optional[str] = None
        
        # Inicializa FFmpeg
        self._init_ffmpeg()
    
    def _init_ffmpeg(self):
        """Encontra FFmpeg no sistema"""
        import shutil
        
        self._ffmpeg_path = shutil.which("ffmpeg")
        
        if not self._ffmpeg_path:
            # Tentar caminhos comuns
            common_paths = [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                "/usr/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
                "/opt/homebrew/bin/ffmpeg",
            ]
            
            for path in common_paths:
                if os.path.isfile(path):
                    self._ffmpeg_path = path
                    break
        
        if self._ffmpeg_path:
            logger.info(f"✓ FFmpeg encontrado: {self._ffmpeg_path}")
        else:
            logger.error("❌ FFmpeg não encontrado!")
    
    def is_available(self) -> bool:
        """Verifica se FFmpeg está disponível"""
        return self._ffmpeg_path is not None
    
    def _build_ffmpeg_command(self, rtsp_url: str) -> list:
        """
        Constrói comando FFmpeg para extrair H.264 raw.
        
        Saída: MPEG-TS via stdout (formato que o browser consegue processar via MSE)
        """
        cmd = [
            self._ffmpeg_path,
            # Opções de entrada
            "-fflags", "+genpts+discardcorrupt+nobuffer",
            "-flags", "low_delay",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-analyzeduration", "500000",
            "-probesize", "500000",
            "-i", rtsp_url,
            # Opções de saída - MPEG-TS para stdout
            "-c:v", "copy",             # Copy H.264 sem recodificar
            "-an",                       # Sem áudio
            "-f", "mpegts",              # Formato MPEG-TS (compatível com MSE)
            "-muxdelay", "0",
            "-muxpreload", "0",
            "pipe:1"                     # Saída para stdout
        ]
        
        return cmd
    
    def start_stream(self, stream_key: str, rtsp_url: str, camera_name: str = "") -> dict:
        """
        Inicia streaming de uma câmera via WebSocket.
        
        Args:
            stream_key: Identificador único do stream
            rtsp_url: URL RTSP da câmera
            camera_name: Nome da câmera (opcional)
        
        Returns:
            Dict com status da operação
        """
        if not self._ffmpeg_path:
            return {"success": False, "error": "FFmpeg não disponível"}
        
        if stream_key in self.active_streams:
            existing = self.active_streams[stream_key]
            if existing.ffmpeg_process.poll() is None:
                return {"success": False, "error": "Stream já está ativo"}
        
        # WebSocket URL para este stream
        ws_url = f"{self.server_url}/ws/produce/{stream_key}"
        
        logger.info(f"🎬 Iniciando stream WebSocket: {stream_key}")
        logger.info(f"   RTSP: {rtsp_url}")
        logger.info(f"   WS: {ws_url}")
        
        try:
            # Iniciar FFmpeg
            cmd = self._build_ffmpeg_command(rtsp_url)
            
            ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0  # Unbuffered para baixa latência
            )
            
            # Aguardar um pouco para ver se não crashou
            time.sleep(0.5)
            
            if ffmpeg_process.poll() is not None:
                stderr = ffmpeg_process.stderr.read().decode('utf-8', errors='ignore')
                return {"success": False, "error": f"FFmpeg falhou: {stderr[-200:]}"}
            
            # Iniciar thread de envio WebSocket
            ws_thread = threading.Thread(
                target=self._websocket_sender,
                args=(stream_key, ffmpeg_process, ws_url),
                daemon=True
            )
            ws_thread.start()
            
            # Registrar stream
            stream_info = WebSocketStream(
                stream_key=stream_key,
                rtsp_url=rtsp_url,
                ws_url=ws_url,
                ffmpeg_process=ffmpeg_process,
                ws_thread=ws_thread,
                started_at=datetime.now(),
                camera_name=camera_name,
                status="running"
            )
            
            self.active_streams[stream_key] = stream_info
            
            if self.on_status_change:
                self.on_status_change(stream_key, "running", "")
            
            return {
                "success": True,
                "stream_key": stream_key,
                "ws_url": ws_url.replace("wss://", "").replace("ws://", ""),
                "mode": "websocket"
            }
            
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar stream: {e}")
            return {"success": False, "error": str(e)}
    
    def _websocket_sender(self, stream_key: str, ffmpeg_process: subprocess.Popen, ws_url: str):
        """
        Thread que lê dados do FFmpeg e envia via WebSocket.
        """
        import websocket
        
        ws = None
        reconnect_delay = 1
        max_reconnect_delay = 30
        
        while self._running and stream_key in self.active_streams:
            try:
                # Conectar ao servidor
                logger.info(f"🔌 Conectando ao servidor WebSocket: {ws_url}")
                ws = websocket.create_connection(
                    ws_url,
                    timeout=10,
                    skip_utf8_validation=True
                )
                logger.info(f"✅ WebSocket conectado: {stream_key}")
                reconnect_delay = 1  # Reset delay on success
                
                # Ler dados do FFmpeg e enviar
                chunk_size = 4096  # 4KB chunks para baixa latência
                
                while self._running and stream_key in self.active_streams:
                    # Verificar se FFmpeg ainda está rodando
                    if ffmpeg_process.poll() is not None:
                        logger.warning(f"⚠️ FFmpeg encerrou para {stream_key}")
                        break
                    
                    # Ler chunk do stdout
                    data = ffmpeg_process.stdout.read(chunk_size)
                    
                    if not data:
                        time.sleep(0.01)
                        continue
                    
                    # Enviar dados binários diretamente
                    ws.send_binary(data)
                    
                    # Atualizar contador
                    if stream_key in self.active_streams:
                        self.active_streams[stream_key].bytes_sent += len(data)
                
            except websocket.WebSocketConnectionClosedException:
                logger.warning(f"⚠️ WebSocket desconectado: {stream_key}")
            except Exception as e:
                logger.error(f"❌ Erro WebSocket ({stream_key}): {e}")
            finally:
                if ws:
                    try:
                        ws.close()
                    except:
                        pass
                    ws = None
            
            # Reconectar com backoff
            if self._running and stream_key in self.active_streams:
                logger.info(f"🔄 Reconectando em {reconnect_delay}s...")
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
        
        logger.info(f"🛑 WebSocket sender encerrado: {stream_key}")
    
    def stop_stream(self, stream_key: str) -> dict:
        """Para um stream ativo"""
        if stream_key not in self.active_streams:
            return {"success": False, "error": "Stream não encontrado"}
        
        stream = self.active_streams[stream_key]
        
        try:
            # Parar FFmpeg
            stream.ffmpeg_process.terminate()
            try:
                stream.ffmpeg_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stream.ffmpeg_process.kill()
            
            # Thread vai parar automaticamente
            stream.status = "stopped"
            del self.active_streams[stream_key]
            
            if self.on_status_change:
                self.on_status_change(stream_key, "stopped", "")
            
            return {"success": True, "stream_key": stream_key}
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def stop_all(self):
        """Para todos os streams"""
        self._running = False
        for key in list(self.active_streams.keys()):
            self.stop_stream(key)
    
    def get_stream_status(self, stream_key: str) -> Optional[dict]:
        """Retorna status de um stream"""
        if stream_key not in self.active_streams:
            return None
        
        stream = self.active_streams[stream_key]
        is_running = stream.ffmpeg_process.poll() is None
        
        return {
            "stream_key": stream_key,
            "camera_name": stream.camera_name,
            "rtsp_url": stream.rtsp_url,
            "ws_url": stream.ws_url,
            "status": "running" if is_running else stream.status,
            "started_at": stream.started_at.isoformat(),
            "bytes_sent": stream.bytes_sent,
            "mode": "websocket"
        }
    
    def get_all_streams(self) -> list:
        """Retorna lista de todos os streams ativos"""
        return [self.get_stream_status(key) for key in self.active_streams]


# Teste standalone
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    producer = WebSocketProducer(
        server_url="wss://your-railway-server.up.railway.app"
    )
    
    if not producer.is_available():
        print("❌ FFmpeg não encontrado!")
        exit(1)
    
    print("✓ WebSocket Producer pronto")
    print("\nDigite: start <stream_key> <rtsp_url>")
    print("       stop <stream_key>")
    print("       list")
    print("       quit")
    
    while True:
        try:
            cmd = input("\n> ").strip().split()
            if not cmd:
                continue
            
            if cmd[0] == "start" and len(cmd) >= 3:
                result = producer.start_stream(cmd[1], cmd[2])
                print(result)
            elif cmd[0] == "stop" and len(cmd) >= 2:
                result = producer.stop_stream(cmd[1])
                print(result)
            elif cmd[0] == "list":
                streams = producer.get_all_streams()
                for s in streams:
                    print(f"  {s['stream_key']}: {s['status']} ({s['bytes_sent']} bytes)")
            elif cmd[0] == "quit":
                producer.stop_all()
                break
            else:
                print("Comando inválido")
        except KeyboardInterrupt:
            producer.stop_all()
            break
