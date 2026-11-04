"""
Segmentação + pinyins (Passo 2 do ChAInese).

Para cada linha de chinês detectada pelo OCR:
    - jieba    -> segmenta em palavras (chinês não tem espaços);
    - pypinyin -> pinyin com tons por palavra (nǐ hǎo, shì jiè).

Notas de performance:
    - O dicionário do jieba (~19 MB) é carregado UMA vez, via `warmup()`
      no arranque (~0.3 s). A segmentação em si custa <1 ms por linha,
      irrelevante comparado com o OCR.
    - pypinyin faz lookup de dicionário por caractere: custo desprezível.
    - O logger `jieba` é silenciado (WARNING): o "Building prefix dict"
      da 1ª chamada não interessa no meio dos logs do app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jieba
import pypinyin

from .ocr import is_cjk

logger = logging.getLogger(__name__)

# O jieba loga o carregamento do dicionário na 1ª chamada (INFO):
# silenciar para não poluir os logs de arranque.
logging.getLogger("jieba").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Word:
    """Palavra segmentada + o pinyin com tons (ex.: "nǐ hǎo")."""

    word: str
    pinyin: str


def _token_pinyin(token: str) -> str:
    """Pinyin de um token; token sem CJK devolve inalterado.

    `lazy_pinyin` devolve caractere a caractere para texto latino: sem
    este guard, "abc" viraria "a b c" na linha de pinyins.
    """
    if not any(is_cjk(ch) for ch in token):
        return token
    return " ".join(pypinyin.lazy_pinyin(token, style=pypinyin.Style.TONE))


def analyze(text: str) -> list[Word]:
    """Segmenta `text` (jieba) e atribui pinyin (pypinyin) a cada palavra."""
    words: list[Word] = []
    for token in jieba.cut(text, HMM=True):
        if not token.strip():
            continue  # espaços: só serviam para alinhamento, não para o output
        words.append(Word(word=token, pinyin=_token_pinyin(token)))
    return words


def pinyin_line(text: str) -> str:
    """Linha de pinyins pronta para imprimir: "nǐ hǎo shì jiè"."""
    return " ".join(w.pinyin for w in analyze(text))


def vocab_words(lines: list[str], limit: int = 10) -> list[Word]:
    """Palavras únicas das linhas (ordem de 1ª ocorrência), só CJK.

    Alimenta o painel de vocabulário do overlay (canto inferior
    esquerdo): pontuação e ruído latino/numérico do OCR ficam de fora
    (só contam tokens com pelo menos um ideograma); o limite é porque o
    painel tem altura finita.
    """
    seen: set[str] = set()
    words: list[Word] = []
    for line in lines:
        for word in analyze(line):
            tok = word.word.strip()
            if not tok or tok in seen or not any(is_cjk(ch) for ch in tok):
                continue
            seen.add(tok)
            words.append(word)
            if len(words) >= limit:
                return words
    return words


def warmup() -> None:
    """Absorve o custo da 1ª chamada (dicionário jieba + lookup pinyin)."""
    list(jieba.cut("你好世界", HMM=True))
    pypinyin.lazy_pinyin("你", style=pypinyin.Style.TONE)
