"""
Stream Processor para IVMS Pro
Exemplo de como processar streams com Python/OpenCV

Requisitos:
    pip install opencv-python numpy requests

Uso:
    python stream_processor.py --stream-url http://localhost:8080/hls/abc123.m3u8
"""

import cv2
import numpy as np
import argparse
import time
import requests
from datetime import datetime

class StreamProcessor:
    def __init__(self, stream_url: str, api_url: str = None):
        self.stream_url = stream_url
        self.api_url = api_url
        self.cap = None
        self.prev_frame = None
        self.motion_threshold = 25
        self.min_area = 500
        
    def connect(self) -> bool:
        """Conecta ao stream HLS"""
        self.cap = cv2.VideoCapture(self.stream_url)
        if not self.cap.isOpened():
            print(f"Erro ao conectar: {self.stream_url}")
            return False
        print(f"Conectado: {self.stream_url}")
        return True
    
    def detect_motion(self, frame: np.ndarray) -> tuple[bool, list]:
        """Detecta movimento no frame"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        
        if self.prev_frame is None:
            self.prev_frame = gray
            return False, []
        
        # Diferença entre frames
        delta = cv2.absdiff(self.prev_frame, gray)
        thresh = cv2.threshold(delta, self.motion_threshold, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        
        # Encontrar contornos
        contours, _ = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        motion_areas = []
        for contour in contours:
            if cv2.contourArea(contour) < self.min_area:
                continue
            (x, y, w, h) = cv2.boundingRect(contour)
            motion_areas.append({"x": x, "y": y, "w": w, "h": h})
        
        self.prev_frame = gray
        return len(motion_areas) > 0, motion_areas
    
    def send_alert(self, camera_id: str, motion_areas: list):
        """Envia alerta para o backend (futuro)"""
        if not self.api_url:
            return
        
        payload = {
            "camera_id": camera_id,
            "timestamp": datetime.now().isoformat(),
            "motion_areas": motion_areas
        }
        
        try:
            requests.post(f"{self.api_url}/alerts", json=payload, timeout=5)
        except Exception as e:
            print(f"Erro ao enviar alerta: {e}")
    
    def process(self, show_preview: bool = False):
        """Loop principal de processamento"""
        if not self.connect():
            return
        
        print("Processando stream... (Ctrl+C para parar)")
        
        try:
            while True:
                ret, frame = self.cap.read()
                if not ret:
                    print("Frame perdido, reconectando...")
                    time.sleep(1)
                    self.connect()
                    continue
                
                has_motion, areas = self.detect_motion(frame)
                
                if has_motion:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Movimento detectado! Áreas: {len(areas)}")
                    
                    if show_preview:
                        for area in areas:
                            cv2.rectangle(
                                frame,
                                (area["x"], area["y"]),
                                (area["x"] + area["w"], area["y"] + area["h"]),
                                (0, 255, 0),
                                2
                            )
                
                if show_preview:
                    cv2.imshow("IVMS Pro - Stream", frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
                        
        except KeyboardInterrupt:
            print("\nParando processamento...")
        finally:
            self.cap.release()
            if show_preview:
                cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IVMS Pro Stream Processor")
    parser.add_argument("--stream-url", required=True, help="URL do stream HLS")
    parser.add_argument("--api-url", help="URL do backend para alertas")
    parser.add_argument("--preview", action="store_true", help="Mostrar preview com OpenCV")
    
    args = parser.parse_args()
    
    processor = StreamProcessor(args.stream_url, args.api_url)
    processor.process(show_preview=args.preview)
