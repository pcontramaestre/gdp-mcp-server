import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dotenv_is_loaded_before_key_store_path_is_read():
    """Importing keystore first must still load .env (it reads its path at import)."""
    code = "import sys, src.keystore; print('src.config' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip().endswith("True")
