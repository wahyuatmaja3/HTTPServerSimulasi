import asyncio
import os
import re
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.models import UserAccount
from core.server_advisor import (
    BdeSettings,
    HttpServerIniSettings,
    ParameterAdvice,
    RecommendationReport,
    calculate_recommendations,
    read_bde_settings,
    read_http_server_ini,
    read_karyawan_summary,
    resolve_http_server_ini_path,
)
from core.simulator import fetch_server_by_key


class BulkPasteUsersDialog(QDialog):
    """Dialog for bulk-pasting user credentials or generating sequential dummy users."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Bulk Paste / Generate User Simulasi")
        self.resize(560, 460)
        self._users: List[UserAccount] = []
        self._replace_existing: bool = False

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Tab 1: Paste rows
        paste_tab = QWidget()
        paste_layout = QVBoxLayout(paste_tab)
        info = QLabel(
            "Masukkan 1 user per baris dengan pemisah koma (,), titik koma (;), tab, atau titik dua (:).\n"
            "Format: user_id, password [, kode] [, sales(Auto/True/False)]"
        )
        info.setObjectName("MutedText")
        info.setWordWrap(True)
        paste_layout.addWidget(info)

        self.paste_edit = QPlainTextEdit()
        self.paste_edit.setPlaceholderText(
            "user01,123,1001,Auto\n"
            "user02,123,1002,False\n"
            "sales01,secret,,True"
        )
        paste_layout.addWidget(self.paste_edit)

        self.chk_replace_paste = QCheckBox("Ganti seluruh daftar user saat ini (Replace)")
        paste_layout.addWidget(self.chk_replace_paste)
        self.tabs.addTab(paste_tab, "Paste Daftar User")

        # Tab 2: Generate sequential users
        gen_tab = QWidget()
        gen_layout = QVBoxLayout(gen_tab)
        form = QFormLayout()

        self.ed_prefix = QLineEdit("user")
        self.sp_start = QSpinBox()
        self.sp_start.setRange(0, 999999)
        self.sp_start.setValue(1)

        self.sp_count = QSpinBox()
        self.sp_count.setRange(1, 5000)
        self.sp_count.setValue(20)

        self.sp_padding = QSpinBox()
        self.sp_padding.setRange(0, 6)
        self.sp_padding.setValue(2)

        self.ed_password = QLineEdit("123")
        self.ed_kode_prefix = QLineEdit("")
        self.ed_kode_prefix.setPlaceholderText("Opsional (kosong = ambil otomatis dari server auth/masuk)")

        self.sp_kode_start = QSpinBox()
        self.sp_kode_start.setRange(0, 999999)
        self.sp_kode_start.setValue(1001)

        self.cb_sales = QComboBox()
        self.cb_sales.addItems(["Auto", "False", "True"])

        self.chk_replace_gen = QCheckBox("Ganti seluruh daftar user saat ini (Replace)")
        self.lbl_preview = QLabel("")
        self.lbl_preview.setObjectName("MutedText")
        self.lbl_preview.setWordWrap(True)

        form.addRow("Prefix UserID:", self.ed_prefix)
        form.addRow("Nomor Awal:", self.sp_start)
        form.addRow("Jumlah User:", self.sp_count)
        form.addRow("Zero Padding:", self.sp_padding)
        form.addRow("Password Default:", self.ed_password)
        form.addRow("Prefix Kode (opsional):", self.ed_kode_prefix)
        form.addRow("Nomor Kode Awal:", self.sp_kode_start)
        form.addRow("Tipe Sales:", self.cb_sales)

        gen_layout.addLayout(form)
        gen_layout.addWidget(self.chk_replace_gen)
        gen_layout.addWidget(self.lbl_preview)
        gen_layout.addStretch()
        self.tabs.addTab(gen_tab, "Generate User Massal")

        # Update preview reactively
        for sig in (
            self.ed_prefix.textChanged,
            self.ed_password.textChanged,
            self.ed_kode_prefix.textChanged,
            self.cb_sales.currentTextChanged,
        ):
            sig.connect(self._update_preview)
        for sp in (self.sp_start, self.sp_count, self.sp_padding, self.sp_kode_start):
            sp.valueChanged.connect(self._update_preview)
        self._update_preview()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Tambahkan")
        buttons.button(QDialogButtonBox.Cancel).setText("Batal")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _format_num(self, val: int, pad: int) -> str:
        return str(val).zfill(pad) if pad > 0 else str(val)

    def _build_generated_sample(self, offset: int) -> UserAccount:
        prefix = self.ed_prefix.text().strip() or "user"
        uid = f"{prefix}{self._format_num(self.sp_start.value() + offset, self.sp_padding.value())}"
        kp = self.ed_kode_prefix.text().strip()
        kode = f"{kp}{self.sp_kode_start.value() + offset}" if kp else ""
        return UserAccount(
            user_id=uid,
            password=self.ed_password.text(),
            kode=kode,
            sales=self.cb_sales.currentText(),
        )

    def _update_preview(self) -> None:
        first = self._build_generated_sample(0)
        cnt = self.sp_count.value()
        last = self._build_generated_sample(cnt - 1)
        self.lbl_preview.setText(
            f"Preview: {first.user_id} (Kode: {first.kode or 'auto'}) s/d {last.user_id} (Kode: {last.kode or 'auto'}) — {cnt} user"
        )

    def _on_accept(self) -> None:
        if self.tabs.currentIndex() == 0:
            raw = self.paste_edit.toPlainText().strip()
            if not raw:
                QMessageBox.warning(self, "Input Kosong", "Silakan tempel daftar user terlebih dahulu.")
                return
            parsed: List[UserAccount] = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in re.split(r"[,;\t:]+", line)]
                if not parts or not parts[0]:
                    continue
                uid = parts[0]
                pwd = parts[1] if len(parts) > 1 else ""
                kode = parts[2] if len(parts) > 2 else ""
                sales_raw = parts[3].lower() if len(parts) > 3 else "auto"
                if sales_raw in ("true", "1", "yes", "sales"):
                    sales = "True"
                elif sales_raw in ("false", "0", "no", "regular", "non-sales"):
                    sales = "False"
                else:
                    sales = "Auto"
                parsed.append(UserAccount(user_id=uid, password=pwd, kode=kode, sales=sales))
            if not parsed:
                QMessageBox.warning(self, "Format Tidak Valid", "Tidak ada baris user valid yang dapat diparsing.")
                return
            self._users = parsed
            self._replace_existing = self.chk_replace_paste.isChecked()
        else:
            cnt = self.sp_count.value()
            self._users = [self._build_generated_sample(i) for i in range(cnt)]
            self._replace_existing = self.chk_replace_gen.isChecked()

        self.accept()

    def get_users(self) -> List[UserAccount]:
        return self._users

    def should_replace(self) -> bool:
        return self._replace_existing


class _FetchServerWorker(QThread):
    success_sig = pyqtSignal(dict)
    error_sig = pyqtSignal(str)

    def __init__(self, server_key: str):
        super().__init__()
        self.server_key = server_key

    def run(self) -> None:
        try:
            res = asyncio.run(fetch_server_by_key(self.server_key))
            self.success_sig.emit(res)
        except Exception as exc:
            self.error_sig.emit(str(exc))


class FetchServerKeyDialog(QDialog):
    """Dialog replicating Flutter SettingPage Server Key lookup via absensi.software-gallery.com."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Ambil Konfigurasi Server dari Server Key")
        self.resize(440, 175)
        self.fetched_server: Optional[dict] = None
        self._worker: Optional[_FetchServerWorker] = None

        layout = QVBoxLayout(self)
        desc = QLabel(
            "Masukkan Server Key perusahaan (sama seperti menu Setting di aplikasi Flutter).\n"
            "Simulator akan memanggil /api/gettoken dan /api/getserver."
        )
        desc.setObjectName("MutedText")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        row = QHBoxLayout()
        row.addWidget(QLabel("Server Key:"))
        self.ed_key = QLineEdit()
        self.ed_key.setPlaceholderText("Contoh: sjab")
        row.addWidget(self.ed_key)
        layout.addLayout(row)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("MutedText")
        layout.addWidget(self.lbl_status)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_fetch = QPushButton("Ambil Config")
        self.btn_fetch.setObjectName("AccentBtn")
        self.btn_cancel = QPushButton("Batal")
        self.btn_fetch.clicked.connect(self._on_fetch)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_fetch)
        btn_row.addWidget(self.btn_cancel)
        layout.addLayout(btn_row)

    def _on_fetch(self) -> None:
        key = self.ed_key.text().strip()
        if not key:
            QMessageBox.warning(self, "Key Kosong", "Masukkan Server Key terlebih dahulu.")
            return
        self.btn_fetch.setEnabled(False)
        self.lbl_status.setText("Menghubungi absensi.software-gallery.com ...")
        self._worker = _FetchServerWorker(key)
        self._worker.success_sig.connect(self._on_success)
        self._worker.error_sig.connect(self._on_error)
        self._worker.start()

    def _on_success(self, data: dict) -> None:
        self.btn_fetch.setEnabled(True)
        self.fetched_server = data
        self.accept()

    def _on_error(self, err: str) -> None:
        self.btn_fetch.setEnabled(True)
        self.lbl_status.setText("")
        QMessageBox.critical(self, "Gagal Mengambil Config", err)


class ServerRecommendationDialog(QDialog):
    """Dialog kalkulasi kapasitas server & rekomendasi HTTPServerDB.ini + BDE SYSTEM\\INIT agar 0% gagal."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        app_root: str = "",
        tables_dir: str = "",
        db_path_cfg: str = "tables/Karyawan.DB",
        only_active: bool = True,
        ini_path_cfg: str = r"D:\SJABPayroll\HTTPServerDB.ini",
        fallback_user_count: int = 1,
        bde_reader: Optional[Callable[[], BdeSettings]] = None,
        ini_reader: Optional[Callable[[str], HttpServerIniSettings]] = None,
        db_summary_reader: Optional[Callable[..., Dict[str, Any]]] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Kalkulasi Kapasitas Server (Karyawan.DB, BDE & .ini)")
        self.resize(980, 800)
        self.setMinimumSize(820, 600)

        self._app_root = app_root
        self._tables_dir = tables_dir
        self._db_path_cfg = db_path_cfg
        self._fallback_user_count = max(1, int(fallback_user_count or 1))
        self._bde_reader = bde_reader or read_bde_settings
        self._ini_reader = ini_reader or read_http_server_ini
        self._db_summary_reader = db_summary_reader or read_karyawan_summary

        self._db_info: Dict[str, Any] = {}
        self._current_bde: BdeSettings = BdeSettings()
        self._current_ini: HttpServerIniSettings = HttpServerIniSettings()
        self._report: Optional[RecommendationReport] = None
        self.applied_sim_settings: Optional[Dict[str, Any]] = None
        self.selected_ini_path: str = resolve_http_server_ini_path(ini_path_cfg, app_root)
        self._syncing_controls = False

        self._build_ui(only_active=only_active, ini_path=self.selected_ini_path)
        self.reload_sources_and_calculate()

    def _build_ui(self, only_active: bool, ini_path: str) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background-color: #0f172a; border: none; }")
        content = QWidget()
        content.setObjectName("AdvisorScrollContent")
        content.setStyleSheet("QWidget#AdvisorScrollContent { background-color: #0f172a; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 8, 6)
        layout.setSpacing(10)

        # 1. Sumber Data Terbaca Otomatis
        grp_src = QGroupBox("1. Sumber Data & Kondisi Saat Ini (Dibaca Otomatis)")
        src_layout = QVBoxLayout(grp_src)
        src_layout.setSpacing(8)

        db_row = QHBoxLayout()
        self.lbl_db_summary = QLabel("Membaca Karyawan.DB ...")
        self.lbl_db_summary.setWordWrap(True)
        self.chk_only_active = QCheckBox("Hanya Karyawan Aktif")
        self.chk_only_active.setChecked(only_active)
        self.chk_only_active.toggled.connect(self._on_only_active_toggled)

        db_row.addWidget(self.lbl_db_summary, 1)
        db_row.addWidget(self.chk_only_active)
        src_layout.addLayout(db_row)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target User Simulasi (dari Karyawan.DB):"))
        self.sp_target_users = QSpinBox()
        self.sp_target_users.setRange(1, 10000)
        self.sp_target_users.valueChanged.connect(self._recalculate)
        target_row.addWidget(self.sp_target_users)

        self.lbl_hw_info = QLabel("")
        self.lbl_hw_info.setObjectName("MutedText")
        target_row.addSpacing(12)
        target_row.addWidget(self.lbl_hw_info, 1)

        self.btn_reload_all = QPushButton("↻ Baca Ulang DB / BDE / .ini")
        self.btn_reload_all.clicked.connect(self.reload_sources_and_calculate)
        target_row.addWidget(self.btn_reload_all)
        src_layout.addLayout(target_row)

        ini_row = QHBoxLayout()
        ini_row.addWidget(QLabel("File HTTPServerDB.ini:"))
        self.ed_ini_path = QLineEdit(ini_path)
        self.ed_ini_path.editingFinished.connect(self._on_ini_path_edited)
        self.btn_browse_ini = QPushButton("Browse .ini...")
        self.btn_browse_ini.clicked.connect(self._on_browse_ini)
        ini_row.addWidget(self.ed_ini_path, 1)
        ini_row.addWidget(self.btn_browse_ini)
        src_layout.addLayout(ini_row)

        self.lbl_ini_status = QLabel("")
        self.lbl_ini_status.setWordWrap(True)
        self.lbl_bde_status = QLabel("")
        self.lbl_bde_status.setWordWrap(True)
        src_layout.addWidget(self.lbl_ini_status)
        src_layout.addWidget(self.lbl_bde_status)
        layout.addWidget(grp_src)

        # 2. Dua Centangan (BDE & .ini) + Tombol Kalkulasi
        grp_opt = QGroupBox("2. Pilih Komponen yang Boleh Diubah")
        opt_layout = QVBoxLayout(grp_opt)
        opt_layout.setSpacing(8)

        chk_row = QHBoxLayout()
        self.chk_allow_bde = QCheckBox("Ubah Setting BDE (Registry SYSTEM\\INIT)")
        self.chk_allow_bde.setChecked(True)
        self.chk_allow_bde.setStyleSheet("font-weight: 700;")
        self.chk_allow_ini = QCheckBox("Ubah Setting HTTPServerDB.ini")
        self.chk_allow_ini.setChecked(True)
        self.chk_allow_ini.setStyleSheet("font-weight: 700;")

        self.chk_allow_bde.toggled.connect(self._recalculate)
        self.chk_allow_ini.toggled.connect(self._recalculate)

        self.btn_recalculate = QPushButton("Kalkulasi Ulang")
        self.btn_recalculate.setObjectName("AccentBtn")
        self.btn_recalculate.clicked.connect(self._recalculate)

        chk_row.addWidget(self.chk_allow_bde)
        chk_row.addSpacing(18)
        chk_row.addWidget(self.chk_allow_ini)
        chk_row.addStretch()
        chk_row.addWidget(self.btn_recalculate)
        opt_layout.addLayout(chk_row)

        self.banner_frame = QFrame()
        self.banner_frame.setObjectName("BannerFrame")
        banner_layout = QVBoxLayout(self.banner_frame)
        banner_layout.setContentsMargins(12, 10, 12, 10)
        banner_layout.setSpacing(4)
        self.lbl_headline = QLabel("")
        self.lbl_headline.setWordWrap(True)
        self.lbl_headline.setStyleSheet("background: transparent; border: none; font-weight: 700; font-size: 13px;")
        self.lbl_capacity_detail = QLabel("")
        self.lbl_capacity_detail.setWordWrap(True)
        self.lbl_capacity_detail.setStyleSheet("background: transparent; border: none;")
        banner_layout.addWidget(self.lbl_headline)
        banner_layout.addWidget(self.lbl_capacity_detail)
        opt_layout.addWidget(self.banner_frame)
        layout.addWidget(grp_opt)

        # 3. Tabel Rekomendasi HTTPServerDB.ini
        self.grp_ini_tbl = QGroupBox("3. Rekomendasi Setting HTTPServerDB.ini ([Settings])")
        ini_tbl_layout = QVBoxLayout(self.grp_ini_tbl)
        self.tbl_ini = QTableWidget(0, 5)
        self.tbl_ini.setHorizontalHeaderLabels(["Parameter", "Saat Ini", "Rekomendasi", "Status", "Alasan"])
        self.tbl_ini.verticalHeader().setVisible(False)
        self.tbl_ini.verticalHeader().setDefaultSectionSize(28)
        self.tbl_ini.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_ini.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_ini.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl_ini.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_ini.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_ini.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_ini.setAlternatingRowColors(True)
        self.tbl_ini.setMinimumHeight(176)
        ini_tbl_layout.addWidget(self.tbl_ini)
        layout.addWidget(self.grp_ini_tbl)

        # 4. Tabel Rekomendasi BDE SYSTEM\INIT
        self.grp_bde_tbl = QGroupBox("4. Rekomendasi Setting BDE (Registry SYSTEM\\INIT)")
        bde_tbl_layout = QVBoxLayout(self.grp_bde_tbl)
        self.tbl_bde = QTableWidget(0, 6)
        self.tbl_bde.setHorizontalHeaderLabels(
            ["Parameter", "Registry (Aktif)", "IDAPI32.CFG", "Rekomendasi", "Status", "Alasan"]
        )
        self.tbl_bde.verticalHeader().setVisible(False)
        self.tbl_bde.verticalHeader().setDefaultSectionSize(28)
        self.tbl_bde.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_bde.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_bde.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl_bde.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_bde.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_bde.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.tbl_bde.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_bde.setAlternatingRowColors(True)
        self.tbl_bde.setMinimumHeight(204)
        bde_tbl_layout.addWidget(self.tbl_bde)
        layout.addWidget(self.grp_bde_tbl)

        # 5. Catatan & Peringatan
        self.grp_notes = QGroupBox("5. Peringatan & Panduan Penerapan")
        notes_layout = QVBoxLayout(self.grp_notes)
        self.txt_notes = QPlainTextEdit()
        self.txt_notes.setReadOnly(True)
        self.txt_notes.setMinimumHeight(92)
        self.txt_notes.setMaximumHeight(130)
        mono = QFont("Consolas", 9)
        self.txt_notes.setFont(mono)
        notes_layout.addWidget(self.txt_notes)
        layout.addWidget(self.grp_notes)

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # Bottom action buttons
        btn_row = QHBoxLayout()
        self.btn_copy_summary = QPushButton("Copy Ringkasan (.ini & BDE)")
        self.btn_copy_summary.clicked.connect(self._on_copy_summary)
        self.lbl_copy_feedback = QLabel("")
        self.lbl_copy_feedback.setStyleSheet("color: #34d399; font-weight: 600;")

        self.btn_apply_sim = QPushButton("Terapkan Setting Simulasi Aman (0% Gagal)")
        self.btn_apply_sim.setObjectName("PrimaryBtn")
        self.btn_apply_sim.clicked.connect(self._on_apply_safe_sim)

        self.btn_close = QPushButton("Tutup")
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_copy_summary)
        btn_row.addWidget(self.lbl_copy_feedback)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_apply_sim)
        btn_row.addWidget(self.btn_close)
        outer.addLayout(btn_row)

    def _reload_db_only(self) -> None:
        self._db_info = self._db_summary_reader(
            self._db_path_cfg,
            self._app_root,
            self._tables_dir,
            only_active=self.chk_only_active.isChecked(),
            fallback_user_count=self._fallback_user_count,
        )
        self._syncing_controls = True
        try:
            self.sp_target_users.setValue(int(self._db_info.get("user_count", self._fallback_user_count)))
        finally:
            self._syncing_controls = False

        if self._db_info.get("loaded"):
            db_name = os.path.basename(str(self._db_info.get("db_path", "")))
            fmt = self._db_info.get("format", "Paradox")
            uc = self._db_info.get("user_count", 0)
            act = self._db_info.get("active_count", uc)
            all_acc = self._db_info.get("all_accounts_count", uc)
            tot = self._db_info.get("total_rows", 0)
            self.lbl_db_summary.setStyleSheet("color: #34d399; font-weight: 600;")
            self.lbl_db_summary.setText(
                f"✓ {db_name} ({fmt}): {uc} user dimuat | Aktif: {act}, Total ber-UserID: {all_acc}, Total baris: {tot}"
            )
        else:
            err = self._db_info.get("error", "Gagal membaca tabel")
            self.lbl_db_summary.setStyleSheet("color: #fbbf24; font-weight: 600;")
            self.lbl_db_summary.setText(f"⚠ {err} — menggunakan fallback {self._fallback_user_count} user.")

    def reload_sources_and_calculate(self) -> None:
        self._reload_db_only()
        self.selected_ini_path = self.ed_ini_path.text().strip() or self.selected_ini_path
        self._current_ini = self._ini_reader(self.selected_ini_path)
        self._current_bde = self._bde_reader()
        self._update_source_badges()
        self._recalculate()

    def _update_source_badges(self) -> None:
        ini = self._current_ini
        if ini.exists:
            self.lbl_ini_status.setStyleSheet("color: #34d399;")
            self.lbl_ini_status.setText(
                f"✓ .ini terbaca: Port={ini.port}, MaxConnections={ini.max_connections}, "
                f"ListenQueue={ini.listen_queue}, MaxThreads={ini.max_threads}, "
                f"SessionTimeOut={ini.session_timeout}, DetailLog={ini.detail_log}"
            )
        else:
            self.lbl_ini_status.setStyleSheet("color: #fbbf24;")
            self.lbl_ini_status.setText(
                f"⚠ File .ini tidak ditemukan di '{ini.ini_path}' (perhitungan memakai baseline default HTTPServerDB)."
            )

        bde = self._current_bde
        if bde.registry_found:
            if bde.cfg_mismatch:
                self.lbl_bde_status.setStyleSheet("color: #fbbf24;")
                self.lbl_bde_status.setText(
                    f"⚠ BDE Registry ({bde.source}): MEMSIZE={bde.memsize} MB, SHAREDMEMSIZE={bde.sharedmemsize} KB, "
                    f"MAXFILEHANDLES={bde.maxfilehandles}. BERBEDA dengan {bde.cfg_settings and bde.config_file}!"
                )
            else:
                self.lbl_bde_status.setStyleSheet("color: #34d399;")
                self.lbl_bde_status.setText(
                    f"✓ BDE Registry aktif: MEMSIZE={bde.memsize} MB, SHAREDMEMSIZE={bde.sharedmemsize} KB, "
                    f"MAXFILEHANDLES={bde.maxfilehandles}, MIN/MAXBUFSIZE={bde.minbufsize}/{bde.maxbufsize} KB"
                )
        else:
            self.lbl_bde_status.setStyleSheet("color: #fbbf24;")
            self.lbl_bde_status.setText(f"⚠ {bde.source}")

    def _on_only_active_toggled(self, _checked: bool) -> None:
        self._reload_db_only()
        self._recalculate()

    def _on_ini_path_edited(self) -> None:
        new_path = self.ed_ini_path.text().strip()
        if new_path and new_path != self.selected_ini_path:
            self.selected_ini_path = new_path
            self._current_ini = self._ini_reader(new_path)
            self._update_source_badges()
            self._recalculate()

    def _on_browse_ini(self) -> None:
        start_dir = os.path.dirname(self.ed_ini_path.text().strip()) or self._app_root or "."
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Pilih File HTTPServerDB.ini",
            start_dir,
            "INI Files (*.ini);;All Files (*)",
        )
        if not path:
            return
        self.ed_ini_path.setText(path)
        self.selected_ini_path = path
        self._current_ini = self._ini_reader(path)
        self._update_source_badges()
        self._recalculate()

    def _recalculate(self, *_args) -> None:
        if self._syncing_controls:
            return
        self.lbl_copy_feedback.setText("")
        allow_bde = self.chk_allow_bde.isChecked()
        allow_ini = self.chk_allow_ini.isChecked()
        target_users = self.sp_target_users.value()

        report = calculate_recommendations(
            user_count=target_users,
            current_bde=self._current_bde,
            current_ini=self._current_ini,
            allow_bde=allow_bde,
            allow_ini=allow_ini,
            db_info=self._db_info,
        )
        self._report = report
        self._render_report(report)

    def _fmt_val(self, val: Any, unit: str) -> str:
        return f"{val} {unit}".strip() if unit else str(val)

    def _style_status_item(self, advice: ParameterAdvice) -> QTableWidgetItem:
        item = QTableWidgetItem(advice.status)
        if advice.locked:
            item.setForeground(QColor("#94a3b8"))
        elif advice.changed:
            item.setForeground(QColor("#34d399"))
        else:
            item.setForeground(QColor("#38bdf8"))
        return item

    def _render_report(self, rep: RecommendationReport) -> None:
        ram_txt = f"{rep.system_ram_mb / 1024:.1f} GB RAM" if rep.system_ram_mb else "RAM N/A"
        self.lbl_hw_info.setText(f"Spec PC Server: {rep.cpu_threads} CPU Threads | {ram_txt}")

        if rep.can_achieve_full_burst_zero_fail:
            self.banner_frame.setStyleSheet(
                "QFrame#BannerFrame { background-color: #064e3b; border: 1px solid #10b981; border-radius: 8px; }"
            )
            self.lbl_headline.setStyleSheet(
                "background: transparent; border: none; color: #6ee7b7; font-weight: 700; font-size: 13px;"
            )
        else:
            self.banner_frame.setStyleSheet(
                "QFrame#BannerFrame { background-color: #451a03; border: 1px solid #f59e0b; border-radius: 8px; }"
            )
            self.lbl_headline.setStyleSheet(
                "background: transparent; border: none; color: #fcd34d; font-weight: 700; font-size: 13px;"
            )

        self.lbl_headline.setText(rep.headline_status)
        cur_comb = min(rep.current_ini_capacity, rep.current_bde_capacity)
        self.lbl_capacity_detail.setText(
            f"Saat ini: ~{cur_comb} serentak (.ini ~{rep.current_ini_capacity} | BDE ~{rep.current_bde_capacity})  →  "
            f"Setelah rekomendasi: ~{rep.projected_safe_concurrency} serentak (.ini ~{rep.projected_ini_capacity} | BDE ~{rep.projected_bde_capacity})  |  "
            f"Simulator 0% Gagal: User Bersamaan={rep.recommended_sim_concurrency}, Jeda Start={rep.recommended_sim_interval_ms} ms"
        )

        # Group titles clearly indicate locked vs active
        self.grp_ini_tbl.setTitle(
            "3. Rekomendasi Setting HTTPServerDB.ini ([Settings])"
            if rep.allow_ini
            else "3. Setting HTTPServerDB.ini — TIDAK DIUBAH (Centangan .ini non-aktif)"
        )
        self.grp_bde_tbl.setTitle(
            "4. Rekomendasi Setting BDE (Registry SYSTEM\\INIT)"
            if rep.allow_bde
            else "4. Setting BDE (Registry SYSTEM\\INIT) — TIDAK DIUBAH (Centangan BDE non-aktif)"
        )

        # Populate INI table
        self.tbl_ini.setRowCount(len(rep.ini_items))
        for r, item in enumerate(rep.ini_items):
            cur_s = self._fmt_val(item.current, item.unit)
            rec_s = self._fmt_val(item.recommended, item.unit)
            rec_cell = QTableWidgetItem(rec_s)
            if item.locked:
                rec_cell.setForeground(QColor("#94a3b8"))
            elif item.changed:
                rec_cell.setForeground(QColor("#34d399"))
                f = rec_cell.font()
                f.setBold(True)
                rec_cell.setFont(f)
            else:
                rec_cell.setForeground(QColor("#38bdf8"))

            self.tbl_ini.setItem(r, 0, QTableWidgetItem(item.name))
            self.tbl_ini.setItem(r, 1, QTableWidgetItem(cur_s))
            self.tbl_ini.setItem(r, 2, rec_cell)
            self.tbl_ini.setItem(r, 3, self._style_status_item(item))
            self.tbl_ini.setItem(r, 4, QTableWidgetItem(item.reason))

        # Populate BDE table
        self.tbl_bde.setRowCount(len(rep.bde_items))
        for r, item in enumerate(rep.bde_items):
            cur_s = self._fmt_val(item.current, item.unit)
            cfg_s = self._fmt_val(item.cfg_value, item.unit) if item.cfg_value != "-" else "-"
            rec_s = self._fmt_val(item.recommended, item.unit)

            cfg_cell = QTableWidgetItem(cfg_s)
            if item.cfg_value != "-" and str(item.cfg_value).upper() != str(item.current).upper():
                cfg_cell.setForeground(QColor("#fbbf24"))

            rec_cell = QTableWidgetItem(rec_s)
            if item.locked:
                rec_cell.setForeground(QColor("#94a3b8"))
            elif item.changed:
                rec_cell.setForeground(QColor("#34d399"))
                f = rec_cell.font()
                f.setBold(True)
                rec_cell.setFont(f)
            else:
                rec_cell.setForeground(QColor("#38bdf8"))

            self.tbl_bde.setItem(r, 0, QTableWidgetItem(item.name))
            self.tbl_bde.setItem(r, 1, QTableWidgetItem(cur_s))
            self.tbl_bde.setItem(r, 2, cfg_cell)
            self.tbl_bde.setItem(r, 3, rec_cell)
            self.tbl_bde.setItem(r, 4, self._style_status_item(item))
            self.tbl_bde.setItem(r, 5, QTableWidgetItem(item.reason))

        # Notes & Warnings
        lines: List[str] = []
        for w in rep.warnings:
            lines.append(f"[PERINGATAN] {w}")
        for n in rep.notes:
            lines.append(f"[PANDUAN]    {n}")
        if not lines:
            lines.append("Konfigurasi server siap.")
        self.txt_notes.setPlainText("\n".join(lines))

    def _on_copy_summary(self) -> None:
        if not self._report:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self._report.summary_text)
        self.lbl_copy_feedback.setText("✓ Tersalin!")

    def _on_apply_safe_sim(self) -> None:
        if not self._report:
            return
        rep = self._report
        self.applied_sim_settings = {
            "target_total_runs": rep.user_count,
            "max_concurrency": rep.recommended_sim_concurrency,
            "launch_interval_ms": rep.recommended_sim_interval_ms,
            "timeout_seconds": rep.recommended_sim_timeout_s,
            "http_server_ini_path": self.selected_ini_path,
        }
        self.accept()

    def get_report(self) -> Optional[RecommendationReport]:
        return self._report

