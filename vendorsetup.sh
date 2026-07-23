#!/bin/bash
# device/xiaomi/duchamp/vendorsetup.sh
#
# Convenience function: merges the freshly-built TWRP vendor_boot.img
# ("recovery" fragment) with a reference vendor_boot.img from the real ROM
# ("default" fragment), without breaking normal boot.
#
# Usage, after `lunch twrp_duchamp-eng && mka vendorbootimage`:
#
#   twrpmergevendorboot /path/to/vendor_boot-pixelos-reference.img
#
# Leaves the result at $OUT/vendor_boot-final.img

function twrpmergevendorboot() {
    local rom_ref="$1"
    if [ -z "$rom_ref" ]; then
        echo "Usage: twrpmergevendorboot <path/to/vendor_boot-rom-reference.img>"
        return 1
    fi
    if [ ! -f "$rom_ref" ]; then
        echo "File not found: $rom_ref"
        return 1
    fi
    if [ -z "$OUT" ]; then
        echo "OUT is not set -- run lunch first"
        return 1
    fi
    if [ ! -f "$OUT/vendor_boot.img" ]; then
        echo "$OUT/vendor_boot.img not found -- run 'mka vendorbootimage' first"
        return 1
    fi
    python3 "$ANDROID_BUILD_TOP/device/xiaomi/duchamp/tools/merge_vendor_boot.py" \
        "$rom_ref" \
        "$OUT/vendor_boot.img" \
        -o "$OUT/vendor_boot-final.img" \
    && echo "Done: $OUT/vendor_boot-final.img"
}
