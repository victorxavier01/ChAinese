"""
Diagnóstico do overlay — evidência de apresentação (investigação DWM).

Módulo puramente observacional: NENHUMA mudança de comportamento do
overlay. Só gera evidência para o incidente "o cartão aparece e some":

  - screenshots do monitor com o cartão em t=0,4/1,5/5 s após o publish:
    o PNG mostra se o cartão esteve realmente apresentado e quando
    deixou de estar;
  - retenção limitada (os N mais recentes) para debug/ não crescer.

Os grabs rodam em threads daemon curtas, cada uma com a sua instância
mss: o mss não é thread-safe (1 instância = 1 DC de GDI) e a doutrina
do app é nunca bloquear a thread principal do Qt com I/O.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# debug/ já está no .gitignore (screenshots de teste).
_DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug"
_KEEP_LAST = 20


def screenshot_region(region: dict, tag: str, delay_s: float = 0.0) -> None:
    """Agenda um screenshot de `region` (coordenadas globais de desktop).

    :param region: {"left", "top", "width", "height"} em coordenadas do
                   desktop virtual (a mesma base do mss/`ScreenCapturer`).
    :param tag: sufixo do nome do arquivo (ex.: "t1500").
    :param delay_s: atraso antes do grab (capturar o t+X após o publish).
    """
    threading.Thread(
        target=_grab, args=(dict(region), tag, delay_s), name=f"diag-{tag}", daemon=True
    ).start()


def _grab(region: dict, tag: str, delay_s: float) -> None:
    if delay_s > 0:
        time.sleep(delay_s)
    try:
        import mss  # tardio: só quando o diagnóstico é solicitado

        _DEBUG_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = _DEBUG_DIR / f"overlay_{ts}_{tag}.png"
        with mss.mss() as sct:
            # O top/left do desktop virtual PODE ser negativo (nesta
            # máquina top=-3): coordenada negativa falha no BitBlt —
            # converter para a origem do DC, como em capture.py.
            virt = sct.monitors[0]
            grab = {
                "left": region["left"] - virt["left"],
                "top": region["top"] - virt["top"],
                "width": region["width"],
                "height": region["height"],
            }
            # mss 10.x: to_png recebe BYTES brutos RGB + (w, h) — não o
            # ScreenShot (a API do mss 9 mudou). BGRA -> RGB, mesma base
            # de canais de capture.py.
            arr = np.asarray(sct.grab(grab), dtype=np.uint8)  # (H, W, 4) BGRA
            rgb = np.ascontiguousarray(arr[:, :, 2::-1])  # (H, W, 3) RGB
            mss.tools.to_png(
                rgb.tobytes(), (rgb.shape[1], rgb.shape[0]), output=str(path)
            )
        _prune()
        logger.info("Diagnóstico [%s]: screenshot -> %s", tag, path)
    except Exception:
        logger.debug("Diagnóstico [%s]: screenshot falhou", tag, exc_info=True)


def _prune() -> None:
    """Mantém só os `_KEEP_LAST` PNGs mais recentes de overlay."""
    try:
        files = sorted(
            _DEBUG_DIR.glob("overlay_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old in files[_KEEP_LAST:]:
        try:
            old.unlink()
        except OSError:
            pass
