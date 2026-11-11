import os
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from core.server_advisor import (
    BdeSettings,
    HttpServerIniSettings,
    calculate_recommendations,
    estimate_bde_safe_concurrency,
    estimate_ini_safe_concurrency,
    parse_idapi_cfg_bytes,
    read_bde_settings,
    read_http_server_ini,
    read_karyawan_summary,
)
from tests.test_simulator import build_synthetic_paradox_db


def _build_synthetic_idapi_cfg(init_pairs: dict) -> bytes:
    """Construct a minimal Borland IDAPI32.CFG binary tree containing SYSTEM\\INIT."""
    chunks = [
        b"\x03\x00\x00\x00SYSTEM\x00\x01\x00",
        b"\x03\x00\x00\x00INIT\x00\x01\x00",
    ]
    for k, v in init_pairs.items():
        chunks.append(
            b"\x03\x00\x01\x00"
            + k.encode("ascii")
            + b"\x00\x04\x00"
            + str(v).encode("ascii")
            + b"\x00"
        )
    chunks.append(b"\x02\x00\x02\x00")
    chunks.append(b"\x03\x00\x00\x00DRIVERS\x00\x01\x00\x02\x00")
    return b"".join(chunks)


class TestServerAdvisor(unittest.TestCase):
    def test_read_bde_settings_detects_registry_vs_cfg_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "IDAPI32.CFG")
            with open(cfg_path, "wb") as f:
                f.write(
                    _build_synthetic_idapi_cfg(
                        {
                            "MEMSIZE": "128",
                            "SHAREDMEMSIZE": "8192",
                            "MAXFILEHANDLES": "255",
                            "MINBUFSIZE": "1024",
                            "MAXBUFSIZE": "8192",
                            "LOCAL SHARE": "FALSE",
                        }
                    )
                )

            def fake_reg(key_path: str):
                if key_path.endswith(r"\SYSTEM\INIT"):
                    return {
                        "MEMSIZE": "16",
                        "SHAREDMEMSIZE": "2048",
                        "MAXFILEHANDLES": "48",
                        "MINBUFSIZE": "128",
                        "MAXBUFSIZE": "2048",
                        "LOCAL SHARE": "FALSE",
                    }
                if key_path.endswith(r"\Borland\Database Engine"):
                    return {"CONFIGFILE01": cfg_path, "SAVECONFIG": "WIN31"}
                return None

            with open(cfg_path, "rb") as f:
                parsed_cfg = parse_idapi_cfg_bytes(f.read())
            self.assertEqual(parsed_cfg["MEMSIZE"], "128")
            self.assertEqual(parsed_cfg["MAXFILEHANDLES"], "255")

            bde = read_bde_settings(registry_reader=fake_reg)
            self.assertTrue(bde.registry_found)
            self.assertEqual(bde.memsize, 16)
            self.assertEqual(bde.sharedmemsize, 2048)
            self.assertEqual(bde.maxfilehandles, 48)
            self.assertGreaterEqual(len(bde.cfg_mismatch), 3)
            self.assertTrue(any("MEMSIZE" in m for m in bde.cfg_mismatch))

    def test_read_bde_settings_fallback_when_missing(self) -> None:
        bde = read_bde_settings(registry_reader=lambda _p: None, cfg_parser=lambda _c: {})
        self.assertFalse(bde.registry_found)
        self.assertEqual(bde.memsize, 16)
        self.assertEqual(bde.sharedmemsize, 2048)
        self.assertEqual(bde.maxfilehandles, 48)

    def test_read_http_server_ini_parses_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ini_path = os.path.join(tmpdir, "HTTPServerDB.ini")
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write(
                    "[Settings]\n"
                    "Port=56563\n"
                    "MaxConnections=20\n"
                    "ListenQueue=5\n"
                    "SessionTimeOut=0\n"
                    "MaxThreads=500\n"
                    "DetailLog=Y\n"
                    "DBAlias=sgPayroll\n"
                    "NetDir=D:\\TEMP\n"
                )
            ini = read_http_server_ini(ini_path)
            self.assertTrue(ini.exists)
            self.assertEqual(ini.port, 56563)
            self.assertEqual(ini.max_connections, 20)
            self.assertEqual(ini.listen_queue, 5)
            self.assertEqual(ini.max_threads, 500)
            self.assertEqual(ini.detail_log, "Y")
            self.assertEqual(ini.db_alias, "sgPayroll")

    def test_recommendations_all_four_checkbox_combinations(self) -> None:
        cur_bde = BdeSettings(memsize=16, sharedmemsize=2048, maxfilehandles=48, minbufsize=128, maxbufsize=2048)
        cur_ini = HttpServerIniSettings(
            ini_path=r"D:\SJABPayroll\HTTPServerDB.ini",
            exists=True,
            max_connections=20,
            listen_queue=5,
            session_timeout=0,
            max_threads=500,
            detail_log="Y",
        )

        # 1. Both BDE & .ini checked -> 0% fail achievable for 124 users simultaneously
        rep_both = calculate_recommendations(124, cur_bde, cur_ini, allow_bde=True, allow_ini=True)
        self.assertTrue(rep_both.can_achieve_full_burst_zero_fail)
        self.assertGreaterEqual(rep_both.recommended_ini.max_connections, 124)
        self.assertGreaterEqual(rep_both.recommended_ini.listen_queue, 124)
        self.assertEqual(rep_both.recommended_ini.detail_log, "N")
        self.assertEqual(rep_both.recommended_ini.max_threads, 500)  # 500 already >= 320
        self.assertEqual(rep_both.recommended_bde.memsize, 205)
        self.assertGreaterEqual(rep_both.recommended_bde.sharedmemsize, 16384)
        self.assertGreaterEqual(rep_both.recommended_bde.maxfilehandles, 512)
        self.assertEqual(rep_both.recommended_sim_concurrency, 124)

        # 2. ONLY .ini checked (BDE unchecked) -> BDE MUST be strictly unchanged!
        rep_ini_only = calculate_recommendations(124, cur_bde, cur_ini, allow_bde=False, allow_ini=True)
        for item in rep_ini_only.bde_items:
            self.assertEqual(item.current, item.recommended)
            self.assertFalse(item.changed)
            self.assertTrue(item.locked)
            self.assertEqual(item.status, "Tidak diubah")
        self.assertEqual(rep_ini_only.recommended_bde.memsize, 16)
        self.assertEqual(rep_ini_only.recommended_bde.sharedmemsize, 2048)
        self.assertEqual(rep_ini_only.recommended_bde.maxfilehandles, 48)
        # Meanwhile .ini IS updated
        self.assertGreater(rep_ini_only.recommended_ini.max_connections, 20)
        self.assertGreater(rep_ini_only.recommended_ini.listen_queue, 5)
        self.assertEqual(rep_ini_only.recommended_ini.detail_log, "N")
        # Unchanged default BDE caps full 124 burst -> gives safe concurrency < BDE ceiling
        self.assertFalse(rep_ini_only.can_achieve_full_burst_zero_fail)
        bde_cap, _ = estimate_bde_safe_concurrency(cur_bde)
        self.assertLessEqual(rep_ini_only.recommended_sim_concurrency, bde_cap)
        self.assertIn("BDE tidak diubah", rep_ini_only.headline_status)

        # 2b. ONLY .ini checked when BDE is ALREADY tuned high -> 0% fail full burst with BDE unchanged!
        tuned_bde = BdeSettings(memsize=205, sharedmemsize=16384, maxfilehandles=1024, minbufsize=1024, maxbufsize=8192)
        rep_ini_only_tuned = calculate_recommendations(124, tuned_bde, cur_ini, allow_bde=False, allow_ini=True)
        self.assertTrue(rep_ini_only_tuned.can_achieve_full_burst_zero_fail)
        self.assertTrue(all(item.status == "Tidak diubah" for item in rep_ini_only_tuned.bde_items))
        self.assertGreater(rep_ini_only_tuned.recommended_ini.max_connections, 20)

        # 3. ONLY BDE checked (.ini unchecked) -> .ini MUST be strictly unchanged!
        rep_bde_only = calculate_recommendations(124, cur_bde, cur_ini, allow_bde=True, allow_ini=False)
        for item in rep_bde_only.ini_items:
            self.assertEqual(item.current, item.recommended)
            self.assertFalse(item.changed)
            self.assertTrue(item.locked)
            self.assertEqual(item.status, "Tidak diubah")
        self.assertEqual(rep_bde_only.recommended_ini.max_connections, 20)
        self.assertEqual(rep_bde_only.recommended_ini.listen_queue, 5)
        self.assertEqual(rep_bde_only.recommended_bde.memsize, 205)
        ini_cap, _ = estimate_ini_safe_concurrency(cur_ini)
        self.assertLessEqual(rep_bde_only.recommended_sim_concurrency, ini_cap)

        # 4. Neither checked -> both unchanged
        rep_none = calculate_recommendations(124, cur_bde, cur_ini, allow_bde=False, allow_ini=False)
        self.assertTrue(all(item.locked and not item.changed for item in rep_none.ini_items))
        self.assertTrue(all(item.locked and not item.changed for item in rep_none.bde_items))
        self.assertFalse(rep_none.can_achieve_full_burst_zero_fail)

    def test_read_karyawan_summary_counts_active_and_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tables_dir = os.path.join(tmpdir, "tables")
            db_path = os.path.join(tables_dir, "Karyawan.DB")
            cols = [
                ("Kode", 0x01, 10),
                ("UserAPK", 0x01, 15),
                ("PasswordAPK", 0x01, 15),
                ("Aktif", 0x09, 1),
            ]
            build_synthetic_paradox_db(
                db_path,
                cols,
                [
                    {"Kode": "1", "UserAPK": "u1", "PasswordAPK": "p1", "Aktif": True},
                    {"Kode": "2", "UserAPK": "u2", "PasswordAPK": "p2", "Aktif": False},
                    {"Kode": "3", "UserAPK": "u3", "PasswordAPK": "p3", "Aktif": True},
                    {"Kode": "4", "UserAPK": "", "PasswordAPK": "", "Aktif": True},
                ],
            )
            summary = read_karyawan_summary("tables/Karyawan.DB", tmpdir, tables_dir, only_active=True)
            self.assertTrue(summary["loaded"])
            self.assertEqual(summary["user_count"], 2)
            self.assertEqual(summary["active_count"], 2)
            self.assertEqual(summary["all_accounts_count"], 3)
            self.assertEqual(summary["total_rows"], 4)


if __name__ == "__main__":
    unittest.main()
