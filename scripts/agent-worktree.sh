#!/usr/bin/env bash
# Создать/убрать изолированный worktree для агента.
# Файлы worktree физически отдельны (нельзя снести чужие незакоммиченные правки),
# но git-история, ветки и коммиты — общие (агенты видят работу друг друга).
#
#   scripts/agent-worktree.sh add  <имя> <задача>   # напр.: add a couriers
#   scripts/agent-worktree.sh list
#   scripts/agent-worktree.sh rm   <имя>
#
# После add: cd ../Teplo-agent-<имя> и работай там.
# Второму агенту подними изолированный стек: apps/docker-compose.agent-b.yml
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
PARENT="$(dirname "$ROOT")"
cmd="${1:-}"

case "$cmd" in
  add)
    name="${2:?нужно имя агента}"; task="${3:?нужно имя задачи}"
    dir="$PARENT/Teplo-agent-$name"
    branch="agent/$name-$task"
    git -C "$ROOT" fetch --quiet origin || true
    git -C "$ROOT" worktree add "$dir" -b "$branch"
    echo "✓ worktree: $dir   ветка: $branch"
    echo "→ cd $dir  и впиши свою зону в WORK-IN-PROGRESS.md (git pull сначала)"
    ;;
  list)
    git -C "$ROOT" worktree list
    ;;
  rm)
    name="${2:?нужно имя агента}"
    git -C "$ROOT" worktree remove "$PARENT/Teplo-agent-$name"
    echo "✓ убран worktree agent-$name (ветка осталась — смёрджи или удали вручную)"
    ;;
  *)
    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
