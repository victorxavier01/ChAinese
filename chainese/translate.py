"""
Tradução chinês -> português via Ollama local (Qwen2.5).

Notas de performance:
    - Ollama local: sem rede, sem custo, sem fila. O 3b em 4 GB de VRAM
      traduz uma linha de diálogo em segundos.
    - Cache LRU por texto: linhas de diálogo se repetem muito, então cada
      texto novo consulta o modelo UMA única vez.
    - temperature baixo (0.1) + teto de tokens (200): tradução curta e
      determinística, sem divagação (e sem gasto de tempo à toa).
    - Degradação graciosa: com o Ollama desligado o app segue sem tradução
      (aviso único no log), sem crash.

Nota de threads:
    As traduções rodam na thread do worker; o busy lock do main impede dois
    F9 ao mesmo tempo, então não há lock interno aqui.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

import requests

from . import config

logger = logging.getLogger(__name__)

# O prompt do system fica em INGLES de proposito: um modelo de 3B segue
# meta-instrucoes em ingles com muito mais fiabilidade. Na versao em
# portugues ele engasgava e repetia o texto chinês em vez de traduzir
# (diagnosticado com teste A/B/C/D contra o Ollama local). A saida continua
# sendo portugues do Brasil porque e isso que a instrucao pede.
_SYSTEM_PROMPT = (
    "You are a Mandarin teacher for Brazilian players. "
    "Translate the user's Chinese text to natural, concise Brazilian Portuguese, "
    "like a game subtitle. Reply with ONLY the translation. "
    "No quotes, no explanations, no Chinese."
)


class OllamaOffline(RuntimeError):
    """O Ollama não respondeu (desligado, porta errada, host fora do ar)."""


class Translator:
    """Cliente do Ollama com cache LRU e health check no arranque."""

    def __init__(
        self,
        base_url: str = config.OLLAMA_BASE_URL,
        model: str = config.OLLAMA_MODEL,
        timeout_s: float = config.OLLAMA_TIMEOUT_S,
        max_cache: int = config.TRANSLATE_CACHE_MAX,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_s
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_cache = max_cache
        # Sessao com trust_env=False: nesta maquina o proxy do sistema
        # (registrado na registry pela MDM) intercepta ate o trafego local
        # e devolve 503 para 127.0.0.1. Ignorar o ambiente deixa a conexao
        # com o Ollama ir direto.
        self._session = requests.Session()
        self._session.trust_env = False
        self.available = self._check_health()

    def _check_health(self) -> bool:
        """GET /api/version: confirma o Ollama no ar (timeout curto, 2 s)."""
        try:
            r = self._session.get(f"{self._base}/api/version", timeout=2.0)
            r.raise_for_status()
            logger.info(
                "Tradução: Ollama OK em %s (modelo %s)", self._base, self._model
            )
            return True
        except Exception:
            logger.warning(
                "Tradução: Ollama indisponível em %s (o app segue sem tradução; "
                "inicie o Ollama e rode o app de novo)",
                self._base,
            )
            return False

    def translate(self, text: str) -> str:
        """Traduz `text` (com cache). Lança OllamaOffline se o servidor cair."""
        cached = self._cache_get(text)
        if cached is not None:
            logger.debug("Tradução em cache: %r", text)
            return cached

        try:
            r = self._session.post(
                f"{self._base}/api/chat",
                json={
                    "model": self._model,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 200},
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise OllamaOffline(f"Ollama não respondeu: {exc}") from exc

        data = r.json()
        translation = str((data.get("message") or {}).get("content", "")).strip()
        if not translation:
            raise OllamaOffline(f"Resposta vazia do Ollama: {str(data)[:200]}")

        self._cache_set(text, translation)
        return translation

    # ------------------------------------------------------------------
    # Cache LRU (sem lock: ver nota de threads no topo do módulo)
    # ------------------------------------------------------------------
    def _cache_get(self, text: str) -> str | None:
        if text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        return None

    def _cache_set(self, text: str, translation: str) -> None:
        self._cache[text] = translation
        self._cache.move_to_end(text)
        while len(self._cache) > self._max_cache:
            self._cache.popitem(last=False)
