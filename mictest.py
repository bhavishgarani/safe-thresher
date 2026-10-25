
from pathlib import Path

path = Path("Safe_Thresher_Predictive.py")

lines = path.read_text(encoding="utf-8").splitlines()

# Find the first incorrectly nested method.
start = next(
    i for i, line in enumerate(lines)
    if line.startswith("        def connect(self):")
)

# Remove one indentation level from everything after it.
for i in range(start, len(lines)):
    if lines[i].startswith("    "):
        lines[i] = lines[i][4:]

fixed = "\n".join(lines) + "\n"

# Validate before modifying the original.
compile(fixed, str(path), "exec")

# Preserve the original.
backup = path.with_suffix(".backup.py")
backup.write_text(
    path.read_text(encoding="utf-8"),
    encoding="utf-8"
)

path.write_text(fixed, encoding="utf-8")

print("SUCCESS: Indentation fixed.")
print("Backup saved to:", backup)
