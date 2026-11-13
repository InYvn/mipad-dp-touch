/* Host-side tests for the experimental native-touch lab interposer.
 *
 * Build/run (host only; the DSO itself is built by tablet/build_dso.sh with
 * MIPAD_DSO_LAB=1):
 *
 *   cc -Wall -Wextra -O2 -Wno-deprecated-declarations \
 *      -I tablet/native -I tablet/native/test \
 *      -DFULLSPLIT_LAB -DFS_TEST_LAB \
 *      -o test-lab tablet/native/test-lab.c
 *   ./test-lab 2> test-lab.log
 *
 * The tests never touch a real hidg/evdev/configfs node. open()/write()/
 * ioctl() paths are exercised through the real interposer entry points using
 * fake descriptors seeded into the lab fd table, and every syscall the host
 * cannot perform is replaced by the FS_TEST_LAB_* hooks declared in
 * fullsplit-lab.h. The stderr log (android/log.h stub) is inspected by
 * dso/run-tests.sh for the NativeHidLab classification lines.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Linux-only flags read by open()/openat(); the host tests never pass them. */
#ifndef O_TMPFILE
#define O_TMPFILE 0x410000
#endif

#include "fullsplit-bounded-interposer.c"

/* --- fake syscalls ------------------------------------------------------ */
struct fake_ioctl {
	int fd;
	int req;
	void *arg;
};
static struct fake_ioctl ioctl_calls[64];
static int ioctl_call_count;
static int fake_ioctl_rc;
static int fake_ioctl_errno;

int FS_TEST_LAB_IOCTL(int fd, int req, void *arg)
{
	if (ioctl_call_count < (int)(sizeof(ioctl_calls) / sizeof(ioctl_calls[0]))) {
		ioctl_calls[ioctl_call_count].fd = fd;
		ioctl_calls[ioctl_call_count].req = req;
		ioctl_calls[ioctl_call_count].arg = arg;
	}
	ioctl_call_count++;
	errno = fake_ioctl_errno;
	return fake_ioctl_rc;
}

static char fake_readlink[LAB_PATH_MAX];

ssize_t FS_TEST_LAB_READLINK(const char *path, char *buf, size_t n)
{
	(void)path;
	if (fake_readlink[0]) {
		size_t len = strlen(fake_readlink);
		if (len >= n) {
			len = n - 1;
		}
		memcpy(buf, fake_readlink, len);
		buf[len] = 0;
		return (ssize_t)len;
	}
	errno = ENOENT; /* host has no /proc/self/fd: the fd table must carry it */
	return -1;
}

struct fake_write {
	int fd;
	unsigned char data[1024];
	size_t len;
};
static struct fake_write write_calls[8];
static int write_call_count;

ssize_t FS_TEST_LAB_WRITE(int fd, const void *buf, size_t n)
{
	struct fake_write *w = NULL;
	if (write_call_count < (int)(sizeof(write_calls) / sizeof(write_calls[0]))) {
		w = &write_calls[write_call_count];
		w->fd = fd;
		w->len = n < sizeof(w->data) ? n : sizeof(w->data);
		memcpy(w->data, buf, w->len);
	}
	write_call_count++;
	return (ssize_t)n; /* pretend the configfs/hidg write consumed everything */
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

/* macOS may not declare memmem; a tiny local search keeps the test portable. */
static void *find_bytes(const void *hay, size_t haylen, const void *needle, size_t needlen)
{
	const unsigned char *h = hay, *n = needle;
	size_t i;
	if (!needlen || haylen < needlen) {
		return NULL;
	}
	for (i = 0; i + needlen <= haylen; i++) {
		if (!memcmp(h + i, n, needlen)) {
			return (void *)(h + i);
		}
	}
	return NULL;
}

static void reset_fakes(void)
{
	ioctl_call_count = 0;
	write_call_count = 0;
	memset(ioctl_calls, 0, sizeof(ioctl_calls));
	memset(write_calls, 0, sizeof(write_calls));
}

static void reset_lab(const char *variant, int nograb, const char *dev)
{
	if (variant) {
		setenv("MIPAD_LAB_VARIANT", variant, 1);
	} else {
		unsetenv("MIPAD_LAB_VARIANT");
	}
	setenv("MIPAD_NATIVE_LAB", "1", 1);
	if (nograb) {
		setenv("MIPAD_LAB_NOGRAB", "1", 1);
	} else {
		unsetenv("MIPAD_LAB_NOGRAB");
	}
	if (dev) {
		setenv("MIPAD_LAB_NOGRAB_DEV", dev, 1);
	} else {
		unsetenv("MIPAD_LAB_NOGRAB_DEV");
	}
	lab_fd_del_all_for_test();
	fake_readlink[0] = 0;
	lab_init();
	reset_fakes();
}

int main(void)
{
	char path[512];
	const char *tmp = getenv("TMPDIR");
	unsigned char frame[62], mapped[62];
	unsigned char orig[62];
	int one = 1;
	ssize_t rc;
	int fd, e;

	if (!tmp || !*tmp) {
		tmp = "/tmp";
	}
	snprintf(path, sizeof(path), "%s/fs-test-lab.bin", tmp);

	printf("fullsplit lab interposer host tests (tablet/native/test-lab.c)\n\n");

	/* ---------------------------------------------------------------- L0 */
	printf("--- L0  MIPAD_NATIVE_LAB unset: every hook is a silent pass-through\n");
	unsetenv("MIPAD_NATIVE_LAB");
	reset_fakes();
	lab_init();
	ok(lab_enabled == 0, "lab disabled");
	fake_ioctl_rc = 7;
	fake_ioctl_errno = 0;
	rc = ioctl(5, EVIOCGRAB, &one);
	ok(rc == 7 && ioctl_call_count == 1, "ioctl forwarded when lab is off");
	fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	ok(fd >= 0, "open of a regular file still works when lab is off");
	ok(lab_fd_count_for_test() == 0, "no fd table entries when lab is off");
	if (fd >= 0) {
		ok(write(fd, "x", 1) == 1, "write forwarded when lab is off");
		close(fd);
	}

	/* ---------------------------------------------------------------- L1 */
	printf("--- L1  probe: open/openat fd table, non-target pass-through, close\n");
	reset_lab(NULL, 0, NULL);
	ok(lab_enabled == 1, "lab enabled");
	fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	printf("      open fd=%d errno=%d\n", fd, errno);
	ok(fd >= 0, "open forwarded");
	{
		char got[LAB_PATH_MAX];
		ok(lab_fd_lookup(fd, got, sizeof(got)) && !strcmp(got, path),
		   "open recorded fd->path in the lab table");
	}
	ok(state.poison == 0 && state.pending == 0 && state.total == 0,
	   "gate state untouched by a probed open");
	close(fd);
	{
		char got[LAB_PATH_MAX];
		ok(!lab_fd_lookup(fd, got, sizeof(got)), "close removed the fd table entry");
	}
	{
		int dfd = openat(AT_FDCWD, path, O_WRONLY | O_APPEND, 0644);
		char got[LAB_PATH_MAX];
		printf("      openat dfd=%d errno=%d\n", dfd, errno);
		ok(dfd >= 0, "openat forwarded");
		ok(lab_fd_lookup(dfd, got, sizeof(got)) && !strcmp(got, path),
		   "openat recorded fd->path");
		close(dfd);
	}

	/* --------------------------------------------------------------- L1b */
	printf("--- L1b stale fd table entries are corrected from /proc/self/fd (T4 grab mislabel)\n");
	reset_lab(NULL, 0, NULL);
	lab_fd_put(77, "/dev/input"); /* stale: closedir() bypassed our close() */
	lab_fd_put(78, "/dev/input/event6");
	{
		char got[LAB_PATH_MAX];
		snprintf(fake_readlink, sizeof(fake_readlink), "/dev/input/event9");
		ok(lab_fd_path(77, got, sizeof(got)) && !strcmp(got, "/dev/input/event9"),
		   "readlink wins over the stale table entry");
		ok(lab_fd_lookup(77, got, sizeof(got)) && !strcmp(got, "/dev/input/event9"),
		   "table entry refreshed after the readlink");
	}
	reset_lab(NULL, 1, "event9");
	lab_fd_put(77, "/dev/input");
	snprintf(fake_readlink, sizeof(fake_readlink), "/dev/input/event9");
	rc = ioctl(77, EVIOCGRAB, &one);
	ok(rc == 0 && ioctl_call_count == 0 && lab_grab_suppressed == 1,
	   "no-grab matches the real device even when the table entry is stale");
	fake_readlink[0] = 0;
	reset_lab(NULL, 0, NULL);
	lab_fd_put(77, "/dev/input");
	{
		char got[LAB_PATH_MAX];
		ok(lab_fd_path(77, got, sizeof(got)) && !strcmp(got, "/dev/input"),
		   "table fallback still used when readlink fails (host seam)");
	}

	/* ---------------------------------------------------------------- L2 */
	printf("--- L2  ioctl pass-through + classification counters\n");
	reset_lab(NULL, 0, NULL);
	lab_fd_put(41, "/dev/input/event11");
	lab_fd_put(42, "/dev/hidg1");
	fake_ioctl_rc = 0;
	fake_ioctl_errno = 0;
	rc = ioctl(41, EVIOCGRAB, &one);
	ok(rc == 0 && ioctl_call_count == 1, "EVIOCGRAB forwarded when no-grab is off");
	ok(ioctl_calls[0].fd == 41 && ioctl_calls[0].req == (int)EVIOCGRAB &&
	   ioctl_calls[0].arg == &one, "ioctl vararg forwarded unchanged");
	ok(lab_grab_seen == 1, "EVIOCGRAB classified");
	rc = ioctl(41, EVIOCREVOKE, &one);
	ok(rc == 0 && ioctl_call_count == 2, "EVIOCREVOKE forwarded");
	ok(lab_grab_seen == 2, "EVIOCREVOKE classified");
	rc = ioctl(41, 0x40084501, &one);
	ok(rc == 0 && ioctl_call_count == 3, "other request forwarded");
	fake_ioctl_rc = -1;
	fake_ioctl_errno = EINVAL;
	errno = 0;
	rc = ioctl(41, EVIOCGRAB, &one);
	e = errno;
	ok(rc == -1 && e == EINVAL, "failure rc/errno forwarded unchanged");

	/* ---------------------------------------------------------------- L3 */
	printf("--- L3  no-grab: only the whitelisted evdev node is suppressed\n");
	reset_lab(NULL, 1, NULL);
	lab_fd_put(41, "/dev/input/event11");
	lab_fd_put(43, "/dev/input/event6");
	lab_fd_put(42, "/dev/hidg1");
	fake_ioctl_rc = 0;
	fake_ioctl_errno = 0;
	rc = ioctl(41, EVIOCGRAB, &one);
	ok(rc == 0 && ioctl_call_count == 0, "EVIOCGRAB on event11 swallowed (not forwarded)");
	ok(lab_grab_suppressed == 1, "grab-suppressed counter");
	rc = ioctl(41, EVIOCREVOKE, &one);
	ok(rc == 0 && ioctl_call_count == 0, "EVIOCREVOKE on event11 swallowed");
	rc = ioctl(41, 0x40084501, &one);
	ok(rc == 0 && ioctl_call_count == 1, "non-grab ioctl on event11 still forwarded");
	rc = ioctl(43, EVIOCGRAB, &one);
	ok(rc == 0 && ioctl_call_count == 2, "EVIOCGRAB on event6 forwarded (not whitelisted)");
	rc = ioctl(42, EVIOCGRAB, &one);
	ok(rc == 0 && ioctl_call_count == 3, "EVIOCGRAB on hidg1 forwarded");
	ok(lab_nograb_match("/dev/input/event11") == 1, "basename whitelist matches");
	ok(lab_nograb_match("/dev/input/event6") == 0, "other event node not matched");

	/* ---------------------------------------------------------------- L4 */
	printf("--- L4  no-grab whitelist override (path and comma list)\n");
	reset_lab(NULL, 1, "/dev/input/event6,event7");
	lab_fd_put(41, "/dev/input/event11");
	lab_fd_put(43, "/dev/input/event6");
	lab_fd_put(44, "/dev/input/event7");
	rc = ioctl(41, EVIOCGRAB, &one);
	ok(rc == 0 && ioctl_call_count == 1, "event11 forwarded when whitelist is event6/7");
	rc = ioctl(43, EVIOCGRAB, &one);
	ok(ioctl_call_count == 1 && lab_grab_suppressed == 1, "full path whitelist matched");
	rc = ioctl(44, EVIOCGRAB, &one);
	ok(ioctl_call_count == 1 && lab_grab_suppressed == 2, "comma list basename matched");

	/* --------------------------------------------------------------- L4b */
	printf("--- L4b T4-proven whitelist: all four stock-HAL grabbed evdev nodes\n");
	reset_lab(NULL, 1, "event6,event11,event7,event8");
	lab_fd_put(41, "/dev/input/event11");
	lab_fd_put(43, "/dev/input/event6");
	lab_fd_put(45, "/dev/input/event7");
	lab_fd_put(46, "/dev/input/event8");
	lab_fd_put(47, "/dev/input/event5");
	rc = ioctl(41, EVIOCGRAB, &one);
	ok(rc == 0 && ioctl_call_count == 0, "event11 grab suppressed (T4 list)");
	rc = ioctl(43, EVIOCGRAB, &one);
	ok(ioctl_call_count == 0, "event6 (Android touchscreen) grab suppressed (T4 list)");
	rc = ioctl(45, EVIOCGRAB, &one);
	rc = ioctl(46, EVIOCGRAB, &one);
	ok(ioctl_call_count == 0 && lab_grab_suppressed == 4, "event7/event8 grab suppressed (T4 list)");
	rc = ioctl(47, EVIOCGRAB, &one);
	ok(ioctl_call_count == 1, "event5 not in the T4 list is forwarded");
	rc = ioctl(43, EVIOCREVOKE, &one);
	ok(ioctl_call_count == 1 && lab_grab_suppressed == 5, "revoke on event6 suppressed too");

	/* --------------------------------------------------------------- L4c */
	printf("--- L4c long whitelist: no per-entry limit, only the 160-byte buffer\n");
	{
		char list[150];
		int used = 0;
		for (int i = 1; i <= 12 && used < 130; i++) {
			used += snprintf(list + used, sizeof(list) - (size_t)used,
					 "%sevent%d", i == 1 ? "" : ",", i);
		}
		printf("      whitelist len=%d: %s\n", used, list);
		reset_lab(NULL, 1, list);
		lab_fd_put(41, "/dev/input/event12");
		lab_fd_put(43, "/dev/input/event13");
		rc = ioctl(41, EVIOCGRAB, &one);
		ok(rc == 0 && ioctl_call_count == 0, "12th entry is still parsed and matched");
		rc = ioctl(43, EVIOCGRAB, &one);
		ok(ioctl_call_count == 1, "13th (absent) device is forwarded");
		ok(used < LAB_PATH_MAX,
		   "test list fits the documented 159-character environment limit");
	}

	/* ---------------------------------------------------------------- L5 */
	printf("--- L5  variant table: factory/V1/V3 descriptor bytes\n");
	reset_lab("V1", 0, NULL);
	ok(lab_variant && !strcmp(lab_variant->id, "V1"), "V1 selected");
	ok(lab_variant->desc_required == 1, "V1 requires a descriptor override");
	ok(lab_variant->desc_len == 914 && lab_variant->desc != NULL, "V1 descriptor is 914 bytes");
	ok(!memcmp(LAB_DESC_V0, LAB_DESC_FACTORY, LAB_DESC_FACTORY_LEN),
	   "V0 descriptor is the reviewed factory sha-shaped bytes");
	ok(LAB_DESC_V0_LEN == 914 && LAB_DESC_V1_LEN == 914 && LAB_DESC_V3_LEN == 914,
	   "all built-in descriptors keep the 914-byte length");
	{
		/* V1: the Contact Count input item follows Report ID 1 directly. */
		static const unsigned char count_item[] = {
			0x09, 0x54, 0x15, 0x00, 0x25, 0x0a, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02
		};
		ok(!memcmp(LAB_DESC_V1 + 8, count_item, sizeof(count_item)),
		   "V1 moves Contact Count to offset 8 (right after Report ID 1)");
		ok(!memcmp(LAB_DESC_V1, LAB_DESC_V0, 8), "V1 prefix (collection + report id) unchanged");
		ok(!memcmp(LAB_DESC_V1 + 20, LAB_DESC_V0 + 8, 772) &&
		   !memcmp(LAB_DESC_V1 + 20 + 772, LAB_DESC_V0 + 792, 122),
		   "V1 remaining bytes shifted by exactly one 12-byte item");
	}
	reset_lab("V3", 0, NULL);
	{
		static const unsigned char openb[] = {0x09, 0x22, 0xa1, 0x02};
		static const unsigned char iditem[] = {
			0x09, 0x51, 0x15, 0x00, 0x25, 0x09, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02
		};
		static const unsigned char tip[] = {
			0x09, 0x42, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x01, 0x81, 0x02
		};
		char *p = (char *)LAB_DESC_V3;
		int blocks = 0;
		while (p && p - (const char *)LAB_DESC_V3 < LAB_DESC_V3_LEN) {
			char *hit = find_bytes(p, LAB_DESC_V3_LEN - (size_t)(p - (const char *)LAB_DESC_V3),
					       openb, sizeof(openb));
			if (!hit) {
				break;
			}
			if (!memcmp(hit + sizeof(openb), iditem, sizeof(iditem)) &&
			    !memcmp(hit + sizeof(openb) + sizeof(iditem), tip, sizeof(tip))) {
				blocks++;
			}
			p = hit + 1;
		}
		ok(blocks == 10, "V3 orders Contact Identifier before Tip Switch in all 10 fingers");
		ok(!memcmp(LAB_DESC_V3 + 8, LAB_DESC_V1 + 8, 12), "V3 also starts with Contact Count");
	}

	/* --------------------------------------------------------------- L5b */
	printf("--- L5b hid-re-aligned variants V2/V4/V6: descriptor-only, identity frames\n");
	{
		static const struct { const char *id; const unsigned char *desc; int want_diff; } cases[] = {
			{"V2", LAB_DESC_V2, 10},
			{"V4", LAB_DESC_V4, 2},
			{"V6", LAB_DESC_V6, 10},
		};
		for (unsigned c = 0; c < sizeof(cases) / sizeof(cases[0]); c++) {
			int diff = 0;
			reset_lab(cases[c].id, 0, NULL);
			ok(lab_variant && !strcmp(lab_variant->id, cases[c].id), "variant selected");
			ok(lab_variant->report_length == 62, "report_length stays 62");
			ok(lab_perm_identity(lab_variant), "identity frame permutation");
			ok(lab_variant->desc_required == 1, "descriptor override required");
			ok(lab_variant->desc_len == 914 && !memcmp(lab_variant->desc, cases[c].desc, 914),
			   "table bytes are the generated descriptor");
			for (int i = 0; i < 914; i++) {
				diff += cases[c].desc[i] != LAB_DESC_FACTORY[i];
			}
			ok(diff == cases[c].want_diff, "descriptor diff count matches hid-re's variant");
		}
		/* V4/V3 rename the feature report ID 2 -> 1; V1/V2/V6 keep it. */
		{
			int pos = -1;
			for (int i = 0; i + 1 < 914; i++) {
				if (LAB_DESC_FACTORY[i] == 0x85 && LAB_DESC_FACTORY[i + 1] == 0x02) {
					pos = i;
				}
			}
			ok(pos > 0 && LAB_DESC_V4[pos + 1] == 0x01 && LAB_DESC_V3[pos + 1] == 0x01,
			   "85 02 -> 85 01 in V4/V3");
			ok(LAB_DESC_V1[pos + 1] == 0x02 && LAB_DESC_V2[pos + 1] == 0x02 &&
			   LAB_DESC_V6[pos + 1] == 0x02, "V1/V2/V6 keep the feature report ID 2");
		}
		/* V2: ten Logical collections become Physical collections (a1 02 -> a1 00). */
		{
			int n = 0;
			for (int i = 0; i + 3 < 914; i++) {
				if (!memcmp(LAB_DESC_FACTORY + i, "\x09\x22\xa1\x02", 4) &&
				    !memcmp(LAB_DESC_V2 + i, "\x09\x22\xa1\x00", 4)) {
					n++;
				}
			}
			ok(n == 10, "V2 turns all 10 Finger collections Physical");
		}
		/* V4: application usage Touch Screen 09 04 -> Touch Pad 09 05. */
		ok(!memcmp(LAB_DESC_V4, "\x05\x0d\x09\x05\xa1\x01", 6),
		   "V4 first application collection declares Touch Pad (09 05)");
		ok(!memcmp(LAB_DESC_V2, "\x05\x0d\x09\x04\xa1\x01", 6),
		   "V2 keeps Touch Screen (09 04)");
		/* V6: all ten Finger usages become Stylus usages. */
		{
			int n = 0, base = 0;
			for (int i = 0; i + 2 < 914; i++) {
				if (!memcmp(LAB_DESC_V6 + i, "\x09\x20\xa1\x02", 4)) {
					n++;
				}
				if (!memcmp(LAB_DESC_FACTORY + i, "\x09\x20\xa1\x02", 4)) {
					base++;
				}
			}
			ok(base == 1, "factory has one Stylus collection (the pen report 3)");
			ok(n == 11, "V6 converts all 10 Finger collections to Stylus (11 total)");
		}
		/* V3 == contact-id-first V1 layout plus the single feature-report rename. */
		{
			static const unsigned char openb[] = {0x09, 0x22, 0xa1, 0x02};
			static const unsigned char iditem[] = {
				0x09, 0x51, 0x15, 0x00, 0x25, 0x09, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02
			};
			static const unsigned char tip[] = {
				0x09, 0x42, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x01, 0x81, 0x02
			};
			static const unsigned char inr[] = {0x09, 0x32, 0x81, 0x02};
			static const unsigned char pad[] = {0x75, 0x06, 0x81, 0x03};
			unsigned char expect[914];
			int n = 0, pos = -1;
			memcpy(expect, LAB_DESC_V1, 914);
			for (int i = 0; i + 1 < 914; i++) {
				if (LAB_DESC_V1[i] == 0x85 && LAB_DESC_V1[i + 1] == 0x02) {
					pos = i;
				}
			}
			for (int i = 0; i + 35 < 914; i++) {
				if (!memcmp(expect + i, openb, 4) &&
				    !memcmp(expect + i + 4, tip, 12) &&
				    !memcmp(expect + i + 16, inr, 4) &&
				    !memcmp(expect + i + 20, pad, 4) &&
				    !memcmp(expect + i + 24, iditem, 12)) {
					unsigned char block[36];
					memcpy(block, expect + i, 36);
					memcpy(block, openb, 4);
					memcpy(block + 4, iditem, 12);
					memcpy(block + 16, tip, 12);
					memcpy(block + 28, inr, 4);
					memcpy(block + 32, pad, 4);
					memcpy(expect + i, block, 36);
					i += 35;
					n++;
				}
			}
			ok(n == 10, "expected V3 block rebuild matches 10 reordered finger collections");
			if (pos > 0) {
				expect[pos + 1] = 0x01;
			}
			ok(!memcmp(expect, LAB_DESC_V3, 914),
			   "V3 == contact-id-first V1 layout + single 85 02 -> 85 01 byte");
		}
	}

	/* V5 (hid-re, single finger + 62B padding; only when variants.json is
	 * consumed at build time). It is the only variant whose descriptor length
	 * differs from 914, so it exercises the length-tolerant write path. */
	if (lab_variant_find("V5")) {
		printf("--- L5c V5: 225-byte descriptor replaces the 914-byte factory write\n");
		reset_lab("V5", 0, NULL);
		ok(lab_variant && !strcmp(lab_variant->id, "V5"), "V5 selected from the matrix");
		ok(lab_variant->report_length == 62, "V5 keeps report_length 62");
		ok(lab_variant->desc_required == 1 && lab_variant->desc_len == 225,
		   "V5 carries a 225-byte descriptor override");
		ok(lab_variant->perm_len == 62 && lab_variant->perm[1] == 61 &&
		   lab_variant->perm[8] == -1, "V5 frame perm keeps count/pads the rest");
		lab_fd_put(60, "/config/usb_gadget/g1/functions/hid.usb1/report_desc");
		errno = ERANGE;
		rc = write(60, LAB_DESC_FACTORY, LAB_DESC_FACTORY_LEN);
		ok(rc == (ssize_t)LAB_DESC_FACTORY_LEN, "HAL still sees the 914-byte count");
		ok(write_call_count == 1 && write_calls[0].len == 225 &&
		   !memcmp(write_calls[0].data, lab_variant->desc, 225),
		   "kernel write is the 225-byte V5 descriptor");
		ok(lab_desc_active == 1 && errno == ERANGE, "override active, errno preserved");
		{
			unsigned char f[62], m[62];
			memset(f, 0x5a, sizeof(f));
			f[0] = 1;
			f[61] = 1;
			ok(lab_frame_map(f, 62, m) == 1, "V5 frames are permuted");
			ok(m[1] == f[61] && m[2] == f[1] && m[8] == 0,
			   "V5 frame: count at 1, single contact at 2..7, padding zeroed");
		}
	}

	/* ---------------------------------------------------------------- L6 */
	printf("--- L6  frame permutation V1: count-first, byte-exact, invertible\n");
	reset_lab("V1", 0, NULL);
	ok(lab_variant->perm[0] == 0 && lab_variant->perm[1] == 61, "V1 perm moves old byte 61 to byte 1");
	ok(lab_variant->perm[2] == 1 && lab_variant->perm[61] == 60, "V1 perm shifts the tail");
	for (int i = 0; i < 62; i++) {
		orig[i] = (unsigned char)(i + 1);
	}
	orig[0] = 1;
	orig[61] = 2;
	rc = lab_frame_map(orig, 62, mapped);
	printf("      blocked-before-descriptor map rc=%zd\n", rc);
	ok(rc == 0, "frames stay factory while the descriptor override is not active");
	lab_desc_active = 1;
	rc = lab_frame_map(orig, 62, mapped);
	ok(rc == 1, "frame mapped once the descriptor override is active");
	ok(mapped[0] == orig[0], "report id kept");
	ok(mapped[1] == orig[61], "contact count is byte 1");
	ok(!memcmp(mapped + 2, orig + 1, 60), "all other bytes shift by one");
	{
		unsigned char unmapped[62];
		int good = 1;
		memset(unmapped, 0, sizeof(unmapped));
		for (int i = 0; i < 62; i++) {
			signed char s = lab_variant->perm[i];
			if (s >= 0) {
				unmapped[(unsigned char)s] = mapped[i];
			}
		}
		good = !memcmp(unmapped, orig, 62);
		ok(good, "permutation is invertible (frame can be reconstructed byte-exactly)");
	}
	{
		unsigned char pen[62];
		memset(pen, 0x11, sizeof(pen));
		pen[0] = 3;
		ok(lab_frame_map(pen, 62, mapped) == 0, "62-byte pen report 3 is never reordered");
		ok(lab_frame_map(orig, 61, mapped) == 0, "non-62-byte writes keep pass-through semantics");
	}

	/* ---------------------------------------------------------------- L7 */
	printf("--- L7  frame permutation V3: contact-id-first\n");
	reset_lab("V3", 0, NULL);
	for (int i = 0; i < 62; i++) {
		orig[i] = (unsigned char)(0x40 + i);
	}
	orig[0] = 1;
	orig[61] = 1;
	lab_desc_active = 1;
	rc = lab_frame_map(orig, 62, mapped);
	ok(rc == 1, "V3 mapped");
	ok(mapped[1] == orig[61], "V3 contact count is byte 1");
	{
		int good = 1;
		for (int i = 0; i < 10; i++) {
			int src = 1 + 6 * i, dst = 2 + 6 * i;
			good &= mapped[dst + 0] == orig[src + 1]; /* contact id first */
			good &= mapped[dst + 1] == orig[src + 0]; /* flags second */
			good &= mapped[dst + 2] == orig[src + 2];
			good &= mapped[dst + 3] == orig[src + 3];
			good &= mapped[dst + 4] == orig[src + 4];
			good &= mapped[dst + 5] == orig[src + 5];
		}
		ok(good, "V3 moves Contact ID before Tip Switch in every contact slot");
	}

	/* ---------------------------------------------------------------- L8 */
	printf("--- L8  configfs report_desc substitution\n");
	reset_lab("V1", 0, NULL);
	ok(lab_variant->desc_required == 1, "V1 descriptor override armed");
	lab_fd_put(50, "/config/usb_gadget/g1/functions/hid.usb1/report_desc");
	lab_fd_put(51, "/config/usb_gadget/g1/functions/hid.usb0/report_desc");
	lab_fd_put(52, "/config/usb_gadget/g1/functions/hid.usb1/report_length");
	errno = ERANGE;
	rc = write(50, LAB_DESC_FACTORY, LAB_DESC_FACTORY_LEN);
	e = errno;
	printf("      desc write rc=%zd errno=%d\n", rc, e);
	ok(rc == (ssize_t)LAB_DESC_FACTORY_LEN, "HAL sees its own byte count");
	ok(e == ERANGE, "caller errno preserved on the reported success");
	ok(write_call_count == 1, "one kernel write issued for the replacement");
	ok(write_calls[0].fd == 50 && write_calls[0].len == LAB_DESC_V1_LEN,
	   "replacement wrote the V1 descriptor length");
	ok(!memcmp(write_calls[0].data, LAB_DESC_V1, LAB_DESC_V1_LEN),
	   "replacement bytes are the V1 descriptor");
	ok(lab_desc_active == 1 && lab_desc_replaced == 1, "descriptor override recorded active");
	{
		unsigned char frame62[62];
		memset(frame62, 0x22, sizeof(frame62));
		frame62[0] = 1;
		ok(lab_frame_map(frame62, 62, mapped) == 1, "frames may now be permuted");
	}
	reset_fakes();
	rc = write(51, LAB_DESC_FACTORY, LAB_DESC_FACTORY_LEN);
	ok(rc == (ssize_t)LAB_DESC_FACTORY_LEN && write_call_count == 1,
	   "hid.usb0 descriptor write is forwarded untouched");
	ok(!memcmp(write_calls[0].data, LAB_DESC_FACTORY, LAB_DESC_FACTORY_LEN),
	   "hid.usb0 bytes unchanged");
	reset_fakes();
	rc = write(52, "62", 2);
	ok(rc == 2 && write_call_count == 1 && write_calls[0].len == 2 &&
	   !memcmp(write_calls[0].data, "62", 2), "report_length write forwarded verbatim");
	ok(lab_desc_writes == 3, "configfs writes counted for the probe");

	/* ---------------------------------------------------------------- L9 */
	printf("--- L9  unexpected/split descriptor writes fail closed\n");
	reset_lab("V1", 0, NULL);
	lab_fd_put(50, "/config/usb_gadget/g1/functions/hid.usb1/report_desc");
	{
		unsigned char other[914];
		memset(other, 0xAA, sizeof(other));
		rc = write(50, other, sizeof(other));
		ok(rc == 914 && write_call_count == 1 && !memcmp(write_calls[0].data, other, 914),
		   "unknown 914-byte descriptor forwarded unchanged");
		ok(lab_variant_blocked == 1, "variant blocked after an unexpected descriptor");
		frame[0] = 1;
		ok(lab_frame_map(frame, 62, mapped) == 0, "frames stay factory once blocked");
	}
	reset_lab("V1", 0, NULL);
	lab_fd_put(50, "/config/usb_gadget/g1/functions/hid.usb1/report_desc");
	rc = write(50, LAB_DESC_FACTORY, 512);
	ok(rc == 512 && write_call_count == 1 && write_calls[0].len == 512 &&
	   !memcmp(write_calls[0].data, LAB_DESC_FACTORY, 512),
	   "split descriptor write forwarded unchanged");
	ok(lab_variant_blocked == 1, "variant blocked on a split write");

	/* --------------------------------------------------------------- L10 */
	printf("--- L10 unsupported variant falls back to V0 and keeps frames factory\n");
	reset_lab("V0", 0, NULL);
	ok(lab_variant && !strcmp(lab_variant->id, "V0"), "V0 selected");
	ok(lab_variant_supported == 1, "V0 supported");
	ok(lab_perm_identity(lab_variant), "V0 perm is identity");
	ok(lab_frame_map(orig, 62, mapped) == 0, "V0 never rewrites frames");

	/* --------------------------------------------------------------- L11 */
	printf("--- L11 pen-frame diagnostic: one-shot report-3 frame, gate frames intact\n");
	setenv("MIPAD_NATIVE_LAB", "1", 1);
	unsetenv("MIPAD_LAB_VARIANT");
	unsetenv("MIPAD_LAB_NOGRAB");
	setenv("MIPAD_LAB_PEN_PROBE", "1", 1);
	lab_fd_del_all_for_test();
	fake_readlink[0] = 0;
	lab_init();
	reset_fakes();
	ok(lab_pen_probe == 1, "pen probe armed from the environment");
	{
		unsigned char report1[62];
		memset(report1, 0x33, sizeof(report1));
		report1[0] = 1;
		rc = raw_write(9, report1, sizeof(report1));
		ok(rc == 62, "gate frame still reported as written");
		ok(write_call_count == 2, "exactly one pen frame injected after the first report-1 frame");
		ok(write_calls[0].len == 62 && write_calls[0].data[0] == 1,
		   "first write is the untouched report-1 frame");
		ok(write_calls[1].len == 62 && write_calls[1].data[0] == 3,
		   "second write is the 62-byte report-3 pen frame");
		ok(lab_pen_probe_done == 1, "pen probe marked done");
		write_call_count = 0;
		rc = raw_write(9, report1, sizeof(report1));
		ok(rc == 62 && write_call_count == 1, "pen probe fires only once per session");
	}
	unsetenv("MIPAD_LAB_PEN_PROBE");

	/* --------------------------------------------------------------- L12 */
	printf("--- L12 non-interesting open paths are log-budgeted (T5/T6 noise)\n");
	reset_lab(NULL, 0, NULL);
	{
		const char *bl = "/sys/devices/platform/soc/d0000000.dpu/backlight/panel0-backlight/brightness";
		int allowed = 0;
		for (int i = 0; i < 8; i++) {
			allowed += lab_open_log_budget(bl);
		}
		ok(allowed == 4, "first 4 opens of one sysfs path are logged, the rest suppressed");
		ok(lab_open_log_budget(bl) == 0, "budget stays exhausted for that path");
		ok(lab_open_log_budget("/dev/input/event6") == 1, "a different path has its own budget");
	}
	reset_lab(NULL, 0, NULL);
	{
		char got[LAB_PATH_MAX];
		for (int i = 0; i < 6; i++) {
			lab_open_note("/sys/class/backlight/fake/brightness", 0, -1, 90, 0);
		}
		ok(lab_fd_lookup(90, got, sizeof(got)) &&
		   !strcmp(got, "/sys/class/backlight/fake/brightness"),
		   "suppressed open logs still record fd->path");
	}

	printf("\n%d checks, %d failure(s)\n", checks, fails);
	return fails ? 1 : 0;
}
