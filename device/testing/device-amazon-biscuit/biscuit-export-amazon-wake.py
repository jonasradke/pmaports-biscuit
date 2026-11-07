#!/usr/bin/env python3
"""On your computer: copy Amazon's wake word engine out of your own Fire OS system image.

    pip install ext4
    python biscuit-export-amazon-wake.py path/to/system.img amazon-wake

Then choose the folder it made on the Echo's settings page, under Stock files, Amazon's wake word engine.
Any Fire OS build will do: this goes by where the files are, and the Echo then runs them to see that they work.
Nothing of Amazon's ships with this project.

A folder works too, if you have the image's contents unpacked already.

The whisper model is not in the firmware: Amazon's devices download it after signing in, and only an owner
can do that. If you have it, add it with --whisper <folder holding pryon_whisper.manifest>.
"""
import argparse
import os
import shutil
import struct
import sys

LOCALES_FIRST = ("en-US", "en-GB", "en-CA", "en-AU", "en-IN", "de-DE")


class Folder:
    def __init__(self, root):
        self.root = root
        if os.path.isdir(os.path.join(root, "system", "lib")) and not os.path.isdir(os.path.join(root, "lib")):
            self.root = os.path.join(root, "system")

    def _at(self, path):
        return os.path.join(self.root, *path.strip("/").split("/"))

    def read(self, path):
        try:
            with open(self._at(path), "rb") as f:
                return f.read()
        except OSError:
            return None

    def names(self, path):
        try:
            return sorted(os.listdir(self._at(path)))
        except OSError:
            return []

    def isdir(self, path):
        return os.path.isdir(self._at(path))


class Image:
    def __init__(self, path):
        try:
            import ext4
        except ImportError:
            sys.exit("Reading an image needs the ext4 package: pip install ext4")
        self.ext4 = ext4
        self.volume = ext4.Volume(open(path, "rb"), offset=0)
        root = list(self._names(self.volume.root))
        self.prefix = "/system/" if "system" in root and "lib" not in root else "/"

    def _names(self, directory):
        for entry, _kind in directory.opendir():
            name = entry.name if isinstance(entry.name, str) else bytes(entry.name)[:entry.name_len].decode(
                "utf-8", "replace")
            name = name[:entry.name_len]
            if name not in (".", ".."):
                yield name

    def _inode(self, path):
        try:
            return self.volume.inode_at(self.prefix + path.strip("/"))
        except Exception:  # noqa: BLE001 - this library has its own idea of errors; absent is absent
            return None

    def read(self, path):
        inode = self._inode(path)
        return inode.open().read() if isinstance(inode, self.ext4.File) else None

    def names(self, path):
        inode = self._inode(path)
        return sorted(self._names(inode)) if isinstance(inode, self.ext4.Directory) else []

    def isdir(self, path):
        return isinstance(self._inode(path), self.ext4.Directory)


def needed(data):
    """The libraries a 32-bit ELF asks for."""
    if data[:4] != b"\x7fELF" or data[4] != 1:
        return []
    phoff, = struct.unpack_from("<I", data, 28)
    phes, phn = struct.unpack_from("<HH", data, 42)
    loads, dyn = [], None
    for i in range(phn):
        kind, off, va, _pa, size = struct.unpack_from("<IIIII", data, phoff + i * phes)
        if kind == 1:
            loads.append((va, off, size))
        elif kind == 2:
            dyn = (off, size)
    if not dyn:
        return []

    def offset(addr):
        for va, off, size in loads:
            if va <= addr < va + size:
                return addr - va + off
        return None

    strtab, wanted = None, []
    for i in range(dyn[1] // 8):
        tag, value = struct.unpack_from("<II", data, dyn[0] + i * 8)
        if tag == 5:
            strtab = offset(value)
        elif tag == 1:
            wanted.append(value)
    return [data[strtab + n:data.index(b"\0", strtab + n)].decode() for n in wanted]


def save(out, rel, data):
    path = os.path.join(out, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def copy_tree(source, path, out, rel):
    count = 0
    for name in source.names(path):
        child = path + "/" + name
        if source.isdir(child):
            count += copy_tree(source, child, out, rel + "/" + name)
        else:
            data = source.read(child)
            if data is not None:
                save(out, rel + "/" + name, data)
                count += 1
    return count


def keyword_model(source):
    """The folder of the ALEXA model: English first, since that is the word most people will say."""
    base = "local/models/keyword"
    present = source.names(base)
    for locale in list(LOCALES_FIRST) + present:
        if locale in present and source.isdir(base + "/" + locale + "/ALEXA"):
            return locale, base + "/" + locale + "/ALEXA"
    return None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="system.img, or a folder holding its contents")
    parser.add_argument("out", help="the folder to make")
    parser.add_argument("--whisper", help="a folder holding pryon_whisper.manifest, to add")
    args = parser.parse_args()

    source = Folder(args.source) if os.path.isdir(args.source) else Image(args.source)
    seen, todo = {}, ["libpryon.so"]
    while todo:
        lib = todo.pop()
        if lib in seen:
            continue
        data = source.read("lib/" + lib)
        if data is None:
            sys.exit("This Fire OS has no lib/%s, so it cannot supply the engine." % lib)
        seen[lib] = len(data)
        save(args.out, "lib/" + lib, data)
        todo.extend(needed(data))
    linker = source.read("bin/linker")
    if linker is None:
        sys.exit("This Fire OS has no bin/linker.")
    save(args.out, "bin/linker", linker)

    locale, model = keyword_model(source)
    if not model:
        sys.exit("This Fire OS has no ALEXA model under local/models/keyword.")
    print("Wake word model: %s, %d files" % (locale, copy_tree(source, model, args.out, "models/ALEXA")))
    sounds = copy_tree(source, "local/models/AED", args.out, "models/AED")
    print("Sound detection model: %s" % ("%d files" % sounds if sounds else "not in this Fire OS"))
    print("Libraries: %s" % ", ".join(sorted(seen)))
    if args.whisper:
        if not os.path.isfile(os.path.join(args.whisper, "pryon_whisper.manifest")):
            sys.exit("No pryon_whisper.manifest in %s." % args.whisper)
        shutil.copytree(args.whisper, os.path.join(args.out, "whisper"), dirs_exist_ok=True)
        print("Whisper model: added")
    print("Done. Choose the folder %s on the Echo's settings page." % args.out)


if __name__ == "__main__":
    main()
