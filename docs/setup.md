# Установка

Занимает около часа, из них половина — ожидание, пока macOS спросит разрешения.

Порядок важен: запускатор собирается **до** launchd, потому что разрешения
macOS выдаются именно ему.

---

## Что понадобится

| Что | Зачем | Проверка |
|---|---|---|
| macOS 13+ | launchd, AppleScript, Календарь, Напоминания | `sw_vers` |
| Python 3.9+ | сам бот; сторонних библиотек нет | `/usr/bin/python3 --version` |
| Xcode Command Line Tools | `clang` для запускатора, `git` | `xcode-select -p` |
| Obsidian | хранилище заметок | — |
| iCloud Drive | синхронизация с телефоном (необязательно) | — |
| git | история и резервная копия (необязательно) | `git --version` |

Ставить Command Line Tools, если их нет:

```bash
xcode-select --install
```

---

## Шаг 1. Хранилище

Создайте хранилище Obsidian и в нём папки верхнего уровня:

```bash
VAULT="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/brain"
mkdir -p "$VAULT"/{inbox,daily,projects,areas,resources,archive,assets/excalidraw,private,templates}
```

Путь может быть любым — он задаётся переменной `VAULT`. Папка `inbox/`
обязательна: в неё бот кладёт заметки.

Если хотите, чтобы Claude Code разбирал входящие по правилам, положите в корень
хранилища [templates/CLAUDE.md](../templates/CLAUDE.md) и перепишите первый
абзац под себя.

---

## Шаг 2. Бот в Telegram

1. Напишите [@BotFather](https://t.me/BotFather), команда `/newbot`, придумайте имя.
2. Он вернёт токен вида `1234567890:AA...`. Это секрет: он даёт полный доступ
   к боту. В репозиторий, в заметки и в чаты он не попадает.
3. Узнайте **свой** числовой id — например, у [@userinfobot](https://t.me/userinfobot).

Частая ошибка: вписать в `TG_CHAT_ID` id бота вместо своего. Тогда бот молча
не отвечает: он игнорирует сообщения из чужих чатов, а своим считает не тот.
Признак — id совпадает с числом до двоеточия в токене.

4. Отправьте боту любое сообщение — без этого он не сможет вам отвечать.

---

## Шаг 3. Файлы на место

```bash
mkdir -p ~/bin
cp src/tg-inbox.py src/shrink-images.py ~/bin/
cp scripts/vault-sync.sh scripts/add-event.sh ~/bin/
chmod 755 ~/bin/tg-inbox.py ~/bin/shrink-images.py ~/bin/vault-sync.sh ~/bin/add-event.sh
```

`add-event.sh` ищет `tg-inbox.py` рядом с собой — в репозитории они лежат
в разных папках, а на машине должны оказаться в одной.

Папка `~/bin` и файлы в ней должны принадлежать вам и не быть доступны на
запись группе и остальным, иначе запускатор откажется работать:

```bash
chmod 755 ~/bin
```

---

## Шаг 4. Запускатор

```bash
./runner/build.sh
```

Он соберёт `~/bin/brain-runner` и подпишет его ad-hoc подписью с hardened
runtime. Идентификатор по умолчанию — `com.example.brain-runner`; свой
задаётся переменной `BRAIN_RUNNER_ID`.

**Зачем это вообще нужно.** macOS не пускает процессы, запущенные launchd,
к файлам в iCloud Drive. Разрешение «Полный доступ к диску» выдаётся не
скрипту, а программе, которая его запустила, — и выдавать его `/bin/bash`
или `/usr/bin/python3` нельзя: это откроет доступ всему, что запущено через
них. Запускатор — маленькая программа, которая умеет ровно две вещи:
запустить бота или синхронизацию. Разрешение выдаётся ей.

**Идентификатор подписи менять нельзя.** По нему macOS помнит выданные
разрешения. Пересобрали или сменили идентификатор — выдавайте заново.

---

## Шаг 5. launchd

```bash
cp launchd/com.example.tginbox.plist.example ~/Library/LaunchAgents/com.example.tginbox.plist
cp launchd/com.example.vaultsync.plist.example ~/Library/LaunchAgents/com.example.vaultsync.plist

# подставить своё имя пользователя
sed -i '' "s|/Users/ПОЛЬЗОВАТЕЛЬ|$HOME|g" ~/Library/LaunchAgents/com.example.*.plist

# токен и id чата — вручную, в редакторе
open -e ~/Library/LaunchAgents/com.example.tginbox.plist

# внутри токен: файл закрывается от посторонних
chmod 600 ~/Library/LaunchAgents/com.example.tginbox.plist

plutil -lint ~/Library/LaunchAgents/com.example.*.plist
```

Загрузить:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.tginbox.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.vaultsync.plist
launchctl list | grep example
```

У бота в первой колонке должен быть PID, а не прочерк: это постоянный процесс.
У синхронизации прочерк — норма, она запускается раз в полчаса и завершается.

Правку plist launchd подхватывает только при перезагрузке агента:

```bash
launchctl kickstart -k gui/$(id -u)/com.example.tginbox
```

---

## Шаг 6. Разрешения macOS

При первом запуске система спросит доступ. Все запросы относятся к
`brain-runner` — соглашайтесь именно на них.

| Раздел «Системные настройки → Конфиденциальность и безопасность» | Что добавить |
|---|---|
| Полный доступ к диску | `brain-runner` — иначе не видно iCloud Drive |
| Автоматизация | `brain-runner` → Календарь и Напоминания |

Если запрос не появился или вы случайно нажали «Запретить», добавьте программу
в список вручную: кнопка «+», затем `Cmd+Shift+G` и путь `~/bin`.

Признак того, что разрешения нет, — в логе:

```
ОШИБКА: нет доступа к …/inbox (Operation not permitted) — сообщения не забираю
```

---

## Шаг 7. Календарь и Напоминания

Создайте в приложении «Календарь» календарь с именем из `CAL_NAME`
(по умолчанию `brain`), в «Напоминаниях» — список с именем из `REM_LIST`.
Имена должны совпадать точно, иначе AppleScript ответит ошибкой, а бот
сохранит текст в `inbox/` с пометкой, что событие не создано.

---

## Шаг 8. Проверка

Напишите боту:

| Сообщение | Ожидаемый ответ |
|---|---|
| `/menu` | появляется клавиатура из четырёх кнопок |
| любой текст | `📝 Сохранено в inbox`, за одну-две секунды |
| `/e завтра 15:00 90 Созвон` | `📅 Созвон — …`, событие в календаре |
| `/r пт 19:00 Позвонить` | `⏰ Позвонить — …`, напоминание в списке |
| `/stats` | сколько заметок в inbox |

Лог: `~/Library/Logs/tg-inbox.log`. В спокойном состоянии он молчит — пустые
опросы туда не пишутся.

---

## Шаг 9. Git (необязательно)

Хранилище можно держать в git: история правок и копия на случай, если iCloud
что-нибудь съест.

```bash
cd "$VAULT"
git init
git config core.quotepath false        # кириллица в именах файлов
git config core.precomposeunicode true # macOS нормализует юникод по-своему
```

Репозиторий удобно вынести за пределы хранилища, чтобы Obsidian и iCloud
не видели служебных файлов:

```bash
git init --separate-git-dir ~/git/brain.git
```

Если заводите приватный репозиторий на GitHub — отдельный SSH-ключ и алиас
в `~/.ssh/config`, чтобы не смешивать с рабочим:

```
Host github-notes
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_notes
    IdentitiesOnly yes
```

Папку `private/` можно шифровать через
[git-crypt](https://github.com/AGWA/git-crypt), тогда её содержимое уезжает
на сервер в зашифрованном виде. Ключ (`.git/git-crypt/keys/default`) хранится
в менеджере паролей: потеряете — данные не восстановить.

Синхронизация раз в полчаса — это `vault-sync.sh` из шага 5. Он не делает
ничего опасного: пропускает запуск, если в репозитории незаконченный rebase,
если git занят или если сервер недоступен.

---

## Диагностика

| Симптом | Причина |
|---|---|
| в `launchctl list` прочерк вместо PID у бота | процесс не запустился, смотрите лог |
| «нет доступа», «Operation not permitted» | нет разрешения у `brain-runner` (шаг 6) |
| бот молчит, в логе пусто | не тот `TG_CHAT_ID` — сравните со своим id |
| «getUpdates: Unauthorized» | неверный токен |
| «getUpdates: Conflict» | бот с тем же токеном опрашивается ещё откуда-то, или у него включён webhook |
| «событие не создано» | нет календаря или списка с таким именем, либо не выдана «Автоматизация» |
| «нет связи» | бот сам повторит: паузы 1, 2, 4… 60 секунд |

Перезапустить бота:

```bash
launchctl kickstart -k gui/$(id -u)/com.example.tginbox
```

Остановить совсем:

```bash
launchctl bootout gui/$(id -u)/com.example.tginbox
```
