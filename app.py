# app.py - 自动排班系统 Web 应用（支持自定义人员名称）
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

# ==================== 辅助函数 ====================
def load_previous_schedule_from_file(uploaded_file, person_names):
    """从上传的文件加载上个月排班数据"""
    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith('.xlsx'):
                df = pd.read_excel(uploaded_file)
                last_three_days = {}
                # 使用真实人员名称匹配
                for name in person_names:
                    if name in df.columns:
                        person_data = df[name].tail(3).tolist()
                        last_three_days[name] = person_data
                return last_three_days
            elif uploaded_file.name.endswith('.json'):
                data = json.load(uploaded_file)
                return data.get('last_three_days', {})
        except Exception as e:
            st.error(f"读取文件失败: {e}")
    return None

def save_default_names():
    """保存默认人员名称到 session_state"""
    if 'person_names' not in st.session_state:
        # 默认人员名称（可以改成你的真实人员）
        default_names = []
        for i in range(25):
            default_names.append(f"员工{i+1}")
        for i in range(2):
            default_names.append(f"兼职{i+1}")
        st.session_state.person_names = default_names

# ==================== 排班核心类 ====================
class ShiftScheduler:
    def __init__(self, year, month, person_names, target_hours=166, max_hours=180, 
                 night_rest_days=3, previous_schedule=None):
        
        self.year = year
        self.month = month
        self.person_names = person_names
        self.num_fulltime = 25
        self.num_parttime = 2
        self.total_people = len(person_names)
        self.target_hours = target_hours
        self.max_hours = max_hours
        self.night_rest_days = night_rest_days
        self.previous_schedule = previous_schedule
        
        # 特殊人员索引（使用人员名称）
        self.unable_night_person = person_names[24]  # 第25人
        self.only_t25_t16_persons = [person_names[21], person_names[22], person_names[23]]
        self.only_fc3_person = person_names[20]
        self.parttime_fc_only = person_names[25]
        self.parttime_flexible = person_names[26]
        
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
        self.dates = [datetime(year, month, d+1) for d in range(self.days_in_month)]
        
        # 每天需求配置
        self.day_config = {
            0: {"night": 3, "day_total": 10, "has_fc": True,
                "ratio": {"FC": 1, "FC3": 1, "T16": 2, "T25": 4, "T38": 2}},
            1: {"night": 3, "day_total": 10, "has_fc": True,
                "ratio": {"FC": 1, "FC3": 1, "T16": 2, "T25": 4, "T38": 2}},
            2: {"night": 3, "day_total": 10, "has_fc": True,
                "ratio": {"FC": 1, "FC3": 1, "T16": 2, "T25": 4, "T38": 2}},
            3: {"night": 3, "day_total": 10, "has_fc": True,
                "ratio": {"FC": 1, "FC3": 1, "T16": 2, "T25": 4, "T38": 2}},
            4: {"night": 2, "day_total": 7, "has_fc": False,
                "ratio": {"T16": 2, "T25": 3, "T38": 2}},
            5: {"night": 2, "day_total": 7, "has_fc": False,
                "ratio": {"T16": 2, "T25": 3, "T38": 2}},
            6: {"night": 2, "day_total": 7, "has_fc": False,
                "ratio": {"T16": 2, "T25": 3, "T38": 2}}
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
    
    def _add_eq_constraint(self, model, day, shift_idx, target):
        vars_list = []
        for p in range(self.total_people):
            b = model.NewBoolVar(f'c_{day}_{p}_{shift_idx}')
            model.Add(self.shifts[(p, day)] == shift_idx).OnlyEnforceIf(b)
            model.Add(self.shifts[(p, day)] != shift_idx).OnlyEnforceIf(b.Not())
            vars_list.append(b)
        model.Add(sum(vars_list) == target)
    
    def run(self):
        model = cp_model.CpModel()
        
        self.shifts = {}
        for p in range(self.total_people):
            for d in range(self.days_in_month):
                self.shifts[(p, d)] = model.NewIntVar(0, len(self.all_shifts)-1, f"s_{p}_{d}")
        
        total_hours = {}
        for p in range(self.total_people):
            total_hours[p] = model.NewIntVar(0, self.max_hours * self.days_in_month, f"th_{p}")
        
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
        
        # 获取特殊人员的索引
        unable_idx = self.person_names.index(self.unable_night_person)
        only_fc3_idx = self.person_names.index(self.only_fc3_person)
        parttime_fc_idx = self.person_names.index(self.parttime_fc_only)
        parttime_flex_idx = self.person_names.index(self.parttime_flexible)
        only_t25_t16_indices = [self.person_names.index(name) for name in self.only_t25_t16_persons]
        
        # 跨月连续约束
        if self.previous_schedule:
            for p, name in enumerate(self.person_names):
                if name in self.previous_schedule:
                    prev_schedule = self.previous_schedule[name]
                    for offset in range(min(3, self.days_in_month)):
                        if offset < len(prev_schedule):
                            prev_shift = prev_schedule[offset]
                            if prev_shift in self.shift_to_index:
                                model.Add(self.shifts[(p, offset)] == self.shift_to_index[prev_shift])
        
        # 人员限制
        for d in range(self.days_in_month):
            model.Add(self.shifts[(unable_idx, d)] != night_idx)
        
        for p in only_t25_t16_indices:
            for d in range(self.days_in_month):
                model.Add(self.shifts[(p, d)] != night_idx)
                model.Add(self.shifts[(p, d)] != fc_idx)
                model.Add(self.shifts[(p, d)] != fc3_idx)
                model.Add(self.shifts[(p, d)] != t38_idx)
        
        for d in range(self.days_in_month):
            model.Add(self.shifts[(only_fc3_idx, d)] != night_idx)
            model.Add(self.shifts[(only_fc3_idx, d)] != fc_idx)
            model.Add(self.shifts[(only_fc3_idx, d)] != t16_idx)
            model.Add(self.shifts[(only_fc3_idx, d)] != t25_idx)
            model.Add(self.shifts[(only_fc3_idx, d)] != t38_idx)
        
        for d in range(self.days_in_month):
            model.Add(self.shifts[(parttime_fc_idx, d)] != night_idx)
            model.Add(self.shifts[(parttime_fc_idx, d)] != fc3_idx)
            model.Add(self.shifts[(parttime_fc_idx, d)] != t16_idx)
            model.Add(self.shifts[(parttime_fc_idx, d)] != t25_idx)
            model.Add(self.shifts[(parttime_fc_idx, d)] != t38_idx)
            if d % 7 >= 4:
                model.Add(self.shifts[(parttime_fc_idx, d)] == off_idx)
        
        # 每天班次需求
        for d in range(self.days_in_month):
            weekday = d % 7
            cfg = self.day_config[weekday]
            self._add_eq_constraint(model, d, night_idx, cfg["night"])
            
            if cfg["has_fc"]:
                self._add_eq_constraint(model, d, fc_idx, cfg["ratio"]["FC"])
                self._add_eq_constraint(model, d, fc3_idx, cfg["ratio"]["FC3"])
                self._add_eq_constraint(model, d, t16_idx, cfg["ratio"]["T16"])
                self._add_eq_constraint(model, d, t25_idx, cfg["ratio"]["T25"])
                self._add_eq_constraint(model, d, t38_idx, cfg["ratio"]["T38"])
            else:
                self._add_eq_constraint(model, d, t16_idx, cfg["ratio"]["T16"])
                self._add_eq_constraint(model, d, t25_idx, cfg["ratio"]["T25"])
                self._add_eq_constraint(model, d, t38_idx, cfg["ratio"]["T38"])
                for p in range(self.total_people):
                    model.Add(self.shifts[(p, d)] != fc_idx)
                    model.Add(self.shifts[(p, d)] != fc3_idx)
        
        # 夜班后强制休息
        for p in range(self.num_fulltime):
            for d in range(self.days_in_month - self.night_rest_days):
                night_shift = model.NewBoolVar(f'night_{p}_{d}')
                model.Add(self.shifts[(p, d)] == night_idx).OnlyEnforceIf(night_shift)
                model.Add(self.shifts[(p, d)] != night_idx).OnlyEnforceIf(night_shift.Not())
                for rd in range(d+1, d+self.night_rest_days+1):
                    model.Add(self.shifts[(p, rd)] == off_idx).OnlyEnforceIf(night_shift)
        
        # 连续工作3天后至少休息2天
        for p in range(self.total_people):
            for d in range(self.days_in_month - 3):
                work_vars = []
                for i in range(4):
                    is_work = model.NewBoolVar(f'work_{p}_{d}_{i}')
                    model.Add(self.shifts[(p, d+i)] != off_idx).OnlyEnforceIf(is_work)
                    model.Add(self.shifts[(p, d+i)] == off_idx).OnlyEnforceIf(is_work.Not())
                    work_vars.append(is_work)
                model.Add(sum(work_vars) <= 3)
            
            for d in range(self.days_in_month - 4):
                work3_vars = []
                for i in range(3):
                    is_work = model.NewBoolVar(f'work3_{p}_{d}_{i}')
                    model.Add(self.shifts[(p, d+i)] != off_idx).OnlyEnforceIf(is_work)
                    model.Add(self.shifts[(p, d+i)] == off_idx).OnlyEnforceIf(is_work.Not())
                    work3_vars.append(is_work)
                
                all_work3 = model.NewBoolVar(f'all_work3_{p}_{d}')
                model.Add(sum(work3_vars) == 3).OnlyEnforceIf(all_work3)
                model.Add(sum(work3_vars) != 3).OnlyEnforceIf(all_work3.Not())
                
                model.Add(self.shifts[(p, d+3)] == off_idx).OnlyEnforceIf(all_work3)
                if d+4 < self.days_in_month:
                    model.Add(self.shifts[(p, d+4)] == off_idx).OnlyEnforceIf(all_work3)
        
        # FC3后只能接允许的班次
        for p in range(self.total_people):
            for d in range(self.days_in_month - 1):
                is_fc3 = model.NewBoolVar(f'fc3_{p}_{d}')
                model.Add(self.shifts[(p, d)] == fc3_idx).OnlyEnforceIf(is_fc3)
                model.Add(self.shifts[(p, d)] != fc3_idx).OnlyEnforceIf(is_fc3.Not())
                
                allowed_conditions = []
                for allowed_shift in self.fc3_allowed_next:
                    is_allowed = model.NewBoolVar(f'fc3_allowed_{p}_{d}_{allowed_shift}')
                    model.Add(self.shifts[(p, d+1)] == allowed_shift).OnlyEnforceIf(is_allowed)
                    model.Add(self.shifts[(p, d+1)] != allowed_shift).OnlyEnforceIf(is_allowed.Not())
                    allowed_conditions.append(is_allowed)
                model.AddBoolOr(allowed_conditions).OnlyEnforceIf(is_fc3)
        
        for p in range(self.num_fulltime):
            model.Add(total_hours[p] <= self.max_hours)
        
        # 优化目标
        hours_penalty = model.NewIntVar(0, 1000000, "hours_penalty")
        hours_diff = []
        for p in range(self.num_fulltime):
            diff = model.NewIntVar(-self.max_hours*2, self.max_hours*2, f"diff_{p}")
            model.Add(diff == total_hours[p] - self.target_hours)
            abs_diff = model.NewIntVar(0, self.max_hours*2, f"abs_{p}")
            model.AddAbsEquality(abs_diff, diff)
            hours_diff.append(abs_diff)
        model.Add(hours_penalty == sum(hours_diff))
        
        # 夜班均衡
        night_counts = []
        for p in range(self.num_fulltime):
            nc = model.NewIntVar(0, self.days_in_month, f"nc_{p}")
            terms = []
            for d in range(self.days_in_month):
                b = model.NewBoolVar(f'nb_{p}_{d}')
                model.Add(self.shifts[(p, d)] == night_idx).OnlyEnforceIf(b)
                model.Add(self.shifts[(p, d)] != night_idx).OnlyEnforceIf(b.Not())
                terms.append(b)
            model.Add(nc == sum(terms))
            night_counts.append(nc)
        
        nc_flex = model.NewIntVar(0, self.days_in_month, "nc_flex")
        flex_terms = []
        for d in range(self.days_in_month):
            b = model.NewBoolVar(f'nf_{d}')
            model.Add(self.shifts[(parttime_flex_idx, d)] == night_idx).OnlyEnforceIf(b)
            model.Add(self.shifts[(parttime_flex_idx, d)] != night_idx).OnlyEnforceIf(b.Not())
            flex_terms.append(b)
        model.Add(nc_flex == sum(flex_terms))
        night_counts.append(nc_flex)
        
        max_night = model.NewIntVar(0, self.days_in_month, "max_n")
        min_night = model.NewIntVar(0, self.days_in_month, "min_n")
        model.AddMaxEquality(max_night, night_counts)
        model.AddMinEquality(min_night, night_counts)
        night_penalty = model.NewIntVar(0, self.days_in_month, "np")
        model.Add(night_penalty == max_night - min_night)
        
        model.Minimize(10 * hours_penalty + night_penalty)
        
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
        off_idx = self.shift_to_index[self.shift_off]
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
            
            stats.append({
                "人员": name,
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
st.markdown("---")

# 初始化人员名称
if 'person_names' not in st.session_state:
    # 在这里修改成你的人员名单！
    st.session_state.person_names = [
        "张三", "李四", "王五", "赵六", "钱七",  # 人员1-5
        "孙八", "周九", "吴十", "郑十一", "王十二",  # 人员6-10
        "冯十三", "陈十四", "褚十五", "卫十六", "蒋十七",  # 人员11-15
        "沈十八", "韩十九", "杨二十", "朱二十一", "秦二十二",  # 人员16-20
        "尤二十三", "许二十四", "何二十五",  # 人员21-23（只T25/T16）
        "吕禁夜",  # 人员24（不能上夜班）
        "施兼职A",  # 人员25（兼职，只上FC）
        "张兼职B"   # 人员26（兼职，全能）
    ]

# 显示人员编辑界面
st.subheader("👥 人员名单设置")
st.markdown("修改下方的人员名称，排班表将自动使用新名称。")

# 创建可编辑的人员表格
edited_names = st.data_editor(
    pd.DataFrame({"序号": range(1, len(st.session_state.person_names) + 1), 
                  "人员名称": st.session_state.person_names}),
    use_container_width=True,
    hide_index=True,
    column_config={
        "序号": st.column_config.NumberColumn("序号", disabled=True),
        "人员名称": st.column_config.TextColumn("人员名称", required=True)
    }
)

# 更新人员名称
if st.button("✅ 应用人员名称", use_container_width=True):
    new_names = edited_names["人员名称"].tolist()
    st.session_state.person_names = new_names
    st.success("人员名称已更新！")
    st.rerun()

st.markdown("---")

# 月份选择
col1, col2 = st.columns(2)
with col1:
    year = st.number_input("📆 年份", min_value=2024, max_value=2030, value=datetime.now().year, step=1)
with col2:
    month = st.number_input("📆 月份", min_value=1, max_value=12, value=datetime.now().month, step=1)

st.markdown("---")

# 上传上个月排班数据
st.subheader("📤 上传上个月排班数据（可选）")

uploaded_file = st.file_uploader(
    "选择上个月的排班 Excel 文件",
    type=['xlsx', 'json'],
    help="上传之前生成的排班表（.xlsx 文件）或 JSON 文件"
)

previous_schedule = None
if uploaded_file:
    previous_schedule = load_previous_schedule_from_file(uploaded_file, st.session_state.person_names)
    if previous_schedule:
        st.success(f"✅ 已成功加载上个月最后三天的排班数据")
    else:
        st.warning("⚠️ 无法读取文件，本月将从零开始排班")

st.markdown("---")

# 参数设置
st.sidebar.header("⚙️ 排班参数设置")

target_hours = st.sidebar.number_input("🎯 目标工时（小时/月）", min_value=140, max_value=200, value=166, step=1)
max_hours = st.sidebar.number_input("⚠️ 最大工时（小时/月）", min_value=160, max_value=220, value=180, step=1)
night_rest_days = st.sidebar.slider("🌙 夜班后强制休息天数", min_value=1, max_value=5, value=3, step=1)

st.sidebar.markdown("### 📋 班次说明")
st.sidebar.markdown("""
- **N**: 夜班 (14小时)
- **FC**: 白班_FC (8小时)
- **FC3**: 白班_FC3 (11小时)
- **T16**: 白班_T16 (11小时)
- **T25**: 白班_T25 (11小时)
- **T38**: 白班_T38 (11小时)
""")

# 开始排班按钮
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
            
            # 显示统计摘要
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("总人数", len(st.session_state.person_names))
            with col2:
                st.metric("平均工时", f"{df_stats['总工时'].mean():.1f}h")
            with col3:
                st.metric("工时范围", f"{df_stats['总工时'].min()} - {df_stats['总工时'].max()}h")
            with col4:
                st.metric("平均夜班", f"{df_stats['N'].mean():.1f}天")
            
            # 显示排班表预览
            st.subheader("📊 排班表预览（前20天）")
            st.dataframe(df_schedule.head(20), use_container_width=True)
            
            # 显示统计表
            st.subheader("📈 人员统计")
            st.dataframe(df_stats, use_container_width=True)
            
            # 下载按钮
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
            
            # JSON 数据
            json_output = json.dumps({
                "year": year,
                "month": month,
                "person_names": st.session_state.person_names,
                "last_three_days": last_three_days
            }, ensure_ascii=False, indent=2)
            
            st.download_button(
                label="📥 下载 JSON 数据（供下个月使用）",
                data=json_output,
                file_name=f"schedule_{year}_{month:02d}.json",
                mime="application/json",
                use_container_width=True
            )
        else:
            st.error("❌ 未找到可行解，请尝试调整参数（如减少夜班后休息天数）")

st.markdown("---")
st.markdown("💡 **提示**: 在表格中直接修改人员名称，然后点击「应用人员名称」即可")
