/*
 * lua_hook.c — LD_PRELOAD hook for luaL_loadbufferx
 * Dumps all decrypted Lua buffers (bytecode + source) to DUMP_DIR
 *
 * Build: gcc -shared -fPIC -o lua_hook.so lua_hook.c -ldl
 * Usage: LD_PRELOAD=./lua_hook.so DUMP_DIR=/tmp/dump FXServer ...
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <dlfcn.h>
#include <sys/stat.h>
#include <pthread.h>

typedef void lua_State;

/* Original function pointer */
typedef int (*luaL_loadbufferx_fn)(lua_State *L, const char *buf, size_t size,
                                   const char *name, const char *mode);

static luaL_loadbufferx_fn original = NULL;
static const char *dump_dir = "/tmp/fivem_dump";
static int dump_count = 0;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

static void sanitize_filename(const char *name, char *out, size_t out_size) {
    size_t j = 0;
    if (!name || !*name) {
        snprintf(out, out_size, "unnamed");
        return;
    }
    /* Skip prefix chars */
    const char *p = name;
    while (*p == '@' || *p == '=' || *p == '.') p++;

    for (size_t i = 0; p[i] && j < out_size - 1; i++) {
        char c = p[i];
        if (c == '/' || c == '\\') {
            out[j++] = '_';
        } else if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                   (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.') {
            out[j++] = c;
        } else {
            out[j++] = '_';
        }
    }
    out[j] = '\0';
    if (j == 0) snprintf(out, out_size, "unnamed");
}

__attribute__((constructor))
static void init(void) {
    original = (luaL_loadbufferx_fn)dlsym(RTLD_NEXT, "luaL_loadbufferx");
    dump_dir = getenv("DUMP_DIR");
    if (!dump_dir) dump_dir = "/tmp/fivem_dump";
    mkdir(dump_dir, 0755);

    /* Write PID file so the web app can find us */
    char pid_path[512];
    snprintf(pid_path, sizeof(pid_path), "%s/.hook_pid", dump_dir);
    int fd = open(pid_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        char pid_str[32];
        snprintf(pid_str, sizeof(pid_str), "%d", getpid());
        write(fd, pid_str, strlen(pid_str));
        close(fd);
    }

    fprintf(stderr, "[HOOK] lua_hook.so loaded, DUMP_DIR=%s, PID=%d\n", dump_dir, getpid());
}

int luaL_loadbufferx(lua_State *L, const char *buf, size_t size,
                     const char *name, const char *mode) {
    if (!original) {
        /* Fallback: try dlopen */
        void *h = dlopen(NULL, RTLD_NOW);
        original = (luaL_loadbufferx_fn)dlsym(h, "luaL_loadbufferx");
    }

    /* Dump the buffer */
    if (buf && size > 4) {
        /* Skip FXAP-encrypted buffers */
        if (size >= 4 && memcmp(buf, "FXAP", 4) == 0) {
            goto call_original;
        }

        /* Skip non-Lua content */
        int is_lua_bytecode = (size >= 4 && memcmp(buf, "\x1bLua", 4) == 0);
        int is_lua_source = 0;
        if (!is_lua_bytecode && size >= 6) {
            /* Check for common Lua source starts */
            const char *s = buf;
            while (*s == ' ' || *s == '\t' || *s == '\n' || *s == '\r') s++;
            if (strncmp(s, "local ", 6) == 0 || strncmp(s, "function", 8) == 0 ||
                strncmp(s, "--", 2) == 0 || strncmp(s, "return ", 7) == 0 ||
                strncmp(s, "if ", 3) == 0) {
                is_lua_source = 1;
            }
        }

        if (is_lua_bytecode || is_lua_source) {
            pthread_mutex_lock(&lock);
            int id = dump_count++;

            char safe_name[256];
            sanitize_filename(name, safe_name, sizeof(safe_name));

            char filepath[512];
            snprintf(filepath, sizeof(filepath), "%s/%s_%d%s",
                     dump_dir, safe_name, id,
                     is_lua_bytecode ? ".bin" : ".lua");

            int fd = open(filepath, O_WRONLY | O_CREAT | O_TRUNC, 0644);
            if (fd >= 0) {
                write(fd, buf, size);
                close(fd);
                fprintf(stderr, "\n[DUMP #%d] %s %s (%zu bytes) chunk='%s'\n",
                        id + 1,
                        is_lua_bytecode ? "BYTECODE" : "SOURCE",
                        filepath, size,
                        name ? name : "(null)");
            }
            pthread_mutex_unlock(&lock);
        }
    }

call_original:
    return original(L, buf, size, name, mode);
}
