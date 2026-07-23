#!/usr/bin/env python3
"""
merge_vendor_boot.py — merges a ROM vendor_boot.img (working "default"
fragment) with a TWRP vendor_boot.img (working "recovery" fragment), using
the header/dtb/bootconfig from the ROM.

Meant to live inside the TWRP tree (e.g. device/xiaomi/duchamp/tools/) and
work with ANY pair of rom_vendor_boot.img / twrp_vendor_boot.img on a device
with vendor_boot header v4 (GKI, recovery-in-vendor_boot).

Does not depend on AOSP's mkbootimg/unpack_bootimg: header v4 and the
ramdisk table are implemented directly from bootimg.h. Ramdisk format:
lz4 legacy frame, using the real system liblz4 via ctypes (no vendored
decoder, no extra Python dependencies).

Usage:
    python3 merge_vendor_boot.py <rom_vendor_boot.img> <twrp_vendor_boot.img> \\
        -o vendor_boot-merged.img [--default-name NAME] [--recovery-name NAME]

By default it looks in the ROM image for the fragment with an empty name or
type PLATFORM (the "default" fragment), and in the TWRP image for the
fragment named "recovery" or type RECOVERY. If your device names its
fragments differently, use --default-name/--recovery-name.
"""
import argparse
import ctypes
import ctypes.util
import gzip
import struct
import sys

RAMDISK_TYPES = {0: "NONE", 1: "PLATFORM", 2: "RECOVERY", 3: "DLKM"}
FDT_MAGIC = 0xd00dfeed
DTBO_MAGIC = 0xd7b7ab1e

# Known fix: the VINTF manifest shipped by com.android.hardware.boot in this
# tree uses <fqname> (a syntax this recovery's libvintf does not recognize)
# and is installed with type="device" in the wrong folder (system/etc/vintf/
# manifest/, which is the framework folder). We rewrite it here, in the same
# location, with type="framework" and <interface>/<instance> syntax -- the
# same pattern used by keystore2.xml (which does work in this recovery).
# This is applied automatically to the recovery cpio if the old (broken)
# file is present; if the source tree already ships the fix, this is a
# no-op.
_BOOT_VINTF_PATH = "system/etc/vintf/manifest/android.hardware.boot-service.default.xml"
_BOOT_VINTF_FIXED_CONTENT = b"""<manifest version="9.0" type="framework">
    <hal format="aidl">
        <name>android.hardware.boot</name>
        <version>1</version>
        <interface>
            <name>IBootControl</name>
            <instance>default</instance>
        </interface>
    </hal>
</manifest>
"""


# ---------- lz4 legacy frame: real implementation (liblz4.so.1 via ctypes) ----------

_LZ4_BLOCK_SIZE = 8 * 1024 * 1024
_lz4_lib = None


def _lz4_load():
    global _lz4_lib
    if _lz4_lib is not None:
        return _lz4_lib
    path = ctypes.util.find_library("lz4") or "liblz4.so.1"
    lib = ctypes.CDLL(path)
    lib.LZ4_compressBound.restype = ctypes.c_int
    lib.LZ4_compressBound.argtypes = [ctypes.c_int]
    lib.LZ4_compress_HC.restype = ctypes.c_int
    lib.LZ4_compress_HC.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.LZ4_decompress_safe.restype = ctypes.c_int
    lib.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    _lz4_lib = lib
    return lib


def lz4_legacy_is(data):
    return data[:4] == b"\x02\x21\x4c\x18"


def gzip_is(data):
    return data[:2] == b"\x1f\x8b"


def lz4_legacy_compress(data, level=9):
    lib = _lz4_load()
    out = bytearray(b"\x02\x21\x4c\x18")
    for off in range(0, len(data), _LZ4_BLOCK_SIZE):
        chunk = data[off:off + _LZ4_BLOCK_SIZE]
        bound = lib.LZ4_compressBound(len(chunk))
        buf = ctypes.create_string_buffer(bound)
        written = lib.LZ4_compress_HC(chunk, buf, len(chunk), bound, level)
        if written <= 0:
            raise RuntimeError("LZ4_compress_HC failed")
        out += struct.pack("<I", written)
        out += buf.raw[:written]
    return bytes(out)


def lz4_legacy_decompress(data, max_block_size=_LZ4_BLOCK_SIZE):
    lib = _lz4_load()
    if not lz4_legacy_is(data):
        raise ValueError(f"invalid lz4 magic: {data[:4]!r}")
    i, n = 4, len(data)
    out = bytearray()
    while i + 4 <= n:
        (block_size,) = struct.unpack_from("<I", data, i)
        i += 4
        if block_size == 0 or i + block_size > n:
            break
        block = data[i:i + block_size]
        i += block_size
        buf = ctypes.create_string_buffer(max_block_size)
        written = lib.LZ4_decompress_safe(block, buf, len(block), max_block_size)
        if written < 0:
            raise RuntimeError("LZ4_decompress_safe failed")
        out += buf.raw[:written]
    return bytes(out)


def ramdisk_decompress(data):
    """Decompresses a ramdisk fragment, auto-detecting its format."""
    if lz4_legacy_is(data):
        return lz4_legacy_decompress(data), "lz4-legacy"
    if gzip_is(data):
        return gzip.decompress(data), "gzip"
    raise ValueError(f"unknown ramdisk format, magic={data[:4]!r}")


def ramdisk_compress(data, fmt):
    if fmt == "lz4-legacy":
        return lz4_legacy_compress(data)
    if fmt == "gzip":
        return gzip.compress(data, compresslevel=6)
    raise ValueError(f"unknown compression format: {fmt}")


# ---------- cpio 'newc': reading and surgical single-entry editing ----------

_CPIO_MAGIC = b"070701"


def _cpio_iter(data):
    i, n = 0, len(data)
    while i < n:
        start = i
        magic = data[i:i + 6]
        if magic != _CPIO_MAGIC:
            raise ValueError(f"invalid cpio magic at offset {i}: {magic!r}")
        fields = data[i + 6:i + 6 + 13 * 8]
        vals = [int(fields[j * 8:(j + 1) * 8], 16) for j in range(13)]
        (ino, mode, uid, gid, nlink, mtime, filesize,
         devmajor, devminor, rdevmajor, rdevminor, namesize, check) = vals
        hdr_len = 6 + 13 * 8
        name_start = i + hdr_len
        name = data[name_start:name_start + namesize - 1].decode(errors="replace")
        total_hdr = hdr_len + namesize
        pad_hdr = (4 - (total_hdr % 4)) % 4
        data_start = name_start + namesize + pad_hdr
        pad_data = (4 - (filesize % 4)) % 4
        entry_end = data_start + filesize + pad_data
        yield dict(name=name, offset=data_start, size=filesize, start=start, end=entry_end)
        if name == "TRAILER!!!":
            return
        i = entry_end


def cpio_rename_and_replace(data, old_name, new_name, new_data):
    """Replaces the name+content of ONE cpio entry, copying everything else
    byte-for-byte without rebuilding anything else (surgical edit)."""
    out = bytearray()
    found = False
    for e in _cpio_iter(data):
        if e["name"] == old_name:
            found = True
            new_namesize = len(new_name.encode()) + 1
            new_filesize = len(new_data)
            hdr_fields = [0, 0o100644, 0, 0, 1, 0, new_filesize, 0, 0, 0, 0, new_namesize, 0]
            new_header = _CPIO_MAGIC + b"".join(("%08x" % (f & 0xFFFFFFFF)).encode() for f in hdr_fields)
            total_hdr = len(new_header) + new_namesize
            pad_hdr = (4 - (total_hdr % 4)) % 4
            pad_data = (4 - (new_filesize % 4)) % 4
            out += new_header
            out += new_name.encode() + b"\x00"
            out += b"\x00" * pad_hdr
            out += new_data
            out += b"\x00" * pad_data
        else:
            out += data[e["start"]:e["end"]]
        if e["name"] == "TRAILER!!!":
            break
    return bytes(out), found


def apply_boot_vintf_fix(recovery_cpio):
    """If the recovery cpio ships the old/broken VINTF file, fix it.
    If the source tree already ships it fixed, do nothing and report it."""
    for e in _cpio_iter(recovery_cpio):
        if e["name"] == _BOOT_VINTF_PATH:
            content = recovery_cpio[e["offset"]:e["offset"] + e["size"]]
            if b'type="framework"' in content and b"<interface>" in content:
                print("  [OK] the boot control VINTF fix is already applied from the source tree")
                return recovery_cpio, False
            new_cpio, found = cpio_rename_and_replace(
                recovery_cpio, _BOOT_VINTF_PATH, _BOOT_VINTF_PATH, _BOOT_VINTF_FIXED_CONTENT)
            print("  [FIX] patching boot control VINTF (type=device -> framework, fqname -> interface/instance)")
            return new_cpio, True
    print("  [INFO] boot control VINTF file not found under system/etc/vintf/manifest/ "
          "(this tree may no longer use it, or the path changed)")
    return recovery_cpio, False


# ---------- vendor_boot header v4 ----------

def align_up(n, page_size):
    return (n + page_size - 1) // page_size * page_size


def parse_header(data):
    if data[0:8] != b"VNDRBOOT":
        raise ValueError(f"invalid magic: {data[0:8]!r} (expected VNDRBOOT)")
    off = 8
    header_version, page_size, kernel_addr, ramdisk_addr, vendor_ramdisk_size = \
        struct.unpack_from("<IIIII", data, off); off += 20
    cmdline = data[off:off + 2048]; off += 2048
    (tags_addr,) = struct.unpack_from("<I", data, off); off += 4
    name = data[off:off + 16]; off += 16
    (header_size,) = struct.unpack_from("<I", data, off); off += 4
    (dtb_size,) = struct.unpack_from("<I", data, off); off += 4
    (dtb_addr,) = struct.unpack_from("<Q", data, off); off += 8
    hdr = dict(
        header_version=header_version, page_size=page_size, kernel_addr=kernel_addr,
        ramdisk_addr=ramdisk_addr, vendor_ramdisk_size=vendor_ramdisk_size,
        cmdline=cmdline.split(b"\x00")[0].decode(errors="replace"),
        tags_addr=tags_addr, name=name.split(b"\x00")[0].decode(errors="replace"),
        header_size=header_size, dtb_size=dtb_size, dtb_addr=dtb_addr,
    )
    if header_version >= 4:
        (vendor_ramdisk_table_size,) = struct.unpack_from("<I", data, off); off += 4
        (vendor_ramdisk_table_entry_num,) = struct.unpack_from("<I", data, off); off += 4
        (vendor_ramdisk_table_entry_size,) = struct.unpack_from("<I", data, off); off += 4
        (bootconfig_size,) = struct.unpack_from("<I", data, off); off += 4
        hdr.update(vendor_ramdisk_table_size=vendor_ramdisk_table_size,
                   vendor_ramdisk_table_entry_num=vendor_ramdisk_table_entry_num,
                   vendor_ramdisk_table_entry_size=vendor_ramdisk_table_entry_size,
                   bootconfig_size=bootconfig_size)
    else:
        raise ValueError(f"header_version={header_version}: this script only supports v4")
    return hdr


def parse_vendor_boot(path):
    with open(path, "rb") as f:
        data = f.read()
    hdr = parse_header(data)
    page_size = hdr["page_size"]
    header_pages = align_up(hdr["header_size"], page_size)
    ramdisk_pages = align_up(hdr["vendor_ramdisk_size"], page_size)
    dtb_pages = align_up(hdr["dtb_size"], page_size)

    ramdisk_off = header_pages
    dtb_off = ramdisk_off + ramdisk_pages
    table_off = dtb_off + dtb_pages
    table_pages = align_up(hdr["vendor_ramdisk_table_size"], page_size)
    bootconfig_off = table_off + table_pages

    dtb_data = data[dtb_off: dtb_off + hdr["dtb_size"]]
    bootconfig_data = data[bootconfig_off: bootconfig_off + hdr["bootconfig_size"]]

    fragments = []
    entry_size = hdr["vendor_ramdisk_table_entry_size"]
    for i in range(hdr["vendor_ramdisk_table_entry_num"]):
        e = data[table_off + i * entry_size: table_off + (i + 1) * entry_size]
        r_size, r_offset, r_type = struct.unpack_from("<III", e, 0)
        r_name = e[12:12 + 32].split(b"\x00")[0].decode(errors="replace")
        board_id = e[12 + 32:12 + 32 + 64]
        frag_data = data[ramdisk_off + r_offset: ramdisk_off + r_offset + r_size]
        fragments.append(dict(index=i, name=r_name, type=RAMDISK_TYPES.get(r_type, str(r_type)),
                               type_raw=r_type, size=r_size, data=frag_data, board_id=board_id))
    return hdr, fragments, dtb_data, bootconfig_data


def find_fragment(fragments, wanted_name, wanted_type_raw, forced_name=None):
    if forced_name is not None:
        for f in fragments:
            if f["name"] == forced_name:
                return f
        raise KeyError(f"fragment named '{forced_name}' not found")
    for f in fragments:
        if f["name"] == wanted_name:
            return f
    for f in fragments:
        if f["type_raw"] == wanted_type_raw:
            return f
    raise KeyError(f"no fragment found with type={wanted_type_raw} or name='{wanted_name}'")


def dtb_check(dtb_data, label):
    if len(dtb_data) < 4:
        print(f"  [WARN] {label}: dtb too small to inspect")
        return
    (magic,) = struct.unpack_from(">I", dtb_data, 0)
    if magic == FDT_MAGIC:
        print(f"  [OK] {label}: dtb is a valid FDT (magic d00dfeed)")
    elif magic == DTBO_MAGIC:
        print(f"  [WARN] {label}: the dtb section contains a DTBO (overlays), "
              f"not a flat FDT — this probably should NOT be used as the vendor_boot dtb")
    else:
        print(f"  [WARN] {label}: unknown dtb magic: {magic:#010x}")


def build(rom_path, twrp_path, out_path, default_name_override, recovery_name_override):
    print(f"Reading ROM:  {rom_path}")
    hdr_r, frags_r, dtb_r, bootcfg_r = parse_vendor_boot(rom_path)
    print(f"Reading TWRP: {twrp_path}")
    hdr_t, frags_t, dtb_t, bootcfg_t = parse_vendor_boot(twrp_path)

    if hdr_r["page_size"] != hdr_t["page_size"]:
        sys.exit(f"ERROR: page_size mismatch between ROM ({hdr_r['page_size']}) "
                 f"and TWRP ({hdr_t['page_size']}) — cannot safely merge")

    print("\nValidations:")
    dtb_check(dtb_r, "ROM")
    dtb_check(dtb_t, "TWRP (unused, reference only)")
    if "bootconfig" in hdr_r["cmdline"] and "bootconfig" not in hdr_t["cmdline"]:
        print("  [INFO] the ROM cmdline includes 'bootconfig' and TWRP's does not "
              "— using the ROM's (correct)")
    if hdr_r["cmdline"] != hdr_t["cmdline"]:
        print(f"  [INFO] cmdline differs:\n    ROM : {hdr_r['cmdline']!r}\n    TWRP: {hdr_t['cmdline']!r}\n"
              f"    -> using the ROM's")

    default_frag = find_fragment(frags_r, "", 1, default_name_override)   # PLATFORM
    recovery_frag = find_fragment(frags_t, "recovery", 2, recovery_name_override)  # RECOVERY

    default_data_orig, default_fmt = ramdisk_decompress(default_frag["data"])
    print(f"\n'default'  fragment taken from ROM  ({rom_path}): {default_frag['size']} bytes "
          f"compressed, type={default_frag['type']}, format={default_fmt}")

    recovery_data_orig, recovery_fmt = ramdisk_decompress(recovery_frag["data"])
    print(f"'recovery' fragment taken from TWRP ({twrp_path}): {recovery_frag['size']} bytes "
          f"compressed, type={recovery_frag['type']}, format={recovery_fmt}")

    print("\nApplying known boot control VINTF fix on the recovery fragment:")
    recovery_cpio_fixed, changed = apply_boot_vintf_fix(recovery_data_orig)

    # IMPORTANT: mixing compression formats between the default/recovery
    # fragments breaks boot on some bootloaders (confirmed on real hardware
    # tests) -- always recompress both in the SAME format, whatever the
    # default fragment already uses.
    target_fmt = default_fmt
    default_data = default_frag["data"]  # untouched, original raw bytes
    if changed or recovery_fmt != target_fmt:
        if changed:
            print(f"  -> recompressing recovery as '{target_fmt}' (matching default) after the fix")
        else:
            print(f"  -> recompressing recovery from '{recovery_fmt}' to '{target_fmt}' "
                  f"to match default (avoids mixing formats)")
        recovery_data = ramdisk_compress(recovery_cpio_fixed, target_fmt)
    else:
        recovery_data = recovery_frag["data"]  # untouched, already fine

    print(f"\n'default'  fragment final: {len(default_data)} bytes ({target_fmt})")
    print(f"'recovery' fragment final: {len(recovery_data)} bytes ({target_fmt})")

    page_size = hdr_r["page_size"]
    new_default_size = len(default_data)
    new_recovery_offset = new_default_size
    new_recovery_size = len(recovery_data)
    new_vendor_ramdisk_size = new_default_size + new_recovery_size

    magic = b"VNDRBOOT"
    cmdline_bytes = hdr_r["cmdline"].encode() + b"\x00" * (2048 - len(hdr_r["cmdline"]))
    name_bytes = hdr_r["name"].encode() + b"\x00" * (16 - len(hdr_r["name"]))
    table_entry_size = hdr_r["vendor_ramdisk_table_entry_size"]
    table_entry_num = hdr_r["vendor_ramdisk_table_entry_num"]
    table_size = table_entry_size * table_entry_num
    bootconfig_size = len(bootcfg_r)

    header = b"".join([
        magic,
        struct.pack("<IIIII", hdr_r["header_version"], page_size, hdr_r["kernel_addr"],
                    hdr_r["ramdisk_addr"], new_vendor_ramdisk_size),
        cmdline_bytes,
        struct.pack("<I", hdr_r["tags_addr"]),
        name_bytes,
        struct.pack("<I", hdr_r["header_size"]),
        struct.pack("<I", hdr_r["dtb_size"]),
        struct.pack("<Q", hdr_r["dtb_addr"]),
        struct.pack("<IIII", table_size, table_entry_num, table_entry_size, bootconfig_size),
    ])
    assert len(header) == 2128, len(header)

    def build_entry(size, offset, rtype, name, board_id_raw):
        name_b = name.encode() + b"\x00" * (32 - len(name))
        return struct.pack("<III", size, offset, rtype) + name_b + board_id_raw

    entry0 = build_entry(new_default_size, 0, default_frag["type_raw"], "", default_frag["board_id"])
    entry1 = build_entry(new_recovery_size, new_recovery_offset, recovery_frag["type_raw"],
                          "recovery", recovery_frag["board_id"])
    table = entry0 + entry1

    def pad(b):
        rem = len(b) % page_size
        return b + b"\x00" * (page_size - rem) if rem else b

    out = bytearray()
    out += pad(header)
    out += pad(default_data + recovery_data)
    out += pad(dtb_r)
    out += pad(table)
    out += pad(bootcfg_r)

    with open(out_path, "wb") as f:
        f.write(out)

    print(f"\nWrote {out_path}: {len(out)} bytes")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom_vendor_boot", help="ROM's vendor_boot.img (working normal boot)")
    ap.add_argument("twrp_vendor_boot", help="TWRP's vendor_boot.img (working recovery)")
    ap.add_argument("-o", "--output", default="vendor_boot-merged.img")
    ap.add_argument("--default-name", default=None,
                    help="exact name of the 'default' fragment in the ROM if not the standard one")
    ap.add_argument("--recovery-name", default=None,
                    help="exact name of the 'recovery' fragment in TWRP if not the standard one")
    args = ap.parse_args()
    build(args.rom_vendor_boot, args.twrp_vendor_boot, args.output,
          args.default_name, args.recovery_name)


if __name__ == "__main__":
    main()
