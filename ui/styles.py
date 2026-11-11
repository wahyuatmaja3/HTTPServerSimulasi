DARK_STYLESHEET = """
QMainWindow, QDialog {
    background-color: #0f172a;
    color: #e2e8f0;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
}

QWidget {
    color: #e2e8f0;
    font-family: "Segoe UI", Arial, sans-serif;
}

QGroupBox {
    background-color: #1e293b;
    border: 1px solid #334155;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
    color: #93c5fd;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #38bdf8;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

QFrame#MetricCard {
    background-color: #1e293b;
    border: 1px solid #334155;
    border-radius: 8px;
}

QFrame#HeaderBar {
    background-color: #1e293b;
    border-bottom: 1px solid #334155;
}

QLabel#MutedText {
    color: #94a3b8;
    font-size: 12px;
}

QLabel#MetricValue {
    font-size: 20px;
    font-weight: 700;
    color: #f8fafc;
}

QLabel#MetricLabel {
    font-size: 11px;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.4px;
}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {
    background-color: #0f172a;
    border: 1px solid #475569;
    border-radius: 6px;
    padding: 6px 8px;
    color: #f1f5f9;
    selection-background-color: #2563eb;
}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid #38bdf8;
}

QLineEdit:disabled, QSpinBox:disabled {
    background-color: #162032;
    color: #64748b;
}

QPushButton {
    background-color: #334155;
    border: 1px solid #475569;
    border-radius: 6px;
    padding: 7px 14px;
    color: #f8fafc;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #475569;
}

QPushButton:disabled {
    background-color: #1e293b;
    color: #64748b;
    border-color: #334155;
}

QPushButton#PrimaryBtn {
    background-color: #10b981;
    border: 1px solid #059669;
    color: #022c22;
    font-weight: 700;
    padding: 8px 18px;
}

QPushButton#PrimaryBtn:hover {
    background-color: #34d399;
}

QPushButton#PrimaryBtn:disabled {
    background-color: #1e293b;
    color: #64748b;
    border-color: #334155;
}

QPushButton#DangerBtn {
    background-color: #ef4444;
    border: 1px solid #dc2626;
    color: #ffffff;
    font-weight: 700;
    padding: 8px 16px;
}

QPushButton#DangerBtn:hover {
    background-color: #f87171;
}

QPushButton#DangerBtn:disabled {
    background-color: #1e293b;
    color: #64748b;
    border-color: #334155;
}

QPushButton#AccentBtn {
    background-color: #0284c7;
    border: 1px solid #0369a1;
    color: #ffffff;
}

QPushButton#AccentBtn:hover {
    background-color: #0ea5e9;
}

QTabWidget::pane {
    border: 1px solid #334155;
    border-radius: 8px;
    background-color: #1e293b;
    top: -1px;
}

QTabBar::tab {
    background-color: #0f172a;
    border: 1px solid #334155;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 16px;
    margin-right: 4px;
    color: #94a3b8;
    font-weight: 600;
}

QTabBar::tab:selected {
    background-color: #1e293b;
    color: #38bdf8;
    border-bottom: 2px solid #38bdf8;
}

QTableWidget {
    background-color: #0f172a;
    alternate-background-color: #162032;
    gridline-color: #334155;
    border: 1px solid #334155;
    border-radius: 6px;
    color: #e2e8f0;
    selection-background-color: #1d4ed8;
}

QHeaderView::section {
    background-color: #1e293b;
    color: #94a3b8;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #334155;
    border-bottom: 1px solid #334155;
    font-weight: 600;
    font-size: 12px;
}

QProgressBar {
    background-color: #0f172a;
    border: 1px solid #334155;
    border-radius: 6px;
    text-align: center;
    color: #f8fafc;
    font-weight: 700;
    height: 22px;
}

QProgressBar::chunk {
    background-color: #10b981;
    border-radius: 5px;
}

QCheckBox, QRadioButton {
    spacing: 8px;
    color: #e2e8f0;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #64748b;
    background-color: #0f172a;
}

QCheckBox::indicator:checked {
    background-color: #10b981;
    border-color: #10b981;
}

QScrollArea {
    border: none;
    background-color: transparent;
}
"""
