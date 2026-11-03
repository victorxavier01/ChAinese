"""
ChAInese — Configuração global do módulo de Captura + OCR.

TODO parâmetro que afeta performance ou comportamento está centralizado aqui
para que a calibração (region, teclas, thresholds) não exija tocar o código.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Região de captura
# ---------------------------------------------------------------------------
# None  -> captura o monitor onde está o RATO no momento do F9 (o ecrã
#          "ativo", onde o jogo está a ser visto); a etapa `det` do
#          PaddleOCR encontra sozinha onde está o texto (modo padrão).
# dict  -> região fixa {"left", "top", "width", "height"} em pixels
#          (coordenadas do desktop virtual: origin 0,0 no monitor mais à
#          esquerda), override/fallback se a captura a ecrã inteiro ficar
#          lenta para o caso de uso (mais pixels = OCR mais lento).
SCAN_REGION: dict[str, int] | None = None

# ---------------------------------------------------------------------------
# Hotkeys
# ---------------------------------------------------------------------------
# Definidas em main.py via chainese.hotkeys (Win32 RegisterHotKey
# funcionam SEM Administrador):
#   F9     -> captura + OCR (raramente usada pelos jogos, fácil com a mão no rato)
#   Ctrl+Q -> sair

# ---------------------------------------------------------------------------
# OCR (PaddleOCR 3.x / PP-OCRv6)
# ---------------------------------------------------------------------------
OCR_LANG: str = "ch"    # chinês simplificado (o modelo PP-OCRv6 cobre EN no mesmo modelo)
OCR_DEVICE: str = "cpu" # CPU: previsível e não disputa VRAM com o jogo

# Thresholds da deteção de texto (etapa `det`).
#  * Texto do diálogo não é detetado?  -> baixa DET_THRESH.
#  * Excesso de falsos positivos?      -> sobe DET_THRESH e/ou BOX_THRESH.
DET_THRESH: float = 0.3     # confiança mínima para um pixel contar como "texto"
BOX_THRESH: float = 0.5     # score mínimo para uma box ser mantida
REC_SCORE_MIN: float = 0.6  # confiança mínima de reconhecimento (etapa `rec`)

# ---------------------------------------------------------------------------
# Pós-filtro: isola o diálogo chinês do jogo (HUD, chat, números)
# ---------------------------------------------------------------------------
# Uma linha só conta como "chinês" se pelo menos esta fração dos caracteres
# for CJK (ideogramas unificados U+4E00..U+9FFF + extensões).
# 0.3 elimina linhas como "XP +120" ou "Quest: ...".
CJK_MIN_RATIO: float = 0.3

# ---------------------------------------------------------------------------
# Tradução (Ollama local + Qwen2.5)
# ---------------------------------------------------------------------------
# O Ollama roda em http://localhost:11434 por padrão. A URL é configurável
# para apontar para outro host (ex.: outra máquina com mais GPU).
# qwen2.5:3b é o ponto ideal para 4 GB de VRAM (o 7b pede ~4,7 GB e não
# cabe inteiro na GPU).
OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_MODEL: str = "qwen2.5:3b"
OLLAMA_TIMEOUT_S: float = 30.0    # tempo máximo por tradução (segundos)
TRANSLATE_CACHE_MAX: int = 512    # linhas únicas mantidas em cache (LRU)

# ---------------------------------------------------------------------------
# Controlo de CPU (opcional)
# ---------------------------------------------------------------------------
# Threads usadas pelo inferência (Paddle/oneDNN).
#   None = usa todos os núcleos (máx. velocidade; pico de CPU durante o OCR).
#   4    = se o jogo perder frames durante o OCR, fixa o nº de threads.
# A variável de ambiente é lida pelo Paddle/oneDNN no import, por isso é
# aplicada em ocr.py *antes* de importar o paddle.
MAX_CPU_THREADS: int | None = None
