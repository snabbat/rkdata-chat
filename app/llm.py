"""Clients LLM : Ollama (API native) ou tout serveur compatible OpenAI (vLLM, LM Studio...).

Dans les deux cas le modèle tourne sur l'infrastructure du client : aucune
donnée ne sort du réseau interne.
"""

from __future__ import annotations

import requests

from app.config import Settings


class LLMError(Exception):
    """Le serveur de modèle est injoignable ou a renvoyé une erreur."""


class OllamaClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.llm_base_url
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_s
        self.num_ctx = settings.llm_num_ctx
        self.keep_alive = settings.llm_keep_alive

    def warmup(self) -> None:
        """Charge le modèle en mémoire (appel sans message), avec la même fenêtre de
        contexte que les vraies requêtes : la première question n'attend pas le chargement."""
        payload = {
            "model": self.model,
            "messages": [],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx},
        }
        try:
            requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        except requests.RequestException:
            pass  # serveur pas encore prêt : le modèle se chargera à la première question

    def chat(self, messages: list[dict], temperature: float = 0.0) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,  # garde le modèle en mémoire entre deux questions
            # num_ctx : la fenêtre par défaut d'Ollama tronquerait le schéma du prompt.
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        try:
            response = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
            response.raise_for_status()
            return response.json()["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise LLMError(f"Ollama ({self.base_url}) : {exc}") from exc

    def health(self) -> tuple[bool, str]:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            response.raise_for_status()
            models = [m.get("name", "") for m in response.json().get("models", [])]
        except (requests.RequestException, ValueError) as exc:
            return False, f"injoignable ({type(exc).__name__})"
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted in models:
            return True, "prêt"
        return False, f"modèle absent — lancez : ollama pull {self.model}"


class OpenAICompatClient:
    """Serveur exposant /v1/chat/completions (vLLM, LM Studio, llama.cpp, TGI...)."""

    def __init__(self, settings: Settings):
        self.base_url = settings.llm_base_url
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_s
        self.headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}

    def warmup(self) -> None:
        """Rien à faire : ces serveurs gardent le modèle chargé en permanence."""

    def chat(self, messages: list[dict], temperature: float = 0.0) -> str:
        payload = {"model": self.model, "messages": messages, "temperature": temperature, "stream": False}
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions", json=payload, headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Serveur LLM ({self.base_url}) : {exc}") from exc

    def health(self) -> tuple[bool, str]:
        try:
            response = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=5)
            response.raise_for_status()
            return True, "prêt"
        except requests.RequestException as exc:
            return False, f"injoignable ({type(exc).__name__})"


def get_llm(settings: Settings):
    return OllamaClient(settings) if settings.llm_provider == "ollama" else OpenAICompatClient(settings)
