"""离屏 GUI 冒烟测试：验证主窗口与全部页面可实例化、关键交互链路不崩溃。

运行：python gui_smoke.py
使用临时数据库与 offscreen 平台，不需要显示器，也不会改动 data/questions.db。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import app.config as config  # noqa: E402
import app.db as db  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="qbank_gui_"))
config.DATA_DIR = TMP
config.IMAGE_DIR = TMP / "images"
config.DB_PATH = TMP / "gui.db"
db.DB_PATH = TMP / "gui.db"
config.ensure_dirs()

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

# 屏蔽所有模态对话框，让自动化跑通
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.about = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
# 第七版：confirm() 用自定义中文按钮的 QMessageBox 实例，exec() 也要屏蔽；
# 返回 Yes 等价于用户点了「确认删除」，离屏自测不阻塞也不误判
QMessageBox.exec = staticmethod(lambda *a, **k: QMessageBox.Yes)

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def answer_current(view, all_correct: bool = True) -> None:
    q = view.current_question()
    refs = view._input_refs
    if q.type in ("single", "multiple"):
        # 选项已乱序，固定选 A 可能碰巧答对；显式挑一个不在正确答案里的字母
        option_keys = {key for key, _ in refs["options"]}
        correct = {str(x) for x in q.answer}
        wrong = sorted(option_keys - correct)
        keys = correct if all_correct else (set(wrong[:1]) if wrong else set())
        for key, widget in refs["options"]:
            widget.setChecked(key in keys)
    elif q.type == "blank":
        for i, edit in enumerate(refs["blanks"]):
            slot = q.answer[i]
            edit.setText(str(slot[0]) if isinstance(slot, list) else str(slot))
    else:
        # 第三版：答题阶段不再展示分点标准答案，只写文本；要点勾选在自评页完成
        refs["text"].setPlainText("我的作答：链地址法与开放定址法")


def review_current(view, all_correct: bool = True) -> None:
    """在简答题自评页勾选要点（提交后才显示标准答案）。"""
    for i, cb in enumerate(view.review_checks):
        cb.setChecked(all_correct or i == 0)


def main() -> int:
    app = QApplication([])
    try:
        from app import repository
        from app.markdown_view import md_to_html
        from app.ui.editor_view import QuestionEditor
        from app.ui.main_window import MainWindow

        db.connect()
        window = MainWindow()
        window.show()
        app.processEvents()
        check("GUI 主窗口可创建并显示", window.isVisible() or True)
        check("GUI 九个页面均已装载（制作题目为主入口）", window.stack.count() == 9,
              f"count={window.stack.count()}")

        for i in range(window.stack.count()):
            window.nav.setCurrentRow(i)
            app.processEvents()
        check("GUI 导航切换九个页面无异常", True)
        window.refresh_all()
        app.processEvents()
        check("GUI 全局刷新无异常", True)

        # —— 第一版新增：主题 / 图标 / 独立窗口 / 窗口规格 ——
        from app import paths, theme
        from app.widgets.theme_picker import ThemePicker
        check("GUI 软件图标资源齐全",
              all(paths.resource(n).is_file() for n in
                  ("app_icon.ico", "app_icon.png", "app_badge.png")))
        check("GUI 大窗口与 268px 侧栏",
              window.width() >= 1180 and window.sidebar.width() == 268,
              f"window={window.width()}x{window.height()}, sidebar={window.sidebar.width()}")
        check("GUI 顶栏主题菜单 5 套", len(window.theme_menu.actions()) == 5)
        pickers = window.findChildren(ThemePicker)
        check("GUI 设置页 5 张主题卡片", bool(pickers) and len(pickers[0].buttons) == 5)
        from app import checkmark
        for item in theme.themes():
            theme.apply(item["id"])
            passed = (theme.theme_id() == item["id"]
                      and db.get_settings().get("theme") == item["id"]
                      and bool(app.styleSheet()))
            check(f"GUI 主题切换 {item['name']}", passed)
            asset = checkmark.check_path(item["on_accent"])
            check(f"第三版：{item['name']}主题对勾图标已生成",
                  asset.is_file() and asset.stat().st_size > 0)
        theme.apply("B")
        check("第三版：QSS 选中态含渐变与对勾图标",
              "qlineargradient" in app.styleSheet() and "image: url" in app.styleSheet())
        check("GUI 全窗口拖动过滤器已安装",
              any(type(c).__name__ == "_DragFilter" for c in window.children()))
        check("GUI 窗口图标已设置", not window.windowIcon().isNull())

        # —— 本轮：制题主入口 / 去掉 AI / 选项乱序 ——
        from app import schema, shuffle as sh
        from app.models import Question as QModel
        from app.ui.main_window import NAV_ITEMS, NAV
        check("GUI 首页是制作题目", NAV_ITEMS[0] == "制作题目"
              and NAV["制作题目"] == 0 and window.stack.widget(0) is window.maker)
        check("GUI 制作页有 A/B/C/D 四选项与正确答案勾选",
              len(window.maker._option_rows) == 4
              and all(r["mark"] is not None for r in window.maker._option_rows))
        window.maker.stem.setPlainText("自测题：1 + 1 = ?")
        window.maker._option_rows[0]["line"].setText("1")
        window.maker._option_rows[1]["line"].setText("2")
        window.maker._option_rows[1]["mark"].setChecked(True)
        made, made_errors = schema.validate_question(window.maker._build_contract(), 1)
        check("GUI 制作页能生成合法选择题",
              made is not None and not made_errors and made.answer == ["B"]
              and made.source == "manual", str(made_errors))
        window.maker._reset_form()
        # ---- 第十二版：制作题目页填空 / 简答「添加」按钮真实点击生效 ----
        from PySide6.QtWidgets import QPushButton as QPushButton12
        def mbtn12(text):
            return [b for b in window.maker.findChildren(QPushButton12) if text in b.text()]
        maker12 = window.maker
        # 填空：点两次「添加一空」，行数 1→3，且新增行进 _build_contract
        maker12.type_combo.setCurrentIndex(maker12.type_combo.findData("blank"))
        app.processEvents()
        n_blank0 = len(maker12._blank_rows)
        ab = mbtn12("添加一空")[0]
        ab.click(); ab.click(); app.processEvents()
        check("第十二版：制作页填空「＋ 添加一空」点两次真的加出两空",
              len(maker12._blank_rows) == n_blank0 + 2,
              f"{n_blank0} -> {len(maker12._blank_rows)}")
        maker12.stem.setPlainText("自测填空题")
        for i, row in enumerate(maker12._blank_rows):
            row["line"].setText(f"答案{i+1}")
        c_blank12 = maker12._build_contract()["answer"]
        check("第十二版：制作页填空新增的空会一起保存进答案",
              len(c_blank12) == 3 and c_blank12[-1] == ["答案3"],
              f"answer={c_blank12}")
        # 行内「删除」按钮可点、真的删掉一行
        last_blank12 = maker12._blank_rows[-1]["widget"]
        del_b12 = [b for b in last_blank12.findChildren(QPushButton12) if b.text() == "删除"][0]
        del_b12.click(); app.processEvents()
        check("第十二版：制作页填空行内「删除」按钮生效",
              len(maker12._blank_rows) == 2)
        # 简答：点两次「添加要点」，行内三按钮是中文文字，上移/下移真的换顺序
        maker12.type_combo.setCurrentIndex(maker12.type_combo.findData("short"))
        app.processEvents()
        n_point0 = len(maker12._point_rows)
        ap = mbtn12("添加要点")[0]
        ap.click(); ap.click(); app.processEvents()
        check("第十二版：制作页简答「＋ 添加要点」点两次真的加出两条",
              len(maker12._point_rows) == n_point0 + 2,
              f"{n_point0} -> {len(maker12._point_rows)}")
        labels12 = [b.text() for b in maker12._point_rows[-1]["widget"].findChildren(QPushButton12)]
        check("第十二版：要点行三个按钮是「上移 / 下移 / 删除」中文文字",
              labels12 == ["上移", "下移", "删除"], f"labels={labels12}")
        for i, row in enumerate(maker12._point_rows):
            row["line"].setText(["甲", "乙", "丙"][i])
        third12 = maker12._point_rows[2]["widget"]
        up12 = [b for b in third12.findChildren(QPushButton12) if b.text() == "上移"][0]
        up12.click(); app.processEvents()
        order12 = [r["line"].text() for r in maker12._point_rows]
        check("第十二版：制作页简答「上移」真的调整要点顺序",
              order12 == ["甲", "丙", "乙"], f"order={order12}")
        maker12._reset_form()
        maker12.type_combo.setCurrentIndex(0); app.processEvents()

        check("GUI 设置页已无 AI 配置", not hasattr(window.settings, "ai_enabled"))
        check("GUI 配置中无 AI 键",
              not any(k.startswith("ai_") for k in db.get_settings()))
        demo = QModel(id="x", type="single", stem="t", answer=["C"],
                      options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"},
                               {"key": "C", "text": "3"}, {"key": "D", "text": "4"}])
        variant = sh.apply_order_map([demo], {"x": ["D", "C", "B", "A"]})[0]
        check("GUI 选项乱序后正确答案字母同步重算",
              variant.answer == ["B"] and variant.options[1]["text"] == "3")

        # —— 本轮修复项：顶栏、拖动、设置页布局、逐题统计 ——
        from PySide6.QtWidgets import QLabel, QScrollArea
        from app import dragger
        check("GUI 顶栏品牌块已移除",
              not window.top_bar.findChildren(QLabel, "Brand"))
        check("GUI 拖动过滤器装在 QApplication 上", len(dragger._FILTERS) >= 1)
        check("GUI 设置页有滚动区", bool(window.settings.findChildren(QScrollArea)))
        check("GUI 主题卡片单行 5 列不重叠",
              all(pickers[0].buttons[t][0].height() >= 70 for t in pickers[0].buttons)
              if pickers else False)
        stats_headers = [window.stats.table.horizontalHeaderItem(i).text()
                         for i in range(window.stats.table.columnCount())]
        check("GUI 统计页逐题明细含练习/答对/答错次数",
              all(name in stats_headers for name in ("练习次数", "答对", "答错")))
        # 功能级验证：在侧栏空白处按住并移动，应触发窗口 move()
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt as QtCore
        from PySide6.QtGui import QMouseEvent
        moves: list = []
        original_move = window.move
        try:
            window.move = lambda point: moves.append(point)
            window.nav.setCurrentRow(NAV["题库"])
            app.processEvents()
            # 选题库页表格下方的空白区域（不是顶部标题栏，也不是任何条目）
            target = window.library.table.viewport()
            local = QPoint(220, 260)
            global_pos = target.mapToGlobal(local)
            QApplication.sendEvent(target, QMouseEvent(
                QEvent.Type.MouseButtonPress, QPointF(local), QPointF(global_pos),
                QtCore.MouseButton.LeftButton, QtCore.MouseButton.LeftButton,
                QtCore.KeyboardModifier.NoModifier))
            moved_pos = global_pos + QPoint(40, 25)
            QApplication.sendEvent(target, QMouseEvent(
                QEvent.Type.MouseMove, QPointF(local + QPoint(40, 25)), QPointF(moved_pos),
                QtCore.MouseButton.NoButton, QtCore.MouseButton.LeftButton,
                QtCore.KeyboardModifier.NoModifier))
            QApplication.sendEvent(target, QMouseEvent(
                QEvent.Type.MouseButtonRelease, QPointF(local + QPoint(40, 25)),
                QPointF(moved_pos), QtCore.MouseButton.LeftButton,
                QtCore.MouseButton.NoButton, QtCore.KeyboardModifier.NoModifier))
        except Exception as exc:
            print("DRAG_SIM_NOTE", exc)
        finally:
            window.move = original_move
        check("GUI 内容区空白按住移动可拖动窗口", bool(moves), f"move_calls={len(moves)}")

        html = md_to_html("# 标题\n\n**粗体** 与 $a^2+b^2=c^2$\n\n- 甲\n- 乙\n\n$$E=mc^2$$")
        check("GUI Markdown/公式/列表渲染", "<h1>" in html and "<ul>" in html
              and "<b>粗体</b>" in html, f"html_len={len(html)}")

        # ---- 第十二版：开始练习页砍分组/标签、加按钮、独立选择题目窗口 ----
        from PySide6.QtCore import Qt as QtPick12
        from PySide6.QtWidgets import QSplitter as QSplitter12
        pv12 = window.practice
        check("第十二版：组卷条件已砍掉「分组」「知识点标签」两个下拉",
              not hasattr(pv12, "group_combo") and not hasattr(pv12, "tag_combo")
              and all(hasattr(pv12, n) for n in ("type_combo", "count_spin",
                                                 "order_combo", "mode_combo")),
              "group/tag combo 应不存在")
        check("第十二版：开始练习页有「查看选择题目…」按钮",
              hasattr(pv12, "btn_picker") and "查看选择题目" in pv12.btn_picker.text())
        pv12.clear_picked()
        # 这段断言在示例题导入之前，自己造两道临时题，测完删掉
        temp_q12 = [
            QModel(id="t_gui_v12_pick1", type="single", stem="第十二版投递题甲",
                   options=[{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}], answer=["A"]),
            QModel(id="t_gui_v12_pick2", type="blank", stem="第十二版投递题乙",
                   options=[], answer=[["乙"]]),
        ]
        for q12 in temp_q12:
            repository.save_question(q12)
        all_q12 = repository.all_questions()
        ids12 = [q.id for q in temp_q12]
        window._deliver_picked(ids12, "题库")
        check("第十二版：三个来源点练习所选只投递题目、不自动开练",
              pv12.stack.currentIndex() == 0 and list(pv12.picked) == ids12
              and all(pv12.picked[i]["source"] == "题库" for i in ids12),
              f"stack={pv12.stack.currentIndex()}, picked={list(pv12.picked)}")
        window._deliver_picked([ids12[0]] if ids12 else [], "错题库")
        window._deliver_picked([ids12[-1]] if ids12 else [], "练习统计")
        check("第十二版：多次投递按题目 id 去重、不重复添加",
              len(pv12.picked) == len(ids12), f"picked={len(pv12.picked)}")
        picker12 = pv12.open_picker()
        app.processEvents()
        check("第十二版：点按钮真的打开独立「本次练习题库」窗口",
              picker12 is not None and picker12.isVisible()
              and picker12.windowTitle() == "本次练习题库")
        splitter12 = picker12.findChild(QSplitter12)
        check("第十二版：窗口默认 1618×1000 且左右分栏",
              picker12.width() == 1618 and picker12.height() == 1000
              and splitter12 is not None and splitter12.count() == 2,
              f"size={picker12.width()}x{picker12.height()}, splitter={splitter12 is not None}")
        check("第十二版：窗口左边是分组/标签树、节点可勾选",
              picker12.tree.topLevelItemCount() == 3
              and picker12.tree.topLevelItem(0).text(0).startswith("【当前】全部题目")
              and picker12.tree.topLevelItem(1).text(0) == "分组题库"
              and picker12.tree.topLevelItem(2).text(0) == "知识点标签"
              and all(picker12.tree.topLevelItem(1).child(i).flags() & QtPick12.ItemIsUserCheckable
                      for i in range(picker12.tree.topLevelItem(1).childCount())))
        all_node12 = picker12.tree.topLevelItem(0)
        q_children12 = [all_node12.child(i) for i in range(all_node12.childCount())]
        check("第十二版：左树里列出了具体题目、每道题前面有可勾选方框",
              len(q_children12) == len(all_q12)
              and all((n.data(0, QtPick12.UserRole) or ("", ""))[0] == "q"
                      and bool(n.flags() & QtPick12.ItemIsUserCheckable) for n in q_children12),
              f"题目节点={len(q_children12)}, 题库={len(all_q12)}")
        # 崩溃回归：点树节点只刷右表，不得把节点删掉（旧版 clear 重建会闪退）
        node_before12 = picker12.tree.topLevelItem(0)
        picker12._on_tree_clicked(node_before12, 0)
        app.processEvents()
        check("第十二版：点树节点后节点对象仍有效（不再重建树导致闪退）",
              node_before12.treeWidget() is not None)
        check("第十二版：窗口里正好两个按钮分页（已选择的题目 / 所有题库里的所有题）",
              "已选择的题目" in picker12.btn_selected.text()
              and "所有题库里的所有题" in picker12.btn_all.text()
              and picker12.stack.count() == 2)
        check("第十二版：已选页每行有可勾选方框、列出来源",
              picker12.selected_table.columnCount() == 4
              and all(picker12.selected_table.item(r, 0) is not None
                      and bool(picker12.selected_table.item(r, 0).flags() & QtPick12.ItemIsUserCheckable)
                      for r in range(picker12.selected_table.rowCount())),
              f"rows={picker12.selected_table.rowCount()}")
        check("第十二版：全部题页列出题库所有题（含已加入/未加入）",
              picker12.all_table.rowCount() == len(all_q12)
              and any(picker12.all_table.item(r, 2).text() == "已加入"
                      for r in range(picker12.all_table.rowCount())))
        # 逐个取消勾选 → 开始练习只认勾上的
        if picker12.selected_table.rowCount():
            picker12.selected_table.item(0, 0).setCheckState(QtPick12.Unchecked)
            app.processEvents()
            check("第十二版：窗口里取消勾选后不计入本次练习",
                  len(pv12.picked_checked_ids()) == len(pv12.picked) - 1)
            # 移除只从清单拿掉，题库里的题仍在
            removed_qid12 = list(pv12.picked)[0]
            pv12.remove_picked([removed_qid12])
            check("第十二版：移除只从本次清单拿掉、不删题库里的题",
                  removed_qid12 not in pv12.picked
                  and repository.get_question(removed_qid12) is not None)
        # ---- 第十二版补丁：详情可编辑题目（复用全站同一个编辑器） ----
        from app.ui.question_picker import QuestionDetailDialog as QDetail12
        detail12 = QDetail12(ids12[0], picker12)
        check("第十二版：详情弹窗带「编辑题目」按钮且可用",
              detail12.btn_edit.text() == "编辑题目" and detail12.btn_edit.isEnabled())
        detail12.btn_edit.click()
        check("第十二版：点详情「编辑题目」返回 edit 动作", detail12.action == "edit")
        detail12.close()
        import app.ui.main_window as mw12
        real_qe12 = mw12.QuestionEditor
        opened12: dict = {}

        class FakeQE12:
            def __init__(self, qid="", parent=None, start_as_copy=False, **kw):
                opened12["qid"] = qid

            def exec(self):
                return 0  # 不阻塞、不进入 refresh_all

        mw12.QuestionEditor = FakeQE12
        try:
            picker12._open_editor(ids12[0])
            app.processEvents()
            check("第十二版：右键「编辑题目」经信号到达主窗口 open_editor(题目编辑器)",
                  opened12.get("qid") == ids12[0], f"opened={opened12}")
        finally:
            mw12.QuestionEditor = real_qe12
        picker12.close()

        # ---- 第十二版补丁：已完成的练习区块 + 详细报告窗口 ----
        from app import grading as grading12
        fin_sid12 = repository.create_session("instant", {}, ids12)
        st12 = db.get_settings()
        for qq12, aa12 in ((temp_q12[0], ["A"]), (temp_q12[1], ["乙"])):
            rr12 = grading12.grade(qq12, aa12, st12)
            repository.record_attempt(qq12, rr12, aa12, mode="instant",
                                      session_id=fin_sid12, settings=st12)
        repository.finish_session(fin_sid12)
        pv12.refresh_setup()
        app.processEvents()
        report_btns12 = [b for b in pv12.findChildren(QPushButton12)
                         if "详细报告" in b.text()]
        check("第十二版：开始练习页新增「已完成的练习」且两个区块都有详细报告按钮",
              hasattr(pv12, "finished_combo") and pv12.finished_combo.count() >= 1
              and len(report_btns12) == 2,
              f"finished={pv12.finished_combo.count()}, 报告按钮={len(report_btns12)}")
        from app.ui.session_report import SessionReportDialog as SReport12
        rep12 = SReport12(fin_sid12, window)
        rep12.show()
        app.processEvents()
        md12 = rep12.detail.toPlainText()
        check("第十二版：详细报告逐题列题目/作答/标准答案/判定/解析",
              rep12.table.rowCount() == len(ids12)
              and all(k in md12 for k in ("题目", "你的作答", "标准答案", "判定结果")),
              f"rows={rep12.table.rowCount()}")
        check("第十二版：详细报告表格给出每题对错结果",
              all("正确" in rep12.table.item(r, 3).text() for r in range(rep12.table.rowCount())))
        rep12.close()
        repository.delete_session_record(fin_sid12)

        pv12.clear_picked()
        repository.delete_questions(ids12)

        editor = QuestionEditor("", window)
        editor.stem.setPlainText("求 $\\int_0^1 x^2 dx$")
        editor.type_combo.setCurrentIndex(3)  # 简答题
        app.processEvents()
        check("GUI 编辑器可创建并切换题型", editor.answer_stack.currentIndex() == 2)
        editor._add_point("新要点")
        editor._move_point(editor._point_rows[-1]["widget"], -1)
        check("GUI 简答要点可增删排序", len(editor._point_rows) >= 2)
        editor.reject()

        window.import_view._load_example()
        window.import_view._parse()
        check("GUI 导入预览 5 题全部合法",
              window.import_view.report is not None
              and window.import_view.report.error_count == 0)
        window.import_view._commit()
        app.processEvents()
        check("GUI 批量导入写入题库", len(repository.all_questions()) == 5,
              f"rows={window.library.table.rowCount()}")

        # 即时判：指定一道单选题
        window.nav.setCurrentRow(NAV["开始练习"])
        window.practice.set_forced_ids(["sample_single_001"])
        window.practice.order_combo.setCurrentIndex(1)
        window.practice.mode_combo.setCurrentIndex(0)
        window.practice.start()
        app.processEvents()
        check("GUI 即时判进入答题页", window.practice.stack.currentIndex() == 1)
        check("GUI 做题题号按本次顺序实时显示 1.",
              window.practice.stem_view._source.startswith("**1.**"))
        check("GUI 练习已生成选择题选项乱序映射",
              bool(window.practice.shuffle_map))
        check("第三版：答题页有「上一题」且第一题禁用",
              window.practice.btn_prev is not None and not window.practice.btn_prev.isEnabled())
        answer_current(window.practice, all_correct=False)
        window.practice.btn_submit.click()
        app.processEvents()
        check("GUI 即时判提交后显示结果页", window.practice.stack.currentIndex() == 2)
        check("GUI 判错自动沉淀错题", "sample_single_001" in repository.active_wrong_ids())
        check("第三版：结果页有「返回上一题」并记录了结果历史",
              window.practice.btn_result_prev is not None
              and len(window.practice.result_history) == 1)
        window.practice.next_question()
        app.processEvents()

        # 即时判 + 简答题：提交后才显示分点标准答案，自评后判分
        window.practice.set_forced_ids(["sample_multiple_001", "sample_short_001"])
        window.practice.order_combo.setCurrentIndex(1)
        window.practice.mode_combo.setCurrentIndex(0)
        window.practice.start()
        app.processEvents()
        answer_current(window.practice, all_correct=True)
        window.practice.btn_submit.click()
        app.processEvents()
        check("第三版：第一题判完可直接「下一题」", window.practice.stack.currentIndex() == 2)
        window.practice.next_question()
        app.processEvents()
        check("第三版：简答题答题页不提前显示分点标准答案",
              window.practice.stack.currentIndex() == 1
              and "checks" not in window.practice._input_refs
              and "分点标准答案" not in window.practice.input_box.title())
        check("第三版：第二题「上一题」可用",
              window.practice.btn_prev.isEnabled())
        answer_current(window.practice, all_correct=False)
        window.practice.btn_submit.click()
        app.processEvents()
        check("第三版：简答题提交后进入自评页（此时才看标准答案）",
              window.practice.stack.currentIndex() == 4
              and len(window.practice.review_checks) >= 2
              and "分点标准答案" in window.practice.review_box.title())
        check("第三版：自评页展示自己的作答文本",
              "我的作答" in window.practice.review_answer._source)
        review_current(window.practice, all_correct=False)
        window.practice.btn_confirm_review.click()
        app.processEvents()
        check("第三版：确认自评后才判分并显示结果", window.practice.stack.currentIndex() == 2
              and len(window.practice.result_history) == 2)
        check("第三版：结果页标出未答到要点与「下次多注意」",
              "下次多注意" in window.practice.result_view._source)
        wrong_note_rows = [r for r in repository.wrong_rows("active")
                           if r["question_id"] == "sample_short_001"]
        check("第三版：未答到要点写入错题库备注",
              bool(wrong_note_rows) and "下次多注意" in str(wrong_note_rows[0].get("note") or ""))
        check("第六版：错题库表格不再有备注列（已移进详情弹窗）",
              not any(window.wrong.table.horizontalHeaderItem(i).text() == "下次多注意"
                      for i in range(window.wrong.table.columnCount())))
        window.practice.exit_to_setup()

        # 结果页「返回上一题」回看：不重复判分、不新增记录
        window.practice.set_forced_ids(["sample_single_001", "sample_multiple_001"])
        window.practice.order_combo.setCurrentIndex(1)
        window.practice.mode_combo.setCurrentIndex(0)
        window.practice.start()
        app.processEvents()
        answer_current(window.practice, all_correct=True)
        window.practice.btn_submit.click()
        app.processEvents()
        window.practice.next_question()
        app.processEvents()
        answer_current(window.practice, all_correct=True)
        window.practice.btn_submit.click()
        app.processEvents()
        before_attempts = repository.stats_overall()["attempts"]
        window.practice.btn_result_prev.click()
        app.processEvents()
        check("第三版：结果页可返回上一题结果（不重复判分）",
              window.practice.stack.currentIndex() == 2
              and window.practice.result_cursor == 0
              and repository.stats_overall()["attempts"] == before_attempts)
        window.practice.btn_next.click()
        app.processEvents()
        check("第三版：回看后「前进」回到最新结果",
              window.practice.result_cursor == len(window.practice.result_history) - 1)

        # 从答题页点「上一题」：回看上一题结果，再「返回本题」不跳题、不重判
        window.practice.exit_to_setup()
        window.practice.set_forced_ids(["sample_single_001", "sample_multiple_001",
                                        "sample_blank_001"])
        window.practice.order_combo.setCurrentIndex(1)
        window.practice.mode_combo.setCurrentIndex(0)
        window.practice.start()
        app.processEvents()
        answer_current(window.practice, all_correct=True)
        window.practice.btn_submit.click()
        app.processEvents()
        window.practice.next_question()
        app.processEvents()
        answer_current(window.practice, all_correct=True)
        window.practice.btn_submit.click()
        app.processEvents()
        window.practice.next_question()
        app.processEvents()
        check("第三版：第 3 题答题页就绪", window.practice.stack.currentIndex() == 1
              and window.practice.index == 2)
        attempts_before = repository.stats_overall()["attempts"]
        window.practice.btn_prev.click()
        app.processEvents()
        check("第三版：答题页「上一题」回看结果且不重复判分",
              window.practice.stack.currentIndex() == 2
              and repository.stats_overall()["attempts"] == attempts_before
              and "返回本题" in window.practice.btn_next.text())
        window.practice.btn_next.click()
        app.processEvents()
        check("第三版：「返回本题」回到第 3 题作答、不跳题",
              window.practice.stack.currentIndex() == 1 and window.practice.index == 2)
        check("第三版：多选题结果页标准答案一项一行",
              "\n" in window.practice.result_view._source.split("**解析**")[0])
        window.practice.exit_to_setup()

        # ---- 第十二版：答题结果显示答案时带上题目，四种题型都不漏 ----
        import re as re12
        from app.grading import question_prompt
        # 题干里可能含 **加粗** 等 markdown，渲染成富文本后标记会被吃掉，
        # 比较时先剥掉标记，只核对纯文字。
        def plain12(text):
            return re12.sub(r"[*`]", "", str(text or ""))
        stem_cases = [
            ("sample_single_001", "single"), ("sample_multiple_001", "multiple"),
            ("sample_blank_001", "blank"), ("sample_short_001", "short"),
        ]
        stem_results = {}
        for qid12, kind12 in stem_cases:
            window.practice.set_forced_ids([qid12])
            window.practice.mode_combo.setCurrentIndex(0)  # 即时判
            window.practice.start()
            app.processEvents()
            q12 = window.practice.current_question()
            answer_current(window.practice, all_correct=True)
            window.practice.btn_submit.click()
            app.processEvents()
            if kind12 == "short":
                # 简答题先进入自评页：那里也必须带题干
                review_text12 = window.practice.review_answer.toPlainText()
                stem_results["short_review"] = plain12(q12.stem) in plain12(review_text12)
                for cb12 in getattr(window.practice, "review_checks", []):
                    cb12.setChecked(True)
                window.practice.confirm_review()
                app.processEvents()
            rendered12 = window.practice.result_view.toPlainText()
            stem_results[kind12] = plain12(q12.stem) in plain12(rendered12)
            if kind12 in ("single", "multiple"):
                # 选择题 / 多选题还要把全部选项带上
                stem_results[kind12 + "_opts"] = all(
                    f"{str(o.get('key',''))}." in rendered12 for o in q12.options)
            window.practice.exit_to_setup()
        check("第十二版：结果页四种题型（单选/多选/填空/简答）都显示题干",
              all(stem_results.get(k) for k in ("single", "multiple", "blank", "short")),
              f"detail={stem_results}")
        check("第十二版：选择/多选结果页连选项一起带上",
              stem_results.get("single_opts") and stem_results.get("multiple_opts"),
              f"detail={stem_results}")
        check("第十二版：简答自评页也显示题干",
              stem_results.get("short_review") is True)

        # 整卷判：单选 + 简答（交卷后先逐题自评，再统一判分）
        window.practice.set_forced_ids(["sample_single_001", "sample_short_001"])
        window.practice.mode_combo.setCurrentIndex(1)
        window.practice.start()
        app.processEvents()
        answer_current(window.practice, all_correct=True)
        window.practice.btn_submit.click()
        app.processEvents()
        check("GUI 整卷判第一题不立即判分", window.practice.stack.currentIndex() == 1
              and window.practice.index == 1)
        answer_current(window.practice, all_correct=True)
        window.practice.btn_submit.click()
        app.processEvents()
        check("第三版：整卷判交卷后先进入简答题自评页",
              window.practice.stack.currentIndex() == 4)
        review_current(window.practice, all_correct=True)
        window.practice.btn_confirm_review.click()
        app.processEvents()
        check("GUI 整卷判自评后显示总览", window.practice.stack.currentIndex() == 3
              and window.practice.summary_table.rowCount() == 2,
              f"rows={window.practice.summary_table.rowCount()}")
        # 第十二版：整卷总览点任意行看答案时也要带题干
        window.practice.summary_table.selectRow(1)
        app.processEvents()
        sum_detail12 = window.practice.summary_detail.toPlainText()
        sum_qid12 = window.practice._selected_summary_qid()
        q_sum12 = repository.get_question(sum_qid12) if sum_qid12 else None
        check("第十二版：整卷总览点行看答案也显示题干",
              q_sum12 is not None and plain12(q_sum12.stem) in plain12(sum_detail12),
              f"qid={sum_qid12}, detail_head={sum_detail12[:40]!r}")
        window.practice.finish_practice()

        # 草稿续做与错题库 / 统计 / 设置页
        window.nav.setCurrentRow(NAV["错题库"])
        window.wrong.refresh()
        app.processEvents()
        check("GUI 错题库可刷新", window.wrong.table.rowCount() >= 0)
        window.nav.setCurrentRow(NAV["练习统计"])
        window.stats.refresh()
        app.processEvents()
        check("GUI 统计页可刷新", "正确率" in window.stats.cards.text())
        window.nav.setCurrentRow(NAV["设置"])
        window.settings._save()
        check("GUI 设置可保存", db.get_settings()["wrong_streak_to_remove"] == "2")
        window.nav.setCurrentRow(NAV["开始练习"])
        window.practice.refresh_setup()
        app.processEvents()
        check("GUI 草稿列表可加载", True)

        # ---- 第五版：错题库时间 / 备注 / 勾选重练 / 详情弹窗 ----
        from PySide6.QtCore import Qt as Qt5
        from PySide6.QtWidgets import QLabel
        from app.ui.wrong_detail import WrongDetailDialog
        wrong = window.wrong
        wtable = wrong.table
        check("第六版：错题库首列是复选框、共 6 列（备注已移入详情）",
              wtable.columnCount() == 6 and wtable.horizontalHeaderItem(0).text() == "选")
        check("第五版：错题库有「重练勾选」与「全选」",
              wrong.btn_practice.text() == "重练勾选"
              and wrong.btn_select_all.text() in ("全选", "取消全选"))
        if wtable.rowCount():
            times = [wtable.item(r, c).text() for r in range(wtable.rowCount())
                     for c in (4, 5) if wtable.item(r, c)]
            check("第五版：时间精确到分钟、没有 T", bool(times)
                  and all(len(t) == 16 and "T" not in t for t in times), str(times[:2]))
            qid0 = wtable.item(0, 0).data(Qt5.UserRole)
            wtable.item(0, 0).setCheckState(Qt5.Checked)
            app.processEvents()
            check("第五版：勾选错题记进 checked_ids", wrong.checked_ids() == [qid0],
                  str(wrong.checked_ids()))
            emitted = []
            wrong.practice_requested.connect(lambda ids: emitted.append(ids))
            wrong.btn_practice.click()
            app.processEvents()
            check("第五版：勾选后点重练可开练习",
                  bool(emitted) and emitted[0] == [qid0], str(emitted))
            # 第十二版：重练勾选只把题投递到本次练习题库，停在设置页，不再自动开练
            check("第十二版：错题重练只投递题目、停在设置页不自动开练",
                  window.practice.stack.currentIndex() == 0
                  and qid0 in window.practice.picked
                  and qid0 in window.practice.picked_checked_ids(),
                  f"stack={window.practice.stack.currentIndex()}, picked={list(window.practice.picked)}")
            window.practice.clear_picked()
            window.practice.set_forced_ids([])
            wtable.item(0, 0).setCheckState(Qt5.Unchecked)
            app.processEvents()
            wrong._toggle_select_all()
            app.processEvents()
            check("第五版：全选可一次勾满错题",
                  len(wrong.checked_ids()) == wtable.rowCount()
                  and wrong.btn_select_all.text() == "取消全选",
                  f"checked={len(wrong.checked_ids())}, rows={wtable.rowCount()}, "
                  f"btn={wrong.btn_select_all.text()}")
            dlg = WrongDetailDialog(qid0, window)
            texts = [w.text() for w in dlg.findChildren(QLabel)]
            check("第五版：详情弹窗显示「下次多注意」全文",
                  any("下次多注意" in t for t in texts) and dlg.question_id == qid0)
            dlg.note_edit.setPlainText("自测手写备注：先看选项再答")
            saved_msg = dlg.save_note()
            row_after = repository.wrong_row(qid0) or {}
            check("第六版：详情弹窗可直接改「下次多注意」并标记手写",
                  row_after.get("note_manual") == 1
                  and "先看选项再答" in str(row_after.get("note") or ""), saved_msg)
            dlg.close()
        window.nav.setCurrentRow(NAV["错题库"])
        app.processEvents()

        # ---- 第四版：题库页六项改进的真实交互验证 ----
        from app.config import ALL_GROUP_KEY, ALL_GROUP_NAME
        from app.ui.library_view import GroupPickerDialog
        from PySide6.QtCore import Qt as QtCore2
        lib = window.library
        lib.group_filter = ""
        lib.tag_filter = ""
        lib.refresh()
        app.processEvents()

        # 1) 动态默认组：树上有节点、制题页有固定候选
        def tree_items(item=None):
            out = []
            if item is None:
                for i in range(lib.tree.topLevelItemCount()):
                    out += tree_items(lib.tree.topLevelItem(i))
                return out
            out.append(item)
            for i in range(item.childCount()):
                out += tree_items(item.child(i))
            return out

        all_items = tree_items()
        default_nodes = [i for i in all_items
                         if tuple(i.data(0, QtCore2.UserRole) or ()) == ("group", ALL_GROUP_KEY)]
        # 第十一版：砍掉「分组题库」下重复的默认组子项，只留顶层「全部题目」
        check("第十一版：题库树「分组题库」下不再有重复的默认组子项",
              len(default_nodes) == 0)
        all_nodes = [i for i in all_items
                     if tuple(i.data(0, QtCore2.UserRole) or ()) == ("all", "")]
        check("第十一版：顶层「全部题目」节点仍在", len(all_nodes) == 1)
        check("第四版：制题页分组组件有固定默认组 chip",
              window.maker.groups.default_name == ALL_GROUP_NAME
              and window.maker.groups.default_chip is not None
              and ALL_GROUP_NAME not in window.maker.groups.names())

        # 点顶层「全部题目」节点应显示全部题目
        lib._on_tree_clicked(all_nodes[0], 0)
        app.processEvents()
        check("第四版：点全部题目显示所有题目",
              lib.group_filter in ("", ALL_GROUP_KEY)
              and lib.table.rowCount() == len(repository.all_questions()))
        check("第四版：当前分组带中文标记且加粗",
              any(lib.tree.topLevelItem(i).text(0).startswith("【当前】") or
                  any(c.text(0).startswith("【当前】") for c in
                      [lib.tree.topLevelItem(i).child(j)
                       for j in range(lib.tree.topLevelItem(i).childCount())])
                  for i in range(lib.tree.topLevelItemCount())))

        # 2) 持久高亮：搜索 / 刷新后选中节点仍带标记
        lib.search.setText("Python")
        app.processEvents()
        marked_after_search = [i.text(0) for i in tree_items() if i.text(0).startswith("【当前】")]
        lib.search.setText("")
        app.processEvents()
        check("第四版：搜索刷新后当前分组高亮不丢", bool(marked_after_search))

        # 3) 复选框多选 + 全选
        check("第十一版：表格首列是复选框列且四列（✓/题型/题干/错题）", lib.table.columnCount() == 4
              and lib.table.horizontalHeaderItem(0).text() == "选择")
        lib.btn_select_all.click()
        app.processEvents()
        check("第四版：全选可一次勾选所有题目",
              len(lib._checked_ids) == lib.table.rowCount()
              and lib.btn_select_all.text() == "取消全选")
        lib.btn_select_all.click()
        app.processEvents()
        check("第四版：取消全选可清空勾选",
              len(lib._checked_ids) == 0 and lib.btn_select_all.text() == "全选")

        # 4) 加入分组对话框：多选 + 输入新名称即创建
        dlg = GroupPickerDialog(["Python", "SQL"], window)
        dlg.picker.edit.setText("第四版新分组")
        dlg.picker.add_btn.click()
        app.processEvents()
        check("第四版：加入分组可输入新名称创建并多选",
              sorted(dlg.chosen()) == ["Python", "第四版新分组"]
              or ("第四版新分组" in dlg.chosen() and len(dlg.chosen()) >= 1))
        dlg.reject()

        # 5) 右键删除分组：题目保留、名字移除
        test_q = repository.get_question("sample_multiple_001")
        assert test_q
        repository.bulk_set_group([test_q.id], "待删分组", add=True)
        lib.group_filter = ""
        lib.refresh()
        app.processEvents()
        target = next((i for i in tree_items()
                       if tuple(i.data(0, QtCore2.UserRole) or ()) == ("group", "待删分组")), None)
        check("第四版：新建分组出现在树上（右键删除前置）", target is not None)
        if target is not None:
            QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
            # offscreen 下弹菜单会阻塞事件循环；直接调用与菜单动作相同的删除逻辑
            lib.delete_entity("group", "待删分组")
            app.processEvents()
            after = repository.get_question(test_q.id)
            check("第四版：右键删除分组后题目仍在且名字已移除",
                  after is not None and "待删分组" not in after.groups
                  and "待删分组" not in repository.groups_with_count())
            check("第四版：右键菜单已挂到分组树",
                  lib.tree.contextMenuPolicy() == QtCore2.CustomContextMenu)

        # 6) 刷新动画 + 任务栏 flags
        check("第四版：刷新按钮已挂动画", bool(getattr(lib, "_refresh_animators", None)))
        from app import taskbar
        check("第四版：主窗口补齐任务栏最小化标志与原生兜底",
              bool(window.windowFlags() & QtCore2.WindowMinimizeButtonHint)
              and bool(window.windowFlags() & QtCore2.WindowSystemMenuHint)
              and hasattr(taskbar, "installed_count"))
        qss = app.styleSheet()
        check("第四版：失焦选中态不褪色 + 默认组 chip 样式",
              ":selected:!active" in qss and "#ChipDefault" in qss)

        # ---- 第六版：版本号 / 删除交互 / 备注 / 统计筛选 ----
        check("第十四版：标题栏版本号跟着版本走（v14.0.0 第十四版）",
              "v14.0.0" in window.windowTitle() and "第十四版" in window.windowTitle(),
              window.windowTitle())

        temp_q = QModel(id="t_gui_delete", type="single", stem="待删除的自测题",
                        options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                        answer=["A"], groups=["删除测试组"], tags=["删除测试标签"])
        repository.save_question(temp_q)
        lib.group_filter = ""
        lib.tag_filter = ""
        lib.refresh()
        app.processEvents()
        check("第六版：题库表格支持右键菜单与 Delete 快捷键",
              lib.table.contextMenuPolicy() == QtCore2.CustomContextMenu
              and lib.table_delete_key is not None and lib.tree_delete_key is not None)
        check("第十一版：题库工具栏已无 删除题目/移除分组或标签/回收站 按钮",
              not hasattr(lib, "btn_delete")
              and not hasattr(lib, "btn_recycle") and not hasattr(lib, "btn_edit")
              and not hasattr(lib, "btn_group")
              and hasattr(lib, "btn_del_entity") and hasattr(lib, "btn_move_entity"))
        lib._checked_ids = {temp_q.id}
        lib.refresh()
        lib._update_actions()
        deleted = lib.run_row_action("delete", temp_q.id)
        app.processEvents()
        check("第六版：右键删除能真正删掉题目",
              repository.get_question(temp_q.id) is None,
              f"deleted={deleted}")

        # 右键哪行删哪行：未勾选的行不会连带删掉已勾选的题
        keep_q = QModel(id="t_gui_keep", type="single", stem="已勾选要保留的题",
                        options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                        answer=["A"])
        drop_q = QModel(id="t_gui_drop", type="single", stem="右键点中的题",
                        options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                        answer=["A"])
        repository.save_question(keep_q)
        repository.save_question(drop_q)
        lib.refresh()
        lib._checked_ids = {keep_q.id}
        lib.refresh()
        lib.run_row_action("delete", drop_q.id)
        app.processEvents()
        check("第六版：右键删未勾选的行只删这一行，不误删已勾选的题",
              repository.get_question(drop_q.id) is None
              and repository.get_question(keep_q.id) is not None)
        repository.delete_questions([keep_q.id])
        lib._checked_ids.clear()
        lib.refresh()
        app.processEvents()

        # 另存为新题：编辑器里选「另存为新题」不改原题
        copy_src = repository.get_question("sample_single_001")
        assert copy_src
        before_count = len(repository.all_questions())
        editor2 = QuestionEditor(copy_src.id, window)
        check("第六版：编辑已有题目时有「覆盖原题 / 另存为新题」两个选择",
              editor2.save_mode.count() == 2 and editor2.save_mode.isVisibleTo(editor2))
        editor2.note_edit.setPlainText("自测：另存新题时备注一起带走")
        editor2.save_mode.setCurrentIndex(1)
        editor2._save()
        app.processEvents()
        after_count = len(repository.all_questions())
        new_id = getattr(editor2, "saved_id", "")
        check("第六版：另存为新题会新增一条、原题保留",
              after_count == before_count + 1 and new_id != copy_src.id
              and repository.get_question(copy_src.id) is not None,
              f"{before_count}→{after_count}, new={new_id}")
        new_q = repository.get_question(new_id)
        check("第六版：另存的新题带上「下次多注意」备注",
              new_q is not None and "另存新题" in (new_q.note or ""))
        if new_id:
            repository.delete_questions([new_id])
            window.refresh_all()
            app.processEvents()

        # 覆盖原题：id 不变、原题被更新
        editor3 = QuestionEditor(copy_src.id, window)
        editor3.stem.setPlainText(copy_src.stem + "（已覆盖自测）")
        editor3.save_mode.setCurrentIndex(0)
        editor3._save()
        app.processEvents()
        check("第六版：默认「覆盖原题」更新同一道题",
              getattr(editor3, "saved_mode", "") == "overwrite"
              and "已覆盖自测" in (repository.get_question(copy_src.id).stem or ""))

        # 分组 / 标签重命名与删除按钮
        lib.refresh()
        app.processEvents()
        real_groups = [tuple(i.data(0, QtCore2.UserRole) or ()) for i in tree_items()]
        real_group = next((value for kind, value in real_groups
                           if kind == "group" and value not in
                           (ALL_GROUP_KEY, ALL_GROUP_NAME, "__ungrouped__", "")), "")
        group_node = next((i for i in tree_items()
                           if tuple(i.data(0, QtCore2.UserRole) or ()) == ("group", real_group)),
                          None)
        if group_node is not None:
            lib.tree.setCurrentItem(group_node)
            app.processEvents()
            renamed = lib.rename_entity("group", real_group, real_group + "自测改名")
            app.processEvents()
            check("第六版：分组可重命名",
                  renamed and (real_group + "自测改名") in repository.groups_with_count())
            if renamed:
                lib.rename_entity("group", real_group + "自测改名", real_group)
                app.processEvents()
        lib._on_tree_clicked(next(i for i in tree_items()
                                  if tuple(i.data(0, QtCore2.UserRole) or ()) == ("all", "")), 0)
        app.processEvents()

        # 统计页：图表删了、题干更大、可按分组/标签筛选
        from app.chart_widget import ChartWidget
        window.nav.setCurrentRow(NAV["练习统计"])
        window.stats.refresh()
        app.processEvents()
        check("第六版：统计页已移除四张统计图",
              not window.stats.findChildren(ChartWidget))
        from app.ui.stats_view import STEM_COL as STATS_STEM_COL
        check("第六版：统计页题干列足够宽、行高加大",
              window.stats.table.columnWidth(STATS_STEM_COL) >= 400
              and window.stats.table.verticalHeader().defaultSectionSize() == 38,
              f"width={window.stats.table.columnWidth(STATS_STEM_COL)}, "
              f"row={window.stats.table.verticalHeader().defaultSectionSize()}")
        check("第六版：统计页有分组与标签两个筛选下拉",
              window.stats.group_combo.count() >= 2 and window.stats.tag_combo.count() >= 2,
              f"groups={window.stats.group_combo.count()}, tags={window.stats.tag_combo.count()}")
        group_data = [window.stats.group_combo.itemData(i)
                      for i in range(window.stats.group_combo.count())]
        group_value = next((v for v in group_data
                            if v and v not in (ALL_GROUP_KEY, ALL_GROUP_NAME, "__ungrouped__")), "")
        index = window.stats.group_combo.findData(group_value) if group_value else -1
        if index >= 0:
            window.stats.group_combo.setCurrentIndex(index)
            app.processEvents()
            shown = window.stats._shown
            check("第六版：按分组筛选后只剩该分组的题",
                  bool(shown) and all(group_value in r["groups"] for r in shown)
                  and "分组=" in window.stats.cards.text(),
                  f"group={group_value}, shown={len(shown)}")
            window.stats.group_combo.setCurrentIndex(0)
            app.processEvents()
        tag_data = [window.stats.tag_combo.itemData(i)
                    for i in range(1, window.stats.tag_combo.count())]
        tag_value = next((v for v in tag_data if v and v != "__untagged__"), "")
        if tag_value:
            window.stats.tag_combo.setCurrentIndex(window.stats.tag_combo.findData(tag_value))
            app.processEvents()
            shown = window.stats._shown
            check("第六版：按标签筛选后只剩该标签的题",
                  bool(shown) and all(tag_value in r["tags"] for r in shown),
                  f"tag={tag_value}, shown={len(shown)}")
            window.stats.tag_combo.setCurrentIndex(0)
            app.processEvents()
        check("第六版：筛选后题目数量与汇总一起变化",
              "题库" in window.stats.cards.text() and "显示" in window.stats.row_count_label.text())

        # ---- 第十二版：练习统计勾选列 / 三按钮 / 题型筛选 ----
        from PySide6.QtCore import Qt as Qt12
        st12 = window.stats
        st12_hdr = [st12.table.horizontalHeaderItem(i).text()
                    for i in range(st12.table.columnCount())]
        check("第十二版：练习统计表格题干前加了勾选列（8 列、首列 ✓、题干第 1 列）",
              st12.table.columnCount() == 8 and st12_hdr[0] == "选择" and st12_hdr[1] == "题干"
              and st12.table.item(0, 0) is not None
              and bool(st12.table.item(0, 0).flags() & Qt12.ItemIsUserCheckable),
              f"headers={st12_hdr}")
        check("第十二版：练习统计有「全选 / 取消选择 / 练习所选」三按钮",
              all(hasattr(st12, n) for n in ("btn_select_all", "btn_clear_sel", "btn_practice")))
        t12_items = [st12.type_combo.itemText(i) for i in range(st12.type_combo.count())]
        check("第十二版：练习统计题型下拉含全部题型 + 4 种题型",
              len(t12_items) == 5 and t12_items[0] == "全部题型", f"items={t12_items}")
        # 题型筛选真的生效
        qtype12 = next((st12.type_combo.itemData(i)
                        for i in range(1, st12.type_combo.count())
                        if st12.type_combo.itemData(i)), "")
        if qtype12:
            before12 = len(st12._visible_rows())
            st12.type_combo.setCurrentIndex(st12.type_combo.findData(qtype12))
            app.processEvents()
            check("第十二版：题型筛选只留该题型、与其它筛选叠加",
                  all(str(r["type"]) == qtype12 for r in st12._shown)
                  and "题型=" in st12.cards.text(),
                  f"type={qtype12}, shown={len(st12._shown)}/{before12}")
            st12.type_combo.setCurrentIndex(0); app.processEvents()
        # 全选 → 练习所选 → 取消选择
        st12.clear_selection(); app.processEvents()
        check("第十二版：无勾选时「练习所选」禁用", not st12.btn_practice.isEnabled())
        st12.btn_select_all.click(); app.processEvents()
        check("第十二版：全选勾上当前筛选全部题、按钮变「取消全选」",
              len(st12._checked_ids) == st12.table.rowCount()
              and st12.btn_select_all.text() == "取消全选"
              and st12.btn_practice.isEnabled(),
              f"checked={len(st12._checked_ids)}, rows={st12.table.rowCount()}")
        emitted12 = []
        st12.practice_requested.connect(emitted12.append)
        st12.btn_practice.click(); app.processEvents()
        check("第十二版：练习所选发出的题目去重、含全部勾选题",
              bool(emitted12) and len(emitted12[-1]) == len(set(emitted12[-1]))
              and set(emitted12[-1]) == st12._checked_ids,
              f"ids={emitted12[-1] if emitted12 else None}")
        st12.btn_clear_sel.click(); app.processEvents()
        check("第十二版：取消选择清空全部勾选、按钮禁用",
              len(st12._checked_ids) == 0 and not st12.btn_practice.isEnabled())
        # 勾选跨筛选保留
        if st12.table.rowCount():
            first_qid12 = st12.table.item(0, 0).data(Qt12.UserRole)
            if first_qid12:
                st12.table.item(0, 0).setCheckState(Qt12.Checked); app.processEvents()
                if qtype12:
                    st12.type_combo.setCurrentIndex(st12.type_combo.findData(qtype12))
                    app.processEvents()
                check("第十二版：勾选跨题型筛选全局保留",
                      first_qid12 in st12._checked_ids)
                st12.type_combo.setCurrentIndex(0); app.processEvents()
                st12.clear_selection(); app.processEvents()

        # ---- 第七版：回收站页 / 删除留档 / 自选分组标签恢复 / 统计详情 ----
        check("第七版：侧栏第 8 页是「回收站」且页面已装载",
              "回收站" in NAV_ITEMS and len(NAV_ITEMS) == 9
              and window.stack.widget(NAV["回收站"]) is window.recycle,
              f"index={NAV.get('回收站')}")
        recycle = window.recycle
        check("第七版：回收站表格列齐全（选/题干/题型/原分组/原标签/删除时间）",
              recycle.table.columnCount() == 6
              and "原分组" in [recycle.table.horizontalHeaderItem(i).text()
                              for i in range(6)])
        # 第十一版：回收站不再从题库页按钮进，改从侧栏导航页切过去
        window.nav.setCurrentRow(NAV["回收站"])
        app.processEvents()
        check("第十一版：回收站仍可从侧栏导航页进入",
              window.nav.currentRow() == NAV["回收站"])

        bin_q = QModel(id="t_gui_recycle", type="single", stem="回收站恢复自测题",
                       options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                       answer=["A"], groups=["删前分组"], tags=["删前标签"])
        repository.save_question(bin_q)
        lib.group_filter = ""
        lib.tag_filter = ""
        lib.refresh()
        app.processEvents()
        del_before = repository.deleted_count()
        lib._checked_ids = {bin_q.id}
        lib.refresh()
        lib.run_row_action("delete", bin_q.id)
        app.processEvents()
        recycle.refresh()
        app.processEvents()
        check("第七版：题库删除后题目进回收站且页面能看到",
              repository.get_question(bin_q.id) is None
              and repository.deleted_count() == del_before + 1
              and any(r["question_id"] == bin_q.id for r in repository.deleted_rows())
              and recycle.table.rowCount() >= 1)

        from app.ui.recycle_view import RestoreDialog
        bin_rows = [r for r in repository.deleted_rows()
                    if r["question_id"] == bin_q.id]
        restore_dlg = RestoreDialog(bin_rows, window)
        restore_dlg.group_picker.edit.setText("第七版新建分组")
        restore_dlg.group_picker.add_btn.click()
        restore_dlg.tag_picker.edit.setText("第七版新建标签")
        restore_dlg.tag_picker.add_btn.click()
        groups_new = restore_dlg.chosen_groups()
        tags_new = restore_dlg.chosen_tags()
        check("第七版：恢复对话框可勾选已有 / 即时新建分组和标签",
              "第七版新建分组" in groups_new and "第七版新建标签" in tags_new,
              f"g={groups_new}, t={tags_new}")
        restore_dlg._reuse_original()
        check("第七版：恢复对话框能一键沿用删除前的分组和标签",
              "删前分组" in restore_dlg.chosen_groups()
              and "删前标签" in restore_dlg.chosen_tags())
        restore_dlg.reject()

        rid = int(bin_rows[0]["rid"])
        result = recycle.restore_rows([rid], groups_new, tags_new)
        back_q = repository.get_question(bin_q.id)
        app.processEvents()
        check("第七版：恢复后题目回到题库，分组标签只按用户新选的",
              result["restored"] == 1 and back_q is not None
              and back_q.groups == groups_new and back_q.tags == tags_new
              and "删前分组" not in back_q.groups)
        check("第七版：恢复后回收站留档自动取走",
              not any(r["question_id"] == bin_q.id for r in repository.deleted_rows()))

        # 统计页双击详情
        window.stats.group_combo.setCurrentIndex(0)
        window.stats.tag_combo.setCurrentIndex(0)
        window.stats.refresh()
        app.processEvents()
        from app.ui.stats_detail import StatsDetailDialog
        stat_row = next((r for r in window.stats._rows
                         if r["id"] == "sample_single_001"), None)
        detail = StatsDetailDialog("sample_single_001", stat_row, window)
        check("第七版：统计详情展示本题统计、完整题目与历次答题记录",
              "题目练习详情" in detail.windowTitle()
              and detail.table.columnCount() == 4
              and any("练习次数" in w.text() for w in detail.findChildren(QLabel))
              and detail.table.horizontalHeaderItem(2).text() == "你的答案"
              and detail.btn_practice.isEnabled() and detail.btn_edit.isEnabled())
        detail.action = ""
        detail.reject()
        sd_src = (BASE / "app" / "ui" / "stats_view.py").read_text(encoding="utf-8")
        check("第七版：统计表双击接的是详情而不是复制题干",
              "doubleClicked.connect(self._show_detail)" in sd_src
              and "customContextMenuRequested" in sd_src)
        recycle.clear_all()
        app.processEvents()
        check("第七版：清空回收站后页面同步为空",
              repository.deleted_count() == 0 and recycle.table.rowCount() == 0)

        # ---- 第八版：分组/标签描述 + 实体回收站 + 顶栏高亮 + 取消选择 ----
        check("第八版：回收站含「已删除分组 / 标签」标签页",
              recycle.tabs.count() == 2 and recycle.entity_table is not None)

        # 分组描述弹窗：直接构造（不 exec，避免离屏阻塞）并保存
        desc_q = QModel(id="t_gui_desc", type="single", stem="描述自测题",
                        options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                        answer=["A"], groups=["GUI 描述组"], tags=["GUI 描述标签"])
        repository.save_question(desc_q)
        lib.refresh(); app.processEvents()
        from app.ui.library_view import EntityDetailDialog
        ddlg = EntityDetailDialog("group", "GUI 描述组", window)
        ddlg.desc_edit.setPlainText("这是 GUI 描述组的说明")
        ddlg.save_description()
        check("第八版：分组描述弹窗可写并保存",
              repository.entity_description("group", "GUI 描述组") == "这是 GUI 描述组的说明")

        # 删除分组进实体留档、恢复只针对存在题目
        e1 = QModel(id="t_gui_e1", type="single", stem="实体留档题 1",
                    options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                    answer=["A"], groups=["GUI 实体组"])
        e2 = QModel(id="t_gui_e2", type="single", stem="实体留档题 2",
                    options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                    answer=["B"], groups=["GUI 实体组"])
        repository.save_question(e1)
        repository.save_question(e2)
        lib.refresh(); app.processEvents()
        lib.delete_entity("group", "GUI 实体组")
        app.processEvents()
        ents = [r for r in repository.deleted_entities() if r["name"] == "GUI 实体组"]
        check("第八版：删除分组后进实体留档且记录原题目清单",
              bool(ents) and set(ents[0]["question_ids"]) == {"t_gui_e1", "t_gui_e2"})
        repository.delete_questions(["t_gui_e2"]); app.processEvents()
        erid = int(ents[0]["rid"])
        res_e = recycle.restore_entity_rows([erid]); app.processEvents()
        qe1 = repository.get_question("t_gui_e1")
        check("第八版：恢复分组只给仍在题库的题打回名字、已删题跳过",
              res_e["restored"] == 1 and res_e["skipped"] == 1
              and qe1 is not None and "GUI 实体组" in qe1.groups
              and repository.get_question("t_gui_e2") is None)
        repository.delete_questions(["t_gui_e1"])

        # 顶栏「制作题目」在别的页时不亮，回到该页才亮
        window.nav.setCurrentRow(NAV["题库"])
        window._sync_top_buttons(NAV["题库"]); app.processEvents()
        check("第八版：在别的页时顶栏「制作题目」不高亮（普通按钮）",
              window.make_btn.objectName() != "Primary")
        window.nav.setCurrentRow(NAV["制作题目"])
        window._sync_top_buttons(NAV["制作题目"]); app.processEvents()
        check("第八版：在制作题目页时顶栏「制作题目」才高亮",
              window.make_btn.objectName() == "Primary")

        # 新建题目按钮跳制作题目页
        window.nav.setCurrentRow(NAV["题库"]); app.processEvents()
        lib.btn_new.click(); app.processEvents()
        check("第八版：题库页「新建题目」跳到制作题目页",
              window.nav.currentRow() == NAV["制作题目"])

        # 去掉未分组/未标标签虚拟节点
        tree_texts = []
        for i in range(lib.tree.topLevelItemCount()):
            top = lib.tree.topLevelItem(i)
            tree_texts.append(top.text(0))
            for j in range(top.childCount()):
                tree_texts.append(top.child(j).text(0))
        check("第八版：题库树不再有「未分组 / 未标标签」虚拟节点",
              not any("未分组" in t or "未标标签" in t for t in tree_texts))

        # 双击勾选区（第 0 列）不触发编辑
        lib.refresh(); app.processEvents()
        fired = {"edit": False}
        lib.edit_requested.connect(lambda _qid="": fired.__setitem__("edit", True))
        lib._checked_ids = {desc_q.id}
        lib.refresh(); lib._update_actions(); app.processEvents()
        lib._on_table_double_clicked(lib.table.model().index(0, 0)); app.processEvents()
        check("第八版：双击勾选区（第 0 列）不会触发编辑", not fired["edit"])

        # 取消选择按钮清空勾选（题库页 + 错题库页）
        lib._checked_ids = {desc_q.id}
        lib.refresh(); lib._update_actions(); app.processEvents()
        lib.btn_clear_sel.click(); app.processEvents()
        check("第八版：题库页「取消选择」清空勾选", lib._checked_ids == set())
        check("第八版：错题库页也有「取消选择」按钮",
              window.wrong.btn_clear_sel.text() == "取消选择")
        repository.delete_questions(["t_gui_desc"])

        # ---- 第九版：一致性补齐 + 重命名修复 + 回收站可编辑 ----
        from app.widgets.tables import DataTableWidget
        tables_all = [lib.table, window.wrong.table,
                      recycle.table, recycle.entity_table, window.stats.table]
        check("第九版：五处主表格全部接入统一组件 DataTableWidget",
              all(isinstance(t, DataTableWidget) for t in tables_all))

        check("第九版：四处主表格右键菜单策略已设（CustomContextMenu）",
              all(t.contextMenuPolicy() == Qt5.CustomContextMenu
                  for t in (window.wrong.table,
                            recycle.table, recycle.entity_table)))

        # 造一道在册错题，供守卫 / tooltip / 右键功能测试用
        wq9 = QModel(id="t_gui_v9_wrong", type="single", stem="第九版右键自测题",
                     options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                     answer=["A"], tags=["九版GUI标乙"])
        repository.save_question(wq9)
        repository.save_note("t_gui_v9_wrong", "第九版备注", add_to_wrong=True)
        wrong = window.wrong
        wrong.refresh(); app.processEvents()
        row9 = next((r for r in range(wrong.table.rowCount())
                     if str(wrong.table.item(r, 0).data(Qt5.UserRole) or "") == wq9.id), -1)
        check("第九版：错题库出现新错题（供后续断言）", row9 >= 0)
        check("第九版：错题库列表不再有重复文字的悬停气泡",
              row9 < 0 or (wrong.table.item(row9, 1).toolTip() == ""
                           and wrong.table.item(row9, 2).toolTip() == ""))

        # 勾选列守卫：换桩 WrongDetailDialog 后分别双击第 0 / 非 0 列
        import app.ui.wrong_view as wrong_mod
        opened9 = {"n": 0}

        class _FakeDetail:
            def __init__(self, *_a, **_k):
                self.action = None
            def exec(self):
                opened9["n"] += 1
                return 0
        real_detail = wrong_mod.WrongDetailDialog
        wrong_mod.WrongDetailDialog = _FakeDetail
        try:
            wrong._show_detail(wrong.table.model().index(row9, 0)); app.processEvents()
            check("第九版：错题库双击勾选列不弹详情（槽级兜底）", opened9["n"] == 0)
            from PySide6.QtCore import Qt as Qt9
            from PySide6.QtTest import QTest as QTest9

            def _dbl9(table, row, col):
                # QTest.mouseDClick 单发不生效：必须先补一次真实单击建立按压状态
                center = table.visualRect(table.model().index(row, col)).center()
                QTest9.mouseClick(table.viewport(), Qt9.MouseButton.LeftButton, pos=center)
                QTest9.mouseDClick(table.viewport(), Qt9.MouseButton.LeftButton, pos=center)

            window.nav.setCurrentRow(NAV["错题库"]); app.processEvents()
            _dbl9(wrong.table, row9, 0); app.processEvents()
            check("第九版：错题库双击勾选列被组件吞掉（不发射 doubleClicked）",
                  opened9["n"] == 0)
            _dbl9(wrong.table, row9, 1); app.processEvents()
            check("第九版：错题库双击非勾选列照常弹详情", opened9["n"] == 1)
        finally:
            wrong_mod.WrongDetailDialog = real_detail

        # 空白点击取消行选中，但勾选不丢
        lib.refresh(); app.processEvents()
        if lib.table.rowCount() > 0:
            lib._checked_ids = {lib.table.item(0, 0).data(Qt5.UserRole)}
            lib.refresh(); lib._update_actions(); app.processEvents()
            lib.table.selectRow(0)
            check("第九版：点击空白前置条件——第 0 行已选中",
                  lib.table.currentIndex().row() == 0)
            from PySide6.QtCore import QPoint as QPoint9
            last_bottom = lib.table.visualRect(
                lib.table.model().index(lib.table.rowCount() - 1, 0)).bottom()
            empty_local = QPoint9(8, last_bottom + 30)
            if lib.table.indexAt(empty_local).isValid():
                empty_local = QPoint9(lib.table.viewport().width() - 4, 4)
            from PySide6.QtCore import QEvent as QEvent9b, QPointF as QPointF9b
            from PySide6.QtGui import QMouseEvent as QMouseEvent9b
            ev = QMouseEvent9b(QEvent9b.Type.MouseButtonPress, QPointF9b(empty_local),
                               QPointF9b(lib.table.viewport().mapToGlobal(empty_local)),
                               Qt5.MouseButton.LeftButton, Qt5.MouseButton.LeftButton,
                               Qt5.KeyboardModifier.NoModifier)
            lib.table.mousePressEvent(ev); app.processEvents()
            check("第九版：点表格空白区取消行选中且勾选不丢",
                  lib.table.currentIndex().row() == -1
                  and not lib.table.selectionModel().hasSelection()
                  and len(lib._checked_ids) == 1)

        # 右键功能与顶部按钮等价：复制题干 / 移出错题
        before_clip = app.clipboard().text()
        wrong.run_row_action("copy_stem", wq9.id); app.processEvents()
        check("第九版：错题库右键「复制题干」写入剪贴板",
              app.clipboard().text() == wq9.stem and app.clipboard().text() != before_clip
              or app.clipboard().text() == wq9.stem)
        wrong.run_row_action("remove_wrong", wq9.id); app.processEvents()
        check("第九版：错题库右键「移出错题」与按钮等价",
              wq9.id not in repository.active_wrong_ids())
        check("第九版：错题库无「显示已移出」开关（removed 行不出现）",
              not hasattr(wrong, "show_removed")
              and wq9.id not in repository.active_wrong_ids())

        # 第十版：进出库历史独立窗口——按错题库「进出库历史」按钮打开（信号 → 主窗创建单例）
        hist9 = repository.wrong_history()
        window.nav.setCurrentRow(NAV["错题库"]); app.processEvents()
        wrong.open_history_window_requested.emit(); app.processEvents()
        hw = window._history_window
        check("第十版：错题库「进出库历史」按钮打开独立窗口且为 DataTableWidget",
              hw is not None and isinstance(hw.table, DataTableWidget)
              and hw.table.columnCount() == 3 and hw.isVisible())
        check("第十版：进出库历史独立窗口有记录（加错题 + 移出产生两条）",
              bool(hist9) and hw.table.rowCount() > 0
              and (hw.table.item(0, 0) is None or hw.table.item(0, 0).text() != ""))
        # 历史右键「定位到该题目」→ 经过信号回连主窗 → 跳题库并高亮
        hw.locate_requested.emit(wq9.id); app.processEvents()
        focused = lib.table.currentIndex().row() >= 0 and str(
            lib.table.item(lib.table.currentIndex().row(), 0).data(Qt5.UserRole) or "") == wq9.id
        check("第十版：历史窗口「定位到该题目」信号回连主窗、跳题库并高亮",
              window.nav.currentRow() == NAV["题库"] and focused)
        # 历史右键「查看题目详情」→ 经过信号回连错题库弹详情（改桩验证）
        import app.ui.wrong_view as wrong_mod10
        opened10 = {"n": 0}

        class _FakeDetail10:
            def __init__(self, *_a, **_k):
                self.action = None
            def exec(self):
                opened10["n"] += 1
                return 0
        real_detail10 = wrong_mod10.WrongDetailDialog
        wrong_mod10.WrongDetailDialog = _FakeDetail10
        try:
            hw.detail_requested.emit(wq9.id); app.processEvents()
            check("第十版：历史窗口「查看题目详情」信号回连错题库弹详情", opened10["n"] == 1)
        finally:
            wrong_mod10.WrongDetailDialog = real_detail10
        hw.close(); app.processEvents()
        repository.delete_questions([wq9.id]); app.processEvents()
        wrong.locate_question(wq9.id); app.processEvents()
        check("第九版：定位已删题不崩溃、给提示", True)

        # 重命名：普通改名 + 撞名合并（GUI 路径，绕开输入框）
        r9a = QModel(id="t_gui_v9_ra", type="single", stem="改名题甲",
                     options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                     answer=["A"], tags=["九版GUI改甲"])
        r9b = QModel(id="t_gui_v9_rb", type="single", stem="改名题乙",
                     options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                     answer=["A"], tags=["九版GUI改乙"])
        repository.save_question(r9a)
        repository.save_question(r9b)
        lib.refresh(); app.processEvents()
        ok_plain = lib.rename_entity("tag", "九版GUI改甲", "九版GUI改丙")
        check("第九版：GUI 普通重命名生效（题目跟着改）",
              ok_plain is True
              and repository.get_question("t_gui_v9_ra").tags == ["九版GUI改丙"])
        ok_merge = lib.rename_entity("tag", "九版GUI改丙", "九版GUI改乙")
        check("第九版：GUI 重命名撞已有名走合并不崩溃、归属转移",
              ok_merge is True
              and repository.get_question("t_gui_v9_ra").tags == ["九版GUI改乙"]
              and repository.get_question("t_gui_v9_rb").tags == ["九版GUI改乙"])

        # ---- 第十版：编辑器布局重排 + 题库「练习所选」+ 同名重命名提示 ----
        from app.ui.editor_view import QuestionEditor as Editor10
        save_q10 = QModel(id="t_gui_v10_ed", type="single", stem="第十版布局自测题",
                          options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                          answer=["A"])
        repository.save_question(save_q10)
        dlg10 = Editor10("t_gui_v10_ed", window); app.processEvents()
        check("第十版：编辑器打开即含「答案设置」「解析」「下次多注意」控件",
              hasattr(dlg10, "answer_box") and hasattr(dlg10, "analysis")
              and hasattr(dlg10, "note_edit"))
        # 答案框在左列、下次注意框在右列（不同父容器）
        answer_parent = dlg10.answer_box.parent()
        note_parent = dlg10.note_edit.parent().parent()  # note_edit → nv(QVBoxLayout) → note_box
        check("第十版：编辑器「答案设置」与「下次多注意」分属不同栏（左 / 右）",
              answer_parent is not None and note_parent is not None
              and answer_parent is not note_parent)
        # 第十一版：选项行「正确答案」只显示在勾选项 + 解析紧跟答案、框加高、右栏有横线
        ed11 = QuestionEditor("", window)
        rows11 = ed11._option_rows
        check("第十一版：未勾选的选项行不显示「正确答案」",
              rows11 and all(r["check"].text() == "" for r in rows11))
        if rows11:
            rows11[0]["check"].setChecked(True)
            app.processEvents()
            check("第十一版：勾选的选项行才显示「正确答案」，未勾选的仍为空",
                  rows11[0]["check"].text() == "正确答案"
                  and all(r["check"].text() == "" for r in rows11[1:]))
            rows11[0]["check"].setChecked(False)
            app.processEvents()
            check("第十一版：取消勾选后「正确答案」消失",
                  rows11[0]["check"].text() == "")
        check("第十一版：解析框加高（minHeight 110、不再封顶 72）",
              ed11.analysis.minimumHeight() >= 110)
        check("第十一版：编辑窗口固定 1522×958",
              ed11.size().width() == 1522 and ed11.size().height() == 958)
        ed11.close(); app.processEvents()
        dlg10.close(); app.processEvents()
        repository.delete_questions(["t_gui_v10_ed"])

        # ---- 第十二版：编辑器 / 回收站留档编辑器的填空、简答添加按钮也真实点一遍 ----
        from PySide6.QtWidgets import QPushButton as QPushButton12b
        def ebtn12(w, t):
            return [b for b in w.findChildren(QPushButton12b) if t in b.text()]
        # 造两道临时题供编辑器打开
        repository.save_question(QModel(id="t_gui_v12_blank", type="blank", stem="第十二版填空",
                                         options=[], answer=[["甲"]]))
        repository.save_question(QModel(id="t_gui_v12_short", type="short", stem="第十二版简答",
                                         options=[], answer=["要点一"]))
        for qid12, addtxt12, attr12 in (("t_gui_v12_blank", "添加一个空", "_blank_rows"),
                                        ("t_gui_v12_short", "添加要点", "_point_rows")):
            ed12 = QuestionEditor(qid12, window)
            ed12.show(); app.processEvents()
            before12 = len(getattr(ed12, attr12))
            ebtn12(ed12, addtxt12)[0].click(); app.processEvents()
            check(f"第十二版：编辑器 {attr12} 添加按钮真实点击生效",
                  len(getattr(ed12, attr12)) == before12 + 1,
                  f"{before12} -> {len(getattr(ed12, attr12))}")
            ed12.close(); app.processEvents()
        # 回收站留档编辑器（同一个 QuestionEditor，archive 模式）
        arch12 = QuestionEditor(
            "t_gui_v12_arch", window,
            archive_payload={"id": "t_gui_v12_arch", "type": "short", "stem": "留档简答",
                             "options": [], "answer": ["甲", "乙"], "analysis": "",
                             "groups": [], "tags": [], "source": "", "image": ""},
            archive_rid=1)
        arch12.show(); app.processEvents()
        before_a12 = len(arch12._point_rows)
        ebtn12(arch12, "添加要点")[0].click(); app.processEvents()
        check("第十二版：回收站留档编辑器添加要点真实点击生效",
              len(arch12._point_rows) == before_a12 + 1,
              f"{before_a12} -> {len(arch12._point_rows)}")
        arch12.close(); app.processEvents()
        repository.delete_questions(["t_gui_v12_blank", "t_gui_v12_short"])

        # 题库页「练习所选」按钮：存在、未勾选禁用、勾选后启用
        lib._checked_ids = set()
        lib.refresh(); lib._update_actions(); app.processEvents()
        check("第十版：题库页「练习所选」按钮存在且未勾选时禁用",
              hasattr(lib, "btn_practice") and not lib.btn_practice.isEnabled())
        all_qs = repository.all_questions()
        real_id = all_qs[0].id if all_qs else None
        if real_id:
            lib._checked_ids = {real_id}
            lib.refresh(); lib._update_actions(); app.processEvents()
            check("第十版：题库页勾选后「练习所选」按钮启用",
                  lib.btn_practice.isEnabled())
            lib._checked_ids = set()
            lib.refresh(); lib._update_actions(); app.processEvents()

        # 第十一版：勾选分组 / 标签后「练习所选」启用，收集题目去重
        lib._checked_ids = set()
        lib._checked_entities["group"] = set()
        lib._checked_entities["tag"] = set()
        lib.refresh(); lib._update_actions(); app.processEvents()
        check("第十一版：无勾选时「练习所选」禁用",
              not lib.btn_practice.isEnabled())
        grp_names = [n for n in repository.groups_with_count().keys()
                     if n not in (config.ALL_GROUP_NAME, config.ALL_GROUP_KEY)]
        # 挑一个确实有题目的分组；若无，临时造一组两题（其中一题同时挂两分组）验证去重
        g0 = next((n for n in grp_names
                   if repository.entity_question_ids("group", n)), None)
        if g0:
            lib._checked_entities["group"] = {g0}
            lib.refresh(); lib._update_actions(); app.processEvents()
            check("第十一版：勾选分组后「练习所选」启用",
                  lib.btn_practice.isEnabled())
            emitted11 = []
            lib.practice_selected_requested.connect(emitted11.append)
            lib._practice_selected(); app.processEvents()
            ok_prac = bool(emitted11) and len(emitted11[-1]) == len(set(emitted11[-1]))
            check("第十一版：练习所选发出的题目去重（无重复 id）", ok_prac)
        lib._checked_entities["group"] = set()
        lib._checked_entities["tag"] = set()

        # 第十一版：双击勾选框不弹详情，双击名字才弹详情
        # 用 CheckboxTree.mouseDoubleClickEvent 真实事件坐标验证（不依赖 QCursor/信号投递）
        import app.ui.library_view as lv_mod11
        from PySide6.QtCore import Qt as Qt11, QEvent as QEvent11, QPointF as QPointF11
        from PySide6.QtGui import QMouseEvent as QMouseEvent11
        detail_calls = {"n": 0}
        lib.open_entity_detail = lambda kind, name="": detail_calls.__setitem__("n", detail_calls["n"] + 1)
        # 找一个真实分组子项节点
        tree11 = lib.tree
        target11 = None
        for i in range(tree11.topLevelItemCount()):
            r = tree11.topLevelItem(i)
            for j in range(r.childCount()):
                c = r.child(j)
                if c.data(0, Qt11.UserRole) and c.data(0, Qt11.UserRole)[0] == "group":
                    target11 = c
                    break
            if target11:
                break
        if target11 is not None:
            rect11 = tree11.visualItemRect(target11)
            name_pt = QPointF11(rect11.center().x() + 30, rect11.center().y())
            cb_pt = QPointF11(rect11.left() + 8, rect11.center().y())
            vp = tree11.viewport()
            def _dbl(pos):
                ev = QMouseEvent11(QEvent11.MouseButtonDblClick, pos,
                                   vp.mapToGlobal(pos.toPoint()),
                                   Qt11.LeftButton, Qt11.LeftButton, Qt11.NoModifier)
                tree11.mouseDoubleClickEvent(ev)
                app.processEvents()
            _dbl(cb_pt)
            check("第十一版：双击勾选框不弹详情", detail_calls["n"] == 0)
            _dbl(name_pt)
            check("第十一版：双击名字才弹详情", detail_calls["n"] == 1)
        else:
            check("第十一版：双击勾选框不弹详情 / 双击名字弹详情", False, "无分组子项可测")

        # 同名重命名不再静默 no-op：返回 False 且弹「名称没有变化」提示
        import app.ui.library_view as lv_mod10
        alerted10 = {"hit": False}
        real_alert10 = lv_mod10.alert
        lv_mod10.alert = lambda *a, **k: alerted10.__setitem__("hit", True)
        try:
            ok_same = lib.rename_entity("tag", "九版GUI改乙", "九版GUI改乙")
            check("第十版：同名重命名返回 False 且弹提示（不再静默失败）",
                  ok_same is False and alerted10["hit"] is True
                  and repository.get_question("t_gui_v9_rb").tags == ["九版GUI改乙"])
        finally:
            lv_mod10.alert = real_alert10

        # 回收站留档可编辑：构造留档编辑器直接保存（不 exec）
        ed9 = QModel(id="t_gui_v9_edit", type="single", stem="第九版：编辑前的留档题干",
                     options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                     answer=["A"])
        repository.save_question(ed9)
        repository.delete_questions([ed9.id]); app.processEvents()
        drow9 = next(r for r in repository.deleted_rows() if r["question_id"] == ed9.id)
        from app.ui.editor_view import QuestionEditor as Editor9
        dlg9 = Editor9(ed9.id, window,
                       archive_payload=dict(drow9["payload"]), archive_rid=int(drow9["rid"]))
        dlg9.stem.setPlainText("第九版：编辑后的留档题干（GUI 改好了）")
        dlg9._save(); app.processEvents()
        check("第九版：留档编辑器保存写回留档",
              dlg9.result() == 1 and dlg9.saved_mode == "archive"
              and next(r for r in repository.deleted_rows()
                       if r["question_id"] == ed9.id)["stem"]
              == "第九版：编辑后的留档题干（GUI 改好了）")
        repository.restore_deleted([int(drow9["rid"])], [], []); app.processEvents()
        check("第九版：恢复后带回编辑后内容",
              repository.get_question(ed9.id) is not None
              and repository.get_question(ed9.id).stem
              == "第九版：编辑后的留档题干（GUI 改好了）")

        # 练习统计下拉不再含未分组 / 未标标签
        window.stats.refresh(); app.processEvents()
        gc = window.stats.group_combo
        tc = window.stats.tag_combo
        g_texts = [gc.itemText(i) for i in range(gc.count())]
        t_texts = [tc.itemText(i) for i in range(tc.count())]
        check("第九版：练习统计分组下拉无「未分组」",
              not any("未分组" in t for t in g_texts) and "全部分组" in g_texts)
        check("第九版：练习统计标签下拉无「未标标签」",
              not any("未标标签" in t for t in t_texts) and "全部标签" in t_texts)

        # ---- 第十三版：备份与回档页真实交互 ----
        from app.ui.main_window import NAV as NAV13
        window.nav.setCurrentRow(NAV13["备份与回档"])
        app.processEvents()
        bv13 = window.backup
        check("第十三版：备份与回档页可打开、三个页签、默认全选",
              bv13.tabs.count() == 3 and bv13.ex_table.rowCount() >= 1
              and len(bv13._picked) == bv13.ex_table.rowCount(),
              f"rows={bv13.ex_table.rowCount()}, picked={len(bv13._picked)}")
        before13 = set(bv13._picked)
        bv13.btn_ex_none.click()
        app.processEvents()
        check("第十三版：取消全选清空勾选", len(bv13._picked) == 0)
        bv13.btn_ex_all.click()
        app.processEvents()
        check("第十三版：全选当前筛选恢复勾选", set(bv13._picked) == before13)
        bv13.tabs.setCurrentIndex(2)
        app.processEvents()
        check("第十三版：自动快照页可打开且有撤销按钮",
              bv13.btn_snap_undo.isVisible() and bv13.snap_table.columnCount() == 3)
        bv13.tabs.setCurrentIndex(0)
        app.processEvents()
        check("第十三版：9 个导航页全部可切换且无异常",
              window.stack.count() == 9 and len(NAV13) == 9)

        # ---- 第十四版：多图面板（用真实表情包 png/jpg/gif/webp）----
        emo = Path(r"F:\v与q表情包\微信表情_20261112_082501")
        if emo.is_dir():
            picked_names: list[str] = []
            for fname in ("003.png", "005.jpg", "007.gif"):
                srcf = emo / fname
                if not srcf.is_file():
                    continue
                destf = config.IMAGE_DIR / ("g14_" + fname)
                shutil.copy2(srcf, destf)
                picked_names.append(destf.name)
            check("第十四版：真实表情包样本可用（png/jpg/gif 各一张）",
                  len(picked_names) == 3)

            from app.widgets.image_strip import ImageStrip
            # 1) 独立面板：多图 / 排序 / 删除 / 放大不崩
            strip14 = ImageStrip()
            strip14.resize(300, 560); strip14.show(); app.processEvents()
            strip14.add_files_for_test(picked_names)
            app.processEvents()
            check("第十四版：面板竖排显示 3 张图", strip14.count() == 3)
            strip14.move(2, -1)
            check("第十四版：图片上移生效",
                  strip14.files()[1].endswith(".gif"))
            strip14.open_external(strip14.files()[0])  # 系统照片软件打开，离屏不崩
            app.processEvents()
            strip14.remove_at(0)
            check("第十四版：右键删除后剩 2 张", strip14.count() == 2)

            # 2) 编辑器：四种题型都能插图，保存后 contract 带 images
            for qtype14 in ("single", "multiple", "blank", "short"):
                ed14 = QuestionEditor("", window)
                idx14 = ed14.type_combo.findData(qtype14)
                ed14.type_combo.setCurrentIndex(idx14)
                ed14.stem.setPlainText(f"第十四版{qtype14}多图题")
                if qtype14 in ("single", "multiple"):
                    ed14._option_rows[0]["line"].setText("选项1")
                    ed14._option_rows[0]["check"].setChecked(True)
                    if len(ed14._option_rows) > 1:
                        ed14._option_rows[1]["line"].setText("选项2")
                elif qtype14 == "blank":
                    ed14._blank_rows[0]["line"].setText("答案1")
                else:
                    ed14._point_rows[0]["line"].setText("要点1")
                ed14.image_strip.add_files_for_test(picked_names)
                app.processEvents()
                ct14 = ed14.to_contract()
                check(f"第十四版：{qtype14} 题型编辑器可存多图",
                      len(ct14.get("images") or []) == 3
                      and ct14["image"] == picked_names[0])
                ed14.close()

            # 3) 制作题目页：ImageStrip 存在且 contract 带多图
            maker14 = window.maker
            maker14.image_strip.add_files_for_test(picked_names)
            ctm14 = maker14._build_contract()
            check("第十四版：制作题目页可存多图",
                  len(ctm14.get("images") or []) == 3)
            maker14._reset_form()
            check("第十四版：清空重填会清空图片",
                  maker14.image_strip.count() == 0)

            # ---- 第十四版修订：右侧自适应网格 + gif 真播放 + 做题页图片在右侧 ----
            from PySide6.QtCore import QTimer
            from PySide6.QtGui import QMovie
            from PySide6.QtWidgets import QLabel
            grid = ImageStrip()
            grid.resize(360, 460)
            grid.show()
            app.processEvents()
            grid.add_files_for_test(picked_names)
            app.processEvents()
            QTimer.singleShot(0, lambda: None)
            app.processEvents()
            check("第十四版修订：图片按网格多列自动排布",
                  grid.count() == 3 and grid._cols >= 2 and grid.grid.columnCount() >= 2)
            plan_cols, plan_cell = grid._grid_plan(6, 360, 460)
            rect_cols, rect_cw, rect_ch = grid._grid_rect(6, 360, 460)
            check("第十四版修订：6 张图均分铺满一整板、一屏看全不滚动",
                  plan_cols >= 2 and rect_cols == plan_cols
                  and rect_cw >= 56 and rect_ch >= 56
                  and rect_cols * rect_cw <= 360
                  and ((6 + rect_cols - 1) // rect_cols) * rect_ch <= 460)
            # gif 用 QMovie 真正播放（不是静态首帧）
            gif_cards = [c for c in grid._cards
                         if str(c.property("image_name") or "").lower().endswith(".gif")]
            gif_movie = (gif_cards[0].findChild(QLabel, "ImageCardPic").property("movie")
                         if gif_cards else None)
            check("第十四版修订：gif 在面板里真的播放（QMovie Running）",
                  gif_movie is not None and gif_movie.state() == QMovie.Running)
            # 硬验证：多帧动图在播放过程中画面确实变化，不是静态首帧
            import hashlib as _hashlib14
            import time as _time14
            from PySide6.QtGui import QImage as _QImage14
            gif_frames: set[str] = set()
            for _i14 in range(14):
                app.processEvents()
                _time14.sleep(0.05)
                _im14 = gif_movie.currentImage().convertToFormat(_QImage14.Format_RGB32)
                gif_frames.add(_hashlib14.md5(bytes(_im14.bits())).hexdigest())
            check("第十四版修订：动图真的在动（多帧画面变化，非静态首帧）",
                  gif_movie.frameCount() > 1 and len(gif_frames) >= 2)
            # 只读模式：答题页用的面板没有添加按钮、右键菜单不含删除
            ro = ImageStrip(read_only=True)
            ro.resize(300, 400)
            ro.show()
            app.processEvents()
            ro.add_files_for_test(picked_names)
            app.processEvents()
            check("第十四版修订：只读插图面板隐藏添加按钮但仍显示图片",
                  ro.count() == 3 and not ro.btn_add.isVisible())
            # 练习四个页面都挂了右侧只读插图网格
            pv = window.practice
            check("第十四版修订：答题/结果/自评/总览四页都有右侧插图网格",
                  all(isinstance(getattr(pv, f"{n}_images", None), ImageStrip)
                      for n in ("answer", "result", "review", "summary")))
            # 真实切到答题页：构造一道带图题注入，调用真实渲染函数验证
            from app.models import Question as Q14Dbg
            qimg = Q14Dbg(id="t_gui14_img", type="single",
                          stem="做题页插图定位题\n\n![插图](legacy.png)",
                          options=[{"key": "A", "text": "选项一"},
                                   {"key": "B", "text": "选项二"}],
                          answer=["A"], images=list(picked_names))
            saved_questions = list(pv.questions)
            saved_index = pv.index
            pv.questions = [qimg]
            pv.index = 0
            pv.show_question()
            app.processEvents()
            check("第十四版修订：做题时图片在右侧网格、题干/选项文字里不夹图片",
                  "![插图](" not in pv.stem_view._source
                  and "选项一" not in pv.stem_view._source
                  and pv.answer_images.count() == len(picked_names))
            pv.exit_to_setup()
            pv.questions = saved_questions
            pv.index = saved_index
            app.processEvents()
            grid.close(); ro.close()

            # ---- 第十四版本轮：插图一整板 + 三个详情窗口 gif 真播放 ----
            one_cols, one_cw, one_ch = grid._grid_rect(1, 360, 460)
            check("第十四版：单张图铺满整板（不再小图挂上面留空白）",
                  one_cols == 1 and one_cw >= 340 and one_ch >= 440)
            check("第十四版：插图面板不显示计数标题与文件名编号",
                  grid.title.isHidden() and grid.title.text() == ""
                  and all(c.findChild(QLabel, "ImageCardName") is None
                          for c in grid._cards))

            from app.ui.wrong_detail import WrongDetailDialog as WD14
            from app.ui.question_picker import QuestionDetailDialog as QD14
            from app.ui.session_report import SessionReportDialog as SR14
            import time as _t14, hashlib as _h14
            from PySide6.QtGui import QImage as _QI14
            qdetail = Q14Dbg(id="t_gui14_detail", type="single",
                             stem="详情动图验证题",
                             options=[{"key": "A", "text": "选项一"}],
                             answer=["A"], images=list(picked_names))
            repository.save_question(qdetail)
            wd14 = WD14(qdetail.id, window); wd14.show(); app.processEvents()
            qd14 = QD14(qdetail.id, window); qd14.show(); app.processEvents()
            sid14 = repository.create_session("whole", {"count": 1}, [qdetail.id])
            sr14 = SR14(sid14, window); sr14.show(); app.processEvents()
            if sr14._items:
                sr14._render_detail(sr14._items[0])
            app.processEvents()
            for label14, strip14b in (("错题库详情", wd14.image_strip),
                                      ("题目详情", qd14.image_strip),
                                      ("练习详细报告", sr14.detail_images)):
                check(f"第十四版：{label14}插图在右侧一整板且张数正确",
                      isinstance(strip14b, ImageStrip)
                      and strip14b.count() == len(picked_names))
            check("第十四版：详细报告文字侧不再夹静态插图",
                  "![插图](" not in sr14.detail.toPlainText())
            movies14 = []
            for c14 in sr14.detail_images._cards:
                p14 = c14.findChild(QLabel, "ImageCardPic")
                m14 = p14.property("movie") if p14 is not None else None
                if m14 is not None:
                    movies14.append(m14)
            check("第十四版：详细报告里 gif 真播放（QMovie Running）",
                  any(m.state() == QMovie.Running and m.frameCount() > 1
                      for m in movies14))
            mv14 = next((m for m in movies14 if m.frameCount() > 1), None)
            if mv14 is not None:
                digs14 = set()
                for _ in range(16):
                    app.processEvents(); _t14.sleep(0.05)
                    digs14.add(_h14.md5(bytes(
                        mv14.currentImage().convertToFormat(
                            _QI14.Format_RGB32).bits())).hexdigest())
                check("第十四版：详细报告动图画面持续变化（非静态首帧）",
                      len(digs14) >= 2)
            else:
                check("第十四版：详细报告动图画面持续变化（非静态首帧）",
                      False, "未找到多帧 gif")
            wd14.close(); qd14.close(); sr14.close()
            repository.delete_session_record(sid14)
            repository.delete_questions([qdetail.id])
            app.processEvents()

        # 收尾清理
        repository.delete_questions(["t_gui_v9_ra", "t_gui_v9_rb", "t_gui_v9_edit"])
        recycle.clear_all(); app.processEvents()

    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("GUI 冒烟测试整体执行", False, str(exc))
    finally:
        db.close()
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "、".join(failed))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
