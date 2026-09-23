#!/bin/bash
# Синхронизация vault «brain» с GitHub: коммит изменений, pull --rebase, push.
# Запускается launchd каждые 30 минут; лог пишется в stdout (StandardOutPath в plist).
#
# Ничего не делает, если:
#   - репозиторий в состоянии merge/rebase — нужно вмешательство, код 1;
#   - git занят (index.lock): в этот же репозиторий коммитит Obsidian Git, код 0;
#     lock старше часа считается зависшим, код 1;
#   - remote недоступен — код 0.

set -u -o pipefail

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15"
VAULT="${VAULT:-$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/brain}"
STALE_LOCK_SECONDS=3600

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

fail() {
  log "ОШИБКА: $1"
  if [ -n "${2:-}" ]; then printf '%s\n' "$2" | sed 's/^/    /'; fi
  exit 1
}

cd "$VAULT" 2>/dev/null || fail "нет доступа к папке vault: $VAULT" "$(ls -ld "$VAULT" 2>&1)"
GIT_DIR_PATH="$(git rev-parse --git-dir 2>&1)" || fail "git не видит репозиторий в $VAULT" "$GIT_DIR_PATH
чтение файла .git: $(head -c 80 .git 2>&1)"

for state in MERGE_HEAD rebase-merge rebase-apply CHERRY_PICK_HEAD REVERT_HEAD; do
  if [ -e "$GIT_DIR_PATH/$state" ]; then
    fail "репозиторий в состоянии $state — синхронизацию пропускаю, разберись вручную"
  fi
done

# .git вынесена через --separate-git-dir, поэтому путь к lock — только через rev-parse.
LOCK="$(git rev-parse --git-dir)/index.lock"
if [ -e "$LOCK" ]; then
  age=$(( $(date +%s) - $(stat -f %m "$LOCK") ))
  if [ "$age" -gt "$STALE_LOCK_SECONDS" ]; then
    fail "index.lock висит $((age / 60)) мин — похоже на упавший git, проверь: $LOCK"
  fi
  log "пропуск: git занят (index.lock ${age} с) — вероятно, коммитит Obsidian Git"
  exit 0
fi

if ! out=$(git ls-remote origin HEAD 2>&1); then
  log "пропуск: remote недоступен"
  printf '%s\n' "$out" | sed 's/^/    /'
  exit 0
fi

out=$(git add -A 2>&1) || fail "git add -A" "$out"
committed=""
if ! git diff --cached --quiet; then
  out=$(git commit -q -m "vault(auto): $(date '+%Y-%m-%d %H:%M')" 2>&1) || fail "git commit" "$out"
  committed="$(git show --shortstat --format= HEAD | sed 's/^ *//')"
fi

out=$(git pull --rebase --autostash -q 2>&1) || fail "git pull --rebase --autostash" "$out"

# Хэш — после pull: rebase мог переписать только что сделанный коммит.
if [ -n "$committed" ]; then
  done_commit="коммит $(git rev-parse --short HEAD): $committed"
else
  done_commit="изменений нет"
fi

ahead=$(git rev-list --count '@{u}..HEAD' 2>&1) || fail "у ветки нет upstream" "$ahead"
if [ "$ahead" -gt 0 ]; then
  out=$(git push -q 2>&1) || fail "git push" "$out"
  done_push="отправлено коммитов: $ahead"
else
  done_push="отправлять нечего"
fi

log "$done_commit; $done_push"
