# app.py - 自动排班系统 Web 应用（支持跨月连续）
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from ortools.sat.python import cp_model
import io
from io import BytesIO
import os
import json

st.set_page_config(
    page_title="自动排班系统",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==================== 辅助函数 ====================
def get_last_month(year, month):
    """获取上一个月"""
    if month == 1:
        return year - 1, 12
    else:
        return year, month - 1

def load_previous_schedule(year, month):
    """加载上个月最后三天的排班数据"""
    prev_year, prev_month = get_last_month(year, month)
    filename = f"schedule_{prev_year}_{prev_month:02d}.json"
    
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_three_days = data.get('last_three_days', {})
                return last_three_days
        except:
            return None
    return None

def save_schedule_info(year, month, last_three_days):
    """保存当月最后三天的排班信息，供下个月使用"""
    filename = f"schedule_{year}_{month:02d}.json"
    
    existing_data = {}
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
        except:
            pass
    
    existing_data['last_three_days'] = last_three_days
    existing_data['year'] = year
    existing_data['month'] = month
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(existing_data, f, ensure_ascii=False, indent=2)

# ==================== 排班核心类 ====================
class ShiftScheduler:
    def __init__(self, year, month, num_fulltime=25, num_parttime=2,
                 target_hours=166, max_hours=180, night_rest_days=3,
                 max_consecutive_work=3, min_rest_after_work=2,
                 previous_schedule=None):
        
        self.year = year
        self.month = month
        self.num_fulltime = num_fulltime
        self.num_parttime = num_parttime
        self.total_people = num_fulltime + num_parttime
        self.target_hours = target_hours
        self.max_hours = max_hours
        self.night_rest_days = night_rest_days
        self.max_consecutive_work = max_consecutive_work
        self.min_rest_after_work = min_rest_after_work
        self.previous_schedule = previous_schedule
        
        self.unable_night_person = num_fulltime - 1
        self.only_t25_t16_persons = [num_fulltime - 4, num_fulltime - 3, num_fulltime - 2]
        self.only_fc3_person = num_fulltime - 5
        self.parttime_fc_only = num_fulltime
        self.parttime_flexible = num_fulltime + 1
        
        self.days_in_month = self._get_days_in_month()
        self.dates = [datetime(year, month, d+1) for d in range(self.days_in_month)]
        
        self.shift_night = "夜班"
        self.shift_fc = "白班_FC"
        self.shift_fc3 = "白班_FC3"
        self.shift_t16 = "白班_T16"
        self.shift_t25 = "白班_T25"
        self.shift_t28 = "白班_T28"
        self.shift_off = "休息"
        
        self.all_shifts = [self.shift_night, self.shift_fc, self.shift_fc3,
                          self.shift_t16, self.shift_t25, self.shift_t28, self.shift_off]
        self.shift_to_index = {s: i for i, s in enumerate(self.all_shifts)}
        self.hours = [14, 8, 11, 11, 11, 11, 0]
        
        self.day_config = {
            0: {"night": 3, "day_total": 10, "has_fc": True,
                "ratio": {"FC": 1, "FC3": 1, "T16": 2, "T25": 4, "T28": 2}},
            1: {"night": 3, "day_total": 10, "has_fc": True,
                "ratio": {"FC": 1, "FC3": 1, "T16": 2, "T25": 4, "T28": 2}},
            2: {"night": 3, "day_total": 10, "has_fc": True,
                "ratio": {"FC": 1, "FC3": 1, "T16": 2, "T25": 4, "T28": 2}},
            3: {"night": 3, "day_total": 10, "has_fc": True,
                "ratio": {"FC": 1, "FC3": 1, "T16": 2, "T25": 4, "T28": 2}},
            4: {"night": 2, "day_total": 7, "has_fc": False,
                "ratio": {"T16": 2, "T25": 3, "T28": 2}},
            5: {"night": 2, "day_total": 7, "has_fc": False,
                "ratio": {"T16": 2, "T25": 3, "T28": 2}},
            6: {"night": 2, "day_total": 7, "has_fc": False,
                "ratio": {"T16": 2, "T25": 3, "T28": 2}}
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
        t28_idx = self.shift_to_index[self.shift_t28]
        
        # 跨月连续约束
        if self.previous_schedule:
            for p in range(self.total_people):
                person_key = f"人员{p+1}"
                if person_key in self.previous_schedule:
                    prev_schedule = self.previous_schedule[person_key]
                    for offset in range(min(3, self.days_in_month)):
                        if offset < len(prev_schedule):
                            prev_shift = prev_schedule[offset]
                            if prev_shift in self.shift_to_index:
                                model.Add(self.shifts[(p, offset)] == self.shift_to_index[prev_shift])
        
        for d in range(self.days_in_month):
            model.Add(self.shifts[(self.unable_night_person, d)] != night_idx)
        
        for p in self.only_t25_t16_persons:
            for d in range(self.days_in_month):
                model.Add(self.shifts[(p, d)] != night_idx)
                model.Add(self.shifts[(p, d)] != fc_idx)
                model.Add(self.shifts[(p, d)] != fc3_idx)
                model.Add(self.shifts[(p, d)] != t28_idx)
        
        for d in range(self.days_in_month):
            model.Add(self.shifts[(self.only_fc3_person, d)] != night_idx)
            model.Add(self.shifts[(self.only_fc3_person, d)] != fc_idx)
            model.Add(self.shifts[(self.only_fc3_person, d)] != t16_idx)
            model.Add(self.shifts[(self.only_fc3_person, d)] != t25_idx)
            model.Add(self.shifts[(self.only_fc3_person, d)] != t28_idx)
        
        for d in range(self.days_in_month):
            model.Add(self.shifts[(self.parttime_fc_only, d)] != night_idx)
            model.Add(self.shifts[(self.parttime_fc_only, d)] != fc3_idx)
            model.Add(self.shifts[(self.parttime_fc_only, d)] != t16_idx)
            model.Add(self.shifts[(self.parttime_fc_only, d)] != t25_idx)
            model.Add(self.shifts[(self.parttime_fc_only, d)] != t28_idx)
            if d % 7 >= 4:
                model.Add(self.shifts[(self.parttime_fc_only, d)] == off_idx)
        
        for d in range(self.days_in_month):
            weekday = d % 7
            cfg = self.day_config[weekday]
            self._add_eq_constraint(model, d, night_idx, cfg["night"])
            
            if cfg["has_fc"]:
                self._add_eq_constraint(model, d, fc_idx, cfg["ratio"]["FC"])
                self._add_eq_constraint(model, d, fc3_idx, cfg["ratio"]["FC3"])
                self._add_eq_constraint(model, d, t16_idx, cfg["ratio"]["T16"])
                self._add_eq_constraint(model, d, t25_idx, cfg["ratio"]["T25"])
                self._add_eq_constraint(model, d, t28_idx, cfg["ratio"]["T28"])
            else:
                self._add_eq_constraint(model, d, t16_idx, cfg["ratio"]["T16"])
                self._add_eq_constraint(model, d, t25_idx, cfg["ratio"]["T25"])
                self._add_eq_constraint(model, d, t28_idx, cfg["ratio"]["T28"])
                for p in range(self.total_people):
                    model.Add(self.shifts[(p, d)] != fc_idx)
                    model.Add(self.shifts[(p, d)] != fc3_idx)
        
        for p in range(self.num_fulltime):
            for d in range(self.days_in_month - self.night_rest_days):
                night_shift = model.NewBoolVar(f'night_{p}_{d}')
                model.Add(self.shifts[(p, d)] == night_idx).OnlyEnforceIf(night_shift)
                model.Add(self.shifts[(p, d)] != night_idx).OnlyEnforceIf(night_shift.Not())
                for rd in range(d+1, d+self.night_rest_days+1):
                    model.Add(self.shifts[(p, rd)] == off_idx).OnlyEnforceIf(night_shift)
        
        for p in range(self.total_people):
            for d in range(self.days_in_month - 3):
                work_vars = []
                for i in range(4):
                    is_work = model.NewBoolVar(f'work_{p}_{d}_{i}')
                    model.Add(self.shifts[(p, d+i)] != off_idx).OnlyEnforceIf(is_work)
                    model.Add(self.shifts[(p, d+i)] == off_idx).OnlyEnforceIf(is_work.Not())
                    work_vars.append(is_work)
                model.Add(sum(work_vars) <= 3)
        
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
        
        hours_penalty = model.NewIntVar(0, 1000000, "hours_penalty")
        hours_diff = []
        for p in range(self.num_fulltime):
            diff = model.NewIntVar(-self.max_hours*2, self.max_hours*2, f"diff_{p}")
            model.Add(diff == total_hours[p] - self.target_hours)
            abs_diff = model.NewIntVar(0, self.max_hours*2, f"abs_{p}")
            model.AddAbsEquality(abs_diff, diff)
            hours_diff.append(abs_diff)
        model.Add(hours_penalty == sum(hours_diff))
        
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
        
        columns = ["日期", "星期"] + [f"人员{p+1}" for p in range(self.total_people)]
        df_schedule = pd.DataFrame(rows, columns=columns)
        
        night_idx = self.shift_to_index[self.shift_night]
        off_idx = self.shift_to_index[self.shift_off]
        fc_idx = self.shift_to_index[self.shift_fc]
        fc3_idx = self.shift_to_index[self.shift_fc3]
        t16_idx = self.shift_to_index[self.shift_t16]
        t25_idx = self.shift_to_index[self.shift_t25]
        t28_idx = self.shift_to_index[self.shift_t28]
        
        stats = []
        for p in range(self.total_people):
            night_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == night_idx)
            fc_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == fc_idx)
            fc3_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == fc3_idx)
            t16_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == t16_idx)
            t25_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == t25_idx)
            t28_cnt = sum(1 for d in range(self.days_in_month) if solver.Value(self.shifts[(p, d)]) == t28_idx)
            work_days = night_cnt + fc_cnt + fc3_cnt + t16_cnt + t25_cnt + t28_cnt
            
            total_h = sum(self.hours[solver.Value(self.shifts[(p, d)])] for d in range(self.days_in_month))
            
            stats.append({
                "人员": f"人员{p+1}",
                "总工时": total_h,
                "上班天数": work_days,
                "休息天数": self.days_in_month - work_days,
                "夜班": night_cnt,
                "FC": fc_cnt,
                "FC3": fc3_cnt,
                "T16": t16_cnt,
                "T25": t25_cnt,
                "T28": t28_cnt
            })
        df_stats = pd.DataFrame(stats)
        
        last_three_days = {}
        for p in range(self.total_people):
            last_three = []
            for offset in range(3):
                d = self.days_in_month - 3 + offset
                if d >= 0:
                    shift_idx = solver.Value(self.shifts[(p, d)])
                    last_three.append(self.all_shifts[shift_idx])
            last_three_days[f"人员{p+1}"] = last_three
        
        return df_schedule, df_stats, last_three_days


# ==================== 主程序 ====================
st.title("📅 自动排班系统")
st.markdown("---")

col1, col2 = st.columns(2)
with col1:
    year = st.number_input("📆 年份", min_value=2024, max_value=2030, value=datetime.now().year, step=1)
with col2:
    month = st.number_input("📆 月份", min_value=1, max_value=12, value=datetime.now().month, step=1)

prev_year, prev_month = (year - 1 if month == 1 else year, 12 if month == 1 else month - 1)
previous_schedule = load_previous_schedule(prev_year, prev_month)

if previous_schedule:
    st.info(f"📂 检测到 {prev_year}年{prev_month}月 的排班数据，将自动衔接上月最后三天的班次。")
else:
    st.info(f"ℹ️ 未检测到 {prev_year}年{prev_month}月 的排班数据，本月将从零开始排班。")

st.markdown("---")

st.sidebar.header("⚙️ 排班参数设置")
target_hours = st.sidebar.number_input("🎯 目标工时（小时/月）", min_value=140, max_value=200, value=166, step=1)
max_hours = st.sidebar.number_input("⚠️ 最大工时（小时/月）", min_value=160, max_value=220, value=180, step=1)
night_rest_days = st.sidebar.slider("🌙 夜班后强制休息天数", min_value=1, max_value=5, value=3, step=1)
num_fulltime = st.sidebar.number_input("正式工人数", min_value=20, max_value=30, value=25, step=1)
num_parttime = st.sidebar.number_input("兼职人数", min_value=0, max_value=5, value=2, step=1)

if st.button("🚀 开始排班", type="primary", use_container_width=True):
    with st.spinner("正在求解，请稍候（约2-5分钟）..."):
        scheduler = ShiftScheduler(
            year=year, month=month,
            num_fulltime=num_fulltime, num_parttime=num_parttime,
            target_hours=target_hours, max_hours=max_hours,
            night_rest_days=night_rest_days,
            previous_schedule=previous_schedule
        )
        
        result = scheduler.run()
        
        if result:
            df_schedule, df_stats, last_three_days = result
            save_schedule_info(year, month, last_three_days)
            
            st.success("✅ 排班成功！")
            
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("正式工人数", num_fulltime)
            with col2:
                st.metric("平均工时", f"{df_stats['总工时'].mean():.1f}h")
            with col3:
                st.metric("工时范围", f"{df_stats['总工时'].min()} - {df_stats['总工时'].max()}h")
            with col4:
                st.metric("平均夜班", f"{df_stats['夜班'].mean():.1f}天")
            
            st.subheader("📊 排班表预览")
            st.dataframe(df_schedule.head(20), use_container_width=True)
            
            st.subheader("📈 人员统计")
            st.dataframe(df_stats, use_container_width=True)
            
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
        else:
            st.error("❌ 未找到可行解，请尝试调整参数")

st.markdown("---")
st.markdown("💡 **提示**: 如果求解失败，可以尝试减少夜班后休息天数")
