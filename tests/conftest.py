"""TestClient ходит с Host: testserver, поэтому разрешаем его только в тестах."""

import os
import tempfile

os.environ.setdefault("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
# Тесты не передают skills_path явно: без этого стартовые навыки копировались бы в data/skills проекта.
# Папка существует и пуста, поэтому навыков нет и use_skill не предлагается; навыки проверяются отдельно.
os.environ.setdefault("SKILLS_PATH", tempfile.mkdtemp(prefix="skills-"))
# То же для проектов: тесты, которым важен список проектов, передают projects_path явно.
os.environ.setdefault("PROJECTS_PATH", tempfile.mkdtemp(prefix="projects-"))
