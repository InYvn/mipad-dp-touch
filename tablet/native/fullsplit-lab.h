/* Native-touch lab interposer extensions (experimental, compiled only with
 * -DFULLSPLIT_LAB; the production DSO build does not include this file).
 *
 * Everything here is inert unless the candidate process was started with
 * MIPAD_NATIVE_LAB=1. In that mode the module still performs its normal gate
 * work; these hooks only observe (probe) or, when explicitly asked, substitute
 * the reviewable 62-byte HID shape (variant) / pretend the evdev grab
 * succeeded (no-grab).
 *
 * Environment (read once from the candidate's environment):
 *   MIPAD_NATIVE_LAB=1            enable probe logging (tag NativeHidLab)
 *   MIPAD_LAB_VARIANT=<id>        V0 (factory, default) | V1 | V3 | ...
 *   MIPAD_LAB_DESC_PRELOADED=1    descriptor was already planted by a helper
 *                                 before launch, so the candidate's own
 *                                 factory-descriptor write must NOT be
 *                                 substituted again; frame mapping is allowed
 *   MIPAD_LAB_NOGRAB=1            swallow EVIOCGRAB/EVIOCREVOKE on the
 *                                 whitelisted evdev node below
 *   MIPAD_LAB_PEN_PROBE=1         one-shot diagnostic: after the first 62-byte
 *                                 report-1 frame, write one synthetic 62-byte
 *                                 report-3 (pen) frame to the target. Mac-side
 *                                 kernel log then shows whether the Finger or
 *                                 the Pen collection is dispatching report 1
 *                                 (hid-re 2026-11-13 null-event analysis)
 *   MIPAD_LAB_NOGRAB_DEV=<list>   comma list, default "event11"; entries may
 *                                 be full paths (/dev/input/event11) or
 *                                 basenames (event11)
 *
 * Invariants kept on purpose:
 *   * The 62-byte report length is never changed. A variant that asks for a
 *     different report_length is refused at init (logged as
 *     variant-unavailable) instead of relaxing candidate_descriptors_check.
 *   * The report descriptor is only replaced when the HAL's write is exactly
 *     the reviewed 914-byte factory descriptor; anything else is forwarded
 *     untouched and the variant stays blocked (frames keep the factory
 *     layout, so host and descriptor never disagree).
 *   * Frames are only permuted for report ID 1 (pen report 3 untouched), and
 *     only after the variant descriptor actually landed (or when
 *     MIPAD_LAB_DESC_PRELOADED=1 says a helper already planted it).
 *   * Probe paths forward through the syscall; ioctl's varargs argument is
 *     forwarded byte-for-byte. no-grab is the only behavior change and is
 *     opt-in via MIPAD_LAB_NOGRAB=1 for a path whitelist.
 */
#ifndef FULLSPLIT_LAB_H
#define FULLSPLIT_LAB_H

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/syscall.h>

#include "fullsplit-lab-variants.h"

#define LAB_TAG "NativeHidLab"
#define LAB_FD_MAX 256
#define LAB_PATH_MAX 160
#define LAB_IOCTL_PAIRS 96
#define LAB_FRAME_LOG_FIRST 24
#define LAB_FRAME_LOG_EVERY 200
#define LAB_WRITE_LOG_FIRST 16
#define LAB_WRITE_LOG_EVERY 128
#define LAB_REPORT_LENGTH 62

#ifndef EVIOCGRAB
#define EVIOCGRAB 0x40044590L
#endif
#ifndef EVIOCREVOKE
#define EVIOCREVOKE 0x40044591L
#endif

enum { LAB_KIND_NONE = 0, LAB_KIND_CONFIGFS, LAB_KIND_DESC, LAB_KIND_LENGTH,
       LAB_KIND_HIDG, LAB_KIND_INPUT };

/* The host unit test (test-lab.c) replaces the syscalls it cannot perform on
 * macOS; every raw operation goes through these macros. */
#ifdef FS_TEST_LAB
extern int FS_TEST_LAB_IOCTL(int, int, void *);
extern ssize_t FS_TEST_LAB_READLINK(const char *, char *, size_t);
extern ssize_t FS_TEST_LAB_WRITE(int, const void *, size_t);
#define LAB_SYS_IOCTL(fd, req, arg) FS_TEST_LAB_IOCTL((fd), (int)(req), (arg))
#define LAB_READLINK(p, b, n) FS_TEST_LAB_READLINK((p), (b), (n))
#define LAB_SYS_WRITE(fd, b, n) FS_TEST_LAB_WRITE((fd), (b), (n))
#else
#define LAB_SYS_IOCTL(fd, req, arg) syscall(SYS_ioctl, (fd), (req), (arg))
#define LAB_READLINK(p, b, n) readlink((p), (b), (n))
#define LAB_SYS_WRITE(fd, b, n) syscall(SYS_write, (fd), (b), (n))
#endif

typedef struct {
	int used;
	int fd;
	char path[LAB_PATH_MAX];
} lab_fd_entry;

static lab_fd_entry lab_fds[LAB_FD_MAX];
static pthread_mutex_t lab_lock = PTHREAD_MUTEX_INITIALIZER;
static int lab_ready;            /* lab_init ran (env resolved) */
static int lab_enabled;          /* MIPAD_NATIVE_LAB=1 */
static int lab_nograb;           /* MIPAD_LAB_NOGRAB=1 */
static int lab_pen_probe;        /* MIPAD_LAB_PEN_PROBE=1 */
static int lab_pen_probe_done;
static int lab_desc_preloaded;   /* MIPAD_LAB_DESC_PRELOADED=1 */
static char lab_nograb_dev[LAB_PATH_MAX] = "event11";
static const lab_variant_table *lab_variant;
static int lab_variant_supported;
static int lab_desc_active;      /* variant descriptor actually planted */
static int lab_variant_blocked;  /* set after an unexpected descriptor write */
static int lab_init_logged;
static int lab_frame_block_logged;
static _Thread_local int lab_in_log;
static unsigned long lab_frame_writes;    /* raw 62B passes through raw_write */
static unsigned long lab_frame_mapped;
static unsigned long lab_frame_logged;
static unsigned long lab_target_writes;
static unsigned long lab_desc_writes;
static unsigned long lab_desc_replaced;
static unsigned long lab_grab_seen;
static unsigned long lab_grab_suppressed;
static unsigned long lab_ioctl_seen;
static struct { int used; int fd; int req; } lab_pairs[LAB_IOCTL_PAIRS];
static int lab_pair_count;

static void lab_log(const char *fmt, ...)
{
	char line[512];
	int used;
	va_list ap;
	if (lab_in_log) {
		return;
	}
	lab_in_log = 1;
	va_start(ap, fmt);
	used = vsnprintf(line, sizeof(line), fmt, ap);
	va_end(ap);
	if (used < 0) {
		line[0] = 0;
	} else if ((size_t)used >= sizeof(line)) {
		line[sizeof(line) - 1] = 0;
	}
	/* __android_log_print is variadic; the NDK also has __android_log_vprint,
	 * but formatting here keeps the host stub (android/log.h stand-in) exact. */
	__android_log_print(ANDROID_LOG_INFO, LAB_TAG, "%s", line);
	lab_in_log = 0;
}

static int lab_env_flag(const char *name)
{
	const char *v = getenv(name);
	return v && v[0] == '1' && v[1] == '\0';
}

static int lab_fd_put(int fd, const char *path)
{
	int slot = -1, i;
	if (fd < 0 || !path || !*path) {
		return 0;
	}
	pthread_mutex_lock(&lab_lock);
	for (i = 0; i < LAB_FD_MAX; i++) {
		if (lab_fds[i].used && lab_fds[i].fd == fd) {
			slot = i;
			break;
		}
		if (slot < 0 && !lab_fds[i].used) {
			slot = i;
		}
	}
	if (slot >= 0) {
		lab_fds[slot].used = 1;
		lab_fds[slot].fd = fd;
		snprintf(lab_fds[slot].path, sizeof(lab_fds[slot].path), "%s", path);
	}
	pthread_mutex_unlock(&lab_lock);
	return slot >= 0;
}

static void lab_fd_del(int fd)
{
	int i;
	if (fd < 0) {
		return;
	}
	pthread_mutex_lock(&lab_lock);
	for (i = 0; i < LAB_FD_MAX; i++) {
		if (lab_fds[i].used && lab_fds[i].fd == fd) {
			lab_fds[i].used = 0;
			lab_fds[i].fd = -1;
			lab_fds[i].path[0] = 0;
			break;
		}
	}
	pthread_mutex_unlock(&lab_lock);
}

static int lab_fd_lookup(int fd, char *out, size_t n)
{
	int i, found = 0;
	if (fd < 0 || !out || !n) {
		return 0;
	}
	pthread_mutex_lock(&lab_lock);
	for (i = 0; i < LAB_FD_MAX; i++) {
		if (lab_fds[i].used && lab_fds[i].fd == fd) {
			snprintf(out, n, "%s", lab_fds[i].path);
			found = 1;
			break;
		}
	}
	pthread_mutex_unlock(&lab_lock);
	return found;
}

/* Canonical fd -> path. The fd table is authoritative for descriptors the
 * candidate opened after the DSO was loaded; /proc/self/fd is the fallback for
 * inherited descriptors (and is where a relative openat is resolved). */
static int lab_fd_path(int fd, char *out, size_t n)
{
	char link[48], name[LAB_PATH_MAX];
	ssize_t r;
	if (lab_fd_lookup(fd, out, n)) {
		return 1;
	}
	snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
	r = LAB_READLINK(link, name, sizeof(name) - 1);
	if (r <= 0) {
		return 0;
	}
	name[r] = 0;
	/* Anonymous kernel objects are not interesting and must not be cached
	 * under a growing set of throw-away descriptors. */
	if (!strncmp(name, "socket:", 7) || !strncmp(name, "anon_inode:", 11) ||
	    !strncmp(name, "pipe:", 5) || !strncmp(name, "/memfd:", 7) ||
	    !strncmp(name, "/dev/null", 9) || !strncmp(name, "/dev/urandom", 12)) {
		return 0;
	}
	snprintf(out, n, "%s", name);
	lab_fd_put(fd, out);
	return 1;
}

static int lab_path_kind(const char *path)
{
	const char *slash;
	size_t len;
	if (!path || !*path) {
		return LAB_KIND_NONE;
	}
	if (!strncmp(path, "/dev/hidg", 9)) {
		return LAB_KIND_HIDG;
	}
	if (!strncmp(path, "/dev/input/event", 16)) {
		return LAB_KIND_INPUT;
	}
	if (!strstr(path, "/config/usb_gadget/")) {
		return LAB_KIND_NONE;
	}
	len = strlen(path);
	slash = path + len;
	if (len >= 12 && !strcmp(slash - 12, "/report_desc")) {
		return LAB_KIND_DESC;
	}
	if (len >= 14 && !strcmp(slash - 14, "/report_length")) {
		return LAB_KIND_LENGTH;
	}
	return LAB_KIND_CONFIGFS;
}

static const lab_variant_table *lab_variant_find(const char *id)
{
	int i;
	for (i = 0; i < LAB_VARIANT_COUNT; i++) {
		if (!strcmp(LAB_VARIANTS[i].id, id)) {
			return &LAB_VARIANTS[i];
		}
	}
	return NULL;
}

static int lab_perm_identity(const lab_variant_table *v)
{
	unsigned i;
	if (!v || !v->perm || v->perm_len != LAB_REPORT_LENGTH) {
		return 1;
	}
	for (i = 0; i < LAB_REPORT_LENGTH; i++) {
		if (v->perm[i] != (signed char)i) {
			return 0;
		}
	}
	return 1;
}

/* (Re)resolve the lab configuration from the environment. Called once from a
 * constructor and again by the host unit test. Safe to call more than once. */
static void lab_init(void)
{
	const char *id;
	lab_enabled = lab_env_flag("MIPAD_NATIVE_LAB");
	lab_ready = 1;
	lab_desc_active = 0;
	lab_variant_blocked = 0;
	lab_variant_supported = 0;
	lab_variant = NULL;
	lab_nograb = 0;
	lab_desc_preloaded = 0;
	lab_pen_probe = 0;
	lab_pen_probe_done = 0;
	lab_frame_writes = 0;
	lab_frame_mapped = 0;
	lab_frame_logged = 0;
	lab_target_writes = 0;
	lab_desc_writes = 0;
	lab_desc_replaced = 0;
	lab_grab_seen = 0;
	lab_grab_suppressed = 0;
	lab_ioctl_seen = 0;
	lab_pair_count = 0;
	memset(lab_pairs, 0, sizeof(lab_pairs));
	snprintf(lab_nograb_dev, sizeof(lab_nograb_dev), "event11");
	if (!lab_enabled) {
		return;
	}
	lab_nograb = lab_env_flag("MIPAD_LAB_NOGRAB");
	lab_desc_preloaded = lab_env_flag("MIPAD_LAB_DESC_PRELOADED");
	lab_pen_probe = lab_env_flag("MIPAD_LAB_PEN_PROBE");
	id = getenv("MIPAD_LAB_VARIANT");
	if (!id || !*id) {
		id = "V0";
	}
	lab_variant = lab_variant_find(id);
	if (!lab_variant) {
		lab_log("variant-unknown id=%s fallback=V0", id);
		lab_variant = lab_variant_find("V0");
		id = "V0";
	}
	if (!lab_variant) {
		lab_log("variant-table-empty fallback=none");
		return;
	}
	if (lab_variant->report_length != LAB_REPORT_LENGTH) {
		lab_log("variant-unavailable id=%s report_length=%d need=%d fallback=V0",
			lab_variant->id, lab_variant->report_length, LAB_REPORT_LENGTH);
		lab_variant = lab_variant_find("V0");
		lab_variant_supported = 0;
	} else {
		lab_variant_supported = 1;
	}
	if (lab_variant && lab_variant->desc_required && lab_desc_preloaded) {
		lab_desc_active = 1;
	}
	{
		const char *dev = getenv("MIPAD_LAB_NOGRAB_DEV");
		if (dev && *dev) {
			snprintf(lab_nograb_dev, sizeof(lab_nograb_dev), "%s", dev);
		}
	}
	if (!lab_init_logged) {
		lab_init_logged = 1;
		lab_log("init enabled=1 variant=%s report_length=%d desc_required=%d "
			"desc_preloaded=%d nograb=%d nograb_dev=%s pen_probe=%d pid=%d",
			lab_variant ? lab_variant->id : "?",
			lab_variant ? lab_variant->report_length : -1,
			lab_variant ? lab_variant->desc_required : -1,
			lab_desc_preloaded, lab_nograb, lab_nograb_dev, lab_pen_probe,
			(int)getpid());
	}
	if (lab_variant && lab_variant->report_length == LAB_REPORT_LENGTH &&
	    lab_variant->desc_required == 0 && !lab_perm_identity(lab_variant)) {
		/* A frame-only variant is technically expressible, but the factory
		 * descriptor would then advertise a different bit order. Refuse it. */
		lab_log("variant-inconsistent id=%s desc_required=0 perm!=identity blocked",
			lab_variant->id);
		lab_variant_supported = 0;
		lab_variant = lab_variant_find("V0");
	}
}

static void lab_ensure(void)
{
	if (!lab_ready) {
		lab_init();
	}
}

static void lab_open_note(const char *given, int flags, int dirfd, int fd, int err)
{
	char path[LAB_PATH_MAX], dir[LAB_PATH_MAX];
	if (!lab_enabled) {
		return;
	}
	if (fd >= 0) {
		if (!lab_fd_path(fd, path, sizeof(path))) {
			if (dirfd >= 0 && given && given[0] != '/' &&
			    lab_fd_path(dirfd, dir, sizeof(dir))) {
				snprintf(path, sizeof(path), "%s/%s", dir, given);
			} else if (given) {
				snprintf(path, sizeof(path), "%s", given);
			} else {
				path[0] = 0;
			}
			lab_fd_put(fd, path);
		}
	} else {
		path[0] = 0;
	}
	if (dirfd >= 0) {
		lab_log("openat fd=%d dirfd=%d name=%s flags=0x%x rc=%s path=%s",
			fd, dirfd, given ? given : "?", flags,
			err ? "error" : "ok", path[0] ? path : "?");
	} else {
		lab_log("open fd=%d flags=0x%x rc=%s path=%s", fd, flags,
			err ? "error" : "ok", path[0] ? path : (given ? given : "?"));
	}
}

static void lab_close_note(int fd)
{
	char path[LAB_PATH_MAX];
	int kind;
	if (lab_fd_lookup(fd, path, sizeof(path))) {
		kind = lab_path_kind(path);
		if (kind != LAB_KIND_NONE) {
			lab_log("close fd=%d path=%s", fd, path);
		}
		lab_fd_del(fd);
	}
}

static void lab_dup_note(int oldfd, int newfd, int rc)
{
	char path[LAB_PATH_MAX];
	if (rc < 0 || newfd < 0) {
		return;
	}
	if (lab_fd_path(oldfd, path, sizeof(path))) {
		lab_fd_put(newfd, path);
	}
}

static void lab_note_target_write(int fd, const void *buf, size_t n)
{
	const unsigned char *p = buf;
	unsigned rid = n ? p[0] : 0xffu;
	lab_target_writes++;
	/* The gate already logs every 62-byte report-ID-1 frame; this covers the
	 * pen/other reports and any short write the probe should see. */
	if (n == LAB_REPORT_LENGTH && rid == 1) {
		return;
	}
	if (lab_target_writes <= LAB_WRITE_LOG_FIRST || lab_target_writes % LAB_WRITE_LOG_EVERY == 0) {
		char hex[33];
		int i, lim = n < 16 ? (int)n : 16;
		for (i = 0; i < lim; i++) {
			snprintf(hex + i * 2, 3, "%02x", p[i]);
		}
		hex[lim * 2] = 0;
		lab_log("hidg-write fd=%d len=%zu rid=%u first=%s", fd, n, rid, hex);
	}
}

/* One-shot report-3 (pen) diagnostic frame. hid-re's null-event analysis: if
 * the Mac kernel log gains ~10 null lines for one report-3 frame, the Finger
 * collection exists but is dispatch-failed; if it gains none, the Touch Screen
 * collection itself is not registered (supports the V4 Touch Pad hypothesis).
 * The frame is written outside the gate and only once. */
static void lab_pen_probe_maybe(int fd)
{
	unsigned char pen[LAB_REPORT_LENGTH];
	ssize_t rc;
	if (!lab_enabled || !lab_pen_probe || lab_pen_probe_done) {
		return;
	}
	lab_pen_probe_done = 1;
	memset(pen, 0, sizeof(pen));
	pen[0] = 3; /* pen report id, in-range/tip cleared */
	rc = LAB_SYS_WRITE(fd, pen, sizeof(pen));
	lab_log("pen-probe fd=%d len=%zu rid=3 rc=%zd errno=%d", fd, sizeof(pen), rc,
		rc < 0 ? errno : 0);
}

/* --- report descriptor substitution ------------------------------------- */
static int lab_iso_hexdump(const unsigned char *b, size_t n, char *out, size_t outn)
{
	size_t i, lim = n < 12 ? n : 12;
	size_t used = 0;
	for (i = 0; i < lim && used + 3 < outn; i++) {
		int w = snprintf(out + used, outn - used, "%02x", b[i]);
		if (w < 0) {
			break;
		}
		used += (size_t)w;
	}
	out[used] = 0;
	return (int)used;
}

/* Returns 1 when the call was handled here (result in *out, errno set), 0 when
 * the caller must forward it. */
static int lab_desc_try_write(int fd, const void *buf, size_t n, ssize_t *out)
{
	char path[LAB_PATH_MAX], hex[40];
	int kind, is_hid1, saved = errno;
	ssize_t rc;
	if (!lab_enabled) {
		return 0;
	}
	if (!lab_fd_path(fd, path, sizeof(path))) {
		return 0;
	}
	kind = lab_path_kind(path);
	if (kind != LAB_KIND_DESC && kind != LAB_KIND_LENGTH) {
		return 0;
	}
	lab_desc_writes++;
	lab_iso_hexdump(buf, n, hex, sizeof(hex));
	if (kind == LAB_KIND_LENGTH) {
		rc = LAB_SYS_WRITE(fd, buf, n);
		lab_log("configfs length-write fd=%d path=%s len=%zu head=%s rc=%zd errno=%d",
			fd, path, n, hex, rc, rc < 0 ? errno : 0);
		errno = rc < 0 ? errno : saved;
		*out = rc;
		return 1;
	}
	is_hid1 = strstr(path, "hid.usb1") != NULL;
	lab_log("configfs desc-write fd=%d path=%s len=%zu head=%s", fd, path, n, hex);
	if (!is_hid1 || lab_variant_blocked || !lab_variant || !lab_variant->desc_required) {
		rc = LAB_SYS_WRITE(fd, buf, n);
		lab_log("desc-forward fd=%d path=%s len=%zu rc=%zd", fd, path, n, rc);
		errno = rc < 0 ? errno : saved;
		*out = rc;
		return 1;
	}
	if (lab_variant->desc == NULL || lab_variant->desc_len == 0) {
		lab_variant_blocked = 1;
		lab_log("desc-variant-missing id=%s block=1", lab_variant->id);
		rc = LAB_SYS_WRITE(fd, buf, n);
		errno = rc < 0 ? errno : saved;
		*out = rc;
		return 1;
	}
	/* Substitute only the exact reviewed factory descriptor in one write. A
	 * split/differing write means we cannot reason about the result: forward
	 * it unchanged and keep the factory frame layout (fail closed, loudly). */
	if (n != LAB_DESC_FACTORY_LEN || memcmp(buf, LAB_DESC_FACTORY, LAB_DESC_FACTORY_LEN)) {
		if (n == lab_variant->desc_len && !memcmp(buf, lab_variant->desc, n)) {
			lab_desc_active = 1;
			lab_log("desc-already-variant fd=%d variant=%s len=%zu", fd,
				lab_variant->id, n);
		} else {
			lab_variant_blocked = 1;
			lab_frame_block_logged = 0;
			lab_log("desc-write-unexpected fd=%d variant=%s len=%zu expected_factory=%d "
				"block=1 frames=factory", fd, lab_variant->id, n,
				LAB_DESC_FACTORY_LEN);
		}
		rc = LAB_SYS_WRITE(fd, buf, n);
		lab_log("desc-forward fd=%d path=%s len=%zu rc=%zd", fd, path, n, rc);
		errno = rc < 0 ? errno : saved;
		*out = rc;
		return 1;
	}
	rc = LAB_SYS_WRITE(fd, lab_variant->desc, lab_variant->desc_len);
	if (rc == (ssize_t)lab_variant->desc_len) {
		lab_desc_active = 1;
		lab_desc_replaced++;
		lab_log("desc-replaced fd=%d variant=%s factory_len=%zu variant_len=%zu "
			"reported=%zu", fd, lab_variant->id, n, lab_variant->desc_len, n);
		*out = (ssize_t)n;
		errno = saved;
		return 1;
	}
	{
		int e = errno;
		lab_log("desc-replace-failed fd=%d variant=%s variant_len=%zu rc=%zd errno=%d "
			"fallback=factory", fd, lab_variant->id, lab_variant->desc_len, rc, e);
		rc = LAB_SYS_WRITE(fd, buf, n);
		if (rc >= 0) {
			lab_variant_blocked = 1;
			lab_frame_block_logged = 0;
			*out = (ssize_t)n;
			errno = saved;
			return 1;
		}
		errno = e;
		*out = rc;
		return 1;
	}
}

/* --- frame variant ------------------------------------------------------ */
/* Map a factory-layout report into the active variant. Returns 1 and fills
 * out[62] when the frame was rewritten, 0 when the caller must send it as-is. */
static int lab_frame_map(const void *buf, size_t n, unsigned char out[LAB_REPORT_LENGTH])
{
	const unsigned char *b = buf;
	unsigned i;
	if (!lab_enabled || !lab_variant || n != LAB_REPORT_LENGTH) {
		return 0;
	}
	if (b[0] != 1 || lab_perm_identity(lab_variant)) {
		return 0; /* pen report 3 and identity variants are untouched */
	}
	if (lab_variant->desc_required && !lab_desc_active) {
		if (!lab_frame_block_logged) {
			lab_frame_block_logged = 1;
			lab_log("frame-map-blocked variant=%s reason=descriptor-not-active "
				"frames=factory", lab_variant->id);
		}
		return 0;
	}
	for (i = 0; i < LAB_REPORT_LENGTH; i++) {
		signed char src = lab_variant->perm[i];
		out[i] = src < 0 ? 0x00 : b[(unsigned char)src];
	}
	lab_frame_mapped++;
	if (lab_frame_logged < LAB_FRAME_LOG_FIRST ||
	    lab_frame_logged % LAB_FRAME_LOG_EVERY == 0) {
		char in[40], outhex[40];
		lab_iso_hexdump(b, n, in, sizeof(in));
		lab_iso_hexdump(out, n, outhex, sizeof(outhex));
		lab_log("frame-map variant=%s seq=%lu in=%s out=%s", lab_variant->id,
			lab_frame_mapped, in, outhex);
	}
	lab_frame_logged++;
	return 1;
}

/* --- no-grab ------------------------------------------------------------ */
static int lab_grab_request(int req)
{
	return (unsigned long)req == (unsigned long)EVIOCGRAB ||
	       (unsigned long)req == (unsigned long)EVIOCREVOKE;
}

static int lab_nograb_match(const char *path)
{
	char list[LAB_PATH_MAX], *save = NULL, *tok;
	if (!path || !*path) {
		return 0;
	}
	snprintf(list, sizeof(list), "%s", lab_nograb_dev);
	for (tok = strtok_r(list, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
		size_t len;
		while (*tok == ' ' || *tok == '\t') {
			tok++;
		}
		len = strlen(tok);
		while (len && (tok[len - 1] == ' ' || tok[len - 1] == '\t')) {
			tok[--len] = 0;
		}
		if (!len) {
			continue;
		}
		if (tok[0] == '/') {
			if (!strcmp(path, tok)) {
				return 1;
			}
		} else {
			const char *base = strrchr(path, '/');
			base = base ? base + 1 : path;
			if (!strcmp(base, tok)) {
				return 1;
			}
		}
	}
	return 0;
}

static int lab_ioctl_pair_first(int fd, int req)
{
	int i;
	pthread_mutex_lock(&lab_lock);
	for (i = 0; i < lab_pair_count; i++) {
		if (lab_pairs[i].used && lab_pairs[i].fd == fd && lab_pairs[i].req == req) {
			pthread_mutex_unlock(&lab_lock);
			return 0;
		}
	}
	if (lab_pair_count < LAB_IOCTL_PAIRS) {
		lab_pairs[lab_pair_count].used = 1;
		lab_pairs[lab_pair_count].fd = fd;
		lab_pairs[lab_pair_count].req = req;
		lab_pair_count++;
	}
	pthread_mutex_unlock(&lab_lock);
	return 1;
}

/* --- hooks (lab DSO only) ---------------------------------------------- */
static void lab_ctor(void) __attribute__((constructor));
static void lab_ctor(void)
{
	lab_init();
}

int openat(int dirfd, const char *path, int flags, ...)
{
	mode_t mode = 0;
	int rc, e;
	va_list ap;
	if (flags & O_CREAT || (flags & O_TMPFILE) == O_TMPFILE) {
		va_start(ap, flags);
		mode = (mode_t)va_arg(ap, int);
		va_end(ap);
	}
	lab_ensure();
	rc = (int)syscall(SYS_openat, dirfd, path, flags, mode);
	e = errno;
	if (lab_enabled) {
		lab_open_note(path, flags, dirfd, rc, rc < 0 ? e : 0);
	}
	errno = e;
	return rc;
}

int ioctl(int fd, int request, ...)
{
	void *arg;
	int rc, e, known;
	char path[LAB_PATH_MAX];
	va_list ap;
	va_start(ap, request);
	/* All Android ioctl users pass the third argument; ioctls that ignore it
	 * ignore the forwarded value just the same. */
	arg = va_arg(ap, void *);
	va_end(ap);
	lab_ensure();
	if (!lab_enabled) {
		return (int)LAB_SYS_IOCTL(fd, request, arg);
	}
	lab_ioctl_seen++;
	known = lab_fd_path(fd, path, sizeof(path));
	if (lab_nograb && known && lab_grab_request(request) && lab_nograb_match(path)) {
		lab_grab_suppressed++;
		lab_grab_seen++;
		lab_log("grab-suppressed fd=%d request=0x%x(%s) path=%s",
			fd, (unsigned)request,
			(unsigned long)request == (unsigned long)EVIOCGRAB ? "EVIOCGRAB" : "EVIOCREVOKE",
			path);
		return 0;
	}
	rc = (int)LAB_SYS_IOCTL(fd, request, arg);
	e = errno;
	if (lab_grab_request(request)) {
		lab_grab_seen++;
		lab_log("grab fd=%d request=0x%x(%s) path=%s rc=%d errno=%d", fd,
			(unsigned)request,
			(unsigned long)request == (unsigned long)EVIOCGRAB ? "EVIOCGRAB" : "EVIOCREVOKE",
			known ? path : "?", rc, e);
	} else if (known && lab_ioctl_pair_first(fd, request)) {
		lab_log("ioctl fd=%d request=0x%x path=%s rc=%d errno=%d", fd,
			(unsigned)request, path, rc, e);
	}
	errno = e;
	return rc;
}

#ifdef FS_TEST_LAB
/* Host-test helpers only; the DSO build never emits them. */
static void lab_fd_del_all_for_test(void)
{
	pthread_mutex_lock(&lab_lock);
	memset(lab_fds, 0, sizeof(lab_fds));
	pthread_mutex_unlock(&lab_lock);
}

static int lab_fd_count_for_test(void)
{
	int i, n = 0;
	pthread_mutex_lock(&lab_lock);
	for (i = 0; i < LAB_FD_MAX; i++) {
		n += lab_fds[i].used ? 1 : 0;
	}
	pthread_mutex_unlock(&lab_lock);
	return n;
}
#endif /* FS_TEST_LAB */

#endif /* FULLSPLIT_LAB_H */
