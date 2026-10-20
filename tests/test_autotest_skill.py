"""Скилл autotest-write без сети и модели: локаторы → константы, обвязка из harness, проверки правил, выбор слоя."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "meepo" / "skills" / "sdk"))
spec = importlib.util.spec_from_file_location("autotest_write", ROOT / "meepo/skills/builtin/autotest-write/script.py")
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)

# Маленький qa_harness: ядро, общий conftest, примеры UI и API.
HARNESS = {
    "AGENTS.md": "# Правила\n",
    "pytest.ini": "[pytest]\n",
    ".env.example": "STAGE_APP_FRONTEND_URL=\n",
    "requirements/base.txt": "pytest>=8\n",
    "config.py": "ACTIVE_CONFIG = {}\n",
    "report.py": "def step(title):\n    return lambda func: func\n",
    "conftest.py": "from checks import base\n",
    "checks/base.py": "class Checker:\n    def check_true(self, condition, message):\n        return self\n",
    "clients/browser.py": "class BasePage:\n    def element(self, name, selector):\n        pass\n\n    def open(self, path='/'):\n        pass\n",
    "examples/conftest.py": "import pytest\n",
    "examples/ui/conftest.py": "from examples.ui.pages.login_page import LoginPage\n",
    "examples/ui/pages/login_page.py": "from clients.browser import BasePage\nfrom report import step\n\nclass LoginPage(BasePage):\n    pass\n",
    "examples/ui/test_ui_login.py": "from examples.ui.pages.login_page import LoginPage\n",
    "examples/api/test_api_orders.py": "from checks.base import Checker\n",
}

GOOD_PAGE = '''from clients.browser import BasePage
LINK_ZAKAZY = 'internal:role=link[name="Заказы"i]'  # page.get_by_role("link", name="Заказы")

class OrdersPage(BasePage):
    def open_section(self):
        self.element("Пункт меню «Заказы»", LINK_ZAKAZY).click()
'''
GOOD_TEST = '''import allure
from tests.ui.pages.orders_page import OrdersPage

@allure.epic("Фронтенд")
@allure.feature("Заказы")
class TestOrders:
    @allure.title("Раздел открывается из меню")
    def test_section(self, auth_page):
        orders_page = OrdersPage(auth_page)
        orders_page.open()
        orders_page.open_section()
'''
DETAILS = 'ссылка меню page.get_by_role("link", name="Заказы")'


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "harness"
    for path, text in HARNESS.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("MEEPO_PROJECT", str(project))
    return root


def test_locators_become_playwright_selectors():
    block = skill.locator_block('page.get_by_role("link", name="Заказы"); page.locator("[data-testid=orders-table]"); '
                                'page.get_by_label("Логин"); page.get_by_role("columnheader", name="Сумма").nth(1)')
    assert [(name, selector) for name, selector, _ in block] == [
        ("LINK_ZAKAZY", 'internal:role=link[name="Заказы"i]'),
        ("ELEMENT_ORDERS_TABLE", "[data-testid=orders-table]"),
        ("FIELD_LOGIN", 'internal:label="Логин"i'),
        ("COLUMN_SUMMA", 'internal:role=columnheader[name="Сумма"i] >> nth=1'),
    ]
    assert skill.render_block(block[:1]) == ('LINK_ZAKAZY = \'internal:role=link[name="Заказы"i]\'  '
                                             '# page.get_by_role("link", name="Заказы")')


def test_setup_takes_only_missing_files_and_renames_examples(harness):
    project = Path(skill.ms.project())
    (project / "config.py").write_text("# свой конфиг проекта\n", encoding="utf-8")
    copied = skill.setup(harness, "ui", "tests")
    assert "config.py" not in copied and (project / "config.py").read_text(encoding="utf-8") == "# свой конфиг проекта\n"
    assert {"report.py", "clients/browser.py", "checks/base.py", "tests/ui/conftest.py", "tests/ui/pages/login_page.py"} <= set(copied)
    # Импорты примеров — на папку тестов проекта; сами примеры тестов не копируются.
    assert (project / "tests/ui/conftest.py").read_text(encoding="utf-8").startswith("from tests.ui.pages.login_page")
    assert not (project / "tests/ui/test_ui_login.py").exists()
    assert skill.setup(harness, "ui", "tests") == []


def test_rules_catch_invented_locators_missing_open_and_methods(harness):
    job = skill.Job("tests", "ui", "ui", skill.locator_block(DETAILS), skill.core_methods(harness))
    good = {"tests/ui/pages/orders_page.py": GOOD_PAGE, "tests/ui/test_ui_orders.py": GOOD_TEST}
    assert skill.problems(good, job, set()) == []
    bad = {"tests/ui/pages/orders_page.py": GOOD_PAGE.replace("internal:role=link[name=\"Заказы\"i]", "link: Заказы"),
           "tests/ui/test_ui_orders.py": GOOD_TEST.replace("        orders_page.open()\n", "")}
    found = "\n".join(skill.problems(bad, job, set()))
    assert "нет константы LINK_ZAKAZY" in found and "селектор «link: Заказы» не из блока" in found
    assert "test_section не открывает страницу" in found
    api = GOOD_TEST.replace("orders_page.open()", "checker.check_magic(1)")
    found = "\n".join(skill.problems({"tests/api/test_api_orders.py": api}, skill.Job("tests", "api", "api", [], job.methods), set()))
    assert "метода checker.check_magic нет в harness. Есть: check_true" in found
    assert "имя теста" not in found and "@allure.step" not in found


def test_names_and_allure_rules(harness):
    job = skill.Job("tests", "ui", "ui", [], skill.core_methods(harness))
    found = "\n".join(skill.problems({"tests/ui/test_orders.py": "@allure.step('x')\ndef f():\n    pass\n",
                                      "docs/x.py": "x = 1\n"}, job, set()))
    assert "имя теста — test_ui_<тема>.py" in found and "@allure.step запрещён" in found
    assert "нет @allure.epic" in found and "docs/x.py: файлы — только .py внутри tests/ui/" in found


def test_layer_from_request_without_model(harness):
    assert skill.pick_layer(harness, "раздел «Заказы», в таблице колонки", DETAILS, "") == "ui"
    assert skill.pick_layer(harness, "GET /api/v1/orders возвращает заказы", "", "") == "api"
    assert skill.pick_layer(harness, "что угодно", "", "api") == "api"
    with pytest.raises(ValueError, match="layer — один из: api, ui"):
        skill.pick_layer(harness, "что угодно", "", "db")
    assert skill.harness_root(str(harness))[0] == harness
