"""
CAD Requests - the front door to the cad schedule (Google Sheet).

  New Request      PM fills in a short form -> a row goes into this month's tab under the date, Drw. by left blank
  Unallocated Jobs everything with no CAD tech yet -> pick who gets it, press Save
  Find a Job       every time a job number has been on the schedule, plus its Clarity folder

Needs (requirements.txt): streamlit, gspread, google-auth, pandas
Secrets (.streamlit/secrets.toml locally, or the app's Secrets box on Streamlit Cloud) - see SETUP.txt
"""
import datetime as dt
import glob
import os
import re
import urllib.parse

import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(HERE, "geetee_logo.png")
BRAND = "#D60C4D"                                  # GeeTee red, taken from the logo

st.set_page_config(page_title="CAD Requests | GeeTee", page_icon=LOGO if os.path.exists(LOGO) else None, layout="wide")


def page_style():
    """GeeTee header bar and brand colours."""
    import base64
    logo = ""
    if os.path.exists(LOGO):
        logo = base64.b64encode(open(LOGO, "rb").read()).decode()
    st.markdown(f"""
<style>
  #MainMenu, footer, [data-testid="stToolbar"], [data-testid="stHeader"], [data-testid="stDecoration"] {{ display: none; }}
  .block-container {{ padding-top: 1.6rem; max-width: 1250px; }}
  .gt-head {{ display: flex; align-items: center; gap: 22px; padding: 14px 4px 16px; border-bottom: 3px solid {BRAND};
              margin-bottom: 18px; }}
  .gt-head img {{ height: 64px; width: auto; }}
  .gt-head h1 {{ font-size: 1.85rem; font-weight: 700; margin: 0; padding: 0; letter-spacing: -0.01em; line-height: 1.15; }}
  .gt-head p {{ margin: 3px 0 0; opacity: .65; font-size: .95rem; }}
  .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {{
      background: {BRAND}; border-color: {BRAND}; color: #fff; font-weight: 600; }}
  .stButton > button[kind="primary"]:hover {{ background: #b80a42; border-color: #b80a42; color: #fff; }}
  .stButton > button[kind="primary"]:disabled {{ background: #e9a3ba; border-color: #e9a3ba; }}
  [data-testid="stMetricValue"] {{ color: {BRAND}; font-weight: 700; }}
  [role="radiogroup"] label p {{ font-weight: 600; }}
  [data-testid="stCaptionContainer"] {{ opacity: .8; }}
</style>
<div class="gt-head">
  {f'<img src="data:image/png;base64,{logo}" alt="GeeTee">' if logo else ''}
  <div><h1>CAD Requests</h1><p>Artwork requests straight onto the cad schedule</p></div>
</div>""", unsafe_allow_html=True)

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------
def _secret(key, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return default


SHEET_ID = _secret("sheet_id", "17yQp7muhaHA1LWMVrTfEpBiYvFyklW3VD115kUc5GfM")
CLARITY_ROOT = _secret("clarity_root", r"Z:\Clarity Job Documents")
CAD_TEAM = list(_secret("cad_team", ["DJ", "DM", "JA"]))
# Where a client's Clarity folder isn't named the same as on the schedule, e.g. {"Five Guys JV" = "Five Guys"}
CLIENT_FOLDERS = dict(_secret("client_folders", {}))

# Schedule columns (A..J): count, job no., client, site, req. by, time, Drw. by, Notes, Drw. No., Folder
C_JOB, C_CLIENT, C_SITE, C_PM, C_TIME, C_DRAWN, C_NOTES, C_DWG, C_FOLDER = 1, 2, 3, 4, 5, 6, 7, 8, 9
NCOLS = 10
DATE_HEAD = re.compile(r"^\s*(\d{1,2})[-\s]([A-Za-z]{3})[A-Za-z]*\s*$")
TAB_FORMATS = ("%B %Y", "%b %Y", "%B %y", "%b %y")


# --------------------------------------------------------------------------------------
# Pure helpers (no Google calls) - these are what the tests run against
# --------------------------------------------------------------------------------------
def tab_name(d):
    return f"{d:%B %Y}"


def tab_month(title):
    """'October 2026' / 'MAY 21' / 'APRIL 2022' -> date(…, 1) or None."""
    t = title.strip().title()
    for f in TAB_FORMATS:
        try:
            return dt.datetime.strptime(t, f).date().replace(day=1)
        except ValueError:
            pass
    return None


def pad(row, n=NCOLS):
    row = list(row) + [""] * n
    return [str(x) for x in row[:n]]


def is_blank(row):
    return not any(str(x).strip() for x in row)


def head_day(row, month):
    """The date a heading row stands for ('20-Oct' in the October tab), else None."""
    if not row or any(str(x).strip() for x in row[1:4]):
        return None
    m = DATE_HEAD.match(str(row[0]))
    if not m:
        return None
    try:
        d = dt.datetime.strptime(f"{m.group(1)} {m.group(2).title()} {month.year}", "%d %b %Y").date()
    except ValueError:
        return None
    return d if d.month == month.month else None


def is_job_row(row):
    """A real job line (not a date heading, 'JAY HOLIDAY' note or a totals line)."""
    r = pad(row)
    return bool(r[C_JOB].strip() or (r[C_SITE].strip() and r[C_PM].strip()))


def folder_path(job, client):
    client = CLIENT_FOLDERS.get(client.strip(), client.strip())
    return os.path.join(CLARITY_ROOT, client, str(job).strip()) if os.name == "nt" else f"{CLARITY_ROOT}\\{client}\\{str(job).strip()}"


def folder_formula(path):
    url = "file:///" + urllib.parse.quote(path.replace("\\", "/"), safe="/:")
    return f'=HYPERLINK("{url}","{path}")'


def where_to_put(values, d):
    """Row (1-based) for a new line under date d's heading, and whether it must be inserted (True) or can fill a
    blank row (False). Returns (None, None) if the date has no heading in this tab."""
    month = d.replace(day=1)
    start = None
    for i, row in enumerate(values):
        if head_day(row, month) == d:
            start = i
            break
    if start is None:
        return None, None
    i = start + 1
    while i < len(values) and not is_blank(values[i]) and head_day(values[i], month) is None:
        i += 1
    # i = first blank row / next heading / end of sheet. Keep one blank line between days.
    if i >= len(values):
        return i + 1, False
    if is_blank(values[i]) and i + 1 < len(values) and is_blank(values[i + 1]):
        return i + 1, False                       # two blanks: use the first, the second stays as the gap
    return i + 1, True                            # insert here, everything below moves down


def build_row(job, client, site, pm, hours, notes, folder):
    r = [""] * NCOLS
    r[C_JOB], r[C_CLIENT], r[C_SITE], r[C_PM] = str(job).strip(), client.strip(), site.strip(), pm.strip().upper()
    r[C_TIME] = (f"{hours:g}" if isinstance(hours, (int, float)) and hours else str(hours or "")).strip()
    r[C_NOTES] = notes.strip()
    r[C_FOLDER] = folder_formula(folder) if folder else ""
    return r


def history_from(tabs):
    """tabs = {title: values} -> one dict per job line, oldest first."""
    out = []
    for title, values in tabs.items():
        month = tab_month(title)
        if not month:
            continue
        day = None
        for i, row in enumerate(values):
            hd = head_day(row, month)
            if hd:
                day = hd
                continue
            if not is_job_row(row):
                continue
            r = pad(row)
            out.append({"Date": day or month, "Tab": title, "Row": i + 1, "Job": r[C_JOB].strip(), "Client": r[C_CLIENT].strip(),
                        "Site": r[C_SITE].strip(), "PM": r[C_PM].strip(), "Hrs": r[C_TIME].strip(), "Drw. by": r[C_DRAWN].strip(),
                        "Notes": r[C_NOTES].strip(), "Drw. No.": r[C_DWG].strip()})
    out.sort(key=lambda x: (x["Date"], x["Tab"], x["Row"]))
    return out


def unallocated_from(title, values):
    month = tab_month(title)
    day, out = None, []
    for i, row in enumerate(values):
        hd = head_day(row, month) if month else None
        if hd:
            day = hd
            continue
        r = pad(row)
        if is_job_row(r) and not r[C_DRAWN].strip():
            out.append({"Tab": title, "Row": i + 1, "Date": day, "Job": r[C_JOB].strip(), "Client": r[C_CLIENT].strip(),
                        "Site": r[C_SITE].strip(), "PM": r[C_PM].strip(), "Hrs": r[C_TIME].strip(), "Notes": r[C_NOTES].strip()})
    return out


# --------------------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def book():
    import gspread
    import json
    raw = _secret("gcp_json")                    # easiest: the whole downloaded .json pasted in Secrets as gcp_json
    creds = json.loads(raw) if raw else _secret("gcp_service_account")
    if not creds:
        raise RuntimeError("No Google key set up yet - see SETUP.txt (gcp_json in the app's Secrets).")
    creds = dict(creds)
    key = str(creds.get("private_key", ""))
    if "..." in key or "BEGIN PRIVATE KEY" not in key:
        raise RuntimeError("The private_key in Secrets is still the example text - paste the real key from the .json file "
                           "(easiest: paste the whole .json as gcp_json, see SETUP.txt).")
    creds["private_key"] = key.replace("\\n", "\n")         # key pasted with literal \n's
    return gspread.service_account_from_dict(creds).open_by_key(SHEET_ID)


def ws(title):
    for w in book().worksheets():
        if w.title.strip().lower() == title.lower():
            return w
    return None


@st.cache_data(ttl=600, show_spinner="Reading the whole schedule...")
def all_history():
    b = book()
    titles = [w.title for w in b.worksheets() if tab_month(w.title)]
    res = b.values_batch_get([f"'{t}'!A1:J" for t in titles]).get("valueRanges", [])
    tabs = {t: r.get("values", []) for t, r in zip(titles, res)}
    return history_from(tabs)


def latest_for(job):
    job = str(job).strip()
    hits = [h for h in all_history() if h["Job"] == job]
    return hits[-1] if hits else None


def add_request(d, row):
    w = ws(tab_name(d))
    if w is None:
        raise RuntimeError(f"There's no '{tab_name(d)}' tab on the schedule yet - make the month tab first.")
    values = w.get_all_values()
    if values and not pad(values[0])[C_FOLDER].strip():
        w.update(range_name="J1", values=[["Folder"]])
    at = None
    for k in range(7):                       # a weekend / missing date goes under the next date heading in the tab
        day = d + dt.timedelta(days=k)
        if day.month != d.month:
            break
        at, insert = where_to_put(values, day)
        if at is not None:
            break
    if at is None:
        raise RuntimeError(f"Couldn't find the {d.day}-{d:%b} heading (or a later one that week) in '{w.title}'. Add the date to the tab first.")
    if insert:
        w.insert_row(row, index=at, value_input_option="USER_ENTERED", inherit_from_before=True)
    else:
        w.update(range_name=f"A{at}:J{at}", values=[row], value_input_option="USER_ENTERED")
    return w.title, at, day


def allocate(items):
    """items = [(tab, row, job, site, tech)] -> writes Drw. by, after checking each row is still that job."""
    done, skipped = [], []
    by_tab = {}
    for it in items:
        by_tab.setdefault(it[0], []).append(it)
    for tab, its in by_tab.items():
        w = ws(tab)
        values = w.get_all_values()
        for _, row, job, site, tech in its:
            r = pad(values[row - 1]) if row - 1 < len(values) else pad([])
            target = row
            if not (r[C_JOB].strip() == job and r[C_SITE].strip() == site and not r[C_DRAWN].strip()):
                # rows have moved (someone added a line) - find it again
                target = next((i + 1 for i, x in enumerate(values) if pad(x)[C_JOB].strip() == job
                               and pad(x)[C_SITE].strip() == site and not pad(x)[C_DRAWN].strip()), None)
            if target is None:
                skipped.append(f"{job} {site}")
                continue
            w.update(range_name=f"G{target}", values=[[tech]])
            values[target - 1] = pad(values[target - 1])
            values[target - 1][C_DRAWN] = tech
            done.append(f"{job} {site} -> {tech}")
    return done, skipped


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------
def open_folder_button(path, key):
    """Opens the folder in Explorer when the app is running on an office PC that can see Z:."""
    if os.name == "nt" and os.path.isdir(path):
        if st.button("Open folder", key=key):
            os.startfile(path)
        return True
    return False


def find_local_folder(job, client):
    """On an office PC: look for the real Clarity folder for this job (client folder first, then every client)."""
    if os.name != "nt" or not os.path.isdir(CLARITY_ROOT):
        return None
    guess = folder_path(job, client)
    if os.path.isdir(guess):
        return guess
    hits = glob.glob(os.path.join(CLARITY_ROOT, "*", f"{job}*"))
    return hits[0] if hits else None


def new_request():
    with st.container(border=True):
        _new_request()


def _new_request():
    today = dt.date.today()
    c0, c1 = st.columns([1, 3])
    job = c0.text_input("Clarity job no.", key="nr_job", placeholder="e.g. 14973").strip()
    prev = latest_for(job) if re.fullmatch(r"\d{4,6}", job or "") else None
    if job and prev:
        c1.success(f"Last on the schedule {prev['Date']:%d/%m/%y}: {prev['Client']} - {prev['Site']} (PM {prev['PM']}, drawn by "
                   f"{prev['Drw. by'] or 'nobody yet'}). Filled in below - change anything that's different.")
    elif job:
        c1.info("New job number - not on the schedule before. Fill in the client and site.")
    k = job or "new"
    a1, a2, a3 = st.columns(3)
    client = a1.text_input("Client", value=prev["Client"] if prev else "", key=f"nr_client_{k}")
    site = a2.text_input("Site / project", value=prev["Site"] if prev else "", key=f"nr_site_{k}")
    pm = a3.text_input("Requested by (your initials)", value=prev["PM"] if prev else "", key=f"nr_pm_{k}", max_chars=4)
    b1, b2, b3 = st.columns(3)
    hours = b1.number_input("Estimated CAD time (hours)", 0.0, 200.0, 0.0, 0.5, key="nr_hrs")
    need = b2.date_input("Required by", value=None, min_value=today - dt.timedelta(days=7), key="nr_need", format="DD/MM/YYYY")
    day = b3.date_input("Put it on the schedule under", value=today, key="nr_day", format="DD/MM/YYYY")
    details = st.text_area("What's needed", key="nr_details", height=90,
                           placeholder="e.g. Amend wayfinding package in line with client comments received today")
    how = st.radio("Request came by", ["Email request", "Verbal instruction", "Form only"], horizontal=True, key="nr_how")
    folder = folder_path(job, client) if job and client else ""
    if folder:
        st.caption(f"Clarity folder: `{folder}`")
    notes = " - ".join(x for x in (how if how != "Form only" else "", details.strip(),
                                   f"Req. by {need:%a %d/%m}" if need else "") if x)
    missing = [n for n, v in (("job no. or site", job or site), ("client", client), ("your initials", pm), ("what's needed", details))
               if not str(v).strip()]
    if st.button("Submit request", type="primary", key="nr_go", disabled=bool(missing)):
        row = build_row(job, client, site, pm, hours, notes, folder)
        try:
            tab, at, used = add_request(day, row)
        except Exception as e:
            st.error(str(e))
            return
        all_history.clear()
        st.success(f"Added to '{tab}' row {at}, under {used:%d-%b}. Drw. by is left blank - it'll show on Unallocated Jobs.")
        st.balloons()
    elif missing:
        st.caption("Still needed: " + ", ".join(missing))


def unallocated():
    today = dt.date.today()
    prev_month = (today.replace(day=1) - dt.timedelta(days=1))
    rows = []
    for t in (tab_name(prev_month), tab_name(today)):
        w = ws(t)
        if w is not None:
            rows += unallocated_from(w.title, w.get_all_values())
    if st.button("Refresh", key="ua_ref"):
        st.rerun()
    if not rows:
        st.success("Nothing waiting - every job on this month's and last month's schedule has a CAD tech.")
        return
    df = pd.DataFrame(rows)
    df["Date"] = df["Date"].apply(lambda d: f"{d:%a %d-%b}" if d else "")
    df.insert(0, "Give to", None)
    st.metric("Jobs waiting for a CAD tech", len(df))
    ed = st.data_editor(df, hide_index=True, use_container_width=True, key="ua_ed",
                        column_config={"Give to": st.column_config.SelectboxColumn("Give to", options=CAD_TEAM, width="small"),
                                       "Tab": None, "Row": None},
                        disabled=[c for c in df.columns if c != "Give to"])
    picks = ed[ed["Give to"].notna()]
    if st.button(f"Save ({len(picks)})", type="primary", disabled=picks.empty, key="ua_save"):
        done, skipped = allocate([(r["Tab"], int(r["Row"]), r["Job"], r["Site"], r["Give to"]) for _, r in picks.iterrows()])
        all_history.clear()
        if done:
            st.success("Saved: " + "; ".join(done))
        if skipped:
            st.warning("Couldn't find these any more (moved or already given out): " + "; ".join(skipped))


def find_job():
    q = st.text_input("Job number, client or site", key="fj_q", placeholder="14973, Next, Braintree...").strip()
    if not q:
        st.info("Type a job number to see every time it's been on the schedule, who drew it, and its Clarity folder.")
        return
    hist = all_history()
    if re.fullmatch(r"\d{4,6}", q):
        hits = [h for h in hist if h["Job"] == q]
    else:
        ql = q.lower()
        hits = [h for h in hist if ql in h["Client"].lower() or ql in h["Site"].lower()]
    if not hits:
        st.warning("Not on the schedule.")
        return
    last = hits[-1]
    if re.fullmatch(r"\d{4,6}", q):
        st.subheader(f"{q} - {last['Client']} - {last['Site']}")
        path = find_local_folder(q, last["Client"]) or folder_path(q, last["Client"])
        st.code(path, language=None)
        if not open_folder_button(path, "fj_open"):
            st.caption("Copy the path into the Explorer address bar. (Running the app on an office PC gives you an Open folder button.)")
        drawn = sorted({h["Drw. by"] for h in hits if h["Drw. by"]})
        hrs = sum(float(h["Hrs"]) for h in hits if re.fullmatch(r"\d+(\.\d+)?", h["Hrs"]))
        m1, m2, m3 = st.columns(3)
        m1.metric("Days on the schedule", len({h["Date"] for h in hits}))
        m2.metric("Drawn by", ", ".join(drawn) or "-")
        m3.metric("Hours logged", f"{hrs:g}")
    df = pd.DataFrame(hits[::-1][:300])
    df["Date"] = df["Date"].apply(lambda d: f"{d:%d/%m/%y}")
    st.dataframe(df.drop(columns=["Tab", "Row"]), hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------
pw = str(_secret("app_password", "") or "").strip()
if pw and st.session_state.get("ok") != pw:
    page_style()
    with st.form("login"):
        typed = st.text_input("Password", type="password")
        go = st.form_submit_button("Log in", type="primary")
    if go:
        if typed.strip() == pw:
            st.session_state["ok"] = pw
            st.rerun()
        st.error("Wrong password - it's the app_password set in the app's Secrets.")
    st.stop()

page_style()
TOOL_HELP = {
    "New Request": "Ask the CAD team for artwork",
    "Unallocated Jobs": "Give waiting jobs to a CAD tech",
    "Find a Job": "Job history and Clarity folder",
}
TOOL_HOWTO = {
    "New Request": [
        "Type the Clarity job number. If it's been on the schedule before, the client, site and PM fill themselves in.",
        "Add the estimated hours, the date you need it by and what's needed, then press Submit request.",
        "It goes into this month's tab under the date you pick (today unless you change it), at the bottom of that day, "
        "with Drw. by left blank and a link to the Clarity folder in the Folder column.",
        "No job number yet? Leave it blank and fill in the site instead.",
    ],
    "Unallocated Jobs": [
        "Lists every line on this month's and last month's tabs with a PM but no Drw. by.",
        "Pick a name in the 'Give to' column for as many as you like, then press Save - it writes their initials into Drw. by.",
        "If someone's added lines since you opened the page, it finds the job again before writing, so nothing lands on the wrong row.",
    ],
    "Find a Job": [
        "Type a job number for its full history on the schedule, or a client / site name to search.",
        "The Clarity folder path is shown to copy into Explorer. Run the app on an office PC and you get an Open folder button.",
    ],
}
tool = st.radio("Tool", list(TOOL_HELP), horizontal=True, key="tool", label_visibility="collapsed", captions=list(TOOL_HELP.values()))
with st.expander(f"How to use: {tool}", expanded=False):
    for line in TOOL_HOWTO[tool]:
        st.markdown(f"- {line}")
try:
    book()
except Exception as e:
    st.error(f"Can't open the cad schedule: {e}")
    st.stop()
if tool == "New Request":
    new_request()
elif tool == "Unallocated Jobs":
    unallocated()
else:
    find_job()
