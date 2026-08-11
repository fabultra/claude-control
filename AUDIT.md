# Audit de code — Claude Control v1.13.4

Audit demandé suite à deux symptômes : **« le CLI ne répond plus »** et **« je ne vois pas tous les MCP »**.

Périmètre : `src/app.py` (7444 lignes), `scripts/`, `tests/`.
Méthode : lecture du code, exécution de la suite de tests, traçage des chemins d'appel HTTP → backend.
Suite de tests au moment de l'audit : **115/115 verte** (`python3 -m unittest discover -s tests`).
Les tests passent, mais ils ne couvrent aucun des chemins critiques identifiés ci-dessous.

Chaque constat cite un numéro de ligne vérifié. Les deux symptômes sont expliqués.

---

## Résumé exécutif

| # | Constat | Gravité | Symptôme |
|---|---|---|---|
| 1 | `get_skill_usage()` re-parse tout `~/.claude/projects` à chaque requête, 24×/min | Critique | Ne répond plus |
| 2 | `/api/state` lance ~50 sous-process (`pgrep`/`lsof`) par appel, toutes les 5 s | Critique | Ne répond plus |
| 3 | `get_running_mcps()` repose sur 6 mots-clés codés en dur | Critique | MCP manquants |
| 4 | Trois sources de MCP jamais lues (CLI user, projet, plugins) | Élevée | MCP manquants |
| 5 | `read_mcp_error()` charge des logs entiers en mémoire | Élevée | Ne répond plus |
| 6 | `restart_claude()` fait `pkill -9 -f Claude` (motif non ancré) | Élevée | Ne répond plus |
| 7 | Aucune garde de concurrence sur le polling côté UI | Élevée | Ne répond plus |
| 8 | `loadState()` sans `.catch()` : liste MCP figée et silencieuse en cas d'erreur | Élevée | MCP manquants |
| 9 | Nom de MCP injecté brut dans `innerHTML` (XSS + casse le rendu) | Élevée | MCP manquants |
| 10 | CSRF possible via formulaire `text/plain` sur les routes destructrices | Élevée | — |
| 11 | `_notify()` : ordre d'échappement AppleScript inversé | Moyenne | — |
| 12 | Écritures de config non atomiques + races entre threads | Moyenne | — |

---

## Symptôme A — « Le CLI ne répond plus »

Le blocage ne vient pas de l'intégration Claude Code CLI. Il vient du fait que **le serveur HTTP est saturé par son propre polling**. Trois causes s'additionnent, et elles se dégradent avec le temps — ce qui correspond à « ne répond *plus* ».

### 1. `get_skill_usage()` relit tout l'historique de sessions à chaque requête — Critique

`src/app.py:2986`. La fonction parcourt `~/.claude/projects/**/*.jsonl`, ouvre chaque fichier modifié dans les 30 derniers jours et fait un `json.loads()` **sur chaque ligne** :

```python
for jsonl in PROJECTS_LOGS_DIR.rglob("*.jsonl"):      # 3014
    ...
    for line in f:                                     # 3027
        obj = json.loads(line)                         # 3033
```

Aucun cache, aucun plafond de taille, aucune limite de lignes, aucun budget temps.

Le problème est le nombre d'appels :

- `get_state()` l'appelle à la ligne **687** → sert `/api/state`, **polled toutes les 5 s** (ligne 7195).
- `get_overview()` l'appelle **deux fois** : indirectement via `get_state()` ligne **3198**, puis à nouveau ligne **3236** → sert `/api/overview`, **polled toutes les 10 s**.

Soit par minute : `12 × 1 + 6 × 2 =` **24 scans complets de `~/.claude/projects/`**.

Sur une machine qui utilise Claude Code quotidiennement, ce répertoire pèse couramment plusieurs Go. Dès que le scan dépasse 5 secondes, chaque tick de polling démarre un nouveau thread qui refait le scan entier avant que le précédent ait fini. Les threads s'accumulent, le GIL est saturé par du parsing JSON, la mémoire monte. **L'app cesse de répondre, et le seuil est franchi progressivement à mesure que l'historique grossit.**

**Correctif.** Mettre en cache le résultat avec un TTL (5 min suffisent — c'est une statistique d'usage, pas un état temps réel), le calculer dans un thread de fond, et le sortir du chemin de `/api/state`. Au minimum : supprimer le double appel dans `get_overview()` en réutilisant le résultat déjà calculé par `get_state()`.

### 2. `/api/state` lance des dizaines de sous-process à chaque appel — Critique

Toujours dans `get_state()`, pour **chaque** Desktop Extension (ligne **768**) :

```python
running_ext = bool(_extension_pids(e["name"]))
```

`_extension_pids()` (ligne **1996**) essaie jusqu'à 3 empreintes, chacune via un `pgrep` (`_safe_pids_for_fingerprint`, ligne **1966**, timeout 3 s). Si aucune ne matche, il enchaîne sur 3 fallbacks `lsof` (`_pids_via_lsof`, ligne **1499**, timeout 3 s), chacun suivi d'un `ps -p` par PID (ligne **1516**, timeout 2 s).

Or le code documente lui-même, ligne **2214**, que sur Claude Desktop moderne les extensions tournent dans des Helper Node anonymes que `pgrep` ne matche jamais. **Le pire cas est donc le cas nominal** : ~6 sous-process par extension, à chaque appel.

Avec 8 extensions : ~48 `fork/exec` par `/api/state`. Multiplié par 12 appels/min, plus autant via `/api/overview` : **~1 150 créations de process par minute**, chacune pouvant bloquer un thread jusqu'à 3 s. S'y ajoute un `ps auxww` (ligne **84**) par appel.

**Correctif.** Un seul `ps auxww` par cycle, mis en cache ~10 s et partagé entre toutes les extensions, au lieu d'un `pgrep`/`lsof` par extension.

### 3. `read_mcp_error()` charge des logs entiers en mémoire — Élevée

`src/app.py:3465` :

```python
content = log_file.read_text(errors="replace")
```

Les candidats incluent `mcp.log` et `main.log` (ligne **3458**), qui atteignent facilement plusieurs centaines de Mo. Le fichier entier est chargé, puis `_scan_log_for_error()` (ligne **3433**) fait un `splitlines()` puis une list-comprehension de filtrage — soit environ 3× la taille du fichier en RAM, alors que seules les **300 dernières lignes** sont utilisées (ligne 3436).

Même problème dans `_mcp_log_says_frozen()` (ligne **1561**), appelé par la boucle watchdog, qui lit tout le fichier pour ne regarder que les 4000 derniers caractères (ligne 1564).

Le projet contient déjà le helper correct — `_read_log_tail()` (ligne **2387**), lecture bornée par `seek()`, testé dans `tests/test_v183_regressions.py`. Ces deux chemins ne l'utilisent pas.

**Correctif.** Remplacer les deux `read_text()` par `_read_log_tail()`.

### 4. `restart_claude()` utilise un motif `pkill` non ancré — Élevée

`src/app.py:3365` :

```python
subprocess.run(["pkill", "-9", "-f", "Claude"], check=False)
```

`pkill -f` matche la **ligne de commande complète** de tous les process de l'utilisateur. Le motif `Claude` n'est ancré sur rien.

La fonction sœur `restart_claude_desktop()` (ligne **2254**) fait la chose correcte :

```python
subprocess.run(["pkill", "-9", "-f", "/Applications/Claude.app/Contents/MacOS/Claude"], ...)
```

Deux fonctions, même intention, portées radicalement différentes. Le bouton « Redémarrer Claude » (lignes 4485 et 4772) appelle la version large.

Collatéral confirmé dans le dépôt : `_call_claude_cli()` (ligne **393**) lance `[cli_path, "-p", prompt, ...]` où `prompt` est le contenu d'un `SKILL.md` — qui contient presque toujours le mot « Claude ». Un « Redémarrer Claude » pendant une suggestion en vol tue le CLI. Tout autre process de l'utilisateur dont l'`argv` contient « Claude » est également tué (session Claude Code lancée depuis un chemin contenant « Claude », `tail -f ~/Library/Logs/Claude/main.log`, etc.).

Le même motif non ancré est exécuté **automatiquement** par le watchdog, ligne **2931**, quand `_claude_responsive()` échoue. Nuance importante : le watchdog est opt-in (`enabled: False`, `freeze_detection: False`, ligne **1373**). Mais s'il a été activé, un simple `osascript` lent — Claude Desktop occupé sur un tour long — suffit à déclencher un `pkill -9` toutes les 30 s. **À vérifier en premier sur la machine concernée : `cat ~/.claude/claude-control-watchdog.json`.**

**Correctif.** Aligner `restart_claude()` sur le motif ancré de `restart_claude_desktop()`, ici et ligne 2931. Idéalement supprimer `restart_claude()` et router `/api/restart-claude` vers `restart_claude_desktop()`.

### 5. Aucune garde de concurrence ni timeout côté UI — Élevée

Ligne **7195** :

```js
setInterval(loadOverview,10000); setInterval(loadState,5000);
setInterval(loadPlugins,15000); setInterval(loadCommands,30000);
setInterval(loadWatchdog,10000); setInterval(loadWatchdogTab,10000);
```

Aucun de ces loaders ne teste si la requête précédente est encore en vol. `loadState()` (ligne **5484**) fait un `fetch` nu, sans `AbortController` ni timeout. Quand le serveur ralentit à cause des points 1 et 2, le navigateur empile les requêtes, qui empilent les threads serveur — boucle de rétroaction qui transforme un ralentissement en gel complet.

**Correctif.** Un flag `inFlight` par loader, et remplacer `setInterval` par un `setTimeout` re-armé après la fin de la requête.

---

## Symptôme B — « Je ne vois pas tous les MCP »

Quatre causes distinctes, indépendantes les unes des autres.

### 6. `get_running_mcps()` repose sur 6 mots-clés codés en dur — Critique

`src/app.py:82` :

```python
keywords = {"mongodb-mcp": "klide-mongodb", "mailchimp-mcp": "mailchimp",
            "sekoia-geo": "sekoia-geo", "compass-mcp": "compass",
            "thedotmack/plugin": "claude-mem-search", "mcp-pdf": "pdf"}
```

La fonction scanne `ps auxww` et ne peut retourner que ces 6 étiquettes. Le résultat est ensuite comparé au nom de la clé de config (ligne **749**) :

```python
[{"name": n, ..., "running": n in running, ...} for n in sorted(active.keys())]
```

Conséquence : **tout MCP classique absent de cette table a `running = False`, définitivement.** L'UI (ligne **5523**) sépare la liste en deux colonnes sur ce champ :

```js
const running = s.mcps.filter(m=>m.running);
const stopped = s.mcps.filter(m=>!m.running);
```

Tous les MCP non listés atterrissent donc dans la colonne « Inactifs », avec un badge `?` « pas démarré · pourquoi ? » (ligne 5503) — même s'ils tournent parfaitement. Ils ne sont pas absents, ils sont **tous relégués du mauvais côté**, ce qui se lit exactement comme « je ne vois pas mes MCP ».

De plus, le match ne fonctionne que si l'étiquette est **exactement** égale à la clé de config. Cette table décrit une machine précise, pas un cas général.

**Correctif.** Dériver l'empreinte de chaque MCP depuis sa `command`/`args` — la logique existe déjà dans `_mcp_process_fingerprint()` (ligne **1429**) — et matcher sur la sortie `ps` déjà collectée, au lieu d'une table statique.

### 7. Trois sources de MCP ne sont jamais lues — Élevée

`get_state()` (ligne **673**) ne construit `mcps_list` qu'à partir de deux sources :

1. `claude_desktop_config.json` → `mcpServers` + `_disabledMcps` (ligne 749-751)
2. les Desktop Extensions via `_list_extensions()` (ligne 752)

Ne sont **jamais** lues :

- **`~/.claude.json`** — les MCP user-scope de Claude Code CLI (`claude mcp add`). Zéro occurrence dans tout le fichier.
- **`<projet>/.mcp.json`** — les MCP project-scope.
- **`<plugin>/.mcp.json`** — les MCP fournis par les plugins. Ils sont bien lus par `_read_plugin_mcp_servers()` (ligne **3972**) mais n'apparaissent **que dans l'onglet Plugins**, jamais dans l'onglet MCP.

C'est cohérent avec le README (« Manage your Claude **Desktop** MCPs »), mais l'app affiche aussi les plugins Claude Code et propose un bouton « bridge » — l'utilisateur attend donc légitimement une vue unifiée. Si les MCP manquants sont ceux ajoutés via `claude mcp add`, c'est ici la cause, et c'est une fonctionnalité absente plutôt qu'un bug.

**Correctif.** Lire `~/.claude.json` et remonter les MCP de plugins dans l'onglet MCP avec un badge de provenance (`desktop` / `cli` / `plugin`), sur le modèle de ce qui est déjà fait pour les skills (`_source`, ligne 721).

### 8. `loadState()` n'a pas de `.catch()` — la liste se fige en silence — Élevée

`src/app.py:5485` :

```js
const s = await (await fetch('/api/state')).json();
```

Aucun `try/catch`. Si `/api/state` renvoie une erreur, met trop longtemps, ou si `get_state()` lève (par exemple `load_config()` ligne **66** sur un `claude_desktop_config.json` malformé — `json.load` sans `try`), la promesse est rejetée. Le rejet n'est pas traité : **la liste MCP conserve indéfiniment son dernier rendu, sans aucun message d'erreur**, et `loadMcpConflicts()` (ligne 5542) ne s'exécute jamais.

Un `claude_desktop_config.json` invalide (virgule en trop après une édition manuelle) produit exactement le symptôme rapporté : les MCP semblent avoir disparu, sans explication.

**Correctif.** `try/catch` autour de chaque loader, avec bandeau d'erreur visible. Et rendre `load_config()` tolérant en renvoyant une erreur structurée plutôt qu'en levant.

### 9. Le nom de MCP est injecté brut dans `innerHTML` — Élevée

`_renderMcpRow()`, ligne **5520** :

```js
<span class="font-medium truncate">${m.name}</span>
```

et ligne **5500** :

```js
const toggleFn = isExt ? `toggleExtension('${m.name}', this.checked)` : `toggleMcp('${m.name}')`;
```

Le helper `escAttr()` existe (ligne **5632**) et est utilisé 70 fois dans le fichier — y compris juste au-dessus, ligne 5499, pour `m.version`. Il est omis pour `m.name`.

Deux conséquences :

- Un nom contenant `'` casse le JavaScript de la ligne ; un nom contenant `<` ouvre une balise qui **avale les lignes suivantes** — des MCP disparaissent visuellement de la liste.
- C'est une XSS. Les noms de MCP viennent de `claude_desktop_config.json`, alimenté par `import_mcp_json()`, `import_mcp_git()` et `import_mcp_zip()` (lignes 3688-3739, 3837). Un dépôt Git ou un ZIP hostile injecte du JS dans une page qui peut appeler `/api/delete-mcp`, `/api/apply-update` et `/api/restart-self`.

**Correctif.** `escAttr(m.name)` dans les deux positions. Vérifier aussi les noms de skills, de plugins et de commandes rendus de la même façon.

---

## Sécurité

### 10. CSRF sur toutes les routes POST destructrices — Élevée

Le serveur écoute sur `127.0.0.1` (ligne **7414**) sans vérification d'`Origin`, de `Host`, ni jeton CSRF. `do_POST` (ligne **7313**) parse le corps **sans regarder le `Content-Type`** :

```python
data = json.loads(self.rfile.read(length)) if length else {}
```

Les `fetch` cross-origin en `application/json` sont bloqués par le préflight CORS (le serveur ne gère pas `OPTIONS`). Mais un formulaire HTML en `enctype="text/plain"` est une *simple request* : pas de préflight, corps arbitraire. N'importe quel site visité par l'utilisateur peut donc déclencher `/api/delete-mcp`, `/api/delete-plugin`, `/api/plugin-cleanup`, `/api/apply-update` ou `/api/restart-self`. L'attaquant ne lit pas la réponse, mais ces routes agissent quand même. L'absence de contrôle du `Host` ouvre aussi le DNS rebinding.

**Correctif.** Rejeter toute requête dont l'`Origin` n'est pas `http://localhost:8765` / `http://127.0.0.1:8765`, exiger `Content-Type: application/json`, et valider l'en-tête `Host`.

### 11. `_notify()` : ordre d'échappement AppleScript inversé — Moyenne

`src/app.py:55` :

```python
safe_title = (title or "").replace('"', '\\"').replace('\\', '\\\\')[:120]
```

Les guillemets sont échappés **avant** les antislashs. Un `"` devient `\"`, puis le second `replace` transforme son antislash en `\\` : le résultat est `\\"`, c'est-à-dire un antislash échappé **suivi d'un guillemet non échappé**, qui ferme la chaîne AppleScript.

Vérification sur `He said "hi"` → `He said \\"hi\\"` → chaîne terminée prématurément.

L'ordre correct est l'inverse : antislashs d'abord, guillemets ensuite. Comme AppleScript permet `do shell script`, une chaîne contrôlée par l'attaquant (nom de MCP issu d'un import hostile, propagé jusqu'à `_notify()` par `restart_mcp`/`stop_mcp`) permet l'exécution de commandes.

`_show_dialog()` (ligne **7382**) interpole sans aucun échappement, mais n'est alimenté que par une trace d'exception locale.

**Correctif.** Inverser les deux `replace`. Mieux : passer la chaîne en argument `osascript` séparé plutôt que par interpolation.

### 12. Écritures de configuration non atomiques et concurrentes — Moyenne

`save_config()` (ligne **78**), `_save_extension_settings()` (ligne **1690**), `save_watchdog_config()` (ligne **1424**), `save_settings()` (ligne **3312**) écrivent tous directement sur le fichier final :

```python
with open(CONFIG_PATH, "w") as f:
    json.dump(config, f, indent=2)
```

L'ouverture en `"w"` tronque immédiatement. Si le `json.dump` échoue en cours de route, ou si le process est tué entre-temps — ce que `restart_self()` (ligne **4430**) et le `pkill` du point 4 peuvent provoquer — **`claude_desktop_config.json` reste tronqué**, et Claude Desktop ne charge plus aucun MCP. C'est un chemin plausible vers le symptôme B.

Le serveur étant multi-thread (ligne **7411**) sans verrou, deux `toggle_mcp` simultanés font aussi un cycle lecture-modification-écriture concurrent : la dernière écriture écrase l'autre.

Les backups horodatés existent et atténuent la perte, mais ne l'empêchent pas.

**Correctif.** Écrire dans un fichier temporaire du même répertoire puis `os.replace()` (atomique sur POSIX), et protéger le cycle read-modify-write par un `threading.Lock` global.

---

## Points positifs

- Contrainte stdlib-only tenue sur 7444 lignes, sans dépendance.
- Backups horodatés systématiques avant chaque modification de config.
- `_safe_pids_for_fingerprint()` (ligne 1960) : approche par allow-list de launchers avant tout `kill`, avec exclusion explicite du process courant. Bonne défense.
- `_read_log_tail()` (ligne 2387) : lecture bornée par `seek`, correctement testée — le helper est bon, il manque juste aux deux appelants du point 3.
- `_safe_extract_zip()` (ligne 3795) rejette les entrées absolues et les `..`.
- `reveal_path_in_finder()` (ligne 4170) valide la racine après `resolve()`.
- Commentaires de régression datés et précis, qui documentent le *pourquoi* de chaque correctif.
- 115 tests unitaires sans dépendance externe.

---

## Plan d'action

**Débloquer le symptôme A** — dans l'ordre :

1. Mettre `get_skill_usage()` en cache (TTL 5 min) et le sortir de `get_state()` (points 1).
2. Mutualiser un seul `ps auxww` par cycle au lieu des `pgrep`/`lsof` par extension (point 2).
3. Remplacer les deux `read_text()` de logs par `_read_log_tail()` (point 3).
4. Ancrer les motifs `pkill` lignes 3365 et 2931 (point 4).
5. Ajouter garde `inFlight` + `AbortController` sur les loaders JS (point 5).

**Débloquer le symptôme B** :

6. Remplacer la table codée en dur de `get_running_mcps()` par une détection dérivée de la config (point 6).
7. `try/catch` + bandeau d'erreur sur `loadState()` (point 8).
8. `escAttr()` sur `m.name` (point 9).
9. Décider si l'app doit lire `~/.claude.json` et remonter les MCP de plugins dans l'onglet MCP (point 7) — c'est une évolution de périmètre, pas une correction.

**Sécurité** :

10. Contrôle `Origin`/`Host` + `Content-Type` sur `do_POST` (point 10).
11. Inverser l'échappement de `_notify()` (point 11).
12. Écritures atomiques via `os.replace()` + verrou (point 12).

**Vérification immédiate sur la machine concernée**, avant tout correctif :

```bash
cat ~/.claude/claude-control-watchdog.json          # freeze_detection activé ?
du -sh ~/.claude/projects                            # volume scanné 24×/min
python3 -c "import json;json.load(open('$HOME/Library/Application Support/Claude/claude_desktop_config.json'))"
ls -lh ~/Library/Logs/Claude/main.log                # taille chargée en RAM
```

---

## Limites de cet audit

- Analyse statique uniquement. L'app est macOS-only et n'a pas pu être exécutée ici ; les chemins `osascript`, `pgrep`, `lsof` et LaunchServices n'ont pas été observés à l'exécution.
- Les volumes cités (`~/.claude/projects` en Go, `main.log` en centaines de Mo) sont des ordres de grandeur usuels, pas des mesures sur la machine concernée — d'où les commandes de vérification ci-dessus.
- La constante `HTML` (lignes 4457-7198) a été analysée sur les chemins liés aux deux symptômes, pas exhaustivement.
- Aucune modification de code n'a été apportée : ce document est un diagnostic.
