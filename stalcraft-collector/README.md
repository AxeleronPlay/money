# STALCRAFT EU - облачный сборщик истории продаж

Работает в паре с основным проектом (stalcraft-deals). Запускается по
расписанию через GitHub Actions - копит историю продаж аукциона
НЕЗАВИСИМО от того, включён ли твой личный компьютер.

Полная инструкция по настройке - в README.md основного проекта
(stalcraft-deals), раздел "Облачный сборщик (работает, пока ты спишь)".

Коротко:
1. Создать **публичный** репозиторий на GitHub, залить сюда всё
   содержимое этой папки.
2. Settings → Secrets and variables → Actions → добавить
   `STALCRAFT_CLIENT_ID` и `STALCRAFT_CLIENT_SECRET`.
3. Вкладка Actions → "Collect STALCRAFT auction history" → Run workflow
   (первый запуск вручную, чтобы проверить).
4. Скопировать ссылку на сырой файл базы:
   `https://raw.githubusercontent.com/ТВОЙ_ЮЗЕРНЕЙМ/ИМЯ_РЕПО/main/stalcraft_history.db`
5. Вставить эту ссылку в `.env` основного проекта как `CLOUD_DB_RAW_URL`.
