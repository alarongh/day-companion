"""Copy the same tested application modules into the Python Worker bundle."""

import shutil
from pathlib import Path

cloud = Path(__file__).resolve().parent
source = cloud.parent / "companion"
target = cloud / "src" / "companion"
target.mkdir(parents=True, exist_ok=True)
for path in source.glob("*.py"):
    shutil.copy2(path, target / path.name)
shutil.copy2(cloud / 'src' / 'telegram_api.py', target / 'telegram.py')
print("Shared Python application modules prepared")
