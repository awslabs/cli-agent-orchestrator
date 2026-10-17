"""Render aws/host/user-data.sh.tmpl for one model from models.json.

python3 aws/cli/render_user_data.py jevk5-4b > user-data.rendered.sh
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # examples/decision-model-host
USER_DATA_LIMIT = 16 * 1024  # EC2's limit on raw user data


def render(model_key: str) -> str:
    config = json.loads((ROOT / "models.json").read_text())
    model = config["models"][model_key]
    values = {
        "MODEL_REPO": model["repo"],
        "MODEL_FILE": model["file"],
        "MODEL_SHA256": model["sha256"],
        "MODEL_NAME": model["name"],
        "MODEL_TEMP": str(model["temperature"]),
        "LLAMA_TAG": config["llama_tag"],
        "JEVK5_TAG": config["jevk5_tag"],
        "IDLE_MINUTES": str(config["idle_minutes"]),
        "CUDA_ARCH": str(config["cuda_arch"]),
        "SHIM_B64": base64.encodebytes((ROOT / "serve_gguf.py").read_bytes()).decode(),
    }
    text = (ROOT / "aws" / "host" / "user-data.sh.tmpl").read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    left = re.findall(r"@[A-Z0-9_]+@", text)
    if left:
        raise SystemExit(f"unfilled placeholders: {sorted(set(left))}")
    if len(text.encode()) > USER_DATA_LIMIT:
        raise SystemExit(f"user data is {len(text.encode())} bytes, over EC2's 16 KiB limit")
    return text


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.stdout.write(render(sys.argv[1]))
