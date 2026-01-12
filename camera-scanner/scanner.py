#!/usr/bin/env python3
"""
Scanner de Câmeras na Rede Local
Detecta câmeras IP via escaneamento de portas e identificação de marca
Gera arquivo JSON para importar no site
"""

import socket
import json
import sys
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import ssl

# Portas comuns de câmeras IP
CAMERA_PORTS = [554, 80, 8080, 8000, 8888, 37777, 34567]

# Templates RTSP por marca
RTSP_TEMPLATES = {
    "hikvision": [
        "rtsp://{user}:{pass}@{ip}:554/Streaming/Channels/101",
        "rtsp://{user}:{pass}@{ip}:554/Streaming/Channels/1",
        "rtsp://{user}:{pass}@{ip}:554/h264/ch1/main/av_stream"
    ],
    "dahua": [
        "rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel=1&subtype=0",
        "rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel=1&subtype=1"
    ],
    "intelbras": [
        "rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel=1&subtype=0",
        "rtsp://{user}:{pass}@{ip}:554/Streaming/Channels/101"
    ],
    "axis": [
        "rtsp://{user}:{pass}@{ip}:554/axis-media/media.amp",
        "rtsp://{user}:{pass}@{ip}:554/mpeg4/media.amp"
    ],
    "vivotek": [
        "rtsp://{user}:{pass}@{ip}:554/live.sdp",
        "rtsp://{user}:{pass}@{ip}:554/video.mp4"
    ],
    "foscam": [
        "rtsp://{user}:{pass}@{ip}:554/videoMain",
        "rtsp://{user}:{pass}@{ip}:88/videoMain"
    ],
    "reolink": [
        "rtsp://{user}:{pass}@{ip}:554/h264Preview_01_main",
        "rtsp://{user}:{pass}@{ip}:554/h264Preview_01_sub"
    ],
    "generic": [
        "rtsp://{user}:{pass}@{ip}:554/stream1",
        "rtsp://{user}:{pass}@{ip}:554/1",
        "rtsp://{user}:{pass}@{ip}:554/"
    ]
}

# Credenciais padrão por marca
DEFAULT_CREDENTIALS = {
    "hikvision": [["admin", "admin"], ["admin", "12345"], ["admin", ""]],
    "dahua": [["admin", "admin"], ["admin", ""]],
    "intelbras": [["admin", "admin"], ["admin", ""]],
    "axis": [["root", "pass"], ["root", "root"]],
    "vivotek": [["root", ""], ["admin", ""]],
    "foscam": [["admin", ""], ["admin", "admin"]],
    "reolink": [["admin", ""], ["admin", "admin"]],
    "generic": [["admin", "admin"], ["admin", ""], ["root", "root"], ["user", "user"]]
}


def get_local_ip():
    """Detecta o IP local da máquina"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.1.1"


def get_network_range(ip):
    """Retorna o range da rede baseado no IP"""
    parts = ip.split(".")
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def check_port(ip, port, timeout=1):
    """Verifica se uma porta está aberta"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def detect_camera_brand(ip):
    """Tenta detectar a marca da câmera via HTTP"""
    brand = "generic"
    
    endpoints = [
        (80, "/", ["hikvision", "HIKVISION", "Hikvision"], "hikvision"),
        (80, "/", ["dahua", "DAHUA", "Dahua"], "dahua"),
        (80, "/", ["intelbras", "INTELBRAS", "Intelbras"], "intelbras"),
        (80, "/", ["axis", "AXIS", "Axis"], "axis"),
        (80, "/", ["vivotek", "VIVOTEK", "Vivotek"], "vivotek"),
        (80, "/", ["foscam", "FOSCAM", "Foscam"], "foscam"),
        (80, "/", ["reolink", "REOLINK", "Reolink"], "reolink"),
        (80, "/ISAPI/System/deviceInfo", ["Hikvision", "hikvision"], "hikvision"),
        (80, "/cgi-bin/magicBox.cgi?action=getSystemInfo", ["Dahua", "dahua"], "dahua"),
    ]
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    for port, path, keywords, brand_name in endpoints:
        if not check_port(ip, port, timeout=0.5):
            continue
        try:
            url = f"http://{ip}:{port}{path}"
            req = urllib.request.Request(url, headers={"User-Agent": "CameraScanner/1.0"})
            with urllib.request.urlopen(req, timeout=2, context=ctx) as response:
                content = response.read().decode("utf-8", errors="ignore")
                for keyword in keywords:
                    if keyword in content:
                        brand = brand_name
                        break
        except Exception:
            pass
        
        if brand != "generic":
            break
    
    return brand


def check_rtsp(ip, port=554, timeout=2):
    """Verifica se a porta RTSP responde"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, port))
        sock.send(b"OPTIONS rtsp://" + ip.encode() + b":554/ RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        response = sock.recv(1024)
        sock.close()
        return b"RTSP" in response or b"200" in response
    except Exception:
        return False


def scan_ip(ip):
    """Escaneia um IP específico para detectar câmera"""
    result = {
        "ip": ip,
        "is_camera": False,
        "open_ports": [],
        "brand": None,
        "rtsp_available": False,
        "rtsp_templates": [],
        "default_credentials": []
    }
    
    # Verifica portas abertas
    for port in CAMERA_PORTS:
        if check_port(ip, port, timeout=0.5):
            result["open_ports"].append(port)
    
    if not result["open_ports"]:
        return None
    
    # Verifica RTSP
    if 554 in result["open_ports"]:
        result["rtsp_available"] = check_rtsp(ip)
    
    # Tenta detectar a marca
    if 80 in result["open_ports"] or 8080 in result["open_ports"]:
        brand = detect_camera_brand(ip)
        result["brand"] = brand
        result["rtsp_templates"] = RTSP_TEMPLATES.get(brand, RTSP_TEMPLATES["generic"])
        result["default_credentials"] = DEFAULT_CREDENTIALS.get(brand, DEFAULT_CREDENTIALS["generic"])
        result["is_camera"] = True
    elif result["rtsp_available"]:
        result["brand"] = "generic"
        result["rtsp_templates"] = RTSP_TEMPLATES["generic"]
        result["default_credentials"] = DEFAULT_CREDENTIALS["generic"]
        result["is_camera"] = True
    
    return result if result["is_camera"] else None


def scan_network(network_range=None):
    """Escaneia a rede em busca de câmeras"""
    if network_range is None:
        local_ip = get_local_ip()
        network_range = get_network_range(local_ip)
    
    cameras = []
    total = 254
    scanned = 0
    
    print(f"\n🔍 Escaneando rede {network_range}.1 - {network_range}.254...")
    print("   Isso pode levar alguns minutos...\n")
    
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {}
        for i in range(1, 255):
            ip = f"{network_range}.{i}"
            future = executor.submit(scan_ip, ip)
            futures[future] = ip
        
        for future in as_completed(futures):
            scanned += 1
            ip = futures[future]
            
            # Barra de progresso simples
            progress = int((scanned / total) * 50)
            bar = "█" * progress + "░" * (50 - progress)
            print(f"\r   [{bar}] {scanned}/{total}", end="", flush=True)
            
            try:
                result = future.result()
                if result:
                    cameras.append(result)
                    print(f"\n   ✅ Câmera encontrada: {result['ip']} ({result['brand']})")
            except Exception:
                pass
    
    print(f"\n\n📷 Total de câmeras encontradas: {len(cameras)}")
    return cameras


def save_results(cameras, filename=None):
    """Salva resultados em arquivo JSON"""
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"cameras_{timestamp}.json"
    
    result = {
        "version": "1.0",
        "scan_date": datetime.now().isoformat(),
        "local_ip": get_local_ip(),
        "network_range": get_network_range(get_local_ip()),
        "cameras_found": len(cameras),
        "cameras": cameras
    }
    
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    
    return filename


def print_banner():
    """Exibe banner do scanner"""
    print("""
╔════════════════════════════════════════════════════════════╗
║           🎥 Scanner de Câmeras na Rede Local              ║
╠════════════════════════════════════════════════════════════╣
║  Este script detecta câmeras IP na sua rede local          ║
║  e gera um arquivo JSON para importar no site              ║
╚════════════════════════════════════════════════════════════╝
""")


def print_results(cameras):
    """Exibe resumo das câmeras encontradas"""
    if not cameras:
        print("\n❌ Nenhuma câmera encontrada na rede.")
        print("   Dicas:")
        print("   - Verifique se as câmeras estão ligadas e conectadas")
        print("   - Confirme se você está na mesma rede das câmeras")
        print("   - Algumas câmeras podem ter portas não-padrão")
        return
    
    print("\n" + "=" * 60)
    print("📋 CÂMERAS ENCONTRADAS")
    print("=" * 60)
    
    for i, cam in enumerate(cameras, 1):
        print(f"\n{i}. {cam['ip']}")
        print(f"   Marca: {cam['brand'].title()}")
        print(f"   Portas: {', '.join(map(str, cam['open_ports']))}")
        print(f"   RTSP: {'Sim' if cam['rtsp_available'] else 'Não detectado'}")
        if cam['default_credentials']:
            creds = [f"{u}:{p}" for u, p in cam['default_credentials'][:2]]
            print(f"   Credenciais comuns: {', '.join(creds)}")


if __name__ == "__main__":
    print_banner()
    
    local_ip = get_local_ip()
    network = get_network_range(local_ip)
    
    print(f"   IP Local detectado: {local_ip}")
    print(f"   Rede a escanear: {network}.0/24")
    
    # Permite especificar rede diferente
    if len(sys.argv) > 1:
        network = sys.argv[1]
        print(f"   Rede especificada: {network}.0/24")
    
    # Executa scan
    cameras = scan_network(network)
    
    # Exibe resultados
    print_results(cameras)
    
    # Salva arquivo JSON
    if cameras:
        filename = save_results(cameras)
        abs_path = os.path.abspath(filename)
        
        print("\n" + "=" * 60)
        print("💾 ARQUIVO GERADO")
        print("=" * 60)
        print(f"\n   Arquivo: {filename}")
        print(f"   Caminho: {abs_path}")
        print("\n   📤 Para importar no site:")
        print("      1. Abra o site e vá em 'Adicionar Câmera'")
        print("      2. Clique na aba 'Importar'")
        print("      3. Selecione este arquivo JSON")
        print("      4. Configure as credenciais de cada câmera")
    
    print("\n")
