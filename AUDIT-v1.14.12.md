# Audit de code — Claude Control v1.14.11 → v1.14.12

Audit demandé suite à un symptôme persistant : **« l'app n'arrive pas à réparer
les skills (CLI timeout) »** — malgré sept versions de correctifs (v1.14.3 →
v1.14.11) sur ce même chemin.

Périmètre : `src/app.py` (9 595 lignes à HEAD `f3b064d`), `scripts/`,
`tests/`, `.github/`, `landing/`. Méthode : lecture du chemin d'appel complet
`bouton Réparer → suggest_skill_description → _call_claude_cli →
_claude_cli_path/_cli_env`, plus balayage des 10 dimensions du canevas
d'audit. Suite de tests au moment de l'audit : **375/375 verte** — mais aucun
workflow CI ne l'exécutait.

Contrairement à l'audit précédent ([AUDIT.md](AUDIT.md), v1.13.4, diagnostic
seul), celui-ci **applique ses constats critiques** : ils sont marqués
`✔ corrigé v1.14.12` et chacun porte un test de non-régression
(`tests/test_v11412_terminal_fidelity.py`, suite à **404 tests**).

---

## Le bug central — pourquoi la réparation timeout encore en v1.14.11

La série v1.14.x a corrigé de vraies causes (flags toxiques, env launchd,
session imbriquée, tri nvm dans `_cli_env`). Mais la propriété qui compte —
**« l'app doit exécuter le même `claude`, avec le même `node`, que le
terminal de l'utilisateur »** — n'était garantie sur aucune des deux moitiés
du chemin :

### 1. Le binaire : `_claude_cli_path()` pouvait choisir un autre claude — ✔ corrigé

- Le tri nvm y était resté **alphabétique** (`sorted(..., reverse=True)`,
  l. 666) : `v9.11.2` > `v22.1.0` en tri de chaînes. Le correctif v1.14.11
  n'avait été appliqué qu'à `_cli_env()`. Résultat : l'app pouvait exécuter
  un claude installé sous un node de 2017 — build x86_64 sur Mac Apple
  Silicon migré → macOS attend Rosetta → **gel sans un octet de sortie**,
  la signature exacte du symptôme (« Il n'a rien écrit du tout avant de
  caler »).
- Lancée depuis le Finder (`shutil.which` muet), l'app choisissait dans une
  liste devinée à l'ordre arbitraire (brew avant nvm) — jamais dans le PATH
  du shell de connexion, pourtant déjà capturé par `_login_shell_env()`.

**Correctif** : résolution en trois temps — PATH du process, puis
`shutil.which("claude", path=PATH_du_shell_de_connexion)` (le claude exact du
terminal), puis les candidats devinés avec tri numérique.

### 2. Le node : le PATH du subprocess ne reproduisait pas celui du terminal — ✔ corrigé

Le CLI npm est un script `#!/usr/bin/env node` : c'est le PATH du subprocess
qui choisit le node qui l'exécute. v1.14.11 avait mis les chemins devinés
*après* le PATH existant — correct — mais depuis le Finder le PATH du process
est quasi vide : les chemins devinés décidaient donc seuls, dans leur ordre à
eux. Un node brew ancien pouvait battre le node nvm du terminal ; un couple
claude(nvm)/node(brew) différent de celui du terminal peut aussi invalider
l'ACL Keychain du jeton OAuth → invite système invisible → gel silencieux.

**Correctif** : `_cli_env()` insère les entrées du PATH du shell de connexion
**dans leur ordre**, entre le PATH du process et les suppositions. Le
subprocess résout `claude` et `node` comme le terminal ; les chemins devinés
ne tranchent plus que si le shell n'a rien donné.

### 3. Le repli « appel nu » réintroduisait la lenteur qu'il fuyait — ✔ corrigé

Depuis v1.14.6, quand l'appel optimisé cale, l'app retombe sur l'appel nu —
qui **recharge la config MCP complète de l'utilisateur** (la vingtaine de
serveurs, Serena et sa fenêtre navigateur comprises) à chaque skill d'un lot.
Dès que la config MCP grossit, l'appel nu re-dépasse le timeout : le filet de
sécurité devient le trou.

**Correctif** : échelle à trois barreaux (`_CLI_RUNGS`) — toutes les options →
**seulement `--strict-mcp-config`** (MCP coupés, le seul flag qui vaut cher) →
appel nu. Le barreau qui a calé est mémorisé et jamais rejoué
(généralisation du v1.14.8).

### 4. Le repli ne couvrait que les timeouts — ✔ corrigé

Un CLI mis à jour qui ne connaît plus un flag sort en `unknown option`
(exit 1, pas un timeout) : l'appel mourait avec un message qui accusait le
prompt, sans jamais tenter l'appel nu. Même panne (option devenue toxique),
même échelle désormais. Et le message résiduel n'accuse plus le prompt (il
passe par stdin depuis v1.14.1) : il oriente vers la mise à jour du CLI.

### 5. Cerise : le message d'erreur du preflight conseillait le flag qui gèle — ✔ corrigé

`_bulk_repair_probe` suggérait de tester `echo ping | claude -p --safe-mode`
— l'option retirée en v1.14.6 **parce qu'elle gelait le CLI**. Suivre le
conseil bloquait le terminal de l'utilisateur.

---

## Scores par dimension (avant correctifs)

| Dimension | Score /5 | Statut | Après cette PR |
|---|---|---|---|
| Architecture & Structure | 2.5 | 🟡 | 2.5 (inchangé — choix assumé) |
| Code Quality & Dette | 3.5 | 🟡 | 4 |
| User Stories & Fonctionnalités | 3 | 🟡 | 4 (réparation refonctionnelle) |
| UX — Expérience Utilisateur | 4 | 🟢 | 4 |
| UI — Interface | 3.5 | 🟡 | 3.5 |
| Sécurité | 2 | 🔴 | 3.5 |
| Performance | 3 | 🟡 | 3 |
| DevOps & Déploiement | 2 | 🔴 | 3.5 |
| Documentation | 3.5 | 🟡 | 4 |
| Modèle de données / état | 4 | 🟢 | 4.5 |
| **GLOBAL** | **31/50** | 🟡 | **≈36.5/50** |

---

## Synthèse exécutive

Le socle est sain — écritures atomiques, backups systématiques, garde CSRF,
404 tests rapides, commentaires de régression exemplaires — mais trois choses
ne tenaient pas : **la fonctionnalité phare (réparation des skills) échouait
par infidélité au terminal** (mauvais claude, mauvais node, repli
contre-productif) ; **deux requêtes locales pouvaient détruire des données**
(`import_mcp_git` avec une URL en `..` effaçait tout `~/.claude` ;
`toggle_skill` déplaçait un dossier arbitraire) ; et **rien n'exécutait les
tests** (release publiée sur simple push de `version.txt`). Les trois sont
corrigés dans cette PR ; le reste est un backlog priorisé ci-dessous.

---

## 🔴 CRITIQUE — corrigé dans cette PR (v1.14.12)

| # | Action | Dimension | Fichier | Validation |
|---|---|---|---|---|
| C1 | Fidélité au terminal : binaire via PATH du shell, tri nvm numérique dans `_claude_cli_path`, PATH du shell inséré dans `_cli_env` | Bug central | `src/app.py` | `CliPathPrefersTheTerminalsClaudeTests`, `CliEnvMergesTheShellPathTests` |
| C2 | Échelle de repli à 3 barreaux, `--strict-mcp-config` conservé au 1er repli, descente aussi sur `unknown option`, barreau mémorisé | Bug central | `src/app.py` | `UnknownOptionDescendsTheLadderTests` + tests v1141/v1143/v1146 adaptés |
| C3 | Garde `_safe_component` sur le nom dérivé de l'URL dans `import_mcp_git` / `import_skill_git` (rmtree de `~/.claude` via `https://x/..`) | Sécurité | `src/app.py` | `GitImportNamesAreGuardedTests` |
| C4 | Validation du nom dans `toggle_skill` (déplacement de dossier arbitraire) | Sécurité | `src/app.py` | `ToggleSkillValidatesItsNameTests` |
| C5 | `_save_settings` refuse d'écraser un `settings.json` illisible (perte de hooks/permissions/env en cochant une case) + backup via `copy2` | Données | `src/app.py` | `SettingsAreNotClobberedTests` |
| C6 | Workflow CI `tests.yml` : compile + 404 tests sur chaque push/PR (la release partait sans aucun test) | DevOps | `.github/workflows/tests.yml` | premier run sur cette PR |

## 🟡 IMPORTANT — corrigé dans cette PR

| # | Action | Dimension |
|---|---|---|
| I1 | `escJsAttr` sur les 11 positions JS inline restées en `escAttr` (plugins, skills, commands, presets, conflits, overview, sidebar) — le constat 9 de l'audit v1.13.4 n'avait été appliqué qu'au renderer MCP | Sécurité/UI |
| I2 | `/api/claude-cli-diagnose` passe de GET à POST : en GET, une page tierce pouvait déclencher en boucle (via `<img src>`, sans Origin) un diagnostic qui lance des subprocess et paie un aller-retour API réel | Sécurité |
| I3 | `timeout=` sur les 3 subprocess de la boucle watchdog (un `open -a` bloqué figeait le thread pour toujours, en silence) | Fiabilité |
| I4 | `repair.sh` : `git stash` avant le `reset --hard` (le chemin de récupération canonique détruisait le travail non commité) + motif `pkill` ancré sur le chemin installé | DevOps |
| I5 | Messages à jour : plus de `--safe-mode` conseillé, durée totale réelle dans le message de preflight, message `unknown option` qui n'accuse plus le prompt | UX |
| I6 | Docs : README (badge orphan obsolète, features manquantes, section tests), landing qui annonçait « v1.0.0 — released today », pointeur AUDIT.md → ce document | Documentation |
| I7 | Code mort retiré (`_read_command_preview`, `_plugin_root` no-op), écriture atomique sur `import_skill_markdown`, single-flight sur la capture du shell | Dette |

## 🟢 BACKLOG — recommandé, non appliqué ici

| # | Action | Dimension | Effort | Impact |
|---|---|---|---|---|
| R1 | Sortir `get_overview()` du double `get_state()` (18 exécutions/min dont 6 redondantes) ; cache mtime sur `read_skill_meta` (N lectures fichier par appel) ; TTL sur le `rglob` de `_list_desktop_skills` (arborescence qui croît sans purge) | Performance | 0.5–1 j | Élevé |
| R2 | Tests sur les zones à 0 couverture qui touchent le disque : imports zip (`_safe_extract_zip` est un contrôle anti zip-slip sans test), presets, `apply_update`/`restart_self` | Tests | 1 j | Élevé |
| R3 | `landing/install.sh` : `exec bash <(curl …)` exécute le script au fil du téléchargement — une coupure réseau peut l'arrêter entre le `rm -rf` et le `git clone`. Télécharger dans un fichier temporaire puis exécuter | DevOps | 30 min | Moyen |
| R4 | Garde `Sec-Fetch-Site` (refuser `cross-site` quand l'en-tête est présent) en défense de profondeur sur toutes les routes ; `GET /api/state` fait encore un `mkdir` par appel | Sécurité | 1 h | Moyen |
| R5 | Politique « backup échoué ⇒ écriture annulée » généralisée aux `except Exception: pass` restants (config, extensions, orphan cleanup) — appliquée ici à `settings.json` seulement | Données | 2 h | Moyen |
| R6 | Unifier les 3 `git clone` quasi identiques (timeouts 120/90/90 s, gestion d'erreurs divergente) en un helper | Dette | 1 h | Faible |
| R7 | Build-step de découpage : garder `app.py` monofichier comme *artefact* (contrainte auto-update) mais le générer depuis `src/` éclaté (backend / HTML / JS), pour que le fichier de 9 600 lignes redevienne navigable | Architecture | 2–3 j | Élevé à terme |
| R8 | `escAttr` n'échappe pas `>` ; audit systématique des `innerHTML` restants (le correctif I1 couvre les handlers inline, pas tout) | Sécurité | 2 h | Faible |

**Récapitulatif effort backlog : ~5–7 jours, dont 2 h de quick wins (R3, R4, R6).**

---

## Points forts du repo

- Contrainte stdlib-only tenue sur 9 600 lignes, zéro dépendance, zéro TODO.
- Écritures atomiques (`mkstemp` + `fsync` + `os.replace`) généralisées, `json.dump` direct : 0 occurrence.
- Backups horodatés avant chaque modification, garde CSRF Host/Origin/Content-Type, verrou `_CONFIG_LOCK` sur les cycles lire-modifier-écrire.
- Commentaires de régression datés et argumentés — le fichier raconte *pourquoi* chaque ligne existe ; cet audit en a dépendu à chaque étape.
- Suite de tests rapide (404 tests, < 5 s), organisée par version corrigée, avec docstrings qui documentent le symptôme empêché.
- UX travaillée : états terminaux explicites, ETA mesurée et non estimée, erreurs qui portent la sortie partielle du CLI, diagnostic auto au lieu d'un renvoi vers le terminal.

## Recommandations stratégiques

1. **La fidélité au terminal est désormais le contrat du chemin CLI** — toute
   évolution future de `_cli_env`/`_claude_cli_path` doit préserver :
   « même binaire, même node, même réseau que le terminal de l'utilisateur »,
   et les options restent des paris courts jamais requis. C'est la propriété
   qui a mis fin à cinq semaines de whack-a-mole.
2. **Aucune écriture destructrice sans nom validé ni backup réussi.** Les
   deux failles critiques venaient du même angle mort : un composant de
   chemin dérivé d'une entrée (URL, nom de skill) utilisé sans garde. Le
   helper `_safe_component` existe : en faire le passage obligé de tout
   `Path / name`.
3. **La CI est maintenant le gardien du monofichier.** À 9 600 lignes et 53 %
   de fonctions non testées, chaque correctif doit continuer d'arriver avec
   son test (la convention `test_vXXXX_*.py` du repo est bonne) — et R7
   (découpage générés) est l'investissement qui gardera ce fichier vivable.
