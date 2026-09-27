"""TestClient ходит с Host: testserver, поэтому разрешаем его только в тестах."""

import os

os.environ.setdefault("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
