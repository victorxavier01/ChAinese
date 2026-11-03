from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001

# Modificadores (Win32 MOD_*)
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x0400  # ignora auto-repeat (só relevante em modo register)

# Códigos de tecla virtual (VK_*) as que usamos
VK_F9 = 0x78  # VK_F1 = 0x70 ... VK_F10 = 0x79
VK_Q = 0x51

# MOD_* -> VK correspondente (modo polling)
MOD_TO_VK = {
    MOD_ALT: 0x12,      # VK_MENU
    MOD_CONTROL: 0x11,  # VK_CONTROL
    MOD_SHIFT: 0x10,    # VK_SHIFT
    MOD_WIN: 0x5B,      # VK_LWIN
}

POLL_INTERVAL_S = 0.01  # 100 Hz: CPU ~0%; 10 ms para não perder toques curtos de F9


user32.RegisterHotKey.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [wintypes.WORD]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetLastError.restype = wintypes.DWORD


class HotkeyError(RuntimeError):
    """Falha fatal no arranque das hotkeys."""


def _key_down(vk: int) -> bool:
    """Estado físico da tecla (bit 7 de GetAsyncKeyState)."""
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def _mods_down(mods: int) -> bool:
    """Todos os modificadores pedidos (MOD_*) estão pressionados."""
    return all(
        _key_down(MOD_TO_VK[m])
        for m in (MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN)
        if mods & m
    )


class HotkeyManager:
    """Hotkeys globais sem Admin: RegisterHotKey + fallback por polling."""

    def __init__(self, hotkeys: list[tuple[int, int, object]]) -> None:
        """
        :param hotkeys: lista de (vk, mods, callback) ex.:
                        [(VK_F9, 0, on_capture), (VK_Q, MOD_CONTROL, on_quit)]
        """
        self._hotkeys = hotkeys
        self._callbacks: dict[int, object] = {}
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.last_error: str | None = None
        self.mode: str | None = None  # "register" | "polling"
        self._register_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        """Tenta RegisterHotKey; se falhar, cai automaticamente no polling."""
        if self.mode is not None:
            return
        self._register_thread = threading.Thread(
            target=self._run_register, name="hotkeys", daemon=True
        )
        self._register_thread.start()
        if not self._ready.wait(timeout=10):
            raise HotkeyError("A thread de hotkeys não arrancou (timeout).")

        if self._callbacks:
            self.mode = "register"
            logger.info("Hotkeys ativas (modo RegisterHotKey sem Admin).")
            return

        # Registo falhou (bloqueado nesta máquina) -> fallback polling.
        # Limpa registos parciais para evitar dupla disparada.
        for idx in list(self._callbacks):
            user32.UnregisterHotKey(None, idx)
        self._callbacks.clear()
        self.mode = "polling"
        self._poll_thread = threading.Thread(
            target=self._run_polling, name="hotkeys-poll", daemon=True
        )
        self._poll_thread.start()
        logger.warning(
            "RegisterHotKey indisponível nesta máquina (falha silenciosa)%s "
            "fallback: polling GetAsyncKeyState a cada %.0f ms.",
            f" (Win32 code {self.last_error})" if self.last_error else "",
            POLL_INTERVAL_S * 1000,
        )

    def stop(self) -> None:
        self._stop.set()
        for th in (self._register_thread, self._poll_thread):
            if th is not None:
                th.join(timeout=1.0)
        self._register_thread = None
        self._poll_thread = None
        self.mode = None

    # ------------------------------------------------------------------
    # Modo register (Win32 RegisterHotKey + message loop)
    # ------------------------------------------------------------------
    def _run_register(self) -> None:
        """Registo (nesta thread) + pump da fila de mensagens.

        Com hWnd=NULL o WM_HOTKEY chega à fila da thread que registou por
        isso o registo e o pump têm de ser na MESMA thread.
        """
        for idx, (vk, mods, callback) in enumerate(self._hotkeys, start=1):
            if self._stop.is_set():
                break
            if not user32.RegisterHotKey(None, idx, mods | MOD_NOREPEAT, vk):
                err = kernel32.GetLastError()
                self.last_error = str(err)
                logger.error(
                    "RegisterHotKey falhou (vk=0x%02X, mods=0x%04X, Win32 code %s) "
                    "a tecla pode estar em uso ou a API bloqueada.",
                    vk, mods, err,
                )
                continue
            self._callbacks[idx] = callback
        self._ready.set()
        if not self._callbacks:
            return

        msg = wintypes.MSG()
        while not self._stop.is_set():
            # PM_REMOVE: remove a mensagem da fila ao processar.
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY and msg.wParam in self._callbacks:
                    # O callback roda NESTA thread: nunca bloquear aqui
                    # (o main lança worker threads para o OCR).
                    self._callbacks[msg.wParam]()
            self._stop.wait(0.05)  # ~0% CPU; permite o stop() limpo

        # Limpeza: unregister na thread que registou.
        for idx in list(self._callbacks):
            user32.UnregisterHotKey(None, idx)
        logger.debug("HotkeyManager (register) parado.")

    # ------------------------------------------------------------------
    # Modo polling (fallback GetAsyncKeyState)
    # ------------------------------------------------------------------
    def _run_polling(self) -> None:
        """Polling do estado físico do teclado com deteção de rising edge."""
        prev: dict[int, bool] = {}
        while not self._stop.is_set():
            for idx, (vk, mods, callback) in enumerate(self._hotkeys, start=1):
                if self._stop.is_set():
                    return
                now = _key_down(vk) and _mods_down(mods)
                if now and not prev.get(idx, False):
                    callback()
                prev[idx] = now
            self._stop.wait(POLL_INTERVAL_S)
        logger.debug("HotkeyManager (polling) parado.")
