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
def job_no(t):
    """'C14973', 'c 14973', 'C-14973' -> '14973' (Clarity puts a C in front; the schedule doesn't)."""
    t = str(t or "").strip()
    m = re.fullmatch(r"[Cc]\s*-?\s*(\d{3,7})", t)
    return m.group(1) if m else t


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


def build_row(job, client, site, pm, hours, notes, folder=""):
    """Columns A-I only - nothing is written past Drw. No."""
    r = [""] * (C_DWG + 1)
    r[C_JOB], r[C_CLIENT], r[C_SITE], r[C_PM] = str(job).strip(), client.strip(), site.strip(), pm.strip().upper()
    r[C_TIME] = (f"{hours:g}" if isinstance(hours, (int, float)) and hours else str(hours or "")).strip()
    r[C_NOTES] = notes.strip()
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
    creds = None
    if raw:
        raw = str(raw).strip().lstrip("\ufeff")
        i, j = raw.find("{"), raw.rfind("}")
        if i < 0 or j < i:
            raise RuntimeError("gcp_json in Secrets hasn't got the key file in it - it should start with {  and end with }. "
                               "Open the downloaded .json in Notepad, Ctrl+A, Ctrl+C, and paste it between the three-quote marks.")
        try:
            creds = json.loads(raw[i:j + 1])
        except ValueError as e:
            raise RuntimeError(f"The key pasted in gcp_json isn't complete ({e}). Paste the whole .json again - all of it, "
                               "from the first {  to the last }.")
    else:
        creds = _secret("gcp_service_account")
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


RED = {"red": 0.86, "green": 0.0, "blue": 0.0}
BLACK = {"red": 0.0, "green": 0.0, "blue": 0.0}


def add_request(d, row, urgent=False):
    w = ws(tab_name(d))
    if w is None:
        raise RuntimeError(f"There's no '{tab_name(d)}' tab on the schedule yet - make the month tab first.")
    values = w.get_all_values()
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
        w.update(range_name=f"A{at}:I{at}", values=[row], value_input_option="USER_ENTERED")
    try:                                     # URGENT = whole line in red; otherwise make sure it isn't red from the line above
        w.format(f"A{at}:I{at}", {"textFormat": {"foregroundColor": RED if urgent else BLACK, "bold": bool(urgent)}})
    except Exception:
        pass
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
# Saved requests: every request is also kept on a 'Requests Log' tab (the schedule rows get edited and moved,
# the log never changes), and any of them can be downloaded as a one-page request sheet.
# --------------------------------------------------------------------------------------
LOG_TAB = "Requests Log"
LOG_COLS = ["Ref", "Submitted", "Job No", "Client", "Site", "Requested by", "Hours", "Required by", "On schedule",
            "Urgent", "What's needed", "Clarity folder", "Schedule tab", "Schedule row"]


def uk_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/London"))
    except Exception:
        return dt.datetime.now()


def log_ws():
    w = ws(LOG_TAB)
    if w is None:
        w = book().add_worksheet(title=LOG_TAB, rows=1000, cols=len(LOG_COLS))
        w.update(range_name="A1", values=[LOG_COLS])
    return w


def log_request(rec):
    w = log_ws()
    head = [h for h in (w.get_all_values() or [[]])[0] if str(h).strip()]
    extra = [c for c in LOG_COLS if c not in head]
    if extra:                                    # older log tab - add any new columns to the end of the header
        head += extra
        w.update(range_name="A1", values=[head])
    w.append_row([str(rec.get(c, "")) for c in head], value_input_option="RAW")
    saved_requests.clear()


@st.cache_data(ttl=120, show_spinner=False)
def saved_requests():
    w = ws(LOG_TAB)
    if w is None:
        return []
    v = w.get_all_values()
    if not v:
        return []
    head = v[0]
    return [dict(zip(head, pad(r, len(head)))) for r in v[1:] if any(str(x).strip() for x in r)]


def request_status(rec, hist):
    """Who the request went to, from the schedule (the row under that date with the same job / site)."""
    job, site = rec.get("Job No", "").strip(), rec.get("Site", "").strip()
    try:
        day = dt.datetime.strptime(rec.get("On schedule", ""), "%d/%m/%Y").date()
    except ValueError:
        day = None
    rows = [h for h in hist if (h["Job"] == job if job else h["Site"] == site) and (day is None or h["Date"] >= day)]
    det = rec.get("What's needed", "").strip()[:40]
    exact = [h for h in rows if det and det in h["Notes"]]       # the very line this request made
    if exact:
        rows = exact
    drawn = [h["Drw. by"] for h in rows if h["Drw. by"]]
    return f"Given to {drawn[0]}" if drawn else ("Waiting" if rows else "Not found on schedule")


def has_pdf():
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def pdf_button(where, label, rec, key, **kw):
    """PDF download, or a note if reportlab isn't installed (needs 'reportlab' in requirements.txt)."""
    if has_pdf():
        where.download_button(label, request_pdf(rec), f"Artwork request {rec.get('Ref')} {rec.get('Job No')}.pdf",
                              "application/pdf", key=key, **kw)
    else:
        where.caption("PDF request sheet: add a line saying reportlab to requirements.txt on GitHub.")


def request_pdf(rec):
    """One A4 page per request - for the job file / printing."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Artwork request {rec.get('Ref', '')}")
    red = colors.HexColor(BRAND)
    h = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=20, leading=24)
    sub = ParagraphStyle("s", fontName="Helvetica", fontSize=10, leading=13, textColor=colors.HexColor("#666666"))
    lab = ParagraphStyle("l", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=colors.HexColor("#444444"))
    val = ParagraphStyle("v", fontName="Helvetica", fontSize=10.5, leading=14)
    head_cells = [[Paragraph("Artwork Request", h), ""],
                  [Paragraph(f"{escape(rec.get('Ref', ''))} &nbsp;&middot;&nbsp; submitted {escape(rec.get('Submitted', ''))}", sub), ""]]
    if os.path.exists(LOGO):
        head_cells[0][1] = Image(LOGO, width=38 * mm, height=38 * mm * 343 / 690)
    top = Table(head_cells, colWidths=[124 * mm, 50 * mm])
    top.setStyle(TableStyle([("SPAN", (1, 0), (1, 1)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 1), "RIGHT"),
                             ("LINEBELOW", (0, 1), (-1, 1), 2, red), ("BOTTOMPADDING", (0, 1), (-1, 1), 8)]))
    fields = ["Job No", "Client", "Site", "Requested by", "Hours", "Required by", "On schedule", "Urgent", "What's needed",
              "Clarity folder"]
    names = {"Job No": "Clarity job no.", "Site": "Site / project", "Hours": "Estimated CAD time (hours)",
             "On schedule": "On the cad schedule under"}
    data = [[Paragraph(names.get(f, f), lab), Paragraph(escape(rec.get(f, "") or "-").replace("\n", "<br/>"), val)] for f in fields]
    t = Table(data, colWidths=[52 * mm, 122 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    sign = Table([[Paragraph("CAD technician", lab), "", Paragraph("Date completed", lab), ""]],
                 colWidths=[34 * mm, 54 * mm, 34 * mm, 52 * mm], rowHeights=[14 * mm])
    sign.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.6, colors.black), ("LINEBELOW", (3, 0), (3, 0), 0.6, colors.black),
                              ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    doc.build([top, Spacer(1, 8 * mm), t, Spacer(1, 14 * mm), sign])
    return buf.getvalue()


def saved_screen():
    recs = saved_requests()
    if not recs:
        st.info("No requests saved yet. Every request made on New Request is kept here (and on the 'Requests Log' tab of the schedule).")
        return
    q = st.text_input("Search", key="sv_q", placeholder="Job no., client, site, initials...").strip().lower()
    if q:
        recs = [r for r in recs if any(q in str(v).lower() for v in r.values())]
    recs = recs[::-1]
    hist = all_history()
    df = pd.DataFrame([{"Ref": r.get("Ref"), "Submitted": r.get("Submitted"), "Job No": r.get("Job No"), "Client": r.get("Client"),
                        "Site": r.get("Site"), "By": r.get("Requested by"), "Hours": r.get("Hours"), "Required by": r.get("Required by"),
                        "Urgent": r.get("Urgent", ""), "Status": request_status(r, hist), "What's needed": r.get("What's needed")} for r in recs])
    st.caption(f"{len(df)} request(s), newest first.")
    st.dataframe(df, hide_index=True, use_container_width=True)
    if not recs:
        return
    pick = st.selectbox("Open a request", range(len(recs)), key="sv_pick",
                        format_func=lambda i: f"{recs[i].get('Ref')}  -  {recs[i].get('Job No') or '-'}  {recs[i].get('Client')}  "
                                              f"{recs[i].get('Site')}")
    r = recs[pick]
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        c1.markdown(f"**{r.get('Job No') or ''} {r.get('Client')} - {r.get('Site')}**  \n"
                    f"Requested by {r.get('Requested by')} on {r.get('Submitted')}  \n"
                    f"Hours: {r.get('Hours') or '-'} · Required by: {r.get('Required by') or '-'} · {request_status(r, hist)}")
        needed = r.get("What's needed", "")
        c1.markdown(f"> {needed}")
        if r.get("Clarity folder"):
            c1.code(r["Clarity folder"], language=None)
        pdf_button(c2, "Download request sheet (PDF)", r, "sv_pdf", type="primary")


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


def logo_party():
    """GeeTee logos floating up the screen instead of balloons."""
    import base64
    import random
    if not os.path.exists(LOGO):
        st.balloons()
        return
    src = "data:image/png;base64," + base64.b64encode(open(LOGO, "rb").read()).decode()
    rnd = random.Random()
    imgs = "".join(
        f'<img src="{src}" style="left:{rnd.uniform(0, 92):.1f}vw; width:{rnd.randint(70, 150)}px; '
        f'animation-delay:{rnd.uniform(0, 1.6):.2f}s; animation-duration:{rnd.uniform(2.8, 4.4):.2f}s; '
        f'--sway:{rnd.choice([-1, 1]) * rnd.randint(20, 70)}px; --spin:{rnd.randint(-18, 18)}deg;">'
        for _ in range(26))
    st.markdown(f"""
<style>
  .gt-party {{ position: fixed; inset: 0; pointer-events: none; z-index: 99999; overflow: hidden; }}
  .gt-party img {{ position: absolute; bottom: -180px; opacity: 0; filter: drop-shadow(0 4px 6px rgba(0,0,0,.18));
                   background: #fff; border-radius: 10px; animation-name: gtRise; animation-timing-function: ease-out;
                   animation-fill-mode: forwards; }}
  @keyframes gtRise {{
    0%   {{ transform: translate(0, 0) rotate(0deg); opacity: 0; }}
    10%  {{ opacity: 1; }}
    50%  {{ transform: translate(var(--sway), -60vh) rotate(var(--spin)); opacity: 1; }}
    100% {{ transform: translate(calc(var(--sway) * -0.5), -125vh) rotate(calc(var(--spin) * -1)); opacity: 0; }}
  }}
</style>
<div class="gt-party">{imgs}</div>""", unsafe_allow_html=True)


def clear_form():
    for k in [k for k in st.session_state if k.startswith("nr_")]:
        del st.session_state[k]


def new_request():
    done = st.session_state.get("nr_done")
    if done:
        with st.container(border=True):
            if not st.session_state.get("nr_party"):
                st.session_state["nr_party"] = True
                logo_party()
            if done.get("Urgent"):
                st.error("URGENT - it's in red on the schedule.")
            st.success(f"Request {done['Ref']} sent - it's on the cad schedule ('{done['Schedule tab']}', under "
                       f"{done['On schedule'][:5].replace('/', '-')}) with Drw. by blank, and saved in Saved Requests.")
            needed = done.get("What's needed", "")
            st.markdown(f"**{done['Job No']} {done['Client']} - {done['Site']}**  \n{needed}")
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("New request", type="primary", key="nr_again", use_container_width=True):
                clear_form()
                st.rerun()
            pdf_button(b2, "Request sheet (PDF)", done, "nr_pdf", use_container_width=True)
        return
    with st.container(border=True):
        _new_request()
        st.button("Clear form", key="nr_clear", on_click=clear_form)


def _new_request():
    today = dt.date.today()
    c0, c1 = st.columns([1, 3])
    job = job_no(c0.text_input("Clarity job no.", key="nr_job", placeholder="e.g. 14973 (or C14973)"))
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
    urgent = st.toggle("URGENT - puts the whole line on the schedule in red", key="nr_urgent")
    folder = folder_path(job, client) if job and client else ""
    if folder:
        st.caption(f"Clarity folder: `{folder}`")
    notes = details.strip()
    missing = [n for n, v in (("job no. or site", job or site), ("client", client), ("your initials", pm), ("what's needed", details))
               if not str(v).strip()]
    if st.button("Submit request", type="primary", key="nr_go", disabled=bool(missing)):
        row = build_row(job, client, site, pm, hours, notes, folder)
        try:
            tab, at, used = add_request(day, row, urgent)
        except Exception as e:
            st.error(str(e))
            return
        all_history.clear()
        now = uk_now()
        try:
            n_today = sum(1 for r in saved_requests() if str(r.get("Ref", "")).startswith(f"REQ-{now:%y%m%d}"))
        except Exception:
            n_today = 0
        rec = {"Ref": f"REQ-{now:%y%m%d}-{n_today + 1:02d}", "Submitted": f"{now:%d/%m/%Y %H:%M}", "Job No": job, "Client": client.strip(),
               "Site": site.strip(), "Requested by": pm.strip().upper(), "Hours": f"{hours:g}" if hours else "",
               "Required by": f"{need:%d/%m/%Y}" if need else "", "On schedule": f"{used:%d/%m/%Y}", "Urgent": "URGENT" if urgent else "",
               "What's needed": details.strip(), "Clarity folder": folder, "Schedule tab": tab, "Schedule row": at}
        try:
            log_request(rec)
        except Exception as e:
            st.warning(f"It's on the schedule, but couldn't be saved to the Requests Log: {e}")
        st.session_state["nr_done"] = rec
        st.rerun()
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
    if re.fullmatch(r"[Cc]\s*-?\s*\d{4,6}", q):
        q = job_no(q)
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
_tok = __import__("hashlib").sha256(("cadreq:" + pw).encode()).hexdigest()[:16] if pw else ""
if pw and st.query_params.get("k") == _tok:               # remembered in the page address - refresh doesn't log you out
    st.session_state["ok"] = pw
if pw and st.session_state.get("ok") != pw:
    page_style()
    with st.form("login"):
        typed = st.text_input("Password", type="password")
        go = st.form_submit_button("Log in", type="primary")
    if go:
        if typed.strip() == pw:
            st.session_state["ok"] = pw
            st.query_params["k"] = _tok
            st.rerun()
        st.error("Wrong password - it's the app_password set in the app's Secrets.")
    st.stop()

page_style()
TOOL_HELP = {
    "New Request": "Ask the CAD team for artwork",
    "Unallocated Jobs": "Give waiting jobs to a CAD tech",
    "Find a Job": "Job history and Clarity folder",
    "Saved Requests": "Every request made, for reference",
}
TOOL_HOWTO = {
    "New Request": [
        "Type the Clarity job number. If it's been on the schedule before, the client, site and PM fill themselves in.",
        "Add the estimated hours, the date you need it by and what's needed, then press Submit request.",
        "It goes into this month's tab under the date you pick (today unless you change it), at the bottom of that day, "
        "with Drw. by left blank. Tick URGENT and the whole line goes in red.",
        "No job number yet? Leave it blank and fill in the site instead.",
    ],
    "Unallocated Jobs": [
        "Lists every line on this month's and last month's tabs with a PM but no Drw. by.",
        "Pick a name in the 'Give to' column for as many as you like, then press Save - it writes their initials into Drw. by.",
        "If someone's added lines since you opened the page, it finds the job again before writing, so nothing lands on the wrong row.",
    ],
    "Saved Requests": [
        "Every request made through New Request is kept here, newest first, with who it was given to from the schedule.",
        "Search by job number, client, site or initials. Open one to see the full request and download it as a one-page PDF "
        "request sheet (with the GeeTee logo) for the job file.",
        "They're stored on a 'Requests Log' tab in the cad schedule, so nothing is lost when the app restarts. Don't edit that tab by hand.",
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
    msg, name = str(e).strip(), type(e).__name__
    robot = ""
    try:
        import json as _j
        raw = str(_secret("gcp_json") or "")
        robot = _j.loads(raw[raw.find("{"):raw.rfind("}") + 1]).get("client_email", "")
    except Exception:
        pass
    if name in ("PermissionError", "SpreadsheetNotFound") or "403" in msg or "404" in msg:
        st.error("The key works, but Google won't let the app into the cad schedule. Open the cad schedule > Share, add "
                 f"**{robot or 'the client_email from the .json file'}** as an **Editor**, untick Notify, press Share - "
                 "then refresh this page.")
    elif "has not been used" in msg or "is disabled" in msg or "SERVICE_DISABLED" in msg:
        st.error("The Google Sheets API isn't switched on for the key's project. In console.cloud.google.com search "
                 "'Google Sheets API' > Enable (and 'Google Drive API' > Enable), wait a minute, then refresh.")
    else:
        st.error(f"Can't open the cad schedule: {msg or name}")
    st.stop()
if tool == "New Request":
    new_request()
elif tool == "Unallocated Jobs":
    unallocated()
elif tool == "Saved Requests":
    saved_screen()
else:
    find_job()
