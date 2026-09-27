// Переключатель темы применяет выбор сразу и запоминает его в браузере.
const THEMES = ["light", "dark", "sunset"];

export function initTheme() {
  const saved = document.documentElement.dataset.theme;
  const currentTheme = THEMES.includes(saved) ? saved : "light";
  document.documentElement.dataset.theme = currentTheme;
  for (const option of document.querySelectorAll('input[name="theme"]')) {
    option.checked = option.value === currentTheme;
    option.addEventListener("change", () => {
      if (!option.checked) return;
      document.documentElement.dataset.theme = option.value;
      try { localStorage.setItem("app-theme", option.value); } catch { /* Theme still applies for this page. */ }
    });
  }
}
