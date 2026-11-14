// vedid — virtual EDID override CLI for Apple Silicon macOS.
//
// This file is a derived work of https://github.com/arlint/vedid (MIT License,
// Copyright (c) 2026 Austin Arlint — see LICENSE). Modifications for
// "Mipad DP Touch" (EDID geometry correction B, Xiaomi Mi Pad 9 Pro Max):
//
//   * EDID length check accepts any multiple of 128 up to 32768 and strictly
//     validates the extension-count field (upstream fix for the 384-byte
//     panel EDID: 1 base + 2 extensions).
//   * New subcommands:
//       validate  — offline EDID validation, JSON on stdout, rc 0/1
//       selftest  — offline internal validation (no display is touched)
//       readback  — display identity + CGDisplayScreenSize + mode, JSON
//       guard     — apply, then own the IOAV target handle; restore through
//                   that SAME retained handle when stdin reaches EOF or on
//                   SIGTERM/SIGINT/SIGHUP. Before resetting it re-reads the
//                   EDID and only resets if it still equals what we applied
//                   (a reconnected display is a different service instance —
//                   we must never reset it). No permanent daemon.
//   * guard/preflight require exact target constraints, full factory bytes,
//     a known firmware profile independent of serial, and unique AV mapping.
//     Standalone apply/reset are disabled; no mode/arrangement setters exist.
//   * One JSON line per stage; guard has two stages; logs on stderr.
//
// The override is RAM-only (IOAVServiceSetVirtualEDIDMode, the same SPI
// BetterDisplay uses for "Apply Custom EDID"). It NEVER writes the monitor's
// EEPROM. Worst case is reverted by unplug/replug or reboot.
//
// Build:  make           (see Makefile; needs Xcode CLT clang)
// License: MIT (see LICENSE)

#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <CoreGraphics/CoreGraphics.h>
#import <IOKit/IOKitLib.h>
#import <CommonCrypto/CommonDigest.h>
#include <sys/sysctl.h>
#include <sys/fcntl.h>
#include <signal.h>
#include <unistd.h>
#include <limits.h>
#include <math.h>
#include <errno.h>

// ---- Private SPI (resolved at link time from CoreDisplay/IOKit private frameworks).
//      Validated live on M3 Pro / macOS 26.5.1 and Apple Silicon macOS 27.3.
typedef CFTypeRef IOAVServiceRef;
extern IOAVServiceRef IOAVServiceCreateWithService(CFAllocatorRef, io_service_t);
extern IOReturn IOAVServiceCopyEDID(IOAVServiceRef, CFDataRef *);
extern IOReturn IOAVServiceSetVirtualEDIDMode(IOAVServiceRef, uint32_t, CFDataRef);
extern CFDictionaryRef CoreDisplay_DisplayCreateInfoDictionary(CGDirectDisplayID);

static NSString *const UpstreamLicense = @"MIT License\n\nCopyright (c) 2026 Austin Arlint\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\nof this software and associated documentation files (the \"Software\"), to deal\nin the Software without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n";

static volatile sig_atomic_t gStop = 0;

static void OnSignal(int sig) { gStop = 1; }

#pragma mark - JSON helpers

static NSString *JStr(NSString *s) {
    NSData *d = [NSJSONSerialization dataWithJSONObject:s ?: @"" options:NSJSONWritingFragmentsAllowed error:nil];
    return d ? [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] : @"\"\"";
}

static void PrintJSON(NSDictionary *obj) {
    NSData *d = [NSJSONSerialization dataWithJSONObject:obj options:0 error:nil];
    if (!d) return;
    NSString *s = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding];
    printf("%s\n", s.UTF8String);
    fflush(stdout);
}

static NSDictionary *SZ(CGSize s) {
    return @{@"w": @(s.width), @"h": @(s.height)};
}

#pragma mark - EDID helpers

static bool ValidEDID(const uint8_t *b, size_t n) {
    if (n < 128 || n > 32768 || n % 128 != 0) return false;
    static const uint8_t magic[8] = {0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0};
    if (memcmp(b, magic, 8) != 0) return false;
    for (size_t off = 0; off < n; off += 128) {
        uint8_t sum = 0;
        for (size_t i = 0; i < 128; i++) sum = (uint8_t)(sum + b[off + i]);
        if (sum != 0) return false;
    }
    if (b[0x7E] != n / 128 - 1) return false;
    return true;
}

static NSString *EDIDError(const uint8_t *b, size_t n) {
    if (n < 128) return @"length < 128";
    if (n > 32768 || n % 128 != 0) return @"length not a multiple of 128";
    static const uint8_t magic[8] = {0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0};
    if (memcmp(b, magic, 8) != 0) return @"bad header magic";
    if (b[0x7E] != n / 128 - 1) return @"extension count mismatch";
    for (size_t off = 0; off < n; off += 128) {
        uint8_t sum = 0;
        for (size_t i = 0; i < 128; i++) sum = (uint8_t)(sum + b[off + i]);
        if (sum != 0) return [NSString stringWithFormat:@"block %zu checksum bad", off / 128];
    }
    return nil;
}

static NSString *EDIDName(const uint8_t *e, size_t n) {
    if (n < 128) return nil;
    for (size_t off = 54; off + 18 <= 128; off += 18) {
        if (e[off] == 0 && e[off + 1] == 0 && e[off + 3] == 0xFC) {
            char buf[14];
            memcpy(buf, e + off + 5, 13);
            buf[13] = 0;
            for (int i = 0; i < 13; i++) if (buf[i] == '\n' || buf[i] == '\r') { buf[i] = 0; break; }
            NSString *s = [NSString stringWithUTF8String:buf];
            return [s stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceCharacterSet]];
        }
    }
    return nil;
}

static void EDIDPreferredTiming(const uint8_t *e, size_t n, uint32_t *h, uint32_t *v) {
    *h = *v = 0;
    if (n < 72) return;
    if (e[54] == 0 && e[55] == 0) return;      // not a DTD
    *h = e[56] | ((e[58] & 0xF0) << 4);
    *v = e[59] | ((e[61] & 0xF0) << 4);
}

static void FixChecksum(uint8_t *e, size_t base) {
    uint8_t sum = 0;
    for (int i = 0; i < 127; i++) sum = (uint8_t)(sum + e[base + i]);
    e[base + 127] = (uint8_t)((256 - sum) & 0xFF);
}

// Build a minimal valid 128-byte EDID in memory (offline selftest).
static NSData *SyntheticEDID(void) {
    uint8_t e[128] = {0};
    memcpy(e, (uint8_t[]){0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0}, 8);
    e[8] = 0x61; e[9] = 0xA9;                  // vendor 25001 (same decode as CG)
    e[10] = 0x04; e[11] = 0x00;                // product 4
    e[18] = 0x01; e[19] = 0x04;                // version 1.4
    e[20] = 0x80;                              // digital input
    e[21] = 28; e[22] = 19;                    // 28x19 cm
    // preferred DTD: 3408x2272 (same encoding as the panel's real DTD)
    e[54] = 0x98; e[55] = 0xC4;                // pixel clock 503.28 MHz
    e[56] = 0x50; e[57] = 0xC0; e[58] = 0xD0;  // H active 3408
    e[59] = 0xE0; e[60] = 0x3A; e[61] = 0x80;  // V active 2272
    // monitor name descriptor "MI DISPLAY"
    e[72] = 0; e[73] = 0; e[74] = 0; e[75] = 0xFC; e[76] = 0;
    memcpy(e + 77, "MI DISPLAY", 10);
    e[87] = '\n';
    e[126] = 0;                                // 0 extensions
    FixChecksum(e, 0);
    return [NSData dataWithBytes:e length:128];
}

// Reconstruct B independently at the trust boundary. Exact equality protects all
// identities, timings, DisplayID/CTA data, padding, and every non-geometry byte.
static NSData *GeometryB(NSData *factory) {
    if (!ValidEDID(factory.bytes, factory.length)) return nil;
    NSMutableData *m = [factory mutableCopy];
    uint8_t *b = m.mutableBytes;
    NSMutableIndexSet *offsets = [NSMutableIndexSet indexSet];
    for (NSUInteger o = 54; o <= 108; o += 18)
        if (b[o] || b[o+1]) [offsets addIndex:o];
    for (NSUInteger base = 128; base < m.length; base += 128) {
        if (b[base] != 2) continue;
        NSUInteger start = b[base+2];
        if (!start) continue;
        if (start < 4 || start > 127) return nil;
        for (NSUInteger o = base+start; o+18 <= base+127; o += 18) {
            if (!b[o] && !b[o+1]) break;
            [offsets addIndex:o];
        }
    }
    b[21] = 28; b[22] = 19;
    NSMutableIndexSet *blocks = [NSMutableIndexSet indexSetWithIndex:0];
    [offsets enumerateIndexesUsingBlock:^(NSUInteger o, BOOL *stop) {
        b[o+12] = 282 & 255; b[o+13] = 188; b[o+14] = 0x10;
        [blocks addIndex:o / 128];
    }];
    [blocks enumerateIndexesUsingBlock:^(NSUInteger block, BOOL *stop) {
        FixChecksum(b, block*128);
    }];
    return m;
}

// Conservative supported-firmware allowlist, not a device serial allowlist.
// Hash all 384 bytes except unit serial/manufacture date and base checksum.
// This prevents a timing/identity override that keeps factory geometry from
// being mistaken for clean factory data on the very first launch.
static BOOL KnownFactory(NSData *factory) {
    if (factory.length != 384 || !ValidEDID(factory.bytes,factory.length)) return NO;
    NSMutableData *m=[factory mutableCopy]; uint8_t *b=m.mutableBytes;
    memset(b+12,0,6); b[127]=0;
    unsigned char digest[CC_SHA256_DIGEST_LENGTH]; CC_SHA256(b,(CC_LONG)m.length,digest);
    NSMutableString *hex=[NSMutableString string];
    for (unsigned i=0;i<sizeof(digest);i++) [hex appendFormat:@"%02x",digest[i]];
    return [hex isEqualToString:@"cb9050fc6cad56a5b720e7d1d44e0a06e12e933faa943813878f229c89a0c592"];
}

static NSString *CheckPatch(NSData *factory, NSData *patched) {
    NSData *expected = GeometryB(factory);
    if (!expected || !patched || !ValidEDID(patched.bytes, patched.length))
        return @"invalid factory/candidate EDID";
    if ([factory isEqualToData:patched]) return @"candidate is unchanged / preexisting B";
    return [expected isEqualToData:patched] ? nil : @"candidate is not exact geometry-only B (282x188)";
}

#pragma mark - IOKit / display resolution

static io_service_t CopyAVServiceForDisplay(CGDirectDisplayID display) {
    if (CGDisplayIsBuiltin(display)) return MACH_PORT_NULL;     // never touch builtin
    CFDictionaryRef info = CoreDisplay_DisplayCreateInfoDictionary(display);
    if (!info) return MACH_PORT_NULL;
    CFStringRef loc = CFDictionaryGetValue(info, CFSTR("IODisplayLocation"));
    char wantPath[1024] = {0};
    if (!loc || CFGetTypeID(loc) != CFStringGetTypeID() || !CFStringGetCString(loc, wantPath, sizeof(wantPath), kCFStringEncodingUTF8)) {
        CFRelease(info); return MACH_PORT_NULL;
    }
    // On Apple Silicon the framebuffer shim and AV proxy are siblings under
    // one controller, not descendants. Scope to that exact controller only.
    if (strlen(wantPath) >= sizeof(io_string_t)) { CFRelease(info); return MACH_PORT_NULL; }
    char *last = strrchr(wantPath, '/');
    if (last && strcmp(last + 1, "IOMobileFramebufferShim") == 0) *last = 0;
    CFRetain(loc); CFRelease(info);

    io_registry_entry_t root = IORegistryGetRootEntry(kIOMainPortDefault);
    io_iterator_t it = IO_OBJECT_NULL;
    kern_return_t kr = IORegistryEntryCreateIterator(root, kIOServicePlane,
            kIORegistryIterateRecursively, &it);
    IOObjectRelease(root);
    if (kr != KERN_SUCCESS) { CFRelease(loc); return MACH_PORT_NULL; }
    io_service_t result = MACH_PORT_NULL; io_service_t s; unsigned matches = 0;
    while ((s = IOIteratorNext(it)) != IO_OBJECT_NULL) {
        io_string_t path = {0};
        size_t len = strlen(wantPath);
        BOOL controllerMatch = NO;
        // macOS 26/27 (Apple Silicon) 把 DCPAVServiceProxy 从显示控制器子树
        // (dispN@.../...) 移到独立的 dcpextN@... 节点下，AV 服务与 IODisplayLocation
        // 不再共享路径前缀，且 location 末尾会多出 AppleCLCD2 一层。因此改为按 DCP
        // 单元名匹配 epic 服务节点：单元名取 location 中最深的 "名称@地址" 节点
        // （disp0@8A000000 → disp0、dispext0@88000000 → dispext0），对应 AV 节点
        // "<单元>:dcpav-service-epic:N"；不再要求路径落在控制器子树内。
        char unit[128] = {0}, scan[1024];
        strlcpy(scan,wantPath,sizeof(scan));
        for (char *tok = scan; tok && *tok; ) {
            char *end = strchr(tok,'/');
            if (end) *end = 0;
            char *at = strchr(tok,'@');
            if (at) { *at = 0; strlcpy(unit,tok,sizeof(unit)); }
            tok = end ? end + 1 : NULL;
        }
        if (!unit[0]) {
            char *lastSlash = strrchr(wantPath,'/');
            strlcpy(unit, lastSlash ? lastSlash + 1 : wantPath, sizeof(unit));
        }
        char epic[256]; snprintf(epic,sizeof(epic),"/%s:dcpav-service-epic:",unit);
        kern_return_t pathRC = IORegistryEntryGetPath(s, kIOServicePlane, path);
        controllerMatch = pathRC == KERN_SUCCESS && unit[0] && strstr(path,epic)!=NULL;
        if (pathRC == KERN_SUCCESS &&
            (controllerMatch || (strncmp(path, wantPath, len) == 0 && (path[len] == '/' || path[len] == 0)))) {
            io_name_t name = {0};
            if (IORegistryEntryGetName(s, name) == KERN_SUCCESS &&
                strcmp(name, "DCPAVServiceProxy") == 0) {
                CFTypeRef l = IORegistryEntrySearchCFProperty(s, kIOServicePlane,
                    CFSTR("Location"), kCFAllocatorDefault, kIORegistryIterateRecursively);
                bool external = l && CFGetTypeID(l) == CFStringGetTypeID() &&
                    CFStringCompare((CFStringRef)l, CFSTR("External"), 0) == kCFCompareEqualTo;
                if (l) CFRelease(l);
                BOOL identity = NO;
                if (external) {
                    IOAVServiceRef probe = IOAVServiceCreateWithService(kCFAllocatorDefault, s);
                    CFDataRef data = NULL;
                    if (probe && IOAVServiceCopyEDID(probe, &data) == kIOReturnSuccess && data) {
                        const uint8_t *b = CFDataGetBytePtr(data); size_t n = CFDataGetLength(data);
                        if (ValidEDID(b,n)) {
                            uint32_t serial = b[12]|((uint32_t)b[13]<<8)|((uint32_t)b[14]<<16)|((uint32_t)b[15]<<24);
                            identity = ((uint32_t)((b[8]<<8)|b[9]) == CGDisplayVendorNumber(display) &&
                                (uint32_t)(b[10]|(b[11]<<8)) == CGDisplayModelNumber(display) && serial == CGDisplaySerialNumber(display));
                        }
                    }
                    if (data) CFRelease(data); if (probe) CFRelease(probe);
                }
                if (external && identity) {
                    matches++;
                    if (!result) { result = s; IOObjectRetain(result); }
                }
            }
        }
        IOObjectRelease(s);
    }
    IOObjectRelease(it); CFRelease(loc);
    if (matches != 1 && result) { IOObjectRelease(result); result = MACH_PORT_NULL; }
    return result;
}

static IOAVServiceRef CreateAVService(CGDirectDisplayID d) {
    io_service_t svc = CopyAVServiceForDisplay(d);
    if (!svc) return NULL;
    IOAVServiceRef av = IOAVServiceCreateWithService(kCFAllocatorDefault, svc);
    IOObjectRelease(svc);
    return av;
}

static NSData *ReadEDID(IOAVServiceRef av) {
    CFDataRef e = NULL;
    if (IOAVServiceCopyEDID(av, &e) != kIOReturnSuccess || !e) return nil;
    return (__bridge_transfer NSData *)e;
}

static NSString *DisplayName(CGDirectDisplayID d) {
    CFDictionaryRef info = CoreDisplay_DisplayCreateInfoDictionary(d);
    NSString *name = nil;
    if (info) {
        CFDictionaryRef names = CFDictionaryGetValue(info, CFSTR("DisplayProductName"));
        if (names && CFGetTypeID(names) == CFDictionaryGetTypeID()) {
            CFIndex c = CFDictionaryGetCount(names);
            if (c > 0) {
                const void *vals[c];
                CFDictionaryGetKeysAndValues(names, NULL, vals);
                name = [(__bridge NSString *)vals[0] copy];
            }
        }
        CFRelease(info);
    }
    return name ?: @"(unknown)";
}

static uint32_t StrictUInt(NSString *s) {
    if (!s.length) return 0;
    const char *p=s.UTF8String;
    for (const char *q=p;*q;q++) if (*q<'0' || *q>'9') return 0;
    errno=0; unsigned long long n=strtoull(p,NULL,10);
    return errno || n>UINT32_MAX ? 0 : (uint32_t)n;
}

static CGDirectDisplayID ResolveDisplay(NSString *sel) {
    CGDirectDisplayID ids[128]; uint32_t n = 0;
    if (CGGetOnlineDisplayList(128, ids, &n)!=kCGErrorSuccess || n>=128) return 0;
    if (!sel || sel.length == 0) return 0;
    BOOL numeric = [sel rangeOfCharacterFromSet:
        [[NSCharacterSet decimalDigitCharacterSet] invertedSet]].location == NSNotFound;
    if (numeric) {
        CGDirectDisplayID want = StrictUInt(sel);
        for (uint32_t i = 0; i < n; i++) if (ids[i] == want) return ids[i];
    }
    return 0;
}

#pragma mark - identity verification (before any apply/reset)

// Returns nil when the display matches every given constraint, else an error string.
static NSString *VerifyIdentity(CGDirectDisplayID d, uint32_t vendor, uint32_t product,
                                NSString *name, uint32_t natW, uint32_t natH,
                                NSData **factoryOut) {
    if (CGDisplayIsBuiltin(d)) return @"display is builtin — refusing";
    if (vendor && CGDisplayVendorNumber(d) != vendor)
        return [NSString stringWithFormat:@"vendor mismatch (CG=%u, want %u)",
                CGDisplayVendorNumber(d), vendor];
    if (product && CGDisplayModelNumber(d) != product)
        return [NSString stringWithFormat:@"product mismatch (CG=%u, want %u)",
                CGDisplayModelNumber(d), product];
    IOAVServiceRef av = CreateAVService(d);
    if (!av) return @"no AV service for display";
    NSData *e = ReadEDID(av);
    CFRelease(av);
    if (!e) return @"EDID read failed";
    NSString *err = EDIDError(e.bytes, e.length);
    if (err) return [NSString stringWithFormat:@"EDID invalid: %@", err];
    NSString *edName = EDIDName(e.bytes, e.length);
    if (name && name.length > 0 && (!edName ||
        [edName compare:name options:NSCaseInsensitiveSearch] != NSOrderedSame))
        return [NSString stringWithFormat:@"EDID name %@ != %@",
                edName ? JStr(edName) : @"(none)", JStr(name)];
    if (natW && natH) {
        uint32_t h = 0, v = 0;
        EDIDPreferredTiming(e.bytes, e.length, &h, &v);
        if (h != natW || v != natH)
            return [NSString stringWithFormat:@"preferred timing %ux%u != %ux%u",
                    h, v, natW, natH];
    }
    if (factoryOut) *factoryOut = e;
    return nil;
}

#pragma mark - commands

static int cmd_list(void) {
    CGDirectDisplayID ids[16]; uint32_t n = 0;
    CGGetOnlineDisplayList(16, ids, &n);
    NSMutableArray *all = [NSMutableArray array];
    for (uint32_t i = 0; i < n; i++) {
        CGDirectDisplayID d = ids[i];
        [all addObject:@{@"id": @(d),
                         @"name": DisplayName(d),
                         @"vendor": @(CGDisplayVendorNumber(d)),
                         @"model": @(CGDisplayModelNumber(d)),
                         @"serial": @(CGDisplaySerialNumber(d)),
                         @"builtin": @(CGDisplayIsBuiltin(d)),
                         @"sizeMM": SZ(CGDisplayScreenSize(d)),
                         @"pixelsWide": @(CGDisplayPixelsWide(d)),
                         @"pixelsHigh": @(CGDisplayPixelsHigh(d))}];
    }
    PrintJSON(@{@"rc": @0, @"displays": all});
    return 0;
}

static int cmd_readback(CGDirectDisplayID d) {
    CGDisplayModeRef m = CGDisplayCopyDisplayMode(d);
    NSMutableDictionary *v = [@{@"rc": @0, @"id": @(d),
                                @"vendor": @(CGDisplayVendorNumber(d)),
                                @"model": @(CGDisplayModelNumber(d)),
                                @"serial": @(CGDisplaySerialNumber(d)),
                                @"builtin": @(CGDisplayIsBuiltin(d)),
                                @"name": DisplayName(d),
                                @"sizeMM": SZ(CGDisplayScreenSize(d))} mutableCopy];
    if (m) {
        v[@"logicalWidth"] = @(CGDisplayModeGetWidth(m));
        v[@"logicalHeight"] = @(CGDisplayModeGetHeight(m));
        v[@"pixelWidth"] = @(CGDisplayModeGetPixelWidth(m));
        v[@"pixelHeight"] = @(CGDisplayModeGetPixelHeight(m));
        v[@"hz"] = @(CGDisplayModeGetRefreshRate(m));
        CGDisplayModeRelease(m);
    }
    PrintJSON(v);
    return 0;
}

static int cmd_validate(NSString *file) {
    NSData *e = [NSData dataWithContentsOfFile:file];
    if (!e) { PrintJSON(@{@"valid": @NO, @"error": @"cannot read file"}); return 1; }
    NSString *err = EDIDError(e.bytes, e.length);
    if (err) { PrintJSON(@{@"valid": @NO, @"error": err}); return 1; }
    PrintJSON(@{@"valid": @YES, @"length": @(e.length),
                @"blocks": @(e.length / 128)});
    return 0;
}

static int cmd_dump(CGDirectDisplayID d, NSString *out) {
    IOAVServiceRef av = CreateAVService(d);
    if (!av) { PrintJSON(@{@"rc": @1, @"error": @"no AV service"}); return 1; }
    NSData *e = ReadEDID(av);
    CFRelease(av);
    if (!e) { PrintJSON(@{@"rc": @1, @"error": @"EDID read failed"}); return 1; }
    if (out && ![out isEqualToString:@"-"]) {
        NSError *w = nil;
        BOOL ok = [e writeToFile:out options:NSDataWritingAtomic error:&w];
        PrintJSON(@{@"rc": ok ? @0 : @1, @"bytes": @(e.length), @"file": out,
                    @"error": ok ? @"" : (w.localizedDescription ?: @"write failed")});
        return ok ? 0 : 1;
    }
    PrintJSON(@{@"rc": @0, @"bytes": @(e.length),
                @"edid": [e base64EncodedStringWithOptions:0]});
    return 0;
}

// Only an owning guard can write. Unguarded apply/reset are unavailable.
static BOOL NearSize(CGSize a, CGSize b) {
    return isfinite(a.width) && isfinite(a.height) && a.width>0 && a.height>0 &&
        fabs(a.width-b.width)<=4 && fabs(a.height-b.height)<=4;
}
static NSArray *Snapshot(void) {
    CGDirectDisplayID ids[128]; uint32_t n=0;
    if (CGGetOnlineDisplayList(128,ids,&n)!=kCGErrorSuccess || n>=128) return nil;
    NSMutableArray *all=[NSMutableArray array];
    for (uint32_t i=0;i<n;i++) {
        CGDirectDisplayID d=ids[i]; CGRect b=CGDisplayBounds(d);
        CGDisplayModeRef m=CGDisplayCopyDisplayMode(d); if (!m) return nil;
        [all addObject:@{@"id":@(d),@"vendor":@(CGDisplayVendorNumber(d)),@"model":@(CGDisplayModelNumber(d)),
          @"serial":@(CGDisplaySerialNumber(d)),@"builtin":@(CGDisplayIsBuiltin(d)),@"sizeMM":SZ(CGDisplayScreenSize(d)),
          @"x":@(b.origin.x),@"y":@(b.origin.y),@"w":@(b.size.width),@"h":@(b.size.height),
          @"rotation":@(CGDisplayRotation(d)),@"mirror":@(CGDisplayMirrorsDisplay(d)),
          @"logicalWidth":@(CGDisplayModeGetWidth(m)),@"logicalHeight":@(CGDisplayModeGetHeight(m)),
          @"pixelWidth":@(CGDisplayModeGetPixelWidth(m)),@"pixelHeight":@(CGDisplayModeGetPixelHeight(m)),
          @"hz":@(CGDisplayModeGetRefreshRate(m)),@"mode":@(CGDisplayModeGetIODisplayModeID(m))}];
        CGDisplayModeRelease(m);
    }
    return [all sortedArrayUsingComparator:^NSComparisonResult(NSDictionary *a,NSDictionary *b) {
        return [a[@"id"] compare:b[@"id"]];
    }];
}
// Ignore only OUR target's expected geometry change. Other screens' geometry
// and every screen's identity/video configuration remain part of the check.
static BOOL SameConfiguration(NSArray *before,NSArray *after,CGDirectDisplayID target) {
    if (!before || !after || before.count!=after.count) return NO;
    for (NSUInteger i=0;i<before.count;i++) {
        NSMutableDictionary *a=[before[i] mutableCopy],*b=[after[i] mutableCopy];
        if ([a[@"id"] unsignedIntValue]==target) { [a removeObjectForKey:@"sizeMM"]; [a removeObjectForKey:@"mode"]; }
        if ([b[@"id"] unsignedIntValue]==target) { [b removeObjectForKey:@"sizeMM"]; [b removeObjectForKey:@"mode"]; }
        if (![a isEqualToDictionary:b]) return NO;
    }
    return YES;
}
// macOS re-flows the arrangement when a display's physical size changes (the
// panel is re-registered as a new virtual display). That is an expected side
// effect of the geometry-only B guard, not a configuration corruption: restore
// the pre-apply origins and only then treat the apply as clean.
static BOOL OnlyArrangementChanged(NSArray *before,NSArray *after,CGDirectDisplayID target) {
    if (!before || !after || before.count!=after.count) return NO;
    for (NSUInteger i=0;i<before.count;i++) {
        if (![before[i][@"id"] isEqual:after[i][@"id"]]) return NO;
        NSMutableDictionary *a=[before[i] mutableCopy],*b=[after[i] mutableCopy];
        if ([a[@"id"] unsignedIntValue]==target) {
            for (NSString *k in @[@"sizeMM",@"mode",@"w",@"h",@"pixelWidth",@"pixelHeight",@"logicalWidth",@"logicalHeight",@"hz"]) {
                [a removeObjectForKey:k]; [b removeObjectForKey:k];
            }
        }
        [a removeObjectForKey:@"x"]; [a removeObjectForKey:@"y"];
        [b removeObjectForKey:@"x"]; [b removeObjectForKey:@"y"];
        if (![a isEqualToDictionary:b]) return NO;
    }
    return YES;
}
static BOOL RestoreArrangement(NSArray *before) {
    CGDisplayConfigRef cfg=NULL;
    if (CGBeginDisplayConfiguration(&cfg)!=kCGErrorSuccess) return NO;
    BOOL ok=YES;
    for (NSDictionary *s in before) {
        CGError e=CGConfigureDisplayOrigin(cfg,(CGDirectDisplayID)[s[@"id"] unsignedIntValue],
                                           (int32_t)[s[@"x"] intValue],(int32_t)[s[@"y"] intValue]);
        if (e!=kCGErrorSuccess) ok=NO;
    }
    if (CGCompleteDisplayConfiguration(cfg,kCGConfigureForSession)!=kCGErrorSuccess) ok=NO;
    return ok;
}
static BOOL SameConfigurationForApply(NSArray *before,NSArray *after,CGDirectDisplayID target) {
    if (!before || !after || before.count!=after.count) return NO;
    for (NSUInteger i=0;i<before.count;i++) {
        if (![before[i][@"id"] isEqual:after[i][@"id"]]) return NO;
        NSMutableDictionary *a=[before[i] mutableCopy],*b=[after[i] mutableCopy];
        if ([a[@"id"] unsignedIntValue]==target) {
            for (NSString *k in @[@"sizeMM",@"mode",@"w",@"h",@"pixelWidth",@"pixelHeight",@"logicalWidth",@"logicalHeight",@"hz"]) {
                [a removeObjectForKey:k]; [b removeObjectForKey:k];
            }
        }
        if (![a isEqualToDictionary:b]) return NO;
    }
    return YES;
}
static BOOL SameService(CGDirectDisplayID d,uint64_t originalID) {
    io_service_t s=CopyAVServiceForDisplay(d); uint64_t now=0; if (!s) return NO;
    kern_return_t rc=IORegistryEntryGetRegistryEntryID(s,&now); IOObjectRelease(s);
    return rc==KERN_SUCCESS && now==originalID;
}
static BOOL WaitReadback(CGDirectDisplayID d,IOAVServiceRef av,NSData *bytes,CGSize size) {
    for (int i=0;i<40;i++) {
        if ([ReadEDID(av) isEqualToData:bytes] && NearSize(CGDisplayScreenSize(d),size)) return YES;
        usleep(100000);
    }
    return NO;
}
static int cmd_guard(CGDirectDisplayID d,NSString *file,NSString *factoryFile,
                     uint32_t vendor,uint32_t product,NSString *name,uint32_t natW,uint32_t natH,BOOL readonly) {
    // Install handlers BEFORE any write or output (parent may already be gone).
    signal(SIGPIPE,SIG_IGN); struct sigaction sa={0}; sa.sa_handler=OnSignal;
    sigaction(SIGTERM,&sa,NULL); sigaction(SIGINT,&sa,NULL); sigaction(SIGHUP,&sa,NULL);
    NSString *why=nil;
    NSData *expected=[NSData dataWithContentsOfFile:factoryFile ?: @""];
    NSData *patched=[NSData dataWithContentsOfFile:file];
    if (vendor!=25001 || product!=4 || ![name isEqualToString:@"MI DISPLAY"] || natW!=3408 || natH!=2272)
        why=@"all exact Xiaomi identity flags are required";
    CGDirectDisplayID ids[128]; uint32_t n=0; unsigned matches=0;
    if (CGGetOnlineDisplayList(128,ids,&n)!=kCGErrorSuccess || n>=128) { why=@"display enumeration failed/truncated"; n=0; }
    for (uint32_t i=0;i<n;i++) if (!CGDisplayIsBuiltin(ids[i]) &&
        CGDisplayVendorNumber(ids[i])==25001 && CGDisplayModelNumber(ids[i])==4) matches++;
    if (matches!=1) why=@"Xiaomi target absent/ambiguous";
    NSData *checked=nil;
    if (!why) why=VerifyIdentity(d,vendor,product,name,natW,natH,&checked);
    if (!why && (!expected || ![checked isEqualToData:expected])) why=@"full factory bytes mismatch";
    io_service_t svc=why ? 0 : CopyAVServiceForDisplay(d); uint64_t serviceID=0; IOAVServiceRef av=NULL;
    if (!why && (!svc || IORegistryEntryGetRegistryEntryID(svc,&serviceID)!=KERN_SUCCESS)) why=@"no unique AV registry identity";
    if (!why) av=IOAVServiceCreateWithService(kCFAllocatorDefault,svc);
    if (!why && !av) why=@"cannot retain original AV handle";
    NSData *factory=av ? ReadEDID(av) : nil;
    CGSize before=CGDisplayScreenSize(d),desired=CGSizeMake(282,188);
    if (!why && ![factory isEqualToData:expected]) why=@"original handle factory/full identity mismatch";
    if (!why && !KnownFactory(factory)) why=@"unknown factory firmware / preexisting EDID override; refusing";
    if (!why) why=CheckPatch(factory, patched);
    if (!why) {
        const uint8_t *b=factory.bytes;
        uint32_t ev=(b[8]<<8)|b[9],ep=b[10]|(b[11]<<8);
        uint32_t serial=b[12]|((uint32_t)b[13]<<8)|((uint32_t)b[14]<<16)|((uint32_t)b[15]<<24);
        if (ev!=vendor || ep!=product || serial!=CGDisplaySerialNumber(d)) why=@"CG/EDID full identity mismatch";
        if (!NearSize(before,CGSizeMake(b[21]*10,b[22]*10))) why=@"preexisting override / effective geometry differs from factory";
    }
    NSArray *beforeAll=Snapshot();
    if (!why && !beforeAll) why=@"cannot snapshot every display mode/arrangement";
    if (!why && (!SameService(d,serviceID) || ![ReadEDID(av) isEqualToData:factory])) why=@"target changed before apply";
    if (gStop) why=@"signal before apply";
    if (why) {
        if (av) CFRelease(av); if (svc) IOObjectRelease(svc);
        PrintJSON(@{@"cmd":@"guard",@"rc":@1,@"stage":@"refused",@"error":why}); return 1;
    }
    if (readonly) {
        PrintJSON(@{@"rc":@0,@"stage":@"ready",@"readonly":@YES,@"serviceID":@(serviceID),
                    @"sizeBefore":SZ(before),@"snapshotBefore":beforeAll});
        CFRelease(av); IOObjectRelease(svc); return 0;
    }
    // A stalled stdout consumer cannot prevent parent EOF/signal cleanup.
    int outFlags=fcntl(STDOUT_FILENO,F_GETFL,0);
    if (outFlags>=0) fcntl(STDOUT_FILENO,F_SETFL,outFlags|O_NONBLOCK);
    IOReturn applyRC=IOAVServiceSetVirtualEDIDMode(av,1,(__bridge CFDataRef)patched);
    BOOL geometryOK=applyRC==kIOReturnSuccess && WaitReadback(d,av,patched,desired) && SameService(d,serviceID);
    BOOL arrangementRestored=NO;
    if (geometryOK && !SameConfiguration(beforeAll,Snapshot(),d) &&
        OnlyArrangementChanged(beforeAll,Snapshot(),d)) {
        arrangementRestored=RestoreArrangement(beforeAll);
        for (int i=0;i<10 && arrangementRestored && !SameConfiguration(beforeAll,Snapshot(),d);i++) usleep(100000);
    }
    BOOL applied=geometryOK && SameConfigurationForApply(beforeAll,Snapshot(),d);
    NSString *applyError=applied ? @"" : [NSString stringWithFormat:@"apply/readback/config verification failed (api=0x%x)",applyRC];
    PrintJSON(@{@"cmd":@"guard",@"rc":applied ? @0 : @1,@"stage":applied ? @"applied" : @"apply-failed",
      @"error":applyError,@"apiRC":@(applyRC),@"edidMatches":@([ReadEDID(av) isEqualToData:patched]),
      @"geometryMatches":@(NearSize(CGDisplayScreenSize(d),desired)),@"sameService":@(SameService(d,serviceID)),
      @"configurationUnchanged":@(SameConfiguration(beforeAll,Snapshot(),d)),@"arrangementRestored":@(arrangementRestored),
      @"sizeBefore":SZ(before),@"sizeAfter":SZ(CGDisplayScreenSize(d)),
      @"serviceID":@(serviceID),@"snapshotBefore":beforeAll,@"snapshotAfter":Snapshot() ?: @[]});
    int flags=fcntl(STDIN_FILENO,F_GETFL,0); if (flags>=0) fcntl(STDIN_FILENO,F_SETFL,flags|O_NONBLOCK);
    BOOL eof=NO;
    while (applied && !gStop && !eof) {
        char buf[64]; ssize_t rc=read(STDIN_FILENO,buf,sizeof(buf));
        if (rc==0 || (rc<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR)) eof=YES;
        CFRunLoopRunInMode(kCFRunLoopDefaultMode,0.1,true);
    }
    // Reset ONLY original retained av; never a reconnected service or third party.
    NSData *now=ReadEDID(av); CGSize current=CGDisplayScreenSize(d);
    BOOL same=SameService(d,serviceID),restored=NO; NSString *restoreError=nil;
    IOReturn resetRC=kIOReturnSuccess;
    if (same && [now isEqualToData:factory] && NearSize(current,before)) restored=YES;
    else if (same && (([now isEqualToData:patched] && (!applied || NearSize(current,desired))) ||
             (!applied && [now isEqualToData:factory] && NearSize(current,desired)))) {
        resetRC=IOAVServiceSetVirtualEDIDMode(av,0,NULL);
        BOOL edidBack=NO;
        for (int i=0;i<60;i++) { if ([ReadEDID(av) isEqualToData:factory]) { edidBack=YES; break; } usleep(100000); }
        for (int i=0;i<30 && !NearSize(CGDisplayScreenSize(d),before);i++) usleep(100000);
        restored=resetRC==kIOReturnSuccess && edidBack && SameService(d,serviceID);
        if (!restored) restoreError=@"reset API/readback verification failed";
    } else restoreError=@"original service disconnected/replaced or third-party EDID; left untouched";
    NSArray *afterAll=Snapshot(); CFRelease(av); IOObjectRelease(svc);
    PrintJSON(@{@"cmd":@"guard",@"rc":restored ? @0 : @1,@"stage":@"restored",@"restored":@(restored),
      @"error":restoreError ?: @"",@"apiRC":@(resetRC),@"sizeAfterReset":SZ(CGDisplayScreenSize(d)),
      @"snapshotAfter":afterAll ?: @[],@"configurationUnchanged":@(SameConfiguration(beforeAll,afterAll,d))});
    return applied && restored ? 0 : 1;
}

// Offline selftest: synthetic EDID + parser/checksum guards. Touches no display.
static int cmd_selftest(void) {
    NSData *s = SyntheticEDID();
    BOOL ok = YES;
    if (![JStr(@"MI DISPLAY") isEqualToString:@"\"MI DISPLAY\""]) ok=NO;
    if (StrictUInt(@"25001oops") || StrictUInt(@"4294967296") || StrictUInt(@"-1") ||
        StrictUInt(@"25001")!=25001 || StrictUInt(@"4294967295")!=UINT32_MAX) ok=NO;
    if (!NearSize(CGSizeMake(280,190),CGSizeMake(282,188)) ||
        NearSize(CGSizeMake(NAN,188),CGSizeMake(282,188))) ok=NO;
    NSData *geometry=GeometryB(s);
    if (!geometry || !ValidEDID(geometry.bytes,geometry.length) || CheckPatch(s,geometry)) ok=NO;
    if (!ValidEDID(s.bytes, s.length)) { fprintf(stderr, "FAIL: synthetic EDID invalid\n"); ok = NO; }
    NSString *nm = EDIDName(s.bytes, s.length);
    if (![nm isEqualToString:@"MI DISPLAY"]) { fprintf(stderr, "FAIL: name parse %s\n", nm.UTF8String); ok = NO; }
    uint32_t h = 0, v = 0;
    EDIDPreferredTiming(s.bytes, s.length, &h, &v);
    if (h != 3408 || v != 2272) { fprintf(stderr, "FAIL: timing parse %ux%u\n", h, v); ok = NO; }

    NSMutableData *bad = [s mutableCopy];
    ((uint8_t *)bad.mutableBytes)[0x7e] = 1;
    FixChecksum(bad.mutableBytes, 0);
    if (ValidEDID(bad.bytes, bad.length)) { fprintf(stderr, "FAIL: extension count mismatch accepted\n"); ok = NO; }
    bad = [s mutableCopy];
    ((uint8_t *)bad.mutableBytes)[20] ^= 1;
    if (ValidEDID(bad.bytes, bad.length)) { fprintf(stderr, "FAIL: bad checksum accepted\n"); ok = NO; }
    if (ValidEDID(s.bytes, 127)) { fprintf(stderr, "FAIL: truncated EDID accepted\n"); ok = NO; }
    if (ValidEDID(s.bytes, 200)) { fprintf(stderr, "FAIL: non-multiple accepted\n"); ok = NO; }

    // 384-byte (1 base + 2 ext) must validate — the panel EDID is exactly this shape.
    NSMutableData *big = [NSMutableData dataWithData:s];
    uint8_t ext1[128] = {0}, ext2[128] = {0};
    ext1[0] = 0x70;                              // DisplayID extension
    ext2[0] = 0x02;                              // CTA-861 extension
    [big appendBytes:ext1 length:128];
    [big appendBytes:ext2 length:128];
    uint8_t *bb = big.mutableBytes;
    bb[0x7e] = 2;
    FixChecksum(bb, 0); FixChecksum(bb, 128); FixChecksum(bb, 256);
    if (!ValidEDID(bb, 384)) { fprintf(stderr, "FAIL: valid 384-byte EDID rejected\n"); ok = NO; }

    // Mode IDs may be regenerated by an EDID re-probe; actual video attributes may not change.
    NSDictionary *screen=@{@"id":@2,@"mode":@33,@"pixelWidth":@3408,@"hz":@120,@"sizeMM":@[@590,@340]};
    NSMutableDictionary *rebuilt=[screen mutableCopy]; rebuilt[@"mode"]=@41; rebuilt[@"sizeMM"]=@[@280,@190];
    if (!SameConfiguration(@[screen],@[rebuilt],2)) {fprintf(stderr,"FAIL: equivalent target mode renumber rejected\n");ok=NO;}
    rebuilt[@"hz"]=@60;
    if (SameConfiguration(@[screen],@[rebuilt],2)) {fprintf(stderr,"FAIL: changed refresh accepted\n");ok=NO;}
    rebuilt[@"hz"]=@120;
    if (SameConfiguration(@[screen],@[rebuilt],99)) {fprintf(stderr,"FAIL: other-screen changes accepted\n");ok=NO;}
    fprintf(stderr, "selftest %s\n", ok ? "PASS" : "FAIL");
    PrintJSON(@{@"selftest": ok ? @"pass" : @"fail",
                @"rosetta": @(({ int t = 0; size_t z = sizeof(t);
                                 sysctlbyname("sysctl.proc_translated", &t, &z, NULL, 0) == 0
                                 && t == 1; }))});
    return ok ? 0 : 1;
}

static void usage(void) {
    fprintf(stderr,
"vedid — Mipad DP Touch geometry-only B guard (RAM-only private SPI)\n"
"READ ONLY:\n"
"  vedid help | --help | license | selftest | list | snapshot\n"
"  vedid readback --display ID\n"
"  vedid dump --display ID [--out FILE|-]\n"
"  vedid validate --edid FILE\n"
"  vedid check-factory --factory FILE\n"
"  vedid check-patch --factory FILE --edid FILE\n"
"  vedid preflight --display ID --factory FILE --edid FILE [identity flags]\n"
"WRITE (only when explicitly authorized):\n"
"  vedid guard --display ID --factory FILE --edid FILE [identity flags]\n"
"Required flags: --vendor 25001 --product 4 --name 'MI DISPLAY' --native 3408x2272\n"
"Only known factory firmware, exact 282x188 geometry-only B, unique non-builtin\n"
"full identity/registry mapping and factory-clean geometry are allowed. Guard\n"
"emits one JSON line for apply and another after verified original-handle\n"
"restore on parent stdin EOF or SIGTERM/SIGINT/SIGHUP. Standalone apply/reset\n"
"are disabled. Selftest/preflight never set/reset an EDID. SIGKILL cannot be caught.\n");
}

static NSString *opt(NSArray *a, NSString *flag) {
    NSUInteger i = [a indexOfObject:flag];
    return (i != NSNotFound && i + 1 < a.count) ? a[i + 1] : nil;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        int translated = 0; size_t sz = sizeof(translated);
        if (sysctlbyname("sysctl.proc_translated", &translated, &sz, NULL, 0) == 0 && translated == 1) {
            fprintf(stderr, "vedid: must run native arm64, not under Rosetta.\n"); return 2;
        }
        NSMutableArray *a = [NSMutableArray array];
        for (int i = 1; i < argc; i++) [a addObject:[NSString stringWithUTF8String:argv[i]]];
        if (a.count == 0) { usage(); return 1; }
        NSString *cmd = a[0];
        NSString *sel = opt(a, @"--display");
        uint32_t vendor = StrictUInt(opt(a, @"--vendor"));
        uint32_t product = StrictUInt(opt(a, @"--product"));
        NSString *name = opt(a, @"--name");
        uint32_t natW = 0, natH = 0;
        NSString *nat = opt(a, @"--native");
        if (nat) { NSArray *wh = [nat componentsSeparatedByString:@"x"];
            if (wh.count == 2) { natW = StrictUInt(wh[0]); natH = StrictUInt(wh[1]); } }

        if ([cmd isEqualToString:@"license"] || [cmd isEqualToString:@"--license"]) { PrintJSON(@{@"license":UpstreamLicense,@"upstream":@"https://github.com/arlint/vedid"}); return 0; }
        if ([cmd isEqualToString:@"--help"] || [cmd isEqualToString:@"help"]) { usage(); return 0; }
        if ([cmd isEqualToString:@"snapshot"]) { NSArray *v=Snapshot(); PrintJSON(@{@"rc":v ? @0 : @1,@"displays":v ?: @[]}); return v ? 0 : 1; }
        if ([cmd isEqualToString:@"list"]) return cmd_list();
        if ([cmd isEqualToString:@"validate"]) {
            NSString *f = opt(a, @"--edid");
            if (!f) { usage(); return 1; }
            return cmd_validate(f);
        }
        if ([cmd isEqualToString:@"check-factory"]) {
            BOOL ok=KnownFactory([NSData dataWithContentsOfFile:opt(a,@"--factory") ?: @""]);
            PrintJSON(@{@"rc":ok ? @0 : @1,@"knownFactory":@(ok)}); return ok ? 0 : 1;
        }
        if ([cmd isEqualToString:@"check-patch"]) {
            NSString *err = CheckPatch([NSData dataWithContentsOfFile:opt(a, @"--factory") ?: @""],
                                       [NSData dataWithContentsOfFile:opt(a, @"--edid") ?: @""]);
            PrintJSON(@{@"rc": err ? @1 : @0, @"error": err ?: @""});
            return err ? 1 : 0;
        }
        if ([cmd isEqualToString:@"selftest"]) return cmd_selftest();

        CGDirectDisplayID d = ResolveDisplay(sel ?: @"");
        if (!d) { PrintJSON(@{@"rc": @1, @"error": @"display not found"}); return 1; }
        if ([cmd isEqualToString:@"readback"]) return cmd_readback(d);
        if ([cmd isEqualToString:@"dump"])   return cmd_dump(d, opt(a, @"--out"));
        if ([cmd isEqualToString:@"apply"] || [cmd isEqualToString:@"reset"]) {
            PrintJSON(@{@"rc":@1,@"error":@"unguarded writes are disabled; use guard with --factory"}); return 1;
        }
        if ([cmd isEqualToString:@"guard"] || [cmd isEqualToString:@"preflight"])  {
            NSString *f = opt(a, @"--edid");
            if (!f) { usage(); return 1; }
            return cmd_guard(d, f, opt(a, @"--factory"), vendor, product, name, natW, natH,
                             [cmd isEqualToString:@"preflight"]);
        }
        usage(); return 1;
    }
}
