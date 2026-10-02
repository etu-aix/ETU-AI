# ETU-AI

Assistant local pour les étudiants du BUT Informatique. L'application tourne sur `127.0.0.1:8900` et utilise MAIA-Beacon sur `127.0.0.1:11343` pour l'inférence.

## Démarrer en développement (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-app.txt
.\.venv\Scripts\python.exe run_launcher.py
```

Au premier lancement, l'assistant de configuration vérifie le matériel, propose des modèles GGUF et prépare Beacon. Les binaires et dépendances de Beacon sont gérés séparément dans `ai-server` ; voir [sa documentation](ai-server/README.md). Le lanceur ouvre l'interface dans le navigateur. Une distribution Windows sans Python préinstallé reste à construire.

## Ce qui fonctionne

- Chat local en streaming, indication de réflexion/rédaction et historique SQLite. Les lectures du moteur ne bloquent pas l'API pendant la génération.
- Profils d'assistant enregistrables : nom, emoji ou petite image locale, instructions personnelles. Une conversation garde son profil après redémarrage. L'image est réduite à 128 × 128 et enregistrée localement en WebP.
- Les réponses HTML/CSS sont écrites par le modèle et affichées progressivement dans un cadre isolé ; leur source reste consultable. Leur JavaScript local démarre une seule fois après la génération, dans une iframe sans accès à l'application ni au réseau. Les boutons `data-copy` et `data-action` permettent les interactions avec l'application et les éléments `data-update-marker` peuvent être modifiés par `<template for="…">`. Le Markdown reste lisible.
- Catalogue Hugging Face public sans identifiants, vérification des noms de fichiers avant téléchargement, reprise, annulation et suivi de la progression. Une empreinte SHA256 publiée par Hugging Face est comparée lorsqu'elle est disponible.
- Affichage distinct du matériel conseillé, du modèle chargé et de l'état de veille de Beacon. La présence d'un NPU ne prouve pas son utilisation.

Etu2 n'est pas encore connecté. L'ancien appairage et les outils de démonstration ne sont pas exposés au chat. L'intégration prévue découvrira en GET un contrat d'API publié par Etu2, d'abord testé avec un serveur local. Aucun résultat étudiant simulé ne doit être présenté comme réel.

## Vérifier

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check ui/app.js
```

Les tests API utilisent une base temporaire pour préserver les conversations locales.

Les étapes et critères restants sont dans [ROADMAP.md](ROADMAP.md).

Le prototype `ui-beta` de Phil Holden a inspiré l'interface et le mécanisme de mise à jour. Voir [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) pour l'attribution et la licence MIT.
