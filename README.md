# ETU-AI : Assistant Étudiant Autonome

Application d'IA locale, légère et autonome pour les étudiants de la plateforme **etu-aix.alwaysdata.net**.

## Architecture & Ports

* **Port 8900 (`http://127.0.0.1:8900`)** : Interface web interactive ETU-AI (Setup Wizard, chat streaming SSE, gestionnaire de modèles, appairage).
* **Port 11343 (`http://127.0.0.1:11343`)** : Superviseur d'inférence MAIA-Beacon & `llama-server` (TurboQuant GGUF).

---

## Démarrage Rapide (1-Clic)

```powershell
python run_launcher.py
# ou
python main.py
```

Le script démarre le serveur web sur le port **8900** et ouvre automatiquement votre navigateur par défaut.

---

## Parcours Utilisateur Initial (Setup Wizard)

1. **Écran d'Accueil & Présentation :** Présentation du projet et diagnostic matériel en temps réel (GPU, CPU, accélération recommandée).
2. **Choix des Modèles :** Sélection d'un ou plusieurs modèles GGUF selon votre configuration (Ministral 3B, Qwen 1.5B/7B, Llama 3.2 3B).
3. **Installation & Préparation Automatique :**
   - Téléchargement et vérification de `llama-server`.
   - Configuration de l'environnement virtuel MAIA-Beacon.
   - Téléchargement des modèles choisis avec suivi de vitesse en direct.
   - Démarrage et validation de l'inférence locale sur le port 11343.
   - Journal de logs de configuration en direct.
4. **Espace de Discussion (Chat UI) :** Accès direct à l'interface de conversation avec Generative UI et outils étudiants.
