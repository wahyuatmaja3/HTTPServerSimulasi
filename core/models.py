from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class UserAccount:
    user_id: str
    password: str = ""
    kode: str = ""
    sales: str = "Auto"  # "Auto" | "True" | "False"
    alamat: str = ""
    latitude: str = ""
    longitude: str = ""

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "password": self.password,
            "kode": self.kode,
            "sales": self.sales,
            "alamat": self.alamat,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserAccount":
        return cls(
            user_id=str(data.get("user_id", "")).strip(),
            password=str(data.get("password", "")).strip(),
            kode=str(data.get("kode", "")).strip(),
            sales=str(data.get("sales", "Auto")).strip() or "Auto",
            alamat=str(data.get("alamat", "")).strip(),
            latitude=str(data.get("latitude", "")).strip(),
            longitude=str(data.get("longitude", "")).strip(),
        )


@dataclass
class SimConfig:
    ip_address: str = "localhost"
    ip_port: str = "56563"
    ftp_address: str = "localhost"
    ftp_port: str = "56562"
    ftp_user_upload: str = "sjabpayroll"
    ftp_password_upload: str = "12345"
    ftp_remote_dir: str = "/SJABPayroll/img"
    app_version: str = "1.0.33"
    user_agent: str = "Dart/3.1 (dart:io)"
    timeout_seconds: float = 10.0
    ftp_timeout_seconds: float = 30.0

    # Flow toggles matching sgabsensi_sjab
    step_getversion: bool = True
    step_auth: bool = True
    step_getabsen: bool = True
    step_getpesanunread: bool = True
    step_getabsen_bulanan: bool = False
    step_masuk: bool = True
    step_is_in_customer_sales: bool = True
    step_ftp_upload: bool = False
    step_absen: bool = True
    strict_lock_validation: bool = False

    # Location & payload settings
    coord_mode: str = "server_jitter"  # "server_jitter" | "custom"
    custom_latitude: str = "-7.359569347527608"
    custom_longitude: str = "112.59350851182998"
    default_alamat: str = "Jl. Raya Simulasi No. 1, Sidoarjo, 61257"
    default_deskripsi: str = "Simulasi Absensi"
    default_customer_kode: str = "CUST001"
    default_customer_nama: str = "CUSTOMER SIMULASI"
    image_mode: str = "generated"  # "generated" | "custom_file"
    custom_image_path: str = ""

    # Concurrency & speed controls
    user_run_mode: str = "all_once"  # "all_once" | "target_total"
    target_total_runs: int = 50
    max_concurrency: int = 10
    launch_interval_ms: int = 100
    step_delay_min_ms: int = 100
    step_delay_max_ms: int = 300

    # User source (Karyawan DB vs manual list)
    user_source: str = "karyawan_db"  # "karyawan_db" | "manual"
    karyawan_db_path: str = "tables/Karyawan.DB"
    karyawan_db_only_active: bool = True
    http_server_ini_path: str = r"D:\SJABPayroll\HTTPServerDB.ini"

    @property
    def base_url(self) -> str:
        host = self.ip_address.strip()
        port = self.ip_port.strip()
        if host.startswith("http://") or host.startswith("https://"):
            return f"{host}:{port}" if port and ":" not in host.split("://", 1)[1] else host
        return f"http://{host}:{port}" if port else f"http://{host}"

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict) -> "SimConfig":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class StepMetric:
    name: str
    success: int = 0
    failed: int = 0
    total_ms: float = 0.0

    @property
    def count(self) -> int:
        return self.success + self.failed

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


@dataclass
class UserRunRecord:
    run_index: int
    user_id: str
    kode: str = "-"
    is_sales: bool = False
    current_step: str = "Queued"
    status: str = "Queued"  # Queued | Running | Success | Failed | Cancelled
    total_ms: float = 0.0
    detail: str = ""
    steps_trace: List[str] = field(default_factory=list)
