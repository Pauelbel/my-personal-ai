// Переключатель темы применяет выбор сразу и запоминает его в браузере.
export function initTheme() {
  const currentTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
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
