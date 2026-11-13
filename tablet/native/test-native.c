/* Host-side tests for the production native-touch interposer (module 2.1.10).
 *
 * Build/run (host only; the DSO itself is built by tablet/build_dso.sh with
 * MIPAD_DSO_NATIVE=1):
 *
 *   cc -Wall -Wextra -O2 -Wno-deprecated-declarations \
 *      -I tablet/native -I tablet/native/test \
 *      -DFULLSPLIT_NATIVE -DFS_TEST_NATIVE -o test-native tablet/native/test-native.c
 *   ./test-native 2> test-native.log
 *
 * The fake seams declared in fullsplit-native.h replace the two syscalls the
 * host cannot perform (readlink on /proc/self/fd and write to configfs). The
 * real write()/ioctl() entry points of the interposer are exercised, so the
 * tests cover exactly the production dispatch path.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef O_TMPFILE
#define O_TMPFILE 0x410000
#endif

#include "fullsplit-bounded-interposer.c"

#define FAKE_MAX 16
struct fake_link {
	int fd;
	char path[256];
};
static struct fake_link fake_links[FAKE_MAX];
static int fake_link_count;

struct fake_write {
	int fd;
	unsigned char data[1024];
	size_t len;
};
static struct fake_write fake_writes[8];
static int fake_write_count;
static int fake_write_fail_once;
static int fake_write_fail_errno = EIO;

struct fake_ioctl {
	int fd;
	int req;
	void *arg;
};
static struct fake_ioctl fake_ioctls[16];
static int fake_ioctl_count;

ssize_t FS_TEST_NATIVE_READLINK(const char *path, char *buf, size_t n)
{
	int fd = -1;
	if (sscanf(path, "/proc/self/fd/%d", &fd) != 1) {
		errno = ENOENT;
		return -1;
	}
	for (int i = 0; i < fake_link_count; i++) {
		if (fake_links[i].fd == fd) {
			size_t len = strlen(fake_links[i].path);
			if (len >= n) {
				len = n - 1;
			}
			memcpy(buf, fake_links[i].path, len);
			buf[len] = 0;
			return (ssize_t)len;
		}
	}
	errno = ENOENT;
	return -1;
}

ssize_t FS_TEST_NATIVE_WRITE(int fd, const void *buf, size_t n)
{
	if (fake_write_fail_once) {
		fake_write_fail_once = 0;
		errno = fake_write_fail_errno;
		return -1;
	}
	if (fake_write_count < (int)(sizeof(fake_writes) / sizeof(fake_writes[0]))) {
		struct fake_write *w = &fake_writes[fake_write_count];
		w->fd = fd;
		w->len = n < sizeof(w->data) ? n : sizeof(w->data);
		memcpy(w->data, buf, w->len);
	}
	fake_write_count++;
	return (ssize_t)n;
}

static int fake_ioctl_rc;
static int fake_ioctl_errno;

int FS_TEST_NATIVE_IOCTL(int fd, int req, void *arg)
{
	if (fake_ioctl_count < (int)(sizeof(fake_ioctls) / sizeof(fake_ioctls[0]))) {
		fake_ioctls[fake_ioctl_count].fd = fd;
		fake_ioctls[fake_ioctl_count].req = req;
		fake_ioctls[fake_ioctl_count].arg = arg;
	}
	fake_ioctl_count++;
	errno = fake_ioctl_errno;
	return fake_ioctl_rc;
}

static int checks, fails;

static void ok(int cond, const char *what)
{
	checks++;
	if (!cond) {
		fails++;
		printf("      FAIL  %s\n", what);
	}
}

static void reset_fakes(void)
{
	fake_link_count = 0;
	fake_write_count = 0;
	fake_ioctl_count = 0;
	fake_write_fail_once = 0;
	memset(fake_links, 0, sizeof(fake_links));
	memset(fake_writes, 0, sizeof(fake_writes));
	memset(fake_ioctls, 0, sizeof(fake_ioctls));
}

static void map_fd(int fd, const char *path)
{
	if (fake_link_count < FAKE_MAX) {
		fake_links[fake_link_count].fd = fd;
		snprintf(fake_links[fake_link_count].path, sizeof(fake_links[0].path), "%s", path);
		fake_link_count++;
	}
}

static int open_temp(const char *tag)
{
	char path[256];
	const char *tmp = getenv("TMPDIR");
	int fd;
	if (!tmp || !*tmp) {
		tmp = "/tmp";
	}
	snprintf(path, sizeof(path), "%s/fs-native-%s.bin", tmp, tag);
	fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
	return fd;
}

static void read_temp(int fd, unsigned char *buf, size_t n)
{
	ssize_t r;
	lseek(fd, 0, SEEK_SET);
	r = read(fd, buf, n);
	if (r < 0) {
		r = 0;
	}
	if ((size_t)r < n) {
		memset(buf + r, 0, n - (size_t)r);
	}
}

static void lab_reset(const char *allow_grab)
{
	if (allow_grab) {
		setenv("MIPAD_NATIVE_ALLOW_GRAB", allow_grab, 1);
	} else {
		unsetenv("MIPAD_NATIVE_ALLOW_GRAB");
	}
	reset_fakes();
	native_reset_for_test();
}

int main(void)
{
	const char *hid1 = "/config/usb_gadget/g1/functions/hid.usb1/report_desc";
	const char *hid0 = "/config/usb_gadget/g1/functions/hid.usb0/report_desc";
	unsigned char unknown[914];
	ssize_t rc;
	int one = 1;
	int e;

	printf("fullsplit native-touch production interposer tests (2.1.10)\n\n");

	/* ---------------------------------------------------------------- N0 */
	printf("--- N0 native init defaults: V4 descriptor + no-grab on, allow-grab off\n");
	lab_reset(NULL);
	ok(native_allow_grab == 0, "allow-grab defaults off");
	ok(NATIVE_DESC_V4_LEN == 914 && NATIVE_DESC_FACTORY_LEN == 914,
	   "factory and V4 descriptors are 914 bytes");
	ok(memcmp(NATIVE_DESC_V4, NATIVE_DESC_FACTORY, 914) != 0,
	   "V4 descriptor differs from the factory descriptor");
	{
		static const unsigned char count_item[] = {
			0x09, 0x54, 0x15, 0x00, 0x25, 0x0a, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02
		};
		int factory_count = -1, v4_count = -1, diff = 0, rid2 = -1;
		for (int i = 0; i + 1 < 914; i++) {
			if (!memcmp(NATIVE_DESC_FACTORY + i, count_item, sizeof(count_item))) {
				factory_count = i;
			}
			if (!memcmp(NATIVE_DESC_V4 + i, count_item, sizeof(count_item))) {
				v4_count = i;
			}
			if (NATIVE_DESC_FACTORY[i] == 0x85 && NATIVE_DESC_FACTORY[i + 1] == 0x02) {
				rid2 = i;
			}
		}
		for (int i = 0; i < 914; i++) {
			diff += NATIVE_DESC_V4[i] != NATIVE_DESC_FACTORY[i];
		}
		ok(diff == 2, "V4 differs from the factory descriptor in exactly 2 bytes");
		ok(NATIVE_DESC_V4[3] == 0x05, "V4 application usage is Touch Pad (09 05)");
		ok(rid2 > 0 && NATIVE_DESC_V4[rid2 + 1] == 0x01,
		   "V4 moves the feature report ID 2 to report ID 1");
		ok(factory_count > 0 && v4_count == factory_count,
		   "V4 keeps the factory frame layout (Contact Count stays last)");
	}

	/* ---------------------------------------------------------------- N1 */
	printf("--- N1 factory descriptor write is replaced by V4; HAL sees its own count\n");
	lab_reset(NULL);
	map_fd(7, hid1);
	errno = ERANGE;
	rc = write(7, NATIVE_DESC_FACTORY, NATIVE_DESC_FACTORY_LEN);
	e = errno;
	printf("      rc=%zd errno=%d\n", rc, e);
	ok(rc == (ssize_t)NATIVE_DESC_FACTORY_LEN, "write returns the HAL's byte count");
	ok(e == ERANGE, "caller errno preserved");
	ok(fake_write_count == 1, "exactly one kernel write");
	ok(fake_writes[0].len == 914 && !memcmp(fake_writes[0].data, NATIVE_DESC_V4, 914),
	   "the kernel receives the V4 descriptor bytes");
	ok(native_desc_replaced == 1, "replacement recorded");
	ok(state.poison == 0 && state.pending == 0 && state.total == 0,
	   "gate state untouched by the descriptor write");

	/* ---------------------------------------------------------------- N2 */
	printf("--- N2 non-touch descriptor / split / unexpected writes fail closed\n");
	lab_reset(NULL);
	{
		unsigned char back[1024];
		int tfd = open_temp("hid0");
		map_fd(tfd, hid0);
		rc = write(tfd, NATIVE_DESC_FACTORY, NATIVE_DESC_FACTORY_LEN);
		read_temp(tfd, back, sizeof(back));
		ok(rc == (ssize_t)NATIVE_DESC_FACTORY_LEN && fake_write_count == 0 &&
		   !memcmp(back, NATIVE_DESC_FACTORY, 914),
		   "hid.usb0 descriptor write forwarded untouched");
		close(tfd);
	}
	lab_reset(NULL);
	{
		unsigned char back[1024];
		int tfd = open_temp("split");
		map_fd(tfd, hid1);
		rc = write(tfd, NATIVE_DESC_FACTORY, 512);
		read_temp(tfd, back, sizeof(back));
		ok(rc == 512 && fake_write_count == 0 &&
		   !memcmp(back, NATIVE_DESC_FACTORY, 512),
		   "split descriptor write forwarded untouched");
		ok(native_desc_replaced == 0, "no replacement on a split write");
		close(tfd);
	}
	lab_reset(NULL);
	{
		unsigned char back[1024];
		int tfd = open_temp("unknown");
		map_fd(tfd, hid1);
		memset(unknown, 0xAA, sizeof(unknown));
		rc = write(tfd, unknown, sizeof(unknown));
		read_temp(tfd, back, sizeof(back));
		ok(rc == 914 && fake_write_count == 0 && !memcmp(back, unknown, 914),
		   "unknown descriptor content forwarded untouched");
		ok(native_error_logged == 1, "unexpected descriptor logged once");
		close(tfd);
	}
	{
		unsigned char frame[62];
		ssize_t out = -1;
		memset(frame, 0x11, sizeof(frame));
		frame[0] = 1;
		ok(native_desc_try_write(10, frame, sizeof(frame), &out) == 0,
		   "62-byte frames never enter the descriptor path");
	}

	/* ---------------------------------------------------------------- N3 */
	printf("--- N3 failed V4 write falls back to the factory descriptor\n");
	lab_reset(NULL);
	map_fd(11, hid1);
	fake_write_fail_once = 1;
	rc = write(11, NATIVE_DESC_FACTORY, NATIVE_DESC_FACTORY_LEN);
	printf("      rc=%zd writes=%d errno=%d\n", rc, fake_write_count, errno);
	ok(rc == (ssize_t)NATIVE_DESC_FACTORY_LEN, "HAL still sees a successful full write");
	ok(fake_write_count == 1 && !memcmp(fake_writes[0].data, NATIVE_DESC_FACTORY, 914),
	   "fallback wrote the HAL's original factory bytes");
	ok(native_desc_replaced == 0 && native_error_logged == 1,
	   "replacement not claimed and the failure logged once");

	/* ---------------------------------------------------------------- N4 */
	printf("--- N4 no-grab default suppresses exactly the reviewed evdev nodes\n");
	lab_reset(NULL);
	map_fd(20, "/dev/input/event6");
	map_fd(21, "/dev/input/event9");
	map_fd(22, "/dev/input/event11");
	map_fd(23, "/dev/input/event7");
	map_fd(24, "/dev/input/event8");
	map_fd(25, "/dev/input/event5");
	map_fd(26, "/dev/hidg1");
	for (int fd = 20; fd <= 24; fd++) {
		rc = ioctl(fd, (int)EVIOCGRAB, &one);
		ok(rc == 0, "reviewed device grab swallowed");
	}
	ok(fake_ioctl_count == 0, "no grab reached the kernel for the five nodes");
	rc = ioctl(20, (int)EVIOCREVOKE, &one);
	ok(rc == 0 && fake_ioctl_count == 0, "revoke on a reviewed device swallowed too");
	fake_ioctl_rc = 0;
	fake_ioctl_errno = 0;
	rc = ioctl(25, (int)EVIOCGRAB, &one);
	ok(rc == 0 && fake_ioctl_count == 1, "non-reviewed event node grab reaches the kernel");
	rc = ioctl(26, (int)EVIOCGRAB, &one);
	ok(rc == 0 && fake_ioctl_count == 2, "hidg grab reaches the kernel");
	rc = ioctl(20, 0x80084502, &one); /* EVIOCGID, not a grab request */
	ok(rc == 0 && fake_ioctl_count == 3, "ordinary ioctl still forwarded");
	fake_ioctl_rc = -1;
	fake_ioctl_errno = EINVAL;
	errno = 0;
	rc = ioctl(25, (int)EVIOCGRAB, &one);
	ok(rc == -1 && errno == EINVAL, "forwarded failure rc/errno unchanged");
	ok(native_grab_logged[0] == 1, "suppression logged once for event6");

	/* ---------------------------------------------------------------- N5 */
	printf("--- N5 allow-grab escape restores the kernel grab (diagnostic only)\n");
	lab_reset("1");
	ok(native_allow_grab == 1, "allow-grab resolved from the environment");
	map_fd(30, "/dev/input/event6");
	map_fd(31, "/dev/input/event11");
	fake_ioctl_rc = 0;
	fake_ioctl_errno = 0;
	ioctl(30, (int)EVIOCGRAB, &one);
	ioctl(31, (int)EVIOCGRAB, &one);
	ok(fake_ioctl_count == 2, "allow-grab forwards the kernel grab for reviewed nodes");
	ok(native_grab_logged[0] == 1, "grab-allowed logged for the reviewed node");

	/* ---------------------------------------------------------------- N6 */
	printf("--- N6 environment parsing rejects non-\"1\" values\n");
	lab_reset("true");
	ok(native_allow_grab == 0, "MIPAD_NATIVE_ALLOW_GRAB only accepts 1");

	printf("\n%d checks, %d failure(s)\n", checks, fails);
	return fails ? 1 : 0;
}
