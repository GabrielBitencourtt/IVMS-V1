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
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
    print("⚠ pystray ou PIL não instalado. Ícone na bandeja do sistema não disponível.")

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


# ============= TEMA DA PLATAFORMA =============
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
    
    def __init__(self):
        self.url = SUPABASE_URL
        self.anon_key = SUPABASE_ANON_KEY
        self.access_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.user_email: Optional[str] = None
    
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
            return True
            
        except Exception as e:
            logger.error(f"Erro no login: {e}")
            raise
    
    def logout(self):
        """Faz logout"""
        self.access_token = None
        self.user_id = None
        self.user_email = None
    
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
        
        return self._request(
            "/rest/v1/discovered_devices?on_conflict=user_id,ip",
            method="POST",
            data=data,
            prefer_header="resolution=merge-duplicates"
        )
    
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
                                    self.supabase.save_discovered_device(device, network_range)
                                    logger.info(f"✓ Dispositivo salvo: {device['ip']} ({device['brand_name']})")
                                    
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
        """Card melhorado para exibir câmera encontrada"""
        def __init__(self, parent, device, on_copy_url=None, **kwargs):
            super().__init__(parent, bg=Theme.BG_CARD, **kwargs)
            
            self.device = device
            self.on_copy_url = on_copy_url
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
            
            # Sync status on the right
            status_frame = tk.Frame(top_row, bg=Theme.BG_CARD)
            status_frame.pack(side='right')
            tk.Label(status_frame, text="✓ Sincronizado",
                    font=(Theme.FONT_FAMILY, 9, 'bold'),
                    bg=Theme.BG_CARD, fg=Theme.SUCCESS).pack()
            
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
            
            # === BOTTOM ROW: RTSP URL ===
            if device.get('suggested_url'):
                url_frame = tk.Frame(content, bg=Theme.BG_INPUT,
                                    highlightbackground=Theme.BORDER, highlightthickness=1)
                url_frame.pack(fill='x', pady=(12, 0))
                
                url_inner = tk.Frame(url_frame, bg=Theme.BG_INPUT)
                url_inner.pack(fill='x', padx=12, pady=8)
                
                tk.Label(url_inner, text="📡",
                        font=(Theme.FONT_FAMILY, 10),
                        bg=Theme.BG_INPUT, fg=Theme.FG_MUTED).pack(side='left')
                
                # URL truncada
                url = device['suggested_url']
                display_url = url if len(url) < 60 else url[:57] + "..."
                
                url_text = tk.Label(url_inner, text=display_url,
                        font=(Theme.FONT_MONO, 9),
                        bg=Theme.BG_INPUT, fg=Theme.FG_SECONDARY)
                url_text.pack(side='left', padx=(8, 0))
                
                # Botão copiar
                copy_btn = tk.Label(url_inner, text="📋 Copiar",
                                   font=(Theme.FONT_FAMILY, 9),
                                   bg=Theme.BG_INPUT, fg=Theme.PRIMARY, cursor='hand2')
                copy_btn.pack(side='right')
                copy_btn.bind('<Button-1>', lambda e: self._copy_url(url, copy_btn))
                copy_btn.bind('<Enter>', lambda e: copy_btn.config(fg=Theme.PRIMARY_HOVER))
                copy_btn.bind('<Leave>', lambda e: copy_btn.config(fg=Theme.PRIMARY))
        
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
            
            # Clientes
            self.supabase = SupabaseClient()
            self.scanner = None
            
            # Estado
            self.message_queue = queue.Queue()
            self.minimized_to_tray = False
            self.camera_cards = []
            
            # Configura fechamento
            self.root.protocol("WM_DELETE_WINDOW", self.minimize_to_tray)
            
            self.show_login_screen()
            self.process_messages()
        
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
            logo_container = tk.Frame(left_content, bg=Theme.PRIMARY, width=100, height=100)
            logo_container.pack(pady=(0, 32))
            logo_container.pack_propagate(False)
            
            # Canvas para logo arredondado
            logo_canvas = tk.Canvas(logo_container, width=100, height=100, 
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
            if hasattr(self, 'tray_icon') and self.tray_icon:
                try:
                    self.tray_icon.stop()
                except:
                    pass
            
            # Para o scanner se estiver rodando
            if self.scanner and self.scanner.is_scanning:
                self.scanner.stop_scan()
            
            self.root.destroy()
            sys.exit(0)
        
        def process_messages(self):
            try:
                while True:
                    msg_type, data = self.message_queue.get_nowait()
                    
                    if msg_type == 'login_success':
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
                                card = CameraCard(self.cameras_list, device)
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
                        
                        card = CameraCard(self.cameras_list, device)
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
