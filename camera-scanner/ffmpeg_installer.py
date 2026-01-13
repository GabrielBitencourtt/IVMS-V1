#!/usr/bin/env python3
"""
FFmpeg Auto-Installer
Detecta e instala FFmpeg automaticamente no sistema
Funciona tanto em ambiente Python quanto em executáveis PyInstaller
"""

import os
import sys
import ssl
import shutil
import subprocess
import platform
import tempfile
import zipfile
import tarfile
import logging
from typing import Optional, Callable, Tuple
from pathlib import Path
import urllib.request

logger = logging.getLogger(__name__)


def get_ssl_context():
    """
    Retorna contexto SSL que funciona em executáveis PyInstaller.
    Usa múltiplos fallbacks para garantir funcionamento.
    """
    # Tenta contexto padrão primeiro
    try:
        ctx = ssl.create_default_context()
        return ctx
    except Exception:
        pass
    
    # Fallback: contexto sem verificação (funciona sempre)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        logger.warning("SSL: Usando modo sem verificação de certificado")
        return ctx
    except Exception:
        pass
    
    return None


# URLs de download do FFmpeg (com fallbacks)
FFMPEG_DOWNLOADS = {
    "Windows": {
        "urls": [
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
            "https://github.com/GyanD/codexffmpeg/releases/download/7.1/ffmpeg-7.1-essentials_build.zip",
        ],
        "executable": "ffmpeg.exe"
    },
    "Linux": {
        "urls": [
            "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
        ],
        "executable": "ffmpeg"
    },
    "Darwin": {
        "urls": [
            "https://evermeet.cx/ffmpeg/getrelease/zip",
            "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.0/darwin-x64.gz",
        ],
        "executable": "ffmpeg"
    }
}


class FFmpegInstaller:
    """Gerencia detecção e instalação do FFmpeg"""
    
    def __init__(self, progress_callback: Optional[Callable] = None):
        self.progress_callback = progress_callback
        self.system = platform.system()
        self.install_dir = self._get_install_dir()
        
    def _get_install_dir(self) -> Path:
        """Retorna o diretório de instalação do FFmpeg"""
        if self.system == "Windows":
            base = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")))
            return base / "CameraScanner" / "ffmpeg"
        else:
            return Path.home() / ".local" / "share" / "camera-scanner" / "ffmpeg"
    
    def _report_progress(self, message: str, percent: int = -1):
        """Reporta progresso da instalação"""
        logger.info(message)
        if self.progress_callback:
            self.progress_callback(message, percent)
    
    def find_ffmpeg(self) -> Optional[str]:
        """
        Procura FFmpeg no sistema.
        Retorna o caminho completo se encontrado, None caso contrário.
        """
        # 1. Verifica no PATH do sistema
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            logger.info(f"FFmpeg encontrado no PATH: {ffmpeg_path}")
            return ffmpeg_path
        
        # 2. Verifica no diretório de instalação local
        if self.system == "Windows":
            local_ffmpeg = self.install_dir / "bin" / "ffmpeg.exe"
        else:
            local_ffmpeg = self.install_dir / "ffmpeg"
        
        if local_ffmpeg.exists():
            logger.info(f"FFmpeg encontrado localmente: {local_ffmpeg}")
            return str(local_ffmpeg)
        
        # 3. Caminhos comuns no Windows
        if self.system == "Windows":
            common_paths = [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
                os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"),
            ]
            for path in common_paths:
                if os.path.isfile(path):
                    logger.info(f"FFmpeg encontrado em: {path}")
                    return path
        
        # 4. Caminhos comuns no macOS
        if self.system == "Darwin":
            brew_paths = [
                "/usr/local/bin/ffmpeg",
                "/opt/homebrew/bin/ffmpeg",
            ]
            for path in brew_paths:
                if os.path.isfile(path):
                    logger.info(f"FFmpeg encontrado em: {path}")
                    return path
        
        # 5. Caminhos comuns no Linux
        if self.system == "Linux":
            linux_paths = [
                "/usr/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
            ]
            for path in linux_paths:
                if os.path.isfile(path):
                    logger.info(f"FFmpeg encontrado em: {path}")
                    return path
        
        logger.warning("FFmpeg não encontrado no sistema")
        return None
    
    def get_ffmpeg_version(self, ffmpeg_path: str) -> Optional[str]:
        """Retorna a versão do FFmpeg"""
        try:
            result = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                first_line = result.stdout.split('\n')[0]
                return first_line
            return None
        except Exception as e:
            logger.error(f"Erro ao verificar versão do FFmpeg: {e}")
            return None
    
    def is_ffmpeg_available(self) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Verifica se FFmpeg está disponível.
        Retorna: (disponível, caminho, versão)
        """
        ffmpeg_path = self.find_ffmpeg()
        if not ffmpeg_path:
            return False, None, None
        
        version = self.get_ffmpeg_version(ffmpeg_path)
        return True, ffmpeg_path, version
    
    def _download_with_progress(self, url: str, dest_path: str) -> bool:
        """Baixa arquivo com progresso usando múltiplos métodos"""
        
        # Método 1: Tenta com PowerShell no Windows (mais confiável)
        if self.system == "Windows":
            try:
                self._report_progress("Baixando via PowerShell...", 0)
                
                ps_script = f'''
                $ProgressPreference = 'SilentlyContinue'
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -Uri "{url}" -OutFile "{dest_path}" -UseBasicParsing
                '''
                
                result = subprocess.run(
                    ["powershell", "-Command", ps_script],
                    capture_output=True,
                    text=True,
                    timeout=600
                )
                
                if result.returncode == 0 and os.path.exists(dest_path):
                    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
                    self._report_progress(f"Download concluído: {size_mb:.1f} MB", 100)
                    return True
                    
            except Exception as e:
                logger.warning(f"PowerShell falhou: {e}")
        
        # Método 2: Tenta com curl (disponível em Windows 10+, Linux, macOS)
        try:
            self._report_progress("Baixando via curl...", 0)
            
            result = subprocess.run(
                ["curl", "-L", "-o", dest_path, "--progress-bar", url],
                capture_output=True,
                timeout=600
            )
            
            if result.returncode == 0 and os.path.exists(dest_path):
                size_mb = os.path.getsize(dest_path) / (1024 * 1024)
                self._report_progress(f"Download concluído: {size_mb:.1f} MB", 100)
                return True
                
        except Exception as e:
            logger.warning(f"curl falhou: {e}")
        
        # Método 3: Tenta com urllib (fallback)
        try:
            self._report_progress("Baixando via Python...", 0)
            
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            })
            
            # Tenta com SSL padrão primeiro
            ssl_context = get_ssl_context()
            
            with urllib.request.urlopen(req, timeout=300, context=ssl_context) as response:
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                
                with open(dest_path, 'wb') as f:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            size_mb = downloaded / (1024 * 1024)
                            self._report_progress(f"Baixando: {size_mb:.1f} MB", percent)
            
            if os.path.exists(dest_path):
                self._report_progress("Download concluído", 100)
                return True
                
        except Exception as e:
            logger.error(f"urllib falhou: {e}")
            self._report_progress(f"Erro no download: {e}", -1)
        
        return False
    
    def _extract_windows(self, zip_path: str) -> Optional[str]:
        """Extrai FFmpeg do arquivo ZIP (Windows)"""
        try:
            self._report_progress("Extraindo arquivos...", 0)
            
            self.install_dir.mkdir(parents=True, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zf:
                root_dirs = [n for n in zf.namelist() if n.count('/') == 1 and n.endswith('/')]
                root_dir = root_dirs[0] if root_dirs else ""
                
                members = zf.namelist()
                total = len(members)
                
                for i, member in enumerate(members):
                    if 'bin/ffmpeg' in member or 'bin/ffprobe' in member:
                        target_path = member.replace(root_dir, "")
                        target_full = self.install_dir / target_path
                        
                        target_full.parent.mkdir(parents=True, exist_ok=True)
                        
                        with zf.open(member) as src, open(target_full, 'wb') as dst:
                            dst.write(src.read())
                    
                    if i % 10 == 0:
                        self._report_progress(f"Extraindo... {i}/{total}", int((i / total) * 100))
            
            ffmpeg_path = self.install_dir / "bin" / "ffmpeg.exe"
            if ffmpeg_path.exists():
                self._report_progress("Extração concluída", 100)
                return str(ffmpeg_path)
            
            return None
            
        except Exception as e:
            logger.error(f"Erro na extração: {e}")
            return None
    
    def _extract_linux(self, tar_path: str) -> Optional[str]:
        """Extrai FFmpeg do arquivo tar.xz (Linux)"""
        try:
            self._report_progress("Extraindo arquivos...", 0)
            
            self.install_dir.mkdir(parents=True, exist_ok=True)
            
            with tarfile.open(tar_path, 'r:xz') as tf:
                for member in tf.getmembers():
                    if member.name.endswith('/ffmpeg') or member.name.endswith('/ffprobe'):
                        member.name = os.path.basename(member.name)
                        tf.extract(member, self.install_dir)
            
            ffmpeg_path = self.install_dir / "ffmpeg"
            if ffmpeg_path.exists():
                os.chmod(ffmpeg_path, 0o755)
                self._report_progress("Extração concluída", 100)
                return str(ffmpeg_path)
            
            return None
            
        except Exception as e:
            logger.error(f"Erro na extração: {e}")
            return None
    
    def _extract_macos(self, zip_path: str) -> Optional[str]:
        """Extrai FFmpeg do arquivo ZIP (macOS)"""
        try:
            self._report_progress("Extraindo arquivos...", 0)
            
            self.install_dir.mkdir(parents=True, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(self.install_dir)
            
            ffmpeg_path = self.install_dir / "ffmpeg"
            if ffmpeg_path.exists():
                os.chmod(ffmpeg_path, 0o755)
                self._report_progress("Extração concluída", 100)
                return str(ffmpeg_path)
            
            return None
            
        except Exception as e:
            logger.error(f"Erro na extração: {e}")
            return None
    
    def _extract_gzip(self, gz_path: str) -> Optional[str]:
        """Extrai FFmpeg de arquivo .gz simples"""
        try:
            import gzip
            
            self._report_progress("Extraindo arquivo gzip...", 0)
            self.install_dir.mkdir(parents=True, exist_ok=True)
            
            ffmpeg_path = self.install_dir / "ffmpeg"
            
            with gzip.open(gz_path, 'rb') as f_in:
                with open(ffmpeg_path, 'wb') as f_out:
                    f_out.write(f_in.read())
            
            os.chmod(ffmpeg_path, 0o755)
            self._report_progress("Extração concluída", 100)
            return str(ffmpeg_path)
            
        except Exception as e:
            logger.error(f"Erro na extração gzip: {e}")
            return None
    
    def install_ffmpeg(self) -> Tuple[bool, Optional[str]]:
        """
        Instala o FFmpeg automaticamente.
        Tenta múltiplos URLs de download como fallback.
        """
        if self.system not in FFMPEG_DOWNLOADS:
            self._report_progress(f"Sistema não suportado: {self.system}")
            return False, None
        
        config = FFMPEG_DOWNLOADS[self.system]
        urls = config["urls"]
        
        for url_index, url in enumerate(urls):
            self._report_progress(f"Tentando fonte {url_index + 1} de {len(urls)}...")
            
            with tempfile.TemporaryDirectory() as temp_dir:
                if url.endswith('.tar.xz') or 'linux' in url.lower():
                    download_file = os.path.join(temp_dir, "ffmpeg.tar.xz")
                elif url.endswith('.gz'):
                    download_file = os.path.join(temp_dir, "ffmpeg.gz")
                else:
                    download_file = os.path.join(temp_dir, "ffmpeg.zip")
                
                self._report_progress(f"Baixando de: {url[:50]}...")
                if not self._download_with_progress(url, download_file):
                    self._report_progress(f"Falha no download, tentando próxima fonte...")
                    continue
                
                ffmpeg_path = None
                try:
                    if download_file.endswith('.tar.xz'):
                        ffmpeg_path = self._extract_linux(download_file)
                    elif download_file.endswith('.gz') and not download_file.endswith('.tar.gz'):
                        ffmpeg_path = self._extract_gzip(download_file)
                    elif self.system == "Windows":
                        ffmpeg_path = self._extract_windows(download_file)
                    else:
                        ffmpeg_path = self._extract_macos(download_file)
                except Exception as e:
                    self._report_progress(f"Erro na extração: {e}")
                    continue
                
                if ffmpeg_path:
                    self._report_progress(f"✓ FFmpeg instalado em: {ffmpeg_path}")
                    return True, ffmpeg_path
                else:
                    self._report_progress(f"Falha na extração, tentando próxima fonte...")
        
        self._report_progress("❌ Todas as fontes de download falharam")
        return False, None
    
    def ensure_ffmpeg(self) -> Tuple[bool, Optional[str]]:
        """
        Garante que o FFmpeg está disponível.
        Se não estiver, tenta instalar automaticamente.
        """
        available, path, version = self.is_ffmpeg_available()
        
        if available:
            self._report_progress(f"✓ FFmpeg já instalado: {version}")
            return True, path
        
        self._report_progress("FFmpeg não encontrado. Instalando...")
        success, path = self.install_ffmpeg()
        
        return success, path


def check_and_install_ffmpeg(progress_callback: Optional[Callable] = None) -> Tuple[bool, Optional[str]]:
    """
    Função de conveniência para verificar e instalar FFmpeg.
    """
    installer = FFmpegInstaller(progress_callback)
    return installer.ensure_ffmpeg()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    def progress(msg, pct):
        if pct >= 0:
            print(f"[{pct:3d}%] {msg}")
        else:
            print(f"       {msg}")
    
    print("=" * 50)
    print("FFmpeg Auto-Installer")
    print("=" * 50)
    
    installer = FFmpegInstaller(progress)
    
    print("\n🔍 Verificando FFmpeg...")
    success, path = installer.ensure_ffmpeg()
    
    if success:
        print(f"\n✅ FFmpeg pronto para uso!")
        print(f"   Caminho: {path}")
        
        version = installer.get_ffmpeg_version(path)
        if version:
            print(f"   Versão: {version}")
    else:
        print("\n❌ Não foi possível instalar o FFmpeg automaticamente.")
        print("\n📋 Instalação manual:")
        
        system = platform.system()
        if system == "Windows":
            print("   1. Baixe de: https://www.gyan.dev/ffmpeg/builds/")
            print("   2. Extraia para C:\\ffmpeg")
            print("   3. Adicione C:\\ffmpeg\\bin ao PATH do sistema")
        elif system == "Darwin":
            print("   Execute: brew install ffmpeg")
        else:
            print("   Execute: sudo apt install ffmpeg")
