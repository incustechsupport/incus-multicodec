/* Force-included when building BlueZ libsbc with MSVC. */
#pragma once
#include <stdint.h>
#include <BaseTsd.h>
typedef SSIZE_T ssize_t;
#define __attribute__(x)
#define __LITTLE_ENDIAN 1234
#define __BIG_ENDIAN 4321
#define __BYTE_ORDER __LITTLE_ENDIAN
