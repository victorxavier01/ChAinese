# ChAInese

Overlay para jogos no Windows que **lê o chinês que aparece na tela** (diálogos) e mostra, por cima do jogo:

- **Cartões de tradução** ancorados no próprio texto: chinês + pinyin + português;
- **Painel de vocabulário** no canto superior esquerdo: palavra + pinyin + tradução, paginado.

Tudo roda **localmente**: OCR com PaddleOCR (CPU, não disputa VRAM com o jogo) e tradução com Ollama (Qwen2.5 3B). Sem rede, sem API keys, sem custo por uso.

## Pipeline

```
F9
 └─ captura do monitor ativo (mss — a tela onde está o mouse)
     └─ PaddleOCR 3.x / PP-OCRv6 (modelo chinês, CPU) → linhas + bboxes
         └─ filtro: linhas com ≥30% de caracteres CJK (separa diálogo de HUD/chat)
             ├─ jieba (segmentação) + pypinyin (pinyin com tons)
             ├─ Ollama local (qwen2.5:3b): tradução das linhas + das palavras
             │  (cache LRU; palavras em lotes de 20 por chamada)
             └─ overlay PyQt6: 1 janela por cartão + painel de vocabulário
```

## Funcionalidades

- **Click-through**: as janelas do overlay não capturam cliques nem roubam foco — o jogo mantém o foco enquanto o overlay está no ar.
- **Sem sobreposição**: cartões e painel são posicionados com um layout monótono (ordem de leitura) em que duas janelas nunca se tocam.
- **Por monitor**: o F9 só afeta o monitor escaneado; os cartões dos demais monitores continuam no ar.
- **Painel paginado**: até 100 palavras por scan (10 por página); F8/F10 trocam de página, F9 volta para a página 1.
- **Degradação graciosa**: com o Ollama desligado, o app continua sem tradução (palavra + pinyin + "?"), sem crash.
- **Relatório no terminal**: cada F9 imprime as linhas detectadas (texto, pinyin, tradução, confiança, bbox, tempo total).
- **Hotkeys globais sem admin** (Win32 `RegisterHotKey`): funcionam com o jogo em janela borderless.

## Requisitos

- Windows 10/11 (usa APIs Win32: `RegisterHotKey`, `SetWindowRgn`, `GetCursorPos`)
- Python 3.10+ (desenvolvido com 3.12)
- Ollama + modelo `qwen2.5:3b` (opcional, mas recomendado — sem ele as traduções ficam "?")
  - `qwen2.5:3b` é o ponto ideal para ~4 GB de VRAM; o 7b não cabe inteiro na GPU
- Escala de DPI em 100% (o mss e o Qt usam pixels físicos)

## Instalação

```powershell
git clone <repo>
cd ChAInese

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# tradução (opcional):
ollama pull qwen2.5:3b
```

> Na primeira execução, os modelos do PaddleOCR são baixados automaticamente e a inicialização leva mais alguns segundos.

## Como usar

```powershell
.\venv\Scripts\python.exe main.py
```

| Tecla | Ação |
|---|---|
| **F9** | Captura + OCR + tradução + overlay (alguns segundos; o relatório aparece no terminal) |
| **F8** | Página anterior do painel de vocabulário |
| **F10** | Página seguinte do painel de vocabulário |
| **Ctrl+Q** | Sair |

O F9 usa o monitor onde o mouse está no momento (a tela onde o jogo está sendo visto). Dois F9 em rápida sucessão: o segundo é ignorado enquanto o primeiro estiver em execução.

## Configuração

Tudo em [`chainese/config.py`](chainese/config.py) — calibração sem mexer no código:

| Parâmetro | Default | Descrição |
|---|---|---|
| `SCAN_REGION` | `None` | `None` = monitor ativo; ou região fixa `{"left","top","width","height"}` (OCR mais rápido com menos pixels) |
| `OCR_DEVICE` | `"cpu"` | Dispositivo do PaddleOCR (CPU não disputa VRAM com o jogo) |
| `DET_THRESH` / `BOX_THRESH` / `REC_SCORE_MIN` | `0.3 / 0.5 / 0.6` | Thresholds de detecção/reconhecimento. Texto não detectado? Baixe o `DET_THRESH`. Falsos positivos? Suba |
| `CJK_MIN_RATIO` | `0.3` | Fração mínima de caracteres CJK para a linha contar como diálogo (elimina "XP +120", HUD, chat) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Aponte para outro host se o modelo roda em outra máquina |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Modelo de tradução |
| `OLLAMA_TIMEOUT_S` | `30.0` | Timeout por chamada (as palavras vão em lotes de 20, com folga de sobra) |
| `TRANSLATE_CACHE_MAX` | `512` | Cache LRU de linhas únicas (diálogos se repetem — cada texto novo consulta o modelo uma vez) |
| `VOCAB_MAX_WORDS` | `100` | Máximo de palavras no painel (ordem de 1ª ocorrência) |
| `VOCAB_PAGE_SIZE` | `10` | Palavras por página |
| `VOCAB_WORDS_BATCH` | `20` | Palavras por chamada do Ollama no lote do painel |
| `MAX_CPU_THREADS` | `None` | Limita as threads de inferência se o jogo perder frames durante o OCR |

## Estrutura

```
main.py                 # orquestração: hotkeys, worker do F9, publicação no overlay
requirements.txt
chainese/
  config.py             # todos os parâmetros calibráveis
  capture.py            # captura de tela (mss; monitor ativo ou região fixa)
  ocr.py                # PaddleOCR 3.x (PP-OCRv6, warmup, threads limitadas)
  segment.py            # jieba + pypinyin (palavras + pinyin com tons)
  translate.py          # cliente Ollama (cache LRU, lotes de palavras, degradação graciosa)
  overlay.py            # janelas PyQt6: cartões + painel (opacas, click-through, top-most)
  hotkeys.py            # hotkeys globais (RegisterHotKey + fallback por polling)
  diag.py               # screenshots de diagnóstico (evidência de apresentação)
debug/
  selftest_overlay.py   # 2 cartões + painel por 10 s, com screenshots em t=0.4/1.5/5 s
```

## Diagnóstico

```powershell
.\venv\Scripts\python.exe debug\selftest_overlay.py
```

Mostra na tela real 2 cartões sintéticos (um com tradução longa, para validar a quebra de linha) e o painel de vocabulário no canto superior esquerdo, e grava screenshots em `debug/overlay_*_t0400/t1500/t5000.png` — ground truth visual do overlay.

## Notas de implementação

- **Janelas opacas de propósito**: a antiga abordagem com fundo translúcido (superfície *layered*) deixava de ser composta pelo DWM desta máquina ~400 ms após a mapeação. As janelas opacas seguem o caminho normal de composição; os cantos arredondados vêm do `SetWindowRgn` (GDI), não de alpha.
- **Tamanhos determinísticos**: os rótulos medem o conteúdo no motor de renderização real (`QTextDocument`) antes do `show()` — o `sizeHint` de um `QLabel` com word-wrap depende da largura atual do widget (~0 no construtor) e cortava o texto.
- **Prompt em inglês, saída em português**: um 3B segue meta-instruções em inglês com mais fiabilidade (diagnosticado com teste A/B contra o Ollama local).
- **Thread-safety**: hotkeys → worker (thread) → sinal *queued* para o overlay (thread principal). Nenhuma chamada Qt fora da thread principal; o busy lock impede dois OCR simultâneos.
