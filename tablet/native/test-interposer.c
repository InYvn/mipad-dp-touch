/* Host-side test for the LD_PRELOAD interposer's non-target pass-through path.
 *
 * fullsplit-bounded-interposer.c interposes write/open/close/dup2/dup3 and only takes
 * over descriptors that are the RX target (/dev/hidg1 by default). On a normal host
 * that device - and /proc/self/fd - are absent, so target_fd() is always false and
 * every call must be forwarded byte for byte with the caller's errno preserved. This
 * is the path the pen interface and all non-target descriptors take on the device.
 *
 * Build/run (host only; the DSO itself is built by tablet/build_dso.sh):
 *
 *   cc -Wall -Wextra -O2 -Wno-deprecated-declarations -I tablet/native/test \
 *      -o test-interposer tablet/native/test-interposer.c
 *   ./test-interposer
 *
 * -Wno-deprecated-declarations is only needed on macOS (syscall(2) is deprecated
 * there). tablet/native/test/android/log.h stands in for the NDK header.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Linux-only flag read by open() in the interposer; this test never opens O_TMPFILE. */
#ifndef O_TMPFILE
#define O_TMPFILE 0x410000
#endif

#include "fullsplit-bounded-interposer.c"

static int checks, fails;

static void ok(int cond, const char *what)
{
	checks++;
	if (!cond) {
		fails++;
		printf("      FAIL  %s\n", what);
	}
}

int main(void)
{
	unsigned char frame[62], back[128];
	char path[512];
	const char *tmp = getenv("TMPDIR");

	if (!tmp || !*tmp)
		tmp = "/tmp";
	snprintf(path, sizeof(path), "%s/fs-test-interposer.bin", tmp);

	printf("fullsplit interposer host tests (tablet/native/test-interposer.c)\n\n");

	/* I1: a 62-byte frame on a non-target descriptor is forwarded untouched. */
	printf("--- I1  write() on a non-target fd is byte-exact pass-through\n");
	int p[2];
	ok(pipe(p) == 0, "pipe created");
	for (int i = 0; i < 62; i++)
		frame[i] = (unsigned char)(i * 7 + 1);
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
	}

	/* I4: none of that touched the gate state. */
	printf("--- I4  gate state is untouched by non-target calls\n");
	printf("      poison=%d pending=%d next=%d total=%d active=%d owner_fd=%d seq=%lu/%lu\n",
	       state.poison, state.pending, state.next, state.total, state.active, owner_fd,
	       sequence, input_sequence);
	ok(state.poison == 0, "poison untouched");
	ok(state.pending == 0 && state.next == 0 && state.total == 0, "transaction untouched");
	ok(state.active == 0, "contact state untouched");
	ok(owner_fd == -1, "no target fd was claimed");
	ok(sequence == 0 && input_sequence == 0, "no target write was counted");

	printf("\n%d checks, %d failure(s)\n", checks, fails);
	return fails ? 1 : 0;
}
