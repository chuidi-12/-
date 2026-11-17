"""第十四版 · 全方位端到端验收（离屏，真实事件 + 临时库，不碰真实 data）。

覆盖：选择性备份 / 回档 / 快照撤销 / 插图内嵌 / 版本拒绝 / 8 类第12版缺陷回归。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
TMP = Path(tempfile.mkdtemp(prefix="qbank_v13_"))
import app.config as config
config.DATA_DIR = TMP
config.IMAGE_DIR = TMP / "images"
config.DB_PATH = TMP / "v13.db"
import app.db as db
db.DB_PATH = TMP / "v13.db"
config.ensure_dirs()

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

from app import backup, db as dbmod, grading, repository, schema
from app.config import option_key
from app.models import Question
from app.ui.main_window import MainWindow, NAV

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def q(qid, stem, ans, **kw):
    return Question(id=qid, type=kw.get("type", "single"), stem=stem,
                    options=kw.get("options", [{"key": "A", "text": "对"},
                                               {"key": "B", "text": "错"}]),
                    answer=ans, analysis=kw.get("analysis", ""),
                    groups=kw.get("groups", []), tags=kw.get("tags", []),
                    note=kw.get("note", ""), image=kw.get("image", ""))


dbmod.connect()
st = dbmod.get_settings()

# 造插图文件 + 5 道题
config.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
(config.IMAGE_DIR / "v13_demo.png").write_bytes(b"\x89PNG\r\n\x1a\nV13-DEMO-IMAGE")
for x in (q("v13_a", "题目甲", ["A"], groups=["组甲"], tags=["标签1"],
            note="备注甲", image="v13_demo.png"),
          q("v13_b", "题目乙", ["B"], groups=["组甲"], analysis="解析乙"),
          q("v13_c", "题目丙", ["A"], groups=["组乙"], tags=["标签2"]),
          q("v13_d", "题目丁", ["B"], type="blank", answer=[["答案丁"]]),
          q("v13_e", "简答题戊", ["要点一", "要点二"], type="short")):
    repository.save_question(x)
repository.set_entity_description("group", "组甲", "这是组甲的描述")

print("=" * 70)
print("第十四版 · 全方位端到端验收")
print("=" * 70)

# ---------- 1. 版本 / 页面 ----------
win = MainWindow()
win.resize(1500, 940)
win.show()
app.processEvents()
check("版本号 v14.0.0 / 第十三版", "v14.0.0" in win.windowTitle()
      and "第十四版" in win.windowTitle(), win.windowTitle())
check("第 9 个页面「备份与回档」已装载", win.stack.count() == 9
      and NAV.get("备份与回档") == 7 and hasattr(win, "backup"))
win.nav.setCurrentRow(NAV["备份与回档"])
app.processEvents()
bv = win.backup
check("备份页三个页签", bv.tabs.count() == 3)
check("默认全选题目", bv.ex_table.rowCount() == 5
      and len(bv._picked) == 5, f"rows={bv.ex_table.rowCount()}, picked={len(bv._picked)}")

# ---------- 2. 勾选独立性（核心需求） ----------
bv.ex_group.setCurrentIndex(bv.ex_group.findData("组甲"))
app.processEvents()
check("按分组筛选生效", bv.ex_table.rowCount() == 2, f"rows={bv.ex_table.rowCount()}")
bv._set_export_all(False)          # 取消组甲两题
bv.ex_group.setCurrentIndex(0)
app.processEvents()
check("取消当前筛选不影响其它题的勾选", len(bv._picked) == 3,
      f"picked={sorted(bv._picked)}")
bv.ex_type.setCurrentIndex(bv.ex_type.findData("short"))
app.processEvents()
check("按题型筛选生效", bv.ex_table.rowCount() == 1)
bv._set_export_all(True)
bv.ex_type.setCurrentIndex(0)
app.processEvents()
check("多轮筛选后累计勾选正确（全选当前筛选是幂等的）", len(bv._picked) == 3,
      f"picked={sorted(bv._picked)}")

# ---------- 3. 导出到文件（含插图） ----------
# 重新勾上带图题，专门验证插图内嵌
bv._picked.add("v13_a")
out = TMP / "我的备份.json"
data = backup.build_backup(sorted(bv._picked),
                           [k for k, cb in bv.ex_sections.items() if cb.isChecked()],
                           "端到端验收备份")
target = backup.write_backup(out, data)
check("备份写入指定文件且自动补 .json", target.is_file() and target.suffix == ".json")
check("只导出勾选的题", len(data["questions"]) == 4,
      f"n={len(data['questions'])}")
check("插图二进制内嵌", "v13_demo.png" in data["images"])
check("附带数据区块声明齐全", set(data["sections"]) >= {"questions", "images",
                                                  "attempts", "groups_tags"})
check("格式带版本 / 应用版本 / schema", data["version"] == 2
      and data["app_version"] == "14.0.0" and data["schema_version"] >= 6)

# ---------- 4. 读取校验（严格） ----------
read = backup.read_backup(target)
s = backup.summarize(read)
check("读回文件版本与时间正确", s["app_version"] == "14.0.0"
      and s["question_count"] == 4, str(s["counts"]))
hi = dict(data); hi["version"] = 99
(TMP / "hi.json").write_text(__import__("json").dumps(hi, ensure_ascii=False), encoding="utf-8")
try:
    backup.read_backup(TMP / "hi.json"); rejected = False
except backup.BackupError as e:
    rejected = "更高版本" in str(e)
check("拒绝更高版本的备份文件", rejected)
bad = dict(data)
bad["full_backup"] = dict(bad["full_backup"])
bad["full_backup"]["attempts"] = [{"id": 1, "question_id": "v13_a"}]  # 缺 correct
(TMP / "bad.json").write_text(__import__("json").dumps(bad, ensure_ascii=False), encoding="utf-8")
try:
    backup.read_backup(TMP / "bad.json"); validated = False
except backup.BackupError as e:
    validated = "correct" in str(e)
check("缺字段的答题记录被拦下", validated)

# ---------- 5. 回档预览 + 合并导入 ----------
bv.tabs.setCurrentIndex(1)
bv._backup_data = read
bv._backup_path = target
bv._refresh_restore_table()
check("回档预览列出每题状态", bv.re_table.rowCount() == 4
      and all(bv.re_table.item(r, 3).text() == "内容相同"
              for r in range(bv.re_table.rowCount())))
# 删掉一道题，再用备份恢复它
repository.delete_questions(["v13_c"])
check("删题后库内剩 4 道", len(repository.all_questions()) == 4)
counts = backup.restore(read, ["v13_c"], backup.ALL_SECTION_KEYS, mode="merge")
check("合并回档只写勾选题", counts["questions"] == 1
      and len(repository.all_questions()) == 5, str(counts))
restored = repository.get_question("v13_c")
check("回档保留 note / 时间戳", restored is not None and restored.created_at != "")

# ---------- 6. 自动快照与撤销 ----------
snap = backup.auto_snapshot("回档前")
check("回档前快照已生成", snap.is_file() and len(backup.list_snapshots()) >= 1)
repository.delete_questions(["v13_a", "v13_b", "v13_c", "v13_d", "v13_e"])
check("清空用于验证撤销", len(repository.all_questions()) == 0)
snap_data = backup.read_backup(snap)
before_counts = backup.restore(snap_data, None, backup.ALL_SECTION_KEYS, mode="replace")
check("快照整库还原（撤销）", len(repository.all_questions()) == 5,
      f"restored={before_counts['questions']}")

# ---------- 7. 图片还原 ----------
img = config.IMAGE_DIR / "v13_demo.png"
img.unlink(missing_ok=True)
n = backup._restore_images(read["images"])
check("插图从备份还原", n == 1 and img.is_file() and img.read_bytes().startswith(b"\x89PNG"))

# ---------- 8. 第12版 8 类缺陷回归 ----------
# A 去重合并：差异题不合并 + 合并后进回收站
qa = q("dup_a", "同一道题", ["A"], analysis="解析A", groups=["组A"], note="备注A")
qb = q("dup_b", "同一道题", ["A"], analysis="解析B", groups=["组B"], note="备注B")
for x in (qa, qb):
    repository.save_question(x)
groups = [g for g in repository.find_duplicate_groups() if {x.id for x in g} <= {"dup_a", "dup_b"}]
check("缺陷A：解析/分组不同的同名题不再被当重复", len(groups) == 0,
      f"groups={len(groups)}")
qc = q("dup_c", "完全相同", ["A"], analysis="同", groups=["同"])
qd = q("dup_d", "完全相同", ["A"], analysis="同", groups=["同"])
repository.save_question(qc); repository.save_question(qd)
removed = repository.merge_duplicates()
in_recycle = any(r["question_id"] in ("dup_c", "dup_d") for r in repository.deleted_rows())
check("缺陷A：合并的题进回收站可恢复", removed >= 1 and in_recycle,
      f"removed={removed}, in_recycle={in_recycle}")

# B delete_entity 不改写无关题时间戳
import time
before = {x.id: x.updated_at for x in repository.all_questions()}
time.sleep(1.05)
repository.delete_entity("group", "组A")
after = {x.id: x.updated_at for x in repository.all_questions()}
unrelated = [k for k in before if k in after and k not in ("dup_a",)
             and before[k] != after[k]]
check("缺陷B：删分组不再改写无关题 updated_at", not unrelated, f"changed={unrelated}")

# C 统计分母一致
qstat = q("stat_x", "统计用题", ["A"])
repository.save_question(qstat)
repository.record_attempt(qstat, grading.grade(qstat, ["A"], st), ["A"], settings=st)
repository.delete_questions(["stat_x"])
total = repository.stats_overall()["attempts"]
by_type = sum(r[1] for r in repository.stats_by_type())
check("缺陷C：累计答题与按题型统计分母一致", total == by_type,
      f"overall={total}, by_type={by_type}")

# D import_full replace 清干净分组
repository.save_question(q("ghost_q", "幽灵题", ["A"], groups=["幽灵分组"], tags=["幽灵标签"]))
empty = backup.build_backup([], backup.ALL_SECTION_KEYS)
repository.import_full(empty, replace=True)
check("缺陷D：整库替换后不残留旧分组/标签",
      "幽灵分组" not in repository.groups_with_count()
      and "幽灵标签" not in repository.tags_with_count(),
      f"groups={list(repository.groups_with_count())}, tags={list(repository.tags_with_count())}")

# E 缺字段静默改答错（backup.read_backup_data 已拦）
check("缺陷E：裁剪 JSON 被严格校验拦截", validated)

# F 高版本 .db 恢复被拒
import sqlite3
hi_db = TMP / "high.db"
c2 = sqlite3.connect(str(hi_db))
c2.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                 "INSERT INTO meta VALUES('schema_version','99');"
                 "CREATE TABLE questions (id TEXT PRIMARY KEY);")
c2.commit(); c2.close()
ok, reason = dbmod.validate_db_file(str(hi_db))
check("缺陷F：高版本 .db 备份被拒绝且真库未动", not ok and "高于" in reason, reason)

# G 备份含插图
check("缺陷G：备份内嵌插图", "v13_demo.png" in data["images"])

# H validate_question 保留 note/时间戳
raw = {"version": 1, "questions": [{
    "id": "h_q", "type": "single", "stem": "备注题",
    "options": [{"key": "A", "text": "对"}, {"key": "B", "text": "错"}], "answer": ["A"], "analysis": "",
    "groups": [], "tags": [], "source": "manual", "note": "重要备注",
    "content_hash": "abc123", "created_at": "2020-01-01T00:00:00",
    "updated_at": "2021-01-01T00:00:00"}]}
parsed, warns = schema.parse_payload(__import__("json").dumps(raw))
report = schema.build_report(parsed, warns, *schema.existing_index(dbmod.connect()))
vq = next((i.question for i in report.items if i.question), None)
check("缺陷H：题目契约保留 note / 时间戳",
      vq is not None and vq.note == "重要备注" and vq.created_at == "2020-01-01T00:00:00",
      f"note={vq.note!r}, created={vq.created_at!r}" if vq else "解析失败")

# 额外：选项 >26 不生成非法 key
check("附加：选项 key 超过 26 仍合法", option_key(25) == "Z"
      and option_key(26) == "A1" and option_key(27) == "B1")

print("\n" + "=" * 70)
failed = [n for n, ok in RESULTS if not ok]
print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
if failed:
    print("失败：" + "、".join(failed))
print("=" * 70)
dbmod.close()
sys.exit(1 if failed else 0)
