/* Host-only stub of the NDK's <android/log.h>.
 *
 * tablet/native/test-interposer.c compiles the real LD_PRELOAD interposer on a normal
 * host, where <android/log.h> does not exist. Only the log entry point is needed and
 * the host tests never reach a target fd, so the stub just swallows the messages.
 * The DSO build (tablet/build_dso.sh) does not add this directory to the include path
 * and therefore still uses the NDK header.
 */
#ifndef FS_TEST_ANDROID_LOG_H
#define FS_TEST_ANDROID_LOG_H

#include <stdarg.h>
#include <stdio.h>

#define ANDROID_LOG_INFO 4

static void fs_test_android_log(int prio, const char *tag, const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	fprintf(stderr, "%s: ", tag);
	vfprintf(stderr, fmt, ap);
	fputc('\n', stderr);
	va_end(ap);
	(void)prio;
}

#define __android_log_print fs_test_android_log

#endif
