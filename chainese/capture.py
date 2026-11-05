from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import Any

import mss
import numpy as np

logger = logging.getLogger(__name__)

_GRAB_ATTEMPTS = 5
_GRAB_RETRY_DELAY_S = 0.4

Region = dict[str, int]  # {"left", "top", "width", "height"}

# ---------------------------------------------------------------------------
# Win32: posição do mouse (para escolher o monitor ativo)
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
user32.GetCursorPos.restype = wintypes.BOOL


def _cursor_pos() -> tuple[int, int]:
    """Posição global do mouse (coordenadas do desktop virtual)."""
    pt = _POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        # Fallback defensivo: canto superior esquerdo.
        return 0, 0
    return pt.x, pt.y


class CaptureError(RuntimeError):
    """Erro de captura (região inválida, falha do mss)."""


class ScreenCapturer:
    """Captura a tela (ou uma região fixa) com o mss."""

    def __init__(self, region: Region | None = None) -> None:
        """
        :param region: região em pixels {"left","top","width","height"},
                       ou None para o monitor onde está o mouse no momento
                       da captura (o monitor "ativo").
        :raises CaptureError: se a região fixa não intersectar nenhum monitor.
        """
        self._sct = mss.mss()
        self._region = region
        # Placeholder: _resolve_region compara contra _resolved (log de mudança).
        self._resolved: Region = {"left": 0, "top": 0, "width": 0, "height": 0}
        # Origem de tela (desktop virtual) do último frame: offset a somar
        # às boxes do OCR para obter coordenadas de tela reais.
        self._frame_origin: tuple[int, int] = (0, 0)
        self._resolved = self._resolve_region()
        logger.info("ScreenCapturer pronto: região=%s", self._resolved)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def capture(self) -> np.ndarray:
        region = self._resolve_region()
        grab, origin = self._prepare_grab(region)
        for attempt in range(1, _GRAB_ATTEMPTS + 1):
            try:
                shot = self._sct.grab(grab)
                break
            except Exception as exc:
                if attempt == _GRAB_ATTEMPTS:
                    msg = (
                        "Falha na captura de tela após "
                        f"{_GRAB_ATTEMPTS} tentativas: {exc}"
                    )
                    raise CaptureError(msg) from exc
                logger.warning(
                    "BitBlt falhou (tentativa %d/%d), repetindo em %.1fs...",
                    attempt,
                    _GRAB_ATTEMPTS,
                    _GRAB_RETRY_DELAY_S,
                )
                time.sleep(_GRAB_RETRY_DELAY_S)

        frame = np.asarray(shot, dtype=np.uint8)  # (H, W, 4) BGRA
        self._frame_origin = origin
        return np.ascontiguousarray(frame[:, :, :3])  # (H, W, 3) BGR

    @property
    def frame_origin(self) -> tuple[int, int]:
        """Origem de tela (desktop virtual) do último frame capturado.

        As boxes do OCR saem em coordenadas do frame; somar esta origem
        converte para coordenadas de tela (necessário para o overlay em
        qualquer monitor).
        """
        return self._frame_origin

    @property
    def region(self) -> Region:
        """Última região resolvida (evita mutação externa)."""
        return dict(self._resolved)

    def _prepare_grab(self, region: Region) -> tuple[Region, tuple[int, int]]:
        """Retângulo para o mss + origem de tela real do frame.

        O DC de tela (DISPLAY) usado pelo BitBlt do GDI tem origem no canto
        superior esquerdo do desktop virtual, que PODE ser negativo (nesta
        máquina o desktop virtual começa em top=-3): coordenada negativa
        falha no BitBlt e coordenada "crua" desloca o frame. Converte-se o
        retângulo para coordenadas do DC (sempre >= 0) e devolve a origem
        absoluta do frame para o offset das bboxes.
        """
        virt = self._sct.monitors[0]
        left = max(region["left"], virt["left"])
        top = max(region["top"], virt["top"])
        right = min(region["left"] + region["width"], virt["left"] + virt["width"])
        bottom = min(region["top"] + region["height"], virt["top"] + virt["height"])
        if right - left <= 0 or bottom - top <= 0:
            raise CaptureError(f"Região {region} fora do desktop visível {virt}")
        grab = {
            "left": left - virt["left"],
            "top": top - virt["top"],
            "width": right - left,
            "height": bottom - top,
        }
        return grab, (left, top)

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    def __enter__(self) -> "ScreenCapturer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Interno
    # ------------------------------------------------------------------
    def _resolve_region(self) -> Region:
        """Resolve a região ativa (o monitor ativo pode mudar entre capturas)."""
        # mss: monitors[0] = desktop virtual; monitors[1:] = monitores físicos.
        physical = self._sct.monitors[1:] or self._sct.monitors

        if self._region is None:
            region = self._monitor_at_cursor(physical)
        else:
            region = None
            for mon in physical:
                if self._intersects(self._region, mon):
                    region = {
                        k: int(self._region[k])
                        for k in ("left", "top", "width", "height")
                    }
                    break
            if region is None:
                raise CaptureError(
                    f"Região {self._region} não intersecta nenhum monitor "
                    f"(monitores: {physical})"
                )

        if region != self._resolved:
            logger.debug("Monitor ativo mudou: %s -> %s", self._resolved, region)
        self._resolved = region
        return region

    def _monitor_at_cursor(self, physical: list[Region]) -> Region:
        """O monitor que contém o mouse (é onde o jogo está sendo visto).

        Fallback: o monitor primário (mouse na borda do desktop virtual ou
        falha da API).
        """
        cx, cy = _cursor_pos()
        for mon in physical:
            if (
                mon["left"] <= cx < mon["left"] + mon["width"]
                and mon["top"] <= cy < mon["top"] + mon["height"]
            ):
                return {k: int(mon[k]) for k in ("left", "top", "width", "height")}
        try:
            primary = self._sct.primary_monitor
        except Exception:
            primary = physical[0]
        return {k: int(primary[k]) for k in ("left", "top", "width", "height")}

    @staticmethod
    def _intersects(a: Region, b: Region) -> bool:
        return not (
            a["left"] + a["width"] <= b["left"]
            or a["left"] >= b["left"] + b["width"]
            or a["top"] + a["height"] <= b["top"]
            or a["top"] >= b["top"] + b["height"]
        )
