/* v3 interposer: one rewrite per input report, no multi-frame split. */
#define _GNU_SOURCE
#include <unistd.h>
#include <sys/syscall.h>
#include <sys/stat.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdlib.h>
#include <errno.h>
#include <time.h>
#include <poll.h>
#include <android/log.h>
#include "fullsplit-v4.c"

#ifndef RX_TARGET_PATH
#define RX_TARGET_PATH "/dev/hidg1"
#endif

static RX state;
__attribute__((constructor)) static void init_state(void) { rx_reset(&state); }
static int64_t now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

/* A gadget endpoint that is being (re)enumerated is transient: retry the whole
 * report inside a short bounded window. A failed whole-report write leaves no
 * partial state on the host, so retrying is safe. */
static ssize_t raw_write(int fd, const void *b, size_t n) {
    int64_t deadline = now_ms() + 100;
    for (;;) {
        ssize_t r = syscall(SYS_write, fd, b, n);
        if (r >= 0) return r;
        int e = errno;
        if (!(e == EAGAIN || e == EWOULDBLOCK || e == EINTR || e == ESHUTDOWN || e == ENODEV)) return r;
        if (now_ms() >= deadline) return r;
        struct pollfd p = { .fd = fd, .events = POLLOUT };
        poll(&p, 1, 10);
    }
}
static _Thread_local int entered;
static int owner_fd = -1;
static dev_t device, rdevice;
static ino_t inode;

static int target_fd(int fd, struct stat *s) {
    struct stat t;
    char p[64], name[128];
    if (fstat(fd, s) || !S_ISCHR(s->st_mode) || stat(RX_TARGET_PATH, &t)) return 0;
    if (s->st_dev != t.st_dev || s->st_ino != t.st_ino || s->st_rdev != t.st_rdev) return 0;
    snprintf(p, sizeof(p), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(p, name, sizeof(name) - 1);
    if (n < 0) return 0;
    name[n] = 0;
    return !strcmp(name, RX_TARGET_PATH);
}

static void reset(const char *why, int fd) {
    int e = errno;
    __android_log_print(ANDROID_LOG_INFO, "FullSplitBounded", "reset=%s fd=%d", why, fd);
    rx_reset(&state);
    state.generation++;
    errno = e;
}

ssize_t write(int fd, const void *b, size_t n) {
    struct stat s;
    int saved = errno;
    if (entered) return syscall(SYS_write, fd, b, n);
    if (!target_fd(fd, &s)) { errno = saved; return syscall(SYS_write, fd, b, n); }
    entered = 1;
    if (owner_fd != fd || device != s.st_dev || inode != s.st_ino || rdevice != s.st_rdev) {
        reset("target-generation", fd);
        owner_fd = fd;
        device = s.st_dev;
        inode = s.st_ino;
        rdevice = s.st_rdev;
    }
    if (n == 62 && ((const unsigned char *)b)[0] == 1 && ((const unsigned char *)b)[61] <= 2) {
        const unsigned char *in = (const unsigned char *)b;
        __android_log_print(ANDROID_LOG_INFO, "FullSplitV3", "in c=%d s0=%02x/%d/%d/%d s1=%02x/%d/%d/%d",
            in[61], in[1], in[2], in[3] | (in[4] << 8), in[5] | (in[6] << 8),
            in[7], in[8], in[9] | (in[10] << 8), in[11] | (in[12] << 8));
    }
    ssize_t r = rx_send(&state, 1, fd, (const unsigned char *)b, n, raw_write);
    if (n == 62 && ((const unsigned char *)b)[0] == 1 && ((const unsigned char *)b)[61] <= 2) {
        const unsigned char *o = state.out;
        __android_log_print(ANDROID_LOG_INFO, "FullSplitV3", "out c=%d s0=%02x/%d/%d/%d s1=%02x/%d/%d/%d",
            o[61], o[1], o[2], o[3] | (o[4] << 8), o[5] | (o[6] << 8),
            o[7], o[8], o[9] | (o[10] << 8), o[11] | (o[12] << 8));
    }
    int e = errno;
    entered = 0;
    errno = e;
    return r;
}

ssize_t __write_chk(int fd, const void *b, size_t n, size_t size) {
    if (n > size) abort();
    return write(fd, b, n);
}

int close(int fd) {
    struct stat s;
    if (!target_fd(fd, &s)) return syscall(SYS_close, fd);
    if (entered) { errno = EDEADLK; return -1; }
    entered = 1;
    int r = syscall(SYS_close, fd), e = errno;
    if (!r) { reset("target-close", fd); owner_fd = -1; }
    entered = 0;
    errno = e;
    return r;
}

int open(const char *p, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT || (flags & O_TMPFILE) == O_TMPFILE) {
        va_list a; va_start(a, flags); mode = va_arg(a, int); va_end(a);
    }
    if (strcmp(p, RX_TARGET_PATH)) return syscall(SYS_openat, AT_FDCWD, p, flags, mode);
    if (entered) { errno = EDEADLK; return -1; }
    entered = 1;
    int r = syscall(SYS_openat, AT_FDCWD, p, flags, mode), e = errno;
    if (r >= 0) { reset("target-open", r); owner_fd = -1; }
    entered = 0;
    errno = e;
    return r;
}

int open64(const char *p, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT || (flags & O_TMPFILE) == O_TMPFILE) {
        va_list a; va_start(a, flags); mode = va_arg(a, int); va_end(a);
    }
    return open(p, flags, mode);
}

int __open_2(const char *p, int flags) {
    if (flags & O_CREAT || (flags & O_TMPFILE) == O_TMPFILE) abort();
    return open(p, flags);
}

static int duplicate(int a, int b, int flags, int is2) {
    struct stat s, t;
    int affected = target_fd(a, &s) || target_fd(b, &t);
    if (entered && affected) { errno = EDEADLK; return -1; }
    int was = entered;
    if (affected) entered = 1;
    int r = (is2 && a == b) ? (fcntl(a, F_GETFD) >= 0 ? a : -1) : syscall(SYS_dup3, a, b, flags);
    int e = errno;
    if (affected) {
        if (r >= 0 && a != b) { reset("target-dup", b); owner_fd = -1; }
        entered = was;
    }
    errno = e;
    return r;
}

int dup2(int a, int b) { return duplicate(a, b, 0, 1); }
int dup3(int a, int b, int flags) { return duplicate(a, b, flags, 0); }
