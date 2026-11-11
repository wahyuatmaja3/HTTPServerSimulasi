import asyncio
import csv
import json
import os
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config.defaults import DEFAULT_CONFIG, DEFAULT_USERS
from core.karyawan_db import load_users_from_karyawan_db, resolve_karyawan_db_path
from core.models import SimConfig, UserAccount, UserRunRecord
from core.simulator import BulkAttendanceSimulator, test_server_connection
from ui.dialogs import BulkPasteUsersDialog, FetchServerKeyDialog, ServerRecommendationDialog


APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(APP_ROOT, "simulator_config.json")
USERS_FILE = os.path.join(APP_ROOT, "users.json")
TABLES_DIR = os.path.join(APP_ROOT, "tables")


class SimulatorWorker(QThread):
    """Runs the asyncio BulkAttendanceSimulator on a dedicated worker thread."""

    init_records_sig = pyqtSignal(list)
    user_update_sig = pyqtSignal(tuple)
    stats_update_sig = pyqtSignal(dict)
    log_sig = pyqtSignal(str, str)
    finished_sig = pyqtSignal()
    error_sig = pyqtSignal(str)

    def __init__(self, config: SimConfig, users: List[UserAccount]):
        super().__init__()
        self.config = config
        self.users = users
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sim: Optional[BulkAttendanceSimulator] = None
        self.final_records: List[UserRunRecord] = []
        self.final_stats: Dict[str, Any] = {}

    def stop(self) -> None:
        if self._loop and self._sim and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._sim.request_stop)
            except RuntimeError:
                pass

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        def _on_init(records: List[UserRunRecord]) -> None:
            snapshots = [
                (r.run_index, r.user_id, r.kode, r.is_sales, r.current_step, r.status, r.total_ms, r.detail)
                for r in records
            ]
            self.init_records_sig.emit(snapshots)

        def _on_user(r: UserRunRecord) -> None:
            self.user_update_sig.emit(
                (r.run_index, r.user_id, r.kode, r.is_sales, r.current_step, r.status, r.total_ms, r.detail)
            )

        def _on_stats(stats: Dict[str, Any]) -> None:
            self.final_stats = stats
            self.stats_update_sig.emit(stats)

        def _on_log(level: str, msg: str) -> None:
            self.log_sig.emit(level, msg)

        self._sim = BulkAttendanceSimulator(
            config=self.config,
            users=self.users,
            on_init_records=_on_init,
            on_log=_on_log,
            on_user_update=_on_user,
            on_stats_update=_on_stats,
        )
        try:
            loop.run_until_complete(self._sim.run())
            self.final_records = list(self._sim.records)
        except Exception as exc:
            self.error_sig.emit(str(exc))
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self.finished_sig.emit()


class TestConnectionWorker(QThread):
    success_sig = pyqtSignal(dict)
    error_sig = pyqtSignal(str)

    def __init__(self, config: SimConfig):
        super().__init__()
        self.config = config

    def run(self) -> None:
        try:
            res = asyncio.run(test_server_connection(self.config))
            self.success_sig.emit(res)
        except Exception as exc:
            self.error_sig.emit(str(exc))


class MetricCard(QFrame):
    def __init__(self, title: str, initial: str = "0", accent_color: str = "#f8fafc"):
        super().__init__()
        self.setObjectName("MetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        self.lbl_title = QLabel(title)
        self.lbl_title.setObjectName("MetricLabel")
        self.lbl_value = QLabel(initial)
        self.lbl_value.setObjectName("MetricValue")
        self.lbl_value.setStyleSheet(f"color: {accent_color}; font-size: 20px; font-weight: 700;")
        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_value)

    def set_value(self, text: str) -> None:
        self.lbl_value.setText(text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SG Absensi Bulk Simulator — Mimic Flutter (sgabsensi_sjab)")
        self.resize(1380, 860)

        self._config = self._load_config()
        self._http_server_ini_path: str = (
            self._config.http_server_ini_path or DEFAULT_CONFIG["http_server_ini_path"]
        )
        self._users: List[UserAccount] = []
        self._db_info: Dict[str, Any] = {}
        self._applying_config = False
        self._worker: Optional[SimulatorWorker] = None
        self._test_worker: Optional[TestConnectionWorker] = None

        self._last_records: List[UserRunRecord] = []
        self._last_stats: Dict[str, Any] = {}
        self._log_queue: Deque[Tuple[str, str]] = deque(maxlen=5000)
        self._all_logs: List[Tuple[str, str]] = []

        self._build_ui()
        self._apply_config_to_ui(self._config)
        self._refresh_users_for_source(silent=True)
        self._update_header_badges()

        # Timer for batched log rendering at ~10Hz so burst mode keeps UI fluid
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(100)
        self._log_timer.timeout.connect(self._flush_logs)
        self._log_timer.start()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_config(self) -> SimConfig:
        if os.path.isfile(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return SimConfig.from_dict(data)
            except Exception:
                pass
        return SimConfig.from_dict(DEFAULT_CONFIG)

    def _save_config(self, config: SimConfig) -> None:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config.to_dict(), f, indent=2)
        except Exception as exc:
            self._append_log("WARN", f"Gagal menyimpan config: {exc}")

    def _load_users(self) -> List[UserAccount]:
        if os.path.isfile(USERS_FILE):
            try:
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                users = [UserAccount.from_dict(u) for u in raw if isinstance(u, dict)]
                if users:
                    return users
            except Exception:
                pass
        return [UserAccount.from_dict(u) for u in DEFAULT_USERS]

    def _save_users(self, users: List[UserAccount]) -> None:
        try:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump([u.to_dict() for u in users], f, indent=2)
        except Exception as exc:
            self._append_log("WARN", f"Gagal menyimpan users: {exc}")

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(8)

        # 1. Header Bar
        header = QFrame()
        header.setObjectName("HeaderBar")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        lbl_title = QLabel("SG Absensi Bulk Simulator")
        lbl_title.setStyleSheet("font-size: 18px; font-weight: 700; color: #f8fafc;")
        self.lbl_subtitle = QLabel("Target: - | 0 User Terdaftar")
        self.lbl_subtitle.setObjectName("MutedText")
        title_col.addWidget(lbl_title)
        title_col.addWidget(self.lbl_subtitle)
        header_layout.addLayout(title_col)
        header_layout.addStretch()

        header_layout.addWidget(QLabel("Preset Kecepatan:"))
        btn_normal = QPushButton("Normal / Natural")
        btn_ramai = QPushButton("Ramai / Jam Masuk")
        btn_burst = QPushButton("Burst / Stress Test")
        btn_normal.clicked.connect(lambda: self._apply_speed_preset("normal"))
        btn_ramai.clicked.connect(lambda: self._apply_speed_preset("ramai"))
        btn_burst.clicked.connect(lambda: self._apply_speed_preset("burst"))
        header_layout.addWidget(btn_normal)
        header_layout.addWidget(btn_ramai)
        header_layout.addWidget(btn_burst)

        header_layout.addSpacing(10)
        self.btn_calc_server = QPushButton("Kalkulasi BDE / .ini")
        self.btn_calc_server.setObjectName("AccentBtn")
        self.btn_calc_server.setToolTip(
            "Baca jumlah user Karyawan.DB, setting BDE & HTTPServerDB.ini, lalu hitung rekomendasi agar 0% gagal"
        )
        self.btn_calc_server.clicked.connect(self._on_calculate_server_clicked)
        header_layout.addWidget(self.btn_calc_server)

        header_layout.addSpacing(10)
        self.btn_start = QPushButton("▶ Mulai Simulasi")
        self.btn_start.setObjectName("PrimaryBtn")
        self.btn_stop = QPushButton("■ Stop")
        self.btn_stop.setObjectName("DangerBtn")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._on_start_clicked)
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        header_layout.addWidget(self.btn_start)
        header_layout.addWidget(self.btn_stop)
        root_layout.addWidget(header)

        # Body Wrapper
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 4, 12, 12)
        body_layout.setSpacing(10)
        root_layout.addWidget(body, 1)

        # 2. KPI Cards + Progress Bar
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(10)
        self.card_progress = MetricCard("Progress", "0 / 0", "#f8fafc")
        self.card_success = MetricCard("Sukses", "0", "#34d399")
        self.card_failed = MetricCard("Gagal", "0", "#f87171")
        self.card_active = MetricCard("Worker Aktif", "0", "#38bdf8")
        self.card_rps = MetricCard("HTTP Req / Detik", "0.0", "#fbbf24")
        self.card_latency = MetricCard("Avg Durasi / User", "0 ms", "#c084fc")
        for c in (
            self.card_progress,
            self.card_success,
            self.card_failed,
            self.card_active,
            self.card_rps,
            self.card_latency,
        ):
            kpi_row.addWidget(c)
        body_layout.addLayout(kpi_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m selesai (%p%)")
        body_layout.addWidget(self.progress_bar)

        # 3. Main Splitter (Left Controls | Right Workspace Tabs)
        splitter = QSplitter(Qt.Horizontal)
        body_layout.addWidget(splitter, 1)

        # LEFT PANEL (Scrollable Controls)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setMinimumWidth(345)
        left_scroll.setMaximumWidth(420)
        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 6, 0)
        left_layout.setSpacing(10)

        # Group A: Kecepatan & Jumlah User
        grp_speed = QGroupBox("Kecepatan & Jumlah User")
        spd_layout = QVBoxLayout(grp_speed)
        spd_layout.setSpacing(8)

        self.rb_all_once = QRadioButton("Jalankan semua user di daftar (1x per user)")
        self.rb_target_total = QRadioButton("Target jumlah simulasi (round-robin user)")
        self.rb_all_once.toggled.connect(self._on_run_mode_changed)
        spd_layout.addWidget(self.rb_all_once)
        spd_layout.addWidget(self.rb_target_total)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target Jumlah:"))
        self.sp_target_total = QSpinBox()
        self.sp_target_total.setRange(1, 50000)
        self.sp_target_total.setSingleStep(10)
        target_row.addWidget(self.sp_target_total)
        spd_layout.addLayout(target_row)

        conc_lbl_row = QHBoxLayout()
        conc_lbl_row.addWidget(QLabel("User Bersamaan (Concurrency):"))
        self.sp_concurrency = QSpinBox()
        self.sp_concurrency.setRange(1, 500)
        conc_lbl_row.addWidget(self.sp_concurrency)
        spd_layout.addLayout(conc_lbl_row)

        self.sl_concurrency = QSlider(Qt.Horizontal)
        self.sl_concurrency.setRange(1, 500)
        self.sp_concurrency.valueChanged.connect(self.sl_concurrency.setValue)
        self.sl_concurrency.valueChanged.connect(self.sp_concurrency.setValue)
        spd_layout.addWidget(self.sl_concurrency)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("Jeda Start Antar User (ms):"))
        self.sp_launch_interval = QSpinBox()
        self.sp_launch_interval.setRange(0, 30000)
        self.sp_launch_interval.setSingleStep(25)
        self.sp_launch_interval.setToolTip("0 ms = semua worker diluncurkan serentak bersamaan")
        interval_row.addWidget(self.sp_launch_interval)
        spd_layout.addLayout(interval_row)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Jeda Antar Step Min/Max (ms):"))
        self.sp_step_min = QSpinBox()
        self.sp_step_min.setRange(0, 10000)
        self.sp_step_min.setSingleStep(50)
        self.sp_step_max = QSpinBox()
        self.sp_step_max.setRange(0, 10000)
        self.sp_step_max.setSingleStep(50)
        step_row.addWidget(self.sp_step_min)
        step_row.addWidget(QLabel("-"))
        step_row.addWidget(self.sp_step_max)
        spd_layout.addLayout(step_row)

        timeout_row = QHBoxLayout()
        timeout_row.addWidget(QLabel("HTTP Timeout (detik):"))
        self.sp_timeout = QDoubleSpinBox()
        self.sp_timeout.setRange(1.0, 120.0)
        self.sp_timeout.setSingleStep(1.0)
        timeout_row.addWidget(self.sp_timeout)
        spd_layout.addLayout(timeout_row)

        left_layout.addWidget(grp_speed)

        # Group B: Alur API Flutter (sgabsensi_sjab)
        grp_flow = QGroupBox("Alur Request (Sama Dengan Flutter)")
        flow_layout = QVBoxLayout(grp_flow)
        flow_layout.setSpacing(6)

        self.chk_getversion = QCheckBox("1. GET /API/GETVERSION (Cek versi)")
        self.chk_auth = QCheckBox("2. GET /API/auth (Login & ambil Kode)")
        self.chk_getabsen = QCheckBox("3. GET /API/getabsenusertglrange1008")
        self.chk_getpesan = QCheckBox("4. GET /api/getpesanunread (Inbox)")
        self.chk_bulanan = QCheckBox("5. GET getabsenuserperbulan (Tab Absensi)")
        self.chk_masuk = QCheckBox("6. GET /api/masuk1018 (Jam server & GPS)")
        self.chk_sales_cek = QCheckBox("7. GET getabsenusertglrange (Khusus Sales)")
        self.chk_ftp = QCheckBox("8a. Upload Foto JPEG ke FTP (Paralel)")
        self.chk_absen = QCheckBox("8b. GET /API/absen1018 / absensales1018")
        self.chk_strict = QCheckBox("Validasi LockGPS (<100m) & LockTime")
        self.chk_strict.setToolTip("Jika aktif dan di luar jam/radius dari server, simulasi dihentikan sebelum submit absen.")

        for chk in (
            self.chk_getversion,
            self.chk_auth,
            self.chk_getabsen,
            self.chk_getpesan,
            self.chk_bulanan,
            self.chk_masuk,
            self.chk_sales_cek,
            self.chk_ftp,
            self.chk_absen,
            self.chk_strict,
        ):
            flow_layout.addWidget(chk)

        left_layout.addWidget(grp_flow)
        left_layout.addStretch()
        left_scroll.setWidget(left_container)
        splitter.addWidget(left_scroll)

        # RIGHT PANEL (Workspace Tabs)
        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self._build_tab_monitor()
        self._build_tab_users()
        self._build_tab_config()
        self._build_tab_logs()

    def _build_tab_monitor(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)

        lbl_ep = QLabel("Ringkasan Per Endpoint")
        lbl_ep.setStyleSheet("font-weight: 700; color: #93c5fd;")
        layout.addWidget(lbl_ep)

        self.tbl_endpoints = QTableWidget(0, 5)
        self.tbl_endpoints.setHorizontalHeaderLabels(["Endpoint", "Total Req", "Sukses", "Gagal", "Avg Latency (ms)"])
        self.tbl_endpoints.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4):
            self.tbl_endpoints.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tbl_endpoints.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_endpoints.setAlternatingRowColors(True)
        self.tbl_endpoints.setMaximumHeight(220)
        layout.addWidget(self.tbl_endpoints)

        lbl_users = QLabel("Status Live Simulasi Per User")
        lbl_users.setStyleSheet("font-weight: 700; color: #93c5fd;")
        layout.addWidget(lbl_users)

        self.tbl_monitor = QTableWidget(0, 8)
        self.tbl_monitor.setHorizontalHeaderLabels(
            ["#", "UserID", "Kode", "Tipe", "Step Saat Ini", "Status", "Durasi (ms)", "Detail / Trace"]
        )
        self.tbl_monitor.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_monitor.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_monitor.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl_monitor.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_monitor.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_monitor.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.tbl_monitor.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.tbl_monitor.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.tbl_monitor.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_monitor.setAlternatingRowColors(True)
        self.tbl_monitor.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.tbl_monitor, 1)

        self.tabs.addTab(tab, "Live Monitor")

    def _build_tab_users(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        # Source selector: direct Karyawan DB vs manual list
        grp_src = QGroupBox("Sumber Data User")
        src_layout = QVBoxLayout(grp_src)
        src_layout.setSpacing(6)

        mode_row = QHBoxLayout()
        self.rb_src_db = QRadioButton("Baca langsung dari Karyawan DB (tables/karyawan.db)")
        self.rb_src_manual = QRadioButton("Daftar Manual (users.json / Edit / Import CSV)")
        self.rb_src_db.toggled.connect(self._on_user_source_toggled)
        mode_row.addWidget(self.rb_src_db)
        mode_row.addWidget(self.rb_src_manual)
        mode_row.addStretch()
        src_layout.addLayout(mode_row)

        db_row = QHBoxLayout()
        db_row.addWidget(QLabel("File DB:"))
        self.ed_db_path = QLineEdit()
        self.ed_db_path.setPlaceholderText("tables/Karyawan.DB")
        self.ed_db_path.editingFinished.connect(self._on_db_setting_changed)
        self.btn_browse_db = QPushButton("Browse...")
        self.btn_browse_db.clicked.connect(self._on_browse_db)
        self.chk_db_only_active = QCheckBox("Hanya Karyawan Aktif")
        self.chk_db_only_active.setToolTip("Jika dicentang, baris dengan kolom Aktif = False tidak ikut dimuat.")
        self.chk_db_only_active.toggled.connect(self._on_db_setting_changed)
        self.btn_reload_db = QPushButton("↻ Muat Ulang DB")
        self.btn_reload_db.setObjectName("AccentBtn")
        self.btn_reload_db.clicked.connect(lambda: self._reload_users_from_db(silent=False))

        db_row.addWidget(self.ed_db_path, 1)
        db_row.addWidget(self.btn_browse_db)
        db_row.addWidget(self.chk_db_only_active)
        db_row.addWidget(self.btn_reload_db)
        src_layout.addLayout(db_row)

        self.lbl_db_status = QLabel("")
        self.lbl_db_status.setObjectName("MutedText")
        self.lbl_db_status.setWordWrap(True)
        src_layout.addWidget(self.lbl_db_status)
        layout.addWidget(grp_src)

        toolbar = QHBoxLayout()
        self.btn_add_user = QPushButton("+ Tambah User")
        self.btn_del_user = QPushButton("Hapus Terpilih")
        self.btn_bulk_users = QPushButton("Bulk Paste / Generate...")
        self.btn_bulk_users.setObjectName("AccentBtn")
        self.btn_import_csv = QPushButton("Import CSV")
        self.btn_export_csv = QPushButton("Export CSV")
        self.btn_save_users = QPushButton("Simpan Daftar User")

        self.btn_add_user.clicked.connect(self._on_add_user_row)
        self.btn_del_user.clicked.connect(self._on_delete_user_rows)
        self.btn_bulk_users.clicked.connect(self._on_bulk_users_dialog)
        self.btn_import_csv.clicked.connect(self._on_import_users_csv)
        self.btn_export_csv.clicked.connect(self._on_export_users_csv)
        self.btn_save_users.clicked.connect(self._on_save_users_clicked)

        for b in (
            self.btn_add_user,
            self.btn_del_user,
            self.btn_bulk_users,
            self.btn_import_csv,
            self.btn_export_csv,
        ):
            toolbar.addWidget(b)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_save_users)
        layout.addLayout(toolbar)

        hint = QLabel(
            "Tips: Pada mode DB, simulator membaca kolom UserAPK/UserID dan PasswordApk langsung dari file DB "
            "setiap kali aplikasi dibuka dan sebelum simulasi dimulai (read-only)."
        )
        hint.setObjectName("MutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tbl_users = QTableWidget(0, 7)
        self.tbl_users.setHorizontalHeaderLabels(
            ["UserID *", "Password", "Kode", "Sales", "Alamat (Opsional)", "Latitude (Ops)", "Longitude (Ops)"]
        )
        self.tbl_users.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tbl_users.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.tbl_users.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl_users.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_users.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_users.horizontalHeader().setSectionResizeMode(5, QHeaderView.Interactive)
        self.tbl_users.horizontalHeader().setSectionResizeMode(6, QHeaderView.Interactive)
        self.tbl_users.setAlternatingRowColors(True)
        self.tbl_users.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.tbl_users, 1)

        self.tab_users_idx = self.tabs.addTab(tab, "Daftar User")

    def _build_tab_config(self) -> None:
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)

        # HTTP Server Group
        grp_http = QGroupBox("Server API HTTP")
        f_http = QGridLayout(grp_http)
        self.ed_ip = QLineEdit()
        self.ed_port = QLineEdit()
        self.ed_ip.textEdited.connect(self._update_header_badges)
        self.ed_port.textEdited.connect(self._update_header_badges)
        self.ed_user_agent = QLineEdit()
        self.ed_app_version = QLineEdit()

        btn_server_key = QPushButton("Ambil via Server Key...")
        btn_server_key.setObjectName("AccentBtn")
        btn_server_key.clicked.connect(self._on_fetch_server_key)
        self.btn_test_conn = QPushButton("Test Koneksi")
        self.btn_test_conn.clicked.connect(self._on_test_connection)

        f_http.addWidget(QLabel("IP / Domain:"), 0, 0)
        f_http.addWidget(self.ed_ip, 0, 1)
        f_http.addWidget(QLabel("Port:"), 0, 2)
        f_http.addWidget(self.ed_port, 0, 3)

        f_http.addWidget(QLabel("User-Agent:"), 1, 0)
        f_http.addWidget(self.ed_user_agent, 1, 1)
        f_http.addWidget(QLabel("App Version:"), 1, 2)
        f_http.addWidget(self.ed_app_version, 1, 3)

        btn_row_http = QHBoxLayout()
        btn_row_http.addWidget(btn_server_key)
        btn_row_http.addWidget(self.btn_test_conn)
        btn_row_http.addStretch()
        f_http.addLayout(btn_row_http, 2, 0, 1, 4)
        layout.addWidget(grp_http)

        # FTP Server Group
        grp_ftp = QGroupBox("Server FTP Upload Foto")
        f_ftp = QGridLayout(grp_ftp)
        self.ed_ftp_host = QLineEdit()
        self.ed_ftp_port = QLineEdit()
        self.ed_ftp_user = QLineEdit()
        self.ed_ftp_pass = QLineEdit()
        self.ed_ftp_pass.setEchoMode(QLineEdit.Password)
        self.ed_ftp_dir = QLineEdit()
        self.sp_ftp_timeout = QDoubleSpinBox()
        self.sp_ftp_timeout.setRange(1.0, 180.0)

        f_ftp.addWidget(QLabel("FTP Host:"), 0, 0)
        f_ftp.addWidget(self.ed_ftp_host, 0, 1)
        f_ftp.addWidget(QLabel("FTP Port:"), 0, 2)
        f_ftp.addWidget(self.ed_ftp_port, 0, 3)

        f_ftp.addWidget(QLabel("FTP User Upload:"), 1, 0)
        f_ftp.addWidget(self.ed_ftp_user, 1, 1)
        f_ftp.addWidget(QLabel("FTP Pass Upload:"), 1, 2)
        f_ftp.addWidget(self.ed_ftp_pass, 1, 3)

        f_ftp.addWidget(QLabel("Remote Directory:"), 2, 0)
        f_ftp.addWidget(self.ed_ftp_dir, 2, 1)
        f_ftp.addWidget(QLabel("FTP Timeout (s):"), 2, 2)
        f_ftp.addWidget(self.sp_ftp_timeout, 2, 3)
        layout.addWidget(grp_ftp)

        # Payload & Location Group
        grp_pay = QGroupBox("Payload Absensi, Lokasi GPS & Foto")
        f_pay = QFormLayout(grp_pay)

        self.cb_coord_mode = QComboBox()
        self.cb_coord_mode.addItem("Ikuti Koordinat Server (/api/masuk1018) + Jitter GPS (~30m)", "server_jitter")
        self.cb_coord_mode.addItem("Gunakan Koordinat Kustom Default di bawah", "custom")

        coord_custom_row = QHBoxLayout()
        self.ed_custom_lat = QLineEdit()
        self.ed_custom_lon = QLineEdit()
        coord_custom_row.addWidget(QLabel("Lat:"))
        coord_custom_row.addWidget(self.ed_custom_lat)
        coord_custom_row.addWidget(QLabel("Lon:"))
        coord_custom_row.addWidget(self.ed_custom_lon)

        self.ed_default_alamat = QLineEdit()
        self.cb_image_mode = QComboBox()
        self.cb_image_mode.addItem("Auto Generate JPEG 480x640 (Pillow, identik TakePicture)", "generated")
        self.cb_image_mode.addItem("Gunakan File Gambar Kustom di bawah", "custom_file")

        img_row = QHBoxLayout()
        self.ed_custom_img = QLineEdit()
        btn_browse_img = QPushButton("Browse...")
        btn_browse_img.clicked.connect(self._on_browse_image)
        img_row.addWidget(self.ed_custom_img)
        img_row.addWidget(btn_browse_img)

        self.ed_deskripsi = QLineEdit()
        self.ed_cust_kode = QLineEdit()
        self.ed_cust_nama = QLineEdit()

        f_pay.addRow("Mode Koordinat:", self.cb_coord_mode)
        f_pay.addRow("Koordinat Kustom:", coord_custom_row)
        f_pay.addRow("Default Alamat:", self.ed_default_alamat)
        f_pay.addRow("Mode Foto Upload:", self.cb_image_mode)
        f_pay.addRow("File Gambar Kustom:", img_row)
        f_pay.addRow("Sales Deskripsi:", self.ed_deskripsi)
        f_pay.addRow("Sales Kode Customer:", self.ed_cust_kode)
        f_pay.addRow("Sales Nama Customer:", self.ed_cust_nama)
        layout.addWidget(grp_pay)

        action_row = QHBoxLayout()
        btn_reset = QPushButton("Reset ke Default")
        btn_save_cfg = QPushButton("Simpan Konfigurasi")
        btn_save_cfg.setObjectName("PrimaryBtn")
        btn_reset.clicked.connect(self._on_reset_config)
        btn_save_cfg.clicked.connect(self._on_save_config_clicked)
        action_row.addWidget(btn_reset)
        action_row.addStretch()
        action_row.addWidget(btn_save_cfg)
        layout.addLayout(action_row)
        layout.addStretch()

        scroll.setWidget(content)
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addWidget(scroll)
        self.tabs.addTab(tab, "Server & Payload")

    def _build_tab_logs(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Filter:"))
        self.cb_log_filter = QComboBox()
        self.cb_log_filter.addItems(["Semua", "Error Saja"])
        self.cb_log_filter.currentIndexChanged.connect(self._rebuild_log_view)
        self.chk_autoscroll = QCheckBox("Auto-scroll")
        self.chk_autoscroll.setChecked(True)
        btn_clear = QPushButton("Clear Logs")
        btn_clear.clicked.connect(self._on_clear_logs)
        btn_exp_csv = QPushButton("Export Hasil (CSV)")
        btn_exp_json = QPushButton("Export Summary (JSON)")
        btn_exp_csv.clicked.connect(self._on_export_results_csv)
        btn_exp_json.clicked.connect(self._on_export_summary_json)

        toolbar.addWidget(self.cb_log_filter)
        toolbar.addWidget(self.chk_autoscroll)
        toolbar.addWidget(btn_clear)
        toolbar.addStretch()
        toolbar.addWidget(btn_exp_csv)
        toolbar.addWidget(btn_exp_json)
        layout.addLayout(toolbar)

        self.txt_logs = QPlainTextEdit()
        self.txt_logs.setReadOnly(True)
        self.txt_logs.setMaximumBlockCount(3000)
        mono = QFont("Consolas", 10)
        self.txt_logs.setFont(mono)
        layout.addWidget(self.txt_logs, 1)

        self.tabs.addTab(tab, "Live Logs & Export")

    # ------------------------------------------------------------------
    # Config & UI Sync Helpers
    # ------------------------------------------------------------------
    def _apply_config_to_ui(self, cfg: SimConfig) -> None:
        self._applying_config = True
        try:
            self.rb_all_once.setChecked(cfg.user_run_mode != "target_total")
            self.rb_target_total.setChecked(cfg.user_run_mode == "target_total")
            self.sp_target_total.setValue(int(cfg.target_total_runs))
            self._on_run_mode_changed()

            self.sp_concurrency.setValue(int(cfg.max_concurrency))
            self.sp_launch_interval.setValue(int(cfg.launch_interval_ms))
            self.sp_step_min.setValue(int(cfg.step_delay_min_ms))
            self.sp_step_max.setValue(int(cfg.step_delay_max_ms))
            self.sp_timeout.setValue(float(cfg.timeout_seconds))

            self.chk_getversion.setChecked(bool(cfg.step_getversion))
            self.chk_auth.setChecked(bool(cfg.step_auth))
            self.chk_getabsen.setChecked(bool(cfg.step_getabsen))
            self.chk_getpesan.setChecked(bool(cfg.step_getpesanunread))
            self.chk_bulanan.setChecked(bool(cfg.step_getabsen_bulanan))
            self.chk_masuk.setChecked(bool(cfg.step_masuk))
            self.chk_sales_cek.setChecked(bool(cfg.step_is_in_customer_sales))
            self.chk_ftp.setChecked(bool(cfg.step_ftp_upload))
            self.chk_absen.setChecked(bool(cfg.step_absen))
            self.chk_strict.setChecked(bool(cfg.strict_lock_validation))

            self.ed_ip.setText(cfg.ip_address)
            self.ed_port.setText(cfg.ip_port)
            self.ed_user_agent.setText(cfg.user_agent)
            self.ed_app_version.setText(cfg.app_version)

            self.ed_ftp_host.setText(cfg.ftp_address)
            self.ed_ftp_port.setText(cfg.ftp_port)
            self.ed_ftp_user.setText(cfg.ftp_user_upload)
            self.ed_ftp_pass.setText(cfg.ftp_password_upload)
            self.ed_ftp_dir.setText(cfg.ftp_remote_dir)
            self.sp_ftp_timeout.setValue(float(cfg.ftp_timeout_seconds))

            idx_coord = self.cb_coord_mode.findData(cfg.coord_mode)
            self.cb_coord_mode.setCurrentIndex(max(0, idx_coord))
            self.ed_custom_lat.setText(cfg.custom_latitude)
            self.ed_custom_lon.setText(cfg.custom_longitude)
            self.ed_default_alamat.setText(cfg.default_alamat)

            idx_img = self.cb_image_mode.findData(cfg.image_mode)
            self.cb_image_mode.setCurrentIndex(max(0, idx_img))
            self.ed_custom_img.setText(cfg.custom_image_path)

            self.ed_deskripsi.setText(cfg.default_deskripsi)
            self.ed_cust_kode.setText(cfg.default_customer_kode)
            self.ed_cust_nama.setText(cfg.default_customer_nama)

            is_db = (cfg.user_source or "karyawan_db") == "karyawan_db"
            self.rb_src_db.setChecked(is_db)
            self.rb_src_manual.setChecked(not is_db)
            self.ed_db_path.setText(cfg.karyawan_db_path or "tables/Karyawan.DB")
            self.chk_db_only_active.setChecked(bool(cfg.karyawan_db_only_active))
            self._http_server_ini_path = cfg.http_server_ini_path or DEFAULT_CONFIG["http_server_ini_path"]
        finally:
            self._applying_config = False

    def _collect_config_from_ui(self) -> SimConfig:
        step_min = self.sp_step_min.value()
        step_max = max(step_min, self.sp_step_max.value())
        return SimConfig(
            ip_address=self.ed_ip.text().strip() or DEFAULT_CONFIG["ip_address"],
            ip_port=self.ed_port.text().strip(),
            ftp_address=self.ed_ftp_host.text().strip(),
            ftp_port=self.ed_ftp_port.text().strip() or "21",
            ftp_user_upload=self.ed_ftp_user.text().strip(),
            ftp_password_upload=self.ed_ftp_pass.text(),
            ftp_remote_dir=self.ed_ftp_dir.text().strip(),
            app_version=self.ed_app_version.text().strip() or DEFAULT_CONFIG["app_version"],
            user_agent=self.ed_user_agent.text().strip() or DEFAULT_CONFIG["user_agent"],
            timeout_seconds=self.sp_timeout.value(),
            ftp_timeout_seconds=self.sp_ftp_timeout.value(),
            step_getversion=self.chk_getversion.isChecked(),
            step_auth=self.chk_auth.isChecked(),
            step_getabsen=self.chk_getabsen.isChecked(),
            step_getpesanunread=self.chk_getpesan.isChecked(),
            step_getabsen_bulanan=self.chk_bulanan.isChecked(),
            step_masuk=self.chk_masuk.isChecked(),
            step_is_in_customer_sales=self.chk_sales_cek.isChecked(),
            step_ftp_upload=self.chk_ftp.isChecked(),
            step_absen=self.chk_absen.isChecked(),
            strict_lock_validation=self.chk_strict.isChecked(),
            coord_mode=str(self.cb_coord_mode.currentData() or "server_jitter"),
            custom_latitude=self.ed_custom_lat.text().strip(),
            custom_longitude=self.ed_custom_lon.text().strip(),
            default_alamat=self.ed_default_alamat.text().strip(),
            default_deskripsi=self.ed_deskripsi.text().strip(),
            default_customer_kode=self.ed_cust_kode.text().strip(),
            default_customer_nama=self.ed_cust_nama.text().strip(),
            image_mode=str(self.cb_image_mode.currentData() or "generated"),
            custom_image_path=self.ed_custom_img.text().strip(),
            user_run_mode="target_total" if self.rb_target_total.isChecked() else "all_once",
            target_total_runs=self.sp_target_total.value(),
            max_concurrency=self.sp_concurrency.value(),
            launch_interval_ms=self.sp_launch_interval.value(),
            step_delay_min_ms=step_min,
            step_delay_max_ms=step_max,
            user_source="karyawan_db" if self.rb_src_db.isChecked() else "manual",
            karyawan_db_path=self.ed_db_path.text().strip() or DEFAULT_CONFIG["karyawan_db_path"],
            karyawan_db_only_active=self.chk_db_only_active.isChecked(),
            http_server_ini_path=self._http_server_ini_path or DEFAULT_CONFIG["http_server_ini_path"],
        )

    def _on_run_mode_changed(self) -> None:
        self.sp_target_total.setEnabled(self.rb_target_total.isChecked())

    def _apply_speed_preset(self, preset: str) -> None:
        if preset == "normal":
            self.sp_concurrency.setValue(5)
            self.sp_launch_interval.setValue(400)
            self.sp_step_min.setValue(300)
            self.sp_step_max.setValue(900)
        elif preset == "ramai":
            self.sp_concurrency.setValue(30)
            self.sp_launch_interval.setValue(50)
            self.sp_step_min.setValue(100)
            self.sp_step_max.setValue(300)
        elif preset == "burst":
            self.sp_concurrency.setValue(150)
            self.sp_launch_interval.setValue(0)
            self.sp_step_min.setValue(0)
            self.sp_step_max.setValue(0)

    def _update_header_badges(self) -> None:
        cfg = self._collect_config_from_ui()
        users_cnt = self.tbl_users.rowCount()
        if cfg.user_source == "karyawan_db":
            db_name = os.path.basename(resolve_karyawan_db_path(cfg.karyawan_db_path, APP_ROOT, TABLES_DIR))
            src_txt = f"DB: {db_name}"
        else:
            src_txt = "Manual"
        self.lbl_subtitle.setText(f"Target Server: {cfg.base_url}  |  {users_cnt} User ({src_txt})")
        self.tabs.setTabText(self.tab_users_idx, f"Daftar User ({users_cnt})")

    # ------------------------------------------------------------------
    # Users Source & Table Helpers
    # ------------------------------------------------------------------
    def _sync_user_source_controls(self) -> None:
        is_db = self.rb_src_db.isChecked()
        for w in (self.ed_db_path, self.btn_browse_db, self.chk_db_only_active, self.btn_reload_db):
            w.setEnabled(is_db)
        for b in (
            self.btn_add_user,
            self.btn_del_user,
            self.btn_bulk_users,
            self.btn_import_csv,
            self.btn_save_users,
        ):
            b.setEnabled(not is_db)
        if is_db:
            self.tbl_users.setEditTriggers(QAbstractItemView.NoEditTriggers)
        else:
            self.tbl_users.setEditTriggers(
                QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed
            )

    def _refresh_users_for_source(self, silent: bool = True) -> bool:
        self._sync_user_source_controls()
        if self.rb_src_db.isChecked():
            return self._reload_users_from_db(silent=silent)
        users = self._load_users()
        self._users = users
        self._db_info = {}
        self._populate_users_table(users)
        self.lbl_db_status.setStyleSheet("color: #94a3b8;")
        self.lbl_db_status.setText(f"Mode Manual aktif — {len(users)} user dimuat dari users.json (dapat diedit).")
        return True

    def _reload_users_from_db(self, silent: bool = False) -> bool:
        cfg_path = self.ed_db_path.text().strip()
        resolved = resolve_karyawan_db_path(cfg_path, APP_ROOT, TABLES_DIR)
        only_active = self.chk_db_only_active.isChecked()
        try:
            users, info = load_users_from_karyawan_db(resolved, only_active=only_active)
            self._users = users
            self._db_info = info
            self._populate_users_table(users)

            skipped_txt = []
            if info.get("skipped_inactive"):
                skipped_txt.append(f"{info['skipped_inactive']} non-aktif dilewati")
            if info.get("skipped_empty_user"):
                skipped_txt.append(f"{info['skipped_empty_user']} user kosong dilewati")
            extra = f" ({', '.join(skipped_txt)})" if skipped_txt else ""
            msg = (
                f"✓ Dibaca langsung dari {os.path.basename(resolved)} ({info['format']}) | "
                f"{info['loaded_count']} user dimuat dari {info['total_rows']} baris | "
                f"Kolom: {info['user_col']} & {info['password_col']}{extra}"
            )
            self.lbl_db_status.setStyleSheet("color: #34d399;")
            self.lbl_db_status.setText(msg)
            self._append_log("INFO", f"DB Karyawan dimuat: {resolved} ({info['loaded_count']} user)")
            return True
        except Exception as exc:
            self._users = []
            self._db_info = {}
            self._populate_users_table([])
            self.lbl_db_status.setStyleSheet("color: #f87171;")
            self.lbl_db_status.setText(f"✗ Gagal membaca DB ({resolved}): {exc}")
            self._append_log("WARN", f"Gagal membaca DB Karyawan ({resolved}): {exc}")
            if not silent:
                QMessageBox.critical(self, "Gagal Membaca DB Karyawan", f"{resolved}\n\n{exc}")
            return False

    def _on_user_source_toggled(self, _checked: bool) -> None:
        if self._applying_config:
            return
        self._refresh_users_for_source(silent=False)
        self._save_config(self._collect_config_from_ui())

    def _on_db_setting_changed(self, *_args) -> None:
        if self._applying_config or not self.rb_src_db.isChecked():
            return
        self._reload_users_from_db(silent=True)
        self._save_config(self._collect_config_from_ui())

    def _on_browse_db(self) -> None:
        start_dir = TABLES_DIR if os.path.isdir(TABLES_DIR) else APP_ROOT
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Pilih File Database Karyawan",
            start_dir,
            "Database Files (*.db *.DB);;All Files (*)",
        )
        if not path:
            return
        try:
            rel = os.path.relpath(path, APP_ROOT)
            if not rel.startswith(".."):
                path = rel.replace("\\", "/")
        except ValueError:
            pass
        self.ed_db_path.setText(path)
        self._reload_users_from_db(silent=False)
        self._save_config(self._collect_config_from_ui())

    def _insert_user_row(self, u: UserAccount) -> None:
        row = self.tbl_users.rowCount()
        self.tbl_users.insertRow(row)
        self.tbl_users.setItem(row, 0, QTableWidgetItem(u.user_id))
        self.tbl_users.setItem(row, 1, QTableWidgetItem(u.password))
        self.tbl_users.setItem(row, 2, QTableWidgetItem(u.kode))

        cb = QComboBox()
        cb.addItems(["Auto", "False", "True"])
        idx = cb.findText(u.sales if u.sales in ("Auto", "False", "True") else "Auto")
        cb.setCurrentIndex(max(0, idx))
        cb.setEnabled(not self.rb_src_db.isChecked())
        self.tbl_users.setCellWidget(row, 3, cb)

        self.tbl_users.setItem(row, 4, QTableWidgetItem(u.alamat))
        self.tbl_users.setItem(row, 5, QTableWidgetItem(u.latitude))
        self.tbl_users.setItem(row, 6, QTableWidgetItem(u.longitude))

    def _populate_users_table(self, users: List[UserAccount]) -> None:
        self.tbl_users.setUpdatesEnabled(False)
        self.tbl_users.setRowCount(0)
        for u in users:
            self._insert_user_row(u)
        self.tbl_users.setUpdatesEnabled(True)
        self._update_header_badges()

    def _collect_users_from_table(self) -> List[UserAccount]:
        users: List[UserAccount] = []
        for r in range(self.tbl_users.rowCount()):
            uid_item = self.tbl_users.item(r, 0)
            uid = uid_item.text().strip() if uid_item else ""
            if not uid:
                continue
            pwd = self.tbl_users.item(r, 1).text().strip() if self.tbl_users.item(r, 1) else ""
            kode = self.tbl_users.item(r, 2).text().strip() if self.tbl_users.item(r, 2) else ""
            cb = self.tbl_users.cellWidget(r, 3)
            sales = cb.currentText() if isinstance(cb, QComboBox) else "Auto"
            alamat = self.tbl_users.item(r, 4).text().strip() if self.tbl_users.item(r, 4) else ""
            lat = self.tbl_users.item(r, 5).text().strip() if self.tbl_users.item(r, 5) else ""
            lon = self.tbl_users.item(r, 6).text().strip() if self.tbl_users.item(r, 6) else ""
            users.append(
                UserAccount(
                    user_id=uid,
                    password=pwd,
                    kode=kode,
                    sales=sales,
                    alamat=alamat,
                    latitude=lat,
                    longitude=lon,
                )
            )
        return users

    def _on_add_user_row(self) -> None:
        next_num = self.tbl_users.rowCount() + 1
        self._insert_user_row(UserAccount(user_id=f"user{next_num:02d}", password="123", sales="Auto"))
        self._update_header_badges()

    def _on_delete_user_rows(self) -> None:
        selected = sorted({idx.row() for idx in self.tbl_users.selectedIndexes()}, reverse=True)
        for r in selected:
            self.tbl_users.removeRow(r)
        self._update_header_badges()

    def _on_bulk_users_dialog(self) -> None:
        dlg = BulkPasteUsersDialog(self)
        if dlg.exec_() == dlg.Accepted:
            new_users = dlg.get_users()
            if dlg.should_replace():
                self._populate_users_table(new_users)
            else:
                self.tbl_users.setUpdatesEnabled(False)
                for u in new_users:
                    self._insert_user_row(u)
                self.tbl_users.setUpdatesEnabled(True)
                self._update_header_badges()

    def _on_import_users_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import User CSV", APP_ROOT, "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        imported: List[UserAccount] = []
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                rows = list(reader)
            if not rows:
                return
            header_lower = [c.strip().lower() for c in rows[0]]
            start_idx = 1 if ("user_id" in header_lower or "userid" in header_lower) else 0
            for row in rows[start_idx:]:
                if not row or not row[0].strip():
                    continue
                imported.append(
                    UserAccount(
                        user_id=row[0].strip(),
                        password=row[1].strip() if len(row) > 1 else "",
                        kode=row[2].strip() if len(row) > 2 else "",
                        sales=row[3].strip() if len(row) > 3 and row[3].strip() in ("Auto", "True", "False") else "Auto",
                        alamat=row[4].strip() if len(row) > 4 else "",
                        latitude=row[5].strip() if len(row) > 5 else "",
                        longitude=row[6].strip() if len(row) > 6 else "",
                    )
                )
            if imported:
                self._populate_users_table(imported)
                QMessageBox.information(self, "Import Berhasil", f"Berhasil mengimport {len(imported)} user.")
        except Exception as exc:
            QMessageBox.critical(self, "Gagal Import CSV", str(exc))

    def _on_export_users_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export User CSV", os.path.join(APP_ROOT, "users_export.csv"), "CSV Files (*.csv)")
        if not path:
            return
        users = self._collect_users_from_table()
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["user_id", "password", "kode", "sales", "alamat", "latitude", "longitude"])
                for u in users:
                    w.writerow([u.user_id, u.password, u.kode, u.sales, u.alamat, u.latitude, u.longitude])
            QMessageBox.information(self, "Export Berhasil", f"Daftar user tersimpan ke:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Gagal Export CSV", str(exc))

    def _on_save_users_clicked(self) -> None:
        users = self._collect_users_from_table()
        self._users = users
        self._save_users(users)
        self._update_header_badges()
        QMessageBox.information(self, "Tersimpan", f"{len(users)} user disimpan ke users.json.")

    # ------------------------------------------------------------------
    # Server & Config Actions
    # ------------------------------------------------------------------
    def _on_fetch_server_key(self) -> None:
        dlg = FetchServerKeyDialog(self)
        if dlg.exec_() == dlg.Accepted and dlg.fetched_server:
            s = dlg.fetched_server
            self.ed_ip.setText(str(s.get("IPName", "") or ""))
            self.ed_port.setText(str(s.get("IPPort", "") or ""))
            self.ed_ftp_host.setText(str(s.get("FTPName", "") or ""))
            self.ed_ftp_port.setText(str(s.get("FTPPort", "") or "21"))
            self.ed_ftp_user.setText(str(s.get("FTPUserUp", "") or ""))
            self.ed_ftp_pass.setText(str(s.get("FTPPassUp", "") or ""))
            self._update_header_badges()
            QMessageBox.information(
                self,
                "Server Config Diperoleh",
                f"HTTP: {self.ed_ip.text()}:{self.ed_port.text()}\n"
                f"FTP: {self.ed_ftp_host.text()}:{self.ed_ftp_port.text()}",
            )

    def _on_test_connection(self) -> None:
        cfg = self._collect_config_from_ui()
        self.btn_test_conn.setEnabled(False)
        self.btn_test_conn.setText("Menghubungi...")
        self._test_worker = TestConnectionWorker(cfg)
        self._test_worker.success_sig.connect(self._on_test_conn_ok)
        self._test_worker.error_sig.connect(self._on_test_conn_err)
        self._test_worker.start()

    def _on_test_conn_ok(self, res: dict) -> None:
        self.btn_test_conn.setEnabled(True)
        self.btn_test_conn.setText("Test Koneksi")
        ver_list = (res.get("version_data") or {}).get("data", {}).get("result", [])
        min_ver = ver_list[0].get("MinimumVersion", "-") if isinstance(ver_list, list) and ver_list else "-"
        QMessageBox.information(
            self,
            "Koneksi Sukses",
            f"Target: {res.get('base_url')}\n"
            f"/API/GETVERSION: {res.get('version_ms')} ms (MinVersion: {min_ver})\n"
            f"/API/getsetting: {res.get('setting_ms')} ms",
        )

    def _on_test_conn_err(self, err: str) -> None:
        self.btn_test_conn.setEnabled(True)
        self.btn_test_conn.setText("Test Koneksi")
        QMessageBox.critical(self, "Koneksi Gagal", err)

    def _on_browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Pilih Foto Absensi", APP_ROOT, "Image Files (*.jpg *.jpeg *.png *.bmp)"
        )
        if path:
            self.ed_custom_img.setText(path)
            self.cb_image_mode.setCurrentIndex(1)

    def _on_save_config_clicked(self) -> None:
        self._config = self._collect_config_from_ui()
        self._save_config(self._config)
        self._update_header_badges()
        QMessageBox.information(self, "Konfigurasi Disimpan", "Pengaturan disimpan ke simulator_config.json.")

    def _on_reset_config(self) -> None:
        self._config = SimConfig.from_dict(DEFAULT_CONFIG)
        self._apply_config_to_ui(self._config)
        self._refresh_users_for_source(silent=True)
        self._update_header_badges()

    def _create_recommendation_dialog(self, **overrides) -> ServerRecommendationDialog:
        cfg = self._collect_config_from_ui()
        kwargs = {
            "app_root": APP_ROOT,
            "tables_dir": TABLES_DIR,
            "db_path_cfg": cfg.karyawan_db_path,
            "only_active": cfg.karyawan_db_only_active,
            "ini_path_cfg": cfg.http_server_ini_path,
            "fallback_user_count": max(1, self.tbl_users.rowCount()),
        }
        kwargs.update(overrides)
        return ServerRecommendationDialog(self, **kwargs)

    def _on_calculate_server_clicked(self) -> None:
        dlg = self._create_recommendation_dialog()
        dlg.exec_()
        if dlg.selected_ini_path:
            self._http_server_ini_path = dlg.selected_ini_path
        if dlg.applied_sim_settings:
            applied = dlg.applied_sim_settings
            self.sp_target_total.setValue(int(applied.get("target_total_runs", self.sp_target_total.value())))
            self.sp_concurrency.setValue(int(applied.get("max_concurrency", self.sp_concurrency.value())))
            self.sp_launch_interval.setValue(int(applied.get("launch_interval_ms", self.sp_launch_interval.value())))
            self.sp_timeout.setValue(float(applied.get("timeout_seconds", self.sp_timeout.value())))
            self._append_log(
                "INFO",
                "Setting aman simulator diterapkan dari kalkulasi server: "
                f"Concurrency={self.sp_concurrency.value()}, Jeda={self.sp_launch_interval.value()} ms, "
                f"Timeout={self.sp_timeout.value():.0f} dtk",
            )
        self._config = self._collect_config_from_ui()
        self._save_config(self._config)

    # ------------------------------------------------------------------
    # Simulation Execution & Live Signals
    # ------------------------------------------------------------------
    def _on_start_clicked(self) -> None:
        if self._worker and self._worker.isRunning():
            return

        cfg = self._collect_config_from_ui()
        if cfg.user_source == "karyawan_db":
            if not self._reload_users_from_db(silent=False):
                self.tabs.setCurrentIndex(self.tab_users_idx)
                return
            users = list(self._users)
        else:
            users = self._collect_users_from_table()

        if not users:
            QMessageBox.warning(
                self,
                "Daftar User Kosong",
                "Minimal harus ada 1 user di tab 'Daftar User' sebelum memulai simulasi.",
            )
            self.tabs.setCurrentIndex(self.tab_users_idx)
            return

        if not any(
            [
                cfg.step_getversion,
                cfg.step_auth,
                cfg.step_getabsen,
                cfg.step_getpesanunread,
                cfg.step_getabsen_bulanan,
                cfg.step_masuk,
                cfg.step_ftp_upload,
                cfg.step_absen,
            ]
        ):
            QMessageBox.warning(self, "Tidak Ada Step Aktif", "Aktifkan minimal 1 langkah API Flutter untuk disimulasikan.")
            return

        self._config = cfg
        self._users = users
        self._save_config(cfg)
        if cfg.user_source == "manual":
            self._save_users(users)
        self._update_header_badges()

        self._last_records = []
        self._last_stats = {}
        self.tbl_endpoints.setRowCount(0)
        self.tbl_monitor.setRowCount(0)
        self.progress_bar.setValue(0)
        self.tabs.setCurrentIndex(0)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        self._worker = SimulatorWorker(cfg, users)
        self._worker.init_records_sig.connect(self._on_sim_init_records)
        self._worker.user_update_sig.connect(self._on_sim_user_update)
        self._worker.stats_update_sig.connect(self._on_sim_stats_update)
        self._worker.log_sig.connect(self._append_log)
        self._worker.error_sig.connect(lambda e: self._append_log("ERROR", f"Worker error: {e}"))
        self._worker.finished_sig.connect(self._on_sim_finished)
        self._worker.start()

    def _on_stop_clicked(self) -> None:
        if self._worker and self._worker.isRunning():
            self.btn_stop.setEnabled(False)
            self._append_log("WARN", "Menghentikan simulasi...")
            self._worker.stop()

    def _status_color(self, status: str) -> QColor:
        if status == "Success":
            return QColor("#34d399")
        if status == "Failed":
            return QColor("#f87171")
        if status == "Running":
            return QColor("#38bdf8")
        if status == "Cancelled":
            return QColor("#fbbf24")
        return QColor("#94a3b8")

    def _on_sim_init_records(self, snapshots: list) -> None:
        total = len(snapshots)
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self.card_progress.set_value(f"0 / {total}")

        self.tbl_monitor.setUpdatesEnabled(False)
        self.tbl_monitor.setRowCount(total)
        for row_idx, snap in enumerate(snapshots):
            run_idx, uid, kode, is_sales, cur_step, status, total_ms, detail = snap
            vals = [
                str(run_idx),
                uid,
                kode,
                "Sales" if is_sales else "Reguler",
                cur_step,
                status,
                "-",
                detail,
            ]
            for col_idx, val in enumerate(vals):
                item = QTableWidgetItem(val)
                if col_idx == 5:
                    item.setForeground(self._status_color(status))
                self.tbl_monitor.setItem(row_idx, col_idx, item)
        self.tbl_monitor.setUpdatesEnabled(True)

    def _on_sim_user_update(self, snap: tuple) -> None:
        run_idx, uid, kode, is_sales, cur_step, status, total_ms, detail = snap
        row = run_idx - 1
        if row < 0 or row >= self.tbl_monitor.rowCount():
            return

        self.tbl_monitor.item(row, 1).setText(uid)
        self.tbl_monitor.item(row, 2).setText(kode)
        self.tbl_monitor.item(row, 3).setText("Sales" if is_sales else "Reguler")
        self.tbl_monitor.item(row, 4).setText(cur_step)

        st_item = self.tbl_monitor.item(row, 5)
        st_item.setText(status)
        st_item.setForeground(self._status_color(status))

        self.tbl_monitor.item(row, 6).setText(f"{total_ms:.0f}" if total_ms > 0 else "-")
        self.tbl_monitor.item(row, 7).setText(detail)

    def _on_sim_stats_update(self, stats: dict) -> None:
        self._last_stats = stats
        total = stats.get("total", 0)
        completed = stats.get("completed", 0)
        self.card_progress.set_value(f"{completed} / {total}")
        self.card_success.set_value(str(stats.get("success", 0)))
        self.card_failed.set_value(str(stats.get("failed", 0)))
        self.card_active.set_value(str(stats.get("active", 0)))
        self.card_rps.set_value(f"{stats.get('rps', 0.0):.1f}")
        self.card_latency.set_value(f"{stats.get('avg_ms', 0.0):.0f} ms")
        self.progress_bar.setValue(completed)

        step_metrics: Dict[str, dict] = stats.get("step_metrics", {})
        self.tbl_endpoints.setRowCount(len(step_metrics))
        for r, (name, m) in enumerate(step_metrics.items()):
            self.tbl_endpoints.setItem(r, 0, QTableWidgetItem(name))
            self.tbl_endpoints.setItem(r, 1, QTableWidgetItem(str(m.get("count", 0))))
            ok_item = QTableWidgetItem(str(m.get("success", 0)))
            ok_item.setForeground(QColor("#34d399"))
            self.tbl_endpoints.setItem(r, 2, ok_item)
            err_item = QTableWidgetItem(str(m.get("failed", 0)))
            if m.get("failed", 0) > 0:
                err_item.setForeground(QColor("#f87171"))
            self.tbl_endpoints.setItem(r, 3, err_item)
            self.tbl_endpoints.setItem(r, 4, QTableWidgetItem(f"{m.get('avg_ms', 0.0):.1f}"))

    def _on_sim_finished(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if self._worker:
            self._last_records = self._worker.final_records
            if self._worker.final_stats:
                self._on_sim_stats_update(self._worker.final_stats)
        self._flush_logs()

    # ------------------------------------------------------------------
    # Logging & Exports
    # ------------------------------------------------------------------
    def _append_log(self, level: str, msg: str) -> None:
        self._all_logs.append((level, msg))
        if len(self._all_logs) > 5000:
            self._all_logs = self._all_logs[-3000:]
        self._log_queue.append((level, msg))

    def _flush_logs(self) -> None:
        if not self._log_queue:
            return
        only_err = self.cb_log_filter.currentIndex() == 1
        lines: List[str] = []
        while self._log_queue:
            lvl, msg = self._log_queue.popleft()
            if only_err and lvl != "ERROR":
                continue
            lines.append(f"[{lvl}] {msg}")
        if lines:
            self.txt_logs.appendPlainText("\n".join(lines))
            if self.chk_autoscroll.isChecked():
                sb = self.txt_logs.verticalScrollBar()
                sb.setValue(sb.maximum())

    def _rebuild_log_view(self) -> None:
        only_err = self.cb_log_filter.currentIndex() == 1
        filtered = [f"[{lvl}] {msg}" for lvl, msg in self._all_logs if not only_err or lvl == "ERROR"]
        self.txt_logs.setPlainText("\n".join(filtered[-3000:]))

    def _on_clear_logs(self) -> None:
        self._all_logs.clear()
        self._log_queue.clear()
        self.txt_logs.clear()

    def _on_export_results_csv(self) -> None:
        if self.tbl_monitor.rowCount() == 0:
            QMessageBox.information(self, "Belum Ada Data", "Jalankan simulasi terlebih dahulu untuk mengekspor hasil.")
            return
        default_name = f"sim_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export Hasil Simulasi CSV", os.path.join(APP_ROOT, default_name), "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["No", "UserID", "Kode", "Tipe", "Step", "Status", "Durasi_ms", "Detail"])
                for r in range(self.tbl_monitor.rowCount()):
                    w.writerow([self.tbl_monitor.item(r, c).text() for c in range(8)])
            QMessageBox.information(self, "Export Berhasil", f"Hasil simulasi disimpan ke:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Gagal Export", str(exc))

    def _on_export_summary_json(self) -> None:
        if not self._last_stats:
            QMessageBox.information(self, "Belum Ada Summary", "Jalankan simulasi terlebih dahulu untuk mengekspor summary.")
            return
        default_name = f"sim_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path, _ = QFileDialog.getSaveFileName(self, "Export Summary JSON", os.path.join(APP_ROOT, default_name), "JSON (*.json)")
        if not path:
            return
        payload = {
            "exported_at": datetime.now().isoformat(),
            "config": self._config.to_dict(),
            "summary": self._last_stats,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            QMessageBox.information(self, "Export Berhasil", f"Summary JSON disimpan ke:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Gagal Export", str(exc))

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            ans = QMessageBox.question(
                self,
                "Simulasi Sedang Berjalan",
                "Simulasi masih aktif. Hentikan simulasi dan tutup aplikasi?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                event.ignore()
                return
            self._worker.stop()
            self._worker.wait(3000)
        cfg = self._collect_config_from_ui()
        self._save_config(cfg)
        if cfg.user_source == "manual":
            self._save_users(self._collect_users_from_table())
        event.accept()
