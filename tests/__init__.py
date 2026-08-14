"""v1.14.14 - Garde d'hygiene globale de la suite.

_pretrust_cli_workdir() ecrit dans ~/.claude.json (le vrai fichier d'etat du
CLI de la machine) la premiere fois qu'un appel CLI part. Les tests qui
exercent _call_claude_cli / _cli_probe avec un subprocess mocke
declencheraient cette ecriture sur le fichier REEL du developpeur. Le memo
est donc arme ici, avant tout test ; les tests du pretrust lui-meme le
desarment explicitement avec des chemins patches.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

app._CLI_WORKDIR_TRUSTED["done"] = True
