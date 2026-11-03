"""
Overlay visual (PyQt6): cartões de tradução flutuando sobre o jogo.

Arquitetura (prioridade: custo zero em repouso + nunca atrapalhar o jogo):
    - Uma janela por monitor: frameless, fundo transparente, always-on-top e
      "input-transparent" (click-through): mouse e teclado atravessam a
      janela e vão para o jogo. Nada do overlay pode ser clicado. Ela só
      existe enquanto há cartões naquele monitor.
    - As janelas vivem na thread principal (loop de eventos do Qt). O worker
      do F9 (outra thread) publica os resultados via sinal Qt; a conexão
      queued faz a passagem entre threads de forma segura. Nenhuma chamada
      Qt acontece fora da thread principal.
    - Os cartões são ancorados na bbox do OCR (mesma base de coordenadas
      globais do mss): o canto superior esquerdo do cartão é alinhado ao
      canto (x0, y0) da bbox, ou seja, o cartão fica exatamente sobre a
      linha de texto original.
    - A cada F9 as janelas antigas são descartadas e criadas novas com o
      cartão já desenhado no primeiro show() (nesta máquina só a primeira
      mapeação com conteúdo apresenta de forma confiável), mais pulsos de
      SetWindowPos (ver _MonitorWindow.reassert): o DWM desta máquina
      entra em fases em que não apresenta a superfície da janela sozinho,
      e a chamada força a reapresentação.
    - Enquanto os cartões estão visíveis, um timer de 1 s reafirma a
      apresentação (SetWindowPos + pulso dos atributos layered; o DWM
      pode parar de apresentar de novo). O timer só roda com cartões no
      ar: custo zero em repouso.
    - Cada F9 atualiza só o monitor escaneado: F9 sem chinês num monitor
      esconde os cartões daquele monitor e preserva os dos demais.
    - Em repouso não há timer nem animação: o custo de CPU entre F9 é
      praticamente zero.
    - DPI: assume escala 100% (mss e Qt usam pixels físicos na máquina do
      usuário). Multi-escala fica para o passo 5.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QScreen
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32: reafirmar o topmost (ver _MonitorWindow.reassert)
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
# SetWindowPos: uma chamada com SWP_NOZORDER (hWndInsertAfter ignorado,
# sem mexer na posição real) já força o DWM a revalidar a janela e
# reapresentar a superfície. Verificado empiricamente numa fase ruim do
# DWM, em que o show() sozinho deixava a janela invisível: esta chamada
# fez o cartão aparecer (mais de 1900 pixels de diferença na tela).
# Não usar HWND_TOPMOST (-1) aqui: ctypes não converte -1 nem o valor
# 64 bits equivalente (ArgumentError), e exceção dentro do slot do Qt
# aborta o processo inteiro.
_SWP_INSERT_DUMMY = 1
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_NOOWNERCHANGING = 0x0020
_LWA_ALPHA = 0x0002
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_byte,
    wintypes.DWORD,
]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL


@dataclass
class CardItem:
    """Linha de texto pronta para exibição (coordenadas globais de tela)."""

    text: str
    pinyin: str
    translation: str  # "" quando o Ollama não estava disponível
    bbox: tuple[int, int, int, int]  # (x0, y0, x1, y1) globais


class _Bridge(QObject):
    """Sinais para cruzar da thread do worker para a thread principal (Qt)."""

    cards_updated = pyqtSignal(object)  # (list[CardItem], scope | None)
    quit_app = pyqtSignal()


_CARD_QSS = """
QFrame#chCard {
    background-color: rgba(14, 16, 22, 212);
    /* Borda mais grossa e mais forte: o cartão precisa aparecer sobre
       jogo escuro, não só sobre fundo claro. */
    border: 2px solid rgba(255, 178, 74, 220);
    border-radius: 6px;
}
QLabel#chText { color: #ffffff; font-size: 15px; font-weight: bold; }
QLabel#chPy   { color: #ffd479; font-size: 12px; }
QLabel#chPt   { color: #9fe8a0; font-size: 13px; }
"""


class _Card(QFrame):
    """Cartão com as 3 linhas: chinês, pinyin e tradução."""

    def __init__(self, item: CardItem, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("chCard")
        self.setStyleSheet(_CARD_QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(2)

        lbl_text = QLabel(item.text, self)
        lbl_text.setObjectName("chText")
        # Largura máxima: uma tradução longa não pode virar um banner
        # atravessando a tela do jogo.
        lbl_text.setWordWrap(True)
        lbl_text.setMaximumWidth(340)
        lay.addWidget(lbl_text)

        if item.pinyin.strip():
            lbl_py = QLabel(item.pinyin, self)
            lbl_py.setObjectName("chPy")
            lay.addWidget(lbl_py)

        if item.translation:
            lbl_pt = QLabel(item.translation, self)
            lbl_pt.setObjectName("chPt")
            lbl_pt.setWordWrap(True)
            lbl_pt.setMaximumWidth(340)
            lay.addWidget(lbl_pt)

        self.adjustSize()


class _MonitorWindow(QWidget):
    """Janela de cobertura do monitor: transparente, top-most e click-through."""

    def __init__(self, screen: QScreen, items: list[CardItem]) -> None:
        super().__init__()
        self.setGeometry(screen.geometry())
        # Frameless + Tool: sem borda/título e fora da taskbar.
        # WindowStaysOnTopHint: fica acima do jogo (borderless windowed).
        # WindowTransparentForInput: no Win32 vira WS_EX_TRANSPARENT
        # (click-through): cliques e mouse atravessam e vão para o jogo.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._cards: list[_Card] = []
        self._keepalive: QTimer | None = None
        # O cartão é desenhado ANTES do primeiro show(): nesta máquina o
        # DWM só apresenta de forma confiável a superfície mapeada já com
        # conteúdo. Janela mapeada vazia e depois remapeada (hide/show)
        # com conteúdo falha em reaparecer (verificado empiricamente).
        self.set_cards(items)
        self.show()

    def set_cards(self, items: list[CardItem]) -> None:
        """Desenha os cartões ancorados nas bboxes (posições locais)."""
        geo = self.geometry()
        for card in self._cards:
            card.deleteLater()
        self._cards.clear()

        for item in items:
            card = _Card(item, self)
            self._cards.append(card)

            x0, y0, x1, y1 = item.bbox
            cw, ch = card.width(), card.height()
            # Exatamente sobre o texto original: o canto superior esquerdo
            # do cartão é alinhado ao canto (x0, y0) da bbox do OCR, ou
            # seja, o cartão cobre a linha extraída.
            x = x0 - geo.x()
            y = y0 - geo.y()
            # Mantém o cartão dentro do monitor.
            x = max(8, min(x, geo.width() - cw - 8))
            y = max(8, min(y, geo.height() - ch - 8))
            card.move(x, y)

    def reassert(self) -> None:
        """Força o DWM a reapresentar esta janela.

        O DWM desta máquina (iGPU Intel + EDR) passa por fases em que não
        apresenta a superfície da janela sozinho: a janela existe, é
        topmost, e os cartões já foram desenhados, mas nada aparece na
        tela. Uma chamada SetWindowPos (mesmo com SWP_NOZORDER, sem mudar
        a ordem Z) força o DWM a revalidar a janela e reapresentar a
        superfície; o cartão aparece. Verificado empiricamente numa fase
        ruim, em que o show() sozinho deixava a janela invisível: esta
        chamada fez o cartão aparecer (mais de 1900 pixels de diferença
        na captura de tela).

        Custo: uma chamada Win32 por invocação; zero em repouso.
        """
        hwnd = int(self.winId())
        user32.SetWindowPos(
            hwnd,
            _SWP_INSERT_DUMMY,
            0,
            0,
            0,
            0,
            _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_NOOWNERCHANGING,
        )
        # Segundo truque (também comprovado na bateria de testes): o pulso
        # nos atributos da janela layered força a revalidação do DWM.
        # Juntos, maximizam a chance de a superfície reaparecer em cada
        # pulso do keepalive.
        user32.SetLayeredWindowAttributes(hwnd, 0, 254, _LWA_ALPHA)
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA)

    def _start_keepalive(self) -> None:
        """Timer de 1 s que reafirma a apresentação enquanto houver cartões.

        O DWM desta máquina pode parar de apresentar a janela de novo até
        depois que os pulsos iniciais acabam; a reafirmação periódica faz o
        cartão reaparecer em menos de 1 s. Só roda com cartões visíveis
        (custo zero em repouso).
        """
        if self._keepalive is None:
            self._keepalive = QTimer(self)
            self._keepalive.setInterval(1000)
            self._keepalive.timeout.connect(self.reassert)
        self._keepalive.start()

    def _stop_keepalive(self) -> None:
        if self._keepalive is not None:
            self._keepalive.stop()


class Overlay:
    """Ponto de acesso do overlay: cria as janelas e roda o loop de eventos.

    Uso:
        overlay = Overlay()            # no arranque (thread principal)
        overlay.publish(items, scope)  # de qualquer thread (sinal queued)
        overlay.hide_all()             # esconde tudo (legado)
        overlay.run()                  # thread principal: bloqueia até sair
    """

    def __init__(self) -> None:
        self._app = QApplication.instance() or QApplication([])
        self._bridge = _Bridge()
        self._bridge.cards_updated.connect(self._show_cards)
        self._bridge.quit_app.connect(self._app.quit)
        # Nenhuma janela no arranque: ela é criada a cada F9, com o cartão
        # já desenhado no primeiro show(). Nesta máquina o DWM só apresenta
        # de forma confiável a PRIMEIRA mapeação de uma janela (com
        # conteúdo); janela mapeada vazia e depois remapeada falha. Custo:
        # criação de janela (milissegundos) apenas quando há cartões.
        self._screens: dict[str, QScreen] = {}
        for screen in self._app.screens():
            self._screens[screen.name()] = screen
        self._windows: dict[str, _MonitorWindow] = {}

    # -- API pública (segura para chamar de qualquer thread) ------------
    def publish(
        self,
        items: list[CardItem],
        scope: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Publica os cartões e atualiza só o monitor escaneado (scope).

        :param scope: retângulo global do monitor capturado no F9,
                      (left, top, width, height). Sem texto chinês,
                      esconde apenas os cartões desse monitor; os demais
                      monitores continuam no ar. None = comportamento
                      legado (afeta todos os monitores).
        """
        self._bridge.cards_updated.emit((list(items), scope))

    def show_cards(self, items: list[CardItem]) -> None:
        """Legado: atualiza todos os monitores (sem scope)."""
        self.publish(items, None)

    def hide_all(self) -> None:
        """Legado: esconde os cartões de todos os monitores."""
        self.publish([], None)

    def quit(self) -> None:
        """Pede a saída do app de forma thread-safe (sinal queued)."""
        self._bridge.quit_app.emit()

    def run(self) -> None:
        """Thread principal: bloqueia aqui até o Ctrl+Q sair."""
        self._app.exec()

    # -- internos (rodam na thread principal, chamados via sinal) -------
    def _show_cards(self, payload: tuple) -> None:
        items, scope = payload
        # O F9 afeta só o monitor escaneado (scope). Sem scope (chamadas
        # legadas), afeta todos os monitores.
        if scope is not None:
            left, top, width, height = scope
            cx, cy = left + width // 2, top + height // 2
            touched = {
                name
                for name, screen in self._screens.items()
                if self._in_geometry(screen.geometry(), cx, cy)
            }
            if not touched:  # defensivo: scope fora de todos os monitores
                touched = set(self._screens)
        else:
            touched = set(self._screens)

        # Descarta janelas antigas SÓ nos monitores afetados: a única
        # forma confiável de apresentar nesta máquina é uma mapeação nova
        # com o cartão já desenhado (ver __init__), e os cartões dos
        # demais monitores continuam no ar.
        for name in touched:
            win = self._windows.pop(name, None)
            if win is not None:
                win._stop_keepalive()
                win.close()
                win.deleteLater()

        by_window: dict[str, list[CardItem]] = {name: [] for name in touched}
        for item in items:
            cx = (item.bbox[0] + item.bbox[2]) // 2
            cy = (item.bbox[1] + item.bbox[3]) // 2
            target = next(
                (
                    name
                    for name in touched
                    if self._in_geometry(self._screens[name].geometry(), cx, cy)
                ),
                None,
            )
            if target is None:  # fallback: primeiro monitor afetado
                target = next(iter(by_window))
            by_window[target].append(item)

        for name, bucket in by_window.items():
            if bucket:
                win = _MonitorWindow(self._screens[name], bucket)
                self._windows[name] = win
                # Pulsos de reafirmação: o DWM desta máquina só
                # reapresenta a superfície com um curto atraso após o
                # paint do cartão. Três pulsos cobrem até 2 s.
                for delay in (300, 1000, 2000):
                    QTimer.singleShot(delay, win.reassert)
                # Mantém reapresentando enquanto os cartões estiverem no
                # ar (o DWM pode parar de apresentar a janela de novo).
                win._start_keepalive()
        if items:
            logger.info("Overlay: %d cartão(ões) no ar", len(items))
        elif scope is not None:
            logger.info(
                "Overlay: F9 sem texto chinês: cartões deste monitor "
                "ocultados (demais monitores preservados)"
            )
        else:
            logger.info("Overlay: cartões ocultados (F9 sem texto chinês)")

    @staticmethod
    def _in_geometry(geo: QRect, x: int, y: int) -> bool:
        return (
            geo.x() <= x < geo.x() + geo.width()
            and geo.y() <= y < geo.y() + geo.height()
        )
