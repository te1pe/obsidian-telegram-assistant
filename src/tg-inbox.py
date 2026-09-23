#!/usr/bin/python3
"""Telegram-бот захвата для vault «brain».

Постоянный процесс под launchd (KeepAlive): висит на getUpdates с long polling,
получает сообщение через секунду после отправки и обрабатывает его на месте.

Окружение: TG_TOKEN, TG_CHAT_ID, VAULT, CAL_NAME, REM_LIST.

- любой текст → VAULT/inbox/YYYY-MM-DD-HHMMSS.md (created, source: telegram, status: raw);
- кнопки «Напоминание» и «Событие» → бот ждёт следующим сообщением дату, время и текст;
- /e <дата> <время> [минут] <название> → событие в календаре CAL_NAME;
- /e <повтор> <время> [минут] <название> → повторяющееся событие, но сначала спрашивает;
- /r <дата> <время> <текст> → напоминание в списке REM_LIST;
- /menu, /cancel, /help, /stats — клавиатура, отмена, справка, состояние.

Разобранная дата оказалась в прошлом — бот не молчит и не отказывает, а
переспрашивает: задним числом или ты имел в виду другую дату. Явно указанный
год («15.09.2025») считается недвусмысленным и вопроса не вызывает.

Обрабатываются только сообщения из чата TG_CHAT_ID, на каждое бот отвечает
разобранными данными. Имя календаря, списка напоминаний и файла в ответ не идут:
в подтверждении остаётся только то, что бот разобрал из сообщения. Не удалось
создать событие или напоминание — ответ с текстом ошибки и именем файла,
а сообщение сохраняется в inbox: заметка важнее ответа.
Текст передаётся в AppleScript аргументами и не становится частью кода скрипта.
Токен в лог не попадает.

SIGTERM: дорабатывается текущее сообщение, затем выход с кодом 0.

Тот же файл обслуживает ~/bin/add-event.sh:
    tg-inbox.py add-event "Название" "YYYY-MM-DD HH:MM" [минут] [календарь]
создаёт событие тем же AppleScript, что и команда /e.
"""
import calendar as calendar_module
import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

API_URL = 'https://api.telegram.org/bot%s/%s'
OFFSET_FILE = os.path.expanduser('~/.tg-inbox-offset')
STATE_FILE = os.path.expanduser('~/.tg-inbox-state.json')
LOCK_FILE = os.path.expanduser('~/.tg-inbox.lock')
OFFSET_MAX_AGE = 6 * 24 * 3600  # после недели простоя Telegram меняет нумерацию update_id
POLL_TIMEOUT = 50               # сколько Telegram держит getUpdates открытым
HTTP_TIMEOUT = 60               # больше POLL_TIMEOUT, иначе клиент оборвёт связь раньше сервера
BACKOFF_START = 1
BACKOFF_MAX = 60
ERROR_LOG_INTERVAL = 600        # одна и та же ошибка связи пишется в лог не чаще
FATAL_PAUSE = 60                # пауза перед выходом: с KeepAlive иначе выйдет перезапуск по кругу
STATE_TTL = 600                 # состояние живёт 10 минут
COMMANDS_MAX_TRIES = 3
DEFAULT_VAULT = '~/Library/Mobile Documents/iCloud~md~obsidian/Documents/brain'
DEFAULT_CALENDAR = 'brain'
DEFAULT_REMINDER_LIST = 'brain'
DEFAULT_DURATION = 60
SERIES_MONTHS = 3               # по умолчанию повторы живут три месяца
TOKEN_PLACEHOLDER = 'ВСТАВЬ_ТОКЕН'

RELATIVE_DAYS = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2}
WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
RRULE_DAYS = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU']  # тот же порядок, что WEEKDAYS
MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
ATTACHMENTS = ('photo', 'document', 'voice', 'audio', 'video', 'video_note',
               'sticker', 'animation', 'location', 'contact', 'poll')

# Слова, задающие повтор. Ключ — сколько слов занимает, значение — частота и дни.
RECURRENCE_WORDS = {
    ('каждый', 'день'): ('DAILY', None),
    ('каждую', 'неделю'): ('WEEKLY', None),
    ('каждый', 'месяц'): ('MONTHLY', None),
    ('будни',): ('WEEKLY', ['MO', 'TU', 'WE', 'TH', 'FR']),
    ('выходные',): ('WEEKLY', ['SA', 'SU']),
}

START_TIME = datetime.datetime.now()

# Дата собирается из аргументов 4–8. Сначала day = 1: если сегодня 31-е, смена
# месяца на короткий иначе переполнит дату в следующий месяц.
BUILD_DATE = '''set day of theDate to 1
set year of theDate to (item 4 of argv) as integer
set month of theDate to (item 5 of argv) as integer
set day of theDate to (item 6 of argv) as integer
set time of theDate to ((item 7 of argv) as integer) * hours + ((item 8 of argv) as integer) * minutes'''

# Аргумент 10 — правило повтора (RRULE) или пустая строка. Календарь принимает
# строку как есть и возвращает её нормализованной.
EVENT_SCRIPT = '''on run argv
set calName to item 1 of argv
set theTitle to item 2 of argv
set theNotes to item 3 of argv
set theRule to item 10 of argv
set theDate to current date
%s
set endDate to theDate + ((item 9 of argv) as integer) * minutes
tell application "Calendar"
tell calendar calName
set theEvent to make new event with properties {summary:theTitle, start date:theDate, end date:endDate, description:theNotes}
if theRule is not "" then
set recurrence of theEvent to theRule
end if
end tell
end tell
end run''' % BUILD_DATE

REMINDER_SCRIPT = '''on run argv
set listName to item 1 of argv
set theTitle to item 2 of argv
set theNotes to item 3 of argv
set theDate to current date
%s
tell application "Reminders"
tell list listName
make new reminder with properties {name:theTitle, body:theNotes, due date:theDate, remind me date:theDate}
end tell
end tell
end run''' % BUILD_DATE


class Log:
    """Строка лога с временем; секреты заменяются на ***."""

    def __init__(self):
        self.secrets = []

    def hide(self, secret):
        if secret:
            self.secrets.append(secret)

    def __call__(self, msg):
        for secret in self.secrets:
            msg = msg.replace(secret, '***')
        print('%s %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)


log = Log()


class ParseError(Exception):
    pass


class ApiError(Exception):
    """fatal — повторять бессмысленно: неверный токен, чужой getUpdates."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class NoteError(Exception):
    pass


class Stop(Exception):
    """Пришёл SIGTERM в момент ожидания — выходим штатно."""


# Сигнал приходит между байткодами, а процесс почти всегда спит внутри getUpdates.
# Флага там мало: по PEP 475 сокет просто продолжит ждать до таймаута, и launchd
# успеет прислать SIGKILL. Поэтому в простое обработчик рвёт ожидание исключением,
# а во время обработки сообщения только ставит флаг — сообщение дорабатывается.
_stopping = False
_busy = False


def on_stop_signal(signum, frame):
    global _stopping
    _stopping = True
    if not _busy:
        raise Stop()


def install_signal_handlers():
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_stop_signal)


def fatal(message):
    """Выход с паузой: без неё launchd перезапускал бы процесс каждые ThrottleInterval."""
    log(message)
    if not sys.stdout.isatty():
        try:
            time.sleep(FATAL_PAUSE)
        except Stop:
            pass
    return 1


def plural(n, one, few, many):
    tail = abs(n) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def add_months(when, count):
    """Тот же день через count месяцев; 31-е в коротком месяце — последний день."""
    index = when.month - 1 + count
    year = when.year + index // 12
    month = index % 12 + 1
    day = min(when.day, calendar_module.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


# --- кнопки и клавиатуры ----------------------------------------------------

BTN_REMINDER = '⏰ Напоминание'
BTN_EVENT = '📅 Событие'
BTN_MONEY = '💰 Деньги'
BTN_COOK = '🍳 Cook'
BTN_CANCEL = '✖️ Отмена'
# Кнопки-ответы: живут только внутри вопроса, в общей клавиатуре их нет.
BTN_BACKDATE = '⏪ Задним числом'
BTN_CREATE = '✅ Создать'
BTN_FOREVER = '♾️ Без конца'

MAIN_KEYBOARD = {'keyboard': [[{'text': BTN_REMINDER}, {'text': BTN_EVENT}],
                              [{'text': BTN_MONEY}, {'text': BTN_COOK}]],
                 'resize_keyboard': True, 'is_persistent': True}
CANCEL_KEYBOARD = {'keyboard': [[{'text': BTN_CANCEL}]],
                   'resize_keyboard': True, 'is_persistent': True}

COMMANDS = [
    ('menu', 'показать клавиатуру'),
    ('cancel', 'отменить текущее действие'),
    ('help', 'справка по форматам'),
    ('stats', 'сколько в inbox, когда синхронизация'),
    ('e', 'событие: /e завтра 15:00 90 Созвон'),
    ('r', 'напоминание: /r пт 19:00 Позвонить маме'),
]


def answer_keyboard(labels):
    """Клавиатура вопроса: ответы по два в ряд, отмена отдельной строкой."""
    rows, row = [], []
    for label in labels:
        row.append({'text': label})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{'text': BTN_CANCEL}])
    return {'keyboard': rows, 'resize_keyboard': True, 'is_persistent': True}


def help_text():
    return '\n'.join([
        'Любой текст — заметка в inbox.',
        '⏰ Напоминание и 📅 Событие — кнопкой, дальше одним сообщением.',
        '/e <дата> <время> [минут] <название> — событие',
        '/r <дата> <время> <текст> — напоминание',
        'Дата: сегодня, завтра, послезавтра, пн…вс, ДД.ММ, ДД.ММ.ГГГГ, ГГГГ-ММ-ДД',
        'Время: ЧЧ:ММ, ЧЧ.ММ или ЧЧ. Без длительности — %d минут.' % DEFAULT_DURATION,
        'Повтор вместо даты: пн,ср,пт | будни | выходные | каждый день |',
        '  каждую неделю [пн] | каждый месяц. Пример: /e будни 09:00 15 Планирование',
        'Со второй строки — описание.',
        '/menu — клавиатура, /cancel — отмена, /stats — что в inbox',
    ])


# --- разбор -----------------------------------------------------------------

def parse_time(token):
    m = re.fullmatch(r'(\d{1,2})(?:[:.](\d{2}))?', token)
    if not m:
        raise ParseError('не понял время «%s»: нужно ЧЧ:ММ, ЧЧ.ММ или ЧЧ' % token)
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise ParseError('нет такого времени: %s' % token)
    return datetime.time(hour, minute)


def explicit_year(date_token):
    """Год указан явно — намерение недвусмысленное, переспрашивать не о чем."""
    token = date_token.strip().lower()
    return bool(re.fullmatch(r'\d{1,2}\.\d{1,2}\.\d{4}', token)
                or re.fullmatch(r'\d{4}-\d{2}-\d{2}', token))


def parse_when(date_token, time_token, sent):
    """Дата и время считаются от момента отправки сообщения, а не обработки.

    ДД.ММ в прошлом остаётся в прошлом: раньше он молча переезжал на следующий
    год, теперь про него спрашивают (раздел F)."""
    at = parse_time(time_token)
    today = sent.date()
    token = date_token.lower()
    try:
        if token in RELATIVE_DAYS:
            day = today + datetime.timedelta(days=RELATIVE_DAYS[token])
        elif token in WEEKDAYS:
            day = today + datetime.timedelta(days=(WEEKDAYS.index(token) - today.weekday()) % 7)
            if datetime.datetime.combine(day, at) <= sent:
                day += datetime.timedelta(days=7)
        elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', token):
            year, month, dom = map(int, token.split('-'))
            day = datetime.date(year, month, dom)
        elif re.fullmatch(r'\d{1,2}\.\d{1,2}\.\d{4}', token):
            dom, month, year = map(int, token.split('.'))
            day = datetime.date(year, month, dom)
        elif re.fullmatch(r'\d{1,2}\.\d{1,2}', token):
            dom, month = map(int, token.split('.'))
            day = datetime.date(today.year, month, dom)
        else:
            raise ParseError('не понял дату «%s»' % date_token)
    except ValueError:
        raise ParseError('нет такой даты: %s' % date_token)
    return datetime.datetime.combine(day, at)


class Rule:
    """Правило повтора: частота, дни недели, дата окончания."""

    def __init__(self, freq, days=None, until=None):
        self.freq, self.days, self.until = freq, days, until

    def text(self):
        parts = ['FREQ=' + self.freq]
        if self.days:
            parts.append('BYDAY=' + ','.join(self.days))
        if self.until:
            # UNTIL по стандарту — в UTC; наивное время считается местным.
            parts.append('UNTIL=' + self.until.astimezone(datetime.timezone.utc)
                         .strftime('%Y%m%dT%H%M%SZ'))
        return ';'.join(parts)

    def human(self):
        if self.days:
            return ', '.join(WEEKDAYS[RRULE_DAYS.index(d)] for d in self.days)
        return {'DAILY': 'каждый день', 'WEEKLY': 'каждую неделю',
                'MONTHLY': 'каждый месяц'}[self.freq]

    def to_data(self):
        return {'freq': self.freq, 'days': self.days,
                'until': self.until.isoformat() if self.until else None}

    @staticmethod
    def from_data(data):
        if not data:
            return None
        until = data.get('until')
        return Rule(data['freq'], data.get('days'),
                    datetime.datetime.fromisoformat(until) if until else None)


def parse_recurrence(args):
    """'будни 09:00 15 Планирование' → (Rule, '09:00 15 Планирование'); не повтор → (None, args)."""
    words, _ = split_words(args, 3)
    if not words:
        return None, args
    for size in (2, 1):
        key = tuple(w.lower() for w in words[:size])
        if key in RECURRENCE_WORDS:
            freq, days = RECURRENCE_WORDS[key]
            used = size
            if freq == 'WEEKLY' and not days and len(words) > size and words[size].lower() in WEEKDAYS:
                days = [RRULE_DAYS[WEEKDAYS.index(words[size].lower())]]
                used += 1
            _, rest = split_words(args, used)
            return Rule(freq, days), rest
    # «пн,ср,пт» — только через запятую: одиночное «пн» остаётся разовой датой
    parts = [p for p in words[0].lower().split(',') if p]
    if len(parts) > 1 and all(p in WEEKDAYS for p in parts):
        days = [RRULE_DAYS[WEEKDAYS.index(p)] for p in parts]
        _, rest = split_words(args, 1)
        return Rule('WEEKLY', sorted(set(days), key=RRULE_DAYS.index)), rest
    return None, args


def series_start(rule, at, sent):
    """Первое срабатывание серии — ближайшее подходящее время после отправки."""
    if rule.days:
        for shift in range(8):
            day = sent.date() + datetime.timedelta(days=shift)
            candidate = datetime.datetime.combine(day, at)
            if RRULE_DAYS[candidate.weekday()] in rule.days and candidate > sent:
                return candidate
    candidate = datetime.datetime.combine(sent.date(), at)
    return candidate if candidate > sent else candidate + datetime.timedelta(days=1)


def occurrences(rule, start, count):
    """Ближайшие срабатывания — их бот показывает перед созданием серии."""
    if rule.days:
        out, day = [], start
        while len(out) < count:
            if RRULE_DAYS[day.weekday()] in rule.days:
                out.append(day)
            day += datetime.timedelta(days=1)
        return out
    if rule.freq == 'DAILY':
        return [start + datetime.timedelta(days=i) for i in range(count)]
    if rule.freq == 'WEEKLY':
        return [start + datetime.timedelta(days=7 * i) for i in range(count)]
    return [add_months(start, i) for i in range(count)]


def split_words(text, count):
    """Отделить count первых слов; остаток вернуть с исходными пробелами внутри."""
    words = []
    for _ in range(count):
        m = re.match(r'\s*(\S+)', text)
        if not m:
            break
        words.append(m.group(1))
        text = text[m.end():]
    return words, text.strip()


def split_command(text):
    """'/e@bot аргументы\\nописание' → ('e', 'аргументы', 'описание'); не команда → None."""
    first, _, rest = text.partition('\n')
    m = re.fullmatch(r'/([A-Za-z]+)(?:@\w+)?(?:\s+(.*))?', first.strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or '').strip(), rest.strip()


def split_lines(text):
    """'аргументы\\nописание' → ('аргументы', 'описание'). Ввод после нажатия кнопки."""
    first, _, rest = text.partition('\n')
    return first.strip(), rest.strip()


class Plan:
    """Что и когда создать. Умеет лечь в файл состояния и подняться обратно."""

    def __init__(self, kind, when, duration, title, notes='', raw='', sent=None,
                 exact_year=False, rule=None):
        self.kind = kind
        self.when = when
        self.duration = duration
        self.title = title
        self.notes = notes
        self.raw = raw
        self.sent = sent or when
        self.exact_year = exact_year
        self.rule = rule

    @property
    def what(self):
        return 'событие' if self.kind == 'e' else 'напоминание'

    def to_data(self):
        return {'kind': self.kind, 'when': self.when.isoformat(), 'duration': self.duration,
                'title': self.title, 'notes': self.notes, 'raw': self.raw,
                'sent': self.sent.isoformat(), 'exact_year': self.exact_year,
                'rule': self.rule.to_data() if self.rule else None}

    @staticmethod
    def from_data(data):
        return Plan(data['kind'], datetime.datetime.fromisoformat(data['when']),
                    data['duration'], data['title'], data.get('notes', ''),
                    data.get('raw', ''), datetime.datetime.fromisoformat(data['sent']),
                    data.get('exact_year', False), Rule.from_data(data.get('rule')))


def parse_event(args, sent):
    rule, rest = parse_recurrence(args)
    words, tail = split_words(rest, 1)
    if not words:
        raise ParseError('нужно: <дата> <время> [минут] <название>')
    if rule:
        at = parse_time(words[0])
        when = series_start(rule, at, sent)
        exact = True  # начало серии всегда в будущем, спрашивать не о чем
        rest = tail
    else:
        words, rest = split_words(args, 2)
        if len(words) < 2:
            raise ParseError('нужно: <дата> <время> [минут] <название>')
        when = parse_when(words[0], words[1], sent)
        exact = explicit_year(words[0])
    duration = DEFAULT_DURATION
    extra, remainder = split_words(rest, 1)
    if extra and remainder and re.fullmatch(r'\d{1,4}', extra[0]) and 5 <= int(extra[0]) <= 1440:
        duration, rest = int(extra[0]), remainder
    if not rest:
        raise ParseError('нет названия события')
    return Plan('e', when, duration, rest, sent=sent, exact_year=exact, rule=rule)


def parse_reminder(args, sent):
    rule, _ = parse_recurrence(args)
    if rule:
        raise ParseError('повторы пока умею только для событий')
    words, rest = split_words(args, 2)
    if len(words) < 2:
        raise ParseError('нужно: <дата> <время> <текст>')
    when = parse_when(words[0], words[1], sent)
    if not rest:
        raise ParseError('нет текста напоминания')
    return Plan('r', when, 0, rest, sent=sent, exact_year=explicit_year(words[0]))


# --- формат подтверждений ---------------------------------------------------

def format_date(when, now):
    """«20 сентября»; год — только если не текущий."""
    date = '%d %s' % (when.day, MONTHS[when.month - 1])
    return date if when.year == now.year else '%s %d' % (date, when.year)


def format_when(when, now):
    """«пн, 20 сентября, 15:00»; год — только если не текущий."""
    return '%s, %s, %s' % (WEEKDAYS[when.weekday()], format_date(when, now),
                           when.strftime('%H:%M'))


def format_duration(minutes):
    """60 → «1 ч», 90 → «1 ч 30 мин», 45 → «45 мин»."""
    hours, rest = divmod(int(minutes), 60)
    parts = []
    if hours:
        parts.append('%d ч' % hours)
    if rest or not hours:
        parts.append('%d мин' % rest)
    return ' '.join(parts)


def format_ago(delta):
    seconds = int(delta.total_seconds())
    days = seconds // 86400
    if days:
        return '%d %s назад' % (days, plural(days, 'день', 'дня', 'дней'))
    hours = seconds // 3600
    if hours:
        return '%d %s назад' % (hours, plural(hours, 'час', 'часа', 'часов'))
    minutes = max(1, seconds // 60)
    return '%d %s назад' % (minutes, plural(minutes, 'минуту', 'минуты', 'минут'))


def schedule_argv(target, title, notes, when, duration, rule=''):
    """Аргументы EVENT_SCRIPT и REMINDER_SCRIPT — один формат для бота и add-event.sh."""
    return [target, title, notes] + [str(v) for v in (when.year, when.month, when.day,
                                                       when.hour, when.minute, duration)] + [rule]


# --- заметки ----------------------------------------------------------------

def save_note(inbox, sent, text, callout=''):
    """Новая заметка в inbox; существующие файлы не перезаписываются."""
    body = '---\ncreated: %s\nsource: telegram\nstatus: raw\n---\n%s\n' % (
        sent.strftime('%Y-%m-%d %H:%M'), text.rstrip())
    if callout:
        body += '\n%s\n' % callout
    stem = sent.strftime('%Y-%m-%d-%H%M%S')
    try:
        for n in range(1, 100):
            name = '%s.md' % stem if n == 1 else '%s-%d.md' % (stem, n)
            try:
                fd = os.open(os.path.join(inbox, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            return name
    except OSError as e:
        raise NoteError('%s: %s' % (type(e).__name__, e))
    raise NoteError('нет свободного имени для %s' % stem)


def osascript_command(script, argv):
    cmd = ['/usr/bin/osascript']
    for line in script.splitlines():
        if line.strip():
            cmd += ['-e', line]
    return cmd + list(argv)


def run_osascript(script, argv):
    """None — успех, иначе текст ошибки."""
    try:
        res = subprocess.run(osascript_command(script, argv), capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 'osascript не ответил за 60 секунд — возможно, ждёт разрешения на автоматизацию'
    except OSError as e:
        return 'osascript не запустился: %s' % e
    if res.returncode != 0:
        return (res.stderr.strip() or 'osascript завершился с кодом %d' % res.returncode)[:300]
    return None


def api_call(token, method, payload, timeout):
    request = urllib.request.Request(API_URL % (token, method),
                                     data=json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode('utf-8'))
        except ValueError:
            raise ApiError('%s: HTTP %d' % (method, e.code), fatal=e.code in (401, 404, 409))
    except urllib.error.URLError as e:
        raise ApiError('%s: нет связи (%s)' % (method, e.reason))
    except (OSError, ValueError) as e:
        raise ApiError('%s: %s' % (method, type(e).__name__))
    if not data.get('ok'):
        code = data.get('error_code')
        raise ApiError('%s: %s' % (method, data.get('description', 'ok=false')),
                       fatal=code in (401, 404, 409))
    return data['result']


# --- состояние --------------------------------------------------------------

IDLE = 'IDLE'


class State:
    """Одно состояние машины: подсказка при входе, обработчик текста, клавиатура."""

    def __init__(self, prompt, handler, keyboard):
        self.prompt, self.handler, self.keyboard = prompt, handler, keyboard


def read_state():
    """(имя, момент входа, данные); файла нет, он битый или состояние незнакомое → IDLE."""
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return IDLE, 0.0, {}
    except (ValueError, OSError) as e:
        log('состояние не прочитано (%s) — считаю IDLE' % type(e).__name__)
        return IDLE, 0.0, {}
    if not isinstance(raw, dict) or raw.get('state') not in STATES:
        return IDLE, 0.0, {}
    try:
        since = float(raw.get('since') or 0)
    except (TypeError, ValueError):
        since = 0.0
    data = raw.get('data')
    return raw['state'], since, data if isinstance(data, dict) else {}


def write_state(name, since, data):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'v': 1, 'state': name, 'since': since, 'data': data}, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# --- бот --------------------------------------------------------------------

class Bot:
    def __init__(self, token, chat_id, inbox, cal, rem, vault=''):
        self.token, self.chat_id, self.inbox = token, chat_id, inbox
        self.cal, self.rem, self.vault = cal, rem, vault
        self.state, self.state_since, self.data = read_state()

    def api(self, method, payload, timeout=30):
        return api_call(self.token, method, payload, timeout)

    def reply(self, message_id, text, keyboard=None):
        """Ответ не важнее заметки: ошибка отправки только пишется в лог."""
        payload = {'chat_id': self.chat_id, 'text': text,
                   'reply_parameters': {'message_id': message_id,
                                        'allow_sending_without_reply': True}}
        if keyboard:
            payload['reply_markup'] = keyboard
        try:
            self.api('sendMessage', payload)
            log('sendMessage: ok')
        except ApiError as e:
            log('sendMessage: ошибка — %s' % e)

    def register_commands(self):
        try:
            self.api('setMyCommands',
                     {'commands': [{'command': c, 'description': d} for c, d in COMMANDS]})
            log('setMyCommands: ok')
            return True
        except ApiError as e:
            log('setMyCommands: ошибка — %s' % e)
            return False

    def osascript(self, script, argv):
        return run_osascript(script, argv)

    def now(self):
        return datetime.datetime.now()

    def set_state(self, name, data=None):
        data = data or {}
        if name == IDLE and self.state == IDLE and not data and not self.data:
            return  # на диске уже IDLE — файл не трогаем
        self.state, self.state_since, self.data = name, time.time(), data
        try:
            write_state(name, self.state_since, data)
        except OSError as e:
            log('состояние не записано (%s) — работаю без него' % e)

    def expire_state(self):
        """Состояние старше 10 минут: сброс, чтобы вечерняя мысль не ушла в парсер дат."""
        if self.state != IDLE and time.time() - self.state_since > STATE_TTL:
            log('состояние %s протухло — сбрасываю' % self.state)
            self.set_state(IDLE)
            return True
        return False


# --- создание события и напоминания -----------------------------------------

def create_scheduled(plan, message_id, bot):
    """Собственно создание: один путь для команды, кнопки и ответа на вопрос."""
    target = bot.cal if plan.kind == 'e' else bot.rem
    script = EVENT_SCRIPT if plan.kind == 'e' else REMINDER_SCRIPT
    rule_text = plan.rule.text() if plan.rule else ''
    error = bot.osascript(script, schedule_argv(target, plan.title, plan.notes, plan.when,
                                                plan.duration, rule_text))
    if error:
        return schedule_failed(plan, error, True, message_id, bot)

    now = bot.now()
    if plan.rule:
        answer = '🔁 %s — %s в %s (%s)' % (plan.title, plan.rule.human(),
                                           plan.when.strftime('%H:%M'),
                                           format_duration(plan.duration))
        answer += '\nПервое: %s' % format_when(plan.when, now)
        if plan.rule.until:
            answer += '\nДо %s' % format_date(plan.rule.until, now)
        else:
            answer += '\nБез даты окончания'
    elif plan.kind == 'e':
        answer = '📅 %s — %s (%s)' % (plan.title, format_when(plan.when, plan.sent),
                                      format_duration(plan.duration))
    else:
        answer = '⏰ %s — %s' % (plan.title, format_when(plan.when, plan.sent))
    if plan.when < now and not plan.rule:
        answer += '\n⏪ Задним числом'
    bot.reply(message_id, answer, MAIN_KEYBOARD)
    return '%s%s на %s' % (plan.what, ' (серия)' if plan.rule else '',
                           plan.when.strftime('%Y-%m-%d %H:%M'))


def schedule_failed(plan, reason, has_text, message_id, bot):
    """Не создалось событие или напоминание: в ответе подробности и имя файла."""
    what = plan.what
    if not has_text:
        bot.reply(message_id, '⚠️ %s не создано: %s' % (what.capitalize(), reason), MAIN_KEYBOARD)
        return '%s не создано: пустая команда' % what
    name = save_note(bot.inbox, plan.sent, plan.raw,
                     '> [!warning] Бот: %s не создано — %s' % (what, reason))
    bot.reply(message_id, '⚠️ %s не создано: %s\nТекст сохранил в inbox: %s' % (
        what.capitalize(), reason, name), MAIN_KEYBOARD)
    return '%s не создано (%s), текст в заметке %s' % (what, reason, name)


def ask_past(plan, message_id, bot):
    """Раздел F: дата в прошлом — не отказывать молча, а переспросить."""
    now = bot.now()
    choices = []
    if plan.when.date() == now.date():
        choices.append(plan.when + datetime.timedelta(days=1))
    else:
        for step in range(1, 13):
            candidate = add_months(plan.when, step)
            if candidate > now:
                choices.append(candidate)
                break
        year_later = add_months(plan.when, 12)
        if year_later not in choices:
            choices.append(year_later)
    labels = {format_date(c, now): c.isoformat() for c in choices}
    data = plan.to_data()
    data['choices'] = labels
    bot.set_state('WAIT_PAST_CHOICE', data)
    bot.reply(message_id, '⚠️ %s — это уже прошло (%s).\nСоздать задним числом или '
                          'ты имел в виду другую дату?' % (format_when(plan.when, now),
                                                           format_ago(now - plan.when)),
              answer_keyboard([BTN_BACKDATE] + list(labels)))
    return '%s в прошлом — спрашиваю' % plan.what


def ask_confirm(plan, message_id, bot):
    """Раздел E: серия без подтверждения засоряет календарь надолго."""
    now = bot.now()
    plan.rule.until = add_months(plan.when, SERIES_MONTHS)
    nearest = ', '.join('%s %s' % (WEEKDAYS[d.weekday()], format_date(d, now))
                        for d in occurrences(plan.rule, plan.when, 3))
    bot.set_state('WAIT_EVENT_CONFIRM', plan.to_data())
    bot.reply(message_id,
              '🔁 %s — %s в %s (%s)\nБлижайшие: %s\nСерия до %s. Создавать?' % (
                  plan.title, plan.rule.human(), plan.when.strftime('%H:%M'),
                  format_duration(plan.duration), nearest,
                  format_date(plan.rule.until, now)),
              answer_keyboard([BTN_CREATE, BTN_FOREVER]))
    return 'серия %s — жду подтверждения' % plan.rule.text()


# --- обработчики состояний и кнопок ----------------------------------------

def handle_event_text(text, sent, message_id, bot, data):
    args, notes = split_lines(text)
    return handle_schedule('e', args, notes, text, sent, message_id, bot)


def handle_reminder_text(text, sent, message_id, bot, data):
    args, notes = split_lines(text)
    return handle_schedule('r', args, notes, text, sent, message_id, bot)


def handle_not_ready(text, sent, message_id, bot, data):
    """Заглушка для объявленных, но не включённых состояний: текст всё равно сохраняем."""
    name = save_note(bot.inbox, sent, text)
    bot.reply(message_id, 'Пока не умею — сохранил как заметку в inbox.', MAIN_KEYBOARD)
    return 'заглушка состояния, заметка ' + name


def handle_past_choice(text, sent, message_id, bot, data):
    """Ответ на вопрос о прошедшей дате."""
    plan = Plan.from_data(data)
    answer = text.strip()
    if answer == BTN_BACKDATE:
        return create_scheduled(plan, message_id, bot)
    chosen = (data.get('choices') or {}).get(answer)
    if chosen:
        plan.when = datetime.datetime.fromisoformat(chosen)
        return create_scheduled(plan, message_id, bot)
    name = save_note(bot.inbox, sent, text)
    bot.reply(message_id, '📝 Сохранено в inbox\nВопрос про дату снял, %s не создал.'
              % plan.what, MAIN_KEYBOARD)
    return 'вопрос о прошедшей дате отменён, заметка ' + name


def handle_event_confirm(text, sent, message_id, bot, data):
    """Ответ на подтверждение серии."""
    plan = Plan.from_data(data)
    answer = text.strip()
    if answer == BTN_CREATE:
        return create_scheduled(plan, message_id, bot)
    if answer == BTN_FOREVER:
        plan.rule.until = None
        return create_scheduled(plan, message_id, bot)
    name = save_note(bot.inbox, sent, text)
    bot.reply(message_id, '📝 Сохранено в inbox\nСерию не создал.', MAIN_KEYBOARD)
    return 'серия отменена, заметка ' + name


# Машина состояний. Добавить шаг = добавить строку сюда и, если он вызывается
# кнопкой, строку в BUTTONS. WAIT_MONEY и WAIT_COOK объявлены, но кнопками пока
# не включаются: их кнопки отвечают «пока не умею». Включение — замена
# button_stub на button_enter и настоящий обработчик вместо handle_not_ready.
STATES = {
    IDLE: State('', None, MAIN_KEYBOARD),
    'WAIT_EVENT_TEXT': State('Дата, время, [минут], название.\nНапример: завтра 15:00 90 Созвон',
                             handle_event_text, CANCEL_KEYBOARD),
    'WAIT_REMINDER_TEXT': State('Дата, время, текст.\nНапример: пт 19:00 Позвонить маме',
                                handle_reminder_text, CANCEL_KEYBOARD),
    'WAIT_MONEY': State('Сумма и категория.\nНапример: 450 еда',
                        handle_not_ready, CANCEL_KEYBOARD),
    'WAIT_COOK': State('Блюдо и что в нём.\nНапример: паста карбонара',
                       handle_not_ready, CANCEL_KEYBOARD),
    # Вопросы бота: в них он ждёт ответа кнопкой, подсказка не нужна — вопрос уже задан.
    'WAIT_PAST_CHOICE': State('', handle_past_choice, CANCEL_KEYBOARD),
    'WAIT_EVENT_CONFIRM': State('', handle_event_confirm, CANCEL_KEYBOARD),
}

# Кнопки-ответы: если их нажали, когда вопроса уже нет, бот скажет об этом,
# а не заведёт заметку с текстом «✅ Создать».
ANSWER_BUTTONS = (BTN_BACKDATE, BTN_CREATE, BTN_FOREVER)


def button_enter(state_name):
    def press(message_id, bot):
        state = STATES[state_name]
        bot.set_state(state_name)
        bot.reply(message_id, state.prompt, state.keyboard)
        return 'кнопка → %s' % state_name
    return press


def button_stub(what):
    def press(message_id, bot):
        bot.set_state(IDLE)
        bot.reply(message_id, '%s — пока не умею.' % what, MAIN_KEYBOARD)
        return 'кнопка «%s»: заглушка' % what
    return press


def button_cancel(message_id, bot):
    was = bot.state
    bot.set_state(IDLE)
    bot.reply(message_id, 'Отменил. Дальше текст снова становится заметкой.', MAIN_KEYBOARD)
    return 'отмена (было %s)' % was


BUTTONS = {
    BTN_REMINDER: button_enter('WAIT_REMINDER_TEXT'),
    BTN_EVENT: button_enter('WAIT_EVENT_TEXT'),
    BTN_MONEY: button_stub('Деньги'),
    BTN_COOK: button_stub('Cook'),
    BTN_CANCEL: button_cancel,
}


# --- команды ----------------------------------------------------------------

def git_line(vault):
    """Строка про синхронизацию для /stats: когда был последний коммит и что не в нём."""
    if not vault:
        return '🔄 синхронизация: не знаю, где vault'

    def git(*args):
        res = subprocess.run(['/usr/bin/git', '-C', vault] + list(args),
                             capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise OSError((res.stderr.strip() or 'git вернул %d' % res.returncode)[:100])
        return res.stdout
    try:
        stamp = git('log', '-1', '--format=%ct').strip()
        when = datetime.datetime.fromtimestamp(int(stamp))
        line = '🔄 синхронизация: %s' % format_when(when, datetime.datetime.now())
        dirty = [l for l in git('status', '--porcelain').splitlines() if l.strip()]
        if dirty:
            line += '\n📄 не закоммичено: %d %s' % (len(dirty),
                                                    plural(len(dirty), 'файл', 'файла', 'файлов'))
        return line
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return '🔄 синхронизация: git не ответил (%s)' % e


def stats_text(bot):
    lines = []
    try:
        notes = len([n for n in os.listdir(bot.inbox) if n.endswith('.md')])
        lines.append('📥 inbox: %d %s' % (notes, plural(notes, 'заметка', 'заметки', 'заметок')))
    except OSError as e:
        lines.append('📥 inbox: не прочитать (%s)' % (e.strerror or e))
    lines.append(git_line(bot.vault))
    lines.append('🤖 бот на связи с %s' % START_TIME.strftime('%H:%M'))
    return '\n'.join(lines)


# --- обработка сообщения ----------------------------------------------------

def handle(update, bot):
    """Обработать одно обновление; вернуть строку для лога."""
    msg = update.get('message')
    if not msg:
        return 'пропущено: не сообщение'
    chat = msg.get('chat', {}).get('id')
    if chat != bot.chat_id:
        return 'пропущено: чужой чат %s' % chat
    message_id = msg['message_id']
    sent = datetime.datetime.fromtimestamp(msg['date'])
    text = msg.get('text') or msg.get('caption') or ''
    attachment = next((kind for kind in ATTACHMENTS if kind in msg), None)
    if not text.strip():
        bot.reply(message_id, 'Сохраняю только текст — вложения и голосовые не в этой версии.')
        return 'пропущено: без текста (%s)' % (attachment or 'пусто')

    expired = bot.expire_state()
    raw = (msg.get('text') or '').strip()

    press = BUTTONS.get(raw)
    if press:
        return press(message_id, bot)

    command = split_command(msg['text']) if msg.get('text') else None
    if command:
        # Команда — такое же явное намерение, как нажатие кнопки: сбрасывает состояние.
        was, name, args, notes = bot.state, command[0], command[1], command[2]
        bot.set_state(IDLE)
        if name in ('help', 'start'):
            bot.reply(message_id, help_text(), MAIN_KEYBOARD)
            return 'справка'
        if name == 'menu':
            bot.reply(message_id, 'Клавиатура внизу.', MAIN_KEYBOARD)
            return 'меню'
        if name == 'cancel':
            bot.reply(message_id, 'Отменил.' if was != IDLE else 'Нечего отменять.', MAIN_KEYBOARD)
            return 'отмена команды (было %s)' % was
        if name == 'stats':
            bot.reply(message_id, stats_text(bot))
            return 'статистика'
        if name in ('e', 'r'):
            return handle_schedule(name, args, notes, msg['text'], sent, message_id, bot)
        # Незнакомая команда — это просто текст, он уйдёт в заметку ниже.

    if bot.state != IDLE:
        state = STATES[bot.state]
        data = bot.data
        bot.set_state(IDLE)  # шаг однократный; обработчик при нужде выставит своё состояние
        return state.handler(text, sent, message_id, bot, data)

    if raw in ANSWER_BUTTONS:
        bot.reply(message_id, 'Этот вопрос уже закрыт — ответ не к чему применить.', MAIN_KEYBOARD)
        return 'ответ на закрытый вопрос: %s' % raw

    callout = ''
    if attachment:
        callout = '> [!note] Бот: вложение (%s) не сохранено — в этой версии только текст' % attachment
    name = save_note(bot.inbox, sent, text, callout)
    answer = '📝 Сохранено в inbox'
    if attachment:
        answer += '\n⚠️ Вложение (%s) не сохранил — в этой версии только текст.' % attachment
    if expired:
        answer += '\nПрошло больше 10 минут с нажатия кнопки — сохранил как заметку.'
    bot.reply(message_id, answer)
    return 'заметка ' + name


def handle_schedule(kind, args, notes, raw_text, sent, message_id, bot):
    """Общая часть для команд /e, /r и для ввода после нажатия кнопки."""
    try:
        plan = parse_event(args, sent) if kind == 'e' else parse_reminder(args, sent)
    except ParseError as e:
        empty = Plan(kind, sent, 0, '', notes, raw_text, sent)
        return schedule_failed(empty, str(e), bool(args or notes), message_id, bot)
    plan.notes, plan.raw = notes, raw_text
    if plan.rule:
        return ask_confirm(plan, message_id, bot)
    if plan.when < bot.now() and not plan.exact_year:
        return ask_past(plan, message_id, bot)
    return create_scheduled(plan, message_id, bot)


def rescue(update, bot):
    """Сбой в обработке: сохранить текст как есть, чтобы мысль не потерялась."""
    msg = update.get('message') or {}
    text = msg.get('text') or msg.get('caption') or ''
    if msg.get('chat', {}).get('id') != bot.chat_id or not text.strip():
        return 'сбой, сохранять нечего'
    sent = datetime.datetime.fromtimestamp(msg.get('date', time.time()))
    name = save_note(bot.inbox, sent, text, '> [!warning] Бот: сбой при обработке, текст сохранён как есть')
    bot.reply(msg.get('message_id'), '⚠️ Сбой при обработке. Текст сохранил в inbox: %s' % name)
    return 'сбой, текст в заметке %s' % name


# --- цикл -------------------------------------------------------------------

def read_offset():
    try:
        age = time.time() - os.stat(OFFSET_FILE).st_mtime
    except FileNotFoundError:
        return 0
    if age > OFFSET_MAX_AGE:
        log('offset не менялся %d дн. — сбрасываю в 0' % (age // 86400))
        return 0
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip() or 0)
    except ValueError:
        log('offset повреждён — сбрасываю в 0')
        return 0


def write_offset(value):
    tmp = OFFSET_FILE + '.tmp'
    with open(tmp, 'w') as f:
        f.write('%d\n' % value)
    os.replace(tmp, OFFSET_FILE)


def poll_loop(bot):
    """Вечный цикл long polling. Файл offset пишется только при продвижении:
    по его дате видно, когда было последнее сообщение."""
    global _busy
    offset = read_offset()
    backoff = BACKOFF_START
    last_error, last_error_at = None, 0.0
    commands_done, command_tries = False, 0
    log('старт: long polling, таймаут %d с, состояние %s' % (POLL_TIMEOUT, bot.state))
    while True:
        payload = {'timeout': POLL_TIMEOUT, 'allowed_updates': ['message']}
        if offset:
            payload['offset'] = offset
        try:
            updates = bot.api('getUpdates', payload, HTTP_TIMEOUT)
        except ApiError as e:
            if e.fatal:
                return fatal('getUpdates: %s — дальше повторять нечего, останавливаюсь' % e)
            reason = str(e)
            now_ts = time.time()
            if reason != last_error or now_ts - last_error_at >= ERROR_LOG_INTERVAL:
                log('getUpdates: ошибка — %s; пауза %d с' % (reason, backoff))
                last_error, last_error_at = reason, now_ts
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        if last_error:
            log('связь восстановлена')
            last_error = None
        backoff = BACKOFF_START
        if not commands_done and command_tries < COMMANDS_MAX_TRIES:
            # Только после удачного getUpdates: сразу после загрузки мака сети может не быть.
            commands_done = bot.register_commands()
            command_tries += 1
        if updates:
            log('получено обновлений: %d' % len(updates))
        for update in updates:
            uid = update['update_id']
            _busy = True
            try:
                try:
                    result = handle(update, bot)
                except NoteError as e:
                    return fatal('update %d: заметка не записана (%s) — offset не сдвигаю, '
                                 'перезапущусь и повторю' % (uid, e))
                except Exception:
                    log('update %d: сбой обработки\n%s' % (uid, traceback.format_exc()))
                    try:
                        result = rescue(update, bot)
                    except NoteError as e:
                        return fatal('update %d: не записал и запасную заметку (%s) — '
                                     'offset не сдвигаю' % (uid, e))
                offset = uid + 1
                write_offset(offset)
                log('update %d: %s' % (uid, result))
            finally:
                _busy = False
            if _stopping:
                log('SIGTERM: сообщение дообработано, выхожу')
                return 0


def run(bot):
    try:
        return poll_loop(bot)
    except Stop:
        log('SIGTERM в ожидании: выхожу')
        return 0


def add_event_cli(args, runner=run_osascript, now=None):
    """add-event.sh "Название" "YYYY-MM-DD HH:MM" [минут] [календарь].

    Та же реализация, что у команды /e: EVENT_SCRIPT и schedule_argv.
    Имя календаря здесь остаётся в выводе: это аргумент команды, а не конфиг.
    Код 0 — событие создано, 1 — ошибка Календаря, 2 — неверные аргументы."""
    usage = 'использование: add-event.sh "Название" "YYYY-MM-DD HH:MM" [минут] [календарь]'
    if not 2 <= len(args) <= 4:
        print(usage, file=sys.stderr)
        return 2
    title = args[0].strip()
    if not title:
        print('ОШИБКА: пустое название\n' + usage, file=sys.stderr)
        return 2
    try:
        when = datetime.datetime.strptime(args[1].strip(), '%Y-%m-%d %H:%M')
    except ValueError:
        print('ОШИБКА: «%s» — нужна существующая дата в формате YYYY-MM-DD HH:MM\n%s' % (args[1], usage),
              file=sys.stderr)
        return 2
    duration = DEFAULT_DURATION
    if len(args) >= 3:
        if not re.fullmatch(r'\d{1,4}', args[2].strip()) or not 1 <= int(args[2]) <= 1440:
            print('ОШИБКА: длительность «%s» — нужно число минут от 1 до 1440' % args[2], file=sys.stderr)
            return 2
        duration = int(args[2])
    cal_name = args[3].strip() if len(args) == 4 and args[3].strip() else DEFAULT_CALENDAR

    error = runner(EVENT_SCRIPT, schedule_argv(cal_name, title, '', when, duration))
    if error:
        print('ОШИБКА: событие не создано — %s' % error, file=sys.stderr)
        return 1
    current = now() if now else datetime.datetime.now()
    print('📅 %s — %s (%s) → «%s»' % (title, format_when(when, current),
                                      format_duration(duration), cal_name))
    if when < current:
        print('⚠️ Это время уже прошло', file=sys.stderr)
    return 0


def main():
    install_signal_handlers()
    token = os.environ.get('TG_TOKEN', '').strip()
    if not token or token == TOKEN_PLACEHOLDER:
        return fatal('ОШИБКА: TG_TOKEN не задан — впиши токен в plist')
    log.hide(token)
    try:
        chat_id = int(os.environ.get('TG_CHAT_ID', ''))
    except ValueError:
        return fatal('ОШИБКА: TG_CHAT_ID не задан или не число')
    vault = os.path.expanduser(os.environ.get('VAULT') or DEFAULT_VAULT)
    inbox = os.path.join(vault, 'inbox')
    if not os.path.isdir(inbox):
        return fatal('ОШИБКА: нет папки %s' % inbox)
    try:
        os.listdir(inbox)  # под launchd macOS может запретить доступ к iCloud Drive
    except OSError as e:
        return fatal('ОШИБКА: нет доступа к %s (%s) — сообщения не забираю' % (inbox, e.strerror or e))

    lock = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return fatal('уже работает другой запуск — выхожу')

    bot = Bot(token, chat_id, inbox, os.environ.get('CAL_NAME') or DEFAULT_CALENDAR,
              os.environ.get('REM_LIST') or DEFAULT_REMINDER_LIST, vault)
    return run(bot)


if __name__ == '__main__':
    if sys.argv[1:2] == ['add-event']:
        sys.exit(add_event_cli(sys.argv[2:]))
    try:
        sys.exit(main())
    except Stop:
        log('SIGTERM до старта цикла: выхожу')
        sys.exit(0)
    except Exception:
        log('ОШИБКА: непредвиденный сбой\n%s' % traceback.format_exc())
        sys.exit(1)
