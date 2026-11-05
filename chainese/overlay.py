"""
Overlay visual (PyQt6): cartões de tradução flutuando sobre o jogo.

Arquitetura (prioridade: custo zero em repouso + nunca atrapalhar o jogo):
    - Uma janela POR CARTÃO: frameless, OPAÇA, do tamanho exato do cartão,
      always-on-top e "input-transparent" (click-through): mouse e teclado
      atravessam a janela e vão para o jogo. Nada do overlay pode ser
      clicado. A janela também não aceita foco nem é ativada ao aparecer,
      para o jogo não perder o foco quando o cartão surge.
    - Um painel de vocabulário (_VocabWindow): coluna vertical com
      palavra + pinyin → tradução, fixa ao canto superior esquerdo do
      monitor escaneado, com o mesmo tratamento de janela dos cartões.
      Paginado (F8/F10): a janela guarda a lista COMPLETA do último F9 e
      mostra VOCAB_PAGE_SIZE palavras por página; o rodapé indica
      "página X/Y". O painel é click-through, por isso a paginação vem
      das hotkeys GLOBAIS do app (sem depender de foco), nunca de
      interação com a janela.
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
    - A janela do cartão é ancorada na bbox do OCR (mesma base de
      coordenadas globais do mss): o canto superior esquerdo do cartão é
      alinhado ao canto (x0, y0) da bbox, ou seja, o cartão fica
      exatamente sobre a linha de texto original. O painel de vocabulário
      fica fixo no canto superior esquerdo do monitor (margem de 16 px).
    - Sem sobreposição (ver _Overlay._layout_monitor): o painel é fixo e
      vira obstáculo; os cartões são posicionados em ordem de leitura (cima
      para baixo) e cada um é empurrado de forma MONÓTONA (para baixo;
      para cima se não couber; troca de coluna como último recurso) até
      deixar de tocar qualquer janela já posicionada — o empurrão monótono
      evita a oscilação A<->B do antigo empurrão alternado, que ao
      esgotar os passes aceitava sobreposição total.
    - A cada F9 as janelas antigas do monitor escaneado são descartadas e
      criadas novas com o cartão já desenhado no primeiro show() (criar a
      janela já com o conteúdo continua sendo a única forma que se mostrou
      consistente nesta máquina).
    - Reafirmação (ver _FloatWindow.reassert): pulsos iniciais + um timer
      de 1 s, só enquanto há cartões no ar. Mantidos como rede de
      segurança enquanto a apresentação opaca é validada; se a
      estabilidade se confirmar, são removidos (custo zero em repouso é a
      meta).
    - Os timers de pulso são filhos da própria janela: morrem junto com
      ela, então nunca disparam em um objeto Qt já destruído (isso
      abortaria o processo).
    - Cada F9 atualiza só o monitor escaneado: F9 sem chinês num monitor
      esconde os cartões daquele monitor (e o painel) e preserva os dos
      demais.
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
from PyQt6.QtGui import QFontMetrics, QScreen, QTextDocument
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from . import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32: reafirmar a apresentação (ver _FloatWindow.reassert) e cantos
# arredondados (ver _FloatWindow._round_corners)
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
# slot do Qt abortaria o processo inteiro.
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
# Mesma radiação do QSS border-radius do cartão/painel (6 px); o diâmetro
# da elipse do CreateRoundRectRgn é o dobro.
_CORNER_RADIUS_PX = 6

# Pulsos logo após o show() (ms) e período do keepalive (ms).
_PULSE_DELAYS_MS = (300, 1000, 2000)
_KEEPALIVE_MS = 1000

# Margem do painel de vocabulário em relação ao canto do monitor (px) e
# distância mínima entre janelas vizinhas na resolução de colisões (px).
_PANEL_MARGIN_PX = 16
_LAYOUT_GAP_PX = 6

# Largura máxima da coluna de tradução do painel (px): o mesmo teto do
# setMaximumWidth dos rótulos — o texto mais longo quebra em 2-3 linhas.
_VOCAB_TRANS_MAX_PX = 200


@dataclass
class CardItem:
    """Linha de texto pronta para exibição (coordenadas globais de tela)."""

    text: str
    pinyin: str
    translation: str  # "" quando o Ollama não estava disponível
    bbox: tuple[int, int, int, int]  # (x0, y0, x1, y1) globais


@dataclass
class VocabItem:
    """Palavra para o painel de vocabulário (canto superior esquerdo)."""

    word: str
    pinyin: str
    translation: str  # "" quando o Ollama não estava disponível


class _Bridge(QObject):
    """Sinais para cruzar da thread do worker para a thread principal (Qt)."""

    cards_updated = pyqtSignal(object)  # (list[CardItem], scope, list[VocabItem])
    vocab_paged = pyqtSignal(int)  # delta de página do painel (F8 = -1, F10 = +1)
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

_VOCAB_QSS = """
QFrame#chVocab {
    background-color: rgba(14, 16, 22, 212);
    border: 2px solid rgba(255, 178, 74, 220);
    border-radius: 6px;
}
QLabel#chVw { color: #ffffff; font-size: 14px; font-weight: bold; }
QLabel#chVp { color: #ffd479; font-size: 12px; }
QLabel#chVt { color: #9fe8a0; font-size: 13px; }
/* Rodapé de paginação ("página 2/3 · 24 palavras"): discreto. */
QLabel#chVf { color: #8a8f9c; font-size: 11px; }
"""


class _Card(QFrame):
    """Cartão com as 3 linhas: chinês, pinyin e tradução.

    Os rótulos têm tamanho FIXO medido no motor de renderização
    (ver _fit_label): o sizeHint de um QLabel com word wrap não é
    determinístico — depende da largura ATUAL do widget (~0 no
    construtor, e pode mudar entre o adjustSize() e o show()). Sem o
    fix, a janela setFixedSize() no primeiro show() cortava o
    texto/tradução quando o conteúdo precisava quebrar ("as vezes não
    possui tamanho vertical").
    """

    # Largura máxima de texto quebrável (px): o mesmo teto do
    # setMaximumWidth dos rótulos — o conteúdo mais longo quebra em
    # 2-3 linhas, nunca vira um banner atravessando a tela do jogo.
    _MAX_TEXT_PX = 340

    def __init__(self, item: CardItem, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("chCard")
        self.setStyleSheet(_CARD_QSS)
        lay = QVBoxLayout(self)
        # Margens apertadas: o cartão é leitura rápida sobre o jogo — o
        # texto deve preencher o cartão, não flutuar dentro dele. (Com a
        # margem de documento a 0 em _fit_label, estas são as únicas
        # margens internas do cartão.)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(2)

        lbl_text = QLabel(item.text, self)
        lbl_text.setObjectName("chText")
        lbl_text.setWordWrap(True)
        lbl_text.setMaximumWidth(_Card._MAX_TEXT_PX)
        self._fit_label(lbl_text)
        lay.addWidget(lbl_text, 0, Qt.AlignmentFlag.AlignLeft)

        if item.pinyin.strip():
            lbl_py = QLabel(item.pinyin, self)
            lbl_py.setObjectName("chPy")
            self._fit_label(lbl_py)
            lay.addWidget(lbl_py, 0, Qt.AlignmentFlag.AlignLeft)

        if item.translation:
            lbl_pt = QLabel(item.translation, self)
            lbl_pt.setObjectName("chPt")
            lbl_pt.setWordWrap(True)
            lbl_pt.setMaximumWidth(_Card._MAX_TEXT_PX)
            self._fit_label(lbl_pt)
            lay.addWidget(lbl_pt, 0, Qt.AlignmentFlag.AlignLeft)

        self.adjustSize()

    @staticmethod
    def _fit_label(lbl: QLabel) -> None:
        """Fixa o tamanho do rótulo medindo o conteúdo na fonte real.

        Sem wrap: largura/altura exatas do texto. Com wrap: largura =
        natural limitada a _MAX_TEXT_PX e altura = a quebra REAL nessa
        largura, medida com QTextDocument — o MESMO motor que o QLabel
        usa para renderizar o texto quebrado. A margem do documento vai
        a 0 (o padrão de 4 px somava +8 px de altura/largura por
        rótulo — o "espaçamento interno" grande do cartão). O
        setFixedSize trava o mínimo E o máximo: nenhum layout consegue
        espremer o rótulo (o aperto era a causa do corte vertical).
        """
        # Garante a fonte do QSS (15px bold / 12px / 13px) antes de medir.
        lbl.ensurePolished()
        fm = QFontMetrics(lbl.font())
        text = lbl.text()
        w = fm.horizontalAdvance(text)
        if lbl.wordWrap():
            w = min(w, _Card._MAX_TEXT_PX)
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        doc.setDefaultFont(lbl.font())
        doc.setTextWidth(w)
        doc.setPlainText(text)
        lbl.setFixedSize(w, int(doc.size().height()))


class _VocabPanel(QFrame):
    """Painel de vocabulário: coluna vertical de palavra (pinyin) → pt.

    Uma LINHA por palavra: [palavra] [pinyin] [→ tradução] num HBox com
    10 px entre os rótulos — sem colunas fixas de grid: a coluna de uma
    grid tem a largura da célula mais larga da página, e nas linhas curtas
    isso vira um vão grande entre o pinyin e a tradução ("espaçamento
    grande"). Todos os rótulos têm tamanho fixo medido no motor de
    renderização (ver _fit_row_labels): o painel fica denso (altura exata
    da fonte por linha, sem o ar do sizeHint) e determinístico, com a
    largura da linha mais longa, cores idênticas às do cartão.

    :param page_label: rodapé opcional de paginação ("página 2/3 · 24
                       palavras"); vazio = sem rodapé (1 página).
    """

    def __init__(
        self,
        items: list[VocabItem],
        parent: QWidget,
        page_label: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("chVocab")
        self.setStyleSheet(_VOCAB_QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(4)

        rows: list[tuple[QLabel, QLabel, QLabel]] = []
        for item in items:
            lbl_word = QLabel(item.word, self)
            lbl_word.setObjectName("chVw")
            lbl_py = QLabel(item.pinyin, self)
            lbl_py.setObjectName("chVp")
            # "?" quando o Ollama não estava disponível: o painel continua
            # útil (palavra + pinyin) sem parecer que falhou o layout.
            lbl_pt = QLabel(f"→ {item.translation or '?'}", self)
            lbl_pt.setObjectName("chVt")
            # A tradução é a única que pode quebrar: teto de largura +
            # wrap; o tamanho final é fixado em _fit_row_labels.
            lbl_pt.setWordWrap(True)
            lbl_pt.setMaximumWidth(_VOCAB_TRANS_MAX_PX)
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(lbl_word)
            row.addWidget(lbl_py)
            row.addWidget(lbl_pt)
            # O stretch à direita cola o conteúdo à esquerda da linha:
            # sem ele a linha (esticada à largura do painel pela VBox)
            # distribui o espaço excedente ENTRE os rótulos e as linhas
            # curtas voltam a parecer colunas com vão.
            row.addStretch(1)
            lay.addLayout(row)
            rows.append((lbl_word, lbl_py, lbl_pt))

        # Rodapé de paginação: linha discreta após as palavras.
        lbl_page = None
        if page_label:
            lbl_page = QLabel(page_label, self)
            lbl_page.setObjectName("chVf")
            lay.addWidget(lbl_page)

        self._fit_row_labels(rows, lbl_page)

        self.adjustSize()

    @staticmethod
    def _fit_row_labels(
        rows: list[tuple[QLabel, QLabel, QLabel]],
        lbl_page: QLabel | None = None,
    ) -> None:
        """Fixa o tamanho de cada rótulo medindo no motor de renderização.

        Mesmo padrão do _Card._fit_label: o sizeHint de um QLabel depende
        da largura ATUAL do widget (~0 no construtor) — indeterminado para
        rótulos com word wrap — e o sizeHint de linha única costuma sair
        mais alto do que a linha renderizada (linhas "espaçadas"). Medir
        com QTextDocument (o MESMO motor que o QLabel usa para desenhar)
        e travar width+height com setFixedSize deixa cada linha com a
        altura exata da fonte e a largura exata do conteúdo: painel denso
        e determinístico, sem corte.
        """
        if not rows:
            return
        for lw, lp, lt in rows:
            # O QSS aplica a fonte (14px bold / 12px / 13px) no polish:
            # garantir que já está efetiva antes de medir.
            lw.ensurePolished()
            lp.ensurePolished()
            lt.ensurePolished()

        for lw, lp, lt in rows:
            for lbl, wrap in ((lw, None), (lp, None), (lt, _VOCAB_TRANS_MAX_PX)):
                fm = QFontMetrics(lbl.font())
                text = lbl.text()
                w = fm.horizontalAdvance(text)
                if wrap is not None:
                    w = min(w, wrap)
                doc = QTextDocument()
                doc.setDocumentMargin(0)  # sem a margem padrão de 4 px:
                # size() viria +8 px em altura/largura (linhas "espaçadas")
                doc.setDefaultFont(lbl.font())
                doc.setTextWidth(w)
                doc.setPlainText(text)
                lbl.setFixedSize(w, int(doc.size().height()))

        if lbl_page is not None:
            lbl_page.ensurePolished()
            fm = QFontMetrics(lbl_page.font())
            w = fm.horizontalAdvance(lbl_page.text())
            doc = QTextDocument()
            doc.setDocumentMargin(0)
            doc.setDefaultFont(lbl_page.font())
            doc.setTextWidth(w)
            doc.setPlainText(lbl_page.text())
            lbl_page.setFixedSize(w, int(doc.size().height()))


class _FloatWindow(QWidget):
    """Base das janelas flutuantes do overlay: opaca, top-most e
    click-through, com cantos arredondados e pulsos de reafirmação.

    Os subclasses montam o conteúdo no construtor (ANTES de place());
    place() posiciona e apresenta — a janela só é mapeada já com o
    conteúdo desenhado, a única forma que se apresentou com consistência
    nesta máquina.
    """

    def __init__(self, label: str) -> None:
        super().__init__()
        self._label = label
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
        # Sem WA_TranslucentBackground de propósito: janela opaca =
        # composição normal do DWM (ver docstring do módulo).
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

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

    def place(self, x: int, y: int) -> None:
        """Posiciona (coordenadas globais) e apresenta a janela."""
        self.move(x, y)
        self.show()
        self._round_corners()
        self.start_presenting()

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
        os pulsos continuam rodando enquanto a estabilidade é validada
        (ver docstring do módulo). Dois passos:

        1. SetWindowPos com SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER: força o
           DWM a revalidar a janela sem mexer em posição/tamanho/ordem z.
        2. update(): pede um repaint do Qt.

        NÃO chamar SetLayeredWindowAttributes aqui: isso converte a janela
        para uma superfície layered de "alpha constante" e devolve-nos o
        bug original (o cartão some).

        Nunca levanta exceção: roda em slot do Qt (exceção não tratada em
        slot abortaria o processo).

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

        O keepalive só roda com janelas visíveis (custo zero em repouso).
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


class _CardWindow(_FloatWindow):
    """Janela OPAÇA do tamanho exato do cartão (uma linha de tradução).

    Substitui a antiga janela full-monitor com WA_TranslucentBackground:
    a superfície layered (alpha por pixel) deixava de ser composta pelo DWM
    desta máquina centenas de ms após a mapeação.
    """

    def __init__(self, screen: QScreen, item: CardItem) -> None:
        super().__init__(screen.name())
        # Âncora da janela (canto da bbox do OCR); o _layout_monitor usa
        # para posicionar a janela e resolver colisões.
        self._anchor = (item.bbox[0], item.bbox[1])

        card = _Card(item, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(card)
        # A janela tem EXATAMENTE o tamanho do cartão (o layout preenche).
        self.setFixedSize(card.width(), card.height())


class _VocabWindow(_FloatWindow):
    """Janela do painel de vocabulário (canto superior esquerdo do
    monitor escaneado).

    Paginado: a janela guarda a lista COMPLETA do último F9 e o número
    da página atual; `show_page()` troca o conteúdo NO LOCAL (sem janela
    nova — a criação já com conteúdo desenhado é o caminho validado, mas
    recriar a janela a cada F8/F10 seria um overkill). A âncora topo-
    esquerdo mantém x,y; a janela só cresce/enxuga para baixo.
    """

    def __init__(
        self,
        screen: QScreen,
        items: list[VocabItem],
        page: int = 1,
    ) -> None:
        super().__init__(screen.name())
        self._items = list(items)  # lista completa (o último F9)
        self._page = max(1, min(page, self.page_count()))
        panel = self._build_panel()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(panel)
        # A janela tem EXATAMENTE o tamanho do painel (idêntico ao
        # _CardWindow).
        self.setFixedSize(panel.width(), panel.height())

    # -- paginação ------------------------------------------------------
    @property
    def page(self) -> int:
        """Página atual (1-based)."""
        return self._page

    def page_count(self) -> int:
        """Nº de páginas da lista completa (1 quando vazia)."""
        if not self._items:
            return 1
        size = config.VOCAB_PAGE_SIZE
        return (len(self._items) + size - 1) // size

    def show_page(self, page: int) -> None:
        """Troca para a página `page` (1-based) reconstruindo o painel.

        A janela já foi apresentada (place()): troca-se o _VocabPanel no
        layout, ajusta-se o tamanho (âncora topo-esquerdo: x,y fixos) e
        reafirma-se a apresentação — a região dos cantos arredondados
        depende do tamanho e o reassert força o DWM a revalidar (os
        timers keepalive continuam rodando, são filhos da janela).
        """
        page = max(1, min(page, self.page_count()))
        if page == self._page:
            return
        self._page = page
        old = self._panel
        new = self._build_panel()
        lay = self.layout()
        lay.removeWidget(old)
        old.close()
        old.deleteLater()
        lay.addWidget(new)
        self.setFixedSize(new.width(), new.height())
        self._round_corners()
        self.reassert()

    def _build_panel(self) -> _VocabPanel:
        """Monta o _VocabPanel da página atual (slice da lista completa)."""
        size = config.VOCAB_PAGE_SIZE
        start = (self._page - 1) * size
        items = self._items[start : start + size]
        # Rodapé só com mais de uma página: 1 página = painel idêntico
        # ao anterior (sem "página 1/1" a ocupar espaço).
        label = ""
        if self.page_count() > 1:
            label = f"página {self._page}/{self.page_count()} · {len(self._items)} palavras"
        self._panel = _VocabPanel(items, self, page_label=label)
        return self._panel


class Overlay:
    """Ponto de acesso do overlay: cria as janelas e roda o loop de eventos.

    Uso:
        overlay = Overlay()                 # no arranque (thread principal)
        overlay.publish(items, scope, vocab)  # de qualquer thread (queued)
        overlay.hide_all()                  # esconde tudo (legado)
        overlay.run()                       # thread principal: bloqueia até sair
    """

    def __init__(self) -> None:
        self._app = QApplication.instance() or QApplication([])
        self._bridge = _Bridge()
        self._bridge.cards_updated.connect(self._show_cards)
        self._bridge.vocab_paged.connect(self._page_vocab)
        self._bridge.quit_app.connect(self._shutdown)
        # Nenhuma janela no arranque: elas são criadas a cada F9, já com o
        # conteúdo desenhado no primeiro show(). Custo: criação de janela
        # (milissegundos) apenas quando há cartões.
        self._screens: dict[str, QScreen] = {}
        for screen in self._app.screens():
            self._screens[screen.name()] = screen
        # monitor -> janelas (1 janela por cartão + o painel, se houver).
        self._windows: dict[str, list[_FloatWindow]] = {}
        # Painel do último F9 (para F8/F10): None quando não há painel no
        # ar (ou quando o monitor que o trazia foi descartado por F9).
        self._vocab_win: _VocabWindow | None = None

    # -- API pública (segura para chamar de qualquer thread) ------------
    def publish(
        self,
        items: list[CardItem],
        scope: tuple[int, int, int, int] | None = None,
        vocab: list[VocabItem] | None = None,
    ) -> None:
        """Publica os cartões e atualiza só o monitor escaneado (scope).

        :param scope: retângulo global do monitor capturado no F9,
                      (left, top, width, height). Sem texto chinês,
                      esconde apenas os cartões desse monitor; os demais
                      monitores continuam no ar. None = comportamento
                      legado (afeta todos os monitores).
        :param vocab: palavras para o painel de vocabulário (canto
                      superior esquerdo do monitor escaneado). None ou
                      vazio = sem painel. A paginação reseta à página 1
                      (F8/F10 folheiam entre F9s).
        """
        self._bridge.cards_updated.emit(
            (list(items), scope, list(vocab or []))
        )

    def page_vocab(self, delta: int) -> None:
        """Folheia o painel de vocabulário (F8 = -1, F10 = +1).

        Thread-safe (sinal queued, como `publish`). Sem painel no ar:
        no-op. A página é clamped a 1..nº de páginas: nas bordas,
        no-op com log (sem circular, por decisão de uso).
        """
        self._bridge.vocab_paged.emit(delta)

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
        items, scope, vocab = payload
        # O F9 afeta só o monitor escaneado (scope). Sem scope (chamadas
        # legadas), afeta todos os monitores.
        center = self._scope_center(scope)
        if center is not None:
            touched = {
                name
                for name, screen in self._screens.items()
                if self._in_geometry(screen.geometry(), *center)
            }
            if not touched:  # defensivo: scope fora de todos os monitores
                touched = set(self._screens)
        else:
            touched = set(self._screens)

        # Descarta as janelas antigas SÓ nos monitores afetados: os
        # cartões dos demais monitores continuam no ar. Se o painel
        # descartado for o referenciado para F8/F10, a referência desliga
        # (as hotkeys passam a no-op até ao próximo F9 com vocabulário).
        for name in touched:
            for win in self._windows.pop(name, []):
                if win is self._vocab_win:
                    self._vocab_win = None
                win.dispose()

        by_monitor: dict[str, list[_FloatWindow]] = {name: [] for name in touched}
        fallback = next(iter(touched), None)

        # 1 janela por cartão. O monitor de referência é o que contém o
        # centro da bbox; fallback: o primeiro monitor afetado.
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
            by_monitor[name].append(_CardWindow(self._screens[name], item))

        # Painel de vocabulário: canto superior esquerdo do monitor
        # escaneado (o monitor do scope; sem scope, o 1º monitor).
        # Paginado: a janela guarda a lista completa e começa na página 1
        # (cada F9 reseta a paginação; F8/F10 folheiam — ver page_vocab).
        if vocab and fallback is not None:
            if center is not None:
                pmon = next(
                    (
                        n
                        for n in touched
                        if self._in_geometry(self._screens[n].geometry(), *center)
                    ),
                    fallback,
                )
            else:
                pmon = fallback
            vwin = _VocabWindow(self._screens[pmon], vocab)
            self._vocab_win = vwin
            by_monitor.setdefault(pmon, []).append(vwin)

        # Layout + apresentação, monitor a monitor.
        for name, wins in by_monitor.items():
            self._layout_monitor(self._screens[name], wins)

        # Regista as janelas por monitor: o próximo F9 (ou o _shutdown)
        # descarta exactamente estas (ver o pop() acima). Sem este update
        # as janelas ficariam órfãs (nunca descartadas) e o vis_check não
        # as encontraria.
        self._windows.update(by_monitor)

        if items:
            extra = f" + painel ({len(vocab)} palavras)" if vocab else ""
            logger.info("Overlay: %d cartão(ões) no ar%s", len(items), extra)
        elif scope is not None:
            logger.info(
                "Overlay: F9 sem texto chinês: cartões deste monitor "
                "ocultados (demais monitores preservados)"
            )
        else:
            logger.info("Overlay: cartões ocultados (F9 sem texto chinês)")

    def _page_vocab(self, delta: int) -> None:
        """Aplica o delta de página (thread principal, via sinal).

        Só o painel do último F9 (self._vocab_win) é folheável; os
        cartões — e eventuais painéis de outros monitores — não são
        tocados.
        """
        win = self._vocab_win
        # _closing antes de isVisible(): depois de dispose() o objeto C++
        # pode já ter sido apagado (deleteLater) — ler o atributo Python é
        # sempre seguro, tocar no C++ não.
        if win is None or win._closing or not win.isVisible():
            logger.info("Overlay: F8/F10 sem painel de vocabulário no ar (no-op)")
            return
        total = win.page_count()
        new_page = win.page + delta
        if not 1 <= new_page <= total:
            logger.info(
                "Overlay: painel já na página %s de %s (F8/F10 no-op)",
                win.page,
                total,
            )
            return
        win.show_page(new_page)
        logger.info(
            "Overlay: painel de vocabulário na página %s de %s", new_page, total
        )

    def _layout_monitor(self, screen: QScreen, wins: list[_FloatWindow]) -> None:
        """Posiciona as janelas de um monitor e as apresenta.

        O painel de vocabulário (se existir) é FIXO no canto superior
        esquerdo e vira obstáculo; os cartões são posicionados em ordem de
        leitura (cima para baixo) na âncora da bbox do OCR e cada um é
        empurrado monotonamente até deixar de tocar qualquer janela já
        posicionada — os cartões nunca se sobrepõem (ver _resolve_collision).
        """
        geo = screen.geometry()
        panel = next((w for w in wins if isinstance(w, _VocabWindow)), None)
        cards = [w for w in wins if isinstance(w, _CardWindow)]

        placed: list[tuple[int, int, int, int]] = []

        # Painel: canto superior esquerdo, margem fixa.
        if panel is not None:
            px = geo.x() + _PANEL_MARGIN_PX
            py = geo.y() + _PANEL_MARGIN_PX
            placed.append((px, py, panel.width(), panel.height()))

        # Cartões: âncora (x0, y0) da bbox, em ordem de leitura.
        positions: dict[_CardWindow, tuple[int, int, int, int]] = {}
        for win in sorted(cards, key=lambda w: (w._anchor[1], w._anchor[0])):
            x0, y0 = win._anchor
            rect = self._clamp_rect((x0, y0, win.width(), win.height()), geo)
            rect = self._resolve_collision(rect, placed, geo, gap=_LAYOUT_GAP_PX)
            placed.append(rect)
            positions[win] = rect

        # Apresentação (a janela só é mapeada já posicionada).
        for win, (x, y, _, _) in positions.items():
            win.place(x, y)
        if panel is not None:
            px, py = placed[0][0], placed[0][1]
            panel.place(px, py)

    @staticmethod
    def _clamp_rect(rect: tuple[int, int, int, int], geo: QRect) -> tuple[int, int, int, int]:
        """Mantém o retângulo dentro do monitor (margem de 8 px)."""
        x, y, w, h = rect
        x = max(geo.x() + 8, min(x, geo.x() + geo.width() - w - 8))
        y = max(geo.y() + 8, min(y, geo.y() + geo.height() - h - 8))
        return (x, y, w, h)

    @staticmethod
    def _rects_overlap(
        a: tuple[int, int, int, int],
        b: tuple[int, int, int, int],
        gap: int,
    ) -> bool:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return (
            ax < bx + bw + gap
            and ax + aw + gap > bx
            and ay < by + bh + gap
            and ay + ah + gap > by
        )

    @classmethod
    def _resolve_collision(
        cls,
        rect: tuple[int, int, int, int],
        obstacles: list[tuple[int, int, int, int]],
        geo: QRect,
        gap: int,
        max_passes: int = 16,
    ) -> tuple[int, int, int, int]:
        """Empurra `rect` até ele deixar de tocar nenhum obstáculo.

        Empurrão MONÓTONO por direção (sem oscilação):
            1. para baixo a partir da âncora (ordem de leitura);
            2. para cima, se não couber para baixo;
            3. troca de coluna (direita, depois esquerda) + empurrão
               vertical de novo.

        O antigo empurrão alternado por obstáculo (baixo/cima) oscilava
        entre duas janelas vizinhas (A→B→A→B…) e, ao esgotar
        max_passes, aceitava a última posição — que podia ser a MESMA
        de outra janela (sobreposição total). O empurrão monótono só
        avança numa direção por tentativa, então converge em no máximo
        len(obstacles) passos por direção.
        Fallback final (nenhuma posição coube): mantém a âncora clampada
        (melhor sobrepor um pouco do que perder o cartão).
        """
        x, y, w, h = rect
        top = geo.y() + 8
        bottom = geo.y() + geo.height() - 8
        left = geo.x() + 8
        right = geo.x() + geo.width() - 8

        def fits(vy: int) -> bool:
            return top <= vy and vy + h <= bottom

        def vertical(vy: int, direction: int, vx: int) -> int | None:
            """Empurrão vertical monótono (+1 = desce, -1 = sobe).

            Devolve a y final ou None se não couber no monitor.
            """
            for _ in range(max_passes):  # monótono: converge rápido
                moved = False
                for (ox, oy, ow, oh) in obstacles:
                    if not cls._rects_overlap((vx, vy, w, h), (ox, oy, ow, oh), gap):
                        continue
                    if direction > 0:
                        ny = oy + oh + gap
                        if ny > vy:
                            vy, moved = ny, True
                    else:
                        ny = oy - gap - h
                        if ny < vy:
                            vy, moved = ny, True
                if not moved:
                    break
            return vy if fits(vy) else None

        # 1) Para baixo a partir da âncora (ordem de leitura).
        vy = vertical(y, +1, x)
        if vy is not None:
            return (x, vy, w, h)
        # 2) Para cima a partir da âncora.
        vy = vertical(y, -1, x)
        if vy is not None:
            return (x, vy, w, h)
        # 3) Troca de coluna (hipóteses à direita e à esquerda dos
        # obstáculos, em ordem) + empurrão vertical de novo.
        candidates = {ox + ow + gap for (ox, _, ow, _) in obstacles}
        candidates |= {ox - gap - w for (ox, _, ow, _) in obstacles}
        for nx in sorted(candidates):
            if not (left <= nx and nx + w <= right):
                continue
            vy = vertical(y, +1, nx)
            if vy is None:
                vy = vertical(y, -1, nx)
            if vy is not None:
                return (nx, vy, w, h)
        # Fallback final: âncora (onde está o texto original).
        return (x, y, w, h)

    @staticmethod
    def _scope_center(
        scope: tuple[int, int, int, int] | None,
    ) -> tuple[int, int] | None:
        if scope is None:
            return None
        left, top, width, height = scope
        return (left + width // 2, top + height // 2)

    @staticmethod
    def _in_geometry(geo: QRect, x: int, y: int) -> bool:
        return (
            geo.x() <= x < geo.x() + geo.width()
            and geo.y() <= y < geo.y() + geo.height()
        )
