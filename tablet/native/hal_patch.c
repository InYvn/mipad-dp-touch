/* mipad-hal-patch: add one DT_NEEDED entry to the vendor DP-in HAL.
 *
 * The Android linker rejects in-place edits of the dynamic array (verified),
 * so this copies the input and relocates .dynstr/.dynamic into the zero-filled
 * gap that follows the PT_LOAD holding PT_DYNAMIC, then extends that PT_LOAD
 * over the new data. No program header is added, nothing else moves.
 *
 * Refuses any layout it does not recognise: the caller must pass a hash
 * whitelist, this tool only checks structural invariants.
 *
 * usage: mipad-hal-patch <input> <output> <needed-path>
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <sys/stat.h>
#include <unistd.h>

typedef struct {
    uint64_t off, vaddr, filesz, memsz, align;
    uint32_t type, flags;
} Phdr;

static int read_file(const char *p, unsigned char **buf, size_t *len) {
    FILE *f = fopen(p, "rb");
    if (!f) return -1;
    if (fseek(f, 0, SEEK_END)) { fclose(f); return -1; }
    long n = ftell(f);
    if (n <= 0) { fclose(f); return -1; }
    rewind(f);
    unsigned char *b = malloc((size_t)n);
    if (!b) { fclose(f); return -1; }
    if (fread(b, 1, (size_t)n, f) != (size_t)n) { free(b); fclose(f); return -1; }
    fclose(f);
    *buf = b; *len = (size_t)n;
    return 0;
}

static int write_file(const char *p, const unsigned char *b, size_t n) {
    char tmp[512];
    snprintf(tmp, sizeof(tmp), "%s.tmp", p);
    FILE *f = fopen(tmp, "wb");
    if (!f) return -1;
    if (fwrite(b, 1, n, f) != n) { fclose(f); unlink(tmp); return -1; }
    fflush(f);
    fsync(fileno(f));
    fclose(f);
    chmod(tmp, 0755);
    if (rename(tmp, p)) { unlink(tmp); return -1; }
    return 0;
}

#define PH_LOAD 1
#define PH_DYNAMIC 2

int main(int argc, char **argv) {
    if (argc != 4) { fprintf(stderr, "usage: %s <in> <out> <needed>\n", argv[0]); return 2; }
    unsigned char *buf = NULL;
    size_t len = 0;
    if (read_file(argv[1], &buf, &len)) { fprintf(stderr, "read failed\n"); return 1; }
    const char *needed = argv[3];
    size_t needed_len = strlen(needed) + 1;

    if (len < 64 || memcmp(buf, "\x7f" "ELF", 4) || buf[4] != 2 || buf[5] != 1) {
        fprintf(stderr, "not ELF64 LE\n"); return 1;
    }
    uint64_t e_phoff = *(uint64_t *)(buf + 0x20);
    uint16_t e_phentsize = *(uint16_t *)(buf + 0x36);
    uint16_t e_phnum = *(uint16_t *)(buf + 0x38);
    if (e_phentsize != 56 || e_phoff + (uint64_t)e_phnum * e_phentsize > len) {
        fprintf(stderr, "bad phdr table\n"); return 1;
    }

    Phdr ph[64];
    if (e_phnum > 64) { fprintf(stderr, "too many phdrs\n"); return 1; }
    for (int i = 0; i < e_phnum; i++) {
        unsigned char *p = buf + e_phoff + (uint64_t)i * e_phentsize;
        ph[i].type = *(uint32_t *)(p + 0);
        ph[i].flags = *(uint32_t *)(p + 4);
        ph[i].off = *(uint64_t *)(p + 8);
        ph[i].vaddr = *(uint64_t *)(p + 16);
        ph[i].filesz = *(uint64_t *)(p + 32);
        ph[i].memsz = *(uint64_t *)(p + 40);
        ph[i].align = *(uint64_t *)(p + 48);
    }

    int dyn_idx = -1, seg_idx = -1;
    for (int i = 0; i < e_phnum; i++) if (ph[i].type == PH_DYNAMIC) dyn_idx = i;
    if (dyn_idx < 0) { fprintf(stderr, "no PT_DYNAMIC\n"); return 1; }
    for (int i = 0; i < e_phnum; i++) {
        if (ph[i].type != PH_LOAD) continue;
        if (ph[dyn_idx].off >= ph[i].off && ph[dyn_idx].off < ph[i].off + ph[i].filesz) seg_idx = i;
    }
    if (seg_idx < 0) { fprintf(stderr, "PT_DYNAMIC not inside a PT_LOAD\n"); return 1; }
    if (ph[dyn_idx].filesz % 16) { fprintf(stderr, "bad dynamic size\n"); return 1; }

    /* next segment above this one decides how much gap we may use */
    uint64_t gap_end = UINT64_MAX;
    for (int i = 0; i < e_phnum; i++) {
        if (ph[i].type != PH_LOAD || i == seg_idx) continue;
        if (ph[i].vaddr > ph[seg_idx].vaddr && ph[i].vaddr < gap_end) gap_end = ph[i].vaddr;
    }
    uint64_t seg_end = ph[seg_idx].vaddr + ph[seg_idx].memsz;
    if (gap_end == UINT64_MAX || gap_end <= seg_end) { fprintf(stderr, "no gap after segment\n"); return 1; }

    /* locate DT_STRTAB / DT_STRSZ in the dynamic array */
    int n_dyn = (int)(ph[dyn_idx].filesz / 16);
    uint64_t strtab = 0, strsz = 0;
    for (int i = 0; i < n_dyn; i++) {
        uint64_t *d = (uint64_t *)(buf + ph[dyn_idx].off + (uint64_t)i * 16);
        if (d[0] == 5) strtab = d[1];
        if (d[0] == 10) strsz = d[1];
    }
    if (!strtab || !strsz) { fprintf(stderr, "no DT_STRTAB/DT_STRSZ\n"); return 1; }

    /* the source string table lives in some PT_LOAD (usually the first one) */
    int str_seg = -1;
    for (int i = 0; i < e_phnum; i++) {
        if (ph[i].type != PH_LOAD) continue;
        if (strtab >= ph[i].vaddr && strtab + strsz <= ph[i].vaddr + ph[i].memsz) str_seg = i;
    }
    if (str_seg < 0) { fprintf(stderr, "strtab outside every PT_LOAD\n"); return 1; }

    uint64_t new_str_vaddr = (seg_end + 7) & ~(uint64_t)7;
    uint64_t new_str_size = strsz + needed_len;
    uint64_t new_dyn_vaddr = (new_str_vaddr + new_str_size + 15) & ~(uint64_t)15;
    uint64_t new_dyn_size = (uint64_t)(n_dyn + 2) * 16; /* + NEEDED + NULL */
    uint64_t new_end = new_dyn_vaddr + new_dyn_size;
    if (new_end > gap_end) { fprintf(stderr, "gap too small: need %llu have %llu\n",
        (unsigned long long)(new_end - seg_end), (unsigned long long)(gap_end - seg_end)); return 1; }

    uint64_t new_str_off = ph[seg_idx].off + (new_str_vaddr - ph[seg_idx].vaddr);
    uint64_t new_dyn_off = ph[seg_idx].off + (new_dyn_vaddr - ph[seg_idx].vaddr);
    if (new_end > len) {
        /* the file must be extended with zeros; refuse rather than guess */
        fprintf(stderr, "file too short for gap data (%llu > %zu)\n",
                (unsigned long long)new_end, len);
        return 1;
    }
    /* the target region must be zero-filled: we are turning former bss into
     * file-backed bytes and must not resurrect stale content */
    for (uint64_t i = new_str_off; i < new_dyn_off + new_dyn_size; i++) {
        if (buf[i]) { fprintf(stderr, "gap region not zero at %llu\n", (unsigned long long)i); return 1; }
    }

    /* copy the old string table and append our path */
    uint64_t old_str_off = ph[str_seg].off + (strtab - ph[str_seg].vaddr);
    memcpy(buf + new_str_off, buf + old_str_off, strsz);
    memcpy(buf + new_str_off + strsz, needed, needed_len);

    /* build the new dynamic array */
    unsigned char *nd = buf + new_dyn_off;
    int out = 0;
    /* Our interposer must be searched before libc: symbol lookup follows the
     * DT_NEEDED order, so put it first (patchelf does the same). */
    *(uint64_t *)(nd + out * 16) = 1;            /* DT_NEEDED */
    *(uint64_t *)(nd + out * 16 + 8) = strsz;    /* offset of the appended path */
    out++;
    for (int i = 0; i < n_dyn; i++) {
        uint64_t *d = (uint64_t *)(buf + ph[dyn_idx].off + (uint64_t)i * 16);
        if (d[0] == 0) continue;                 /* old terminator */
        if (d[0] == 5) { *(uint64_t *)(nd + out * 16) = 5; *(uint64_t *)(nd + out * 16 + 8) = new_str_vaddr; out++; continue; }
        if (d[0] == 10) { *(uint64_t *)(nd + out * 16) = 10; *(uint64_t *)(nd + out * 16 + 8) = new_str_size; out++; continue; }
        memcpy(nd + out * 16, d, 16);
        out++;
    }
    *(uint64_t *)(nd + out * 16) = 0;            /* DT_NULL */
    *(uint64_t *)(nd + out * 16 + 8) = 0;
    out++;

    /* retarget PT_DYNAMIC */
    {
        unsigned char *p = buf + e_phoff + (uint64_t)dyn_idx * e_phentsize;
        *(uint64_t *)(p + 8) = new_dyn_off;
        *(uint64_t *)(p + 16) = new_dyn_vaddr;
        *(uint64_t *)(p + 24) = new_dyn_vaddr;
        *(uint64_t *)(p + 32) = new_dyn_size;
        *(uint64_t *)(p + 40) = new_dyn_size;
    }
    /* extend the containing PT_LOAD over the new data */
    {
        unsigned char *p = buf + e_phoff + (uint64_t)seg_idx * e_phentsize;
        uint64_t filesz = new_end - ph[seg_idx].vaddr;
        *(uint64_t *)(p + 32) = filesz;
        *(uint64_t *)(p + 40) = filesz;
    }

    /* the linker cross-checks the .dynamic section header against PT_DYNAMIC */
    {
        uint64_t e_shoff = *(uint64_t *)(buf + 0x28);
        uint16_t e_shentsize = *(uint16_t *)(buf + 0x3a);
        uint16_t e_shnum = *(uint16_t *)(buf + 0x3c);
        uint16_t e_shstrndx = *(uint16_t *)(buf + 0x3e);
        if (e_shoff && e_shnum && e_shstrndx < e_shnum) {
            uint64_t shstr = *(uint64_t *)(buf + e_shoff + (uint64_t)e_shstrndx * e_shentsize + 0x18);
            for (int i = 0; i < e_shnum; i++) {
                unsigned char *sh = buf + e_shoff + (uint64_t)i * e_shentsize;
                uint32_t name = *(uint32_t *)(sh + 0);
                const char *nm = (const char *)(buf + shstr + name);
                if (!strcmp(nm, ".dynamic")) {
                    *(uint64_t *)(sh + 0x10) = new_dyn_vaddr;
                    *(uint64_t *)(sh + 0x18) = new_dyn_off;
                    *(uint64_t *)(sh + 0x20) = new_dyn_size;
                } else if (!strcmp(nm, ".dynstr")) {
                    *(uint64_t *)(sh + 0x10) = new_str_vaddr;
                    *(uint64_t *)(sh + 0x18) = new_str_off;
                    *(uint64_t *)(sh + 0x20) = new_str_size;
                }
            }
        }
    }

    if (write_file(argv[2], buf, len)) { fprintf(stderr, "write failed: %s\n", strerror(errno)); return 1; }
    printf("patched: needed=%s strtab=%#llx dyn=%#llx size=%llu\n",
           needed, (unsigned long long)new_str_vaddr, (unsigned long long)new_dyn_vaddr,
           (unsigned long long)new_dyn_size);
    return 0;
}
