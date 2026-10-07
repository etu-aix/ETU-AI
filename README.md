# ETU-AI

Assistant local pour les étudiants du BUT Informatique. L'interface et le bridge restent sur `127.0.0.1:8900` et `127.0.0.1:11346`. MAIA-Beacon écoute par défaut sur `127.0.0.1:11343`.

## Démarrer en développement (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-app.txt
.\.venv\Scripts\python.exe run_launcher.py
```

Au premier lancement, l'assistant de configuration vérifie le matériel, propose des modèles GGUF et prépare Beacon. Les binaires et dépendances de Beacon sont gérés séparément dans `ai-server` ; voir [sa documentation](ai-server/README.md). Le lanceur ouvre l'interface dans le navigateur. Une distribution Windows sans Python préinstallé reste à construire.

Le lanceur propose trois modes. **Normal** garde les services en boucle locale et refuse les origines ETU 2 sur localhost. **Local** autorise `http://localhost`, `http://127.0.0.1` et `http://[::1]` pour le développement, sans exposer de service au réseau. **Serveur** publie MAIA-Beacon sur le réseau local au port `11343`, tandis que l'interface et le bridge restent locaux. L'API Beacon n'a pas d'authentification : tous les appareils pouvant atteindre ce port peuvent appeler le modèle. Réservez ce mode à un réseau de confiance. Les modes peuvent aussi être choisis directement avec `run_launcher.py --mode normal|local|server`.

## Ce qui fonctionne

- Markdown et coloration syntaxique pendant le streaming et dans l'historique : titres, listes, tableaux, citations, liens et blocs de code. Le code de l'interface générée est également consultable et coloré pendant la génération. Marked et highlight.js sont inclus localement ; le rendu filtre les éléments HTML et liens dangereux.
- Les groupes de mise en page générés (grilles et flex) s'affichent sur le fond du chat, sans cadre décoratif extérieur. Les éléments conservent leurs couleurs ; `data-surface="panel"` préserve les cartes et panneaux expressément demandés. Le code original reste consultable.
- Réponse textuelle par défaut ; les interfaces sont réservées aux demandes explicites. Le texte autour d'un bloc HTML reste visible. Les mises à jour d'interface s'appliquent une seule fois à la fin de la réponse. Changer de discussion annule le flux actif ; une deuxième génération simultanée dans la même discussion est refusée.
- Chat local en streaming, indication de réflexion/rédaction et historique SQLite. Les lectures du moteur ne bloquent pas l'API pendant la génération.
- Bouton ■ pour arrêter la réflexion ou la rédaction : fermeture de la connexion au moteur, remise à disposition du formulaire et conservation de la réponse partielle dans l'historique. Le changement de discussion interrompt également la génération en cours.
- Gestion Vision / mmproj sur les modèles locaux installés : recherche des projecteurs dans le dépôt exact du modèle, correspondance de variante et de taille, téléchargement avec vérification SHA256, import local GGUF (2 Go maximum), association persistante et rechargement du moteur. Les projecteurs sont séparés des modèles de chat et stockés sous des noms distincts pour éviter les collisions entre dépôts. Les noms génériques ne sont proposés automatiquement que si le dépôt contient une seule variante de modèle. Un modèle textuel ne devient pas vision avec un mmproj ; les imports manuels doivent provenir de la variante exacte du modèle. Pour Ministral 3B Instruct 2512, le dépôt officiel publie `Ministral-3-3B-Instruct-2512-BF16-mmproj.gguf`.
- Pièces jointes via le bouton ＋, glisser-déposer ou collage d'images : PDF avec texte, DOCX, XLSX, TXT/Markdown/CSV/JSON et fichiers de code, ainsi que PNG/JPEG/WebP/GIF. Le contenu des documents est extrait localement ; les images sont adaptées aux API compatibles OpenAI, Anthropic et Gemini et nécessitent un modèle vision. Les pièces jointes restent disponibles dans l'historique récent et sont supprimées avec la conversation. Limites : 6 fichiers, 10 Mo chacun, 20 Mo au total, 100 pages par PDF et 30 000 caractères par document (60 000 caractères de documents dans le contexte). Les PDF scannés sans texte demandent des pages jointes en images ; l'OCR automatique, l'audio, la vidéo et les anciens formats DOC/XLS ne sont pas pris en charge. Si une API distante est active, le contenu joint lui est transmis avec le message.
- Profils d'assistant enregistrables : nom, emoji ou petite image locale, instructions personnelles. Une conversation garde son profil après redémarrage. L'image est réduite à 128 × 128 et enregistrée localement en WebP. Sous Windows, les clés API sont chiffrées avec DPAPI. Si l'ancien coffre est inaccessible depuis le compte Windows actuel, ETU-AI en conserve une copie chiffrée dans `storage/` et crée un nouveau coffre pour les clés saisies. Si DPAPI échoue également à l'écriture, ETU-AI essaie le Gestionnaire d'identifiants Windows.
- Les réponses HTML/CSS et les formulaires sont affichés progressivement dans le chat, avec des styles isolés par Shadow DOM et une source consultable. Seuls les programmes JavaScript utilisent une iframe transparente, sans accès à l'application ni au réseau, et démarrent une fois après la génération. Les formulaires transmettent leurs champs au modèle, y compris les cases décochées, les sélections multiples et le bouton choisi. Les boutons `data-copy` et `data-action` permettent les autres interactions.
- Les zones `data-update-marker` et les marqueurs `<?start name="…">…<?end>` peuvent être modifiés par `<template for="…">` dans une réponse suivante, aussi après réouverture de la conversation. Les marqueurs réservés `chat-canvas` et `chat-style` permettent au modèle de réutiliser une vue et de changer l'apparence de la conversation. La vue occupe la zone de conversation ; un bouton dans l'en-tête permet de revenir à la discussion et d'afficher à nouveau la vue. Les styles sont limités aux messages : la sidebar, l'en-tête et les réglages restent sous le contrôle de l'application. Le Markdown reste lisible.
- Catalogue Hugging Face public sans identifiants, vérification des noms de fichiers avant téléchargement, reprise, annulation et suivi de la progression. Une empreinte SHA256 publiée par Hugging Face est comparée lorsqu'elle est disponible.
- Affichage distinct du matériel conseillé, du modèle chargé et de l'état de veille de Beacon. La présence d'un NPU ne prouve pas son utilisation.

Le bridge ETU 2 utilise une identité Ed25519 protégée par DPAPI, un consentement explicite, des challenges signés, des sessions temporaires liées à l'origine et une révocation. Le relais expose les outils de lecture du groupe et de l'emploi du temps via l'onglet ETU 2 connecté ; il ne permet aucune modification et n'expose aucun outil Workspace. Le modèle local peut les utiliser directement. Pour un fournisseur distant compatible OpenAI comme Mistral, l'envoi des résultats est désactivé par défaut et nécessite l'autorisation explicite dans **Configuration → Connexion ETU 2**. Le chat contextualisé du bridge ne conserve pas les données ETU dans l'historique. Le protocole est décrit dans [integration/README.md](integration/README.md). L'ancien `/api/handshake` reste désactivé.

## Vérifier

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check ui/app.js
node --check ui/generative-ui.js
node --check ui/etu-bridge.js
```

Pour vérifier le rendu natif, les marqueurs, les styles et les formulaires dans un navigateur : lancer `python -m http.server 8902 --bind 127.0.0.1`, ouvrir `http://localhost:8902/tests/generative_ui.html`, puis soumettre les deux formulaires de test. La page affiche les résultats `PASS`/`FAIL` ; le compteur du programme doit rester jouable après la mise à jour de sa zone de texte.

Les tests API utilisent une base temporaire pour préserver les conversations locales.
Les commandes d'arrêt et le panneau mmproj peuvent être vérifiés dans `http://localhost:8902/tests/chat_controls.html` avec le serveur statique ci-dessus ; les requêtes y sont simulées et les résultats s'affichent en PASS/FAIL.

Les étapes et critères restants sont dans [ROADMAP.md](ROADMAP.md).

Le prototype `ui-beta` de Phil Holden a inspiré l'interface et le mécanisme de mise à jour. Voir [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) pour l'attribution et la licence MIT.
