# Copyright (C) 2024 The Android Open Source Project
# SPDX-License-Identifier: Apache-2.0

LOCAL_PATH := device/$(PRODUCT_MANUFACTURER)/$(PRODUCT_DEVICE)

# A/B
AB_OTA_POSTINSTALL_CONFIG += \
    RUN_POSTINSTALL_system=true \
    POSTINSTALL_PATH_system=system/bin/otapreopt_script \
    FILESYSTEM_TYPE_system=ext4 \
    POSTINSTALL_OPTIONAL_system=true

PRODUCT_PACKAGES += \
    otapreopt_script 


# Required for FBE metadata/data decryption
PRODUCT_PACKAGES += \
    keystore2



# POS-PORT: Recovery configuration
AB_OTA_PARTITIONS += \
    boot \
    init_boot \
    odm \
    odm_dlkm \
    product \
    system \
    system_dlkm \
    system_ext \
    vbmeta \
    vbmeta_system \
    vbmeta_vendor \
    vendor \
    vendor_boot \
    vendor_dlkm

PRODUCT_PACKAGES += \
    create_pl_dev \
    create_pl_dev.recovery \
    e2fsck.vendor_ramdisk \
    fsck.f2fs.vendor_ramdisk \
    linker.vendor_ramdisk \
    resize2fs.vendor_ramdisk \
    tune2fs.vendor_ramdisk
# POS-PORT: End recovery configuration

# API
PRODUCT_SHIPPING_API_LEVEL := 32

# Boot Control HAL
PRODUCT_PACKAGES += \
    android.hardware.boot@1.2-mtkimpl \
    android.hardware.boot@1.2-mtkimpl.recovery \
    bootctrl.mt6897 \
    bootctrl.mt6897.recovery

# Dynamic Partitions
PRODUCT_USE_DYNAMIC_PARTITIONS := true

# Fastbootd
PRODUCT_PACKAGES += \
    android.hardware.fastboot@1.0-impl-mock \
    fastbootd

# Health HAL
PRODUCT_PACKAGES += \
    android.hardware.health@2.1-impl \
    android.hardware.health@2.1-service

# Recovery: Additional Libraries
TARGET_RECOVERY_DEVICE_MODULES += \
    libion \
    libxml2

TW_RECOVERY_ADDITIONAL_RELINK_LIBRARY_FILES += \
    $(TARGET_OUT_SHARED_LIBRARIES)/libion.so \
    $(TARGET_OUT_SHARED_LIBRARIES)/libxml2.so

# VNDK
PRODUCT_TARGET_VNDK_VERSION := 34

# Update Engine
PRODUCT_PACKAGES += \
    update_engine \
    update_verifier \
    update_engine_sideload

# MiTEE KeyMint runtime dependencies
PRODUCT_PACKAGES += \
    libcppbor_external \
    libcppcose_rkp
