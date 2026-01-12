#!/usr/bin/env python3
"""
FFmpeg Auto-Installer
Detecta e instala FFmpeg automaticamente no sistema
"""

import os
import sys
import shutil
import subprocess
import platform
import tempfile
import zipfile
import tarfile
import logging
from pathlib import Path
from typing import Optional, Tuple, Callable
import urllib.request

logger = logging.getLogger(__name__)

# URLs de download do FFmpeg
FFMPEG_DOWNLOADS = {
    "Windows": {
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "executable": "ffmpeg.exe"
    },
    "Linux": {
        "url": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        "executable": "ffmpeg"
    },
    "Darwin": {  # macOS
        "url": "https://evermeet.cx/ffmpeg/getrelease/zip",
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
            # Instala em AppData/Local/CameraScanner/ffmpeg
            base = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")))
            return base / "CameraScanner" / "ffmpeg"
        else:
            # Linux/macOS: ~/.local/share/camera-scanner/ffmpeg
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
        
        # 4. Caminhos comuns no macOS (Homebrew)
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
                # Extrai versão da primeira linha
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
        """Baixa arquivo com progresso"""
        try:
            self._report_progress(f"Conectando a {url[:50]}...", 0)
            
            # Cria request com headers
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            
            with urllib.request.urlopen(req, timeout=60) as response:
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                chunk_size = 1024 * 1024  # 1MB chunks
                
                with open(dest_path, 'wb') as f:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            size_mb = downloaded / (1024 * 1024)
                            total_mb = total_size / (1024 * 1024)
                            self._report_progress(
                                f"Baixando FFmpeg: {size_mb:.1f}/{total_mb:.1f} MB",
                                percent
                            )
            
            self._report_progress("Download concluído", 100)
            return True
            
        except Exception as e:
            logger.error(f"Erro no download: {e}")
            self._report_progress(f"Erro no download: {e}", -1)
            return False
    
    def _extract_windows(self, zip_path: str) -> Optional[str]:
        """Extrai FFmpeg do arquivo ZIP (Windows)"""
        try:
            self._report_progress("Extraindo arquivos...", 0)
            
            # Cria diretório de instalação
            self.install_dir.mkdir(parents=True, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # Encontra o diretório raiz do ZIP
                root_dirs = [n for n in zf.namelist() if n.count('/') == 1 and n.endswith('/')]
                root_dir = root_dirs[0] if root_dirs else ""
                
                members = zf.namelist()
                total = len(members)
                
                for i, member in enumerate(members):
                    # Extrai apenas bin/ffmpeg.exe e bin/ffprobe.exe
                    if 'bin/ffmpeg' in member or 'bin/ffprobe' in member:
                        # Remove o diretório raiz do caminho
                        target_path = member.replace(root_dir, "")
                        target_full = self.install_dir / target_path
                        
                        target_full.parent.mkdir(parents=True, exist_ok=True)
                        
                        with zf.open(member) as src, open(target_full, 'wb') as dst:
                            dst.write(src.read())
                    
                    if i % 10 == 0:
                        self._report_progress(
                            f"Extraindo... {i}/{total}",
                            int((i / total) * 100)
                        )
            
            ffmpeg_path = self.install_dir / "bin" / "ffmpeg.exe"
            if ffmpeg_path.exists():
                self._report_progress("Extração concluída", 100)
                return str(ffmpeg_path)
            
            return None
            
        except Exception as e:
            logger.error(f"Erro na extração: {e}")
            self._report_progress(f"Erro na extração: {e}", -1)
            return None
    
    def _extract_linux(self, tar_path: str) -> Optional[str]:
        """Extrai FFmpeg do arquivo tar.xz (Linux)"""
        try:
            self._report_progress("Extraindo arquivos...", 0)
            
            self.install_dir.mkdir(parents=True, exist_ok=True)
            
            with tarfile.open(tar_path, 'r:xz') as tf:
                members = tf.getmembers()
                
                for member in members:
                    if member.name.endswith('/ffmpeg') or member.name.endswith('/ffprobe'):
                        # Extrai apenas o executável
                        member.name = os.path.basename(member.name)
                        tf.extract(member, self.install_dir)
            
            ffmpeg_path = self.install_dir / "ffmpeg"
            if ffmpeg_path.exists():
                # Torna executável
                os.chmod(ffmpeg_path, 0o755)
                self._report_progress("Extração concluída", 100)
                return str(ffmpeg_path)
            
            return None
            
        except Exception as e:
            logger.error(f"Erro na extração: {e}")
            self._report_progress(f"Erro na extração: {e}", -1)
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
    
    def install_ffmpeg(self) -> Tuple[bool, Optional[str]]:
        """
        Instala o FFmpeg automaticamente.
        Retorna: (sucesso, caminho_ffmpeg)
        """
        if self.system not in FFMPEG_DOWNLOADS:
            self._report_progress(f"Sistema não suportado: {self.system}")
            return False, None
        
        config = FFMPEG_DOWNLOADS[self.system]
        url = config["url"]
        
        # Cria diretório temporário para download
        with tempfile.TemporaryDirectory() as temp_dir:
            if self.system == "Linux":
                download_file = os.path.join(temp_dir, "ffmpeg.tar.xz")
            else:
                download_file = os.path.join(temp_dir, "ffmpeg.zip")
            
            # Download
            self._report_progress("Iniciando download do FFmpeg...")
            if not self._download_with_progress(url, download_file):
                return False, None
            
            # Extração
            if self.system == "Windows":
                ffmpeg_path = self._extract_windows(download_file)
            elif self.system == "Linux":
                ffmpeg_path = self._extract_linux(download_file)
            else:  # macOS
                ffmpeg_path = self._extract_macos(download_file)
            
            if ffmpeg_path:
                self._report_progress(f"✓ FFmpeg instalado em: {ffmpeg_path}")
                return True, ffmpeg_path
            else:
                self._report_progress("❌ Falha na instalação do FFmpeg")
                return False, None
    
    def ensure_ffmpeg(self) -> Tuple[bool, Optional[str]]:
        """
        Garante que o FFmpeg está disponível.
        Se não estiver, tenta instalar automaticamente.
        Retorna: (disponível, caminho)
        """
        # Primeiro verifica se já existe
        available, path, version = self.is_ffmpeg_available()
        
        if available:
            self._report_progress(f"✓ FFmpeg já instalado: {version}")
            return True, path
        
        # Tenta instalar
        self._report_progress("FFmpeg não encontrado. Instalando...")
        success, path = self.install_ffmpeg()
        
        return success, path


def check_and_install_ffmpeg(progress_callback: Optional[Callable] = None) -> Tuple[bool, Optional[str]]:
    """
    Função de conveniência para verificar e instalar FFmpeg.
    
    Args:
        progress_callback: Função callback(message, percent) para progresso
    
    Returns:
        (sucesso, caminho_ffmpeg)
    """
    installer = FFmpegInstaller(progress_callback)
    return installer.ensure_ffmpeg()


# Teste standalone
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    def progress(msg, pct):
        if pct >= 0:
            print(f"[{pct:3d}%] {msg}")
        else:
            print(f"       {msg}")
    
    print("=" * 50)
    print("FFmpeg Auto-Installer")
    print("=" * 50)
    
    installer = FFmpegInstaller(progress)
    
    # Verifica se já tem
    available, path, version = installer.is_ffmpeg_available()
    
    if available:
        print(f"\n✓ FFmpeg disponível: {path}")
        print(f"  Versão: {version}")
    else:
        print("\n⚠ FFmpeg não encontrado")
        response = input("\nDeseja instalar automaticamente? (s/n): ")
        
        if response.lower() == 's':
            success, path = installer.install_ffmpeg()
            
            if success:
                print(f"\n✓ FFmpeg instalado com sucesso!")
                print(f"  Caminho: {path}")
            else:
                print("\n❌ Falha na instalação")
