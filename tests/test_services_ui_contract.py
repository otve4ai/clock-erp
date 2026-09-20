import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ServicesUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "app" / "templates" / "services.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "app" / "static" / "js" / "services.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "app" / "static" / "css" / "services.css").read_text(encoding="utf-8")

    def test_filters_show_name_value_then_favorites_toggle(self):
        employee = self.template.index('class="services-filter-name">Сотрудники')
        service = self.template.index('class="services-filter-name">Сервисы')
        favorite = self.template.index('id="favoriteToggle"')
        self.assertLess(employee, service)
        self.assertLess(service, favorite)
        self.assertIn('data-employee-value>Все', self.template)
        self.assertIn('data-service-value>Все', self.template)
        self.assertIn('data-favorite-icon aria-hidden="true">☆', self.template)

    def test_archive_action_is_in_overflow_menu(self):
        self.assertIn('class="service-card-overflow"', self.script)
        self.assertIn('aria-haspopup="menu"', self.script)
        self.assertIn('role="menuitem" data-archive=', self.script)
        self.assertNotIn('class="minor" type="button" data-archive=', self.script)

    def test_card_actions_are_compact_and_do_not_wrap(self):
        self.assertIn('.service-action-primary{', self.styles)
        self.assertIn('.service-overflow-toggle{', self.styles)
        self.assertIn('.service-open,.service-card-actions .minor{flex:0 1 auto', self.styles)
        self.assertIn('white-space:nowrap', self.styles)

    def test_icon_editor_has_empty_state_and_replace_remove_actions(self):
        self.assertIn('Иконка сервиса', self.template)
        self.assertIn('data-icon-empty', self.template)
        self.assertIn('data-icon-upload', self.template)
        self.assertIn('data-icon-replace', self.template)
        self.assertIn('data-icon-remove', self.template)
        self.assertIn('name="icon_remove"', self.template)
        self.assertIn('function setIconFormState', self.script)
        self.assertIn('icon_remove', self.script)
        self.assertIn('iconUrl(service)', self.script)
        self.assertIn('icon_blob=NULL,icon_mime=NULL', (ROOT / "app" / "services" / "service_vault.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
