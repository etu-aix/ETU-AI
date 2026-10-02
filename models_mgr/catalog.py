"""
Catalogue de modèles GGUF recommandés et moteur de compatibilité matérielle.
Associe automatiquement les modèles optimaux aux capacités CPU, RAM, GPU et NPU de l'étudiant,
et détecte automatiquement les fichiers déjà installés ou ajoutés localement.
"""

import os
from pathlib import Path
from typing import Any, Optional

DEFAULT_MODELS_DIR = Path(__file__).resolve().parent.parent / "ai-server" / "models"

# Catalogue de référence optimisé pour les étudiants avec support NPU et GPU
RECOMMENDED_CATALOG = [
    {
        "id": "ministral-3b",
        "name": "Ministral 3B Instruct (Défaut)",
        "repo": "mistralai/Ministral-3-3B-Instruct-2512-GGUF",
        "filename": "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf",
        "size_gib": 2.1,
        "required_ram_gib": 6.0,
        "description": "Modèle officiel par défaut, compact et adapté au français. L'accélération dépend du backend disponible.",
        "category": "Ultra-Léger",
        "quant": "Q4_K_M",
        "npu_compatible": True,
        "npu_recommended": True,
    },
    {
        "id": "qwen2.5-1.5b",
        "name": "Qwen 2.5 1.5B Instruct",
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "size_gib": 1.1,
        "required_ram_gib": 4.0,
        "description": "Modèle très léger pour PC portables. Fonctionne sur CPU ou GPU selon le moteur disponible.",
        "category": "Poids Plume",
        "quant": "Q4_K_M",
        "npu_compatible": True,
        "npu_recommended": True,
    },
    {
        "id": "phi-3.5-mini",
        "name": "Phi-3.5 Mini 3.8B Instruct",
        "repo": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "filename": "Phi-3.5-mini-instruct-Q4_K_M.gguf",
        "size_gib": 2.4,
        "required_ram_gib": 6.0,
        "description": "Modèle compact pour le raisonnement et le code. Un backend NPU dédié est nécessaire pour utiliser un NPU.",
        "category": "Optimisé NPU",
        "quant": "Q4_K_M",
        "npu_compatible": True,
        "npu_recommended": True,
    },
    {
        "id": "llama-3.2-3b",
        "name": "Llama 3.2 3B Instruct",
        "repo": "bartowski/Llama-3.2-3B-Instruct-GGUF",
        "filename": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "size_gib": 2.0,
        "required_ram_gib": 6.0,
        "description": "Modèle léger de Meta pour les résumés et les questions ; fonctionne sur CPU ou GPU avec le moteur actuel.",
        "category": "Polyvalent",
        "quant": "Q4_K_M",
        "npu_compatible": True,
        "npu_recommended": True,
    },
    {
        "id": "qwen2.5-7b",
        "name": "Qwen 2.5 7B Instruct",
        "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "size_gib": 4.7,
        "required_ram_gib": 10.0,
        "description": "Très puissant en logique, mathématiques et code. Idéal avec carte graphique dédiée (CUDA/Vulkan).",
        "category": "Équilibré",
        "quant": "Q4_K_M",
        "npu_compatible": False,
        "npu_recommended": False,
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
        "npu_compatible": False,
        "npu_recommended": False,
    },
]


def detect_local_models(models_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """Détecte et liste tous les fichiers de modèles GGUF présents sur le disque.

    Args:
        models_dir (Path, optional): Dossier des modèles. Par défaut ai-server/models.

    Returns:
        list of dict: Liste des modèles locaux avec nom, taille, date de modification et chemin.
    """
    target = models_dir or DEFAULT_MODELS_DIR
    if not target.exists():
        return []

    local_models = []
    for f in target.glob("*.gguf"):
        if f.is_file() and f.stat().st_size > 1024 * 1024:
            size_gib = round(f.stat().st_size / (1024 ** 3), 2)
            local_models.append({
                "filename": f.name,
                "path": str(f),
                "size_gib": size_gib,
                "size_bytes": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            })
    return local_models


def assess_compatibility(
    model: dict[str, Any],
    hardware_info: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Évalue la compatibilité d'un modèle avec le matériel détecté sur la machine (CPU, RAM, GPU, NPU)."""
    hw = hardware_info or {}
    gpus = hw.get("gpus", [])
    npus = hw.get("npus", [])
    backend = hw.get("recommended_backend", "cpu")

    ram_info = hw.get("ram", {})
    total_ram = ram_info.get("total_gib", hw.get("ram_total_gib", 8.0))
    req_ram = model.get("required_ram_gib", 6.0)
    is_npu_comp = model.get("npu_compatible", False)
    has_gpu = bool(gpus and backend in ("cuda", "vulkan", "metal"))
    has_npu = bool(npus and len(npus) > 0)

    # Détermination des badges et recommandations
    if req_ram > total_ram:
        badge = "Charge trop lourde"
        color = "warning"
        recommendation = f"Nécessite ~{req_ram} Go de RAM alors que votre système dispose de {total_ram} Go."
        score = 20
    elif has_gpu:
        badge = "GPU possible"
        color = "success"
        recommendation = "Accélération graphique possible ; le backend réellement utilisé sera indiqué après chargement."
        score = 90
    elif req_ram <= 6.0 or req_ram <= (total_ram * 0.75):
        badge = "Compatible CPU"
        color = "success"
        recommendation = "Fonctionnera de manière fluide sur votre processeur."
        score = 80
    else:
        badge = "Charge élevée"
        color = "warning"
        recommendation = f"Ce modèle nécessite ~{req_ram} Go de RAM libre pour éviter tout ralentissement."
        score = 50

    return {
        "badge": badge,
        "color": color,
        "recommendation": recommendation,
        "score": score,
        "has_gpu_acceleration": has_gpu,
        "has_npu_acceleration": False,
        "npu_candidate": bool(has_npu and is_npu_comp),
        "npu_supported": False,
        "required_ram_gib": req_ram,
    }


def get_catalog_with_status(
    hardware_info: Optional[dict[str, Any]] = None,
    installed_files: Optional[list[str]] = None,
    models_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Retourne le catalogue enrichi des statuts d'installation et de compatibilité,

    avec détection automatique des modèles locaux additionnels.
    """
    local_models = detect_local_models(models_dir)
    detected_filenames = {m["filename"] for m in local_models}

    if installed_files is not None:
        installed_set = set(installed_files).union(detected_filenames)
    else:
        installed_set = detected_filenames

    local_model_map = {m["filename"]: m for m in local_models}
    enriched = []
    known_filenames = set()

    # 1. Modèles du catalogue de référence
    for item in RECOMMENDED_CATALOG:
        entry = dict(item)
        fn = entry["filename"]
        known_filenames.add(fn)
        is_installed = fn in installed_set
        entry["is_installed"] = is_installed
        if is_installed and fn in local_model_map:
            entry["local_size_gib"] = local_model_map[fn]["size_gib"]
        entry["compatibility"] = assess_compatibility(entry, hardware_info)
        entry["npu_candidate"] = entry.get("npu_compatible", False)
        entry["npu_compatible"] = False
        enriched.append(entry)

    # 2. Détection des modèles locaux tiers (fichiers GGUF hors catalogue officiel)
    for loc in local_models:
        fn = loc["filename"]
        if fn not in known_filenames:
            clean_name = fn.replace(".gguf", "").replace("-", " ").title()
            custom_entry = {
                "id": f"local-{fn}",
                "name": f"{clean_name} (Modèle Local Détecté)",
                "repo": "local",
                "filename": fn,
                "size_gib": loc["size_gib"],
                "required_ram_gib": max(4.0, round(loc["size_gib"] * 1.5, 1)),
                "description": f"Modèle GGUF détecté directement dans ai-server/models. Prêt instantanément sans téléchargement.",
                "category": "Modèle Local",
                "quant": "GGUF",
                "npu_candidate": loc["size_gib"] <= 4.0,
                "npu_compatible": False,
                "npu_recommended": False,
                "is_installed": True,
                "is_custom_local": True,
            }
            custom_entry["compatibility"] = assess_compatibility(custom_entry, hardware_info)
            enriched.append(custom_entry)

    return enriched

