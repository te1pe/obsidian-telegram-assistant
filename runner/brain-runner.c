// brain-runner — запускатор launchd-агентов vault «brain».
//
// macOS не пускает процессы, запущенные launchd, к файлам в iCloud Drive.
// «Полный доступ к диску» выдаётся только этой программе, а не /bin/bash
// и не python3. Она запускает один из двух скриптов дочерним процессом
// (не exec: иначе разрешение перестанет действовать), ждёт завершения
// и возвращает его код выхода. Сигналы остановки передаёт скрипту.
//
//   brain-runner tginbox    → /usr/bin/python3 -I ~/bin/tg-inbox.py
//   brain-runner vaultsync  → /bin/bash ~/bin/vault-sync.sh
//
// Скрипту передаются только нужные переменные окружения, PATH фиксирован.
// Скрипт и папка ~/bin должны принадлежать пользователю, не быть ссылками
// и быть закрыты на запись для группы и остальных — иначе отказ (код 77).
//
// Сборка; после пересборки «Полный доступ к диску» нужно выдать заново.
// --options runtime обязателен: без hardened runtime dyld подгрузит в запускатор
// чужую библиотеку из DYLD_INSERT_LIBRARIES, и она получит полный доступ к диску.
//   clang -O2 -Wall -Wextra -o ~/bin/brain-runner ~/bin/brain-runner.c
//   codesign --force --sign - --options runtime --identifier com.example.brain-runner ~/bin/brain-runner

#include <errno.h>
#include <limits.h>
#include <pwd.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef SCRIPTS_DIR
#define SCRIPTS_DIR NULL  // по умолчанию ~/bin; тесты подставляют свою папку при сборке
#endif

#define EX_USAGE 64
#define EX_SOFTWARE 70
#define EX_OSERR 71
#define EX_NOPERM 77

extern char **environ;

static const char *const ALLOWED_ENV[] = {
    "HOME", "USER", "LOGNAME", "TMPDIR", "SSH_AUTH_SOCK", "LANG", "LC_ALL", "LC_CTYPE",
    "TG_TOKEN", "TG_CHAT_ID", "VAULT", "CAL_NAME", "REM_LIST", NULL,
};

static volatile sig_atomic_t child_pid = 0;

static void forward_signal(int sig) {
    if (child_pid > 0) {
        kill((pid_t)child_pid, sig);
    }
}

static int usage(void) {
    fprintf(stderr, "использование: brain-runner tginbox | vaultsync\n");
    return EX_USAGE;
}

// Принадлежит пользователю, не ссылка и закрыт на запись для группы и остальных.
static int owned_and_closed(const char *path, int want_dir) {
    struct stat st;
    if (lstat(path, &st) != 0) {
        fprintf(stderr, "brain-runner: %s: %s\n", path, strerror(errno));
        return 0;
    }
    int type_ok = want_dir ? S_ISDIR(st.st_mode) : S_ISREG(st.st_mode);
    if (!type_ok || st.st_uid != getuid() || (st.st_mode & (S_IWGRP | S_IWOTH))) {
        fprintf(stderr, "brain-runner: отказ — %s должен быть %s пользователя без записи для группы и остальных\n",
                path, want_dir ? "папкой" : "обычным файлом");
        return 0;
    }
    return 1;
}

static char **filtered_environment(void) {
    size_t count = 0;
    for (char **e = environ; *e; e++) {
        count++;
    }
    char **env = calloc(count + 2, sizeof(char *));
    if (!env) {
        return NULL;
    }
    size_t k = 0;
    env[k++] = "PATH=/usr/bin:/bin:/usr/sbin:/sbin";
    for (char **e = environ; *e; e++) {
        for (const char *const *name = ALLOWED_ENV; *name; name++) {
            size_t len = strlen(*name);
            if (strncmp(*e, *name, len) == 0 && (*e)[len] == '=') {
                env[k++] = *e;
                break;
            }
        }
    }
    env[k] = NULL;
    return env;
}

int main(int argc, char *argv[]) {
    if (argc != 2) {
        return usage();
    }
    const char *interpreter;
    const char *script_name;
    const char *extra_flag = NULL;
    if (strcmp(argv[1], "tginbox") == 0) {
        interpreter = "/usr/bin/python3";
        extra_flag = "-I";  // изолированный режим: без PYTHON* и пользовательских site-packages
        script_name = "tg-inbox.py";
    } else if (strcmp(argv[1], "vaultsync") == 0) {
        interpreter = "/bin/bash";
        script_name = "vault-sync.sh";
    } else {
        return usage();
    }

    char dir[PATH_MAX];
    char script[PATH_MAX];
    const char *scripts_dir = SCRIPTS_DIR;
    if (scripts_dir) {
        snprintf(dir, sizeof dir, "%s", scripts_dir);
    } else {
        struct passwd *pw = getpwuid(getuid());
        if (!pw || !pw->pw_dir) {
            fprintf(stderr, "brain-runner: не найден домашний каталог\n");
            return EX_SOFTWARE;
        }
        snprintf(dir, sizeof dir, "%s/bin", pw->pw_dir);
    }
    int written = snprintf(script, sizeof script, "%s/%s", dir, script_name);
    if (written < 0 || (size_t)written >= sizeof script) {
        fprintf(stderr, "brain-runner: слишком длинный путь к скрипту\n");
        return EX_SOFTWARE;
    }
    if (!owned_and_closed(dir, 1) || !owned_and_closed(script, 0)) {
        return EX_NOPERM;
    }

    char **env = filtered_environment();
    if (!env) {
        perror("brain-runner: calloc");
        return EX_OSERR;
    }
    char *child_argv[4];
    int i = 0;
    child_argv[i++] = (char *)interpreter;
    if (extra_flag) {
        child_argv[i++] = (char *)extra_flag;
    }
    child_argv[i++] = script;
    child_argv[i] = NULL;

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    // Дочерний процесс, а не exec: разрешение macOS считается по этой программе.
    pid_t pid;
    int rc = posix_spawn(&pid, interpreter, NULL, NULL, child_argv, env);
    if (rc != 0) {
        fprintf(stderr, "brain-runner: не запустить %s: %s\n", interpreter, strerror(rc));
        return EX_OSERR;
    }
    child_pid = pid;

    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("brain-runner: waitpid");
            return EX_OSERR;
        }
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return EX_SOFTWARE;
}
