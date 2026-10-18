"""
Client d'API Hugging Face sans credential (Public REST API).
Permet de rechercher, explorer et choisir automatiquement toutes les IA (modèles GGUF)
possibles et compatibles avec la configuration matérielle du PC de l'utilisateur (CPU, RAM, GPU, NPU).
"""

import json
import os
from pathlib import Path
import re
import time
from typing import Any, Optional
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

DEFAULT_USER_AGENT = "ETU-AI-HuggingFace-Client/1.0"
HF_API_BASE = "https://huggingface.co/api"
TRUSTED_GGUF_PUBLISHERS = ("mistralai", "Qwen", "Alibaba-NLP", "deepseek-ai", "meta-llama")
PARAMETER_RANGES = {
    "1to2": (1, 2), "2to4": (2, 4), "4to7": (4, 7),
    "7to14": (7, 14), "14to35": (14, 35), "35plus": (35, None),
}

_HF_TREE_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hf-tree")
_HF_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hf-search")


# Catalogue de secours hors-ligne / rapide enrichi avec métadonnées complètes
FALLBACK_GGUF_MODELS = [
    {
        "id": "mistralai/Ministral-3-3B-Instruct-2512-GGUF",
        "name": "Ministral 3B Instruct 2512",
        "author": "mistralai",
        "repo": "mistralai/Ministral-3-3B-Instruct-2512-GGUF",
        "description": "Modèle officiel Mistral AI 3B compact, optimisé pour le français, le raisonnement rapide et l'edge/NPU.",
        "downloads": 85000,
        "likes": 420,
        "tags": ["gguf", "text-generation", "french", "mistral", "npu-friendly"],
        "params": "3B",
        "default_file": {
            "filename": "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf",
            "size_gib": 2.1,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf", "size_gib": 2.1, "quant": "Q4_K_M"},
            {"filename": "Ministral-3-3B-Instruct-2512-Q5_K_M.gguf", "size_gib": 2.5, "quant": "Q5_K_M"},
            {"filename": "Ministral-3-3B-Instruct-2512-Q8_0.gguf", "size_gib": 3.6, "quant": "Q8_0"},
        ],
    },
    {
        "id": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "name": "Qwen 2.5 1.5B Instruct",
        "author": "Qwen",
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "description": "Poids plume ultra-rapide. Fonctionne instantanément même sur les configurations PC portables les plus légères et NPU.",
        "downloads": 120000,
        "likes": 580,
        "tags": ["gguf", "text-generation", "lightweight", "npu-friendly", "code"],
        "params": "1.5B",
        "default_file": {
            "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
            "size_gib": 1.1,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "size_gib": 1.1, "quant": "Q4_K_M"},
            {"filename": "qwen2.5-1.5b-instruct-q5_k_m.gguf", "size_gib": 1.3, "quant": "Q5_K_M"},
            {"filename": "qwen2.5-1.5b-instruct-q8_0.gguf", "size_gib": 1.8, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "name": "Phi-3.5 Mini 3.8B Instruct",
        "author": "bartowski / Microsoft",
        "repo": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "description": "Architecture Microsoft de pointe pour le raisonnement mathématique et la logique sur PC portable et NPU.",
        "downloads": 95000,
        "likes": 390,
        "tags": ["gguf", "text-generation", "reasoning", "microsoft", "npu-friendly"],
        "params": "3.8B",
        "default_file": {
            "filename": "Phi-3.5-mini-instruct-Q4_K_M.gguf",
            "size_gib": 2.4,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "Phi-3.5-mini-instruct-Q4_K_M.gguf", "size_gib": 2.4, "quant": "Q4_K_M"},
            {"filename": "Phi-3.5-mini-instruct-Q5_K_M.gguf", "size_gib": 2.8, "quant": "Q5_K_M"},
            {"filename": "Phi-3.5-mini-instruct-Q8_0.gguf", "size_gib": 4.1, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/Llama-3.2-3B-Instruct-GGUF",
        "name": "Llama 3.2 3B Instruct",
        "author": "bartowski / Meta",
        "repo": "bartowski/Llama-3.2-3B-Instruct-GGUF",
        "description": "Modèle 3B polyvalent de Meta. Haute qualité pour les résumés, rédactions de cours et synthèses.",
        "downloads": 140000,
        "likes": 710,
        "tags": ["gguf", "text-generation", "llama", "meta", "npu-friendly"],
        "params": "3B",
        "default_file": {
            "filename": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
            "size_gib": 2.0,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "Llama-3.2-3B-Instruct-Q4_K_M.gguf", "size_gib": 2.0, "quant": "Q4_K_M"},
            {"filename": "Llama-3.2-3B-Instruct-Q5_K_M.gguf", "size_gib": 2.4, "quant": "Q5_K_M"},
            {"filename": "Llama-3.2-3B-Instruct-Q8_0.gguf", "size_gib": 3.4, "quant": "Q8_0"},
        ],
    },
    {
        "id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "name": "Qwen 2.5 7B Instruct",
        "author": "Qwen",
        "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "description": "Excellente puissance de calcul, code Python/C++, mathématiques et compréhension globale.",
        "downloads": 230000,
        "likes": 1200,
        "tags": ["gguf", "text-generation", "coding", "reasoning"],
        "params": "7B",
        "default_file": {
            "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
            "size_gib": 4.7,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "qwen2.5-7b-instruct-q4_k_m.gguf", "size_gib": 4.7, "quant": "Q4_K_M"},
            {"filename": "qwen2.5-7b-instruct-q5_k_m.gguf", "size_gib": 5.4, "quant": "Q5_K_M"},
            {"filename": "qwen2.5-7b-instruct-q8_0.gguf", "size_gib": 8.1, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        "name": "DeepSeek R1 Distill Qwen 7B",
        "author": "bartowski / DeepSeek",
        "repo": "bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        "description": "Modèle de raisonnement avec chaîne de pensée pas-à-pas (Chain-of-Thought) pour démonstrations et sciences.",
        "downloads": 180000,
        "likes": 1600,
        "tags": ["gguf", "text-generation", "deepseek", "reasoning", "chain-of-thought"],
        "params": "7B",
        "default_file": {
            "filename": "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
            "size_gib": 4.8,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf", "size_gib": 4.8, "quant": "Q4_K_M"},
            {"filename": "DeepSeek-R1-Distill-Qwen-7B-Q5_K_M.gguf", "size_gib": 5.5, "quant": "Q5_K_M"},
            {"filename": "DeepSeek-R1-Distill-Qwen-7B-Q8_0.gguf", "size_gib": 8.2, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/gemma-2-2b-it-GGUF",
        "name": "Gemma 2 2B Instruct",
        "author": "bartowski / Google",
        "repo": "bartowski/gemma-2-2b-it-GGUF",
        "description": "Modèle compact développé par Google DeepMind. Excellente compréhension générale et vitesse d'inférence.",
        "downloads": 78000,
        "likes": 450,
        "tags": ["gguf", "google", "gemma", "lightweight", "npu-friendly"],
        "category": "⚡ NPU & Ultra-Léger",
        "params": "2B",
        "default_file": {
            "filename": "gemma-2-2b-it-Q4_K_M.gguf",
            "size_gib": 1.7,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "gemma-2-2b-it-Q4_K_M.gguf", "size_gib": 1.7, "quant": "Q4_K_M"},
            {"filename": "gemma-2-2b-it-Q5_K_M.gguf", "size_gib": 2.0, "quant": "Q5_K_M"},
            {"filename": "gemma-2-2b-it-Q8_0.gguf", "size_gib": 2.9, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/SmolLM2-1.7B-Instruct-GGUF",
        "name": "SmolLM2 1.7B Instruct",
        "author": "bartowski / HuggingFace",
        "repo": "bartowski/SmolLM2-1.7B-Instruct-GGUF",
        "description": "Modèle ultraléger officiel de Hugging Face conçu pour les petits appareils, téléphones et NPU.",
        "downloads": 65000,
        "likes": 320,
        "tags": ["gguf", "huggingface", "ultralight", "npu-friendly"],
        "category": "⚡ NPU & Ultra-Léger",
        "params": "1.7B",
        "default_file": {
            "filename": "SmolLM2-1.7B-Instruct-Q4_K_M.gguf",
            "size_gib": 1.2,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "SmolLM2-1.7B-Instruct-Q4_K_M.gguf", "size_gib": 1.2, "quant": "Q4_K_M"},
            {"filename": "SmolLM2-1.7B-Instruct-Q8_0.gguf", "size_gib": 1.9, "quant": "Q8_0"},
        ],
    },
    {
        "id": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
        "name": "Qwen 2.5 Coder 1.5B Instruct",
        "author": "Qwen",
        "repo": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
        "description": "Modèle poids plume dédié à l'écriture de code, débogage Python/JS/C et algorithmique. Ultra-rapide sur NPU.",
        "downloads": 110000,
        "likes": 640,
        "tags": ["gguf", "coding", "python", "javascript", "npu-friendly"],
        "category": "💻 Code & Dev",
        "params": "1.5B",
        "default_file": {
            "filename": "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",
            "size_gib": 1.1,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf", "size_gib": 1.1, "quant": "Q4_K_M"},
            {"filename": "qwen2.5-coder-1.5b-instruct-q8_0.gguf", "size_gib": 1.8, "quant": "Q8_0"},
        ],
    },
    {
        "id": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "name": "Qwen 2.5 Coder 7B Instruct",
        "author": "Qwen",
        "repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "description": "L'un des meilleurs modèles mondiaux de programmation 7B. Génération de code complexe, SQL, C++, Python et tests.",
        "downloads": 290000,
        "likes": 1850,
        "tags": ["gguf", "coding", "software-engineering", "python", "sql"],
        "category": "💻 Code & Dev",
        "params": "7B",
        "default_file": {
            "filename": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
            "size_gib": 4.7,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "qwen2.5-coder-7b-instruct-q4_k_m.gguf", "size_gib": 4.7, "quant": "Q4_K_M"},
            {"filename": "qwen2.5-coder-7b-instruct-q5_k_m.gguf", "size_gib": 5.4, "quant": "Q5_K_M"},
            {"filename": "qwen2.5-coder-7b-instruct-q8_0.gguf", "size_gib": 8.1, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "name": "Meta Llama 3.1 8B Instruct",
        "author": "bartowski / Meta",
        "repo": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "description": "La référence universelle open-source de Meta avec contexte étendu 128k, synthèse de cours et polyvalence extrême.",
        "downloads": 420000,
        "likes": 2400,
        "tags": ["gguf", "llama", "meta", "instruct", "general-purpose"],
        "category": "🚀 Polyvalent 7B-8B",
        "params": "8B",
        "default_file": {
            "filename": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
            "size_gib": 4.9,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", "size_gib": 4.9, "quant": "Q4_K_M"},
            {"filename": "Meta-Llama-3.1-8B-Instruct-Q5_K_M.gguf", "size_gib": 5.7, "quant": "Q5_K_M"},
            {"filename": "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf", "size_gib": 8.5, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        "name": "Mistral 7B Instruct v0.3",
        "author": "bartowski / Mistral AI",
        "repo": "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        "description": "Modèle phare français de Mistral AI. Excellente qualité rédactionnelle, grammaire irréprochable et support d'outils.",
        "downloads": 310000,
        "likes": 1500,
        "tags": ["gguf", "mistral", "french", "instruct", "tools"],
        "category": "🇫🇷 Spécial Français",
        "params": "7B",
        "default_file": {
            "filename": "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
            "size_gib": 4.4,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf", "size_gib": 4.4, "quant": "Q4_K_M"},
            {"filename": "Mistral-7B-Instruct-v0.3-Q5_K_M.gguf", "size_gib": 5.1, "quant": "Q5_K_M"},
            {"filename": "Mistral-7B-Instruct-v0.3-Q8_0.gguf", "size_gib": 7.7, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/Llama-3.2-1B-Instruct-GGUF",
        "name": "Llama 3.2 1B Instruct",
        "author": "bartowski / Meta",
        "repo": "bartowski/Llama-3.2-1B-Instruct-GGUF",
        "description": "Modèle ultra-compact 1B de Meta. Vitesse d'exécution fulgurante sur tout ordinateur, idéal pour réponses instantanées.",
        "downloads": 98000,
        "likes": 510,
        "tags": ["gguf", "llama", "meta", "ultralight", "npu-friendly"],
        "category": "⚡ NPU & Ultra-Léger",
        "params": "1B",
        "default_file": {
            "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
            "size_gib": 0.85,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf", "size_gib": 0.85, "quant": "Q4_K_M"},
            {"filename": "Llama-3.2-1B-Instruct-Q8_0.gguf", "size_gib": 1.3, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/granite-3.0-2b-instruct-GGUF",
        "name": "IBM Granite 3.0 2B Instruct",
        "author": "bartowski / IBM",
        "repo": "bartowski/granite-3.0-2b-instruct-GGUF",
        "description": "Modèle compact développé par IBM pour les tâches académiques et d'entreprise. Très sobre en mémoire.",
        "downloads": 45000,
        "likes": 210,
        "tags": ["gguf", "ibm", "granite", "enterprise", "npu-friendly"],
        "category": "⚡ NPU & Ultra-Léger",
        "params": "2B",
        "default_file": {
            "filename": "granite-3.0-2b-instruct-Q4_K_M.gguf",
            "size_gib": 1.5,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "granite-3.0-2b-instruct-Q4_K_M.gguf", "size_gib": 1.5, "quant": "Q4_K_M"},
            {"filename": "granite-3.0-2b-instruct-Q8_0.gguf", "size_gib": 2.6, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/granite-3.0-8b-instruct-GGUF",
        "name": "IBM Granite 3.0 8B Instruct",
        "author": "bartowski / IBM",
        "repo": "bartowski/granite-3.0-8b-instruct-GGUF",
        "description": "Version 8B d'IBM Granite avec support du raisonnement logique poussé, extraction de données et synthèses de documents.",
        "downloads": 58000,
        "likes": 280,
        "tags": ["gguf", "ibm", "granite", "enterprise", "reasoning"],
        "category": "🚀 Polyvalent 7B-8B",
        "params": "8B",
        "default_file": {
            "filename": "granite-3.0-8b-instruct-Q4_K_M.gguf",
            "size_gib": 4.9,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "granite-3.0-8b-instruct-Q4_K_M.gguf", "size_gib": 4.9, "quant": "Q4_K_M"},
            {"filename": "granite-3.0-8b-instruct-Q8_0.gguf", "size_gib": 8.6, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/Hermes-3-Llama-3.1-8B-GGUF",
        "name": "Hermes 3 Llama 3.1 8B",
        "author": "bartowski / NousResearch",
        "repo": "bartowski/Hermes-3-Llama-3.1-8B-GGUF",
        "description": "Modèle agentique avancé de NousResearch. Maîtrise l'appel d'outils, la résolution de problèmes et les jeux de rôle.",
        "downloads": 135000,
        "likes": 880,
        "tags": ["gguf", "nous-research", "hermes", "agentic", "tools"],
        "category": "🧠 Raisonnement",
        "params": "8B",
        "default_file": {
            "filename": "Hermes-3-Llama-3.1-8B-Q4_K_M.gguf",
            "size_gib": 4.9,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "Hermes-3-Llama-3.1-8B-Q4_K_M.gguf", "size_gib": 4.9, "quant": "Q4_K_M"},
            {"filename": "Hermes-3-Llama-3.1-8B-Q8_0.gguf", "size_gib": 8.5, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/StarCoder2-3B-GGUF",
        "name": "StarCoder2 3B",
        "author": "bartowski / BigCode",
        "repo": "bartowski/StarCoder2-3B-GGUF",
        "description": "Spécialiste de la complétion de code, algorithmique et architecture logicielle sur 600+ langages.",
        "downloads": 54000,
        "likes": 290,
        "tags": ["gguf", "bigcode", "starcoder", "coding", "npu-friendly"],
        "category": "💻 Code & Dev",
        "params": "3B",
        "default_file": {
            "filename": "StarCoder2-3B-Q4_K_M.gguf",
            "size_gib": 2.0,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "StarCoder2-3B-Q4_K_M.gguf", "size_gib": 2.0, "quant": "Q4_K_M"},
            {"filename": "StarCoder2-3B-Q8_0.gguf", "size_gib": 3.3, "quant": "Q8_0"},
        ],
    },
    {
        "id": "bartowski/openchat_3.5-GGUF",
        "name": "OpenChat 3.5 7B",
        "author": "bartowski / OpenChat",
        "repo": "bartowski/openchat_3.5-GGUF",
        "description": "Modèle conversationnel très réactif et naturel, calibré pour les dialogues interactifs et explications claires.",
        "downloads": 89000,
        "likes": 470,
        "tags": ["gguf", "openchat", "chat", "general-purpose"],
        "category": "🚀 Polyvalent 7B-8B",
        "params": "7B",
        "default_file": {
            "filename": "openchat_3.5-Q4_K_M.gguf",
            "size_gib": 4.4,
            "quant": "Q4_K_M",
        },
        "quantizations": [
            {"filename": "openchat_3.5-Q4_K_M.gguf", "size_gib": 4.4, "quant": "Q4_K_M"},
            {"filename": "openchat_3.5-Q8_0.gguf", "size_gib": 7.7, "quant": "Q8_0"},
        ],
    },
]


class HuggingFaceClient:
    """Client public pour l'API REST Hugging Face (sans token ni credential)."""

    def __init__(self, timeout: int = 2):
        self.timeout = timeout
        self._cache: dict[str, Any] = {}
        self._cache_time: dict[str, float] = {}
        self.cache_ttl = 300  # 5 minutes de cache mémoire

    def _get_json(self, url: str) -> Optional[Any]:
        """Effectue une requête GET HTTP publique vers Hugging Face avec cache et User-Agent."""
        now = time.monotonic()
        if url in self._cache and (now - self._cache_time.get(url, 0)) < self.cache_ttl:
            return self._cache[url]

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    self._cache[url] = data
                    self._cache_time[url] = now
                    return data
        except Exception:
            pass
        return None

    def search_models(
        self,
        query: str = "",
        filter_tag: str = "gguf",
        sort: str = "downloads",
        limit: int = 40,
        author: str = "",
        single_source: bool = False,
    ) -> list[dict[str, Any]]:
        """Recherche des dépôts de modèles GGUF sur Hugging Face sans identifiant."""
        if author or single_source:
            search_queries = [query]
        elif not query:
            # En mode découverte globale, interroge plusieurs requêtes clés pour obtenir un large catalogue
            search_queries = ["instruct gguf", "qwen gguf", "llama gguf", "code gguf", "mistral gguf"]
        else:
            search_queries = [f"{query} gguf".strip() if "gguf" not in query.lower() else query]

        aggregated_raw: list[dict[str, Any]] = []
        seen_ids = set()

        for sq in search_queries:
            params = {
                "search": sq,
                "sort": sort,
                "direction": "-1",
                "limit": str(min(limit, 60 if single_source else 30)),
            }
            if filter_tag:
                params["filter"] = filter_tag
            if author:
                params["author"] = author

            query_str = urllib.parse.urlencode(params)
            url = f"{HF_API_BASE}/models?{query_str}"
            raw_models = self._get_json(url)

            if raw_models and isinstance(raw_models, list):
                for rm in raw_models:
                    rid = rm.get("id")
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        aggregated_raw.append(rm)

        if not aggregated_raw:
            # En cas d'indisponibilité réseau ou timeout, utilisation du catalogue de secours
            fallback = self._filter_fallback_catalog(query)
            if author:
                fallback = [item for item in fallback if item["repo"].split("/", 1)[0].lower() == author.lower()]
            return fallback[:limit]

        results = []
        for item in aggregated_raw:
            repo_id = item.get("id", "")
            if not repo_id or "/" not in repo_id:
                continue
            
            author, model_name = repo_id.split("/", 1)
            clean_name = self._beautify_model_name(model_name)
            params_label = self._extract_params_size(model_name)

            results.append({
                "id": repo_id,
                "name": clean_name,
                "author": author,
                "repo": repo_id,
                "downloads": item.get("downloads", 0),
                "likes": item.get("likes", 0),
                "tags": item.get("tags", []),
                "params": params_label,
                "last_modified": item.get("lastModified"),
            })

        return results

    def get_model_files(self, repo_id: str) -> list[dict[str, Any]]:
        """Récupère la liste des fichiers .gguf d'un dépôt Hugging Face avec leurs tailles exactes."""
        url = f"{HF_API_BASE}/models/{repo_id}/tree/main"
        tree = self._get_json(url)

        if not tree or not isinstance(tree, list):
            return []

        gguf_files = []
        for file_info in tree:
            path = file_info.get("path", "")
            if re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", path, re.IGNORECASE) and file_info.get("type") == "file":
                size_bytes = file_info.get("size") or (file_info.get("lfs") or {}).get("size") or 0
                if size_bytes <= 0:
                    continue
                size_gib = round(size_bytes / (1024 ** 3), 2) if size_bytes else 0.0
                quant = self._extract_quant(path)
                gguf_files.append({
                    "is_projector": any(word in path.lower() for word in ("mmproj", "projector")),
                    "filename": path,
                    "size_bytes": size_bytes,
                    "size_gib": size_gib,
                    "quant": quant,
                    "sha256": (file_info.get("lfs") or {}).get("oid"),
                })

        # Tri par pertinence de quantification : Q4_K_M en premier, puis Q5_K_M, etc.
        def _sort_key(f):
            q = f["quant"].upper()
            if "Q4_K_M" in q:
                return 0
            if "Q5_K_M" in q:
                return 1
            if "Q4" in q:
                return 2
            if "Q5" in q:
                return 3
            if "Q8" in q:
                return 4
            return 5

        gguf_files.sort(key=_sort_key)
        return gguf_files

    def evaluate_model_compatibility(
        self,
        size_gib: float,
        model_name: str,
        hardware_info: Optional[dict[str, Any]] = None,
        quant: str = "Q4_K_M",
    ) -> dict[str, Any]:
        """Évalue précisément si le modèle GGUF est exécutable sur la machine (CPU, RAM, GPU, NPU)."""
        hw = hardware_info or {}
        gpus = hw.get("gpus", [])
        npus = hw.get("npus", [])
        backend = hw.get("recommended_backend", "cpu")
        
        # Détection de la RAM système
        ram_info = hw.get("ram", {})
        total_ram_gib = ram_info.get("total_gib", hw.get("ram_total_gib", 8.0))
        avail_ram_gib = ram_info.get("available_gib", hw.get("ram_available_gib", total_ram_gib * 0.7))

        # Estimation de la RAM totale requise (Modèle + Contexte KV Cache + Overhead)
        required_ram_gib = round(size_gib * 1.25 + 1.2, 1)

        has_npu = bool(npus and len(npus) > 0)
        has_gpu = bool(gpus and backend in ("cuda", "vulkan", "metal"))

        # La taille rend un modèle léger, mais ne prouve pas un backend NPU.
        is_npu_optimal = has_npu and (size_gib <= 3.2)
        is_npu_compatible = has_npu and (size_gib <= 4.2)

        # Calcul du score de compatibilité (0 à 100)
        score = 0
        is_compatible = True
        status_tier = "optimal"  # optimal, compatible, tight, incompatible

        if required_ram_gib > total_ram_gib:
            is_compatible = False
            status_tier = "incompatible"
            badge = "❌ RAM insuffisante"
            color = "danger"
            recommendation = f"Nécessite ~{required_ram_gib} Go de RAM mais votre PC dispose de {total_ram_gib} Go."
            score = 10
        elif required_ram_gib > (total_ram_gib * 0.85):
            is_compatible = True
            status_tier = "tight"
            badge = "⚠️ Limite mémoire"
            color = "warning"
            recommendation = f"Fonctionnera mais consommera presque toute votre RAM ({required_ram_gib} Go / {total_ram_gib} Go)."
            score = 45
        elif is_npu_optimal:
            status_tier = "optimal"
            badge = "🚀 Recommandé NPU"
            color = "success"
            recommendation = "Modèle compact adapté à un NPU détecté ; l'accélération réelle dépend du backend Beacon installé."
            score = 95
        elif is_npu_compatible:
            status_tier = "compatible"
            badge = "✅ Compatible NPU"
            color = "info"
            recommendation = "Peut convenir au NPU détecté ; l'accélération réelle dépend du backend Beacon installé."
            score = 80
        elif has_gpu and size_gib <= 8.0:
            is_compatible = True
            status_tier = "optimal"
            badge = "🚀 Recommandé GPU"
            color = "success"
            recommendation = f"Excellente vitesse avec accélération matérielle graphique ({backend.upper()})."
            score = 95
        elif required_ram_gib <= 6.0:
            is_compatible = True
            status_tier = "optimal"
            badge = "✅ Fluide sur CPU"
            color = "success"
            recommendation = "Modèle léger, parfaitement fluide sur votre processeur."
            score = 85
        else:
            is_compatible = True
            status_tier = "compatible"
            badge = "✅ Compatible PC"
            color = "info"
            recommendation = f"Pris en charge sur votre système ({required_ram_gib} Go de RAM requis)."
            score = 75

        return {
            "is_compatible": is_compatible,
            "status_tier": status_tier,
            "badge": badge,
            "color": color,
            "recommendation": recommendation,
            "score": score,
            "required_ram_gib": required_ram_gib,
            "has_npu_acceleration": False,
            "npu_candidate": is_npu_optimal or is_npu_compatible,
            "has_gpu_acceleration": has_gpu,
            "npu_compatible": False,
            "system_total_ram_gib": total_ram_gib,
        }

    def get_all_compatible_models(
        self,
        hardware_info: Optional[dict[str, Any]] = None,
        query: str = "",
        limit: int = 30,
        only_compatible: bool = True,
        publisher: str = "",
        parameter_range: str = "",
        trusted_only: bool = False,
        hardware_filter: str = "",
        preserve_order: bool = False,
    ) -> list[dict[str, Any]]:
        """Récupère toutes les IA possibles depuis Hugging Face en évaluant automatiquement

        la compatibilité matérielle avec le PC.
        """
        # L'explorateur pagine par lots de dix modèles. Limiter les dépôts
        # candidats à la taille de ce lot évite une rafale d'appels /tree sur
        # Hugging Face à chaque nouvelle recherche.
        # The explorer only needs enough candidates to fill the requested page.
        # Doubling this count made each search fetch up to twice as many repository
        # trees as it could display, and a slow Hugging Face response stalled UI search.
        candidate_limit = limit
        trusted_authors = {author.lower() for author in TRUSTED_GGUF_PUBLISHERS}
        if trusted_only and not publisher:
            search_future = _HF_SEARCH_EXECUTOR.submit(
                self._search_trusted_models, query, candidate_limit
            )
            completed, _ = wait((search_future,), timeout=18)
            if search_future in completed:
                raw_list = search_future.result()
            else:
                search_future.cancel()
                raw_list = self._filter_fallback_catalog(query)
        else:
            search_future = _HF_SEARCH_EXECUTOR.submit(
                self.search_models,
                query=query,
                limit=candidate_limit,
                author=publisher,
                single_source=preserve_order,
            )
            completed, _ = wait((search_future,), timeout=18)
            if search_future in completed:
                raw_list = search_future.result()
            else:
                search_future.cancel()
                raw_list = self._filter_fallback_catalog(query)
        compatible_models = []

        # Fusion avec les modèles de référence
        seen_repos = set()
        candidates = []

        # L'explorateur montre la recherche live ; les autres vues gardent les références intégrées.
        if not preserve_order:
            for fb in FALLBACK_GGUF_MODELS:
                if publisher and fb["repo"].split("/", 1)[0].lower() != publisher.lower():
                    continue
                if trusted_only and fb["repo"].split("/", 1)[0].lower() not in trusted_authors:
                    continue
                if not query or query.lower() in fb["name"].lower() or query.lower() in fb["repo"].lower():
                    candidates.append(fb)
                    seen_repos.add(fb["repo"].lower())
                    if len(candidates) >= limit:
                        break

        # 2. Modèles issus de Hugging Face Live API
        for item in raw_list[:max(0, candidate_limit - len(candidates))]:
            repo = item["repo"]
            if publisher and repo.split("/", 1)[0].lower() != publisher.lower():
                continue
            if trusted_only and repo.split("/", 1)[0].lower() not in trusted_authors:
                continue
            if repo.lower() not in seen_repos:
                candidates.append(item)
                seen_repos.add(repo.lower())

        if preserve_order:
            query_lower = query.lower().strip()
            query_key = re.sub(r"[^a-z0-9]+", "", query_lower)
            for fallback in FALLBACK_GGUF_MODELS:
                repo = fallback["repo"]
                author = repo.split("/", 1)[0]
                if repo.lower() in seen_repos:
                    continue
                if publisher and author.lower() != publisher.lower():
                    continue
                if trusted_only and author.lower() not in trusted_authors:
                    continue
                if query_lower:
                    searchable = f"{fallback['name']} {repo}".lower()
                    searchable_key = re.sub(r"[^a-z0-9]+", "", searchable)
                    if query_lower not in searchable and query_key not in searchable_key:
                        continue
                candidates.append(fallback)
                seen_repos.add(repo.lower())

        # L'API de recherche Hugging Face ne fournit pas les fichiers du dépôt.
        # Les demander séquentiellement rendait l'explorateur très lent (jusqu'à
        # plusieurs timeouts successifs). Précharge les arbres en parallèle tout
        # en conservant l'ordre initial des dépôts.
        missing_files = [candidate for candidate in candidates if not candidate.get("default_file")]
        if missing_files:
            futures = {_HF_TREE_EXECUTOR.submit(self.get_model_files, candidate["repo"]): candidate for candidate in missing_files}
            completed, pending = wait(futures, timeout=18)
            for future in completed:
                candidate = futures[future]
                candidate["_file_lookup_attempted"] = True
                try:
                    files = future.result()
                except Exception:
                    files = []
                files = [file_info for file_info in files if not file_info.get("is_projector")]
                if files:
                    candidate["default_file"] = files[0]
                    candidate["quantizations"] = files
            for future in pending:
                futures[future]["_file_lookup_attempted"] = True
                future.cancel()

        for c in candidates:
            repo = c["repo"]
            params_label = c.get("params") or self._extract_params_size(repo.split("/", 1)[-1])
            if parameter_range and not self._matches_parameter_range(params_label, parameter_range):
                continue
            
            # Détermination du fichier et de la taille
            default_file = c.get("default_file")
            quantizations = c.get("quantizations")
            
            verified_file = False
            if not default_file:
                # Ne pas refaire en série une vérification déjà tentée en parallèle.
                if c.get("_file_lookup_attempted"):
                    continue
                # Récupération des fichiers du dépôt
                files = [item for item in self.get_model_files(repo) if not item.get("is_projector")]
                if not files:
                    continue
                default_file = files[0]
                quantizations = files
                verified_file = True

            size_gib = default_file.get("size_gib", 2.5)
            comp = self.evaluate_model_compatibility(
                size_gib=size_gib,
                model_name=c.get("name", repo),
                hardware_info=hardware_info,
                quant=default_file.get("quant", "Q4_K_M"),
            )

            if only_compatible and not comp["is_compatible"]:
                continue
            if hardware_filter == "gpu" and not comp["has_gpu_acceleration"]:
                continue
            if hardware_filter == "npu" and not comp["npu_candidate"]:
                continue
            if hardware_filter == "cpu" and not comp["is_compatible"]:
                continue

            entry = {
                "id": c.get("id", repo),
                "name": c.get("name", self._beautify_model_name(repo.split("/")[-1])),
                "author": c.get("author", repo.split("/")[0] if "/" in repo else "huggingface"),
                "repo": repo,
                "description": c.get("description", f"Modèle GGUF haute performance disponible sur Hugging Face ({repo})."),
                "downloads": c.get("downloads", 0),
                "likes": c.get("likes", 0),
                "params": params_label,
                "filename": default_file.get("filename"),
                "size_gib": size_gib,
                "quant": default_file.get("quant", "Q4_K_M"),
                "quantizations": quantizations or [default_file],
                "compatibility": comp,
                "npu_compatible": comp["npu_compatible"],
                "is_installed": False,
                "is_huggingface_live": verified_file,
                "file_verified": verified_file,
            }
            compatible_models.append(entry)
            if preserve_order and len(compatible_models) >= limit:
                break

        # Tri : Score de compatibilité décroissant, puis téléchargements
        if not preserve_order:
            compatible_models.sort(
                key=lambda m: (m["compatibility"]["score"], m.get("downloads", 0)),
                reverse=True,
            )

        return compatible_models

    def _search_trusted_models(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Search trusted publishers and keep the same popularity ordering."""
        raw_list = []
        for author in TRUSTED_GGUF_PUBLISHERS:
            raw_list.extend(self.search_models(query=query, limit=limit, author=author, single_source=True))
        raw_list.sort(key=lambda item: (item.get("downloads", 0), item["repo"]), reverse=True)
        return raw_list

    def auto_select_best_models(
        self,
        hardware_info: Optional[dict[str, Any]] = None,
        top_k: int = 1,
    ) -> list[dict[str, Any]]:
        """Sélectionne automatiquement les meilleurs modèles d'IA adaptés au PC détecté."""
        models = self.get_all_compatible_models(
            hardware_info=hardware_info,
            only_compatible=True,
            limit=20,
        )
        if not models:
            # Sécurité : renvoie le premier modèle de secours
            first_fallback = dict(FALLBACK_GGUF_MODELS[0])
            first_fallback["filename"] = first_fallback["default_file"]["filename"]
            first_fallback["size_gib"] = first_fallback["default_file"]["size_gib"]
            first_fallback["is_installed"] = False
            first_fallback["file_verified"] = False
            first_fallback["compatibility"] = self.evaluate_model_compatibility(
                size_gib=first_fallback["default_file"]["size_gib"],
                model_name=first_fallback["name"],
                hardware_info=hardware_info,
            )
            return [first_fallback]

        return models[:top_k]

    def _filter_fallback_catalog(self, query: str) -> list[dict[str, Any]]:
        """Filtre le catalogue de secours selon la recherche."""
        if not query:
            return list(FALLBACK_GGUF_MODELS)
        q = query.lower()
        return [
            m for m in FALLBACK_GGUF_MODELS
            if q in m["name"].lower() or q in m["repo"].lower() or any(q in t.lower() for t in m.get("tags", []))
        ]

    def _beautify_model_name(self, raw_name: str) -> str:
        """Formate proprement le nom d'un modèle pour l'affichage."""
        name = re.sub(r"-GGUF$", "", raw_name, flags=re.IGNORECASE)
        name = re.sub(r"\.gguf$", "", name, flags=re.IGNORECASE)
        name = re.sub(r"[-_]", " ", name)
        return name.strip().title()

    def _extract_params_size(self, text: str) -> str:
        """Extrait le nombre de paramètres (ex: 1.5B, 3B, 7B, 14B, 70B)."""
        match = re.search(r"(\d+(?:\.\d+)?)\s*[bB]", text)
        if match:
            return f"{match.group(1).upper()}B"
        return "Inconnu"

    @staticmethod
    def _matches_parameter_range(params_label: str, parameter_range: str) -> bool:
        bounds = PARAMETER_RANGES.get(parameter_range)
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[bB]", str(params_label))
        if not bounds or not match:
            return False
        value = float(match.group(1))
        lower, upper = bounds
        return value >= lower and (upper is None or value < upper)

    def _extract_quant(self, filename: str) -> str:
        """Extrait le type de quantification d'un fichier GGUF (ex: Q4_K_M, Q5_K_M, Q8_0)."""
        match = re.search(r"(Q\d+_[A-Z0-9_]+|IQ\d+_[A-Z0-9_]+|FP16|BF16)", filename, re.IGNORECASE)
        if match:
            return match.group(1).upper()
        return "Q4_K_M"

    def _estimate_size_from_params(self, params_str: str) -> float:
        """Estime la taille en Go d'un fichier Q4_K_M selon les paramètres."""
        try:
            num = float(re.sub(r"[^\d.]", "", params_str))
            if num <= 2:
                return 1.2
            elif num <= 4:
                return 2.2
            elif num <= 8:
                return 4.8
            elif num <= 14:
                return 8.5
            elif num <= 32:
                return 19.0
            return round(num * 0.65, 1)
        except Exception:
            return 2.5


# Instance partagée
hf_client = HuggingFaceClient()
