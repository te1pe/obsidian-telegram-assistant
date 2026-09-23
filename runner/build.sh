#!/bin/bash
# Сборка и подпись запускатора brain-runner.
#
#   ./build.sh [папка-назначения]        по умолчанию ~/bin
#
# ВНИМАНИЕ: идентификатор подписи — это то, по чему macOS запоминает выданные
# разрешения («Полный доступ к диску», «Автоматизация»). Если собрать программу
# с другим идентификатором или пересобрать её вообще, система посчитает её новой
# и разрешения придётся выдавать заново.
#
# --options runtime обязателен: без hardened runtime dyld подгрузит в запускатор
# чужую библиотеку из DYLD_INSERT_LIBRARIES, и она получит полный доступ к диску.
set -eu

DEST="${1:-$HOME/bin}"
IDENT="${BRAIN_RUNNER_ID:-com.example.brain-runner}"
SRC="$(cd "$(dirname "$0")" && pwd)/brain-runner.c"

mkdir -p "$DEST"
echo "сборка:      $SRC → $DEST/brain-runner"
echo "идентификатор: $IDENT  (переопределяется переменной BRAIN_RUNNER_ID)"

clang -O2 -Wall -Wextra -o "$DEST/brain-runner" "$SRC"
codesign --force --sign - --options runtime --identifier "$IDENT" "$DEST/brain-runner"
codesign -dv "$DEST/brain-runner" 2>&1 | grep -E "Identifier|flags" || true

echo
echo "Готово. При первом запуске macOS спросит доступ к папкам и к Календарю"
echo "с Напоминаниями — разрешения нужно выдать именно brain-runner."
