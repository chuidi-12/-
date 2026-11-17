"""无界面端到端自测：数据契约、判分、错题闭环、导出恢复、静态红线检查。

运行：python selftest.py
使用临时数据库，不触碰项目里的 data/questions.db。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import app.config as config  # noqa: E402
import app.db as db  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="qbank_selftest_"))
config.DATA_DIR = TMP
config.IMAGE_DIR = TMP / "images"
config.DB_PATH = TMP / "test.db"
db.DB_PATH = TMP / "test.db"
config.ensure_dirs()

from app import grading, repository, schema  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    try:
        # ---- SC-1 首次建库 ----
        conn = db.connect()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        need = {"meta", "questions", "groups_", "tags", "attempts", "wrong_questions",
                "wrong_history", "practice_sessions", "session_answers", "settings"}
        check("SC-1 首次运行自动建库，表齐全", need <= tables, f"共 {len(tables)} 张表")
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        check("SC-1 schema_version 迁移写入", version and version["value"] == str(db.SCHEMA_VERSION))
        db.migrate(conn)
        check("SC-1 迁移可重复执行且不报错", True)
        wrong_cols = {r[1] for r in conn.execute("PRAGMA table_info(wrong_questions)")}
        check("第三版：错题库含 note 备注列（下次多注意）", "note" in wrong_cols)

        # ---- SC-2 导入契约 ----
        sample = (BASE / "sample_questions.json").read_text(encoding="utf-8")
        raw, warnings = schema.parse_payload(sample)
        report = schema.build_report(raw, warnings, *schema.existing_index(conn))
        check("SC-2 示例题目全部通过校验", report.error_count == 0,
              f"total={report.total}, ok={report.ok_count}, err={report.error_count}")
        inserted, updated = repository.commit_import(report.importable)
        check("SC-2 事务导入覆盖四类题", inserted == 5 and updated == 0, f"inserted={inserted}")
        types = {q.type for q in repository.all_questions()}
        check("SC-2 四类题型均已入库", types == {"single", "multiple", "blank", "short"}, str(types))

        report2 = schema.build_report(raw, [], *schema.existing_index(conn))
        check("SC-2 同内容二次导入被查重", report2.duplicate_count == 5,
              f"duplicate={report2.duplicate_count}")

        dirty = {"version": 1, "questions": [
            {"type": "choice", "stem": "非法题型"},
            {"type": "single", "stem": "", "options": [], "answer": []},
            {"type": "blank", "stem": "一个空", "answer": ["3.14"]},
            {"type": "short", "stem": "合法题", "answer": ["要点"]},
        ]}
        dirty_raw, _ = schema.parse_payload(json.dumps(dirty, ensure_ascii=False))
        dirty_report = schema.build_report(dirty_raw, [], *schema.existing_index(conn))
        check("SC-2 脏数据被逐条拦截并给出原因", dirty_report.error_count == 3
              and all(i.messages for i in dirty_report.items if i.status == "error"),
              f"err={dirty_report.error_count}, ok={dirty_report.ok_count}")
        before = len(repository.all_questions())
        repository.commit_import(dirty_report.importable)
        check("SC-2 只导入合法行，总数按预期增加", len(repository.all_questions()) == before + 1)

        bad_json = "{'not': 'json'}"
        _, errs = schema.parse_payload(bad_json)
        check("SC-2 非法 JSON 有明确错误提示", bool(errs), errs[0][:40] if errs else "")

        # ---- SC-6 判分引擎（只判对/错，不打分，无 AI）----
        settings = db.get_settings()
        single = repository.get_question("sample_single_001")
        assert single
        r1 = grading.grade(single, single.answer, settings)
        r2 = grading.grade(single, ["A"] if single.answer != ["A"] else ["B"], settings)
        check("SC-6 单选完全匹配才算对", r1.correct and not r2.correct)
        check("SC-6 判卷结果只含对错、不记分值",
              isinstance(r1.correct, bool) and isinstance(r2.correct, bool)
              and not hasattr(r1, "earned") and not hasattr(r1, "max_score"))

        from app.models import Question as Q
        multiple_q = Q(id="t_m", type="multiple", stem="多选",
                       options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}, {"key": "C", "text": "c"}],
                       answer=["A", "B"])
        check("SC-6 多选漏选判错", not grading.grade(multiple_q, ["A"], settings).correct)
        check("SC-6 多选全中判对", grading.grade(multiple_q, ["A", "B"], settings).correct)
        check("SC-6 多选错选判错", not grading.grade(multiple_q, ["A", "C"], settings).correct)

        blank_q = Q(id="t_b", type="blank", stem="填空", answer=[["π", "3.14"], ["1/3", "三分之一"]])
        check("SC-6 填空等价答案 / 全半角归一命中", grading.grade(blank_q, ["π", "三分之一"], settings).correct)
        check("SC-6 填空有一空不对即判错", not grading.grade(blank_q, ["3.14", "xyz"], settings).correct)

        short_q = Q(id="t_s2", type="short", stem="简答", answer=["要点一", "要点二", "要点三"])
        rs_all = grading.grade(short_q, {"text": "x", "checked": [0, 1, 2]}, settings)
        rs_part = grading.grade(short_q, {"text": "x", "checked": [0, 1]}, settings)
        check("SC-6 简答全部要点答到才算对", rs_all.correct and rs_all.grade_source == "self")
        check("SC-6 简答缺要点判错（不按命中比例给分）", not rs_part.correct)
        check("第三版：简答判分记录未答到要点明细",
              rs_part.detail.get("missed") == ["3. 要点三"]
              and rs_all.detail.get("missed") == [], str(rs_part.detail.get("missed")))
        multi_summary = grading.answer_summary(Q(
            id="t_ms", type="multiple", stem="m", answer=["A", "C"],
            options=[{"key": "A", "text": "ACK"}, {"key": "B", "text": "FIN"},
                     {"key": "C", "text": "SYN"}, {"key": "D", "text": "RST"}]))
        check("第三版：结果页标准答案一项一行（C 在 A 下方）",
              multi_summary == "A. ACK\nC. SYN" and "；" not in multi_summary,
              repr(multi_summary))
        check("SC-6 简答题只做分点自评、无任何 AI 判分",
              rs_all.grade_source == "self" and "ai_fallback_reason" not in rs_all.detail)
        check("SC-6 代码中无 AI 配置残留", not any(k.startswith("ai_") for k in db.get_settings()))
        made = {
            "type": "single", "stem": "自制题：1 + 1 = ?",
            "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
            "answer": ["B"], "source": "manual",
        }
        made_q, made_errors = schema.validate_question(made, 1)
        check("SC-3 手动制作的选择题可通过数据契约校验",
              made_q is not None and not made_errors and made_q.source == "manual")
        from app import shuffle
        display = shuffle.apply_order_map([single], {"sample_single_001": ["D", "C", "B", "A"]})[0]
        check("SC-5 选项乱序后正确答案字母同步重算",
              display.answer == ["B"]
              and display.options[1]["text"] == single.options[2]["text"]
              and display.id == single.id and single.answer == ["C"])

        # ---- SC-4 / SC-7 答题记录与错题闭环（答错入库，连续答对移出）----
        wrong_answer = ["A"] if single.answer != ["A"] else ["B"]
        bad = grading.grade(single, wrong_answer, settings)
        ok, msg, attempt_id = repository.record_attempt(single, bad, wrong_answer,
                                                        mode="instant", session_id="selftest")
        check("SC-4 判错后写入答题记录并返回 id", ok and attempt_id > 0, f"id={attempt_id}")
        check("SC-7 判错自动进入错题库", single.id in repository.active_wrong_ids(), msg)

        good_answer = single.answer
        good = grading.grade(single, good_answer, settings)
        _, msg1, _ = repository.record_attempt(single, good, good_answer, mode="instant")
        check("SC-7 第 1 次答对累计 streak", "1/2" in msg1, msg1)
        _, msg2, _ = repository.record_attempt(single, good, good_answer, mode="instant")
        check("SC-7 连续答对 2 次自动移出", "移出" in msg2
              and single.id not in repository.active_wrong_ids(), msg2)
        check("SC-7 进出库历史可查", len(repository.wrong_history()) >= 3)

        # ---- SC-9 导出再导入无损 ----
        full = repository.export_full()
        check("SC-9 全库导出含题目与业务数据",
              len(full["questions"]) == 6 and bool(full["full_backup"]["attempts"]),
              f"questions={len(full['questions'])}, attempts={len(full['full_backup']['attempts'])}")
        check("SC-9 导出设置不含任何 AI / 密钥字段",
              not any(k.startswith("ai_") for k in full["full_backup"]["settings"]))
        check("SC-9 Markdown / CSV 导出非空",
              len(repository.export_markdown()) > 200 and len(repository.export_csv()) > 200)

        counts = repository.import_full(full, replace=True)
        full2 = repository.export_full()
        check("SC-9 清空后完整恢复题目 / 答题记录 / 错题状态",
              counts["questions"] == len(full["questions"])
              and len(full2["full_backup"]["attempts"]) == len(full["full_backup"]["attempts"])
              and full2["full_backup"]["wrong_questions"] == full["full_backup"]["wrong_questions"],
              f"counts={counts}")
        check("SC-9 分组与标签无损",
              sorted(repository.groups_with_count()) == sorted(repository.groups_with_count()))

        # ---- SC-8 统计与静态红线 ----
        stats = repository.stats_overall()
        check("SC-8 统计接口可用", stats["attempts"] == 3 and stats["accuracy"] > 0,
              f"accuracy={stats['accuracy']:.2f}")
        check("SC-8 按标签 / 题型 / 按日统计均可计算",
              bool(repository.stats_by_tag()) and bool(repository.stats_by_type())
              and len(repository.stats_daily()) == 14)
        per_question = repository.stats_per_question()
        check("SC-8 逐题统计含练习 / 答对 / 答错次数",
              len(per_question) == 6
              and all(k in per_question[0] for k in ("attempts", "correct", "wrong", "accuracy"))
              and sum(x["attempts"] for x in per_question) == 3,
              f"rows={len(per_question)}, attempts={sum(x['attempts'] for x in per_question)}")
        forbidden = ("mastery", "familiarity", "ease_factor", "interval_repeat", "sm2_")
        violations = []
        for path in (BASE / "app").rglob("*.py"):
            low = path.read_text(encoding="utf-8").lower()
            for word in forbidden:
                if word in low:
                    violations.append(f"{path.name}:{word}")
        check("SC-8 代码中无熟悉度 / 间隔重复建模逻辑", not violations, ", ".join(violations))
        columns = {r[1] for r in db.connect().execute("PRAGMA table_info(questions)")}
        check("SC-8 数据库无熟练度字段",
              not any("master" in c or "familiar" in c for c in columns))

        # ---- 会话草稿 ----
        sid = repository.create_session("instant", {"count": 2}, [single.id, short_q.id])
        repository.save_draft_answer(sid, single.id, ["B"])
        repository.set_session_index(sid, 1)
        loaded = repository.load_draft_answers(sid)
        check("补充：练习草稿可保存与续做",
              loaded.get(single.id) == ["B"] and repository.session_row(sid)["current_index"] == 1)

        # ---- 备份恢复 ----
        backup = db.backup_to(TMP / "backup.db")
        valid, reason = db.validate_db_file(backup)
        check("补充：热备份文件可校验", valid, reason)

        # ---- 第三版：简答题未答到要点写入错题库「下次多注意」 ----
        # 放在计数类断言之后，避免影响 SC-8 / SC-9 的题目与记录数量
        short_wrong_q = repository.get_question("sample_short_001")
        assert short_wrong_q
        sw_answer = {"text": "只写了链地址法", "checked": [0]}
        sw_result = grading.grade(short_wrong_q, sw_answer, settings)
        repository.record_attempt(short_wrong_q, sw_result, sw_answer,
                                  mode="instant", session_id="selftest")
        sw_row = next((r for r in repository.wrong_rows("active")
                       if r["question_id"] == short_wrong_q.id), None)
        sw_note = str(sw_row.get("note") or "") if sw_row else ""
        check("第三版：未答到要点写进错题库备注「下次多注意」",
              "下次多注意" in sw_note and "开放定址法" in sw_note, sw_note[:80])
        sw_hist = [h for h in repository.wrong_history()
                   if h["question_id"] == short_wrong_q.id]
        check("第三版：进出库历史也带上「下次多注意」",
              any("下次多注意" in h["note"] for h in sw_hist))
        sw_full = repository.export_full()
        check("第三版：全库导出含错题库备注且可无损恢复",
              any("下次多注意" in str(r.get("note") or "")
                  for r in sw_full["full_backup"]["wrong_questions"]))
        repository.import_full(sw_full, replace=True)
        sw_after = next((r for r in repository.wrong_rows("active")
                         if r["question_id"] == short_wrong_q.id), None)
        check("第三版：清空恢复后「下次多注意」备注不丢",
              sw_after is not None and "下次多注意" in str(sw_after.get("note") or ""))

        # ---- 第三版：勾选美化资源（随主题色生成对勾 PNG） ----
        from app import checkmark, theme
        asset_b = checkmark.ensure_check_asset(theme.BY_ID["B"]["on_accent"])
        asset_a = checkmark.ensure_check_asset(theme.BY_ID["A"]["on_accent"])
        check("第三版：勾选对勾图标可生成且按主题分色",
              Path(asset_b).is_file() and Path(asset_a).is_file() and asset_b != asset_a)
        qss_b = theme.build_qss(theme.BY_ID["B"])
        check("第三版：全局 QSS 含渐变选中态与对勾图标",
              "qlineargradient" in qss_b and "image: url" in qss_b
              and "QCheckBox::indicator:checked" in qss_b)

        # ---- 第三版：静态红线（答题页不提前展示分点标准答案） ----
        practice_src = (BASE / "app" / "ui" / "practice_view.py").read_text(encoding="utf-8")
        check("第三版：答题阶段不再有「提交前勾选」标准答案",
              "提交前勾选" not in practice_src and "_build_review_page" in practice_src)
        check("第三版：练习页含上一题回看与自评页",
              "def prev_question" in practice_src and "def confirm_review" in practice_src)

        # ---- 第四版：动态默认组（不写入题目数据，含以后新增） ----
        from app.config import ALL_GROUP_KEY, ALL_GROUP_NAME
        all_qs = repository.all_questions()
        before_groups = {q.id: list(q.groups) for q in all_qs}
        by_default = repository.filter_questions(group=ALL_GROUP_KEY)
        by_name = repository.filter_questions(group=ALL_GROUP_NAME)
        check("第四版：默认组筛选返回全部题目（动态归属）",
              len(by_default) == len(all_qs) == len(by_name),
              f"default={len(by_default)}, name={len(by_name)}, all={len(all_qs)}")
        check("第四版：默认组不改动任何题目的分组数据",
              all(list(q.groups) == before_groups[q.id] for q in all_qs))
        added = Q(id="t_default_group", type="single", stem="默认组新题",
                  options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                  answer=["B"], groups=["Python"])
        repository.save_question(added)
        check("第四版：默认组自动包含以后新增的题目",
              added.id in {q.id for q in repository.filter_questions(group=ALL_GROUP_KEY)})
        repository.delete_questions([added.id])

        # ---- 第四版：删除分组 / 标签只去名字，不删题目 ----
        entity_q = Q(id="t_entity", type="single", stem="分组删除测试",
                     options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                     answer=["B"], groups=["临时分组"], tags=["临时标签"])
        repository.save_question(entity_q)
        repository.delete_entity("group", "临时分组")
        repository.delete_entity("tag", "临时标签")
        after_entity = repository.get_question(entity_q.id)
        check("第四版：右键删除分组/标签后题目仍在且名字已移除",
              after_entity is not None and "临时分组" not in after_entity.groups
              and "临时标签" not in after_entity.tags)
        repository.delete_questions([entity_q.id])

        # ---- 第四版：窗口任务栏 flags + 刷新动画 / 多选组件静态检查 ----
        from PySide6.QtCore import Qt
        from app import taskbar
        check("第四版：任务栏模块提供最小化标志集合",
              bool(Qt.WindowMinimizeButtonHint and Qt.WindowSystemMenuHint)
              and hasattr(taskbar, "enable_taskbar_minimize"))
        lib_src = (BASE / "app" / "ui" / "library_view.py").read_text(encoding="utf-8")
        check("第四版：题库页含复选框多选、全选、右键菜单与刷新动画",
              all(s in lib_src for s in ("_toggle_select_all", "_on_tree_menu",
                                         "attach_refresh_animation", "GroupPickerDialog",
                                         "checked_ids")))
        refresh_src = (BASE / "app" / "widgets" / "refresh_fx.py").read_text(encoding="utf-8")
        check("第四版：刷新动画含旋转帧与淡入效果",
              "SPIN_FRAMES" in refresh_src and "QPropertyAnimation" in refresh_src
              and "已刷新" in refresh_src)
        ns_src = (BASE / "app" / "widgets" / "name_select.py").read_text(encoding="utf-8")
        check("第四版：分组组件含固定默认组且不写进 names()",
              "default_name" in ns_src and "default_chip" in ns_src
              and "def names" in ns_src and "ALL_GROUP_NAME" in ns_src)

        # ---- 第五版：错题库时间 / 备注 / 勾选多选 ----
        from app.widgets.common import fmt_time
        check("第五版：时间显示精确到分钟且不带 T",
              fmt_time("2026-11-14T06:12:34") == "2026-11-14 06:12"
              and fmt_time("2026-11-14T06:12:34.123456") == "2026-11-14 06:12"
              and fmt_time("") == "—",
              fmt_time("2026-11-14T06:12:34"))

        note_q = Q(id="t_note_choice", type="multiple", stem="备注题型测试",
                   options=[{"key": "A", "text": "共享内存"}, {"key": "B", "text": "全局变量"},
                            {"key": "C", "text": "信号量"}],
                   answer=["A", "C"])
        wrong_note = repository.attention_note(grading.grade(note_q, ["A", "B"], db.get_settings()))
        check("第五版：选择题答错也生成「下次多注意」",
              "正确答案 A、C" in wrong_note and "你选了 A、B" in wrong_note, wrong_note)
        right_note = repository.attention_note(grading.grade(note_q, ["A", "C"], db.get_settings()))
        check("第五版：答对不写备注", right_note == "", right_note)
        fallback = repository.wrong_note_text(note_q, {"note": ""})
        check("第五版：旧数据空备注给可读兜底", "正确答案 A、C" in fallback, fallback)
        check("第五版：备注展示去掉重复前缀",
              repository.wrong_note_text(
                  note_q, {"note": "下次多注意：正确答案 A、C；你选了 B"})
              == "正确答案 A、C；你选了 B")

        wv_src = (BASE / "app" / "ui" / "wrong_view.py").read_text(encoding="utf-8")
        check("第五版：错题库含复选框、全选、勾选重练与详情入口",
              all(s in wv_src for s in ("_toggle_select_all", "WrongDetailDialog",
                                        "重练勾选", "fmt_time", "_checked_ids")))
        check("第五版：题干独占拉伸（第六版已把备注列移进详情）",
              "setSectionResizeMode(1, QHeaderView.Stretch)" in wv_src
              and "下次多注意" not in wv_src.split("setHorizontalHeaderLabels")[1].split(")")[0])
        wd_src = (BASE / "app" / "ui" / "wrong_detail.py").read_text(encoding="utf-8")
        check("第五版：详情弹窗展示完整题干、标准答案与下次多注意",
              all(s in wd_src for s in ("下次多注意", "标准答案", "题干", "answer_summary")))

        # ---- 第六版：版本号 / 备注可手写 / 删除交互 ----
        check("第十四版：版本号 v14.0.0 且标注第十四版",
              config.APP_VERSION == "14.0.0" and config.APP_EDITION == "第十四版",
              f"{config.APP_VERSION} / {config.APP_EDITION}")
        import app as app_pkg
        check("第六版：包内 __version__ 与配置一致", app_pkg.__version__ == config.APP_VERSION)

        cols_q = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
        cols_w = {r[1] for r in conn.execute("PRAGMA table_info(wrong_questions)")}
        check("第六版：schema v4 增加 questions.note 与 note_manual",
              "note" in cols_q and "note_manual" in cols_w)
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.executescript(db.DDL_V1)
        legacy.execute("INSERT INTO meta(key,value) VALUES('schema_version','3')")
        legacy.commit()
        db._migrate_v4(legacy)
        cols_after = {r[1] for r in legacy.execute("PRAGMA table_info(questions)")}
        cols_w_after = {r[1] for r in legacy.execute("PRAGMA table_info(wrong_questions)")}
        check("第六版：v3 → v4 迁移可重复执行且补齐两列",
              "note" in cols_after and "note_manual" in cols_w_after)
        db._migrate_v4(legacy)
        check("第六版：v4 迁移重复执行不报错", True)
        legacy.close()

        # 手写备注：不在错题库的题也能写，写了顺手入库
        note_target = Q(id="t_manual_note", type="single", stem="手写备注测试题",
                        options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                        answer=["A"])
        repository.save_question(note_target)
        check("第六版：写备注前该题不在错题库",
              note_target.id not in repository.active_wrong_ids())
        msg = repository.save_note(note_target.id, "总把 A B 看反，先读选项再答",
                                   add_to_wrong=True)
        saved_row = repository.wrong_row(note_target.id) or {}
        check("第六版：不在错题库的题写备注后顺手入库且标记手写",
              note_target.id in repository.active_wrong_ids()
              and saved_row.get("note_manual") == 1
              and "看反" in str(saved_row.get("note") or ""), msg)
        check("第六版：题目表也存了这条备注",
              "看反" in (repository.get_question(note_target.id).note or ""))
        manual_text = repository.wrong_note_text(
            repository.get_question(note_target.id), saved_row)
        check("第六版：详情展示的就是手写备注", manual_text == "总把 A B 看反，先读选项再答",
              manual_text)

        # 再答错一次：系统文案不许覆盖手写备注
        again = grading.grade(note_target, ["B"], db.get_settings())
        repository.record_attempt(note_target, again, ["B"], mode="instant")
        after_wrong = repository.wrong_row(note_target.id) or {}
        check("第六版：手写备注不被答错时的系统文案覆盖",
              "看反" in str(after_wrong.get("note") or "")
              and after_wrong.get("note_manual") == 1,
              str(after_wrong.get("note")))
        check("第六版：详情里仍显示手写备注",
              repository.wrong_note_text(repository.get_question(note_target.id),
                                         after_wrong) == "总把 A B 看反，先读选项再答")

        # 连续答对移出后，手写备注仍保留
        good_answer2 = ["A"]
        for _ in range(2):
            res = grading.grade(note_target, good_answer2, db.get_settings())
            repository.record_attempt(note_target, res, good_answer2, mode="instant")
        removed_row = repository.wrong_row(note_target.id) or {}
        check("第六版：移出错题库后手写备注不丢",
              removed_row.get("status") == "removed"
              and "看反" in str(removed_row.get("note") or ""), str(removed_row.get("status")))

        # 清空备注：标记回系统自动
        repository.save_note(note_target.id, "")
        cleared = repository.wrong_row(note_target.id) or {}
        check("第六版：清空备注后标记取消，留下空备注",
              cleared.get("note_manual") == 0 and str(cleared.get("note") or "") == "")

        manual_edit, auto_edit = repository.note_for_editor(note_target.id)
        check("第六版：编辑器读到手写备注 / 自动提醒两种来源",
              manual_edit == "" and ("正确答案 A" in auto_edit or auto_edit == ""),
              f"manual={manual_edit!r}, auto={auto_edit!r}")

        # 导出恢复后手写备注与标记都不丢
        repository.save_note(note_target.id, "导出恢复测试备注")
        snapshot = repository.export_full()
        repository.import_full(snapshot, replace=True)
        restored = repository.wrong_row(note_target.id) or {}
        check("第六版：全库导出恢复后备注与手写标记无损",
              "导出恢复测试备注" in str(restored.get("note") or "")
              and restored.get("note_manual") == 1)
        repository.delete_questions([note_target.id])

        # 静态检查：删除入口 + 统计页改造
        lib_src = (BASE / "app" / "ui" / "library_view.py").read_text(encoding="utf-8")
        tables_src = (BASE / "app" / "widgets" / "tables.py").read_text(encoding="utf-8")
        check("第十一版：题库仍支持右键菜单与 Delete 键删除（分组/标签按钮已移除，功能走左树）",
              all(s in lib_src for s in ("_on_table_menu", "run_row_action",
                                         "QKeySequence.Delete", "_delete_current_entity",
                                         "rename_entity"))
              and "_delete_checked_entities" in lib_src)
        check("第六版：右键菜单不止删除（编辑 / 另存新题 / 加入分组 / 复制题干）",
              all(s in lib_src + tables_src for s in ('"编辑题目"', "另存为新题",
                                                      "加入分组", "复制题干")))
        ed_src = (BASE / "app" / "ui" / "editor_view.py").read_text(encoding="utf-8")
        check("第六版：编辑器含「下次多注意」与覆盖/另存为选项",
              "note_edit" in ed_src and "覆盖原题" in ed_src and "另存为新题" in ed_src
              and "start_as_copy" in ed_src)
        st_src = (BASE / "app" / "ui" / "stats_view.py").read_text(encoding="utf-8")
        check("第六版：统计页删掉四张图、题干列加大并可按分组/标签筛选",
              "ChartWidget" not in st_src and "group_combo" in st_src
              and "tag_combo" in st_src and "STEM_COL_WIDTH" in st_src
              and "ROW_HEIGHT" in st_src)
        wd_src2 = (BASE / "app" / "ui" / "wrong_detail.py").read_text(encoding="utf-8")
        check("第六版：错题详情可编辑并保存「下次多注意」",
              "note_edit" in wd_src2 and "def save_note" in wd_src2
              and "保存备注" in wd_src2)

        # ---- 第七版：回收站（删除留档 / 自选分组标签恢复）/ 统计详情 ----
        check("第八版：SCHEMA_VERSION 至少为 6", config.SCHEMA_VERSION >= 6)
        tables5 = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("第七版：新库自动建 deleted_questions 回收站表",
              "deleted_questions" in tables5)
        del_cols = {r[1] for r in conn.execute("PRAGMA table_info(deleted_questions)")}
        check("第七版：回收站表字段齐全",
              {"rid", "question_id", "payload", "stem", "qtype", "deleted_at"} <= del_cols,
              str(sorted(del_cols)))
        legacy5 = sqlite3.connect(":memory:")
        legacy5.row_factory = sqlite3.Row
        legacy5.executescript(db.DDL_V1.replace(db.DDL_DELETED, ""))  # 模拟 v4 旧库
        legacy5.execute("INSERT INTO meta(key,value) VALUES('schema_version','4')")
        legacy5.commit()
        db._migrate_v5(legacy5)
        old_tables = {r[0] for r in legacy5.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("第七版：v4 → v5 迁移补建回收站表", "deleted_questions" in old_tables)
        db._migrate_v5(legacy5)
        check("第七版：v5 迁移可重复执行不报错", True)
        legacy5.close()

        recycle_q = Q(id="t_recycle", type="single", stem="回收站链路测试题",
                      options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                      answer=["A"], groups=["旧分组"], tags=["旧标签"],
                      note="留档里也要带上备注")
        repository.save_question(recycle_q)
        wrong_ans = grading.grade(recycle_q, ["B"], db.get_settings())
        repository.record_attempt(recycle_q, wrong_ans, ["B"], mode="instant")
        before = repository.deleted_count()
        moved = repository.delete_questions([recycle_q.id])
        check("第七版：删除题目后题库移除、回收站 +1",
              moved == 1 and repository.get_question(recycle_q.id) is None
              and repository.deleted_count() == before + 1)
        kept = [r for r in repository.deleted_rows()
                if r["question_id"] == recycle_q.id]
        check("第七版：留档含完整题干 / 原分组 / 原标签 / 备注",
              bool(kept) and kept[0]["payload"].get("stem") == "回收站链路测试题"
              and kept[0]["payload"].get("groups") == ["旧分组"]
              and kept[0]["payload"].get("tags") == ["旧标签"]
              and kept[0]["payload"].get("note") == "留档里也要带上备注")
        check("第七版：删除题目不丢历史答题记录",
              len(repository.attempts_for_question(recycle_q.id)) >= 1)
        check("第七版：错题库状态随题目一起移除",
              recycle_q.id not in repository.active_wrong_ids())

        rid = int(kept[0]["rid"])
        restored = repository.restore_deleted([rid], ["新分组"], ["新标签"])
        back = repository.get_question(recycle_q.id)
        check("第七版：恢复时只采用用户自选的分组和标签",
              restored["restored"] == 1 and back is not None
              and back.groups == ["新分组"] and back.tags == ["新标签"]
              and "旧分组" not in back.groups and "旧标签" not in back.tags)
        check("第七版：恢复后内容与手写备注完整",
              back.stem == "回收站链路测试题" and back.note == "留档里也要带上备注")
        check("第七版：恢复后回收站里那条已取走",
              repository.deleted_count() == before)

        # 回收站非空时的全库导出 / 清空恢复无损
        repository.delete_questions([recycle_q.id])
        snapshot7 = repository.export_full()
        rows_before = [r for r in repository.deleted_rows()]
        check("第七版：全库导出包含回收站留档",
              len(snapshot7["full_backup"].get("deleted_questions") or []) == len(rows_before)
              and snapshot7["full_backup"].get("deleted_questions"))
        repository.import_full(snapshot7, replace=True)
        check("第七版：清空后恢复回收站留档无损",
              repository.deleted_count() == len(rows_before)
              and any(r["question_id"] == recycle_q.id for r in repository.deleted_rows()))
        repository.clear_deleted()
        check("第七版：清空回收站后为 0", repository.deleted_count() == 0)

        # 静态检查：中文确认、按钮区分、回收站页、统计详情、导航注册
        common_src = (BASE / "app" / "widgets" / "common.py").read_text(encoding="utf-8")
        check("第七版：确认框改中文按钮、默认取消、不误删",
              'yes: str = "确认"' in common_src and 'no: str = "取消"' in common_src
              and "setDefaultButton(no_btn)" in common_src)
        check("第十一版：删除题目的确认与进回收站逻辑保留（工具栏按钮已砍，删除走 Delete 键/右键）",
              all(s in lib_src for s in ("确认删除", "移到回收站"))
              and "goto_recycle" not in lib_src
              and "btn_delete" not in lib_src)
        check("第七版：删除后不再连弹第二个提示框",
              'alert(self, f"已删除' not in lib_src and "已把 {moved} 道题移到回收站" in lib_src)
        recycle_src = (BASE / "app" / "ui" / "recycle_view.py").read_text(encoding="utf-8")
        check("第七版：回收站页可多选、恢复、彻底删除、清空",
              all(s in recycle_src for s in ("RestoreDialog", "NameSelectWidget",
                                             "restore_rows", "purge_rows", "clear_all",
                                             "deleted_rows", "fmt_time")))
        check("第七版：恢复对话框让用户自选 / 新建分组和标签",
              "chosen_groups" in recycle_src and "chosen_tags" in recycle_src
              and "_reuse_original" in recycle_src)
        detail_src = (BASE / "app" / "ui" / "stats_detail.py").read_text(encoding="utf-8")
        check("第七版：统计详情含本题统计、下次多注意、历次答题记录",
              all(s in detail_src for s in ("StatsDetailDialog", "练习次数", "正确率",
                                            "下次多注意", "历次答题记录",
                                            "attempts_for_question", "answer_text")))
        st_src7 = (BASE / "app" / "ui" / "stats_view.py").read_text(encoding="utf-8")
        check("第七版：统计表双击改为打开详情、复制题干移到右键",
              "doubleClicked.connect(self._show_detail)" in st_src7
              and "_on_table_menu" in st_src7 and "_copy_stem" in st_src7)
        mw_src = (BASE / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
        check("第七版：侧栏新增第 8 页「回收站」并在 refresh_all 里刷新",
              '"回收站"' in mw_src and "RecycleView" in mw_src
              and "self.recycle.refresh()" in mw_src)
        check("第七版：数据层导出表含回收站、导入会恢复它",
              '"deleted_questions"' in (BASE / "app" / "repository.py").read_text(encoding="utf-8")
              and "def restore_deleted" in (BASE / "app" / "repository.py").read_text(encoding="utf-8"))

        # ---- 第八版：分组 / 标签描述 + 实体回收站（删除留档可复原题目）----
        tables6 = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("第八版：新库自动建 deleted_entities 实体留档表",
              "deleted_entities" in tables6)
        g_cols = {r[1] for r in conn.execute("PRAGMA table_info(groups_)")}
        t_cols = {r[1] for r in conn.execute("PRAGMA table_info(tags)")}
        check("第八版：groups_ / tags 含 description 描述列",
              "description" in g_cols and "description" in t_cols)

        # v5 → v6 迁移：旧库补 description 列 + 建 deleted_entities
        legacy6 = sqlite3.connect(":memory:")
        legacy6.row_factory = sqlite3.Row
        legacy6.executescript(db.DDL_V1)  # 含 v5 的 deleted_questions
        legacy6.execute("INSERT INTO meta(key,value) VALUES('schema_version','5')")
        legacy6.commit()
        db._migrate_v6(legacy6)
        old6 = {r[0] for r in legacy6.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        old_g = {r[1] for r in legacy6.execute("PRAGMA table_info(groups_)")}
        old_t = {r[1] for r in legacy6.execute("PRAGMA table_info(tags)")}
        check("第八版：v5 → v6 迁移补 description 列且建实体留档表",
              "deleted_entities" in old6
              and "description" in old_g and "description" in old_t)
        db._migrate_v6(legacy6)
        check("第八版：v6 迁移重复执行不报错", True)
        legacy6.close()

        # 描述读写
        repository.set_entity_description("group", "操作系统", "进程、内存、文件系统")
        check("第八版：分组描述可写入",
              repository.entity_description("group", "操作系统") == "进程、内存、文件系统")
        repository.set_entity_description("tag", "三次握手", "TCP 建立连接的过程")
        check("第八版：标签描述可写入",
              repository.entity_description("tag", "三次握手") == "TCP 建立连接的过程")

        # 删除分组/标签：名字进回收站，记录描述与归属题目
        ent_q1 = Q(id="t_ent_1", type="single", stem="实体留档题 1",
                   options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                   answer=["A"], groups=["操作系统"], tags=["三次握手"])
        ent_q2 = Q(id="t_ent_2", type="single", stem="实体留档题 2",
                   options=[{"key": "A", "text": "3"}, {"key": "B", "text": "4"}],
                   answer=["B"], groups=["操作系统"], tags=["三次握手"])
        repository.save_question(ent_q1)
        repository.save_question(ent_q2)
        repository.delete_entity("group", "操作系统")
        del_entities = [r for r in repository.deleted_entities()
                        if r["name"] == "操作系统" and r["kind"] == "group"]
        check("第八版：删除分组后进实体留档且记录描述与原题目清单",
              bool(del_entities)
              and del_entities[0]["description"] == "进程、内存、文件系统"
              and set(del_entities[0]["question_ids"]) == {"t_ent_1", "t_ent_2"}
              and repository.entity_description("group", "操作系统") == ""
              and "操作系统" not in repository.get_question("t_ent_1").groups)
        # 其中一个题目被删，恢复时应跳过
        repository.delete_questions(["t_ent_2"])
        rid6 = int(del_entities[0]["rid"])
        res6 = repository.restore_entity(rid6)
        after_restore = repository.get_question("t_ent_1")
        check("第八版：恢复分组只给仍在题库的题打回名字、已删题跳过",
              res6["restored"] == 1 and res6["skipped"] == 1
              and after_restore is not None
              and "操作系统" in after_restore.groups
              and repository.get_question("t_ent_2") is None
              and repository.entity_description("group", "操作系统") == "进程、内存、文件系统")
        check("第八版：恢复后实体留档里这条已取走",
              not any(r["rid"] == rid6 for r in repository.deleted_entities()))
        repository.delete_questions(["t_ent_1"])

        # 实体留档随全库备份导出 / 恢复
        repository.set_entity_description("tag", "三次握手", "TCP 建立连接的过程")
        repository.delete_entity("tag", "三次握手")
        snapshot8 = repository.export_full()
        export_entities = snapshot8["full_backup"].get("deleted_entities") or []
        check("第八版：全库导出包含实体留档（含描述）",
              any(r["name"] == "三次握手"
                  and r["description"] == "TCP 建立连接的过程"
                  for r in export_entities))
        repository.import_full(snapshot8, replace=True)
        restored_ent = [r for r in repository.deleted_entities()
                        if r["name"] == "三次握手"]
        check("第八版：清空后恢复实体留档无损（描述随留档保存）",
              bool(restored_ent)
              and restored_ent[0]["description"] == "TCP 建立连接的过程")
        repository.clear_deleted_entities()

        # 静态检查：回收站双标签页 + 原题目清单 + 分组/标签描述弹窗
        recycle_src8 = (BASE / "app" / "ui" / "recycle_view.py").read_text(encoding="utf-8")
        check("第八版：回收站含「已删除分组 / 标签」标签页与原题目清单弹窗",
              all(s in recycle_src8 for s in (
                  "EntityQuestionsDialog", "deleted_entities",
                  "restore_entity_rows", "purge_entity_rows",
                  "clear_entity_rows", "查看原题目清单")))
        lib_src8 = (BASE / "app" / "ui" / "library_view.py").read_text(encoding="utf-8")
        check("第八版：题库页可写分组/标签描述、去掉未分组/未分标签、勾选区不进编辑、有取消选择",
              (
                  all(s in lib_src8 for s in (
                      "EntityDetailDialog", "save_description",
                      "VIRTUAL_GROUP_KEYS", "VIRTUAL_TAG_KEYS",
                      "_on_table_double_clicked", "clear_selection",
                      "取消选择"))
                  and '"未分组"' not in lib_src8 and '"未标标签"' not in lib_src8
              ))
        body8 = lib_src8.split("def _on_table_double_clicked", 1)[1]
        check("第八版：双击勾选区（第 0 列）不进编辑",
              "index.column() == 0" in body8 and "return" in body8)
        mw_src8 = (BASE / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
        check("第八版：顶栏「制作题目 / 设置」按当前页高亮、不再常亮",
              all(s in mw_src8 for s in (
                  "make_btn", "settings_btn", "_sync_top_buttons",
                  "NAV[\"制作题目\"]", "NAV[\"设置\"]", "Primary")))
        wv_src8 = (BASE / "app" / "ui" / "wrong_view.py").read_text(encoding="utf-8")
        check("第八版：错题库刷新旁有取消选择",
              "btn_clear_sel" in wv_src8 and "取消选择" in wv_src8
              and "clear_selection" in wv_src8)
        themes_src = (BASE / "app" / "themes.py").read_text(encoding="utf-8")
        check("第八版：浅色/暖杏/经典紫蓝主题 text_dim 与 text_faint 调高对比",
              '"text_dim": "#334155"' in themes_src
              and '"text_faint": "#475569"' in themes_src
              and '"text_dim": "#443D33"' in themes_src
              and '"text_dim": "#DCE5F7"' in themes_src)
        theme_src8 = (BASE / "app" / "theme.py").read_text(encoding="utf-8")
        check("第八版：切换主题后强制所有控件重新 polish",
              "unpolish" in theme_src8 and "polish" in theme_src8
              and "allWidgets" in theme_src8)

        # ---- 第九版：一致性补齐 + 重命名修复 + 回收站可编辑 ----
        tables_src = (BASE / "app" / "widgets" / "tables.py").read_text(encoding="utf-8")
        check("第九版：共享表格组件（勾选守卫 + 空白取消选中 + 右键构造器 + 复制题干）",
              all(s in tables_src for s in ("class DataTableWidget", "class RowActionMenu",
                                             "mouseDoubleClickEvent", "clear_row_selection",
                                             "copy_stem_to_clipboard")))
        wv_src9 = (BASE / "app" / "ui" / "wrong_view.py").read_text(encoding="utf-8")
        check("第九版：错题库主表接入统一组件且右键就绪；进出库历史已迁出到独立窗口",
              all(s in wv_src9 for s in ("DataTableWidget", "_on_table_menu",
                                         "locate_requested", "run_row_action"))
              and "_on_history_menu" not in wv_src9)
        hw_src = (BASE / "app" / "ui" / "history_window.py").read_text(encoding="utf-8")
        check("第十版：进出库历史独立窗口（class HistoryWindow）含三列表与右键、靠信号回连",
              all(s in hw_src for s in ("class HistoryWindow", "DataTableWidget",
                                        "复制时间", "定位到该题目", "查看题目详情",
                                        "locate_requested", "detail_requested")))
        # 第十版：题库页「练习所选」按钮 + 勾选后即开练
        lv_src10 = (BASE / "app" / "ui" / "library_view.py").read_text(encoding="utf-8")
        check("第十版：题库页新增「练习所选」按钮（带 tooltip、按勾选数启用）",
              all(s in lv_src10 for s in ("practice_selected_requested", 'button("练习所选")',
                                          "btn_practice.setEnabled", "_practice_selected")))
        check("第十版：题库页「练习所选」发射勾选 id 列表供主窗开练",
              "_practice_selected_requested.emit(ids)" in lv_src10
              or "practice_selected_requested.emit(ids)" in lv_src10)
        # 第十版：重命名改名无效问题——同名不再静默 no-op，给明确提示
        check("第十版：重命名同名不再静默失败，弹「名称没有变化」提示",
              'alert(self, "名称没有变化' in lv_src10)
        # 第十版：错题库页面「进出库历史」按钮发射独立窗口信号
        check("第十版：错题库「进出库历史」按钮发射 open_history_window_requested（且旧方法体已迁出）",
              all(s in wv_src9 for s in ("open_history_window_requested",
                                         '"进出库历史"'))
              and "_on_history_menu" not in wv_src9)
        wv_body9 = wv_src9.split('"""', 2)[2]  # 跳过模块 docstring，只查代码体
        wv_code9 = "\n".join(line.split("#", 1)[0]
                             for line in wv_body9.splitlines())
        check("第九版：错题库主页面不再有「显示已移出」开关",
              "show_removed" not in wv_code9 and "显示已移出" not in wv_code9
              and "QCheckBox(" not in wv_code9)
        rv_src9 = (BASE / "app" / "ui" / "recycle_view.py").read_text(encoding="utf-8")
        check("第九版：回收站两标签页接入统一组件、右键就绪且可编辑留档",
              all(s in rv_src9 for s in ("DataTableWidget", "_on_table_menu",
                                         "_on_entity_menu", "edit_deleted_question",
                                         "archive_payload")))
        check("第九版：回收站保留信息类 tooltip（删除时归属）且不再有题干重复气泡",
              "删除时归属" in rv_src9
              and "setToolTip(stem)" not in rv_src9 and "setToolTip(value)" not in rv_src9)
        ed_src9 = (BASE / "app" / "ui" / "editor_view.py").read_text(encoding="utf-8")
        repo_src9 = (BASE / "app" / "repository.py").read_text(encoding="utf-8")
        check("第九版：编辑器支持留档编辑模式（保存写回 deleted_questions）",
              "archive_payload" in ed_src9 and "archive_mode" in ed_src9
              and "update_deleted_question" in ed_src9
              and "def update_deleted_question" in repo_src9)
        # 第十版：编辑器布局重排——答案置顶、解析在分组标签之上、下次注意挪到右侧
        i_answer = ed_src9.find('self.answer_box = QGroupBox("答案设置")')
        i_analysis = ed_src9.find('analysis_box = QGroupBox("解析')
        i_meta = ed_src9.find('meta = QGroupBox("分组与标签')
        check("第十版：编辑器答案在「解析」之上、解析在「分组与标签」之上（置顶顺序正确）",
              -1 < i_answer < i_analysis < i_meta)
        check("第十版：编辑器「下次多注意」放进右列（rcol），不再堆在左列",
              "rcol.addWidget(note_box" in ed_src9
              and ed_src9.count("col.addWidget(note_box") == 1  # 唯一出现即 rcol，左列 col 没有
              and "QSplitter(Qt.Horizontal)" in ed_src9)

        # ---- 第十一版：题库页表格四列 + 工具栏砍 5 按钮 + 编辑器选项文案/解析/横线 ----
        lib_src11 = (BASE / "app" / "ui" / "library_view.py").read_text(encoding="utf-8")
        ed_src11 = ed_src9
        check("第十一版：题库表格砍「分组」「标签」两列，只留 ✓/题型/题干/错题",
              'setHorizontalHeaderLabels(["选择", "题型", "题干", "错题"])' in lib_src11
              and "DataTableWidget(0, 4" in lib_src11
              and '", ".join(q.groups)' not in lib_src11
              and '", ".join(q.tags)' not in lib_src11)
        check("第十一版：题库工具栏已无 编辑/删除题目/加入分组/移除分组或标签/回收站 五按钮",
              all(s not in lib_src11 for s in ("btn_edit", "btn_delete", "btn_group",
                                               "btn_recycle"))
              and "btn_practice" in lib_src11 and "btn_new" in lib_src11
              and "self.tree" in lib_src11)  # 左树保留不动
        check("第十一版：题库新增「删除所选 / 移到分组」批量操作（勾选大类后激活，与工具栏竖线隔开）",
              "btn_del_entity" in lib_src11 and "btn_move_entity" in lib_src11
              and "_delete_checked_entities" in lib_src11
              and "_move_checked_entities" in lib_src11)
        check("第十一版：编辑器选项行「正确答案」只显示在被勾选（正确）那一行",
              'QCheckBox("正确答案" if checked else "")' in ed_src11
              and 'check.setText("正确答案" if on else "")' in ed_src11
              and "check.setFixedWidth(112)" in ed_src11)
        check("第十一版：解析紧跟答案、框加高（无 72 封顶、minHeight 110），右栏加横线、左列不被硬性锁宽",
              "setMinimumHeight(110)" in ed_src11
              and "setMaximumHeight(72)" not in ed_src11
              and "QFrame.HLine" in ed_src11
              and "setChildrenCollapsible(False)" in ed_src11
              and "setMinimumWidth(460)" not in ed_src11)
        st_src9 = (BASE / "app" / "ui" / "stats_view.py").read_text(encoding="utf-8")
        check("第九版：练习统计表格接入统一组件、完整题干改存 UserRole",
              "DataTableWidget" in st_src9 and "UserRole" in st_src9
              and "_copy_stem" in st_src9)

        # ---- 第十二版：练习统计勾选列 + 三按钮 + 题型筛选 ----
        st_src12 = st_src9
        check("第十二版：练习统计表格题干前加勾选列（8 列，✓ 在首列、题干第 1 列）",
              'HEADERS = ["选择", "题干", "题型", "练习次数", "答对", "答错", "正确率", "最近练习"]' in st_src12
              and "CHECK_COL = 0" in st_src12 and "STEM_COL = 1" in st_src12
              and "checkbox_col=CHECK_COL" in st_src12
              and "Qt.ItemIsUserCheckable" in st_src12)
        check("第十二版：练习统计新增「全选 / 取消选择 / 练习所选」三按钮",
              all(s in st_src12 for s in ("btn_select_all", "btn_clear_sel", "btn_practice",
                                          "_toggle_select_all", "clear_selection",
                                          "_practice_selected")))
        check("第十二版：练习统计新增题型筛选下拉（全部题型 + 4 种题型），与其它筛选叠加",
              'addItem("全部题型", "")' in st_src12
              and "_match_type" in st_src12 and "type_combo" in st_src12
              and "self._match_type(r, qtype)" in st_src12)
        check("第十二版：练习统计勾选跨筛选全局保留、去重、随机打乱后开练",
              "_checked_ids" in st_src12 and "dict.fromkeys" in st_src12
              and "random.shuffle(ids)" in st_src12
              and "practice_requested.emit(ids)" in st_src12)
        check("第十二版：练习统计勾选列双击只勾选不开详情、完整题干移到第 1 列",
              "if index.column() == CHECK_COL:" in st_src12
              and "self.table.item(index.row(), STEM_COL)" in st_src12
              and "item.setData(Qt.UserRole, r[\"stem\"])" in st_src12)

        # ---- 第十二版：答题结果显示答案时带上题目（四种题型都不漏）----
        gr_src12 = (BASE / "app" / "grading.py").read_text(encoding="utf-8")
        pv_src12 = (BASE / "app" / "ui" / "practice_view.py").read_text(encoding="utf-8")
        check("第十二版：新增 question_prompt() 生成题目全文（四种题型含题干，选择/多选带选项）",
              "def question_prompt(q: Question" in gr_src12
              and 'q.type in ("single", "multiple")' in gr_src12
              and "q.options or []" in gr_src12
              and "（本题无题干）" in gr_src12)
        check("第十二版：结果页 / 整卷总览 / 简答自评页三处都显示「题目」（第十四版改为右侧网格显示插图）",
              pv_src12.count("**题目**") == 3
              and pv_src12.count("question_prompt(q, include_images=False)") == 3
              and "from ..grading import answer_summary, grade, question_prompt" in pv_src12
              and "answer_images = ImageStrip(read_only=True" in pv_src12
              and "result_images = ImageStrip(read_only=True" in pv_src12)

        # ---- 第十二版：制作题目页「添加一空 / 添加要点」点击失效修复 ----
        mk_src12 = (BASE / "app" / "ui" / "maker_view.py").read_text(encoding="utf-8")
        ed_src12 = (BASE / "app" / "ui" / "editor_view.py").read_text(encoding="utf-8")
        check("第十二版：制作题目页添加按钮用 lambda 包一层，避免 clicked(bool) 误传进 text",
              "add.clicked.connect(lambda: self._add_blank())" in mk_src12
              and "add.clicked.connect(lambda: self._add_point())" in mk_src12
              and "add.clicked.connect(self._add_blank)" not in mk_src12
              and "add.clicked.connect(self._add_point)" not in mk_src12)
        check("第十二版：制作页与编辑器的行内按钮改成看得懂的中文文字（不再用 ✕↑↓ 符号）",
              all(x in mk_src12 for x in ('QPushButton("上移")', 'QPushButton("下移")',
                                          'QPushButton("删除")'))
              and all(x in ed_src12 for x in ('QPushButton("上移")', 'QPushButton("下移")',
                                              'QPushButton("删除")'))
              and all(sym not in mk_src12 + ed_src12
                      for sym in ('QPushButton("✕")', 'QPushButton("↑")', 'QPushButton("↓")')))
        check("第十二版：行内按钮带 tooltip 说明作用",
              "删除这一空" in mk_src12 and "把这条要点往上挪一位" in mk_src12
              and "删除这个选项" in ed_src12)

        # ---- 第十二版：开始练习页砍下拉、加按钮、独立选择题目窗口 ----
        pvw_src12 = (BASE / "app" / "ui" / "practice_view.py").read_text(encoding="utf-8")
        mw_src12 = (BASE / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
        pick_src12 = (BASE / "app" / "ui" / "question_picker.py").read_text(encoding="utf-8")
        check("第十二版：开始练习页砍掉分组 / 知识点标签两个下拉，题型题量顺序判卷模式保留",
              "self.group_combo" not in pvw_src12 and "self.tag_combo" not in pvw_src12
              and all(s in pvw_src12 for s in ("self.type_combo", "self.count_spin",
                                               "self.order_combo", "self.mode_combo")))
        check("第十二版：开始练习页新增「查看选择题目…」按钮并打开独立窗口",
              'button("查看选择题目…"' in pvw_src12
              and "def open_picker" in pvw_src12
              and "QuestionPickerWindow" in pvw_src12)
        check("第十二版：三个来源点练习所选只投递题目、不再自动 start()",
              "def _deliver_picked" in mw_src12
              and 'lambda ids: self._deliver_picked(ids, "题库")' in mw_src12
              and 'lambda ids: self._deliver_picked(ids, "练习统计")' in mw_src12
              and "self.practice.start()" not in mw_src12)
        check("第十二版：独立窗口有两个分页按钮、勾选列与右键加入/移除",
              all(s in pick_src12 for s in ("已选择的题目", "所有题库里的所有题",
                                            "加入本次练习", "从本次练习移除",
                                            "ItemIsUserCheckable", "def refresh"))
              and "class QuestionPickerWindow(QDialog)" in pick_src12)
        check("第十二版：开始练习只练清单里勾选的题",
              "picked_checked_ids()" in pvw_src12
              and "filter_questions(ids=picked_ids)" in pvw_src12)
        check("第十二版：本次练习题库窗口默认 1618×1000、打开居中、左右分栏",
              "WINDOW_W = 1618" in pick_src12 and "WINDOW_H = 1000" in pick_src12
              and "center_on_screen" in pick_src12 and "center_on_screen" in pvw_src12
              and "QSplitter(Qt.Horizontal)" in pick_src12)
        check("第十二版：窗口左边是分组/标签树（可勾选、带计数）、右边是题目列表",
              all(s in pick_src12 for s in ("QTreeWidget", "groups_with_count",
                                            "tags_with_count", "entity_question_ids",
                                            "ItemIsUserCheckable", "_matches_tree",
                                            "_on_tree_clicked", "_on_tree_item_changed")))
        check("第十二版：左树分组/标签下展开具体题目、每道题前有可勾选方框",
              "make_question_node" in pick_src12
              and '("q", qid)' in pick_src12
              and "for qid in ids:" in pick_src12
              and 'elif kind == "q" and value:' in pick_src12)
        check("第十二版：树点击/勾选只原地同步勾选状态、绝不重建树（防卡死闪退）",
              "_sync_tree_checks" in pick_src12
              and "QTimer.singleShot(0, self._do_sync)" in pick_src12
              and "blockSignals" in pick_src12
              and "self._fill_tree()" not in pick_src12.split("def _on_tree_clicked")[1].split("def _on_tree_item_changed")[0])

        # ---- 第十二版补丁：详情可编辑 + 已完成的练习 + 详细报告 ----
        report_src12 = (BASE / "app" / "ui" / "session_report.py").read_text(encoding="utf-8")
        repo_src12 = (BASE / "app" / "repository.py").read_text(encoding="utf-8")
        check("第十二版：本次练习题库右键详情能编辑题目（复用全站同一个 QuestionEditor）",
              'button("编辑题目", True)' in pick_src12
              and 'menu.addAction("编辑题目")' in pick_src12
              and "def _open_editor" in pick_src12
              and "edit_requested.emit(qid)" in pick_src12
              and "def _finish" in pick_src12
              and "self.practice.edit_requested.connect(self.open_editor)" in mw_src12
              and "edit_requested = Signal(str)" in pvw_src12)
        check("第十二版：开始练习页新增「已完成的练习」区块，两区块都有「详细报告」",
              'QGroupBox("已完成的练习")' in pvw_src12
              and "self.finished_combo" in pvw_src12
              and pvw_src12.count("详细报告") >= 2
              and "def draft_detail_report" in pvw_src12
              and "def finished_detail_report" in pvw_src12
              and "SessionReportDialog" in pvw_src12)
        check("第十二版：数据层提供已完成会话 / 逐题报告 / 删除记录",
              all(s in repo_src12 for s in ("def finished_sessions", "def session_attempts",
                                            "def session_report_rows", "def delete_session_record")))
        check("第十二版：详细报告窗口逐题展示题目/作答/标准答案/判定/解析",
              "class SessionReportDialog(QDialog)" in report_src12
              and all(s in report_src12 for s in ("question_prompt", "answer_summary",
                                                  "user_answer_text", "**你的作答**",
                                                  "**判定结果**", "要点命中情况")))

        # 需求七：全仓虚拟项只剩注释
        leftovers = []
        for py in (BASE / "app").rglob("*.py"):
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if any(k in code for k in ("__ungrouped__", "__untagged__", "未分组", "未标标签")):
                    leftovers.append(f"{py.name}:{i}")
        check("第九版：全仓不再引用「未分组 / 未标标签」虚拟项（仅剩注释）",
              not leftovers, "、".join(leftovers[:5]))
        # 需求九：全仓不再有「重复可见文字」的悬停气泡（题干 / 单元格原值）
        bubble_left = []
        for py in (BASE / "app" / "ui").rglob("*.py"):
            if any(k in py.read_text(encoding="utf-8")
                   for k in ("setToolTip(stem)", "setToolTip(value)")):
                bubble_left.append(py.name)
        check("第九版：各列表不再弹重复文字的悬停气泡（setToolTip(stem/value) 清零）",
              not bubble_left, "、".join(bubble_left))

        # 需求八：题目 id 仍为 q_ 前缀唯一串，且无按位置重排 id 的逻辑
        schema_src9 = (BASE / "app" / "schema.py").read_text(encoding="utf-8")
        from app.models import Question as Q9
        from app.schema import generated_id
        probe_q = Q9(id="", type="single", stem="第九版：id 规则探针题",
                     options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
                     answer=["A"])
        gid = generated_id(probe_q)
        check("第九版：题目 id 保持 q_ 前缀内容哈希串（不按第几个排）",
              gid.startswith("q_") and len(gid) == 22
              and '"q_" + content_hash' in schema_src9
              and '"q_" + uuid.uuid4().hex[:12]' in repo_src9
              and not any(s in repo_src9 + schema_src9
                          for s in ('f"q{i}', 'f"q{i}"', '"q" + str(i)')))

        # 需求五：重命名 —— 普通改名全链路
        qr1 = Q9(id="v9_rename_a", type="single", stem="第九版重命名用题",
                 options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
                 answer=["A"], groups=["九版旧组"], tags=["九版旧标"])
        repository.save_question(qr1)
        merged_plain = repository.rename_entity("tag", "九版旧标", "九版新标")
        after_q = repository.get_question("v9_rename_a")
        tag_names = [r[0] for r in conn.execute("SELECT name FROM tags").fetchall()]
        check("第九版：改名为全新名字 → 普通改名（表行 + 题目字段）",
              merged_plain is False and "九版新标" in tag_names
              and "九版旧标" not in tag_names
              and after_q is not None and after_q.tags == ["九版新标"])

        # 需求五：重命名撞已存在名 → 合并归属 + 删旧行，绝不静默失败
        qa = Q9(id="v9_merge_a", type="single", stem="合并用题甲",
                options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
                answer=["A"], groups=["九版合并甲"], tags=["九版合并乙"])
        qb = Q9(id="v9_merge_b", type="single", stem="合并用题乙",
                options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
                answer=["A"], groups=["九版合并乙"], tags=["九版合并乙"])
        repository.save_question(qa)
        repository.save_question(qb)
        repository.set_entity_description("group", "九版合并甲", "甲组的描述")
        repository.set_entity_description("group", "九版合并乙", "乙组的描述")
        merged_hit = repository.rename_entity("group", "九版合并甲", "九版合并乙")
        g_names = [r[0] for r in conn.execute("SELECT name FROM groups_").fetchall()]
        after_a = repository.get_question("v9_merge_a")
        check("第九版：重命名撞已有名 → 合并不崩、旧名消失、归属转移无丢失",
              merged_hit is True and "九版合并甲" not in g_names
              and after_a is not None and after_a.groups == ["九版合并乙"]
              and repository.entity_description("group", "九版合并乙") == "乙组的描述")

        # 需求五补充：目标描述为空时合并会带过去
        repository.set_entity_description("group", "九版合并丙", "丙组的描述")
        qc = Q9(id="v9_merge_c", type="single", stem="合并用题丙",
                options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
                answer=["A"], groups=["九版合并丙"])
        repository.save_question(qc)
        repository.set_entity_description("group", "九版合并丁", "")
        merged_carry = repository.rename_entity("group", "九版合并丙", "九版合并丁")
        check("第九版：合并时目标描述为空则带上旧描述",
              merged_carry is True
              and repository.entity_description("group", "九版合并丁") == "丙组的描述"
              and repository.get_question("v9_merge_c").groups == ["九版合并丁"])

        # 需求六：回收站留档可编辑，恢复带编辑后内容
        qd = Q9(id="v9_del_edit", type="single", stem="第九版：留档编辑前的题干",
                options=[{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
                answer=["A"])
        repository.save_question(qd)
        repository.delete_questions(["v9_del_edit"])
        drow = next(r for r in repository.deleted_rows() if r["question_id"] == "v9_del_edit")
        payload9 = dict(drow["payload"])
        payload9["stem"] = "第九版：留档编辑后的题干（改好了）"
        upd_ok = repository.update_deleted_question(int(drow["rid"]), payload9)
        drow2 = next(r for r in repository.deleted_rows() if r["question_id"] == "v9_del_edit")
        check("第九版：编辑留档写回 deleted_questions（payload 与冗余列同步）",
              upd_ok and drow2["stem"] == "第九版：留档编辑后的题干（改好了）"
              and "编辑后" in drow2["payload"]["stem"])
        repository.restore_deleted([int(drow["rid"])], [], [])
        restored_q = repository.get_question("v9_del_edit")
        check("第九版：恢复后带回编辑后的内容",
              restored_q is not None
              and restored_q.stem == "第九版：留档编辑后的题干（改好了）")

        # ---- 第十三版：选择性备份 / 回档 数据层与缺陷回归 ----
        from app import backup as bk13
        from app.config import option_key
        bk_src13 = (BASE / "app" / "backup.py").read_text(encoding="utf-8")
        bv_src13 = (BASE / "app" / "ui" / "backup_view.py").read_text(encoding="utf-8")
        db_src13 = (BASE / "app" / "db.py").read_text(encoding="utf-8")
        repo_src13 = (BASE / "app" / "repository.py").read_text(encoding="utf-8")
        check("第十三版：备份数据层提供构建/校验/预览/回档/快照",
              all(s in bk_src13 for s in ("def build_backup", "def read_backup",
                                          "def read_backup_data", "def preview_rows",
                                          "def restore", "def auto_snapshot",
                                          "def list_snapshots")))
        check("第十三版：备份页可勾选题目、可选附带数据、可选文件、可选回档方式",
              all(s in bv_src13 for s in ("_picked", "QFileDialog.getSaveFileName",
                                          "QFileDialog.getOpenFileName", "QTabWidget",
                                          "合并导入", "整库回档", "自动快照")))
        check("第十三版：备份格式严格校验版本与字段",
              all(s in bk_src13 for s in ("更高版本生成", "schema_version",
                                          "缺少 correct", "BackupError")))
        check("第十三版：修复 .db 恢复高版本库 / 回滚 / 连接半死",
              "schema_version" in db_src13.split("def validate_db_file")[1].split("def restore_from")[0]
              and "shutil.copy2(bak, DB_PATH)" in db_src13
              and "migrate(conn)" in db_src13.split("def connect")[1][:1200])
        merge_src13 = repo_src13.split("def merge_duplicates")[1][:4000]
        check("第十三版：去重合并只合并完整契约相同的题且进回收站留档",
              'if k != "id"' in repo_src13
              and "INSERT INTO deleted_questions" in merge_src13
              and "UPDATE OR IGNORE attempts" in merge_src13)
        check("第十三版：统计分母一致、删分组不改写无关题、留档编辑保留时间戳",
              "def _attempt_meta" in repo_src13
              and "if name not in names:" in repo_src13
              and "self.archive_payload" in (BASE / "app" / "ui" / "editor_view.py").read_text(encoding="utf-8"))
        # 只检查真正的代码行（注释里提到这些字形不算）
        def code_only13(fname: str) -> str:
            return "\n".join(line.split("#", 1)[0] for line in
                              (BASE / "app" / "ui" / fname).read_text(encoding="utf-8").splitlines())
        check("第十三版：全站特殊符号清零（不再出现方块字形）",
              not any(sym in code_only13(f)
                      for f in ("library_view.py", "stats_view.py", "practice_view.py",
                                "question_picker.py", "recycle_view.py", "maker_view.py")
                      for sym in ("✓", "✗", "▎", "★", "●", "○")))
        check("第十三版：新增 option_key 支持超过 26 个选项",
              option_key(25) == "Z" and option_key(26) == "A1" and option_key(27) == "B1")
        check("第十三版：备份页接入主窗口第 9 页且信号连通",
              "self.backup = BackupView()" in mw_src12
              and "self.backup.data_changed.connect(self.refresh_all)" in mw_src12
              and "self.backup.refresh()" in mw_src12)

        # 真实数据层闭环（在临时库里，不碰用户数据）
        import json as json13
        v13_q = Q9(id="v13_selftest_q", type="single", stem="第十三版自测备份题",
                   options=[{"key": "A", "text": "对"}, {"key": "B", "text": "错"}],
                   answer=["A"], note="要保住的备注")
        repository.save_question(v13_q)
        v13_data = bk13.build_backup(["v13_selftest_q"], ["images", "groups_tags"], "自测")
        v13_path = bk13.write_backup(TMP / "v13_selftest.json", v13_data)
        v13_read = bk13.read_backup(v13_path)
        check("第十三版：备份写文件→读回→题目与备注无损",
              len(v13_read["questions"]) == 1
              and v13_read["questions"][0]["note"] == "要保住的备注")
        try:
            bk13.read_backup_data(dict(v13_data, version=99))
            v13_rejected = False
        except bk13.BackupError:
            v13_rejected = True
        check("第十三版：拒绝更高版本备份文件（不写库）", v13_rejected)
        v13_bad = dict(v13_data)
        v13_bad["full_backup"] = dict(v13_data["full_backup"])
        v13_bad["full_backup"]["attempts"] = [{"question_id": "x"}]
        try:
            bk13.read_backup_data(v13_bad)
            v13_bad_rejected = False
        except bk13.BackupError:
            v13_bad_rejected = True
        check("第十三版：缺字段的答题记录被拦下（不再静默记成答错）", v13_bad_rejected)
        v13_snap = bk13.auto_snapshot("自测快照")
        check("第十三版：回档前自动快照可生成并列出",
              v13_snap.is_file() and v13_snap in bk13.list_snapshots())

        # ---- 第十四版：多图数据层 ----
        check("第十四版：SCHEMA_VERSION 升到 7", config.SCHEMA_VERSION >= 7)
        cols14 = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
        check("第十四版：questions 表有 images 列", "images" in cols14)

        from app.models import Question as _Q14
        q14 = _Q14(id="m_multi", type="single", stem="多图题",
                   options=[{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                   answer=["A"],
                   images=["a.png", "b.gif", "c.webp"])
        check("第十四版：image_files 返回全部多图（按序）",
              q14.image_files() == ["a.png", "b.gif", "c.webp"])
        repository.save_question(q14)
        row14 = conn.execute("SELECT image,images FROM questions WHERE id='m_multi'").fetchone()
        check("第十四版：image 存主图、images 存完整数组",
              row14["image"] == "a.png"
              and json.loads(row14["images"]) == ["a.png", "b.gif", "c.webp"])
        loaded14 = repository.get_question("m_multi")
        check("第十四版：读回多图不丢（from_row）",
              loaded14.image_files() == ["a.png", "b.gif", "c.webp"])
        contract14 = q14.to_contract()
        check("第十四版：Data Contract 带 images 字段",
              contract14.get("images") == ["a.png", "b.gif", "c.webp"])
        parsed14, errs14 = schema.validate_question(contract14, 1)
        check("第十四版：validate_question 保留多图",
              not errs14 and parsed14.image_files() == ["a.png", "b.gif", "c.webp"])

        # 老契约只有单图 image：自动兜底成一张，不丢
        legacy14 = {"id": "m_legacy", "type": "single", "stem": "老单图",
                    "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                    "answer": ["A"], "image": "old.png"}
        leg14, legerr14 = schema.validate_question(legacy14, 1)
        check("第十四版：老单图契约自动兜底为 images[0]",
              not legerr14 and leg14.image_files() == ["old.png"])

        # question_prompt 补全部插图（结果页/报告/详情共用）
        prompt14 = grading.question_prompt(loaded14)
        check("第十四版：题目全文带全部插图 markdown",
              all(f"![插图]({n})" in prompt14 for n in ("a.png", "b.gif", "c.webp")))

        # ---- 第十四版修订：练习页文本侧不带图，插图交给右侧网格 ----
        clean14 = grading.question_prompt(loaded14, include_images=False)
        check("第十四版修订：分栏页面的题目全文不带插图 markdown",
              "![插图](" not in clean14 and "多图题" in clean14)
        old_stem14 = _Q14(id="m_oldstem", type="single", stem="题干文字\n\n![插图](old.png)",
                              options=[], answer=[], images=["old.png"])
        check("第十四版修订：老题干里残留的图片 markdown 会被剥掉",
              "![插图](" not in grading.question_prompt(old_stem14, include_images=False)
              and "题干文字" in grading.question_prompt(old_stem14, include_images=False))
        pv_src14 = (BASE / "app" / "ui" / "practice_view.py").read_text(encoding="utf-8")
        check("第十四版修订：答题 / 结果 / 自评 / 总览四页都有只读右侧插图网格",
              all(f"{n}_images = ImageStrip(read_only=True" in pv_src14
                  for n in ("answer", "result", "review", "summary"))
              and pv_src14.count("set_files(q.image_files())") == 4)
        mk_src14 = (BASE / "app" / "ui" / "maker_view.py").read_text(encoding="utf-8")
        ed_src14 = (BASE / "app" / "ui" / "editor_view.py").read_text(encoding="utf-8")
        check("第十四版修订：制作页 / 编辑器预览不再把图片塞进选项附近",
              "不再把图片 markdown 塞进预览" in mk_src14
              and "插图在右侧网格面板里显示" in ed_src14
              and "![插图]({name})" not in mk_src14)
        strip_src14 = (BASE / "app" / "widgets" / "image_strip.py").read_text(encoding="utf-8")
        check("第十四版修订：插图组件按组件宽高自动算网格列数与缩略图边长",
              all(s in strip_src14 for s in (
                  "def _grid_plan", "QGridLayout", "_MAX_COLS", "movie.start()",
                  "read_only", "setScaledSize")))

        # gif/webp 后缀都在格式白名单里
        from app.widgets.image_strip import IMAGE_SUFFIXES
        check("第十四版：图片面板支持 gif/webp/png/jpg",
              {".gif", ".webp", ".png", ".jpg"} <= IMAGE_SUFFIXES)

        # ---- 第十四版本轮：插图一整板 + 三个详情窗口动图 + 版本归属 ----
        strip_src14b = (BASE / "app" / "widgets" / "image_strip.py").read_text(encoding="utf-8")
        check("第十四版：插图面板取消缩略图上限、按矩形格子均分铺满一整板",
              "def _grid_rect" in strip_src14b
              and "_MAX_CELL" not in strip_src14b
              and "setFixedSize(cw, ch)" in strip_src14b)
        check("第十四版：插图面板不显示计数标题与文件名编号",
              "self.title.hide()" in strip_src14b
              and "ImageCardName" not in strip_src14b
              and "（{len(self._files)} 张）" not in strip_src14b)
        for f14b, cap14b in (("session_report.py", "练习详细报告"),
                             ("wrong_detail.py", "错题详情"),
                             ("question_picker.py", "题目详情")):
            src14b = (BASE / "app" / "ui" / f14b).read_text(encoding="utf-8")
            check(f"第十四版：{cap14b}窗口插图改到右侧动图板",
                  "ImageStrip(read_only=True)" in src14b
                  and "![插图]" not in src14b)
        check("第十四版：版本号升到 v14.0.0 / 第十四版",
              config.APP_VERSION == "14.0.0" and config.APP_EDITION == "第十四版")

    except Exception:
        # 第十三版：旧版只有 try/finally，中途崩溃不打印汇总，排查困难
        import traceback
        traceback.print_exc()
        check("自测整体执行未抛异常", False, "见上方堆栈")
    finally:
        db.close()
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "、".join(failed))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
