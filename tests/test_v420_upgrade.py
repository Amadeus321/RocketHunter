"""Load split, compressed v4.20.0 regression tests for unittest discovery."""
import base64
import gzip
from pathlib import Path

_root = Path(__file__).resolve().parents[1] / "source"
_parts = sorted(_root.glob("test-v420-upgrade-part-*.b64"))
if len(_parts) != 2:
    raise RuntimeError(f"expected 2 v4.20 test parts, found {len(_parts)}")
_payload = b"".join(base64.b64decode(p.read_text(encoding="ascii")) for p in _parts)
_code = gzip.decompress(_payload).decode("utf-8")
exec(compile(_code, __file__, "exec"), globals(), globals())
