"""
OCR local de chinês com PaddleOCR 3.x (PP-OCRv6).

Notas de performance (baixo CPU):
- O PaddleOCR é inicializado EXATAMENTE UMA vez, no arranque (carregar os
  modelos demora alguns segundos e ~1–2 GB de RAM). Nunca por captura.
- Os 3 pipelines de pré-processamento documental (orientação do documento,
  "unwarping" de perspectiva, orientação por linha) estão desativados: cada
  um é uma passagem CNN extra por frame e é inútil para UI de jogo frontal.
- O logger `paddleocr` já arranca em nível ERROR por defeito (3.7.0), por isso
  não há logs ruidosos a silenciar.
- `enable_mkldnn=False`: o oneDNN (ligado por defeito) crasha com um bug PIR
  nesta versão do Paddle; o engine corre em modo "paddle" puro (estável).
- A primeira inferência é mais lenta (warmup da CPU); `warmup()` corre um
  frame dummy no arranque para absorver esse custo antes de entrar no jogo.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import config

logger = logging.getLogger(__name__)

# Limita as threads de inferência ANTES do import do Paddle (ele lê as vars
# de ambiente no import). Só tem efeito se MAX_CPU_THREADS for definido.
if config.MAX_CPU_THREADS:
    for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ.setdefault(_var, str(config.MAX_CPU_THREADS))


class OCRInitError(RuntimeError):
    """Erro fatal na inicialização do PaddleOCR (pacote em falta, modelos…)."""


class OCRError(RuntimeError):
    """Erro durante a inferência (o main trata e continua o loop)."""


@dataclass
class DetectedText:
    """Linha reconhecida com as suas coordenadas."""

    text: str
    confidence: float
    box: np.ndarray  # quadrado (4, 2) em coordenadas da captura

    @property
    def bbox(self) -> str:
        """Bounding box axis-aligned "x0,y0-x1,y1" — com captura a tela
        inteira, estas são diretamente coordenadas da tela."""
        x0, y0 = self.box.min(axis=0)
        x1, y1 = self.box.max(axis=0)
        return f"({x0},{y0})-({x1},{y1})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF    # CJK Unified Ideographs (bloco principal)
        or 0x3400 <= cp <= 0x4DBF  # Extensão A
        or 0xF900 <= cp <= 0xFAFF  # Ideogramas de compatibilidade
    )


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if is_cjk(ch)) / len(text)


def _to_quad(points: Any) -> np.ndarray:
    """Normaliza qualquer polígono para quadrado de 4 pontos.

    Defensivo: o nome/forma do campo das boxes varia entre versões do
    PaddleOCR (rec_boxes vs rec_polys, (4,2) vs (N,2)).
    """
    pts = np.asarray(points, dtype=np.int32).reshape(-1, 2)
    if len(pts) == 4:
        return pts
    if len(pts) > 4:
        step = max(len(pts) // 4, 1)
        return pts[::step][:4]
    x0, y0 = int(pts[:, 0].min()), int(pts[:, 1].min())
    x1, y1 = int(pts[:, 0].max()), int(pts[:, 1].max())
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.int32)


def _field(res: Any, name: str, default: Any) -> Any:
    """Lê um campo do Result do PaddleOCR (dict-like) com fallback por
    defeito — tolerante a variações de nome entre versões."""
    try:
        value = res[name]
    except (KeyError, TypeError):
        return default
    return default if value is None else value


# ---------------------------------------------------------------------------
# Classe principal
# ---------------------------------------------------------------------------
class TextRecognizer:
    """Wrapper de det+rec do PaddleOCR com filtro chinês."""

    def __init__(self, lang: str = config.OCR_LANG, device: str = config.OCR_DEVICE) -> None:
        try:
            # Import tardio: o paddle é pesado; carrega-se apenas quando o
            # TextRecognizer é construído, nunca no import do módulo.
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OCRInitError(
                "paddleocr não está instalado. Corre: pip install -r requirements.txt"
            ) from exc

        try:
            self._ocr = PaddleOCR(
                lang=lang,
                device=device,
                # ---- sub-pipelines pesados desativados (performance) ----
                use_doc_orientation_classify=False,  # sem classificação de rotação
                use_doc_unwarping=False,             # sem "unwarping" de perspetiva
                use_textline_orientation=False,     # sem rotação por linha
                # ---- thresholds de detecção (calibráveis em config.py) ----
                text_det_thresh=config.DET_THRESH,
                text_det_box_thresh=config.BOX_THRESH,
                text_rec_score_thresh=config.REC_SCORE_MIN,
                # ---- engine: oneDNN/MKLDNN desativado ----
                # Ligado por defeito no CPU, o oneDNN dispara um bug PIR nesta
                # versão do Paddle ("ConvertPirAttribute2RuntimeAttribute not
                # support ArrayAttribute<DoubleAttribute>") e a inferência
                # crasha. Com mkldnn=False o engine corre em run_mode="paddle":
                # ligeiramente mais lento, mas estável.
                enable_mkldnn=False,
            )
        except Exception as exc:  # falha de download de modelos, parâmetro inválido…
            raise OCRInitError(f"Falha ao inicializar o PaddleOCR: {exc}") from exc

    def warmup(self) -> None:
        """Absorve a inferência inicial (mais lenta) antes do jogo."""
        dummy = np.zeros((32, 32, 3), dtype=np.uint8)
        self.recognize(dummy)

    def recognize(
        self,
        image: np.ndarray,
        only_chinese: bool = True,
        origin: tuple[int, int] = (0, 0),
    ) -> list[DetectedText]:
        """Corre det+rec e devolve as linhas reconhecidas (com bboxes).

        :param image: frame BGR (H, W, 3) uint8.
        :param only_chinese: se True, devolve apenas linhas com razão CJK >=
                             config.CJK_MIN_RATIO (elimina HUD/chat em inglês
                             e números).
        :param origin: origem de tela do frame (x, y). As boxes saem do
                       PaddleOCR em coordenadas do frame; somar a origem
                       devolve coordenadas de tela reais (necessário para o
                       overlay em monitores fora da origem (0,0)).
        """
        try:
            results = self._ocr.predict(image)
        except Exception as exc:
            raise OCRError(f"Falha na inferência OCR: {exc}") from exc

        items: list[DetectedText] = []
        for res in results:
            texts = _field(res, "rec_texts", [])
            scores = _field(res, "rec_scores", [])
            boxes = _field(res, "rec_boxes", None)
            if boxes is None:  # em algumas versões o campo chama-se rec_polys
                boxes = _field(res, "rec_polys", [])
            # Paddle 3.x devolve `rec_boxes` como ndarray (N,4,2): normalizar
            # para listas Python — `bool(ndarray)` com 2+ elementos levanta
            # "truth value of an array is ambiguous" no teste abaixo.
            texts = [] if texts is None else list(texts)
            scores = [] if scores is None else list(scores)
            boxes = [] if boxes is None else list(boxes)
            if texts and not boxes:
                logger.warning(
                    "Resultado OCR sem boxes (variação de versão?): %s", type(res)
                )
                continue

            for text, score, box in zip(texts, scores, boxes):
                quad = _to_quad(box)
                if origin != (0, 0):
                    quad = quad + np.asarray(origin, dtype=np.int32)
                items.append(
                    DetectedText(
                        text=str(text),
                        confidence=float(score),
                        box=quad,
                    )
                )

        items = [
            it
            for it in items
            if it.confidence >= config.REC_SCORE_MIN
            and (not only_chinese or _cjk_ratio(it.text) >= config.CJK_MIN_RATIO)
        ]
        return items
