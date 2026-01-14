#!/usr/bin/env python3
"""
Build Script - Gera executável standalone do Camera Scanner Agent
O executável final não requer Python instalado no computador do usuário
"""

import os
import sys
import platform
import subprocess
import shutil


def install_requirements():
    """Instala dependências necessárias para o build"""
    print("📦 Verificando dependências do build...")
    
    # Dependências do app
    deps = [
        ("pystray", "pystray"),
        ("PIL", "Pillow"),
        ("websockets", "websockets"),
    ]
    
    for module_name, pip_name in deps:
        try:
            __import__(module_name)
            print(f"  ✓ {pip_name} já instalado")
        except ImportError:
            print(f"  ⬇ Instalando {pip_name}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "-q"])
            print(f"  ✓ {pip_name} instalado")
    
    # PyInstaller para o build
    try:
        import PyInstaller
        print("  ✓ PyInstaller já instalado")
    except ImportError:
        print("  ⬇ Instalando PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])
        print("  ✓ PyInstaller instalado")


def build_executable():
    """Gera o executável standalone"""
    system = platform.system()
    
    print(f"\n{'='*50}")
    print(f"🔨 Building Camera Scanner Agent")
    print(f"   Sistema: {system}")
    print(f"   Python: {sys.version.split()[0]}")
    print(f"{'='*50}\n")
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app_path = os.path.join(base_dir, "app.py")
    dist_dir = os.path.join(base_dir, "dist")
    
    # Limpa builds anteriores
    for folder in ["build", "dist", "__pycache__"]:
        path = os.path.join(base_dir, folder)
        if os.path.exists(path):
            shutil.rmtree(path)
    
    spec_file = os.path.join(base_dir, "CameraScannerAgent.spec")
    if os.path.exists(spec_file):
        os.remove(spec_file)
    
    # Configuração PyInstaller
    options = [
        app_path,
        "--name=CameraScannerAgent",
        "--onefile",              # Tudo em um único arquivo
        "--clean",
        f"--distpath={dist_dir}",
        f"--workpath={os.path.join(base_dir, 'build')}",
        f"--specpath={base_dir}",
        "--noconfirm",
        # Hidden imports para system tray
        "--hidden-import=pystray",
        "--hidden-import=pystray._win32",
        "--hidden-import=PIL",
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.ImageDraw",
        # WebSocket e bridge
        "--hidden-import=websockets",
        "--hidden-import=websockets.server",
        "--hidden-import=websockets.client",
        # Requests para ONVIF
        "--hidden-import=requests",
        # Módulos do projeto
        f"--add-data={os.path.join(base_dir, 'stream_bridge.py')}{os.pathsep}.",
        f"--add-data={os.path.join(base_dir, 'websocket_server.py')}{os.pathsep}.",
        f"--add-data={os.path.join(base_dir, 'ffmpeg_installer.py')}{os.pathsep}.",
        f"--add-data={os.path.join(base_dir, 'cloud_agent.py')}{os.pathsep}.",
        f"--add-data={os.path.join(base_dir, 'onvif_events.py')}{os.pathsep}.",
        f"--add-data={os.path.join(base_dir, 'rtsp_tester.py')}{os.pathsep}.",
        f"--add-data={os.path.join(base_dir, 'scanner.py')}{os.pathsep}.",
    ]
    
    # Opções específicas por plataforma
    if system == 'Windows':
        options.append("--windowed")  # Sem janela de console
        options.append("--uac-admin")  # Solicita admin se necessário
        icon_path = os.path.join(base_dir, "icon.ico")
        if os.path.exists(icon_path):
            options.append(f"--icon={icon_path}")
            
    elif system == 'Darwin':  # macOS
        options.append("--windowed")
        options.append("--osx-bundle-identifier=com.camerascanner.agent")
        icon_path = os.path.join(base_dir, "icon.icns")
        if os.path.exists(icon_path):
            options.append(f"--icon={icon_path}")
            
    else:  # Linux
        icon_path = os.path.join(base_dir, "icon.png")
        if os.path.exists(icon_path):
            options.append(f"--icon={icon_path}")
    
    print("🔧 Executando PyInstaller...")
    print(f"   Opções: {' '.join(options)}\n")
    
    try:
        import PyInstaller.__main__
        PyInstaller.__main__.run(options)
        
        # Verifica resultado
        if system == 'Windows':
            exe_name = "CameraScannerAgent.exe"
        else:
            exe_name = "CameraScannerAgent"
        
        exe_path = os.path.join(dist_dir, exe_name)
        
        if os.path.exists(exe_path):
            size_mb = os.path.getsize(exe_path) / (1024 * 1024)
            
            print(f"\n{'='*50}")
            print(f"✅ BUILD CONCLUÍDO COM SUCESSO!")
            print(f"{'='*50}")
            print(f"\n📁 Executável: {exe_path}")
            print(f"📊 Tamanho: {size_mb:.1f} MB")
            print(f"\n💡 O usuário só precisa baixar e executar este arquivo!")
            print(f"   Não precisa ter Python instalado.\n")
            
            return True
        else:
            print(f"\n❌ Executável não encontrado: {exe_path}")
            return False
            
    except Exception as e:
        print(f"\n❌ Erro no build: {e}")
        return False


def clean():
    """Remove arquivos de build"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    for folder in ["build", "dist", "__pycache__", "bundled_ffmpeg"]:
        path = os.path.join(base_dir, folder)
        if os.path.exists(path):
            shutil.rmtree(path)
            print(f"🗑 Removido: {path}")
    
    spec_file = os.path.join(base_dir, "CameraScannerAgent.spec")
    if os.path.exists(spec_file):
        os.remove(spec_file)
        print(f"🗑 Removido: {spec_file}")
    
    print("✓ Limpeza concluída")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'clean':
        clean()
    else:
        install_requirements()
        build_executable()
