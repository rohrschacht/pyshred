#!/usr/bin/env python3
"""
PyQt6 GUI for listing storage devices and securely wiping them with `shred`.
Now updates device details when navigating with arrow keys in the table.
"""

import sys
import os
import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QMessageBox,
    QTextEdit,
    QFrame,
    QSpacerItem,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)


@dataclass
class BlockDevice:
    name: str
    path: str
    size: str
    model: str
    vendor: str
    type: str
    tran: Optional[str] = None
    rota: Optional[str] = None


def parse_lsblk() -> List[BlockDevice]:
    cmd = ["lsblk", "-J", "-o", "NAME,KNAME,TYPE,SIZE,MODEL,VENDOR,TRAN,ROTA"]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout)

    devices: List[BlockDevice] = []

    def walk(nodes):
        for n in nodes:
            if n.get("type") == "disk":
                name = n.get("kname") or n.get("name")
                devices.append(
                    BlockDevice(
                        name=name,
                        path=f"/dev/{name}",
                        size=n.get("size", ""),
                        model=(n.get("model") or "").strip(),
                        vendor=(n.get("vendor") or "").strip(),
                        type=n.get("type"),
                        tran=n.get("tran"),
                        rota=str(n.get("rota")) if n.get("rota") is not None else None,
                    )
                )
            if n.get("children"):
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
        shred_cmd = ["shred", "-v", f"-n", str(self.passes), self.device_path]
        use_pkexec = (os.geteuid() != 0) and shutil.which("pkexec") is not None
        cmd = ["pkexec"] + shred_cmd if use_pkexec else shred_cmd

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception as e:
            self.output.emit(f"Failed to start shred: {e}\n")
            self.finished.emit(1)
            return

        for line in proc.stdout:
            self.output.emit(line)
        proc.wait()
        self.finished.emit(proc.returncode)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Secure Wipe GUI")
        self.resize(900, 500)

        self.layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.layout.addLayout(header)

        header.addWidget(QLabel("<b>Secure Wipe — select a device to securely erase</b>"))
        header.addItem(QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        about_btn = QPushButton("About / Safety")
        about_btn.clicked.connect(self.show_about)
        header.addWidget(about_btn)

        refresh_btn = QPushButton("Refresh Devices")
        refresh_btn.clicked.connect(self.refresh_devices)
        header.addWidget(refresh_btn)
        self.refresh_btn = refresh_btn

        main_h = QHBoxLayout()
        self.layout.addLayout(main_h)

        # Table for devices
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Type", "Path", "Size", "Vendor", "Model", "Transport"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.cellClicked.connect(self.on_device_selected)
        self.table.currentCellChanged.connect(self.on_device_selected_keyboard)
        # Stretch columns to fill available width
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        main_h.addWidget(self.table, 50)

        vright = QVBoxLayout()
        main_h.addLayout(vright, 50)

        details_frame = QFrame()
        details_layout = QVBoxLayout(details_frame)

        self.details_label = QLabel("Select a device to see details and actions.")
        self.details_label.setWordWrap(True)
        details_layout.addWidget(self.details_label)

        self.wipe_btn = QPushButton("Securely Wipe Device")
        self.wipe_btn.setEnabled(False)
        self.wipe_btn.clicked.connect(self.on_wipe_clicked)
        details_layout.addWidget(self.wipe_btn)

        vright.addWidget(details_frame, 20)

        self.output_console = QTextEdit()
        self.output_console.setReadOnly(True)
        vright.addWidget(self.output_console, 80)

        self.devices: List[BlockDevice] = []
        self.selected_device: Optional[BlockDevice] = None

        self.refresh_devices()

    def show_about(self):
        QMessageBox.information(self, "About & Safety", (
            "This tool lists block devices using 'lsblk' and uses 'shred' to overwrite them.\n\n"
            "⚠️ Wiping is irreversible. Double-check the device path before confirming.\n"
            "You may be prompted for admin rights using 'pkexec'."
        ))

    def refresh_devices(self):
        self.refresh_btn.setEnabled(False)
        self.table.setRowCount(0)
        self.devices.clear()
        self.details_label.setText("Refreshing device list...")
        QApplication.processEvents()

        try:
            self.devices = parse_lsblk()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to list devices: {e}")
            self.refresh_btn.setEnabled(True)
            return

        self.table.setRowCount(len(self.devices))
        for row, d in enumerate(self.devices):
            icon_text = self.device_icon_text(d)
            self.table.setItem(row, 0, QTableWidgetItem(icon_text))
            self.table.setItem(row, 1, QTableWidgetItem(d.path))
            self.table.setItem(row, 2, QTableWidgetItem(d.size))
            self.table.setItem(row, 3, QTableWidgetItem(d.vendor))
            self.table.setItem(row, 4, QTableWidgetItem(d.model))
            self.table.setItem(row, 5, QTableWidgetItem(d.tran or ""))

        self.refresh_btn.setEnabled(True)
        self.details_label.setText("Select a device to see details and actions.")

    def device_icon_text(self, d: BlockDevice) -> str:
        if d.tran and d.tran.lower() == "usb":
            return "🔌 USB"
        try:
            if d.rota is not None and int(d.rota) == 1:
                return "🟠 HDD"
        except Exception:
            pass
        return "⚫ SSD"

    def on_device_selected(self, row, col):
        self.update_device_selection(row)

    def on_device_selected_keyboard(self, current_row, current_col, previous_row, previous_col):
        if current_row >= 0:
            self.update_device_selection(current_row)

    def update_device_selection(self, row: int):
        d = self.devices[row]
        self.selected_device = d
        self.details_label.setText(
            f"Selected: {d.path}\nSize: {d.size}\nVendor: {d.vendor or 'Unknown'}\nModel: {d.model or 'Unknown'}\nTransport: {d.tran or 'Unknown'}"
        )
        self.wipe_btn.setEnabled(True)

    def on_wipe_clicked(self):
        if not self.selected_device:
            return
        d = self.selected_device

        reply = QMessageBox.question(
            self,
            "Confirm Wipe",
            f"Are you sure you want to securely wipe {d.path}?\nThis will irreversibly destroy all data.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        confirm_text, ok = QMessageBox.getText(
            self,
            "Type to Confirm",
            f"Type the device path to confirm (e.g. {d.path}):",
        )
        if not ok or confirm_text.strip() != d.path:
            QMessageBox.warning(self, "Confirmation Failed", "Device path did not match. Aborting.")
            return

        self.wipe_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.output_console.append(f"Starting shred on {d.path}...\n")

        self.shred_worker = ShredWorker(d.path, passes=3)
        self.shred_worker.output.connect(self.output_console.append)
        self.shred_worker.finished.connect(self.shred_finished)
        self.shred_worker.start()

    def shred_finished(self, exit_code: int):
        if exit_code == 0:
            self.output_console.append("\nShred completed successfully.")
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
