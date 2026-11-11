import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from PyQt5.QtWidgets import QApplication

import ui.main_window as mw_mod
from core.server_advisor import BdeSettings
from test_simulator import build_synthetic_paradox_db
from ui.dialogs import BulkPasteUsersDialog, FetchServerKeyDialog, ServerRecommendationDialog
from ui.styles import DARK_STYLESHEET


class _MockAbsensiHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/API/GETVERSION":
            body = '{"status":"Success","data":{"result":[{"MinimumVersion":"1.0.33"}]}}'
        elif path == "/API/auth":
            if hasattr(self.server, "auth_calls"):
                self.server.auth_calls.append(
                    (qs.get("user_id", [""])[0], qs.get("password", [""])[0])
                )
            uid = qs.get("user_id", ["u1"])[0]
            body = f'{{"status":"Success","data":{{"result":[{{"Kode":"1001","UserID":"{uid}","Sales":"False"}}]}}}}'
        elif path == "/api/masuk1018":
            body = (
                '{"status":"Success","data":{"result":[{"Kode":"1001","Latitude":"-7.3595",'
                '"Longitude":"112.5935","LockGPS":"False","LockTime":"False",'
                '"Sekarang":"2026/11/11 08:15:30","JamKerja":"SHIFT 1"}]}}'
            )
        elif path in ("/API/getabsenusertglrange1008", "/api/getpesanunread"):
            body = '{"status":"Success","data":{"result":[]}}'
        elif path == "/API/absen1018":
            body = '{"status":"Success","msg":"OK"}'
        else:
            body = '{"status":"Success","data":{"result":[]}}'

        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestGuiIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(DARK_STYLESHEET)

    def test_mainwindow_run_and_dialogs(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockAbsensiHandler)
        port = server.server_address[1]
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_cfg = mw_mod.CONFIG_FILE
            orig_usr = mw_mod.USERS_FILE
            orig_tbl = mw_mod.TABLES_DIR
            mw_mod.CONFIG_FILE = os.path.join(tmpdir, "cfg.json")
            mw_mod.USERS_FILE = os.path.join(tmpdir, "usr.json")
            mw_mod.TABLES_DIR = os.path.join(tmpdir, "tables")
            # Seed manual mode in temp config to verify manual list workflow + users.json save
            with open(mw_mod.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"user_source": "manual"}, f)
            try:
                win = mw_mod.MainWindow()
                # Check dialogs instantiate cleanly and BulkPaste generator works
                dlg_bulk = BulkPasteUsersDialog(win)
                dlg_bulk.tabs.setCurrentIndex(1)
                dlg_bulk.sp_count.setValue(5)
                dlg_bulk._on_accept()
                gen_users = dlg_bulk.get_users()
                self.assertEqual(len(gen_users), 5)

                dlg_key = FetchServerKeyDialog(win)
                self.assertEqual(dlg_key.ed_key.text(), "")

                # Apply preset & point window to local mock server
                win._apply_speed_preset("burst")
                self.assertEqual(win.sp_concurrency.value(), 150)
                win.ed_ip.setText("127.0.0.1")
                win.ed_port.setText(str(port))
                win.rb_target_total.setChecked(True)
                win.sp_target_total.setValue(6)

                # Trigger simulation via UI button and wait for QThread completion
                win._on_start_clicked()
                self.assertIsNotNone(win._worker)
                while win._worker.isRunning():
                    self.app.processEvents()

                # Flush pending Qt signals/events
                for _ in range(10):
                    self.app.processEvents()

                self.assertEqual(win.card_success.lbl_value.text(), "6")
                self.assertEqual(win.card_failed.lbl_value.text(), "0")
                self.assertEqual(win.tbl_monitor.rowCount(), 6)
                self.assertGreater(win.tbl_endpoints.rowCount(), 0)
                win._flush_logs()
                self.assertIn("Simulasi selesai", win.txt_logs.toPlainText())

                win.close()
                self.assertTrue(os.path.isfile(mw_mod.CONFIG_FILE))
                self.assertTrue(os.path.isfile(mw_mod.USERS_FILE))
            finally:
                mw_mod.CONFIG_FILE = orig_cfg
                mw_mod.USERS_FILE = orig_usr
                mw_mod.TABLES_DIR = orig_tbl
                server.shutdown()
                server.server_close()

    def test_mainwindow_reads_karyawan_db_and_runs_fresh(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockAbsensiHandler)
        server.auth_calls = []
        port = server.server_address[1]
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_cfg = mw_mod.CONFIG_FILE
            orig_usr = mw_mod.USERS_FILE
            orig_tbl = mw_mod.TABLES_DIR
            mw_mod.CONFIG_FILE = os.path.join(tmpdir, "cfg.json")
            mw_mod.USERS_FILE = os.path.join(tmpdir, "usr.json")
            mw_mod.TABLES_DIR = os.path.join(tmpdir, "tables")
            db_file = os.path.join(mw_mod.TABLES_DIR, "Karyawan.DB")

            cols = [
                ("Kode", 0x01, 10),
                ("UserID", 0x01, 15),
                ("PasswordApk", 0x01, 15),
                ("Aktif", 0x09, 1),
                ("Sales", 0x09, 1),
            ]
            build_synthetic_paradox_db(
                db_file,
                cols,
                [
                    {"Kode": "0001", "UserID": "pegawai1", "PasswordApk": "rahasia1", "Aktif": True, "Sales": False},
                    {"Kode": "0002", "UserID": "pegawai2", "PasswordApk": "rahasia2", "Aktif": False, "Sales": False},
                    {"Kode": "0003", "UserID": "pegawai3", "PasswordApk": "rahasia3", "Aktif": True, "Sales": False},
                ],
            )
            try:
                win = mw_mod.MainWindow()
                self.assertTrue(win.rb_src_db.isChecked())
                self.assertEqual(win.tbl_users.rowCount(), 2)
                self.assertEqual(win.tbl_users.item(0, 0).text(), "pegawai1")
                self.assertEqual(win.tbl_users.item(0, 1).text(), "rahasia1")
                self.assertFalse(win.btn_save_users.isEnabled())

                # Show inactive as well -> 3 rows
                win.chk_db_only_active.setChecked(False)
                self.assertEqual(win.tbl_users.rowCount(), 3)
                win.chk_db_only_active.setChecked(True)
                self.assertEqual(win.tbl_users.rowCount(), 2)

                # Update Karyawan.DB externally on disk; clicking Start must re-read directly
                build_synthetic_paradox_db(
                    db_file,
                    cols,
                    [
                        {"Kode": "0001", "UserID": "pegawai_baru", "PasswordApk": "pw_baru", "Aktif": True, "Sales": False},
                        {"Kode": "0002", "UserID": "pegawai_off", "PasswordApk": "pw_off", "Aktif": False, "Sales": False},
                    ],
                )

                win._apply_speed_preset("burst")
                win.ed_ip.setText("127.0.0.1")
                win.ed_port.setText(str(port))
                win.rb_all_once.setChecked(True)
                win._on_start_clicked()
                self.assertIsNotNone(win._worker)
                while win._worker.isRunning():
                    self.app.processEvents()
                for _ in range(10):
                    self.app.processEvents()

                self.assertEqual(win.tbl_users.rowCount(), 1)
                self.assertEqual(win.card_success.lbl_value.text(), "1")
                self.assertEqual(server.auth_calls, [("pegawai_baru", "pw_baru")])

                win.close()
                self.assertFalse(os.path.exists(mw_mod.USERS_FILE))
            finally:
                mw_mod.CONFIG_FILE = orig_cfg
                mw_mod.USERS_FILE = orig_usr
                mw_mod.TABLES_DIR = orig_tbl
                server.shutdown()
                server.server_close()

    def test_server_recommendation_dialog_checkbox_behavior_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_cfg = mw_mod.CONFIG_FILE
            orig_usr = mw_mod.USERS_FILE
            orig_tbl = mw_mod.TABLES_DIR
            mw_mod.CONFIG_FILE = os.path.join(tmpdir, "cfg.json")
            mw_mod.USERS_FILE = os.path.join(tmpdir, "usr.json")
            mw_mod.TABLES_DIR = os.path.join(tmpdir, "tables")

            db_file = os.path.join(mw_mod.TABLES_DIR, "Karyawan.DB")
            cols = [
                ("Kode", 0x01, 10),
                ("UserAPK", 0x01, 16),
                ("PasswordAPK", 0x01, 16),
                ("Aktif", 0x09, 1),
            ]
            rows = [
                {"Kode": f"{i:04d}", "UserAPK": f"usr{i:03d}", "PasswordAPK": "123", "Aktif": (i < 120)}
                for i in range(130)
            ]
            build_synthetic_paradox_db(db_file, cols, rows, records_per_block=20)

            ini_file = os.path.join(tmpdir, "HTTPServerDB.ini")
            with open(ini_file, "w", encoding="utf-8") as f:
                f.write(
                    "[Settings]\n"
                    "Port=56563\n"
                    "MaxConnections=20\n"
                    "ListenQueue=5\n"
                    "SessionTimeOut=0\n"
                    "MaxThreads=500\n"
                    "DetailLog=Y\n"
                )
            with open(mw_mod.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"http_server_ini_path": ini_file}, f)

            fake_bde = BdeSettings(
                memsize=16,
                sharedmemsize=2048,
                maxfilehandles=48,
                minbufsize=128,
                maxbufsize=2048,
                local_share="FALSE",
                registry_found=True,
                source="HKLM\\Mock\\SYSTEM\\INIT",
                cfg_settings={"MEMSIZE": "128", "SHAREDMEMSIZE": "8192", "MAXFILEHANDLES": "255"},
                cfg_mismatch=["MEMSIZE: Registry=16 MB vs CFG=128 MB"],
            )

            try:
                win = mw_mod.MainWindow()
                self.assertTrue(hasattr(win, "btn_calc_server"))
                self.assertEqual(win.tbl_users.rowCount(), 120)

                dlg = win._create_recommendation_dialog(
                    app_root=tmpdir,
                    tables_dir=mw_mod.TABLES_DIR,
                    bde_reader=lambda: fake_bde,
                )
                # Reads 120 active users from Karyawan.DB automatically
                self.assertEqual(dlg.sp_target_users.value(), 120)
                self.assertTrue(dlg.chk_allow_bde.isChecked())
                self.assertTrue(dlg.chk_allow_ini.isChecked())

                rep_both = dlg.get_report()
                self.assertIsNotNone(rep_both)
                self.assertTrue(rep_both.can_achieve_full_burst_zero_fail)
                self.assertEqual(dlg.tbl_ini.rowCount(), 5)
                self.assertEqual(dlg.tbl_bde.rowCount(), 6)

                # Uncheck BDE -> ONLY .ini suggestions allowed; BDE stays unchanged
                dlg.chk_allow_bde.setChecked(False)
                self.app.processEvents()
                rep_ini_only = dlg.get_report()
                self.assertFalse(rep_ini_only.allow_bde)
                self.assertTrue(rep_ini_only.allow_ini)
                self.assertTrue(all(it.status == "Tidak diubah" for it in rep_ini_only.bde_items))
                self.assertEqual(dlg.tbl_bde.item(0, 3).text(), "16 MB")
                self.assertEqual(dlg.tbl_bde.item(0, 4).text(), "Tidak diubah")
                self.assertGreater(rep_ini_only.recommended_ini.max_connections, 20)
                self.assertEqual(dlg.tbl_ini.item(0, 3).text(), "Ubah")

                # Uncheck .ini while BDE checked -> .ini stays unchanged
                dlg.chk_allow_bde.setChecked(True)
                dlg.chk_allow_ini.setChecked(False)
                self.app.processEvents()
                rep_bde_only = dlg.get_report()
                self.assertTrue(all(it.status == "Tidak diubah" for it in rep_bde_only.ini_items))
                self.assertEqual(dlg.tbl_ini.item(0, 2).text(), "20")

                # Toggling "Hanya Karyawan Aktif" reads 130 rows directly from Karyawan.DB
                dlg.chk_only_active.setChecked(False)
                self.app.processEvents()
                self.assertEqual(dlg.sp_target_users.value(), 130)

                # Apply safe simulator recommendation & verify button flow on MainWindow
                dlg.chk_allow_bde.setChecked(False)
                dlg.chk_allow_ini.setChecked(True)
                self.app.processEvents()
                dlg._on_apply_safe_sim()
                self.assertIsNotNone(dlg.applied_sim_settings)

                # Test MainWindow.btn_calc_server click integration cleanly
                orig_factory = win._create_recommendation_dialog

                def _mock_factory(**kwargs):
                    d = orig_factory(app_root=tmpdir, tables_dir=mw_mod.TABLES_DIR, bde_reader=lambda: fake_bde)
                    d.chk_allow_bde.setChecked(False)
                    d.chk_allow_ini.setChecked(True)
                    d.exec_ = d._on_apply_safe_sim  # simulate user clicking Terapkan
                    return d

                win._create_recommendation_dialog = _mock_factory
                win.btn_calc_server.click()
                self.app.processEvents()
                self.assertLessEqual(win.sp_concurrency.value(), rep_ini_only.projected_bde_capacity)
                self.assertGreaterEqual(win.sp_launch_interval.value(), 25)
                self.assertEqual(win.sp_target_total.value(), 120)

                win.close()
            finally:
                mw_mod.CONFIG_FILE = orig_cfg
                mw_mod.USERS_FILE = orig_usr
                mw_mod.TABLES_DIR = orig_tbl


if __name__ == "__main__":
    unittest.main()
