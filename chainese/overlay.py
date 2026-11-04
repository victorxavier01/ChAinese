"""
Overlay visual (PyQt6): cartões de tradução flutuando sobre o jogo.

Arquitetura (prioridade: custo zero em repouso + nunca atrapalhar o jogo):
    - Uma janela POR CARTÃO: frameless, OPAÇA, do tamanho exato do cartão,
      always-on-top e "input-transparent" (click-through): mouse e teclado
      atravessam a janela e vão para o jogo. Nada do overlay pode ser
      clicado. A janela também não aceita foco nem é ativada ao aparecer,
      para o jogo não perder o foco quando o cartão surge.
    - OPAÇA de propósito (teste contra o bug "o cartão aparece e some"):
      a antiga janela full-monitor com WA_TranslucentBackground só se
      apresenta via UpdateLayeredWindow (superfície layered de alpha por
      pixel), e o DWM desta máquina (iGPU Intel + EDR) deixava de compor
      essa superfície ~400 ms após a mapeação. A janela opaca segue o
      caminho NORMAL de composição do DWM (sem layered surface); os cantos
      arredondados vêm do SetWindowRgn (GDI), não de alpha por pixel.
    - As janelas vivem na thread principal (loop de eventos do Qt). O
      worker do F9 (outra thread) publica os resultados via sinal Qt; a
      conexão queued faz a passagem entre threads de forma segura. Nenhuma
      chamada Qt acontece fora da thread principal.
    - A janela é ancorada na bbox do OCR (mesma base de coordenadas
      globais do mss): o canto superior esquerdo do cartão é alinhado ao
      canto (x0, y0) da bbox, ou seja, o cartão fica exatamente sobre a
      linha de texto original, mantido dentro do monitor (margem de 8 px).
    - A cada F9 as janelas antigas do monitor escaneado são descartadas e
      criadas novas com o cartão já desenhado no primeiro show() (criar a
      janela já com o conteúdo continua a ser a forma que se apresentou
      com consistência nesta máquina).
    - Reafirmação (ver _CardWindow.reassert): pulsos iniciais + um timer
      de 1 s, só enquanto há cartões no ar. Mantidos como rede de
      segurança enquanto a apresentação opaca é validada; se a
      estabilidade se confirmar, são removidos (custo zero em repouso é a
      meta).
    - Os timers de pulso são filhos da própria janela: morrem junto com
      ela, então nunca disparam em um objeto Qt já destruído (isso
      abortaria o processo).
    - Cada F9 atualiza só o monitor escaneado: F9 sem chinês num monitor
      esconde os cartões daquele monitor e preserva os dos demais.
    - Em repouso não há timer nem animação: o custo de CPU entre F9 é
      praticamente zero.
    - Diagnóstico: se uma janela for escondida por fora do app (jogo
      retomando a tela, DWM, outro programa), aparece um WARNING no log.
    - DPI: assume escala 100% (mss e Qt usam pixels físicos na máquina do
      usuário). Multi-escala fica para o passo 5.
"""

from __future__ import annotations

import ctypes
import logging
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
# Win32: reafirmar a apresentação (ver _CardWindow.reassert) e cantos
# arredondados (ver _CardWindow._round_corners)
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
# SetWindowPos: SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER com (0,0,0,0) força o
# DWM a revalidar a janela sem mexer em posição/tamanho/ordem z.
# IMPORTANTE: sem SWP_NOMOVE|SWP_NOSIZE os argumentos 0,0,0,0 MOVIAM a
# janela para (0,0) — bug encontrado na fase opaca (o cartão saltava para
# o canto do desktop a cada pulso).
# Não usar HWND_TOPMOST (-1) como hWndInsertAfter: ctypes não converte -1
# nem o valor 64 bits equivalente (ArgumentError), e exceção dentro do
# slot do Qt aborta o processo inteiro.
_SWP_INSERT_DUMMY = 1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_NOOWNERCHANGING = 0x0020
# HGDIOBJ/HHRGN são handles de 64 bits: sem restype/argtypes explícitos o
# ctypes corrói o valor (DeleteObject/SetWindowRgn receberiam um handle
# truncado). Definido UMA vez, no arranque do módulo.
gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]

# Raio dos cantos da janela (0 = cantos retos, fallback para debug).
# Mesma radiação do QSS border-radius do cartão (6 px); o diâmetro da
# elipse do CreateRoundRectRgn é o dobro.
_CORNER_RADIUS_PX = 6

# Pulsos logo após o show() (ms) e período do keepalive (ms).
_PULSE_DELAYS_MS = (300, 1000, 2000)
_KEEPALIVE_MS = 1000


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


class _CardWindow(QWidget):
    """Janela OPAÇA do tamanho exato do cartão: top-most e click-through.

    Substitui a antiga janela full-monitor com WA_TranslucentBackground:
    a superfície layered (alpha por pixel) era deixada de compor pelo DWM
    desta máquina centenas de ms após a mapeação. Janela opaca vai pelo
    caminho normal de composição; os cantos arredondados vêm do
    SetWindowRgn (a área fora da região não é composta, sem layered
    window, sem alpha por pixel).
    """

    def __init__(self, screen: QScreen, item: CardItem) -> None:
        super().__init__()
        self._label = screen.name()
        # True quando o PRÓPRIO app está fechando a janela (dispose): o
        # hideEvent só vira WARNING quando a janela some por fora.
        self._closing = False
        # Fundo sólido escuro (a versão opaca do antigo rgba(14,16,22,212)):
        # cobre a janela inteira — nem 1 px à cor do palete por omissão.
        self.setObjectName("chOverlay")
        self.setStyleSheet("QWidget#chOverlay { background-color: #0e1016; }")
        # Frameless + Tool: sem borda/título e fora da taskbar.
        # WindowStaysOnTopHint: fica acima do jogo (borderless windowed).
        # WindowTransparentForInput: no Win32 vira WS_EX_TRANSPARENT
        # (click-through): cliques e mouse atravessam e vão para o jogo.
        # WindowDoesNotAcceptFocus + WA_ShowWithoutActivating: a janela
        # não é ativada ao aparecer, então o jogo mantém o foco (um jogo
        # que perde o foco pode minimizar, pausar ou retomar a tela).
        # Sem WA_TranslucentBackground de propósito: ver docstring da
        # classe (janela opaca = composição normal do DWM).
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # O cartão existe ANTES do primeiro show() (a janela é criada já
        # com o conteúdo desenhado, a única forma que se apresentou com
        # consistência nesta máquina).
        card = _Card(item, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(card)
        # A janela tem EXATAMENTE o tamanho do cartão (o layout preenche).
        self.setFixedSize(card.width(), card.height())

        # Âncora: canto (x0, y0) da bbox do OCR em coordenadas globais,
        # mantido dentro do monitor (margem de 8 px).
        geo = screen.geometry()
        cw, ch = self.width(), self.height()
        x = max(geo.x() + 8, min(item.bbox[0], geo.x() + geo.width() - cw - 8))
        y = max(geo.y() + 8, min(item.bbox[1], geo.y() + geo.height() - ch - 8))
        self.move(x, y)

        # Timers filhos da janela: são destruídos com ela, então nunca
        # disparam em um objeto Qt já destruído.
        self._pulses: list[QTimer] = []
        for delay in _PULSE_DELAYS_MS:
            pulse = QTimer(self)
            pulse.setSingleShot(True)
            pulse.setInterval(delay)
            pulse.timeout.connect(self.reassert)
            self._pulses.append(pulse)
        self._keepalive = QTimer(self)
        self._keepalive.setInterval(_KEEPALIVE_MS)
        self._keepalive.timeout.connect(self.reassert)

        self.show()
        self._round_corners()

    def _round_corners(self) -> None:
        """Arredonda os cantos da janela com SetWindowRgn (GDI).

        A área fora da região deixa de ser composta pelo DWM — sem janela
        layered e sem alpha por pixel. Mesma radiação do QSS
        border-radius: raio 6 px -> elipse de 12 px de CreateRoundRectRgn.
        Nunca levanta exceção: roda no arranque da janela (exceção aqui
        abortaria o processo).
        """
        if _CORNER_RADIUS_PX <= 0:
            return
        try:
            region = gdi32.CreateRoundRectRgn(
                0,
                0,
                self.width(),
                self.height(),
                2 * _CORNER_RADIUS_PX,
                2 * _CORNER_RADIUS_PX,
            )
            if not region:
                return
            user32.SetWindowRgn(int(self.winId()), region, True)
            gdi32.DeleteObject(region)
        except Exception:
            logger.exception(
                "Overlay: falha no SetWindowRgn de %s (ignorada)", self._label
            )

    def reassert(self) -> None:
        """Reafirma a apresentação desta janela no DWM (rede de segurança).

        Com a janela opaca o caminho de apresentação é o normal do DWM;
        os pulsos continuam a correr enquanto a estabilidade é validada
        (ver docstring do módulo). Dois passos:

        1. SetWindowPos com SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER: força o
           DWM a revalidar a janela sem mexer em posição/tamanho/ordem z.
        2. update(): pede um repaint do Qt.

        NÃO chamar SetLayeredWindowAttributes aqui: isso converte a janela
        para uma superfície layered de "alpha constante" e devolve-nos o
        bug original (o cartão some).

        Nunca levanta exceção: roda em slot do Qt (exceção não tratada em
        slot aborta o processo).

        Custo: uma chamada Win32 + um repaint por invocação; zero em
        repouso.
        """
        if self._closing:
            return
        try:
            hwnd = int(self.winId())
            user32.SetWindowPos(
                hwnd,
                _SWP_INSERT_DUMMY,
                0,
                0,
                0,
                0,
                _SWP_NOSIZE
                | _SWP_NOMOVE
                | _SWP_NOZORDER
                | _SWP_NOACTIVATE
                | _SWP_NOOWNERCHANGING,
            )
            self.update()
        except Exception:
            logger.exception("Overlay: falha no reassert de %s (ignorada)", self._label)

    def start_presenting(self) -> None:
        """Dispara os pulsos iniciais e o keepalive de 1 s.

        O keepalive só roda com cartões visíveis (custo zero em repouso).
        """
        for pulse in self._pulses:
            pulse.start()
        self._keepalive.start()

    def stop_presenting(self) -> None:
        for pulse in self._pulses:
            pulse.stop()
        self._keepalive.stop()

    def dispose(self) -> None:
        """Fecha e descarta a janela (chamado pelo Overlay)."""
        self._closing = True
        self.stop_presenting()
        self.close()
        self.deleteLater()

    def hideEvent(self, event) -> None:  # noqa: N802 (nome do Qt)
        if not self._closing:
            logger.warning(
                "Overlay: a janela de %s foi ESCONDIDA por fora do app "
                "(jogo retomando a tela? outro programa?)",
                self._label,
            )
        super().hideEvent(event)


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
        self._bridge.quit_app.connect(self._shutdown)
        # Nenhuma janela no arranque: elas são criadas a cada F9, já com o
        # cartão desenhado no primeiro show(). Custo: criação de janela
        # (milissegundos) apenas quando há cartões.
        self._screens: dict[str, QScreen] = {}
        for screen in self._app.screens():
            self._screens[screen.name()] = screen
        # monitor -> janelas (agora 1 janela por cartão; ver _CardWindow).
        self._windows: dict[str, list[_CardWindow]] = {}

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
    def _shutdown(self) -> None:
        """Fecha as janelas e sai (chamado via sinal, na thread principal).

        O dispose() marca _closing, pelo que o hideEvent do teardown não
        gera WARNINGs falsos (o aviso é só para janelas escondidas por
        FORA do app — o sinal de diagnóstico que interessa).
        """
        for wins in self._windows.values():
            for win in wins:
                win.dispose()
        self._windows.clear()
        self._app.quit()

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

        # Descarta as janelas antigas SÓ nos monitores afetados: os
        # cartões dos demais monitores continuam no ar.
        for name in touched:
            for win in self._windows.pop(name, []):
                win.dispose()

        # 1 janela por cartão, ancorada na bbox (ver _CardWindow). O
        # monitor de referência é o que contém o centro da bbox; fallback:
        # o primeiro monitor afetado.
        fallback = next(iter(touched), None)
        for item in items:
            cx = (item.bbox[0] + item.bbox[2]) // 2
            cy = (item.bbox[1] + item.bbox[3]) // 2
            name = next(
                (
                    n
                    for n in touched
                    if self._in_geometry(self._screens[n].geometry(), cx, cy)
                ),
                fallback,
            )
            if name is None:  # sem monitores conhecidos (defensivo)
                continue
            win = _CardWindow(self._screens[name], item)
            self._windows.setdefault(name, []).append(win)
            # Pulsos iniciais + keepalive de 1 s enquanto o cartão estiver
            # no ar (timers filhos da janela).
            win.start_presenting()

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
