/* Host stand-in for the NDK <android/log.h> (test builds only). */
#ifndef MIPAD_TEST_ANDROID_LOG_H
#define MIPAD_TEST_ANDROID_LOG_H

#define ANDROID_LOG_INFO 4

static inline int __android_log_print(int prio, const char *tag, const char *fmt, ...) {
    (void)prio; (void)tag; (void)fmt;
    return 0;
}

#endif
