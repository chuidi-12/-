"""题库训练 — 程序入口。运行：python main.py"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from PySide6.QtGui import QFont, QIcon  # noqa: E402
    from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
except ImportError as exc:
    print("无法导入 PySide6，程序无法启动。")
    print("请先安装依赖：python -m pip install -r requirements.txt")
    print(f"详细信息：{exc}")
    sys.exit(2)

from app import db, paths, theme  # noqa: E402
from app.config import APP_NAME, APP_VERSION  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.widgets.common import center_on_screen  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 11))
    icon_file = paths.resource("app_icon.ico")
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))
    try:
        db.connect()  # 首次运行自动建库并执行迁移
    except Exception as exc:
        QMessageBox.critical(None, "无法启动", f"数据库初始化失败：\n{exc}")
        return 1
    theme.restore(app)  # 恢复上次的界面主题
    window = MainWindow()
    center_on_screen(window)   # 第十三版：主窗口也居中，不再固定落在屏幕左上角
    window.show()
    try:
        return app.exec()
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
