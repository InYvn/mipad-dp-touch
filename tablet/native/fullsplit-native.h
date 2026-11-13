/* Production native-touch additions for the LD_PRELOAD gate DSO (module 2.1.10).
 *
 * Compiled in only with -DFULLSPLIT_NATIVE (tablet/build_dso.sh
 * MIPAD_DSO_NATIVE=1); the plain build stays byte-identical to the 2.1.9
 * gate DSO. Two behaviours are added on top of the 2.1.9 fullsplit gate:
 *
 * 1. Report descriptor substitution. The stock HAL writes the reviewed
 *    914-byte factory hid.usb1 descriptor through libc write() (T4 on-device
 *    evidence). When - and only when - that exact descriptor arrives in one
 *    write on a /config/usb_gadget/.../functions/hid.usb1/report_desc fd, the
 *    DSO plants the validated V4 descriptor instead (application usage
 *    0x0D:0x05 Touch Pad + feature report ID 2 -> 1) and reports the HAL's own
 *    byte count. V4 keeps the factory 62-byte frame layout, so no frame is
 *    rewritten; the gate state machine remains untouched.
 *    Fail-closed: any other descriptor content/size or a failed substitute
 *    leaves the factory HID shape in place and logs once, which the module's
 *    candidate descriptor contract turns into a handover abort (native touch
 *    off, legacy App path unaffected).
 *
 * 2. no-grab. The stock HAL calls EVIOCGRAB on five evdev nodes; grabbing
 *    event6 (Xiaomi_Touch_Input_0, Android's main touchscreen) is what makes
 *    tablet touch unusable while DP-in is active. EVIOCGRAB/EVIOCREVOKE on the
 *    reviewed node list are swallowed (return 0 without the kernel call) so
 *    Android InputReader and macOS both receive the panel. Every other ioctl
 *    is forwarded byte for byte. `$S/allow-grab` is translated by platform.sh
 *    into MIPAD_NATIVE_ALLOW_GRAB=1 and restores the kernel grab (diagnostic
 *    escape only).
 *
 * Logging: the only new lines are native-init once, desc-replaced once,
 * desc-write-unexpected/desc-replace-failed once, grab-suppressed once per
 * device, grab-allowed once per device and grab-unmatched once. No per-frame
 * or per-open logging (the existing FullSplitBounded frame log is unchanged).
 */
#ifndef FULLSPLIT_NATIVE_H
#define FULLSPLIT_NATIVE_H

#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syscall.h>

#include "native-v4-desc.h"

#define NATIVE_TAG "FullSplitBounded"
#define NATIVE_PATH_MAX 256
#define NATIVE_GRAB_SLOTS 8

#ifndef EVIOCGRAB
#define EVIOCGRAB 0x40044590L
#endif
#ifndef EVIOCREVOKE
#define EVIOCREVOKE 0x40044591L
#endif

/* Reviewed evdev nodes the stock HAL grabs. Basenames accepted because the
 * resolved /proc/self/fd path is /dev/input/event<N>. */
static const char NATIVE_NOGRAB_DEVS[] = "event6,event9,event11,event7,event8";

#ifdef FS_TEST_NATIVE
extern ssize_t FS_TEST_NATIVE_READLINK(const char *, char *, size_t);
extern ssize_t FS_TEST_NATIVE_WRITE(int, const void *, size_t);
extern int FS_TEST_NATIVE_IOCTL(int, int, void *);
#define NATIVE_READLINK(p, b, n) FS_TEST_NATIVE_READLINK((p), (b), (n))
#define NATIVE_SYS_WRITE(fd, b, n) FS_TEST_NATIVE_WRITE((fd), (b), (n))
#define NATIVE_SYS_IOCTL(fd, req, arg) FS_TEST_NATIVE_IOCTL((fd), (req), (arg))
#else
#define NATIVE_READLINK(p, b, n) readlink((p), (b), (n))
#define NATIVE_SYS_WRITE(fd, b, n) syscall(SYS_write, (fd), (b), (n))
#define NATIVE_SYS_IOCTL(fd, req, arg) syscall(SYS_ioctl, (fd), (req), (arg))
#endif

static int native_ready;
static int native_allow_grab;
static int native_desc_replaced;
static int native_error_logged;
static int native_grab_logged[NATIVE_GRAB_SLOTS];   /* per whitelist device */
static int native_unmatched_logged;
static char native_unmatched_path[NATIVE_PATH_MAX];
static char native_grab_paths[NATIVE_GRAB_SLOTS][NATIVE_PATH_MAX];

static void native_log(const char *fmt, ...)
{
	char line[400];
	int used;
	va_list ap;
	va_start(ap, fmt);
	used = vsnprintf(line, sizeof(line), fmt, ap);
	va_end(ap);
	if (used < 0) {
		return;
	}
	if ((size_t)used >= sizeof(line)) {
		line[sizeof(line) - 1] = 0;
	}
	__android_log_print(ANDROID_LOG_INFO, NATIVE_TAG, "%s", line);
}

static void native_init(void)
{
	const char *v;
	native_ready = 1;
	native_desc_replaced = 0;
	native_error_logged = 0;
	native_unmatched_logged = 0;
	native_unmatched_path[0] = 0;
	memset(native_grab_logged, 0, sizeof(native_grab_logged));
	memset(native_grab_paths, 0, sizeof(native_grab_paths));
	v = getenv("MIPAD_NATIVE_ALLOW_GRAB");
	native_allow_grab = v && v[0] == '1' && v[1] == 0;
	native_log("native init variant=V4 desc_sha=%s report_length=%d bytes=%d "
		   "no_grab=%d allow_grab=%d",
		   NATIVE_DESC_V4_SHA, 62, (int)NATIVE_DESC_V4_LEN,
		   native_allow_grab ? 0 : 1, native_allow_grab);
}

static void native_ensure(void)
{
	if (!native_ready) {
		native_init();
	}
}

#ifdef FS_TEST_NATIVE
/* Host tests re-run the environment resolution. */
static void native_reset_for_test(void)
{
	native_ready = 0;
	native_ensure();
}
#endif

static int native_fd_path(int fd, char *out, size_t n)
{
	char link[48];
	ssize_t r;
	if (fd < 0 || !out || !n) {
		return 0;
	}
	snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
	/* /proc/self/fd is authoritative: libc-internal closes (closedir et al.)
	 * can bypass the interposed close(), so a cached table could mis-label a
	 * reused descriptor (T4 hit exactly that on a grab fd). */
	r = NATIVE_READLINK(link, out, n - 1);
	if (r <= 0) {
		return 0;
	}
	out[r] = 0;
	return 1;
}

static int native_is_hid1_desc(const char *path)
{
	static const char suffix[] = "/functions/hid.usb1/report_desc";
	size_t len, slen = sizeof(suffix) - 1;
	if (!path || !strstr(path, "/config/usb_gadget/")) {
		return 0;
	}
	len = strlen(path);
	return len >= slen && !strcmp(path + len - slen, suffix);
}

static int native_grab_request(int request)
{
	return (unsigned long)request == (unsigned long)EVIOCGRAB ||
	       (unsigned long)request == (unsigned long)EVIOCREVOKE;
}

/* 0..NATIVE_GRAB_SLOTS-1 when the resolved path is one of the reviewed nodes,
 * -1 otherwise. */
static int native_nograb_index(const char *path)
{
	char list[sizeof(NATIVE_NOGRAB_DEVS)];
	char *save = NULL, *tok;
	const char *base;
	int idx = 0;
	if (!path || !*path) {
		return -1;
	}
	base = strrchr(path, '/');
	base = base ? base + 1 : path;
	snprintf(list, sizeof(list), "%s", NATIVE_NOGRAB_DEVS);
	for (tok = strtok_r(list, ",", &save); tok; tok = strtok_r(NULL, ",", &save), idx++) {
		if (idx >= NATIVE_GRAB_SLOTS) {
			break;
		}
		if (!strcmp(base, tok)) {
			return idx;
		}
	}
	return -1;
}

/* Descriptor substitution. Returns 1 when this call handled the write (result
 * in *out and errno set like a normal successful write), 0 to let the caller
 * forward it unchanged. */
static int native_desc_try_write(int fd, const void *buf, size_t n, ssize_t *out)
{
	char path[NATIVE_PATH_MAX];
	int saved = errno;
	ssize_t rc;
	if (n != NATIVE_DESC_FACTORY_LEN) {
		return 0;
	}
	native_ensure();
	if (!native_fd_path(fd, path, sizeof(path)) || !native_is_hid1_desc(path)) {
		return 0;
	}
	if (memcmp(buf, NATIVE_DESC_FACTORY, NATIVE_DESC_FACTORY_LEN) != 0) {
		if (!native_error_logged) {
			native_error_logged = 1;
			native_log("native-desc-write-unexpected variant=V4 len=%zu "
				   "expected_factory=%d action=forward_factory",
				   n, (int)NATIVE_DESC_FACTORY_LEN);
		}
		return 0;
	}
	rc = NATIVE_SYS_WRITE(fd, NATIVE_DESC_V4, NATIVE_DESC_V4_LEN);
	if (rc == (ssize_t)NATIVE_DESC_V4_LEN) {
		if (!native_desc_replaced) {
			native_desc_replaced = 1;
			native_log("native-desc-replaced variant=V4 factory_len=%zu "
				   "variant_len=%zu reported=%zu sha=%s", n,
				   (size_t)NATIVE_DESC_V4_LEN, n, NATIVE_DESC_V4_SHA);
		}
		errno = saved;
		*out = (ssize_t)n;
		return 1;
	}
	{
		int e = errno;
		if (!native_error_logged) {
			native_error_logged = 1;
			native_log("native-desc-replace-failed variant=V4 errno=%d "
				   "variant_len=%zu action=fallback_factory",
				   e, (size_t)NATIVE_DESC_V4_LEN);
		}
		/* Keep the session alive with the factory HID shape: write the HAL's
		 * original bytes. The module descriptor contract then aborts the
		 * handover, so native touch is off but nothing is left inconsistent. */
		rc = NATIVE_SYS_WRITE(fd, buf, n);
		if (rc >= 0) {
			errno = saved;
			*out = (ssize_t)n;
			return 1;
		}
		errno = e;
		*out = rc;
		return 1;
	}
}

/* --- hooks (native build only) ----------------------------------------- */
int ioctl(int fd, int request, ...)
{
	void *arg;
	int rc, e;
	char path[NATIVE_PATH_MAX];
	va_list ap;
	va_start(ap, request);
	/* Every Android ioctl caller passes the argument; ioctls that ignore it
	 * ignore the forwarded register just the same. */
	arg = va_arg(ap, void *);
	va_end(ap);
	native_ensure();
	if (native_grab_request(request) && native_fd_path(fd, path, sizeof(path))) {
		int idx = native_nograb_index(path);
		if (idx >= 0) {
			if (native_allow_grab) {
				if (!native_grab_logged[idx]) {
					native_grab_logged[idx] = 1;
					native_log("native-grab-allowed request=%s path=%s "
						   "allow_grab=1", (unsigned long)request ==
						   (unsigned long)EVIOCGRAB ? "EVIOCGRAB" : "EVIOCREVOKE",
						   path);
				}
			} else {
				if ((unsigned long)request == (unsigned long)EVIOCGRAB &&
				    !native_grab_logged[idx]) {
					native_grab_logged[idx] = 1;
					snprintf(native_grab_paths[idx], NATIVE_PATH_MAX, "%s", path);
					native_log("native-grab-suppressed path=%s "
						   "reason=dual_touch", path);
				}
				return 0;
			}
		} else if (!native_unmatched_logged ||
			   strcmp(native_unmatched_path, path)) {
			native_unmatched_logged = 1;
			snprintf(native_unmatched_path, sizeof(native_unmatched_path), "%s", path);
			native_log("native-grab-unmatched request=%s path=%s action=forward",
				   (unsigned long)request == (unsigned long)EVIOCGRAB ?
				   "EVIOCGRAB" : "EVIOCREVOKE", path);
		}
	}
	rc = (int)NATIVE_SYS_IOCTL(fd, request, arg);
	e = errno;
	errno = e;
	return rc;
}

static void native_ctor(void) __attribute__((constructor));
static void native_ctor(void)
{
	native_init();
}

#endif /* FULLSPLIT_NATIVE_H */
