"""Boot one configured local test image and probe its HTTPS management port."""

import argparse
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def screenshot(monitor, output):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(str(monitor))
            sock.recv(4096)
            sock.sendall(b'{"execute":"qmp_capabilities"}\n')
            sock.recv(4096)
            command = {"execute": "human-monitor-command",
                       "arguments": {"command-line": f"screendump {output}"}}
            sock.sendall((json.dumps(command) + "\n").encode())
            sock.recv(4096)
    except (OSError, ValueError):
        return False
    return output.exists()


def boot(image, *, mode="bios", timeout=240):
    with tempfile.TemporaryDirectory(prefix="ikuai-qemu-smoke-") as tmp:
        root = Path(tmp)
        overlay = root / "disk.qcow2"
        monitor = root / "qmp.sock"
        output = root / "qemu.stdout"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", image,
                        str(overlay)], check=True, capture_output=True)
        args = ["qemu-system-x86_64", "-machine", "pc" if mode == "bios" else "q35",
                "-accel", "tcg", "-cpu", "max", "-m", "2048", "-smp", "2",
                "-drive", f"file={overlay},format=qcow2,if=ide" if mode == "bios" else
                          f"file={overlay},format=qcow2,if=virtio",
                "-netdev", "user,id=n0,hostfwd=tcp:127.0.0.1:10443-:443,hostfwd=tcp:127.0.0.1:10080-:80",
                "-device", "e1000,netdev=n0,mac=52:54:00:aa:bb:cc",
                "-display", "none", "-serial", f"file:{root / 'serial.log'}",
                "-qmp", f"unix:{monitor},server=on,wait=off"]
        if mode == "uefi":
            args += ["-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
                     "-drive", f"if=pflash,format=raw,file={root / 'vars.fd'}"]
            subprocess.run(["cp", "/usr/share/OVMF/OVMF_VARS_4M.fd", str(root / "vars.fd")], check=True)
        context = ssl._create_unverified_context()
        with output.open("w") as log:
            process = subprocess.Popen(args, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"QEMU exited {process.returncode}: {output.read_text()}")
                    for url in ("https://127.0.0.1:10443/", "http://127.0.0.1:10080/"):
                        try:
                            with urllib.request.urlopen(url, context=context, timeout=2) as response:
                                print(f"Web reachable: {url} HTTP {response.status}")
                                return
                        except (OSError, urllib.error.URLError):
                            pass
                    time.sleep(3)
                image_path = Path("/tmp/ikuai-qemu-failure.ppm")
                screenshot(monitor, image_path)
                raise TimeoutError(f"Web did not respond; screenshot={image_path}; serial={root / 'serial.log'}")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if process.returncode is not None:
                    shutil.copy2(overlay, "/tmp/ikuai-qemu-last-overlay.qcow2")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--mode", choices=("bios", "uefi"), default="bios")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    boot(args.image, mode=args.mode, timeout=args.timeout)
