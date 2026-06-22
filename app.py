# app.py - 自动排班系统（全职优先版）
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from ortools.sat.python import cp_model
from io import BytesIO
import json

st.set_page_config(
    page_title="自动排班系统",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==================== 强制重置 ====================
for key in list(st.session_state.keys()):
    del st.session_state[key]

# ==================== 人员名单 ====================
PERSON_NAMES = [
    # 全能人员（20人）索引 0-18
    "Flora Feng", "Ivy Chen", "Yolanda Yu", "Vivian You", "Eddie Yang",
    "Yulia Tang", "Lusi Cai", "Peter Li", "Donnie Wu", "Sam Jiang",
    "England Chen", "Zac Yang", "Riky Ye", "Celine Li", "Hope He",
    "Sama Liu", "Yuki Jiang", "Jessica Dong", "Riley Ren",
    # 只T25/T16（3人）索引 19-21
    "Catherine Yeung", "Frankie Wong", "Cecilia Szeto",
    # 禁夜班（1人）索引 22
    "Joyce Luk",
    # 兼职-只FC（1人）索引 25
    "Jane Wang",
    # 兼职-全能（1人）索引 24
    "Edward Liu",
    # 只FC3（1人）索引 23
    "Clara Fong"
]

st.session_state.person_names = PERSON_NAMES

# ==================== 索引配置 ====================
ONLY_T25_T16_INDICES = [19, 20, 21]
UNABLE_NIGHT_INDEX = 22
PARTTIME_FC_ONLY_INDEX = 25
PARTTIME_FLEXIBLE_INDEX = 24
ONLY_FC3_INDEX = 23
FULLTIME_INDICES = list(range(24))  # 索引0-23为全职
PARTTIME_INDICES = [24, 25]     # 索引24-25为兼职（Jane Wang, Edward Liu）


def load_previous_schedule(uploaded_file, person_names):
    """从上传的文件加载上个月排班数据"""
    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith('.xlsx'):
                df = pd.read_excel(uploaded_file, engine='openpyxl')
                last_three_days = {}
                for name in person_names:
                    if name in df.columns:
                        person_data = df[name].tail(3).tolist()
                        person_data = [str(x) if pd.notna(x) else "休息" for x in person_data]
                        last_three_days[name] = person_data
                return last_three_days
            elif uploaded_file.name.endswith('.json'):
                data = json.load(uploaded_file)
                return data.get('last_three_days', {})
        except Exception as e:
            st.error(f"读取文件失败: {e}")
            return None
    return None


class ShiftScheduler:
    def __init__(self, year, month, person_names, target_hours=167, max_hours=180,
                 min_hours=167, night_rest_days=3, previous_schedule=None,
                 prioritize_fulltime=True):

        self.year = year
        self.month = month
        self.person_names = person_names
        self.total_people = len(person_names)
        self.target_hours = target_hours
        self.max_hours = max_hours
        self.min_hours = min_hours
        self.night_rest_days = night_rest_days
        self.previous_schedule = previous_schedule
        self.prioritize_fulltime = prioritize_fulltime

        self.num_fulltime = 25
        self.num_parttime = 2

        # 班次定义
        self.shift_night = "N"
        self.shift_fc = "FC"
        self.shift_fc3 = "FC3"
        self.shift_t16 = "T16"
        self.shift_t25 = "T25"
        self.shift_t38 = "T38"
        self.shift_off = "休息"

        self.all_shifts = [self.shift_night, self.shift_fc, self.shift_fc3,
                          self.shift_t16, self.shift_t25, self.shift_t38, self.shift_off]
        self.shift_to_index = {s: i for i, s in enumerate(self.all_shifts)}
        self.hours = [14, 8, 11, 11, 11, 11, 0]

        self.days_in_month = self._get_days_in_month()
        self.dates = [datetime(year, month, d + 1) for d in range(self.days_in_month)]

        self.day_config = {
            0: {"night": 3, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            1: {"night": 3, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            2: {"night": 3, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            3: {"night": 3, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            4: {"night": 2, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            5: {"night": 2, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            6: {"night": 2, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4}
        }

        self.fc3_allowed_next = [
            self.shift_to_index[self.shift_fc3],
            self.shift_to_index[self.shift_night],
            self.shift_to_index[self.shift_off]
        ]

    def _get_days_in_month(self):
        if self.month == 12:
            next_month = datetime(self.year + 1, 1, 1)
        else:
            next_month = datetime(self.year, self.month + 1, 1)
        return (next_month - datetime(self.year, self.month, 1)).days

    def _add_count_constraint(self, model, day, shift_idx, target):
        vars_list = []
        for p in range(self.total_people):
            b = model.NewBoolVar(f'c_{day}_{p}_{shift_idx}')
            model.Add(self.shifts[(p, day)] == shift_idx).OnlyEnforceIf(b)
            model.Add(self.shifts[(p, day)] != shift_idx).OnlyEnforceIf(b.Not())
            vars_list.append(b)
        model.Add(sum(vars_list) == target)

    def run(self):
        model = cp_model.CpModel()

        # 决策变量
        self.shifts = {}
        for p in range(self.total_people):
            for d in range(self.days_in_month):
                self.shifts[(p, d)] = model.NewIntVar(0, len(self.all_shifts) - 1, f"s_{p}_{d}")

        # 工时变量
        total_hours = {}
        for p in range(self.total_people):
            total_hours[p] = model.NewIntVar(0, self.max_hours * self.days_in_month, f"th_{p}")

        # 计算工时
        for p in range(self.total_people):
            hour_terms = []
            for d in range(self.days_in_month):
                for s_idx, h in enumerate(self.hours):
                    b = model.NewBoolVar(f'h_{p}_{d}_{s_idx}')
                    model.Add(self.shifts[(p, d)] == s_idx).OnlyEnforceIf(b)
                    model.Add(self.shifts[(p, d)] != s_idx).OnlyEnforceIf(b.Not())
                    hour_terms.append(h * b)
            model.Add(total_hours[p] == sum(hour_terms))

        night_idx = self.shift_to_index[self.shift_night]
        off_idx = self.shift_to_index[self.shift_off]
        fc_idx = self.shift_to_index[self.shift_fc]
        fc3_idx = self.shift_to_index[self.shift_fc3]
        t16_idx = self.shift_to_index[self.shift_t16]
        t25_idx = self.shift_to_index[self.shift_t25]
        t38_idx = self.shift_to_index[self.shift_t38]

        # ========== 跨月连续约束 ==========
        if self.previous_schedule:
            for p, name in enumerate(self.person_names):
                if name in self.previous_schedule:
                    prev_schedule = self.previous_schedule[name]
                    for offset in range(min(3, self.days_in_month)):
                        if offset < len(prev_schedule):
                            prev_shift = prev_schedule[offset]
                            if prev_shift in self.shift_to_index:
                                if prev_shift == self.shift_off:
                                    model.Add(self.shifts[(p, offset)] == self.shift_to_index[prev_shift])
                                elif prev_shift == self.shift_night:
                                    model.Add(self.shifts[(p, offset)] == self.shift_to_index[prev_shift])
                                elif prev_shift == self.shift_fc3:
                                    model.Add(self.shifts[(p, offset)] == self.shift_to_index[prev_shift])
                                else:
                                    model.Add(self.shifts[(p, offset)] != off_idx)

        # ========== 人员限制 ==========

        # 1. Joyce Luk不能上夜班
        for d in range(self.days_in_month):
            model.Add(self.shifts[(UNABLE_NIGHT_INDEX, d)] != night_idx)

        # 2. 只T25/T16的人
        for p in ONLY_T25_T16_INDICES:
            for d in range(self.days_in_month):
                model.Add(self.shifts[(p, d)] != night_idx)
                model.Add(self.shifts[(p, d)] != fc_idx)
                model.Add(self.shifts[(p, d)] != fc3_idx)
                model.Add(self.shifts[(p, d)] != t38_idx)

        # 3. Clara Fong 只上FC3/休息
        for d in range(self.days_in_month):
            model.Add(self.shifts[(ONLY_FC3_INDEX, d)] != night_idx)
            model.Add(self.shifts[(ONLY_FC3_INDEX, d)] != fc_idx)
            model.Add(self.shifts[(ONLY_FC3_INDEX, d)] != t16_idx)
            model.Add(self.shifts[(ONLY_FC3_INDEX, d)] != t25_idx)
            model.Add(self.shifts[(ONLY_FC3_INDEX, d)] != t38_idx)

        # 4. Jane Wang（兼职）只上FC/休息，周末休息
        for d in range(self.days_in_month):
            model.Add(self.shifts[(PARTTIME_FC_ONLY_INDEX, d)] != night_idx)
            model.Add(self.shifts[(PARTTIME_FC_ONLY_INDEX, d)] != fc3_idx)
            model.Add(self.shifts[(PARTTIME_FC_ONLY_INDEX, d)] != t16_idx)
            model.Add(self.shifts[(PARTTIME_FC_ONLY_INDEX, d)] != t25_idx)
            model.Add(self.shifts[(PARTTIME_FC_ONLY_INDEX, d)] != t38_idx)
            if d % 7 >= 4:
                model.Add(self.shifts[(PARTTIME_FC_ONLY_INDEX, d)] == off_idx)

        # ========== 每天班次需求 ==========
        for d in range(self.days_in_month):
            weekday = d % 7
            cfg = self.day_config[weekday]
            self._add_count_constraint(model, d, night_idx, cfg["night"])
            self._add_count_constraint(model, d, fc_idx, cfg["fc"])
            self._add_count_constraint(model, d, fc3_idx, cfg["fc3"])
            self._add_count_constraint(model, d, t38_idx, cfg["t38"])
            self._add_count_constraint(model, d, t16_idx, cfg["t16"])
            self._add_count_constraint(model, d, t25_idx, cfg["t25"])

        # ========== N后休息3天 ==========
        for p in range(self.total_people):
            for d in range(self.days_in_month - self.night_rest_days):
                night_shift = model.NewBoolVar(f'night_{p}_{d}')
                model.Add(self.shifts[(p, d)] == night_idx).OnlyEnforceIf(night_shift)
                model.Add(self.shifts[(p, d)] != night_idx).OnlyEnforceIf(night_shift.Not())
                for rd in range(d + 1, d + self.night_rest_days + 1):
                    model.Add(self.shifts[(p, rd)] == off_idx).OnlyEnforceIf(night_shift)

        # ========== 上3休3 ==========
        for p in range(self.total_people):
            for d in range(self.days_in_month - 3):
                work_vars = []
                for i in range(4):
                    is_work = model.NewBoolVar(f'work_{p}_{d}_{i}')
                    model.Add(self.shifts[(p, d + i)] != off_idx).OnlyEnforceIf(is_work)
                    model.Add(self.shifts[(p, d + i)] == off_idx).OnlyEnforceIf(is_work.Not())
                    work_vars.append(is_work)
                model.Add(sum(work_vars) <= 3)

            for d in range(self.days_in_month - 5):
                work3_vars = []
                for i in range(3):
                    is_work = model.NewBoolVar(f'work3_{p}_{d}_{i}')
                    model.Add(self.shifts[(p, d + i)] != off_idx).OnlyEnforceIf(is_work)
                    model.Add(self.shifts[(p, d + i)] == off_idx).OnlyEnforceIf(is_work.Not())
                    work3_vars.append(is_work)

                all_work3 = model.NewBoolVar(f'all_work3_{p}_{d}')
                model.Add(sum(work3_vars) == 3).OnlyEnforceIf(all_work3)
                model.Add(sum(work3_vars) != 3).OnlyEnforceIf(all_work3.Not())

                for rd in range(3, 6):
                    if d + rd < self.days_in_month:
                        model.Add(self.shifts[(p, d + rd)] == off_idx).OnlyEnforceIf(all_work3)

        # ========== FC3后限制 ==========
        for p in range(self.total_people):
            for d in range(self.days_in_month - 1):
                is_fc3 = model.NewBoolVar(f'fc3_{p}_{d}')
                model.Add(self.shifts[(p, d)] == fc3_idx).OnlyEnforceIf(is_fc3)
                model.Add(self.shifts[(p, d)] != fc3_idx).OnlyEnforceIf(is_fc3.Not())

                allowed_conditions = []
                for allowed_shift in self.fc3_allowed_next:
                    is_allowed = model.NewBoolVar(f'fc3_allowed_{p}_{d}_{allowed_shift}')
                    model.Add(self.shifts[(p, d + 1)] == allowed_shift).OnlyEnforceIf(is_allowed)
                    model.Add(self.shifts[(p, d + 1)] != allowed_shift).OnlyEnforceIf(is_allowed.Not())
                    allowed_conditions.append(is_allowed)
                model.AddBoolOr(allowed_conditions).OnlyEnforceIf(is_fc3)

        # ========== 全职人员工时约束 ==========
        # 全职人员（索引0-24）工时必须 >= min_hours（167小时）
        for p in FULLTIME_INDICES:
            model.Add(total_hours[p] >= self.min_hours)
            model.Add(total_hours[p] <= self.max_hours)

        # ========== 优先全职排班（通过目标函数实现） ==========

        # 目标1：工时偏差（尽量接近目标工时）
        hours_penalty = model.NewIntVar(0, 1000000, "hours_penalty")
        hours_diff = []
        for p in FULLTIME_INDICES:
            diff = model.NewIntVar(-self.max_hours * 2, self.max_hours * 2, f"diff_{p}")
            model.Add(diff == total_hours[p] - self.target_hours)
            abs_diff = model.NewIntVar(0, self.max_hours * 2, f"abs_{p}")
            model.AddAbsEquality(abs_diff, diff)
            hours_diff.append(abs_diff)
        model.Add(hours_penalty == sum(hours_diff))

        # 目标2：夜班均衡（全职人员）
        night_counts = []
        for p in FULLTIME_INDICES:
            if p == UNABLE_NIGHT_INDEX:
                continue
            if p in ONLY_T25_T16_INDICES:
                continue
            if p == ONLY_FC3_INDEX:
                continue
            nc = model.NewIntVar(0, self.days_in_month, f"nc_{p}")
            terms = []
            for d in range(self.days_in_month):
                b = model.NewBoolVar(f'nb_{p}_{d}')
                model.Add(self.shifts[(p, d)] == night_idx).OnlyEnforceIf(b)
                model.Add(self.shifts[(p, d)] != night_idx).OnlyEnforceIf(b.Not())
                terms.append(b)
            model.Add(nc == sum(terms))
            night_counts.append(nc)

        # ========== 目标3：优先全职，兼职作为补充 ==========
        # 兼职工时尽量少（权重低），全职工时偏差权重高
        # 这样求解器会优先让全职人员上班，兼职只在必要时补充

        # 兼职工时惩罚（越小越好，但需要满足班次需求）
        parttime_penalty = model.NewIntVar(0, 5000, "parttime_penalty")
        parttime_hours = []
        for p in PARTTIME_INDICES:
            if p == PARTTIME_FC_ONLY_INDEX:
                continue  # Jane Wang 本来就有限制
            pt_hours = model.NewIntVar(0, 500, f"pt_{p}")
            model.Add(pt_hours == total_hours[p])
            parttime_hours.append(pt_hours)
        model.Add(parttime_penalty == sum(parttime_hours))

        if night_counts:
            max_night = model.NewIntVar(0, self.days_in_month, "max_n")
            min_night = model.NewIntVar(0, self.days_in_month, "min_n")
            model.AddMaxEquality(max_night, night_counts)
            model.AddMinEquality(min_night, night_counts)
            night_penalty = model.NewIntVar(0, self.days_in_month, "np")
            model.Add(night_penalty == max_night - min_night)
            # 全职工时偏差权重最高(10)，夜班均衡其次(1)，兼职工时惩罚最小(0.01)
            model.Minimize(10 * hours_penalty + 1 * night_penalty + parttime_penalty)
        else:
            model.Minimize(10 * hours_penalty + parttime_penalty)

        # ========== 求解 ==========
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 300

        status = solver.Solve(model)

        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            return self._generate_output(solver)
        else:
            return None

    def _generate_output(self, solver):
        rows = []
        for i, d in enumerate(range(self.days_in_month)):
            row = [self.dates[i].strftime("%m/%d"),
                   ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][self.dates[i].weekday()]]
            for p in range(self.total_people):
                shift_idx = solver.Value(self.shifts[(p, d)])
                row.append(self.all_shifts[shift_idx])
            rows.append(row)

        columns = ["日期", "星期"] + self.person_names
        df_schedule = pd.DataFrame(rows, columns=columns)

        night_idx = self.shift_to_index[self.shift_night]
        fc_idx = self.shift_to_index[self.shift_fc]
        fc3_idx = self.shift_to_index[self.shift_fc3]
        t16_idx = self.shift_to_index[self.shift_t16]
        t25_idx = self.shift_to_index[self.shift_t25]
        t38_idx = self.shift_to_index[self.shift_t38]

        stats = []
        for p, name in enumerate(self.person_names):
            night_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == night_idx)
            fc_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == fc_idx)
            fc3_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == fc3_idx)
            t16_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == t16_idx)
            t25_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == t25_idx)
            t38_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == t38_idx)
            work_days = night_cnt + fc_cnt + fc3_cnt + t16_cnt + t25_cnt + t38_cnt

            total_h = sum(self.hours[solver.Value(self.shifts[(p, d)])] for d in range(self.days_in_month))

            if p in ONLY_T25_T16_INDICES:
                tag = "只T25/T16"
            elif p == UNABLE_NIGHT_INDEX:
                tag = "禁夜班"
            elif p == PARTTIME_FC_ONLY_INDEX:
                tag = "兼职-只FC"
            elif p == PARTTIME_FLEXIBLE_INDEX:
                tag = "兼职-全能"
            elif p == ONLY_FC3_INDEX:
                tag = "只FC3"
            else:
                tag = "全职"

            # 标记工时是否达标
            if p in FULLTIME_INDICES:
                status_tag = "✅" if total_h >= self.min_hours else "⚠️"
            else:
                status_tag = ""

            stats.append({
                "人员": name,
                "类型": tag,
                "总工时": total_h,
                f"是否≥{self.min_hours}h": status_tag,
                "上班天数": work_days,
                "休息天数": self.days_in_month - work_days,
                "N": night_cnt,
                "FC": fc_cnt,
                "FC3": fc3_cnt,
                "T16": t16_cnt,
                "T25": t25_cnt,
                "T38": t38_cnt
            })
        df_stats = pd.DataFrame(stats)

        last_three_days = {}
        for p, name in enumerate(self.person_names):
            last_three = []
            for offset in range(3):
                d = self.days_in_month - 3 + offset
                if d >= 0:
                    shift_idx = solver.Value(self.shifts[(p, d)])
                    last_three.append(self.all_shifts[shift_idx])
            last_three_days[name] = last_three

        return df_schedule, df_stats, last_three_days


# ==================== 主程序 ====================
st.title("📅 自动排班系统")

# 显示当前人员数量
st.write(f"**当前人员数量: {len(st.session_state.person_names)} 人**")

# 检查重复
duplicates = [x for x in st.session_state.person_names if st.session_state.person_names.count(x) > 1]
if duplicates:
    st.error(f"⚠️ 发现重复人员: {set(duplicates)}")
    st.button("🔄 点击修复", on_click=lambda: st.session_state.__setitem__('person_names', PERSON_NAMES))

with st.expander("👥 人员配置", expanded=True):
    col1, col2 = st.columns(2)

    with col1:
        st.write("**全职人员 (25人)**")
        for i, name in enumerate(st.session_state.person_names[:25]):
            if i in ONLY_T25_T16_INDICES:
                st.write(f"  {i+1}. {name} 🔒 (只T25/T16)")
            elif i == UNABLE_NIGHT_INDEX:
                st.write(f"  {i+1}. {name} 🚫 (禁夜班)")
            elif i == ONLY_FC3_INDEX:
                st.write(f"  {i+1}. {name} 🔒 (只FC3)")
            else:
                st.write(f"  {i+1}. {name}")

    with col2:
        st.write("**兼职 (2人)**")
        st.write(f"  26. {st.session_state.person_names[24]} (Jane Wang, 只FC/周末休)")
        st.write(f"  27. {st.session_state.person_names[25]} (Edward Liu, 全能)")
        st.write(f"  28. {st.session_state.person_names[26]} (Clara Fong, 只FC3)")

st.markdown("---")

# 月份选择
col1, col2 = st.columns(2)
with col1:
    year = st.number_input("📆 年份", min_value=2024, max_value=2030, value=datetime.now().year, step=1)
with col2:
    month = st.number_input("📆 月份", min_value=1, max_value=12, value=datetime.now().month, step=1)

st.markdown("---")

# 上传上月排班数据
st.subheader("📤 上传上月排班数据（可选）")

uploaded_file = st.file_uploader(
    "选择上个月的排班 Excel 或 JSON 文件",
    type=['xlsx', 'json']
)

previous_schedule = None
if uploaded_file:
    with st.spinner("正在读取文件..."):
        previous_schedule = load_previous_schedule(uploaded_file, st.session_state.person_names)
        if previous_schedule:
            st.success("✅ 已成功加载上个月最后3天的排班数据")
        else:
            st.warning("⚠️ 无法读取文件，本月将从零开始排班")

st.markdown("---")

# 参数设置
st.sidebar.header("⚙️ 排班参数")

min_hours = st.sidebar.number_input("📈 全职最低工时（小时/月）", min_value=160, max_value=175, value=167, step=1)
target_hours = st.sidebar.number_input("🎯 目标工时（小时/月）", min_value=160, max_value=180, value=170, step=1)
max_hours = st.sidebar.number_input("⚠️ 最大工时（小时/月）", min_value=170, max_value=200, value=180, step=1)
night_rest_days = st.sidebar.slider("🌙 N后强制休息天数", min_value=1, max_value=5, value=3, step=1)

st.sidebar.markdown("### 📋 班次说明")
st.sidebar.markdown("""
| 班次 | 工时 |
|------|------|
| N (夜班) | 14h |
| FC | 8h |
| FC3 | 11h |
| T16 | 11h |
| T25 | 11h |
| T38 | 11h |
""")

st.sidebar.markdown("### 📊 每日需求")
st.sidebar.markdown("""
**周一-周四:** N×3, FC×1, FC3×1, T38×2, T16×2, T25×4
**周五-周日:** N×2, FC×1, FC3×1, T38×2, T16×2, T25×4
""")

st.sidebar.markdown("### 🔒 特殊人员")
st.sidebar.markdown("""
- **Catherine Yeung, Frankie Wong, Cecilia Szeto**: 只T25/T16
- **Joyce Luk**: 禁夜班
- **Jane Wang**: 只FC，周末休
- **Edward Liu**: 全能兼职
- **Clara Fong**: 只FC3
""")

st.sidebar.markdown("### ⭐ 排班策略")
st.sidebar.markdown("""
- **全职优先**: 全职人员先排班，工时≥167h
- **兼职补充**: 兼职人员只在必要时填补空缺
""")

# 开始排班
if st.button("🚀 开始排班", type="primary", use_container_width=True):
    with st.spinner("正在求解，请稍候（约2-5分钟）..."):
        scheduler = ShiftScheduler(
            year=year,
            month=month,
            person_names=st.session_state.person_names,
            target_hours=target_hours,
            max_hours=max_hours,
            min_hours=min_hours,
            night_rest_days=night_rest_days,
            previous_schedule=previous_schedule,
            prioritize_fulltime=True
        )

        result = scheduler.run()

        if result:
            df_schedule, df_stats, last_three_days = result

            st.success("✅ 排班成功！")

            # 统计摘要
            col1, col2, col3, col4, col5 = st.columns(5)
            fulltime_stats = df_stats[df_stats["类型"] == "全职"]
            parttime_stats = df_stats[df_stats["类型"].str.contains("兼职")]

            with col1:
                st.metric("总人数", len(st.session_state.person_names))
            with col2:
                st.metric("全职平均工时", f"{fulltime_stats['总工时'].mean():.1f}h")
            with col3:
                st.metric("全职工时达标率", f"{len(fulltime_stats[fulltime_stats[f'是否≥{min_hours}h'] == '✅'])}/{len(fulltime_stats)}")
            with col4:
                st.metric("兼职平均工时", f"{parttime_stats['总工时'].mean():.1f}h" if len(parttime_stats) > 0 else "0h")
            with col5:
                st.metric("平均夜班", f"{fulltime_stats['N'].mean():.1f}天")

            st.subheader("📊 排班表预览（前20天）")
            st.dataframe(df_schedule.head(20), use_container_width=True)

            st.subheader("📈 人员统计")
            st.dataframe(df_stats, use_container_width=True)

            # 下载 Excel
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_schedule.to_excel(writer, sheet_name=f"{year}年{month}月排班表", index=False)
                df_stats.to_excel(writer, sheet_name="工时统计", index=False)

            st.download_button(
                label="📥 下载 Excel 排班表",
                data=output.getvalue(),
                file_name=f"排班表_{year}_{month:02d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

            # 下载 JSON
            json_output = json.dumps({
                "year": year,
                "month": month,
                "person_names": st.session_state.person_names,
                "last_three_days": last_three_days
            }, ensure_ascii=False, indent=2)

            st.download_button(
                label="📥 下载 JSON（供下月使用）",
                data=json_output,
                file_name=f"schedule_{year}_{month:02d}.json",
                mime="application/json",
                use_container_width=True
            )

        else:
            st.error("❌ 未找到可行解")
            st.info("💡 建议：\n1. 降低全职最低工时要求（从167降到165或160）\n2. 检查上月最后3天是否与本月冲突\n3. 减少夜班后休息天数\n4. 放宽工时上限")

st.markdown("---")
st.markdown("💡 **提示**: 系统会优先给全职人员排班，确保全职人员工时≥167小时")
