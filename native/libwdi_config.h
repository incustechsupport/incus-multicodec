/* libwdi build configuration for Incus Multicodec: WinUSB only, x64 only.
 * build-wdi.cmd copies this over third_party\libwdi\msvc\config.h before building. */
#pragma once

#if defined(_PREFAST_)
#pragma warning(disable:28719)
#endif
#if defined(_MSC_VER)
#pragma warning(disable:4996)
#endif

/* WinUSB co-installers from Microsoft's WDK 8.0 redistributable (wdfcoinstaller.msi),
 * extracted by build-wdi.cmd. Windows 10/11 ignore them, but libwdi's INF template needs them. */
#ifndef WDK_DIR
/* relative to third_party/libwdi/libwdi, where the embedder runs */
#define WDK_DIR "../../wdk8/Windows Kits/8.0"
#endif
#define WDF_VER 1011
#define COINSTALLER_DIR "wdf"
#define X64_DIR "x64"

/* libusb0 / libusbK / user drivers: not used */

#define OPT_M64

#define INCLUDE_DEBUG_LOGGING
#define ENABLE_LOGGING 1
