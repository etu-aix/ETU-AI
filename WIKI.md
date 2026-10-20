# Wiki local et mémoire RAG

## Architecture retenue

ETU-AI utilise FastAPI (`api_server.py`), un flux de conversation commun à Beacon,
OpenAI compatible, Anthropic et Gemini (`chat/sse_handler.py`), SQLite pour les
conversations et des modules JavaScript locaux. Le Wiki se raccorde à ce flux avant
la construction des requêtes propres à chaque fournisseur. Il n'utilise aucun
service externe et n'ajoute aucune dépendance Python à `requirements-app.txt`.

Comparaison effectuée avant l'intégration :

| Option | Ressources / dépendances | Maintenance et décision |
| --- | --- | --- |
| Markdown + SQLite FTS5 | Bibliothèque standard, recherche BM25 sur disque, aucun modèle supplémentaire | Retenu par défaut ; peu de composants, données portables |
| SQLite + embeddings du moteur local | Vecteurs persistants, parcours borné, un seul modèle déjà résident | Facultatif ; repli textuel en cas d'indisponibilité |
| LlamaIndex Core + intégrations locales | Packages supplémentaires, configuration explicite des modèles et du stockage | Possible pour des pipelines plus complexes ; inutile pour le premier moteur |
| Serveur vectoriel / runtime Transformers séparé | Processus, index ou poids supplémentaires | Écarté pour le mode léger |

LlamaIndex est modulaire et peut fonctionner localement. Ses valeurs par défaut
peuvent cependant faire appel à des modèles distants ; il faudrait fournir
explicitement chaque modèle et stockage. L'interface `Retriever` et la méthode
`WikiService.retriever()` permettent d'en ajouter un adaptateur sans changer le
format Markdown, l'API, la file ou le chat.

Références : [SQLite FTS5 et BM25](https://www.sqlite.org/fts5.html),
[installation modulaire LlamaIndex](https://developers.llamaindex.ai/python/framework/getting_started/installation/),
[confidentialité LlamaIndex](https://developers.llamaindex.ai/python/framework/understanding/privacy/).

## Données et modules

- `storage/wiki/` : fichiers Markdown en UTF-8, dossiers et sous-dossiers libres.
- `storage/wiki.sqlite3` : index de passages, relations, vecteurs, provenance,
  révisions, paramètres, événements du chat et tâches persistantes. WAL et délai
  d'attente SQLite limitent les conflits avec les lectures.
- `wiki/store.py` : chemins confinés au Wiki, écritures atomiques, comparaison de
  l'empreinte avant modification, historique et restauration. Liens symboliques,
  jonctions et chemins sortants refusés.
- `wiki/frontmatter.py` : lecture légère des propriétés YAML courantes ; le bloc
  original reste intact dans le Markdown.
- `wiki/external.py` : ouverture facultative du dossier ou du coffre Obsidian.
- `wiki/retrieval.py` : recherche FTS5/BM25, repli SQL si FTS5 manque, fusion des
  rangs textuels et vectoriels. Aucun index vectoriel complet chargé en mémoire.
- `wiki/queue.py` : worker unique, priorités, reprise, pause et annulation.
- `wiki/service.py` : politique de mémoire, ressources, extraction et outils LLM.
- `wiki/api.py`, `ui/wiki.js` : API locale et panneau intégré aux paramètres.

Les fichiers constituent la source des connaissances. Les écritures faites par
ETU-AI mettent immédiatement à jour l'index textuel. Les modifications externes
sont recherchées au démarrage, en arrière-plan lorsque le chat est libre, et
avant les lectures du Wiki ou la recherche RAG (intervalle minimal de quelques
secondes). Le bouton de réindexation force une tâche explicite. Les dates et
tailles de fichiers évitent de relire les notes inchangées ; les empreintes
vérifient les notes modifiées. Si des passages changent, leurs vecteurs sont
invalidés puis la tâche d'embeddings est planifiée si elle est configurée.

Les liens `[[Document]]`, `[[Dossier/Document]]` et `[[Document|Alias]]` sont
indexés. Le graphe résout d'abord les chemins explicites, puis les noms de
fichiers, titres et `aliases` YAML non ambigus. Les propriétés YAML placées entre
`---` en tête de fichier sont conservées telles quelles. ETU-AI interprète les
champs simples `title`, `aliases` et `tags` (valeurs et listes) pour la recherche
et la navigation ; les structures YAML plus complexes restent éditables dans le
Markdown sans conversion ni réécriture automatique.

Le panneau affiche le chemin du dossier et propose **Ouvrir le dossier Wiki**.
**Ouvrir dans Obsidian** utilise le protocole local d'Obsidian lorsqu'il est
installé et que le dossier a déjà été ouvert comme coffre. La première fois,
ouvrir ce dossier comme coffre dans Obsidian ; ETU-AI ouvre le dossier et indique
cette étape. Obsidian n'est jamais requis pour utiliser le Wiki ou son graphe.

L'index se reconstruit depuis les Markdown ; les révisions, la provenance et les
tâches nécessitent la base SQLite. Pour une sauvegarde complète à chaud, utiliser
la sauvegarde SQLite ; à l'arrêt, copier le dossier et sa base. Ne pas supprimer
la base en pensant supprimer uniquement un cache. Aucun de ces contenus n'est
versionné dans Git.

## Réglages et comportement

Dans **Paramètres → Wiki et mémoire**, on peut désactiver tout le Wiki, le RAG
ou la mémorisation séparément. La mémorisation automatique et le partage du Wiki
aux fournisseurs distants sont désactivés initialement.

Le déclenchement du RAG est déterministe : messages suffisamment longs contenant
des termes significatifs, sans appel LLM de routage. Salutations et acquittements
courts sont ignorés. Le budget de contexte utilise le nombre d'octets UTF-8 comme
borne prudente pour les tokenizers à base d'octets ; il peut donc sous-utiliser le
budget de certains modèles. Les passages portent le chemin, la position et
l'empreinte du document et sont présentés comme des données non fiables. Un
document modifié depuis son indexation ne fournit pas un ancien passage au chat.

Un modèle d'embeddings vide active le mode textuel. Pour le mode hybride, choisir
un modèle **déjà chargé dans Beacon** qui expose `/v1/embeddings`, puis lancer
la génération des embeddings. Aucun téléchargement ni chargement de modèle n'est
demandé par le Wiki. Si le modèle chargé diffère, la tâche échoue avec un message
explicite, et la recherche textuelle continue. Il est possible de revenir au
modèle de chat après l'indexation ; si le moteur ne peut alors calculer le vecteur
de requête, cette recherche reste textuelle. Ce choix privilégie la mémoire
disponible à la présence permanente d'un second modèle.

Le nombre de documents, le budget de contexte, la limite de passages vectoriels,
la RAM libre minimale et la période d'inactivité sont configurables. Le Wiki ne
fait pas de sondage GPU coûteux et ne charge aucun modèle supplémentaire. Le
graphe Canvas utilise les `[[liens]]`, sans LLM ni simulation physique continue ;
il affiche au maximum 1 000 documents, avec zoom, déplacement, filtre, catégories
et voisinage à un ou deux niveaux. La liste expose au maximum 2 000 documents.

Pendant l'extraction, le modèle choisi reçoit une seule consigne supplémentaire :
repérer, dans la citation exacte de l'utilisateur, au plus cinq notions réutilisables.
Il n'a pas besoin de parcourir le Wiki ni d'effectuer un deuxième appel. Les noms
proposés sont comparés au texte source, puis normalisés (casse, accents et tirets)
pour retrouver une page `Notions/` existante. Le mode léger reconnaît quelques
formes explicites de formation, lieu et commerce sans modèle. Un fait enregistré
contient sa citation intacte et une copie avec des liens `[[Notions/…|nom cité]]`.
Après cette écriture, la file crée seulement les pages de notions manquantes.
Par exemple, deux faits qui mentionnent « aix en provence » et « Aix-en-Provence »
pointent vers la même page ; le graphe affiche leurs relations sans inférence LLM
au moment de l'affichage. Une proposition faite par `wiki_propose` peut aussi créer
ses cibles `[[Nom de notion]]` après validation, si ces noms sont dans la citation.

## Mémorisation et corrections

Le mode léger extrait seulement certaines déclarations explicites de
l'utilisateur : préférences, décisions, « nous utilisons… », « retiens… ».
Il conserve une citation exacte, l'identifiant du message et de la conversation.
Il ne déduit pas de faits des réponses de l'assistant.

Les filtres rejettent les incertitudes, les demandes de non-mémorisation et les
formes courantes de secrets et d'informations sensibles. Ils sont prudents mais
ne constituent pas un classificateur universel des données personnelles ; pour
des échanges confidentiels, conserver la mémorisation automatique désactivée.
Les contenus rejetés ne sont pas copiés dans la file. L'analyse référence le
message original et le revérifie avant l'écriture, y compris après une reprise.

Le sélecteur des tâches Wiki reprend les modèles locaux installés et les API
configurées dans les chats. Il peut suivre le modèle courant du chat ou en fixer
un pour l'analyse et l'extraction ; les tâches API passent après la réponse,
sans attendre l'inactivité, et transmettent le message source au fournisseur
choisi. Un modèle local n'est jamais chargé en parallèle : la tâche attend que
le modèle sélectionné soit déjà actif. Les embeddings gardent leur réglage
local dédié. Les citations sont vérifiées dans le message utilisateur et les propositions du modèle
pour un nouveau sujet sont enregistrées automatiquement si la mémoire est activée.
Un sujet existant ou un contenu différent déclenche une proposition à valider.
Les propositions explicites de `wiki_propose` demandent aussi validation : la détection
des contradictions est conservatrice, pas une preuve sémantique exhaustive.
L'interface permet de comparer et fusionner dans l'éditeur. Une approbation ne
contourne jamais un conflit d'empreinte : recharger et fusionner avant d'écrire.

Les modèles compatibles avec les outils OpenAI peuvent appeler `wiki_search`,
`wiki_read` et `wiki_propose`. Ce dernier produit une proposition, pas une écriture
immédiate. Les fournisseurs sans outils compatibles bénéficient toujours du RAG
automatique autorisé. Tous les outils revérifient l'autorisation de partage.

Chaque édition est historisée. Restaurer une révision crée une nouvelle édition,
ce qui permet aussi d'annuler une restauration ou une suppression. Une révision
de suppression contient le dernier contenu, pour permettre sa récupération.
Les documents Markdown sont limités à 256 Ko pour borner les opérations.

## File et API

Les générations ont priorité sur le RAG nécessaire, puis viennent les tâches
explicitement demandées (priorité 3), l'enrichissement (4), et la maintenance (5).
Une génération interrompt les requêtes LLM de fond et les traitements reprennent
depuis leurs checkpoints. Une petite écriture atomique déjà engagée peut finir ;
la reprise reconnaît une écriture identique. L'indexation reprend par empreintes,
les embeddings par passages déjà calculés. Les tâches en cours au redémarrage
redeviennent en attente. Une tâche en échec nécessite une relance explicite.

Les modes « inactivité ETU-AI » et « manuel » ne dépendent pas de l'inactivité de
tout l'ordinateur. Le lancement manuel et les demandes API passent devant
l'attente d'inactivité, mais respectent une pause, la priorité au chat et le seuil
de RAM. Le statut HTTP 202 signifie **planifié**, jamais **terminé**.

L'API utilise le même cookie administrateur local et la même vérification
d'origine que les réglages du bridge. Après ouverture de l'interface locale,
les mutations nécessitent son cookie `etu_local_admin` et une origine locale
autorisée ; aucune nouvelle API non authentifiée n'est publiée.

| Route | Usage |
| --- | --- |
| `GET/PUT /api/wiki/settings` | Paramètres validés |
| `GET /api/wiki/location` | Chemin local et disponibilité d'Obsidian |
| `POST /api/wiki/open-folder`, `/open-obsidian` | Ouverture locale explicite |
| `GET /api/wiki/documents?q=...` | Liste / filtre |
| `GET /api/wiki/deleted` | Documents supprimés encore restaurables |
| `GET/PUT/DELETE /api/wiki/document` | Lecture (`?path=`), écriture/suppression JSON (`path`, `content`, `expected_hash`) |
| `GET /api/wiki/search?q=...` | Recherche textuelle immédiate |
| `GET /api/wiki/graph` | Nœuds et relations |
| `GET /api/wiki/history?path=...` | Révisions, y compris d'un document supprimé |
| `POST /api/wiki/restore` | `path`, `revision`, `expected_hash` |
| `GET /api/wiki/jobs` | Tâches, progression, résultats et compteurs |
| `POST /api/wiki/jobs/index` ou `/embed` | Tâche explicite asynchrone, identifiant durable |
| `POST /api/wiki/jobs/run` | Lancement de la file |
| `POST /api/wiki/jobs/{id}/cancel` ou `/approve` | Annuler, valider ou relancer |
| `GET /api/wiki/events/{conversation}` | Sources et opérations persistées dans le chat |

Pour créer un fichier, `expected_hash` vaut `null`. Pour le modifier ou le
supprimer, envoyer l'empreinte renvoyée par sa lecture. Un conflit renvoie 409.
Les sources et propositions sont rendues comme du texte dans les événements et
la file ; l'aperçu Markdown filtre le HTML et n'exécute aucun programme.

## Vérification

```powershell
python -m unittest discover -s tests -p 'test_wiki*.py' -v
node --check ui/wiki.js
node --check ui/app.js
node tests/test_wiki_ui.cjs # Avec Playwright et Edge installés
```

Les tests utilisent des répertoires temporaires et des transports HTTP simulés :
stockage, restauration, liens, recherche textuelle/hybride, budget, indexation
incrémentale, modèle indisponible, secrets/incertitudes, déduplication, conflits,
pause, priorités, faible RAM, interruption/reprise, autorisation API et injection
dans les trois protocoles de fournisseurs. Aucun modèle réel n'est nécessaire.
Le test navigateur utilise les vrais scripts de l'application et des API simulées,
sans accéder aux conversations locales. Il vérifie édition, aperçu sans exécution
HTML, liens, recherche, suppression/restauration, navigation depuis le chat, graphe
et commandes de file.
