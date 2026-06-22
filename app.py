# 强制重置（仅在需要时使用）
st.session_state.clear()

# app.py - 自动排班系统
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

# ==================== 人员名单（固定） ====================
# 注意：请确保此列表与下方索引配置完全对应
FIXED_PERSON_NAMES = [
    # 全能人员（20人）索引 0-19
    "Flora Feng", "Ivy Chen", "Yolanda Yu", "Vivian You", "Eddie Yang",
    "Yulia Tang", "Lusi Cai", "Peter Li", "Donnie Wu", "Sam Jiang",
    "England Chen", "Zac Yang", "Riky Ye", "Celine Li", "Hope He",
    "Sama Liu", "Yuki Jiang", "Jessica Dong", "Erin Li", "Riley Ren",
    # 只T25/T16（3人）索引 20-22
    "Catherine Yeung", "Frankie Wong", "Cecilia Szeto",
    # 不能上夜班（1人）索引 23
    "Joyce Luk",
    # 兼职 - 只上FC（1人）索引 24
    "Jane Wang",
    # 兼职 - 全能（1人）索引 25
    "Edward Liu",
    # 只上FC3（1人）索引 26
    "Clara Fong"
]

# 特殊人员索引配置
ONLY_T25_T16_INDICES = [20, 21, 22]  # Catherine Yeung, Frankie Wong, Cecilia Szeto
UNABLE_NIGHT_INDEX = 23              # Joyce Luk
PARTTIME_FC_ONLY_INDEX = 24          # Jane Wang（兼职，只上FC）
PARTTIME_FLEXIBLE_INDEX = 25         # Edward Liu（兼职，全能）
ONLY_FC3_INDEX = 26                  # Clara Fong（只上FC3）


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
    def __init__(self, year, month, person_names, target_hours=166, max_hours=180,
                 night_rest_days=3, previous_schedule=None):

        self.year = year
        self.month = month
        self.person_names = person_names
        self.total_people = len(person_names)
        self.target_hours = target_hours
        self.max_hours = max_hours
        self.night_rest_days = night_rest_days
        self.previous_schedule = previous_schedule

        # 正式工：索引0-24（25人，包含Joyce Luk和只T25/T16的人）
        # 兼职：索引25-26（2人）
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

        # 计算天数
        self.days_in_month = self._get_days_in_month()
        self.dates = [datetime(year, month, d + 1) for d in range(self.days_in_month)]

        # 每天需求配置（周一=0, 周日=6）
        self.day_config = {
            0: {"night": 3, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            1: {"night": 3, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            2: {"night": 3, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            3: {"night": 3, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            4: {"night": 2, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            5: {"night": 2, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4},
            6: {"night": 2, "fc": 1, "fc3": 1, "t38": 2, "t16": 2, "t25": 4}
        }

        # FC3后允许的班次：FC3、N、休息
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
        """添加某天某班次人数等于target"""
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

        # 索引
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

        # 2. Catherine Yeung, Frankie Wong, Cecilia Szeto 只能上T25/T16/休息
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
            if d % 7 >= 4:  # 周五~周日休息
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

        # ========== N后强制休息3天 ==========
        for p in range(self.total_people):
            for d in range(self.days_in_month - self.night_rest_days):
                night_shift = model.NewBoolVar(f'night_{p}_{d}')
                model.Add(self.shifts[(p, d)] == night_idx).OnlyEnforceIf(night_shift)
                model.Add(self.shifts[(p, d)] != night_idx).OnlyEnforceIf(night_shift.Not())
                for rd in range(d + 1, d + self.night_rest_days + 1):
                    model.Add(self.shifts[(p, rd)] == off_idx).OnlyEnforceIf(night_shift)

        # ========== 上3天班后休息3天 ==========
        for p in range(self.total_people):
            # 任何连续4天内至少有1天休息
            for d in range(self.days_in_month - 3):
                work_vars = []
                for i in range(4):
                    is_work = model.NewBoolVar(f'work_{p}_{d}_{i}')
                    model.Add(self.shifts[(p, d + i)] != off_idx).OnlyEnforceIf(is_work)
                    model.Add(self.shifts[(p, d + i)] == off_idx).OnlyEnforceIf(is_work.Not())
                    work_vars.append(is_work)
                model.Add(sum(work_vars) <= 3)

            # 连续工作3天后，第4、5、6天必须休息（上3休3）
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

        # ========== FC3后只能接FC3/N/休息 ==========
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

        # ========== 工时上限（仅正式工） ==========
        for p in range(self.num_fulltime):
            model.Add(total_hours[p] <= self.max_hours)

        # ========== 优化目标 ==========
        hours_penalty = model.NewIntVar(0, 1000000, "hours_penalty")
        hours_diff = []
        for p in range(self.num_fulltime):
            diff = model.NewIntVar(-self.max_hours * 2, self.max_hours * 2, f"diff_{p}")
            model.Add(diff == total_hours[p] - self.target_hours)
            abs_diff = model.NewIntVar(0, self.max_hours * 2, f"abs_{p}")
            model.AddAbsEquality(abs_diff, diff)
            hours_diff.append(abs_diff)
        model.Add(hours_penalty == sum(hours_diff))

        # 夜班均衡
        night_counts = []
        for p in range(self.total_people):
            if p == PARTTIME_FC_ONLY_INDEX:
                continue
            if p == ONLY_FC3_INDEX:
                continue
            if p in ONLY_T25_T16_INDICES:
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

        if night_counts:
            max_night = model.NewIntVar(0, self.days_in_month, "max_n")
            min_night = model.NewIntVar(0, self.days_in_month, "min_n")
            model.AddMaxEquality(max_night, night_counts)
            model.AddMinEquality(min_night, night_counts)
            night_penalty = model.NewIntVar(0, self.days_in_month, "np")
            model.Add(night_penalty == max_night - min_night)
            model.Minimize(10 * hours_penalty + night_penalty)
        else:
            model.Minimize(10 * hours_penalty)

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

        # 统计
        night_idx = self.shift_to_index[self.shift_night]
        fc_idx = self.shift_to_index[self.shift_fc]
        fc3_idx = self.shift_to_index[self.shift_fc3]
        t16_idx = self.shift_to_index[self.shift_t16]
        t25_idx = self.shift_to_index[self.shift_t25]
        t38_idx = self.shift_to_index[self.shift_t38]
        off_idx = self.shift_to_index[self.shift_off]

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
                tag = "全能"

            stats.append({
                "人员": name,
                "类型": tag,
                "总工时": total_h,
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

        # 记录最后3天
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

# 使用固定人员名单，确保与索引配置一致
if 'person_names' not in st.session_state or st.session_state.person_names != FIXED_PERSON_NAMES:
    st.session_state.person_names = FIXED_PERSON_NAMES.copy()

# 显示人员配置
with st.expander("👥 人员配置", expanded=True):
    st.write(f"**总人数: {len(st.session_state.person_names)} 人**")

    col1, col2 = st.columns(2)

    with col1:
        st.write("**正式工 (25人)**")
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
st.markdown("上传上个月生成的排班表，系统会自动读取最后3天的班次进行跨月衔接。")

uploaded_file = st.file_uploader(
    "选择上个月的排班 Excel 或 JSON 文件",
    type=['xlsx', 'json'],
    help="上传之前生成的排班表，确保跨月连续性"
)

previous_schedule = None
if uploaded_file:
    with st.spinner("正在读取文件..."):
        previous_schedule = load_previous_schedule(uploaded_file, st.session_state.person_names)
        if previous_schedule:
            st.success("✅ 已成功加载上个月最后3天的排班数据")
            with st.expander("查看加载的数据（前5人）"):
                preview = []
                for i, (name, days) in enumerate(list(previous_schedule.items())[:5]):
                    preview.append({
                        "人员": name,
                        "倒数第3天": days[0] if len(days) > 0 else "-",
                        "倒数第2天": days[1] if len(days) > 1 else "-",
                        "最后1天": days[2] if len(days) > 2 else "-"
                    })
                st.dataframe(pd.DataFrame(preview))
        else:
            st.warning("⚠️ 无法读取文件，本月将从零开始排班")

st.markdown("---")

# 参数设置
st.sidebar.header("⚙️ 排班参数")

target_hours = st.sidebar.number_input("🎯 目标工时（小时/月）", min_value=140, max_value=200, value=166, step=1)
max_hours = st.sidebar.number_input("⚠️ 最大工时（小时/月）", min_value=160, max_value=220, value=180, step=1)
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

# 开始排班
if st.button("🚀 开始排班", type="primary", use_container_width=True):
    with st.spinner("正在求解，请稍候（约2-5分钟）..."):
        scheduler = ShiftScheduler(
            year=year,
            month=month,
            person_names=st.session_state.person_names,
            target_hours=target_hours,
            max_hours=max_hours,
            night_rest_days=night_rest_days,
            previous_schedule=previous_schedule
        )

        result = scheduler.run()

        if result:
            df_schedule, df_stats, last_three_days = result

            st.success("✅ 排班成功！")

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("总人数", len(st.session_state.person_names))
            with col2:
                st.metric("平均工时", f"{df_stats['总工时'].mean():.1f}h")
            with col3:
                st.metric("工时范围", f"{df_stats['总工时'].min()} - {df_stats['总工时'].max()}h")
            with col4:
                st.metric("平均夜班", f"{df_stats['N'].mean():.1f}天")

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

            # 约束验证
            st.subheader("✅ 约束验证")
            validations = []

            # 验证N后休息
            night_violations = []
            for p in range(scheduler.total_people):
                for d in range(scheduler.days_in_month - night_rest_days):
                    if solver.Value(scheduler.shifts[(p, d)]) == night_idx:
                        for rd in range(d + 1, d + night_rest_days + 1):
                            if solver.Value(scheduler.shifts[(p, rd)]) != off_idx:
                                night_violations.append(f"{scheduler.person_names[p]} 第{d+1}天N后第{rd+1}天未休息")
            if night_violations:
                validations.append(f"❌ N后休息: {len(night_violations)}处违规")
            else:
                validations.append("✅ N后休息3天: 通过")

            # 验证上3休3
            work_violations = []
            for p in range(scheduler.total_people):
                for d in range(scheduler.days_in_month - 5):
                    work_streak = 0
                    for i in range(3):
                        if solver.Value(scheduler.shifts[(p, d + i)]) != off_idx:
                            work_streak += 1
                    if work_streak == 3:
                        for rd in range(3, 6):
                            if d + rd < scheduler.days_in_month:
                                if solver.Value(scheduler.shifts[(p, d + rd)]) != off_idx:
                                    work_violations.append(f"{scheduler.person_names[p]} 第{d+1}-{d+3}天上班后未休3天")
                                    break
            if work_violations:
                validations.append(f"❌ 上3休3: {len(work_violations)}处违规")
            else:
                validations.append("✅ 上3休3: 通过")

            # 验证FC3后约束
            fc3_violations = []
            for p in range(scheduler.total_people):
                for d in range(scheduler.days_in_month - 1):
                    if solver.Value(scheduler.shifts[(p, d)]) == fc3_idx:
                        next_shift = solver.Value(scheduler.shifts[(p, d + 1)])
                        if next_shift not in scheduler.fc3_allowed_next:
                            fc3_violations.append(f"{scheduler.person_names[p]} 第{d+1}天FC3后第{d+2}天上了{scheduler.all_shifts[next_shift]}")
            if fc3_violations:
                validations.append(f"❌ FC3后约束: {len(fc3_violations)}处违规")
            else:
                validations.append("✅ FC3后只能接FC3/N/休息: 通过")

            for v in validations:
                if v.startswith("✅"):
                    st.success(v)
                else:
                    st.error(v)

        else:
            st.error("❌ 未找到可行解")
            st.info("💡 建议：\n1. 检查上月最后3天是否与本月冲突\n2. 尝试减少夜班后休息天数\n3. 放宽工时上限")

st.markdown("---")
st.markdown("💡 **提示**: 系统会自动保存每月最后3天的排班数据供下月使用")
