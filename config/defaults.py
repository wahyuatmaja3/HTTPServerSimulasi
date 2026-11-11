"""Default configuration values matching d:\\projects\\sgabsensi_sjab\\config.json and Flutter source."""

DEFAULT_APP_VERSION = "1.0.33"
DEFAULT_USER_AGENT = "Dart/3.1 (dart:io)"
DEFAULT_SERVER_DISCOVERY_BASE = "http://absensi.software-gallery.com"
DEFAULT_SERVER_DISCOVERY_PASS = "Best`234"
DEFAULT_FTP_REMOTE_DIR = "/SJABPayroll/img"

DEFAULT_CONFIG = {
    "ip_address": "localhost",
    "ip_port": "56563",
    "ftp_address": "localhost",
    "ftp_port": "56562",
    "ftp_user_upload": "sjabpayroll",
    "ftp_password_upload": "12345",
    "ftp_remote_dir": DEFAULT_FTP_REMOTE_DIR,
    "app_version": DEFAULT_APP_VERSION,
    "user_agent": DEFAULT_USER_AGENT,
    "timeout_seconds": 10.0,
    "ftp_timeout_seconds": 30.0,
    # Simulation flow toggles (matching Flutter app sequence)
    "step_getversion": True,
    "step_auth": True,
    "step_getabsen": True,
    "step_getpesanunread": True,
    "step_getabsen_bulanan": False,
    "step_masuk": True,
    "step_is_in_customer_sales": True,
    "step_ftp_upload": False,
    "step_absen": True,
    "strict_lock_validation": False,
    # Location & payload defaults
    "coord_mode": "server_jitter",  # "server_jitter" | "custom"
    "custom_latitude": "-7.359569347527608",
    "custom_longitude": "112.59350851182998",
    "default_alamat": "Jl. Raya Simulasi No. 1, Sidoarjo, 61257",
    "default_deskripsi": "Simulasi Absensi",
    "default_customer_kode": "CUST001",
    "default_customer_nama": "CUSTOMER SIMULASI",
    "image_mode": "generated",  # "generated" | "custom_file"
    "custom_image_path": "",
    # Concurrency & speed controls
    "user_run_mode": "all_once",  # "all_once" | "target_total"
    "target_total_runs": 50,
    "max_concurrency": 10,
    "launch_interval_ms": 100,
    "step_delay_min_ms": 100,
    "step_delay_max_ms": 300,
    # User source (Karyawan DB vs manual list)
    "user_source": "karyawan_db",  # "karyawan_db" | "manual"
    "karyawan_db_path": "tables/Karyawan.DB",
    "karyawan_db_only_active": True,
    "http_server_ini_path": r"D:\SJABPayroll\HTTPServerDB.ini",
}

DEFAULT_USERS = [
    {
        "user_id": "user01",
        "password": "123",
        "kode": "1001",
        "sales": "Auto",
        "alamat": "",
        "latitude": "",
        "longitude": "",
    },
    {
        "user_id": "user02",
        "password": "123",
        "kode": "1002",
        "sales": "Auto",
        "alamat": "",
        "longitude": "",
        "latitude": "",
    },
]
