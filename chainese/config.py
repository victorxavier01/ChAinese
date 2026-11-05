"""
ChAInese — Configuração global do módulo de Captura + OCR.

Todo parâmetro que afeta performance ou comportamento está centralizado aqui
para a calibração (region, teclas, thresholds) não exigir mexer no código.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Região de captura
# ---------------------------------------------------------------------------
# None  -> captura o monitor onde está o MOUSE no momento do F9 (a tela
#          "ativa", onde o jogo está sendo visto); a etapa `det` do
#          PaddleOCR encontra sozinha onde está o texto (modo padrão).
# dict  -> região fixa {"left", "top", "width", "height"} em pixels
#          (coordenadas do desktop virtual: origin 0,0 no monitor mais à
#          esquerda), override/fallback se a captura a tela inteira ficar
#          lenta para o caso de uso (mais pixels = OCR mais lento).
SCAN_REGION: dict[str, int] | None = None

# ---------------------------------------------------------------------------
# Hotkeys
# ---------------------------------------------------------------------------
# Definidas em main.py via chainese.hotkeys (Win32 RegisterHotKey
# funcionam SEM Administrador):
#   F9     -> captura + OCR (raramente usada pelos jogos, fácil com a mão no mouse)
#   Ctrl+Q -> sair

# ---------------------------------------------------------------------------
# OCR (PaddleOCR 3.x / PP-OCRv6)
# ---------------------------------------------------------------------------
OCR_LANG: str = "ch"    # chinês simplificado (o modelo PP-OCRv6 cobre EN no mesmo modelo)
OCR_DEVICE: str = "cpu" # CPU: previsível e não disputa VRAM com o jogo

# Thresholds da detecção de texto (etapa `det`).
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
# Painel de vocabulário (overlay, canto superior esquerdo)
# ---------------------------------------------------------------------------
# Máximo de palavras consideradas (ordem de 1ª ocorrência no diálogo):
# limita o total de páginas e o lote de tradução (chamadas ao Ollama).
# O painel é paginado (VOCAB_PAGE_SIZE): F8/F10 folheiam as páginas.
# Com 100: até 10 páginas por scan; a tradução é FATIADA em lotes de
# VOCAB_WORDS_BATCH (100 palavras = 5 chamadas curtas, cada uma bem
# dentro do timeout de 30 s — medido: 98 palavras numa chamada única
# estouravam o timeout e matavam a tradução da sessão inteira).
VOCAB_MAX_WORDS: int = 100

# Palavras por chamada do Ollama no lote de vocabulário: listas longas
# demoram mais que OLLAMA_TIMEOUT_S a gerar e o modelo 3b perde a
# numeração da lista. Lotes curtos respondem em segundos e seguem o
# formato com fiabilidade.
VOCAB_WORDS_BATCH: int = 20

# Palavras por página do painel: coluna vertical no canto da tela, com
# altura finita (~260 px para 10 linhas). Mais que isto por página
# viraria um banner lateral a tapar o jogo; o resto fica nas páginas
# seguintes.
VOCAB_PAGE_SIZE: int = 10

# ---------------------------------------------------------------------------
# Controlo de CPU (opcional)
# ---------------------------------------------------------------------------
# Threads usadas pelo inferência (Paddle/oneDNN).
#   None = usa todos os núcleos (máx. velocidade; pico de CPU durante o OCR).
#   4    = se o jogo perder frames durante o OCR, fixa o nº de threads.
# A variável de ambiente é lida pelo Paddle/oneDNN no import, por isso é
# aplicada em ocr.py *antes* de importar o paddle.
MAX_CPU_THREADS: int | None = None
