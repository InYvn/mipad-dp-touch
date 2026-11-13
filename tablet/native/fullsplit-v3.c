/* v3: single-frame rewrite for the firmware's release artifacts.
 *
 * Two artifacts break the host's two-finger centroid at lift-off:
 *   1) a contact that is still active gets one axis zeroed (Y=0 then X=0)
 *      before its release frame;
 *   2) the surviving contact is compacted into the released slot in the same
 *      frame, so the host sees slot 0 jump from finger A to finger B.
 *
 * We rewrite one frame in place: restore the zeroed axis from the last valid
 * value, and keep every prior active contact in its prior slot for this frame
 * (vanished contacts are emitted released with their last valid coordinates).
 * No multi-frame split, no poison: exactly one write per input report.
 */
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <sys/types.h>

typedef ssize_t (*writer)(int, const void *, size_t);

#define MAXC 10
#define REPORT_LEN 62

typedef struct {
    uint64_t generation;
    int fd;
    int have_prior;
    unsigned char prior[REPORT_LEN];
    unsigned short slot_x[MAXC], slot_y[MAXC];
    unsigned char slot_id[MAXC];
    unsigned char out[REPORT_LEN];
} RX;

static void rx_reset(RX *s) {
    memset(s, 0, sizeof(*s));
    s->fd = -1;
}

typedef struct {
    int count;
    int id[MAXC];
    int flags[MAXC];
    int x[MAXC];
    int y[MAXC];
} Frame;

static int frame_parse(const unsigned char *b, Frame *f) {
    int n = b[61];
    if (n > MAXC) return -1;
    f->count = n;
    for (int i = 0; i < n; i++) {
        int o = 1 + 6 * i;
        f->flags[i] = b[o] & 3;
        f->id[i] = b[o + 1];
        f->x[i] = b[o + 2] | (b[o + 3] << 8);
        f->y[i] = b[o + 4] | (b[o + 5] << 8);
    }
    return 0;
}

static void frame_put(unsigned char *b, const Frame *f) {
    memset(b, 0, REPORT_LEN);
    b[0] = 1;
    for (int i = 0; i < MAXC; i++) {
        int o = 1 + 6 * i;
        b[o] = (unsigned char)f->flags[i];
        b[o + 1] = (unsigned char)f->id[i];
        b[o + 2] = (unsigned char)(f->x[i] & 0xff);
        b[o + 3] = (unsigned char)((f->x[i] >> 8) & 0xff);
        b[o + 4] = (unsigned char)(f->y[i] & 0xff);
        b[o + 5] = (unsigned char)((f->y[i] >> 8) & 0xff);
    }
    b[61] = (unsigned char)f->count;
}

static ssize_t rx_send(RX *s, int target, int fd, const unsigned char *buf, size_t n, writer fn) {
    if (!target) return fn(fd, buf, n);
    if (n != REPORT_LEN || buf[0] != 1) return fn(fd, buf, n); /* pen and others pass through */
    if (s->fd != fd) { rx_reset(s); s->fd = fd; }

    Frame cur;
    memset(&cur, 0, sizeof(cur));
    if (frame_parse(buf, &cur)) return fn(fd, buf, n);

    /* 1) keep every slot's last valid coordinates.
     * Active contacts: restore a single zeroed axis (firmware Y=0/X=0 artifact).
     * Released or out-of-count slots: the host still reads them, so a released
     * slot whose coordinates snap to 0 is exactly what moves the centroid. */
    for (int i = 0; i < MAXC; i++) {
        if (i < cur.count && (cur.flags[i] & 3)) {
            if (cur.x[i] == 0 && s->slot_x[i] != 0) cur.x[i] = s->slot_x[i];
            if (cur.y[i] == 0 && s->slot_y[i] != 0) cur.y[i] = s->slot_y[i];
        } else if (cur.x[i] == 0 && cur.y[i] == 0 && s->slot_x[i] != 0) {
            cur.x[i] = s->slot_x[i];
            cur.y[i] = s->slot_y[i];
            if (i >= cur.count) {
                /* keep the released identity in the unused slot */
                cur.flags[i] = 0;
                cur.id[i] = s->slot_id[i];
            }
        }
    }

    /* 2) slot compaction: keep prior active contacts in their prior slots. */
    if (s->have_prior) {
        Frame prev;
        memset(&prev, 0, sizeof(prev));
        if (frame_parse(s->prior, &prev) == 0 && prev.count > cur.count) {
            Frame out;
            memset(&out, 0, sizeof(out));
            out.count = 0;
            for (int p = 0; p < prev.count; p++) {
                if (!(prev.flags[p] & 3)) continue;
                int found = -1;
                for (int c = 0; c < cur.count; c++) {
                    if (cur.id[c] == prev.id[p]) { found = c; break; }
                }
                int o = out.count++;
                if (found >= 0) {
                    out.flags[o] = cur.flags[found] & 3;
                    out.id[o] = prev.id[p];
                    out.x[o] = cur.x[found];
                    out.y[o] = cur.y[found];
                } else {
                    out.flags[o] = 0; /* released, keep identity and last position */
                    out.id[o] = prev.id[p];
                    out.x[o] = s->slot_x[p];
                    out.y[o] = s->slot_y[p];
                }
            }
            cur = out;
        }
    }

    frame_put(s->out, &cur);
    for (int i = 0; i < cur.count; i++) {
        if (cur.flags[i] & 3) {
            s->slot_x[i] = (unsigned short)(cur.x[i] & 0xffff);
            s->slot_y[i] = (unsigned short)(cur.y[i] & 0xffff);
            s->slot_id[i] = (unsigned char)cur.id[i];
        }
    }
    memcpy(s->prior, s->out, REPORT_LEN);
    s->have_prior = 1;

    return fn(fd, s->out, REPORT_LEN);
}
