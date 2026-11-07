# Waiting for a kernel build

`mt6625l-wifi-motion-rcpi.patch` adds `/proc/net/wlan/rcpi` to the Wi-Fi driver: the level of the last data frame, a
frame counter and whether it was a group frame. Nothing about a frame's content. `biscuit_wifi_motion.py` uses the file
by itself as soon as it exists, and its detector is then far better than what `/proc/net/wireless` allows.

It is **not part of any build**. It applies to `mt6625l-wlan-20260823.tar.gz` but has never been compiled or run, and a
kernel for this device is only released after it has been tested on one. To use it, copy it into
`device/testing/linux-amazon-biscuit` and apply it in `prepare()` of the APKBUILD there, after the driver's tarball is
unpacked, with the source list and checksums updated, and test the kernel on an Echo with a way back (`fastboot boot`,
never flashed first). It lives here because CI's kernel cache is keyed on everything in that folder: a file there, even
an unused one, means an hour-long kernel build.
