import json
import re
import io
import subprocess
import sys
from itertools import product
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import streamlit as st
from playwright.sync_api import sync_playwright
import asyncio
import traceback


# Install playwright browsers on first run
@st.cache_resource
def install_playwright():
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)

install_playwright()

# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────
DAY_MAP = {
    "Su": "Sunday", "Mo": "Monday", "Tu": "Tuesday",
    "We": "Wednesday", "Th": "Thursday", "Fr": "Friday", "Sa": "Saturday"
}
ALL_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"]
COLORS = ["#4A90D9", "#E67E22", "#2ECC71", "#9B59B6", "#E74C3C", "#1ABC9C"]

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def is_time_string(s):
    return bool(re.match(r'^(Su|Mo|Tu|We|Th|Fr|Sa)', s))

def to_minutes(t):
    time, period = t[:-2], t[-2:]
    h, m = map(int, time.split(":"))
    if period == "PM" and h != 12:
        h += 12
    if period == "AM" and h == 12:
        h = 0
    return h * 60 + m

def parse_time(time_str):
    if not time_str or time_str.strip() == "TBA":
        return [], None, None

    if " & " in time_str:
        all_days = []
        start = None
        end = None
        for part in time_str.split(" & "):
            days, s, e = parse_time(part.strip())
            all_days.extend(days)
            if start is None or s < start:
                start = s
            if end is None or e > end:
                end = e
        return all_days, start, end

    parts = time_str.split(" ")
    if len(parts) < 4:
        return [], None, None

    days_str = parts[0]
    start_str = parts[1]
    end_str = parts[3]

    days = re.findall(r'Su|Mo|Tu|We|Th|Fr|Sa', days_str)
    days = [DAY_MAP[d] for d in days]

    return days, to_minutes(start_str), to_minutes(end_str)

# ─────────────────────────────────────────────
#  SCRAPE
# ─────────────────────────────────────────────
def scrape(user_id, password):
    # --- WINDOWS STREAMLIT FIX ---
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    # -----------------------------

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://sis.upm.edu.sa/psp/ps/?cmd=login&languageCd=ENG&")
        page.get_by_role("textbox", name="User ID").fill(user_id)
        page.get_by_role("textbox", name="Password").fill(password)
        page.get_by_role("button", name="To enable screen reader mode").click()

        page.goto("https://sis.upm.edu.sa/psc/ps/EMPLOYEE/HRMS/c/UPM_PAYMENT_PROFILE.UPM_MAJOR_PROMOTED.GBL?PAGE=UPM_ENRL_ANCEMNT")
        page.get_by_role("button", name="NavBar").click()
        page.locator("iframe[name=\"psNavBarIFrame\"]").content_frame.get_by_role("button", name="Menu").click()
        page.locator("iframe[name=\"psNavBarIFrame\"]").content_frame.get_by_role("menuitem", name="Self Service").click()
        page.locator("iframe[name=\"psNavBarIFrame\"]").content_frame.get_by_role("menuitem", name="Enrollment").click()
        page.locator("iframe[name=\"psNavBarIFrame\"]").content_frame.get_by_role("menuitem", name="Enrollment: Add Classes").click()
        page.locator("iframe[name=\"TargetContent\"]").content_frame.get_by_role("tab", name="Search").click()
        page.locator("iframe[name=\"TargetContent\"]").content_frame.get_by_role("button", name="Search").click()
        page.locator("iframe[name=\"TargetContent\"]").content_frame.get_by_role("button", name="Search").click()
        page.locator("iframe[name=\"ptModFrame_0\"]").content_frame.get_by_role("button", name="OK").click()

        frame = page.frame_locator('iframe[name="TargetContent"]')
        locator = frame.locator('[id^="win0divSSR_CLSRSLT_WRK_GROUPBOX2$"]')
        locator.first.wait_for()

        count = locator.count()
        all_sections = []

        for i in range(count):
            container = locator.nth(i)
            container_text = container.inner_text()
            lines = [line.strip() for line in container_text.splitlines() if line.strip()]

            course_name = lines[0]
            j = 1

            while j < len(lines):
                if "Class" in lines[j] and "Section" in lines[j]:
                    j += 1
                    if j + 4 < len(lines):
                        section_name = lines[j + 1]
                        time1 = lines[j + 3]

                        if j + 4 < len(lines) and is_time_string(lines[j + 4]):
                            time2 = lines[j + 4]
                            combined_time = f"{time1} & {time2}"
                            room = lines[j + 5] if j + 5 < len(lines) else "TBA"
                            skip = 9
                        else:
                            combined_time = time1
                            room = lines[j + 4]
                            skip = 7

                        all_sections.append({
                            "course": course_name,
                            "section": section_name,
                            "time": combined_time,
                            "room": room,
                        })
                        j += skip
                else:
                    j += 1

        context.close()
        browser.close()
        return all_sections

# ─────────────────────────────────────────────
#  FILTER, GROUP, CONFLICT, SCORE
# ─────────────────────────────────────────────
def filter_courses(all_sections, wanted_courses):
    filtered = []
    for wanted in wanted_courses:
        for section in all_sections:
            if wanted.replace(" ", "") in section["course"].upper().replace(" ", ""):
                filtered.append(section)
                break
    return filtered

def group_sections(sections):
    grouped = defaultdict(lambda: defaultdict(list))
    for section in sections:
        match = re.search(r'(\d+)', section["section"])
        if match:
            group_number = match.group(1)
            grouped[section["course"]][group_number].append(section)
    return grouped

def has_conflict(section1, section2):
    days1, start1, end1 = parse_time(section1["time"])
    days2, start2, end2 = parse_time(section2["time"])
    if not days1 or not days2:
        return False
    common_days = set(days1) & set(days2)
    if not common_days:
        return False
    return start1 < end2 and start2 < end1

def find_valid_combinations(grouped):
    course_groups = []
    for course, groups in grouped.items():
        course_groups.append(list(groups.values()))

    valid_combinations = []
    for combination in product(*course_groups):
        all_sections = [s for group in combination for s in group]
        conflict = False
        for i in range(len(all_sections)):
            for j in range(i + 1, len(all_sections)):
                if all_sections[i]["course"] == all_sections[j]["course"]:
                    continue
                if has_conflict(all_sections[i], all_sections[j]):
                    conflict = True
                    break
            if conflict:
                break
        if not conflict:
            valid_combinations.append(all_sections)

    return valid_combinations

def score_combination(sections):
    days_used = set()
    day_slots = defaultdict(list)
    for section in sections:
        days, start, end = parse_time(section["time"])
        if not days:
            continue
        for day in days:
            days_used.add(day)
            day_slots[day].append((start, end))

    free_days = len([d for d in ALL_DAYS if d not in days_used])
    total_break = 0
    for day, slots in day_slots.items():
        slots.sort()
        for i in range(1, len(slots)):
            gap = slots[i][0] - slots[i - 1][1]
            if gap > 0:
                total_break += gap

    return (free_days, -total_break)

# ─────────────────────────────────────────────
#  DRAW SCHEDULE — returns image buffer
# ─────────────────────────────────────────────
def draw_schedule(sections):
    fig, ax = plt.subplots(figsize=(12, 8))
    day_index = {day: i for i, day in enumerate(ALL_DAYS)}
    y_min, y_max = 7 * 60, 22 * 60

    courses = list(set(s["course"] for s in sections))
    color_map = {course: COLORS[i % len(COLORS)] for i, course in enumerate(courses)}

    for section in sections:
        time_str = section["time"]
        parsed_days, start, end = parse_time(time_str)
        if not parsed_days:
            continue

        color = color_map[section["course"]]
        time_parts = time_str.split(" & ") if " & " in time_str else [time_str]

        for part in time_parts:
            part_days, part_start, part_end = parse_time(part.strip())
            if not part_days:
                continue
            for day in part_days:
                if day not in day_index:
                    continue
                x = day_index[day]
                height = part_end - part_start
                rect = plt.Rectangle(
                    (x + 0.05, part_start), 0.9, height,
                    linewidth=1, edgecolor="white", facecolor=color, alpha=0.85
                )
                ax.add_patch(rect)
                label = f"{section['course'].split('-')[0].strip()}\n{section['section']}\n{section['room']}"
                ax.text(x + 0.5, part_start + height / 2, label,
                        ha="center", va="center", fontsize=7,
                        color="white", fontweight="bold")

    ax.set_xlim(0, len(ALL_DAYS))
    ax.set_xticks([i + 0.5 for i in range(len(ALL_DAYS))])
    ax.set_xticklabels(ALL_DAYS, fontsize=11, fontweight="bold")

    y_ticks = list(range(y_min, y_max + 1, 60))
    y_labels = []
    for m in y_ticks:
        h = m // 60
        period = "AM" if h < 12 else "PM"
        h_display = h if h <= 12 else h - 12
        if h_display == 0:
            h_display = 12
        y_labels.append(f"{h_display}:00 {period}")

    ax.set_ylim(y_max, y_min)
    ax.set_yticks(y_ticks)
    ax.set_yticklabels(y_labels, fontsize=9)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.grid(axis="x", linestyle="-", alpha=0.2)

    for i in range(1, len(ALL_DAYS)):
        ax.axvline(x=i, color="gray", linewidth=0.8, alpha=0.5)

    #legend_patches = [mpatches.Patch(color=color_map[c], label=c) for c in courses]
    #ax.legend(handles=legend_patches, loc="upper right", fontsize=8, framealpha=0.9)
    ax.set_title("Optimal Weekly Schedule", fontsize=14, fontweight="bold", pad=15)

    tba_sections = [s for s in sections if not parse_time(s["time"])[0]]
    if tba_sections:
        tba_text = "TBA: " + ", ".join(f"{s['course']} {s['section']}" for s in tba_sections)
        fig.text(0.5, 0.01, tba_text, ha="center", fontsize=8, color="gray", style="italic")

    plt.tight_layout()

    # Save to buffer instead of showing
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150)
    buf.seek(0)
    plt.close()
    return buf

# ─────────────────────────────────────────────
#  STREAMLIT UI
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  STREAMLIT UI
# ─────────────────────────────────────────────
st.set_page_config(page_title="Schedule Optimizer", page_icon="📅", layout="wide")
st.title("📅 Schedule Optimizer")
st.markdown("Automatically finds the best schedule with maximum free days and minimum break time.")
st.warning("⚠️ Important: This tool only finds the most optimized schedule for the courses you enter. You are responsible for enrolling in the courses.")

with st.form("schedule_form"):
    # Add input fields for User ID and Password
    user_id = st.text_input("University ID", placeholder="e.g. 4510163")
    password = st.text_input("Password", type="password", placeholder="Enter your password")
    
    user_input = st.text_input(
        "Course codes (comma separated)",
        placeholder="e.g. STAT 232, MATH 101, CS 141"
    )
    submitted = st.form_submit_button("🔍 Generate Schedule", use_container_width=True)
st.markdown(""" 💬Having a problem or a suggestion?  \n📧 **4510163@upm.edu.sa** """)

if submitted:
    # Check if ID and password were provided
    if not user_id or not password:
        st.error("Please enter both your University ID and Password.")
    elif not user_input:
        st.error("Please enter at least one course code.")
    else:
        wanted_courses = [c.strip().upper() for c in user_input.split(",")]

        with st.spinner("Finding courses data..."):
            try:
                all_sections = scrape(user_id, password)
            except Exception as e:
                # Clean, user-friendly error message hiding technical details
                error_trace = traceback.format_exc()
                st.error(f"Cloud Debug Error:\n\n{error_trace}")
                st.stop()

        with st.spinner("Finding best schedule..."):
            filtered = filter_courses(all_sections, wanted_courses)

            if not filtered:
                st.error("No courses found. Check your course codes and try again.")
                st.stop()

            grouped = group_sections(filtered)
            valid = find_valid_combinations(grouped)

            if not valid:
                st.error("No valid combinations found without conflicts.")
                st.stop()

            best = max(valid, key=score_combination)

        st.success(f"Found {len(valid)} valid combinations. Showing the best schedule!")

        # Show schedule image
        buf = draw_schedule(best)
        st.image(buf, use_column_width=True)

        # Show JSON details
        with st.expander("📋 View schedule details"):
            st.json(best)

        # Download button
        st.download_button(
            label="⬇️ Download Schedule Image",
            data=buf,
            file_name="schedule.png",
            mime="image/png"
        )
