#!/usr/bin/env python3
"""
Simple PyQt6 GUI for listing storage devices and securely wiping them with `shred`.

Features:
- Refresh button to reload block devices (uses `lsblk -J -o ...`).
- Shows icon (HDD/USB/SSD), device path (/dev/sdX), size, vendor, model.
- Click a device to select it; shows a "Securely Wipe" button.
- Double confirmation dialogs before running `shred` on the device.
- Runs `shred` in a background thread and streams output to the UI.

Safety notes (displayed on startup): ALWAYS double-check the device path before wiping.
Make sure you understand which device you're wiping; wiping is irreversible.

Dependencies:
    pip install PyQt6

Run:
    python3 pyqt_shred_gui.py

The program will call `pkexec shred ...` if not run as root. If your system lacks `pkexec`, run
this program as root (not recommended unless you know what you're doing):
    sudo python3 pyqt_shred_gui.py

"""

import sys
import os
import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QTextEdit,
    QFrame,
    QSpacerItem,
    QSizePolicy,
)


@dataclass
class BlockDevice:
    name: str  # e.g. sda
    path: str  # e.g. /dev/sda
    size: str
    model: str
    vendor: str
    type: str  # disk, part, rom, etc.
    tran: Optional[str] = None
    rota: Optional[str] = None


def parse_lsblk() -> List[BlockDevice]:
    """Run lsblk and parse JSON output to return a list of top-level block devices (type==disk).
    """
    cmd = ["lsblk", "-J", "-o", "NAME,KNAME,TYPE,SIZE,MODEL,VENDOR,TRAN,ROTA"]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("lsblk not found on this system")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"lsblk failed: {e.stderr}")

    data = json.loads(proc.stdout)
    devices: List[BlockDevice] = []

    def walk(nodes):
        for n in nodes:
            typ = n.get("type")
            if typ == "disk":
                name = n.get("kname") or n.get("name")
                path = f"/dev/{name}"
                devices.append(
                    BlockDevice(
                        name=name,
                        path=path,
                        size=n.get("size", ""),
                        model=(n.get("model") or "").strip(),
                        vendor=(n.get("vendor") or "").strip(),
                        type=typ,
                        tran=n.get("tran"),
                        rota=str(n.get("rota")) if n.get("rota") is not None else None,
                    )
                )
            # recurse if children exist
            if "children" in n and n["children"]:
                walk(n["children"])

    walk(data.get("blockdevices", []))
    return devices


class ShredWorker(QThread):
    output = pyqtSignal(str)
    finished = pyqtSignal(int)

    def __init__(self, device_path: str, passes: int = 3):
        super().__init__()
        self.device_path = device_path
        self.passes = passes

    def run(self):
        # Build shred command
        shred_cmd = ["shred", "-v", f"-n", str(self.passes), self.device_path]

        # If not root, try pkexec
        use_pkexec = (os.geteuid() != 0) and shutil.which("pkexec") is not None
        if use_pkexec:
            cmd = ["pkexec"] + shred_cmd
        else:
            cmd = shred_cmd

        try:
            # Start process and stream output
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        except FileNotFoundError as e:
            msg = f"Command not found: {e}\nMake sure 'shred' is installed and available or run this program as root."
            self.output.emit(msg)
            self.finished.emit(1)
            return
        except Exception as e:
            self.output.emit(f"Failed to start shred: {e}\n")
            self.finished.emit(1)
            return

        # Stream
        assert proc.stdout is not None
        for line in proc.stdout:
            self.output.emit(line)
        proc.wait()
        self.finished.emit(proc.returncode)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Secure Wipe GUI")
        self.resize(800, 500)

        self.layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.layout.addLayout(header)

        title = QLabel("<b>Secure Wipe — select a device to securely erase</b>")
        header.addWidget(title)
        header.addItem(QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        about_btn = QPushButton("About / Safety")
        about_btn.clicked.connect(self.show_about)
        header.addWidget(about_btn)

        refresh_btn = QPushButton("Refresh Devices")
        refresh_btn.clicked.connect(self.refresh_devices)
        header.addWidget(refresh_btn)
        self.refresh_btn = refresh_btn

        # Main split: left list of devices, right details + output
        main_h = QHBoxLayout()
        self.layout.addLayout(main_h)

        self.dev_list = QListWidget()
        self.dev_list.itemClicked.connect(self.on_device_clicked)
        main_h.addWidget(self.dev_list, 40)

        vright = QVBoxLayout()
        main_h.addLayout(vright, 60)

        # Details frame
        details_frame = QFrame()
        details_frame.setFrameShape(QFrame.Shape.StyledPanel)
        details_layout = QVBoxLayout(details_frame)

        self.details_label = QLabel("Select a device to see details and actions.")
        self.details_label.setWordWrap(True)
        details_layout.addWidget(self.details_label)

        self.wipe_btn = QPushButton("Securely Wipe Device")
        self.wipe_btn.setEnabled(False)
        self.wipe_btn.clicked.connect(self.on_wipe_clicked)
        details_layout.addWidget(self.wipe_btn)

        vright.addWidget(details_frame, 20)

        # Output console
        self.output_console = QTextEdit()
        self.output_console.setReadOnly(True)
        vright.addWidget(self.output_console, 80)

        # Internal state
        self.devices: List[BlockDevice] = []
        self.selected_device: Optional[BlockDevice] = None
        self.shred_worker: Optional[ShredWorker] = None

        # Initial refresh
        self.refresh_devices()

    def show_about(self):
        QMessageBox.information(
            self,
            "About & Safety",
            (
                "This small tool lists block devices using 'lsblk' and calls 'shred' to overwrite a\n"
                "device. Wiping is irreversible.\n\n"
                "If you run this as a normal user, the program will try to use 'pkexec' to get\n"
                "elevated privileges for the shred command. If 'pkexec' is not available, run\n"
                "the GUI as root (not recommended) or run shred manually from a root shell.\n\n"
                "Always double-check the device path (e.g. /dev/sda).\n"
            ),
        )

    def refresh_devices(self):
        self.refresh_btn.setEnabled(False)
        self.dev_list.clear()
        self.devices = []
        self.selected_device = None
        self.wipe_btn.setEnabled(False)
        self.details_label.setText("Refreshing device list...")
        QApplication.processEvents()

        try:
            devs = parse_lsblk()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to list devices: {e}")
            self.details_label.setText("Failed to list devices.")
            self.refresh_btn.setEnabled(True)
            return

        self.devices = devs
        if not devs:
            self.details_label.setText("No block devices found.")
        else:
            for d in devs:
                item = QListWidgetItem(self._format_device_item_text(d))
                # store BlockDevice on item for convenience
                item.setData(Qt.ItemDataRole.UserRole, d)
                self.dev_list.addItem(item)
            self.details_label.setText("Select a device to see details and actions.")

        self.refresh_btn.setEnabled(True)

    def _format_device_item_text(self, d: BlockDevice) -> str:
        # Build an icon text based on transport/rota
        icon_text = self._device_icon_text(d)
        name = d.path
        size = d.size
        vendor = d.vendor or ""
        model = d.model or ""
        return f"{icon_text}  {name}  —  {size}  —  {vendor} {model}"

    def _device_icon_text(self, d: BlockDevice) -> str:
        # Use simple emoji/text icons to indicate type
        if d.tran and d.tran.lower() == "usb":
            return "🔌 USB"
        # rota == 1 likely spinning HDD
        try:
            if d.rota is not None and int(d.rota) == 1:
                return "🟠 HDD"
        except Exception:
            pass
        # otherwise assume SSD or unknown
        return "⚫ SSD"

    def on_device_clicked(self, item: QListWidgetItem):
        d: BlockDevice = item.data(Qt.ItemDataRole.UserRole)
        self.selected_device = d
        self.details_label.setText(
            f"Selected: {d.path}\nSize: {d.size}\nVendor: {d.vendor or 'Unknown'}\nModel: {d.model or 'Unknown'}\nTransport: {d.tran or 'Unknown'}"
        )
        self.wipe_btn.setEnabled(True)

    def on_wipe_clicked(self):
        if not self.selected_device:
            return
        d = self.selected_device

        # First confirmation
        reply = QMessageBox.question(
            self,
            "Confirm Wipe",
            f"Are you sure you want to securely wipe {d.path}?\nThis will irreversibly destroy data on this device.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Second confirmation (type device path to confirm)
        confirm_text, ok = QMessageBox.getText(
            self,
            "Type to Confirm",
            f"Type the device path to confirm (e.g. {d.path}):",
        )
        if not ok or confirm_text.strip() != d.path:
            QMessageBox.warning(self, "Confirmation Failed", "Device path did not match. Aborting.")
            return

        # Disable UI controls while running
        self.wipe_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.output_console.append(f"Starting shred on {d.path}...\n")

        # Start worker thread
        self.shred_worker = ShredWorker(d.path, passes=3)
        self.shred_worker.output.connect(self._append_output)
        self.shred_worker.finished.connect(self._shred_finished)
        self.shred_worker.start()

    def _append_output(self, text: str):
        # Append text from worker
        self.output_console.append(text.rstrip('\n'))

    def _shred_finished(self, exit_code: int):
        if exit_code == 0:
            self.output_console.append('\nShred completed successfully.')
            QMessageBox.information(self, "Done", "Shred completed successfully.")
        else:
            self.output_console.append(f"\nShred exited with code {exit_code}")
            QMessageBox.warning(self, "Shred Failed", f"Shred exited with code {exit_code}")

        self.wipe_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
