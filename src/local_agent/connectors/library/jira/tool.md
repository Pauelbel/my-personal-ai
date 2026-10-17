---
name: Jira (чтение)
description: Читать Jira: action mine — мои незакрытые задачи; search — поиск по JQL; issue — задача по ключу (PROJ-123) с описанием, связями и комментариями. Только чтение: создавать и менять задачи нельзя
requires_approval: false
network: true
timeout: 40
secrets: JIRA_URL, JIRA_TOKEN, JIRA_EMAIL?, JIRA_VERIFY_SSL?
---
Коннектор к Jira только на чтение. Код — script.py рядом, его можно поправить в окне «код» или попросить агента доработать (propose с name jira).
Доступы задаются формой «настроить» (Возможности → Инструменты → Коннекторы): JIRA_URL и JIRA_TOKEN обязательны; JIRA_EMAIL — только для Jira Cloud; JIRA_VERIFY_SSL=false — для корпоративного сертификата. Знак ? после имени — секрет необязательный.
