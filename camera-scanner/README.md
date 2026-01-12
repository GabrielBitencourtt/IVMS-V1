# 📹 Camera Scanner Agent

Aplicativo desktop para descoberta automática de câmeras IP na rede local.

## 🚀 Para o Usuário Final

**Só precisa baixar e executar!** Não precisa instalar Python ou qualquer outra coisa.

### Windows
1. Baixe `CameraScannerAgent.exe`
2. Dê duplo-clique para executar
3. Se aparecer aviso do Windows Defender, clique em "Mais informações" > "Executar assim mesmo"

### macOS
1. Baixe `CameraScannerAgent`
2. Clique com botão direito > Abrir
3. Confirme a execução

### Linux
1. Baixe `CameraScannerAgent`
2. Dê permissão: `chmod +x CameraScannerAgent`
3. Execute: `./CameraScannerAgent`

---

## 🛠 Para Desenvolvedores (Gerar o Executável)

Requer Python 3.8+ instalado apenas para compilar:

```bash
cd camera-scanner
python build.py
```

O executável será gerado em `dist/CameraScannerAgent`

### Iniciar com o Sistema
```bash
./CameraScannerAgent --install-autostart
```
