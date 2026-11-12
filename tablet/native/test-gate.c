/* Host-side unit tests for the fullsplit write gate.
 *
 * The gate ships as tablet/module/fullsplit-bounded-v2.so: the LD_PRELOAD interposer
 * (fullsplit-bounded-interposer.c) re-routes write(2) for /dev/hidg1 into rx_send() from
 * fullsplit-bounded.c, which wraps the core state machine in fullsplit.c with a bounded
 * retry writer. This test includes those two translation units unchanged, replaces the
 * kernel write with a scripted fake writer and drives rx_send() directly, so the gate
 * can be exercised on a normal host without the NDK and without a device:
 *
 *   cc -Wall -Wextra -O2 -o test-gate tablet/native/test-gate.c
 *   ./test-gate
 *
 * Exit status 0 means every scenario passed. Scenarios S1..S8 cover the fix brief;
 * C1..C3 pin the same rules in the core (rx_send_legacy), so the guarantee cannot be
 * lost by a change in the bounded adapter.
 *
 * Fail-closed contract under test:
 *   - a failure that wrote nothing (s->next == 0) is recoverable and must NOT poison;
 *   - a failure after part of the transaction reached the host keeps poison = 1;
 *   - poison may only be cleared by a whole release-all (report 1, zero contacts) write;
 *   - the pen report (report 3) and non-target fds keep their pass-through behaviour;
 *   - the 2 -> 1 contact expansion emits exactly the same bytes as before the fix.
 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "fullsplit-bounded.c"

/* ----------------------------------------------------------------- fake writer -- */
#define MAXW 64
typedef struct {
	ssize_t rc;
	int err;
} reply;

static reply script[MAXW];
static int script_len, script_pos;
static reply fallback = {62, 0};
static int wcount;
static unsigned char wframe[MAXW][62];
static size_t wlen[MAXW];
static int wfd[MAXW];

static void fw_reset(void)
{
	script_len = script_pos = wcount = 0;
	fallback.rc = 62;
	fallback.err = 0;
}

static void fw_ok(void)
{
	fw_reset();
}

static void fw_always(ssize_t rc, int err)
{
	fw_reset();
	fallback.rc = rc;
	fallback.err = err;
}

static void fw_script(const reply *steps, int n)
{
	fw_reset();
	if (n > MAXW)
		n = MAXW;
	memcpy(script, steps, sizeof(reply) * n);
	script_len = n;
}

static ssize_t fake_write(int fd, const void *b, size_t n)
{
	if (wcount < MAXW) {
		memcpy(wframe[wcount], b, n < 62 ? n : 62);
		wlen[wcount] = n;
		wfd[wcount] = fd;
		wcount++;
	}
	reply r = script_pos < script_len ? script[script_pos++] : fallback;
	errno = r.err;
	return r.rc;
}

/* ------------------------------------------------------------------ checkers -- */
static int checks, fails;
static void ok(int cond, const char *what)
{
	checks++;
	if (!cond) {
		fails++;
		printf("      FAIL  %s\n", what);
	}
}

static int64_t now_ms(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static int bytes_of_hex(unsigned char out[62], const char *hex)
{
	if (strlen(hex) != 124)
		return -1;
	for (int i = 0; i < 62; i++) {
		unsigned v;
		if (sscanf(hex + 2 * i, "%2x", &v) != 1)
			return -1;
		out[i] = (unsigned char)v;
	}
	return 0;
}

static void dump_frame(int idx)
{
	if (idx >= wcount) {
		printf("      (no write #%d)\n", idx);
		return;
	}
	printf("      write #%d len=%zu fd=%d ", idx, wlen[idx], wfd[idx]);
	for (size_t i = 0; i < wlen[idx] && i < 62; i++)
		printf("%02x", wframe[idx][i]);
	printf("\n");
}

static void check_frame(int idx, const char *hex, const char *what)
{
	unsigned char expect[62];
	ok(idx < wcount, what);
	if (idx >= wcount) {
		dump_frame(idx);
		return;
	}
	ok(wlen[idx] == 62, "frame is 62 bytes");
	if (bytes_of_hex(expect, hex)) {
		ok(0, "golden literal is valid");
		return;
	}
	if (memcmp(wframe[idx], expect, 62)) {
		dump_frame(idx);
		printf("      expected  %s\n", hex);
		ok(0, what);
	} else {
		ok(1, what);
	}
}

static void check_written(int idx, const unsigned char *f, size_t n, int fd, const char *what)
{
	ok(idx < wcount, what);
	if (idx >= wcount) {
		dump_frame(idx);
		return;
	}
	int same = wlen[idx] == n && wfd[idx] == fd && !memcmp(wframe[idx], f, n);
	if (!same) {
		dump_frame(idx);
		ok(0, what);
	} else {
		ok(1, what);
	}
}

/* -------------------------------------------------------------------- fixtures -- */
static RX gate;
static uint64_t gen;
static int pfd[2]; /* pollable, never written through: the fake writer intercepts */

static void gate_open(int explicit_release)
{
	memset(&gate, 0, sizeof(gate));
	gate.explicit_release = explicit_release;
	rx_begin_session(&gate, ++gen);
}

/* switches, contact id, X(lo,hi), Y(lo,hi) - the layout the firmware uses. */
static void frame_one(unsigned char f[62], int id, int x, int y)
{
	memset(f, 0, 62);
	f[0] = 1;
	f[1] = 3;
	f[2] = (unsigned char)id;
	f[3] = (unsigned char)(x & 0xff);
	f[4] = (unsigned char)(x >> 8);
	f[5] = (unsigned char)(y & 0xff);
	f[6] = (unsigned char)(y >> 8);
	f[61] = 1;
}

static void frame_two(unsigned char f[62], int id0, int x0, int y0, int id1, int x1, int y1)
{
	frame_one(f, id0, x0, y0);
	f[7] = 3;
	f[8] = (unsigned char)id1;
	f[9] = (unsigned char)(x1 & 0xff);
	f[10] = (unsigned char)(x1 >> 8);
	f[11] = (unsigned char)(y1 & 0xff);
	f[12] = (unsigned char)(y1 >> 8);
	f[61] = 2;
}

static void frame_three(unsigned char f[62])
{
	memset(f, 0, 62);
	f[0] = 1;
	for (int i = 0; i < 3; i++) {
		int o = 1 + 6 * i;
		f[o] = 3;
		f[o + 1] = (unsigned char)i;
		f[o + 2] = (unsigned char)(0x40 + i);
		f[o + 3] = 0;
		f[o + 4] = (unsigned char)(0x50 + i);
		f[o + 5] = 0;
	}
	f[61] = 3;
}

/* report 1, contact count 0: the only frame a poisoned gate may try to write. */
static void frame_release_all(unsigned char f[62])
{
	memset(f, 0, 62);
	f[0] = 1;
}

/* report 1, contact count 1, tip switch 0: a released contact, NOT release-all. */
static void frame_release_contact(unsigned char f[62])
{
	memset(f, 0, 62);
	f[0] = 1;
	f[61] = 1;
}

static void frame_pen(unsigned char f[62])
{
	memset(f, 0, 62);
	f[0] = 3;
	for (int i = 1; i < 62; i++)
		f[i] = (unsigned char)(0x80 + i);
	f[61] = 0x2a;
}

/* Establish the poisoned state the device hit: a 2 -> 1 contact split whose release
 * prefix was written and whose second packet failed. Leaves the gate poisoned. */
static void make_poisoned(void)
{
	unsigned char f[62];
	reply steps[2] = {{62, 0}, {-1, EIO}};

	gate_open(1);
	fw_ok();
	frame_two(f, 4, 0x1111, 0x2222, 7, 0x3333, 0x4444);
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62, "setup: prior two-contact frame accepted");
	fw_script(steps, 2);
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == -1, "setup: split fails midway");
	ok(gate.poison == 1, "setup: gate poisoned after a partial transaction");
}

/* ------------------------------------------------------- regression goldens (S8) -- */
/* Captured from the pre-fix gate (git 7a06fc2) before the failure handling changed;
 * these bytes must stay identical. */
static const char *const GOLD_A_WRITE =
	"0103003412785600000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001";
static const char *const GOLD_TWO_CONTACT =
	"0103041111222203073333444400000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002";
static const char *const GOLD_SPLIT_EXPLICIT_0 =
	"0100041111222200073333444400000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002";
static const char *const GOLD_SPLIT_EXPLICIT_1 =
	"0100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000";
static const char *const GOLD_SPLIT_EXPLICIT_2 =
	"0103003412785600000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001";
static const char *const GOLD_SPLIT_PLAIN_0 =
	"0100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000";
static const char *const GOLD_SPLIT_PLAIN_1 =
	"0103003412785600000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001";
static const char *const GOLD_AFTER_SPLIT_TWO =
	"0103011000200003023000400000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002";
static const char *const GOLD_SPLIT3_EXPLICIT_0 =
	"0100004000500000014100510000024200520000000000000000000000000000000000000000000000000000000000000000000000000000000000000003";
static const char *const GOLD_SPLIT3_EXPLICIT_1 =
	"0100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000";
static const char *const GOLD_SPLIT3_EXPLICIT_2 =
	"0103003412785600000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001";
static const char *const GOLD_RELEASE_ONE_CONTACT =
	"0100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001";

/* ------------------------------------------------------------------ scenarios -- */

/* S1: the boot-time candidate. Its very first write returns ESHUTDOWN for as long as the
 * bounded deadline allows. Nothing was written, so the gate must stay usable and the
 * next frame has to go through. */
static void s1_first_write_eshutdown(void)
{
	unsigned char f[62];
	printf("--- S1  first write ESHUTDOWN -> no poison, next frame succeeds\n");
	gate_open(1);
	fw_always(-1, ESHUTDOWN);
	frame_release_all(f);
	int64_t t0 = now_ms();
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	int err = errno;
	int64_t elapsed = now_ms() - t0;
	int attempts = wcount;
	printf("      rc=%zd errno=%d elapsed=%lldms attempts=%d poison=%d\n",
	       rc, err, (long long)elapsed, attempts, gate.poison);
	ok(rc == -1, "first frame reports a failure");
	ok(err == ETIMEDOUT, "deadline exhausted (ETIMEDOUT)");
	ok(gate.poison == 0, "gate is NOT poisoned");
	ok(gate.pending == 0, "pending attempt dropped");
	ok(gate.active == 0 && gate.prior[0] == 0, "prior/active kept (host untouched)");
	ok(attempts >= 2, "bounded retry tried again inside the deadline");
	ok(attempts <= 40, "retry attempts stay bounded");
	ok(elapsed >= FS_DEADLINE_MS - 20 && elapsed <= 1000, "bounded by the existing deadline");

	fw_ok();
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      next frame rc=%zd poison=%d\n", rc, gate.poison);
	ok(rc == 62, "next frame succeeds");
	ok(gate.poison == 0, "still not poisoned");
	check_written(0, f, 62, pfd[1], "next frame written whole");
}

/* S1b: ESHUTDOWN is genuinely transient (one failed write, then the endpoint accepts).
 * The bounded adapter retries inside the deadline, so even the first transaction lands. */
static void s1b_eshutdown_recovers_inside_deadline(void)
{
	unsigned char f[62];
	reply steps[2] = {{-1, ESHUTDOWN}, {62, 0}};
	printf("--- S1b ESHUTDOWN once, endpoint recovers -> first frame still written\n");
	gate_open(1);
	fw_script(steps, 2);
	frame_one(f, 0, 0x1234, 0x5678);
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      rc=%zd attempts=%d poison=%d\n", rc, wcount, gate.poison);
	ok(rc == 62, "first frame written after the retry");
	ok(gate.poison == 0, "no poison");
	ok(wcount == 2, "one retry, not more");
	check_written(1, f, 62, pfd[1], "retried packet is the input frame");
}

/* S2: EAGAIN for the whole deadline. Same rule: bounded, no poison, next frame works. */
static void s2_first_write_eagain_deadline(void)
{
	unsigned char f[62];
	printf("--- S2  first write EAGAIN past deadline -> error, no poison\n");
	gate_open(1);
	fw_always(-1, EAGAIN);
	frame_one(f, 0, 0x1234, 0x5678);
	int64_t t0 = now_ms();
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	int err = errno;
	int64_t elapsed = now_ms() - t0;
	int attempts = wcount;
	printf("      rc=%zd errno=%d elapsed=%lldms attempts=%d poison=%d\n",
	       rc, err, (long long)elapsed, attempts, gate.poison);
	ok(rc == -1, "frame reports a failure");
	ok(err == ETIMEDOUT, "deadline exhausted (ETIMEDOUT)");
	ok(gate.poison == 0, "gate is NOT poisoned");
	ok(gate.pending == 0, "pending dropped");
	ok(attempts >= 2, "retried inside the deadline");
	ok(attempts <= 40, "retry attempts stay bounded");
	ok(elapsed >= FS_DEADLINE_MS - 20 && elapsed <= 1000, "no unbounded wait");

	fw_ok();
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62, "next frame succeeds");
	ok(gate.poison == 0, "still not poisoned");
}

/* S3: the expanded transaction emitted its release prefix, then the next packet failed.
 * The host state cannot be rolled back, so this one must stay poisoned. */
static void s3_partial_transaction_poisons(void)
{
	printf("--- S3  failure after a partially emitted transaction -> poison stays\n");
	make_poisoned();
	ok(wcount == 2, "release prefix + failed packet attempted");
	check_frame(0, GOLD_SPLIT_EXPLICIT_0, "emitted prefix is the legacy sequence");
	check_frame(1, GOLD_SPLIT_EXPLICIT_1, "second packet is the legacy empty frame");
	ok(gate.pending == 0, "pending cleared after the failure");
	ok(wcount == 2, "only the prefix and the failed packet were attempted");
}

/* S4: poisoned, then a release-all report proves the host has no contacts. Writing it
 * whole clears the poison and resets the state machine. */
static void s4_release_all_heals(void)
{
	unsigned char f[62];
	printf("--- S4  poisoned + release-all written -> poisoned cleared\n");
	make_poisoned();
	fw_ok();
	int before = wcount;
	frame_release_all(f);
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      heal rc=%zd poison=%d active=%d\n", rc, gate.poison, gate.active);
	ok(rc == 62, "release-all accepted");
	ok(gate.poison == 0, "poison cleared");
	ok(gate.pending == 0, "no pending transaction");
	ok(gate.active == 0, "state reset to contact-free");
	check_written(before, f, 62, pfd[1], "release-all written whole, byte-exact");

	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      next contact frame rc=%zd poison=%d\n", rc, gate.poison);
	ok(rc == 62, "next contact frame passes");
	ok(gate.poison == 0, "poison stays cleared");
	ok(gate.active == 1, "state tracks the new contact");
}

/* S4b: the healing write itself fails - fail-closed, the poison must survive, and the
 * next release-all may try again. */
static void s4b_failed_heal_stays_poisoned(void)
{
	unsigned char f[62];
	printf("--- S4b poisoned + release-all write fails -> still poisoned\n");
	make_poisoned();
	fw_always(-1, EIO);
	frame_release_all(f);
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      heal rc=%zd errno=%d poison=%d\n", rc, errno, gate.poison);
	ok(rc == -1, "failed healing write reported");
	ok(gate.poison == 1, "gate stays poisoned (fail-closed)");

	fw_ok();
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      retry heal rc=%zd poison=%d\n", rc, gate.poison);
	ok(rc == 62, "retry heals once the write succeeds");
	ok(gate.poison == 0, "poison cleared");
}

/* S5: poisoned and the frame claims contacts - reject with EIO, do not touch the fd. */
static void s5_poisoned_contact_frame_rejected(void)
{
	unsigned char f[62];
	printf("--- S5  poisoned + non-zero contact frame -> EIO\n");
	make_poisoned();
	fw_ok();
	int before = wcount;
	frame_one(f, 0, 0x1234, 0x5678);
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      contact frame rc=%zd errno=%d writes=%d\n", rc, errno, wcount);
	ok(rc == -1 && errno == EIO, "contact frame rejected with EIO");
	ok(wcount == before, "no write attempted");
	ok(gate.poison == 1, "still poisoned");

	/* a released contact (count 1, tip clear) is not a release-all: still rejected */
	frame_release_contact(f);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      count=1 release rc=%zd errno=%d writes=%d\n", rc, errno, wcount);
	ok(rc == -1 && errno == EIO, "count=1 release frame still rejected");
	ok(wcount == before, "no write attempted");
	ok(gate.poison == 1, "still poisoned");
}

/* S6: the pen report passes through while poisoned and does not clear the poison. */
static void s6_poisoned_pen_passes(void)
{
	unsigned char f[62];
	printf("--- S6  poisoned + report ID 3 (pen) -> passes through\n");
	make_poisoned();
	fw_ok();
	int before = wcount;
	frame_pen(f);
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      pen rc=%zd writes=%d poison=%d\n", rc, wcount, gate.poison);
	ok(rc == 62, "pen frame accepted");
	check_written(before, f, 62, pfd[1], "pen frame written byte-exact");
	ok(gate.poison == 1, "pen does not clear the poison");
}

/* S7: non-target fds and non-report payloads keep byte-exact pass-through semantics. */
static void s7_passthrough(void)
{
	unsigned char f[62], other[62];
	printf("--- S7  non-target fd / non-report payload pass-through\n");
	gate_open(1);
	fw_ok();
	frame_one(f, 0, 0x1234, 0x5678);
	int before = wcount;
	ssize_t rc = rx_send(&gate, 0, 77, f, 62, fake_write);
	printf("      target=0 rc=%zd writes=%d\n", rc, wcount - before);
	ok(rc == 62, "non-target call returns the writer result");
	check_written(before, f, 62, 77, "non-target fd passes through byte-exact");
	ok(gate.poison == 0 && gate.pending == 0 && gate.active == 0, "gate state untouched");

	/* a frame shorter than a full report is not a transaction */
	reply short_ok = {8, 0};
	fw_script(&short_ok, 1);
	before = wcount;
	rc = rx_send(&gate, 1, 77, f, 8, fake_write);
	printf("      short frame rc=%zd writes=%d\n", rc, wcount - before);
	ok(rc == 8, "short frame returns the writer result");
	ok(wcount == before + 1 && wlen[before] == 8 && wfd[before] == 77 &&
		   !memcmp(wframe[before], f, 8), "short frame passed through byte-exact");

	/* report id != 1 is not a report-1 transaction */
	fw_ok();
	memset(other, 0, 62);
	other[0] = 2;
	before = wcount;
	rc = rx_send(&gate, 1, 77, other, 62, fake_write);
	printf("      report 2 rc=%zd writes=%d\n", rc, wcount - before);
	ok(rc == 62, "report-2 payload returns the writer result");
	ok(wcount == before + 1 && wfd[before] == 77 && !memcmp(wframe[before], other, 62),
	   "report-2 payload passed through byte-exact");
}

/* S8: the expansion algorithm itself must not change - golden bytes from the old gate. */
static void s8_expansion_goldens(void)
{
	unsigned char f[62];
	printf("--- S8  expansion output sequence matches the pre-fix gate\n");

	/* one contact, no split */
	gate_open(1);
	fw_ok();
	frame_one(f, 0, 0x1234, 0x5678);
	ssize_t rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62 && wcount == 1, "single contact: one packet");
	check_frame(0, GOLD_A_WRITE, "single contact packet unchanged");

	/* two contacts -> one contact, explicit release prefix */
	gate_open(1);
	fw_ok();
	frame_two(f, 4, 0x1111, 0x2222, 7, 0x3333, 0x4444);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62 && wcount == 1, "two contacts: one packet");
	check_frame(0, GOLD_TWO_CONTACT, "two-contact packet unchanged");
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62 && wcount == 4, "2->1 split: three packets");
	check_frame(1, GOLD_SPLIT_EXPLICIT_0, "split: release prefix unchanged");
	check_frame(2, GOLD_SPLIT_EXPLICIT_1, "split: empty frame unchanged");
	check_frame(3, GOLD_SPLIT_EXPLICIT_2, "split: single contact unchanged");
	ok(gate.active == 1, "split: state tracks one contact");

	/* two contacts -> one contact, no explicit release prefix */
	gate_open(0);
	fw_ok();
	frame_two(f, 4, 0x1111, 0x2222, 7, 0x3333, 0x4444);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62 && wcount == 1, "no-prefix: two-contact packet");
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62 && wcount == 3, "no-prefix: two packets");
	check_frame(1, GOLD_SPLIT_PLAIN_0, "no-prefix: empty frame unchanged");
	check_frame(2, GOLD_SPLIT_PLAIN_1, "no-prefix: single contact unchanged");

	/* after the split, a two-contact frame is a plain packet again */
	gate_open(1);
	fw_ok();
	frame_two(f, 4, 0x1111, 0x2222, 7, 0x3333, 0x4444);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62, "after-split setup");
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62, "after-split: split");
	frame_two(f, 1, 0x10, 0x20, 2, 0x30, 0x40);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62 && wcount == 5, "after-split: two contacts again");
	check_frame(4, GOLD_AFTER_SPLIT_TWO, "after-split two-contact packet unchanged");

	/* three active contacts: the prefix has to carry all of them in one packet */
	gate_open(1);
	fw_ok();
	frame_three(f);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62, "three-contact setup");
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62 && wcount == 4, "3->1 split: three packets");
	check_frame(1, GOLD_SPLIT3_EXPLICIT_0, "3->1 split: release prefix unchanged");
	check_frame(2, GOLD_SPLIT3_EXPLICIT_1, "3->1 split: empty frame unchanged");
	check_frame(3, GOLD_SPLIT3_EXPLICIT_2, "3->1 split: single contact unchanged");

	/* a single released contact is written as-is, not expanded */
	gate_open(1);
	fw_ok();
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62, "release-one setup");
	frame_release_contact(f);
	rc = rx_send(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62 && wcount == 2, "released contact: one packet");
	check_frame(1, GOLD_RELEASE_ONE_CONTACT, "released-contact packet unchanged");
	ok(gate.active == 0, "released contact: no active contact");
}

/* C1: the core itself refuses to poison when nothing was written. */
static void c1_core_no_poison_without_output(void)
{
	unsigned char f[62];
	printf("--- C1  core: first write fails -> no poison (adapter independent)\n");
	gate_open(1);
	fw_always(-1, ESHUTDOWN);
	frame_release_all(f);
	ssize_t rc = rx_send_legacy(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      rc=%zd errno=%d poison=%d pending=%d\n", rc, errno, gate.poison, gate.pending);
	ok(rc == -1, "core reports the failure");
	ok(errno == ESHUTDOWN, "core preserves the writer errno");
	ok(gate.poison == 0, "core did not poison");
	ok(gate.pending == 0, "core dropped the attempt");
}

/* C2: the core poisons once part of the transaction was emitted. */
static void c2_core_partial_poisons(void)
{
	unsigned char f[62];
	reply steps[2] = {{62, 0}, {-1, EIO}};
	printf("--- C2  core: failure after a partial transaction -> poison\n");
	gate_open(1);
	fw_ok();
	frame_two(f, 4, 0x1111, 0x2222, 7, 0x3333, 0x4444);
	ssize_t rc = rx_send_legacy(&gate, 1, pfd[1], f, 62, fake_write);
	ok(rc == 62, "core setup: two-contact frame");
	fw_script(steps, 2);
	frame_one(f, 0, 0x1234, 0x5678);
	rc = rx_send_legacy(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      rc=%zd errno=%d poison=%d\n", rc, errno, gate.poison);
	ok(rc == -1, "core reports the failure");
	ok(gate.poison == 1, "core poisoned after a partial transaction");
}

/* C3: the core owns pen pass-through and release-all recovery. */
static void c3_core_pen_and_heal(void)
{
	unsigned char f[62];
	printf("--- C3  core: pen passes while poisoned, release-all heals\n");
	make_poisoned();
	fw_ok();
	frame_pen(f);
	ssize_t rc = rx_send_legacy(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      pen rc=%zd poison=%d\n", rc, gate.poison);
	ok(rc == 62, "core lets the pen through");
	ok(gate.poison == 1, "core keeps the poison for report-1 frames");
	frame_release_all(f);
	rc = rx_send_legacy(&gate, 1, pfd[1], f, 62, fake_write);
	printf("      release-all rc=%zd poison=%d active=%d\n", rc, gate.poison, gate.active);
	ok(rc == 62, "core writes the release-all");
	ok(gate.poison == 0, "core clears the poison");
	ok(gate.active == 0, "core resets the contact state");
}

int main(void)
{
	if (pipe(pfd)) {
		perror("pipe");
		return 2;
	}
	printf("fullsplit gate host tests (tablet/native/test-gate.c)\n\n");
	s1_first_write_eshutdown();
	s1b_eshutdown_recovers_inside_deadline();
	s2_first_write_eagain_deadline();
	s3_partial_transaction_poisons();
	s4_release_all_heals();
	s4b_failed_heal_stays_poisoned();
	s5_poisoned_contact_frame_rejected();
	s6_poisoned_pen_passes();
	s7_passthrough();
	s8_expansion_goldens();
	c1_core_no_poison_without_output();
	c2_core_partial_poisons();
	c3_core_pen_and_heal();
	printf("\n%d checks, %d failure(s)\n", checks, fails);
	return fails ? 1 : 0;
}
