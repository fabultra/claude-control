# La machine à états « santé du CLI Claude »

La réparation des skills repose sur le CLI Claude Code (`claude`). Des
semaines de « timeouts » muets (v1.14.x) ont produit une chaîne complète de
détection, diagnostic et auto-réparation. Cette page est la carte de ce qui
vit dans `src/parts/backend.py`.

## Vue d'ensemble

```
                    ┌─────────────────────────────────────┐
                    │  Appel CLI (_call_claude_cli)       │
                    │  prompt via stdin, start_new_session│
                    └───────────────┬─────────────────────┘
              succès ◄──────────────┤
                                    │ TimeoutExpired / unknown option
                                    ▼
                    ┌─────────────────────────────────────┐
                    │  Sortie partielle porte un marqueur │
                    │  d'authentification ?               │
                    │  (_looks_not_logged_in)             │
                    └──────┬──────────────────┬───────────┘
                     oui   │                  │ non
                           ▼                  ▼
              ClaudeCliNotLoggedIn    Échelle de repli (_CLI_RUNGS)
              → UX de reconnexion     barreau suivant : moins d'options
              (dialogue + bandeau)    3 barreaux : complet → MCP off → nu
                                            │ tous épuisés
                                            ▼
                                      ClaudeCliTimeout
                                      → auto-heal (une fois/process)
                                      → diagnostic décisif
```

## Les briques, dans l'ordre d'une réparation

### 1. Résolution du binaire — `_claude_cli_path`
PATH du process → PATH du login shell (`_login_shell_env`, mis en cache) →
candidats devinés (`~/.local/bin/claude`, nvm trié numériquement par
`_node_version_key`, npm global…). Premier chemin exécutable gagne.

### 2. Environnement fidèle — `_cli_env`
Fusion PATH process/shell/candidats, liste fermée `_CLI_ENV_INHERIT`
(jamais tout l'environnement), purge des `CLAUDE_CODE_*` hérités puis
`DISABLE_AUTOUPDATER=1` et `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.

### 3. Pas de questions invisibles
- `start_new_session=True` sur **tous** les spawns : le CLI ne peut pas
  poser une question sur un /dev/tty que personne ne voit — c'est ce qui a
  transformé les gels muets en messages capturables (v1.14.14).
- `_pretrust_cli_workdir()` : `hasTrustDialogAccepted` écrit d'avance dans
  `~/.claude.json` pour le dossier de travail neutre `CLI_WORKDIR`.

### 4. L'appel et l'échelle — `_call_claude_cli` + `_CLI_RUNGS`
Trois barreaux : toutes options → `_CLI_MCP_OFF` seul → appel nu. Descente
sur timeout **et** sur `unknown option` (un CLI mis à jour peut retirer un
flag). Le barreau qui répond est mémorisé (`_CLI_FALLBACK`).

### 5. Session expirée — la cause réelle des gels de 2026-08
- Marqueurs (`_NOT_LOGGED_IN_MARKERS`) : « login expired »,
  « Re-authenticate », « Not logged in »… détectés même dans la sortie
  partielle d'un timeout → `ClaudeCliNotLoggedIn`, jamais l'échelle
  (redescendre ne reconnecte personne).
- Lecture locale de l'expiration du jeton OAuth
  (`_read_cli_oauth_expiry` → `_check_cli_session`, cache 5 min, marge
  1 h pour laisser sa chance au refresh). **Seul `expiresAt` est lu — le
  secret n'est jamais conservé ni loggé.**
- `_ask_relogin_dialog` : boîte macOS « Ouvrir le Terminal ? » pré-remplie
  de `claude /login`. Armée **uniquement par `main()`** (jamais depuis les
  tests), cooldown 30 min. Le bandeau rouge « Se reconnecter » de l'onglet
  Skills vient de `cli_session_expired` dans `/api/state`.

### 6. Auto-réparation — `_cli_auto_heal_after_timeout`
Sur timeout avec signature « binaire périmé » (version installée <
dernière publiée, `_check_cli_freshness` via le registre npm) : l'app met
le CLI à jour elle-même (`update_claude_cli`, installeur épinglé
`https://claude.ai/install.sh` **dans le code**, jamais depuis une
requête), vérifie que la version a changé, réarme l'échelle. Une fois par
process, armée uniquement par `main()`.

### 7. Diagnostic décisif — `_diagnose_claude_cli(_stages)`
Sondes indépendantes, chacune avec verdict :
`--version` → node (`_check_cli_node`) → jeton Keychain
(`_check_cli_credentials`, existence **pas** validité, secret jeté) →
réseau (`_check_api_reachable`, un 401 prouve la connectivité) → appel nu
(`_cli_probe`) → flag coupable (`_blame_cli_flag`) → journal `--debug`
(`_cli_debug_tail`) → fraîcheur (`_check_cli_freshness`). « Copier le
rapport » exporte tout.

### 8. Dernier recours prouvé — la réparation via le Terminal
`_build_terminal_repair_run` écrit prompts + `run.sh` trivial
(`claude -p < prompt > out`, sorties atomiques `.tmp` → `mv`, marqueur
`DONE`) ; Terminal.app exécute dans le **vrai** environnement de
l'utilisateur ; `_terminal_repair_watcher` applique les descriptions côté
app (stall 240 s). Fonctionne par construction, quelle que soit la
différence résiduelle d'environnement.

## Invariantes de construction (à ne jamais casser)

1. **Armement par `main()` seulement** : auto-heal, dialogue de
   reconnexion, rétention des backups. Une suite de tests ne peut ni
   lancer un installeur, ni afficher un dialogue, ni purger un disque.
2. **Jamais de sentinelle `ts = 0.0`** avec `time.monotonic()` (qui part
   du boot : ~90 s sur un runner CI neuf). Sentinelle `None` ou recul de
   `TTL + 1`.
3. **Le secret OAuth ne sort jamais** de `_read_cli_oauth_expiry` — testé
   (`assertNotIn(SECRET, json.dumps(...))`).
4. **Domaine d'installation épinglé dans le code**, jamais dérivé d'une
   requête.
5. Les caches transverses sont **à clé de chemin**
   (`_state_cache_key`) : l'hermétisme des tests est garanti par
   construction, pas par `tests/__init__.py` (qui ne s'exécute pas sous
   `unittest discover -s tests`).
