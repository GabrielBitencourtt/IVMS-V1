#!/usr/bin/env python3
"""
IVMS Cloud Agent - Agente local que conecta ao backend cloud
Este agente roda na rede do cliente e se comunica com o cloud via HTTPS/WSS
Inclui suporte a eventos ONVIF para Motion Detection, Analytics, etc.
Suporta streaming via WebSocket (baixa latência ~1-2s)
"""

AGENT_VERSION = "2.0.0"  # Versão WebSocket-only (removido HLS/RTMP)

import os
import sys
import json
import time
import socket
import platform
import threading
import subprocess
import logging
import signal
from datetime import datetime
from typing import Optional, Dict, Any, Callable, List
from urllib.parse import urlparse
import argparse

# Dependências
try:
    import requests
except ImportError:
    print("❌ Instale requests: pip install requests")
    sys.exit(1)

try:
    from supabase import create_client, Client
except ImportError:
    print("❌ Instale supabase: pip install supabase")
    sys.exit(1)

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)
logger.info(f"🚀 IVMS Cloud Agent v{AGENT_VERSION} iniciando...")

# Tenta importar cliente ONVIF
ONVIF_AVAILABLE = False
try:
    from onvif_events import OnvifEventsManager, OnvifEvent
    ONVIF_AVAILABLE = True
    logger.info("📡 ONVIF Events disponível")
except ImportError:
    logger.warning("⚠️ ONVIF Events não disponível (onvif_events.py não encontrado)")

# Tenta importar WebSocket Producer
WEBSOCKET_AVAILABLE = False
try:
    from websocket_producer import WebSocketProducer
    WEBSOCKET_AVAILABLE = True
    logger.info("⚡ WebSocket streaming disponível")
except ImportError:
    logger.warning("⚠️ WebSocket streaming não disponível (websocket_producer.py não encontrado)")


class StreamProcess:
    """Representa um processo de streaming ativo"""
    def __init__(self, stream_key: str, rtsp_url: str, camera_name: str = ""):
        self.stream_key = stream_key
        self.rtsp_url = rtsp_url
        self.camera_name = camera_name
        self.process: Optional[subprocess.Popen] = None
        self.started_at: Optional[datetime] = None
        self.status: str = "stopped"  # stopped, starting, running, error
        self.error: Optional[str] = None


class CloudAgent:
    """
    Agente que conecta ao backend cloud IVMS
    - Autentica via device token
    - Envia heartbeats periódicos
    - Recebe comandos do cloud
    - Gerencia streams RTSP → RTMP
    - Escuta eventos ONVIF e envia para o cloud
    """
    
    def __init__(
        self,
        cloud_url: str,
        device_token: str,
        rtmp_url: str = "rtmp://localhost/live",
        heartbeat_interval: int = 10,  # Reduced from 30s for faster command processing
        enable_onvif_events: bool = True,
        enable_websocket: bool = True,  # Habilitar streaming WebSocket
        websocket_server_url: str = "",  # URL do servidor WebSocket (Railway)
    ):
        self.cloud_url = cloud_url.rstrip('/')
        self.device_token = device_token
        self.rtmp_url = rtmp_url
        self.heartbeat_interval = heartbeat_interval
        self.enable_onvif_events = enable_onvif_events and ONVIF_AVAILABLE
        self.enable_websocket = enable_websocket and WEBSOCKET_AVAILABLE
        self.websocket_server_url = websocket_server_url
        
        # Estado
        self.agent_id: Optional[str] = None
        self.client_id: Optional[str] = None
        self.user_id: Optional[str] = None
        self.supabase: Optional[Client] = None
        self.supabase_url: Optional[str] = None
        self.supabase_key: Optional[str] = None
        
        # Streams ativos (RTMP)
        self.streams: Dict[str, StreamProcess] = {}
        self.streams_lock = threading.Lock()
        
        # WebSocket Producer para streaming de baixa latência
        self.ws_producer: Optional[WebSocketProducer] = None
        logger.info(f"⚡ WebSocket config: enable={self.enable_websocket}, url={websocket_server_url}")
        if self.enable_websocket and websocket_server_url:
            try:
                self.ws_producer = WebSocketProducer(
                    server_url=websocket_server_url,
                    on_status_change=self._on_ws_status_change
                )
                logger.info(f"⚡ WebSocket Producer: ✓ (server: {websocket_server_url})")
            except Exception as e:
                logger.error(f"❌ Falha ao criar WebSocket Producer: {e}")
                self.ws_producer = None
        else:
            logger.info(f"⚡ WebSocket Producer: ✗ (desabilitado)")
        
        
        # ONVIF Events Manager
        self.onvif_manager: Optional[OnvifEventsManager] = None
        self.onvif_cameras: Dict[str, Dict] = {}  # IP -> {username, password, name}
        
        # FFmpeg
        self.ffmpeg_path = self._find_ffmpeg()
        self.ffmpeg_available = self.ffmpeg_path is not None
        
        # Sistema
        self.hostname = socket.gethostname()
        self.local_ip = self._get_local_ip()
        self.os_info = f"{platform.system()} {platform.release()}"
        self.network_range = self._get_network_range()
        
        # Controle
        self.running = False
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.realtime_channel = None
        
        # Event buffer for batching
        self._event_buffer: List[Dict] = []
        self._event_buffer_lock = threading.Lock()
        self._last_event_flush = time.time()
        
        logger.info(f"🖥️  Hostname: {self.hostname}")
        logger.info(f"🌐 IP Local: {self.local_ip}")
        logger.info(f"💻 Sistema: {self.os_info}")
        logger.info(f"🎬 FFmpeg: {'✓ Disponível' if self.ffmpeg_available else '✗ Não encontrado'}")
        logger.info(f"📡 ONVIF Events: {'✓ Habilitado' if self.enable_onvif_events else '✗ Desabilitado'}")
        logger.info(f"⚡ WebSocket: {'✓ Habilitado' if self.ws_producer else '✗ Desabilitado'}")
    
    def _on_ws_status_change(self, stream_key: str, status: str, error: str):
        """Callback quando status de um stream WebSocket muda"""
        logger.info(f"[WS] Stream {stream_key}: {status}" + (f" - {error}" if error else ""))
    
    def _find_ffmpeg(self) -> Optional[str]:
        """Encontra o executável do FFmpeg"""
        # Tenta encontrar no PATH
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                return "ffmpeg"
        except:
            pass
        
        # Tenta caminhos comuns
        common_paths = [
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "C:\\ffmpeg\\bin\\ffmpeg.exe",
            os.path.expanduser("~/.local/bin/ffmpeg"),
        ]
        
        for path in common_paths:
            if os.path.isfile(path):
                return path
        
        return None
    
    def _get_local_ip(self) -> str:
        """Obtém o IP local da máquina"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
    
    def _get_network_range(self) -> str:
        """Estima o range da rede local"""
        try:
            ip_parts = self.local_ip.split('.')
            return f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.0/24"
        except:
            return "192.168.1.0/24"
    
    def register(self) -> bool:
        """Registra o agente no cloud"""
        logger.info("📡 Registrando agente no cloud...")
        
        try:
            response = requests.post(
                f"{self.cloud_url}/functions/v1/agent-register",
                headers={
                    "Content-Type": "application/json",
                    "x-device-token": self.device_token,
                },
                json={
                    "hostname": self.hostname,
                    "local_ip": self.local_ip,
                    "os_info": self.os_info,
                    "ffmpeg_installed": self.ffmpeg_available,
                    "network_range": self.network_range,
                },
                timeout=30,
            )
            
            if response.status_code != 200:
                logger.error(f"❌ Falha no registro: {response.status_code} - {response.text}")
                return False
            
            data = response.json()
            
            if not data.get("success"):
                logger.error(f"❌ Registro rejeitado: {data.get('error')}")
                return False
            
            self.agent_id = data["agent_id"]
            self.client_id = data["client_id"]
            self.user_id = data["user_id"]
            self.supabase_url = data["supabase_url"]
            self.supabase_key = data["supabase_anon_key"]
            
            logger.info(f"✅ Registrado como {data.get('agent_name', 'Agent')}")
            logger.info(f"   Agent ID: {self.agent_id}")
            logger.info(f"   User ID: {self.user_id}")
            
            # Inicializa cliente Supabase para Realtime
            self.supabase = create_client(self.supabase_url, self.supabase_key)
            
            return True
            
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Erro de conexão: {e}")
            return False
    
    def send_heartbeat(self) -> List[Dict]:
        """Envia heartbeat e retorna comandos pendentes"""
        try:
            # Coleta status das câmeras ativas (streams)
            camera_statuses = []
            with self.streams_lock:
                for stream in self.streams.values():
                    camera_statuses.append({
                        "stream_key": stream.stream_key,
                        "status": stream.status,
                        "error_message": stream.error,
                    })
            
            # Coleta status das conexões ONVIF
            onvif_statuses = []
            for camera_ip, info in self.onvif_cameras.items():
                onvif_statuses.append({
                    "camera_ip": camera_ip,
                    "camera_name": info.get("name", ""),
                    "onvif_connected": True,
                    "port": info.get("port", 80),
                })
            
            response = requests.post(
                f"{self.cloud_url}/functions/v1/agent-heartbeat",
                headers={
                    "Content-Type": "application/json",
                    "x-device-token": self.device_token,
                },
                json={
                    "client_id": self.client_id,
                    "active_streams": len([s for s in self.streams.values() if s.status == "running"]),
                    "camera_statuses": camera_statuses,
                    "onvif_statuses": onvif_statuses,
                },
                timeout=15,
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("pending_commands", [])
            else:
                logger.warning(f"⚠️ Heartbeat falhou: {response.status_code}")
                return []
                
        except Exception as e:
            logger.error(f"❌ Erro no heartbeat: {e}")
            return []
    
    def send_command_result(
        self,
        command_id: str,
        status: str,
        result: Optional[Dict] = None,
        error_message: Optional[str] = None
    ):
        """Envia resultado de um comando para o cloud"""
        try:
            requests.post(
                f"{self.cloud_url}/functions/v1/agent-command-result",
                headers={
                    "Content-Type": "application/json",
                    "x-device-token": self.device_token,
                },
                json={
                    "command_id": command_id,
                    "status": status,
                    "result": result,
                    "error_message": error_message,
                },
                timeout=15,
            )
        except Exception as e:
            logger.error(f"❌ Erro ao enviar resultado: {e}")
    
    def process_command(self, command: Dict):
        """Processa um comando recebido do cloud"""
        cmd_id = command["id"]
        cmd_type = command["command_type"]
        payload = command.get("payload", {})
        
        logger.info(f"📥 Comando recebido: '{cmd_type}' (tipo: {type(cmd_type).__name__}, repr: {repr(cmd_type)})")
        
        try:
            if cmd_type == "start_stream":
                result = self._handle_start_stream(payload)
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "stop_stream":
                result = self._handle_stop_stream(payload)
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "test_rtsp":
                result = self._handle_test_rtsp(payload)
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "scan_network":
                result = self._handle_scan_network(payload)
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "get_status":
                result = self._handle_get_status()
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "test_onvif":
                logger.info(f"🔧 Processando test_onvif com payload: {payload}")
                result = self._handle_test_onvif(payload)
                logger.info(f"📤 Resultado test_onvif: {result}")
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "start_onvif_events":
                logger.info(f"📡 Processando start_onvif_events com payload: {payload}")
                result = self._handle_start_onvif_events(payload)
                logger.info(f"📤 Resultado start_onvif_events: {result}")
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "stop_onvif_events":
                logger.info(f"🛑 Processando stop_onvif_events com payload: {payload}")
                result = self._handle_stop_onvif_events(payload)
                logger.info(f"📤 Resultado stop_onvif_events: {result}")
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "get_onvif_status":
                logger.info(f"📊 Processando get_onvif_status")
                result = self._handle_get_onvif_status()
                self.send_command_result(cmd_id, "completed", result)
                
            else:
                self.send_command_result(
                    cmd_id, 
                    "failed", 
                    error_message=f"Comando desconhecido: {cmd_type}"
                )
                
        except Exception as e:
            logger.error(f"❌ Erro ao processar comando: {e}")
            self.send_command_result(cmd_id, "failed", error_message=str(e))
    
    def _handle_start_stream(self, payload: Dict) -> Dict:
        """Inicia um stream RTSP → WebSocket e opcionalmente escuta eventos ONVIF"""
        stream_key = payload.get("stream_key")
        rtsp_url = payload.get("rtsp_url")
        camera_name = payload.get("camera_name", "")
        enable_events = payload.get("enable_onvif_events", True)
        onvif_username = payload.get("onvif_username", "admin")
        onvif_password = payload.get("onvif_password", "")
        onvif_port = payload.get("onvif_port", 80)
        
        if not stream_key or not rtsp_url:
            return {"success": False, "error": "stream_key e rtsp_url são obrigatórios"}
        
        if not self.ffmpeg_available:
            return {"success": False, "error": "FFmpeg não disponível"}
        
        # Extrai IP da câmera da URL RTSP
        camera_ip = self._extract_ip_from_rtsp(rtsp_url)
        
        # Extrai credenciais da URL RTSP se não fornecidas
        if not onvif_password:
            parsed_creds = self._extract_credentials_from_rtsp(rtsp_url)
            if parsed_creds:
                onvif_username = parsed_creds.get("username", onvif_username)
                onvif_password = parsed_creds.get("password", "")
        
        # Câmeras locais usam APENAS WebSocket (menor latência)
        if not self.ws_producer:
            return {"success": False, "error": "WebSocket Producer não disponível. Verifique a configuração."}
        
        logger.info(f"⚡ Iniciando stream WebSocket: {stream_key}")
        
        # Verifica se já existe
        if self.ws_producer.get_stream_status(stream_key):
            return {"success": False, "error": "Stream já existe"}
        
        result = self.ws_producer.start_stream(stream_key, rtsp_url, camera_name)
        
        if result.get("success"):
            # Inicia escuta de eventos ONVIF se habilitado
            if enable_events and self.enable_onvif_events and camera_ip:
                self._start_onvif_events(
                    camera_ip=camera_ip,
                    camera_name=camera_name or stream_key,
                    username=onvif_username,
                    password=onvif_password,
                    port=onvif_port,
                )
            
            return {
                "success": True,
                "stream_key": stream_key,
                "mode": "websocket",
                "ws_url": result.get("ws_url"),
                "onvif_events": enable_events and self.enable_onvif_events,
            }
        else:
            return {"success": False, "error": result.get("error", "Falha ao iniciar WebSocket stream")}
    
    def _handle_stop_stream(self, payload: Dict) -> Dict:
        """Para um stream WebSocket ativo e a escuta de eventos ONVIF"""
        stream_key = payload.get("stream_key")
        camera_ip = payload.get("camera_ip")  # Opcional: IP para parar ONVIF
        
        if not stream_key:
            return {"success": False, "error": "stream_key é obrigatório"}
        
        # Para stream WebSocket
        if not self.ws_producer:
            return {"success": False, "error": "WebSocket Producer não disponível"}
        
        ws_status = self.ws_producer.get_stream_status(stream_key)
        if not ws_status:
            return {"success": False, "error": "Stream não encontrado"}
        
        result = self.ws_producer.stop_stream(stream_key)
        if not result.get("success"):
            return {"success": False, "error": result.get("error", "Falha ao parar stream")}
        
        logger.info(f"🛑 Stream WebSocket {stream_key} parado")
        
        # Para escuta ONVIF se ativa
        if camera_ip and self.onvif_manager:
            self._stop_onvif_events(camera_ip)
        
        return {"success": True, "stream_key": stream_key, "mode": "websocket"}
    
    def _handle_test_rtsp(self, payload: Dict) -> Dict:
        """Testa conexão RTSP usando socket direto com suporte a Digest Auth"""
        rtsp_url = payload.get("rtsp_url")
        
        if not rtsp_url:
            return {"success": False, "error": "rtsp_url é obrigatório"}
        
        start_time = time.time()
        
        try:
            # Usa o módulo de teste RTSP com suporte a Digest Auth
            from rtsp_tester import test_rtsp_connection
            
            success, message, details = test_rtsp_connection(rtsp_url, timeout=10)
            response_time = int((time.time() - start_time) * 1000)
            
            if success:
                return {
                    "success": True,
                    "message": message,
                    "rtsp_url": rtsp_url,
                    "response_time_ms": response_time,
                    "requires_auth": details.get("requires_auth", False),
                    "auth_type": details.get("auth_type"),
                    "codec": details.get("codec"),
                }
            else:
                return {
                    "success": False,
                    "rtsp_url": rtsp_url,
                    "error": message,
                    "requires_auth": details.get("requires_auth", False),
                }
                
        except Exception as e:
            logger.error(f"Erro ao testar RTSP: {e}")
            return {
                "success": False,
                "rtsp_url": rtsp_url,
                "error": str(e),
            }
    
    def _handle_scan_network(self, payload: Dict) -> Dict:
        """Escaneia a rede em busca de câmeras (simplificado)"""
        # TODO: Implementar scanner de rede completo
        return {
            "success": True,
            "network_range": self.network_range,
            "message": "Scan de rede não implementado nesta versão",
        }
    
    def _handle_get_status(self) -> Dict:
        """Retorna status atual do agente incluindo streams WebSocket e RTMP"""
        # Streams RTMP
        with self.streams_lock:
            rtmp_streams = [
                {
                    "stream_key": s.stream_key,
                    "camera_name": s.camera_name,
                    "status": s.status,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "error": s.error,
                    "mode": "rtmp",
                }
                for s in self.streams.values()
            ]
        
        # Streams WebSocket
        ws_streams = []
        if self.ws_producer:
            ws_streams = [
                {
                    "stream_key": s["stream_key"],
                    "camera_name": s.get("camera_name", ""),
                    "status": s["status"],
                    "started_at": s.get("started_at"),
                    "bytes_sent": s.get("bytes_sent", 0),
                    "mode": "websocket",
                }
                for s in self.ws_producer.get_all_streams()
            ]
        
        all_streams = rtmp_streams + ws_streams
        
        return {
            "hostname": self.hostname,
            "local_ip": self.local_ip,
            "os_info": self.os_info,
            "ffmpeg_available": self.ffmpeg_available,
            "websocket_available": self.ws_producer is not None,
            "websocket_server": self.websocket_server_url if self.ws_producer else None,
            "active_streams": len([s for s in all_streams if s["status"] == "running"]),
            "rtmp_streams": len(rtmp_streams),
            "ws_streams": len(ws_streams),
            "streams": all_streams,
        }
    
    def _handle_test_onvif(self, payload: Dict) -> Dict:
        """Testa conectividade ONVIF e retorna capabilities da câmera"""
        # Suporta tanto 'camera_ip' quanto 'ip' para compatibilidade
        camera_ip = payload.get("camera_ip") or payload.get("ip")
        camera_port = payload.get("camera_port") or payload.get("port", 80)
        username = payload.get("username", "admin")
        password = payload.get("password", "")
        
        if not camera_ip:
            return {"success": False, "error": "camera_ip ou ip é obrigatório"}
        
        if not ONVIF_AVAILABLE:
            return {
                "success": False, 
                "error": "Módulo ONVIF não disponível no agente",
                "onvif_available": False,
            }
        
        start_time = time.time()
        
        try:
            # Cria cliente temporário para teste
            from onvif_events import OnvifEventsClient
            
            test_client = OnvifEventsClient(
                camera_ip=camera_ip,
                camera_port=camera_port,
                username=username,
                password=password,
                camera_name="test",
            )
            
            # Testa capabilities
            has_capabilities = test_client.check_capabilities()
            capabilities = test_client.event_capabilities if has_capabilities else {}
            
            # Tenta criar subscription para verificar suporte completo
            subscription_ok = False
            subscription_error = None
            
            if has_capabilities:
                try:
                    subscription_ok = test_client.create_pull_point_subscription()
                except Exception as sub_e:
                    subscription_error = str(sub_e)
            
            response_time = int((time.time() - start_time) * 1000)
            
            # Tenta obter informações do dispositivo
            device_info = self._get_onvif_device_info(camera_ip, camera_port, username, password)
            
            return {
                "success": has_capabilities,
                "camera_ip": camera_ip,
                "camera_port": camera_port,
                "response_time_ms": response_time,
                "onvif_available": True,
                "capabilities": capabilities,
                "pull_point_support": capabilities.get("pull_point", False),
                "basic_notification_support": capabilities.get("basic_notification_interface", False),
                "subscription_test": subscription_ok,
                "subscription_error": subscription_error,
                "device_info": device_info,
                "message": "Câmera suporta eventos ONVIF" if has_capabilities else "Câmera não suporta eventos ONVIF ou credenciais inválidas",
            }
            
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            logger.error(f"❌ Erro ao testar ONVIF: {e}")
            return {
                "success": False,
                "camera_ip": camera_ip,
                "response_time_ms": response_time,
                "error": str(e),
                "onvif_available": True,
                "message": f"Erro ao conectar: {str(e)}",
            }
    
    def _get_onvif_device_info(self, camera_ip: str, camera_port: int, username: str, password: str) -> Dict:
        """Obtém informações do dispositivo via ONVIF"""
        try:
            from onvif_events import OnvifAuth
            import xml.etree.ElementTree as ET
            
            wsse_header = OnvifAuth.create_wsse_header(username, password)
            
            # GetDeviceInformation request
            envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
            <soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                           xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
                <soap:Header>
                    {wsse_header}
                </soap:Header>
                <soap:Body>
                    <tds:GetDeviceInformation/>
                </soap:Body>
            </soap:Envelope>'''
            
            response = requests.post(
                f"http://{camera_ip}:{camera_port}/onvif/device_service",
                data=envelope,
                headers={
                    'Content-Type': 'application/soap+xml; charset=utf-8',
                    'SOAPAction': 'http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation',
                },
                timeout=5,
            )
            
            if response.status_code == 200:
                root = ET.fromstring(response.text)
                
                # Parse device info
                ns = {'tds': 'http://www.onvif.org/ver10/device/wsdl'}
                info = root.find('.//tds:GetDeviceInformationResponse', ns)
                
                if info is not None:
                    return {
                        "manufacturer": info.findtext('tds:Manufacturer', '', ns),
                        "model": info.findtext('tds:Model', '', ns),
                        "firmware_version": info.findtext('tds:FirmwareVersion', '', ns),
                        "serial_number": info.findtext('tds:SerialNumber', '', ns),
                        "hardware_id": info.findtext('tds:HardwareId', '', ns),
                    }
        except Exception as e:
            logger.debug(f"Não foi possível obter info do dispositivo: {e}")
        
        return {}
    
    def _handle_start_onvif_events(self, payload: Dict) -> Dict:
        """Inicia escuta de eventos ONVIF para uma câmera específica"""
        camera_ip = payload.get("camera_ip") or payload.get("ip")
        camera_port = payload.get("camera_port") or payload.get("port", 80)
        username = payload.get("username", "admin")
        password = payload.get("password", "")
        camera_name = payload.get("camera_name", "")
        camera_id = payload.get("camera_id", "")
        
        if not camera_ip:
            return {"success": False, "error": "camera_ip ou ip é obrigatório"}
        
        if not ONVIF_AVAILABLE:
            return {
                "success": False, 
                "error": "Módulo ONVIF não disponível no agente",
                "onvif_available": False,
            }
        
        # Verifica se já está escutando
        if camera_ip in self.onvif_cameras:
            return {
                "success": True,
                "camera_ip": camera_ip,
                "message": "Já está escutando eventos desta câmera",
                "already_listening": True,
            }
        
        try:
            # Inicia escuta
            self._start_onvif_events(
                camera_ip=camera_ip,
                camera_name=camera_name or f"Camera_{camera_ip}",
                username=username,
                password=password,
                port=camera_port,
            )
            
            # Aguarda um pouco para verificar se iniciou
            time.sleep(1)
            
            is_listening = camera_ip in self.onvif_cameras
            
            return {
                "success": is_listening,
                "camera_ip": camera_ip,
                "camera_id": camera_id,
                "camera_name": camera_name,
                "port": camera_port,
                "message": "Escuta de eventos ONVIF iniciada" if is_listening else "Falha ao iniciar escuta",
            }
            
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar ONVIF events: {e}")
            return {
                "success": False,
                "camera_ip": camera_ip,
                "error": str(e),
            }
    
    def _handle_stop_onvif_events(self, payload: Dict) -> Dict:
        """Para escuta de eventos ONVIF de uma câmera"""
        camera_ip = payload.get("camera_ip") or payload.get("ip")
        camera_id = payload.get("camera_id", "")
        
        if not camera_ip:
            return {"success": False, "error": "camera_ip ou ip é obrigatório"}
        
        if camera_ip not in self.onvif_cameras:
            return {
                "success": True,
                "camera_ip": camera_ip,
                "message": "Câmera não estava sendo monitorada",
                "was_listening": False,
            }
        
        try:
            self._stop_onvif_events(camera_ip)
            
            return {
                "success": True,
                "camera_ip": camera_ip,
                "camera_id": camera_id,
                "message": "Escuta de eventos ONVIF parada",
            }
            
        except Exception as e:
            logger.error(f"❌ Erro ao parar ONVIF events: {e}")
            return {
                "success": False,
                "camera_ip": camera_ip,
                "error": str(e),
            }
    
    def _handle_get_onvif_status(self) -> Dict:
        """Retorna status de todas as escutas ONVIF ativas"""
        cameras_listening = []
        
        for camera_ip, info in self.onvif_cameras.items():
            cameras_listening.append({
                "camera_ip": camera_ip,
                "camera_name": info.get("name", ""),
                "port": info.get("port", 80),
            })
        
        manager_status = {}
        if self.onvif_manager:
            manager_status = self.onvif_manager.get_status()
        
        return {
            "success": True,
            "onvif_available": ONVIF_AVAILABLE,
            "cameras_listening": cameras_listening,
            "total_cameras": len(cameras_listening),
            "manager_status": manager_status,
            "event_buffer_size": len(self._event_buffer),
        }

    def _heartbeat_loop(self):
        """Loop de heartbeat"""
        while self.running:
            commands = self.send_heartbeat()
            
            # Processa comandos recebidos
            for cmd in commands:
                try:
                    self.process_command(cmd)
                except Exception as e:
                    logger.error(f"Erro ao processar comando: {e}")
            
            time.sleep(self.heartbeat_interval)
    
    def _monitor_streams(self):
        """Monitora saúde dos streams, conexões ONVIF e faz flush de eventos"""
        onvif_check_counter = 0
        
        while self.running:
            with self.streams_lock:
                for stream_key, stream in list(self.streams.items()):
                    if stream.process and stream.status == "running":
                        retcode = stream.process.poll()
                        if retcode is not None:
                            logger.warning(f"⚠️ Stream {stream_key} terminou (code: {retcode})")
                            stream.status = "error"
                            stream.error = f"FFmpeg terminou com código {retcode}"
            
            # Flush eventos periodicamente
            self._flush_events()
            
            # Envia status ONVIF a cada 30 segundos
            onvif_check_counter += 1
            if onvif_check_counter >= 6:  # 6 * 5s = 30s
                self._send_onvif_status_update()
                onvif_check_counter = 0
            
            time.sleep(5)
    
    def _extract_ip_from_rtsp(self, rtsp_url: str) -> Optional[str]:
        """Extrai IP de uma URL RTSP"""
        try:
            parsed = urlparse(rtsp_url)
            host = parsed.hostname
            if host:
                return host
        except:
            pass
        return None
    
    def _extract_credentials_from_rtsp(self, rtsp_url: str) -> Optional[Dict]:
        """Extrai credenciais de uma URL RTSP"""
        try:
            parsed = urlparse(rtsp_url)
            if parsed.username and parsed.password:
                return {
                    "username": parsed.username,
                    "password": parsed.password,
                }
        except:
            pass
        return None
    
    def _start_onvif_events(
        self,
        camera_ip: str,
        camera_name: str,
        username: str = "admin",
        password: str = "",
        port: int = 80,
    ):
        """Inicia escuta de eventos ONVIF para uma câmera"""
        if not self.enable_onvif_events:
            return
        
        # Inicializa manager se necessário
        if self.onvif_manager is None:
            self.onvif_manager = OnvifEventsManager(
                event_callback=self._on_onvif_event
            )
            logger.info("📡 ONVIF Events Manager inicializado")
        
        # Verifica se já está escutando essa câmera
        if camera_ip in self.onvif_cameras:
            logger.info(f"📡 Já escutando eventos de {camera_ip}")
            return
        
        # Registra câmera
        self.onvif_cameras[camera_ip] = {
            "name": camera_name,
            "username": username,
            "password": password,
            "port": port,
        }
        
        # Inicia escuta em thread separada
        def start_listener():
            try:
                success = self.onvif_manager.add_camera(
                    camera_ip=camera_ip,
                    username=username,
                    password=password,
                    camera_name=camera_name,
                    camera_port=port,
                )
                if success:
                    logger.info(f"📡 Escutando eventos ONVIF de {camera_name} ({camera_ip})")
                else:
                    logger.warning(f"⚠️ Não foi possível iniciar ONVIF para {camera_name}")
            except Exception as e:
                logger.error(f"❌ Erro ao iniciar ONVIF: {e}")
        
        threading.Thread(target=start_listener, daemon=True).start()
    
    def _stop_onvif_events(self, camera_ip: str):
        """Para escuta de eventos ONVIF de uma câmera"""
        if self.onvif_manager and camera_ip in self.onvif_cameras:
            try:
                self.onvif_manager.remove_camera(camera_ip)
                del self.onvif_cameras[camera_ip]
                logger.info(f"📡 Parou escuta ONVIF de {camera_ip}")
            except Exception as e:
                logger.error(f"❌ Erro ao parar ONVIF: {e}")
    
    def _on_onvif_event(self, event):
        """Callback quando um evento ONVIF é recebido"""
        logger.info(f"📥 Evento ONVIF: {event.event_type} de {event.camera_name}")
        
        # Adiciona ao buffer para envio em lote
        event_data = {
            "event_type": event.event_type,
            "camera_ip": event.camera_ip,
            "camera_name": event.camera_name,
            "severity": self._map_event_severity(event.event_type),
            "message": f"{event.event_type.replace('_', ' ').title()} detectado",
            "metadata": {
                "topic": event.topic,
                "source": event.source,
                "data": event.data,
            },
            "timestamp": event.timestamp.isoformat(),
        }
        
        with self._event_buffer_lock:
            self._event_buffer.append(event_data)
        
        # Flush imediato para eventos críticos
        if event.event_type in ['tampering', 'video_loss', 'intrusion_detection']:
            self._flush_events()
    
    def _map_event_severity(self, event_type: str) -> str:
        """Mapeia tipo de evento para severidade"""
        critical = ['tampering', 'video_loss']
        warning = ['intrusion_detection', 'line_crossing', 'alarm_input']
        
        if event_type in critical:
            return 'critical'
        elif event_type in warning:
            return 'warning'
        return 'info'
    
    def _flush_events(self):
        """Envia eventos em lote para o cloud"""
        with self._event_buffer_lock:
            if not self._event_buffer:
                return
            
            # Limita a 50 eventos por vez
            events_to_send = self._event_buffer[:50]
            self._event_buffer = self._event_buffer[50:]
        
        logger.info(f"📤 Enviando {len(events_to_send)} eventos ONVIF para o cloud...")
        
        try:
            headers = {
                "Content-Type": "application/json",
                "x-device-token": self.device_token,
            }
            
            # Adiciona apikey se disponível
            if self.supabase_key:
                headers["apikey"] = self.supabase_key
            else:
                logger.warning("⚠️ supabase_key não disponível, tentando sem apikey")
            
            url = f"{self.cloud_url}/functions/v1/receive-camera-event"
            logger.debug(f"URL: {url}")
            
            response = requests.post(
                url,
                headers=headers,
                json={"events": events_to_send},
                timeout=15,
            )
            
            if response.status_code == 200:
                logger.info(f"✅ Enviados {len(events_to_send)} eventos com sucesso")
            else:
                logger.warning(f"⚠️ Falha ao enviar eventos: {response.status_code} - {response.text}")
                # Recoloca eventos no buffer
                with self._event_buffer_lock:
                    self._event_buffer = events_to_send + self._event_buffer
                    
        except Exception as e:
            logger.error(f"❌ Erro ao enviar eventos: {e}")
            # Recoloca eventos no buffer
            with self._event_buffer_lock:
                self._event_buffer = events_to_send + self._event_buffer
    
    def _fetch_onvif_cameras(self) -> List[Dict]:
        """Busca todas as câmeras com ONVIF habilitado do banco de dados"""
        if not self.supabase:
            logger.warning("⚠️ Supabase não inicializado, não é possível buscar câmeras")
            return []
        
        try:
            logger.info("📋 Buscando câmeras com ONVIF habilitado...")
            
            # Busca câmeras do usuário com ONVIF habilitado
            response = self.supabase.table("cameras").select("*").eq(
                "user_id", self.user_id
            ).eq("onvif_enabled", True).execute()
            
            cameras = response.data or []
            logger.info(f"📷 Encontradas {len(cameras)} câmeras com ONVIF habilitado")
            
            return cameras
            
        except Exception as e:
            logger.error(f"❌ Erro ao buscar câmeras: {e}")
            return []
    
    def _auto_start_onvif_listeners(self):
        """Inicia automaticamente escuta ONVIF para todas as câmeras habilitadas"""
        cameras = self._fetch_onvif_cameras()
        
        if not cameras:
            logger.info("📷 Nenhuma câmera com ONVIF habilitado encontrada")
            return
        
        logger.info(f"📡 Iniciando escuta ONVIF automática para {len(cameras)} câmeras...")
        
        for camera in cameras:
            try:
                # Extrai IP da URL RTSP
                camera_ip = self._extract_ip_from_rtsp(camera.get("stream_url", ""))
                
                if not camera_ip:
                    logger.warning(f"⚠️ Não foi possível extrair IP da câmera {camera.get('name')}")
                    continue
                
                # Define credenciais ONVIF
                onvif_port = camera.get("onvif_port", 80) or 80
                
                # Se usa credenciais RTSP, extrai da URL
                if camera.get("onvif_use_rtsp_credentials", True):
                    creds = self._extract_credentials_from_rtsp(camera.get("stream_url", ""))
                    if creds:
                        username = creds.get("username", "admin")
                        password = creds.get("password", "")
                    else:
                        username = camera.get("onvif_username", "admin") or "admin"
                        password = camera.get("onvif_password", "") or ""
                else:
                    username = camera.get("onvif_username", "admin") or "admin"
                    password = camera.get("onvif_password", "") or ""
                
                camera_name = camera.get("name", f"Camera_{camera_ip}")
                
                logger.info(f"📡 Iniciando ONVIF para {camera_name} ({camera_ip}:{onvif_port})")
                
                # Inicia escuta ONVIF
                self._start_onvif_events(
                    camera_ip=camera_ip,
                    camera_name=camera_name,
                    username=username,
                    password=password,
                    port=onvif_port,
                )
                
                # Pequeno delay entre câmeras para não sobrecarregar
                time.sleep(0.5)
                
            except Exception as e:
                logger.error(f"❌ Erro ao iniciar ONVIF para câmera {camera.get('name')}: {e}")
    
    def _send_onvif_status_update(self):
        """Envia status das conexões ONVIF para o cloud"""
        if not self.supabase or not self.onvif_cameras:
            return
        
        try:
            # Prepara status das câmeras ONVIF
            onvif_statuses = []
            
            for camera_ip, info in self.onvif_cameras.items():
                status_data = {
                    "camera_ip": camera_ip,
                    "camera_name": info.get("name", ""),
                    "onvif_connected": True,
                    "port": info.get("port", 80),
                }
                onvif_statuses.append(status_data)
            
            # Inclui status no próximo heartbeat (via metadata)
            logger.debug(f"📊 ONVIF status: {len(onvif_statuses)} câmeras conectadas")
            
        except Exception as e:
            logger.debug(f"Erro ao preparar status ONVIF: {e}")
    
    def start(self):
        """Inicia o agente"""
        logger.info("🚀 Iniciando IVMS Cloud Agent...")
        
        # Registra no cloud
        if not self.register():
            logger.error("❌ Falha no registro. Encerrando.")
            return False
        
        self.running = True
        
        # Inicia threads
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        
        self.monitor_thread = threading.Thread(target=self._monitor_streams, daemon=True)
        self.monitor_thread.start()
        
        # AUTO-START: Inicia escuta ONVIF para todas as câmeras habilitadas
        if self.enable_onvif_events:
            logger.info("📡 Iniciando auto-start ONVIF...")
            onvif_thread = threading.Thread(target=self._auto_start_onvif_listeners, daemon=True)
            onvif_thread.start()
        
        logger.info("✅ Agente iniciado e conectado ao cloud!")
        logger.info(f"📡 Heartbeat a cada {self.heartbeat_interval}s")
        
        return True
    
    def stop(self):
        """Para o agente"""
        logger.info("🛑 Parando agente...")
        self.running = False
        
        # Para escuta ONVIF
        if self.onvif_manager:
            self.onvif_manager.stop_all()
            logger.info("📡 ONVIF Events parado")
        
        # Para todos os streams WebSocket
        if self.ws_producer:
            self.ws_producer.stop_all()
            logger.info("⚡ WebSocket streams parados")
        
        # Para todos os streams RTMP
        with self.streams_lock:
            for stream_key in list(self.streams.keys()):
                self._handle_stop_stream({"stream_key": stream_key})
        
        # Flush final de eventos
        self._flush_events()
        
        logger.info("👋 Agente encerrado")
    
    def run_forever(self):
        """Executa o agente indefinidamente"""
        if not self.start():
            return
        
        # Configura handler de sinal
        def signal_handler(sig, frame):
            self.stop()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Mantém rodando
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()


def main():
    parser = argparse.ArgumentParser(description="IVMS Cloud Agent")
    parser.add_argument(
        "--cloud-url",
        default=os.environ.get("CLOUD_URL", "https://cedkflgtubaologqjker.supabase.co"),
        help="URL do backend cloud"
    )
    parser.add_argument(
        "--device-token",
        default=os.environ.get("DEVICE_TOKEN"),
        help="Token de autenticação do dispositivo"
    )
    parser.add_argument(
        "--rtmp-url",
        default=os.environ.get("RTMP_URL", "rtmp://localhost/live"),
        help="URL do servidor RTMP"
    )
    parser.add_argument(
        "--websocket-url",
        default=os.environ.get("WEBSOCKET_URL", "wss://ivms-v1-production.up.railway.app"),
        help="URL do servidor WebSocket para streaming de baixa latência"
    )
    parser.add_argument(
        "--heartbeat",
        type=int,
        default=30,
        help="Intervalo de heartbeat em segundos"
    )
    parser.add_argument(
        "--no-websocket",
        action="store_true",
        help="Desabilita streaming via WebSocket (usa apenas RTMP)"
    )
    
    args = parser.parse_args()
    
    if not args.device_token:
        print("❌ Device token é obrigatório!")
        print("   Use: --device-token TOKEN")
        print("   Ou defina a variável DEVICE_TOKEN")
        sys.exit(1)
    
    agent = CloudAgent(
        cloud_url=args.cloud_url,
        device_token=args.device_token,
        rtmp_url=args.rtmp_url,
        heartbeat_interval=args.heartbeat,
        enable_websocket=not args.no_websocket,
        websocket_server_url=args.websocket_url,
    )
    
    agent.run_forever()


if __name__ == "__main__":
    main()