import asyncio
import random
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

from config.defaults import DEFAULT_SERVER_DISCOVERY_BASE, DEFAULT_SERVER_DISCOVERY_PASS
from core.ftp_client import upload_photo_ftp
from core.image_gen import generate_attendance_jpeg
from core.json_utils import (
    add_gps_jitter,
    build_dart_url,
    haversine_meters,
    parse_server_datetime,
    parse_version_score,
    safe_json_decode,
)
from core.models import SimConfig, StepMetric, UserAccount, UserRunRecord


def _parse_hhmmss(time_str: str, base_dt: datetime) -> Optional[datetime]:
    try:
        parts = [int(p) for p in str(time_str).strip().split(":")]
        if len(parts) < 2:
            return None
        sec = parts[2] if len(parts) > 2 else 0
        return base_dt.replace(hour=parts[0], minute=parts[1], second=sec, microsecond=0)
    except Exception:
        return None


def _check_lock_time(masuk_row: dict, jam_server: datetime, has_checked_in_today: bool) -> Tuple[bool, str]:
    """Replicate cekJamAbsenValid() from home_screen.dart lines 1876-1935."""
    weekday = jam_server.isoweekday()  # 1=Mon .. 7=Sun
    is_mon_fri = 1 <= weekday <= 5
    if is_mon_fri:
        min_key = "JamPulangSJMin" if has_checked_in_today else "JamMasukSJMin"
        max_key = "JamPulangSJMax" if has_checked_in_today else "JamMasukSJMax"
    else:
        min_key = "JamPulangSabtuMin" if has_checked_in_today else "JamMasukSabtuMin"
        max_key = "JamPulangSabtuMax" if has_checked_in_today else "JamMasukSabtuMax"

    t_min_str = masuk_row.get(min_key, "")
    t_max_str = masuk_row.get(max_key, "")
    t_min = _parse_hhmmss(t_min_str, jam_server)
    t_max = _parse_hhmmss(t_max_str, jam_server)
    if not t_min or not t_max:
        return True, ""
    if jam_server < t_min or jam_server > t_max:
        kind = "Pulang" if has_checked_in_today else "Masuk"
        return (
            False,
            f"Diluar toleransi jam {kind} ({t_min_str} - {t_max_str}), jam server {jam_server.strftime('%H:%M:%S')}",
        )
    return True, ""


def _create_connector(limit: int = 100, force_close: bool = True) -> aiohttp.TCPConnector:
    """Match Flutter's per-call Dio()/HttpClient TCP behavior and use Windows-safe ThreadedResolver."""
    return aiohttp.TCPConnector(
        limit=limit,
        ttl_dns_cache=300,
        force_close=force_close,
        resolver=aiohttp.ThreadedResolver(),
    )


async def fetch_server_by_key(server_key: str, timeout_s: float = 10.0) -> dict:
    """Port of SettingPage.fetchToken() + fetchServer() from lib/pages/setting.dart."""
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    headers = {"User-Agent": "Dart/3.1 (dart:io)"}
    async with aiohttp.ClientSession(
        connector=_create_connector(20),
        timeout=timeout,
        headers=headers,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        token_url = build_dart_url(
            DEFAULT_SERVER_DISCOVERY_BASE,
            "/api/gettoken",
            [("pass", DEFAULT_SERVER_DISCOVERY_PASS)],
        )
        # Note: Dart Uri.parse encodes ` to %60
        token_url = token_url.replace("`", "%60")
        async with session.get(token_url) as resp:
            raw = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"gettoken HTTP {resp.status}: {raw[:200]}")
            parsed = safe_json_decode(raw)
            token = str(parsed.get("data", {}).get("result", "") or "")
            if not token:
                raise RuntimeError("Token kosong dari server discovery")

        srv_url = build_dart_url(
            DEFAULT_SERVER_DISCOVERY_BASE,
            "/api/getserver",
            [("key", server_key.strip()), ("token", token)],
        )
        async with session.get(srv_url) as resp:
            raw = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"getserver HTTP {resp.status}: {raw[:200]}")
            parsed = safe_json_decode(raw)
            results = parsed.get("data", {}).get("result", [])
            if not isinstance(results, list) or not results:
                raise RuntimeError(f"Server key '{server_key}' tidak ditemukan")
            return results[0]


async def test_server_connection(config: SimConfig) -> Dict[str, Any]:
    """Quick connectivity test calling /API/GETVERSION and /API/getsetting."""
    timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)
    headers = {"User-Agent": config.user_agent}
    async with aiohttp.ClientSession(
        connector=_create_connector(20),
        timeout=timeout,
        headers=headers,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        t0 = time.perf_counter()
        url_ver = build_dart_url(config.base_url, "/API/GETVERSION")
        async with session.get(url_ver) as resp:
            raw_ver = await resp.text()
            ver_ms = (time.perf_counter() - t0) * 1000.0
            ver_json = safe_json_decode(raw_ver)

        t1 = time.perf_counter()
        url_set = build_dart_url(config.base_url, "/API/getsetting")
        async with session.get(url_set) as resp:
            raw_set = await resp.text()
            set_ms = (time.perf_counter() - t1) * 1000.0
            set_json = safe_json_decode(raw_set)

        return {
            "base_url": config.base_url,
            "version_ms": round(ver_ms, 1),
            "version_data": ver_json,
            "setting_ms": round(set_ms, 1),
            "setting_data": set_json,
        }


class BulkAttendanceSimulator:
    """Executes concurrent simulated Flutter attendance flows with real-time callbacks."""

    def __init__(
        self,
        config: SimConfig,
        users: List[UserAccount],
        on_init_records: Optional[Callable[[List[UserRunRecord]], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        on_user_update: Optional[Callable[[UserRunRecord], None]] = None,
        on_stats_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.config = config
        self.users = users
        self.on_init_records = on_init_records or (lambda recs: None)
        self.on_log = on_log or (lambda level, msg: None)
        self.on_user_update = on_user_update or (lambda rec: None)
        self.on_stats_update = on_stats_update or (lambda stats: None)

        self._stop_event = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self.step_metrics: Dict[str, StepMetric] = {}
        self.records: List[UserRunRecord] = []

        self.total_runs = 0
        self.completed_runs = 0
        self.success_runs = 0
        self.failed_runs = 0
        self.active_workers = 0
        self.total_http_requests = 0
        self.started_at = 0.0

    def request_stop(self) -> None:
        self._stop_event.set()
        for t in self._tasks:
            if not t.done():
                t.cancel()

    def _log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.on_log(level, f"[{ts}] {message}")

    def _record_step(self, step_name: str, ok: bool, elapsed_ms: float) -> None:
        if step_name not in self.step_metrics:
            self.step_metrics[step_name] = StepMetric(name=step_name)
        m = self.step_metrics[step_name]
        if ok:
            m.success += 1
        else:
            m.failed += 1
        m.total_ms += elapsed_ms

    def _emit_stats(self) -> None:
        elapsed_s = max(time.perf_counter() - self.started_at, 0.001)
        done_durations = [r.total_ms for r in self.records if r.status in ("Success", "Failed")]
        avg_ms = sum(done_durations) / len(done_durations) if done_durations else 0.0
        min_ms = min(done_durations) if done_durations else 0.0
        max_ms = max(done_durations) if done_durations else 0.0
        rps = self.total_http_requests / elapsed_s
        ups = self.completed_runs / elapsed_s

        self.on_stats_update(
            {
                "total": self.total_runs,
                "completed": self.completed_runs,
                "success": self.success_runs,
                "failed": self.failed_runs,
                "active": self.active_workers,
                "http_requests": self.total_http_requests,
                "elapsed_s": elapsed_s,
                "rps": rps,
                "ups": ups,
                "avg_ms": avg_ms,
                "min_ms": min_ms,
                "max_ms": max_ms,
                "step_metrics": {
                    k: {
                        "count": v.count,
                        "success": v.success,
                        "failed": v.failed,
                        "avg_ms": v.avg_ms,
                    }
                    for k, v in self.step_metrics.items()
                },
            }
        )

    async def _http_get_json(
        self,
        session: aiohttp.ClientSession,
        run_idx: int,
        step_name: str,
        url: str,
    ) -> Tuple[bool, Optional[dict], float, str]:
        t0 = time.perf_counter()
        self.total_http_requests += 1
        try:
            async with session.get(url) as resp:
                raw_text = await resp.text()
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if resp.status != 200:
                    self._record_step(step_name, False, elapsed_ms)
                    err = f"HTTP {resp.status}: {raw_text[:150]}"
                    self._log("ERROR", f"[#{run_idx}] {step_name} -> {err} ({elapsed_ms:.0f} ms)")
                    return False, None, elapsed_ms, err

                try:
                    parsed = safe_json_decode(raw_text)
                except Exception as je:
                    self._record_step(step_name, False, elapsed_ms)
                    err = f"JSON parse error: {je} | raw={raw_text[:120]}"
                    self._log("ERROR", f"[#{run_idx}] {step_name} -> {err} ({elapsed_ms:.0f} ms)")
                    return False, None, elapsed_ms, err

                if isinstance(parsed, dict) and str(parsed.get("status", "")).lower() == "error":
                    msg = str(parsed.get("msg") or parsed.get("message") or raw_text[:150])
                    self._record_step(step_name, False, elapsed_ms)
                    self._log("ERROR", f"[#{run_idx}] {step_name} -> Server Error: {msg} ({elapsed_ms:.0f} ms)")
                    return False, parsed, elapsed_ms, msg

                self._record_step(step_name, True, elapsed_ms)
                self._log("INFO", f"[#{run_idx}] {step_name} OK ({resp.status}, {elapsed_ms:.0f} ms)")
                return True, parsed if isinstance(parsed, dict) else {"raw": parsed}, elapsed_ms, "OK"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._record_step(step_name, False, elapsed_ms)
            err = f"{type(exc).__name__}: {exc}"
            self._log("ERROR", f"[#{run_idx}] {step_name} -> {err} ({elapsed_ms:.0f} ms)")
            return False, None, elapsed_ms, err

    async def _human_step_delay(self) -> None:
        d_min = max(0, int(self.config.step_delay_min_ms))
        d_max = max(d_min, int(self.config.step_delay_max_ms))
        if d_max > 0:
            delay_s = random.uniform(d_min, d_max) / 1000.0
            await asyncio.sleep(delay_s)

    async def _run_single_user(
        self,
        sem: asyncio.Semaphore,
        session: aiohttp.ClientSession,
        record: UserRunRecord,
        user: UserAccount,
    ) -> None:
        if self._stop_event.is_set():
            record.status = "Cancelled"
            record.current_step = "Cancelled"
            self.on_user_update(record)
            return

        async with sem:
            if self._stop_event.is_set():
                record.status = "Cancelled"
                record.current_step = "Cancelled"
                self.on_user_update(record)
                return

            self.active_workers += 1
            run_t0 = time.perf_counter()
            record.status = "Running"
            record.current_step = "Starting"
            self.on_user_update(record)
            self._emit_stats()

            kode = user.kode or ""
            user_id = user.user_id
            is_sales = user.sales == "True"
            jam_server = datetime.now()
            jam_kerja = ""
            masuk_row: Dict[str, Any] = {}
            data_absen_hari_ini_count = 0
            is_in_sales = True

            try:
                # 1. GETVERSION (CekUpdate.dart)
                if self.config.step_getversion:
                    record.current_step = "GETVERSION"
                    self.on_user_update(record)
                    url_ver = build_dart_url(self.config.base_url, "/API/GETVERSION")
                    ok, data, _, err = await self._http_get_json(session, record.run_index, "GETVERSION", url_ver)
                    record.steps_trace.append(f"GETVERSION:{'OK' if ok else 'ERR'}")
                    if ok and data and self.config.strict_lock_validation:
                        results = data.get("data", {}).get("result", [])
                        if isinstance(results, list) and results and "MinimumVersion" in results[0]:
                            min_ver = str(results[0]["MinimumVersion"])
                            if parse_version_score(self.config.app_version) < parse_version_score(min_ver):
                                raise RuntimeError(f"App version {self.config.app_version} < MinimumVersion {min_ver}")
                    await self._human_step_delay()

                # 2. Auth / Login (login_page.dart -> /API/auth)
                if self.config.step_auth:
                    record.current_step = "Auth"
                    self.on_user_update(record)
                    url_auth = build_dart_url(
                        self.config.base_url,
                        "/API/auth",
                        [("user_id", user.user_id), ("password", user.password)],
                    )
                    ok, data, _, err = await self._http_get_json(session, record.run_index, "auth", url_auth)
                    record.steps_trace.append(f"auth:{'OK' if ok else 'ERR'}")
                    if not ok or not data:
                        raise RuntimeError(f"Auth gagal: {err}")
                    results = data.get("data", {}).get("result", [])
                    if not isinstance(results, list) or not results or "UserID" not in results[0]:
                        raise RuntimeError("Auth gagal: User atau password salah (result kosong)")
                    row = results[0]
                    kode = str(row.get("Kode", kode) or "")
                    user_id = str(row.get("UserID", user.user_id) or user.user_id)
                    if user.sales == "Auto":
                        is_sales = str(row.get("Sales", "False")).lower() == "true"
                    record.kode = kode or "-"
                    record.is_sales = is_sales
                    self.on_user_update(record)
                    await self._human_step_delay()
                else:
                    if not kode:
                        kode = user_id
                    record.kode = kode
                    record.is_sales = is_sales

                if self._stop_event.is_set():
                    raise asyncio.CancelledError()

                # 3. Get Absen HomeScreen (getabsenusertglrange1008)
                if self.config.step_getabsen:
                    record.current_step = "getabsen1008"
                    self.on_user_update(record)
                    today_mmddyyyy = datetime.now().strftime("%m/%d/%Y")
                    url_getabsen = build_dart_url(
                        self.config.base_url,
                        "/API/getabsenusertglrange1008",
                        [("kode", kode), ("TanggalAwal", today_mmddyyyy)],
                    )
                    ok, data, _, err = await self._http_get_json(
                        session, record.run_index, "getabsen1008", url_getabsen
                    )
                    record.steps_trace.append(f"getabsen1008:{'OK' if ok else 'ERR'}")
                    if not ok:
                        raise RuntimeError(f"getabsenusertglrange1008 gagal: {err}")
                    if data:
                        rows = data.get("data", {}).get("result", [])
                        if isinstance(rows, list) and rows:
                            is_empty_1899 = any("1899" in str(r.get("Tanggal", "")) for r in rows if isinstance(r, dict))
                            if not is_empty_1899:
                                today_ddmmyyyy = datetime.now().strftime("%d/%m/%Y")
                                data_absen_hari_ini_count = sum(
                                    1 for r in rows if isinstance(r, dict) and str(r.get("Tanggal", "")) == today_ddmmyyyy
                                )

                # 4. Get Pesan Unread (home_screen.dart -> /api/getpesanunread)
                if self.config.step_getpesanunread:
                    record.current_step = "getpesanunread"
                    self.on_user_update(record)
                    url_pesan = build_dart_url(
                        self.config.base_url,
                        "/api/getpesanunread",
                        [("kode", kode)],
                    )
                    ok, _, _, _ = await self._http_get_json(session, record.run_index, "getpesanunread", url_pesan)
                    record.steps_trace.append(f"pesan:{'OK' if ok else 'ERR'}")

                # 5. Optional Monthly Absensi Tab (coba_api.dart / kunjungan_screen.dart)
                if self.config.step_getabsen_bulanan:
                    record.current_step = "getabsen_bulanan"
                    self.on_user_update(record)
                    now = datetime.now()
                    ep = "/API/getabsenuserperbulansales1017" if is_sales else "/API/getabsenuserperbulan1009"
                    url_bulanan = build_dart_url(
                        self.config.base_url,
                        ep,
                        [("Kode", kode), ("bulan", str(now.month)), ("tahun", str(now.year))],
                    )
                    ok, _, _, _ = await self._http_get_json(session, record.run_index, "getabsen_bulanan", url_bulanan)
                    record.steps_trace.append(f"bulanan:{'OK' if ok else 'ERR'}")

                if self.config.step_getabsen or self.config.step_getpesanunread or self.config.step_getabsen_bulanan:
                    await self._human_step_delay()

                if self._stop_event.is_set():
                    raise asyncio.CancelledError()

                # 6. Masuk (home_screen.dart -> /api/masuk1018)
                if self.config.step_masuk:
                    record.current_step = "masuk1018"
                    self.on_user_update(record)
                    url_masuk = build_dart_url(
                        self.config.base_url,
                        "/api/masuk1018",
                        [("UserID", user_id)],
                    )
                    ok, data, _, err = await self._http_get_json(session, record.run_index, "masuk1018", url_masuk)
                    record.steps_trace.append(f"masuk1018:{'OK' if ok else 'ERR'}")
                    if not ok or not data:
                        raise RuntimeError(f"masuk1018 gagal: {err}")
                    results = data.get("data", {}).get("result", [])
                    if not isinstance(results, list) or not results or "Kode" not in results[0]:
                        raise RuntimeError("masuk1018 gagal: Data Kode tidak ditemukan")
                    masuk_row = results[0]
                    if not user.kode and not self.config.step_auth and masuk_row.get("Kode"):
                        kode = str(masuk_row["Kode"])
                        record.kode = kode
                    jam_server = parse_server_datetime(str(masuk_row.get("Sekarang", "")))
                    jam_kerja = str(masuk_row.get("JamKerja", "") or "")
                    await self._human_step_delay()

                # 7. Sales isInCustomer check (home_screen_sales.dart -> /API/getabsenusertglrange)
                if is_sales and self.config.step_is_in_customer_sales:
                    record.current_step = "getabsen_sales_cek"
                    self.on_user_update(record)
                    tgl_str = jam_server.strftime("%m/%d/%Y")
                    url_cek = build_dart_url(
                        self.config.base_url,
                        "/API/getabsenusertglrange",
                        [("kode", kode), ("TanggalAwal", tgl_str), ("TanggalAkhir", tgl_str)],
                    )
                    ok, data, _, _ = await self._http_get_json(session, record.run_index, "getabsen_sales", url_cek)
                    record.steps_trace.append(f"cek_sales:{'OK' if ok else 'ERR'}")
                    if ok and data:
                        rows = data.get("data", {}).get("result", [])
                        if isinstance(rows, list) and len(rows) == 1:
                            is_in_sales = False
                        elif isinstance(rows, list) and len(rows) >= 2:
                            r1, r2 = rows[-1], rows[-2]
                            is_in_sales = (
                                r1.get("NamaCustomer") == r2.get("NamaCustomer")
                                and r1.get("Customer") == r2.get("Customer")
                            )
                        else:
                            is_in_sales = True

                # Resolve coordinates
                if user.latitude and user.longitude:
                    lat_str, lon_str = user.latitude, user.longitude
                elif (
                    self.config.coord_mode == "server_jitter"
                    and masuk_row.get("Latitude")
                    and masuk_row.get("Longitude")
                ):
                    try:
                        srv_lat = float(masuk_row["Latitude"])
                        srv_lon = float(masuk_row["Longitude"])
                        lat_str, lon_str = add_gps_jitter(srv_lat, srv_lon, max_meters=30.0)
                    except Exception:
                        lat_str = self.config.custom_latitude
                        lon_str = self.config.custom_longitude
                else:
                    lat_str = self.config.custom_latitude
                    lon_str = self.config.custom_longitude

                # Strict GPS & Time Lock Validation (optional)
                if self.config.strict_lock_validation and masuk_row:
                    if str(masuk_row.get("LockGPS", "False")).lower() == "true":
                        try:
                            dist = haversine_meters(
                                float(lat_str),
                                float(lon_str),
                                float(masuk_row.get("Latitude", 0)),
                                float(masuk_row.get("Longitude", 0)),
                            )
                            if dist >= 100.0:
                                raise RuntimeError(f"LockGPS aktif: jarak {dist:.1f}m >= 100m dari kantor")
                        except ValueError:
                            pass
                    if str(masuk_row.get("LockTime", "False")).lower() == "true":
                        valid_time, msg = _check_lock_time(masuk_row, jam_server, data_absen_hari_ini_count >= 1)
                        if not valid_time:
                            raise RuntimeError(f"LockTime aktif: {msg}")

                if self._stop_event.is_set():
                    raise asyncio.CancelledError()

                # 8. Submit Absen + Optional FTP Upload (check_page.dart / check_page_sales.dart)
                if self.config.step_absen or self.config.step_ftp_upload:
                    remote_filename = f"{kode}-{jam_server.strftime('%y%m%d%H%M%S')}.jpg"
                    alamat = user.alamat or self.config.default_alamat

                    nourut = 1
                    if is_sales and self.config.step_absen:
                        record.current_step = "getnourut"
                        self.on_user_update(record)
                        if is_in_sales:
                            url_no = build_dart_url(self.config.base_url, "/API/getnourutcekin")
                            ok_no, d_no, _, _ = await self._http_get_json(session, record.run_index, "getnourut", url_no)
                            if ok_no and d_no:
                                res_no = d_no.get("data", {}).get("result", [])
                                if res_no:
                                    try:
                                        nourut = int(res_no[0].get("NoUrut", 0)) + 1
                                    except Exception:
                                        nourut = 1
                        else:
                            url_no = build_dart_url(
                                self.config.base_url, "/API/getnourutcekout", [("Kode", kode)]
                            )
                            ok_no, d_no, _, _ = await self._http_get_json(session, record.run_index, "getnourut", url_no)
                            if ok_no and d_no:
                                res_no = d_no.get("data", {}).get("result", [])
                                if res_no:
                                    try:
                                        nourut = int(res_no[0].get("NoUrut", 1))
                                    except Exception:
                                        nourut = 1

                    record.current_step = "FTP + Absen" if (self.config.step_ftp_upload and self.config.step_absen) else (
                        "FTP Upload" if self.config.step_ftp_upload else "Submit Absen"
                    )
                    self.on_user_update(record)

                    tasks = []

                    async def _do_ftp() -> Tuple[bool, str]:
                        t_ftp = time.perf_counter()
                        try:
                            img_path = (
                                self.config.custom_image_path
                                if self.config.image_mode == "custom_file"
                                else ""
                            )
                            jpg_bytes = await asyncio.to_thread(
                                generate_attendance_jpeg, kode, user_id, jam_server, img_path
                            )
                            await upload_photo_ftp(
                                host=self.config.ftp_address,
                                port=int(self.config.ftp_port or 21),
                                user=self.config.ftp_user_upload,
                                password=self.config.ftp_password_upload,
                                remote_dir=self.config.ftp_remote_dir,
                                remote_filename=remote_filename,
                                jpeg_bytes=jpg_bytes,
                                timeout=self.config.ftp_timeout_seconds,
                            )
                            ms = (time.perf_counter() - t_ftp) * 1000.0
                            self._record_step("ftp_upload", True, ms)
                            self._log("INFO", f"[#{record.run_index}] FTP Upload {remote_filename} OK ({ms:.0f} ms)")
                            return True, "OK"
                        except Exception as fe:
                            ms = (time.perf_counter() - t_ftp) * 1000.0
                            self._record_step("ftp_upload", False, ms)
                            err_m = f"FTP Error: {fe}"
                            self._log("ERROR", f"[#{record.run_index}] {err_m} ({ms:.0f} ms)")
                            return False, err_m

                    async def _do_absen() -> Tuple[bool, str]:
                        tgl = jam_server.strftime("%m/%d/%Y")
                        jam_masuk = jam_server.strftime("%H:%M:%S")
                        if is_sales:
                            url_absen = build_dart_url(
                                self.config.base_url,
                                "/API/absensales1018",
                                [
                                    ("Tanggal", tgl),
                                    ("Kode", kode),
                                    ("JamMasuk", jam_masuk),
                                    ("Foto", remote_filename),
                                    ("latitude", lat_str),
                                    ("longitude", lon_str),
                                    ("Deskripsi", self.config.default_deskripsi),
                                    ("Alamat", alamat),
                                    ("Customer", self.config.default_customer_kode),
                                    ("NoUrut", str(nourut)),
                                    ("NamaCustomer", self.config.default_customer_nama),
                                    ("JamKerja", jam_kerja),
                                ],
                                encode_component_keys={"Deskripsi"},
                            )
                            ep_name = "absensales1018"
                        else:
                            url_absen = build_dart_url(
                                self.config.base_url,
                                "/API/absen1018",
                                [
                                    ("Tanggal", tgl),
                                    ("Kode", kode),
                                    ("JamMasuk", jam_masuk),
                                    ("Foto", remote_filename),
                                    ("latitude", lat_str),
                                    ("longitude", lon_str),
                                    ("Alamat", alamat),
                                    ("JamKerja", jam_kerja),
                                ],
                            )
                            ep_name = "absen1018"

                        ok_a, _, _, err_a = await self._http_get_json(session, record.run_index, ep_name, url_absen)
                        return ok_a, err_a

                    if self.config.step_ftp_upload:
                        tasks.append(_do_ftp())
                    if self.config.step_absen:
                        tasks.append(_do_absen())

                    results = await asyncio.gather(*tasks)
                    for ok_item, err_item in results:
                        if not ok_item:
                            raise RuntimeError(err_item)
                    record.steps_trace.append("submit:OK")

                record.status = "Success"
                record.current_step = "Selesai"
                record.detail = " -> ".join(record.steps_trace)
                self.success_runs += 1
            except asyncio.CancelledError:
                record.status = "Cancelled"
                record.current_step = "Cancelled"
                record.detail = "Dibatalkan oleh user"
            except Exception as exc:
                record.status = "Failed"
                record.current_step = "Error"
                record.detail = str(exc)
                self.failed_runs += 1
            finally:
                record.total_ms = (time.perf_counter() - run_t0) * 1000.0
                self.active_workers -= 1
                self.completed_runs += 1
                self.on_user_update(record)
                self._emit_stats()

    async def run(self) -> None:
        if not self.users:
            self._log("WARN", "Daftar user kosong!")
            return

        # Build execution plan based on user_run_mode
        planned_users: List[UserAccount] = []
        if self.config.user_run_mode == "target_total":
            total = max(1, int(self.config.target_total_runs))
            for i in range(total):
                planned_users.append(self.users[i % len(self.users)])
        else:
            planned_users = list(self.users)

        self.total_runs = len(planned_users)
        self.completed_runs = 0
        self.success_runs = 0
        self.failed_runs = 0
        self.active_workers = 0
        self.total_http_requests = 0
        self.step_metrics.clear()
        self.records = [
            UserRunRecord(run_index=i + 1, user_id=u.user_id, kode=u.kode or "-")
            for i, u in enumerate(planned_users)
        ]
        self.started_at = time.perf_counter()

        self.on_init_records(self.records)
        self._emit_stats()

        self._log(
            "INFO",
            f"Memulai simulasi: {self.total_runs} user | Max Parallel={self.config.max_concurrency} | "
            f"Launch Interval={self.config.launch_interval_ms} ms | Target={self.config.base_url}",
        )

        connector = _create_connector(
            limit=max(self.config.max_concurrency * 2, 100),
            force_close=True,
        )
        timeout = aiohttp.ClientTimeout(total=float(self.config.timeout_seconds))
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        sem = asyncio.Semaphore(max(1, int(self.config.max_concurrency)))

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            self._tasks = []
            interval_s = max(0, int(self.config.launch_interval_ms)) / 1000.0
            for idx, (rec, usr) in enumerate(zip(self.records, planned_users)):
                if self._stop_event.is_set():
                    break
                task = asyncio.create_task(self._run_single_user(sem, session, rec, usr))
                self._tasks.append(task)
                if interval_s > 0 and idx < len(planned_users) - 1:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=interval_s)
                        break
                    except asyncio.TimeoutError:
                        pass

            if self._stop_event.is_set():
                for t in self._tasks:
                    if not t.done():
                        t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)

            if self._stop_event.is_set():
                for rec in self.records:
                    if rec.status == "Queued":
                        rec.status = "Cancelled"
                        rec.current_step = "Cancelled"
                        rec.detail = "Dibatalkan sebelum dijalankan"
                        self.on_user_update(rec)

        self._emit_stats()
        self._log(
            "INFO",
            f"Simulasi selesai: Sukses={self.success_runs}, Gagal={self.failed_runs}, "
            f"Total HTTP={self.total_http_requests}, Waktu={time.perf_counter() - self.started_at:.2f}s",
        )
