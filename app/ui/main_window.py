"""Главное окно первого этапа разработки."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Контакт — учёт электроэнергии")
        self.resize(960, 600)
        self.setMinimumSize(680, 400)

        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(16)

        title = QLabel("контакт", content)
        title.setStyleSheet("font-size: 30px; font-weight: 650;")
        layout.addWidget(title)

        info = QLabel(
            "Локальная база данных готова.\n"
            "Справочник арендаторов и расчёты будут добавлены на следующих этапах.",
            content,
        )
        info.setWordWrap(True)
        info.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(info)
        layout.addStretch()

        self.setCentralWidget(content)
        self.statusBar().showMessage("База данных подключена")
