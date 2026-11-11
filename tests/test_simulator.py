import asyncio
import io
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from PIL import Image
import aiohttp
from aiohttp import web

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from core.image_gen import generate_attendance_jpeg
from core.json_utils import (
    add_gps_jitter,
    build_dart_url,
    haversine_meters,
    parse_server_datetime,
    parse_version_score,
    safe_json_decode,
)
from core.karyawan_db import load_users_from_karyawan_db, read_karyawan_rows, resolve_karyawan_db_path
from core.models import SimConfig, UserAccount
from core.simulator import BulkAttendanceSimulator, _create_connector


def build_synthetic_paradox_db(
    path: str,
    columns: list,
    rows: list,
    records_per_block: int = 2,
) -> None:
    """Create a valid unencrypted Paradox 7 (.DB) binary file for testing.

    columns: list of (name, ftype, fsize) supporting Alpha(0x01) and Logical(0x09).
    """
    num_fields = len(columns)
    rec_size = sum(sz for _, _, sz in columns)
    block_size = max(1024, ((6 + records_per_block * rec_size + 1023) // 1024) * 1024)
    max_tbl_size = block_size // 1024

    field_names_blob = b"".join(name.encode("cp1252") + b"\x00" for name, _, _ in columns)
    names_off = 0x78 + 2 * num_fields + 4 + 4 * num_fields + 261
    hdr_size = ((names_off + len(field_names_blob) + 1023) // 1024) * 1024

    # Split rows across blocks
    blocks = []
    if rows:
        for i in range(0, len(rows), records_per_block):
            blocks.append(rows[i : i + records_per_block])
    num_blocks = len(blocks)

    hdr = bytearray(hdr_size)
    struct.pack_into("<HHBB", hdr, 0, rec_size, hdr_size, 0x00, max_tbl_size)
    struct.pack_into(
        "<IHHHH",
        hdr,
        6,
        len(rows),
        num_blocks,
        num_blocks,
        1 if num_blocks else 0,
        num_blocks,
    )
    struct.pack_into("<H", hdr, 0x21, num_fields)
    struct.pack_into("<I", hdr, 0x25, 0xFF00FF00)
    hdr[0x39] = 0x0C  # Paradox 7
    struct.pack_into("<H", hdr, 0x6A, 1252)

    for idx, (_name, ftype, fsize) in enumerate(columns):
        struct.pack_into("<BB", hdr, 0x78 + idx * 2, ftype, fsize)

    tbl_name = os.path.basename(path).encode("cp1252")[:260]
    tbl_name_off = 0x78 + 2 * num_fields + 4 + 4 * num_fields
    hdr[tbl_name_off : tbl_name_off + len(tbl_name)] = tbl_name
    hdr[names_off : names_off + len(field_names_blob)] = field_names_blob

    out = bytearray(hdr)
    for b_idx, blk_rows in enumerate(blocks):
        blk_num = b_idx + 1
        next_blk = blk_num + 1 if blk_num < num_blocks else 0
        prev_blk = blk_num - 1 if blk_num > 1 else 0
        add_data_size = (len(blk_rows) - 1) * rec_size
        blk = bytearray(block_size)
        struct.pack_into("<HHh", blk, 0, next_blk, prev_blk, add_data_size)

        for r_idx, row in enumerate(blk_rows):
            rec = bytearray(rec_size)
            off = 0
            for name, ftype, fsize in columns:
                val = row.get(name)
                if ftype == 0x01:
                    encoded = str(val or "").encode("cp1252")[:fsize]
                    rec[off : off + len(encoded)] = encoded
                elif ftype == 0x09:
                    if val is True:
                        rec[off] = 0x81
                    elif val is False:
                        rec[off] = 0x80
                    else:
                        rec[off] = 0x00
                off += fsize
            r_off = 6 + r_idx * rec_size
            blk[r_off : r_off + rec_size] = rec
        out.extend(blk)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(out)


class TestJsonUtilsAndImage(unittest.TestCase):
    def test_safe_json_decode_invalid_escape(self) -> None:
        dirty = r'''{"status":"Success","data":{"result":[{"Nama":"O\'Connor","Note":"Test \'quote\'"}]}}'''
        parsed = safe_json_decode(dirty)
        self.assertEqual(parsed["status"], "Success")
        self.assertEqual(parsed["data"]["result"][0]["Nama"], "O'Connor")

    def test_build_dart_url_matches_flutter(self) -> None:
        url = build_dart_url(
            "http://127.0.0.1:8080",
            "/API/absen1018",
            [
                ("Tanggal", "11/11/2026"),
                ("Kode", "1001"),
                ("JamMasuk", "08:15:30"),
                ("latitude", "-7.3595,0"),
                ("Alamat", "Jl. Merdeka No. 1"),
            ],
        )
        self.assertIn("Tanggal=11/11/2026", url)
        self.assertIn("JamMasuk=08:15:30", url)
        self.assertIn("latitude=-7.3595,0", url)
        self.assertIn("Alamat=Jl.%20Merdeka%20No.%201", url)

        sales_url = build_dart_url(
            "http://127.0.0.1:8080",
            "/API/absensales1018",
            [("Deskripsi", "Visit Toko A & B")],
            encode_component_keys={"Deskripsi"},
        )
        self.assertEqual(sales_url, "http://127.0.0.1:8080/API/absensales1018?Deskripsi=Visit+Toko+A+%26+B")

    def test_version_and_gps_helpers(self) -> None:
        self.assertEqual(parse_version_score("1.0.33"), 10033)
        self.assertGreater(parse_version_score("1.0.34"), parse_version_score("1.0.33"))
        lat, lon = add_gps_jitter(-7.3595, 112.5935, max_meters=30.0)
        dist = haversine_meters(-7.3595, 112.5935, float(lat), float(lon))
        self.assertLessEqual(dist, 35.0)

    def test_generate_jpeg(self) -> None:
        dt = parse_server_datetime("2026/11/11 08:15:30")
        jpg = generate_attendance_jpeg("1001", "user01", dt)
        self.assertTrue(jpg.startswith(b"\xff\xd8"))
        with Image.open(io.BytesIO(jpg)) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.size, (480, 640))

    def test_connector_uses_threaded_resolver(self) -> None:
        async def _check():
            conn = _create_connector(50)
            try:
                self.assertIsInstance(conn._resolver, aiohttp.ThreadedResolver)
                self.assertTrue(conn.force_close)
            finally:
                await conn.close()

        asyncio.run(_check())


class TestBulkSimulatorE2E(unittest.TestCase):
    def test_concurrent_bulk_flow_regular_and_sales(self) -> None:
        async def _run_test():
            recorded_requests = []
            in_flight = 0
            max_in_flight = 0

            async def handle_any(request: web.Request) -> web.Response:
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                try:
                    await asyncio.sleep(0.015)
                    path = request.path
                    q = request.rel_url.query
                    recorded_requests.append((path, dict(q), request.headers.get("User-Agent")))

                    if path == "/API/GETVERSION":
                        return web.json_response({"status": "Success", "data": {"result": [{"MinimumVersion": "1.0.33"}]}})

                    if path == "/API/auth":
                        uid = q.get("user_id", "")
                        is_sales = "True" if "sales" in uid.lower() else "False"
                        kode = "2001" if is_sales == "True" else "1001"
                        # Return dirty \' escape just like backend SQL Server / Classic ASP often emits
                        body = (
                            '{"status":"Success","data":{"result":[{"Kode":"'
                            + kode
                            + '","UserID":"'
                            + uid
                            + '","Nama":"User \\\'Test\\\'","kLockgps":"True","Sales":"'
                            + is_sales
                            + '"}]}}'
                        )
                        return web.Response(text=body, content_type="text/plain")

                    if path == "/API/getabsenusertglrange1008":
                        return web.json_response(
                            {"status": "Success", "data": {"result": [{"Tanggal": "1899-12-30"}]}}
                        )

                    if path == "/api/getpesanunread":
                        return web.json_response({"status": "Success", "data": {"result": []}})

                    if path == "/API/getabsenuserperbulan1009":
                        return web.json_response({"status": "Success", "data": {"result": []}})

                    if path == "/API/getabsenuserperbulansales1017":
                        return web.json_response({"status": "Success", "data": {"result": []}})

                    if path == "/api/masuk1018":
                        uid = q.get("UserID", "")
                        kode = "2001" if "sales" in uid.lower() else "1001"
                        return web.json_response(
                            {
                                "status": "Success",
                                "data": {
                                    "result": [
                                        {
                                            "Kode": kode,
                                            "Latitude": "-7.359569347527608",
                                            "Longitude": "112.59350851182998",
                                            "LockGPS": "True",
                                            "LockTime": "False",
                                            "Sekarang": "2026/11/11 08:15:30",
                                            "JamKerja": "SHIFT 1",
                                        }
                                    ]
                                },
                            }
                        )

                    if path == "/API/getabsenusertglrange":
                        return web.json_response({"status": "Success", "data": {"result": []}})

                    if path == "/API/getnourutcekin":
                        return web.json_response({"status": "Success", "data": {"result": [{"NoUrut": "4"}]}})

                    if path == "/API/absen1018":
                        self.assertIn("Tanggal", q)
                        self.assertIn("Kode", q)
                        self.assertIn("JamMasuk", q)
                        self.assertIn("Foto", q)
                        self.assertIn("latitude", q)
                        self.assertIn("longitude", q)
                        self.assertIn("Alamat", q)
                        self.assertIn("JamKerja", q)
                        self.assertNotIn("Deskripsi", q)
                        return web.json_response({"status": "Success", "msg": "Absen OK"})

                    if path == "/API/absensales1018":
                        self.assertEqual(q.get("NoUrut"), "5")
                        self.assertIn("Deskripsi", q)
                        self.assertIn("Customer", q)
                        self.assertIn("NamaCustomer", q)
                        return web.json_response({"status": "Success", "msg": "Absen Sales OK"})

                    return web.json_response({"status": "Error", "msg": f"Unknown path {path}"}, status=404)
                finally:
                    in_flight -= 1

            app = web.Application()
            app.router.add_get("/{tail:.*}", handle_any)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]

            try:
                cfg = SimConfig(
                    ip_address="127.0.0.1",
                    ip_port=str(port),
                    user_run_mode="target_total",
                    target_total_runs=20,
                    max_concurrency=10,
                    launch_interval_ms=0,
                    step_delay_min_ms=5,
                    step_delay_max_ms=10,
                    step_getversion=True,
                    step_auth=True,
                    step_getabsen=True,
                    step_getpesanunread=True,
                    step_getabsen_bulanan=True,
                    step_masuk=True,
                    step_is_in_customer_sales=True,
                    step_ftp_upload=False,
                    step_absen=True,
                    strict_lock_validation=True,
                )
                users = [
                    UserAccount(user_id="user01", password="123", kode="", sales="Auto"),
                    UserAccount(user_id="sales01", password="123", kode="", sales="Auto"),
                ]

                stats_snapshots = []
                sim = BulkAttendanceSimulator(
                    config=cfg,
                    users=users,
                    on_stats_update=lambda s: stats_snapshots.append(s),
                )
                await sim.run()

                self.assertEqual(sim.total_runs, 20)
                self.assertEqual(sim.success_runs, 20)
                self.assertEqual(sim.failed_runs, 0)
                self.assertGreater(max_in_flight, 1)
                self.assertEqual(sim.step_metrics["auth"].success, 20)
                self.assertEqual(sim.step_metrics["absen1018"].success, 10)
                self.assertEqual(sim.step_metrics["absensales1018"].success, 10)
                self.assertTrue(all(ua == "Dart/3.1 (dart:io)" for _, _, ua in recorded_requests))
                self.assertTrue(len(stats_snapshots) > 0)
            finally:
                await runner.cleanup()

        asyncio.run(_run_test())

    def test_stop_cancellation_and_strict_lock_gps(self) -> None:
        async def _run_cancel_test():
            async def slow_handler(request: web.Request) -> web.Response:
                if request.path == "/API/GETVERSION":
                    await asyncio.sleep(0.05)
                    return web.json_response({"status": "Success", "data": {"result": [{"MinimumVersion": "1.0.33"}]}})
                if request.path == "/API/auth":
                    return web.json_response(
                        {"status": "Success", "data": {"result": [{"Kode": "1001", "UserID": "u1", "Sales": "False"}]}}
                    )
                if request.path == "/api/masuk1018":
                    return web.json_response(
                        {
                            "status": "Success",
                            "data": {
                                "result": [
                                    {
                                        "Kode": "1001",
                                        "Latitude": "-7.3595",
                                        "Longitude": "112.5935",
                                        "LockGPS": "True",
                                        "LockTime": "False",
                                        "Sekarang": "2026/11/11 08:15:30",
                                        "JamKerja": "SHIFT 1",
                                    }
                                ]
                            },
                        }
                    )
                return web.json_response({"status": "Success", "data": {"result": []}})

            app = web.Application()
            app.router.add_get("/{tail:.*}", slow_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]

            try:
                # 1. Test LockGPS strict rejection when custom coordinate is far away
                cfg_gps = SimConfig(
                    ip_address="127.0.0.1",
                    ip_port=str(port),
                    coord_mode="custom",
                    custom_latitude="-6.2000",  # Jakarta, >600km away from Sidoarjo
                    custom_longitude="106.8000",
                    strict_lock_validation=True,
                    step_delay_min_ms=0,
                    step_delay_max_ms=0,
                )
                sim_gps = BulkAttendanceSimulator(cfg_gps, [UserAccount(user_id="u1", password="123")])
                await sim_gps.run()
                self.assertEqual(sim_gps.failed_runs, 1)
                self.assertIn("LockGPS aktif", sim_gps.records[0].detail)

                # 2. Test early stop cancellation across queued users
                cfg_cancel = SimConfig(
                    ip_address="127.0.0.1",
                    ip_port=str(port),
                    user_run_mode="target_total",
                    target_total_runs=15,
                    max_concurrency=5,
                    launch_interval_ms=200,
                    step_delay_min_ms=0,
                    step_delay_max_ms=0,
                )
                sim_cancel = BulkAttendanceSimulator(cfg_cancel, [UserAccount(user_id="u1", password="123")])

                async def _stop_soon():
                    await asyncio.sleep(0.15)
                    sim_cancel.request_stop()

                await asyncio.gather(sim_cancel.run(), _stop_soon())
                cancelled_cnt = sum(1 for r in sim_cancel.records if r.status == "Cancelled")
                self.assertGreater(cancelled_cnt, 0)
            finally:
                await runner.cleanup()

        asyncio.run(_run_cancel_test())

    def test_indy_keepalive_header_immediate_close_no_disconnect(self) -> None:
        """Simulate Indy server returning Connection: Keep-Alive then immediately closing socket."""

        async def _run_indy_quirk_test():
            connection_count = 0
            request_count = 0

            async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
                nonlocal connection_count, request_count
                connection_count += 1
                try:
                    raw_head = await reader.readuntil(b"\r\n\r\n")
                    req_line = raw_head.decode("latin-1", "replace").splitlines()[0]
                    path = req_line.split(" ")[1].split("?")[0]
                    request_count += 1

                    if path == "/API/GETVERSION":
                        body = b'{"status":"Success","data":{"result":[{"MinimumVersion":"1.0.33"}]}}'
                    elif path == "/API/auth":
                        body = b'{"status":"Success","data":{"result":[{"Kode":"1001","UserID":"u1","Sales":"False"}]}}'
                    elif path == "/api/masuk1018":
                        body = (
                            b'{"status":"Success","data":{"result":[{"Kode":"1001","Latitude":"-7.3595",'
                            b'"Longitude":"112.5935","LockGPS":"False","LockTime":"False",'
                            b'"Sekarang":"2026/11/11 08:15:30","JamKerja":"SHIFT 1"}]}}'
                        )
                    elif path == "/API/absen1018":
                        body = b'{"status":"Success","msg":"OK"}'
                    else:
                        body = b'{"status":"Success","data":{"result":[]}}'

                    resp = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Connection: Keep-Alive\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                        + body
                    )
                    writer.write(resp)
                    await writer.drain()
                finally:
                    # Indy KeepAlive=False closes socket immediately right after response
                    writer.close()

            srv = await asyncio.start_server(handle_conn, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            try:
                cfg = SimConfig(
                    ip_address="127.0.0.1",
                    ip_port=str(port),
                    user_run_mode="target_total",
                    target_total_runs=30,
                    max_concurrency=30,
                    launch_interval_ms=0,
                    step_delay_min_ms=0,
                    step_delay_max_ms=0,
                    step_getversion=True,
                    step_auth=True,
                    step_getabsen=True,
                    step_getpesanunread=True,
                    step_getabsen_bulanan=True,
                    step_masuk=True,
                    step_ftp_upload=False,
                    step_absen=True,
                )
                sim = BulkAttendanceSimulator(cfg, [UserAccount(user_id="u1", password="123", sales="False")])
                await sim.run()

                self.assertEqual(sim.success_runs, 30)
                self.assertEqual(sim.failed_runs, 0)
                # Like Flutter's new Dio() per HTTP call, each request uses its own TCP connection
                self.assertEqual(connection_count, request_count)
                self.assertEqual(connection_count, 210)
            finally:
                srv.close()
                await srv.wait_closed()

        asyncio.run(_run_indy_quirk_test())


class TestKaryawanDbReader(unittest.TestCase):
    def test_paradox_db_userapk_and_userid_aliases_multiblock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Test UserAPK / PasswordAPK columns across multiple blocks
            p_apk = os.path.join(tmpdir, "tables", "karyawan.db")
            cols_apk = [
                ("Kode", 0x01, 10),
                ("UserAPK", 0x01, 15),
                ("PasswordAPK", 0x01, 15),
                ("Aktif", 0x09, 1),
                ("Sales", 0x09, 1),
            ]
            rows_apk = [
                {"Kode": "0001", "UserAPK": "andi", "PasswordAPK": "p1", "Aktif": True, "Sales": False},
                {"Kode": "0002", "UserAPK": "budi", "PasswordAPK": "p2", "Aktif": False, "Sales": True},
                {"Kode": "0003", "UserAPK": "", "PasswordAPK": "", "Aktif": True, "Sales": False},
                {"Kode": "0004", "UserAPK": "citra", "PasswordAPK": "p4", "Aktif": True, "Sales": True},
            ]
            build_synthetic_paradox_db(p_apk, cols_apk, rows_apk, records_per_block=2)

            resolved = resolve_karyawan_db_path("", tmpdir)
            self.assertEqual(os.path.normcase(resolved), os.path.normcase(p_apk))

            users_active, info_active = load_users_from_karyawan_db(resolved, only_active=True)
            self.assertEqual(info_active["format"], "Paradox")
            self.assertEqual(info_active["user_col"], "UserAPK")
            self.assertEqual(info_active["password_col"], "PasswordAPK")
            self.assertEqual(info_active["total_rows"], 4)
            self.assertEqual(info_active["skipped_inactive"], 1)
            self.assertEqual(info_active["skipped_empty_user"], 1)
            self.assertEqual(len(users_active), 2)
            self.assertEqual((users_active[0].user_id, users_active[0].password, users_active[0].sales), ("andi", "p1", "False"))
            self.assertEqual((users_active[1].user_id, users_active[1].password, users_active[1].sales), ("citra", "p4", "True"))

            users_all, info_all = load_users_from_karyawan_db(resolved, only_active=False)
            self.assertEqual(len(users_all), 3)
            self.assertEqual(info_all["skipped_inactive"], 0)

            # 2. Test UserID / PasswordApk columns (matching SJABPayroll Karyawan.DB)
            p_uid = os.path.join(tmpdir, "Karyawan.DB")
            cols_uid = [
                ("Kode", 0x01, 10),
                ("UserID", 0x01, 15),
                ("PasswordApk", 0x01, 15),
                ("Aktif", 0x09, 1),
                ("Sales", 0x09, 1),
            ]
            rows_uid = [
                {"Kode": "00000001", "UserID": "dina", "PasswordApk": "sec", "Aktif": True, "Sales": False},
            ]
            build_synthetic_paradox_db(p_uid, cols_uid, rows_uid)
            users_uid, info_uid = load_users_from_karyawan_db(p_uid, only_active=True)
            self.assertEqual(info_uid["user_col"], "UserID")
            self.assertEqual(info_uid["password_col"], "PasswordApk")
            self.assertEqual(users_uid[0].kode, "00000001")
            self.assertEqual(users_uid[0].user_id, "dina")

    def test_sqlite_karyawan_db_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sq_path = os.path.join(tmpdir, "karyawan_sqlite.db")
            conn = sqlite3.connect(sq_path)
            conn.execute(
                "CREATE TABLE karyawan (Kode TEXT, UserAPK TEXT, PasswordAPK TEXT, Aktif INTEGER, Sales INTEGER)"
            )
            conn.execute("INSERT INTO karyawan VALUES ('01', 'u_sq1', 'pw1', 1, 1)")
            conn.execute("INSERT INTO karyawan VALUES ('02', 'u_sq2', 'pw2', 0, 0)")
            conn.commit()
            conn.close()

            fmt, cols, rows = read_karyawan_rows(sq_path)
            self.assertEqual(fmt, "SQLite")
            self.assertEqual(len(rows), 2)
            users, info = load_users_from_karyawan_db(sq_path, only_active=True)
            self.assertEqual(len(users), 1)
            self.assertEqual((users[0].user_id, users[0].password, users[0].sales), ("u_sq1", "pw1", "True"))


if __name__ == "__main__":
    unittest.main()
