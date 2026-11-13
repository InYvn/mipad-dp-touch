/* v4: stable contact-ID -> slot mapping for the whole gesture.
 *
 * The firmware compacts the surviving contact into slot 0 on lift-off and
 * zeroes the released slot's coordinates in the same frame. The host's pinch
 * state is slot based, so both move the centroid at the moment a finger lifts.
 *
 * We assign each contact ID a fixed slot on first appearance and keep it there
 * until the gesture ends. A released contact keeps its slot, its identity and
 * its last valid coordinates, with the active bits cleared.
 */
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <sys/types.h>

typedef ssize_t (*writer)(int, const void *, size_t);

#define MAXC 10
#define REPORT_LEN 62
#define FREE_SLOT 0xff

typedef struct {
    uint64_t generation;
    int fd;
    unsigned char slot_id[MAXC];        /* id currently assigned to the slot */
    unsigned short slot_x[MAXC];
    unsigned short slot_y[MAXC];
    signed char id_slot[256];           /* id -> slot, -1 = unassigned */
    unsigned char out[REPORT_LEN];
} RX;

typedef struct {
    int count;
    int id[MAXC];
    int flags[MAXC];
    int x[MAXC];
    int y[MAXC];
} Frame;

static void rx_reset(RX *s) {
    memset(s, 0, sizeof(*s));
    s->fd = -1;
    for (int i = 0; i < MAXC; i++) s->slot_id[i] = FREE_SLOT;
    for (int i = 0; i < 256; i++) s->id_slot[i] = -1;
}

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

static void frame_put(unsigned char *b, const RX *s, const int *active_slot, int active_count) {
    memset(b, 0, REPORT_LEN);
    b[0] = 1;
    for (int i = 0; i < MAXC; i++) {
        if (s->slot_id[i] == FREE_SLOT) continue;
        int o = 1 + 6 * i;
        b[o] = (unsigned char)(active_slot[i] ? 3 : 0);
        b[o + 1] = s->slot_id[i];
        b[o + 2] = (unsigned char)(s->slot_x[i] & 0xff);
        b[o + 3] = (unsigned char)((s->slot_x[i] >> 8) & 0xff);
        b[o + 4] = (unsigned char)(s->slot_y[i] & 0xff);
        b[o + 5] = (unsigned char)((s->slot_y[i] >> 8) & 0xff);
    }
    b[61] = (unsigned char)active_count;
}

static ssize_t rx_send(RX *s, int target, int fd, const unsigned char *buf, size_t n, writer fn) {
    if (!target) return fn(fd, buf, n);
    if (n != REPORT_LEN || buf[0] != 1) return fn(fd, buf, n);
    if (s->fd != fd) { rx_reset(s); s->fd = fd; }

    Frame cur;
    memset(&cur, 0, sizeof(cur));
    if (frame_parse(buf, &cur)) return fn(fd, buf, n);

    /* A frame with no contacts ends the gesture: release every slot and reset. */
    int active = 0;
    for (int i = 0; i < cur.count; i++) if (cur.flags[i] & 3) active++;

    if (active == 0) {
        /* Emit the release with the last valid coordinates still in their
         * slots, then forget the gesture. Zeroing here is what makes the host
         * see the final contact snap to (0,0) at lift-off. */
        int none[MAXC] = {0};
        frame_put(s->out, s, none, 0);
        ssize_t r = fn(fd, s->out, REPORT_LEN);
        for (int i = 0; i < MAXC; i++) s->slot_id[i] = FREE_SLOT;
        for (int i = 0; i < 256; i++) s->id_slot[i] = -1;
        return r;
    }

    /* Place every active contact in its assigned slot (assign on first sight). */
    int used[MAXC];
    memset(used, 0, sizeof(used));
    for (int i = 0; i < cur.count; i++) {
        if (!(cur.flags[i] & 3)) continue;
        int id = cur.id[i];
        int slot = s->id_slot[id];
        if (slot < 0) {
            for (int k = 0; k < MAXC; k++) {
                if (s->slot_id[k] == FREE_SLOT) { slot = k; break; }
            }
            if (slot < 0) slot = i; /* no free slot: fall back to the frame slot */
            s->id_slot[id] = (signed char)slot;
            s->slot_id[slot] = (unsigned char)id;
        }
        used[slot] = 1;
        s->slot_x[slot] = (unsigned short)(cur.x[i] & 0xffff);
        s->slot_y[slot] = (unsigned short)(cur.y[i] & 0xffff);
    }

    frame_put(s->out, s, used, active);
    return fn(fd, s->out, REPORT_LEN);
}
