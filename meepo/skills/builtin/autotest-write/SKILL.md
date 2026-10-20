---
name: autotest-write
description: Пишет автотест (UI, API, БД, Excel-отчёт) в папку проекта по правилам и примерам репозитория qa_harness. Применять, когда просят написать автотест.
requires: [pytest>=8, allure-pytest>=2.16, pytest-playwright>=0.7, requests>=2.34, pydantic>=2.13, python-dotenv>=1.0]
secrets: []
timeout: 1800
---
# Что делает
Берёт правила и примеры из репозитория qa_harness (https://github.com/Pauelbel/qa_harness) и пишет по ним тест в проект.
- Harness — справочник: скачивается архивом и обновляется раз в сутки; без сети берётся прежняя копия.
- Слой (ui, api, db, dq_reports) — из аргумента layer или по словам задачи.
- Из harness берётся только нужное слою: AGENTS.md, примеры examples/<слой>/, API ядра. Недостающая в проекте обвязка
  (config.py, report.py, clients/, checks/, conftest.py, pytest.ini, .env.example, requirements/) копируется в проект;
  свои файлы проекта не перезаписываются. Папка examples/ harness в проекте — это папка dir (по умолчанию tests/).
- Тест проверяется по правилам (имя test_<слой>_<тема>.py, метки Allure, @step вместо @allure.step, новые файлы) и
  прогоняется pytest --collect-only; не собрался — модель правит его.

UI: сначала открой страницу через browser и сними snapshot, локаторы из него передай в details.
API: в details — эндпоинт, параметры, поля ответа; БД — таблицы и SQL; отчёт — колонки и правила.

# Аргументы (JSON)
- task — что проверяет тест, словами пользователя (обязательно)
- details — локаторы из snapshot, эндпоинт, SQL, колонки — всё, что известно (необязательно, но без этого будут TODO)
- layer — ui, api, db или dq_reports (необязательно: по умолчанию — по словам задачи)
- dir — папка тестов в проекте (по умолчанию tests)
- harness — адрес zip-архива или папка с qa_harness на диске (по умолчанию — репозиторий на GitHub)

# Результат
Новые файлы теста (для UI — и страницы Page Object в <dir>/ui/pages/), список скопированной обвязки, итог сборки,
заметки модели (что заполнить в .env, допущения). Запуск теста — отдельно, через run_command, с согласия пользователя.

# Примеры
{"task": "Вход в систему с верным логином и паролем", "layer": "ui", "details": "Поле логина: page.get_by_label(\"Логин\"); пароль: page.get_by_label(\"Пароль\"); кнопка: page.get_by_role(\"button\", name=\"Войти\")"}
{"task": "GET /api/v1/orders с $top=5 возвращает 5 заказов", "layer": "api"}
