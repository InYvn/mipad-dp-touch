/* Host-side test for the v4 interposer's non-target pass-through path.
 *
 * The interposer only takes over descriptors that are the RX target
 * (/dev/hidg1). On a normal host that device is absent, so target_fd() is
 * always false and every call must be forwarded byte for byte with the
 * caller's errno preserved. That is the path the pen interface and every
 * other descriptor take on the device.
 *
 * Build/run (host only; the DSO itself is built for arm64 separately):
 *   cc -Wall -Wextra -O2 -I tablet/native/test -o test-interposer-v4 \
 *      tablet/native/test-interposer-v4.c
 *   ./test-interposer-v4
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Linux-only flag read by open(); this test never opens O_TMPFILE. */
#ifndef O_TMPFILE
#define O_TMPFILE 0x410000
#endif

#include "fullsplit-interposer-v4.c"

static int checks, fails;

static void ok(int cond, const char *what) {
    checks++;
    if (!cond) {
        fails++;
        printf("      FAIL  %s\n", what);
    }
}

int main(void) {
    unsigned char frame[62], back[128];
    char path[512];
    const char *tmp = getenv("TMPDIR");

    if (!tmp || !*tmp) tmp = "/tmp";
    snprintf(path, sizeof(path), "%s/fs-test-interposer-v4.bin", tmp);

    printf("fullsplit v4 interposer host tests (tablet/native/test-interposer-v4.c)\n\n");

    /* I1: a 62-byte frame on a non-target descriptor is forwarded untouched. */
    printf("--- I1  write() on a non-target fd is byte-exact pass-through\n");
    int p[2];
    ok(pipe(p) == 0, "pipe created");
    for (int i = 0; i < 62; i++) frame[i] = (unsigned char)(i * 7 + 1);
    frame[0] = 1;
    frame[61] = 1;
    errno = ERANGE;
    ssize_t rc = write(p[1], frame, 62);
    int e = errno;
    printf("      rc=%zd errno=%d\n", rc, e);
    ok(rc == 62, "returns the kernel byte count");
    ok(e == ERANGE, "caller errno preserved");
    ssize_t n = read(p[0], back, sizeof(back));
    ok(n == 62 && !memcmp(back, frame, 62), "bytes forwarded byte-exact");

    /* I2: zero-length writes stay zero-length pass-through. */
    rc = write(p[1], frame, 0);
    printf("      zero-length rc=%zd\n", rc);
    ok(rc == 0, "zero-length write returns 0");
    close(p[1]);
    close(p[0]);

    /* I3: a regular file (open/write/__write_chk/close) is not the target either. */
    printf("--- I3  open/write/__write_chk/close on a regular file pass through\n");
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    printf("      open fd=%d errno=%d\n", fd, errno);
    ok(fd >= 0, "open forwarded");
    if (fd >= 0) {
        frame[0] = 3; /* the pen report rides this path on the device */
        rc = write(fd, frame, 62);
        ok(rc == 62, "write forwarded");
        rc = __write_chk(fd, frame, 62, 62);
        ok(rc == 62, "__write_chk forwarded");
        ok(close(fd) == 0, "close forwarded");
        int rfd = open(path, O_RDONLY);
        ok(rfd >= 0, "reopen forwarded");
        if (rfd >= 0) {
            n = read(rfd, back, sizeof(back));
            printf("      read=%zd\n", n);
            ok(n == 124, "two frames reached the file");
            ok(n == 124 && !memcmp(back, frame, 62) && !memcmp(back + 62, frame, 62),
               "file contents match the frames byte for byte");
            close(rfd);
        }
        unlink(path);
    }

    /* I4: none of that touched the gate state. */
    printf("--- I4  gate state is untouched by non-target calls\n");
    int slots_free = 1, ids_free = 1;
    for (int i = 0; i < MAXC; i++) if (state.slot_id[i] != FREE_SLOT) slots_free = 0;
    for (int i = 0; i < 256; i++) if (state.id_slot[i] != -1) ids_free = 0;
    printf("      fd=%d slots_free=%d ids_free=%d generation=%llu\n",
           state.fd, slots_free, ids_free, (unsigned long long)state.generation);
    ok(slots_free, "no slot was claimed");
    ok(ids_free, "no contact id was mapped");
    ok(state.fd == -1, "no target fd recorded");

    printf("\n%d checks, %d failed\n", checks, fails);
    return fails ? 1 : 0;
}
