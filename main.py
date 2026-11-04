from __future__ import annotations

import logging
import threading
import time

from chainese import config
from chainese.capture import ScreenCapturer
from chainese.hotkeys import (
    HotkeyError,
    HotkeyManager,
    MOD_CONTROL,
    VK_F9,
    VK_Q,
)
from chainese.ocr import DetectedText, TextRecognizer
from chainese.overlay import CardItem, Overlay, VocabItem
from chainese.segment import pinyin_line, vocab_words, warmup as segment_warmup
from chainese.translate import OllamaOffline, Translator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chainese.main")


def print_report(
    results: list[DetectedText],
    elapsed_ms: float,
    translations: dict[str, str] | None = None,
) -> None:
    """Printa o relatório OCR no terminal (bboxes em coordenadas de tela)."""
    width = 62
    print()
    print("=" * width)
    print(f"  F9  |  {len(results)} linha(s) em chinês  |  {elapsed_ms:.0f} ms no total")
    print("=" * width)
    if not results:
        print("  (nenhum texto chinês detetado)")
    for idx, item in enumerate(results, start=1):
        pinyin = pinyin_line(item.text)
        print(f"  [{idx}] {item.text}")
        if pinyin.strip():
            print(f"      {pinyin}")
        pt = (translations or {}).get(item.text)
        if pt:
            print(f"      (pt) {pt}")
        print(f"      confiança: {item.confidence:.2f} | bbox: {item.bbox}")
    print("=" * width)


def _translate_results(
    translator: Translator, results: list[DetectedText]
) -> dict[str, str]:
    """Traduz as linhas únicas (com cache).

    Se o Ollama cair no meio da sessão, a tradução é desativada pelo resto
    da execução (degradação graciosa, sem crash, aviso único).
    """
    translations: dict[str, str] = {}
    if not translator.available:
        return translations
    for item in results:
        if item.text in translations:
            continue
        try:
            translations[item.text] = translator.translate(item.text)
        except OllamaOffline as exc:
            translator.available = False
            logger.warning("Tradução desativada nesta sessão: %s", exc)
            break
    return translations


def _build_vocab(
    translator: Translator, results: list[DetectedText]
) -> list[VocabItem]:
    """Monta o painel de vocabulário: palavras únicas + pinyin + tradução.

    As palavras vêm de `segment.vocab_words` (ordem de 1ª ocorrência,
    só CJK, com limite do config) e a tradução é UMA chamada em lote ao
    Ollama (ver Translator.translate_words). Sem Ollama o painel segue
    com palavra + pinyin e "?" na tradução (degradação graciosa, como
    nos cartões).
    """
    if not results:
        return []
    words = vocab_words([r.text for r in results], limit=config.VOCAB_MAX_WORDS)
    if not words:
        return []
    translations: dict[str, str] = {}
    if translator.available:
        try:
            translations = translator.translate_words([w.word for w in words])
        except OllamaOffline as exc:
            # Mesmo contrato dos cartões: Ollama caiu -> sem tradução pelo
            # resto da sessão, sem crash, aviso único.
            translator.available = False
            logger.warning("Tradução de palavras desativada nesta sessão: %s", exc)
    return [
        VocabItem(
            word=w.word,
            pinyin=w.pinyin,
            translation=translations.get(w.word, ""),
        )
        for w in words
    ]


def _publish_overlay(
    overlay: Overlay,
    results: list[DetectedText],
    translations: dict[str, str],
    scope: tuple[int, int, int, int] | None = None,
    vocab: list[VocabItem] | None = None,
) -> None:
    """Publica os cartões no overlay (ou esconde os do monitor escaneado).

    O F9 afeta só o monitor capturado (scope): sem texto chinês, os
    cartões daquele monitor somem e os dos demais continuam no ar. Roda
    na thread do worker; o Overlay publica via sinal queued, então
    nenhuma chamada Qt acontece fora da thread principal.
    """
    if not results:
        overlay.publish([], scope)
        return
    items: list[CardItem] = []
    for item in results:
        x0, y0 = item.box.min(axis=0)
        x1, y1 = item.box.max(axis=0)
        items.append(
            CardItem(
                text=item.text,
                pinyin=pinyin_line(item.text),
                translation=translations.get(item.text, ""),
                bbox=(int(x0), int(y0), int(x1), int(y1)),
            )
        )
    # O F9 atualiza só o monitor escaneado (scope); os cartões dos demais
    # monitores continuam no ar. A API legado show_cards publicava sem
    # scope (todos os monitores) e era a causa deste caminho apagar todos.
    overlay.publish(items, scope, vocab)


def main() -> int:
    logger.info("A inicializar o PaddleOCR (1ª vez: download dos modelos)…")

    t0 = time.perf_counter()

    try:
        recognizer = TextRecognizer()
    except Exception as exc:
        logger.error("Não foi possível inicializar o OCR: %s", exc)

        return 1
    
    logger.info("PaddleOCR pronto em %.1fs", time.perf_counter() - t0)

    logger.info("Rodando warmup do OCR…")

    try:

        recognizer.warmup()

    except Exception as exc:
        logger.warning("warmup falhou (não fatal): %s", exc)

    logger.info("Preparando segmentação (jieba)…")
    try:
        segment_warmup()
    except Exception as exc:
        logger.warning("warmup da segmentação falhou (não fatal): %s", exc)

    translator = Translator()

    overlay = Overlay()

    capturer = ScreenCapturer(config.SCAN_REGION)

    busy = threading.Lock()

    def run_capture() -> None:
        if not busy.acquire(blocking=False):
            logger.info("OCR já está sendo executado, F9 ignorado.")

            return
        
        try:
            logger.info("Tirando print + OCR (isto pode demorar alguns segundos)…")
            t0 = time.perf_counter()
            frame = capturer.capture()
            ox, oy = capturer.frame_origin
            # Monitor escaneado (coordenadas globais): o overlay atualiza
            # só este monitor e preserva os cartões dos demais.
            reg = capturer.region
            scope = (reg["left"], reg["top"], reg["width"], reg["height"])
            results = recognizer.recognize(frame, origin=(ox, oy))
            translations = _translate_results(translator, results)
            vocab = _build_vocab(translator, results)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print_report(results, elapsed_ms, translations)
            _publish_overlay(overlay, results, translations, scope, vocab)

        except Exception:
            # logger.exception: traceback completo no log (diagnóstico sem
            # nova ronda de re-testes se voltar a falhar).
            logger.exception("Erro na captura/OCR")

        finally:
            busy.release()

    def on_capture() -> None:

        threading.Thread(target=run_capture, name="capture-ocr", daemon=True).start()

    def on_quit() -> None:
        logger.info("Fechando...")
        overlay.quit()

    # Hotkeys globais SEM Admin (Win32 RegisterHotKey). O callback roda na
    # thread de mensagens do HotkeyManager, por isso on_capture só lança
    # worker (nunca bloqueia a thread das hotkeys durante o OCR).
    hotkeys = HotkeyManager(
        [
            (VK_F9, 0, on_capture),        # F9 -> captura + OCR
            (VK_Q, MOD_CONTROL, on_quit),  # Ctrl+Q -> sair
        ]
    )

    try:
        hotkeys.start()

    except HotkeyError as exc:
        logger.error("Não foi possível registar as hotkeys: %s", exc)
        return 1

    logger.info("Pronto: F9 = captura + OCR | Ctrl+Q = sair")
    try:
        overlay.run()  # thread principal: loop de eventos do Qt

    finally:
        hotkeys.stop()
        capturer.close()
        logger.info("Fechado. Adeus.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
