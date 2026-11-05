# Consignes pour les agents

Ces règles s'appliquent à tout le dépôt. Les fichiers `AGENTS.md` placés dans un sous-dossier peuvent préciser les consignes pour ce dossier.

## Avant de modifier le code

- Lis le code et les conventions déjà en place dans la zone concernée avant de changer l'architecture ou le style.
- Préserve les changements déjà présents dans le dépôt. Ne réinitialise pas et ne remplace pas des modifications qui ne font pas partie de ta tâche.
- Fais des changements ciblés et cohérents avec l'application existante. Évite les dépendances et abstractions supplémentaires lorsqu'une solution simple suffit.
- Pour l'interface active, travaille dans `ui/`. Consulte `README.md` pour les commandes et le comportement attendu.

## SVG et icônes

- Avant d'ajouter ou de modifier une icône SVG de l'interface, consulte `ui/svg-manager.js`.
- Utilise `window.ETUSVG.icon("nom")` pour afficher une icône déjà gérée. N'écris pas de SVG d'icône directement dans un composant, une chaîne HTML ou une page.
- Si l'icône nécessaire n'existe pas, ajoute son tracé dans l'objet `paths` de `ui/svg-manager.js`, puis appelle-la par son nom avec `ETUSVG.icon`. Choisis un nom explicite et cohérent avec les noms existants.
- Respecte le format commun du gestionnaire : icône vectorielle simple dans une zone `24 × 24`, sans remplissage, trait `currentColor`, extrémités et jonctions arrondies. Une exception doit répondre à un besoin visuel concret.
- Les SVG de marque ou de plateforme ne sont pas des icônes génériques : réutilise les fichiers existants dans `ui/platform-icons/` ou `ui/provider-icons/`. N'en redessine pas la marque dans `paths`.
- Les icônes décoratives restent masquées aux technologies d'assistance. Une icône seule dans un bouton doit avoir un nom accessible fourni par le bouton.

## Principes d'interface

Conçois une interface claire, sobre et épurée. Prends les principes de Material Design de Google comme repères d'utilisabilité et d'accessibilité, et l'approche visuelle des produits OpenAI comme inspiration de clarté, de retenue et de lisibilité. Adapte ces repères à ETU-AI : ne copie ni leurs écrans, ni leur identité, ni leurs composants à l'identique.

- Donne à chaque écran une hiérarchie immédiatement lisible : une action principale, des libellés directs et des détails secondaires moins visibles.
- Recherche la simplicité maximale dans les actions : réduis les étapes, évite les choix redondants et garde les actions fréquentes à portée de main. Regroupe les réglages rares sans cacher les fonctions essentielles.
- N'ajoute une carte, une bordure, une couleur, une icône ou une animation que si elle aide à comprendre, distinguer ou utiliser l'interface. Préfère l'espace et l'alignement aux séparateurs décoratifs.
- Réutilise les variables, composants, espacements, couleurs et rayons déjà définis. Respecte les thèmes clair et sombre existants ; ne fixe pas une couleur si un jeton de thème convient.
- Utilise des intitulés courts qui nomment l'action ou l'état. Évite les textes d'aide répétitifs, le jargon et les instructions qui pourraient être remplacées par une bonne valeur par défaut.
- Les contrôles doivent avoir des états visibles : repos, survol, focus clavier, désactivé, chargement, réussite et erreur selon le besoin. Préserve un focus clavier net et un contraste lisible.
- Associe les champs à des libellés persistants ; ne fais pas dépendre leur sens d'un placeholder. Présente les erreurs près du champ concerné et indique comment les corriger.
- Préviens avant une action destructive ou difficile à annuler. Pour une action courante et réversible, évite les confirmations inutiles.
- Respecte le clavier, les lecteurs d'écran, les tailles de cible adaptées et `prefers-reduced-motion`. N'utilise pas la couleur seule pour communiquer un état.
- Assure une mise en page utilisable sur les largeurs prises en charge, sans débordement ni perte d'accès aux actions principales.
- Garde les interactions prévisibles : un même libellé et un même symbole doivent garder le même sens dans toute l'application.

## Quand tu modifies l'interface

- Vérifie les parcours autour de la zone modifiée, y compris les états vide, en cours, erreur et succès lorsqu'ils s'appliquent.
- Préserve le fonctionnement sans réseau et les ressources locales lorsque le comportement existant le prévoit.
- Mets à jour la documentation si le comportement visible ou la façon de développer change.
- Dans ton compte rendu, indique les fichiers touchés et les vérifications réellement effectuées. Ne prétends pas avoir vérifié un rendu ou lancé une commande qui ne l'a pas été.
