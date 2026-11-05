import secrets
from pathlib import Path

root = Path(__file__).resolve().parent
env_file = root / ".env"
example_file = root / ".env.example"

if env_file.exists():
    raise SystemExit(0)

lines = []
for line in example_file.read_text(encoding="utf-8").splitlines():
    if line.startswith("SECRET_KEY="):
        line = f"SECRET_KEY={secrets.token_hex(32)}"
    lines.append(line)

env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("Created .env with a new random SECRET_KEY.")
