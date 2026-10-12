# Bridge local ETU 2 → ETU-AI

Lancer `run_launcher.py`, puis ouvrir `http://127.0.0.1:8900`. Le même processus expose le bridge ETU 2 sur `http://127.0.0.1:8901`, afin que l'interface et le bridge partagent les appairages. L'interface fonctionne aussi sans ETU 2. Beacon utilise le port 11343 et reste normalement sur la boucle locale ; son exposition réseau nécessite le mode serveur du lanceur. llama reste toujours en boucle locale.

Le bouton **Configuration** propose **Réinstaller** pour reprendre l'assistant de configuration et **Connexion ETU 2** pour consulter les demandes et les associations autorisées. Une nouvelle demande ouvre automatiquement la fenêtre de connexion. Le bouton **Actualiser** permet de vérifier la connexion ; une erreur de chargement est affichée dans la fenêtre. Un serveur déjà lancé avant la mise à jour doit être redémarré pour charger les routes d'appairage.

Le bridge reçoit des documents ETU et ne possède aucun client permettant de modifier ETU 2. Un relais séparé rend disponibles les outils de lecture du groupe et de l'emploi du temps via l'onglet ETU 2 connecté. Gardez cet onglet ouvert. En inférence locale, les résultats restent sur l'ordinateur. Pour un fournisseur distant compatible OpenAI, l'utilisateur doit activer explicitement **Connexion ETU 2 → Autoriser l'envoi des résultats ETU** ; ce réglage est désactivé par défaut. Les fournisseurs distants Anthropic et Gemini ne reçoivent pas les outils. Le modèle qui appelle l'outil reçoit ensuite son résultat pour formuler la réponse. Le chat bridge appelle exclusivement Beacon en local. Messages, réponses et contexte bridge ne sont pas enregistrés dans SQLite.

## Origines et stockage

Origine de production exacte : `https://etu-aix.alwaysdata.net`. Les origines HTTP `localhost`, `127.0.0.1` et `[::1]` ne sont acceptées qu'en mode de lancement **local** (`ETU_ALLOW_LOCALHOST_ORIGINS=1`). Les modes **normal** et **server** les refusent par défaut. Aucune origine Internet, sous-domaine `.localhost`, ni origine HTTPS locale n'est ajoutée. Chaque association reste liée à l'origine exacte qui l'a demandée. Chaque requête privée exige `Origin` et `Authorization`. ETU ne peut pas accéder aux routes `/api`, aux fichiers statiques ou à l'interface locale. L'en-tête Host est également contrôlé pour limiter le DNS rebinding.

Le lanceur offre trois profils : `normal` conserve tous les services sur la boucle locale ; `local` ouvre seulement l'admission des origines ETU 2 sur localhost ; `server` lie Beacon à `0.0.0.0:11343` tout en laissant l'interface et le bridge sur loopback. L'API Beacon ne dispose pas d'authentification : en mode serveur, toute machine capable de joindre le port peut utiliser le modèle. Utiliser ce mode uniquement sur un réseau de confiance.

La clé privée Ed25519 et les associations sont enregistrées dans `storage/etu_identity.dpapi`, protégées par DPAPI pour l'utilisateur Windows courant. Sur les autres systèmes, le coffre `keyring` doit être disponible ; une erreur du coffre fait échouer l'initialisation. Aucune clé privée en clair n'est écrite. Les jetons d'association sont stockés sous forme de hash. Ce fichier est ignoré par Git. Le déplacer avec le stockage persistant lors de la création de l'installateur ; supprimer l'identité oblige à refaire tous les appairages.

Les sessions, challenges et demandes en attente restent en mémoire. Un redémarrage les invalide. Utiliser un seul processus Uvicorn, sans `--workers` ni rechargement automatique en production. La fenêtre de consentement locale utilise un cookie HttpOnly SameSite=Strict propre au lancement, jamais transmis à ETU. Le premier appairage exige toujours un clic explicite, y compris après une précédente association de la même origine.

## Protocole côté ETU 2 / Local Bridge

1. `GET /v1/status` vérifie la disponibilité de l'API (le statut `ready` n'indique pas qu'un modèle est chargé). `GET /v1/identity` retourne `instance_id`, `public_key` base64 brut et `algorithm: Ed25519`.
2. `POST /v1/pair` avec `{"permissions":["etu.profile.group","etu.schedule.read","etu.workspace.read","etu.ai.models.read","etu.ai.model.select"]}` retourne HTTP 202, `pair_id`, `pairing_token`, expiration et identité. Le jeton de 256 bits n'accorde encore aucun accès. La demande expire après cinq minutes. ETU affiche à l'utilisateur qu'il doit autoriser la demande dans l'interface locale. Les deux permissions `etu.ai.*` sont facultatives et indépendantes : demander `etu.ai.models.read` pour lire la liste et le statut ; demander `etu.ai.model.select` pour changer le modèle.
3. Interroger `GET /v1/pair/{pair_id}` avec `Authorization: Bearer <pairing_token>`. États : `pending`, `approved`, `denied` ; HTTP 404 si absent/expiré. Enregistrer la clé publique et l'identifiant de l'installation pendant cet appairage explicitement approuvé. Ne jamais remplacer automatiquement une clé épinglée lors d'une reconnexion. Le client garde son jeton privé hors des prompts et des journaux.
4. À chaque reconnexion, générer un nonce aléatoire d'au moins 32 octets, l'encoder en base64/base64url et appeler `POST /v1/challenge` avec `{"nonce":"..."}` et le jeton d'association. La réponse contient `challenge_id`, `signed_payload` base64 et `signature` base64. Vérifier la signature Ed25519 sur les **octets décodés exacts** de `signed_payload` avec la clé épinglée. Vérifier ensuite `protocol: etu-ai-session-v1`, le nonce envoyé, `instance_id`, `pair_id`, l'origine, les permissions attendues, le challenge et son expiration. Ce payload est un JSON UTF-8 trié par clés, compact, sans espace ; le client n'a pas besoin de le sérialiser à nouveau pour vérifier la signature.
5. Après cette vérification côté ETU, `POST /v1/session` avec `{"challenge_id":"..."}` et le jeton d'association crée une session de 30 minutes. Le challenge expire après 60 secondes et n'est utilisable qu'une fois. Utiliser ensuite `Authorization: Bearer <access_token>` ; l'origine doit rester exactement la même. Le serveur ne peut pas prouver qu'un client a effectivement vérifié sa signature : cette vérification appartient au client ETU.
6. `PUT /v1/context` remplace le contexte de cette session. Exemple :

```json
{
  "group": "1G1A",
  "schedule": {"range":"today","events":[{"start":"14:00","end":"16:00","course":"R5.A.12","room":"B204"}]},
  "workspace": {"file":{"name":"main.py","language":"python","content":"print('Bonjour')"},"selection":{"start":0,"end":16}}
}
```

Chaque champ exige sa permission respective ; un champ non autorisé fait refuser la requête entière. Limites : contexte 128 Kio, corps de requête 256 Kio. La source `etu2` est indiquée dans la réponse. Les champs absents sont supprimés lors du remplacement. Le contenu est traité comme document externe non fiable ; la garantie de lecture seule repose sur l'absence de routes et d'outils d'écriture, indépendamment du prompt.

7. `POST /v1/chat/completions` accepte `model`, `messages` (texte), `stream`, `temperature` et `max_tokens`. Exemple `{"model":"local","messages":[{"role":"user","content":"Quel est mon prochain cours ?"}],"stream":true}`. La réponse conserve le format OpenAI JSON ou SSE `data: ...`, terminé par `[DONE]`. Une erreur après le début du flux est un événement `error` suivi de `[DONE]`. Le contexte ne doit jamais être interprété comme une commande ETU. Les paramètres d'outils sont refusés.
8. `GET /v1/profile` (permission `etu.workspace.read`) retourne le profil actif `{id,name,avatar,model}`. Les instructions privées du profil ne sont pas exposées.
9. `GET /v1/models` (permission `etu.ai.models.read`) retourne les GGUF locaux installés et le modèle sélectionné : `{"models":[{"id":"modele.gguf","filename":"modele.gguf","name":"modele","source":"local","installed":true}],"selected":{"model":"modele","source":"local","provider":""}}`.
10. `GET /v1/models/status` (permission `etu.ai.models.read`) indique si le modèle sélectionné est chargé dans Beacon. `sleeping:false` signifie que le modèle local sélectionné est chargé et prêt ; `sleeping:true` signifie qu'il n'est pas chargé ou qu'il s'agit d'un modèle distant. `runtime_status` et `loaded_model` aident à diagnostiquer l'état.
11. `POST /v1/models/select` (permission `etu.ai.model.select`) avec `{"model":"modele.gguf"}` sélectionne et charge un GGUF installé. La réponse suit celle de `/api/models/select` ; un modèle absent est refusé.
12. `DELETE /v1/context` efface le contexte ; `DELETE /v1/session` ferme la session et l'efface. L'expiration empêche immédiatement tout accès et un nettoyage périodique retire les données expirées en mémoire. La révocation dans l'interface locale invalide toutes les sessions de l'association. Les requêtes au moteur sont annulées si le client se déconnecte ou si sa session devient invalide, y compris pendant une attente de jeton.

Le relais d'outils utilise `PUT /v1/tools/provider` pour enregistrer le catalogue autorisé, `GET /v1/tools/requests` pour attendre des appels et `POST /v1/tools/results` pour rendre les résultats. Les outils exposés sont `etu_profile_group` et `etu_schedule_read`. Ils exigent respectivement `etu.profile.group` et `etu.schedule.read`. Les appels et résultats restent en mémoire ; si l'onglet ETU 2 cesse de répondre, l'appel expire sans résultat. Aucun outil de modification ou Workspace n'est exposé.

Toutes les réponses privées utilisent `Cache-Control: no-store`. Une nouvelle permission exige un nouvel appairage explicite.

## Distribution et mises à jour

`updates.verify_release` refuse un manifest non signé, une signature incorrecte, une version inattendue et un hash SHA256 différent. La signature porte sur les champs `version`, `url`, `sha256` sérialisés en JSON UTF-8 trié et compact. La clé publique de distribution doit être épinglée dans le paquet par le distributeur, jamais chargée depuis le manifest téléchargé. La clé privée de distribution reste sur l'infrastructure de publication ; elle est distincte de l'identité locale de chaque installation. L'URL doit être HTTPS. L'installateur futur doit appeler cette vérification sur le paquet avant toute installation et empêcher son remplacement entre vérification et utilisation.

Il n'existe actuellement ni installateur autonome ni updater ETU-AI : ce vérificateur est prêt à intégrer, sans prétendre sécuriser une chaîne de publication encore absente. Les téléchargements initiaux de dépendances et modèles dans le wizard ne constituent pas une mise à jour applicative signée. La signature Authenticode d'ETU-AI.exe, de l'updater, de Beacon et du runtime nécessite le certificat et la chaîne de publication du distributeur. Nuitka peut servir au packaging sans remplacer l'authentification, DPAPI ou les signatures.

La connexion réelle et les restrictions du navigateur vers les adresses locales doivent être validées avec le projet ETU 2. Le dépôt ETU-AI fournit le contrat serveur ; le code ETU 2 et son Local Bridge sont des projets externes.


## Connexion distante

Configuration → Connexion ETU 2 → Connecter cet appareil affiche un code valable cinq minutes. Saisir le code dans ETU 2 → Paramètres → ETU-AI et autoriser les permissions. La connexion sortante WSS utilise /wss, réutilise l’identité Ed25519 du coffre local et reprend automatiquement après une coupure. La révocation se fait dans ETU 2. Installer les dépendances requirements-app.txt et redémarrer ETU-AI après la mise à jour. Les ports 8900 et 8901 restent uniquement en boucle locale.
