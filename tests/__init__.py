"""v1.14.14 / v1.14.17 - Gardes d'hygiene de la suite.

LIMITE IMPORTANTE : `python3 -m unittest discover -s tests` importe les
modules en TOP-LEVEL (`test_x`, pas `tests.test_x`) et ce fichier ne
s'execute alors PAS. Les gardes ci-dessous ne valent que pour l'invocation
package (`python3 -m unittest tests.test_x`). C'est ce qui a produit la
flake v1.14.17 : le cache de get_state(), jamais desarme sous discover,
servait a un test l'etat du test precedent. La VRAIE hermetacite est donc
dans app.py lui-meme -- les caches sont invalides par cle de chemins
(_state_cache_key, home du cache Desktop) des qu'un test repatche
SKILLS_DIR/HOME. Ne compte jamais sur ce fichier pour l'isolation.

- Pretrust : sous discover, les tests qui exercent _call_claude_cli avec un
  subprocess mocke peuvent ecrire dans le vrai ~/.claude.json du developpeur
  l'entree de confiance du workdir -- la MEME entree, idempotente et sans
  perte, que la production ecrit au premier usage. Benin, mais autant
  l'eviter quand ce fichier s'execute.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

app._CLI_WORKDIR_TRUSTED["done"] = True

# Ceinture pour l'invocation package : TTL a zero. Sous discover, la cle de
# chemins de _state_cache_key assure l'isolation a elle seule.
app._STATE_TTL = 0.0
