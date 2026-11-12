# Assistant de réunion

Ouvrir http://127.0.0.1:4317 dans Chrome sur Windows. Le serveur ne se lie qu’à l’interface locale.

## Démarrer

Node.js 24, Windows x64 et Chrome sont utilisés sur ce poste. Installer les dépendances avec `npm ci`. Définir `DEEPGRAM_API_KEY` dans le fichier local `.env`, puis lancer `npm start`. Pour la synthèse et la recherche sémantique, démarrer Ollama avec les modèles `qwen3.5:4b` et `embeddinggemma` (installables avec `ollama pull NOM`). Les variables `OLLAMA_MODEL` et `EMBED_MODEL` permettent de les changer.

## Utiliser

- **Démo hors ligne** utilise l’audio et les transcriptions de référence. **Démo avec Deepgram** transcrit les mêmes enregistrements en streaming. Le sélecteur propose une question simple et une discussion avec une modification budgétaire, une question pour Julie et une décision pour Camille.
- **Écouter mon onglet** cible Teams Web ou Google Meet. Choisir l’onglet avec le partage audio et autoriser le micro.
- **Écouter Teams Desktop** cible le processus Teams et ses descendants par WASAPI (Windows build 20348 ou ultérieure). Ouvrir Teams avant l’écoute. Le micro est acquis séparément dans Chrome. Un casque facilite la séparation.
- Importer le dossier avant le direct. Les PDF texte, pages PDF scannées (OCR français) et diapositives PPTX contenant du texte sont indexés avec leur emplacement. Les documents et les analyses sont traités localement. Seuls les flux audio et le prénom indicatif sont envoyés au service de transcription.
- Renseigner son prénom pour filtrer les questions. Une relance après une intervention locale peut aussi déclencher une réponse. Le bouton manuel traite la dernière intervention distante.
- La suggestion provisoire suit la transcription partielle. La réponse confirmée cite ses preuves. L’analyse locale approfondie apporte une formulation, des arguments et leurs sources. Les nouvelles questions remplacent les analyses en attente.
- **Arrêter** libère la capture. L’historique permet d’exporter un compte rendu Markdown incluant les suggestions. Le rechargement de la page efface la session.
- En démo audio, le dernier mot des scénarios est repéré dans le fichier. En direct, son instant est estimé avec les horodatages du moteur. La disponibilité est calculée pour le premier texte identique à la suggestion confirmée. Une anticipation avant la fin du dernier mot vaut 0 ms. La confirmation a son propre chronomètre. Ces mesures ne garantissent pas une limite de latence sur tous les appels.

## Vérifier

Lancer `npm test`. Avec le serveur en marche, `node scripts/check-demo.mjs` vérifie les deux scénarios de référence et l’export. Ajouter `--cloud` pour tester la transcription réelle (audio de démonstration envoyé à Deepgram). Le résultat peut varier avec la transcription, le réseau et les ressources du poste.
## Code et technologies

| Composant | Implémentation |
| --- | --- |
| Interface, démo, acquisition du micro et de l’onglet | HTML/CSS, JavaScript, Web Audio et AudioWorklet dans `public/` |
| Recherche rapide, calculs et détection du destinataire | `public/engine.mjs` |
| Serveur local et relais WebSocket vers Deepgram Nova-3 | Node.js dans `server.mjs` |
| Teams Desktop | `desktop.mjs` et module natif C++ `loopback-capture` utilisant WASAPI |
| Lecture PDF/PPTX et OCR | `documents.mjs` et `ocr.mjs` |
| Recherche sémantique et synthèse locale | `embeddings.mjs` et `synthesis.mjs` via Ollama |
| Tests | `test/` avec Node.js et `scripts/check-demo.mjs` avec Playwright |

L’interface affiche le niveau sonore des deux canaux en direct. Dans l’historique, chaque intervention distante peut recevoir une évaluation manuelle. Le compte rendu exporté conserve ces évaluations, les temps disponibles et les références de l’analyse.

## Application Windows

Le point d’entrée Rust est `src-tauri/src/main.rs`. L’exécutable Tauri utilise WebView2 et lance une instance privée du serveur Node sur un port local attribué par Windows. Pour Teams Desktop et la démo, utiliser la fenêtre native. Pour Teams Web ou Meet, utiliser son menu **Ouvrir dans Chrome** et conserver la fenêtre native ouverte pendant la réunion. La fermeture de l’application libère son serveur privé. Un serveur lancé séparément avec `npm start` a son propre cycle de vie.

Compiler avec `npm run desktop:build` depuis le dossier du projet (Rust MSVC x64, Visual Studio Build Tools C++ et SDK Windows requis). Lancer `src-tauri/target/release/assistant-reunion.exe`. L’exécutable peut aussi être placé à la racine du projet. Il utilise Node.js 24, les dépendances installées par `npm ci`, le fichier local `.env` et les modèles Ollama du poste. Pour vérifier la fenêtre, sa démo et la fermeture du serveur, exécuter `npm run desktop:test`. Le test utilise un profil WebView2 temporaire.

Le frontend est écrit en JavaScript. Le backend reste en Node.js et le moteur de capture WASAPI est le module natif C++ décrit ci-dessus.
