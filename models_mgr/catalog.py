"""
Catalogue de modèles GGUF recommandés et moteur de compatibilité matérielle.
Associe automatiquement les modèles optimaux aux capacités CPU, RAM et GPU de l'étudiant.
"""

from typing import Any, Optional

# Catalogue de référence optimisé pour les étudiants
RECOMMENDED_CATALOG = [
    {
        "id": "ministral-3b",
        "name": "Ministral 3B Instruct (Défaut)",
        "repo": "mistralai/Ministral-3-3B-Instruct-2512-GGUF",
        "filename": "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf",
        "size_gib": 2.1,
        "required_ram_gib": 6.0,
        "description": "Modèle officiel par défaut. Ultra-rapide, excellent en français, parfait pour PC portables et NPU.",
        "category": "Ultra-Léger",
        "quant": "Q4_K_M",
    },
    {
        "id": "qwen2.5-1.5b",
        "name": "Qwen 2.5 1.5B Instruct",
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "size_gib": 1.1,
        "required_ram_gib": 4.0,
        "description": "Poids plume absolu. Fonctionne même sur les configurations les plus modestes sans carte graphique.",
        "category": "Poids Plume",
        "quant": "Q4_K_M",
    },
    {
        "id": "qwen2.5-7b",
        "name": "Qwen 2.5 7B Instruct",
        "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "size_gib": 4.7,
        "required_ram_gib": 10.0,
        "description": "Très puissant en raisonnement, logique et code. Idéal avec une carte graphique dédiée (CUDA/Vulkan).",
        "category": "Équilibré",
        "quant": "Q4_K_M",
    },
    {
        "id": "llama-3.2-3b",
        "name": "Llama 3.2 3B Instruct",
        "repo": "bartowski/Llama-3.2-3B-Instruct-GGUF",
        "filename": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "size_gib": 2.0,
        "required_ram_gib": 6.0,
        "description": "Modèle léger de Meta. Très réactif pour les résumés et les questions du quotidien.",
        "category": "Polyvalent",
        "quant": "Q4_K_M",
    },
    {
        "id": "deepseek-r1-distill-qwen-7b",
        "name": "DeepSeek R1 Distill Qwen 7B",
        "repo": "bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        "filename": "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
        "size_gib": 4.8,
        "required_ram_gib": 12.0,
        "description": "Spécialiste de la réflexion pas-à-pas, des mathématiques et des algorithmes complexes.",
        "category": "Raisonnement Avancé",
        "quant": "Q4_K_M",
    },
]


def assess_compatibility(
    model: dict[str, Any],
    hardware_info: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Évalue la compatibilité d'un modèle avec le matériel détecté sur la machine."""
    hw = hardware_info or {}
    gpus = hw.get("gpus", [])
    backend = hw.get("recommended_backend", "cpu")
    cpu_cores = hw.get("cpu", {}).get("count", 4)

    req_ram = model.get("required_ram_gib", 6.0)
    has_gpu = bool(gpus and backend in ("cuda", "vulkan", "metal"))

    if has_gpu:
        badge = "Compatible GPU"
        color = "success"
        recommendation = "Excellentes performances attendues avec accélération matérielle."
    elif req_ram <= 6.0:
        badge = "Compatible CPU"
        color = "success"
        recommendation = "Fonctionnera de manière fluide sur votre processeur."
    else:
        badge = "Charge élevée"
        color = "warning"
        recommendation = f"Ce modèle nécessite ~{req_ram} Go de RAM libre pour éviter tout ralentissement."

    return {
        "badge": badge,
        "color": color,
        "recommendation": recommendation,
        "has_gpu_acceleration": has_gpu,
    }


def get_catalog_with_status(
    hardware_info: Optional[dict[str, Any]] = None,
    installed_files: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Retourne le catalogue enrichi des statuts d'installation et de compatibilité."""
    installed_set = set(installed_files or [])
    enriched = []
    for item in RECOMMENDED_CATALOG:
        entry = dict(item)
        entry["is_installed"] = entry["filename"] in installed_set
        entry["compatibility"] = assess_compatibility(entry, hardware_info)
        enriched.append(entry)
    return enriched
