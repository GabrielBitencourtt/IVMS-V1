#!/usr/bin/env python3
"""
Camera Scanner Agent - Aplicativo desktop para descoberta de câmeras na rede
Conecta diretamente com a plataforma via Supabase
Design modernizado para combinar com a plataforma web
"""

import sys
import os
import json
import socket
import threading
import queue
import logging
import webbrowser
from datetime import datetime
from typing import Dict, List, Optional, Callable
import urllib.request
import urllib.error
import ssl
import ipaddress
import concurrent.futures

# System tray support
try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
    ImageTk = None
    print("⚠ pystray ou PIL não instalado. Ícone na bandeja do sistema não disponível.")

# ONVIF Events support
ONVIF_AVAILABLE = False
try:
    from onvif_events import OnvifEventsManager, OnvifEvent, OnvifEventsClient, OnvifAuth
    ONVIF_AVAILABLE = True
    print("✓ ONVIF Events disponível")
except ImportError:
    print("⚠ ONVIF Events não disponível (onvif_events.py não encontrado)")

# Configuração
SUPABASE_URL = "https://cedkflgtubaologqjker.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNlZGtmbGd0dWJhb2xvZ3Fqa2VyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjgxNDgyMTksImV4cCI6MjA4MzcyNDIxOX0.VnJBlll6_aiSTzNg92zamW2d-V523yZW7oM28sQlL-E"

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Marcas conhecidas
CAMERA_BRANDS = {
    'hikvision': {
        'name': 'Hikvision',
        'ports': [554, 8000, 80, 443],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/Streaming/Channels/101',
            'rtsp://{user}:{pass}@{ip}:554/h264/ch1/main/av_stream',
            'rtsp://{user}:{pass}@{ip}:554/Streaming/Channels/102',
        ],
        'default_users': ['admin'],
        'default_passwords': ['admin', '12345', ''],
        'detection_keywords': ['hikvision', 'hikdigital', 'dvr', 'nvr', 'hik'],
    },
    'dahua': {
        'name': 'Dahua',
        'ports': [554, 37777, 80],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel=1&subtype=0',
            'rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel=1&subtype=1',
        ],
        'default_users': ['admin'],
        'default_passwords': ['admin', 'admin123', ''],
        'detection_keywords': ['dahua', 'dh-', 'amcrest'],
    },
    'intelbras': {
        'name': 'Intelbras',
        'ports': [554, 37777, 80],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel=1&subtype=0',
            'rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel=1&subtype=1',
            'rtsp://{user}:{pass}@{ip}:554/',
        ],
        'default_users': ['admin'],
        'default_passwords': ['admin', ''],
        'detection_keywords': ['intelbras', 'mhdx', 'vip', 'vhd'],
    },
    'axis': {
        'name': 'Axis',
        'ports': [554, 80, 443],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/axis-media/media.amp',
            'rtsp://{user}:{pass}@{ip}:554/axis-media/media.amp?videocodec=h264',
        ],
        'default_users': ['root', 'admin'],
        'default_passwords': ['root', 'admin', 'pass'],
        'detection_keywords': ['axis', 'axis communications'],
    },
    'vivotek': {
        'name': 'Vivotek',
        'ports': [554, 80, 443],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/live.sdp',
            'rtsp://{user}:{pass}@{ip}:554/live2.sdp',
        ],
        'default_users': ['root', 'admin'],
        'default_passwords': ['admin', ''],
        'detection_keywords': ['vivotek'],
    },
    'hanwha': {
        'name': 'Hanwha (Samsung)',
        'ports': [554, 80, 4520],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/profile1/media.smp',
            'rtsp://{user}:{pass}@{ip}:554/profile2/media.smp',
        ],
        'default_users': ['admin'],
        'default_passwords': ['admin', '4321', ''],
        'detection_keywords': ['hanwha', 'samsung', 'wisenet'],
    },
    'foscam': {
        'name': 'Foscam',
        'ports': [554, 88, 80],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/videoMain',
            'rtsp://{user}:{pass}@{ip}:88/videoMain',
        ],
        'default_users': ['admin'],
        'default_passwords': ['admin', ''],
        'detection_keywords': ['foscam'],
    },
    'tp-link': {
        'name': 'TP-Link',
        'ports': [554, 80, 443],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/stream1',
            'rtsp://{user}:{pass}@{ip}:554/stream2',
        ],
        'default_users': ['admin'],
        'default_passwords': ['admin', ''],
        'detection_keywords': ['tp-link', 'tplink', 'tapo'],
    },
    'generic': {
        'name': 'Câmera Genérica',
        'ports': [554, 80, 8080],
        'rtsp_templates': [
            'rtsp://{user}:{pass}@{ip}:554/stream1',
            'rtsp://{user}:{pass}@{ip}:554/',
            'rtsp://{user}:{pass}@{ip}:554/live/ch00_0',
        ],
        'default_users': ['admin', 'root'],
        'default_passwords': ['admin', '12345', ''],
        'detection_keywords': [],
    }
}

CAMERA_PORTS = [554, 80, 8080, 37777, 8000, 443, 4520, 88]


def test_rtsp_connection(rtsp_url: str, timeout: int = 5) -> tuple:
    """
    Testa conexão RTSP localmente com suporte a Basic e Digest Auth (incluindo qop=auth).
    Retorna (sucesso: bool, mensagem: str, detalhes: dict)
    """
    import re
    import hashlib
    import random
    import string
    
    debug_info = []  # Para coletar informações de debug
    
    def md5_hash(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()
    
    def generate_cnonce(length: int = 8) -> str:
        """Gera um client nonce aleatório (8 chars hex)"""
        return ''.join(random.choices('0123456789abcdef', k=length))
    
    def parse_www_authenticate(header: str) -> dict:
        """Parse WWW-Authenticate header para extrair realm, nonce, qop, etc"""
        result = {}
        # Extrai realm
        realm_match = re.search(r'realm="([^"]*)"', header)
        if realm_match:
            result['realm'] = realm_match.group(1)
        # Extrai nonce
        nonce_match = re.search(r'nonce="([^"]*)"', header)
        if nonce_match:
            result['nonce'] = nonce_match.group(1)
        # Extrai qop
        qop_match = re.search(r'qop="([^"]*)"', header)
        if qop_match:
            result['qop'] = qop_match.group(1)
        # Extrai opaque (se existir)
        opaque_match = re.search(r'opaque="([^"]*)"', header)
        if opaque_match:
            result['opaque'] = opaque_match.group(1)
        # Extrai algorithm (se existir)
        algo_match = re.search(r'algorithm=([^,\s]+)', header)
        if algo_match:
            result['algorithm'] = algo_match.group(1).strip('"')
        return result
    
    def create_digest_auth(username: str, password: str, realm: str, nonce: str, 
                           uri: str, method: str = "DESCRIBE", qop: str = None, 
                           opaque: str = None, nc_val: str = "00000001") -> str:
        """Cria header de autenticação Digest (RFC 2617) com suporte a qop=auth"""
        ha1 = md5_hash(f"{username}:{realm}:{password}")
        ha2 = md5_hash(f"{method}:{uri}")
        
        debug_info.append(f"HA1 input: {username}:{realm}:{password}")
        debug_info.append(f"HA1: {ha1}")
        debug_info.append(f"HA2 input: {method}:{uri}")
        debug_info.append(f"HA2: {ha2}")
        
        if qop and 'auth' in qop:
            # Com qop=auth, precisa de nc e cnonce
            nc = nc_val
            cnonce = generate_cnonce()
            response_input = f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}"
            response = md5_hash(response_input)
            
            debug_info.append(f"Response input (qop=auth): {response_input}")
            debug_info.append(f"Response: {response}")
            
            auth_parts = [
                f'username="{username}"',
                f'realm="{realm}"',
                f'nonce="{nonce}"',
                f'uri="{uri}"',
                f'qop=auth',
                f'nc={nc}',
                f'cnonce="{cnonce}"',
                f'response="{response}"',
            ]
            if opaque:
                auth_parts.append(f'opaque="{opaque}"')
            
            return 'Digest ' + ', '.join(auth_parts)
        else:
            # Sem qop (RFC 2069 estilo antigo)
            response_input = f"{ha1}:{nonce}:{ha2}"
            response = md5_hash(response_input)
            
            debug_info.append(f"Response input (no qop): {response_input}")
            debug_info.append(f"Response: {response}")
            
            auth_parts = [
                f'username="{username}"',
                f'realm="{realm}"',
                f'nonce="{nonce}"',
                f'uri="{uri}"',
                f'response="{response}"',
            ]
            if opaque:
                auth_parts.append(f'opaque="{opaque}"')
            
            return 'Digest ' + ', '.join(auth_parts)
    
    # Parse URL
    pattern = r'rtsp://(?:([^:@]+):([^@]+)@)?([^:/]+):?(\d+)?(/.*)?'
    match = re.match(pattern, rtsp_url)
    
    if not match:
        return False, "URL RTSP inválida", {}
    
    user = match.group(1) or ''
    password = match.group(2) or ''
    host = match.group(3)
    port = int(match.group(4)) if match.group(4) else 554
    path = match.group(5) or '/'
    
    try:
        # Conecta via socket TCP
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        
        # Envia DESCRIBE request inicial (sem auth)
        cseq = 1
        # URI completa para o request RTSP
        full_uri = f"rtsp://{host}:{port}{path}"
        # URI para Digest Auth (apenas o path)
        digest_uri = path
        
        describe_req = f"DESCRIBE {full_uri} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: CameraScanner/1.0\r\nAccept: application/sdp\r\n\r\n"
        sock.send(describe_req.encode())
        
        response = sock.recv(4096).decode('utf-8', errors='ignore')
        
        # Analisa resposta
        if 'RTSP/1.0 200' in response:
            sock.close()
            return True, "Conexão RTSP bem-sucedida!", {"response": "200 OK", "requires_auth": False}
        
        elif 'RTSP/1.0 401' in response:
            # Requer autenticação - tenta com credenciais se fornecidas
            if user and password:
                # Verifica tipo de autenticação
                auth_type = "Basic"
                auth_params = {}
                
                debug_info.append(f"=== WWW-Authenticate Header ===")
                debug_info.append(response[:500])
                
                if 'Digest' in response:
                    auth_type = "Digest"
                    auth_params = parse_www_authenticate(response)
                    debug_info.append(f"Auth params: {auth_params}")
                
                # IMPORTANTE: Reutiliza a MESMA conexão socket!
                # Muitas câmeras geram um novo nonce por conexão
                cseq = 2
                
                if auth_type == "Digest" and auth_params.get('realm') and auth_params.get('nonce'):
                    # Usa Digest Auth com suporte a qop
                    auth_header = create_digest_auth(
                        user, password, 
                        auth_params['realm'], 
                        auth_params['nonce'],
                        digest_uri,
                        qop=auth_params.get('qop'),
                        opaque=auth_params.get('opaque')
                    )
                else:
                    # Usa Basic Auth
                    import base64
                    auth_string = base64.b64encode(f"{user}:{password}".encode()).decode()
                    auth_header = f"Basic {auth_string}"
                
                debug_info.append(f"=== Auth Header Enviado ===")
                debug_info.append(auth_header)
                
                # Envia na MESMA conexão
                auth_req = f"DESCRIBE {full_uri} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: CameraScanner/1.0\r\nAuthorization: {auth_header}\r\nAccept: application/sdp\r\n\r\n"
                sock.send(auth_req.encode())
                
                response2 = sock.recv(4096).decode('utf-8', errors='ignore')
                sock.close()
                
                debug_info.append(f"=== Resposta da câmera ===")
                debug_info.append(response2[:300])
                
                # Log de debug no console
                print("\n".join(debug_info))
                
                if 'RTSP/1.0 200' in response2:
                    return True, f"Autenticação {auth_type} OK!", {"response": "200 OK", "requires_auth": True, "auth_type": auth_type, "debug": debug_info}
                elif 'RTSP/1.0 401' in response2:
                    # Mostra debug na mensagem de erro
                    debug_summary = f"\nRealm: {auth_params.get('realm')}, Nonce: {auth_params.get('nonce', '')[:20]}..."
                    return False, f"Credenciais incorretas ({auth_type}){debug_summary}", {"response": "401 Unauthorized", "auth_type": auth_type, "debug": debug_info}
                else:
                    status_match = re.search(r'RTSP/1\.0 (\d+)', response2)
                    status = status_match.group(1) if status_match else 'Desconhecido'
                    return False, f"Erro: {status}", {"response": status, "debug": debug_info}
            else:
                return False, "Requer autenticação", {"response": "401 Unauthorized", "requires_auth": True}
        
        elif 'RTSP/1.0 404' in response:
            return False, "Stream não encontrado", {"response": "404 Not Found"}
        
        elif 'RTSP/1.0 403' in response:
            return False, "Acesso negado", {"response": "403 Forbidden"}
        
        else:
            status_match = re.search(r'RTSP/1\.0 (\d+)', response)
            status = status_match.group(1) if status_match else 'Desconhecido'
            return False, f"Resposta: {status}", {"response": status}
        
    except socket.timeout:
        return False, "Timeout na conexão", {"error": "timeout"}
    except ConnectionRefusedError:
        return False, "Conexão recusada", {"error": "connection_refused"}
    except Exception as e:
        return False, f"Erro: {str(e)}", {"error": str(e)}



class Theme:
    """Cores e estilos baseados na plataforma web"""
    # Backgrounds
    BG_DARK = '#0a0a12'
    BG_PRIMARY = '#0f0f1a'
    BG_SECONDARY = '#161625'
    BG_CARD = '#1a1a2e'
    BG_ELEVATED = '#1e1e32'
    BG_INPUT = '#12121f'
    
    # Foreground
    FG_PRIMARY = '#f1f5f9'
    FG_SECONDARY = '#94a3b8'
    FG_MUTED = '#64748b'
    FG_DARK = '#475569'
    
    # Accent colors
    PRIMARY = '#0ea5e9'  # Cyan/Blue
    PRIMARY_HOVER = '#38bdf8'
    PRIMARY_DARK = '#0284c7'
    
    SUCCESS = '#22c55e'
    SUCCESS_BG = '#1a3a2a'  # Verde escuro sem transparência
    
    WARNING = '#f59e0b'
    WARNING_BG = '#3a2a1a'  # Laranja escuro sem transparência
    
    ERROR = '#ef4444'
    ERROR_BG = '#3a1a1a'  # Vermelho escuro sem transparência
    
    INFO_BG = '#1a2a3a'  # Azul escuro sem transparência
    
    # Borders
    BORDER = '#2a2a42'
    BORDER_LIGHT = '#3a3a52'
    
    # Font
    FONT_FAMILY = 'Segoe UI'
    FONT_MONO = 'Consolas'


class SupabaseClient:
    """Cliente simples para Supabase usando apenas urllib"""
    
    def __init__(self, on_ffmpeg_progress: Optional[Callable] = None):
        self.url = SUPABASE_URL
        self.anon_key = SUPABASE_ANON_KEY
        self.access_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.user_email: Optional[str] = None
        self.device_token: Optional[str] = None  # Device token para cloud agent
        self.on_ffmpeg_progress = on_ffmpeg_progress
        
        # Informações do cliente para heartbeat
        import platform
        self.hostname = platform.node()
        self.os_info = f"{platform.system()} {platform.release()}"
        self.local_ip = self._get_local_ip()
        # Client ID estável baseado em hostname + IP (não mais UUID aleatório)
        self.client_id = f"{self.hostname}-{self.local_ip}"
        self.ffmpeg_installed = False  # Será verificado depois
        self.ffmpeg_path = None
        self.active_streams = 0
        
        # Configurações do servidor de streaming (buscadas na inicialização)
        self.streaming_server_url = "https://ivms-v1-production.up.railway.app"
        self.rtmp_ingest_url = "rtmp://hopper.proxy.rlwy.net:46960/live"
        
        # Heartbeat thread
        self._heartbeat_thread = None
        self._heartbeat_running = False
        
        # Cloud command polling thread
        self._command_polling_thread = None
        self._command_polling_running = False
        
        # ONVIF Events Manager
        self._onvif_manager: Optional['OnvifEventsManager'] = None
        self._onvif_cameras: Dict[str, Dict] = {}  # IP -> {username, password, name, camera_id}
    
    def check_and_install_ffmpeg(self, progress_callback: Optional[Callable] = None) -> bool:
        """Verifica e instala FFmpeg se necessário, com callbacks de progresso"""
        import shutil
        import subprocess
        
        callback = progress_callback or self.on_ffmpeg_progress
        
        def report(msg: str, pct: int = 0, status: str = "checking"):
            if callback:
                callback(msg, pct, status)
            logger.info(f"FFmpeg: {msg}")
        
        report("Verificando FFmpeg...", 10, "checking")
        
        # Primeiro tenta via PATH
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            report(f"FFmpeg encontrado: {ffmpeg_path}", 100, "success")
            self.ffmpeg_installed = True
            self.ffmpeg_path = ffmpeg_path
            return True
        
        report("Verificando caminhos comuns...", 20, "checking")
        
        # Caminhos comuns
        common_paths = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            os.path.expanduser("~/.local/bin/ffmpeg"),
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/opt/homebrew/bin/ffmpeg",
        ]
        
        for path in common_paths:
            if os.path.isfile(path):
                try:
                    result = subprocess.run([path, "-version"], capture_output=True, timeout=5)
                    if result.returncode == 0:
                        report(f"FFmpeg encontrado: {path}", 100, "success")
                        self.ffmpeg_installed = True
                        self.ffmpeg_path = path
                        return True
                except:
                    pass
        
        report("FFmpeg não encontrado. Iniciando instalação...", 30, "installing")
        
        # Tenta instalar automaticamente
        try:
            from ffmpeg_installer import FFmpegInstaller
            
            def installer_progress(msg, pct):
                # Mapeia 0-100 do installer para 30-95 da nossa barra
                mapped_pct = 30 + int(pct * 0.65)
                report(msg, mapped_pct, "installing")
            
            installer = FFmpegInstaller(progress_callback=installer_progress)
            
            # Primeiro verifica se já está disponível
            available, path, version = installer.is_ffmpeg_available()
            if available:
                report(f"FFmpeg disponível: {version}", 100, "success")
                self.ffmpeg_installed = True
                self.ffmpeg_path = path
                return True
            
            # Se não está disponível, instala
            success, path = installer.install_ffmpeg()
            if success and path:
                report(f"FFmpeg instalado com sucesso!", 100, "success")
                self.ffmpeg_installed = True
                self.ffmpeg_path = path
                return True
            else:
                report("Falha na instalação do FFmpeg", 100, "error")
                self.ffmpeg_installed = False
                return False
                
        except ImportError:
            report("Módulo de instalação não disponível", 100, "error")
            self.ffmpeg_installed = False
            return False
        except Exception as e:
            report(f"Erro: {str(e)}", 100, "error")
            self.ffmpeg_installed = False
            return False
    
    def _get_local_ip(self) -> str:
        """Obtém IP local da máquina"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
    
    def _request(self, endpoint: str, method: str = "GET", data: Optional[Dict] = None, 
                 use_auth: bool = True, prefer_header: str = None) -> Dict:
        """Faz requisição HTTP para Supabase"""
        url = f"{self.url}{endpoint}"
        
        headers = {
            "apikey": self.anon_key,
            "Content-Type": "application/json",
        }
        
        if prefer_header:
            headers["Prefer"] = prefer_header
        
        if use_auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        else:
            headers["Authorization"] = f"Bearer {self.anon_key}"
        
        body = json.dumps(data).encode('utf-8') if data else None
        
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
                response_text = response.read().decode('utf-8')
                if response_text:
                    return json.loads(response_text)
                return {}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            logger.error(f"HTTP Error {e.code}: {error_body}")
            raise Exception(f"Erro: {json.loads(error_body).get('message', error_body)}")
        except Exception as e:
            logger.error(f"Request error: {e}")
            raise
    
    def _send_heartbeat(self) -> bool:
        """Envia heartbeat para o Supabase"""
        if not self.is_logged_in():
            return False
        
        try:
            self._request(
                "/functions/v1/bridge-heartbeat",
                method="POST",
                data={
                    "client_id": self.client_id,
                    "local_ip": self.local_ip,
                    "hostname": self.hostname,
                    "os_info": self.os_info,
                    "ffmpeg_installed": self.ffmpeg_installed,
                    "active_streams": self.active_streams,
                    "network_range": f"{'.'.join(self.local_ip.split('.')[:3])}.0/24"
                }
            )
            logger.debug("Heartbeat enviado com sucesso")
            return True
        except Exception as e:
            logger.error(f"Erro ao enviar heartbeat: {e}")
            return False
    
    def _send_disconnect(self):
        """Envia notificação de desconexão"""
        if not self.is_logged_in():
            return
        
        try:
            self._request(
                "/functions/v1/bridge-disconnect",
                method="POST",
                data={"client_id": self.client_id}
            )
            logger.info("Desconexão notificada")
        except Exception as e:
            logger.error(f"Erro ao notificar desconexão: {e}")
    
    def _start_heartbeat(self, interval: int = 10):
        """Inicia thread de heartbeat periódico"""
        if self._heartbeat_running:
            return
        
        self._heartbeat_running = True
        
        def heartbeat_loop():
            import time
            # Envia heartbeat inicial imediatamente
            self._send_heartbeat()
            
            while self._heartbeat_running:
                time.sleep(interval)
                if self._heartbeat_running and self.is_logged_in():
                    self._send_heartbeat()
        
        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        logger.info(f"✓ Heartbeat iniciado (intervalo: {interval}s)")
    
    def _stop_heartbeat(self):
        """Para thread de heartbeat"""
        self._heartbeat_running = False
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
            self._heartbeat_thread = None
        logger.info("Heartbeat parado")
    
    def _start_command_polling(self, interval: int = 5):
        """Inicia polling de comandos do cloud"""
        if self._command_polling_running:
            return
        
        self._command_polling_running = True
        
        def polling_loop():
            import time
            while self._command_polling_running and self.is_logged_in():
                try:
                    self._poll_and_process_commands()
                except Exception as e:
                    logger.error(f"Erro no polling de comandos: {e}")
                time.sleep(interval)
        
        self._command_polling_thread = threading.Thread(target=polling_loop, daemon=True)
        self._command_polling_thread.start()
        logger.info(f"✓ Command polling iniciado (intervalo: {interval}s)")
    
    def _stop_command_polling(self):
        """Para polling de comandos"""
        self._command_polling_running = False
        if self._command_polling_thread:
            self._command_polling_thread.join(timeout=2)
            self._command_polling_thread = None
        logger.info("Command polling parado")
    
    def _poll_and_process_commands(self):
        """Busca e processa comandos pendentes do cloud"""
        if not self.is_logged_in():
            return
        
        try:
            # Busca comandos pendentes
            commands = self._request(
                f"/rest/v1/agent_commands?user_id=eq.{self.user_id}&status=eq.pending&select=*&order=created_at.asc&limit=10",
                method="GET"
            )
            
            if not commands:
                return
            
            for cmd in commands:
                self._process_cloud_command(cmd)
                
        except Exception as e:
            logger.debug(f"Erro ao buscar comandos: {e}")
    
    def _process_cloud_command(self, command: Dict):
        """Processa um comando recebido do cloud"""
        cmd_id = command.get("id")
        cmd_type = command.get("command_type")
        payload = command.get("payload", {})
        
        logger.info(f"📥 Comando recebido: {cmd_type}")
        
        try:
            # Marca como recebido
            self._request(
                f"/rest/v1/agent_commands?id=eq.{cmd_id}",
                method="PATCH",
                data={"status": "executing", "received_at": datetime.now().isoformat()}
            )
            
            result = None
            error_message = None
            
            if cmd_type == "test_rtsp":
                result = self._handle_test_rtsp_command(payload)
            elif cmd_type == "start_stream":
                result = self._handle_start_stream_command(payload)
            elif cmd_type == "stop_stream":
                result = self._handle_stop_stream_command(payload)
            elif cmd_type == "get_status":
                result = {
                    "hostname": self.hostname,
                    "local_ip": self.local_ip,
                    "ffmpeg_installed": self.ffmpeg_installed,
                    "active_streams": self.active_streams,
                    "onvif_available": ONVIF_AVAILABLE,
                }
            elif cmd_type == "test_onvif":
                result = self._handle_test_onvif_command(payload)
            elif cmd_type == "start_onvif_events":
                result = self._handle_start_onvif_events_command(payload)
            elif cmd_type == "stop_onvif_events":
                result = self._handle_stop_onvif_events_command(payload)
            elif cmd_type == "get_onvif_status":
                result = self._handle_get_onvif_status_command()
            else:
                error_message = f"Comando desconhecido: {cmd_type}"
            
            # Atualiza resultado
            update_data = {
                "status": "completed" if not error_message else "failed",
                "completed_at": datetime.now().isoformat()
            }
            
            if result:
                update_data["result"] = result
            if error_message:
                update_data["error_message"] = error_message
            
            self._request(
                f"/rest/v1/agent_commands?id=eq.{cmd_id}",
                method="PATCH",
                data=update_data
            )
            
            logger.info(f"✓ Comando {cmd_type} processado")
            
        except Exception as e:
            logger.error(f"Erro ao processar comando: {e}")
            try:
                self._request(
                    f"/rest/v1/agent_commands?id=eq.{cmd_id}",
                    method="PATCH",
                    data={
                        "status": "failed",
                        "error_message": str(e),
                        "completed_at": datetime.now().isoformat()
                    }
                )
            except:
                pass
    
    def _handle_test_rtsp_command(self, payload: Dict) -> Dict:
        """Processa comando de teste RTSP"""
        rtsp_url = payload.get("rtsp_url")
        
        if not rtsp_url:
            return {"success": False, "error": "rtsp_url é obrigatório"}
        
        import time
        start_time = time.time()
        
        try:
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
                }
            else:
                return {
                    "success": False,
                    "error": message,
                    "rtsp_url": rtsp_url,
                    "requires_auth": details.get("requires_auth", False),
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "rtsp_url": rtsp_url,
            }
    
    def _handle_start_stream_command(self, payload: Dict) -> Dict:
        """Processa comando de iniciar stream RTSP → RTMP"""
        stream_key = payload.get("stream_key")
        rtsp_url = payload.get("rtsp_url")
        camera_name = payload.get("camera_name", "")
        
        if not stream_key or not rtsp_url:
            return {"success": False, "error": "stream_key e rtsp_url são obrigatórios"}
        
        if not self.ffmpeg_installed or not self.ffmpeg_path:
            return {"success": False, "error": "FFmpeg não disponível"}
        
        # Verifica se stream já existe
        if hasattr(self, '_stream_processes') and stream_key in self._stream_processes:
            process = self._stream_processes[stream_key]
            if process.poll() is None:  # Processo ainda rodando
                logger.info(f"⚠️ Stream {stream_key} já está rodando")
                return {"success": True, "stream_key": stream_key, "already_running": True}
        
        # URL do servidor RTMP (Railway) - usa a config do agente
        rtmp_url = self.rtmp_ingest_url
        rtmp_output = f"{rtmp_url}/{stream_key}"
        
        # URL do servidor HLS
        hls_url = f"{self.streaming_server_url}/hls/{stream_key}.m3u8"
        
        logger.info(f"🎬 Iniciando stream: {stream_key}")
        logger.info(f"   RTSP: {rtsp_url}")
        logger.info(f"   RTMP: {rtmp_output}")
        
        try:
            import subprocess
            import sys
            
            # Comando FFmpeg otimizado para streaming
            # Transcodifica HEVC para H.264 (FLV/RTMP não suporta HEVC)
            cmd = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel", "info",
                "-rtsp_transport", "tcp",
                "-timeout", "10000000",  # 10s timeout
                "-i", rtsp_url,
                "-c:v", "libx264",  # Transcodifica para H.264
                "-preset", "ultrafast",  # Máxima velocidade
                "-tune", "zerolatency",  # Mínima latência
                "-profile:v", "baseline",  # Perfil mais compatível
                "-b:v", "2M",  # Bitrate de 2 Mbps
                "-maxrate", "2M",
                "-bufsize", "4M",
                "-g", "60",  # Keyframe a cada 2 segundos (30fps)
                "-an",  # Remove áudio
                "-f", "flv",
                "-flvflags", "no_duration_filesize",
                rtmp_output,
            ]
            
            logger.info(f"   CMD: {' '.join(cmd)}")
            
            # Configuração de criação de processo
            startupinfo = None
            creationflags = 0
            
            if sys.platform == 'win32':
                # No Windows, evita criar janela do console
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE
                creationflags = subprocess.CREATE_NO_WINDOW
            
            # Inicia processo FFmpeg
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            
            # Monitorar stderr em thread separada para ver logs do FFmpeg
            def monitor_stderr(proc, key):
                try:
                    for line in iter(proc.stderr.readline, b''):
                        if not line:
                            break
                        line_str = line.decode('utf-8', errors='ignore').strip()
                        if line_str:
                            logger.info(f"[FFmpeg {key}] {line_str}")
                except:
                    pass
            
            stderr_thread = threading.Thread(target=monitor_stderr, args=(process, stream_key), daemon=True)
            stderr_thread.start()
            
            # Aguarda mais tempo para verificar se iniciou (5s em vez de 2s)
            import time
            time.sleep(5)
            
            if process.poll() is not None:
                # Processo terminou (erro)
                exit_code = process.poll()
                logger.error(f"❌ Stream falhou com exit code: {exit_code}")
                return {"success": False, "error": f"FFmpeg terminou com código {exit_code}", "stream_key": stream_key}
            
            # Stream iniciado com sucesso
            if not hasattr(self, '_stream_processes'):
                self._stream_processes = {}
            self._stream_processes[stream_key] = process
            
            self.active_streams = len(self._stream_processes)
            
            logger.info(f"✅ Stream {stream_key} iniciado com sucesso (PID: {process.pid})")
            
            return {
                "success": True,
                "stream_key": stream_key,
                "rtmp_url": rtmp_output,
                "hls_url": hls_url
            }
            
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar stream: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {"success": False, "error": str(e), "stream_key": stream_key}
    
    def _handle_stop_stream_command(self, payload: Dict) -> Dict:
        """Processa comando de parar stream"""
        stream_key = payload.get("stream_key")
        
        if not stream_key:
            return {"success": False, "error": "stream_key é obrigatório"}
        
        if not hasattr(self, '_stream_processes'):
            self._stream_processes = {}
        
        if stream_key not in self._stream_processes:
            return {"success": False, "error": "Stream não encontrado"}
        
        try:
            process = self._stream_processes[stream_key]
            
            # Envia SIGTERM
            process.terminate()
            
            try:
                process.wait(timeout=5)
            except:
                process.kill()
            
            del self._stream_processes[stream_key]
            self.active_streams = len(self._stream_processes)
            
            logger.info(f"🛑 Stream {stream_key} parado")
            
            return {"success": True, "stream_key": stream_key}
            
        except Exception as e:
            logger.error(f"Erro ao parar stream: {e}")
            return {"success": False, "error": str(e)}
    
    def _parse_ffmpeg_error(self, stderr: str) -> str:
        """Extrai mensagem de erro amigável do stderr do FFmpeg"""
        stderr_lower = stderr.lower()
        
        if "connection refused" in stderr_lower:
            return "Conexão recusada - câmera offline ou IP incorreto"
        elif "connection timed out" in stderr_lower or "timeout" in stderr_lower:
            return "Timeout - câmera não respondeu"
        elif "401 unauthorized" in stderr_lower or "authentication" in stderr_lower:
            return "Autenticação falhou - usuário/senha incorretos"
        elif "404" in stderr_lower or "not found" in stderr_lower:
            return "Stream não encontrado - verifique a URL RTSP"
        elif "invalid data" in stderr_lower:
            return "Dados inválidos - formato não suportado"
        elif "no route to host" in stderr_lower:
            return "Câmera inacessível - verifique a rede"
        else:
            # Retorna últimas linhas relevantes
            lines = [l for l in stderr.split('\n') if l.strip() and 'error' in l.lower()]
            if lines:
                return lines[-1][:200]
            return "Erro ao conectar na câmera"
    
    def _handle_test_onvif_command(self, payload: Dict) -> Dict:
        """Testa conectividade ONVIF e retorna capabilities da câmera"""
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
        
        import time
        start_time = time.time()
        
        try:
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
        if not ONVIF_AVAILABLE:
            return {}
            
        try:
            import requests
            import xml.etree.ElementTree as ET
            
            wsse_header = OnvifAuth.create_wsse_header(username, password)
            
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
                ns = {'tds': 'http://www.onvif.org/ver10/device/wsdl'}
                info = root.find('.//tds:GetDeviceInformationResponse', ns)
                
                if info is not None:
                    return {
                        "manufacturer": info.findtext('tds:Manufacturer', '', ns),
                        "model": info.findtext('tds:Model', '', ns),
                        "firmware_version": info.findtext('tds:FirmwareVersion', '', ns),
                        "serial_number": info.findtext('tds:SerialNumber', '', ns),
                    }
        except Exception as e:
            logger.debug(f"Não foi possível obter info do dispositivo: {e}")
        
        return {}
    
    def _handle_start_onvif_events_command(self, payload: Dict) -> Dict:
        """Inicia escuta de eventos ONVIF para uma câmera específica"""
        camera_ip = payload.get("camera_ip") or payload.get("ip")
        camera_port = payload.get("camera_port") or payload.get("port", 80)
        username = payload.get("username", "admin")
        password = payload.get("password", "")
        camera_name = payload.get("camera_name", "")
        camera_id = payload.get("camera_id", "")
        
        logger.info(f"🔔 ONVIF Events - IP: {camera_ip}, Port: {camera_port}, User: {username}, Pass: {'*' * len(password) if password else '(vazio)'}")
        
        if not camera_ip:
            return {"success": False, "error": "camera_ip ou ip é obrigatório"}
        
        if not ONVIF_AVAILABLE:
            return {
                "success": False, 
                "error": "Módulo ONVIF não disponível no agente",
                "onvif_available": False,
            }
        
        # Verifica se já está escutando
        if camera_ip in self._onvif_cameras:
            return {
                "success": True,
                "camera_ip": camera_ip,
                "already_listening": True,
                "message": f"Já escutando eventos de {camera_ip}",
            }
        
        try:
            # Inicializa ONVIF Manager se necessário
            if self._onvif_manager is None:
                self._onvif_manager = OnvifEventsManager(
                    event_callback=lambda event: self._on_onvif_event(event)
                )
            
            # Adiciona câmera ao manager
            success = self._onvif_manager.add_camera(
                camera_ip=camera_ip,
                camera_port=camera_port,
                username=username,
                password=password,
                camera_name=camera_name or camera_ip,
            )
            
            if success:
                self._onvif_cameras[camera_ip] = {
                    "username": username,
                    "password": password,
                    "camera_name": camera_name,
                    "camera_id": camera_id,
                    "camera_port": camera_port,
                }
                logger.info(f"📡 Escuta ONVIF iniciada para {camera_ip}")
                return {
                    "success": True,
                    "camera_ip": camera_ip,
                    "message": f"Escuta de eventos ONVIF iniciada para {camera_name or camera_ip}",
                }
            else:
                return {
                    "success": False,
                    "camera_ip": camera_ip,
                    "error": "Falha ao iniciar escuta - câmera pode não suportar eventos ONVIF",
                }
                
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar escuta ONVIF: {e}")
            return {
                "success": False,
                "camera_ip": camera_ip,
                "error": str(e),
            }
    
    def _handle_stop_onvif_events_command(self, payload: Dict) -> Dict:
        """Para escuta de eventos ONVIF de uma câmera"""
        camera_ip = payload.get("camera_ip") or payload.get("ip")
        
        if not camera_ip:
            return {"success": False, "error": "camera_ip ou ip é obrigatório"}
        
        if camera_ip not in self._onvif_cameras:
            return {
                "success": False,
                "camera_ip": camera_ip,
                "error": "Câmera não está sendo monitorada",
            }
        
        try:
            if self._onvif_manager:
                self._onvif_manager.remove_camera(camera_ip)
            
            del self._onvif_cameras[camera_ip]
            
            logger.info(f"🛑 Escuta ONVIF parada para {camera_ip}")
            return {
                "success": True,
                "camera_ip": camera_ip,
                "message": f"Escuta de eventos parada para {camera_ip}",
            }
            
        except Exception as e:
            logger.error(f"❌ Erro ao parar escuta ONVIF: {e}")
            return {
                "success": False,
                "camera_ip": camera_ip,
                "error": str(e),
            }
    
    def _handle_get_onvif_status_command(self) -> Dict:
        """Retorna status de todas as escutas ONVIF ativas"""
        cameras = []
        
        for ip, info in self._onvif_cameras.items():
            cameras.append({
                "camera_ip": ip,
                "camera_name": info.get("camera_name", ip),
                "camera_id": info.get("camera_id", ""),
                "listening": True,
            })
        
        return {
            "success": True,
            "onvif_available": ONVIF_AVAILABLE,
            "active_listeners": len(cameras),
            "cameras": cameras,
        }
    
    def _on_onvif_event(self, event: 'OnvifEvent'):
        """Callback para eventos ONVIF recebidos - envia para a edge function"""
        logger.info(f"🔔 _on_onvif_event chamado: {event.event_type} de {event.camera_ip}")
        
        if not self.is_logged_in():
            logger.warning("⚠ Não logado, ignorando evento ONVIF")
            return
        
        if not self.device_token:
            logger.warning(f"⚠ Device token não disponível para enviar evento ONVIF (user_id: {self.user_id})")
            return
        
        logger.info(f"📤 Enviando evento para edge function: {event.event_type}")
        
        try:
            # Busca camera_id se disponível
            camera_info = self._onvif_cameras.get(event.camera_ip, {})
            camera_id = camera_info.get("camera_id")
            
            # Prepara o evento para a edge function
            event_data = {
                "event_type": event.event_type,
                "camera_ip": event.camera_ip,
                "camera_name": event.camera_name,
                "severity": self._map_event_severity(event.event_type),
                "message": f"{event.event_type} detectado em {event.camera_name}",
                "metadata": {
                    "topic": event.topic,
                    "source": event.source,
                    "data": event.data,
                    "timestamp": event.timestamp.isoformat(),
                },
            }
            
            if camera_id:
                event_data["camera_id"] = camera_id
            
            # Envia para a edge function com device_token
            self._send_camera_event(event_data)
            
            logger.info(f"📤 Evento ONVIF enviado: {event.event_type} de {event.camera_name}")
            
        except Exception as e:
            logger.error(f"❌ Erro ao enviar evento ONVIF: {e}")
    
    def _send_camera_event(self, event_data: Dict):
        """Envia evento de câmera para a edge function usando device_token"""
        url = f"{self.url}/functions/v1/receive-camera-event"
        
        headers = {
            "apikey": self.anon_key,
            "Content-Type": "application/json",
            "x-device-token": self.device_token,
        }
        
        body = json.dumps(event_data).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        
        try:
            ctx = ssl.create_default_context()
            logger.info(f"🌐 POST {url}")
            with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
                response_text = response.read().decode('utf-8')
                result = json.loads(response_text) if response_text else {}
                logger.info(f"✅ Evento enviado com sucesso: {result}")
                return result
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            logger.error(f"❌ Erro ao enviar evento: HTTP {e.code} - {error_body}")
            raise
        except Exception as e:
            logger.error(f"❌ Erro ao enviar evento: {e}")
            raise
    
    def _map_event_severity(self, event_type: str) -> str:
        """Mapeia tipo de evento para severidade"""
        critical_events = ['tampering', 'video_loss', 'alarm_input']
        warning_events = ['intrusion_detection', 'line_crossing']
        
        if event_type in critical_events:
            return 'critical'
        elif event_type in warning_events:
            return 'warning'
        return 'info'
    
    def update_status(self, ffmpeg_installed: bool = None, active_streams: int = None):
        """Atualiza informações de status e envia heartbeat"""
        if ffmpeg_installed is not None:
            self.ffmpeg_installed = ffmpeg_installed
        if active_streams is not None:
            self.active_streams = active_streams
        
        # Envia heartbeat imediatamente após atualização
        if self.is_logged_in():
            self._send_heartbeat()
    
    def login(self, email: str, password: str) -> bool:
        """Faz login com email e senha"""
        try:
            result = self._request(
                "/auth/v1/token?grant_type=password",
                method="POST",
                data={"email": email, "password": password},
                use_auth=False
            )
            
            self.access_token = result.get("access_token")
            self.user_id = result.get("user", {}).get("id")
            self.user_email = result.get("user", {}).get("email")
            
            logger.info(f"Login bem-sucedido: {self.user_email}")
            
            # Obtém ou cria device token automaticamente
            self._ensure_device_token()
            
            # Busca configurações do servidor de streaming
            self._fetch_server_config()
            
            # Inicia heartbeat após login
            self._start_heartbeat()
            
            # Inicia polling de comandos do cloud
            self._start_command_polling()
            
            return True
            
        except Exception as e:
            logger.error(f"Erro no login: {e}")
            raise
    
    def _ensure_device_token(self):
        """Obtém device token existente ou cria um novo automaticamente"""
        if not self.is_logged_in():
            return
        
        try:
            # Busca token existente para este usuário
            tokens = self._request(
                f"/rest/v1/agent_device_tokens?user_id=eq.{self.user_id}&is_active=eq.true&select=*&order=created_at.desc&limit=1",
                method="GET"
            )
            
            if tokens and len(tokens) > 0:
                self.device_token = tokens[0].get('device_token')
                logger.info(f"✓ Device token recuperado: {self.device_token[:8]}...")
            else:
                # Cria novo token
                self._create_device_token()
        except Exception as e:
            logger.warning(f"Erro ao verificar device token: {e}")
            # Tenta criar novo se falhou
            try:
                self._create_device_token()
            except:
                pass
    
    def _create_device_token(self):
        """Cria um novo device token"""
        if not self.is_logged_in():
            return
        
        try:
            # Gera token via função do banco
            token_result = self._request(
                "/rest/v1/rpc/generate_device_token",
                method="POST",
                data={}
            )
            
            new_token = token_result if isinstance(token_result, str) else token_result.get('generate_device_token', '')
            
            if not new_token:
                # Fallback: gera token localmente
                import secrets
                new_token = secrets.token_urlsafe(32)
            
            # Salva no banco
            self._request(
                "/rest/v1/agent_device_tokens",
                method="POST",
                data={
                    "user_id": self.user_id,
                    "agent_name": f"Scanner - {self.hostname}",
                    "device_token": new_token,
                    "is_active": True
                }
            )
            
            self.device_token = new_token
            logger.info(f"✓ Novo device token criado: {new_token[:8]}...")
            
        except Exception as e:
            logger.error(f"Erro ao criar device token: {e}")
    
    def _fetch_server_config(self):
        """Busca configurações do servidor de streaming do banco de dados"""
        if not self.is_logged_in():
            return
        
        try:
            configs = self._request(
                "/rest/v1/system_config?select=key,value",
                method="GET"
            )
            
            for config in configs:
                key = config.get('key')
                value = config.get('value')
                
                if key == 'streaming_server_url' and value:
                    self.streaming_server_url = value
                    logger.info(f"✓ Streaming server: {value}")
                elif key == 'rtmp_ingest_url' and value:
                    self.rtmp_ingest_url = value
                    logger.info(f"✓ RTMP ingest: {value}")
                    
        except Exception as e:
            logger.warning(f"Erro ao buscar configurações do servidor: {e}")
            logger.info(f"   Usando defaults: RTMP={self.rtmp_ingest_url}")
    
    def get_device_token(self) -> Optional[str]:
        """Retorna o device token atual"""
        return getattr(self, 'device_token', None)
    
    def logout(self):
        """Faz logout"""
        self._stop_command_polling()
        self._stop_heartbeat()
        self._send_disconnect()
        
        self.access_token = None
        self.user_id = None
        self.user_email = None
        # NÃO limpa device_token - ele persiste entre sessões
    
    def is_logged_in(self) -> bool:
        """Verifica se está logado"""
        return self.access_token is not None
    
    def save_discovered_device(self, device: Dict, network_range: str) -> Dict:
        """Salva dispositivo descoberto no banco de dados (upsert por user_id + ip)"""
        if not self.is_logged_in():
            raise Exception("Não autenticado")
        
        data = {
            "user_id": self.user_id,
            "ip": device['ip'],
            "brand": device.get('brand', 'generic'),
            "brand_name": device.get('brand_name', 'Câmera Genérica'),
            "open_ports": device.get('open_ports', []),
            "rtsp_templates": device.get('rtsp_templates', []),
            "default_users": device.get('default_users', []),
            "default_passwords": device.get('default_passwords', []),
            "suggested_url": device.get('suggested_url', ''),
            "confidence": device.get('confidence', 0.5),
            "network_range": network_range,
            "discovered_at": datetime.now().isoformat()
        }
        
        # Usa return=representation para receber o registro criado/atualizado com ID
        result = self._request(
            "/rest/v1/discovered_devices?on_conflict=user_id,ip",
            method="POST",
            data=data,
            prefer_header="resolution=merge-duplicates,return=representation"
        )
        
        # Retorna o primeiro item se for lista, ou o objeto diretamente
        if isinstance(result, list) and len(result) > 0:
            return result[0]
        return result
    
    def clear_discovered_devices(self) -> None:
        """Limpa dispositivos descobertos do usuário antes de novo scan"""
        if not self.is_logged_in():
            return
        
        try:
            self._request(
                f"/rest/v1/discovered_devices?user_id=eq.{self.user_id}",
                method="DELETE"
            )
        except Exception as e:
            logger.error(f"Erro ao limpar dispositivos: {e}")
    
    def get_discovered_devices(self) -> List[Dict]:
        """Obtém dispositivos descobertos do usuário"""
        if not self.is_logged_in():
            return []
        
        return self._request(f"/rest/v1/discovered_devices?user_id=eq.{self.user_id}&select=*")
    
    def save_camera(self, camera_data: Dict) -> Dict:
        """Salva uma câmera no banco de dados"""
        if not self.is_logged_in():
            raise Exception("Não autenticado")
        
        data = {
            "user_id": self.user_id,
            "name": camera_data.get("name", f"Câmera {camera_data['ip']}"),
            "stream_url": camera_data.get("stream_url", ""),
            "stream_type": "rtsp",
            "is_active": True,
        }
        
        return self._request(
            "/rest/v1/cameras",
            method="POST",
            data=data
        )
    
    def get_cameras(self) -> List[Dict]:
        """Obtém câmeras do usuário"""
        if not self.is_logged_in():
            return []
        
        return self._request(f"/rest/v1/cameras?user_id=eq.{self.user_id}&select=*")
    
    def update_device_credentials(self, device_id: str, username: str, password: str) -> Dict:
        """Atualiza credenciais de um dispositivo descoberto"""
        if not self.is_logged_in():
            raise Exception("Não autenticado")
        
        data = {
            "username": username,
            "password": password
        }
        
        return self._request(
            f"/rest/v1/discovered_devices?id=eq.{device_id}",
            method="PATCH",
            data=data
        )
    
    def update_device_rtsp_status(self, device_id: str, success: bool, message: str, url: str = None) -> Dict:
        """Atualiza status do teste RTSP de um dispositivo"""
        if not self.is_logged_in():
            raise Exception("Não autenticado")
        
        data = {
            "last_test_at": datetime.now().isoformat(),
            "last_test_success": success,
            "last_test_message": message,
            "rtsp_validated": success
        }
        
        if url:
            data["suggested_url"] = url
        
        return self._request(
            f"/rest/v1/discovered_devices?id=eq.{device_id}",
            method="PATCH",
            data=data
        )


class NetworkScanner:
    """Scanner de rede para descoberta de câmeras"""
    
    def __init__(self, progress_callback: Optional[Callable] = None, 
                 device_found_callback: Optional[Callable] = None,
                 supabase_client: Optional[SupabaseClient] = None):
        self.progress_callback = progress_callback
        self.device_found_callback = device_found_callback
        self.supabase = supabase_client
        self.found_devices: List[Dict] = []
        self.scanning = False
        self.cancel_requested = False
        
    def get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "192.168.1.1"
    
    def get_network_range(self) -> str:
        local_ip = self.get_local_ip()
        parts = local_ip.split('.')
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    
    def check_port(self, ip: str, port: int, timeout: float = 0.5) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, port))
            sock.close()
            return result == 0
        except:
            return False
    
    def detect_brand(self, ip: str, open_ports: List[int]) -> Dict:
        brand_info = {'brand': 'generic', 'brand_name': 'Câmera Genérica', 'confidence': 0.3}
        
        for port in [p for p in open_ports if p in [80, 8080, 443, 88]]:
            try:
                protocol = 'https' if port == 443 else 'http'
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                
                url = f"{protocol}://{ip}:{port}/"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                
                with urllib.request.urlopen(req, timeout=2, context=ctx) as response:
                    content = response.read(4096).decode('utf-8', errors='ignore').lower()
                    server = dict(response.headers).get('Server', '').lower()
                    
                    for brand_key, brand_data in CAMERA_BRANDS.items():
                        if brand_key == 'generic':
                            continue
                        for keyword in brand_data['detection_keywords']:
                            if keyword in content or keyword in server:
                                return {
                                    'brand': brand_key,
                                    'brand_name': brand_data['name'],
                                    'confidence': 0.9
                                }
            except:
                pass
        
        if 37777 in open_ports:
            return {'brand': 'intelbras', 'brand_name': 'Intelbras', 'confidence': 0.7}
        if 8000 in open_ports:
            return {'brand': 'hikvision', 'brand_name': 'Hikvision', 'confidence': 0.6}
        if 4520 in open_ports:
            return {'brand': 'hanwha', 'brand_name': 'Hanwha (Samsung)', 'confidence': 0.6}
        if 88 in open_ports:
            return {'brand': 'foscam', 'brand_name': 'Foscam', 'confidence': 0.5}
        
        return brand_info
    
    def scan_host(self, ip: str) -> Optional[Dict]:
        if self.cancel_requested:
            return None
            
        open_ports = [port for port in CAMERA_PORTS if self.check_port(ip, port)]
        
        if 554 in open_ports or any(p in open_ports for p in [37777, 8000, 4520]):
            brand_info = self.detect_brand(ip, open_ports)
            brand_data = CAMERA_BRANDS.get(brand_info['brand'], CAMERA_BRANDS['generic'])
            
            template = brand_data['rtsp_templates'][0] if brand_data['rtsp_templates'] else ''
            default_url = template.replace('{user}', 'admin').replace('{pass}', 'admin').replace('{ip}', ip)
            
            return {
                'ip': ip,
                'open_ports': open_ports,
                'brand': brand_info['brand'],
                'brand_name': brand_info['brand_name'],
                'confidence': brand_info['confidence'],
                'rtsp_templates': brand_data['rtsp_templates'],
                'default_users': brand_data['default_users'],
                'default_passwords': brand_data['default_passwords'],
                'suggested_url': default_url,
                'discovered_at': datetime.now().isoformat()
            }
        return None
    
    def scan_network(self, network_range: Optional[str] = None, max_workers: int = 50) -> List[Dict]:
        self.scanning = True
        self.cancel_requested = False
        self.found_devices = []
        
        if not network_range:
            network_range = self.get_network_range()
        
        if self.supabase and self.supabase.is_logged_in():
            try:
                self.supabase.clear_discovered_devices()
            except Exception as e:
                logger.error(f"Erro ao limpar dispositivos anteriores: {e}")
        
        try:
            network = ipaddress.ip_network(network_range, strict=False)
            hosts = list(network.hosts())
            total = len(hosts)
            
            # Callback inicial imediato
            if self.progress_callback:
                self.progress_callback({
                    'status': 'scanning', 
                    'progress': 0, 
                    'total': total, 
                    'scanned': 0,
                    'found': 0,
                    'current_ip': str(hosts[0]) if hosts else ''
                })
            
            scanned = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self.scan_host, str(ip)): str(ip) for ip in hosts}
                
                for future in concurrent.futures.as_completed(futures):
                    if self.cancel_requested:
                        break
                    scanned += 1
                    current_ip = futures[future]
                    
                    try:
                        device = future.result()
                        if device:
                            self.found_devices.append(device)
                            
                            if self.supabase and self.supabase.is_logged_in():
                                try:
                                    # Salva e recebe o registro com ID do banco
                                    saved_device = self.supabase.save_discovered_device(device, network_range)
                                    
                                    # Atualiza device local com o ID do banco
                                    if saved_device and saved_device.get('id'):
                                        device['id'] = saved_device['id']
                                    
                                    logger.info(f"✓ Dispositivo salvo: {device['ip']} ({device['brand_name']}) - ID: {device.get('id', 'N/A')}")
                                    
                                    if self.device_found_callback:
                                        self.device_found_callback(device)
                                except Exception as e:
                                    logger.error(f"Erro ao salvar {device['ip']}: {e}")
                    except:
                        pass
                    
                    # Atualiza progresso a cada 3 hosts para UI mais responsiva
                    if self.progress_callback and (scanned % 3 == 0 or scanned == total):
                        self.progress_callback({
                            'status': 'scanning',
                            'progress': int((scanned / total) * 100),
                            'total': total,
                            'scanned': scanned,
                            'found': len(self.found_devices),
                            'current_ip': current_ip
                        })
            
            if self.progress_callback:
                self.progress_callback({
                    'status': 'completed',
                    'progress': 100,
                    'total': total,
                    'scanned': total,
                    'found': len(self.found_devices)
                })
                
        except Exception as e:
            logger.error(f"Erro no scan: {e}")
        
        self.scanning = False
        return self.found_devices
    
    def cancel_scan(self):
        self.cancel_requested = True


def run_gui():
    """Interface gráfica moderna similar à plataforma web"""
    import tkinter as tk
    from tkinter import ttk, messagebox
    
    class ModernButton(tk.Canvas):
        """Botão moderno com hover effects"""
        def __init__(self, parent, text, command=None, variant='primary', width=140, height=38, **kwargs):
            super().__init__(parent, width=width, height=height, bg=Theme.BG_PRIMARY, 
                           highlightthickness=0, **kwargs)
            
            self.command = command
            self.variant = variant
            self.text = text
            self.width = width
            self.height = height
            self.enabled = True
            
            # Colors based on variant
            self.colors = {
                'primary': (Theme.PRIMARY, Theme.PRIMARY_HOVER, '#ffffff'),
                'secondary': (Theme.BG_CARD, Theme.BG_ELEVATED, Theme.FG_PRIMARY),
                'success': (Theme.SUCCESS, '#16a34a', '#ffffff'),
                'danger': (Theme.ERROR, '#dc2626', '#ffffff'),
                'ghost': (Theme.BG_PRIMARY, Theme.BG_CARD, Theme.FG_SECONDARY),
            }
            
            self.draw()
            
            self.bind('<Enter>', self.on_enter)
            self.bind('<Leave>', self.on_leave)
            self.bind('<Button-1>', self.on_click)
        
        def draw(self, hover=False):
            self.delete('all')
            colors = self.colors.get(self.variant, self.colors['primary'])
            bg = colors[1] if hover else colors[0]
            fg = colors[2]
            
            if not self.enabled:
                bg = Theme.BG_SECONDARY
                fg = Theme.FG_MUTED
            
            # Draw rounded rectangle
            self.create_rounded_rect(2, 2, self.width-2, self.height-2, 8, fill=bg, outline='')
            
            # Draw text
            self.create_text(self.width/2, self.height/2, text=self.text, 
                           fill=fg, font=(Theme.FONT_FAMILY, 10, 'bold'))
        
        def create_rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
            points = [
                x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
                x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
                x1, y2, x1, y2-radius, x1, y1+radius, x1, y1
            ]
            return self.create_polygon(points, smooth=True, **kwargs)
        
        def on_enter(self, e):
            if self.enabled:
                self.draw(hover=True)
        
        def on_leave(self, e):
            self.draw(hover=False)
        
        def on_click(self, e):
            if self.enabled and self.command:
                self.command()
        
        def set_enabled(self, enabled):
            self.enabled = enabled
            self.draw()
        
        def set_text(self, text):
            self.text = text
            self.draw()
    
    
    class Toast:
        """Toast notification similar ao web"""
        def __init__(self, parent):
            self.parent = parent
            self.toast_frame = None
            self.hide_after_id = None
        
        def show(self, message, variant='error', duration=4000):
            """Exibe um toast"""
            self.hide()  # Remove toast anterior
            
            colors = {
                'error': (Theme.ERROR, Theme.ERROR_BG, Theme.FG_PRIMARY),
                'success': (Theme.SUCCESS, Theme.SUCCESS_BG, Theme.FG_PRIMARY),
                'warning': (Theme.WARNING, Theme.WARNING_BG, Theme.FG_PRIMARY),
                'info': (Theme.PRIMARY, Theme.INFO_BG, Theme.FG_PRIMARY),
            }
            
            accent, bg, text_color = colors.get(variant, colors['error'])
            
            # Container do toast
            self.toast_frame = tk.Frame(self.parent, bg=bg,
                                       highlightbackground=accent,
                                       highlightthickness=1)
            
            # Posiciona no topo central
            self.toast_frame.place(relx=0.5, y=20, anchor='n')
            
            # Ícone
            icons = {'error': '✕', 'success': '✓', 'warning': '⚠', 'info': 'ℹ'}
            icon = icons.get(variant, '✕')
            
            content = tk.Frame(self.toast_frame, bg=bg)
            content.pack(padx=16, pady=12)
            
            tk.Label(content, text=icon,
                    font=(Theme.FONT_FAMILY, 12, 'bold'),
                    bg=bg, fg=accent).pack(side='left', padx=(0, 10))
            
            tk.Label(content, text=message,
                    font=(Theme.FONT_FAMILY, 10),
                    bg=bg, fg=Theme.FG_PRIMARY).pack(side='left')
            
            # Botão fechar
            close_btn = tk.Label(content, text="✕",
                               font=(Theme.FONT_FAMILY, 10),
                               bg=bg, fg=Theme.FG_MUTED, cursor='hand2')
            close_btn.pack(side='left', padx=(16, 0))
            close_btn.bind('<Button-1>', lambda e: self.hide())
            close_btn.bind('<Enter>', lambda e: close_btn.config(fg=Theme.FG_PRIMARY))
            close_btn.bind('<Leave>', lambda e: close_btn.config(fg=Theme.FG_MUTED))
            
            # Auto hide
            if duration:
                self.hide_after_id = self.parent.after(duration, self.hide)
        
        def hide(self):
            if self.hide_after_id:
                self.parent.after_cancel(self.hide_after_id)
                self.hide_after_id = None
            if self.toast_frame:
                self.toast_frame.destroy()
                self.toast_frame = None
    
    
    class ModernEntry(tk.Frame):
        """Campo de entrada moderno com ícone"""
        def __init__(self, parent, placeholder='', show=None, icon=None, **kwargs):
            super().__init__(parent, bg=Theme.BG_PRIMARY)
            
            self.placeholder = placeholder
            self.show_char = show
            self.icon = icon
            
            # Container com borda arredondada
            self.container = tk.Frame(self, bg=Theme.BG_INPUT, 
                                      highlightbackground=Theme.BORDER,
                                      highlightthickness=1)
            self.container.pack(fill='x', ipady=12, ipadx=14)
            
            # Inner frame para ícone e entry
            inner = tk.Frame(self.container, bg=Theme.BG_INPUT)
            inner.pack(fill='x', padx=2)
            
            # Ícone se existir
            if icon:
                tk.Label(inner, text=icon,
                        font=(Theme.FONT_FAMILY, 12),
                        bg=Theme.BG_INPUT, fg=Theme.FG_MUTED).pack(side='left', padx=(0, 8))
            
            # Entry
            self.entry = tk.Entry(inner, 
                                 font=(Theme.FONT_FAMILY, 11),
                                 bg=Theme.BG_INPUT, fg=Theme.FG_PRIMARY,
                                 insertbackground=Theme.PRIMARY,
                                 relief='flat', show=show or '',
                                 highlightthickness=0)
            self.entry.pack(side='left', fill='x', expand=True)
            
            # Placeholder
            if placeholder:
                self.entry.insert(0, placeholder)
                self.entry.config(fg=Theme.FG_MUTED)
            
            # Bind events
            self.entry.bind('<FocusIn>', self._on_focus_in)
            self.entry.bind('<FocusOut>', self._on_focus_out)
        
        def _on_focus_in(self, e):
            self.container.config(highlightbackground=Theme.PRIMARY)
            if self.entry.get() == self.placeholder:
                self.entry.delete(0, 'end')
                self.entry.config(fg=Theme.FG_PRIMARY)
                if self.show_char:
                    self.entry.config(show=self.show_char)
        
        def _on_focus_out(self, e):
            self.container.config(highlightbackground=Theme.BORDER)
            if not self.entry.get():
                self.entry.insert(0, self.placeholder)
                self.entry.config(fg=Theme.FG_MUTED)
                if self.show_char:
                    self.entry.config(show='')
        
        def get(self):
            val = self.entry.get()
            return '' if val == self.placeholder else val
        
        def bind(self, event, callback):
            self.entry.bind(event, callback)
        
        def focus(self):
            self.entry.focus_set()
    
    
    class CameraCard(tk.Frame):
        """Card melhorado para exibir câmera encontrada com credenciais e teste RTSP"""
        def __init__(self, parent, device, supabase_client=None, on_copy_url=None, on_credentials_saved=None, **kwargs):
            super().__init__(parent, bg=Theme.BG_CARD, **kwargs)
            
            self.device = device
            self.supabase = supabase_client
            self.on_copy_url = on_copy_url
            self.on_credentials_saved = on_credentials_saved
            self.configure(highlightbackground=Theme.BORDER, highlightthickness=1)
            
            # Main content
            content = tk.Frame(self, bg=Theme.BG_CARD)
            content.pack(fill='x', padx=20, pady=16)
            
            # === TOP ROW: IP + Brand + Confidence ===
            top_row = tk.Frame(content, bg=Theme.BG_CARD)
            top_row.pack(fill='x')
            
            # IP Address
            tk.Label(top_row, text=device['ip'], 
                    font=(Theme.FONT_MONO, 14, 'bold'),
                    bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY).pack(side='left')
            
            # Brand badge with color based on confidence
            confidence = device.get('confidence', 0.5)
            if confidence > 0.7:
                brand_bg = Theme.SUCCESS
                conf_text = "Alta"
                conf_icon = "🟢"
            elif confidence > 0.4:
                brand_bg = Theme.PRIMARY
                conf_text = "Média"
                conf_icon = "🟡"
            else:
                brand_bg = Theme.BG_ELEVATED
                conf_text = "Baixa"
                conf_icon = "🔴"
            
            brand_frame = tk.Frame(top_row, bg=brand_bg)
            brand_frame.pack(side='left', padx=(12, 0))
            tk.Label(brand_frame, text=device.get('brand_name', 'Genérica'),
                    font=(Theme.FONT_FAMILY, 9, 'bold'),
                    bg=brand_bg, fg='#ffffff',
                    padx=10, pady=3).pack()
            
            # Test status on the right
            self.status_frame = tk.Frame(top_row, bg=Theme.BG_CARD)
            self.status_frame.pack(side='right')
            
            # Mostra status do último teste se existir
            if device.get('rtsp_validated'):
                self.status_label = tk.Label(self.status_frame, text="✓ Validado",
                        font=(Theme.FONT_FAMILY, 9, 'bold'),
                        bg=Theme.BG_CARD, fg=Theme.SUCCESS)
            elif device.get('last_test_success') is False:
                self.status_label = tk.Label(self.status_frame, text="✗ Falhou",
                        font=(Theme.FONT_FAMILY, 9, 'bold'),
                        bg=Theme.BG_CARD, fg=Theme.ERROR)
            else:
                self.status_label = tk.Label(self.status_frame, text="○ Não testado",
                        font=(Theme.FONT_FAMILY, 9, 'bold'),
                        bg=Theme.BG_CARD, fg=Theme.FG_MUTED)
            self.status_label.pack()
            
            # === MIDDLE ROW: Details ===
            details_frame = tk.Frame(content, bg=Theme.BG_CARD)
            details_frame.pack(fill='x', pady=(12, 0))
            
            # Ports
            ports = device.get('open_ports', [])
            ports_str = ', '.join(map(str, ports[:6]))
            if len(ports) > 6:
                ports_str += f" (+{len(ports) - 6})"
            
            port_label = tk.Frame(details_frame, bg=Theme.BG_CARD)
            port_label.pack(side='left')
            tk.Label(port_label, text="Portas:",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
            tk.Label(port_label, text=ports_str,
                    font=(Theme.FONT_MONO, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_SECONDARY).pack(side='left', padx=(6, 0))
            
            # Confidence indicator
            conf_label = tk.Frame(details_frame, bg=Theme.BG_CARD)
            conf_label.pack(side='left', padx=(20, 0))
            tk.Label(conf_label, text=f"{conf_icon} Confiança: {conf_text}",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
            
            # === CREDENTIALS ROW ===
            creds_frame = tk.Frame(content, bg=Theme.BG_CARD)
            creds_frame.pack(fill='x', pady=(12, 0))
            
            tk.Label(creds_frame, text="🔐 Credenciais:",
                    font=(Theme.FONT_FAMILY, 10, 'bold'),
                    bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY).pack(anchor='w')
            
            creds_input_frame = tk.Frame(creds_frame, bg=Theme.BG_CARD)
            creds_input_frame.pack(fill='x', pady=(8, 0))
            
            # Username field
            user_frame = tk.Frame(creds_input_frame, bg=Theme.BG_CARD)
            user_frame.pack(side='left', fill='x', expand=True)
            
            tk.Label(user_frame, text="Usuário:",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
            
            self.username_entry = tk.Entry(user_frame, 
                                          font=(Theme.FONT_FAMILY, 10),
                                          bg=Theme.BG_INPUT, fg=Theme.FG_PRIMARY,
                                          insertbackground=Theme.PRIMARY,
                                          relief='flat', width=12,
                                          highlightbackground=Theme.BORDER, highlightthickness=1)
            self.username_entry.pack(side='left', padx=(6, 0))
            
            # Preenche com valor salvo ou default
            saved_user = device.get('username', '')
            if saved_user:
                self.username_entry.insert(0, saved_user)
            elif device.get('default_users'):
                self.username_entry.insert(0, device['default_users'][0])
            else:
                self.username_entry.insert(0, 'admin')
            
            # Password field
            pass_frame = tk.Frame(creds_input_frame, bg=Theme.BG_CARD)
            pass_frame.pack(side='left', padx=(16, 0))
            
            tk.Label(pass_frame, text="Senha:",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
            
            self.password_entry = tk.Entry(pass_frame, 
                                          font=(Theme.FONT_FAMILY, 10),
                                          bg=Theme.BG_INPUT, fg=Theme.FG_PRIMARY,
                                          insertbackground=Theme.PRIMARY,
                                          relief='flat', width=12, show='•',
                                          highlightbackground=Theme.BORDER, highlightthickness=1)
            self.password_entry.pack(side='left', padx=(6, 0))
            
            # Preenche com valor salvo ou default
            saved_pass = device.get('password', '')
            if saved_pass:
                self.password_entry.insert(0, saved_pass)
            elif device.get('default_passwords'):
                self.password_entry.insert(0, device['default_passwords'][0])
            
            # Save button (auto-save on blur)
            self.save_btn = tk.Label(creds_input_frame, text="💾 Salvar",
                                    font=(Theme.FONT_FAMILY, 9, 'bold'),
                                    bg=Theme.BG_CARD, fg=Theme.PRIMARY, cursor='hand2',
                                    padx=8)
            self.save_btn.pack(side='left', padx=(16, 0))
            self.save_btn.bind('<Button-1>', lambda e: self._save_credentials())
            self.save_btn.bind('<Enter>', lambda e: self.save_btn.config(fg=Theme.PRIMARY_HOVER))
            self.save_btn.bind('<Leave>', lambda e: self.save_btn.config(fg=Theme.PRIMARY))
            
            # Test RTSP button
            self.test_btn = tk.Label(creds_input_frame, text="🔗 Testar RTSP",
                                    font=(Theme.FONT_FAMILY, 9, 'bold'),
                                    bg=Theme.PRIMARY, fg='#ffffff', cursor='hand2',
                                    padx=12, pady=4)
            self.test_btn.pack(side='right')
            self.test_btn.bind('<Button-1>', lambda e: self._test_rtsp())
            self.test_btn.bind('<Enter>', lambda e: self.test_btn.config(bg=Theme.PRIMARY_HOVER))
            self.test_btn.bind('<Leave>', lambda e: self.test_btn.config(bg=Theme.PRIMARY))
            
            # === RTSP URL SELECTOR ===
            rtsp_templates = device.get('rtsp_templates', [])
            if rtsp_templates:
                rtsp_frame = tk.Frame(content, bg=Theme.BG_CARD)
                rtsp_frame.pack(fill='x', pady=(12, 0))
                
                tk.Label(rtsp_frame, text="📡 Template RTSP:",
                        font=(Theme.FONT_FAMILY, 9),
                        bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
                
                # Dropdown para selecionar template
                self.selected_template = tk.StringVar()
                templates_display = []
                for t in rtsp_templates[:5]:  # Limita a 5 templates
                    # Simplifica a exibição
                    display = t.replace('{user}:{pass}@', '').replace('{ip}', device['ip'])
                    templates_display.append(display)
                
                self.template_menu = ttk.Combobox(rtsp_frame, 
                                                 textvariable=self.selected_template,
                                                 values=templates_display,
                                                 state='readonly',
                                                 width=50)
                self.template_menu.pack(side='left', padx=(8, 0))
                if templates_display:
                    self.template_menu.current(0)
                
                self._rtsp_templates = rtsp_templates  # Guarda os templates originais
            
            # === TEST RESULT / MESSAGE ===
            self.result_frame = tk.Frame(content, bg=Theme.BG_CARD)
            self.result_frame.pack(fill='x', pady=(8, 0))
            
            self.result_label = tk.Label(self.result_frame, text="",
                                        font=(Theme.FONT_FAMILY, 9),
                                        bg=Theme.BG_CARD, fg=Theme.FG_MUTED)
            self.result_label.pack(anchor='w')
            
            # Mostra mensagem do último teste se existir
            if device.get('last_test_message'):
                self.result_label.config(text=f"Último teste: {device['last_test_message']}")
            
            # === COPY URL ROW ===
            if device.get('suggested_url'):
                url_frame = tk.Frame(content, bg=Theme.BG_INPUT,
                                    highlightbackground=Theme.BORDER, highlightthickness=1)
                url_frame.pack(fill='x', pady=(12, 0))
                
                url_inner = tk.Frame(url_frame, bg=Theme.BG_INPUT)
                url_inner.pack(fill='x', padx=12, pady=8)
                
                tk.Label(url_inner, text="✓",
                        font=(Theme.FONT_FAMILY, 10),
                        bg=Theme.BG_INPUT, fg=Theme.SUCCESS).pack(side='left')
                
                # URL truncada
                url = device['suggested_url']
                display_url = url if len(url) < 55 else url[:52] + "..."
                
                self.url_text = tk.Label(url_inner, text=display_url,
                        font=(Theme.FONT_MONO, 9),
                        bg=Theme.BG_INPUT, fg=Theme.FG_SECONDARY)
                self.url_text.pack(side='left', padx=(8, 0))
                
                # Botão copiar
                copy_btn = tk.Label(url_inner, text="📋 Copiar",
                                   font=(Theme.FONT_FAMILY, 9),
                                   bg=Theme.BG_INPUT, fg=Theme.PRIMARY, cursor='hand2')
                copy_btn.pack(side='right')
                copy_btn.bind('<Button-1>', lambda e: self._copy_url(url, copy_btn))
                copy_btn.bind('<Enter>', lambda e: copy_btn.config(fg=Theme.PRIMARY_HOVER))
                copy_btn.bind('<Leave>', lambda e: copy_btn.config(fg=Theme.PRIMARY))
        
        def _save_credentials(self):
            """Salva credenciais no banco de dados"""
            username = self.username_entry.get().strip()
            password = self.password_entry.get()
            
            if not self.supabase or not self.device.get('id'):
                self.result_label.config(text="⚠ Não foi possível salvar (sem conexão)", fg=Theme.WARNING)
                return
            
            self.save_btn.config(text="⏳ Salvando...")
            
            def save_thread():
                try:
                    self.supabase.update_device_credentials(
                        self.device['id'],
                        username,
                        password
                    )
                    self.after(0, lambda: self._on_save_success())
                except Exception as e:
                    self.after(0, lambda: self._on_save_error(str(e)))
            
            threading.Thread(target=save_thread, daemon=True).start()
        
        def _on_save_success(self):
            self.save_btn.config(text="✓ Salvo!", fg=Theme.SUCCESS)
            self.result_label.config(text="✓ Credenciais salvas com sucesso!", fg=Theme.SUCCESS)
            self.after(2000, lambda: self.save_btn.config(text="💾 Salvar", fg=Theme.PRIMARY))
        
        def _on_save_error(self, error):
            self.save_btn.config(text="💾 Salvar", fg=Theme.PRIMARY)
            self.result_label.config(text=f"✗ Erro ao salvar: {error}", fg=Theme.ERROR)
        
        def _test_rtsp(self):
            """Testa conexão RTSP localmente"""
            username = self.username_entry.get().strip()
            password = self.password_entry.get()
            ip = self.device['ip']
            
            # Pega o template selecionado
            if hasattr(self, '_rtsp_templates') and self._rtsp_templates:
                idx = self.template_menu.current()
                if idx >= 0 and idx < len(self._rtsp_templates):
                    template = self._rtsp_templates[idx]
                else:
                    template = self._rtsp_templates[0]
            else:
                template = f"rtsp://{{user}}:{{pass}}@{{ip}}:554/"
            
            # Constrói URL
            rtsp_url = template.replace('{user}', username).replace('{pass}', password).replace('{ip}', ip)
            
            self.test_btn.config(text="⏳ Testando...", bg=Theme.FG_MUTED)
            self.result_label.config(text=f"Testando: {rtsp_url}", fg=Theme.FG_SECONDARY)
            
            def test_thread():
                success, message, details = test_rtsp_connection(rtsp_url)
                self.after(0, lambda: self._on_test_result(success, message, rtsp_url))
            
            threading.Thread(target=test_thread, daemon=True).start()
        
        def _on_test_result(self, success: bool, message: str, rtsp_url: str):
            """Callback do teste RTSP"""
            self.test_btn.config(text="🔗 Testar RTSP", bg=Theme.PRIMARY)
            
            if success:
                self.result_label.config(text=f"✓ {message}", fg=Theme.SUCCESS)
                self.status_label.config(text="✓ Validado", fg=Theme.SUCCESS)
                
                # Atualiza URL no device
                self.device['suggested_url'] = rtsp_url
                self.device['rtsp_validated'] = True
                
                # Salva no banco se possível
                if self.supabase and self.device.get('id'):
                    def save_status():
                        try:
                            self.supabase.update_device_rtsp_status(
                                self.device['id'],
                                success=True,
                                message=message,
                                url=rtsp_url
                            )
                        except Exception as e:
                            logger.error(f"Erro ao salvar status RTSP: {e}")
                    
                    threading.Thread(target=save_status, daemon=True).start()
            else:
                self.result_label.config(text=f"✗ {message}", fg=Theme.ERROR)
                self.status_label.config(text="✗ Falhou", fg=Theme.ERROR)
                
                # Salva falha no banco
                if self.supabase and self.device.get('id'):
                    def save_status():
                        try:
                            self.supabase.update_device_rtsp_status(
                                self.device['id'],
                                success=False,
                                message=message
                            )
                        except Exception as e:
                            logger.error(f"Erro ao salvar status RTSP: {e}")
                    
                    threading.Thread(target=save_status, daemon=True).start()
        
        def _copy_url(self, url, btn):
            """Copia URL para clipboard"""
            try:
                self.clipboard_clear()
                self.clipboard_append(url)
                btn.config(text="✓ Copiado!", fg=Theme.SUCCESS)
                self.after(2000, lambda: btn.config(text="📋 Copiar", fg=Theme.PRIMARY))
            except:
                pass
    
    
    class ScanProgressCard(tk.Frame):
        """Card de progresso detalhado do scan"""
        def __init__(self, parent, **kwargs):
            super().__init__(parent, bg=Theme.BG_CARD, **kwargs)
            self.configure(highlightbackground=Theme.BORDER, highlightthickness=1)
            
            content = tk.Frame(self, bg=Theme.BG_CARD)
            content.pack(fill='x', padx=24, pady=20)
            
            # Header
            header = tk.Frame(content, bg=Theme.BG_CARD)
            header.pack(fill='x')
            
            self.title_label = tk.Label(header, text="🔍 Escaneando Rede...",
                                       font=(Theme.FONT_FAMILY, 14, 'bold'),
                                       bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY)
            self.title_label.pack(side='left')
            
            self.percent_label = tk.Label(header, text="0%",
                                         font=(Theme.FONT_FAMILY, 14, 'bold'),
                                         bg=Theme.BG_CARD, fg=Theme.PRIMARY)
            self.percent_label.pack(side='right')
            
            # Progress bar container
            progress_outer = tk.Frame(content, bg=Theme.BG_INPUT, height=12)
            progress_outer.pack(fill='x', pady=(16, 0))
            progress_outer.pack_propagate(False)
            
            self.progress_bar = tk.Frame(progress_outer, bg=Theme.PRIMARY, width=0)
            self.progress_bar.pack(side='left', fill='y')
            
            # Stats row
            stats_frame = tk.Frame(content, bg=Theme.BG_CARD)
            stats_frame.pack(fill='x', pady=(16, 0))
            
            # Scanned hosts
            stat1 = tk.Frame(stats_frame, bg=Theme.BG_CARD)
            stat1.pack(side='left', expand=True)
            self.scanned_value = tk.Label(stat1, text="0",
                                         font=(Theme.FONT_FAMILY, 20, 'bold'),
                                         bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY)
            self.scanned_value.pack()
            tk.Label(stat1, text="Hosts Verificados",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack()
            
            # Found cameras
            stat2 = tk.Frame(stats_frame, bg=Theme.BG_CARD)
            stat2.pack(side='left', expand=True)
            self.found_value = tk.Label(stat2, text="0",
                                       font=(Theme.FONT_FAMILY, 20, 'bold'),
                                       bg=Theme.BG_CARD, fg=Theme.SUCCESS)
            self.found_value.pack()
            tk.Label(stat2, text="Câmeras Encontradas",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack()
            
            # Remaining
            stat3 = tk.Frame(stats_frame, bg=Theme.BG_CARD)
            stat3.pack(side='left', expand=True)
            self.remaining_value = tk.Label(stat3, text="254",
                                           font=(Theme.FONT_FAMILY, 20, 'bold'),
                                           bg=Theme.BG_CARD, fg=Theme.FG_SECONDARY)
            self.remaining_value.pack()
            tk.Label(stat3, text="Restantes",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack()
            
            # Current IP being scanned
            self.current_ip_label = tk.Label(content, text="",
                                            font=(Theme.FONT_MONO, 9),
                                            bg=Theme.BG_CARD, fg=Theme.FG_DARK)
            self.current_ip_label.pack(pady=(12, 0))
        
        def update_progress(self, progress, scanned, total, found):
            """Atualiza o progresso do scan"""
            self.percent_label.config(text=f"{progress}%")
            self.scanned_value.config(text=str(scanned))
            self.found_value.config(text=str(found))
            self.remaining_value.config(text=str(max(0, total - scanned)))
            
            # Update progress bar
            self.update_idletasks()
            container_width = self.winfo_width() - 48  # padding
            if container_width > 0:
                bar_width = int((progress / 100) * container_width)
                self.progress_bar.configure(width=max(bar_width, 0))
        
        def set_completed(self, found):
            """Marca scan como concluído"""
            self.title_label.config(text="✅ Scan Concluído")
            self.percent_label.config(text="100%", fg=Theme.SUCCESS)
            self.current_ip_label.config(text="")
            
            # Full progress bar
            self.update_idletasks()
            container_width = self.winfo_width() - 48
            self.progress_bar.configure(width=container_width, bg=Theme.SUCCESS)
        
        def set_cancelled(self):
            """Marca scan como cancelado"""
            self.title_label.config(text="⏹ Scan Cancelado")
            self.percent_label.config(fg=Theme.WARNING)
            self.current_ip_label.config(text="")
            self.progress_bar.configure(bg=Theme.WARNING)
    
    
    class CameraScannerApp:
        def __init__(self, root):
            self.root = root
            self.root.title("Camera Scanner Agent")
            self.root.geometry("960x720")
            self.root.minsize(800, 600)
            self.root.configure(bg=Theme.BG_PRIMARY)
            
            # Clientes (inicializado depois)
            self.supabase = None
            self.scanner = None
            
            # Estado
            self.message_queue = queue.Queue()
            self.minimized_to_tray = False
            self.camera_cards = []
            self.requirements_checked = False
            
            # Configura fechamento
            self.root.protocol("WM_DELETE_WINDOW", self.minimize_to_tray)
            
            # Inicia com tela de verificação de requisitos
            self.show_requirements_screen()
            self.process_messages()
        
        def show_requirements_screen(self):
            """Tela de verificação de requisitos do sistema"""
            for widget in self.root.winfo_children():
                widget.destroy()
            
            self.root.configure(bg=Theme.BG_DARK)
            
            # Container central
            center = tk.Frame(self.root, bg=Theme.BG_DARK)
            center.place(relx=0.5, rely=0.5, anchor='center')
            
            # Logo
            logo_frame = tk.Frame(center, bg=Theme.BG_DARK)
            logo_frame.pack(pady=(0, 24))
            
            # Tenta carregar a imagem do logo
            try:
                logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')
                logger.info(f"Buscando logo em: {logo_path}")
                if os.path.exists(logo_path) and ImageTk:
                    logo_img = Image.open(logo_path)
                    logo_img = logo_img.resize((120, 96), Image.Resampling.LANCZOS)
                    self.logo_photo = ImageTk.PhotoImage(logo_img)
                    tk.Label(logo_frame, image=self.logo_photo, bg=Theme.BG_DARK).pack()
                    logger.info("Logo carregado com sucesso!")
                else:
                    logger.warning(f"Logo não encontrado em: {logo_path}")
                    # Fallback para emoji se imagem não existir
                    fallback = tk.Frame(logo_frame, bg=Theme.PRIMARY, width=80, height=80)
                    fallback.pack()
                    fallback.pack_propagate(False)
                    tk.Label(fallback, text="📹", font=(Theme.FONT_FAMILY, 36),
                            bg=Theme.PRIMARY).place(relx=0.5, rely=0.5, anchor='center')
            except Exception as e:
                logger.warning(f"Erro ao carregar logo: {e}")
                fallback = tk.Frame(logo_frame, bg=Theme.PRIMARY, width=80, height=80)
                fallback.pack()
                fallback.pack_propagate(False)
                tk.Label(fallback, text="📹", font=(Theme.FONT_FAMILY, 36),
                        bg=Theme.PRIMARY).place(relx=0.5, rely=0.5, anchor='center')
            
            # Título
            tk.Label(center, text="Camera Scanner",
                    font=(Theme.FONT_FAMILY, 28, 'bold'),
                    bg=Theme.BG_DARK, fg=Theme.FG_PRIMARY).pack()
            
            tk.Label(center, text="Verificando requisitos do sistema...",
                    font=(Theme.FONT_FAMILY, 12),
                    bg=Theme.BG_DARK, fg=Theme.FG_SECONDARY).pack(pady=(8, 32))
            
            # Card de requisitos
            req_card = tk.Frame(center, bg=Theme.BG_CARD, width=450,
                               highlightbackground=Theme.BORDER, highlightthickness=1)
            req_card.pack(pady=16)
            req_card.pack_propagate(False)
            
            req_content = tk.Frame(req_card, bg=Theme.BG_CARD)
            req_content.pack(padx=32, pady=24, fill='both', expand=True)
            
            # Lista de requisitos
            self.req_items = {}
            
            requirements = [
                ("network", "🌐", "Conexão de Rede"),
                ("ffmpeg", "🎬", "FFmpeg (Streaming)"),
                ("platform", "☁️", "Conexão com Plataforma"),
            ]
            
            for key, icon, label in requirements:
                item_frame = tk.Frame(req_content, bg=Theme.BG_CARD)
                item_frame.pack(fill='x', pady=8)
                
                # Ícone
                icon_label = tk.Label(item_frame, text=icon,
                                     font=(Theme.FONT_FAMILY, 16),
                                     bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY)
                icon_label.pack(side='left')
                
                # Nome do requisito
                name_label = tk.Label(item_frame, text=label,
                                     font=(Theme.FONT_FAMILY, 12),
                                     bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY)
                name_label.pack(side='left', padx=(12, 0))
                
                # Status
                status_label = tk.Label(item_frame, text="⏳ Verificando...",
                                       font=(Theme.FONT_FAMILY, 10),
                                       bg=Theme.BG_CARD, fg=Theme.FG_MUTED)
                status_label.pack(side='right')
                
                self.req_items[key] = {
                    'frame': item_frame,
                    'status': status_label,
                    'icon': icon_label
                }
            
            # Barra de progresso
            progress_container = tk.Frame(req_content, bg=Theme.BG_CARD)
            progress_container.pack(fill='x', pady=(24, 8))
            
            progress_outer = tk.Frame(progress_container, bg=Theme.BG_INPUT, height=8)
            progress_outer.pack(fill='x')
            progress_outer.pack_propagate(False)
            
            self.req_progress_bar = tk.Frame(progress_outer, bg=Theme.PRIMARY, width=0)
            self.req_progress_bar.pack(side='left', fill='y')
            
            # Mensagem de status
            self.req_status_label = tk.Label(req_content, text="Iniciando verificação...",
                                            font=(Theme.FONT_FAMILY, 10),
                                            bg=Theme.BG_CARD, fg=Theme.FG_MUTED)
            self.req_status_label.pack(pady=(8, 0))
            
            # Ajusta altura do card
            req_card.configure(height=280)
            
            # Inicia verificação em thread separada
            self.root.after(500, self.start_requirements_check)
        
        def update_requirement_status(self, key: str, status: str, message: str):
            """Atualiza o status de um requisito"""
            if key not in self.req_items:
                return
            
            item = self.req_items[key]
            
            if status == "checking":
                item['status'].config(text="⏳ Verificando...", fg=Theme.FG_MUTED)
            elif status == "installing":
                item['status'].config(text=f"📥 {message}", fg=Theme.WARNING)
            elif status == "success":
                item['status'].config(text="✓ OK", fg=Theme.SUCCESS)
            elif status == "error":
                item['status'].config(text=f"✗ {message}", fg=Theme.ERROR)
            elif status == "warning":
                item['status'].config(text=f"⚠ {message}", fg=Theme.WARNING)
        
        def update_requirement_progress(self, percent: int, message: str = ""):
            """Atualiza a barra de progresso dos requisitos"""
            try:
                # Atualiza barra
                container_width = 386  # largura fixa do container
                bar_width = int((percent / 100) * container_width)
                self.req_progress_bar.configure(width=max(bar_width, 0))
                
                if message:
                    self.req_status_label.config(text=message)
            except:
                pass
        
        def start_requirements_check(self):
            """Inicia verificação de requisitos em thread separada"""
            def check_requirements():
                # 1. Verifica rede
                self.message_queue.put(('req_update', 'network', 'checking', ''))
                self.message_queue.put(('req_progress', 10, 'Verificando conexão de rede...'))
                
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    local_ip = s.getsockname()[0]
                    s.close()
                    self.message_queue.put(('req_update', 'network', 'success', local_ip))
                except:
                    self.message_queue.put(('req_update', 'network', 'error', 'Sem conexão'))
                
                import time
                time.sleep(0.3)
                
                # 2. Verifica FFmpeg (OBRIGATÓRIO)
                self.message_queue.put(('req_update', 'ffmpeg', 'checking', ''))
                self.message_queue.put(('req_progress', 20, 'Verificando FFmpeg...'))
                
                def ffmpeg_progress(msg, pct, status):
                    self.message_queue.put(('req_update', 'ffmpeg', status, msg))
                    # Mapeia progresso do FFmpeg para 20-80%
                    mapped = 20 + int(pct * 0.6)
                    self.message_queue.put(('req_progress', mapped, msg))
                
                # Inicializa SupabaseClient com callback
                self.supabase = SupabaseClient(on_ffmpeg_progress=ffmpeg_progress)
                
                # Tenta instalar FFmpeg até 3 vezes se falhar
                ffmpeg_ok = False
                max_attempts = 3
                for attempt in range(max_attempts):
                    ffmpeg_ok = self.supabase.check_and_install_ffmpeg(ffmpeg_progress)
                    if ffmpeg_ok:
                        break
                    if attempt < max_attempts - 1:
                        self.message_queue.put(('req_update', 'ffmpeg', 'installing', f'Tentativa {attempt + 2} de {max_attempts}...'))
                        time.sleep(2)
                
                if ffmpeg_ok:
                    self.message_queue.put(('req_update', 'ffmpeg', 'success', ''))
                else:
                    # FFmpeg é OBRIGATÓRIO - não continua sem ele
                    self.message_queue.put(('req_update', 'ffmpeg', 'error', 'Falha na instalação'))
                    self.message_queue.put(('req_progress', 100, 'FFmpeg é obrigatório!'))
                    self.message_queue.put(('req_error', 'ffmpeg', 'FFmpeg é necessário para o funcionamento do app. Por favor, instale manualmente ou verifique sua conexão.', None))
                    return
                
                time.sleep(0.3)
                
                # 3. Verifica conexão com plataforma
                self.message_queue.put(('req_update', 'platform', 'checking', ''))
                self.message_queue.put(('req_progress', 85, 'Conectando à plataforma...'))
                
                try:
                    ctx = ssl.create_default_context()
                    req = urllib.request.Request(
                        f"{SUPABASE_URL}/rest/v1/",
                        headers={"apikey": SUPABASE_ANON_KEY}
                    )
                    urllib.request.urlopen(req, timeout=10, context=ctx)
                    self.message_queue.put(('req_update', 'platform', 'success', ''))
                except Exception as e:
                    self.message_queue.put(('req_update', 'platform', 'error', 'Offline'))
                
                self.message_queue.put(('req_progress', 100, 'Verificação concluída!'))
                time.sleep(0.5)
                
                # Marca como verificado e vai para login
                self.message_queue.put(('req_complete', None, None, None))
            
            thread = threading.Thread(target=check_requirements, daemon=True)
            thread.start()
        
        def show_login_screen(self):
            """Tela de login moderna e elegante"""
            for widget in self.root.winfo_children():
                widget.destroy()
            
            # Background com gradiente simulado
            self.root.configure(bg=Theme.BG_DARK)
            
            # Toast para mensagens
            self.toast = Toast(self.root)
            
            # Container principal com layout
            main_container = tk.Frame(self.root, bg=Theme.BG_DARK)
            main_container.pack(fill='both', expand=True)
            
            # Left side - branding
            left_panel = tk.Frame(main_container, bg=Theme.BG_DARK, width=400)
            left_panel.pack(side='left', fill='y')
            left_panel.pack_propagate(False)
            
            left_content = tk.Frame(left_panel, bg=Theme.BG_DARK)
            left_content.place(relx=0.5, rely=0.5, anchor='center')
            
            # Logo grande
            logo_container = tk.Frame(left_content, bg=Theme.BG_DARK)
            logo_container.pack(pady=(0, 32))
            
            # Tenta carregar a imagem do logo
            try:
                logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')
                if os.path.exists(logo_path) and ImageTk:
                    logo_img = Image.open(logo_path)
                    logo_img = logo_img.resize((150, 120), Image.Resampling.LANCZOS)
                    self.login_logo_photo = ImageTk.PhotoImage(logo_img)
                    tk.Label(logo_container, image=self.login_logo_photo, bg=Theme.BG_DARK).pack()
                else:
                    # Fallback para canvas com emoji
                    fallback = tk.Frame(logo_container, bg=Theme.PRIMARY, width=100, height=100)
                    fallback.pack()
                    fallback.pack_propagate(False)
                    logo_canvas = tk.Canvas(fallback, width=100, height=100, 
                                           bg=Theme.PRIMARY, highlightthickness=0)
                    logo_canvas.pack()
                    logo_canvas.create_text(50, 50, text="📹", font=(Theme.FONT_FAMILY, 42))
            except Exception as e:
                logger.warning(f"Erro ao carregar logo login: {e}")
                fallback = tk.Frame(logo_container, bg=Theme.PRIMARY, width=100, height=100)
                fallback.pack()
                fallback.pack_propagate(False)
                logo_canvas = tk.Canvas(fallback, width=100, height=100, 
                                       bg=Theme.PRIMARY, highlightthickness=0)
                logo_canvas.pack()
                logo_canvas.create_text(50, 50, text="📹", font=(Theme.FONT_FAMILY, 42))
            
            # Título principal
            tk.Label(left_content, text="Camera Scanner",
                    font=(Theme.FONT_FAMILY, 32, 'bold'),
                    bg=Theme.BG_DARK, fg=Theme.FG_PRIMARY).pack()
            
            tk.Label(left_content, text="Agente de Descoberta de Câmeras",
                    font=(Theme.FONT_FAMILY, 13),
                    bg=Theme.BG_DARK, fg=Theme.FG_SECONDARY).pack(pady=(8, 40))
            
            # Features
            features = [
                ("🔍", "Scan automático da rede local"),
                ("📡", "Detecção de múltiplas marcas"),
                ("☁️", "Sincronização com a plataforma"),
                ("🔒", "Conexão segura e criptografada"),
            ]
            
            for icon, text in features:
                feat_frame = tk.Frame(left_content, bg=Theme.BG_DARK)
                feat_frame.pack(anchor='w', pady=6)
                tk.Label(feat_frame, text=icon,
                        font=(Theme.FONT_FAMILY, 14),
                        bg=Theme.BG_DARK, fg=Theme.PRIMARY).pack(side='left')
                tk.Label(feat_frame, text=text,
                        font=(Theme.FONT_FAMILY, 11),
                        bg=Theme.BG_DARK, fg=Theme.FG_SECONDARY).pack(side='left', padx=(12, 0))
            
            # Right side - login form
            right_panel = tk.Frame(main_container, bg=Theme.BG_PRIMARY)
            right_panel.pack(side='right', fill='both', expand=True)
            
            # Card de login centralizado
            form_container = tk.Frame(right_panel, bg=Theme.BG_PRIMARY)
            form_container.place(relx=0.5, rely=0.5, anchor='center')
            
            # Card
            card = tk.Frame(form_container, bg=Theme.BG_CARD,
                           highlightbackground=Theme.BORDER, highlightthickness=1)
            card.pack()
            
            card_content = tk.Frame(card, bg=Theme.BG_CARD)
            card_content.pack(padx=48, pady=48)
            
            # Header do card
            tk.Label(card_content, text="Bem-vindo de volta",
                    font=(Theme.FONT_FAMILY, 22, 'bold'),
                    bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY).pack(anchor='w')
            
            tk.Label(card_content, text="Entre com sua conta da plataforma IVMS Pro",
                    font=(Theme.FONT_FAMILY, 11),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(anchor='w', pady=(4, 32))
            
            # Email
            tk.Label(card_content, text="Email",
                    font=(Theme.FONT_FAMILY, 10, 'bold'),
                    bg=Theme.BG_CARD, fg=Theme.FG_SECONDARY).pack(anchor='w', pady=(0, 8))
            
            self.email_entry = ModernEntry(card_content, placeholder="seu@email.com", icon="✉")
            self.email_entry.pack(fill='x', pady=(0, 20))
            self.email_entry.entry.configure(width=35)
            
            # Senha
            tk.Label(card_content, text="Senha",
                    font=(Theme.FONT_FAMILY, 10, 'bold'),
                    bg=Theme.BG_CARD, fg=Theme.FG_SECONDARY).pack(anchor='w', pady=(0, 8))
            
            self.password_entry = ModernEntry(card_content, placeholder="Digite sua senha", show="•", icon="🔒")
            self.password_entry.pack(fill='x', pady=(0, 32))
            self.password_entry.entry.configure(width=35)
            self.password_entry.bind('<Return>', lambda e: self.do_login())
            
            # Login button
            self.login_btn = ModernButton(card_content, "Entrar na conta", 
                                         command=self.do_login, 
                                         variant='primary', width=320, height=44)
            self.login_btn.pack(pady=(0, 24))
            
            # Divider com texto
            divider_frame = tk.Frame(card_content, bg=Theme.BG_CARD)
            divider_frame.pack(fill='x', pady=16)
            
            tk.Frame(divider_frame, bg=Theme.BORDER, height=1).pack(side='left', fill='x', expand=True)
            tk.Label(divider_frame, text="  ou  ",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
            tk.Frame(divider_frame, bg=Theme.BORDER, height=1).pack(side='left', fill='x', expand=True)
            
            # Create account link
            link_frame = tk.Frame(card_content, bg=Theme.BG_CARD)
            link_frame.pack(pady=(8, 0))
            
            tk.Label(link_frame, text="Não tem uma conta?",
                    font=(Theme.FONT_FAMILY, 10),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
            
            create_link = tk.Label(link_frame, text="Criar conta grátis",
                                  font=(Theme.FONT_FAMILY, 10, 'bold'),
                                  bg=Theme.BG_CARD, fg=Theme.PRIMARY, cursor='hand2')
            create_link.pack(side='left', padx=(6, 0))
            create_link.bind('<Button-1>', 
                lambda e: webbrowser.open('https://bb7b0089-1093-460a-a362-22831c464913.lovableproject.com/registro'))
            create_link.bind('<Enter>', lambda e: create_link.config(fg=Theme.PRIMARY_HOVER))
            create_link.bind('<Leave>', lambda e: create_link.config(fg=Theme.PRIMARY))
            
            # Footer
            footer = tk.Frame(right_panel, bg=Theme.BG_PRIMARY)
            footer.pack(side='bottom', pady=24)
            
            tk.Label(footer, text="🔒 Conexão segura com Supabase",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.BG_PRIMARY, fg=Theme.FG_MUTED).pack()
        
        def _get_friendly_error(self, error_msg: str) -> str:
            """Converte erros técnicos em mensagens amigáveis"""
            error_lower = error_msg.lower()
            
            if 'invalid login credentials' in error_lower or 'invalid_credentials' in error_lower:
                return "Email ou senha incorretos"
            elif 'email not confirmed' in error_lower:
                return "Email ainda não confirmado. Verifique sua caixa de entrada"
            elif 'user not found' in error_lower:
                return "Usuário não encontrado"
            elif 'too many requests' in error_lower or 'rate limit' in error_lower:
                return "Muitas tentativas. Aguarde um momento"
            elif 'network' in error_lower or 'connection' in error_lower or 'timeout' in error_lower:
                return "Erro de conexão. Verifique sua internet"
            elif 'password' in error_lower and 'weak' in error_lower:
                return "Senha muito fraca"
            elif 'email' in error_lower and 'invalid' in error_lower:
                return "Email inválido"
            else:
                return "Falha no login. Tente novamente"
        
        def do_login(self):
            email = self.email_entry.get().strip()
            password = self.password_entry.get()
            
            if not email or not password:
                self.toast.show("Preencha email e senha", 'warning')
                return
            
            if '@' not in email or '.' not in email:
                self.toast.show("Digite um email válido", 'warning')
                return
            
            self.login_btn.set_enabled(False)
            self.login_btn.set_text("Entrando...")
            
            def login_thread():
                try:
                    self.supabase.login(email, password)
                    self.message_queue.put(('login_success', None))
                except Exception as e:
                    friendly_msg = self._get_friendly_error(str(e))
                    self.message_queue.put(('login_error', friendly_msg))
            
            threading.Thread(target=login_thread, daemon=True).start()
        
        def show_main_screen(self):
            """Tela principal moderna"""
            for widget in self.root.winfo_children():
                widget.destroy()
            
            self.root.configure(bg=Theme.BG_PRIMARY)
            
            # Toast para mensagens
            self.toast = Toast(self.root)
            
            # Scanner com callbacks
            self.scanner = NetworkScanner(
                progress_callback=self.on_progress,
                device_found_callback=self.on_device_found,
                supabase_client=self.supabase
            )
            
            # ===== HEADER =====
            header = tk.Frame(self.root, bg=Theme.BG_DARK, height=64)
            header.pack(fill='x')
            header.pack_propagate(False)
            
            header_content = tk.Frame(header, bg=Theme.BG_DARK)
            header_content.pack(fill='both', expand=True, padx=24)
            
            # Logo
            logo_frame = tk.Frame(header_content, bg=Theme.BG_DARK)
            logo_frame.pack(side='left', pady=12)
            
            # Tenta carregar a imagem do logo no header
            try:
                logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')
                if os.path.exists(logo_path) and ImageTk:
                    logo_img = Image.open(logo_path)
                    logo_img = logo_img.resize((50, 40), Image.Resampling.LANCZOS)
                    self.header_logo_photo = ImageTk.PhotoImage(logo_img)
                    tk.Label(logo_frame, image=self.header_logo_photo, bg=Theme.BG_DARK).pack(side='left')
                else:
                    tk.Label(logo_frame, text="📹",
                            font=(Theme.FONT_FAMILY, 20),
                            bg=Theme.BG_DARK, fg=Theme.PRIMARY).pack(side='left')
            except Exception as e:
                logger.warning(f"Erro ao carregar logo header: {e}")
                tk.Label(logo_frame, text="📹",
                        font=(Theme.FONT_FAMILY, 20),
                        bg=Theme.BG_DARK, fg=Theme.PRIMARY).pack(side='left')
            
            tk.Label(logo_frame, text="Camera Scanner",
                    font=(Theme.FONT_FAMILY, 16, 'bold'),
                    bg=Theme.BG_DARK, fg=Theme.FG_PRIMARY).pack(side='left', padx=(8, 0))
            
            # User info
            user_frame = tk.Frame(header_content, bg=Theme.BG_DARK)
            user_frame.pack(side='right', pady=12)
            
            tk.Label(user_frame, text=self.supabase.user_email,
                    font=(Theme.FONT_FAMILY, 10),
                    bg=Theme.BG_DARK, fg=Theme.FG_SECONDARY).pack(side='left', padx=(0, 16))
            
            logout_btn = ModernButton(user_frame, "Sair", 
                                     command=self.do_logout,
                                     variant='ghost', width=70, height=32)
            logout_btn.configure(bg=Theme.BG_DARK)
            logout_btn.pack(side='left')
            
            # ===== MAIN CONTENT =====
            main = tk.Frame(self.root, bg=Theme.BG_PRIMARY)
            main.pack(fill='both', expand=True, padx=24, pady=24)
            
            # ===== NETWORK INFO CARD =====
            info_card = tk.Frame(main, bg=Theme.BG_CARD)
            info_card.pack(fill='x', pady=(0, 16))
            
            info_content = tk.Frame(info_card, bg=Theme.BG_CARD)
            info_content.pack(fill='x', padx=24, pady=20)
            
            # Right - Scan buttons (pack primeiro para ficar à direita)
            btn_frame = tk.Frame(info_content, bg=Theme.BG_CARD)
            btn_frame.pack(side='right', anchor='e')
            
            self.scan_btn = ModernButton(btn_frame, "🔍  Buscar Câmeras",
                                        command=self.start_scan,
                                        variant='primary', width=180, height=44)
            self.scan_btn.configure(bg=Theme.BG_CARD)
            self.scan_btn.pack(side='left', padx=(0, 12))
            
            self.stop_btn = ModernButton(btn_frame, "⏹  Parar",
                                        command=self.stop_scan,
                                        variant='secondary', width=100, height=44)
            self.stop_btn.configure(bg=Theme.BG_CARD)
            self.stop_btn.set_enabled(False)
            self.stop_btn.pack(side='left')
            
            # Left - Network info
            info_left = tk.Frame(info_content, bg=Theme.BG_CARD)
            info_left.pack(side='left', fill='x', expand=True)
            
            tk.Label(info_left, text="Informações da Rede",
                    font=(Theme.FONT_FAMILY, 14, 'bold'),
                    bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY).pack(anchor='w')
            
            info_grid = tk.Frame(info_left, bg=Theme.BG_CARD)
            info_grid.pack(anchor='w', pady=(12, 0))
            
            # IP Local
            ip_frame = tk.Frame(info_grid, bg=Theme.BG_CARD)
            ip_frame.pack(anchor='w')
            tk.Label(ip_frame, text="IP Local:",
                    font=(Theme.FONT_FAMILY, 10),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
            tk.Label(ip_frame, text=self.scanner.get_local_ip(),
                    font=(Theme.FONT_MONO, 10, 'bold'),
                    bg=Theme.BG_CARD, fg=Theme.FG_PRIMARY).pack(side='left', padx=(8, 0))
            
            # Network range
            net_frame = tk.Frame(info_grid, bg=Theme.BG_CARD)
            net_frame.pack(anchor='w', pady=(4, 0))
            tk.Label(net_frame, text="Range:",
                    font=(Theme.FONT_FAMILY, 10),
                    bg=Theme.BG_CARD, fg=Theme.FG_MUTED).pack(side='left')
            tk.Label(net_frame, text=self.scanner.get_network_range(),
                    font=(Theme.FONT_MONO, 10),
                    bg=Theme.BG_CARD, fg=Theme.FG_SECONDARY).pack(side='left', padx=(8, 0))
            
            # Sync indicator
            sync_frame = tk.Frame(info_grid, bg=Theme.SUCCESS_BG)
            sync_frame.pack(anchor='w', pady=(12, 0))
            tk.Label(sync_frame, text="● Sincronização automática ativada",
                    font=(Theme.FONT_FAMILY, 9),
                    bg=Theme.SUCCESS_BG, fg=Theme.SUCCESS,
                    padx=10, pady=4).pack()
            
            # ===== PROGRESS CARD (inicialmente oculto) =====
            self.main_container = main  # Guarda referência
            self.progress_card = ScanProgressCard(main)
            self.progress_card_visible = False
            # Não faz pack ainda - só aparece durante o scan
            
            # ===== CAMERAS SECTION =====
            self.cameras_header = tk.Frame(main, bg=Theme.BG_PRIMARY)
            self.cameras_header.pack(fill='x', pady=(16, 16))
            
            tk.Label(self.cameras_header, text="📹 Câmeras Encontradas",
                    font=(Theme.FONT_FAMILY, 16, 'bold'),
                    bg=Theme.BG_PRIMARY, fg=Theme.FG_PRIMARY).pack(side='left')
            
            self.camera_count = tk.Label(self.cameras_header, text="",
                                        font=(Theme.FONT_FAMILY, 12),
                                        bg=Theme.BG_PRIMARY, fg=Theme.FG_MUTED)
            self.camera_count.pack(side='right')
            
            # Scrollable camera list
            self.cameras_container = tk.Frame(main, bg=Theme.BG_PRIMARY)
            self.cameras_container.pack(fill='both', expand=True)
            
            # Canvas with scrollbar
            self.cameras_canvas = tk.Canvas(self.cameras_container, bg=Theme.BG_PRIMARY,
                                           highlightthickness=0)
            scrollbar = ttk.Scrollbar(self.cameras_container, orient='vertical',
                                     command=self.cameras_canvas.yview)
            
            self.cameras_list = tk.Frame(self.cameras_canvas, bg=Theme.BG_PRIMARY)
            
            self.cameras_canvas.create_window((0, 0), window=self.cameras_list, anchor='nw')
            self.cameras_canvas.configure(yscrollcommand=scrollbar.set)
            
            self.cameras_canvas.pack(side='left', fill='both', expand=True)
            scrollbar.pack(side='right', fill='y')
            
            self.cameras_list.bind('<Configure>', 
                lambda e: self.cameras_canvas.configure(scrollregion=self.cameras_canvas.bbox('all')))
            
            # Empty state (inicial)
            self.empty_state = tk.Frame(self.cameras_list, bg=Theme.BG_PRIMARY)
            
            tk.Label(self.empty_state, text="📡",
                    font=(Theme.FONT_FAMILY, 48),
                    bg=Theme.BG_PRIMARY, fg=Theme.FG_DARK).pack()
            
            tk.Label(self.empty_state, text="Nenhuma câmera encontrada ainda",
                    font=(Theme.FONT_FAMILY, 14),
                    bg=Theme.BG_PRIMARY, fg=Theme.FG_MUTED).pack(pady=(16, 4))
            
            tk.Label(self.empty_state, text="Clique em 'Buscar Câmeras' para iniciar a varredura da rede",
                    font=(Theme.FONT_FAMILY, 10),
                    bg=Theme.BG_PRIMARY, fg=Theme.FG_DARK).pack()
            
            # ===== STATUS BAR =====
            status_bar = tk.Frame(self.root, bg=Theme.BG_DARK, height=48)
            status_bar.pack(fill='x', side='bottom')
            status_bar.pack_propagate(False)
            
            self.status_label = tk.Label(status_bar, text="⏳ Carregando câmeras salvas...",
                                        font=(Theme.FONT_FAMILY, 10),
                                        bg=Theme.BG_DARK, fg=Theme.FG_MUTED)
            self.status_label.pack(side='left', padx=24, pady=14)
            
            # Help text
            help_text = tk.Label(status_bar, 
                                text="💡 As câmeras são sincronizadas automaticamente com a plataforma",
                                font=(Theme.FONT_FAMILY, 9),
                                bg=Theme.BG_DARK, fg=Theme.FG_DARK)
            help_text.pack(side='right', padx=24, pady=14)
            
            # Carrega dispositivos já descobertos
            self.load_saved_devices()
        
        def load_saved_devices(self):
            """Carrega dispositivos já descobertos do banco de dados"""
            def load_thread():
                try:
                    devices = self.supabase.get_discovered_devices()
                    self.message_queue.put(('devices_loaded', devices))
                except Exception as e:
                    logger.error(f"Erro ao carregar dispositivos: {e}")
                    self.message_queue.put(('devices_loaded', []))
            
            threading.Thread(target=load_thread, daemon=True).start()
        
        def do_logout(self):
            self.supabase.logout()
            self.show_login_screen()
        
        def start_scan(self):
            try:
                logger.info("Iniciando scan...")
                self.scan_btn.set_enabled(False)
                self.stop_btn.set_enabled(True)
                
                # Clear cameras
                for card in self.camera_cards:
                    card.destroy()
                self.camera_cards = []
                
                try:
                    self.empty_state.pack_forget()
                except Exception as e:
                    logger.debug(f"empty_state pack_forget: {e}")
                
                # Show progress card - reorganiza layout
                try:
                    if not self.progress_card_visible:
                        self.cameras_header.pack_forget()
                        self.cameras_container.pack_forget()
                        
                        self.progress_card.pack(fill='x', pady=(0, 16))
                        self.cameras_header.pack(fill='x', pady=(0, 16))
                        self.cameras_container.pack(fill='both', expand=True)
                        self.progress_card_visible = True
                        logger.info("Progress card exibido")
                except Exception as e:
                    logger.error(f"Erro ao exibir progress card: {e}")
                
                # Reset progress card
                self.progress_card.title_label.config(text="🔍 Escaneando Rede...")
                self.progress_card.percent_label.config(text="0%", fg=Theme.PRIMARY)
                self.progress_card.progress_bar.configure(bg=Theme.PRIMARY, width=0)
                self.progress_card.scanned_value.config(text="0")
                self.progress_card.found_value.config(text="0")
                self.progress_card.remaining_value.config(text="254")
                self.progress_card.current_ip_label.config(text="Iniciando varredura...")
                
                self.status_label.config(text="🔍 Varredura em andamento...", fg=Theme.PRIMARY)
                self.camera_count.config(text="")
                
                # Força atualização visual
                self.root.update_idletasks()
                self.root.update()
                
                logger.info("Iniciando thread de scan...")
                threading.Thread(target=self._run_scan, daemon=True).start()
                
            except Exception as e:
                logger.error(f"Erro em start_scan: {e}")
                self.toast.show(f"Erro ao iniciar scan: {e}", 'error')
                self.scan_btn.set_enabled(True)
                self.stop_btn.set_enabled(False)
        
        def _run_scan(self):
            """Executa o scan em thread separada com tratamento de erros"""
            try:
                self.scanner.scan_network()
            except Exception as e:
                logger.error(f"Erro durante scan: {e}")
                self.message_queue.put(('scan_error', str(e)))
        
        def stop_scan(self):
            self.scanner.cancel_scan()
            self.scan_btn.set_enabled(True)
            self.stop_btn.set_enabled(False)
            self.status_label.config(text="⏹ Scan cancelado pelo usuário", fg=Theme.WARNING)
            self.progress_card.set_cancelled()
        
        def on_progress(self, data: Dict):
            self.message_queue.put(('scan_progress', data))
        
        def on_device_found(self, device: Dict):
            self.message_queue.put(('device_found', device))
        
        def minimize_to_tray(self):
            """Minimiza para a bandeja do sistema (system tray)"""
            self.root.withdraw()
            self.minimized_to_tray = True
            
            if TRAY_AVAILABLE and not hasattr(self, 'tray_icon') or not self.tray_icon:
                self.create_tray_icon()
        
        def create_tray_icon(self):
            """Cria o ícone na bandeja do sistema com menu de contexto"""
            if not TRAY_AVAILABLE:
                return
            
            # Cria imagem do ícone (camera azul)
            def create_icon_image():
                size = 64
                image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
                draw = ImageDraw.Draw(image)
                
                # Fundo circular azul
                draw.ellipse([4, 4, size-4, size-4], fill='#0ea5e9')
                
                # Símbolo de câmera simples (retângulo + círculo)
                draw.rectangle([16, 22, 48, 42], fill='white')
                draw.ellipse([26, 26, 38, 38], fill='#0ea5e9')
                draw.polygon([(48, 26), (56, 20), (56, 44), (48, 38)], fill='white')
                
                return image
            
            def on_show(icon, item):
                """Restaura a janela"""
                self.root.after(0, self.restore_from_tray)
            
            def on_quit(icon, item):
                """Fecha completamente o aplicativo"""
                icon.stop()
                self.root.after(0, self.quit_app)
            
            # Menu de contexto
            menu = pystray.Menu(
                pystray.MenuItem("📹 Abrir Camera Scanner", on_show, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("❌ Fechar e Encerrar", on_quit)
            )
            
            # Cria ícone
            self.tray_icon = pystray.Icon(
                "camera_scanner",
                create_icon_image(),
                "Camera Scanner Agent",
                menu
            )
            
            # Roda em thread separada
            tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
            tray_thread.start()
        
        def restore_from_tray(self):
            """Restaura a janela da bandeja do sistema"""
            self.minimized_to_tray = False
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        
        def quit_app(self):
            """Encerra completamente o aplicativo"""
            logger.info("Encerrando aplicativo...")
            
            # Envia notificação de desconexão ANTES de fechar
            if self.supabase and self.supabase.is_logged_in():
                logger.info("Enviando notificação de desconexão...")
                self.supabase.logout()  # Isso chama _send_disconnect internamente
            
            if hasattr(self, 'tray_icon') and self.tray_icon:
                try:
                    self.tray_icon.stop()
                except:
                    pass
            
            # Para o scanner se estiver rodando
            if self.scanner and hasattr(self.scanner, 'is_scanning') and self.scanner.is_scanning:
                self.scanner.stop_scan()
            
            self.root.destroy()
            sys.exit(0)
        
        def process_messages(self):
            try:
                while True:
                    msg = self.message_queue.get_nowait()
                    
                    # Suporta tuplas de 2 ou 4 elementos
                    if len(msg) == 2:
                        msg_type, data = msg
                        extra1, extra2 = None, None
                    elif len(msg) == 4:
                        msg_type, data, extra1, extra2 = msg
                    else:
                        continue
                    
                    # Mensagens da tela de requisitos
                    if msg_type == 'req_update':
                        key, status, message = data, extra1, extra2
                        self.update_requirement_status(key, status, message)
                    
                    elif msg_type == 'req_progress':
                        percent, message = data, extra1
                        self.update_requirement_progress(percent, message)
                    
                    elif msg_type == 'req_complete':
                        self.requirements_checked = True
                        self.show_login_screen()
                    
                    elif msg_type == 'req_error':
                        # Erro crítico - mostra mensagem e botão para tentar novamente
                        key, error_msg = data, extra1
                        if hasattr(self, 'toast'):
                            self.toast.show(error_msg, 'error', duration=10000)
                        # Adiciona botão para tentar novamente
                        if hasattr(self, 'continue_btn'):
                            self.continue_btn.set_text("Tentar Novamente")
                            self.continue_btn.set_enabled(True)
                            self.continue_btn.command = self.show_requirements_screen
                    
                    # Mensagens existentes
                    elif msg_type == 'login_success':
                        self.show_main_screen()
                        
                    elif msg_type == 'login_error':
                        self.toast.show(data, 'error')
                        self.login_btn.set_enabled(True)
                        self.login_btn.set_text("Entrar na conta")
                    
                    elif msg_type == 'devices_loaded':
                        # Dispositivos carregados do banco
                        devices = data or []
                        
                        if devices:
                            self.empty_state.pack_forget()
                            for device in devices:
                                card = CameraCard(self.cameras_list, device, supabase_client=self.supabase)
                                card.pack(fill='x', pady=(0, 12))
                                self.camera_cards.append(card)
                            
                            self.camera_count.config(text=f"{len(devices)} câmera(s)")
                            self.status_label.config(
                                text=f"✓ {len(devices)} câmera(s) carregada(s) do último scan",
                                fg=Theme.SUCCESS
                            )
                        else:
                            self.empty_state.pack(fill='x', pady=40)
                            self.status_label.config(text="✓ Pronto para escanear", fg=Theme.FG_MUTED)
                        
                    elif msg_type == 'scan_progress':
                        progress = data.get('progress', 0)
                        found = data.get('found', 0)
                        scanned = data.get('scanned', 0)
                        total = data.get('total', 0)
                        current_ip = data.get('current_ip', '')
                        
                        # Update progress card
                        self.progress_card.update_progress(progress, scanned, total, found)
                        self.camera_count.config(text=f"{found} câmera(s)")
                        
                        if current_ip:
                            self.progress_card.current_ip_label.config(text=f"Verificando: {current_ip}")
                        
                        if data.get('status') == 'completed':
                            self.scan_btn.set_enabled(True)
                            self.stop_btn.set_enabled(False)
                            self.progress_card.set_completed(found)
                            
                            if found > 0:
                                self.status_label.config(
                                    text=f"✅ Scan concluído! {found} câmera(s) encontrada(s) e sincronizada(s)",
                                    fg=Theme.SUCCESS
                                )
                                self.toast.show(f"{found} câmera(s) encontrada(s)!", 'success')
                            else:
                                self.status_label.config(
                                    text="⚠ Nenhuma câmera encontrada nesta rede",
                                    fg=Theme.WARNING
                                )
                                self.empty_state.pack(fill='x', pady=40)
                    
                    elif msg_type == 'device_found':
                        device = data
                        try:
                            self.empty_state.pack_forget()
                        except:
                            pass
                        
                        card = CameraCard(self.cameras_list, device, supabase_client=self.supabase)
                        card.pack(fill='x', pady=(0, 12))
                        self.camera_cards.append(card)
                        
                        # Atualiza IP atual no progress
                        self.progress_card.current_ip_label.config(
                            text=f"✓ Encontrada: {device.get('ip', '')} ({device.get('brand_name', '')})"
                        )
                    
                    elif msg_type == 'scan_error':
                        self.scan_btn.set_enabled(True)
                        self.stop_btn.set_enabled(False)
                        self.status_label.config(text=f"❌ Erro: {data}", fg=Theme.ERROR)
                        self.toast.show(f"Erro no scan: {data}", 'error')
                        
            except queue.Empty:
                pass
            except Exception as e:
                logger.error(f"Erro em process_messages: {e}")
            
            self.root.after(100, self.process_messages)
    
    # Configure ttk style
    root = tk.Tk()
    
    style = ttk.Style()
    style.theme_use('clam')
    style.configure('TScrollbar', 
                   background=Theme.BG_CARD,
                   troughcolor=Theme.BG_SECONDARY,
                   arrowcolor=Theme.FG_MUTED)
    
    app = CameraScannerApp(root)
    
    # Handler para fechar gracefully com Ctrl+C ou sinais do sistema
    import signal
    
    def signal_handler(signum, frame):
        logger.info(f"Sinal recebido: {signum}. Encerrando...")
        if app.supabase and app.supabase.is_logged_in():
            app.supabase.logout()
        root.quit()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Também registra atexit para garantir desconexão
    import atexit
    
    def cleanup():
        logger.info("Cleanup: encerrando conexão...")
        if app.supabase and app.supabase.is_logged_in():
            app.supabase.logout()
    
    atexit.register(cleanup)
    
    root.mainloop()


def setup_autostart():
    """Configura autostart"""
    import platform
    system = platform.system()
    app_path = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)
    
    try:
        if system == 'Windows':
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "CameraScannerAgent", 0, winreg.REG_SZ, f'"{app_path}"')
            winreg.CloseKey(key)
            print("✓ Autostart configurado para Windows")
            
        elif system == 'Linux':
            autostart_dir = os.path.expanduser("~/.config/autostart")
            os.makedirs(autostart_dir, exist_ok=True)
            with open(os.path.join(autostart_dir, "camera-scanner.desktop"), 'w') as f:
                f.write(f"[Desktop Entry]\nType=Application\nName=Camera Scanner Agent\nExec={app_path}\nHidden=false\n")
            print("✓ Autostart configurado para Linux")
            
        elif system == 'Darwin':
            plist = os.path.expanduser("~/Library/LaunchAgents/com.camerascanner.agent.plist")
            with open(plist, 'w') as f:
                f.write(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.camerascanner.agent</string>
<key>ProgramArguments</key><array><string>{app_path}</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>''')
            os.system(f"launchctl load {plist}")
            print("✓ Autostart configurado para macOS")
    except Exception as e:
        print(f"⚠ Erro: {e}")


def run_with_bridge():
    """Executa o app com o servidor WebSocket de bridge em background"""
    import asyncio
    
    # Tenta importar o servidor WebSocket
    try:
        from websocket_server import BridgeWebSocketServer
        BRIDGE_AVAILABLE = True
    except ImportError:
        BRIDGE_AVAILABLE = False
        logger.warning("websocket_server não disponível - bridge desabilitado")
    
    # Thread para rodar o servidor WebSocket
    def run_ws_server():
        if not BRIDGE_AVAILABLE:
            return
        
        try:
            server = BridgeWebSocketServer()
            asyncio.run(server.start())
        except Exception as e:
            logger.error(f"Erro no servidor WebSocket: {e}")
    
    # Inicia servidor WebSocket em thread separada
    if BRIDGE_AVAILABLE:
        ws_thread = threading.Thread(target=run_ws_server, daemon=True)
        ws_thread.start()
        logger.info("✓ Bridge WebSocket iniciado em ws://127.0.0.1:8765")
    
    # Executa a GUI principal
    run_gui()


if __name__ == '__main__':
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == '--install-autostart':
            setup_autostart()
        elif cmd == '--bridge-only':
            # Roda apenas o servidor WebSocket (sem GUI)
            from websocket_server import run_server
            run_server()
        elif cmd == '--help':
            print("Camera Scanner Agent + Bridge")
            print("  --install-autostart  Inicia com o sistema")
            print("  --bridge-only        Roda apenas o servidor de bridge (sem GUI)")
            print("  --help               Mostra ajuda")
        else:
            print(f"Comando desconhecido: {cmd}")
    else:
        run_with_bridge()
