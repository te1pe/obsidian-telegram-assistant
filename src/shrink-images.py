#!/usr/bin/python3
"""Сжатие картинок vault в WebP с перезаписью ссылок.

    shrink-images.py [--dry-run] [--include-private] [VAULT]

VAULT по умолчанию — $VAULT или ~/Library/Mobile Documents/iCloud~md~obsidian/Documents/brain.

- Берёт из VAULT/assets/ файлы .png/.jpg/.jpeg тяжелее 300 КБ.
- cwebp -q 82, ширина не больше 1920 (текущая — sips -g pixelWidth).
- WebP не меньше оригинала — откат, оригинал остаётся.
- Переписывает ссылки [[…]] и ![](…) во всех .md vault. Дата изменения
  заметок сохраняется: CLAUDE.md берёт из неё created.
- Оригинал убирает из vault только после успешной перезаписи ссылок —
  переносит в ~/backups/shrink-images/<время>/, а не удаляет
  (CLAUDE.md: ничего не удалять).
- Рисунки Excalidraw как текст не правит: картинку, на которую ссылается
  рисунок, не трогает.
- private/ не читает и не меняет без --include-private.
"""
import argparse
import collections
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

THRESHOLD = 300 * 1024
QUALITY = '82'
MAX_WIDTH = 1920
EXTS = ('.png', '.jpg', '.jpeg')
SKIP_DIRS = {'.git', '.obsidian', '.trash'}
DEFAULT_VAULT = '~/Library/Mobile Documents/iCloud~md~obsidian/Documents/brain'
QUARANTINE = os.path.expanduser('~/backups/shrink-images')

FENCE = re.compile(r'^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1', re.S | re.M)
INLINE = re.compile(r'`[^`\n]*`')
WIKI = re.compile(r'\[\[([^\[\]\n]+?)\]\]')
MDLINK = re.compile(r'\[[^\]\n]*\]\(([^)\n]+)\)')
TITLE = re.compile(r'(.*?)(\s+("[^"]*"|\'[^\']*\'))?$')
EXTERNAL = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')

Link = collections.namedtuple('Link', 'target end text')


def die(msg):
    print('ОШИБКА: ' + msg, file=sys.stderr)
    sys.exit(1)


def kb(n):
    return '%d КБ' % round(n / 1024)


def mb(n):
    return ('%.1f МБ' % (n / 1048576)).replace('.', ',')


def walk(vault, include_private):
    """Относительные пути файлов vault без .git, .obsidian, .trash и private/."""
    for dp, dn, fn in os.walk(vault):
        top = os.path.abspath(dp) == vault
        dn[:] = sorted(d for d in dn
                       if d not in SKIP_DIRS and not (top and d == 'private' and not include_private))
        for n in sorted(fn):
            yield os.path.relpath(os.path.join(dp, n), vault)


class Index:
    """Разрешение ссылок примерно как в Obsidian: путь от корня vault,
    от папки заметки, затем по окончанию пути; без учёта регистра."""

    def __init__(self, files):
        self.files = list(files)
        self.lower = {}
        for f in self.files:
            self.lower.setdefault(f.lower(), f)

    def resolve(self, target, note):
        t = target.strip().lstrip('/').lower()
        if not t:
            return None
        here = os.path.dirname(note).lower()
        for cand in (t, t + '.md'):
            for rel in (cand, os.path.normpath(os.path.join(here, cand))):
                if rel in self.lower:
                    return self.lower[rel]
        found = [f for f in self.files
                 if f.lower().endswith('/' + t) or f.lower().endswith('/' + t + '.md')]
        if not found:
            return None
        found.sort(key=lambda f: (os.path.dirname(f).lower() != here, f.count('/'), f))
        return found[0]


def code_spans(text):
    spans = [m.span() for m in FENCE.finditer(text)]
    spans += [m.span() for m in INLINE.finditer(text)
              if not any(a <= m.start() < b for a, b in spans)]
    return spans


def find_links(text):
    """Ссылки [[…]] и [](…) вне кода; end — позиция конца пути в тексте."""
    spans = code_spans(text)

    def in_code(pos):
        return any(a <= pos < b for a, b in spans)

    links = []
    for m in WIKI.finditer(text):
        if in_code(m.start()):
            continue
        path = re.split(r'\\?\||#|\^', m.group(1), 1)[0].rstrip()
        if path.strip():
            links.append(Link(path, m.start(1) + len(path), m.group(0)))
    for m in MDLINK.finditer(text):
        if in_code(m.start()):
            continue
        raw = m.group(1)
        start = m.start(1) + len(raw) - len(raw.lstrip())
        body = raw.strip()
        if body.startswith('<'):
            if '>' not in body:
                continue
            path, start = body[1:body.index('>')], start + 1
        else:
            path = TITLE.match(body).group(1)
        path = path.split('#', 1)[0]
        if path and not EXTERNAL.match(path):
            links.append(Link(urllib.parse.unquote(path), start + len(path), m.group(0)))
    return links


def is_drawing(rel, text):
    return rel.lower().endswith('.excalidraw.md') or 'excalidraw-plugin:' in text[:2000]


def pixel_width(path):
    out = subprocess.run(['/usr/bin/sips', '-g', 'pixelWidth', path],
                         capture_output=True, text=True).stdout
    m = re.search(r'pixelWidth:\s*(\d+)', out)
    return int(m.group(1)) if m else None


def write_keep_dates(path, text):
    """Атомарная запись с сохранением прав и дат файла."""
    st = os.stat(path)
    tmp = os.path.join(os.path.dirname(path), '.%s.shrink-tmp' % os.path.basename(path))
    try:
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)  # только свой временный файл
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def apply(vault, plan, notes, stamp, include_private):
    placed, written = [], []
    try:
        # 1. WebP рядом с оригиналом, с датами оригинала
        for p in plan:
            dst = os.path.join(vault, p['webp'])
            shutil.copyfile(p['tmp'], dst)
            placed.append(dst)
            st = os.stat(os.path.join(vault, p['rel']))
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))

        # 2. Ссылки: каждая заметка пишется один раз
        edits = collections.defaultdict(set)
        for p in plan:
            for note, link in p['refs']:
                edits[note].add((link.end - len(p['ext']), link.end))
        for note in sorted(edits):
            text = notes[note]
            for a, b in sorted(edits[note], reverse=True):
                text = text[:a] + '.webp' + text[b:]
            path = os.path.join(vault, note)
            written.append((path, notes[note], os.stat(path)))
            write_keep_dates(path, text)

        # 3. Проверка до того, как убрать оригиналы
        originals = {p['rel'] for p in plan}
        webps = {p['webp'] for p in plan}
        index = Index(walk(vault, include_private))
        left, to_webp = [], 0
        for note in notes:
            with open(os.path.join(vault, note), encoding='utf-8') as f:
                text = f.read()
            for link in find_links(text):
                dest = index.resolve(link.target, note)
                if dest in originals:
                    left.append('%s: %s' % (note, link.text))
                elif dest in webps:
                    to_webp += 1
        expected = sum(len(p['refs']) for p in plan)
        if left or to_webp != expected:
            raise RuntimeError('на оригиналы ведут %d ссылок, на WebP — %d из %d. %s'
                               % (len(left), to_webp, expected, '; '.join(left[:5])))
    except BaseException as e:
        for path, text, st in reversed(written):
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(text)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        for dst in placed:
            os.remove(dst)  # WebP, созданный этим же запуском
        die('%s — ссылки и WebP возвращены как было, оригиналы на месте' % e)

    # 4. Оригиналы — из vault в карантин, не удаляются
    qdir = os.path.join(QUARANTINE, stamp)
    for p in plan:
        dst = os.path.join(qdir, p['rel'])
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(os.path.join(vault, p['rel']), dst)
        except OSError as e:
            die('не перенесён %s: %s. Ссылки уже ведут на WebP; оставшиеся оригиналы лежат в vault'
                % (p['rel'], e))
    return qdir


def print_plan(vault, plan, skipped, heavy, images, assets_before, args):
    mode = 'пробный прогон, в vault ничего не меняется' if args.dry_run else 'применение'
    print('Сжатие картинок: %s/assets' % vault)
    print('Порог %s, WebP q%s, ширина не больше %d. Режим: %s.' % (kb(THRESHOLD), QUALITY, MAX_WIDTH, mode))
    if not args.include_private:
        print('private/ не читалась: ссылки оттуда не проверены и не переписаны.')
    print('\nКартинок в assets/: %d, тяжелее порога: %d' % (len(images), len(heavy)))
    if plan:
        print('\n%9s %9s %6s %11s %6s  %s' % ('было', 'стало', 'экон.', 'ширина', 'ссылок', 'файл'))
        for p in plan:
            w = str(p['width']) if p['width'] <= MAX_WIDTH else '%d→%d' % (p['width'], MAX_WIDTH)
            print('%9s %9s %5d%% %11s %6d  %s' % (
                kb(p['size']), kb(p['new_size']), round(100 * (1 - p['new_size'] / p['size'])),
                w, len(p['refs']), os.path.basename(p['rel'])))
    for rel, why in skipped:
        print('  пропущено: %s — %s' % (os.path.basename(rel), why))
    was = sum(p['size'] for p in plan)
    now = sum(p['new_size'] for p in plan)
    print('\nИтого: сжато %d из %d; было %s, стало %s, освобождено %s'
          % (len(plan), len(heavy), mb(was), mb(now), mb(was - now)))
    print('assets/: %s → %s' % (mb(assets_before), mb(assets_before - was + now)))
    per_note = collections.Counter(note for p in plan for note, _ in p['refs'])
    print('Ссылки к перезаписи: %d в %d заметках (дата изменения заметок сохраняется)'
          % (sum(per_note.values()), len(per_note)))
    for note, n in sorted(per_note.items()):
        print('  %3d  %s' % (n, note))


def main():
    ap = argparse.ArgumentParser(description='Сжатие картинок vault в WebP с перезаписью ссылок.')
    ap.add_argument('vault', nargs='?', default=os.environ.get('VAULT', DEFAULT_VAULT))
    ap.add_argument('--dry-run', action='store_true', help='показать план, ничего не меняя в vault')
    ap.add_argument('--include-private', action='store_true', help='обрабатывать и private/')
    args = ap.parse_args()

    vault = os.path.abspath(os.path.expanduser(args.vault))
    if not os.path.isdir(os.path.join(vault, 'assets')):
        die('нет папки assets/ в %s' % vault)
    cwebp = shutil.which('cwebp') or '/opt/homebrew/bin/cwebp'
    if not os.access(cwebp, os.X_OK):
        die('не найден cwebp')

    files = list(walk(vault, args.include_private))
    index = Index(files)
    notes = {}
    for rel in files:
        if rel.lower().endswith('.md'):
            with open(os.path.join(vault, rel), 'rb') as f:
                data = f.read()
            try:
                notes[rel] = data.decode('utf-8')
            except UnicodeDecodeError:
                die('%s не в UTF-8 — ничего не менял' % rel)

    refs, drawn = collections.defaultdict(list), collections.defaultdict(set)
    for rel, text in notes.items():
        drawing = is_drawing(rel, text)
        for link in find_links(text):
            dest = index.resolve(link.target, rel)
            if dest is None:
                continue
            if drawing:
                drawn[dest].add(rel)
            else:
                refs[dest].append((rel, link))

    in_assets = [rel for rel in files if rel.startswith('assets' + os.sep)]
    assets_before = sum(os.path.getsize(os.path.join(vault, rel)) for rel in in_assets)
    images = [rel for rel in in_assets if rel.lower().endswith(EXTS)]
    heavy = [rel for rel in images if os.path.getsize(os.path.join(vault, rel)) > THRESHOLD]

    plan, skipped = [], []
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    with tempfile.TemporaryDirectory(prefix='shrink-images-') as tmp:
        for i, rel in enumerate(heavy):
            src = os.path.join(vault, rel)
            ext = os.path.splitext(rel)[1]
            webp = rel[:-len(ext)] + '.webp'
            if drawn[rel]:
                skipped.append((rel, 'на неё ссылается рисунок Excalidraw (%s)' % ', '.join(sorted(drawn[rel]))))
                continue
            if webp.lower() in index.lower:
                skipped.append((rel, 'уже есть %s' % webp))
                continue
            odd = sorted({note for note, link in refs[rel]
                          if notes[note][link.end - len(ext):link.end].lower() != ext.lower()})
            if odd:
                skipped.append((rel, 'ссылку не переписать автоматически (%s)' % ', '.join(odd)))
                continue
            width = pixel_width(src)
            if width is None:
                skipped.append((rel, 'sips не прочитал ширину'))
                continue
            out = os.path.join(tmp, '%d.webp' % i)
            cmd = [cwebp, '-quiet', '-q', QUALITY]
            if width > MAX_WIDTH:
                cmd += ['-resize', str(MAX_WIDTH), '0']
            res = subprocess.run(cmd + [src, '-o', out], capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(out):
                skipped.append((rel, 'cwebp: %s' % (res.stderr.strip() or 'код %d' % res.returncode)))
                continue
            size, new_size = os.path.getsize(src), os.path.getsize(out)
            if new_size >= size:
                skipped.append((rel, 'WebP не меньше оригинала (%s → %s) — откат' % (kb(size), kb(new_size))))
                continue
            plan.append({'rel': rel, 'webp': webp, 'tmp': out, 'ext': ext, 'size': size,
                         'new_size': new_size, 'width': width, 'refs': refs[rel]})

        print_plan(vault, plan, skipped, heavy, images, assets_before, args)
        if args.dry_run or not plan:
            return
        qdir = apply(vault, plan, notes, stamp, args.include_private)
    print('\nПрименено: WebP на месте, ссылки переписаны и проверены.')
    print('Оригиналы (%d) перенесены из vault в %s' % (len(plan), qdir.replace(os.path.expanduser('~'), '~', 1)))


if __name__ == '__main__':
    main()
