"""Pin and narrowly patch the GPL-3.0 reinstall rescue boot for iKuai."""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
UPSTREAM_COMMIT = "54fae2067557b3c0ca49e7c9ce03bca63c793f8c"
UPSTREAM_SHA256 = {
    "reinstall.sh": "3ca358bf065ebd3ecb2f727e152a4b4fb9b154398abc1b9a5fe0b599f1027d7a",
    "trans.sh": "d8f34529c0a706b17d67965b99a107f8299a30d5303744a20f0fb96556365bf7",
    "initrd-network.sh": "78fd74716b236d0e41e4f5c4034d45db8bcc47fba2d53c4a19a0d21ef60510bc",
}


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f"upstream source changed at marker: {old[:60]!r}")
    return source.replace(old, new)


def patch_reinstall(source):
    source = replace_once(
        source,
        "confhome=https://raw.githubusercontent.com/bin456789/reinstall/main",
        'confhome="file://$IKUAI_VENDOR_DIR"',
    )
    source = replace_once(
        source,
        "confhome_cn=https://cnb.cool/bin456789/reinstall/-/git/raw/main",
        'confhome_cn="file://$IKUAI_VENDOR_DIR"',
    )
    source = replace_once(
        source,
        "    curl -Lo /reinstall-vmlinuz $nextos_vmlinuz\n"
        "    curl -Lo /reinstall-initrd $nextos_initrd\n",
        "    curl -Lo /reinstall-vmlinuz $nextos_vmlinuz\n"
        "    curl -Lo /reinstall-initrd $nextos_initrd\n"
        '    if [ -n "$IKUAI_PROFILE" ]; then\n'
        "        printf '%s  %s\\n' "
        "17fbd3efd16f2e7cb1b945f06423b37399ba7d5ac8cdd3665e7f13f363500e85 "
        "/reinstall-vmlinuz | sha256sum -c -\n"
        "        printf '%s  %s\\n' "
        "41d55a3fd650c99f5fa2a9dfed1eff3b441186831fd4f8d2223f717d8a28217d "
        "/reinstall-initrd | sha256sum -c -\n"
        "    fi\n",
    )
    source = replace_once(
        source,
        "            if grep -q '^28732ac11ff8d211ba4b00a0c93ec93b' $tmp/img-test-hex; then\n"
        "                echo 'DD: Image is EFI.'\n",
        "            if [ -n \"$IKUAI_PROFILE\" ] && [ \"$IKUAI_BOOT_MODE\" = uefi ] &&\n"
        "                [ \"$IKUAI_COMPRESSED_SHA256\" = "
        "e44db656df17e33697bb8a177354a6b2ac19309bc593ff88a222ccf88fef2a57 ]; then\n"
        "                echo 'DD: pinned iKuai UEFI MBR image.'\n"
        "            elif grep -q '^28732ac11ff8d211ba4b00a0c93ec93b' $tmp/img-test-hex; then\n"
        "                echo 'DD: Image is EFI.'\n",
    )
    source = replace_once(
        source,
        "    mkdir -p $initrd_dir/configs\n",
        "    mkdir -p $initrd_dir/configs\n"
        "    cp \"$IKUAI_PROFILE\" $initrd_dir/configs/ikuai-profile.json\n"
        "    cp \"$IKUAI_APPLY\" $initrd_dir/configs/apply_profile.py\n"
        "    cp \"$IKUAI_LIB\" $initrd_dir/configs/ikuai.py\n"
        "    printf '%s\\n' \"$IKUAI_DISK_BASENAME\" >$initrd_dir/configs/ikuai-disk\n"
        "    printf '%s\\n' \"$IKUAI_BOOT_MODE\" >$initrd_dir/configs/ikuai-boot-mode\n"
        "    printf '%s  %s\\n' \"$IKUAI_COMPRESSED_SHA256\" /tmp/ikuai.raw.gz "
        ">$initrd_dir/configs/ikuai-image.sha256\n",
    )
    source = replace_once(
        source,
        "# 检查必须的参数\nverify_os_args",
        "# Rescue password is supplied through the wrapper environment, not argv.\n"
        'username=root\npassword="${IKUAI_RESCUE_PASSWORD:?missing rescue password}"\n'
        "# 检查必须的参数\nverify_os_args",
    )
    return source


def patch_trans(source):
    source = replace_once(
        source,
        'add_default_efi_to_nvram() {\n    info "add default EFI to nvram"\n\n'
        '    apk add lsblk efibootmgr\n',
        'add_default_efi_to_nvram() {\n    info "add default EFI to nvram"\n\n'
        '    apk add lsblk efibootmgr\n'
        '    if [ -f /configs/ikuai-profile.json ] &&\n'
        '        [ "$(cat /configs/ikuai-boot-mode)" = uefi ] &&\n'
        '        grep -Fxq "e44db656df17e33697bb8a177354a6b2ac19309bc593ff88a222ccf88fef2a57  /tmp/ikuai.raw.gz" '
        '/configs/ikuai-image.sha256; then\n'
        '        blockdev --rereadpt "/dev/$xda"\n'
        '        mkdir -p /tmp/ikuai-efi-check\n'
        '        mount -o loop,ro,offset=1048576 "/dev/$xda" /tmp/ikuai-efi-check\n'
        '        test -f /tmp/ikuai-efi-check/EFI/BOOT/BOOTX64.EFI || error_and_exit "missing EFI fallback loader"\n'
        '        umount /tmp/ikuai-efi-check\n'
        '        efibootmgr --create --disk "/dev/$xda" --part 1 --label iKuai-local '
        '--loader "\\\\EFI\\\\BOOT\\\\BOOTX64.EFI"\n'
        '        echo "pinned iKuai UEFI boot entry"\n'
        '        return\n'
        '    fi\n',
    )
    source = replace_once(
        source,
        "get_ttys() {\n    prefix=$1\n",
        "get_ttys() {\n    prefix=$1\n"
        '    if [ -f /configs/ikuai-profile.json ]; then printf "%s\\n" /dev/console; return; fi\n',
    )
    source = replace_once(
        source,
        "setup_web_if_enough_ram() {\n    total_ram=$(get_approximate_ram_size)",
        "setup_web_if_enough_ram() {\n"
        "    if [ -f /configs/ikuai-profile.json ]; then return; fi\n"
        "    total_ram=$(get_approximate_ram_size)",
    )
    first = source.index("dd_raw_with_extract() {")
    last = source.index("\nget_disk_sector_count() {", first)
    original = source[first:last]
    if original.count("wget $img -O- | pipe_extract >/dev/$xda") != 1:
        raise ValueError("upstream DD implementation changed")
    replacement = """dd_raw_with_extract() {
    [ -f /configs/ikuai-profile.json ] || error_and_exit "missing iKuai profile"
    apk add wget
    wget -O /tmp/ikuai.raw.gz "$img" || error_and_exit "image download failed"
    sha256sum -c /configs/ikuai-image.sha256 || error_and_exit "image SHA-256 mismatch"
    gzip -t /tmp/ikuai.raw.gz || error_and_exit "image gzip verification failed"
    gzip -dc /tmp/ikuai.raw.gz | dd of=/dev/$xda bs=4M conv=fsync status=none
    sync
}
"""
    source = replace_once(source, original, replacement)
    source = replace_once(
        source,
        "    if [ -z \"$xda\" ]; then\n        find_xda\n    fi\n",
        "    if [ -z \"$xda\" ]; then\n        find_xda\n    fi\n"
        "    [ -f /configs/ikuai-profile.json ] || error_and_exit \"missing iKuai profile\"\n"
        "    expected_disk=$(cat /configs/ikuai-disk)\n"
        "    [ \"$xda\" = \"$expected_disk\" ] || error_and_exit \"target disk changed\"\n"
        "    if is_efi; then actual_boot=uefi; else actual_boot=bios; fi\n"
        "    [ \"$actual_boot\" = \"$(cat /configs/ikuai-boot-mode)\" ] || "
        "error_and_exit \"boot mode changed\"\n"
        "    [ \"$distro\" = dd ] || error_and_exit \"not DD mode\"\n",
    )
    source = replace_once(
        source,
        "                modify_os_on_disk windows\n            fi\n            ;;\n        qemu) # dd qemu",
        "                [ -f /configs/ikuai-profile.json ] || modify_os_on_disk windows\n"
        "            fi\n            ;;\n        qemu) # dd qemu",
    )
    source = replace_once(
        source,
        'if [ "$hold" = 2 ]; then\n    info "hold 2"\n    exit\nfi\n\n# swapoff -a',
        'if [ -f /configs/ikuai-profile.json ]; then\n'
        '    apk add python3 sfdisk util-linux-misc e2fsprogs e2fsprogs-extra\n'
        '    python3 -c "import sqlite3"\n'
        '    python3 /configs/apply_profile.py --disk /dev/$xda --profile /configs/ikuai-profile.json\n'
        '    sync\n'
        '    reboot\n'
        '    exit\n'
        'fi\n\nif [ "$hold" = 2 ]; then\n    info "hold 2"\n    exit\nfi\n\n# swapoff -a',
    )
    return source


def stage(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    for name, expected in UPSTREAM_SHA256.items():
        original = (HERE / "vendor" / name).read_bytes()
        if hashlib.sha256(original).hexdigest() != expected:
            raise ValueError(f"vendored upstream {name} failed SHA-256")
        if name == "reinstall.sh":
            original = patch_reinstall(original.decode()).encode()
        elif name == "trans.sh":
            original = patch_trans(original.decode()).encode()
        (directory / name).write_bytes(original)
    (directory / "LICENSE").write_bytes((HERE / "vendor" / "LICENSE").read_bytes())
    return directory
